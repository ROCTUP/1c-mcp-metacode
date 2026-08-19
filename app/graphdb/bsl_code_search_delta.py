"""Scoped delta applier for the BSL code search subsystem (Phase 5).

Two transactional models live side-by-side with `BslCodeSearchIndexer`:

- `start_indexing()` — full rebuild via pending epoch swap.
- `BslCodeSearchDeltaApplier.apply()` — ledger-driven scoped mutator that
  stays inside the current epoch.

The applier is the single source of truth for cross-store consistency on
scoped changes (Neo4j + SQLite have no shared tx). It reads `bsl_code_pending_scoped_delta`
as a durable ledger, replays whatever stage was reached by a previous
crashed cycle, and only `commit_scoped_delta`s once all stores agree.

Cross-store ordering (see plan §11):

  5.   SQLite gate ON atomically with `scoped_retry_pending = 1` and
       `visibility_flip_done = 0`. Search service immediately treats every
       routine in `pending_routine_ids_json` as RLM-only.
  5.5. Neo4j visibility flip — set `code_embedding_visible = false` on
       both small `Routine` units and large `RoutineCodeUnit` chunks of
       the affected routines.
  5.7. SQLite `visibility_flip_done = 1`. Search service may now use the
       vector leg again, with the prefilter doing the work.
  6.a. Neo4j `REMOVE` (small) + `DETACH DELETE` (large) for routines whose
       ledger stage is still `snapshot_written`.
  6.b. SQLite tx: reverse counters + delete old units + insert new units +
       positive counters + clear snapshot rows + ledger stage -> `sqlite_applied`.
  7.   Scoped Phase B for `changed`/`added` routines (writes embeddings with
       `visible = false`); ledger stage -> `phase_b_done`. For `deleted` /
       `metadata_only` Phase B is skipped — they go straight to `phase_b_done`.
  8.   Module FTS rebuild for affected `rel_path`s from persisted fragments.
  9.   Recompute `source_state_hash`.
  9.5. Restore Neo4j visibility according to coverage policy for
       `change_kind in (changed, added)` routines that reached `phase_b_done`.
 10.   `commit_scoped_delta` — atomic UPDATE that clears all scoped flags
       and removes the ledger / snapshot rows.

On failure between any two steps, `scoped_retry_pending` stays at 1 and the
ledger is preserved so the next cycle replays from the correct stage. The
applier itself never sets `reindex_requested` mid-apply — that flag belongs to
the operational full rebuild path, whose members are: fingerprint mismatch,
missing base index, and a serving partition that failed its integrity check
before the scoped snapshot could be written. The last one is set by the snapshot
step in `incremental.artifact_sync`: such damage is deterministic, so retrying
the same scoped stage would loop forever instead of recovering.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .cypher_templates import CYPHER_DELETE_ROUTINE_CODE_UNITS_BY_IDS

logger = logging.getLogger(__name__)


class DeltaReadiness(Enum):
    """Semantic state of the fingerprint row."""
    READY = "ready"
    PENDING_REBUILD = "pending_rebuild"
    REINDEX_REQUIRED = "reindex_required"
    CONFIG_DELETE_IN_PROGRESS = "config_delete_in_progress"
    SCOPED_RETRY = "scoped_retry"


class ApplyResult(Enum):
    APPLIED = "applied"
    APPLIED_EMPTY = "applied_empty"
    SKIPPED_EMPTY = "skipped_empty"
    SKIPPED_FULL_REBUILD_REQUIRED = "skipped_full_rebuild_required"
    SKIPPED_PENDING_REBUILD = "skipped_pending_rebuild"
    SKIPPED_PENDING_RACE = "skipped_pending_race"
    SKIPPED_NO_BASE_INDEX = "skipped_no_base_index"
    SKIPPED_CONFIG_DELETE_ACTIVE = "skipped_config_delete_active"
    PHASE_B_DEFERRED = "phase_b_deferred"
    FAILED = "failed"
    FAILED_RETRY_QUEUED = "failed_retry_queued"


class ConfigDeletePrepareOutcome(Enum):
    """Исход подготовки удаления конфигурации из BSL sidecar."""

    PREPARED = "prepared"               # снимок записан, gate поднят
    ADOPTED = "adopted"                 # усыновлена подготовка прошлого цикла
    ABSENT = "absent"                   # конфигурации нет в serving epoch
    DEFERRED = "deferred"               # временное препятствие; граф не трогать
    FALLBACK = "fallback"               # scoped невозможен → legacy purge
    FAILED = "failed"


class ConfigDeleteApplyOutcome(Enum):
    """Исход применения подготовленного удаления."""

    APPLIED = "applied"                             # counters вычтены, commit прошёл
    OBSOLETE_EPOCH_REBUILT = "obsolete_epoch_rebuilt"  # rebuild уже построил эпоху без расширения
    DEFERRED = "deferred"                           # transient; повтор следующим циклом
    FALLBACK_REBUILD_REQUIRED = "fallback_rebuild_required"  # владелец восстановления — full rebuild
    FAILED = "failed"


@dataclass
class ConfigDeletePreparation:
    outcome: ConfigDeletePrepareOutcome
    operation_id: str = ""
    current_epoch: Optional[int] = None
    routines_prepared: int = 0
    serving_routines: int = 0
    rel_paths: Set[str] = field(default_factory=set)
    fallback_reason: str = ""
    detail: str = ""


@dataclass
class ConfigDeleteApplyResult:
    outcome: ConfigDeleteApplyOutcome
    routines_applied: int = 0
    corpus_docs_decremented: int = 0
    fallback_reason: str = ""
    detail: str = ""


@dataclass
class CodeSearchDelta:
    """Same shape as `incremental/bsl_routine_delta.CodeSearchDelta` but owned
    by the BSL code search subsystem on entry to `apply()`."""
    added_or_changed_routine_ids: Set[str] = field(default_factory=set)
    deleted_routine_ids: Set[str] = field(default_factory=set)
    metadata_only_routine_ids: Set[str] = field(default_factory=set)
    affected_rel_paths: Set[str] = field(default_factory=set)

    def is_empty(self) -> bool:
        return not (
            self.added_or_changed_routine_ids
            or self.deleted_routine_ids
            or self.metadata_only_routine_ids
            or self.affected_rel_paths
        )

    @classmethod
    def empty_placeholder(cls) -> "CodeSearchDelta":
        """Marker delta used by SCOPED_RETRY replay — applier rebuilds the
        actual work set from the persisted ledger."""
        return cls()


def _validate_serving_partition(
    units: Sequence[Dict[str, Any]], body_len: int,
) -> str:
    """Целостность сохранённого разбиения. Пустая строка — всё в порядке.

    Разбиение serving epoch — единственный источник границ для обратного
    вычитания (пересчитывать его нельзя, см. `compute_contributions_for_ranges`),
    поэтому его повреждение обязано быть замечено ДО удаления графа, когда
    fallback ещё дёшев.
    """
    if not units:
        return "no unit rows"
    part_totals = {int(u.get("part_total") or 0) for u in units}
    if len(part_totals) != 1:
        return f"inconsistent part_total: {sorted(part_totals)}"
    part_total = part_totals.pop()
    if part_total != len(units):
        return f"part_total={part_total} but {len(units)} unit row(s)"
    indices = {int(u.get("part_index") or 0) for u in units}
    if indices != set(range(part_total)):
        return f"part_index set {sorted(indices)} does not cover 0..{part_total - 1}"
    for u in units:
        start = int(u.get("char_start") or 0)
        end = int(u.get("char_end") or 0)
        if not (0 <= start < end <= body_len):
            return f"range [{start}, {end}) outside body of length {body_len}"
    return ""


def detect_serving_context_drift(
    entry: Dict[str, Any], record: Dict[str, Any],
) -> Optional[Tuple[str, str]]:
    """Сверка build-time контекста units с текущим graph record.

    `bsl_code_units` — durable запись того, из чего вклад считали при сборке.
    Расхождение означает состояние, которого текущий pipeline производить не
    умеет (изменение имени/владельца/типа модуля даёт другой routine_id, а
    изменение пути классифицируется как body_changed), то есть — не тихое
    продолжение, а выход в восстановление.

    Модульного уровня, как и `_validate_serving_partition`: обе проверки нужны и
    удалению конфигурации здесь, и scoped-снапшоту в `incremental.artifact_sync`.
    """
    checks = (
        ("body_hash", entry.get("body_hash") or "", record.get("body_hash") or "",
         "serving_source_drift"),
        ("rel_path", entry.get("rel_path") or "", record.get("file_path") or "",
         "context_drift"),
        ("routine_name", entry.get("routine_name") or "", record.get("name") or "",
         "context_drift"),
        ("owner_qn", entry.get("owner_qn") or "", record.get("owner_qn") or "",
         "context_drift"),
        ("module_type", entry.get("module_type") or "", record.get("module_type") or "",
         "context_drift"),
    )
    for field_name, stored, actual, reason in checks:
        if stored != actual:
            return (
                reason,
                f"{entry.get('routine_id')}: {field_name} stored={stored!r} "
                f"graph={actual!r}",
            )
    progress_hash = entry.get("progress_body_hash")
    if progress_hash is not None and progress_hash != (record.get("body_hash") or ""):
        return (
            "serving_source_drift",
            f"{entry.get('routine_id')}: phase_a_state body_hash mismatch",
        )
    return None


# Детерминированные отказы применения: повтор даст тот же результат, поэтому
# они обязаны переводить операцию в fallback, а не ретраиться вечно.
_DETERMINISTIC_APPLY_MARKERS = (
    ("corpus_idf underflow", "counter_underflow"),
    ("corpus_stats underflow", "counter_underflow"),
    ("remain in", "residual_verification_failed"),
)


def _classify_apply_exception(exc: BaseException) -> Optional[str]:
    """Причина, если отказ детерминированный; `None` — transient (retry)."""
    text = str(exc)
    for marker, reason in _DETERMINISTIC_APPLY_MARKERS:
        if marker in text:
            return reason
    return None


class _BslCodeSearchSnapshotFailed(Exception):
    """Raised by `_apply_bsl` step 4.5 if the snapshot/ledger could not be
    persisted before `load_bsl_signatures` overwrites Neo4j body. Surfaced
    as an applier-internal signal — outer caller aborts the BSL apply for
    this cycle to preserve the old body for the next scoped retry."""


# ----------------------------------------------------------------------
# Config-scoped purge — удаление расширения из sidecar
# ----------------------------------------------------------------------


class ConfigPurgeOutcome(Enum):
    """Исход `purge_config_scope`.

    Продвигать стадию очереди можно только по COMPLETED/ABSENT: DEFERRED
    означает, что переход хранилища НЕ состоялся.
    """

    COMPLETED = "completed"   # serving epoch очищена и верифицирована
    DEFERRED = "deferred"     # активная pending epoch / sidecar недоступен
    ABSENT = "absent"         # файла sidecar не существует — чистить нечего


@dataclass
class ConfigPurgeResult:
    outcome: ConfigPurgeOutcome
    routines_purged: int = 0
    reindex_requested: bool = False
    detail: str = ""

    @property
    def stage_may_advance(self) -> bool:
        return self.outcome in (ConfigPurgeOutcome.COMPLETED, ConfigPurgeOutcome.ABSENT)


def purge_config_scope(
    scope: str,
    config_name: str,
    *,
    bsl_enabled: bool,
    sqlite: Any = None,
) -> ConfigPurgeResult:
    """Убрать конфигурацию из BSL sidecar. Владелец операции — эта подсистема.

    Прямой вызов `BslCodeSqlite` из metadata lifecycle не годится: epoch state
    machine принадлежит BSL, и два её правила ломают наивный вариант.

    1. `classify_delta_readiness` отдаёт `PENDING_REBUILD` приоритет над
       `reindex_requested`. При активной pending epoch Phase 5 rebuild в этом
       цикле не запустит, а purge только `current_epoch` не тронет pending —
       она позже станет serving и вернёт строки расширения. Продвигать стадию
       в такой ситуации нельзя.
    2. Инвалидация sidecar привязана к событию перестройки графа, НЕ к
       `enable_bsl_code_search` (см. `_reset_bsl_code_search_after_bulk_load`):
       sidecar лежит на persistent storage, переживает выключение флага, и
       позднее включение без нового reload доверилось бы устаревшему ready-state.
       Удаление расширения — такое же событие перестройки графа.

    При `bsl_enabled=False` ветка ТЕРМИНАЛЬНА: `DEFERRED` там недопустим,
    потому что снять условие некому — scheduler не конструирует индексер,
    `BslCodeSearchSync.run` выходит до классификации readiness, а
    `start_indexing` немедленно возвращается при выключенной настройке.

    LEGACY/FALLBACK API. Здоровое удаление расширения идёт через
    `BslCodeSearchDeltaApplier.prepare_config_delete` / `apply_config_delete`
    и не ставит `reindex_requested`. Этот путь оставлен для строк очереди,
    созданных старой версией, и для явных fallback-режимов; каждый его вызов
    обязан сопровождаться непустым `fallback_reason` в отчёте — иначе полная
    перестройка корпуса произойдёт без объяснимой причины.
    """
    from config import settings as _settings

    # Проверка файла ДО инстанцирования: `get_bsl_code_sqlite()` материализовал
    # бы пустой sidecar (файл + схему) для, возможно, выключенной подсистемы.
    if sqlite is None:
        try:
            sqlite_path = Path(_settings.bsl_code_search_sqlite_path)
        except Exception:  # noqa: BLE001
            return ConfigPurgeResult(
                ConfigPurgeOutcome.DEFERRED, detail="sqlite path unavailable"
            )
        if not sqlite_path.exists():
            return ConfigPurgeResult(ConfigPurgeOutcome.ABSENT, detail="no sidecar file")
        try:
            from .bsl_code_sqlite import get_bsl_code_sqlite

            sqlite = get_bsl_code_sqlite()
        except Exception as exc:  # noqa: BLE001
            logger.exception("purge_config_scope: cannot open sidecar")
            return ConfigPurgeResult(ConfigPurgeOutcome.DEFERRED, detail=repr(exc))

    # Pending epoch разбирается ДО чтения `current_epoch`. Порядок принципиален:
    # на свежем sidecar первая сборка идёт при `current_epoch = 0` (значение по
    # умолчанию в схеме fingerprints), поэтому ранняя проверка "нет serving
    # epoch → ABSENT" смешала бы два разных состояния — действительно пустой
    # индекс и первую незавершённую сборку. Во втором случае координатор счёл бы
    # BSL-стадию пройденной и снял retry-identity, а последующий `commit_pending`
    # опубликовал бы строки уже удалённого расширения.
    if bsl_enabled:
        try:
            readiness = sqlite.classify_delta_readiness(scope)
        except Exception as exc:  # noqa: BLE001
            logger.exception("purge_config_scope: classify_delta_readiness failed")
            return ConfigPurgeResult(ConfigPurgeOutcome.DEFERRED, detail=repr(exc))
        if readiness == DeltaReadiness.CONFIG_DELETE_IN_PROGRESS:
            # Чужая config-delete операция владеет serving-состоянием. Legacy
            # purge здесь недопустим: он снёс бы строки, вклад которых ещё не
            # вычтен из counters, и поставил бы reindex поверх чужой saga.
            return ConfigPurgeResult(
                ConfigPurgeOutcome.DEFERRED, detail="config delete operation active"
            )
        if readiness == DeltaReadiness.PENDING_REBUILD:
            # Записываем durable repair obligation ПЕРЕД возвратом DEFERRED:
            # это и есть недостающий сигнал очереди владельцу прогресса. Без
            # него `reindex_requested` остался бы нулевым, escape по staleness
            # в `BslCodeSearchSync` никогда бы не сработал, и очередь встала бы
            # навсегда.
            try:
                sqlite.request_reindex_if_pending_active(scope)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "purge_config_scope: failed to record rebuild obligation"
                )
            return ConfigPurgeResult(
                ConfigPurgeOutcome.DEFERRED, detail="pending rebuild active"
            )
    else:
        # Осиротевшая или просто ненужная pending epoch: writer-а в процессе
        # нет по построению, поэтому снять её маркеры безопасно.
        #
        # Именно `drop_pending_epoch`, а НЕ `reset_after_full_reload`: полный
        # сброс стёр бы `bsl_code_units` и вместе с ними право на Phase B
        # transfer, то есть заставил бы переэмбеддить корпус целиком при
        # будущем включении BSL. Здесь изменилась одна конфигурация, а не весь
        # граф.
        try:
            if sqlite.has_active_pending(scope):
                sqlite.drop_pending_epoch(scope)
        except Exception as exc:  # noqa: BLE001
            logger.exception("purge_config_scope: drop_pending_epoch failed")
            return ConfigPurgeResult(ConfigPurgeOutcome.DEFERRED, detail=repr(exc))

    try:
        current_epoch = sqlite.get_current_epoch(scope)
    except Exception as exc:  # noqa: BLE001
        logger.exception("purge_config_scope: get_current_epoch failed")
        return ConfigPurgeResult(ConfigPurgeOutcome.DEFERRED, detail=repr(exc))
    if current_epoch is None or int(current_epoch) <= 0:
        # Serving epoch нет, и активной pending уже тоже нет (её разобрали
        # выше) — чистить действительно нечего.
        return ConfigPurgeResult(ConfigPurgeOutcome.ABSENT, detail="no base epoch")
    current_epoch = int(current_epoch)

    try:
        stats = sqlite.purge_config_and_request_reindex(
            scope, current_epoch, config_name,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("purge_config_scope: purge failed for config=%s", config_name)
        return ConfigPurgeResult(ConfigPurgeOutcome.DEFERRED, detail=repr(exc))

    result = ConfigPurgeResult(
        ConfigPurgeOutcome.COMPLETED,
        routines_purged=int(stats.get("routines", 0)),
        reindex_requested=bool(stats.get("reindex_requested", 0)),
        detail=f"epoch={current_epoch} {stats}",
    )
    logger.info(
        "purge_config_scope: config=%s epoch=%d purged=%s reindex_requested=%s",
        config_name, current_epoch, stats, result.reindex_requested,
    )
    return result


class BslCodeSearchDeltaApplier:
    """Scoped Neo4j + SQLite mutator for routine-level changes."""

    def __init__(self, sqlite: Any, indexer: Any) -> None:
        self.sqlite = sqlite
        self.indexer = indexer

    # ------------------------------------------------------------------ entry

    def apply(
        self,
        scope: str,
        delta: CodeSearchDelta,
        lease: Optional[Any] = None,
    ) -> ApplyResult:
        # 1. Config fingerprint mismatch → full rebuild path owns the recovery.
        try:
            current_fp = self.indexer._compute_config_fingerprint()
        except Exception:
            logger.exception("apply: _compute_config_fingerprint failed")
            return ApplyResult.FAILED
        try:
            stored = self.sqlite.read_fingerprint(scope)
        except Exception:
            stored = None
        stored_fp = (stored.get("fingerprint") if stored else "") or ""
        if stored_fp and stored_fp != current_fp:
            try:
                self.sqlite.request_reindex(scope)
            except Exception:
                logger.exception(
                    "apply: failed to set reindex_requested on fingerprint mismatch"
                )
            return ApplyResult.SKIPPED_FULL_REBUILD_REQUIRED

        # 2. A full rebuild is already in flight — scoped apply must yield.
        if self.sqlite.has_active_pending(scope):
            return ApplyResult.SKIPPED_PENDING_REBUILD

        # 2.5 A config delete owns the serving epoch and the reader gate.
        # Touching the gate here (step 5 rewrites `pending_*_json`) would erase
        # its reader-consistency guarantee, and committing would clear the gate
        # of an operation that has not subtracted its counters yet.
        try:
            if self.sqlite.find_active_config_delete_operations(scope):
                return ApplyResult.SKIPPED_CONFIG_DELETE_ACTIVE
        except Exception:
            logger.exception("apply: config delete probe failed")
            return ApplyResult.FAILED

        # 3. current_epoch must exist; no base index means initial full load
        # hasn't happened yet and scoped apply can't write into a missing epoch.
        current_epoch = self.sqlite.get_current_epoch(scope)
        if current_epoch is None or int(current_epoch) <= 0:
            try:
                self.sqlite.request_reindex(scope)
            except Exception:
                logger.exception(
                    "apply: failed to set reindex_requested on missing base index"
                )
            return ApplyResult.SKIPPED_NO_BASE_INDEX
        current_epoch = int(current_epoch)
        try:
            vector_state = self.sqlite.vector_state(scope)
            vector_epoch = int(getattr(vector_state, "vector_epoch", None)
                               or current_epoch)
        except Exception:
            vector_epoch = current_epoch

        # 4. Read ledger BEFORE gate (R20 F1 + R4 F2): single source of truth
        # for what work is pending and what target set the gate must cover.
        try:
            ledger = self.sqlite.read_pending_scoped_delta(scope)
        except Exception:
            logger.exception("apply: read_pending_scoped_delta failed")
            return ApplyResult.FAILED
        if not ledger:
            # Fresh apply with empty in-memory delta — nothing to do.
            if delta is None or delta.is_empty():
                return ApplyResult.APPLIED_EMPTY
            # Fresh apply was supposed to write a ledger row in step 4.5 of
            # `_apply_bsl` but the in-memory delta is non-empty here. This
            # means the caller skipped step 4.5 (BSL code search disabled,
            # snapshot failed gracefully). Treat as empty: nothing to apply.
            return ApplyResult.APPLIED_EMPTY

        ledger_routine_ids = {r["routine_id"] for r in ledger}
        ledger_rel_paths = self._collect_rel_paths_from_ledger(
            scope, current_epoch, ledger,
        )
        by_stage = self._group_by_stage(ledger)

        # 5. SQLite gate ON + scoped_retry_pending=1 + visibility_flip_done=0
        # atomically. From this point on, search service excludes the affected
        # routines from the RLM leg and uses conservative path for vector.
        try:
            self.sqlite.set_scoped_apply_in_progress_atomic(
                scope, True,
                routine_ids=ledger_routine_ids,
                rel_paths=ledger_rel_paths,
                also_set_scoped_retry_pending=True,
                visibility_flip_done=False,
            )
        except Exception:
            logger.exception("apply: set_scoped_apply_in_progress_atomic failed")
            return ApplyResult.FAILED

        try:
            # 5.5 Neo4j visibility flip (only for routines that actually get
            # invalidated — metadata_only stays visible).
            visibility_flip_ids = {
                r["routine_id"] for r in ledger
                if r["change_kind"] in ("changed", "added", "deleted")
            }
            if visibility_flip_ids:
                self.indexer._neo4j_set_visibility_false_for_routines(
                    scope, list(visibility_flip_ids),
                )

            # 5.7 Signal search service it may use the vector leg again.
            self.sqlite.mark_visibility_flip_done(scope, True)

            # 6. Per-stage work.
            sqlite_applied_ids: Set[str] = set()
            phase_b_done_via_embed: Set[str] = set()

            todo_sqlite = by_stage.get("snapshot_written", [])
            if todo_sqlite:
                # 6.a Neo4j clear + DETACH for changed/added/deleted.
                invalidated_ids = {
                    r["routine_id"] for r in todo_sqlite
                    if r["change_kind"] in ("changed", "added", "deleted")
                }
                if invalidated_ids:
                    self._neo4j_clear_routine_code_embeddings(
                        scope, list(invalidated_ids),
                    )
                    self._neo4j_delete_routine_code_units(
                        scope, list(invalidated_ids),
                    )

                # 6.b SQLite tx for snapshot_written rows (split by change_kind).
                snapshot = self.sqlite.read_pending_reverse_snapshot(
                    scope, [r["routine_id"] for r in todo_sqlite],
                )
                self._scoped_sqlite_apply(
                    scope, current_epoch, todo_sqlite, snapshot,
                    lease=lease,
                )
                sqlite_applied_ids = {r["routine_id"] for r in todo_sqlite}

            # 7. Scoped Phase B for stage='sqlite_applied' AND change_kind∈{added,changed}.
            change_kind_by_rid = {r["routine_id"]: r["change_kind"] for r in ledger}
            candidate_phase_b_targets = (
                sqlite_applied_ids
                | {
                    r["routine_id"]
                    for r in by_stage.get("sqlite_applied", [])
                    if r["change_kind"] in ("changed", "added")
                }
            ) & {
                rid
                for rid, ck in change_kind_by_rid.items()
                if ck in ("changed", "added")
            }

            try:
                from config import settings as _runtime_settings
                _phase_b_enabled = bool(
                    getattr(_runtime_settings, "enable_bsl_code_search", False)
                    and getattr(_runtime_settings, "enable_bsl_code_embedding", False)
                )
            except Exception:
                _phase_b_enabled = False

            if _phase_b_enabled and candidate_phase_b_targets:
                try:
                    result = asyncio.run(
                        self.indexer._embed_units_for_routines(
                            scope, current_epoch, vector_epoch,
                            candidate_phase_b_targets,
                            lease=lease,
                        )
                    )
                except Exception as e:
                    from .embedding_service import is_embedding_unavailable_error
                    if is_embedding_unavailable_error(e):
                        # Expected embedding outage: defer without traceback.
                        logger.warning(
                            "apply: scoped Phase B deferred (embedding unavailable): %s", e,
                        )
                        return ApplyResult.PHASE_B_DEFERRED
                    logger.exception("apply: scoped Phase B failed")
                    return ApplyResult.FAILED_RETRY_QUEUED
                from .bsl_code_indexer import PhaseBOutcome  # local import (cycle)
                if result.outcome == PhaseBOutcome.SUCCESS:
                    phase_b_done_via_embed = set(candidate_phase_b_targets)
                    self.sqlite.update_pending_scoped_delta_stage(
                        scope, phase_b_done_via_embed, stage="phase_b_done",
                    )
                else:
                    logger.info(
                        "BslCodeSearchDeltaApplier: scoped Phase B %s: %s",
                        getattr(result.outcome, "value", result.outcome),
                        result.reason or "(no reason)",
                    )
                    return ApplyResult.PHASE_B_DEFERRED

            # routines with no Phase B step (deleted, metadata_only, or
            # changed/added when embeddings are disabled) → straight to
            # phase_b_done.
            no_phase_b_ids = (
                sqlite_applied_ids
                | {r["routine_id"] for r in by_stage.get("sqlite_applied", [])}
            ) - phase_b_done_via_embed
            if no_phase_b_ids:
                self.sqlite.update_pending_scoped_delta_stage(
                    scope, no_phase_b_ids, stage="phase_b_done",
                )

            # 9.5 Scoped visibility restore (R18+R19+R20): only for
            # changed/added routines that reached phase_b_done in this
            # cycle (`phase_b_done_via_embed`) plus those that were already
            # `phase_b_done` from a crashed previous cycle.
            visibility_restore_ids = (
                phase_b_done_via_embed
                | {
                    r["routine_id"]
                    for r in by_stage.get("phase_b_done", [])
                    if r["change_kind"] in ("changed", "added")
                }
            )
            if visibility_restore_ids:
                from config import settings as _runtime_settings
                excluded_owner_categories = list(
                    getattr(
                        _runtime_settings,
                        "bsl_code_embedding_excluded_owner_categories",
                        (),
                    ) or ()
                )
                exclude_regulated_reports = bool(
                    getattr(
                        _runtime_settings,
                        "bsl_code_search_exclude_regulated_reports",
                        False,
                    )
                )
                try:
                    self.indexer._neo4j_restore_visibility_for_committed(
                        scope, vector_epoch, list(visibility_restore_ids),
                        excluded_owner_categories, exclude_regulated_reports,
                    )
                except Exception:
                    logger.exception(
                        "apply: scoped visibility restore failed"
                    )
                    return ApplyResult.FAILED_RETRY_QUEUED

            # 8-10. Общий хвост: module FTS → source_state_hash → CAS commit.
            try:
                outcome = self._finalize_scoped_mutation(
                    scope, current_epoch, ledger_rel_paths,
                    fingerprint=current_fp,
                    expected_epoch=current_epoch,
                    expected_fingerprint=stored_fp or None,
                    expect_reindex_cleared=True,
                    clear_ledger_routine_ids=ledger_routine_ids,
                )
            except Exception:
                logger.exception("apply: scoped mutation finalize failed")
                return ApplyResult.FAILED_RETRY_QUEUED
            if not outcome.ok:
                # Race with background full rebuild / чужой reindex — ledger и
                # scoped flags остаются, следующий цикл классифицирует
                # SCOPED_RETRY и повторит.
                logger.info(
                    "apply: commit rejected (%s); ledger preserved for retry",
                    outcome.value,
                )
                return ApplyResult.SKIPPED_PENDING_RACE
            return ApplyResult.APPLIED

        except Exception:
            logger.exception(
                "BslCodeSearchDeltaApplier.apply: unhandled error — "
                "scoped_retry_pending stays set; ledger preserved for retry"
            )
            return ApplyResult.FAILED_RETRY_QUEUED

    # ------------------------------------------------- shared mutation tail

    def _finalize_scoped_mutation(
        self,
        scope: str,
        current_epoch: int,
        rel_paths: Iterable[str],
        *,
        fingerprint: str,
        expected_epoch: Optional[int] = None,
        expected_fingerprint: Optional[str] = None,
        expect_reindex_cleared: bool = False,
        clear_ledger_routine_ids: Iterable[str] = (),
        config_delete_operation_id: Optional[str] = None,
    ):
        """Единственный владелец порядка завершения scoped-мутации:

            module FTS rebuild (по rel_paths)
         -> source_state_hash recompute
         -> CAS commit_scoped_delta + снятие reader gate
            (+ перевод header config-delete операции в `committed` В ТОЙ ЖЕ
             транзакции, если передан `config_delete_operation_id`).

        Оба пути — routine-level delta и config delete — обязаны исполнять этот
        порядок одинаково. Держать его в двух orchestration-методах значило бы
        при добавлении новой Phase A side-таблицы или изменении gate/commit
        инварианта починить один путь и забыть второй.

        Отдельный `mark_config_delete_state(..., 'committed')` рядом с CAS
        недопустим: любой из двух порядков даёт неверное восстановление после
        сбоя (пометка до CAS — принять невычтенные counters за согласованные;
        после — отправить уже согласованный индекс в legacy purge).
        """
        rel_paths_list = list(rel_paths or ())
        if rel_paths_list:
            self.indexer._rebuild_module_fts_for_rel_paths(
                scope, int(current_epoch), rel_paths_list,
            )
        lightweight = self.indexer._fetch_routines_lightweight()
        new_src_hash = self.indexer._compute_source_state_hash(lightweight)
        return self.sqlite.commit_scoped_delta(
            scope, new_src_hash, fingerprint,
            clear_ledger_routine_ids=list(clear_ledger_routine_ids or ()),
            clear_pending_rel_paths=rel_paths_list,
            expected_epoch=expected_epoch,
            expected_fingerprint=expected_fingerprint,
            expect_reindex_cleared=expect_reindex_cleared,
            config_delete_operation_id=config_delete_operation_id,
        )

    # --------------------------------------------------- config delete saga

    def prepare_config_delete(
        self,
        scope: str,
        config_name: str,
        operation_id: str,
        *,
        scoped_enabled: bool = True,
        lease: Optional[Any] = None,
    ) -> ConfigDeletePreparation:
        """Durable-снимок обратного вклада конфигурации ДО удаления графа.

        Порядок здесь принципиален и повторяет §5 плана: сначала
        reconciliation уже существующей операции (она могла быть записана
        прошлым циклом, упавшим до продвижения стадии очереди), и только потом
        любые проверки маршрута, включая feature flag. Иначе выключение флага
        в этом окне оставило бы осиротевшую операцию и поднятый gate, а legacy
        purge вечно возвращал бы `DEFERRED` из-за неё же.
        """
        existing = None
        try:
            existing = self.sqlite.read_config_delete_operation(scope, operation_id)
        except Exception:
            logger.exception("prepare_config_delete: read operation failed")
            return ConfigDeletePreparation(
                ConfigDeletePrepareOutcome.FAILED, operation_id=operation_id,
                detail="read operation failed",
            )

        if existing is not None:
            # Усыновление пробуется только если fast path вообще ещё выбирается:
            # выключенный флаг — внешняя причина отказа, и она сильнее валидной
            # подготовки. Иначе операция усыновлялась бы вопреки выключателю.
            adopted = (
                self._try_adopt_config_delete(scope, existing)
                if scoped_enabled else None
            )
            if adopted is not None:
                return adopted
            # Усыновить нельзя (или больше не хотим scoped path). Отменяем ЕЁ ЖЕ
            # вместе с gate и только потом решаем маршрут заново — иначе
            # осиротевшая операция вечно возвращала бы `DEFERRED` из legacy
            # purge, и очередь не двигалась бы никогда.
            try:
                self.sqlite.cancel_config_delete_operation(scope, operation_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("prepare_config_delete: cancel of stale operation failed")
                return ConfigDeletePreparation(
                    ConfigDeletePrepareOutcome.FAILED, operation_id=operation_id,
                    detail=f"cancel failed: {exc!r}",
                )

        if not scoped_enabled:
            return ConfigDeletePreparation(
                ConfigDeletePrepareOutcome.FALLBACK, operation_id=operation_id,
                fallback_reason="scoped_delete_disabled",
            )

        # --- readiness ---------------------------------------------------
        try:
            readiness = self.sqlite.classify_delta_readiness(scope)
        except Exception:
            logger.exception("prepare_config_delete: classify_delta_readiness failed")
            return ConfigDeletePreparation(
                ConfigDeletePrepareOutcome.FAILED, operation_id=operation_id,
                detail="classify failed",
            )
        if readiness == DeltaReadiness.PENDING_REBUILD:
            # Durable obligation ПЕРЕД возвратом: без неё осиротевшая pending
            # epoch (rebuild, стартовавший не по флагу и убитый посередине)
            # навсегда закрывает и scoped path, и escape по staleness.
            try:
                self.sqlite.request_reindex_if_pending_active(scope)
            except Exception:
                logger.exception(
                    "prepare_config_delete: failed to record rebuild obligation"
                )
                return ConfigDeletePreparation(
                    ConfigDeletePrepareOutcome.FAILED, operation_id=operation_id,
                    detail="pending obligation write failed",
                )
            return ConfigDeletePreparation(
                ConfigDeletePrepareOutcome.DEFERRED, operation_id=operation_id,
                detail="pending rebuild active",
            )
        if readiness == DeltaReadiness.REINDEX_REQUIRED:
            return ConfigDeletePreparation(
                ConfigDeletePrepareOutcome.FALLBACK, operation_id=operation_id,
                fallback_reason="full_rebuild_already_required",
            )
        if readiness == DeltaReadiness.CONFIG_DELETE_IN_PROGRESS:
            # Не наша операция (у нашей id совпал бы и мы были бы выше).
            return ConfigDeletePreparation(
                ConfigDeletePrepareOutcome.DEFERRED, operation_id=operation_id,
                detail="another config delete operation active",
            )
        if readiness == DeltaReadiness.SCOPED_RETRY:
            # Чужой незавершённый scoped delta: сначала дренируем его тем же
            # applier'ом. Не смогли — граф не трогаем.
            result = self.apply(scope, CodeSearchDelta.empty_placeholder(), lease=lease)
            if result != ApplyResult.APPLIED:
                return ConfigDeletePreparation(
                    ConfigDeletePrepareOutcome.DEFERRED, operation_id=operation_id,
                    detail=f"foreign scoped ledger not drained ({result.value})",
                )
            try:
                readiness = self.sqlite.classify_delta_readiness(scope)
            except Exception:
                logger.exception("prepare_config_delete: re-classify failed")
                return ConfigDeletePreparation(
                    ConfigDeletePrepareOutcome.FAILED, operation_id=operation_id,
                )
        if readiness != DeltaReadiness.READY:
            return ConfigDeletePreparation(
                ConfigDeletePrepareOutcome.DEFERRED, operation_id=operation_id,
                detail=f"readiness={getattr(readiness, 'value', readiness)}",
            )

        current_epoch = self.sqlite.get_current_epoch(scope)
        if current_epoch is None or int(current_epoch) <= 0:
            # Serving epoch нет — вычитать нечего и rebuild требовать не за что.
            return ConfigDeletePreparation(
                ConfigDeletePrepareOutcome.ABSENT, operation_id=operation_id,
                detail="no base epoch",
            )
        current_epoch = int(current_epoch)

        current_fp = self.indexer._compute_config_fingerprint()
        stored = self.sqlite.read_fingerprint(scope) or {}
        stored_fp = (stored.get("fingerprint") or "")
        if stored_fp and stored_fp != current_fp:
            return ConfigDeletePreparation(
                ConfigDeletePrepareOutcome.FALLBACK, operation_id=operation_id,
                fallback_reason="fingerprint_mismatch",
            )

        return self._build_config_delete_preparation(
            scope, config_name, operation_id,
            current_epoch=current_epoch, fingerprint=current_fp, lease=lease,
        )

    def _try_adopt_config_delete(
        self, scope: str, header: Dict[str, Any],
    ) -> Optional[ConfigDeletePreparation]:
        """Проверить существующую подготовку как СВОЮ. `None` — не подошла.

        Успешная подготовка сама оставляет признаки, которые обычная проверка
        readiness считает запретом: поднятый gate и `CONFIG_DELETE_IN_PROGRESS`.
        Без явного различения «своё постусловие / чужой конфликт» усыновление
        было бы недостижимо, и каждый цикл отменял бы здоровую подготовку,
        чтобы посчитать её заново.

        Поэтому gate признаётся своим только при совпадении СОДЕРЖИМОГО с
        множествами операции, а все внешние сигналы остаются блокирующими.
        """
        operation_id = header.get("operation_id") or ""
        if header.get("state") != "prepared":
            return None
        if header.get("stage_counts", {}).get("sqlite_applied"):
            return None
        try:
            current_epoch = self.sqlite.get_current_epoch(scope)
            fp_row = self.sqlite.read_fingerprint(scope) or {}
            gate = self.sqlite.read_scoped_pending_state(scope)
            foreign_ledger = self.sqlite.read_pending_scoped_delta(scope)
            current_fp = self.indexer._compute_config_fingerprint()
        except Exception:
            logger.exception("_try_adopt_config_delete: state read failed")
            return None

        if current_epoch is None or int(current_epoch) != int(header["index_epoch"]):
            return None
        if (fp_row.get("fingerprint") or "") != (header.get("fingerprint") or ""):
            return None
        if (header.get("fingerprint") or "") != current_fp:
            return None
        # Внешние конфликты остаются блокирующими: `scoped_retry_pending` наша
        # подготовка не ставит (см. `write_config_delete_preparation`), поэтому
        # единица в нём означает именно чужой незавершённый delta.
        if fp_row.get("pending_epoch") is not None:
            return None
        if int(fp_row.get("reindex_requested") or 0):
            return None
        if gate.get("scoped_retry_pending") or foreign_ledger:
            return None
        # Gate обязан быть НАШ — по содержимому, а не по факту поднятия.
        if set(gate.get("pending_routine_ids") or ()) != set(header.get("routine_ids") or ()):
            return None
        if set(gate.get("pending_rel_paths") or ()) != set(header.get("rel_paths") or ()):
            return None

        logger.info(
            "config delete %s: adopting durable preparation (routines=%d, epoch=%s)",
            operation_id, len(header.get("routine_ids") or ()), header.get("index_epoch"),
        )
        return ConfigDeletePreparation(
            ConfigDeletePrepareOutcome.ADOPTED,
            operation_id=operation_id,
            current_epoch=int(header["index_epoch"]),
            routines_prepared=len(header.get("routine_ids") or ()),
            serving_routines=int(header.get("serving_count") or 0),
            rel_paths=set(header.get("rel_paths") or ()),
        )

    def _build_config_delete_preparation(
        self,
        scope: str,
        config_name: str,
        operation_id: str,
        *,
        current_epoch: int,
        fingerprint: str,
        lease: Optional[Any] = None,
    ) -> ConfigDeletePreparation:
        from .bsl_code_indexer import _safe_heartbeat
        from .bsl_code_phase_a_worker import (
            ContributionComputationError,
            compute_contributions_for_ranges,
        )
        from .bsl_code_split import UnitRange

        serving = self.sqlite.list_config_serving_routines(
            scope, current_epoch, config_name,
        )
        method_ids = set(self.sqlite.routine_ids_for_config_methods(
            scope, current_epoch, config_name,
        ))
        serving_ids = set(serving) | method_ids
        if not serving_ids and not self.sqlite.config_has_serving_rows(
            scope, current_epoch, config_name,
        ):
            return ConfigDeletePreparation(
                ConfigDeletePrepareOutcome.ABSENT, operation_id=operation_id,
                current_epoch=current_epoch,
                detail="config not present in serving epoch",
            )

        graph_ids = self.indexer._fetch_routine_ids_for_config(
            scope, config_name, lease=lease,
        )
        missing_in_graph = serving_ids - graph_ids
        if missing_in_graph:
            # Serving routine без graph record: исходный вклад восстановить
            # неоткуда, значит доказать точность вычитания невозможно.
            return ConfigDeletePreparation(
                ConfigDeletePrepareOutcome.FALLBACK, operation_id=operation_id,
                current_epoch=current_epoch,
                fallback_reason="missing_old_record",
                detail=f"{len(missing_in_graph)} serving routine(s) absent from graph",
            )

        records: Dict[str, Dict[str, Any]] = {}
        ordered_serving = sorted(serving_ids)
        for start in range(0, len(ordered_serving), 500):
            chunk = ordered_serving[start: start + 500]
            records.update(
                self.indexer._fetch_routine_records_by_ids(scope, chunk)
            )
            _safe_heartbeat(lease)

        routines_payload: List[Dict[str, Any]] = []
        rel_paths: Set[str] = set()
        for rid in ordered_serving:
            entry = serving.get(rid)
            record = records.get(rid)
            if record is None:
                return ConfigDeletePreparation(
                    ConfigDeletePrepareOutcome.FALLBACK, operation_id=operation_id,
                    current_epoch=current_epoch,
                    fallback_reason="missing_old_record",
                    detail=f"no graph record for {rid}",
                )
            if (record.get("config_name") or "") != config_name:
                return ConfigDeletePreparation(
                    ConfigDeletePrepareOutcome.FALLBACK, operation_id=operation_id,
                    current_epoch=current_epoch,
                    fallback_reason="foreign_routine",
                    detail=f"{rid} belongs to {record.get('config_name')!r}",
                )
            if entry is None:
                # Есть только в methods — вклада в counters не имеет, но должен
                # попасть в операцию, иначе residual оставит его строки.
                routines_payload.append({
                    "routine_id": rid, "rel_path": "", "in_serving": False,
                    "idf_json": "{}", "stats_json": "{}",
                })
                continue

            drift = detect_serving_context_drift(entry, record)
            if drift:
                return ConfigDeletePreparation(
                    ConfigDeletePrepareOutcome.FALLBACK, operation_id=operation_id,
                    current_epoch=current_epoch,
                    fallback_reason=drift[0], detail=drift[1],
                )

            body = record.get("body") or ""
            units = entry["units"]
            invalid = _validate_serving_partition(units, len(body))
            if invalid:
                return ConfigDeletePreparation(
                    ConfigDeletePrepareOutcome.FALLBACK, operation_id=operation_id,
                    current_epoch=current_epoch,
                    fallback_reason="serving_partition_invalid",
                    detail=f"{rid}: {invalid}",
                )
            try:
                contribution = compute_contributions_for_ranges(
                    record,
                    [
                        UnitRange(
                            char_start=u["char_start"], char_end=u["char_end"],
                            line_start=u["line_start"], line_end=u["line_end"],
                            part_index=u["part_index"], part_total=u["part_total"],
                        )
                        for u in units
                    ],
                    sign=1,
                )
            except ContributionComputationError as exc:
                return ConfigDeletePreparation(
                    ConfigDeletePrepareOutcome.FALLBACK, operation_id=operation_id,
                    current_epoch=current_epoch,
                    fallback_reason="contribution_uncomputable",
                    detail=f"{rid}: {exc}",
                )

            rel_path = entry.get("rel_path") or ""
            if rel_path:
                rel_paths.add(rel_path)
            routines_payload.append({
                "routine_id": rid,
                "rel_path": rel_path,
                "in_serving": True,
                "idf_json": json.dumps(
                    contribution.idf, ensure_ascii=False, sort_keys=True,
                ),
                "stats_json": json.dumps(
                    {fk: [int(dc), int(tl)] for fk, (dc, tl) in contribution.stats.items()},
                    ensure_ascii=False, sort_keys=True,
                ),
            })
            _safe_heartbeat(lease)

        # Graph-only routines: вклада нет, но их progress-строки обязана снять
        # residual-стадия, иначе в serving epoch остаются записи о routines,
        # которых больше не существует.
        for rid in sorted(graph_ids - serving_ids):
            routines_payload.append({
                "routine_id": rid, "rel_path": "", "in_serving": False,
                "idf_json": "{}", "stats_json": "{}",
            })

        try:
            vector_epoch = getattr(self.sqlite.vector_state(scope), "vector_epoch", None)
        except Exception:
            vector_epoch = None

        self.sqlite.write_config_delete_preparation(
            scope, operation_id,
            config_name=config_name,
            index_epoch=current_epoch,
            vector_epoch=vector_epoch,
            fingerprint=fingerprint,
            rel_paths=rel_paths,
            routines=routines_payload,
            graph_count=len(graph_ids),
        )

        # Перечитать и проверить: подготовка — единственный источник обратного
        # вклада, и «записали, но не то» обязано быть видно ДО удаления графа.
        verify = self.sqlite.read_config_delete_operation(scope, operation_id)
        expected_ids = {r["routine_id"] for r in routines_payload}
        if (
            verify is None
            or verify["routine_ids"] != expected_ids
            or int(verify["index_epoch"]) != current_epoch
        ):
            return ConfigDeletePreparation(
                ConfigDeletePrepareOutcome.FAILED, operation_id=operation_id,
                current_epoch=current_epoch,
                detail="preparation verification failed",
            )

        serving_count = sum(1 for r in routines_payload if r["in_serving"])
        logger.info(
            "config delete %s prepared: config=%s epoch=%d routines=%d "
            "(serving=%d, graph_only=%d) rel_paths=%d",
            operation_id, config_name, current_epoch, len(routines_payload),
            serving_count, len(routines_payload) - serving_count, len(rel_paths),
        )
        return ConfigDeletePreparation(
            ConfigDeletePrepareOutcome.PREPARED,
            operation_id=operation_id,
            current_epoch=current_epoch,
            routines_prepared=len(routines_payload),
            serving_routines=serving_count,
            rel_paths=rel_paths,
        )

    def apply_config_delete(
        self,
        scope: str,
        operation_id: str,
        *,
        lease: Optional[Any] = None,
    ) -> ConfigDeleteApplyResult:
        """Вычесть подготовленный вклад и закоммитить ту же serving epoch.

        Neo4j здесь не трогается вовсе: `delete_configuration_scope` уже снёс и
        `Routine`, и `RoutineCodeUnit` с verification, поэтому visibility flip и
        DETACH были бы гарантированными no-op. Embedding service не
        вызывается — у deleted-only Phase B отсутствует по построению.
        """
        from .bsl_code_indexer import _safe_heartbeat
        from config import settings as _runtime_settings

        header = self.sqlite.read_config_delete_operation(scope, operation_id)
        if header is None:
            return ConfigDeleteApplyResult(
                ConfigDeleteApplyOutcome.FAILED, detail="operation not found",
            )

        current_epoch = self.sqlite.get_current_epoch(scope)
        if current_epoch is None or int(current_epoch) != int(header["index_epoch"]):
            # После graph_deleted единственный способ сменить epoch — полный
            # rebuild, а он строился уже из графа без расширения. Значит
            # вычитать нечего, и двойного вычитания не будет.
            logger.warning(
                "config delete %s: serving epoch changed (%s -> %s); "
                "operation is obsolete, rebuild already excluded the config",
                operation_id, header["index_epoch"], current_epoch,
            )
            return ConfigDeleteApplyResult(
                ConfigDeleteApplyOutcome.OBSOLETE_EPOCH_REBUILT,
                detail="epoch changed",
            )
        current_epoch = int(current_epoch)

        pending_ids = sorted(header["pending_serving_ids"])
        applied = 0
        docs_decremented = 0
        chunk_size = max(
            1, int(getattr(_runtime_settings, "bsl_code_routine_fetch_batch_size", 1000)),
        )
        for start in range(0, len(pending_ids), chunk_size):
            chunk = pending_ids[start: start + chunk_size]
            snapshot = self.sqlite.read_config_delete_snapshot(
                scope, operation_id, chunk,
            )
            idf_neg, stats_neg = self._invert_snapshot(snapshot, chunk)
            docs_decremented += -int(stats_neg.get("_doc", (0, 0))[0])
            try:
                self.sqlite.delete_units_by_routine_ids(
                    scope, current_epoch, chunk,
                    idf_reverse=idf_neg,
                    stats_reverse=stats_neg,
                    set_ledger_stage=None,
                    config_delete_operation_id=operation_id,
                )
            except Exception as exc:  # noqa: BLE001
                reason = _classify_apply_exception(exc)
                if reason is None:
                    logger.exception(
                        "config delete %s: reverse chunk failed (transient)", operation_id,
                    )
                    return ConfigDeleteApplyResult(
                        ConfigDeleteApplyOutcome.DEFERRED, routines_applied=applied,
                        detail=repr(exc),
                    )
                # Детерминированный отказ: повтор даст тот же результат, а часть
                # чанков уже вычтена. Локальное продолжение больше не доказано —
                # владельцем восстановления становится полный rebuild.
                logger.error(
                    "config delete %s: deterministic failure (%s) — handing "
                    "recovery to full rebuild", operation_id, reason,
                )
                if not self._hand_over_to_full_rebuild(scope, operation_id, reason):
                    # Передать владение не удалось: считать handover
                    # состоявшимся нельзя, иначе операция ждала бы rebuild,
                    # которого никто не инициирует.
                    return ConfigDeleteApplyResult(
                        ConfigDeleteApplyOutcome.DEFERRED, routines_applied=applied,
                        detail=f"handover failed after {reason}",
                    )
                return ConfigDeleteApplyResult(
                    ConfigDeleteApplyOutcome.FALLBACK_REBUILD_REQUIRED,
                    routines_applied=applied, fallback_reason=reason,
                    detail=repr(exc),
                )
            applied += len(chunk)
            _safe_heartbeat(lease)

        try:
            outcome = self._finalize_scoped_mutation(
                scope, current_epoch, header["rel_paths"],
                fingerprint=header["fingerprint"],
                expected_epoch=current_epoch,
                expected_fingerprint=header["fingerprint"],
                expect_reindex_cleared=True,
                config_delete_operation_id=operation_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("config delete %s: finalize failed", operation_id)
            return ConfigDeleteApplyResult(
                ConfigDeleteApplyOutcome.DEFERRED, routines_applied=applied,
                detail=repr(exc),
            )

        if outcome.ok:
            logger.info(
                "config delete %s applied: routines=%d docs_decremented=%d epoch=%d",
                operation_id, applied, docs_decremented, current_epoch,
            )
            return ConfigDeleteApplyResult(
                ConfigDeleteApplyOutcome.APPLIED, routines_applied=applied,
                corpus_docs_decremented=docs_decremented,
            )
        return self._handle_commit_rejection(scope, operation_id, outcome, applied)

    def _handle_commit_rejection(
        self, scope: str, operation_id: str, outcome: Any, applied: int,
    ) -> ConfigDeleteApplyResult:
        """Каждый отказ CAS обязан иметь исходящий переход.

        Обнаружение конфликта без выхода после destructive-границы —
        durable deadlock: расширение из графа уже удалено, gate поднят, очередь
        не финализируется, а rebuild заблокирован самой операцией.
        """
        from .bsl_code_sqlite import CommitOutcome

        if outcome == CommitOutcome.REJECTED_PENDING:
            # Ожидание допустимо только вместе с durable obligation: без неё
            # осиротевшая pending epoch не будет распознана как мёртвая
            # (`_pending_rebuild_is_orphaned` требует `reindex_requested=1`).
            try:
                self.sqlite.request_reindex_if_pending_active(scope)
            except Exception:
                logger.exception(
                    "config delete %s: failed to record pending obligation",
                    operation_id,
                )
                return ConfigDeleteApplyResult(
                    ConfigDeleteApplyOutcome.FAILED, routines_applied=applied,
                    detail="pending obligation write failed",
                )
            return ConfigDeleteApplyResult(
                ConfigDeleteApplyOutcome.DEFERRED, routines_applied=applied,
                detail="pending rebuild active",
            )
        if outcome == CommitOutcome.REJECTED_EPOCH:
            return ConfigDeleteApplyResult(
                ConfigDeleteApplyOutcome.OBSOLETE_EPOCH_REBUILT,
                routines_applied=applied, detail="epoch changed under commit",
            )
        reason = (
            "commit_fingerprint_mismatch"
            if outcome == CommitOutcome.REJECTED_FINGERPRINT
            else "commit_reindex_requested"
        )
        if not self._hand_over_to_full_rebuild(scope, operation_id, reason):
            return ConfigDeleteApplyResult(
                ConfigDeleteApplyOutcome.DEFERRED, routines_applied=applied,
                detail=f"handover failed after {reason}",
            )
        return ConfigDeleteApplyResult(
            ConfigDeleteApplyOutcome.FALLBACK_REBUILD_REQUIRED,
            routines_applied=applied, fallback_reason=reason,
        )

    def _hand_over_to_full_rebuild(
        self, scope: str, operation_id: str, reason: str,
    ) -> bool:
        """Передать восстановление полному rebuild, не снимая reader gate.

        Gate держится до публикации новой serving epoch: строки расширения ещё
        лежат в старой, и поиск не должен их показывать. Частично вычтенные
        counters безвредны — rebuild строит эпоху с нуля.

        `fallback_rebuild_required` перестаёт давать `CONFIG_DELETE_IN_PROGRESS`,
        поэтому `REINDEX_REQUIRED` побеждает и Phase 5 запускает перестройку.
        Оба факта пишутся одной транзакцией: смена состояния без обязательства
        перестроить оставила бы операцию ждать epoch, которую никто не сменит.

        False — передать не удалось; вызывающий обязан трактовать это как
        transient и повторить, а не считать handover состоявшимся.
        """
        try:
            self.sqlite.mark_config_delete_fallback_rebuild(
                scope, operation_id, reason,
            )
            return True
        except Exception:
            logger.exception(
                "config delete %s: handover to full rebuild failed", operation_id,
            )
            return False

    def cancel_config_delete(self, scope: str, operation_id: str) -> None:
        """Отменить подготовку до destructive-границы (каталог вернулся).

        Голое удаление строки очереди недопустимо: оно оставило бы снимок и
        поднятый gate, то есть расширение навсегда исчезло бы из поиска, будучи
        живым и в графе, и в индексе.
        """
        self.sqlite.cancel_config_delete_operation(scope, operation_id)

    def abandon_config_delete(self, scope: str, operation_id: str) -> None:
        """Снять операцию, потерявшую смысл (новая epoch уже без расширения).

        Gate снимается здесь же: `commit_pending` его не трогает, поэтому
        никакой rebuild за нас этого не сделает.
        """
        self.sqlite.drop_config_delete_operation(scope, operation_id, release_gate=True)

    def finalize_config_delete(
        self,
        scope: str,
        operation_id: str,
        config_name: str,
    ) -> Dict[str, int]:
        """Residual-стадия: housekeeping без изменения counters."""
        header = self.sqlite.read_config_delete_operation(scope, operation_id)
        current_epoch = self.sqlite.get_current_epoch(scope) or 0
        routine_ids = sorted((header or {}).get("routine_ids") or ())
        return self.sqlite.finalize_config_delete_residuals(
            scope, int(current_epoch), config_name, routine_ids,
            operation_id=operation_id, drop_operation=True,
        )

    # --------------------------------------------------- legacy / compat API

    def invalidate_routines(self, scope: str, routine_ids: List[str]) -> None:
        """Direct Neo4j-side invalidation for backwards-compat callers.

        Used by `_apply_bsl` (`code_embeddings_to_clear` step) when scoped
        ledger machinery is disabled; no longer touches SQLite or sets
        `reindex_requested` (that flag is reserved for fingerprint mismatch).
        """
        rids = list(routine_ids or ())
        if not rids:
            return
        try:
            self._neo4j_clear_routine_code_embeddings(scope, rids)
        except Exception:
            logger.exception("invalidate_routines: Neo4j embedding clear failed")
            raise
        try:
            self._neo4j_delete_routine_code_units(scope, rids)
        except Exception:
            logger.exception("invalidate_routines: Neo4j RoutineCodeUnit delete failed")
            raise

    # ------------------------------------------------------------ internals

    def _group_by_stage(
        self, ledger: Sequence[Dict[str, Any]],
    ) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        for r in ledger:
            out.setdefault(r["stage"], []).append(r)
        return out

    def _collect_rel_paths_from_ledger(
        self,
        scope: str,
        current_epoch: int,
        ledger: Sequence[Dict[str, Any]],
    ) -> Set[str]:
        rel_paths: Set[str] = set()
        for r in ledger:
            if r.get("old_rel_path"):
                rel_paths.add(r["old_rel_path"])
            if r.get("new_rel_path"):
                rel_paths.add(r["new_rel_path"])
        # cover any stale rel_path still present in current SQLite units for
        # the affected routine_ids.
        ids = [r["routine_id"] for r in ledger]
        if ids:
            try:
                with self.sqlite._lock:  # type: ignore[attr-defined]
                    for start in range(0, len(ids), 500):
                        chunk = ids[start: start + 500]
                        placeholders = ",".join("?" * len(chunk))
                        cur = self.sqlite._conn.execute(  # type: ignore[attr-defined]
                            f"SELECT DISTINCT rel_path FROM bsl_code_units "
                            f"WHERE project_name = ? AND index_epoch = ? "
                            f"AND routine_id IN ({placeholders})",
                            (scope, int(current_epoch), *chunk),
                        )
                        for row in cur.fetchall():
                            rp = row[0] if not hasattr(row, "keys") else row["rel_path"]
                            if rp:
                                rel_paths.add(rp)
            except Exception:
                logger.exception(
                    "_collect_rel_paths_from_ledger: rel_path lookup failed"
                )
        return rel_paths

    def _scoped_sqlite_apply(
        self,
        scope: str,
        current_epoch: int,
        todo_sqlite: Sequence[Dict[str, Any]],
        snapshot: Dict[str, Dict[str, Any]],
        lease: Optional[Any] = None,
    ) -> None:
        """Dispatch each ledger row to its matching SQLite operation:
            changed/added → indexer._build_units_for_routines (replace path)
            deleted       → sqlite.delete_units_by_routine_ids (reverse-only)
            metadata_only → indexer._update_units_metadata_for_routines
        Aggregates per-path metrics and emits a single summary log line at the
        end. `lease` is threaded through to heartbeat the scheduler_lock on
        long-running deltas (chunked fetch, drained worker results, SQLite
        commits)."""
        import time as _time
        from config import settings as _runtime_settings
        from .bsl_code_indexer import _safe_heartbeat

        replace_ids: List[str] = []
        delete_ids: List[str] = []
        metadata_ids: List[str] = []
        for r in todo_sqlite:
            ck = r["change_kind"]
            rid = r["routine_id"]
            if ck in ("changed", "added"):
                replace_ids.append(rid)
            elif ck == "deleted":
                delete_ids.append(rid)
            elif ck == "metadata_only":
                metadata_ids.append(rid)

        t0 = _time.monotonic()
        builder_stats: Dict[str, Any] = {}
        delete_tx = 0
        metadata_count = 0

        if replace_ids:
            try:
                builder_stats = self.indexer._build_units_for_routines(
                    scope, replace_ids, set(),
                    current_epoch=current_epoch,
                    reverse_snapshot=snapshot,
                    lease=lease,
                ) or {}
            except Exception:
                logger.exception("_scoped_sqlite_apply: _build_units_for_routines failed")
                raise

        if delete_ids:
            chunk_size = max(
                1, int(getattr(_runtime_settings, "bsl_code_routine_fetch_batch_size", 1000)),
            )
            try:
                for start in range(0, len(delete_ids), chunk_size):
                    chunk = delete_ids[start: start + chunk_size]
                    idf_neg, stats_neg = self._invert_snapshot(snapshot, chunk)
                    self.sqlite.delete_units_by_routine_ids(
                        scope, current_epoch, chunk,
                        idf_reverse=idf_neg,
                        stats_reverse=stats_neg,
                        clear_snapshot_ids=chunk,
                        set_ledger_stage="sqlite_applied",
                    )
                    delete_tx += 1
                    _safe_heartbeat(lease)
            except Exception:
                logger.exception("_scoped_sqlite_apply: delete_units_by_routine_ids failed")
                raise

        if metadata_ids:
            try:
                metadata_count = self.indexer._update_units_metadata_for_routines(
                    scope, current_epoch, metadata_ids, lease=lease,
                )
                # ledger stage transition done inside update_unit_metadata_for_routines.
            except Exception:
                logger.exception(
                    "_scoped_sqlite_apply: _update_units_metadata_for_routines failed"
                )
                raise

        duration = _time.monotonic() - t0
        sqlite_tx_total = (
            int(builder_stats.get("sqlite_transactions", 0))
            + delete_tx
            + (1 if metadata_ids else 0)
        )
        logger.info(
            "BslCodeSearchSync: scoped Phase 5A complete "
            "replace=%d delete=%d metadata_only=%d "
            "records_fetched=%d missing=%d "
            "packs=%d workers=%d mode=%s "
            "units=%d methods=%d fragments=%d metadata_updated=%d "
            "sqlite_tx=%d duration=%.2fs",
            len(replace_ids), len(delete_ids), len(metadata_ids),
            int(builder_stats.get("records_fetched", 0)),
            int(builder_stats.get("missing", 0)),
            int(builder_stats.get("work_packs", 0)),
            int(builder_stats.get("workers_used", 0)),
            str(builder_stats.get("execution_mode", "n/a")),
            int(builder_stats.get("units_written", 0)),
            int(builder_stats.get("methods_written", 0)),
            int(builder_stats.get("fragments_written", 0)),
            int(metadata_count),
            sqlite_tx_total, duration,
        )

    def _invert_snapshot(
        self,
        snapshot: Dict[str, Dict[str, Any]],
        routine_ids: Iterable[str],
    ) -> Tuple[Dict[str, Dict[str, int]], Dict[str, Tuple[int, int]]]:
        idf_neg: Dict[str, Dict[str, int]] = {}
        stats_neg: Dict[str, Tuple[int, int]] = {}
        for rid in routine_ids:
            entry = snapshot.get(rid)
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

    # ---------------------------------------------------------- Neo4j helpers

    def _neo4j_clear_routine_code_embeddings(
        self, scope: str, rids: List[str],
    ) -> None:
        """REMOVE r.code_embedding/_epoch/_visible + label for the small shape."""
        driver = getattr(self.indexer, "driver", None)
        if driver is None or not rids:
            return
        from config import settings
        with driver.session(database=getattr(settings, "neo4j_database", "neo4j")) as session:
            session.run(
                """
                UNWIND $ids AS rid
                MATCH (r:Routine {id: rid})
                WHERE r.project_name = $project_name
                REMOVE r:BslCodeSearchUnit
                REMOVE r.code_embedding
                REMOVE r.code_embedding_epoch
                REMOVE r.code_embedding_visible
                """,
                ids=rids,
                project_name=scope,
            )

    def _neo4j_delete_routine_code_units(
        self, scope: str, rids: List[str],
    ) -> None:
        """DETACH DELETE RoutineCodeUnit using denormalised `routine_id`.

        FIX: the previous version used `(r)<-[:OF_ROUTINE]-(u)` which referred
        to a non-existent relationship — the write contract is
        `MERGE (parent)-[:HAS_CODE_UNIT]->(u)`. The denormalised
        `u.routine_id` is the cross-cut source of truth.
        """
        driver = getattr(self.indexer, "driver", None)
        if driver is None or not rids:
            return
        from config import settings
        with driver.session(database=getattr(settings, "neo4j_database", "neo4j")) as session:
            session.run(
                CYPHER_DELETE_ROUTINE_CODE_UNITS_BY_IDS,
                routine_ids=rids,
                project_name=scope,
            )
