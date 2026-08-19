"""
BSL code search indexer (independent of the description embedding lifecycle).

Two-phase pipeline:

    Phase A — SQLite/RLM build (always runs when fingerprint or source state
              changed):
                begin_pending(scope) -> pending_epoch
                for each Routine in scope:
                    split via tree-sitter-bsl
                    write units + FTS5 postings + field/structural rows
                build global IDF/avgdl over the just-written corpus
                commit_pending(scope, fingerprint, source_state_hash)

    Phase B — Vector embedding build (only when ENABLE_BSL_CODE_EMBEDDING is on
              and embedding API is reachable):
                set_vector_status(scope, 'building')
                for each unit just written:
                    build embedding text + embed via EmbeddingService
                    write to Neo4j: small -> Routine label/code_embedding,
                                    large -> RoutineCodeUnit
                set_vector_status(scope, 'ready', vector_epoch=committed_epoch)

Recovery on each start_indexing() call:
    * fingerprint mismatch OR source_state_hash mismatch OR reindex_requested=1
        -> full Phase A + Phase B
    * vector not ready / vector_epoch != current_epoch (with hashes matching)
        -> Phase A skipped, only Phase B for current_epoch
    * everything ready -> noop

If embedding API is down mid-run, Phase B writes set_vector_status('failed'),
Phase A stays committed, search falls back to RLM until next restart.
"""
from __future__ import annotations

import asyncio
import dataclasses
import enum
import hashlib
import json
import logging
import random
import re
import time
from collections import Counter
from pathlib import PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from config import settings
from runtime_memory import trim_process_memory

from .bsl_code_compress import compress_unit, is_compression_enabled
from .embedding_service import (
    EmbeddingUnavailableError,
    format_embedding_error,
    is_embedding_unavailable_error,
)
from .bsl_code_embed_text import (
    UnitContext,
    build_raw_embedding_text,
    parse_owner_qn,
)
from .bsl_code_scorers import tokenize, tokenize_1c_light
from .bsl_code_split import UnitRange, slice_body, split_routine, validate_strategy
from .bsl_code_sqlite import (
    DOC_FIELD_KIND,
    FIELD_KINDS,
    STRUCTURAL_FTS_TABLES,
    BslCodeSqlite,
    PhaseAModuleCommit,
    get_bsl_code_sqlite,
)
from . import bsl_code_search_policy as search_policy
from . import embedding_usage_metrics as embedding_metrics
from .cypher_templates import (
    CYPHER_CLEAR_BSL_SMALL_PENDING_OVERLAP_BATCH,
    CYPHER_CLEAR_BSL_SMALL_UNITS_STALE_BATCH,
    CYPHER_DELETE_BSL_LARGE_PENDING_OVERLAP_BATCH,
    CYPHER_DELETE_BSL_LARGE_UNITS_STALE_BATCH,
    CYPHER_FETCH_ROUTINE_BODY_BATCH,
    CYPHER_FETCH_ROUTINE_IDS_FOR_CONFIG,
    CYPHER_FETCH_ROUTINE_RECORDS_BY_IDS,
    CYPHER_FETCH_ROUTINES_BODY_BATCH,
    CYPHER_FETCH_ROUTINES_LIGHTWEIGHT,
    CYPHER_FETCH_ROUTINES_LIGHTWEIGHT_BY_IDS,
    CYPHER_HIDE_BSL_UNITS_FOR_ROUTINES,
    CYPHER_RETAG_BSL_LARGE_UNIT_EPOCH,
    CYPHER_RETAG_BSL_SMALL_UNIT_EPOCH,
    CYPHER_SYNC_BSL_CODE_EMBEDDING_VISIBLE,
    CYPHER_SYNC_BSL_CODE_EMBEDDING_VISIBLE_BY_IDS,
    CYPHER_UPSERT_BSL_LARGE_UNIT,
    CYPHER_UPSERT_BSL_SMALL_UNIT,
)


# Batch size for the post-Phase-A retag-and-mark-done sweep. Sized to keep
# a single Neo4j UNWIND payload and a single SQLite INSERT OR REPLACE
# transaction modest. Not exposed as an env setting on purpose: this is an
# internal optimisation knob, separate from the embedding API batch size.
_PHASE_B_TRANSFER_BATCH_SIZE = 1000


class PhaseBOutcome(enum.Enum):
    """Explicit result of `_run_phase_b_if_enabled`.

    SUCCESS — coordinator finished cleanly (vectors written, or nothing
              was pending). Caller may now sync visibility and commit
              `vector_status='ready'` together with the coverage policy.
    SKIPPED — Phase B did not run because the vector subsystem is
              disabled or `current_epoch <= 0`. Status is not touched.
              Caller MUST NOT advance to `ready`. Skipped is never
              returned after the caller has already set `building`
              (preflight prevents that); if it ever is, callers treat
              it as a programming error.
    Failures (embedding service unavailable, invalid prompt mode,
    worker exception) are signalled via raised exceptions, with the
    coordinator having already set `vector_status='failed'`.
    """

    SUCCESS = "success"
    SKIPPED = "skipped"
    # Scoped Phase B only: embedding endpoint unavailable (startup gate, service
    # None, or a known-unavailable error after retries). Distinct from SKIPPED
    # (a precondition no-op) so the outage path is not confused with "no work".
    # The full Phase B path never returns this — it raises EmbeddingUnavailableError.
    DEFERRED = "deferred"


@dataclasses.dataclass
class ScopedPhaseBResult:
    """Explicit result of `_embed_units_for_routines` so the applier can
    distinguish a successful scoped Phase B from a deferred precondition skip
    (vector_epoch != current_epoch, feature disabled at runtime, etc).
    Applier on SKIPPED leaves the ledger at `sqlite_applied` and does not
    call `commit_scoped_delta`."""
    outcome: PhaseBOutcome
    reason: str = ""
    embedded_count: int = 0


@dataclasses.dataclass(frozen=True)
class PhaseBRunPolicy:
    """Outer-round retry policy for a Phase B run, selected by `run_mode`.

    startup   — long fixed window (12 rounds × 300s) so a slow endpoint recovery
                still completes the initial/full build; used by the startup BSL
                indexer (overlap final catch-up and non-overlap full rebuild).
    scheduled — the existing short exponential policy (3 rounds); used by
                scheduled recovery and scoped/incremental Phase B.
    """
    max_rounds: int
    backoff_mode: str          # "fixed" | "exponential"
    base_seconds: float
    cap_seconds: float
    jitter_seconds: float

    def backoff(self, round_idx: int) -> float:
        jitter = random.uniform(0.0, max(0.0, self.jitter_seconds))
        if self.backoff_mode == "fixed":
            return max(0.0, self.base_seconds) + jitter
        delay = min(self.base_seconds * (2 ** (round_idx - 1)), self.cap_seconds)
        return delay + jitter


def _phase_b_run_policy(run_mode: str) -> PhaseBRunPolicy:
    if run_mode == "startup":
        return PhaseBRunPolicy(
            max_rounds=max(1, int(settings.bsl_code_startup_phase_b_max_rounds)),
            backoff_mode=(
                settings.bsl_code_startup_phase_b_round_backoff_mode or "fixed"
            ),
            base_seconds=float(
                settings.bsl_code_startup_phase_b_round_backoff_seconds
            ),
            cap_seconds=float(
                settings.bsl_code_startup_phase_b_round_backoff_seconds
            ),
            jitter_seconds=float(
                settings.bsl_code_startup_phase_b_round_backoff_jitter_seconds
            ),
        )
    # scheduled / scoped: existing exponential policy.
    return PhaseBRunPolicy(
        max_rounds=max(1, int(settings.bsl_code_phase_b_max_rounds)),
        backoff_mode="exponential",
        base_seconds=float(settings.bsl_code_phase_b_round_backoff_base_seconds),
        cap_seconds=float(settings.bsl_code_phase_b_round_backoff_max_seconds),
        jitter_seconds=float(
            settings.bsl_code_phase_b_round_backoff_jitter_seconds
        ),
    )


@dataclasses.dataclass
class _PhaseBRoundsOutcome:
    """Result of the shared outer-round loop (`_run_phase_b_rounds`).

    succeeded=True means the coordinator returned cleanly on some round (or
    there was nothing not-done). A not-done remainder from benign terminal
    skips (missing body / body_hash mismatch / empty text) is still success —
    it never becomes done and must not loop forever. succeeded=False means the
    LAST round raised and units remained; `last_exc` is that exception and the
    caller decides the terminal action (set failed / raise / defer)."""
    succeeded: bool
    last_exc: Optional[BaseException] = None
    rounds_run: int = 0
    last_stats: Any = None


from .embedding_text_format import (
    build_embedding_format_spec,
    compute_bsl_code_embedding_fingerprint,
    resolve_bsl_code_prompt_profile,
    resolve_effective_embedding_transport,
)


def _log_phase_b_worker_exc(prefix: str, exc: BaseException) -> None:
    """Log a Phase B worker task exception at the right severity.

    An embedding endpoint outage (e.g. provider 200 with empty data) is an
    expected external degradation: log one warning line without a traceback.
    Everything else is a real bug — keep the full traceback via exc_info.
    """
    if is_embedding_unavailable_error(exc):
        logger.warning("%s: %s", prefix, exc)
    else:
        logger.error("%s: %s", prefix, exc, exc_info=exc)


def _log_phase_b_worker_excs(
    prefix: str, excs: Sequence[BaseException],
) -> Optional[BaseException]:
    """Log a round's worker exceptions grouped by kind and pick which to raise.

    Embedding outages (expected external degradation) collapse into one summary
    ``WARNING`` without a traceback; every real bug keeps its full ``exc_info``
    ``ERROR``. Returns the exception to propagate, preferring a real bug over an
    outage so the outer round handler logs a traceback only when one is real —
    and an infra write failure is never masked as a quiet outage.
    """
    if not excs:
        return None
    outages = [e for e in excs if is_embedding_unavailable_error(e)]
    real = [e for e in excs if not is_embedding_unavailable_error(e)]
    if outages:
        logger.warning(
            "%s: %d/%d workers failed (embedding outage): %s",
            prefix, len(outages), len(excs), format_embedding_error(outages[0]),
        )
    for e in real:
        logger.error("%s: %s", prefix, e, exc_info=e)
    return real[0] if real else outages[0]


# ----------------------------------------------------------------------- regexes

# Structural extraction regexes for the RLM structural index.
_META_REF_RE = re.compile(
    r"\b("
    r"Документы|Документ|Справочники|Справочник|"
    r"РегистрыСведений|РегистрСведений|"
    r"РегистрыНакопления|РегистрНакопления|"
    r"РегистрыБухгалтерии|РегистрБухгалтерии|"
    r"ПланыСчетов|ПланСчетов|"
    r"ПланыВидовХарактеристик|ПланВидовХарактеристик|"
    r"ПланыВидовРасчета|ПланВидовРасчета|"
    r"Перечисления|Перечисление|Константы|Константа|"
    r"Обработки|Обработка|Отчеты|Отчет"
    r")\.([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)"
)
_METHOD_CALL_RE = re.compile(r"\b([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)\s*\(")
_IDENT_RE = re.compile(r"\b[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*\b")
_IDENT_CHAIN_RE = re.compile(
    r"\b[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*"
    r"(?:\.[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)+\b"
)
_ASSIGN_RE = re.compile(
    r"\b([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*"
    r"(?:\.[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*){1,3})\s*="
)
_STRING_RE = re.compile(r'"((?:[^"]|"")*)"')
_QUERY_TEXT_RE = re.compile(r"(?is)\bВЫБРАТЬ\b.+?\b(?:ИЗ|ПОМЕСТИТЬ|ОБЪЕДИНИТЬ|ГДЕ)\b")

_BSL_CALL_STOP: frozenset = frozenset({
    "Если", "Тогда", "ИначеЕсли", "Для", "Каждого", "Пока", "Попытка",
    "Исключение", "Возврат", "Новый", "Истина", "Ложь", "Неопределено",
    "Процедура", "Функция", "КонецПроцедуры", "КонецФункции",
    "Экспорт", "Перейти", "Продолжить", "Прервать",
})

_BSL_IDENT_STOP: frozenset = _BSL_CALL_STOP | frozenset({
    "КонецЕсли", "КонецЦикла", "КонецПопытки", "Перем", "Знач",
    "И", "Или", "Не", "По", "Из", "На", "В", "С", "К", "От", "До",
    "ЭтотОбъект", "ЭтаФорма",
})

_REGION_RE = re.compile(r"^\s*#Область\s+(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
_COMMENT_RE = re.compile(r"^\s*//\s*(.+?)\s*$", re.MULTILINE)
_HEADER_RE = re.compile(
    r"^\s*(?:Процедура|Функция|Procedure|Function)\s+(\w+)",
    re.MULTILINE | re.IGNORECASE,
)
_DIRECTIVE_RE = re.compile(r"^\s*(&[A-Za-zА-Яа-яЁё]+)", re.MULTILINE)


# snake_case module_kind used in lexical corpora. Scanner emits PascalCase
# module_type; we additionally map ValueManagerModule and ConfigurationModule
# explicitly so they do not leak PascalCase tokens after 1c_light tokenization.
_MODULE_TYPE_TO_KIND: Dict[str, str] = {
    "CommonModule": "module",
    "CommonFormModule": "module",
    "FormModule": "form_module",
    "ObjectModule": "object_module",
    "ManagerModule": "manager_module",
    "ValueManagerModule": "value_manager_module",
    "RecordSetModule": "record_set_module",
    "CommandModule": "command_module",
    "ConfigurationModule": "configuration_module",
}

logger = logging.getLogger(__name__)


# Phase B usage/progress metrics live in a small shared module so the startup
# overlap controller can reuse the exact same stats model and token/cost rules
# without importing this heavy module. Re-imported here at module scope so
# existing `from graphdb.bsl_code_indexer import _PhaseBStats` and monkeypatch of
# `_ProgressLogger` by name keep working.
from .bsl_code_phase_b_metrics import (  # noqa: E402
    BSL_PHASE_B_PROGRESS_UNITS,
    BSL_PROGRESS_SECONDS,
    _PhaseBStats,
    _ProgressLogger,
    _add_optional_tokens,
    _format_cost,
    _format_elapsed,
    _format_usage_tokens,
)

BSL_PHASE_A_PROGRESS_ROUTINES = 1000
BSL_PHASE_B_TRANSFER_PROGRESS_UNITS = 10000


class _PhaseAPacker:
    """Incremental work-pack builder for Phase A. Single source of truth for
    count/byte caps + huge-routine carve-out + ordinal assignment.

    Mirrors the inline packer in `_phase_a_streaming_write` (lines 1333-1346)
    1:1 — including the pack-boundary ordinal quirk. Concretely:
    - ordinal_counter is incremented on EVERY add() BEFORE the flush check.
    - The freshly-assigned ordinal lands in the internal `_ordinals` dict,
      which is attached to the CLOSED pack at close-time AND THEN reset.
    - The triggering record is then appended to the new (empty) pack with
      an empty ordinals dict. As a result, the routine that causes a pack
      to close has its ordinal recorded in the CLOSED pack (where it is
      unused, since that pack does not contain the record) and ABSENT
      from the new pack. Worker falls back to `routine_ordinal=0` via
      `routine_ordinals.get(rid, 0)` in `bsl_code_phase_a_worker.py:353`.
      This quirk is preserved for byte-for-byte parity with the existing
      full Phase A snapshot; scoped Phase 5A inherits it intentionally.
    Used by both full streaming pipeline (`_phase_a_streaming_write`) and
    scoped Phase 5A (`_build_units_for_routines`).
    """

    def __init__(
        self,
        *,
        work_batch_routines: int,
        work_batch_max_bytes: int,
        ordinal_start: int = 1,
    ) -> None:
        self._work_batch_routines = max(1, int(work_batch_routines))
        self._work_batch_max_bytes = max(1, int(work_batch_max_bytes))
        self._ordinal_counter = int(ordinal_start) - 1
        self._current_pack: List[Dict[str, Any]] = []
        self._current_pack_bytes = 0
        self._ordinals: Dict[str, int] = {}

    def add(
        self, record: Dict[str, Any],
    ) -> Optional[Tuple[List[Dict[str, Any]], Dict[str, int]]]:
        """Feed one record. Order of operations (mirrors inline 1334-1346):
            1. ordinal_counter += 1; _ordinals[rid] = ordinal_counter
            2. flush check on the EXISTING current_pack (without `record`)
            3. if check triggers close: capture (current_pack, _ordinals) as
               return value, reset both to empty
            4. append `record` to current_pack
        Returns closed (records, ordinals) tuple if a close happened, else None.
        Note: the just-assigned ordinal for `record` lands in the closed
        pack's ordinals dict, not in the next pack — see class docstring.
        """
        self._ordinal_counter += 1
        rid = record.get("routine_id")
        if rid:
            self._ordinals[rid] = self._ordinal_counter
        body_len = len(record.get("body") or "")
        closed: Optional[Tuple[List[Dict[str, Any]], Dict[str, int]]] = None
        if self._current_pack and (
            len(self._current_pack) >= self._work_batch_routines
            or self._current_pack_bytes + body_len > self._work_batch_max_bytes
        ):
            closed = (self._current_pack, self._ordinals)
            self._current_pack = []
            self._current_pack_bytes = 0
            self._ordinals = {}
        self._current_pack.append(record)
        self._current_pack_bytes += body_len
        return closed

    def flush(
        self,
    ) -> Optional[Tuple[List[Dict[str, Any]], Dict[str, int]]]:
        """Return the trailing pack (records + ordinals) after stream end.
        Empties internal state. Returns None if no records were added since
        the last close."""
        if not self._current_pack:
            return None
        result = (self._current_pack, self._ordinals)
        self._current_pack = []
        self._current_pack_bytes = 0
        self._ordinals = {}
        return result


def _safe_heartbeat(lease: Optional[Any]) -> None:
    """Main-thread-only heartbeat helper for scoped Phase 5A. Lease is
    passed explicitly (not read from `self._active_lease`) so this works
    for any caller — indexer scoped helper, applier delete loop, applier
    metadata loop. Worker processes never call this (lease is not
    pickle-safe). No-op when lease is None."""
    if lease is None:
        return
    try:
        lease.heartbeat()
    except Exception:
        pass


def _invert_snapshot_subset(
    snapshot: Optional[Dict[str, Dict[str, Any]]],
    routine_ids: Iterable[str],
) -> Tuple[Dict[str, Dict[str, int]], Dict[str, Tuple[int, int]]]:
    """Aggregate negated IDF/stats from `snapshot` for the given subset of
    routine_ids. Same shape as `BslCodeSearchDeltaApplier._invert_snapshot`
    but module-level so `_build_units_for_routines` can call it per pack."""
    idf_neg: Dict[str, Dict[str, int]] = {}
    stats_neg: Dict[str, Tuple[int, int]] = {}
    snap = snapshot or {}
    for rid in routine_ids:
        entry = snap.get(rid)
        if not entry:
            continue
        for fk, tok_map in (entry.get("idf") or {}).items():
            dst = idf_neg.setdefault(fk, {})
            for tok, df in tok_map.items():
                dst[tok] = dst.get(tok, 0) - int(df)
        for fk, dc_tl in (entry.get("stats") or {}).items():
            if isinstance(dc_tl, (list, tuple)) and len(dc_tl) == 2:
                dc, tl = dc_tl
            else:
                dc, tl = 0, 0
            pdc, ptl = stats_neg.get(fk, (0, 0))
            stats_neg[fk] = (pdc - int(dc), ptl - int(tl))
    return idf_neg, stats_neg


# _PhaseBStats moved to bsl_code_phase_b_metrics (re-imported at module scope).


class _PhaseBProgress:
    def __init__(
        self, total_units: int, round_label: Optional[str] = None,
    ) -> None:
        self.stats = _PhaseBStats()
        self._round_label = round_label
        self._lock = asyncio.Lock()
        self._progress = _ProgressLogger(
            "BSL Phase B",
            total_units,
            BSL_PHASE_B_PROGRESS_UNITS,
            item_name="units",
            log=logger,
        )

    async def add(self, stats: _PhaseBStats) -> None:
        async with self._lock:
            self.stats.add(stats)
            self._log_locked()

    async def heartbeat(self) -> None:
        async with self._lock:
            self._log_locked()

    async def final(self) -> _PhaseBStats:
        async with self._lock:
            self._log_locked(final=True)
            return self.stats.copy()

    def _log_locked(self, *, final: bool = False) -> None:
        # `round` is passed first so it renders right after the percentage;
        # None round_label is dropped by _ProgressLogger.maybe_log. Tokens/cost
        # are pre-formatted strings ('unknown' when usage is missing) because
        # maybe_log filters out None values entirely.
        self._progress.maybe_log(
            self.stats.units_requested,
            final=final,
            round=self._round_label,
            units_prepared=self.stats.units_prepared,
            units_written=self.stats.units_written,
            skipped_missing_body=self.stats.skipped_missing_body,
            skipped_hash_mismatch=self.stats.skipped_hash_mismatch,
            skipped_empty_text=self.stats.skipped_empty_text,
            batches=self.stats.batches,
            embedding_api_calls=self.stats.embedding_api_calls,
            input_tokens=_format_usage_tokens(self.stats.input_tokens),
            total_tokens=_format_usage_tokens(self.stats.total_tokens),
            cost=_format_cost(*self.stats.primary_cost()),
        )


class _PhaseADebugProfiler:
    def __init__(
        self,
        total_routines: int,
        seconds_interval: float = BSL_PROGRESS_SECONDS,
    ) -> None:
        self.total_routines = max(0, int(total_routines))
        self.seconds_interval = max(1.0, float(seconds_interval))
        self.started_at = time.perf_counter()
        self.last_logged_at = self.started_at

        self.queue_wait_ms = 0.0
        self.submit_ms = 0.0
        self.future_wait_ms = 0.0
        self.absorb_ms = 0.0
        self.sqlite_flush_ms = 0.0
        self.module_flush_ms = 0.0

        self.worker_total_ms_sum = 0.0
        self.worker_split_ms_sum = 0.0
        self.worker_structural_ms_sum = 0.0
        self.worker_tokenize_ms_sum = 0.0
        self.worker_module_tokenize_ms_sum = 0.0

        self.batches_submitted = 0
        self.batches_drained = 0
        self.sqlite_flushes = 0
        self.module_flushes = 0
        self.module_batches = 0
        self.unit_rows_flushed = 0
        self.method_rows_flushed = 0
        self.idf_rows_flushed = 0
        self.stats_rows_flushed = 0

    def start(self) -> float:
        return time.perf_counter()

    @staticmethod
    def elapsed_ms(started_at: float) -> float:
        return (time.perf_counter() - started_at) * 1000.0

    def add_queue_wait(self, started_at: float) -> float:
        elapsed = self.elapsed_ms(started_at)
        self.queue_wait_ms += elapsed
        return elapsed

    def add_submit(self, started_at: float) -> float:
        elapsed = self.elapsed_ms(started_at)
        self.submit_ms += elapsed
        self.batches_submitted += 1
        return elapsed

    def add_future_wait(self, started_at: float) -> float:
        elapsed = self.elapsed_ms(started_at)
        self.future_wait_ms += elapsed
        return elapsed

    def add_absorb_exclusive(
        self,
        started_at: float,
        nested_flush_ms_before: float,
    ) -> float:
        elapsed = self.elapsed_ms(started_at)
        nested_flush_ms_after = self.sqlite_flush_ms + self.module_flush_ms
        nested_flush_ms = max(0.0, nested_flush_ms_after - nested_flush_ms_before)
        self.absorb_ms += max(0.0, elapsed - nested_flush_ms)
        self.batches_drained += 1
        return elapsed

    def add_sqlite_flush(
        self,
        started_at: float,
        *,
        units: int,
        methods: int,
        idf_rows: int,
        stats_rows: int,
    ) -> float:
        elapsed = self.elapsed_ms(started_at)
        self.sqlite_flush_ms += elapsed
        self.sqlite_flushes += 1
        self.unit_rows_flushed += int(units)
        self.method_rows_flushed += int(methods)
        self.idf_rows_flushed += int(idf_rows)
        self.stats_rows_flushed += int(stats_rows)
        return elapsed

    def add_combined_boundary_write_rows(self, *, units: int, methods: int) -> None:
        self.unit_rows_flushed += int(units)
        self.method_rows_flushed += int(methods)

    def add_module_flush(self, started_at: float, *, modules: int = 1) -> float:
        elapsed = self.elapsed_ms(started_at)
        self.module_flush_ms += elapsed
        self.module_flushes += int(modules)
        self.module_batches += 1
        return elapsed

    def add_worker_timings(self, timings: Optional[Dict[str, Any]]) -> None:
        if not timings:
            return
        self.worker_total_ms_sum += float(timings.get("worker_total_ms") or 0.0)
        self.worker_split_ms_sum += float(timings.get("split_ms") or 0.0)
        self.worker_structural_ms_sum += float(timings.get("structural_ms") or 0.0)
        self.worker_tokenize_ms_sum += float(timings.get("tokenize_ms") or 0.0)
        self.worker_module_tokenize_ms_sum += float(
            timings.get("module_tokenize_ms") or 0.0
        )

    def log_slow_op(self, op: str, elapsed_ms: float, **stats: Any) -> None:
        if elapsed_ms < self.seconds_interval * 1000.0:
            return
        stats_text = ", ".join(
            f"{key}={value}" for key, value in stats.items() if value is not None
        )
        suffix = f", {stats_text}" if stats_text else ""
        logger.debug("BSL Phase A slow op: %s_ms=%d%s", op, int(elapsed_ms), suffix)

    def maybe_log(
        self,
        *,
        processed: int,
        units: int,
        final: bool = False,
    ) -> None:
        now = time.perf_counter()
        if not final and now - self.last_logged_at < self.seconds_interval:
            return

        main_elapsed_ms = max(1.0, (now - self.started_at) * 1000.0)
        timed_main_ms = (
            self.queue_wait_ms + self.future_wait_ms + self.absorb_ms
            + self.sqlite_flush_ms + self.module_flush_ms + self.submit_ms
        )
        other_ms = max(0.0, main_elapsed_ms - timed_main_ms)

        def pct(value: float) -> float:
            return value / main_elapsed_ms * 100.0

        avg_worker_batch_ms = (
            self.worker_total_ms_sum / self.batches_drained
            if self.batches_drained else 0.0
        )
        logger.debug(
            "BSL Phase A profile: processed=%d/%d, units=%d, "
            "batches=%d/%d, "
            "main_pct queue_wait=%.1f future_wait=%.1f absorb=%.1f "
            "sqlite=%.1f module=%.1f submit=%.1f other=%.1f, "
            "main_ms queue_wait=%d future_wait=%d absorb=%d sqlite=%d "
            "module=%d submit=%d other=%d, "
            "worker_ms total_sum=%d split=%d structural=%d tokenize=%d "
            "module_tokenize=%d avg_batch=%d, "
            "flushes sqlite=%d module=%d module_batches=%d, "
            "rows units=%d methods=%d idf=%d stats=%d",
            int(processed), self.total_routines, int(units),
            self.batches_drained, self.batches_submitted,
            pct(self.queue_wait_ms), pct(self.future_wait_ms), pct(self.absorb_ms),
            pct(self.sqlite_flush_ms), pct(self.module_flush_ms), pct(self.submit_ms),
            pct(other_ms),
            int(self.queue_wait_ms), int(self.future_wait_ms), int(self.absorb_ms),
            int(self.sqlite_flush_ms), int(self.module_flush_ms), int(self.submit_ms),
            int(other_ms),
            int(self.worker_total_ms_sum), int(self.worker_split_ms_sum),
            int(self.worker_structural_ms_sum), int(self.worker_tokenize_ms_sum),
            int(self.worker_module_tokenize_ms_sum), int(avg_worker_batch_ms),
            self.sqlite_flushes, self.module_flushes, self.module_batches,
            self.unit_rows_flushed, self.method_rows_flushed,
            self.idf_rows_flushed, self.stats_rows_flushed,
        )
        self.last_logged_at = now


def _process_rss_mb() -> Optional[int]:
    """
    Best-effort process RSS in megabytes for diagnostic logging. Linux
    (Docker runtime) reads /proc/self/status VmRSS. Other platforms return
    None — caller must omit the key from log stats in that case.
    """
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) // 1024
                    return None
    except OSError:
        return None
    return None


@dataclasses.dataclass
class BslCodeEmbeddingJob:
    """All data needed to embed one code unit and write the result to Neo4j."""
    embedding_text: str
    unit_kind: str          # "routine" | "routine_code_unit"
    routine_id: str
    epoch: int
    unit_id: str = ""
    project_name: str = ""
    config_name: str = ""
    owner_qn: str = ""
    owner_qn_prefix: str = ""
    owner_category: str = ""
    module_type: str = ""
    routine_type: str = ""
    export: bool = False
    line_start: int = 0
    line_end: int = 0
    part_index: int = 0
    part_total: int = 1
    body_hash: str = ""
    is_regulated_report: bool = False


_METADATA_WEIGHT = 6
_METADATA_SYMBOL_WEIGHT = 1.0


class BslCodeSearchIndexer:
    """Project-scope (project_name) BSL code indexer."""

    def __init__(self, driver, *, embedding_availability=None) -> None:
        self.driver = driver
        self.scope: str = settings.project_name
        self.sqlite: BslCodeSqlite = get_bsl_code_sqlite()
        # Optional startup EmbeddingAvailability. When set with available=False,
        # Phase B / scoped Phase B skip the embedding call entirely (no bounded
        # or production probe) so a startup cycle can't hang on a dead endpoint.
        # None means "no startup gate" (scheduled/background: current behaviour).
        self._embedding_availability = embedding_availability
        # Set by start_indexing(); "startup" enables overlap + long Phase B
        # policy, "scheduled" (default) keeps the sequential model.
        self._run_mode: str = "scheduled"
        self._active_lease: Optional[Any] = None

    # ------------------------------------------------------------------ public

    def _heartbeat_lease(self) -> None:
        """Heartbeat активного scheduler lease, если он передан в start_indexing().

        Никаких raise/propagation — heartbeat безопасен по контракту LockLease.
        """
        lease = getattr(self, "_active_lease", None)
        if lease is None:
            return
        try:
            lease.heartbeat()
        except Exception:
            pass

    def start_indexing(
        self, lease: Optional[Any] = None, run_mode: str = "scheduled",
    ) -> None:
        """Full rebuild / resume orchestrator.

        `run_mode`: "scheduled" (default; also used by `BslCodeSearchSync`
        recovery) keeps the sequential Phase A -> Phase B model and the short
        3-round policy. "startup" (from `start_bsl_code_indexing_background`)
        selects the long 12×300s Phase B policy and enables the startup-only
        overlapped Phase A + Phase B path.

        Optional `lease`: when phase 5 `BslCodeSearchSync` вызывает recovery
        rebuild под scheduler_lock, передаёт `LockLease`. Сам `start_indexing()`
        делает heartbeat на крупных границах (lightweight fetch, Phase A flush,
        Phase B iteration) — это удерживает scheduler_lock от stale window
        race с background workers. `lease=None` (startup helper) → heartbeat
        — no-op.
        """
        if not settings.enable_bsl_code_search:
            logger.info("BSL code search disabled, skipping indexer")
            return

        scope = self.scope
        logger.info(
            "BSL code search: starting indexer for scope=%s (run_mode=%s)",
            scope, run_mode,
        )

        # Сохраняем lease как instance attribute, чтобы _run_phase_a / _phase_b
        # могли его подхватить без расширения сигнатур internal helpers.
        self._active_lease = lease
        self._run_mode = run_mode

        try:
            removed = self.sqlite.gc_retired_epochs(scope)
            if removed:
                logger.info("BSL sqlite GC removed %d stale unit rows for scope=%s", removed, scope)
        except Exception as e:
            logger.warning("BSL sqlite GC failed (continuing): %s", e)

        self._heartbeat_lease()

        # Lightweight pass: metadata only, ORDER BY (rel_path, routine_id).
        # No body strings held in memory — body batches stream through
        # _iter_routine_body_batches inside Phase A / Phase B.
        lightweight = self._fetch_routines_lightweight()
        if not lightweight:
            logger.info("BSL code search: no routines found for project_name=%s", scope)
            return

        self._heartbeat_lease()

        try:
            validate_strategy(settings.bsl_code_split_strategy)
        except ValueError as e:
            logger.error("BSL code search: invalid split strategy, indexing aborted — %s", e)
            return

        current_fingerprint = self._compute_config_fingerprint()
        current_source_hash = self._compute_source_state_hash(lightweight)
        total_routines = len(lightweight)
        stored = self.sqlite.read_fingerprint(scope)
        stored_fp = (stored.get("fingerprint") or "") if stored else ""
        stored_src = (stored.get("source_state_hash") or "") if stored else ""
        reindex_requested = bool(stored.get("reindex_requested")) if stored else False

        fingerprint_changed = stored_fp != current_fingerprint
        source_changed = stored_src != current_source_hash

        if fingerprint_changed or source_changed or reindex_requested:
            reason = []
            if fingerprint_changed:
                reason.append("config fingerprint changed")
            if source_changed:
                reason.append("source state changed")
            if reindex_requested:
                reason.append("reindex_requested=1")
            logger.info("BSL code search: full rebuild — %s", "; ".join(reason))
            self._run_phase_a_and_finalize(
                total_routines, current_fingerprint, current_source_hash,
                force_fresh=reindex_requested,
            )
            return

        pending_epoch_existing = (stored.get("pending_epoch") if stored else None)
        pending_status_existing = ((stored.get("pending_status") if stored else "idle") or "idle")
        if pending_epoch_existing is not None and pending_status_existing in (
            "writing", "finalizing",
        ):
            pending_fp_existing = (stored.get("pending_fingerprint") or "") if stored else ""
            pending_src_existing = (stored.get("pending_source_state_hash") or "") if stored else ""
            if (
                pending_fp_existing == current_fingerprint
                and pending_src_existing == current_source_hash
            ):
                logger.info(
                    "BSL code search: resuming pending epoch=%d status=%s",
                    pending_epoch_existing, pending_status_existing,
                )
                self._run_phase_a_and_finalize(
                    total_routines, current_fingerprint, current_source_hash,
                )
                return

        current_epoch = self.sqlite.current_epoch(scope)
        vec_state = self.sqlite.vector_state(scope)
        current_embedding_fp = self._compute_embedding_fingerprint()
        coverage_changed, coverage_delta_obj = self._coverage_change(scope)
        vector_enabled = self._bsl_vector_enabled()
        # embedding_fingerprint is a vector-space-compatibility marker, NOT a
        # "Phase B finalised" marker. `_run_phase_b_if_enabled` now stamps the
        # current fingerprint at building-start, so a Phase B that fails under
        # the SAME contract keeps stored fp == current fp and is resumed here
        # via the non-drift path (existing done-markers are trusted, only the
        # remaining not-done units are re-embedded). Finalisation is decided by
        # vector_status == "ready" alone.
        #
        # Drift therefore covers exactly the cases where stored fp != current fp:
        #   (a) prior Phase B ran under a different model/prompt/transport;
        #   (b) a genuine drift whose marker cleanup has not yet succeeded — the
        #       drift branch below intentionally leaves the old stored fp in
        #       place until delete_phase_b_state_for_epoch succeeds, so a
        #       transient cleanup failure is retried on the next run instead of
        #       being silently trusted.
        # The check keys on the fingerprint only, never on vec_state.vector_epoch.
        embedding_fp_drift = (
            vector_enabled
            and vec_state.embedding_fingerprint != current_embedding_fp
        )
        vector_needs_phase_b = (
            vector_enabled
            and (
                vec_state.status != "ready"
                or vec_state.vector_epoch is None
                or vec_state.vector_epoch != current_epoch
                or embedding_fp_drift
            )
        )
        if vector_needs_phase_b:
            if embedding_fp_drift:
                logger.info(
                    "BSL code search: running Phase B only for epoch=%d — "
                    "embedding fingerprint changed (was=%r, now=%r, "
                    "prior vector_status=%s, prior vector_epoch=%s)",
                    current_epoch,
                    vec_state.embedding_fingerprint, current_embedding_fp,
                    vec_state.status, vec_state.vector_epoch,
                )
                # Wipe any done markers for the current epoch so Phase B
                # re-embeds every unit under the new contract. Only advance the
                # stored fingerprint to current AFTER the cleanup succeeds:
                # promoting it while stale old-contract markers are still
                # present would make them trusted forever (the next run would
                # see stored fp == current fp, skip the drift cleanup, and
                # count_phase_b_not_done_units would exclude those units by
                # their lingering done-markers — leaving old-vector-space
                # embeddings under a ready new contract). On a transient
                # cleanup failure we keep the old stored fp untouched, mark the
                # run failed and defer Phase B so the next run re-detects drift
                # and retries the cleanup.
                try:
                    self.sqlite.delete_phase_b_state_for_epoch(
                        scope, current_epoch,
                    )
                except Exception as e:
                    logger.error(
                        "BSL code search: drift marker cleanup failed for "
                        "epoch=%d, deferring Phase B to next cycle: %s",
                        current_epoch, e,
                    )
                    # fp=None leaves the old stored fingerprint in place so the
                    # next start_indexing still sees stored fp != current fp.
                    self.sqlite.set_vector_status(scope, "failed")
                    return
                self.sqlite.set_vector_status(
                    scope, "building", vector_epoch=current_epoch,
                    embedding_fingerprint=current_embedding_fp,
                )
            else:
                logger.info(
                    "BSL code search: SQLite up to date (epoch=%d), running "
                    "Phase B only (vector_status=%s, vector_epoch=%s)",
                    current_epoch, vec_state.status, vec_state.vector_epoch,
                )
            self._safe_run_phase_b_and_finalize(scope, current_epoch)
            return

        if coverage_changed:
            try:
                self._handle_coverage_change(scope, current_epoch, coverage_delta_obj)
            except Exception as e:
                # Phase B / sync already moved vector_status to 'failed';
                # search falls back to RLM. Don't propagate — start_indexing
                # is a startup hook and must not abort the rest of init.
                logger.error(
                    "BSL code search: coverage_change handler aborted: %s",
                    e, exc_info=True,
                )
            return

        logger.info(
            "BSL code search: scope=%s is up to date (epoch=%d, vector=%s, "
            "embedding_fp=%r)",
            scope, current_epoch, vec_state.status,
            vec_state.embedding_fingerprint,
        )

    # ------------------------------------------------------------------ Phase A

    def _resolve_overlap_embedding_context(self):
        """Resolve (embedding_service, doc_spec) for the startup overlap direct
        path, or (None, None) when the endpoint is unavailable / prompt invalid.
        (None, None) simply disables the fast path — Phase A still runs and the
        SQLite catch-up recovers Phase B."""
        avail = self._embedding_availability
        if avail is not None and avail.enabled and not avail.available:
            return None, None
        service = self._get_embedding_service_or_none()
        if service is None:
            return None, None
        try:
            profile = resolve_bsl_code_prompt_profile(
                settings.embedding_model or "",
                settings.bsl_code_embedding_prompt_mode or "auto",
            )
        except Exception:
            return None, None
        transport = resolve_effective_embedding_transport(
            settings.embedding_api_base or "",
            getattr(settings, "embedding_transport", "auto") or "auto",
        )
        doc_spec = build_embedding_format_spec(
            profile=profile,
            transport=transport,
            side="document",
            purpose="code",
            description_instruction="",
        )
        return service, doc_spec

    def _run_phase_a_and_finalize(
        self,
        total_routines: int,
        fingerprint: str,
        source_state_hash: str,
        force_fresh: bool = False,
    ) -> None:
        """Run Phase A then the Phase B finalize, enabling the startup-only
        overlapped Phase B when `run_mode="startup"` (R1-F1: this branch owns
        the overlap controller lifecycle; `_run_phase_a` only gets a sink).

        The final catch-up is the normal `_run_phase_b_and_finalize` under the
        startup policy (12×300s) — it processes every still-not-done unit from
        SQLite (source of truth), ensures the vector index is ONLINE, syncs
        visibility and commits `ready`. Overlap only marks some units done early.
        """
        scope = self.scope
        overlap_enabled = (
            getattr(self, "_run_mode", "scheduled") == "startup"
            and self._bsl_vector_enabled()
            and bool(settings.bsl_code_startup_phase_b_overlap_enabled)
        )
        controller = None
        if overlap_enabled:
            emb_service, doc_spec = self._resolve_overlap_embedding_context()
            if emb_service is not None and doc_spec is not None:
                from .bsl_code_phase_b_overlap import BslPhaseBOverlapController
                controller = BslPhaseBOverlapController(
                    self, embedding_service=emb_service, doc_spec=doc_spec,
                )
                controller.start()
                logger.info(
                    "BSL code search: startup Phase B overlap enabled "
                    "(queue=%d, chunk_units=%d)",
                    int(settings.bsl_code_startup_phase_b_overlap_queue_batches),
                    int(settings.bsl_code_startup_phase_b_overlap_chunk_units),
                )
        sink = controller.submit if controller is not None else None
        aborted = False
        try:
            self._run_phase_a(
                total_routines, fingerprint, source_state_hash,
                force_fresh=force_fresh, committed_units_sink=sink,
            )
        except BaseException:
            aborted = True
            raise
        finally:
            if controller is not None:
                controller.producer_done()
                controller.join()
            # Phase A peak (split/tokenize/structural extraction) is behind us;
            # return allocator-retained memory to the OS before the embedding
            # stage. Runs on abort too so a failed Phase A still trims, without
            # masking the original error. Best-effort: never raises.
            trim_process_memory(
                "BSL Phase A aborted" if aborted else "BSL Phase A completed",
                enabled=settings.memory_trim_enabled,
            )
        # Committed epoch is owned here (R2-F3), not by controller.pending_epoch.
        current_epoch = self.sqlite.current_epoch(scope)
        self._safe_run_phase_b_and_finalize(scope, current_epoch)

    def _run_phase_a(
        self,
        total_routines: int,
        fingerprint: str,
        source_state_hash: str,
        force_fresh: bool = False,
        committed_units_sink: Optional[Any] = None,
    ) -> None:
        """
        Streaming Phase A coordinator (plan decisions #1-#3, #5-#6).

        `committed_units_sink` (startup overlap only): optional observer
        `sink(pending_epoch, unit_rows)` invoked AFTER each durable SQLite commit
        with the flat unit rows just committed. Best-effort — Phase A never
        blocks on or fails because of it. Phase A stays the sole owner of the
        durable epoch transition and knows nothing about the Phase B consumer /
        thread (R1-F1).

        Pipeline:
          1. begin_or_resume_pending; on resume_writing, classify the
             last_rel_path (state-aware taxonomy) and either atomically
             cleanup an in-progress module or continue strictly after a
             fully-flushed one.
          2. Prefetch body batches from Neo4j via _iter_routine_body_batches
             (keyset paginated, ORDER BY rel_path, routine_id).
          3. Pack work batches under min(WORK_BATCH_ROUTINES,
             WORK_BATCH_MAX_MB), assign stable routine_ordinal in
             enumeration order, submit via ProcessPoolExecutor.map (which
             preserves input order on the returning iterator).
          4. Stream results in input order; aggregate per-module fragments
             and IDF/stats in main; on rel_path change, flush units/done
             and atomically commit the previous module's corpus deltas +
             module FTS row.
          5. On stream end, flush the last module aggregate and the final
             write batch. Mark writing complete, write module metadata,
             commit_pending, Neo4j stale cleanup.
        """
        scope = self.scope
        phase_started = time.monotonic()
        logger.info("BSL Phase A: preparing SQLite pending epoch...")
        pending_epoch, resume_mode = self.sqlite.begin_or_resume_pending(
            scope,
            fingerprint=fingerprint,
            source_state_hash=source_state_hash,
            total_routines=total_routines,
            force_fresh=force_fresh,
        )
        logger.info(
            "BSL Phase A: pending_epoch=%d mode=%s (routines=%d)",
            pending_epoch, resume_mode, total_routines,
        )

        if resume_mode == "fresh":
            try:
                self.sqlite.set_vector_status(scope, "not_started")
            except Exception as e:
                logger.warning("BSL Phase A: failed to reset vector_status: %s", e)

        # Resume of a pending epoch may carry Phase B done-markers + Neo4j
        # vectors written by a startup overlap BEFORE commit_pending, under the
        # embedding contract of the crashed process. The pending epoch stores no
        # embedding fingerprint, and begin_or_resume_pending judges compatibility
        # by the Phase A fingerprint + source only (which excludes embedding
        # model/prompt/transport). So a restart under a CHANGED embedding
        # contract would otherwise trust those old-vector-space markers and skip
        # re-embedding, committing `ready` with a mixed vector space
        # (R3-impl-F2). Drop the pending-overlap Phase B state on resume so the
        # final Phase B re-embeds every not-committed-done unit under the current
        # contract. Cheap no-op when no overlap markers exist (scheduled/no
        # overlap). `fresh` never has them (monotonic epoch).
        if resume_mode in ("resume_writing", "resume_finalizing"):
            self.cleanup_phase_b_pending_epoch(scope, pending_epoch)

        # ----- State-aware resume (plan decision #2) ---------------------
        skip_until_rel_path = ""   # if non-empty, fetch starts strictly AFTER this
        cleanup_in_progress = False
        if resume_mode == "resume_writing":
            last = self.sqlite.read_last_in_progress_rel_path(scope, pending_epoch)
            if last:
                state = self.sqlite.classify_last_rel_path_state(
                    scope, pending_epoch, last,
                )
                if state == "in_progress":
                    logger.info(
                        "BSL Phase A resume: in-progress rel_path=%s — atomic cleanup",
                        last,
                    )
                    cleaned_rids = self.sqlite.cleanup_in_progress_rel_path(
                        scope, pending_epoch, last,
                    )
                    # Drop any same-epoch pending overlap vectors for the
                    # reprocessed routines so re-embedding is not shadowed by
                    # stale (visible=false) vectors / done-markers (R1-F2).
                    self._cleanup_neo4j_pending_overlap(
                        scope, pending_epoch, cleaned_rids,
                    )
                    cleanup_in_progress = True
                    # body fetch starts from this rel_path inclusive
                    skip_until_rel_path = ""  # see resume_keyset_floor below
                    resume_keyset_floor = (last, "")
                elif state == "fully_flushed":
                    logger.info(
                        "BSL Phase A resume: rel_path=%s fully flushed — continue after",
                        last,
                    )
                    resume_keyset_floor = (last, "￿")  # strictly after
                else:
                    # invariant violation: force-drop pending epoch and
                    # restart Phase A from scratch.
                    logger.error(
                        "BSL Phase A resume: invariant violation at rel_path=%s "
                        "(units/done + module_fts inconsistent) — dropping pending epoch",
                        last,
                    )
                    self.sqlite.set_vector_status(scope, "not_started")
                    # Drop this epoch's Phase B markers + same-epoch pending
                    # overlap vectors before abandoning it (R1-F2).
                    self.cleanup_phase_b_pending_epoch(scope, pending_epoch)
                    self.sqlite.drop_pending_epoch(scope)
                    # Restart with a fresh pending epoch.
                    return self._run_phase_a(
                        total_routines, fingerprint, source_state_hash,
                        force_fresh=True,
                        committed_units_sink=committed_units_sink,
                    )
            else:
                resume_keyset_floor = ("", "")
        else:
            resume_keyset_floor = ("", "")

        if resume_mode != "resume_finalizing":
            self._phase_a_streaming_write(
                scope=scope,
                pending_epoch=pending_epoch,
                total_routines=total_routines,
                resume_keyset_floor=resume_keyset_floor,
                committed_units_sink=committed_units_sink,
            )

        self.sqlite.mark_phase_a_writing_complete(scope, pending_epoch)

        # Module metadata: still built from persisted units (unchanged).
        self._finalize_phase_a_modules(scope, pending_epoch)

        # corpus_idf / corpus_stats and module FTS are already durable at
        # module boundaries in _phase_a_streaming_write. Finalize is just
        # the epoch switch.

        committed = self.sqlite.commit_pending(scope, fingerprint, source_state_hash)

        # Durable Phase B transfer snapshot was captured inside
        # begin_or_resume_pending BEFORE this Phase A reset vector_status
        # to 'not_started' and BEFORE commit_pending cleared
        # embedding_fingerprint. Reading it from SQLite (instead of in-memory
        # state captured right before commit_pending) makes the transfer
        # decision survive restart/crash mid-Phase-A and also fixes the
        # ordering bug where the live row had already been reset by the time
        # we tried to read it.
        snapshot = self.sqlite.read_phase_b_transfer_snapshot(scope)

        self._maybe_transfer_phase_b_state(
            scope=scope,
            prev_current_epoch=snapshot["prev_current_epoch"],
            new_current_epoch=committed,
            prev_phase_a_fp=snapshot["prev_phase_a_fingerprint"],
            new_phase_a_fp=fingerprint,
            prev_embedding_fp=snapshot["prev_embedding_fingerprint"],
            prev_vector_epoch=snapshot["prev_vector_epoch"],
            prev_vector_status=snapshot["prev_vector_status"],
        )

        logger.info("BSL Phase A: cleaning Neo4j BSL state...")
        self._cleanup_neo4j_bsl_state(scope, current_epoch=committed)
        logger.info(
            "BSL Phase A: committed epoch=%d mode=%s (routines=%d, "
            "source_hash=%s.., elapsed=%s)",
            committed, resume_mode, total_routines, source_state_hash[:12],
            _format_elapsed(time.monotonic() - phase_started),
        )

    def _finalize_phase_a_modules(self, scope: str, pending_epoch: int) -> None:
        batch_size = max(1, int(settings.bsl_code_phase_a_module_commit_batch))
        started = time.monotonic()
        buffer: List[Dict[str, Any]] = []
        written = 0
        failed = 0
        batches = 0

        def _flush() -> None:
            nonlocal written, failed, batches
            if not buffer:
                return
            self._heartbeat_lease()
            try:
                written += self.sqlite.write_modules_batch(
                    scope, pending_epoch, buffer,
                )
            except Exception:
                logger.debug(
                    "BSL Phase A: write_modules_batch failed, falling back to per-module writes",
                    exc_info=True,
                )
                for mod in buffer:
                    try:
                        self.sqlite.write_module(
                            scope=scope, epoch=pending_epoch, module=mod,
                        )
                        written += 1
                    except Exception as e:
                        failed += 1
                        logger.debug(
                            "BSL Phase A: write_module failed for %s: %s",
                            mod.get("module_id"), e,
                        )
            batches += 1
            buffer.clear()

        for mod in self.sqlite.iter_module_metadata_for_rebuild(scope, pending_epoch):
            buffer.append(mod)
            if len(buffer) >= batch_size:
                _flush()
        _flush()

        logger.info(
            "BSL Phase A finalize modules: modules=%d batches=%d failed=%d elapsed=%s",
            written, batches, failed,
            _format_elapsed(time.monotonic() - started),
        )

    def _phase_a_streaming_write(
        self,
        scope: str,
        pending_epoch: int,
        total_routines: int,
        resume_keyset_floor: Tuple[str, str],
        committed_units_sink: Optional[Any] = None,
    ) -> None:
        """
        Drive the body fetch generator -> ProcessPoolExecutor.map -> ordered
        stream -> per-module flush pipeline. See _run_phase_a docstring for
        the high-level protocol.

        `committed_units_sink(pending_epoch, unit_rows)` (optional, startup
        overlap): best-effort observer called after each durable SQLite commit
        with the flat unit rows (the `item["unit"]` dicts — same columns
        `iter_phase_b_not_done_units` yields). SQLite-commit-then-notify keeps
        the "SQLite before queue" invariant; a slow/failing sink never blocks or
        breaks Phase A.
        """
        from concurrent.futures import ProcessPoolExecutor
        from queue import Queue, Empty
        import threading
        from .bsl_code_phase_a_worker import process_batch as _worker_process_batch

        workers_n = max(1, int(settings.bsl_code_phase_a_workers))
        work_batch_routines = max(1, int(settings.bsl_code_phase_a_work_batch_routines))
        work_batch_max_bytes = (
            max(1, int(settings.bsl_code_phase_a_work_batch_max_mb)) * 1024 * 1024
        )
        write_batch_units = max(1, int(settings.bsl_code_phase_a_write_batch_units))
        module_commit_batch_size = max(
            1, int(settings.bsl_code_phase_a_module_commit_batch)
        )
        prefetch_n = max(1, int(settings.bsl_code_routine_prefetch_batches))
        strategy = settings.bsl_code_split_strategy

        progress = _ProgressLogger(
            "BSL Phase A", total_routines, BSL_PHASE_A_PROGRESS_ROUTINES,
            log=logger,
        )
        profiler = (
            _PhaseADebugProfiler(total_routines)
            if logger.isEnabledFor(logging.DEBUG) else None
        )

        # ----- Prefetch thread: Neo4j body batches into a bounded queue --
        body_queue: "Queue[Optional[List[Dict[str, Any]]]]" = Queue(maxsize=prefetch_n)
        fetch_error: List[BaseException] = []

        def _prefetch_loop() -> None:
            try:
                for batch in self._iter_routine_body_batches_from(resume_keyset_floor):
                    body_queue.put(batch)
                body_queue.put(None)
            except BaseException as e:
                fetch_error.append(e)
                body_queue.put(None)

        fetch_thread = threading.Thread(
            target=_prefetch_loop, name="bsl_phase_a_prefetch", daemon=True,
        )
        fetch_thread.start()

        # ----- Pack work items: re-batch fetched routines under count+byte
        # budgets, pre-parse owner_qn (production parser stays in main),
        # assign routine_ordinal in (rel_path, routine_id) order.
        # Packer is the single source of truth for count/byte caps + ordinal
        # quirk (see _PhaseAPacker docstring). Scoped Phase 5A uses the same
        # builder.
        packer = _PhaseAPacker(
            work_batch_routines=work_batch_routines,
            work_batch_max_bytes=work_batch_max_bytes,
            ordinal_start=1,
        )
        processed_routines = 0
        units_written = 0
        skipped_empty = 0
        split_failed = 0
        sqlite_batches = 0

        # Module aggregator state — one rel_path at a time in main RAM.
        current_rel_path: Optional[str] = None
        module_acc: Dict[str, List[Any]] = {
            "object_name": "", "form_name": "", "metadata_type_ru": "",
            "module_kind": "",
            "symbols": [], "region_names": [], "headers": [],
            "comments": [], "body_tokens": [],
        }

        # Pending write buffer per current rel_path. Flushed when
        # (a) it exceeds write_batch_units, or
        # (b) rel_path changes before the module commit.
        unit_write_buffer: List[Dict[str, Any]] = []
        method_write_buffer: List[Dict[str, Any]] = []
        routines_in_buffer: List[Dict[str, Any]] = []
        # Per-routine module fragments collected for the current rel_path.
        # Persisted into bsl_code_module_fragments together with the matching
        # unit writes (invariant 1:1 with bsl_code_units rows) so scoped
        # module FTS rebuild can rely on them.
        fragment_write_buffer: List[Dict[str, Any]] = []
        module_idf_batch: Dict[str, Dict[str, int]] = {}
        module_stats_batch: Dict[str, Tuple[int, int]] = {}
        current_module_had_mid_flush = False
        module_commit_buffer: List[PhaseAModuleCommit] = []
        module_commit_buffer_units = 0

        def _emit_committed(unit_rows: List[Dict[str, Any]]) -> None:
            """Best-effort notify the overlap sink of just-committed units.
            Only called AFTER a successful SQLite commit (SQLite before queue).
            Never raises — a failing/slow sink must not break Phase A."""
            if committed_units_sink is None or not unit_rows:
                return
            try:
                committed_units_sink(pending_epoch, unit_rows)
            except Exception as e:  # noqa: BLE001 — Phase A must not fail here
                logger.debug(
                    "BSL Phase A: committed_units_sink raised (ignored): %s", e,
                )

        def _flush_writes() -> None:
            nonlocal sqlite_batches
            if (not unit_write_buffer and not routines_in_buffer
                    and not method_write_buffer and not fragment_write_buffer):
                return
            debug_start = profiler.start() if profiler is not None else None
            debug_units = len(unit_write_buffer)
            debug_methods = len(method_write_buffer)
            emitted_units = (
                [u["unit"] for u in unit_write_buffer]
                if committed_units_sink is not None else None
            )
            try:
                self.sqlite.flush_phase_a_units_batch(
                    scope=scope,
                    epoch=pending_epoch,
                    units=unit_write_buffer,
                    done_routines=routines_in_buffer,
                    methods=method_write_buffer,
                    module_fragments=fragment_write_buffer,
                )
            finally:
                if profiler is not None and debug_start is not None:
                    elapsed = profiler.add_sqlite_flush(
                        debug_start,
                        units=debug_units,
                        methods=debug_methods,
                        idf_rows=0,
                        stats_rows=0,
                    )
                    profiler.log_slow_op(
                        "sqlite_flush", elapsed,
                        units=debug_units,
                        methods=debug_methods,
                        idf_rows=0,
                        stats_rows=0,
                    )
            sqlite_batches += 1
            if emitted_units:
                _emit_committed(emitted_units)
            unit_write_buffer.clear()
            method_write_buffer.clear()
            routines_in_buffer.clear()
            fragment_write_buffer.clear()

        def _reset_module_buffers() -> None:
            module_acc.clear()
            module_acc.update({
                "object_name": "", "form_name": "", "metadata_type_ru": "",
                "module_kind": "",
                "symbols": [], "region_names": [], "headers": [],
                "comments": [], "body_tokens": [],
            })
            module_idf_batch.clear()
            module_stats_batch.clear()

        def _module_columns() -> Dict[str, str]:
            return {
                "object_name": _token_text(module_acc["object_name"]),
                "form_name": _token_text(module_acc["form_name"]),
                "metadata_type_ru": _token_text(module_acc["metadata_type_ru"]),
                "module_kind": _token_text(module_acc["module_kind"]),
                "symbols": _token_text(" ".join(module_acc["symbols"])),
                "region_names": _token_text(" ".join(module_acc["region_names"])),
                "headers": _token_text(" ".join(module_acc["headers"])),
                "comments": _token_text(" ".join(module_acc["comments"])),
                "body": " ".join(module_acc["body_tokens"]),
            }

        def _reset_current_module_buffers() -> None:
            nonlocal current_module_had_mid_flush
            unit_write_buffer.clear()
            method_write_buffer.clear()
            routines_in_buffer.clear()
            fragment_write_buffer.clear()
            _reset_module_buffers()
            current_module_had_mid_flush = False

        def _current_module_commit(rel_path_to_flush: str) -> PhaseAModuleCommit:
            return PhaseAModuleCommit(
                rel_path=rel_path_to_flush,
                columns=_module_columns(),
                units=list(unit_write_buffer),
                done_routines=list(routines_in_buffer),
                methods=list(method_write_buffer),
                idf_increments={
                    fk: dict(token_to_df)
                    for fk, token_to_df in module_idf_batch.items()
                },
                stats_increments=dict(module_stats_batch),
                module_fragments=list(fragment_write_buffer),
            )

        def _flush_module_commit_batch() -> None:
            nonlocal module_commit_buffer_units
            if not module_commit_buffer:
                return
            debug_start = profiler.start() if profiler is not None else None
            debug_modules = len(module_commit_buffer)
            debug_units = module_commit_buffer_units
            debug_methods = sum(len(m.methods) for m in module_commit_buffer)
            emitted_units = (
                [u["unit"] for m in module_commit_buffer for u in m.units]
                if committed_units_sink is not None else None
            )
            try:
                self.sqlite.commit_phase_a_modules_batch_with_writes(
                    scope=scope,
                    epoch=pending_epoch,
                    modules=module_commit_buffer,
                )
            finally:
                if profiler is not None and debug_start is not None:
                    profiler.add_combined_boundary_write_rows(
                        units=debug_units,
                        methods=debug_methods,
                    )
                    elapsed = profiler.add_module_flush(
                        debug_start, modules=debug_modules,
                    )
                    profiler.log_slow_op(
                        "module_batch_flush", elapsed,
                        modules=debug_modules,
                        units=debug_units,
                        methods=debug_methods,
                    )
            if emitted_units:
                _emit_committed(emitted_units)
            module_commit_buffer.clear()
            module_commit_buffer_units = 0

        def _commit_module_boundary_immediate(rel_path_to_flush: str) -> None:
            # Heartbeat для scheduler_lock на каждой module-flush границе —
            # естественная точка между chunked writes Phase A. Если lease=None
            # (startup helper), no-op без overhead.
            self._heartbeat_lease()
            if not rel_path_to_flush:
                _flush_writes()
                _reset_current_module_buffers()
                return
            debug_start = profiler.start() if profiler is not None else None
            debug_units = len(unit_write_buffer)
            debug_methods = len(method_write_buffer)
            emitted_units = (
                [u["unit"] for u in unit_write_buffer]
                if committed_units_sink is not None else None
            )
            try:
                self.sqlite.commit_phase_a_module_with_writes(
                    scope=scope,
                    epoch=pending_epoch,
                    rel_path=rel_path_to_flush,
                    columns=_module_columns(),
                    units=unit_write_buffer,
                    done_routines=routines_in_buffer,
                    methods=method_write_buffer,
                    idf_increments=module_idf_batch,
                    stats_increments=module_stats_batch,
                    module_fragments=fragment_write_buffer,
                )
            finally:
                if profiler is not None and debug_start is not None:
                    profiler.add_combined_boundary_write_rows(
                        units=debug_units,
                        methods=debug_methods,
                    )
                    elapsed = profiler.add_module_flush(debug_start)
                    profiler.log_slow_op(
                        "module_flush", elapsed,
                        rel_path=rel_path_to_flush,
                        units=debug_units,
                        methods=debug_methods,
                    )
            if emitted_units:
                _emit_committed(emitted_units)
            _reset_current_module_buffers()

        def _enqueue_module_boundary(rel_path_to_flush: str) -> None:
            nonlocal module_commit_buffer_units
            if not rel_path_to_flush:
                _flush_writes()
                _reset_current_module_buffers()
                return
            module_commit = _current_module_commit(rel_path_to_flush)
            module_commit_buffer.append(module_commit)
            module_commit_buffer_units += len(module_commit.units)
            _reset_current_module_buffers()
            if (len(module_commit_buffer) >= module_commit_batch_size
                    or module_commit_buffer_units >= write_batch_units):
                _flush_module_commit_batch()

        def _finish_current_module_boundary(rel_path_to_flush: str) -> None:
            if current_module_had_mid_flush:
                _flush_module_commit_batch()
                _commit_module_boundary_immediate(rel_path_to_flush)
            else:
                _enqueue_module_boundary(rel_path_to_flush)

        def _absorb_fragment(frag: Dict[str, Any]) -> None:
            module_acc["object_name"] = module_acc["object_name"] or frag.get("object_name", "")
            module_acc["form_name"] = module_acc["form_name"] or frag.get("form_name", "")
            module_acc["metadata_type_ru"] = module_acc["metadata_type_ru"] or frag.get("metadata_type_ru", "")
            module_acc["module_kind"] = module_acc["module_kind"] or frag.get("module_kind", "")
            sym = (frag.get("symbol") or "").strip()
            if sym:
                module_acc["symbols"].append(sym)
            rn = (frag.get("region_names") or "").strip()
            if rn:
                module_acc["region_names"].append(rn)
            hdr = (frag.get("headers") or "").strip()
            if hdr:
                module_acc["headers"].append(hdr)
            cmt = (frag.get("comments") or "").strip()
            if cmt:
                module_acc["comments"].append(cmt)
            body_tok = (frag.get("body_tokens_text") or "").strip()
            if body_tok:
                module_acc["body_tokens"].append(body_tok)
            # Persist per-routine fragment alongside the unit row so scoped
            # module FTS rebuild has a 1:1 source set (plan §2.2 invariant).
            fragment_write_buffer.append(frag)

        # ----- Outer iteration: drive prefetch -> work_batches -> workers
        with ProcessPoolExecutor(max_workers=workers_n) as executor:
            pending_futures: List[Any] = []

            def _drain_one_future_blocking() -> None:
                # Pop and process the head of pending_futures in order.
                # ProcessPoolExecutor preserves submit order via list order.
                fut = pending_futures.pop(0)
                if profiler is not None:
                    wait_started = profiler.start()
                    result = fut.result()
                    wait_elapsed = profiler.add_future_wait(wait_started)
                    profiler.log_slow_op("future_wait", wait_elapsed)
                    profiler.add_worker_timings(result.get("debug_timings"))
                    absorb_started = profiler.start()
                    nested_flush_ms_before = (
                        profiler.sqlite_flush_ms + profiler.module_flush_ms
                    )
                    _absorb_worker_result(result)
                    profiler.add_absorb_exclusive(
                        absorb_started, nested_flush_ms_before,
                    )
                else:
                    result = fut.result()
                    _absorb_worker_result(result)

            def _absorb_worker_result(result: Dict[str, Any]) -> None:
                nonlocal current_rel_path, processed_routines, units_written
                nonlocal skipped_empty, split_failed, current_module_had_mid_flush
                # Per-routine processing in worker order (routines_done
                # preserves batch order). For each routine we (a) detect
                # rel_path boundary BEFORE adding the routine's data —
                # boundary commits the PREVIOUS module, including any
                # pending units/done rows and its IDF/stats deltas; (b)
                # append the routine's full payload into the current module
                # buffers.
                units_by_rid: Dict[str, List[Dict[str, Any]]] = {}
                for ur in result["unit_rows"]:
                    units_by_rid.setdefault(ur["unit"]["routine_id"], []).append(ur)
                methods_by_rid: Dict[str, List[Dict[str, Any]]] = {}
                for mr in result["method_rows"]:
                    methods_by_rid.setdefault(mr["routine_id"], []).append(mr)
                idf_contrib_by_rid: Dict[str, List[Dict[str, Any]]] = {}
                for c in result["idf_contributions"]:
                    idf_contrib_by_rid.setdefault(c["routine_id"], []).append(c)
                stats_contrib_by_rid: Dict[str, List[Dict[str, Any]]] = {}
                for c in result["stats_contributions"]:
                    stats_contrib_by_rid.setdefault(c["routine_id"], []).append(c)
                fragment_by_rid: Dict[str, Dict[str, Any]] = {
                    f["routine_id"]: f for f in result["module_fragments"]
                }

                for done_row in result["routines_done"]:
                    rid = done_row["routine_id"]
                    # Determine the routine's rel_path: prefer unit row
                    # (always present when units were produced), fall back
                    # to module_fragment.rel_path; for skipped_empty /
                    # split_failed routines there's no rel_path and we
                    # treat them as belonging to the current module.
                    rp: Optional[str] = None
                    if units_by_rid.get(rid):
                        rp = units_by_rid[rid][0]["unit"]["rel_path"]
                    elif fragment_by_rid.get(rid):
                        rp = fragment_by_rid[rid].get("rel_path")
                    # Boundary check: any non-empty rp different from
                    # current triggers the previous-module flush.
                    if rp and current_rel_path is None:
                        current_rel_path = rp
                    elif rp and rp != current_rel_path:
                        # Flush previous module — buffer now holds the full
                        # payload of every routine of the previous rel_path.
                        _finish_current_module_boundary(current_rel_path)
                        current_rel_path = rp

                    # Append this routine's data into the active buffers.
                    # IDF/stats stay module-scoped until module boundary.
                    for ur in units_by_rid.get(rid, []):
                        unit_write_buffer.append(ur)
                    for mr in methods_by_rid.get(rid, []):
                        method_write_buffer.append(mr)
                    routines_in_buffer.append(done_row)
                    for c in idf_contrib_by_rid.get(rid, []):
                        dst = module_idf_batch.setdefault(c["field_kind"], {})
                        dst[c["token"]] = dst.get(c["token"], 0) + int(c["df"])
                    for c in stats_contrib_by_rid.get(rid, []):
                        prev_dc, prev_tl = module_stats_batch.get(
                            c["field_kind"], (0, 0)
                        )
                        module_stats_batch[c["field_kind"]] = (
                            prev_dc + int(c["doc_count_delta"]),
                            prev_tl + int(c["total_length_delta"]),
                        )
                    frag = fragment_by_rid.get(rid)
                    if frag:
                        _absorb_fragment(frag)

                    # Mid-batch write flush within the SAME module. Corpus
                    # deltas and module FTS still commit at module boundary.
                    if len(unit_write_buffer) >= write_batch_units:
                        _flush_module_commit_batch()
                        _flush_writes()
                        current_module_had_mid_flush = True

                processed_routines += len(result["routines_done"])
                units_written += sum(
                    d["units_written"] for d in result["routines_done"]
                )
                skipped_empty += result["skipped_empty"]
                split_failed += result["split_failed"]

            # ----- Pull batches from the prefetch queue; pack work items
            # via shared _PhaseAPacker. submit closed packs to executor.

            def _submit_pack(
                pack_records: List[Dict[str, Any]],
                pack_ordinals: Dict[str, int],
            ) -> None:
                if not pack_records:
                    return
                # Pre-parse owner_qn in main using production parser so
                # workers don't need to depend on parse_owner_qn semantics.
                debug_start = profiler.start() if profiler is not None else None
                try:
                    for r in pack_records:
                        meta_type, object_name, form_name = parse_owner_qn(
                            r.get("owner_qn")
                        )
                        r["_meta_type_ru"] = meta_type
                        r["_object_name"] = object_name
                        r["_form_name"] = form_name
                    if profiler is not None:
                        fut = executor.submit(
                            _worker_process_batch,
                            pack_records, strategy, pack_ordinals, True,
                        )
                    else:
                        fut = executor.submit(
                            _worker_process_batch,
                            pack_records, strategy, pack_ordinals,
                        )
                finally:
                    if profiler is not None and debug_start is not None:
                        profiler.add_submit(debug_start)
                # Адрес пакета до отправки: main знает его состав, поэтому
                # последняя такая строка перед остановкой прогресса прямо
                # указывает на подозреваемые модули.
                if logger.isEnabledFor(logging.DEBUG):
                    first_path = (pack_records[0].get("file_path") or "").strip()
                    last_path = (pack_records[-1].get("file_path") or "").strip()
                    logger.debug(
                        "BSL Phase A: submitted pack routines=%d chars=%d "
                        "rel_path=%s..%s",
                        len(pack_records),
                        sum(len(r.get("body") or "") for r in pack_records),
                        first_path, last_path,
                    )
                pending_futures.append(fut)
                # Backpressure: while we have many in-flight pending futures
                # blocking a result lets main keep up and bounds RAM.
                while len(pending_futures) >= workers_n * 2:
                    _drain_one_future_blocking()
                    progress.maybe_log(
                        processed_routines,
                        units=units_written, skipped_empty=skipped_empty,
                        split_failed=split_failed,
                        rss_mb=_process_rss_mb(),
                    )
                    if profiler is not None:
                        profiler.maybe_log(
                            processed=processed_routines,
                            units=units_written,
                        )

            try:
                while True:
                    if profiler is not None:
                        queue_started = profiler.start()
                        item = body_queue.get()
                        profiler.add_queue_wait(queue_started)
                    else:
                        item = body_queue.get()
                    if item is None:
                        break
                    for r in item:
                        closed = packer.add(r)
                        if closed is not None:
                            _submit_pack(*closed)
                    if profiler is not None:
                        profiler.maybe_log(
                            processed=processed_routines,
                            units=units_written,
                        )
                trailing = packer.flush()
                if trailing is not None:
                    _submit_pack(*trailing)
            finally:
                # Drain remaining futures in order.
                while pending_futures:
                    _drain_one_future_blocking()
                # Final flush of write buffer and module aggregate.
                if current_rel_path:
                    _finish_current_module_boundary(current_rel_path)
                elif unit_write_buffer or routines_in_buffer or method_write_buffer:
                    _flush_writes()
                _flush_module_commit_batch()

            if fetch_error:
                raise fetch_error[0]

        progress.maybe_log(
            processed_routines, final=True, units=units_written,
            skipped_empty=skipped_empty, split_failed=split_failed,
        )
        if profiler is not None:
            profiler.maybe_log(
                processed=processed_routines,
                units=units_written,
                final=True,
            )

    def _iter_routine_body_batches_from(
        self, floor: Tuple[str, str],
    ) -> Iterable[List[Dict[str, Any]]]:
        """
        Wrap _iter_routine_body_batches with a state-aware floor for
        resume. `floor = (rel_path, routine_id)` advances the keyset
        cursor before the first fetch. Pass ("", "") for fresh start;
        pass (last_rel_path, "\\uffff") to skip a fully-flushed module;
        pass (last_rel_path, "") to include the in-progress module
        (after it has been cleanup'd from SQLite).
        """
        size = int(settings.bsl_code_routine_fetch_batch_size)
        last_rel_path, last_routine_id = floor
        with self.driver.session(database=settings.neo4j_database) as session:
            while True:
                res = session.run(
                    CYPHER_FETCH_ROUTINES_BODY_BATCH,
                    project_name=self.scope,
                    last_rel_path=last_rel_path,
                    last_routine_id=last_routine_id,
                    batch_size=size,
                )
                batch = [dict(record) for record in res]
                if not batch:
                    return
                yield batch
                last = batch[-1]
                last_rel_path = (last.get("file_path") or "").strip()
                last_routine_id = last.get("routine_id") or ""

    # ------------------------------------------------------------------ Phase B

    def _run_phase_b_if_enabled(
        self,
        current_epoch: int,
        *,
        mark_ready_on_success: bool = True,
    ) -> PhaseBOutcome:
        """
        Async Phase B coordinator (plan decision #3): N async workers,
        partitioned by fts_rowid % total_workers. Workers stream
        not-done units from SQLite, batch-fetch Neo4j body for the
        unique routine_ids of each batch, build embedding text via
        the existing UnitContext path, and call the SYNC embedding
        service through asyncio.to_thread to avoid blocking the event
        loop (plan #3 sync→async bridging contract, mirroring
        routine_indexer.py:226-227).

        Phase B is NO LONGER called with a routines list — its source
        is bsl_code_units of the current epoch (plan #6: no re-split).

        Return contract (see PhaseBOutcome): SUCCESS / SKIPPED, or
        raises on failure (vector_status set to 'failed' before raising).
        When `mark_ready_on_success=False`, on success the coordinator
        leaves status at `building` so a caller can run visibility sync
        and commit `ready+policy` atomically.
        """
        if not self._bsl_vector_enabled():
            logger.info("BSL Phase B: skipped (ENABLE_BSL_CODE_EMBEDDING=false or master flag off)")
            return PhaseBOutcome.SKIPPED

        if current_epoch <= 0:
            logger.warning("BSL Phase B: no committed SQLite epoch, skipping")
            return PhaseBOutcome.SKIPPED

        # Startup preflight: a known-unavailable endpoint (from the one-shot
        # startup probe) short-circuits Phase B WITHOUT touching the endpoint —
        # but ONLY for scheduled/recovery runs, where re-probing a dead endpoint
        # every empty cycle is wasteful. For run_mode="startup" the static probe
        # is a DEGRADED signal, not terminal (R3-impl-F1): the endpoint may have
        # recovered during a multi-hour Phase A, so the startup 12×300s rounds
        # must actually attempt it. The rounds' own embedding calls fail/retry if
        # it is still down, and exhaustion leaves vector_status=failed as before.
        avail = self._embedding_availability
        if (
            avail is not None and avail.enabled and not avail.available
            and getattr(self, "_run_mode", "scheduled") != "startup"
        ):
            logger.warning(
                "BSL Phase B: embedding unavailable at startup (%s); "
                "vector_status=failed, Phase A/RLM fallback unaffected",
                avail.reason,
            )
            self.sqlite.set_vector_status(self.scope, "failed")
            raise EmbeddingUnavailableError(
                f"BSL Phase B: embedding unavailable at startup: {avail.reason}"
            )
        if (
            avail is not None and avail.enabled and not avail.available
            and getattr(self, "_run_mode", "scheduled") == "startup"
        ):
            logger.warning(
                "BSL Phase B: startup probe reported endpoint unavailable (%s) — "
                "treating as degraded; attempting startup retry rounds anyway",
                avail.reason,
            )

        embedding_service = self._get_embedding_service_or_none()
        if embedding_service is None:
            logger.warning("BSL Phase B: EmbeddingService unavailable; setting vector_status=failed")
            self.sqlite.set_vector_status(self.scope, "failed")
            raise EmbeddingUnavailableError("BSL Phase B: EmbeddingService unavailable")

        scope = self.scope

        # Stale-epoch sweep (unchanged): drop done markers from a prior
        # vector_epoch so they don't leak across epoch boundaries.
        prior_vec_state = self.sqlite.vector_state(scope)
        if (
            prior_vec_state.vector_epoch is not None
            and prior_vec_state.vector_epoch != current_epoch
        ):
            try:
                removed = self.sqlite.reset_phase_b_state(
                    scope, prior_vec_state.vector_epoch,
                )
                if removed:
                    logger.info(
                        "BSL Phase B: cleared %d stale unit progress rows "
                        "for vector_epoch=%d",
                        removed, prior_vec_state.vector_epoch,
                    )
            except Exception as e:
                logger.warning("BSL Phase B: reset_phase_b_state failed: %s", e)

        try:
            profile = resolve_bsl_code_prompt_profile(
                settings.embedding_model or "",
                settings.bsl_code_embedding_prompt_mode or "auto",
            )
        except ValueError as e:
            logger.error("BSL Phase B: invalid prompt mode: %s", e)
            self.sqlite.set_vector_status(scope, "failed")
            raise

        transport = resolve_effective_embedding_transport(
            settings.embedding_api_base or "",
            getattr(settings, "embedding_transport", "auto") or "auto",
        )
        doc_spec = build_embedding_format_spec(
            profile=profile,
            transport=transport,
            side="document",
            purpose="code",
            description_instruction="",
        )

        # Stamp the embedding contract at building-start (not just on success):
        # embedding_fingerprint marks vector-space compatibility, so a Phase B
        # that fails under this contract keeps stored fp == current fp and is
        # resumed on the next run instead of triggering a full re-embed. Written
        # only after the prompt profile resolved (same resolution the
        # fingerprint depends on), so an invalid prompt mode still fails fast.
        current_embedding_fp = self._compute_embedding_fingerprint()
        self.sqlite.set_vector_status(
            scope, "building",
            vector_epoch=current_epoch,
            embedding_fingerprint=current_embedding_fp,
        )

        workers_n = max(1, int(settings.bsl_code_phase_b_workers))
        batch_size = max(1, int(settings.bsl_code_embedding_batch_size))
        excluded_owner_categories = search_policy.normalize_excluded_categories(
            settings.bsl_code_embedding_excluded_owner_categories or ()
        )
        exclude_regulated_reports = bool(
            settings.bsl_code_search_exclude_regulated_reports
        )
        units_total = self.sqlite.count_phase_b_units(
            scope, current_epoch,
            excluded_owner_categories=excluded_owner_categories,
            exclude_regulated_reports=exclude_regulated_reports,
        )
        units_done = self.sqlite.count_phase_b_done_units(
            scope, current_epoch,
            epoch=current_epoch,
            excluded_owner_categories=excluded_owner_categories,
            exclude_regulated_reports=exclude_regulated_reports,
        )
        units_remaining = self.sqlite.count_phase_b_not_done_units(
            scope, current_epoch, current_epoch,
            excluded_owner_categories=excluded_owner_categories,
            exclude_regulated_reports=exclude_regulated_reports,
        )
        logger.info(
            "BSL Phase B: building embeddings under vector_epoch=%d "
            "(workers=%d, embedding_batch=%d, profile=%s, "
            "units_total=%d, units_done=%d, units_remaining=%d)",
            current_epoch, workers_n, batch_size, profile,
            units_total, units_done, units_remaining,
        )

        phase_started = time.monotonic()
        policy = _phase_b_run_policy(getattr(self, "_run_mode", "scheduled"))
        max_rounds = policy.max_rounds

        def _remaining() -> int:
            return self.sqlite.count_phase_b_not_done_units(
                scope, current_epoch, current_epoch,
                excluded_owner_categories=excluded_owner_categories,
                exclude_regulated_reports=exclude_regulated_reports,
            )

        async def _round(round_idx: int, remaining_before: int) -> _PhaseBStats:
            return await self._run_phase_b_async(
                current_epoch=current_epoch,
                workers_n=workers_n,
                batch_size=batch_size,
                embedding_service=embedding_service,
                doc_spec=doc_spec,
                progress_total_units=remaining_before,
                round_label=f"{round_idx}/{max_rounds}",
            )

        async def _sleep(delay: float) -> None:
            await self._async_sleep_with_heartbeat(delay, self._heartbeat_lease)

        # Shared outer-round loop (R2-F5 classification: retry only on
        # exception, benign terminal skips still count as success).
        outcome = asyncio.run(
            self._run_phase_b_rounds(
                policy=policy,
                remaining_fn=_remaining,
                round_fn=_round,
                sleep_fn=_sleep,
                label="full",
            )
        )
        phase_stats = outcome.last_stats or _PhaseBStats()
        if not outcome.succeeded:
            e = outcome.last_exc
            remaining_after = _remaining()
            # fp=None / vector_epoch=None preserve the building-start stamps.
            self.sqlite.set_vector_status(scope, "failed")
            if is_embedding_unavailable_error(e):
                # Expected external outage after preflight succeeded: one line,
                # no traceback. Search falls back to RLM until the next cycle.
                logger.warning(
                    "BSL Phase B: embedding endpoint unavailable after %d rounds, "
                    "units_remaining=%d, status=failed: %s",
                    max_rounds, remaining_after, e,
                )
                raise EmbeddingUnavailableError(str(e)) from e
            logger.error(
                "BSL Phase B: exhausted %d rounds, units_remaining=%d, "
                "status=failed: %s",
                max_rounds, remaining_after, e, exc_info=True,
            )
            assert e is not None
            raise e

        if mark_ready_on_success:
            self.sqlite.set_vector_status(
                scope, "ready", vector_epoch=current_epoch,
                embedding_fingerprint=current_embedding_fp,
            )
            ready_marker = "status=ready"
        else:
            # Caller will set ready atomically with visibility sync + policy
            # commit; leave status at `building` here so no concurrent
            # search can flip to vector path mid-transition.
            ready_marker = "status=building (caller commits ready after sync)"
        # Usage/cost reflect the last successfully completed async round (the
        # one that returned phase_stats); partially-failed rounds are not folded
        # into a new public contract here — same as the unit/skip counters.
        _final_cost = phase_stats.primary_cost()
        logger.info(
            "BSL Phase B: committed vector_epoch=%d (%s, "
            "profile=%s, units_requested=%d, units_written=%d, "
            "skipped_missing_body=%d, skipped_hash_mismatch=%d, "
            "skipped_empty_text=%d, embedding_api_calls=%d, "
            "input_tokens=%s, total_tokens=%s, cost=%s, elapsed=%s)",
            current_epoch, ready_marker, profile,
            phase_stats.units_requested,
            phase_stats.units_written,
            phase_stats.skipped_missing_body,
            phase_stats.skipped_hash_mismatch,
            phase_stats.skipped_empty_text,
            phase_stats.embedding_api_calls,
            _format_usage_tokens(phase_stats.input_tokens),
            _format_usage_tokens(phase_stats.total_tokens),
            _format_cost(*_final_cost),
            _format_elapsed(time.monotonic() - phase_started),
        )
        return PhaseBOutcome.SUCCESS

    def _phase_b_round_backoff(self, round_idx: int) -> float:
        """Exponential backoff (with jitter) applied only between Phase B outer
        rounds; mirrors the embedding retry backoff pattern in config."""
        base = float(settings.bsl_code_phase_b_round_backoff_base_seconds)
        cap = float(settings.bsl_code_phase_b_round_backoff_max_seconds)
        jitter = float(settings.bsl_code_phase_b_round_backoff_jitter_seconds)
        delay = min(base * (2 ** (round_idx - 1)), cap)
        return delay + random.uniform(0.0, max(0.0, jitter))

    def _sleep_with_lease_heartbeat(self, delay: float) -> None:
        """Sleep `delay` seconds while keeping the scheduler lease alive. The
        full Phase B path runs under `start_indexing(lease)`; a bare
        `time.sleep` up to the backoff cap could exceed the lease heartbeat
        interval and let a competing cycle steal a "stale" lock. Chunk the
        sleep by `BSL_PROGRESS_SECONDS` and heartbeat before each chunk.
        `_heartbeat_lease` is a no-op when no lease is active."""
        deadline = time.monotonic() + max(0.0, delay)
        while True:
            self._heartbeat_lease()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, BSL_PROGRESS_SECONDS))

    async def _async_sleep_with_heartbeat(
        self, delay: float, heartbeat: Any,
    ) -> None:
        """Async heartbeat-aware inter-round sleep. `heartbeat` is a 0-arg
        callable invoked before each sleep chunk. Full/startup paths pass
        `self._heartbeat_lease` (heartbeats `self._active_lease`); the scoped
        path passes `lambda: _safe_heartbeat(lease)` because it runs under an
        EXPLICIT lease and never sets `self._active_lease` (see
        `_run_scoped_phase_b_async` / R1-plan-F2)."""
        deadline = time.monotonic() + max(0.0, delay)
        while True:
            if heartbeat is not None:
                heartbeat()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, BSL_PROGRESS_SECONDS))

    async def _run_phase_b_rounds(
        self,
        *,
        policy: PhaseBRunPolicy,
        remaining_fn,
        round_fn,
        sleep_fn,
        label: str,
    ) -> _PhaseBRoundsOutcome:
        """Shared Phase B outer-round loop (full, startup catch-up, scoped).

        `remaining_fn()` -> int not-done count (sync callable).
        `round_fn(round_idx, remaining_before)` -> awaitable returning per-round
            stats; runs one full pass over the still not-done units.
        `sleep_fn(delay)` -> awaitable heartbeat-aware inter-round sleep, chosen
            by the caller for its lease contract.

        Retry ONLY on exception; a clean coordinator return is success even if
        some units stay not-done (benign terminal skips). Returns
        `_PhaseBRoundsOutcome`; the caller performs the terminal action
        (set failed / raise / defer). Done-markers are never dropped between
        rounds, so a failed round keeps its progress and the next round finishes
        the remainder."""
        last_stats: Any = None
        for round_idx in range(1, policy.max_rounds + 1):
            remaining_before = int(remaining_fn())
            if remaining_before == 0:
                return _PhaseBRoundsOutcome(True, None, round_idx - 1, last_stats)
            logger.info(
                "BSL Phase B [%s]: round %d/%d started, units_remaining=%d",
                label, round_idx, policy.max_rounds, remaining_before,
            )
            try:
                last_stats = await round_fn(round_idx, remaining_before)
                return _PhaseBRoundsOutcome(True, None, round_idx, last_stats)
            except Exception as e:
                remaining_after = int(remaining_fn())
                if remaining_after == 0:
                    logger.info(
                        "BSL Phase B [%s]: round %d/%d raised but "
                        "units_remaining_after=0 — treating as success",
                        label, round_idx, policy.max_rounds,
                    )
                    return _PhaseBRoundsOutcome(True, None, round_idx, last_stats)
                if round_idx < policy.max_rounds:
                    delay = policy.backoff(round_idx)
                    logger.warning(
                        "BSL Phase B [%s]: round %d/%d failed, "
                        "units_remaining_after=%d, retrying in %.1fs: %s",
                        label, round_idx, policy.max_rounds,
                        remaining_after, delay, e,
                    )
                    await sleep_fn(delay)
                    continue
                return _PhaseBRoundsOutcome(False, e, round_idx, last_stats)
        return _PhaseBRoundsOutcome(True, None, policy.max_rounds, last_stats)

    async def _run_phase_b_async(
        self,
        current_epoch: int,
        workers_n: int,
        batch_size: int,
        embedding_service: Any,
        doc_spec: Any,
        progress_total_units: Optional[int] = None,
        round_label: Optional[str] = None,
    ) -> _PhaseBStats:
        """Async coordinator: launch N workers; any worker exception
        propagates (caller sets vector_status='failed')."""
        scope = self.scope
        if progress_total_units is None:
            progress_total_units = await asyncio.to_thread(
                lambda: self.sqlite.count_phase_b_not_done_units(
                    scope, current_epoch, current_epoch,
                    excluded_owner_categories=search_policy.normalize_excluded_categories(
                        settings.bsl_code_embedding_excluded_owner_categories or ()
                    ),
                    exclude_regulated_reports=bool(
                        settings.bsl_code_search_exclude_regulated_reports
                    ),
                )
            )
        progress = _PhaseBProgress(progress_total_units, round_label)

        async def _heartbeat_loop() -> None:
            while True:
                await asyncio.sleep(BSL_PROGRESS_SECONDS)
                await progress.heartbeat()

        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(), name="bsl_phase_b_progress_heartbeat",
        )
        tasks = [
            asyncio.create_task(
                self._phase_b_worker(
                    worker_id=i,
                    total_workers=workers_n,
                    current_epoch=current_epoch,
                    batch_size=batch_size,
                    embedding_service=embedding_service,
                    doc_spec=doc_spec,
                    progress=progress,
                ),
                name=f"bsl_phase_b_worker_{i}",
            )
            for i in range(workers_n)
        ]
        first_err: Optional[BaseException] = None
        final_stats = _PhaseBStats()
        try:
            self._heartbeat_lease()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            self._heartbeat_lease()
            excs = [r for r in results if isinstance(r, BaseException)]
            first_err = _log_phase_b_worker_excs("BSL Phase B worker raised", excs)
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            final_stats = await progress.final()
        if first_err is not None:
            raise first_err
        return final_stats

    async def _phase_b_worker(
        self,
        worker_id: int,
        total_workers: int,
        current_epoch: int,
        batch_size: int,
        embedding_service: Any,
        doc_spec: Any,
        progress: Optional[_PhaseBProgress] = None,
    ) -> None:
        """One Phase B async worker. Streams not-done units of its
        partition, batches them, fetches Neo4j body for each batch's
        unique routine_ids, builds embedding texts, calls the sync
        EmbeddingService via asyncio.to_thread (mandatory bridging
        per plan #3), writes vectors to Neo4j, marks units done."""
        scope = self.scope

        # SQLite reads are sync; we wrap them in to_thread to keep the
        # event loop free for other workers.
        def _stream_partition() -> Iterable[List[Dict[str, Any]]]:
            return self.sqlite.iter_phase_b_not_done_units(
                scope=scope,
                epoch=current_epoch,
                vector_epoch=current_epoch,
                worker_id=worker_id,
                total_workers=total_workers,
                batch_size=batch_size,
                excluded_owner_categories=search_policy.normalize_excluded_categories(
                    settings.bsl_code_embedding_excluded_owner_categories or ()
                ),
                exclude_regulated_reports=bool(
                    settings.bsl_code_search_exclude_regulated_reports
                ),
            )

        # Iterate sync generator one batch at a time so progress starts
        # immediately and memory stays bounded by one batch per worker.
        loop = asyncio.get_running_loop()
        iterator = _stream_partition()
        while True:
            batch = await loop.run_in_executor(None, lambda: next(iterator, None))
            if batch is None:
                return
            stats = await self._phase_b_process_batch(
                batch=batch,
                current_epoch=current_epoch,
                embedding_service=embedding_service,
                doc_spec=doc_spec,
            )
            if progress is not None:
                await progress.add(stats)

    async def _phase_b_process_batch(
        self,
        batch: List[Dict[str, Any]],
        current_epoch: int,
        embedding_service: Any,
        doc_spec: Any,
        *,
        visible_on_upsert: bool = True,
    ) -> _PhaseBStats:
        """Process one Phase B batch: Neo4j body fetch -> embedding text ->
        embedding API (via asyncio.to_thread) -> Neo4j vector write ->
        mark done."""
        stats = _PhaseBStats(units_requested=len(batch), batches=1 if batch else 0)
        if not batch:
            return stats
        scope = self.scope
        loop = asyncio.get_running_loop()

        # Step 1: Neo4j body batch for all unique routine_ids.
        unique_rids = sorted({r["routine_id"] for r in batch if r.get("routine_id")})
        body_map = await loop.run_in_executor(
            None, lambda: self._fetch_bodies_from_neo4j(unique_rids)
        )

        # Step 2: build embedding text for each unit (skip if body missing
        # or body_hash mismatch — benign skip, DO NOT mark done).
        prepared: List[Tuple[BslCodeEmbeddingJob, Dict[str, Any]]] = []
        for u in batch:
            rid = u.get("routine_id") or ""
            body_info = body_map.get(rid)
            if body_info is None:
                stats.skipped_missing_body += 1
                logger.debug(
                    "BSL Phase B: skip unit %s (routine_id=%s) — body missing in Neo4j",
                    u.get("unit_id"), rid,
                )
                continue
            stored_hash = (u.get("body_hash") or "").strip()
            current_hash = (body_info.get("body_hash") or "").strip()
            if stored_hash and current_hash and stored_hash != current_hash:
                stats.skipped_hash_mismatch += 1
                logger.debug(
                    "BSL Phase B: skip unit %s — body_hash drifted (sqlite=%s neo4j=%s)",
                    u.get("unit_id"), stored_hash[:8], current_hash[:8],
                )
                continue
            body = body_info.get("body") or ""
            text = self._build_phase_b_embedding_text(u, body)
            if not text:
                stats.skipped_empty_text += 1
                continue
            job = BslCodeEmbeddingJob(
                embedding_text=text,
                unit_kind=u.get("unit_kind") or "routine_code_unit",
                routine_id=rid,
                epoch=current_epoch,
                unit_id=u["unit_id"],
                project_name=scope,
                config_name=u.get("config_name") or "",
                owner_qn=u.get("owner_qn") or "",
                owner_qn_prefix=u.get("owner_qn_prefix") or "",
                owner_category=u.get("owner_category") or "",
                module_type=u.get("module_type") or "",
                routine_type=u.get("routine_type") or "",
                export=bool(u.get("export")),
                line_start=int(u.get("line_start") or 0),
                line_end=int(u.get("line_end") or 0),
                part_index=int(u.get("part_index") or 0),
                part_total=int(u.get("part_total") or 1),
                body_hash=stored_hash or current_hash,
                is_regulated_report=bool(u.get("is_regulated_report") or 0),
            )
            prepared.append((job, u))
        stats.units_prepared = len(prepared)
        if not prepared:
            return stats

        # Step 3: SYNC embedding call — MANDATORY asyncio.to_thread wrapper.
        # EmbeddingService.get_embeddings_batched takes `format_spec`, mirroring
        # routine_indexer.py:226-229.
        texts = [j.embedding_text for j, _ in prepared]
        metric_started = embedding_metrics.started()
        try:
            batch_result = await asyncio.to_thread(
                lambda: embedding_metrics.call_batched_with_usage(
                    embedding_service, texts, format_spec=doc_spec,
                )
            )
        except Exception:
            embedding_metrics.record_failure(
                event_type="bsl_code.embedding.index",
                embedding_service=embedding_service,
                duration_ms=embedding_metrics.elapsed_ms(metric_started),
            )
            raise
        embedding_metrics.record_result(
            event_type="bsl_code.embedding.index",
            embedding_service=embedding_service,
            result=batch_result,
            duration_ms=embedding_metrics.elapsed_ms(metric_started),
        )
        # Capture usage from the same EmbeddingBatchResult that runtime_metrics
        # already recorded, so progress/final logs show cumulative tokens+cost.
        # Legacy services (get_embeddings_batched only) yield None tokens and
        # cost_source='unknown' via call_batched_with_usage -> logged as 'unknown'.
        stats.embedding_api_calls = int(getattr(batch_result, "api_calls", 0) or 0)
        stats.input_tokens = batch_result.input_tokens
        stats.total_tokens = batch_result.total_tokens
        stats.add_cost(
            batch_result.cost_amount,
            batch_result.cost_unit,
            batch_result.cost_source,
        )
        vectors = batch_result.embeddings
        if not vectors or len(vectors) != len(prepared):
            raise RuntimeError(
                f"BSL Phase B: embedding service returned "
                f"{len(vectors) if vectors else 0} vectors for {len(prepared)} jobs"
            )

        # Step 4: Neo4j vector write + mark done — sync, run in thread.
        jobs_with_vectors: List[Tuple[BslCodeEmbeddingJob, List[float]]] = list(
            zip([j for j, _ in prepared], vectors)
        )
        if visible_on_upsert:
            await loop.run_in_executor(
                None,
                lambda: self._write_phase_b_vectors_and_mark_done(
                    jobs_with_vectors, current_epoch,
                ),
            )
        else:
            await loop.run_in_executor(
                None,
                lambda: self._write_phase_b_vectors_and_mark_done(
                    jobs_with_vectors, current_epoch,
                    visible_on_upsert=False,
                ),
            )
        stats.units_written = len(jobs_with_vectors)
        return stats

    def _fetch_bodies_from_neo4j(
        self, routine_ids: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        if not routine_ids:
            return {}
        with self.driver.session(database=settings.neo4j_database) as session:
            res = session.run(
                CYPHER_FETCH_ROUTINE_BODY_BATCH,
                routine_ids=routine_ids,
            )
            out: Dict[str, Dict[str, Any]] = {}
            for rec in res:
                rid = rec["routine_id"]
                out[rid] = {
                    "body": rec["body"] or "",
                    "body_hash": rec["body_hash"] or "",
                }
            return out

    def _build_phase_b_embedding_text(
        self, unit: Dict[str, Any], body: str,
    ) -> str:
        """Build embedding text from a SQLite unit row + Neo4j body slice.
        Body is sliced strictly by (char_start, char_end) to match Phase A
        FTS payload byte-for-byte (Drift 2). symbol_name comes from the
        denormalized routine_name column (Drift 1; not from a JOIN onto
        bsl_code_methods, whose write is best-effort)."""
        char_start = int(unit.get("char_start") or 0)
        char_end = int(unit.get("char_end") or 0)
        if char_end > char_start:
            excerpt = body[char_start:char_end]
        else:
            # Fallback for rows written before char ranges existed.
            from .bsl_code_search_service import _slice_body_by_lines  # noqa: WPS433
            excerpt = _slice_body_by_lines(
                body,
                int(unit.get("line_start") or 0),
                int(unit.get("line_end") or 0),
            )
        if not excerpt.strip():
            return ""
        meta_type, object_name, form_name = parse_owner_qn(unit.get("owner_qn"))
        ctx = UnitContext(
            metadata_type_ru=meta_type,
            object_name=object_name,
            form_name=form_name,
            symbol_name=(unit.get("routine_name") or "").strip(),
            routine_type=(unit.get("routine_type") or "").strip().lower(),
        )
        if settings.bsl_code_compression_strategy == "none":
            return build_raw_embedding_text(excerpt, ctx)
        return compress_unit(
            excerpt, ctx, strategy=settings.bsl_code_compression_strategy,
        )

    def _write_phase_b_vectors_and_mark_done(
        self,
        jobs_with_vectors: List[Tuple["BslCodeEmbeddingJob", List[float]]],
        current_epoch: int,
        *,
        visible_on_upsert: bool = True,
    ) -> None:
        """Neo4j vector upsert + SQLite done-mark. Crash between Neo4j write
        and done-mark is benign — UPSERT is idempotent so resume re-embeds
        without corrupting state.

        `visible_on_upsert` controls `code_embedding_visible` of the upserted
        unit. Full pipeline uses True; scoped Phase B passes False so vector
        gate stays active until the applier explicitly restores visibility
        in step 9.5 (after module FTS rebuild and source_state_hash recompute).
        """
        with self.driver.session(database=settings.neo4j_database) as session:
            small_payload: List[Dict[str, Any]] = []
            large_payload: List[Dict[str, Any]] = []
            for job, vector in jobs_with_vectors:
                row = {
                    "unit_id": job.unit_id,
                    "routine_id": job.routine_id,
                    "project_name": job.project_name,
                    "config_name": job.config_name,
                    "owner_qn": job.owner_qn,
                    "owner_qn_prefix": job.owner_qn_prefix,
                    "owner_category": job.owner_category,
                    "module_type": job.module_type,
                    "routine_type": job.routine_type,
                    "export": job.export,
                    "line_start": job.line_start,
                    "line_end": job.line_end,
                    "part_index": job.part_index,
                    "part_total": job.part_total,
                    "body_hash": job.body_hash,
                    "is_regulated_report": bool(job.is_regulated_report),
                    "code_embedding": vector,
                    "epoch": int(current_epoch),
                    "visible": bool(visible_on_upsert),
                }
                if job.unit_kind == "routine":
                    small_payload.append(row)
                else:
                    large_payload.append(row)
            if small_payload:
                session.run(CYPHER_UPSERT_BSL_SMALL_UNIT, rows=small_payload)
            if large_payload:
                session.run(CYPHER_UPSERT_BSL_LARGE_UNIT, rows=large_payload)
        done_items = [
            {
                "unit_id": j.unit_id,
                "routine_id": j.routine_id,
                "unit_kind": j.unit_kind,
                "body_hash": j.body_hash,
            }
            for j, _ in jobs_with_vectors
        ]
        self.sqlite.mark_phase_b_units_done(
            scope=self.scope, vector_epoch=current_epoch, items=done_items,
        )

    # ------------------------------------------------------------------ helpers

    def _bsl_vector_enabled(self) -> bool:
        return bool(
            settings.enable_bsl_code_search and settings.enable_bsl_code_embedding
        )

    def _ensure_bsl_vector_index_online(self) -> None:
        """Ensure `vec_bsl_code_unit` exists AND is ONLINE before ready.

        Raises on failure so the caller sets vector_status='failed' and skips
        the ready commit. Uses the bounded startup probe for the dimension
        (index-repair path — a slow/flapping endpoint must not stall this) and
        a heartbeat-aware poll so a long population under a scheduler lease does
        not drop the lock. Idempotent: an already-ONLINE index returns fast."""
        from .indexes import (
            IndexManagementMixin,
            ensure_bsl_code_vector_index_online,
        )

        class _TempIndexManager(IndexManagementMixin):
            def __init__(self, driver):
                self.driver = driver

        dim = _TempIndexManager(self.driver).get_embedding_dimension_from_config(
            use_startup_probe=True,
        )
        if dim is None:
            raise EmbeddingUnavailableError(
                "BSL code search: cannot determine embedding dimension to "
                "ensure vec_bsl_code_unit ONLINE"
            )
        timeout = float(settings.bsl_code_vector_index_online_timeout_seconds)
        with self.driver.session(database=settings.neo4j_database) as session:
            ok = ensure_bsl_code_vector_index_online(
                session, int(dim), timeout,
                sleep_fn=self._sleep_with_lease_heartbeat,
            )
        if not ok:
            raise RuntimeError(
                "BSL code search: vec_bsl_code_unit did not reach ONLINE "
                f"within {timeout:.0f}s before committing ready"
            )

    # ---------------------------------------------- search-visible coverage

    def _current_coverage_policy(self) -> Dict[str, Any]:
        return search_policy.coverage_policy(
            settings.bsl_code_embedding_excluded_owner_categories or (),
            bool(settings.bsl_code_search_exclude_regulated_reports),
        )

    def _coverage_change(
        self, scope: str
    ) -> Tuple[bool, search_policy.CoverageDelta]:
        """Return (changed, delta) based on stored coverage policy vs current.

        `changed=False` when stored fingerprint matches the new one. Delta is
        always computed (even when unchanged) so callers do not need to
        re-read the stored state.
        """
        stored = self.sqlite.read_coverage_state(scope)
        stored_fp = stored.get("coverage_fingerprint") or ""
        stored_policy_raw = stored.get("coverage_policy_json") or ""
        try:
            prev_policy = json.loads(stored_policy_raw) if stored_policy_raw else None
        except json.JSONDecodeError:
            prev_policy = None
        new_policy = self._current_coverage_policy()
        new_fp = search_policy.coverage_fingerprint(new_policy)
        delta = search_policy.coverage_delta(prev_policy, new_policy)
        return (stored_fp != new_fp), delta

    def _safe_run_phase_b_and_finalize(
        self, scope: str, current_epoch: int,
    ) -> None:
        """Wrap `_run_phase_b_and_finalize` so a Phase B / sync failure
        does not propagate out of `start_indexing` (which is a startup
        hook). Phase B has already moved `vector_status='failed'`, so
        search will simply fall back to RLM until the next start.
        """
        try:
            try:
                self._run_phase_b_and_finalize(scope, current_epoch)
            except EmbeddingUnavailableError as e:
                # Expected embedding outage: vector_status is already 'failed'
                # and search falls back to RLM. One line, no traceback.
                logger.warning(
                    "BSL code search: Phase B skipped (embedding unavailable): %s", e,
                )
            except Exception as e:
                if is_embedding_unavailable_error(e):
                    logger.warning(
                        "BSL code search: Phase B skipped (embedding unavailable): %s", e,
                    )
                    return
                logger.error(
                    "BSL code search: Phase B finalize aborted: %s",
                    e, exc_info=True,
                )
        finally:
            # Embedding stage is over (success, outage, or error); return
            # allocator-retained memory to the OS. Debounce in the helper keeps
            # this from duplicating the Phase A trim when Phase B is skipped and
            # this boundary lands seconds later. Best-effort: never raises.
            trim_process_memory(
                "BSL Phase B finalize completed",
                enabled=settings.memory_trim_enabled,
            )

    def _run_phase_b_and_finalize(
        self, scope: str, current_epoch: int,
    ) -> None:
        """`start_indexing` finisher: drive Phase B under the new
        contract and commit `ready+policy` atomically only after a
        successful visibility sync.

        Path-by-outcome:
            SUCCESS — visibility sync, then
                commit_coverage_state(..., vector_status='ready',
                                      vector_epoch=current_epoch).
            SKIPPED — vector subsystem disabled or current_epoch <= 0.
                Persist coverage policy WITHOUT vector_status/vector_epoch
                so that re-enabling embedding later does not see a stale
                ready marker against an unbuilt epoch.
            Exception (FAILED) — Phase B already set 'failed'; do not
                commit policy, re-raise so the outer caller logs the
                error path.
        """
        try:
            outcome = self._run_phase_b_if_enabled(
                current_epoch, mark_ready_on_success=False,
            )
        except Exception:
            # vector_status already set to 'failed' inside Phase B.
            raise
        policy = self._current_coverage_policy()
        policy_json = json.dumps(policy, sort_keys=True, ensure_ascii=False)
        fingerprint = search_policy.coverage_fingerprint(policy)
        if outcome is PhaseBOutcome.SKIPPED:
            # No vector work happened (and Phase B didn't touch status).
            # Persist the policy so the next start_indexing doesn't keep
            # firing coverage_changed against a stale fingerprint.
            self.sqlite.commit_coverage_state(
                scope, policy_json=policy_json, fingerprint=fingerprint,
            )
            # Vector subsystem is disabled: snapshot is no longer relevant.
            self.sqlite.clear_phase_b_transfer_snapshot(scope)
            return
        # Ensure vec_bsl_code_unit is created AND ONLINE before flipping to
        # ready (R1-plan-F1): a startup with a down embedding endpoint skips the
        # vector index in create_indexes; this shared finalize is reached by
        # startup overlap, non-overlap startup rebuild AND scheduled Phase B-only
        # recovery, so the gate lives here — never letting any path commit
        # ready against a missing/offline index. Idempotent when already ONLINE.
        try:
            self._ensure_bsl_vector_index_online()
        except Exception as e:
            logger.error(
                "BSL code search: vector index not ONLINE before ready: %s",
                e, exc_info=True,
            )
            self.sqlite.set_vector_status(scope, "failed")
            raise
        try:
            self._sync_code_embedding_visibility(scope, current_epoch)
        except Exception as e:
            logger.error(
                "BSL code search: post-Phase-B visibility sync failed: %s",
                e, exc_info=True,
            )
            self.sqlite.set_vector_status(scope, "failed")
            raise
        embedding_fp = self._compute_embedding_fingerprint()
        self.sqlite.commit_coverage_state(
            scope,
            policy_json=policy_json,
            fingerprint=fingerprint,
            vector_status="ready",
            vector_epoch=current_epoch,
            embedding_fingerprint=embedding_fp,
        )
        # Phase B reached 'ready' under the new epoch — the durable transfer
        # snapshot has done its job and would only become stale.
        self.sqlite.clear_phase_b_transfer_snapshot(scope)

    def _sync_code_embedding_visibility(
        self, scope: str, current_epoch: int,
    ) -> int:
        """Recompute `code_embedding_visible` on `:BslCodeSearchUnit` for
        scope+epoch according to the current coverage policy. Idempotent;
        loops over batched `CYPHER_SYNC_BSL_CODE_EMBEDDING_VISIBLE` until
        no more rows need updating. Returns total rows updated.

        Run on every transition that moves the vector subsystem into
        `ready`: hidden-only / visible-no-missing / visible-with-missing
        coverage changes, full rebuild, resume pending, Phase B-only
        restart. Failure raises — callers translate that into
        `vector_status='failed'` and skip the coverage commit.
        """
        policy = self._current_coverage_policy()
        excluded = list(policy.get("excluded_owner_categories") or ())
        exclude_reg = bool(policy.get("regulated_reports_excluded") or False)
        batch = max(1, int(getattr(
            settings, "neo4j_clear_project_batch_size", 10000,
        )))
        total = 0
        with self.driver.session(database=settings.neo4j_database) as session:
            while True:
                rec = session.run(
                    CYPHER_SYNC_BSL_CODE_EMBEDDING_VISIBLE,
                    project_name=scope,
                    vector_epoch=int(current_epoch),
                    excluded_owner_categories=excluded,
                    exclude_regulated_reports=exclude_reg,
                    limit=batch,
                ).single()
                updated = int((rec or {}).get("updated") or 0)
                if updated <= 0:
                    break
                total += updated
        if total:
            logger.info(
                "BSL code search: visibility sync updated %d unit(s) "
                "for scope=%s vector_epoch=%d", total, scope, current_epoch,
            )
        return total

    def _handle_coverage_change(
        self,
        scope: str,
        current_epoch: int,
        delta: search_policy.CoverageDelta,
    ) -> None:
        """Coverage policy changed at runtime.

        Single lifecycle for all enabled branches:
            set_vector_status('building', current_epoch)
              -> (Phase B for visible-with-missing)
              -> _sync_code_embedding_visibility
              -> commit_coverage_state(..., vector_status='ready',
                                       vector_epoch=current_epoch)
        Any exception in Phase B or sync sets `vector_status='failed'`
        and leaves the policy untouched so the next run retries.

        `vector_status='ready'` therefore ALWAYS means that
        `code_embedding_visible` on each unit of the current epoch
        matches the just-committed coverage policy.

        Hidden-only delta (newly_hidden_categories or
        regulated_newly_hidden, no visible deltas): visibility sync
        flips the affected nodes to `code_embedding_visible=false`
        BEFORE the new policy is committed, so concurrent vector search
        never returns nodes the new policy excludes.

        Visible-no-missing delta (newly_visible / regulated_newly_visible
        but units already have embeddings): sync flips affected nodes
        back to `true`, then commit.

        Visible-with-missing delta: Phase B runs first (mark_ready_on_success
        =False so it leaves status at `building`), then sync, then
        atomic commit.

        Disabled vector subsystem: persist the new policy AND invalidate
        `vector_status` to `failed` so a future re-enable trips
        `vector_needs_rebuild` in `start_indexing` and re-runs Phase B +
        visibility sync against the new policy. Without the invalidation a
        stale `ready` would survive the policy advance, and the next
        enabled start would see `coverage_changed=False` AND
        `vector_needs_rebuild=False`, so sync would never run against the
        new policy. RLM continues to serve requests while embeddings are
        disabled.
        """
        policy = self._current_coverage_policy()
        policy_json = json.dumps(policy, sort_keys=True, ensure_ascii=False)
        fingerprint = search_policy.coverage_fingerprint(policy)

        if not self._bsl_vector_enabled():
            logger.info(
                "BSL code search: coverage policy changed (vector disabled) — "
                "persisting new policy and invalidating vector_status",
            )
            self.sqlite.commit_coverage_state(
                scope, policy_json=policy_json, fingerprint=fingerprint,
                vector_status="failed",
            )
            return

        if current_epoch <= 0:
            # No SQLite epoch yet — nothing to sync against. Just persist
            # the policy; the next full build will reconcile via the
            # start_indexing path.
            self.sqlite.commit_coverage_state(
                scope, policy_json=policy_json, fingerprint=fingerprint,
            )
            return

        if not delta.has_visible:
            # Hidden-only delta (also covers visibility_policy_version
            # bumps where both has_visible and has_hidden are false):
            # building → sync → atomic commit(ready+policy).
            logger.info(
                "BSL code search: coverage policy changed (hidden-only) — "
                "building → sync → commit(ready+policy)",
            )
            self.sqlite.set_vector_status(scope, "building", current_epoch)
            try:
                self._sync_code_embedding_visibility(scope, current_epoch)
            except Exception as e:
                logger.error(
                    "BSL code search: hidden-only visibility sync failed: %s",
                    e, exc_info=True,
                )
                self.sqlite.set_vector_status(scope, "failed")
                raise
            self.sqlite.commit_coverage_state(
                scope,
                policy_json=policy_json,
                fingerprint=fingerprint,
                vector_status="ready",
                vector_epoch=current_epoch,
            )
            return

        # Visible delta.
        if not self._has_missing_visible_coverage(scope, current_epoch):
            # Visible-no-missing: vectors are already there, just need
            # to flip code_embedding_visible back to true for nodes
            # that were previously hidden.
            logger.info(
                "BSL code search: coverage policy changed (visible delta, "
                "no missing coverage) — building → sync → commit(ready+policy)",
            )
            self.sqlite.set_vector_status(scope, "building", current_epoch)
            try:
                self._sync_code_embedding_visibility(scope, current_epoch)
            except Exception as e:
                logger.error(
                    "BSL code search: visible-no-missing sync failed: %s",
                    e, exc_info=True,
                )
                self.sqlite.set_vector_status(scope, "failed")
                raise
            self.sqlite.commit_coverage_state(
                scope,
                policy_json=policy_json,
                fingerprint=fingerprint,
                vector_status="ready",
                vector_epoch=current_epoch,
            )
            return

        # Visible-with-missing: Phase B for missing units, then sync,
        # then commit.
        logger.info(
            "BSL code search: coverage policy changed (visible delta) — "
            "building → Phase B → sync → commit(ready+policy)",
        )
        self.sqlite.set_vector_status(scope, "building", current_epoch)
        try:
            outcome = self._run_phase_b_if_enabled(
                current_epoch, mark_ready_on_success=False,
            )
        except Exception:
            # Phase B already set vector_status='failed' before raising.
            raise
        if outcome is not PhaseBOutcome.SUCCESS:
            # Should be unreachable: vector enabled + epoch > 0 preflighted
            # above, so Phase B may not return SKIPPED here.
            self.sqlite.set_vector_status(scope, "failed")
            raise RuntimeError(
                f"BSL Phase B returned {outcome!r} after building was "
                "set; expected SUCCESS"
            )
        try:
            self._sync_code_embedding_visibility(scope, current_epoch)
        except Exception as e:
            logger.error(
                "BSL code search: visible-with-missing sync failed: %s",
                e, exc_info=True,
            )
            self.sqlite.set_vector_status(scope, "failed")
            raise
        self.sqlite.commit_coverage_state(
            scope,
            policy_json=policy_json,
            fingerprint=fingerprint,
            vector_status="ready",
            vector_epoch=current_epoch,
        )

    def _has_missing_visible_coverage(
        self, scope: str, current_epoch: int,
    ) -> bool:
        """Probe whether at least one unit in the current embeddable
        scope is not yet marked done for the current vector_epoch."""
        excluded = search_policy.normalize_excluded_categories(
            settings.bsl_code_embedding_excluded_owner_categories or ()
        )
        exclude_reg = bool(settings.bsl_code_search_exclude_regulated_reports)
        for batch in self.sqlite.iter_phase_b_not_done_units(
            scope=scope,
            epoch=current_epoch,
            vector_epoch=current_epoch,
            worker_id=0,
            total_workers=1,
            batch_size=1,
            excluded_owner_categories=excluded,
            exclude_regulated_reports=exclude_reg,
        ):
            if batch:
                return True
        return False

    def _fetch_routines_lightweight(self) -> List[Dict[str, Any]]:
        """
        Lightweight pass: routine metadata WITHOUT body, ordered by
        (rel_path, routine_id). Used for total_routines counting and
        source_state_hash computation. RAM = O(N) metadata only,
        no body strings.
        """
        with self.driver.session(database=settings.neo4j_database) as session:
            res = session.run(
                CYPHER_FETCH_ROUTINES_LIGHTWEIGHT,
                project_name=self.scope,
            )
            return [dict(record) for record in res]

    def _iter_routine_body_batches(
        self, batch_size: Optional[int] = None,
    ) -> Iterable[List[Dict[str, Any]]]:
        """
        Keyset-paginated streaming generator of routine batches WITH body,
        ordered by (rel_path, routine_id) to match the lightweight pass.
        At most one batch is held in memory at a time on the generator
        side; the main coordinator may wrap this in a prefetch thread to
        overlap Neo4j I/O with worker CPU.
        """
        size = int(batch_size or settings.bsl_code_routine_fetch_batch_size)
        last_rel_path = ""
        last_routine_id = ""
        with self.driver.session(database=settings.neo4j_database) as session:
            while True:
                res = session.run(
                    CYPHER_FETCH_ROUTINES_BODY_BATCH,
                    project_name=self.scope,
                    last_rel_path=last_rel_path,
                    last_routine_id=last_routine_id,
                    batch_size=size,
                )
                batch = [dict(record) for record in res]
                if not batch:
                    return
                yield batch
                last = batch[-1]
                last_rel_path = (last.get("file_path") or "").strip()
                last_routine_id = last.get("routine_id") or ""

    def _compute_config_fingerprint(self) -> str:
        """Phase A fingerprint: covers the structure of units / RLM / SQLite.

        Embedding model / prompt profile / transport intentionally live in the
        separate Phase B embedding fingerprint, so swapping the embedding
        contract does not force a Phase A rebuild.
        """
        payload = {
            "bsl_code_split_strategy": settings.bsl_code_split_strategy,
            "bsl_code_compression_strategy": settings.bsl_code_compression_strategy,
            "bsl_code_units_version": int(settings.bsl_code_units_version),
            "bsl_code_structural_extractor_version": int(
                settings.bsl_code_structural_extractor_version
            ),
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _compute_embedding_fingerprint(self) -> str:
        """Embedding-contract fingerprint = model + effective prompt profile +
        effective transport + format version. Raw EMBEDDING_API_BASE is
        intentionally NOT part of the contract — only the derived transport is.
        Operator contract: changing EMBEDDING_API_BASE while model and derived
        transport stay the same must point at a vector-space-compatible backend
        (the endpoint is treated as a route/load-balancer, not a provider
        identity). To move to an incompatible backend, change model / prompt
        mode / format version so the fingerprint shifts and a full re-embed is
        triggered."""
        return compute_bsl_code_embedding_fingerprint(
            embedding_model=settings.embedding_model or "",
            embedding_prompt_mode=settings.bsl_code_embedding_prompt_mode or "auto",
            embedding_api_base=settings.embedding_api_base or "",
            embedding_transport_setting=getattr(
                settings, "embedding_transport", "auto",
            ) or "auto",
        )

    def _compute_source_state_hash(self, routines: List[Dict[str, Any]]) -> str:
        h = hashlib.sha256()
        for r in routines:
            line = "|".join([
                r.get("routine_id") or "",
                r.get("config_name") or "",
                r.get("body_hash") or "",
                r.get("module_type") or "",
                r.get("routine_type") or "",
                "1" if r.get("export") else "0",
                r.get("owner_category") or "",
            ])
            h.update(line.encode("utf-8"))
            h.update(b"\n")
        return h.hexdigest()


    def _cleanup_neo4j_pending_overlap(
        self, scope: str, epoch: int, routine_ids: Optional[Sequence[str]],
    ) -> None:
        """Remove same-epoch pending-overlap vectors (visible=false) written by
        a startup overlap Phase B, guarded by code_embedding_epoch==epoch AND
        code_embedding_visible==false. Committed visible vectors of the same
        epoch number are never touched. `routine_ids=None`/[] cleans all
        pending-overlap units of the epoch; a non-empty list scopes to a
        reprocessed set. Best-effort — never raises out (Phase A must not fail).
        """
        ids = list(routine_ids or ())
        batch = max(1, int(getattr(settings, "neo4j_clear_project_batch_size", 10000)))
        deleted_large = 0
        cleared_small = 0
        try:
            with self.driver.session(database=settings.neo4j_database) as session:
                while True:
                    rec = session.run(
                        CYPHER_DELETE_BSL_LARGE_PENDING_OVERLAP_BATCH,
                        project_name=scope, epoch=int(epoch),
                        routine_ids=ids, limit=batch,
                    ).single()
                    n = int(rec["deleted"]) if rec else 0
                    if not n:
                        break
                    deleted_large += n
                while True:
                    rec = session.run(
                        CYPHER_CLEAR_BSL_SMALL_PENDING_OVERLAP_BATCH,
                        project_name=scope, epoch=int(epoch),
                        routine_ids=ids, limit=batch,
                    ).single()
                    n = int(rec["cleared"]) if rec else 0
                    if not n:
                        break
                    cleared_small += n
            if deleted_large or cleared_small:
                logger.info(
                    "BSL pending-overlap cleanup: epoch=%d routines=%s "
                    "deleted_large=%d cleared_small=%d",
                    epoch, (len(ids) if ids else "all"),
                    deleted_large, cleared_small,
                )
        except Exception as e:
            logger.warning(
                "BSL pending-overlap Neo4j cleanup failed (continuing): %s", e,
            )

    def cleanup_phase_b_pending_epoch(
        self,
        scope: str,
        epoch: int,
        *,
        routine_ids: Optional[Sequence[str]] = None,
    ) -> None:
        """Single idempotent contract to drop Phase B state of a pending epoch:
        SQLite done-markers (bsl_code_phase_b_unit_state) + same-epoch pending
        Neo4j vectors (epoch+visible guard). Used at abandon/reprocess points so
        a re-run never inherits stale markers or shadowed pending vectors.
        Monotonic epoch allocation bounds the correctness-critical case to
        same-epoch reprocess; other call sites use this as idempotent hygiene."""
        ids = list(routine_ids or ())
        try:
            if ids:
                self.sqlite.delete_phase_b_state_by_routine_ids(
                    scope, int(epoch), ids,
                )
            else:
                self.sqlite.delete_phase_b_state_for_epoch(scope, int(epoch))
        except Exception as e:
            logger.warning(
                "BSL cleanup_phase_b_pending_epoch: SQLite delete failed: %s", e,
            )
        self._cleanup_neo4j_pending_overlap(scope, int(epoch), ids)

    def _cleanup_neo4j_bsl_state(self, scope: str, current_epoch: int) -> None:
        """
        Stale-only cleanup: removes RoutineCodeUnit nodes and BslCodeSearchUnit
        labels whose code_embedding_epoch differs from the committed
        current_epoch (or is NULL). Embeddings written by a partial Phase B
        before a crash survive this cleanup so resume can skip them.
        """
        batch = max(1, int(getattr(settings, "neo4j_clear_project_batch_size", 10000)))
        started_at = time.monotonic()
        deleted_large_total = 0
        cleared_small_total = 0
        try:
            with self.driver.session(database=settings.neo4j_database) as session:
                while True:
                    res = session.run(
                        CYPHER_DELETE_BSL_LARGE_UNITS_STALE_BATCH,
                        project_name=scope,
                        current_epoch=int(current_epoch),
                        limit=batch,
                    )
                    rec = res.single()
                    deleted = int(rec["deleted"]) if rec else 0
                    if not deleted:
                        break
                    deleted_large_total += deleted
                    logger.info(
                        "BSL Neo4j cleanup: deleted_stale_large_units batch=%d total=%d elapsed=%s",
                        deleted, deleted_large_total,
                        _format_elapsed(time.monotonic() - started_at),
                    )
                while True:
                    res = session.run(
                        CYPHER_CLEAR_BSL_SMALL_UNITS_STALE_BATCH,
                        project_name=scope,
                        current_epoch=int(current_epoch),
                        limit=batch,
                    )
                    rec = res.single()
                    cleared = int(rec["cleared"]) if rec else 0
                    if not cleared:
                        break
                    cleared_small_total += cleared
                    logger.info(
                        "BSL Neo4j cleanup: cleared_stale_small_units batch=%d total=%d elapsed=%s",
                        cleared, cleared_small_total,
                        _format_elapsed(time.monotonic() - started_at),
                    )
            logger.info(
                "BSL Neo4j cleanup: current_epoch=%d, deleted_stale_large=%d, "
                "cleared_stale_small=%d, elapsed=%s",
                current_epoch, deleted_large_total, cleared_small_total,
                _format_elapsed(time.monotonic() - started_at),
            )
        except Exception as e:
            logger.warning("BSL Neo4j cleanup failed (continuing): %s", e)

    def _maybe_transfer_phase_b_state(
        self,
        *,
        scope: str,
        prev_current_epoch: Optional[int],
        new_current_epoch: int,
        prev_phase_a_fp: str,
        new_phase_a_fp: str,
        prev_embedding_fp: str,
        prev_vector_epoch: Optional[int],
        prev_vector_status: str,
    ) -> None:
        """Carry forward Phase B done-state and Neo4j embedding-epoch from the
        prior epoch when nothing relevant to the embedding contract changed.

        Gating (ALL conditions required):
        - BSL vector subsystem enabled;
        - prior `current_epoch` exists and differs from the new one;
        - Phase A fingerprint unchanged (split/compression/units/structural
          extractor — if any of these moved, the new units are not
          guaranteed to be comparable to the old ones);
        - Phase B embedding fingerprint unchanged AND non-empty (empty means
          no prior embedding contract was ever recorded — nothing to carry);
        - prior vector_status was 'ready'. NOTE: a non-empty embedding
          fingerprint no longer implies "Phase B finalised" — since the
          fingerprint is stamped at building-start, `building`/`failed` runs
          also carry a non-empty fp. Finalisation is decided by this
          vector_status == 'ready' gate alone; do not weaken it by trusting a
          non-empty fingerprint;
        - prior vector_epoch is known.

        Per-unit eligibility is enforced by the SQL JOIN in
        iter_phase_b_transferable_units, which compares only fields that drive
        embedding input (body_hash, char_*, part_*, owner_qn, routine_name,
        normalized routine_type, unit_kind, routine_id) plus a `char_end >
        char_start` guard that excludes the legacy line-range fallback.

        Per-batch order is CRITICAL: Neo4j retag FIRST, SQLite done-mark
        SECOND. A crash between the two is benign (the unit reappears as
        not-done and the obvious Phase B will re-embed it idempotently);
        the reverse order would let `_cleanup_neo4j_bsl_state` strip an
        already-marked-done embedding, leaving the unit silently uncovered.

        Errors here are non-fatal — they degrade the optimisation but do
        not break the committed Phase A epoch nor block the obvious Phase B.
        """
        if not self._bsl_vector_enabled():
            return
        if not prev_current_epoch or int(prev_current_epoch) == int(new_current_epoch):
            return
        if prev_phase_a_fp != new_phase_a_fp:
            logger.info(
                "BSL Phase B transfer: skipped — Phase A fingerprint changed "
                "(prev=%r, new=%r)", prev_phase_a_fp, new_phase_a_fp,
            )
            return
        new_embedding_fp = self._compute_embedding_fingerprint()
        if not prev_embedding_fp or prev_embedding_fp != new_embedding_fp:
            logger.info(
                "BSL Phase B transfer: skipped — embedding fingerprint "
                "drift (prev=%r, new=%r)", prev_embedding_fp, new_embedding_fp,
            )
            return
        if prev_vector_status != "ready":
            logger.info(
                "BSL Phase B transfer: skipped — prior vector_status=%r "
                "(transfer requires 'ready')", prev_vector_status,
            )
            return
        if prev_vector_epoch is None:
            logger.info(
                "BSL Phase B transfer: skipped — prior vector_epoch is NULL",
            )
            return

        prev_epoch_i = int(prev_current_epoch)
        prev_vector_epoch_i = int(prev_vector_epoch)
        new_epoch_i = int(new_current_epoch)

        started = time.monotonic()
        try:
            candidates = self.sqlite.count_phase_b_transferable_units(
                scope,
                prev_epoch=prev_epoch_i,
                new_epoch=new_epoch_i,
                prev_vector_epoch=prev_vector_epoch_i,
            )
        except Exception as e:
            logger.warning(
                "BSL Phase B transfer: skipped — count failed: %s", e,
            )
            return

        if candidates <= 0:
            logger.info(
                "BSL Phase B transfer: from_epoch=%d to_epoch=%d "
                "candidates=0 (nothing to carry forward)",
                prev_epoch_i, new_epoch_i,
            )
            return

        logger.info(
            "BSL Phase B transfer: starting from_epoch=%d to_epoch=%d "
            "candidates=%d batch_size=%d",
            prev_epoch_i, new_epoch_i, candidates, _PHASE_B_TRANSFER_BATCH_SIZE,
        )
        progress = _ProgressLogger(
            "BSL Phase B transfer",
            candidates,
            BSL_PHASE_B_TRANSFER_PROGRESS_UNITS,
            item_name="units",
            log=logger,
        )
        transferred = 0
        small_transferred = 0
        large_transferred = 0
        batches = 0
        try:
            for batch in self.sqlite.iter_phase_b_transferable_units(
                scope,
                prev_epoch=prev_epoch_i,
                new_epoch=new_epoch_i,
                prev_vector_epoch=prev_vector_epoch_i,
                batch_size=_PHASE_B_TRANSFER_BATCH_SIZE,
            ):
                small_rows = [r for r in batch if (r.get("unit_kind") or "") == "routine"]
                large_rows = [r for r in batch if (r.get("unit_kind") or "") != "routine"]
                # Neo4j retag FIRST. Open and close a session per batch so
                # the durable SQLite done-mark below never precedes a
                # closed Neo4j write section — same crash-safety pattern
                # as _write_phase_b_vectors_and_mark_done.
                with self.driver.session(database=settings.neo4j_database) as session:
                    if small_rows:
                        session.run(
                            CYPHER_RETAG_BSL_SMALL_UNIT_EPOCH,
                            rows=small_rows,
                            prev_epoch=prev_epoch_i,
                            new_epoch=new_epoch_i,
                        )
                    if large_rows:
                        session.run(
                            CYPHER_RETAG_BSL_LARGE_UNIT_EPOCH,
                            rows=large_rows,
                            prev_epoch=prev_epoch_i,
                            new_epoch=new_epoch_i,
                        )
                # SQLite done-mark SECOND, after the Neo4j session has
                # exited. Crash between Neo4j retag and SQLite mark is
                # benign (unit reappears as not-done; obvious Phase B
                # re-embeds idempotently).
                self.sqlite.mark_phase_b_units_done(
                    scope=scope,
                    vector_epoch=new_epoch_i,
                    items=[
                        {
                            "unit_id": r["unit_id"],
                            "routine_id": r["routine_id"],
                            "unit_kind": r["unit_kind"],
                            "body_hash": r.get("body_hash") or "",
                        }
                        for r in batch
                    ],
                )
                # Counters bump AFTER durable SQLite mark to keep progress
                # honest about Neo4j-retag-FIRST → SQLite-mark-SECOND
                # crash invariant.
                transferred += len(batch)
                small_transferred += len(small_rows)
                large_transferred += len(large_rows)
                batches += 1
                progress.maybe_log(
                    transferred,
                    small=small_transferred,
                    large=large_transferred,
                    batches=batches,
                )
                self._heartbeat_lease()
            progress.maybe_log(
                transferred,
                final=True,
                small=small_transferred,
                large=large_transferred,
                batches=batches,
            )
        except Exception as e:
            logger.warning(
                "BSL Phase B transfer: aborted from_epoch=%d to_epoch=%d "
                "transferred=%d/%d small=%d large=%d batches=%d "
                "elapsed=%s (Phase B will re-embed remaining units): %s",
                prev_epoch_i, new_epoch_i, transferred, candidates,
                small_transferred, large_transferred, batches,
                _format_elapsed(time.monotonic() - started), e,
            )

    def _get_embedding_service_or_none(self):
        try:
            from .embedding_service import get_embedding_service
            return get_embedding_service()
        except Exception as e:
            logger.warning("BSL Phase B: get_embedding_service failed: %s", e)
            return None

    # ============================================================ scoped Phase 5

    @staticmethod
    def _inject_owner_qn_parsed_fields(record: Dict[str, Any]) -> Dict[str, Any]:
        """Augment a Neo4j routine record with production-parsed owner_qn
        fields (`_meta_type_ru`, `_object_name`, `_form_name`) the way the
        full Phase A pipeline does in `_phase_a_streaming_write` before
        submitting to the worker. Without this scoped paths would fall back
        to worker's `_parse_owner_qn` which uses a different convention
        ("Forms" vs production "Form") — counters become asymmetric for
        form routines."""
        meta_type, object_name, form_name = parse_owner_qn(record.get("owner_qn"))
        record["_meta_type_ru"] = meta_type
        record["_object_name"] = object_name
        record["_form_name"] = form_name
        return record

    def _fetch_routine_records_by_ids(
        self, scope: str, routine_ids: Iterable[str],
    ) -> Dict[str, Dict[str, Any]]:
        """Read full Routine records (body + metadata) for a fixed id set,
        augmented with production-parsed owner_qn fields so the worker can
        be invoked the same way `_phase_a_streaming_write` does in full
        pipeline. Used by `_apply_bsl` step 4.5 to capture OLD state before
        `load_bsl_signatures` overwrites Neo4j. Returns {routine_id: record}."""
        ids = list(routine_ids or ())
        result: Dict[str, Dict[str, Any]] = {}
        if not ids:
            return result
        with self.driver.session(database=settings.neo4j_database) as session:
            for start in range(0, len(ids), 500):
                chunk = ids[start: start + 500]
                rows = session.run(
                    CYPHER_FETCH_ROUTINE_RECORDS_BY_IDS,
                    routine_ids=chunk,
                    project_name=scope,
                ).data()
                for r in rows:
                    rid = r.get("routine_id")
                    if rid:
                        result[rid] = self._inject_owner_qn_parsed_fields(r)
        return result

    def _fetch_routine_ids_for_config(
        self, scope: str, config_name: str, *, lease: Optional[Any] = None,
    ) -> Set[str]:
        """Все Routine id конфигурации из графа.

        Множество шире serving-множества sidecar: routines с пустым телом и
        исключённые политикой BSL в `bsl_code_units` не попадают. Нужно для
        двух вещей — доказать `serving_ids ⊆ graph_ids` (иначе в индексе есть
        routine, чей исходный вклад восстановить неоткуда) и снять их
        progress-строки в residual без остатка.
        """
        out: Set[str] = set()
        with self.driver.session(database=settings.neo4j_database) as session:
            result = session.run(
                CYPHER_FETCH_ROUTINE_IDS_FOR_CONFIG,
                project_name=scope,
                config_name=config_name,
            )
            for rec in result:
                rid = rec.get("routine_id")
                if rid:
                    out.add(rid)
                if len(out) % 5000 == 0:
                    _safe_heartbeat(lease)
        _safe_heartbeat(lease)
        return out

    def _fetch_routines_lightweight_by_ids(
        self, scope: str, routine_ids: Iterable[str],
        *,
        lease: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """Same as `_fetch_routines_lightweight` but for a fixed id set —
        used by scoped metadata-only update path. Chunked by
        `bsl_code_routine_fetch_batch_size`; heartbeat after each chunk."""
        ids = list(routine_ids or ())
        out: List[Dict[str, Any]] = []
        if not ids:
            return out
        chunk_size = max(1, int(settings.bsl_code_routine_fetch_batch_size))
        with self.driver.session(database=settings.neo4j_database) as session:
            for start in range(0, len(ids), chunk_size):
                chunk = ids[start: start + chunk_size]
                out.extend(
                    session.run(
                        CYPHER_FETCH_ROUTINES_LIGHTWEIGHT_BY_IDS,
                        routine_ids=chunk,
                        project_name=scope,
                    ).data()
                )
                _safe_heartbeat(lease)
        return out

    def _fetch_routine_bodies_by_ids(
        self, scope: str, routine_ids: Iterable[str],
        *,
        lease: Optional[Any] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Read NEW body / body_hash for a fixed id set — used by scoped
        Phase 5A builder to feed Phase A worker with worker-compatible records.
        Records are augmented with production-parsed owner_qn fields (the
        same way full pipeline does) so form routines get correct form_name.
        Chunked by `bsl_code_routine_fetch_batch_size`; heartbeat after each chunk."""
        ids = list(routine_ids or ())
        out: Dict[str, Dict[str, Any]] = {}
        if not ids:
            return out
        chunk_size = max(1, int(settings.bsl_code_routine_fetch_batch_size))
        with self.driver.session(database=settings.neo4j_database) as session:
            for start in range(0, len(ids), chunk_size):
                chunk = ids[start: start + chunk_size]
                rows = session.run(
                    CYPHER_FETCH_ROUTINE_RECORDS_BY_IDS,
                    routine_ids=chunk,
                    project_name=scope,
                ).data()
                for r in rows:
                    rid = r.get("routine_id")
                    if rid:
                        out[rid] = self._inject_owner_qn_parsed_fields(r)
                _safe_heartbeat(lease)
        return out

    def _neo4j_set_visibility_false_for_routines(
        self, scope: str, routine_ids: Iterable[str],
    ) -> None:
        """Single Cypher (UNION over small Routine + large RoutineCodeUnit)
        that sets code_embedding_visible=false for both shapes. Idempotent."""
        ids = list(routine_ids or ())
        if not ids:
            return
        with self.driver.session(database=settings.neo4j_database) as session:
            for start in range(0, len(ids), 500):
                chunk = ids[start: start + 500]
                session.run(
                    CYPHER_HIDE_BSL_UNITS_FOR_ROUTINES,
                    routine_ids=chunk,
                    project_name=scope,
                )

    def _neo4j_restore_visibility_for_committed(
        self,
        scope: str,
        vector_epoch: int,
        routine_ids: Iterable[str],
        excluded_owner_categories: Sequence[str],
        exclude_regulated_reports: bool,
    ) -> int:
        ids = list(routine_ids or ())
        if not ids:
            return 0
        total = 0
        with self.driver.session(database=settings.neo4j_database) as session:
            for start in range(0, len(ids), 500):
                chunk = ids[start: start + 500]
                rec = session.run(
                    CYPHER_SYNC_BSL_CODE_EMBEDDING_VISIBLE_BY_IDS,
                    project_name=scope,
                    vector_epoch=int(vector_epoch),
                    routine_ids=chunk,
                    excluded_owner_categories=list(excluded_owner_categories or ()),
                    exclude_regulated_reports=bool(exclude_regulated_reports),
                ).single()
                if rec:
                    total += int(rec.get("updated") or 0)
        return total

    @staticmethod
    def _assemble_module_fts_columns(fragments: Iterable[Dict[str, Any]]) -> Dict[str, str]:
        """Same module-FTS column layout as full-pipeline `_module_columns`,
        but computed from persisted per-routine fragment dicts (one fragment
        per routine). The aggregate covers ALL routines of the given rel_path,
        including unaffected siblings — caller is responsible for passing the
        full fragment set."""
        acc = {
            "object_name": "", "form_name": "", "metadata_type_ru": "",
            "module_kind": "",
            "symbols": [], "region_names": [], "headers": [],
            "comments": [], "body_tokens": [],
        }
        for raw in fragments:
            frag = raw.get("fragment") if "fragment" in raw else raw
            if not frag:
                continue
            for k in ("object_name", "form_name", "metadata_type_ru", "module_kind"):
                v = (frag.get(k) or "").strip()
                if v and not acc[k]:
                    acc[k] = v
            sym = (frag.get("symbol") or "").strip()
            if sym:
                acc["symbols"].append(sym)
            rn = (frag.get("region_names") or "").strip()
            if rn:
                acc["region_names"].append(rn)
            hdr = (frag.get("headers") or "").strip()
            if hdr:
                acc["headers"].append(hdr)
            cmt = (frag.get("comments") or "").strip()
            if cmt:
                acc["comments"].append(cmt)
            body_tok = (frag.get("body_tokens_text") or "").strip()
            if body_tok:
                acc["body_tokens"].append(body_tok)
        return {
            "object_name": _token_text(acc["object_name"]),
            "form_name": _token_text(acc["form_name"]),
            "metadata_type_ru": _token_text(acc["metadata_type_ru"]),
            "module_kind": _token_text(acc["module_kind"]),
            "symbols": _token_text(" ".join(acc["symbols"])),
            "region_names": _token_text(" ".join(acc["region_names"])),
            "headers": _token_text(" ".join(acc["headers"])),
            "comments": _token_text(" ".join(acc["comments"])),
            "body": " ".join(acc["body_tokens"]),
        }

    def _rebuild_module_fts_for_rel_paths(
        self, scope: str, current_epoch: int, rel_paths: Iterable[str],
    ) -> int:
        rps = list(rel_paths or ())
        if not rps:
            return 0
        # group fragments by rel_path
        fragments_by_path: Dict[str, List[Dict[str, Any]]] = {rp: [] for rp in rps}
        for frag in self.sqlite.iter_module_fragments_for_rel_paths(
            scope, int(current_epoch), rps,
        ):
            fragments_by_path.setdefault(frag["rel_path"], []).append(frag)
        columns_by_rel_path: Dict[str, Dict[str, str]] = {}
        for rel_path, frags in fragments_by_path.items():
            columns_by_rel_path[rel_path] = self._assemble_module_fts_columns(frags)
        return self.sqlite.replace_module_fts_for_rel_paths(
            scope, int(current_epoch), columns_by_rel_path,
        )

    def _update_units_metadata_for_routines(
        self, scope: str, current_epoch: int, routine_ids: Iterable[str],
        *,
        lease: Optional[Any] = None,
    ) -> int:
        """Metadata-only UPDATE for `line_only` routines. NOTE: does NOT
        touch `line_start`/`line_end` because full Phase A writes per-unit
        UnitRange (a large routine has different ranges per unit) and a
        scoped lightweight fetch only knows the routine's starting line —
        replacing per-unit ranges with `r.line..r.line` would collapse every
        unit fragment to a single line. Line ranges become stale until the
        next body/signature change triggers a full unit rebuild (see plan
        §9 — line ranges are intentionally NOT in the safe metadata subset).
        Chunked by `bsl_code_routine_fetch_batch_size`; heartbeat after each
        chunk and after each per-chunk SQLite commit."""
        ids = list(routine_ids or ())
        if not ids:
            return 0
        records = self._fetch_routines_lightweight_by_ids(scope, ids, lease=lease)
        rows: List[Dict[str, Any]] = []
        for rec in records:
            rid = rec.get("routine_id")
            if not rid:
                continue
            rows.append({
                "routine_id": rid,
                "rel_path": rec.get("rel_path") or "",
                "config_name": rec.get("config_name") or "",
                "owner_qn": rec.get("owner_qn") or "",
                "owner_category": rec.get("owner_category") or "",
                "module_type": rec.get("module_type") or "",
                "routine_type": rec.get("routine_type") or "",
                "export": bool(rec.get("export")),
            })
        if not rows:
            return 0
        chunk_size = max(1, int(settings.bsl_code_routine_fetch_batch_size))
        total = 0
        for start in range(0, len(rows), chunk_size):
            chunk = rows[start: start + chunk_size]
            total += self.sqlite.update_unit_metadata_for_routines(
                scope, int(current_epoch), chunk,
            )
            _safe_heartbeat(lease)
        return total

    def _build_units_for_routines(
        self,
        scope: str,
        routine_ids: Iterable[str],
        affected_rel_paths: Iterable[str],
        *,
        current_epoch: int,
        reverse_snapshot: Dict[str, Dict[str, Any]],
        lease: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Phase 5A scoped builder, mirrors full Phase A model: chunked fetch,
        sort by (file_path, routine_id), pack via _PhaseAPacker, adaptive
        sequential / ProcessPoolExecutor coordinator with drain-and-commit
        loop (per-pack SQLite TX: delete old + insert new + reverse/positive
        IDF/stats + snapshot clear + ledger stage -> 'sqlite_applied').

        Per-pack TX is the durable resume primitive: on exception mid-stream
        the ledger preserves stage 'snapshot_written' only for the packs that
        did not commit, and SCOPED_RETRY on the next cycle replays them only.

        Returns aggregate stats:
          units_written, methods_written, fragments_written,
          records_fetched, missing,
          work_packs, workers_used, execution_mode,
          sqlite_transactions, duration_seconds.
        """
        from concurrent.futures import ProcessPoolExecutor
        from collections import deque
        import math

        t0 = time.monotonic()
        ids = list(routine_ids or ())
        empty_stats = {
            "units_written": 0, "methods_written": 0, "fragments_written": 0,
            "records_fetched": 0, "missing": 0,
            "work_packs": 0, "workers_used": 0,
            "execution_mode": "sequential",
            "sqlite_transactions": 0,
            "duration_seconds": 0.0,
        }
        if not ids:
            empty_stats["duration_seconds"] = time.monotonic() - t0
            return empty_stats

        # 1. Build worker-compatible records from NEW Neo4j state (chunked).
        records = self._fetch_routine_bodies_by_ids(scope, ids, lease=lease)
        records_fetched = len(records)
        missing = [rid for rid in ids if rid not in records]

        # 2. Missing routines: routine was in `changed/added` but Neo4j has
        # no record for it (race with BSL deletion). Treated as scoped delete:
        # reverse the OLD snapshot from corpus IDF/stats, then delete old
        # units, clear snapshot, advance ledger. Mirrors `_scoped_sqlite_apply`
        # deleted path — keeps corpus counters consistent.
        chunk_size = max(1, int(settings.bsl_code_routine_fetch_batch_size))
        if missing:
            for start in range(0, len(missing), chunk_size):
                mchunk = missing[start: start + chunk_size]
                idf_neg, stats_neg = _invert_snapshot_subset(
                    reverse_snapshot, mchunk,
                )
                self.sqlite.delete_units_by_routine_ids(
                    scope, int(current_epoch), mchunk,
                    idf_reverse=idf_neg, stats_reverse=stats_neg,
                )
                _safe_heartbeat(lease)

        if not records:
            stats = dict(empty_stats)
            stats.update({
                "records_fetched": 0,
                "missing": len(missing),
                "sqlite_transactions": (len(missing) + chunk_size - 1) // chunk_size if missing else 0,
                "duration_seconds": time.monotonic() - t0,
            })
            return stats

        # 3. Sort records by (file_path, routine_id) — full Phase A invariant.
        # CYPHER_FETCH_ROUTINE_RECORDS_BY_IDS returns `file_path`, not `rel_path`.
        records_sorted = sorted(
            records.values(),
            key=lambda r: (r.get("file_path") or "", r.get("routine_id") or ""),
        )

        # 4. Adaptive mode decision based on upper-bound pack estimate.
        # est_packs is the max of count-based and byte-based estimates so that
        # huge bodies with small `len(records)` still force the pool.
        workers_setting = max(1, int(settings.bsl_code_phase_a_workers))
        work_batch_routines = max(1, int(settings.bsl_code_phase_a_work_batch_routines))
        work_batch_max_bytes = (
            max(1, int(settings.bsl_code_phase_a_work_batch_max_mb)) * 1024 * 1024
        )
        est_packs_by_count = math.ceil(len(records_sorted) / work_batch_routines)
        total_bytes = sum(len(r.get("body") or "") for r in records_sorted)
        est_packs_by_bytes = (
            math.ceil(total_bytes / work_batch_max_bytes)
            if total_bytes else 1
        )
        est_packs = max(1, est_packs_by_count, est_packs_by_bytes)
        use_pool = est_packs >= 3 and workers_setting > 1
        if use_pool:
            workers_used = min(workers_setting, est_packs)
            execution_mode = "process_pool"
        else:
            workers_used = 1
            execution_mode = "sequential"

        from .bsl_code_phase_a_worker import process_batch
        strategy = settings.bsl_code_split_strategy

        # 5. Coordinator state.
        packer = _PhaseAPacker(
            work_batch_routines=work_batch_routines,
            work_batch_max_bytes=work_batch_max_bytes,
            ordinal_start=1,
        )
        pending: deque = deque()
        pack_id_lists: deque = deque()  # parallel deque of pack_ids per submission
        progress = _ProgressLogger(
            "BSL Phase 5A", len(records_sorted), BSL_PHASE_A_PROGRESS_ROUTINES,
            log=logger,
        )

        units_written = 0
        methods_written = 0
        fragments_written = 0
        processed_routines = 0
        packs_done = 0
        sqlite_transactions = 1 if missing else 0  # missing chunks counted above

        executor: Optional[ProcessPoolExecutor] = None
        if use_pool:
            executor = ProcessPoolExecutor(max_workers=workers_used)

        class _Done:
            """Synthetic completed-future placeholder for sequential mode so
            the drain loop has a uniform interface."""
            __slots__ = ("_val",)

            def __init__(self, val: Any) -> None:
                self._val = val

            def result(self) -> Any:
                return self._val

        def _commit_pack(pack_ids: List[str], out: Dict[str, Any]) -> None:
            nonlocal units_written, methods_written, fragments_written
            nonlocal processed_routines, packs_done, sqlite_transactions
            # Per-pack positive IDF/stats from worker output.
            idf_pos: Dict[str, Dict[str, int]] = {}
            for c in out.get("idf_contributions", []) or []:
                fk = c["field_kind"]
                tok = c["token"]
                idf_pos.setdefault(fk, {})
                idf_pos[fk][tok] = idf_pos[fk].get(tok, 0) + int(c["df"])
            stats_pos: Dict[str, Tuple[int, int]] = {}
            for c in out.get("stats_contributions", []) or []:
                fk = c["field_kind"]
                dc_prev, tl_prev = stats_pos.get(fk, (0, 0))
                stats_pos[fk] = (
                    dc_prev + int(c["doc_count_delta"]),
                    tl_prev + int(c["total_length_delta"]),
                )
            idf_neg, stats_neg = _invert_snapshot_subset(
                reverse_snapshot, pack_ids,
            )
            self.sqlite.replace_units_for_routines(
                scope, int(current_epoch), pack_ids,
                units=out.get("unit_rows", []) or [],
                methods=out.get("method_rows", []) or [],
                done_routines=out.get("routines_done", []) or [],
                module_fragments=out.get("module_fragments", []) or [],
                idf_increments=idf_pos,
                stats_increments=stats_pos,
                idf_reverse=idf_neg,
                stats_reverse=stats_neg,
                clear_snapshot_ids=pack_ids,
                set_ledger_stage="sqlite_applied",
            )
            units_written += len(out.get("unit_rows", []) or [])
            methods_written += len(out.get("method_rows", []) or [])
            fragments_written += len(out.get("module_fragments", []) or [])
            processed_routines += len(pack_ids)
            packs_done += 1
            sqlite_transactions += 1
            _safe_heartbeat(lease)
            progress.maybe_log(
                processed_routines,
                units=units_written,
                packs_done=packs_done,
                rss_mb=_process_rss_mb(),
            )

        def _drain_until(max_pending: int) -> None:
            while len(pending) > max_pending:
                fut = pending.popleft()
                pack_ids = pack_id_lists.popleft()
                out = fut.result()
                _commit_pack(pack_ids, out)

        def _submit(
            pack_records: List[Dict[str, Any]],
            pack_ordinals: Dict[str, int],
        ) -> None:
            # Pre-parse owner_qn in main thread (production parser stays in main).
            for r in pack_records:
                meta_type, object_name, form_name = parse_owner_qn(
                    r.get("owner_qn")
                )
                r["_meta_type_ru"] = meta_type
                r["_object_name"] = object_name
                r["_form_name"] = form_name
            pack_ids = [r.get("routine_id") for r in pack_records if r.get("routine_id")]
            if executor is None:
                out = process_batch(
                    pack_records, strategy=strategy,
                    routine_ordinals=pack_ordinals, debug_timings=False,
                )
                pending.append(_Done(out))
                pack_id_lists.append(pack_ids)
            else:
                fut = executor.submit(
                    process_batch,
                    pack_records, strategy, pack_ordinals, False,
                )
                pending.append(fut)
                pack_id_lists.append(pack_ids)
            # Backpressure: bound pending payload to workers_used * 2.
            _drain_until(workers_used * 2)

        # 6. Feed records into packer; submit closed packs as they appear.
        try:
            for r in records_sorted:
                closed = packer.add(r)
                if closed is not None:
                    _submit(*closed)
            trailing = packer.flush()
            if trailing is not None:
                _submit(*trailing)
            # Drain remaining packs in submission order.
            _drain_until(0)
        finally:
            if executor is not None:
                # wait=True so already-finished worker results that were not
                # consumed (due to exception mid-stream) are not silently
                # applied; their ledger rows remain at 'snapshot_written'
                # and SCOPED_RETRY replays them.
                executor.shutdown(wait=True)

        progress.maybe_log(
            processed_routines, final=True,
            units=units_written, packs_done=packs_done,
        )

        return {
            "units_written": units_written,
            "methods_written": methods_written,
            "fragments_written": fragments_written,
            "records_fetched": records_fetched,
            "missing": len(missing),
            "work_packs": packs_done,
            "workers_used": workers_used,
            "execution_mode": execution_mode,
            "sqlite_transactions": sqlite_transactions,
            "duration_seconds": time.monotonic() - t0,
        }

    async def _embed_units_for_routines(
        self,
        scope: str,
        current_epoch: int,
        vector_epoch: int,
        routine_ids: Iterable[str],
        lease: Optional[Any] = None,
    ) -> ScopedPhaseBResult:
        """Scoped Phase B: embed units of the given routine_ids using existing
        `_phase_b_process_batch`, but UPSERT with `visible=false`. Visibility
        is restored later in the applier (step 9.5) once all other stores are
        consistent.

        Runs `bsl_code_phase_b_workers` async workers in parallel, mirroring
        full Phase B (`_run_phase_b_async` / `_phase_b_worker`). The `lease`
        parameter is plumbed through because scoped applier path does not call
        `start_indexing(lease)` and `self._active_lease` is not set."""
        if not settings.enable_bsl_code_search or not settings.enable_bsl_code_embedding:
            return ScopedPhaseBResult(PhaseBOutcome.SKIPPED, "feature disabled")
        if int(vector_epoch) != int(current_epoch):
            return ScopedPhaseBResult(
                PhaseBOutcome.SKIPPED,
                "vector_epoch != current_epoch; deferring scoped Phase B",
            )
        ids = list(routine_ids or ())
        if not ids:
            return ScopedPhaseBResult(PhaseBOutcome.SUCCESS, "no targets", 0)
        # Startup preflight: a known-unavailable endpoint defers without hitting
        # the endpoint (no bounded or production probe), so a startup cycle can't
        # hang here. Deferred, not a precondition SKIPPED.
        avail = self._embedding_availability
        if avail is not None and avail.enabled and not avail.available:
            return ScopedPhaseBResult(
                PhaseBOutcome.DEFERRED,
                f"embedding unavailable at startup: {avail.reason}",
            )
        # Reset Phase B done markers for these routines so the iterator
        # returns their units. Done once before workers start.
        self.sqlite.delete_phase_b_state_by_routine_ids(
            scope, int(vector_epoch), ids,
        )
        # Lazily init embedding service.
        embedding_service = self._get_embedding_service_or_none()
        if embedding_service is None:
            return ScopedPhaseBResult(
                PhaseBOutcome.DEFERRED, "embedding service unavailable",
            )
        try:
            profile = resolve_bsl_code_prompt_profile(
                settings.embedding_model or "",
                settings.bsl_code_embedding_prompt_mode or "auto",
            )
        except Exception as e:
            return ScopedPhaseBResult(
                PhaseBOutcome.SKIPPED, f"invalid prompt mode: {e}",
            )
        transport = resolve_effective_embedding_transport(
            settings.embedding_api_base or "",
            getattr(settings, "embedding_transport", "auto") or "auto",
        )
        doc_spec = build_embedding_format_spec(
            profile=profile,
            transport=transport,
            side="document",
            purpose="code",
            description_instruction="",
        )
        filters = {
            "excluded_owner_categories": list(
                settings.bsl_code_embedding_excluded_owner_categories or ()
            ),
            "exclude_regulated_reports": bool(
                settings.bsl_code_search_exclude_regulated_reports
            ),
        }
        workers_n = max(1, int(settings.bsl_code_phase_b_workers or 1))
        batch_size = max(1, int(settings.bsl_code_embedding_batch_size or 16))
        # Scoped/incremental always uses the scheduled policy (short exponential,
        # 3 rounds) — never the startup 12×300s policy.
        policy = _phase_b_run_policy("scheduled")
        max_rounds = policy.max_rounds
        total_written = 0

        def _remaining() -> int:
            return self.sqlite.count_phase_b_units_for_routine_ids(
                scope, int(current_epoch), int(vector_epoch), ids,
                filters=filters,
            )

        async def _round(round_idx: int, remaining_before: int) -> "_PhaseBStats":
            return await self._run_scoped_phase_b_async(
                scope=scope,
                current_epoch=int(current_epoch),
                vector_epoch=int(vector_epoch),
                routine_ids=ids,
                filters=filters,
                workers_n=workers_n,
                batch_size=batch_size,
                embedding_service=embedding_service,
                doc_spec=doc_spec,
                lease=lease,
                round_label=f"{round_idx}/{max_rounds}",
            )

        # Scoped path runs under an EXPLICIT lease (never self._active_lease):
        # heartbeat that lease between rounds via _safe_heartbeat (R1-plan-F2).
        async def _sleep(delay: float) -> None:
            await self._async_sleep_with_heartbeat(
                delay, lambda: _safe_heartbeat(lease),
            )

        # Outer-round loop (shared). Markers were deleted once above (before the
        # loop); each round processes only the still not-done scoped units and
        # done-markers persist between rounds. On exhaustion the applier maps the
        # terminal exception to FAILED_RETRY_QUEUED (durable between-cycle retry).
        outcome = await self._run_phase_b_rounds(
            policy=policy,
            remaining_fn=_remaining,
            round_fn=_round,
            sleep_fn=_sleep,
            label="scoped",
        )
        if outcome.last_stats is not None:
            total_written += int(outcome.last_stats.units_written)
        if not outcome.succeeded:
            e = outcome.last_exc
            remaining_after = _remaining()
            if is_embedding_unavailable_error(e):
                # Expected external outage: defer without traceback. The
                # applier maps DEFERRED to PHASE_B_DEFERRED; done-markers so
                # far are preserved for the next cycle.
                logger.warning(
                    "BSL scoped Phase B: embedding endpoint unavailable after "
                    "%d rounds, units_remaining=%d, deferring: %s",
                    max_rounds, remaining_after, e,
                )
                return ScopedPhaseBResult(
                    PhaseBOutcome.DEFERRED,
                    f"embedding unavailable: {e}",
                    total_written,
                )
            logger.error(
                "BSL scoped Phase B: exhausted %d rounds, "
                "units_remaining=%d: %s",
                max_rounds, remaining_after, e, exc_info=True,
            )
            assert e is not None
            raise e
        return ScopedPhaseBResult(
            PhaseBOutcome.SUCCESS, "", total_written,
        )

    async def _run_scoped_phase_b_async(
        self,
        scope: str,
        current_epoch: int,
        vector_epoch: int,
        routine_ids: List[str],
        filters: Dict[str, Any],
        workers_n: int,
        batch_size: int,
        embedding_service: Any,
        doc_spec: Any,
        lease: Optional[Any] = None,
        round_label: Optional[str] = None,
    ) -> _PhaseBStats:
        """Async coordinator for scoped Phase B: launch N workers each reading
        its own SQLite partition (fts_rowid % total_workers = worker_id).
        Mirrors `_run_phase_b_async` but with routine-id scoped iterator and
        explicit `lease` propagation (scoped applier path does not set
        `self._active_lease`)."""
        progress_total_units = await asyncio.to_thread(
            lambda: self.sqlite.count_phase_b_units_for_routine_ids(
                scope, current_epoch, vector_epoch, routine_ids,
                filters=filters,
            )
        )
        logger.info(
            "BSL scoped Phase B: workers=%d embedding_batch=%d routines=%d "
            "units_remaining=%d vector_epoch=%d",
            workers_n, batch_size, len(routine_ids),
            progress_total_units, vector_epoch,
        )
        progress = _PhaseBProgress(progress_total_units, round_label)
        t0 = time.monotonic()

        async def _heartbeat_loop() -> None:
            while True:
                await asyncio.sleep(BSL_PROGRESS_SECONDS)
                await progress.heartbeat()
                _safe_heartbeat(lease)

        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(), name="bsl_scoped_phase_b_progress_heartbeat",
        )
        tasks = [
            asyncio.create_task(
                self._scoped_phase_b_worker(
                    worker_id=i,
                    total_workers=workers_n,
                    current_epoch=current_epoch,
                    vector_epoch=vector_epoch,
                    routine_ids=routine_ids,
                    filters=filters,
                    batch_size=batch_size,
                    embedding_service=embedding_service,
                    doc_spec=doc_spec,
                    progress=progress,
                ),
                name=f"bsl_scoped_phase_b_worker_{i}",
            )
            for i in range(workers_n)
        ]
        first_err: Optional[BaseException] = None
        final_stats = _PhaseBStats()
        try:
            _safe_heartbeat(lease)
            results = await asyncio.gather(*tasks, return_exceptions=True)
            _safe_heartbeat(lease)
            excs = [r for r in results if isinstance(r, BaseException)]
            first_err = _log_phase_b_worker_excs(
                "BSL scoped Phase B worker raised", excs,
            )
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            final_stats = await progress.final()
        if first_err is not None:
            raise first_err
        logger.info(
            "BSL scoped Phase B: done units_requested=%d units_written=%d "
            "skipped_missing_body=%d skipped_hash_mismatch=%d "
            "skipped_empty_text=%d embedding_api_calls=%d "
            "input_tokens=%s total_tokens=%s cost=%s elapsed=%.3fs",
            final_stats.units_requested, final_stats.units_written,
            final_stats.skipped_missing_body, final_stats.skipped_hash_mismatch,
            final_stats.skipped_empty_text, final_stats.embedding_api_calls,
            _format_usage_tokens(final_stats.input_tokens),
            _format_usage_tokens(final_stats.total_tokens),
            _format_cost(*final_stats.primary_cost()),
            time.monotonic() - t0,
        )
        return final_stats

    async def _scoped_phase_b_worker(
        self,
        worker_id: int,
        total_workers: int,
        current_epoch: int,
        vector_epoch: int,
        routine_ids: List[str],
        filters: Dict[str, Any],
        batch_size: int,
        embedding_service: Any,
        doc_spec: Any,
        progress: Optional[_PhaseBProgress] = None,
    ) -> None:
        """One scoped Phase B async worker. Streams not-done units of its
        partition (fts_rowid % total_workers = worker_id) within the given
        routine_ids, batches them, and routes to the shared
        `_phase_b_process_batch` with `visible_on_upsert=False`."""
        scope = self.scope

        def _stream_partition() -> Iterable[List[Dict[str, Any]]]:
            return self.sqlite.iter_phase_b_units_for_routine_ids(
                scope, current_epoch, vector_epoch, routine_ids,
                batch_size=batch_size,
                filters=filters,
                worker_id=worker_id,
                total_workers=total_workers,
            )

        loop = asyncio.get_running_loop()
        iterator = _stream_partition()
        while True:
            batch = await loop.run_in_executor(None, lambda: next(iterator, None))
            if batch is None:
                return
            stats = await self._phase_b_process_batch(
                batch=batch,
                current_epoch=current_epoch,
                embedding_service=embedding_service,
                doc_spec=doc_spec,
                visible_on_upsert=False,
            )
            if progress is not None:
                await progress.add(stats)


# ---------------------------------------------------------------------- helpers

def _extract_feature_segments(excerpt: str) -> List[str]:
    """Region names + headers + filtered comments + directives in source order."""
    segments: List[str] = []
    if not excerpt:
        return segments
    for m in _REGION_RE.finditer(excerpt):
        segments.append(m.group(1).strip())
    for m in _COMMENT_RE.finditer(excerpt):
        text = m.group(1).strip()
        if text and not text.startswith(("Объект:", "Форма:", "Процедура:", "Функция:")):
            segments.append(text)
    for m in _HEADER_RE.finditer(excerpt):
        segments.append(m.group(1).strip())
    for m in _DIRECTIVE_RE.finditer(excerpt):
        segments.append(m.group(1).strip())
    return [s for s in segments if s]


def _token_text(value: str) -> str:
    """
    Tokenize + space-join with the base stop-word profile (NOT 1c_light),
    so BSL keywords like "возврат" survive in body/structural FTS payload —
    they are meaningful tokens for the RLM scorer.
    """
    return " ".join(tokenize(value or ""))


def _token_join(values: Sequence[str]) -> str:
    """Newline-join the list, then tokenize with the base stop-word profile."""
    joined = "\n".join(str(v) for v in (values or []) if v)
    return _token_text(joined)


def _unique_limited(values: Sequence[str], limit: int = 80) -> List[str]:
    out: List[str] = []
    seen: set = set()
    for v in values:
        v = (str(v) or "").strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
        if len(out) >= limit:
            break
    return out


def _extract_string_literals(text: str, limit: int = 80) -> List[str]:
    out: List[str] = []
    for m in _STRING_RE.finditer(text):
        value = m.group(1).replace('""', '"')
        value = re.sub(r"\s+", " ", value).strip(" |")
        if len(value) < 3 or len(value) > 160:
            continue
        if not re.search(r"[A-Za-zА-Яа-яЁё]", value):
            continue
        out.append(value)
    return _unique_limited(out, limit)


def _extract_structural_terms(text: str) -> Dict[str, List[str]]:
    metadata_refs = _unique_limited(
        f"{kind}.{name}" for kind, name in _META_REF_RE.findall(text)
    )
    query_text = "\n".join(m.group(0) for m in _QUERY_TEXT_RE.finditer(text))
    query_tables = _unique_limited(
        f"{kind}.{name}" for kind, name in _META_REF_RE.findall(query_text)
    )
    method_calls = _unique_limited(
        name
        for name in _METHOD_CALL_RE.findall(text)
        if name not in _BSL_CALL_STOP and len(name) > 2
    )
    assignments = _unique_limited(_ASSIGN_RE.findall(text))
    return {
        "metadata_refs": metadata_refs,
        "query_tables": query_tables,
        "method_calls": method_calls,
        "string_literals": _extract_string_literals(text),
        "assignments": assignments,
    }


def _extract_identifiers(text: str, limit: int = 240) -> List[str]:
    values: List[str] = []
    for chain in _IDENT_CHAIN_RE.findall(text):
        values.append(chain)
        values.extend(chain.split("."))
    for ident in _IDENT_RE.findall(text):
        if ident in _BSL_IDENT_STOP:
            continue
        if len(ident) < 3:
            continue
        values.append(ident)
    return _unique_limited(values, limit)


# Startup helper used by app/mcpsrv/server.py.
def start_bsl_code_indexing_background(loader, *, embedding_availability=None):
    """Start BSL code search indexer in a background thread.

    Returns the spawned threading.Thread, or None when BSL code search is
    disabled or the Neo4j loader is unavailable. Caller may use the returned
    handle for startup-barrier coordination.

    `embedding_availability` (optional startup EmbeddingAvailability) is passed
    to the indexer so a known-unavailable endpoint skips Phase B without hitting
    the production embedding timeout.
    """
    if not settings.enable_bsl_code_search:
        logger.info("BSL code search: indexer not started (ENABLE_BSL_CODE_SEARCH=false)")
        return None
    if not loader or not getattr(loader, "driver", None):
        logger.warning("BSL code search: cannot start indexer (Neo4j loader unavailable)")
        return None

    import threading

    def _run() -> None:
        try:
            time.sleep(2.0)
            indexer = BslCodeSearchIndexer(
                loader.driver, embedding_availability=embedding_availability
            )
            indexer.start_indexing(run_mode="startup")
        except Exception as e:
            if is_embedding_unavailable_error(e):
                # Expected outage escaped to the thread top (e.g. the
                # EmbeddingUnavailableError re-raised by full Phase B): one quiet
                # line, no traceback. SQLite catch-up recovers on the next cycle.
                logger.warning(
                    "BSL code search indexer: embedding endpoint unavailable (%s)",
                    format_embedding_error(e),
                )
            else:
                logger.error("BSL code search indexer thread failed: %s", e, exc_info=True)

    t = threading.Thread(target=_run, name="bsl_code_indexing", daemon=True)
    t.start()
    logger.info("BSL code search: indexer thread started")
    return t
