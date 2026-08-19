"""
Phase A pure-function worker for ProcessPoolExecutor.

Architecture (plan decision #3):
- main process: prefetches Routine.body batches from Neo4j (ordered
  by (rel_path, routine_id)), assigns stable routine_ordinal, packs
  work batches under min(WORK_BATCH_ROUTINES, WORK_BATCH_MAX_MB), and
  submits them via ProcessPoolExecutor.map(...) — map preserves input
  order, so main can stream results and flush per-module aggregates on
  rel_path boundary.
- worker process (this module): runs split_routine + tokenization +
  structural extraction on each routine. Returns a pickle-able payload
  with unit rows, method rows, per-routine IDF / stats delta records,
  and a module fragment per routine.
  Worker has NO Neo4j driver and NO SQLite connection — only stdlib
  + tree-sitter via bsl_code_split.

Two-tokenizer contract (plan decision #6):
- FTS payloads (bsl_code_units_fts, bsl_code_body_fts, structural FTS,
  module FTS) go through the base `tokenize()` profile, like the
  existing `_token_text` / `_token_join` helpers — BSL keywords such
  as "возврат" / "экспорт" must survive for the RLM scorer.
- IDF token counts for `_doc` and `body` field_kind, and for all
  metadata field_kinds, are computed with `tokenize_1c_light()`.
  The hybrid scorer matches on the same `tokenize_1c_light` tokens
  ([bsl_code_search_service._apply_hybrid_blend]).
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .bsl_code_compress import compress_unit  # noqa: F401  (kept for Phase B)
from .bsl_code_scorers import tokenize, tokenize_1c_light
from .bsl_code_search_policy import is_regulated_report as _is_regulated_report
from .bsl_code_split import slice_body, split_routine


logger = logging.getLogger(__name__)

DOC_FIELD_KIND = "_doc"
FIELD_KINDS: Tuple[str, ...] = ("symbol", "object", "form", "metadata_type", "path")

STRUCTURAL_FTS_TABLES: Tuple[str, ...] = (
    "bsl_code_body_fts",
    "bsl_code_feature_fts",
    "bsl_code_structural_fts",
    "bsl_code_metadata_refs_fts",
    "bsl_code_query_tables_fts",
    "bsl_code_method_calls_fts",
    "bsl_code_string_literals_fts",
    "bsl_code_assignments_fts",
    "bsl_code_identifiers_fts",
)

# CONTRIBUTION-AFFECTING CONSTANTS. Меняют вклад routine в corpus_idf/corpus_stats,
# но НЕ входят в `_compute_config_fingerprint` напрямую — их покрывает
# `bsl_code_units_version`. Изменение этих весов (как и `_build_search_text` /
# `_unit_context` / `_build_fields` ниже) обязано сопровождаться повышением
# `BSL_CODE_UNITS_VERSION`: иначе обратное вычитание обоих scoped-путей —
# routine delta и config delete — посчитает вклад по новой формуле и вычтет
# не то, что было добавлено старой.
_METADATA_WEIGHT = 6
_METADATA_SYMBOL_WEIGHT = 1.0

_MODULE_TYPE_TO_KIND: Dict[str, str] = {
    "ObjectModule": "module_object",
    "ManagerModule": "module_manager",
    "FormModule": "module_form",
    "CommandModule": "module_command",
    "RecordSetModule": "module_recordset",
    "ValueManagerModule": "module_valuemanager",
    "ExternalConnectionModule": "module_externalconn",
    "OrdinaryApplicationModule": "module_ordinaryapp",
    "ManagedApplicationModule": "module_managedapp",
    "SessionModule": "module_session",
    "CommonModule": "module_common",
}

_REGION_RE = re.compile(r"#Область\s+([^\r\n]+)", re.IGNORECASE)
_COMMENT_RE = re.compile(r"//\s*([^\r\n]+)")
_HEADER_RE = re.compile(
    r"(?:Процедура|Функция)\s+([^\r\n(]+)\s*\(", re.IGNORECASE
)


# ---------------------------------------------------------------------- helpers

def _token_text(value: str) -> str:
    """Base-tokenizer join — same contract as bsl_code_indexer._token_text."""
    return " ".join(tokenize(value or ""))


def _token_join(values: Sequence[str]) -> str:
    joined = "\n".join(str(v) for v in (values or []) if v)
    return _token_text(joined)


def _parse_owner_qn(owner_qn: Optional[str]) -> Tuple[str, str, str]:
    """
    Minimal port of graphdb.metadata.parse_owner_qn restricted to what
    the worker needs: returns (metadata_type_ru, object_name, form_name).
    Caller normalizes ConfigurationModule via rel_path stem.
    """
    if not owner_qn:
        return "", "", ""
    parts = [p for p in owner_qn.split("/") if p]
    if len(parts) < 2:
        return "", "", ""
    # parts[0] = project, parts[1] = config_name OR meta type marker
    # The actual production parse_owner_qn lives in metadata.py and is
    # complex; here we accept the common encodings used by the indexer
    # (the full parser stays the single source of truth in production —
    # this worker fork only matches what was produced by it, see how
    # main process passes already-parsed values via UnitContext below).
    meta_type = parts[2] if len(parts) > 2 else ""
    object_name = parts[3] if len(parts) > 3 else ""
    form_name = ""
    if "Forms" in parts:
        try:
            i = parts.index("Forms")
            if i + 1 < len(parts):
                form_name = parts[i + 1]
        except ValueError:
            pass
    return meta_type, object_name, form_name


def _unit_context(r: Dict[str, Any], rel_path: str) -> Dict[str, str]:
    """
    Returns dict with keys: metadata_type_ru, object_name, form_name,
    symbol_name, routine_type. Caller may override metadata_type_ru /
    object_name via passing them in the input record (see process_batch
    docstring) — the main coordinator can pre-parse owner_qn with the
    full parse_owner_qn from metadata.py and inject the result, avoiding
    duplicating the production parser inside this worker.
    """
    # Preferred path: main coordinator pre-parses owner_qn and passes
    # the result via these keys. Fallback: best-effort split for
    # standalone tests / fixtures.
    meta_type = r.get("_meta_type_ru")
    object_name = r.get("_object_name")
    form_name = r.get("_form_name")
    if meta_type is None or object_name is None or form_name is None:
        meta_type, object_name, form_name = _parse_owner_qn(r.get("owner_qn"))
    if meta_type == "Конфигурация" and not object_name and rel_path:
        stem = PurePosixPath(rel_path).stem
        if stem:
            object_name = stem
    return {
        "metadata_type_ru": meta_type or "",
        "object_name": object_name or "",
        "form_name": form_name or "",
        "symbol_name": (r.get("name") or "").strip(),
        "routine_type": (r.get("routine_type") or "").strip().lower(),
    }


def _unit_id(routine_id: str, part_index: int, part_total: int, unit_kind: str) -> str:
    if unit_kind == "routine":
        return routine_id
    return f"{routine_id}#unit:{part_index:04d}/{part_total:04d}"


def _build_search_text(excerpt: str, ctx: Dict[str, str], module_kind: str, rel_path: str) -> str:
    meta_segment_parts = [
        ctx["metadata_type_ru"], ctx["object_name"], ctx["form_name"],
        module_kind, rel_path,
    ]
    meta_segment = " ".join(p for p in meta_segment_parts if p)
    symbol = ctx["symbol_name"].strip()
    if _METADATA_WEIGHT <= 0:
        return excerpt or ""
    symbol_repeats = round(_METADATA_WEIGHT * _METADATA_SYMBOL_WEIGHT)
    parts: List[str] = []
    if meta_segment:
        parts.extend([meta_segment] * _METADATA_WEIGHT)
    if symbol and symbol_repeats > 0:
        parts.extend([symbol] * symbol_repeats)
    parts.append(excerpt or "")
    return " ".join(p for p in parts if p)


def _build_fields(ctx: Dict[str, str], rel_path: str) -> Dict[str, str]:
    """Metadata fields ONLY — body is no longer persisted in SQLite
    (plan invariant; raw BSL lives only in Neo4j Routine.body)."""
    return {
        "symbol": ctx["symbol_name"],
        "object": ctx["object_name"],
        "form": ctx["form_name"],
        "metadata_type": ctx["metadata_type_ru"],
        "path": rel_path,
    }


# Structural term extraction is non-trivial; for the initial Phase A
# worker version we depend on the indexer's existing helpers via a
# lazy import to avoid re-implementing the regexes here. The lazy
# import is process-local so each worker process pays it once.
_structural_extractor_loaded = False
_extract_structural_terms = None
_extract_identifiers = None
_extract_feature_segments = None


def _load_structural_extractors() -> None:
    global _structural_extractor_loaded
    global _extract_structural_terms, _extract_identifiers, _extract_feature_segments
    if _structural_extractor_loaded:
        return
    from . import bsl_code_indexer as _idx  # lazy: avoid cycles at import
    _extract_structural_terms = _idx._extract_structural_terms
    _extract_identifiers = _idx._extract_identifiers
    _extract_feature_segments = _idx._extract_feature_segments
    _structural_extractor_loaded = True


def _build_structural(excerpt: str) -> Dict[str, Any]:
    _load_structural_extractors()
    excerpt = excerpt or ""
    result: Dict[str, Any] = {}
    terms = _extract_structural_terms(excerpt)
    result["bsl_code_metadata_refs_fts"] = _token_join(terms["metadata_refs"])
    result["bsl_code_query_tables_fts"] = _token_join(terms["query_tables"])
    result["bsl_code_method_calls_fts"] = _token_join(terms["method_calls"])
    result["bsl_code_string_literals_fts"] = _token_join(terms["string_literals"])
    result["bsl_code_assignments_fts"] = _token_join(terms["assignments"])
    identifiers = _extract_identifiers(excerpt)
    result["bsl_code_identifiers_fts"] = _token_join(identifiers)
    result["bsl_code_body_fts"] = _token_text(excerpt)
    result["bsl_code_structural_fts"] = [
        _token_join(terms["metadata_refs"]),
        _token_join(terms["query_tables"]),
        _token_join(terms["method_calls"]),
        _token_join(terms["string_literals"]),
        _token_join(terms["assignments"]),
    ]
    feature_segments = _extract_feature_segments(excerpt)
    result["bsl_code_feature_fts"] = _token_join(feature_segments)
    return {k: v for k, v in result.items() if v}


# ---------------------------------------------------------------- main entry

def process_batch(
    routines: List[Dict[str, Any]],
    strategy: str,
    routine_ordinals: Dict[str, int],
    debug_timings: bool = False,
) -> Dict[str, Any]:
    """
    Process a batch of routines (each dict carries Routine.body + the
    metadata main pre-parsed from owner_qn). Returns a pickle-able
    payload consumed by the main coordinator's per-module flush loop.

    `routine_ordinals[rid]` is the stable ordinal assigned by main
    BEFORE submission, in (rel_path, routine_id) order. The worker
    propagates it into module fragments so finalize aggregates
    deterministically regardless of worker completion order.

    Output schema (all values are pickle-able):
      {
        "unit_rows": list of dicts ready for flush_phase_a_units_batch,
        "method_rows": list of dicts for write_methods_batch,
        "routines_done": list of {routine_id, body_hash, units_written},
        "module_fragments": list of {routine_id, rel_path, routine_ordinal,
            object_name, form_name, metadata_type_ru, module_kind, symbol,
            region_names, headers, comments, body_tokens_text},
        "idf_contributions": list of {routine_id, field_kind, token, df},
        "stats_contributions": list of {routine_id, field_kind,
            doc_count_delta, total_length_delta},
        "skipped_empty": int,
        "split_failed": int,
        "debug_timings": optional dict when debug_timings=True,
      }
    """
    debug_data: Optional[Dict[str, float]] = None
    debug_started = 0.0
    if debug_timings:
        debug_started = time.perf_counter()
        debug_data = {
            "split_ms": 0.0,
            "structural_ms": 0.0,
            "tokenize_ms": 0.0,
            "module_tokenize_ms": 0.0,
        }

        def _tokenize_1c_light_timed(value: str) -> List[str]:
            started_at = time.perf_counter()
            try:
                return tokenize_1c_light(value)
            finally:
                debug_data["tokenize_ms"] += (
                    time.perf_counter() - started_at
                ) * 1000.0

        def _module_token_text_timed(value: str) -> str:
            started_at = time.perf_counter()
            try:
                return _token_text(value)
            finally:
                debug_data["module_tokenize_ms"] += (
                    time.perf_counter() - started_at
                ) * 1000.0
    else:
        _tokenize_1c_light_timed = tokenize_1c_light
        _module_token_text_timed = _token_text

    unit_rows: List[Dict[str, Any]] = []
    method_rows: List[Dict[str, Any]] = []
    routines_done: List[Dict[str, Any]] = []
    module_fragments: List[Dict[str, Any]] = []

    # Per-routine delta records. The coordinator aggregates them into the
    # current module and commits corpus_idf / corpus_stats at module boundary.
    idf_contributions: List[Dict[str, Any]] = []
    stats_contributions: List[Dict[str, Any]] = []

    skipped_empty = 0
    split_failed = 0

    for r in routines:
        rid = r["routine_id"]
        body = r.get("body") or ""
        body_hash = r.get("body_hash") or ""

        if not body.strip():
            skipped_empty += 1
            routines_done.append({
                "routine_id": rid, "body_hash": body_hash, "units_written": 0,
            })
            continue
        split_started = time.perf_counter() if debug_data is not None else 0.0
        try:
            units = split_routine(body, strategy)
        except Exception as e:
            if debug_data is not None:
                debug_data["split_ms"] += (
                    time.perf_counter() - split_started
                ) * 1000.0
            # Split failures stay non-fatal per routine so the rest of the batch
            # still produces useful sidecar data. Логируем адрес: без него
            # ненулевой split_failed невозможно связать с конкретным телом.
            split_failed += 1
            logger.warning(
                "BSL Phase A: split failed for routine_id=%s rel_path=%s "
                "body_chars=%d — %s: %s",
                rid, (r.get("file_path") or "").strip(), len(body),
                type(e).__name__, str(e)[:300],
            )
            routines_done.append({
                "routine_id": rid, "body_hash": body_hash, "units_written": 0,
            })
            continue
        if debug_data is not None:
            debug_data["split_ms"] += (
                time.perf_counter() - split_started
            ) * 1000.0

        module_kind = _MODULE_TYPE_TO_KIND.get(r.get("module_type") or "", "")
        rel_path = (r.get("file_path") or "").strip()
        routine_type_lower = (r.get("routine_type") or "").strip().lower()
        ctx = _unit_context(r, rel_path)
        unit_kind = "routine" if len(units) == 1 else "routine_code_unit"
        routine_symbol = (r.get("name") or "").strip()
        routine_ordinal = int(routine_ordinals.get(rid, 0))

        routine_body_parts: List[str] = []
        routine_region_names: List[str] = []
        routine_headers: List[str] = []
        routine_comments: List[str] = []

        # Per-routine token deltas, summed across units and emitted below as
        # one record per (routine, field_kind, token).
        routine_idf: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        routine_stats: Dict[str, List[int]] = defaultdict(lambda: [0, 0])

        units_for_routine = 0
        for u in units:
            uid = _unit_id(rid, u.part_index, u.part_total, unit_kind)
            excerpt = slice_body(body, u)
            fts_text = _build_search_text(excerpt, ctx, module_kind, rel_path)
            fields = _build_fields(ctx, rel_path)
            structural_started = (
                time.perf_counter() if debug_data is not None else 0.0
            )
            structural = _build_structural(excerpt)
            if debug_data is not None:
                debug_data["structural_ms"] += (
                    time.perf_counter() - structural_started
                ) * 1000.0

            if rel_path and excerpt:
                routine_body_parts.append(excerpt)
                for m in _REGION_RE.finditer(excerpt):
                    routine_region_names.append(m.group(1).strip())
                for m in _COMMENT_RE.finditer(excerpt):
                    text = m.group(1).strip()
                    if text and not text.startswith(
                        ("Объект:", "Форма:", "Процедура:", "Функция:")
                    ):
                        routine_comments.append(text)
                for m in _HEADER_RE.finditer(excerpt):
                    routine_headers.append(m.group(1).strip())

            # --- IDF / stats deltas: единый аккумулятор, общий с
            # `compute_contributions_for_ranges` (см. его docstring). Здесь
            # он получает уже посчитанные `fts_text`/`fields`/`excerpt`, так
            # что Phase A не платит вторую токенизацию.
            _accumulate_unit_contribution(
                routine_idf, routine_stats,
                fts_text=fts_text, fields=fields, excerpt=excerpt,
                tokenize_fn=_tokenize_1c_light_timed,
            )

            unit_rows.append({
                "unit": {
                    "unit_id": uid,
                    "routine_id": rid,
                    # Drift 1 fix: denormalize routine name for Phase B
                    # symbol_name. bsl_code_methods write is best-effort,
                    # so a JOIN there could return NULL.
                    "routine_name": routine_symbol,
                    "config_name": r.get("config_name") or "",
                    "owner_qn": r.get("owner_qn") or "",
                    "owner_qn_prefix": r.get("owner_qn_prefix") or "",
                    "owner_category": r.get("owner_category") or "",
                    "module_type": r.get("module_type") or "",
                    "module_kind": module_kind,
                    "routine_type": r.get("routine_type") or "",
                    "export": bool(r.get("export")),
                    "line_start": u.line_start,
                    "line_end": u.line_end,
                    # Drift 2 fix: persist exact char range so Phase B and
                    # the hybrid scorer slice body[char_start:char_end]
                    # byte-for-byte equal to Phase A FTS payload.
                    "char_start": u.char_start,
                    "char_end": u.char_end,
                    "part_index": u.part_index,
                    "part_total": u.part_total,
                    "body_hash": body_hash,
                    "rel_path": rel_path,
                    "size_chars": len(excerpt),
                    "size_lines": max(1, u.line_end - u.line_start + 1),
                    "unit_kind": unit_kind,
                    "is_regulated_report": _is_regulated_report(
                        r.get("owner_category") or "", rel_path,
                    ),
                },
                "text_for_fts": fts_text,
                "fields": fields,
                "structural": structural,
            })
            units_for_routine += 1

        # Emit per-routine delta records. The coordinator keeps them
        # module-scoped until the module FTS row is committed.
        for fk, tok_map in routine_idf.items():
            for tok, df in tok_map.items():
                idf_contributions.append({
                    "routine_id": rid, "field_kind": fk,
                    "token": tok, "df": df,
                })
        for fk, (dc, tl) in routine_stats.items():
            if dc or tl:
                stats_contributions.append({
                    "routine_id": rid, "field_kind": fk,
                    "doc_count_delta": dc, "total_length_delta": tl,
                })

        method_rows.append({
            "routine_id": rid,
            "config_name": r.get("config_name") or "",
            "name": r.get("name") or "",
            "signature": r.get("signature") or "",
            "routine_type": r.get("routine_type") or "",
            "symbol_kind": routine_type_lower,
            "export": bool(r.get("export")),
            "owner_qn": r.get("owner_qn") or "",
            "body_hash": body_hash,
            "size_chars": len(body),
            "size_lines": body.count("\n") + 1,
        })

        if rel_path and units_for_routine > 0:
            # body_tokens_text uses base tokenize (NOT 1c_light) so the
            # contentless module FTS index keeps RLM keywords like
            # "возврат" / "экспорт" reachable.
            body_tokens_text = _module_token_text_timed(
                " ".join(routine_body_parts)
            )
            module_fragments.append({
                "routine_id": rid,
                "rel_path": rel_path,
                "routine_ordinal": routine_ordinal,
                "object_name": ctx["object_name"],
                "form_name": ctx["form_name"],
                "metadata_type_ru": ctx["metadata_type_ru"],
                "module_kind": module_kind,
                "symbol": routine_symbol,
                "region_names": " ".join(routine_region_names),
                "headers": " ".join(routine_headers),
                "comments": " ".join(routine_comments),
                "body_tokens_text": body_tokens_text,
            })

        routines_done.append({
            "routine_id": rid,
            "body_hash": body_hash,
            "units_written": units_for_routine,
        })

    result: Dict[str, Any] = {
        "unit_rows": unit_rows,
        "method_rows": method_rows,
        "routines_done": routines_done,
        "module_fragments": module_fragments,
        "idf_contributions": idf_contributions,
        "stats_contributions": stats_contributions,
        "skipped_empty": skipped_empty,
        "split_failed": split_failed,
    }
    if debug_data is not None:
        result["debug_timings"] = {
            "worker_total_ms": (time.perf_counter() - debug_started) * 1000.0,
            "split_ms": debug_data["split_ms"],
            "structural_ms": debug_data["structural_ms"],
            "tokenize_ms": debug_data["tokenize_ms"],
            "module_tokenize_ms": debug_data["module_tokenize_ms"],
            "routines": len(routines),
            "units": sum(int(d["units_written"]) for d in routines_done),
        }
    return result


def _accumulate_unit_contribution(
    routine_idf: Dict[str, Dict[str, int]],
    routine_stats: Dict[str, List[int]],
    *,
    fts_text: str,
    fields: Dict[str, str],
    excerpt: str,
    tokenize_fn=tokenize_1c_light,
) -> None:
    """Вклад ОДНОГО unit в per-routine аккумуляторы — единственная реализация.

    Вызывается и из `process_batch` (полная сборка), и из
    `compute_contributions_for_ranges` (обратное вычитание). Оба передают уже
    построенные тексты, поэтому формула не дублируется и лишней токенизации на
    горячем пути Phase A не появляется.

    Два токенизатора по контракту плана: FTS payload'ы строятся базовым
    `tokenize` выше по стеку, а IDF/stats — только `tokenize_1c_light`.
    `body` не приходит в `fields` (raw BSL не персистится в
    `bsl_code_unit_fields`), но field_kind='body' в IDF нужен hybrid-скореру,
    поэтому excerpt токенизируется здесь отдельно.
    """
    doc_tokens = tokenize_fn(fts_text or "")
    for t in set(doc_tokens):
        routine_idf[DOC_FIELD_KIND][t] += 1
    routine_stats[DOC_FIELD_KIND][0] += 1
    routine_stats[DOC_FIELD_KIND][1] += len(doc_tokens)

    for fk in FIELD_KINDS:
        field_tokens = tokenize_fn(fields.get(fk, "") or "")
        for t in set(field_tokens):
            routine_idf[fk][t] += 1
        routine_stats[fk][0] += 1
        routine_stats[fk][1] += len(field_tokens)

    body_tokens = tokenize_fn(excerpt or "")
    for t in set(body_tokens):
        routine_idf["body"][t] += 1
    routine_stats["body"][0] += 1
    routine_stats["body"][1] += len(body_tokens)


class ContributionComputationError(RuntimeError):
    """Вклад посчитать не удалось — это НЕ то же самое, что нулевой вклад.

    Отличие принципиально для обратного вычитания: routine с непустым телом и
    живыми units, чей вклад «посчитался» как пустой, привела бы к удалению её
    строк без уменьшения corpus counters, и underflow-guard этого не заметил бы
    (вычитается ноль). Поэтому ядро бросает, а не возвращает пустой результат.
    """


@dataclass
class RoutineContribution:
    idf: Dict[str, Dict[str, int]]
    stats: Dict[str, Tuple[int, int]]
    unit_count: int


def compute_contributions_for_ranges(
    record: Dict[str, Any],
    ranges: Sequence[Any],
    *,
    sign: int = 1,
) -> RoutineContribution:
    """Ядро расчёта вклада одной routine по ГОТОВОМУ разбиению.

    Splitter здесь не вызывается: `ranges` — это последовательность `UnitRange`,
    полученная либо от `split_routine` (сборка и routine-level delta), либо из
    `bsl_code_units` serving epoch (config delete). Второй источник обязателен
    для удаления конфигурации, потому что `split_routine` не является чистой
    функцией от `(body, strategy)`: границы зависят от доступности
    tree-sitter/SDBL в момент вызова, а она не входит в config fingerprint.
    Пересчёт разбиения дал бы другие окна (они перекрываются) и, значит, другой
    вклад при том же теле.

    Возвращает вклад со знаком `sign` и число обработанных units.
    """
    routine_idf: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    routine_stats: Dict[str, List[int]] = defaultdict(lambda: [0, 0])

    body = record.get("body") or ""
    units = list(ranges or ())
    if not body.strip():
        if units:
            raise ContributionComputationError(
                "empty body but non-empty unit partition"
            )
        return RoutineContribution({}, {}, 0)
    if not units:
        raise ContributionComputationError("non-empty body but empty unit partition")

    module_kind = _MODULE_TYPE_TO_KIND.get(record.get("module_type") or "", "")
    rel_path = (record.get("file_path") or "").strip()
    ctx = _unit_context(record, rel_path)

    for u in units:
        excerpt = slice_body(body, u)
        _accumulate_unit_contribution(
            routine_idf, routine_stats,
            fts_text=_build_search_text(excerpt, ctx, module_kind, rel_path),
            fields=_build_fields(ctx, rel_path),
            excerpt=excerpt,
        )

    if sign == 1:
        idf_out = {fk: dict(toks) for fk, toks in routine_idf.items()}
        stats_out = {fk: (dc, tl) for fk, (dc, tl) in routine_stats.items()}
    else:
        s = int(sign)
        idf_out = {fk: {t: df * s for t, df in toks.items()} for fk, toks in routine_idf.items()}
        stats_out = {fk: (dc * s, tl * s) for fk, (dc, tl) in routine_stats.items()}
    return RoutineContribution(idf_out, stats_out, len(units))


# Обёртки, пересчитывающей разбиение через `split_routine` ради обратного
# вычитания, здесь намеренно нет. Единственный источник границ для вычитания —
# сохранённое разбиение serving epoch: `split_routine` не является чистой функцией
# от (body, strategy), см. докстринг `compute_contributions_for_ranges`.
