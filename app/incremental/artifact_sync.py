"""
ArtifactSync / PostLinkingSync — phases 2-4 incremental loading.

Reuse vs. new:
- XML mode переиспользует `XmlCycleContext.code_index` (один scan дерева за цикл,
  никаких новых `CodeFileIndexer.scan(code/)` для базы).
- TXT mode использует существующий streaming `CodeFileIndexer.scan(consumers=...)`.
- Cleanup-методы scoped (по project + config + qn-filter); никаких глобальных
  `MATCH ... DETACH DELETE` без scope.
- Post-linking — refresh (delete-then-merge), не add-only `MERGE`.
- `LockLease.heartbeat()` вызывается строго из main thread между крупными буфетами.

Contract:
- Если `artifact_manifest` для scope пуст, фаза для scope пропускается с WARN —
  artifact incremental требует full reload baseline.
- Deletion детектится только при `full_reconcile_allowed=True`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .artifact_hashing import FileStat
from .report import PostLinkingImpact
from .state import IncrementalLoadingState, LockLease
from indexer.data_structures import ProcessingConfig


class _BslCodeSearchSnapshotFailed(Exception):
    """Internal control-flow signal: step 4.5 of `_apply_bsl` could not
    persist the scoped delta snapshot+ledger. Caller aborts BSL apply for
    this cycle so the OLD Neo4j body survives for the next scoped retry."""

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Artifact scope keys
# ----------------------------------------------------------------------

ART_BASE_FORM_XML = "artifact:base:form_xml"
ART_BASE_FORM_BIN = "artifact:base:form_bin"
ART_BASE_PREDEFINED = "artifact:base:predefined"
ART_BASE_HELP = "artifact:base:help"
ART_BASE_EVENT_SUBSCRIPTION = "artifact:base:event_subscription"
ART_BASE_RIGHTS = "artifact:base:rights"
ART_BASE_BSL = "artifact:base:bsl"


def ext_artifact_scope(mode: str, ext_dir: str, kind: str) -> str:
    return f"artifact:ext:{mode}:{ext_dir}:{kind}"


def _bsl_scope_for_form_bin_scope(form_bin_scope: str) -> str:
    """`artifact:base:form_bin` → `artifact:base:bsl`;
    `artifact:ext:<mode>:<dir>:form_bin` → `artifact:ext:<mode>:<dir>:bsl`.

    Form.bin baseline записывается в общий BSL `bsl_file_artifacts` scope; при удалении
    Form.bin его artifact rows должны быть сняты из того же общего scope.
    """
    if form_bin_scope == ART_BASE_FORM_BIN:
        return ART_BASE_BSL
    if form_bin_scope.startswith("artifact:ext:") and form_bin_scope.endswith(":form_bin"):
        return form_bin_scope[: -len(":form_bin")] + ":bsl"
    return ""


# ----------------------------------------------------------------------
# Diff structures
# ----------------------------------------------------------------------


@dataclass
class ArtifactDiff:
    added: List[Path] = field(default_factory=list)
    changed: List[Path] = field(default_factory=list)
    unchanged: List[Path] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)  # rel_paths
    # FileStat для added и changed, посчитанный в `_diff_scope` через
    # `hash_files_parallel`. Используется в `_persist_manifest_after_apply`,
    # чтобы не хешировать те же файлы повторно через sequential `_file_hash`.
    file_stats: Dict[Path, FileStat] = field(default_factory=dict)


@dataclass
class ArtifactSummary:
    """Сводка по scope для логирования."""
    added: int = 0
    changed: int = 0
    deleted: int = 0


@dataclass
class CodeArtifactCycleContext:
    """Один контекст на цикл, шарится между phase 2/3/4."""

    project_name: str
    base_config_name: str
    base_code_directory: Path
    full_reconcile_allowed: bool = False
    data_directory: Optional[Path] = None
    # Metadata source mode для текущего цикла: 'txt' | 'xml'.
    # Используется для list_extension_scopes в base→ext PredefinedItem rebuild.
    source_mode: str = "txt"

    base_code_index: Any = None  # CodeFileIndex
    ext_code_indexes: Dict[str, Any] = field(default_factory=dict)
    # Parsed Configuration objects, нужны form workers для `resolve_datapath_bindings`.
    # Без них `BINDS_TO` edges не пересоздаются после `delete_form_xcf_subtree`.
    base_configuration: Any = None
    ext_configurations: Dict[str, Any] = field(default_factory=dict)  # ext_dir_name → Configuration

    # Per-scope diff buckets.
    affected_artifacts: Dict[str, ArtifactDiff] = field(default_factory=dict)

    # MetadataObject QN, чьим текстам для `description_embedding` нужен
    # пере-расчёт (например, после Help/ru.html изменения). Scheduler читает
    # это поле в `_run_artifact_phases` и сливает в общий
    # `embedding_repass_needed` сета `_cycle()`.
    metadata_embedding_repass_qns: Set[str] = field(default_factory=set)

    # Routine id-ы, которым нужен post-sync doc_description_embedding re-pass:
    # cleared routines (doc_description изменился) + added routines с непустым
    # doc_description. Scheduler читает в `_run_artifact_phases` и запускает
    # routine embedding re-pass при enable_routine_description_embedding=True.
    routine_doc_embedding_repass_ids: Set[str] = field(default_factory=set)

    # Phase 4 post-linking impact — единственный source of truth для handler
    # refresh targets и form-level extension rebuild scope.
    # Scheduler копирует aggregated (root + extension_reports[*]) impact в это
    # поле перед `run_base`.
    post_linking_impact: PostLinkingImpact = field(default_factory=PostLinkingImpact)

    # Affected sets, читаемые artifact phase для BSL-related operations.
    # `affected_extension_configs` — фактически обработанные `run_extensions`
    # scopes (diagnostic поле). Post-link consumers (CALLS / EXTENDS_ROUTINE)
    # читают `known_extension_configs` (registry, заполняется scheduler-ом
    # перед `run_base`).
    affected_bsl_files: List[Tuple[str, str, str]] = field(default_factory=list)  # (scope, file_path, action)
    affected_routines: Set[str] = field(default_factory=set)
    affected_modules: Set[str] = field(default_factory=set)
    affected_extension_configs: Set[Tuple[str, str]] = field(default_factory=set)  # (ext_dir, ext_config_name)

    # State-backed registry: все extensions, известные из SQLite на момент
    # начала цикла. Заполняется scheduler-ом до `run_base` через
    # `state.list_extension_scopes` + `get_extension_scope_config_qn`.
    # Используется post-link consumers и base form impact lookup.
    known_extension_configs: Dict[str, str] = field(default_factory=dict)  # ext_dir -> ext_config_name

    # Phase-1 scopes whose top-level extension directory disappeared.
    # Kept in state until BSL sidecar cleanup and graph postconditions succeed.
    removed_extension_scopes: Dict[str, str] = field(default_factory=dict)

    # SSL: при изменении подсистем СтандартныеПодсистемы (Состав / ПутьПодсистемы)
    # incremental пайплайн поднимает этот флаг — scheduler после BSL apply
    # запускает loader.refresh_ssl_api_for_project. Scoped refresh для затронутых
    # routines выполняется отдельно (через context.affected_routines).
    ssl_owners_dirty: bool = False

    # Pre-phase-2 class (a) callers для scoped CALLS.
    calls_class_a_callers: Set[str] = field(default_factory=set)

    # Form-routines из re-parsed BSL per config (для Phase 4
    # link_form_events_and_commands). Каждый config (base + ext) — свой slot.
    form_routines_by_config: Dict[str, Dict[str, List[Dict[str, Any]]]] = field(default_factory=dict)

    # New / renamed routine targets для phase 4 class (c) lookup. Tuple:
    # `(qualifier_short, manager_qualifier, name_lower, module_type, a_min, a_max)`.
    new_routine_targets: Set[Tuple[str, str, str, str, int, int]] = field(default_factory=set)

    # BSL code search delta, аккумулируется в `_apply_bsl` для phase 5
    # `BslCodeSearchSync`. None означает "ещё не инициализирован"; init происходит
    # на первом `_apply_bsl` вызове или в `_run_cycle` (если ни одного нет).
    code_search_delta: Any = None  # bsl_routine_delta.CodeSearchDelta

    # Optional BSL code search delta applier (injection point из scheduler).
    # Если None — `_apply_bsl` не вызывает `invalidate_routines`; phase 5
    # `BslCodeSearchSync` сам триггерит full rebuild через reindex_requested.
    bsl_code_search_delta_applier: Any = None
    bsl_code_search_scope: Optional[str] = None
    bsl_code_search_indexer: Any = None
    bsl_code_search_sqlite: Any = None

    def graph_changed(self) -> bool:
        """True если artifact/BSL-фаза действительно изменила Neo4j-граф в этом цикле.

        `affected_artifacts` проверяется по СОДЕРЖИМОМУ diff (added/changed/deleted), а не по
        truthiness dict-а: `affected_artifacts[scope]` пишется для каждого просканированного
        scope даже когда diff содержит только `unchanged`. `affected_extension_configs`
        (diagnostic, наполняется на каждый просканированный ext-config) в сигнал не входит.
        Остальные affected-сеты наполняются только на реальном upsert/remove в `_apply_bsl`.
        """
        if any(d.added or d.changed or d.deleted for d in self.affected_artifacts.values()):
            return True
        if (
            self.metadata_embedding_repass_qns
            or self.routine_doc_embedding_repass_ids
            or self.affected_bsl_files
            or self.affected_routines
            or self.affected_modules
            or self.ssl_owners_dirty
        ):
            return True
        if not self.post_linking_impact.is_empty():
            return True
        d = self.code_search_delta
        return d is not None and not d.is_empty()


def _record_form_impact(
    context: "CodeArtifactCycleContext",
    config_name: str,
    form_qns: List[str],
    is_extension: bool,
) -> None:
    """Записать form QN в post_linking_impact + пометить config для handler relink.

    Для extension Form.xml дополнительно отмечаем формы как
    `ext_forms_with_changed_internals` (для form-level rebuild). Для base
    Form.xml отмечаем формы как `base_forms_with_changed_internals` —
    `rebuild_form_level_extension_relationships` сам найдёт extension-формы,
    которые их adopted_from, через Cypher.
    """
    impact = context.post_linking_impact
    for fq in form_qns:
        if not fq:
            continue
        impact.add_form(config_name, fq)
        if is_extension:
            impact.mark_ext_form_internals_changed(config_name, fq)
        else:
            impact.mark_base_form_internals_changed(fq)
    if form_qns:
        impact.mark_handler_relink(config_name)


def _form_routines_slot(
    context: "CodeArtifactCycleContext", config_name: str
) -> Dict[str, List[Dict[str, Any]]]:
    """Получить per-config slot формы→routines (создать пустой если нет)."""
    return context.form_routines_by_config.setdefault(config_name, {})


# ----------------------------------------------------------------------
# Hashing
# ----------------------------------------------------------------------


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------------
# Per-scope diff
# ----------------------------------------------------------------------


def _default_key_fn(root: Path):
    def _fn(path: Path) -> str:
        return str(path.relative_to(root)).replace("\\", "/")
    return _fn


def _diff_scope(
    *,
    state: IncrementalLoadingState,
    source_scope: str,
    root: Path,
    files: List[Path],
    full_reconcile_allowed: bool,
    key_fn: Optional[Any] = None,
    hash_added: bool = True,
) -> ArtifactDiff:
    """Diff one scope: разделить files по {added, changed, unchanged} по mtime+size+hash.

    `key_fn(path) -> str` — функция построения manifest-ключа. По умолчанию POSIX-rel
    относительно `root`. Для BSL передаётся data-directory-relative ключ — совместимо
    со схемой `Routine.file_path`/`Module.path`.

    Hashing идёт параллельно через `hash_files_parallel` для файлов с
    mtime/size mismatch (cheap stat pre-filter сохраняется). Added файлы хешируются
    тем же вызовом, если `hash_added=True` (default) — их FileStat кладётся в
    `diff.file_stats` и переиспользуется в `_persist_manifest_after_apply`.

    Для BSL и Form.bin scopes caller передаёт `hash_added=False`: их manifest потом
    пишется из `ParsedBslFile.content_hash` (parse-only worker уже считает hash),
    поэтому добавочный hash в diff-фазе — лишний I/O в hot path.

    `deleted` populates только при `full_reconcile_allowed`.
    """
    from .artifact_hashing import hash_files_parallel

    diff = ArtifactDiff()
    seen_rel: Set[str] = set()
    kf = key_fn or _default_key_fn(root)

    # Pre-pass: stat + cheap mtime/size match. Mismatch и added (нет manifest) идут
    # в общий bucket для одного batched hashing вызова.
    needs_hash: List[Path] = []
    needs_hash_keys: Dict[Path, tuple] = {}  # path → (rel, manifest_or_None)
    unchanged_rows: List[Dict[str, Any]] = []  # для batch upsert manifest last_seen_at
    for path in files:
        try:
            rel = kf(path)
        except ValueError:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        seen_rel.add(rel)
        manifest = state.get_artifact_manifest(source_scope, rel)
        if manifest is None:
            # Added: hash потребуется для manifest после успешного apply, но только
            # если caller планирует писать manifest из `diff.file_stats`. Для BSL и
            # Form.bin manifest строится из `ParsedBslFile.content_hash`, поэтому
            # caller передаёт `hash_added=False` чтобы не дублировать I/O.
            if hash_added:
                needs_hash.append(path)
                needs_hash_keys[path] = (rel, None)
            else:
                diff.added.append(path)
            continue
        if manifest["mtime_ns"] == st.st_mtime_ns and manifest["size"] == st.st_size:
            diff.unchanged.append(path)
            unchanged_rows.append({
                "source_scope": source_scope,
                "rel_path": rel,
                "size": st.st_size,
                "mtime_ns": st.st_mtime_ns,
                "content_hash": manifest["content_hash"],
            })
            continue
        needs_hash.append(path)
        needs_hash_keys[path] = (rel, manifest)

    # Parallel hash для всех "нужно посчитать" (added + stat-mismatch).
    stat_map = hash_files_parallel(needs_hash) if needs_hash else {}

    changed_rows: List[Dict[str, Any]] = []
    for path in needs_hash:
        rel, manifest = needs_hash_keys[path]
        st = stat_map.get(path)
        if st is None:
            # Файл исчез между stat и hash — пропускаем. Added просто не попадёт
            # в diff.added; changed аналогично выпадает (следующий цикл повторит).
            continue
        if manifest is None:
            # Added: FileStat сохраняем в diff.file_stats; manifest пишется после apply.
            diff.added.append(path)
            diff.file_stats[path] = st
            continue
        if st.content_hash == manifest["content_hash"]:
            # Content unchanged, только mtime/size дрифт — manifest обновляется сразу.
            diff.unchanged.append(path)
            changed_rows.append({
                "source_scope": source_scope,
                "rel_path": rel,
                "size": st.size,
                "mtime_ns": st.mtime_ns,
                "content_hash": st.content_hash,
            })
            continue
        # Changed: FileStat сохраняем для post-apply manifest commit; manifest сам
        # пишется только после успешного apply через `_persist_manifest_after_apply`.
        diff.changed.append(path)
        diff.file_stats[path] = st

    if unchanged_rows or changed_rows:
        state.upsert_artifact_manifest_many(unchanged_rows + changed_rows)

    if full_reconcile_allowed:
        baseline = state.all_artifact_manifest_rel_paths(source_scope)
        diff.deleted = sorted(baseline - seen_rel)

    return diff


def _callsite_matches_any(
    callsite: Dict[str, Any],
    targets: Set[Tuple[str, str, str, str, int, int]],
) -> bool:
    """Best-effort prefilter match callsite-а к набору routine targets.

    Target tuple: `(qualifier_short, manager_qualifier, name_lower, module_type, a_min, a_max)`.

    Совместимо с поведением [CallsitesResolver](app/indexer/callsites_resolver.py):
      - CommonModule: qualifier callsite-а = последний сегмент `owner_qn` routine;
      - Manager call (`Documents.Invoice.Method()`): qualifier_parts callsite-а
        совпадает с `<category_en>.<object_name>` routine;
      - Direct (qualifier пуст): по имени, в любом модуле;
      - Missing `args_count` в callsite трактуется как unknown, не отсекается по arity
        (резолвер делает то же — точная резолюция делается в `CallsitesResolver`).

    Prefilter ВКЛЮЧАЕТ caller с потенциально matching callsite; точная резолюция
    далее выполняет `CallsitesResolver.resolve_calls(...)` на полном корпусе.
    """
    # Resolver-совместимый name contract: scanner для dynamic callsites пишет
    # `name_literal` вместо `name` ([bsl_signature_scanner.py:1319-1327](app/bsl_signature_scanner.py#L1319)).
    # Full resolver делает `raw_name = cs.get("name") or cs.get("name_literal")`.
    name = (
        callsite.get("name")
        or callsite.get("callee_name")
        or callsite.get("name_literal")
        or ""
    ).lower()
    if not name:
        return False
    cs_qualifier_raw = (callsite.get("qualifier") or callsite.get("callee_qualifier") or "").strip()
    # Normalize: short qualifier — первый сегмент chain (для CommonModule callsite).
    parts = cs_qualifier_raw.replace("/", ".").split(".") if cs_qualifier_raw else []
    cs_qualifier_short = parts[0].lower() if parts else ""
    # Manager-style qualifier: ровно два сегмента, нижний регистр; категория нормализуется
    # к EN через `_RU_TO_EN_CATEGORY` (то же, что использует `_routine_target`).
    # Resolver работает на canon RU mapping; для prefilter достаточно симметричной
    # EN-нормализации обеих сторон — оба формата (RU `Документы.X`, EN `Documents.X`)
    # дают одинаковый ключ.
    if len(parts) >= 2:
        head_en = _category_to_en(parts[0])
        cs_manager = f"{head_en}.{parts[1]}".lower()
    else:
        cs_manager = ""

    # Arity: если `args_count` присутствует — учитываем; иначе arity unknown.
    args_count_raw = callsite.get("args_count")
    if args_count_raw is None:
        argc_min: Optional[int] = None
        argc_max: Optional[int] = None
    else:
        try:
            argc_min = int(args_count_raw)
            argc_max = int(args_count_raw)
        except (TypeError, ValueError):
            argc_min = None
            argc_max = None

    for (q_short, q_manager, nm, _module_type, a_min, a_max) in targets:
        if nm != name:
            continue
        # Match по любому из двух qualifier-форматов либо пустого callsite qualifier.
        if cs_qualifier_short or cs_manager:
            q_match = False
            if cs_qualifier_short and q_short and cs_qualifier_short == q_short:
                q_match = True
            if cs_manager and q_manager and cs_manager == q_manager:
                q_match = True
            if not q_match:
                continue
        # Arity check — пропускаем при unknown callsite arity (как делает resolver).
        if argc_min is not None and (argc_max < a_min or argc_min > a_max):
            continue
        return True
    return False


def _routine_target(
    routine: Dict[str, Any],
) -> Tuple[str, str, str, str, int, int]:
    """Target tuple для class (c) prefilter:
    `(qualifier_short, manager_qualifier, name_lower, module_type, a_min, a_max)`.

    qualifier_short — последний сегмент `owner_qn` (имя модуля или объекта).
    manager_qualifier — `<category_en>.<object_name>` для manager-style routines
    ([callsites_resolver.py:247-285](app/indexer/callsites_resolver.py#L247)
    использует canon-mapped category). Для других модулей пуст.
    """
    name = (routine.get("name") or "").lower()
    owner_qn = routine.get("owner_qn") or ""
    parts = owner_qn.split("/") if owner_qn else []
    qualifier_short = parts[-1].lower() if parts else ""
    module_type = (routine.get("module_type") or "").lower()
    a_min = int(routine.get("min_arity", 0) or 0)
    a_max = int(routine.get("max_arity", a_min) or a_min)

    # Manager qualifier: project/cfg/<category_ru>/<object_name>/... — category → EN.
    manager_qualifier = ""
    if len(parts) >= 4:
        category_seg = parts[-2]
        category_en_raw = _CATEGORY_INDEX_BY_FOLD.get(category_seg.casefold(), "")
        category_en = category_en_raw  # already EN canonical
        if category_en and module_type in {
            "managermodule",
            "objectmodule",
            "recordsetmodule",
            "valuemanagermodule",
        }:
            manager_qualifier = f"{category_en}.{qualifier_short}".lower()

    return (qualifier_short, manager_qualifier, name, module_type, a_min, a_max)


def _category_to_en(name: str) -> str:
    """Case-insensitive RU → EN категория. Принимает RU title-case/lower/upper и EN
    (last identity для уже EN-input). Совпадает с поведением [canon_category]
    (app/graphdb/category_canon.py:225-230) по case-fold нормализации.
    """
    if not name:
        return ""
    folded = name.casefold()
    if folded in _CATEGORY_INDEX_BY_FOLD:
        return _CATEGORY_INDEX_BY_FOLD[folded]
    return name


# Reverse mapping для manager qualifier (зеркало RU/EN категорий 1C).
_RU_TO_EN_CATEGORY: Dict[str, str] = {
    "Документы": "Documents",
    "Справочники": "Catalogs",
    "Перечисления": "Enums",
    "Отчеты": "Reports",
    "Обработки": "DataProcessors",
    "ПланыВидовХарактеристик": "ChartsOfCharacteristicTypes",
    "ПланыСчетов": "ChartsOfAccounts",
    "ПланыВидовРасчета": "ChartsOfCalculationTypes",
    "РегистрыСведений": "InformationRegisters",
    "РегистрыНакопления": "AccumulationRegisters",
    "РегистрыБухгалтерии": "AccountingRegisters",
    "РегистрыРасчета": "CalculationRegisters",
    "БизнесПроцессы": "BusinessProcesses",
    "Задачи": "Tasks",
    "ЖурналыДокументов": "DocumentJournals",
    "Константы": "Constants",
    "ПланыОбмена": "ExchangePlans",
    "ОбщиеМодули": "CommonModules",
}


# Case-insensitive lookup: каждое имя (RU и EN) → EN canonical. Покрывает RU title,
# lower, upper и сам EN identity (`documents` → `Documents`).
_CATEGORY_INDEX_BY_FOLD: Dict[str, str] = {}
for _ru, _en in _RU_TO_EN_CATEGORY.items():
    _CATEGORY_INDEX_BY_FOLD[_ru.casefold()] = _en
    _CATEGORY_INDEX_BY_FOLD[_en.casefold()] = _en
del _ru, _en


def _persist_manifest_after_apply(
    state: IncrementalLoadingState,
    source_scope: str,
    root: Path,
    diff: ArtifactDiff,
    *,
    transactional: bool = True,
) -> None:
    """После успешного apply — записать в `artifact_manifest` свежие hash/mtime
    для added/changed и удалить rows для deleted. Без этого следующий incremental
    цикл снова видит те же файлы как changed (см. counterexample R3.3).

    Использует `diff.file_stats` (заполненный `_diff_scope` через `hash_files_parallel`),
    чтобы не хешировать те же файлы повторно. Файлы, для которых stat отсутствует
    (lost между diff и apply), пропускаются молча — следующий цикл повторит.
    Upsert + delete пишутся одной транзакцией.
    """
    rows: List[Dict[str, Any]] = []
    for path in list(diff.added) + list(diff.changed):
        st = diff.file_stats.get(path)
        if st is None:
            continue
        try:
            rel = str(path.relative_to(root)).replace("\\", "/")
        except ValueError:
            continue
        rows.append({
            "source_scope": source_scope,
            "rel_path": rel,
            "size": st.size,
            "mtime_ns": st.mtime_ns,
            "content_hash": st.content_hash,
        })
    def _write() -> None:
        if rows:
            state.upsert_artifact_manifest_many(rows)
        if diff.deleted:
            state.delete_artifact_manifest(source_scope, diff.deleted)

    if not rows and not diff.deleted:
        return
    if transactional:
        with state.transaction():
            _write()
    else:
        _write()


def _bsl_key_fn(data_directory: Optional[Path], fallback_root: Path):
    """POSIX-relative от data_directory (как `Routine.file_path`/`Module.path`).

    Lexical-only: `CodeFileIndex` уже содержит абсолютные пути без симлинков
    (`/app/data/...`), а `settings.data_directory` hardcoded. Поэтому путь не
    резолвится через ФС — это hot path в `manifest rows build`.

    Контракт: если путь не относится ни к `data_directory`, ни к `fallback_root`,
    функция бросает `ValueError`. Callers (`_diff_scope`, `_apply_bsl._key_for`,
    `main.py` baseline loop) ловят его как сигнал «skip path вне scope».
    """
    base = Path(data_directory) if data_directory is not None else Path(fallback_root)
    fallback = Path(fallback_root)

    def _fn(path: Path) -> str:
        p = path if isinstance(path, Path) else Path(path)
        try:
            rel = p.relative_to(base)
        except ValueError:
            rel = p.relative_to(fallback)
        return str(rel).replace("\\", "/")

    return _fn


# ----------------------------------------------------------------------
# Artifact resolvers — file path → object/owner QN
# ----------------------------------------------------------------------


def _form_qn_from_form_xml_entry(entry: Any, project_name: str, config_name: str) -> Optional[str]:
    """FormXmlEntry → form qualified_name `project/config/category_ru/object/Form/form`."""
    from xml_metadata.folder_map import FOLDER_TO_RU_CATEGORY

    folder = getattr(entry, "category_folder", "")
    object_name = getattr(entry, "object_name", "")
    form_name = getattr(entry, "form_name", "")
    if not (folder and object_name and form_name):
        return None
    cat_ru = FOLDER_TO_RU_CATEGORY.get(folder)
    if not cat_ru:
        return None
    return f"{project_name}/{config_name}/{cat_ru}/{object_name}/Form/{form_name}"


def _owner_object_qn_from_path(
    rel_path: str, project_name: str, config_name: str
) -> Optional[str]:
    """Resolve owner object QN из пути типа `Documents/Doc1/Ext/Predefined.xml`."""
    from xml_metadata.folder_map import FOLDER_TO_RU_CATEGORY

    parts = rel_path.replace("\\", "/").strip("/").split("/")
    if len(parts) < 2:
        return None
    folder = parts[0]
    if folder not in FOLDER_TO_RU_CATEGORY:
        return None
    cat_ru = FOLDER_TO_RU_CATEGORY[folder]
    object_name = parts[1]
    if object_name.endswith(".xml"):
        object_name = object_name[: -len(".xml")]
    return f"{project_name}/{config_name}/{cat_ru}/{object_name}"


def _role_qn_from_rights_path(
    rel_path: str, project_name: str, config_name: str
) -> Optional[str]:
    """`Roles/<RoleName>/Ext/Rights.xml` → `project/config/Роли/<RoleName>`."""
    parts = rel_path.replace("\\", "/").strip("/").split("/")
    if len(parts) < 4 or parts[0] != "Roles":
        return None
    return f"{project_name}/{config_name}/Роли/{parts[1]}"


def _subscription_qn_from_path(
    rel_path: str, project_name: str, config_name: str
) -> Optional[str]:
    """`EventSubscriptions/<Name>.xml` → `project/config/ПодпискиНаСобытия/<Name>`."""
    parts = rel_path.replace("\\", "/").strip("/").split("/")
    if len(parts) < 2 or parts[0] != "EventSubscriptions":
        return None
    name = parts[1]
    if name.endswith(".xml"):
        name = name[: -len(".xml")]
    return f"{project_name}/{config_name}/ПодпискиНаСобытия/{name}"


# ----------------------------------------------------------------------
# ArtifactSync
# ----------------------------------------------------------------------


class ArtifactSync:
    """Phase 2 (base) + Phase 3 (extensions) — заливка artifact diff в граф."""

    def __init__(self, loader: Any, state: IncrementalLoadingState) -> None:
        self.loader = loader
        self.state = state

    # -- Phase 2 base -------------------------------------------------

    def run_base(
        self,
        *,
        settings_obj: Any,
        context: CodeArtifactCycleContext,
        lease: Optional[LockLease] = None,
    ) -> Dict[str, ArtifactSummary]:
        """Запустить phase 2 для базы. Возвращает per-scope summary для логирования."""
        if context.base_code_index is None:
            logger.info("Phase 2 base: no code index in context; skipping")
            return {}

        proc_config = ProcessingConfig.from_settings(settings_obj)
        _log_skipped_base_scopes(proc_config)

        project_name = context.project_name
        config_name = context.base_config_name
        root = context.base_code_directory
        index = context.base_code_index
        summaries: Dict[str, ArtifactSummary] = {}

        # Form.xml (base).
        if proc_config.enable_forms:
            form_xml_paths = [e.form_xml_path for e in (index.form_xml_files or [])]
            if self.state.has_any_artifact_baseline(ART_BASE_FORM_XML) or form_xml_paths:
                diff = _diff_scope(
                    state=self.state,
                    source_scope=ART_BASE_FORM_XML,
                    root=root,
                    files=form_xml_paths,
                    full_reconcile_allowed=context.full_reconcile_allowed,
                )
                context.affected_artifacts[ART_BASE_FORM_XML] = diff
                self._apply_form_xml(
                    project_name=project_name,
                    config_name=config_name,
                    source_scope=ART_BASE_FORM_XML,
                    root=root,
                    index=index,
                    diff=diff,
                    context=context,
                    cfg_obj=context.base_configuration,
                )
                summaries[ART_BASE_FORM_XML] = ArtifactSummary(
                    added=len(diff.added), changed=len(diff.changed), deleted=len(diff.deleted)
                )
        if lease is not None:
            lease.heartbeat()

        # Form.bin (base). Только cleanup + manifest update в этом шаге; re-parse
        # будет совмещён с BSL ниже (Form.bin идёт через тот же BSLProcessor).
        # `hash_added=False`: Form.bin manifest пишется из `pbf.content_hash`
        # в `_apply_bsl`, а не из `diff.file_stats` — pre-hash был бы лишним I/O.
        # Form.bin + BSL гейтятся одним флагом `enable_bsl` (full load использует тот же
        # BSLProcessor для обоих наборов файлов; coupling нельзя расщеплять).
        form_bin_diff: Optional[ArtifactDiff] = None
        if proc_config.enable_bsl:
            form_bin_paths = list(index.form_bin_files or [])
            form_bin_diff = _diff_scope(
                state=self.state,
                source_scope=ART_BASE_FORM_BIN,
                root=root,
                files=form_bin_paths,
                full_reconcile_allowed=context.full_reconcile_allowed,
                hash_added=False,
            )
            context.affected_artifacts[ART_BASE_FORM_BIN] = form_bin_diff
            self._apply_form_bin(
                project_name=project_name,
                config_name=config_name,
                source_scope=ART_BASE_FORM_BIN,
                root=root,
                diff=form_bin_diff,
                context=context,
            )
            summaries[ART_BASE_FORM_BIN] = ArtifactSummary(
                added=len(form_bin_diff.added), changed=len(form_bin_diff.changed), deleted=len(form_bin_diff.deleted)
            )
        if lease is not None:
            lease.heartbeat()

        # Predefined.xml (base).
        if proc_config.enable_predefined:
            predef_paths = list(index.predefined_xml_files or [])
            diff = _diff_scope(
                state=self.state,
                source_scope=ART_BASE_PREDEFINED,
                root=root,
                files=predef_paths,
                full_reconcile_allowed=context.full_reconcile_allowed,
            )
            context.affected_artifacts[ART_BASE_PREDEFINED] = diff
            base_predef_owners = self._apply_predefined(
                project_name=project_name,
                config_name=config_name,
                source_scope=ART_BASE_PREDEFINED,
                root=root,
                diff=diff,
            )
            summaries[ART_BASE_PREDEFINED] = ArtifactSummary(
                added=len(diff.added), changed=len(diff.changed), deleted=len(diff.deleted)
            )
            if base_predef_owners:
                # Base Predefined.xml меняется — спроецировать base owner_qn в каждое
                # известное extension config и пересобрать PredefinedItem ADOPTED_FROM
                # scoped по owner subtree. Источник truth для extension list — существующий
                # state-backed registry (list_extension_scopes + get_extension_scope_config_qn).
                self._rebuild_predefineditem_adopted_from_for_base_owners(
                    project_name=project_name,
                    base_config_name=config_name,
                    base_owner_qns=base_predef_owners,
                    source_mode=context.source_mode,
                )

        # Help/ru.html (base).
        if proc_config.enable_help:
            help_paths = list(index.help_html_files or [])
            diff = _diff_scope(
                state=self.state,
                source_scope=ART_BASE_HELP,
                root=root,
                files=help_paths,
                full_reconcile_allowed=context.full_reconcile_allowed,
            )
            context.affected_artifacts[ART_BASE_HELP] = diff
            self._apply_help(
                project_name=project_name,
                config_name=config_name,
                source_scope=ART_BASE_HELP,
                root=root,
                diff=diff,
                context=context,
            )
            summaries[ART_BASE_HELP] = ArtifactSummary(
                added=len(diff.added), changed=len(diff.changed), deleted=len(diff.deleted)
            )

        # EventSubscriptions (base).
        if proc_config.enable_event_subscriptions:
            es_paths = list(index.event_subscription_xml_files or [])
            diff = _diff_scope(
                state=self.state,
                source_scope=ART_BASE_EVENT_SUBSCRIPTION,
                root=root,
                files=es_paths,
                full_reconcile_allowed=context.full_reconcile_allowed,
            )
            context.affected_artifacts[ART_BASE_EVENT_SUBSCRIPTION] = diff
            self._apply_event_subscriptions(
                project_name=project_name,
                config_name=config_name,
                source_scope=ART_BASE_EVENT_SUBSCRIPTION,
                root=root,
                diff=diff,
                context=context,
            )
            summaries[ART_BASE_EVENT_SUBSCRIPTION] = ArtifactSummary(
                added=len(diff.added), changed=len(diff.changed), deleted=len(diff.deleted)
            )

        # Rights.xml (base).
        if proc_config.enable_role_rights:
            rights_paths = list(index.rights_xml_files or [])
            diff = _diff_scope(
                state=self.state,
                source_scope=ART_BASE_RIGHTS,
                root=root,
                files=rights_paths,
                full_reconcile_allowed=context.full_reconcile_allowed,
            )
            context.affected_artifacts[ART_BASE_RIGHTS] = diff
            self._apply_rights_base(
                project_name=project_name,
                config_name=config_name,
                source_scope=ART_BASE_RIGHTS,
                root=root,
                diff=diff,
            )
            summaries[ART_BASE_RIGHTS] = ArtifactSummary(
                added=len(diff.added), changed=len(diff.changed), deleted=len(diff.deleted)
            )
        if lease is not None:
            lease.heartbeat()

        # BSL (base). Form.bin add/changed подаются в тот же BSLProcessor.
        # `hash_added=False`: BSL manifest пишется из `pbf.content_hash` в `_apply_bsl`,
        # parse-only worker уже считает SHA256 — pre-hash в diff-фазе был бы лишним I/O.
        if proc_config.enable_bsl:
            bsl_paths = list(index.bsl_files or [])
            diff = _diff_scope(
                state=self.state,
                source_scope=ART_BASE_BSL,
                root=root,
                files=bsl_paths,
                full_reconcile_allowed=context.full_reconcile_allowed,
                key_fn=_bsl_key_fn(context.data_directory, root),
                hash_added=False,
            )
            context.affected_artifacts[ART_BASE_BSL] = diff
            extras = (
                list(form_bin_diff.added) + list(form_bin_diff.changed)
                if form_bin_diff is not None else []
            )
            self._apply_bsl(
                project_name=project_name,
                config_name=config_name,
                source_scope=ART_BASE_BSL,
                root=root,
                diff=diff,
                context=context,
                settings_obj=settings_obj,
                lease=lease,
                extra_files_to_parse=extras,
                extras_manifest_scope=ART_BASE_FORM_BIN,
            )
            summaries[ART_BASE_BSL] = ArtifactSummary(
                added=len(diff.added), changed=len(diff.changed), deleted=len(diff.deleted)
            )

        # Log summary.
        _log_base_summary(summaries)
        return summaries

    # -- Phase 3 extensions -------------------------------------------

    def run_extensions(
        self,
        *,
        settings_obj: Any,
        context: CodeArtifactCycleContext,
        lease: Optional[LockLease] = None,
    ) -> Dict[str, Dict[str, ArtifactSummary]]:
        """Phase 3 для каждого расширения, известного state-у. Возвращает
        per-extension per-scope summary."""
        all_summaries: Dict[str, Dict[str, ArtifactSummary]] = {}

        proc_config = ProcessingConfig.from_settings(settings_obj)
        if not proc_config.enable_extensions:
            logger.info("Phase 3: skipping all extension artifacts (LOAD_EXTENSIONS=false)")
            return {}
        _log_skipped_ext_scopes(proc_config)

        # XML extensions discovered via xml_walker.
        # TXT extensions discovered via state phase 1 scopes.
        source_mode = getattr(settings_obj, "metadata_source", "txt")
        scopes = self.state.list_extension_scopes(source_mode)

        for scope in sorted(scopes):
            ext_dir_name = scope.split(f"{source_mode}_ext:", 1)[-1]
            ext_config_qn = self.state.get_extension_scope_config_qn(scope)
            if not ext_config_qn:
                continue
            ext_config_name = ext_config_qn.split("/", 1)[1] if "/" in ext_config_qn else ext_config_qn

            # Get ext_code_dir + code_index.
            extensions_dir = getattr(settings_obj, "extensions_directory", None)
            if extensions_dir is None:
                continue
            ext_code_dir = extensions_dir / ext_dir_name / "code"
            if scope in context.removed_extension_scopes:
                ext_summaries = self._prepare_removed_extension_bsl(
                    settings_obj=settings_obj,
                    context=context,
                    lease=lease,
                    source_mode=source_mode,
                    source_scope=scope,
                    ext_dir_name=ext_dir_name,
                    ext_config_name=ext_config_name,
                    ext_code_dir=ext_code_dir,
                )
                all_summaries[ext_dir_name] = ext_summaries
                _log_extension_summary(ext_dir_name, ext_summaries)
                if lease is not None:
                    lease.heartbeat()
                continue
            if not ext_code_dir.exists():
                continue

            code_index = context.ext_code_indexes.get(ext_dir_name)
            if code_index is None:
                # Fallback scan with forms-before-BSL contract (TXT) — здесь это просто scan
                # без consumers, потому что artifact_sync обрабатывает каждый bucket сам.
                from indexer.code_file_index import CodeFileIndexer

                code_index = CodeFileIndexer.scan(ext_code_dir)
                context.ext_code_indexes[ext_dir_name] = code_index

            # TXT extension: scheduler не парсит Configuration заранее, потому что
            # `ext_code_indexes` ещё пуст в `_parse_configurations_for_artifacts`.
            # Парсим здесь lazy — иначе `worker_extension_form(cfg_obj=None)` дропает
            # DataPath bindings (R12.1).
            if context.ext_configurations.get(ext_dir_name) is None:
                try:
                    from indexer.metadata_loader import MetadataLoader

                    ml = MetadataLoader()
                    if source_mode == "xml":
                        configs = ml.load_configurations(
                            ext_code_dir, code_index=code_index, source="xml",
                            is_extension=True,
                        )
                    else:
                        ext_meta_dir = ext_code_dir.parent / "metadata"
                        configs = ml.load_configurations(
                            ext_meta_dir, source="txt", is_extension=True,
                        ) if ext_meta_dir.exists() else None
                    if configs:
                        context.ext_configurations[ext_dir_name] = configs[0]
                except Exception:
                    logger.exception(
                        "Phase 3: lazy Configuration parse failed for ext=%s",
                        ext_dir_name,
                    )

            ext_summaries: Dict[str, ArtifactSummary] = {}
            context.affected_extension_configs.add((ext_dir_name, ext_config_name))

            # Form.xml ext.
            form_xml_paths = [e.form_xml_path for e in (code_index.form_xml_files or [])]
            scope_form = ext_artifact_scope(source_mode, ext_dir_name, "form_xml")
            diff = _diff_scope(
                state=self.state,
                source_scope=scope_form,
                root=ext_code_dir,
                files=form_xml_paths,
                full_reconcile_allowed=context.full_reconcile_allowed,
            )
            context.affected_artifacts[scope_form] = diff
            self._apply_form_xml(
                project_name=context.project_name,
                config_name=ext_config_name,
                source_scope=scope_form,
                root=ext_code_dir,
                index=code_index,
                diff=diff,
                context=context,
                is_extension=True,
                cfg_obj=context.ext_configurations.get(ext_dir_name),
            )
            ext_summaries[scope_form] = ArtifactSummary(
                added=len(diff.added), changed=len(diff.changed), deleted=len(diff.deleted)
            )

            # Form.bin ext. Re-parse совмещён с BSL ext ниже.
            # `hash_added=False`: см. комментарий для base Form.bin.
            form_bin_paths = list(code_index.form_bin_files or [])
            scope_fbin = ext_artifact_scope(source_mode, ext_dir_name, "form_bin")
            ext_form_bin_diff = _diff_scope(
                state=self.state,
                source_scope=scope_fbin,
                root=ext_code_dir,
                files=form_bin_paths,
                full_reconcile_allowed=context.full_reconcile_allowed,
                hash_added=False,
            )
            context.affected_artifacts[scope_fbin] = ext_form_bin_diff
            self._apply_form_bin(
                project_name=context.project_name,
                config_name=ext_config_name,
                source_scope=scope_fbin,
                root=ext_code_dir,
                diff=ext_form_bin_diff,
                context=context,
            )
            ext_summaries[scope_fbin] = ArtifactSummary(
                added=len(ext_form_bin_diff.added), changed=len(ext_form_bin_diff.changed),
                deleted=len(ext_form_bin_diff.deleted),
            )

            # Predefined.xml ext.
            predef_paths = list(code_index.predefined_xml_files or [])
            scope_predef = ext_artifact_scope(source_mode, ext_dir_name, "predefined")
            diff = _diff_scope(
                state=self.state,
                source_scope=scope_predef,
                root=ext_code_dir,
                files=predef_paths,
                full_reconcile_allowed=context.full_reconcile_allowed,
            )
            context.affected_artifacts[scope_predef] = diff
            ext_predef_owners = self._apply_predefined(
                project_name=context.project_name,
                config_name=ext_config_name,
                source_scope=scope_predef,
                root=ext_code_dir,
                diff=diff,
            )
            ext_summaries[scope_predef] = ArtifactSummary(
                added=len(diff.added), changed=len(diff.changed), deleted=len(diff.deleted)
            )
            if ext_predef_owners:
                self._rebuild_predefineditem_adopted_from_for_ext_owners(
                    project_name=context.project_name,
                    ext_config_name=ext_config_name,
                    base_config_name=context.base_config_name,
                    owner_qns=ext_predef_owners,
                )

            # Extension property analysis (Classifier + Extractor).
            pa_paths = list(
                getattr(code_index, "extension_property_analysis_xml_files", None) or []
            )
            scope_pa = ext_artifact_scope(source_mode, ext_dir_name, "property_analysis")
            pa_diff = _diff_scope(
                state=self.state,
                source_scope=scope_pa,
                root=ext_code_dir,
                files=pa_paths,
                full_reconcile_allowed=context.full_reconcile_allowed,
            )
            context.affected_artifacts[scope_pa] = pa_diff
            ext_cfg_qn = f"{context.project_name}/{ext_config_name}"
            self._apply_property_analysis(
                project_name=context.project_name,
                ext_config_qn=ext_cfg_qn,
                source_scope=scope_pa,
                root=ext_code_dir,
                diff=pa_diff,
                xml_files=pa_paths,
                settings_obj=settings_obj,
            )
            ext_summaries[scope_pa] = ArtifactSummary(
                added=len(pa_diff.added),
                changed=len(pa_diff.changed),
                deleted=len(pa_diff.deleted),
            )

            # Help ext. Гейт `enable_help` зеркалит full load
            # (`extensions_loader.py:550` гейтит ext help тем же флагом).
            if proc_config.enable_help:
                help_paths = list(code_index.help_html_files or [])
                scope_help = ext_artifact_scope(source_mode, ext_dir_name, "help")
                diff = _diff_scope(
                    state=self.state,
                    source_scope=scope_help,
                    root=ext_code_dir,
                    files=help_paths,
                    full_reconcile_allowed=context.full_reconcile_allowed,
                )
                context.affected_artifacts[scope_help] = diff
                self._apply_help(
                    project_name=context.project_name,
                    config_name=ext_config_name,
                    source_scope=scope_help,
                    root=ext_code_dir,
                    diff=diff,
                    context=context,
                )
                ext_summaries[scope_help] = ArtifactSummary(
                    added=len(diff.added), changed=len(diff.changed), deleted=len(diff.deleted)
                )

            # EventSubscriptions ext. Гейт `enable_event_subscriptions` зеркалит full load
            # (`extensions_loader.py:498`).
            if proc_config.enable_event_subscriptions:
                es_paths = list(code_index.event_subscription_xml_files or [])
                scope_es = ext_artifact_scope(source_mode, ext_dir_name, "event_subscription")
                diff = _diff_scope(
                    state=self.state,
                    source_scope=scope_es,
                    root=ext_code_dir,
                    files=es_paths,
                    full_reconcile_allowed=context.full_reconcile_allowed,
                )
                context.affected_artifacts[scope_es] = diff
                self._apply_event_subscriptions(
                    project_name=context.project_name,
                    config_name=ext_config_name,
                    source_scope=scope_es,
                    root=ext_code_dir,
                    diff=diff,
                    context=context,
                )
                ext_summaries[scope_es] = ArtifactSummary(
                    added=len(diff.added), changed=len(diff.changed), deleted=len(diff.deleted)
                )

            # Rights ext. Гейт `enable_role_rights` зеркалит full load
            # (`extensions_loader.py:528`).
            if proc_config.enable_role_rights:
                rights_paths = list(code_index.rights_xml_files or [])
                scope_rights = ext_artifact_scope(source_mode, ext_dir_name, "rights")
                diff = _diff_scope(
                    state=self.state,
                    source_scope=scope_rights,
                    root=ext_code_dir,
                    files=rights_paths,
                    full_reconcile_allowed=context.full_reconcile_allowed,
                )
                context.affected_artifacts[scope_rights] = diff
                self._apply_rights_extension(
                    project_name=context.project_name,
                    ext_config_name=ext_config_name,
                    base_config_name=context.base_config_name,
                    source_scope=scope_rights,
                    root=ext_code_dir,
                    diff=diff,
                )
                ext_summaries[scope_rights] = ArtifactSummary(
                    added=len(diff.added), changed=len(diff.changed), deleted=len(diff.deleted)
                )

            # BSL ext. Контракт forms-before-BSL: для TXT extension consumers НЕ подключают
            # on_bsl_file во время scan, поэтому BSL обрабатывается ТОЛЬКО после Form.xml
            # выше. Здесь мы уже после загрузки Form-узлов — порядок сохранён.
            # `hash_added=False`: см. комментарий для base BSL.
            bsl_paths = list(code_index.bsl_files or [])
            scope_bsl = ext_artifact_scope(source_mode, ext_dir_name, "bsl")
            diff = _diff_scope(
                state=self.state,
                source_scope=scope_bsl,
                root=ext_code_dir,
                files=bsl_paths,
                full_reconcile_allowed=context.full_reconcile_allowed,
                key_fn=_bsl_key_fn(context.data_directory, ext_code_dir),
                hash_added=False,
            )
            context.affected_artifacts[scope_bsl] = diff
            self._apply_bsl(
                project_name=context.project_name,
                config_name=ext_config_name,
                source_scope=scope_bsl,
                root=ext_code_dir,
                diff=diff,
                extra_files_to_parse=list(ext_form_bin_diff.added) + list(ext_form_bin_diff.changed),
                extras_manifest_scope=scope_fbin,
                context=context,
                settings_obj=settings_obj,
                lease=lease,
            )
            ext_summaries[scope_bsl] = ArtifactSummary(
                added=len(diff.added), changed=len(diff.changed), deleted=len(diff.deleted)
            )

            all_summaries[ext_dir_name] = ext_summaries
            _log_extension_summary(ext_dir_name, ext_summaries)
            if lease is not None:
                lease.heartbeat()

        return all_summaries

    def _prepare_removed_extension_bsl(
        self,
        *,
        settings_obj: Any,
        context: CodeArtifactCycleContext,
        lease: Optional[LockLease],
        source_mode: str,
        source_scope: str,
        ext_dir_name: str,
        ext_config_name: str,
        ext_code_dir: Path,
    ) -> Dict[str, ArtifactSummary]:
        """Feed every saved BSL file of a removed extension into normal delta cleanup.

        The directory no longer exists, so a regular code-index scan cannot
        produce ``deleted`` paths. The artifact manifest is the authoritative
        baseline and remains available because phase 1 only registered the
        pending removal.
        """
        scope_bsl = ext_artifact_scope(source_mode, ext_dir_name, "bsl")
        # Use both baselines. A partially failed earlier cycle can remove an
        # artifact_manifest row while retaining bsl_file_artifacts (or vice
        # versa); the union preserves every routine/module ID available for
        # cleanup.
        deleted_path_set = set(
            self.state.all_artifact_manifest_rel_paths(scope_bsl)
        )
        deleted_path_set.update(
            row["rel_path"]
            for row in self.state.all_bsl_file_artifacts(scope_bsl)
            if row.get("rel_path")
        )
        deleted_paths = sorted(deleted_path_set)
        diff = ArtifactDiff(deleted=deleted_paths)
        context.affected_artifacts[scope_bsl] = diff
        if deleted_paths:
            self._apply_bsl(
                project_name=context.project_name,
                config_name=ext_config_name,
                source_scope=scope_bsl,
                root=ext_code_dir,
                diff=diff,
                extra_files_to_parse=[],
                extras_manifest_scope=ext_artifact_scope(
                    source_mode, ext_dir_name, "form_bin"
                ),
                context=context,
                settings_obj=settings_obj,
                lease=lease,
            )
        else:
            logger.warning(
                "Removed extension has no BSL artifact baseline: scope=%s",
                source_scope,
            )
        return {
            scope_bsl: ArtifactSummary(deleted=len(deleted_paths)),
        }

    def finalize_removed_extensions(
        self,
        *,
        settings_obj: Any,
        context: CodeArtifactCycleContext,
        report: Any,
        lease: Optional[LockLease] = None,
    ) -> int:
        """Purge graph residue and discard state only after BSL cleanup succeeded."""
        total_deleted = 0
        code_search_enabled = bool(
            getattr(settings_obj, "enable_bsl_code_search", False)
        )
        sqlite = context.bsl_code_search_sqlite
        sqlite_scope = context.bsl_code_search_scope or context.project_name

        for source_scope, config_name in sorted(
            context.removed_extension_scopes.items()
        ):
            if code_search_enabled:
                if sqlite is None:
                    raise RuntimeError(
                        f"BSL code-search sidecar unavailable for {source_scope}"
                    )
                remaining_units = sqlite.count_units_for_config(
                    sqlite_scope, config_name
                )
                if remaining_units:
                    raise RuntimeError(
                        f"BSL cleanup incomplete for {source_scope}: "
                        f"{remaining_units} units remain"
                    )

            deleted = self.loader.delete_extension_scope(
                context.project_name, config_name
            )
            total_deleted += deleted
            if lease is not None:
                lease.heartbeat()

            remaining_nodes = self.loader.count_extension_scope_nodes(
                context.project_name, config_name
            )
            if remaining_nodes:
                raise RuntimeError(
                    f"graph cleanup incomplete for {source_scope}: "
                    f"{remaining_nodes} nodes remain"
                )

            # Destructive state transition is deliberately last. If any
            # postcondition above fails, manifests remain available for retry.
            with self.state.transaction():
                self.state.delete_scope(source_scope)
            context.known_extension_configs.pop(
                source_scope.split(":", 1)[-1], None
            )
            report.notes.append(
                f"extension scope removed: scope={source_scope} "
                f"config={config_name} graph_nodes={deleted}"
            )
            logger.info(
                "Extension scope removed completely: scope=%s "
                "ext_graph_config_name=%s graph_nodes=%d",
                source_scope,
                config_name,
                deleted,
            )

        return total_deleted

    # -- Apply helpers -----------------------------------------------

    def _apply_form_xml(
        self,
        *,
        project_name: str,
        config_name: str,
        source_scope: str,
        root: Path,
        index: Any,
        diff: ArtifactDiff,
        context: CodeArtifactCycleContext,
        is_extension: bool = False,
        cfg_obj: Any = None,
    ) -> None:
        """Удалить XCF subtree affected форм + перепарсить + загрузить через
        forms_processor / load_form_definitions.
        """
        if not (diff.added or diff.changed or diff.deleted):
            return

        # Map path → FormXmlEntry для определения form_qn.
        entry_by_path: Dict[Path, Any] = {e.form_xml_path: e for e in (index.form_xml_files or [])}

        # Раздельные form_qn множества для deleted vs changed/added — позволяет
        # сделать ранний return для changed/added при отсутствии cfg_obj, не
        # повреждая graph (R13.1).
        changed_added_form_qns: List[str] = []
        for path in list(diff.added) + list(diff.changed):
            entry = entry_by_path.get(path)
            if entry is None:
                continue
            form_qn = _form_qn_from_form_xml_entry(entry, project_name, config_name)
            if form_qn:
                changed_added_form_qns.append(form_qn)
        deleted_form_qns: List[str] = []
        for rel_path in diff.deleted:
            form_qn = _form_qn_from_rel_path(rel_path, project_name, config_name)
            if form_qn:
                deleted_form_qns.append(form_qn)

        # Guard: если changed/added есть, но cfg_obj отсутствует — НЕ удаляем
        # XCF subtree (иначе BINDS_TO исчезает без re-create), оставляем форму
        # как есть до следующего цикла. Deleted при этом всё ещё обрабатывается:
        # для них DataPath resolution не нужен.
        if (diff.added or diff.changed) and cfg_obj is None:
            logger.warning(
                "Phase artifact form_xml: cfg_obj is None for scope=%s; "
                "changed/added forms skipped to preserve BINDS_TO edges. "
                "deleted forms processed normally.",
                source_scope,
            )
            if deleted_form_qns:
                self.loader.delete_form_xcf_subtree(
                    project_name, config_name, sorted(set(deleted_form_qns))
                )
                self.loader.clear_form_content_hash(
                    project_name, config_name, sorted(set(deleted_form_qns))
                )
                _record_form_impact(context, config_name, deleted_form_qns, is_extension)
            if diff.deleted:
                self.state.delete_artifact_manifest(source_scope, diff.deleted)
            # changed/added manifest НЕ коммитится — следующий цикл повторит.
            return

        # R14: parse-first. Все changed/added должны успешно распарситься (worker не
        # вернёт None и не бросит) ДО destructive `delete_form_xcf_subtree`. Иначе
        # parse failure (например, temporarily incomplete XML во время bind-mounted
        # экспорта) уничтожает существующее subtree без re-create.
        from indexer.data_structures import FormsData
        from indexer.forms_processor import FormsProcessor
        from indexer.workers import worker_form_xml, worker_extension_form

        if is_extension:
            from indexer.extension_scanner import _read_form_belonging

        fp = FormsProcessor()
        forms_data = FormsData()
        form_hash_rows: List[Dict[str, Any]] = []
        apply_ok = True
        changed_added_results: List[Dict[str, Any]] = []

        for path in (list(diff.added) + list(diff.changed)):
            entry = entry_by_path.get(path)
            if entry is None:
                apply_ok = False
                continue
            try:
                if is_extension:
                    descriptor = getattr(entry, "descriptor_xml_path", None)
                    is_adopted = (
                        _read_form_belonging(descriptor)
                        if descriptor and Path(descriptor).exists()
                        else False
                    )
                    result = worker_extension_form(
                        path,
                        is_adopted,
                        config_name,
                        root,
                        project_name,
                        fp.parser,
                        cfg_obj=cfg_obj,
                    )
                else:
                    cfg_by_name_local: Dict[str, Any] = (
                        {config_name: cfg_obj} if cfg_obj is not None else {}
                    )
                    result = worker_form_xml(
                        path,
                        config_name,
                        root,
                        project_name,
                        fp.parser,
                        cfg_by_name=cfg_by_name_local,
                    )
            except Exception:
                logger.exception("Phase artifact form_xml worker failed for %s", path)
                apply_ok = False
                continue
            # Parse failure (None) или missing rows ломают предусловие safe-cleanup.
            if result is None:
                logger.warning(
                    "Phase artifact form_xml: worker returned None for %s; "
                    "deferring changed/added forms to next cycle to preserve graph.",
                    path,
                )
                apply_ok = False
                continue
            changed_added_results.append(result)

        # Если parse не прошёл для ВСЕХ changed/added — оставляем existing graph
        # нетронутым, manifest не коммитим. Deleted обрабатываются отдельно ниже.
        if (diff.added or diff.changed) and not apply_ok:
            if deleted_form_qns:
                self.loader.delete_form_xcf_subtree(
                    project_name, config_name, sorted(set(deleted_form_qns))
                )
                self.loader.clear_form_content_hash(
                    project_name, config_name, sorted(set(deleted_form_qns))
                )
                _record_form_impact(context, config_name, deleted_form_qns, is_extension)
            if diff.deleted:
                self.state.delete_artifact_manifest(source_scope, diff.deleted)
            return

        # All parses ok → теперь безопасно делать destructive cleanup + reload.
        affected_form_qns_unique = sorted(set(changed_added_form_qns + deleted_form_qns))
        if affected_form_qns_unique:
            self.loader.delete_form_xcf_subtree(project_name, config_name, affected_form_qns_unique)
            if deleted_form_qns:
                self.loader.clear_form_content_hash(
                    project_name, config_name, sorted(set(deleted_form_qns))
                )
            _record_form_impact(context, config_name, affected_form_qns_unique, is_extension)

        # Extract form commands from parsed results → impact.commands_by_config.
        for result in changed_added_results:
            for cmd in (result.get("form_commands") or []):
                cmd_qn = cmd.get("cmd_qn") if isinstance(cmd, dict) else None
                if cmd_qn:
                    context.post_linking_impact.add_command(config_name, cmd_qn)

        # Merge parsed results в FormsData + собрать hash rows.
        for result in changed_added_results:
            fp.merge_form_result(forms_data, result)
            form_qn = result.get("form_qn")
            if form_qn:
                form_hash_rows.append({
                    "form_qn": form_qn,
                    "form_content_hash": result.get("form_content_hash"),
                    "base_form_hash": result.get("base_form_hash"),
                })

        if fp.has_data(forms_data) and apply_ok:
            rows = forms_data.to_dict()
            try:
                self.loader.load_form_definitions(rows)
            except Exception:
                logger.exception("Phase artifact form_xml load_form_definitions failed")
                apply_ok = False

        if apply_ok and form_hash_rows:
            try:
                self.loader.update_form_hashes(form_hash_rows)
            except Exception:
                logger.exception("Phase artifact form_xml update_form_hashes failed")
                apply_ok = False

        # Manifest для deleted снимаем безусловно (cleanup завершён). Для added/changed
        # коммитим только при успешном graph apply — иначе следующий цикл должен
        # повторить попытку загрузки (R8.1).
        if apply_ok:
            _persist_manifest_after_apply(self.state, source_scope, root, diff)
        elif diff.deleted:
            self.state.delete_artifact_manifest(source_scope, diff.deleted)

    def _apply_form_bin(
        self,
        *,
        project_name: str,
        config_name: str,
        source_scope: str,
        root: Path,
        diff: ArtifactDiff,
        context: CodeArtifactCycleContext,
    ) -> None:
        """Form.bin содержит BSL модуль формы.

        Для added/changed Form.bin destructive form-level delete `delete_form_bin_routines`
        **не используем**: routine-level diff в `_apply_bsl` (вызывается выше с
        `extra_files_to_parse=Form.bin paths`) сам сделает scoped Routine update —
        embeddings неизменных routines формы сохраняются.

        Для **deleted** Form.bin (form removed целиком) оставляем form-level delete
        по `form_qn`: для deleted у нас может не быть old artifact в sidecar
        (rollback safety), и form-wide cleanup надёжнее.
        """
        if not (diff.added or diff.changed or diff.deleted):
            return

        affected_form_qns: List[str] = []
        # Все form_qn → post_linking_impact (для phase 4 link_form_events_and_commands).
        is_extension = config_name != context.base_config_name
        for rel_path in (
            [str(p.relative_to(root)).replace("\\", "/") for p in (diff.changed + diff.added)]
            + list(diff.deleted)
        ):
            fqn = _form_qn_from_rel_path(rel_path, project_name, config_name)
            if fqn:
                affected_form_qns.append(fqn)
        if affected_form_qns:
            _record_form_impact(context, config_name, affected_form_qns, is_extension)

        # Destructive form-level delete ТОЛЬКО для deleted Form.bin.
        if diff.deleted:
            deleted_form_qns: List[str] = []
            for rel_path in diff.deleted:
                fqn = _form_qn_from_rel_path(rel_path, project_name, config_name)
                if fqn:
                    deleted_form_qns.append(fqn)
            if deleted_form_qns:
                try:
                    self.loader.delete_form_bin_routines(
                        project_name, config_name, sorted(set(deleted_form_qns))
                    )
                except Exception:
                    logger.exception("Phase artifact form_bin deleted cleanup failed")
                    return

        # Manifest commit для Form.bin делается ТОЛЬКО для deleted (cleanup завершён).
        # added/changed Form.bin будут закоммичены позже через `_apply_bsl`, после того
        # как BSLProcessor успешно перепарсил их (см. R5.2 — иначе manifest опередит graph).
        if diff.deleted:
            # `bsl_file_artifacts` для удалённого Form.bin тоже снять: scoped CALLS
            # corpus не должен содержать routines несуществующего Form.bin (R6.2).
            # BSL scope для base/extension вычисляется из формата source_scope.
            bsl_scope = _bsl_scope_for_form_bin_scope(source_scope)
            bsl_keys: List[str] = []
            if bsl_scope:
                key_fn = _bsl_key_fn(context.data_directory, root)
                # `diff.deleted` уже строится `_diff_scope` под root-relative ключ
                # Form.bin; для bsl_file_artifacts нужен BSL key. Восстанавливаем
                # путь файла из rel + root и применяем `_bsl_key_fn`.
                for rel in diff.deleted:
                    bsl_keys.append(key_fn(root / rel))
            with self.state.transaction():
                self.state.delete_artifact_manifest(source_scope, diff.deleted)
                if bsl_scope and bsl_keys:
                    self.state.delete_bsl_file_artifacts(bsl_scope, bsl_keys)

    def _apply_predefined(
        self,
        *,
        project_name: str,
        config_name: str,
        source_scope: str,
        root: Path,
        diff: ArtifactDiff,
    ) -> Set[str]:
        """Return affected_owner_qns для последующего ADOPTED_FROM rebuild."""
        if not (diff.added or diff.changed or diff.deleted):
            return set()

        affected_owner_qns: Set[str] = set()
        for path in list(diff.added) + list(diff.changed):
            try:
                rel = str(path.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
            owner = _owner_object_qn_from_path(rel, project_name, config_name)
            if owner:
                affected_owner_qns.add(owner)
        for rel_path in diff.deleted:
            owner = _owner_object_qn_from_path(rel_path, project_name, config_name)
            if owner:
                affected_owner_qns.add(owner)

        if affected_owner_qns:
            self.loader.delete_predefined_for_owner_qns(
                project_name, config_name, sorted(affected_owner_qns)
            )

        # Re-load changed/added: parser API такая же, как в full-load streaming scan —
        # вызываем `worker_predefined(path, parser)` per-file, аккумулируем `items` и
        # `relations`. Manifest коммитим только при успешном apply.
        apply_ok = True
        if diff.added or diff.changed:
            from indexer.workers import worker_predefined
            from parsers.predefined_parser import PredefinedParser

            pre_parser = PredefinedParser()
            items_rows: List[Dict[str, Any]] = []
            rel_rows: List[Dict[str, Any]] = []
            for path in diff.added + diff.changed:
                try:
                    result = worker_predefined(path, pre_parser)
                except Exception:
                    logger.exception("Phase artifact predefined worker failed for %s", path)
                    apply_ok = False
                    continue
                if not result:
                    continue
                items_rows.extend(result.get("items") or [])
                rel_rows.extend(result.get("relations") or [])

            if (items_rows or rel_rows) and apply_ok:
                try:
                    self.loader.load_predefined(items_rows, rel_rows, project_name, config_name)
                except Exception:
                    logger.exception("Phase artifact predefined load_predefined failed")
                    apply_ok = False

        if apply_ok:
            _persist_manifest_after_apply(self.state, source_scope, root, diff)
            return affected_owner_qns
        return set()

    def _apply_property_analysis(
        self,
        *,
        project_name: str,
        ext_config_qn: str,
        source_scope: str,
        root: Path,
        diff: ArtifactDiff,
        xml_files: List[Path],
        settings_obj: Any,
    ) -> None:
        """Extension XML property analysis: classification + extracted values.

        Flow:
        1. Analyze added+changed XML.
        2. Derive sidecar rows из analyzer results (label+qn через
           ExtensionsLoader._save logic).
        3. Diff prev sidecar (для changed+deleted rel_paths) vs new sidecar →
           rows для cleanup (classification: clear_extension_property_classification,
           values: clear_extension_property_values guarded по old value).
        4. Cleanup в Neo4j.
        5. Save new analyzer results в Neo4j через ExtensionsLoader._save_extension_analysis_results
           (тот же путь, что full load — byte-by-byte parity).
        6. Replace sidecar rows для affected rel_paths, commit artifact manifest.
        """
        if not (diff.added or diff.changed or diff.deleted):
            return

        from indexer.extension_property_analysis import (
            analyze_xml_files,
            derive_element_qn_and_label,
        )
        from indexer.extensions_loader import ExtensionsLoader

        # rel_path для каждого added/changed file.
        rel_paths_by_file: Dict[Path, str] = {}
        for p in diff.added + diff.changed:
            try:
                rel_paths_by_file[p] = str(p.relative_to(root)).replace("\\", "/")
            except ValueError:
                pass

        # 1. Analyze added+changed
        cls_results: List[Any] = []
        ext_results: List[Any] = []
        if diff.added or diff.changed:
            try:
                cls_results, ext_results = analyze_xml_files(
                    diff.added + diff.changed
                )
            except Exception:
                logger.exception(
                    "Property analysis analyze failed for scope=%s", source_scope
                )

        # 2. Build new sidecar rows.
        new_rows: List[Dict[str, Any]] = []
        # Identity (label, qn, kind, prop_key) → row для быстрого diff.
        new_identities: set = set()

        for xml_file, obj_result in cls_results:
            rel = rel_paths_by_file.get(xml_file)
            if not rel:
                continue
            for element in getattr(obj_result, "elements", []) or []:
                if not getattr(element, "is_adopted", False):
                    continue
                label, qn = derive_element_qn_and_label(obj_result, element, ext_config_qn)
                if not label or not qn:
                    continue
                payload = json.dumps({
                    "controlled": list(getattr(element, "controlled_properties", []) or []),
                    "modified": list(getattr(element, "modified_properties", []) or []),
                }, ensure_ascii=False, sort_keys=True)
                new_rows.append({
                    "source_scope": source_scope,
                    "rel_path": rel,
                    "label": label,
                    "qualified_name": qn,
                    "output_kind": "classification",
                    "property_key": "__classification__",
                    "payload_json": payload,
                })
                new_identities.add((rel, label, qn, "classification", "__classification__"))

        for xml_file, obj_result in ext_results:
            rel = rel_paths_by_file.get(xml_file)
            if not rel:
                continue
            for element in getattr(obj_result, "elements", []) or []:
                prop_values = getattr(element, "property_values", None)
                if not prop_values:
                    continue
                label, qn = derive_element_qn_and_label(obj_result, element, ext_config_qn)
                if not label or not qn:
                    continue
                for key, value in prop_values.items():
                    payload = json.dumps({"value": value}, ensure_ascii=False, sort_keys=True)
                    new_rows.append({
                        "source_scope": source_scope,
                        "rel_path": rel,
                        "label": label,
                        "qualified_name": qn,
                        "output_kind": "property_value",
                        "property_key": key,
                        "payload_json": payload,
                    })
                    new_identities.add((rel, label, qn, "property_value", key))

        # 3. Read prev sidecar для changed+deleted и вычислить removed identities.
        affected_rel_paths: List[str] = []
        for rel in diff.deleted:
            affected_rel_paths.append(rel)
        for p in diff.changed:
            rp = rel_paths_by_file.get(p)
            if rp:
                affected_rel_paths.append(rp)

        cls_clear: List[Dict[str, Any]] = []
        val_clear: List[Dict[str, Any]] = []
        if affected_rel_paths:
            prev_rows = self.state.get_ext_analyzer_outputs_for_files(
                source_scope, affected_rel_paths
            )
            for r in prev_rows:
                identity = (
                    r["rel_path"], r["label"], r["qualified_name"],
                    r["output_kind"], r["property_key"],
                )
                if identity in new_identities:
                    continue  # node всё ещё содержит этот output → не чистим
                if r["output_kind"] == "classification":
                    cls_clear.append({
                        "label": r["label"],
                        "qualified_name": r["qualified_name"],
                    })
                elif r["output_kind"] == "property_value":
                    try:
                        payload = json.loads(r["payload_json"])
                    except Exception:
                        payload = {}
                    val_clear.append({
                        "label": r["label"],
                        "qualified_name": r["qualified_name"],
                        "property_key": r["property_key"],
                        "expected_value": payload.get("value"),
                    })

        # 4. Cleanup + 5. Save в одной Neo4j session.
        # Используем settings_obj.neo4j_database (передан caller-ом) — в этом модуле
        # нет импортированного `settings`.
        neo4j_ok = True
        try:
            with self.loader.driver.session(
                database=getattr(settings_obj, "neo4j_database", "neo4j")
            ) as session:
                if cls_clear:
                    self.loader.clear_extension_property_classification(
                        session, project_name, cls_clear
                    )
                if val_clear:
                    self.loader.clear_extension_property_values(
                        session, project_name, val_clear
                    )
                if cls_results or ext_results:
                    ext_loader_helper = ExtensionsLoader(self.loader, settings_obj)
                    ext_loader_helper._save_extension_analysis_results(
                        session, project_name, ext_config_qn,
                        cls_results, ext_results,
                    )
        except Exception:
            logger.exception(
                "Property analysis Neo4j operations failed for scope=%s", source_scope
            )
            neo4j_ok = False

        # 6. Sidecar update + manifest в одной SQLite transaction.
        # Если Neo4j apply провалился — НЕ обновляем sidecar и manifest, чтобы
        # следующий цикл попытался ещё раз (иначе зафиксируем файл как обработанный
        # без graph apply, и stale данные останутся до full reload).
        if not neo4j_ok:
            logger.warning(
                "Property analysis: skipping sidecar/manifest commit because "
                "Neo4j apply failed (scope=%s)", source_scope
            )
            return
        try:
            with self.state.transaction():
                if affected_rel_paths:
                    self.state.delete_ext_analyzer_outputs_for_files(
                        source_scope, affected_rel_paths
                    )
                if new_rows:
                    self.state.upsert_ext_analyzer_outputs_many(new_rows)
                _persist_manifest_after_apply(
                    self.state, source_scope, root, diff, transactional=False
                )
        except Exception:
            logger.exception(
                "Property analysis sidecar commit failed for scope=%s", source_scope
            )

        logger.info(
            "Ext property analysis: ext_scope=%s added=%d changed=%d deleted=%d "
            "sidecar_new=%d cleared_cls=%d cleared_vals=%d",
            source_scope,
            len(diff.added), len(diff.changed), len(diff.deleted),
            len(new_rows), len(cls_clear), len(val_clear),
        )

    def _rebuild_predefineditem_adopted_from_for_ext_owners(
        self,
        *,
        project_name: str,
        ext_config_name: str,
        base_config_name: Optional[str],
        owner_qns: Set[str],
    ) -> None:
        """Scoped rebuild PredefinedItem ADOPTED_FROM в extension под affected owners."""
        if not owner_qns or not base_config_name:
            return
        try:
            from graphdb.extension_relationships_builder import (
                ExtensionRelationshipsBuilder,
            )

            rel_builder = ExtensionRelationshipsBuilder(self.loader)
            created = rel_builder.build_predefineditem_adopted_from_for_owner_qns(
                ext_config_name,
                base_config_name,
                sorted(owner_qns),
            )
            logger.info(
                "PredefinedItem ADOPTED_FROM rebuild: ext=%s owners=%d created=%d",
                ext_config_name, len(owner_qns), created,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "PredefinedItem ADOPTED_FROM scoped rebuild failed for ext=%s",
                ext_config_name,
            )

    def _rebuild_predefineditem_adopted_from_for_base_owners(
        self,
        *,
        project_name: str,
        base_config_name: str,
        base_owner_qns: Set[str],
        source_mode: str,
    ) -> None:
        """При изменении base Predefined.xml пересобрать ADOPTED_FROM scoped для каждого
        известного extension scope. Источник — state.list_extension_scopes + ext_config_qn."""
        if not base_owner_qns:
            return
        try:
            from graphdb.extension_relationships_builder import (
                ExtensionRelationshipsBuilder,
            )

            rel_builder = ExtensionRelationshipsBuilder(self.loader)
            ext_scopes = self.state.list_extension_scopes(source_mode)
            for scope in sorted(ext_scopes):
                ext_cfg_qn = self.state.get_extension_scope_config_qn(scope)
                if not ext_cfg_qn:
                    continue
                ext_config_name = ext_cfg_qn.split("/")[-1]
                # Проекция base owner_qn -> ext owner_qn
                projected: List[str] = []
                base_token = f"/{base_config_name}/"
                ext_token = f"/{ext_config_name}/"
                for owner_qn in base_owner_qns:
                    projected.append(owner_qn.replace(base_token, ext_token, 1))
                created = rel_builder.build_predefineditem_adopted_from_for_owner_qns(
                    ext_config_name,
                    base_config_name,
                    projected,
                )
                if created:
                    logger.info(
                        "PredefinedItem ADOPTED_FROM rebuild (base->ext): ext=%s "
                        "owners=%d created=%d",
                        ext_config_name, len(projected), created,
                    )
        except Exception:  # noqa: BLE001
            logger.exception(
                "PredefinedItem ADOPTED_FROM base->ext rebuild failed for base=%s",
                base_config_name,
            )

    def _apply_help(
        self,
        *,
        project_name: str,
        config_name: str,
        source_scope: str,
        root: Path,
        diff: ArtifactDiff,
        context: CodeArtifactCycleContext,
    ) -> None:
        if not (diff.added or diff.changed or diff.deleted):
            return

        affected_obj_qns: Set[str] = set()
        for path in list(diff.added) + list(diff.changed):
            try:
                rel = str(path.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
            obj = _owner_object_qn_from_path(rel, project_name, config_name)
            if obj:
                affected_obj_qns.add(obj)
        for rel_path in diff.deleted:
            obj = _owner_object_qn_from_path(rel_path, project_name, config_name)
            if obj:
                affected_obj_qns.add(obj)

        # Parse-first: парсим все added/changed ДО любых destructive операций.
        # `worker_help` возвращает None и при exception, и при пустом help_text;
        # `HelpProcessor.process_file` → ("", "", "") в обоих случаях.
        # Семантика: только реальное исключение в `process_file` — failure;
        # пустой кортеж — валидный сценарий "Help без извлекаемого текста",
        # старая `Справка` всё равно очищается.
        apply_ok = True
        help_by_object: Dict[Any, str] = {}
        if diff.added or diff.changed:
            from indexer.help_processor import HelpProcessor

            hp = HelpProcessor()
            for path in diff.added + diff.changed:
                try:
                    category_folder, object_name, html = hp.process_file(path)
                except Exception:
                    logger.exception("Phase artifact help worker failed for %s", path)
                    apply_ok = False
                    continue
                if category_folder and object_name and html:
                    help_by_object[(category_folder, object_name)] = html

        if apply_ok and affected_obj_qns:
            self.loader.clear_help_content(
                project_name, config_name, sorted(affected_obj_qns)
            )
            if help_by_object:
                try:
                    self.loader.load_help_content(
                        help_by_object, project_name, config_name
                    )
                except Exception:
                    logger.exception("Phase artifact help load_help_content failed")
                    apply_ok = False

            if apply_ok:
                self.loader.invalidate_metadata_description_embedding(
                    sorted(affected_obj_qns)
                )
                context.metadata_embedding_repass_qns.update(affected_obj_qns)

        if apply_ok:
            _persist_manifest_after_apply(self.state, source_scope, root, diff)

    def _apply_event_subscriptions(
        self,
        *,
        project_name: str,
        config_name: str,
        source_scope: str,
        root: Path,
        diff: ArtifactDiff,
        context: CodeArtifactCycleContext,
    ) -> None:
        if not (diff.added or diff.changed or diff.deleted):
            return

        affected_sub_qns: Set[str] = set()
        for path in list(diff.added) + list(diff.changed):
            try:
                rel = str(path.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
            qn = _subscription_qn_from_path(rel, project_name, config_name)
            if qn:
                affected_sub_qns.add(qn)
        for rel_path in diff.deleted:
            qn = _subscription_qn_from_path(rel_path, project_name, config_name)
            if qn:
                affected_sub_qns.add(qn)

        # Parse-first: распарсить все changed/added до destructive cleanup. Если
        # хоть один parse провалится — оставить existing graph нетронутым.
        subs: List[Any] = []
        parse_ok = True
        if diff.added or diff.changed:
            from parsers.event_subscription_parser import EventSubscriptionParser

            ev_parser = EventSubscriptionParser()
            for path in list(diff.added) + list(diff.changed):
                try:
                    sub = ev_parser.parse_file(path)
                    if sub is not None:
                        subs.append(sub)
                except Exception:
                    logger.exception("event subscription parse failed: %s", path)
                    parse_ok = False

        if not parse_ok:
            logger.warning(
                "Phase artifact event_subscriptions [%s]: parse failed; "
                "skipping cleanup and reload to preserve existing graph",
                source_scope,
            )
            return

        # Atomic apply: cleanup + node upsert + source-link reload + scoped
        # handler relink — одной write transaction через `replace_event_subscriptions`.
        # Только для added/changed; для pure-deleted (subs пустой) делаем cleanup без reload.
        if subs:
            try:
                self.loader.replace_event_subscriptions(
                    project_name, config_name, sorted(affected_sub_qns), subs,
                )
            except Exception:
                logger.exception(
                    "Phase artifact event_subscriptions [%s]: replace failed; "
                    "manifest not committed", source_scope,
                )
                # Cleanup для pure-deleted всё равно безопасен; обработаем ниже.
                if diff.deleted:
                    deleted_qns = [
                        _subscription_qn_from_path(rel_path, project_name, config_name)
                        for rel_path in diff.deleted
                    ]
                    deleted_qns = [q for q in deleted_qns if q]
                    if deleted_qns:
                        try:
                            self.loader.delete_event_subscription_links(
                                project_name, config_name, sorted(deleted_qns),
                            )
                            self.state.delete_artifact_manifest(source_scope, diff.deleted)
                        except Exception:
                            logger.exception(
                                "Phase artifact event_subscriptions [%s]: deleted cleanup failed",
                                source_scope,
                            )
                return
        elif diff.deleted:
            # Только удаления — cleanup без reload.
            deleted_qns = [
                _subscription_qn_from_path(rel_path, project_name, config_name)
                for rel_path in diff.deleted
            ]
            deleted_qns = [q for q in deleted_qns if q]
            if deleted_qns:
                try:
                    self.loader.delete_event_subscription_links(
                        project_name, config_name, sorted(deleted_qns),
                    )
                except Exception:
                    logger.exception(
                        "Phase artifact event_subscriptions [%s]: deleted cleanup failed",
                        source_scope,
                    )
                    return

        # Накопить impact + mark_handler_relink. Same-cycle BSL Routine
        # introduction для нового Обработчик-а: BSL apply следует ПОСЛЕ event
        # subs apply. mark_handler_relink гарантирует config-level
        # `link_event_subscriptions_to_handlers` в PostLinking phase 4 после
        # BSL apply — это correctness.
        for qn in affected_sub_qns:
            context.post_linking_impact.add_event_subscription(config_name, qn)
        if affected_sub_qns:
            context.post_linking_impact.mark_handler_relink(config_name)

        _persist_manifest_after_apply(self.state, source_scope, root, diff)

    def _apply_rights_base(
        self,
        *,
        project_name: str,
        config_name: str,
        source_scope: str,
        root: Path,
        diff: ArtifactDiff,
    ) -> None:
        if not (diff.added or diff.changed or diff.deleted):
            return

        affected_role_qns: Set[str] = set()
        for path in list(diff.added) + list(diff.changed):
            try:
                rel = str(path.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
            qn = _role_qn_from_rights_path(rel, project_name, config_name)
            if qn:
                affected_role_qns.add(qn)
        for rel_path in diff.deleted:
            qn = _role_qn_from_rights_path(rel_path, project_name, config_name)
            if qn:
                affected_role_qns.add(qn)

        if affected_role_qns:
            self.loader.delete_role_rights_for_roles(
                project_name, config_name, sorted(affected_role_qns)
            )

        apply_ok = True
        if diff.added or diff.changed:
            from indexer.role_rights_processor import RoleRightsProcessor

            rrp = RoleRightsProcessor()
            try:
                rows = rrp.process_role_rights(
                    root, project_name, config_name,
                    rights_xml_files=list(diff.added) + list(diff.changed),
                )
                if rows:
                    self.loader.load_role_rights_targets(rows)
            except Exception:
                logger.exception("Phase artifact rights (base) load failed")
                apply_ok = False

        if apply_ok:
            _persist_manifest_after_apply(self.state, source_scope, root, diff)

    def _apply_rights_extension(
        self,
        *,
        project_name: str,
        ext_config_name: str,
        base_config_name: str,
        source_scope: str,
        root: Path,
        diff: ArtifactDiff,
    ) -> None:
        if not (diff.added or diff.changed or diff.deleted):
            return

        affected_role_qns: Set[str] = set()
        for path in list(diff.added) + list(diff.changed):
            try:
                rel = str(path.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
            qn = _role_qn_from_rights_path(rel, project_name, ext_config_name)
            if qn:
                affected_role_qns.add(qn)
        for rel_path in diff.deleted:
            qn = _role_qn_from_rights_path(rel_path, project_name, ext_config_name)
            if qn:
                affected_role_qns.add(qn)

        if affected_role_qns:
            self.loader.delete_role_rights_for_roles(
                project_name, ext_config_name, sorted(affected_role_qns)
            )

        apply_ok = True
        if diff.added or diff.changed:
            from indexer.role_rights_processor import RoleRightsProcessor

            rrp = RoleRightsProcessor()
            try:
                rows = rrp.process_role_rights(
                    root, project_name, ext_config_name,
                    rights_xml_files=list(diff.added) + list(diff.changed),
                )
                if rows:
                    # Extension scope: dual-config target resolution.
                    self.loader.load_role_rights_targets_ext(
                        rows, ext_config_name, base_config_name
                    )
            except Exception:
                logger.exception("Phase artifact rights (ext) load failed")
                apply_ok = False

        if apply_ok:
            _persist_manifest_after_apply(self.state, source_scope, root, diff)

    def _apply_bsl(
        self,
        *,
        project_name: str,
        config_name: str,
        source_scope: str,
        root: Path,
        diff: ArtifactDiff,
        context: CodeArtifactCycleContext,
        settings_obj: Any,
        lease: Optional[LockLease],
        extra_files_to_parse: Optional[List[Path]] = None,
        extras_manifest_scope: Optional[str] = None,
    ) -> None:
        """Routine-level BSL apply orchestrator.

        Шаги:
          1. Load old artifacts из `bsl_file_artifacts` (sidecar) для changed/deleted.
          2. Parse-only через `parse_bsl_files_parallel` для added/changed (+ extras).
          3. `build_delta(old, parsed, deleted)` → `BslApplyDelta`.
          4. Pre-cleanup class (a) callers — один Cypher round-trip по
             `delta.calls_class_a_old_targets` ДО любых cleanup write'ов.
          5. Graph apply:
             - delete_bsl_routines_by_ids / delete_bsl_modules_by_ids;
             - load_bsl_signatures(modules, routines, declares, common_declares).
          6. Embedding invalidation:
             - clear_routine_doc_embeddings(doc_embeddings_to_clear);
             - applier.invalidate_routines(code_embeddings_to_clear) — phase 5 hook.
          7. CALLS feeding + form handler feeding в `context`.
          8. Persist sidecar (через batch) + manifest (через batch).
          9. Сохранить `delta.code_search_delta` в `context.code_search_delta`
             для phase 5 `BslCodeSearchSync`.

        Для **deleted file без old artifact** делаем fallback на
        `delete_bsl_by_file_paths` (без artifact'а у нас нет routine_ids).

        `extra_files_to_parse` (Form.bin add/changed из соседнего scope) идут в
        тот же parse + delta + apply path. `extras_manifest_scope` — куда писать
        manifest этих extras.
        """
        extras = list(extra_files_to_parse or [])
        # ---- 0. Merge previously deferred BSL changes for this scope -----
        # When a previous cycle's inline drain of a pending scoped code-search
        # ledger could not complete, the diff (especially `deleted` rediscovered
        # only during full reconcile) is parked in `bsl_deferred_changes`.
        # Reading does NOT clear — that happens only after successful BSL apply
        # in step 8 (manifest persist), so a crash here leaves the deferred
        # row in place for the next cycle. Graph operations downstream are
        # already idempotent (`load_bsl_signatures` uses MERGE, deletes use
        # MATCH ... DETACH DELETE).
        try:
            _deferred = self.state.read_deferred_bsl_diff(project_name, source_scope)
        except Exception:
            logger.exception("read_deferred_bsl_diff failed")
            _deferred = None
        if _deferred is not None:
            diff = ArtifactDiff(
                added=list(diff.added) + [Path(p) for p in (_deferred.get("added") or [])],
                changed=list(diff.changed) + [Path(p) for p in (_deferred.get("changed") or [])],
                unchanged=list(diff.unchanged),
                deleted=list(diff.deleted) + list(_deferred.get("deleted") or []),
                file_stats=dict(diff.file_stats),
            )

        if not (diff.added or diff.changed or diff.deleted or extras):
            return

        # ---- 0.5 Pre-flight: drain pending scoped code-search ledger inline
        # before doing any graph apply. If we let `load_bsl_signatures` run
        # while the ledger has a `snapshot_written` row for the same routine,
        # the next scoped retry would reverse against a stale snapshot.
        try:
            applier_for_drain = getattr(context, "bsl_code_search_delta_applier", None)
            sqlite_for_drain = getattr(context, "bsl_code_search_sqlite", None)
            if applier_for_drain is not None and sqlite_for_drain is not None:
                from graphdb.bsl_code_search_delta import (
                    ApplyResult as _BslApplyResult,
                    CodeSearchDelta as _BslCodeSearchDelta,
                    DeltaReadiness as _BslDeltaReadiness,
                )
                drain_scope = getattr(context, "bsl_code_search_scope", project_name)
                readiness = sqlite_for_drain.classify_delta_readiness(drain_scope)
                if readiness == _BslDeltaReadiness.SCOPED_RETRY:
                    logger.info(
                        "BSL apply: draining pending scoped code-search ledger inline"
                    )
                    result = applier_for_drain.apply(
                        drain_scope,
                        _BslCodeSearchDelta.empty_placeholder(),
                        lease=lease,
                    )
                    if result != _BslApplyResult.APPLIED:
                        # Cannot drain — preserve `diff` so the next cycle has
                        # full work list (deletions from full-reconcile would
                        # otherwise be lost).
                        try:
                            self.state.defer_bsl_changes_for_next_cycle(
                                project_name, source_scope, diff,
                            )
                        except Exception:
                            logger.exception("defer_bsl_changes_for_next_cycle failed")
                        return
        except Exception:
            logger.exception("BSL apply: pre-flight drain failed")
            # On unknown failure, fall through to normal apply path — the
            # snapshot+ledger writer below has its own fail-fast guard.

        from .bsl_parse_only import parse_bsl_files_parallel
        from .bsl_routine_delta import build_delta

        # POSIX-rel ключи (как у `Routine.file_path`).
        key_fn = _bsl_key_fn(context.data_directory, root)
        root_key_fn = _default_key_fn(root)
        extras_paths_set = {p for p in extras if p}

        def _key_for(path: Path) -> Optional[str]:
            try:
                return key_fn(path)
            except ValueError:
                return None

        # ---- 1. Load old artifacts (для changed + deleted + extras) ----
        changed_keys: List[str] = []
        for path in diff.changed:
            k = _key_for(path)
            if k is not None:
                changed_keys.append(k)
        deleted_keys = list(diff.deleted)
        # extras (например Form.bin add/changed) идут через тот же BSL parse-only
        # path. Чтобы routine-level diff сохранил embeddings неизменных routines
        # формы (см. R1 #3), нам нужно их old artifact lookup. Form.bin routines
        # лежат в `bsl_file_artifacts` под тем же source_scope (ART_BASE_BSL),
        # потому что full reload baseline ([app/main.py:_init_artifact_baseline])
        # пишет их именно туда; ключ — тот же `_bsl_key_fn` POSIX-rel.
        extras_keys: List[str] = []
        for path in extras:
            k = _key_for(path)
            if k is not None:
                extras_keys.append(k)
        old_artifacts: Dict[str, Dict[str, Any]] = {}
        for key in changed_keys + deleted_keys + extras_keys:
            art = self.state.get_bsl_file_artifact(source_scope, key)
            if art is not None:
                old_artifacts[key] = art

        # ---- 2. Parse-only added/changed (+ extras) ----
        # extras (Form.bin) идут через тот же parser; в worker'е есть Form.bin ветка.
        files_to_parse: List[Path] = list(diff.added) + list(diff.changed) + extras
        parsed = parse_bsl_files_parallel(
            files_to_parse,
            code_root=root,
            project_name=project_name,
            cfg_name=config_name,
            lease=lease,
        ) if files_to_parse else []

        # ---- 3. build_delta ----
        delta = build_delta(
            old_artifacts=old_artifacts,
            parsed=parsed,
            deleted_paths=deleted_keys,
        )

        # ---- 4.5 Capture OLD routine records and persist scoped-delta ledger.
        # This MUST happen before `load_bsl_signatures` overwrites the Neo4j
        # body, because the OLD routine context is the only source for the
        # reverse IDF/stats counters used by scoped code-search apply.
        try:
            self._bsl_write_scoped_snapshot_and_ledger(
                context=context,
                project_name=project_name,
                delta_cs=delta.code_search_delta,
                parsed=parsed,
                settings_obj=settings_obj,
            )
        except _BslCodeSearchSnapshotFailed:
            # Hard-stop: the scoped snapshot is the sole source for reverse
            # counters of the OLD state. If we cannot persist it, going ahead
            # with `load_bsl_signatures` would erase the OLD body in Neo4j and
            # leave the next scoped retry without anything to subtract. The
            # sidecar `bsl_file_artifacts` is not yet rewritten (step 8 below),
            # so the next cycle will rebuild the same `code_search_delta`.
            logger.warning(
                "BSL apply: aborted before graph apply because scoped "
                "snapshot+ledger could not be persisted"
            )
            return

        # ---- 4. Pre-cleanup class (a) callers (по delta) ----
        if delta.calls_class_a_old_targets:
            try:
                with self.loader.driver.session(
                    database=getattr(__import__("config").settings, "neo4j_database", "neo4j")
                ) as session:
                    res = session.run(
                        """
                        UNWIND $ids AS rid
                        MATCH (callee:Routine {id: rid})
                        WHERE callee.project_name = $project_name
                        MATCH (caller:Routine)-[:CALLS]->(callee)
                        RETURN DISTINCT caller.id AS cid
                        """,
                        ids=list(delta.calls_class_a_old_targets),
                        project_name=project_name,
                    )
                    for rec in res:
                        cid = rec.get("cid")
                        if cid:
                            context.calls_class_a_callers.add(cid)
            except Exception:
                logger.exception("Pre-cleanup class (a) callers lookup failed")

        # ---- 5. Graph apply ----
        bsl_apply_ok = True
        # 5.a Routine/Module scoped deletes (по rid, не по file_path → embeddings
        # соседних routines сохраняются).
        try:
            if delta.routine_ids_to_delete:
                self.loader.delete_bsl_routines_by_ids(
                    project_name, config_name, delta.routine_ids_to_delete
                )
            if delta.module_ids_to_delete:
                self.loader.delete_bsl_modules_by_ids(
                    project_name, config_name, delta.module_ids_to_delete
                )
        except Exception:
            logger.exception("routine-level cleanup failed")
            bsl_apply_ok = False

        # 5.b Fallback для deleted files без sidecar baseline: file-path-based
        # delete. Это редкий случай (rollout без full reload, частичный seed),
        # но без него deleted-file scenario не очищает граф.
        legacy_deleted_keys = [k for k in deleted_keys if k not in old_artifacts]
        if legacy_deleted_keys:
            try:
                self.loader.delete_bsl_by_file_paths(
                    project_name, config_name, legacy_deleted_keys
                )
            except Exception:
                logger.exception("legacy delete_bsl_by_file_paths failed")
                bsl_apply_ok = False

        # 5.c MERGE routines/modules + DECLARES через существующий load_bsl_signatures.
        if delta.routines_to_upsert or delta.modules_to_upsert:
            try:
                self.loader.load_bsl_signatures(
                    project_name,
                    config_name,
                    delta.modules_to_upsert,
                    delta.routines_to_upsert,
                    delta.declares_to_upsert,
                    delta.common_declares_to_upsert,
                    form_routines=None,
                    do_linking=False,
                )
            except Exception:
                logger.exception("load_bsl_signatures failed")
                bsl_apply_ok = False

        # ---- 6. Embedding invalidation ----
        if bsl_apply_ok and delta.doc_embeddings_to_clear:
            try:
                self.loader.clear_routine_doc_embeddings(
                    project_name, delta.doc_embeddings_to_clear
                )
                # Cleared successfully: routines need doc embedding re-pass.
                context.routine_doc_embedding_repass_ids.update(delta.doc_embeddings_to_clear)
            except Exception:
                logger.exception("clear_routine_doc_embeddings failed")
                # Do NOT add to repass — embedding was not cleared.

        if bsl_apply_ok and delta.routine_doc_repass_ids:
            # Added routines with non-empty doc_description need re-pass after upsert.
            context.routine_doc_embedding_repass_ids.update(delta.routine_doc_repass_ids)
        # Code embedding invalidation owned by BSL code search subsystem.
        # `applier.invalidate_routines` — часть idempotent saga внутри
        # `applier.apply()` (см. bsl_code_search_delta.py). НЕ вызываем здесь
        # отдельно: pre-invalidate выставлял бы `reindex_requested=1` ДО phase 5,
        # и `BslCodeSearchSync.run` всегда уходил бы в `REINDEX_REQUIRED` ветку
        # вместо scoped `applier.apply()`. Code-search delta передаётся в
        # `context.code_search_delta` (шаг 9), phase 5 сам решает saga vs full
        # rebuild по `classify_delta_readiness`.

        # ---- 7. CALLS feeding + form handler feeding ----
        if bsl_apply_ok:
            for rid in delta.calls_affected_callers:
                context.affected_routines.add(rid)
            for rid in delta.routine_ids_to_delete:
                # deleted routines тоже могут участвовать в class (a)/(c) — но
                # они уже отражены в calls_class_a_old_targets; их в
                # affected_routines не пихаем (их нет в графе).
                pass
            context.new_routine_targets |= delta.new_routine_targets

            # Form handler feeding: ParsedBslFile.form_links → per-config slot.
            # Формат: form_qn → List[{"name": rn}] (как у `_create_form_event_links`
            # / `_create_command_links`, см. [bsl_loader.py:294-298, 385-389]).
            # Per-config bucket нужен чтобы PostLinkingSync.run не смешивал base
            # и extension routine names при scoped linking.
            slot = _form_routines_slot(context, config_name)
            for pbf in parsed:
                for link in pbf.form_links or []:
                    fq = link.get("form_qn")
                    rn = link.get("routine_name")
                    if fq and rn:
                        slot.setdefault(fq, []).append({"name": rn})

            # BSL routine delta → mark_handler_relink: новая Routine может быть
            # handler-ом существующей подписки / команды / URL method.
            if delta.calls_affected_callers or delta.new_routine_targets or delta.routine_ids_to_delete:
                context.post_linking_impact.mark_handler_relink(config_name)

            # affected_modules: модули, которых мы коснулись.
            for m in delta.modules_to_upsert:
                mid = m.get("id")
                if mid:
                    context.affected_modules.add(mid)

        # ---- 8. Persist sidecar + manifest ----
        if bsl_apply_ok:
            # Заполнить source_scope в новых artifacts (build_delta оставил пустым,
            # потому что не знал phase 2/3 scope).
            new_artifacts = []
            for fd in delta.file_deltas:
                if fd.new_artifact is None:
                    continue
                # Form.bin extras пишут sidecar в свой scope (extras_manifest_scope
                # хранит form_bin scope). Совместимо с full-load: bsl_file_artifacts
                # содержит все BSL routines в общем scope (ART_BASE_BSL), Form.bin
                # routines тоже там. Здесь source_scope=ART_BASE_BSL для всех.
                fd.new_artifact.source_scope = source_scope
                new_artifacts.append(fd.new_artifact)

            # Pre-serialize (heavy JSON dumps) вне транзакции, чтобы не держать
            # BEGIN открытым во время serialization.
            bsl_rows, _bsl_stats = self.state.serialize_bsl_file_artifact_rows(new_artifacts)

            # Manifest: added/changed → upsert (content_hash из ParsedBslFile);
            # deleted → delete. Differentiate BSL vs Form.bin extras: для Form.bin
            # используем root-relative ключ и `extras_manifest_scope`.
            manifest_rows: List[Dict[str, Any]] = []
            # Build path → ParsedBslFile lookup (по abs path).
            abs_to_pbf: Dict[str, Any] = {}
            for pbf in parsed:
                if pbf.abs_path:
                    abs_to_pbf[str(Path(pbf.abs_path))] = pbf

            for path in files_to_parse:
                pbf_for_path = abs_to_pbf.get(str(path))
                if pbf_for_path is None:
                    # Parse-only worker не вернул payload (parser exception, OSError
                    # на read и т.п.). НЕ обновляем manifest — иначе следующий
                    # cycle посчитает файл `unchanged` (по mtime/size/hash) и не
                    # повторит попытку, и граф останется stale до full reload.
                    logger.warning(
                        "BSL apply: skipping manifest update for parse-failed path %s",
                        path,
                    )
                    continue
                is_extra = path in extras_paths_set
                target_scope = (
                    extras_manifest_scope
                    if (extras_manifest_scope and is_extra)
                    else source_scope
                )
                try:
                    manifest_key = root_key_fn(path) if is_extra else key_fn(path)
                except ValueError:
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                content_hash = pbf_for_path.content_hash
                if not content_hash:
                    # ParsedBslFile.content_hash гарантирован [bsl_parse_only.py:81];
                    # его отсутствие — symptom parser bug. Пропускаем manifest update,
                    # следующий cycle re-парсит и заполнит.
                    logger.warning(
                        "BSL apply: skipping manifest update — missing content_hash for %s",
                        path,
                    )
                    continue
                manifest_rows.append({
                    "source_scope": target_scope,
                    "rel_path": manifest_key,
                    "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                    "content_hash": content_hash,
                })

            # Атомарно: sidecar upsert + sidecar delete + manifest upsert + manifest delete.
            # Все четыре write'а — одной транзакцией. JSON dumps уже сделаны выше.
            with self.state.transaction():
                if bsl_rows:
                    self.state.upsert_bsl_file_artifacts_rows(bsl_rows)
                if deleted_keys:
                    self.state.delete_bsl_file_artifacts(source_scope, deleted_keys)
                if manifest_rows:
                    self.state.upsert_artifact_manifest_many(manifest_rows)
                if deleted_keys:
                    self.state.delete_artifact_manifest(source_scope, deleted_keys)

            # In-memory bookkeeping для phase 4 (вне транзакции).
            for fd in delta.file_deltas:
                if fd.new_artifact is None:
                    continue
                context.affected_bsl_files.append((source_scope, fd.rel_path, "upserted"))
            if deleted_keys:
                for key in deleted_keys:
                    context.affected_bsl_files.append((source_scope, key, "removed"))

        # ---- 9. CodeSearchDelta → context (для phase 5) ----
        if not hasattr(context, "code_search_delta") or context.code_search_delta is None:
            context.code_search_delta = delta.code_search_delta
        else:
            ctx_cs = context.code_search_delta
            ctx_cs.added_or_changed_routine_ids |= delta.code_search_delta.added_or_changed_routine_ids
            ctx_cs.deleted_routine_ids |= delta.code_search_delta.deleted_routine_ids
            if hasattr(ctx_cs, "metadata_only_routine_ids"):
                ctx_cs.metadata_only_routine_ids |= getattr(
                    delta.code_search_delta, "metadata_only_routine_ids", set(),
                )
            ctx_cs.affected_rel_paths |= delta.code_search_delta.affected_rel_paths

        # ---- 10. Clear any deferred BSL diff now that apply succeeded.
        # If the cycle crashed earlier (between step 0 read and here), the
        # deferred row remains and the next cycle re-merges and re-applies
        # idempotently (graph ops are MERGE/DETACH-DELETE).
        try:
            self.state.clear_deferred_bsl_diff(project_name, source_scope)
        except Exception:
            logger.exception("clear_deferred_bsl_diff failed")

    # ------------------------------------------------------------------
    # scoped code-search snapshot+ledger writer (step 4.5 of _apply_bsl)
    # ------------------------------------------------------------------

    def _bsl_write_scoped_snapshot_and_ledger(
        self,
        *,
        context: CodeArtifactCycleContext,
        project_name: str,
        delta_cs: Any,
        parsed: List[Any],
        settings_obj: Any = None,
    ) -> None:
        """Compute reverse-counters for OLD state and persist the durable
        ledger row per affected routine. Aborts the whole BSL apply (via
        `_BslCodeSearchSnapshotFailed`) if the write fails — see step 4.5
        docstring in plan §4.
        """
        try:
            sqlite_for_scope = getattr(context, "bsl_code_search_sqlite", None)
            indexer_for_scope = getattr(context, "bsl_code_search_indexer", None)
            scoped_scope = getattr(context, "bsl_code_search_scope", None) or project_name
        except Exception:
            sqlite_for_scope = None
            indexer_for_scope = None
            scoped_scope = project_name
        if sqlite_for_scope is None or indexer_for_scope is None:
            return  # BSL code search disabled → no scoped delta required.

        snapshot_ids: Set[str] = (
            set(getattr(delta_cs, "added_or_changed_routine_ids", set()) or ())
            | set(getattr(delta_cs, "deleted_routine_ids", set()) or ())
        )
        metadata_only_ids: Set[str] = set(
            getattr(delta_cs, "metadata_only_routine_ids", set()) or ()
        )
        ledger_ids: Set[str] = snapshot_ids | metadata_only_ids
        if not ledger_ids:
            return

        from graphdb.bsl_code_phase_a_worker import (
            compute_contributions_from_routine_record,
        )
        import json as _json

        # Resolve scoped epoch target so the ledger can carry the vector_epoch
        # for replay-time matching.
        try:
            vector_state = sqlite_for_scope.vector_state(scoped_scope)
            vector_epoch_target = int(
                getattr(vector_state, "vector_epoch", None)
                or sqlite_for_scope.get_current_epoch(scoped_scope) or 0
            )
        except Exception:
            vector_epoch_target = 0
        # Fetch FULL old routine records from Neo4j (must run BEFORE
        # `load_bsl_signatures` overwrites them).
        try:
            records: Dict[str, Dict[str, Any]] = indexer_for_scope._fetch_routine_records_by_ids(
                scoped_scope, snapshot_ids,
            ) if snapshot_ids else {}
        except Exception:
            logger.exception("fetch_routine_records_by_ids failed in step 4.5")
            raise _BslCodeSearchSnapshotFailed()

        # NEW rel_paths from parse-only output (for ledger.new_rel_path).
        new_rel_path_by_rid: Dict[str, str] = {}
        for pbf in parsed or []:
            for r in getattr(pbf, "routines", []) or []:
                rid = r.get("id")
                if rid:
                    new_rel_path_by_rid[rid] = (
                        r.get("file_path") or r.get("rel_path") or ""
                    )

        try:
            strategy = getattr(settings_obj, "bsl_code_split_strategy", None) or "structural"
        except Exception:
            strategy = "structural"
        snapshot_entries: List[Dict[str, Any]] = []
        for rid in snapshot_ids:
            rec = records.get(rid)
            if not rec or not (rec.get("body") or "").strip():
                snapshot_entries.append({
                    "routine_id": rid, "idf_json": "{}", "stats_json": "{}",
                })
                continue
            try:
                idf, stats = compute_contributions_from_routine_record(
                    rec, strategy, sign=1,
                )
            except Exception:
                logger.exception(
                    "compute_contributions_from_routine_record failed for %s", rid,
                )
                raise _BslCodeSearchSnapshotFailed()
            snapshot_entries.append({
                "routine_id": rid,
                "idf_json": _json.dumps(idf, ensure_ascii=False, sort_keys=True),
                "stats_json": _json.dumps(
                    {fk: [int(dc), int(tl)] for fk, (dc, tl) in stats.items()},
                    ensure_ascii=False, sort_keys=True,
                ),
            })

        ledger_rows: List[Dict[str, Any]] = []
        for rid in ledger_ids:
            old_rec = records.get(rid) or {}
            old_rel = old_rec.get("file_path") or old_rec.get("rel_path") or ""
            new_rel = new_rel_path_by_rid.get(rid, "")
            if rid in (getattr(delta_cs, "deleted_routine_ids", set()) or set()):
                kind = "deleted"
            elif rid in metadata_only_ids:
                kind = "metadata_only"
            elif rid in (getattr(delta_cs, "added_or_changed_routine_ids", set()) or set()):
                kind = "added" if records.get(rid) is None else "changed"
            else:
                # Should not happen — ledger_ids comes from the same source.
                kind = "changed"
            ledger_rows.append({
                "routine_id": rid,
                "change_kind": kind,
                "old_rel_path": old_rel,
                "new_rel_path": new_rel,
                "vector_epoch_target": vector_epoch_target,
                "stage": "snapshot_written",
            })

        try:
            sqlite_for_scope.write_pending_snapshot_and_ledger(
                scope=scoped_scope,
                snapshot_entries=snapshot_entries,
                ledger_rows=ledger_rows,
            )
        except Exception:
            logger.exception("write_pending_snapshot_and_ledger failed")
            raise _BslCodeSearchSnapshotFailed()

# ----------------------------------------------------------------------
# PostLinkingSync
# ----------------------------------------------------------------------


class PostLinkingSync:
    """Phase 4 — refresh handlers + scoped CALLS.

    Все вызовы — main thread; `lease.heartbeat()` между крупными стадиями и внутри
    CALLS stage (после corpus read, после resolve, между chunked Neo4j writes —
    последнее уже встроено в `load_bsl_calls(..., lease=lease)`).
    """

    def __init__(self, loader: Any, state: IncrementalLoadingState) -> None:
        self.loader = loader
        self.state = state

    def run(
        self,
        *,
        settings_obj: Any,
        context: CodeArtifactCycleContext,
        lease: Optional[LockLease] = None,
    ) -> Dict[str, int]:
        stats: Dict[str, int] = {}
        impact = context.post_linking_impact
        project = context.project_name
        base_config = context.base_config_name

        # ---- 1. Config-scoped handler relink ----
        # configs_to_relink = configs_for_handler_relink ∪ keys(any per-config bucket).
        configs_to_relink: Set[str] = set(impact.configs_for_handler_relink)
        configs_to_relink.update(impact.forms_by_config.keys())
        configs_to_relink.update(impact.commands_by_config.keys())
        configs_to_relink.update(impact.url_methods_by_config.keys())
        configs_to_relink.update(impact.event_subscriptions_by_config.keys())

        for config_name in sorted(configs_to_relink):
            is_ext = config_name != base_config
            forms = sorted(impact.forms_by_config.get(config_name, set()))
            commands = sorted(impact.commands_by_config.get(config_name, set()))
            url_methods = sorted(impact.url_methods_by_config.get(config_name, set()))
            event_subs = sorted(impact.event_subscriptions_by_config.get(config_name, set()))

            if forms or commands:
                try:
                    self.loader.refresh_form_event_handlers(
                        project, config_name, forms, commands, is_extension=is_ext,
                    )
                except Exception:
                    logger.exception(
                        "PostLinking: refresh_form_event_handlers failed (%s)", config_name
                    )
            if url_methods:
                try:
                    self.loader.refresh_url_method_handlers(
                        project, config_name, url_methods, is_extension=is_ext,
                    )
                except Exception:
                    logger.exception(
                        "PostLinking: refresh_url_method_handlers failed (%s)", config_name
                    )
            if event_subs:
                try:
                    self.loader.refresh_event_subscription_handlers(
                        project, config_name, event_subs, is_extension=is_ext,
                    )
                except Exception:
                    logger.exception(
                        "PostLinking: refresh_event_subscription_handlers failed (%s)",
                        config_name,
                    )

            # Correctness pass: config-level event subscription handler relink
            # запускается всегда per dirty config (after BSL apply гарантировано
            # этой фазой). Закрывает same-cycle BSL Routine introduction для
            # уже существующих подписок. Идемпотентен (MERGE).
            try:
                self.loader.link_event_subscriptions_to_handlers(project, config_name)
            except Exception:
                logger.exception(
                    "PostLinking: link_event_subscriptions_to_handlers failed (%s)",
                    config_name,
                )

            # Hydrate form routines из per-config slot + догрузить недостающие
            # из Neo4j для форм без entries (Form.xml-only change без BSL delta).
            hydrated = dict(context.form_routines_by_config.get(config_name, {}))
            if forms:
                try:
                    extra = self.loader.collect_form_routines_for_forms(
                        project, config_name, forms,
                    )
                    for fq, rs in (extra or {}).items():
                        hydrated.setdefault(fq, []).extend(rs)
                except Exception:
                    logger.exception(
                        "PostLinking: collect_form_routines_for_forms failed (%s)",
                        config_name,
                    )

            try:
                self.loader.link_form_events_and_commands(
                    project, config_name, hydrated,
                )
            except Exception:
                logger.exception(
                    "PostLinking: link_form_events_and_commands failed (%s)",
                    config_name,
                )

            if lease is not None:
                lease.heartbeat()

        # ---- 2. Extension routine/module links — по known_extension_configs ----
        # Используется state-backed registry, не affected_extension_configs. Этот проход
        # безусловный (idempotent MERGE), поэтому может создать недостающие EXTENDS_* без
        # свежего diff'а (repair после упавшего/preempted цикла). EXTENDS_* попадают в
        # counted Relationships, поэтому суммируем ФАКТИЧЕСКИ созданные рёбра
        # (relationships_created) как сигнал для актуализации статистики Web Console.
        # refresh_extension_*_links (deletions) не считаем: они мутируют только при непустых
        # affected_routines/affected_modules, что уже отражено в context.graph_changed().
        extends_created = 0
        for ext_dir_name, ext_config_name in sorted(context.known_extension_configs.items()):
            try:
                self.loader.refresh_extension_routine_links(
                    project, ext_config_name, base_config,
                    sorted(context.affected_routines), sorted(context.affected_modules),
                )
                self.loader.refresh_extension_module_links(
                    project, ext_config_name, base_config,
                    sorted(context.affected_modules),
                )
                extends_created += self.loader.create_extension_routine_links(
                    settings_obj, ext_config_name, base_config,
                ) or 0
                extends_created += self.loader.create_extension_module_links(
                    settings_obj, ext_config_name, base_config,
                ) or 0
            except Exception:
                logger.exception(
                    "PostLinking: EXTENDS_ROUTINE/MODULE refresh failed (%s)", ext_dir_name,
                )
        stats["extends_relationships_created"] = extends_created
        if lease is not None:
            lease.heartbeat()

        # ---- 3. Form-level extension relationships rebuild (per-form scope) ----
        if (
            impact.base_forms_with_changed_internals
            or impact.ext_forms_with_changed_internals
        ):
            try:
                self.loader.rebuild_form_level_extension_relationships(
                    project_name=project,
                    base_config_name=base_config,
                    known_extension_configs=dict(context.known_extension_configs),
                    base_forms=sorted(impact.base_forms_with_changed_internals),
                    ext_forms_by_config={
                        cfg: sorted(qns)
                        for cfg, qns in impact.ext_forms_with_changed_internals.items()
                    },
                )
            except Exception:
                logger.exception(
                    "PostLinking: rebuild_form_level_extension_relationships failed"
                )
        if lease is not None:
            lease.heartbeat()

        # ---- 4. Scoped CALLS ----
        self._scoped_calls(
            settings_obj=settings_obj,
            context=context,
            lease=lease,
        )

        _log_post_linking_summary(context, stats)
        return stats

    def _scoped_calls(
        self,
        *,
        settings_obj: Any,
        context: CodeArtifactCycleContext,
        lease: Optional[LockLease],
    ) -> None:
        """Scoped CALLS rebuild по трём классам инвалидации.

        Pre-phase-2 уже заполнил `context.calls_class_a_callers`. Здесь добавляем
        (b) callers из re-parsed BSL и (c) callers из reverse-target index.
        """
        # Класс (b): все routines в перепаршенных файлах = callers, чьи callsites обновлены.
        # Берём из affected_routines, заполненного в `_apply_bsl`.
        b_callers: Set[str] = set(context.affected_routines)
        if lease is not None:
            lease.heartbeat()

        # Класс (c): added/renamed routine target lookup. Для каждого нового target
        # `(qualifier, name_lower, min_arity, max_arity)` ищем unchanged callers
        # в `bsl_file_artifacts.callsites_json` с matching qualifier+name+arity.
        c_callers: Set[str] = set()
        if context.new_routine_targets:
            source_mode_local = getattr(settings_obj, "metadata_source", "txt")
            scan_scopes: List[str] = [ART_BASE_BSL]
            # known_extension_configs — state-backed registry, не зависит от
            # успешности filesystem traversal текущего цикла. Используется вместо
            # affected_extension_configs, чтобы не терять sidecar для extensions,
            # у которых ext_code_dir временно отсутствует.
            for ext_dir_name in context.known_extension_configs.keys():
                scan_scopes.append(ext_artifact_scope(source_mode_local, ext_dir_name, "bsl"))
            for scope in scan_scopes:
                for art in self.state.all_bsl_file_artifacts(scope):
                    for cs in art["callsites"]:
                        if _callsite_matches_any(cs, context.new_routine_targets):
                            caller_id = cs.get("caller_id")
                            if caller_id and caller_id not in b_callers:
                                c_callers.add(caller_id)

        affected_callers = sorted(context.calls_class_a_callers | b_callers | c_callers)
        if not affected_callers:
            return

        # Собрать callee corpus: union routines_index из bsl_file_artifacts по всем scope-ам.
        # Базовый scope + все ext bsl scope-ы текущего mode из state-backed registry.
        source_mode = getattr(settings_obj, "metadata_source", "txt")
        all_bsl_scopes: List[str] = [ART_BASE_BSL]
        for ext_dir_name in context.known_extension_configs.keys():
            all_bsl_scopes.append(ext_artifact_scope(source_mode, ext_dir_name, "bsl"))

        routines_indexes: List[Dict[str, Any]] = []
        callsites_for_affected: List[Dict[str, Any]] = []
        affected_set = set(affected_callers)
        for scope in all_bsl_scopes:
            arts = self.state.all_bsl_file_artifacts(scope)
            for art in arts:
                routines_indexes.extend(art["routines_index"])
                for cs in art["callsites"]:
                    if cs.get("caller_id") in affected_set:
                        callsites_for_affected.append(cs)
        if lease is not None:
            lease.heartbeat()

        # Resolve.
        try:
            from indexer.callsites_resolver import CallsitesResolver

            resolver = CallsitesResolver()
            call_rows, _ = resolver.resolve_calls(
                routines_indexes, callsites_for_affected, context.project_name
            )
        except Exception:
            logger.exception("Scoped CALLS: resolve_calls failed")
            return
        if lease is not None:
            lease.heartbeat()

        try:
            self.loader.load_bsl_calls(
                context.project_name, call_rows, affected_callers, lease=lease
            )
        except Exception:
            logger.exception("Scoped CALLS: load_bsl_calls failed")


# ----------------------------------------------------------------------
# Helpers — form path → form_qn для deleted (FormXmlEntry недоступна)
# ----------------------------------------------------------------------


def _form_qn_from_rel_path(
    rel_path: str, project_name: str, config_name: str
) -> Optional[str]:
    """`<Cat>/<Owner>/Forms/<F>/Ext/Form(.xml|.bin)` или `CommonForms/<F>/Ext/Form(.xml|.bin)`.

    Возвращает qualified_name формы или None.
    """
    from xml_metadata.folder_map import FOLDER_TO_RU_CATEGORY

    parts = rel_path.replace("\\", "/").strip("/").split("/")
    if len(parts) < 4:
        return None
    folder = parts[0]
    if folder == "CommonForms":
        # CommonForms/<F>/Ext/Form.xml
        form_name = parts[1]
        return f"{project_name}/{config_name}/ОбщиеФормы/{form_name}/Form/Форма"
    if folder not in FOLDER_TO_RU_CATEGORY:
        return None
    if len(parts) < 5 or parts[2] != "Forms":
        return None
    cat_ru = FOLDER_TO_RU_CATEGORY[folder]
    owner = parts[1]
    form_name = parts[3]
    return f"{project_name}/{config_name}/{cat_ru}/{owner}/Form/{form_name}"


# ----------------------------------------------------------------------
# Logging helpers
# ----------------------------------------------------------------------


def _log_skipped_base_scopes(cfg: ProcessingConfig) -> None:
    skipped: List[str] = []
    if not cfg.enable_forms:
        skipped.append("form_xml")
    if not cfg.enable_bsl:
        skipped.append("form_bin+bsl")
    if not cfg.enable_predefined:
        skipped.append("predefined")
    if not cfg.enable_help:
        skipped.append("help")
    if not cfg.enable_event_subscriptions:
        skipped.append("event_subscription")
    if not cfg.enable_role_rights:
        skipped.append("rights")
    if skipped:
        logger.info(
            "Phase 2 base: skipping scopes due to LOAD_* flags: %s",
            ", ".join(skipped),
        )


def _log_skipped_ext_scopes(cfg: ProcessingConfig) -> None:
    skipped: List[str] = []
    if not cfg.enable_help:
        skipped.append("help")
    if not cfg.enable_event_subscriptions:
        skipped.append("event_subscription")
    if not cfg.enable_role_rights:
        skipped.append("rights")
    if skipped:
        logger.info(
            "Phase 3 ext: skipping scopes due to LOAD_* flags: %s",
            ", ".join(skipped),
        )


def _log_base_summary(summaries: Dict[str, ArtifactSummary]) -> None:
    if not summaries:
        return
    lines = ["Incremental artifacts base complete:"]
    for scope, s in summaries.items():
        if s.added or s.changed or s.deleted:
            kind = scope.split(":")[-1]
            lines.append(f"  {kind} added={s.added} changed={s.changed} deleted={s.deleted}")
    if len(lines) > 1:
        logger.info("\n".join(lines))


def _log_extension_summary(ext_dir_name: str, summaries: Dict[str, ArtifactSummary]) -> None:
    lines = [f"Incremental artifacts ext={ext_dir_name} complete:"]
    has_any = False
    for scope, s in summaries.items():
        if s.added or s.changed or s.deleted:
            kind = scope.split(":")[-1]
            lines.append(f"  {kind} added={s.added} changed={s.changed} deleted={s.deleted}")
            has_any = True
    if has_any:
        logger.info("\n".join(lines))


def _log_post_linking_summary(context: CodeArtifactCycleContext, stats: Dict[str, int]) -> None:
    impact = context.post_linking_impact
    forms_total = sum(len(v) for v in impact.forms_by_config.values())
    commands_total = sum(len(v) for v in impact.commands_by_config.values())
    url_methods_total = sum(len(v) for v in impact.url_methods_by_config.values())
    event_subs_total = sum(
        len(v) for v in impact.event_subscriptions_by_config.values()
    )
    ext_forms_total = sum(
        len(v) for v in impact.ext_forms_with_changed_internals.values()
    )
    logger.info(
        "Incremental post-linking complete: configs_relinked=%d forms=%d commands=%d "
        "event_subs=%d url_methods=%d base_forms_changed=%d ext_forms_changed=%d "
        "affected_bsl_files=%d affected_routines=%d affected_modules=%d "
        "known_ext=%d processed_ext=%d",
        len(impact.configs_for_handler_relink),
        forms_total,
        commands_total,
        event_subs_total,
        url_methods_total,
        len(impact.base_forms_with_changed_internals),
        ext_forms_total,
        len(context.affected_bsl_files),
        len(context.affected_routines),
        len(context.affected_modules),
        len(context.known_extension_configs),
        len(context.affected_extension_configs),
    )


# ----------------------------------------------------------------------
# BslCodeSearchSync — phase 5
# ----------------------------------------------------------------------


class BslCodeSearchSync:
    """Отдельная phase 5 в incremental `_cycle`: sync BSL code search sidecar.

    Не вызывается из `PostLinkingSync` (это другая subsystem с собственным
    transactional invariant). Scheduler передаёт `CodeArtifactCycleContext`
    + `LockLease`; sync читает `context.code_search_delta` и `context.bsl_code_search_delta_applier`
    (с `context.bsl_code_search_scope` как scope).

    State machine (см. plan §D):
      readiness = sqlite.classify_delta_readiness(scope)
      PENDING_REBUILD  → skipped (background full rebuild уже работает);
      REINDEX_REQUIRED → controlled full rebuild через indexer.start_indexing(lease)
                          (приоритет над delta.is_empty() — recovery работает и
                           в cycle без BSL changes);
      empty delta + READY → neutral no-op log;
      non-empty + READY → applier.apply(scope, delta, lease).
    """

    def __init__(self, state: IncrementalLoadingState, loader: Any) -> None:
        self.state = state
        self.loader = loader

    def run(self, context: CodeArtifactCycleContext, lease: Optional[LockLease]) -> bool:
        """Возвращает True, если Phase 5 достигла стадии Neo4j-мутации в этом цикле
        (visibility flip / удаление RoutineCodeUnit / commit), а не «apply завершён».

        Это критично для актуализации статистики: `RoutineCodeUnit` имеют `project_name` и
        связаны с `Routine` через `HAS_CODE_UNIT`, т.е. попадают в counted `TotalNodes` /
        `Relationships` (`get_statistics`). `apply()` удаляет старые `RoutineCodeUnit` ДО
        Phase B, поэтому deferred/failed-after-mutation replay уже изменил counted counts —
        вернуть здесь False = пропустить реальное изменение.
        """
        applier = getattr(context, "bsl_code_search_delta_applier", None)
        indexer = getattr(applier, "indexer", None) if applier is not None else None
        sqlite = getattr(applier, "sqlite", None) if applier is not None else None
        scope = getattr(context, "bsl_code_search_scope", None) or context.project_name

        if applier is None or sqlite is None or indexer is None:
            # Phase 5 не сконфигурирована — пропускаем тихо. Это легитимный
            # rollout state (например, при отключённой BSL code search).
            return False

        # Классификация readiness — semantic enum, не raw fields.
        try:
            readiness = sqlite.classify_delta_readiness(scope)
        except Exception:
            logger.exception("BslCodeSearchSync: classify_delta_readiness failed")
            return False

        from graphdb.bsl_code_search_delta import DeltaReadiness, ApplyResult

        # Исходы, которые возвращаются ПОСЛЕ прохождения стадии Neo4j-мутации
        # (visibility flip / delete RoutineCodeUnit / commit) — counted counts могли
        # измениться. Остальные исходы возвращаются ДО любой Neo4j-записи.
        _POST_MUTATION = {
            ApplyResult.APPLIED,
            ApplyResult.PHASE_B_DEFERRED,
            ApplyResult.FAILED_RETRY_QUEUED,
            ApplyResult.SKIPPED_PENDING_RACE,
        }

        if readiness == DeltaReadiness.PENDING_REBUILD:
            logger.info(
                "BslCodeSearchSync: pending full rebuild active, scoped delta skipped"
            )
            return False

        if readiness == DeltaReadiness.REINDEX_REQUIRED:
            # Recovery owner. Если в long-running app кто-то оставил
            # reindex_requested=1, scheduled cycle сам догоняет rebuild — не
            # ждём рестарта. Lease передаётся внутрь start_indexing для
            # heartbeat (Phase A flush, Phase B around gather).
            logger.warning(
                "BslCodeSearchSync: reindex_required — triggering controlled full rebuild"
            )
            try:
                indexer.start_indexing(lease=lease)
            except Exception:
                logger.exception("BslCodeSearchSync: indexer.start_indexing failed")
                return False
            # Controlled full rebuild переписывает counted RoutineCodeUnit/HAS_CODE_UNIT.
            return True

        from graphdb.bsl_code_search_delta import CodeSearchDelta as ApplierDelta

        if readiness == DeltaReadiness.SCOPED_RETRY:
            # Persisted ledger contains the full work set — applier rebuilds
            # the delta from the ledger; we just give it a placeholder. This
            # path runs even when the current incremental cycle has no fresh
            # BSL changes; without it a crashed previous cycle would leave
            # the ledger and scoped flags forever (until BSL files change
            # again).
            logger.info("BslCodeSearchSync: scoped_retry — replaying ledger")
            try:
                result = applier.apply(
                    scope, ApplierDelta.empty_placeholder(), lease=lease,
                )
            except Exception:
                logger.exception("BslCodeSearchSync: scoped retry apply raised")
                return False
            logger.info(
                "BslCodeSearchSync: scoped retry result=%s",
                getattr(result, "value", result),
            )
            return result in _POST_MUTATION

        delta = getattr(context, "code_search_delta", None)
        if delta is None or delta.is_empty():
            # READY + пустая delta: scoped работы в этом цикле нет. Но полный
            # BSL Phase B, недостроенный из-за прошлого embedding outage
            # (vector_status=failed), в DeltaReadiness НЕ отражён и без рестарта
            # сервиса иначе не догнался бы. Догоняем его здесь, делегируя в уже
            # существующий Phase B-only resume внутри indexer.start_indexing.
            # Recovery запускается ТОЛЬКО на пустой delta: при непустой сначала
            # обязана отработать scoped saga (владеет ledger/snapshot/visibility).
            if self._maybe_recover_vector_phase_b(indexer, sqlite, scope, lease):
                # Phase B-only resume пишет vector properties/visibility, но НЕ
                # меняет counted RoutineCodeUnit/HAS_CODE_UNIT totals → False.
                # Full rebuild из этой точки недостижим: reindex_requested дал бы
                # REINDEX_REQUIRED (обработан выше, return True), config
                # fingerprint process-constant и consumed на startup, а
                # source_changed при пустой delta = False. Поэтому start_indexing
                # здесь детерминированно уходит в Phase B-only ветку.
                return False
            logger.info("BslCodeSearchSync: no BSL routine changes, skipped")
            return False

        # READY + непустой delta → scoped apply.
        try:
            # `applier.apply` принимает CodeSearchDelta из graphdb.bsl_code_search_delta;
            # наш delta — из incremental.bsl_routine_delta (тот же контракт). Конвертация
            # тривиальна — конструируем applier-side dataclass.
            applier_delta = ApplierDelta(
                added_or_changed_routine_ids=set(delta.added_or_changed_routine_ids),
                deleted_routine_ids=set(delta.deleted_routine_ids),
                metadata_only_routine_ids=set(
                    getattr(delta, "metadata_only_routine_ids", set()) or ()
                ),
                affected_rel_paths=set(delta.affected_rel_paths),
            )
            result = applier.apply(scope, applier_delta, lease=lease)
        except Exception:
            logger.exception("BslCodeSearchSync: applier.apply raised")
            return False

        logger.info(
            "BslCodeSearchSync: result=%s (added_or_changed=%d, deleted=%d, files=%d)",
            getattr(result, "value", result),
            len(delta.added_or_changed_routine_ids),
            len(delta.deleted_routine_ids),
            len(delta.affected_rel_paths),
        )
        return result in _POST_MUTATION

    def _maybe_recover_vector_phase_b(
        self, indexer: Any, sqlite: Any, scope: str, lease: Optional[LockLease],
    ) -> bool:
        """Догнать недостроенный полный BSL Phase B в scheduled-цикле.

        Возвращает True, если recovery-путь был выбран (запущен resume ИЛИ
        осознанно пропущен из-за недоступности embedding) — тогда caller НЕ
        печатает нейтральный "no changes" лог. False — recovery-условия не
        выполнены (обычный up-to-date cycle).

        Вызывается только из ветки `READY + пустая delta`.
        """
        if not indexer._bsl_vector_enabled():
            return False

        current_epoch = sqlite.current_epoch(scope)
        if current_epoch <= 0:
            return False

        vs = sqlite.vector_state(scope)
        current_embedding_fp = indexer._compute_embedding_fingerprint()
        vector_needs_phase_b = (
            vs.status != "ready"
            or vs.vector_epoch is None
            or vs.vector_epoch != current_epoch
            or vs.embedding_fingerprint != current_embedding_fp
        )
        if not vector_needs_phase_b:
            return False

        # Bounded availability check: не будим лежащий endpoint каждый пустой
        # цикл, если startup-pass уже знает, что он недоступен; иначе — короткий
        # probe. Это тот же контракт, что и startup-гейт indexer'а.
        avail = getattr(indexer, "_embedding_availability", None)
        if avail is not None and avail.enabled and not avail.available:
            logger.info(
                "BslCodeSearchSync: Phase B recovery skipped, embedding "
                "unavailable: %s", avail.reason,
            )
            return True

        if avail is None:
            from graphdb.embedding_service import probe_embedding_availability
            probe = probe_embedding_availability()
            if not probe.available:
                logger.info(
                    "BslCodeSearchSync: Phase B recovery skipped, embedding "
                    "unavailable: %s", probe.reason,
                )
                return True

        logger.info(
            "BslCodeSearchSync: vector Phase B not ready "
            "(status=%s, vector_epoch=%s, current_epoch=%d), running recovery "
            "via start_indexing",
            vs.status, vs.vector_epoch, current_epoch,
        )
        try:
            indexer.start_indexing(lease=lease)
        except Exception:
            logger.exception(
                "BslCodeSearchSync: Phase B recovery start_indexing failed"
            )
        return True
