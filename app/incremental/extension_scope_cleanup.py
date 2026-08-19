"""Lifecycle полного удаления extension scope.

Владелец durable-очереди `extension_scope_cleanup`. Discovery (TXT и XML)
только ставит запросы в очередь и больше ничего не знает ни про graph labels,
ни про BSL storage, ни про файлы сводок.

Почему очередь, а не последовательность вызовов внутри одного прохода: Neo4j,
incremental SQLite, BSL SQLite и файловая система не разделяют транзакцию.
Любая стадия должна быть идемпотентной и продолжаемой в следующем цикле —
в том числе после рестарта процесса.

Порядок стадий:

    discovered -> graph_deleted -> summaries_quarantined -> bsl_purged -> finalized

`summaries_quarantined` стоит ДО `bsl_purged` сознательно: файловая стадия ни
от чего не зависит, а BSL-стадия единственная может законно отложиться
(`DEFERRED`). При обратном порядке отложенный BSL блокировал бы именно то
последствие, которое хуже всего, — возможность воскрешения устаревшей сводки
при повторном появлении расширения с тем же QN.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .report import ExtensionRemovalReport, IncrementalReport
from .state import (
    CLEANUP_BSL_MODE_ABSENT,
    CLEANUP_BSL_MODE_DEFERRED,
    CLEANUP_BSL_MODE_DISABLED,
    CLEANUP_BSL_MODE_FULL_REBUILD_RECOVERY,
    CLEANUP_BSL_MODE_LEGACY_PURGE,
    CLEANUP_BSL_MODE_SCOPED_DELTA,
    CLEANUP_REASON_CONFIGURATION_RENAMED,
    CLEANUP_REASON_DIRECTORY_REMOVED,
    CLEANUP_REASON_STRUCTURE_INVALID,
    CLEANUP_STAGE_BSL_DELETE_PREPARED,
    CLEANUP_STAGE_BSL_DELTA_APPLIED,
    CLEANUP_STAGE_BSL_PURGED,
    CLEANUP_STAGE_FINALIZED,
    CLEANUP_STAGE_GRAPH_DELETED,
    CLEANUP_STAGE_SUMMARIES_QUARANTINED,
    IncrementalLoadingState,
    cleanup_stage_rank,
)

logger = logging.getLogger(__name__)

# Сколько подтверждений подряд нужно, чтобы снести scope с невалидной
# структурой. Неатомарная выгрузка или обновление bind mount не должны
# мгновенно уничтожать расширение.
STRUCTURE_INVALID_REQUIRED_CONFIRMATIONS = 2


def _open_existing_sidecar() -> Optional[Any]:
    """BSL sidecar, если он уже существует. Не создавать файл и схему.

    Единственный потребитель — teardown config-delete операции при выключенном
    `ENABLE_BSL_CODE_SEARCH`: обычная фабрика материализовала бы пустой sidecar
    для выключенной подсистемы, и позднее включение доверилось бы его
    ready-state.

    `None` означает СТРОГО «файла нет». Ошибка открытия существующего файла
    пробрасывается исключением и обязана сохранить запись очереди: иначе
    «не смогли открыть» превратилось бы в «чистить нечего», очередь исчезла
    бы, а подготовка и поднятый gate остались бы в реально существующем
    sidecar навсегда.
    """
    from graphdb.bsl_code_sqlite import open_existing_bsl_sidecar

    return open_existing_bsl_sidecar()


@dataclass
class ExtensionCleanupRequest:
    source_scope: str
    source_mode: str
    ext_dir_name: str
    config_qn: str
    config_name: str
    reason: str


@dataclass
class ExtensionDiscoveryEvidence:
    """Authoritative-факты одного цикла.

    `None` означает «доказательств нет» — так выглядит вызов до discovery.
    Пустое множество и `None` — разные вещи: первое значит «корень перечислен,
    каталогов нет», второе — «корень не перечисляли».
    """

    present_ext_dirs: Optional[Set[str]] = None
    revalidated_ext_dirs: Optional[Set[str]] = None

    @property
    def is_empty(self) -> bool:
        return self.present_ext_dirs is None and self.revalidated_ext_dirs is None


@dataclass
class ExtensionCleanupResult:
    source_scope: str
    ext_dir_name: str
    config_qn: str
    reason: str
    neo4j_nodes_deleted: int = 0
    graph_refresh_required: bool = False
    bsl_routines_purged: int = 0
    bsl_outcome: str = ""
    summaries_quarantined: bool = False
    finalized: bool = False
    superseded: bool = False
    resumed: bool = False
    attempts: int = 0
    error: Optional[str] = None
    # BSL-маршрут этого удаления. `bsl_full_phase_a_required` — единственный
    # флаг, по которому видно, заплатили ли мы полной перестройкой корпуса.
    bsl_mode: str = ""
    bsl_operation_id: str = ""
    bsl_routines_prepared: int = 0
    bsl_routines_applied: int = 0
    bsl_corpus_docs_decremented: int = 0
    bsl_full_phase_a_required: bool = False
    bsl_fallback_reason: str = ""

    def to_report(self) -> ExtensionRemovalReport:
        return ExtensionRemovalReport(
            source_scope=self.source_scope,
            ext_dir_name=self.ext_dir_name,
            config_qn=self.config_qn,
            reason=self.reason,
            neo4j_nodes_deleted=self.neo4j_nodes_deleted,
            graph_refresh_required=self.graph_refresh_required,
            bsl_routines_purged=self.bsl_routines_purged,
            bsl_outcome=self.bsl_outcome,
            summaries_quarantined=self.summaries_quarantined,
            finalized=self.finalized,
            superseded=self.superseded,
            resumed=self.resumed,
            attempts=self.attempts,
            error=self.error,
            bsl_mode=self.bsl_mode,
            bsl_routines_prepared=self.bsl_routines_prepared,
            bsl_routines_applied=self.bsl_routines_applied,
            bsl_corpus_docs_decremented=self.bsl_corpus_docs_decremented,
            bsl_full_phase_a_required=self.bsl_full_phase_a_required,
            bsl_fallback_reason=self.bsl_fallback_reason,
        )


class ExtensionScopeCleanupCoordinator:
    """Stage-машина удаления extension scope."""

    def __init__(
        self,
        loader: Any,
        state: IncrementalLoadingState,
        settings_obj: Any,
        lease: Any = None,
        bsl_services: Any = None,
    ) -> None:
        self.loader = loader
        self.state = state
        self.settings_obj = settings_obj
        self.lease = lease
        # BSL bundle цикла (см. scheduler). None = подсистема выключена; тогда
        # операцию, уже пересёкшую destructive-границу, закрывает
        # recovery-accessor, а не bundle.
        self.bsl_services = bsl_services
        # Кэш блокировки на цикл. Инвалидируется при enqueue и при финализации,
        # иначе проход 2 discovery работал бы по устаревшему множеству.
        self._blocked_cache: Optional[Set[str]] = None
        # Scope, у которых в ЭТОМ цикле была разрушена и доведена до конца
        # destructive-saga. Координатор живёт ровно один цикл, поэтому
        # множество естественно cycle-local. См. `_stage_finalize`.
        self._destroyed_this_cycle: Set[str] = set()

    # ------------------------------------------------------------------
    # Блокировка scope
    # ------------------------------------------------------------------

    def blocked_scopes(self, refresh: bool = False) -> Set[str]:
        """`source_scope`, которые в этом цикле нельзя загружать и изменять.

        Это незавершённые записи очереди плюс scope, чью destructive-saga этот
        цикл довёл до конца (`_destroyed_this_cycle`).

        Пока запись жива, scope не должен обрабатываться НИ ОДНОЙ фазой цикла:
        `delete_scope` бьёт по `source_scope`, а не по `config_qn`, поэтому
        отложенная финализация иначе снесла бы baseline уже нового поколения,
        а artifact phase успела бы воссоздать узлы старого под уже отработавшей
        стадией `graph_deleted`.

        Исключение НЕ проглатывается: источник блокировки — safety-инвариант,
        и «не смогли прочитать» обязано означать «считаем заблокированным», а не
        «блокировок нет». Решение о fail-closed degradation принимает вызывающий
        (см. `has_pending_cleanup` и scheduler).
        """
        if refresh or self._blocked_cache is None:
            self._blocked_cache = self.state.list_pending_cleanup_scopes()
        return self._blocked_cache | self._destroyed_this_cycle

    def has_pending_cleanup(self, source_scope: str) -> bool:
        """Заблокирован ли scope. При ошибке чтения очереди — True.

        Fail-closed по умолчанию: пропустить обновление расширения на один цикл
        безопасно, а обработать scope, для которого граф уже разрушен, — нет:
        стадии монотонны, повторно граф не почистится, и узлы старого поколения
        останутся orphan-подграфом.
        """
        try:
            return source_scope in self.blocked_scopes()
        except Exception:  # noqa: BLE001
            logger.exception(
                "has_pending_cleanup(%s): cleanup queue unreadable; treating scope "
                "as blocked", source_scope,
            )
            return True

    def mark_destroyed_this_cycle(self, source_scope: str) -> None:
        self._destroyed_this_cycle.add(source_scope)

    def begin_cycle(self) -> None:
        """Открыть новый цикл: снять cycle-local запреты и кэш.

        Scheduler создаёт координатор заново на каждый цикл, поэтому там это
        не нужно. Метод существует для владельцев с более длинным временем
        жизни: без явной границы `_destroyed_this_cycle` из первого цикла
        продолжал бы блокировать scope во всех последующих, превращая
        однократную задержку загрузки в постоянную.
        """
        self._destroyed_this_cycle = set()
        self._blocked_cache = None

    def invalidate_blocked_cache(self) -> None:
        self._blocked_cache = None

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

    def enqueue(self, request: ExtensionCleanupRequest) -> int:
        """Поставить scope в очередь. Идемпотентно; повтор поднимает
        `confirmations`, что и накапливает подтверждения для `structure_invalid`.
        """
        confirmations = self.state.enqueue_extension_cleanup(
            source_scope=request.source_scope,
            config_qn=request.config_qn,
            source_mode=request.source_mode,
            ext_dir_name=request.ext_dir_name,
            config_name=request.config_name,
            reason=request.reason,
        )
        self.invalidate_blocked_cache()
        logger.info(
            "Extension cleanup enqueued: scope=%s config=%s reason=%s confirmations=%d",
            request.source_scope, request.config_qn, request.reason, confirmations,
        )
        return confirmations

    # ------------------------------------------------------------------
    # Resume
    # ------------------------------------------------------------------

    def resume_pending(
        self,
        report: Optional[IncrementalReport] = None,
        *,
        evidence: Optional[ExtensionDiscoveryEvidence] = None,
    ) -> List[ExtensionCleanupResult]:
        """Продвинуть все незавершённые записи очереди.

        Вызывается дважды за цикл, и у вызовов разные полномочия:

        - без evidence (до discovery) обрабатываются ТОЛЬКО записи с
          `graph_mutation_started = 1`: им guard не нужен, destructive-граница
          уже пересечена и saga обязана дойти до конца независимо от того, что
          сейчас на диске. Записи на стадии `discovered` не трогаются вовсе —
          для них ещё нет authoritative-фактов;
        - с evidence (после discovery) обрабатываются все.
        """
        evidence = evidence or ExtensionDiscoveryEvidence()
        try:
            rows = self.state.list_pending_extension_cleanups()
        except Exception:  # noqa: BLE001
            logger.exception("resume_pending: cannot read cleanup queue")
            return []
        if not rows:
            return []

        results: List[ExtensionCleanupResult] = []
        for row in rows:
            if evidence.is_empty and not int(row.get("graph_mutation_started") or 0):
                # Ещё не начинали ломать граф и не знаем, что на диске.
                continue
            result = self._run_one(row, evidence)
            if result is None:
                continue
            results.append(result)
            if report is not None:
                report.removed_extension_scopes.append(result.to_report())

        self.invalidate_blocked_cache()
        if report is not None:
            try:
                # Считаем ТОЛЬКО durable-записи очереди. `blocked_scopes` сюда
                # не годится: он дополнительно содержит cycle-local запрет на
                # повторную загрузку уже финализированных scope, а это не
                # retry-identity — повторять там нечего, и отчёт сообщал бы
                # «will be retried next cycle» про завершённое удаление.
                report.pending_extension_cleanups = len(
                    self.state.list_pending_cleanup_scopes()
                )
            except Exception:  # noqa: BLE001
                logger.exception("resume_pending: pending count failed")
        return results

    # ------------------------------------------------------------------
    # Stage machine
    # ------------------------------------------------------------------

    def _run_one(
        self, row: Dict[str, Any], evidence: ExtensionDiscoveryEvidence
    ) -> Optional[ExtensionCleanupResult]:
        source_scope = row["source_scope"]
        config_qn = row["config_qn"]
        stage = row["stage"]
        reason = row["reason"]
        graph_mutation_started = bool(int(row.get("graph_mutation_started") or 0))

        result = ExtensionCleanupResult(
            source_scope=source_scope,
            ext_dir_name=row["ext_dir_name"],
            config_qn=config_qn,
            reason=reason,
            graph_refresh_required=bool(int(row.get("graph_refresh_required") or 0)),
            resumed=cleanup_stage_rank(stage) > 0,
            attempts=int(row.get("attempts") or 0),
            bsl_mode=row.get("bsl_mode") or "",
            bsl_operation_id=row.get("cleanup_uid") or "",
            bsl_fallback_reason=row.get("bsl_fallback_reason") or "",
        )

        # --- 0. Reason-aware recovery guard -----------------------------
        if not graph_mutation_started and self._should_supersede(row, evidence):
            logger.info(
                "Extension cleanup superseded before destructive boundary: "
                "scope=%s config=%s reason=%s",
                source_scope, config_qn, reason,
            )
            # Сначала снять durable-подготовку, если она уже записана: голое
            # удаление строки очереди оставило бы снимок и поднятый reader
            # gate, то есть живое расширение навсегда исчезло бы из поиска.
            if not self._cancel_prepared_operation(row, result):
                return result
            self.state.delete_extension_cleanup(source_scope, config_qn)
            self.invalidate_blocked_cache()
            result.superseded = True
            return result

        # --- 1. Подтверждение для structure_invalid ---------------------
        if (
            reason == CLEANUP_REASON_STRUCTURE_INVALID
            and not graph_mutation_started
            and int(row.get("confirmations") or 0) < STRUCTURE_INVALID_REQUIRED_CONFIRMATIONS
        ):
            logger.info(
                "Extension cleanup waiting for second confirmation: scope=%s config=%s "
                "(confirmations=%s)",
                source_scope, config_qn, row.get("confirmations"),
            )
            return None

        try:
            # --- 2. bsl_delete_prepared ---------------------------------
            # ДО удаления графа: снимок обратного вклада можно построить только
            # пока граф цел. Стадия пропускается только для legacy-строк и для
            # режимов, где scoped-путь не выбран.
            if cleanup_stage_rank(stage) < cleanup_stage_rank(CLEANUP_STAGE_GRAPH_DELETED):
                if not self._stage_prepare_bsl_delete(row, result):
                    return result
                stage = CLEANUP_STAGE_BSL_DELETE_PREPARED

            # --- 3. graph_deleted ---------------------------------------
            if cleanup_stage_rank(stage) < cleanup_stage_rank(CLEANUP_STAGE_GRAPH_DELETED):
                self._stage_delete_graph(row, result)
                stage = CLEANUP_STAGE_GRAPH_DELETED

            # --- 4. summaries_quarantined -------------------------------
            if cleanup_stage_rank(stage) < cleanup_stage_rank(
                CLEANUP_STAGE_SUMMARIES_QUARANTINED
            ):
                self._stage_quarantine_summaries(row, result)
                stage = CLEANUP_STAGE_SUMMARIES_QUARANTINED

            # --- 5. bsl_delta_applied -----------------------------------
            if cleanup_stage_rank(stage) < cleanup_stage_rank(
                CLEANUP_STAGE_BSL_DELTA_APPLIED
            ):
                if not self._stage_apply_bsl_delta(row, result):
                    return result
                stage = CLEANUP_STAGE_BSL_DELTA_APPLIED

            # --- 6. bsl_purged ------------------------------------------
            if cleanup_stage_rank(stage) < cleanup_stage_rank(CLEANUP_STAGE_BSL_PURGED):
                if not self._stage_purge_bsl(row, result):
                    # DEFERRED — переход хранилища не состоялся. Запись остаётся,
                    # ошибкой это не является.
                    return result
                stage = CLEANUP_STAGE_BSL_PURGED

            # --- 7. finalized -------------------------------------------
            self._stage_finalize(row, result)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Extension cleanup stage failed: scope=%s config=%s stage=%s",
                source_scope, config_qn, stage,
            )
            result.error = repr(exc)
            try:
                self.state.record_extension_cleanup_error(source_scope, config_qn, repr(exc))
            except Exception:  # noqa: BLE001
                logger.exception("record_extension_cleanup_error failed")
        return result

    def _should_supersede(
        self, row: Dict[str, Any], evidence: ExtensionDiscoveryEvidence
    ) -> bool:
        """Исчезла ли причина удаления.

        Условие зависит от reason, потому что «каталог присутствует» означает
        для них разное:

        - `directory_removed` — каталог физически вернулся, причина исчезла;
        - `structure_invalid` — каталог присутствовал и в момент постановки в
          очередь, поэтому presence ничего не доказывает; отменять можно только
          по доказанной валидности структуры. Presence-based условие здесь
          отменяло бы КАЖДЫЙ запрос до второго подтверждения, и повреждённое
          расширение не очищалось бы никогда;
        - `configuration_renamed` — каталог обязан быть на месте, отмены нет.
        """
        reason = row["reason"]
        ext_dir_name = row["ext_dir_name"]
        if reason == CLEANUP_REASON_DIRECTORY_REMOVED:
            present = evidence.present_ext_dirs
            return present is not None and ext_dir_name in present
        if reason == CLEANUP_REASON_STRUCTURE_INVALID:
            revalidated = evidence.revalidated_ext_dirs
            return revalidated is not None and ext_dir_name in revalidated
        if reason == CLEANUP_REASON_CONFIGURATION_RENAMED:
            return False
        return False

    # ------------------------------------------------------------------
    # BSL scoped delete
    # ------------------------------------------------------------------

    def _bsl_scope(self) -> str:
        return (
            getattr(self.settings_obj, "project_name", None)
            or self.state.project_name
        )

    def _bsl_enabled(self) -> bool:
        return bool(getattr(self.settings_obj, "enable_bsl_code_search", False))

    def _scoped_delete_enabled(self) -> bool:
        return bool(
            getattr(self.settings_obj, "bsl_config_delete_scoped_enabled", True)
        )

    def _set_bsl_mode(
        self,
        row: Dict[str, Any],
        result: ExtensionCleanupResult,
        mode: str,
        *,
        fallback_reason: str = "",
        full_phase_a: bool = False,
    ) -> bool:
        """Зафиксировать маршрут durable. False — записать не удалось.

        Это не метрика, а durable route: именно по нему resume после рестарта
        решает, применять ли подготовленный снимок. Поэтому неуспех обязан быть
        виден вызывающему, а не проглочен — продолжив, мы получили бы durable
        стадию и durable маршрут, рассогласованные между собой.
        """
        result.bsl_mode = mode
        result.bsl_fallback_reason = fallback_reason
        result.bsl_full_phase_a_required = full_phase_a
        try:
            self.state.record_extension_cleanup_bsl_mode(
                row["source_scope"], row["config_qn"], mode,
                fallback_reason=fallback_reason,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.exception("record_extension_cleanup_bsl_mode failed")
            result.error = repr(exc)
            return False

    def _cancel_prepared_operation(
        self, row: Dict[str, Any], result: ExtensionCleanupResult
    ) -> bool:
        """Снять подготовку до destructive-границы. False — снять не удалось.

        Вызывается из supersede-ветки. Отказ — не повод продолжить: оставить
        снимок и gate хуже, чем сохранить строку очереди на следующий цикл.
        """
        operation_id = row.get("cleanup_uid") or ""
        if not operation_id:
            return True
        services = self.bsl_services
        if services is None:
            # BSL выключен: операция могла быть подготовлена прошлым запуском.
            # Открываем ТОЛЬКО существующий sidecar, не материализуя новый.
            try:
                sqlite = _open_existing_sidecar()
                if sqlite is None:
                    return True
                if sqlite.read_config_delete_operation(self._bsl_scope(), operation_id):
                    sqlite.cancel_config_delete_operation(
                        self._bsl_scope(), operation_id,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Extension cleanup: cannot cancel prepared BSL operation %s",
                    operation_id,
                )
                result.error = repr(exc)
                return False
            return True
        try:
            if services.sqlite.read_config_delete_operation(
                services.scope, operation_id,
            ):
                services.delta_applier.cancel_config_delete(
                    services.scope, operation_id,
                )
                logger.info(
                    "Extension cleanup: prepared BSL operation %s cancelled "
                    "(supersede before destructive boundary)", operation_id,
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Extension cleanup: cannot cancel prepared BSL operation %s",
                operation_id,
            )
            result.error = repr(exc)
            return False
        return True

    def _stage_prepare_bsl_delete(
        self, row: Dict[str, Any], result: ExtensionCleanupResult
    ) -> bool:
        """Durable-снимок обратного вклада ДО удаления графа.

        Возвращает True, если можно двигаться дальше (в том числе когда
        scoped-путь сознательно не выбран и удаление пойдёт legacy-маршрутом).
        False — только для отложенных состояний, где граф трогать нельзя.

        Пропуск для legacy-строк (`cleanup_uid = ''`) обязателен: они могли
        быть созданы версией без подготовки и уже пересечь границу, а строить
        снимок по исчезнувшему графу невозможно.
        """
        from graphdb.bsl_code_search_delta import ConfigDeletePrepareOutcome

        operation_id = row.get("cleanup_uid") or ""
        if not operation_id:
            return self._set_bsl_mode(
                row, result, CLEANUP_BSL_MODE_LEGACY_PURGE,
                fallback_reason="legacy_queue_row", full_phase_a=True,
            )
        if not self._bsl_enabled():
            return self._set_bsl_mode(row, result, CLEANUP_BSL_MODE_DISABLED)

        services = self.bsl_services
        if services is None:
            logger.warning(
                "Extension cleanup: BSL enabled but services unavailable; "
                "falling back to legacy purge for %s", row["config_qn"],
            )
            return self._set_bsl_mode(
                row, result, CLEANUP_BSL_MODE_LEGACY_PURGE,
                fallback_reason="bsl_services_unavailable", full_phase_a=True,
            )

        preparation = services.delta_applier.prepare_config_delete(
            services.scope, row["config_name"], operation_id,
            scoped_enabled=self._scoped_delete_enabled(),
            lease=self.lease,
        )
        result.bsl_operation_id = operation_id
        result.bsl_routines_prepared = preparation.routines_prepared
        outcome = preparation.outcome

        if outcome in (
            ConfigDeletePrepareOutcome.PREPARED, ConfigDeletePrepareOutcome.ADOPTED,
        ):
            # Стадию продвигаем ТОЛЬКО после успешной записи маршрута: иначе
            # рестарт увидел бы пройденную подготовку с пустым `bsl_mode` и
            # пропустил бы вычитание, а legacy purge вечно упирался бы в
            # собственную же активную операцию.
            if not self._set_bsl_mode(row, result, CLEANUP_BSL_MODE_SCOPED_DELTA):
                return False
            self.state.advance_extension_cleanup_stage(
                row["source_scope"], row["config_qn"], CLEANUP_STAGE_BSL_DELETE_PREPARED,
            )
            return True
        if outcome == ConfigDeletePrepareOutcome.ABSENT:
            # Конфигурации в serving epoch нет: вычитать нечего, перестройку
            # требовать не за что. Граф удаляем, residual только верифицирует.
            if not self._set_bsl_mode(row, result, CLEANUP_BSL_MODE_ABSENT):
                return False
            self.state.advance_extension_cleanup_stage(
                row["source_scope"], row["config_qn"], CLEANUP_STAGE_BSL_DELETE_PREPARED,
            )
            return True
        if outcome == ConfigDeletePrepareOutcome.DEFERRED:
            logger.info(
                "Extension cleanup: BSL preparation deferred for %s (%s); "
                "graph untouched", row["config_qn"], preparation.detail,
            )
            self._set_bsl_mode(row, result, CLEANUP_BSL_MODE_DEFERRED)
            result.bsl_outcome = "deferred"
            return False
        if outcome == ConfigDeletePrepareOutcome.FALLBACK:
            logger.warning(
                "Extension cleanup: scoped BSL delete not possible for %s "
                "(reason=%s, %s); legacy purge with full rebuild",
                row["config_qn"], preparation.fallback_reason, preparation.detail,
            )
            if not self._set_bsl_mode(
                row, result, CLEANUP_BSL_MODE_LEGACY_PURGE,
                fallback_reason=preparation.fallback_reason or "unspecified",
                full_phase_a=True,
            ):
                return False
            self.state.advance_extension_cleanup_stage(
                row["source_scope"], row["config_qn"], CLEANUP_STAGE_BSL_DELETE_PREPARED,
            )
            return True
        # FAILED — временная проблема; граф не трогаем, retry следующим циклом.
        logger.error(
            "Extension cleanup: BSL preparation failed for %s (%s)",
            row["config_qn"], preparation.detail,
        )
        result.bsl_outcome = "prepare_failed"
        result.error = preparation.detail or "bsl preparation failed"
        return False

    def _resolve_bsl_route(
        self, row: Dict[str, Any], result: ExtensionCleanupResult, operation_id: str,
    ) -> str:
        """Durable маршрут стадии применения.

        Колонка `bsl_mode` пишется отдельной транзакцией от стадии, поэтому
        сама операция в BSL SQLite — более надёжный источник: если она
        существует, маршрут определяется её состоянием, а не тем, успела ли
        записаться колонка. Без этого сбой одной записи оставлял бы durable
        стадию и durable маршрут рассогласованными, и подготовленный снимок
        никогда бы не применился.
        """
        mode = result.bsl_mode or row.get("bsl_mode") or ""
        services = self.bsl_services
        if not operation_id or services is None:
            return mode
        if mode in (
            CLEANUP_BSL_MODE_SCOPED_DELTA, CLEANUP_BSL_MODE_FULL_REBUILD_RECOVERY,
        ):
            return mode
        try:
            header = services.sqlite.read_config_delete_operation(
                services.scope, operation_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Extension cleanup: cannot read config delete operation %s",
                operation_id,
            )
            return mode
        if header is None:
            return mode
        recovered = (
            CLEANUP_BSL_MODE_FULL_REBUILD_RECOVERY
            if header["state"] == "fallback_rebuild_required"
            else CLEANUP_BSL_MODE_SCOPED_DELTA
        )
        logger.warning(
            "Extension cleanup: durable bsl_mode was %r but operation %s exists "
            "(state=%s); recovering route as %s",
            mode, operation_id, header["state"], recovered,
        )
        self._set_bsl_mode(
            row, result, recovered,
            fallback_reason=header.get("fallback_reason") or "",
            full_phase_a=(recovered == CLEANUP_BSL_MODE_FULL_REBUILD_RECOVERY),
        )
        return recovered

    def _await_full_rebuild_recovery(
        self, row: Dict[str, Any], result: ExtensionCleanupResult, operation_id: str,
    ) -> bool:
        """Дождаться новой serving epoch и закрыть операцию.

        Владельцем восстановления стал полный rebuild. Пока он не опубликовал
        новую epoch, gate обязан держаться (строки расширения ещё лежат в
        старой), а очередь — ждать. После смены epoch операция устарела:
        rebuild строил уже из графа без расширения, поэтому вычитать нечего, но
        снять операцию и gate обязаны мы — `commit_pending` scoped-флаги не
        трогает, и за нас этого не сделает никто.
        """
        services = self.bsl_services
        if services is None:
            return self._recover_bsl_disabled(row, result)
        try:
            header = services.sqlite.read_config_delete_operation(
                services.scope, operation_id,
            )
            current_epoch = services.sqlite.get_current_epoch(services.scope)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Extension cleanup: cannot read operation %s during rebuild wait",
                operation_id,
            )
            result.error = repr(exc)
            return False

        if header is None:
            self.state.advance_extension_cleanup_stage(
                row["source_scope"], row["config_qn"], CLEANUP_STAGE_BSL_DELTA_APPLIED,
            )
            return True
        if current_epoch is None or int(current_epoch) == int(header["index_epoch"]):
            # Ждать можно только пока обязательство перестроить индекс реально
            # существует: без него ветка не имеет выхода — операция уже не даёт
            # `CONFIG_DELETE_IN_PROGRESS`, а сменить epoch некому. Переставляем
            # флаг, если он потерян (сбой записи, откат из прошлой сборки).
            try:
                fingerprint = services.sqlite.read_fingerprint(services.scope) or {}
                if not int(fingerprint.get("reindex_requested") or 0):
                    logger.warning(
                        "Extension cleanup: operation %s awaits a rebuild but the "
                        "obligation is missing — re-arming reindex_requested",
                        operation_id,
                    )
                    services.sqlite.request_reindex(services.scope)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Extension cleanup: cannot re-arm rebuild obligation for %s",
                    operation_id,
                )
                result.error = repr(exc)
                return False
            logger.info(
                "Extension cleanup: waiting for full rebuild to publish a new "
                "epoch before closing operation %s (epoch=%s)",
                operation_id, current_epoch,
            )
            result.bsl_outcome = "awaiting_full_rebuild"
            return False

        logger.info(
            "Extension cleanup: rebuild published epoch %s (operation %s was "
            "built for %s) — dropping operation and releasing gate",
            current_epoch, operation_id, header["index_epoch"],
        )
        try:
            services.delta_applier.abandon_config_delete(services.scope, operation_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Extension cleanup: cannot drop obsolete operation %s", operation_id,
            )
            result.error = repr(exc)
            return False
        result.bsl_outcome = "obsolete_epoch_rebuilt"
        self.state.advance_extension_cleanup_stage(
            row["source_scope"], row["config_qn"], CLEANUP_STAGE_BSL_DELTA_APPLIED,
        )
        return True

    def _stage_apply_bsl_delta(
        self, row: Dict[str, Any], result: ExtensionCleanupResult
    ) -> bool:
        """Вычесть подготовленный вклад и закоммитить ту же serving epoch."""
        from graphdb.bsl_code_search_delta import ConfigDeleteApplyOutcome

        operation_id = row.get("cleanup_uid") or ""
        mode = self._resolve_bsl_route(row, result, operation_id)

        if mode == CLEANUP_BSL_MODE_FULL_REBUILD_RECOVERY:
            return self._await_full_rebuild_recovery(row, result, operation_id)

        if mode != CLEANUP_BSL_MODE_SCOPED_DELTA or not operation_id:
            # Legacy/absent/disabled: вычитать нечего, стадию просто проходим.
            self.state.advance_extension_cleanup_stage(
                row["source_scope"], row["config_qn"], CLEANUP_STAGE_BSL_DELTA_APPLIED,
            )
            return True

        services = self.bsl_services
        if services is None:
            # BSL выключили между циклами, а операция уже за границей: закрыть
            # её обязан recovery-accessor, иначе очередь и gate зависнут.
            return self._recover_bsl_disabled(row, result)

        apply_result = services.delta_applier.apply_config_delete(
            services.scope, operation_id, lease=self.lease,
        )
        result.bsl_routines_applied = apply_result.routines_applied
        result.bsl_corpus_docs_decremented = apply_result.corpus_docs_decremented
        result.bsl_outcome = apply_result.outcome.value

        if apply_result.outcome == ConfigDeleteApplyOutcome.APPLIED:
            self.state.advance_extension_cleanup_stage(
                row["source_scope"], row["config_qn"], CLEANUP_STAGE_BSL_DELTA_APPLIED,
            )
            return True
        if apply_result.outcome == ConfigDeleteApplyOutcome.OBSOLETE_EPOCH_REBUILT:
            # Полный rebuild уже построил эпоху из графа без расширения:
            # вычитать нечего, но операцию и gate обязаны снять мы.
            logger.info(
                "Extension cleanup: BSL operation %s obsolete (epoch rebuilt); "
                "dropping operation and releasing gate", operation_id,
            )
            services.delta_applier.abandon_config_delete(services.scope, operation_id)
            self._set_bsl_mode(
                row, result, CLEANUP_BSL_MODE_FULL_REBUILD_RECOVERY,
                fallback_reason="obsolete_epoch_rebuilt", full_phase_a=True,
            )
            self.state.advance_extension_cleanup_stage(
                row["source_scope"], row["config_qn"], CLEANUP_STAGE_BSL_DELTA_APPLIED,
            )
            return True
        if apply_result.outcome == ConfigDeleteApplyOutcome.FALLBACK_REBUILD_REQUIRED:
            # Владельцем восстановления стал полный rebuild. Gate остаётся
            # поднятым до публикации новой epoch, очередь ждёт её.
            logger.error(
                "Extension cleanup: BSL operation %s handed over to full rebuild "
                "(reason=%s)", operation_id, apply_result.fallback_reason,
            )
            self._set_bsl_mode(
                row, result, CLEANUP_BSL_MODE_FULL_REBUILD_RECOVERY,
                fallback_reason=apply_result.fallback_reason or "unspecified",
                full_phase_a=True,
            )
            return False
        logger.info(
            "Extension cleanup: BSL delta deferred for %s (%s)",
            row["config_qn"], apply_result.detail,
        )
        return False

    def _recover_bsl_disabled(
        self, row: Dict[str, Any], result: ExtensionCleanupResult
    ) -> bool:
        """Закрыть операцию за destructive-границей при выключенном BSL.

        Без этого получается несовместимость двух правил: bundle при
        выключенной подсистеме не поднимается, а операция и reader gate обязаны
        быть сняты. Gate переживает даже последующий полный rebuild
        (`commit_pending` его не трогает), поэтому оставить его нельзя.
        """
        operation_id = row["cleanup_uid"]
        sqlite = _open_existing_sidecar()
        if sqlite is None:
            self._set_bsl_mode(row, result, CLEANUP_BSL_MODE_ABSENT)
            self.state.advance_extension_cleanup_stage(
                row["source_scope"], row["config_qn"], CLEANUP_STAGE_BSL_DELTA_APPLIED,
            )
            return True
        scope = self._bsl_scope()
        try:
            epoch = sqlite.get_current_epoch(scope) or 0
            stats = sqlite.recover_config_delete_operation_disabled(
                scope, operation_id, row["config_name"], int(epoch),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Extension cleanup: disabled-BSL recovery failed for %s", operation_id,
            )
            result.error = repr(exc)
            return False
        legacy = stats.get("mode") == "legacy_purge"
        result.bsl_outcome = f"disabled_recovery:{stats.get('mode')}"
        result.bsl_routines_purged = int(stats.get("routines", 0) or 0)
        self._set_bsl_mode(
            row, result,
            CLEANUP_BSL_MODE_LEGACY_PURGE if legacy else CLEANUP_BSL_MODE_DISABLED,
            fallback_reason="bsl_disabled_after_boundary" if legacy else "",
            full_phase_a=legacy,
        )
        self.state.advance_extension_cleanup_stage(
            row["source_scope"], row["config_qn"], CLEANUP_STAGE_BSL_DELTA_APPLIED,
        )
        return True

    def _stage_delete_graph(
        self, row: Dict[str, Any], result: ExtensionCleanupResult
    ) -> None:
        source_scope = row["source_scope"]
        config_qn = row["config_qn"]
        # Durable-флаги ДО первого DETACH DELETE: первый батч может упасть, и
        # признак «граф уже начали ломать» обязан это пережить.
        self.state.mark_extension_cleanup_graph_mutation_started(source_scope, config_qn)
        result.graph_refresh_required = True

        stats = self.loader.delete_configuration_scope(
            project_name=self.state.project_name,
            config_name=row["config_name"],
            config_qn=config_qn,
            lease=self.lease,
        )
        result.neo4j_nodes_deleted = int(getattr(stats, "total_deleted", 0) or 0)
        self.state.advance_extension_cleanup_stage(
            source_scope, config_qn, CLEANUP_STAGE_GRAPH_DELETED,
        )

    def _stage_quarantine_summaries(
        self, row: Dict[str, Any], result: ExtensionCleanupResult
    ) -> None:
        from object_summary import storage as os_storage

        moved = os_storage.quarantine_configuration(row["config_name"])
        result.summaries_quarantined = moved is not None
        self.state.advance_extension_cleanup_stage(
            row["source_scope"], row["config_qn"], CLEANUP_STAGE_SUMMARIES_QUARANTINED,
        )

    def _stage_purge_bsl(
        self, row: Dict[str, Any], result: ExtensionCleanupResult
    ) -> bool:
        """Вернуть True, если стадию можно продвинуть.

        Для scoped-режима это residual: housekeeping, который НЕ трогает
        corpus counters и `reindex_requested`. Отдельная стадия нужна ровно
        затем, чтобы падение после вычитания повторяло только housekeeping и не
        вычитало вклад второй раз.

        Для legacy/fallback режимов — прежний `purge_config_scope` с полной
        перестройкой.
        """
        from graphdb.bsl_code_search_delta import purge_config_scope

        mode = result.bsl_mode or row.get("bsl_mode") or ""
        operation_id = row.get("cleanup_uid") or ""
        services = self.bsl_services

        if mode == CLEANUP_BSL_MODE_SCOPED_DELTA and operation_id and services is not None:
            try:
                stats = services.delta_applier.finalize_config_delete(
                    services.scope, operation_id, row["config_name"],
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Extension cleanup: residual cleanup failed for %s",
                    row["config_qn"],
                )
                result.error = repr(exc)
                result.bsl_outcome = "residual_failed"
                return False
            result.bsl_outcome = "scoped_residual_done"
            result.bsl_routines_purged = result.bsl_routines_applied
            logger.info(
                "Extension cleanup: residual done for %s: %s",
                row["config_qn"], stats,
            )
            self.state.advance_extension_cleanup_stage(
                row["source_scope"], row["config_qn"], CLEANUP_STAGE_BSL_PURGED,
            )
            return True

        if (
            mode in (CLEANUP_BSL_MODE_ABSENT, CLEANUP_BSL_MODE_FULL_REBUILD_RECOVERY)
            and operation_id
            and services is not None
        ):
            # Вычитать нечего: конфигурации не было в serving epoch либо новую
            # epoch уже построил rebuild из графа без расширения. Но пройти
            # мимо молча нельзя — residual с пустым множеством выполняет ту же
            # verification и упадёт, если строки конфигурации всё-таки есть.
            # Полную перестройку здесь требовать не за что.
            try:
                epoch = services.sqlite.get_current_epoch(services.scope) or 0
                services.sqlite.finalize_config_delete_residuals(
                    services.scope, int(epoch), row["config_name"], [],
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Extension cleanup: residual verification failed for %s",
                    row["config_qn"],
                )
                result.error = repr(exc)
                result.bsl_outcome = "residual_verification_failed"
                return False
            self.state.advance_extension_cleanup_stage(
                row["source_scope"], row["config_qn"], CLEANUP_STAGE_BSL_PURGED,
            )
            result.bsl_outcome = result.bsl_outcome or "absent"
            return True

        # Прежний путь. Сюда попадают legacy-строки, fallback-режимы и
        # выключенный BSL: последний обязан остаться терминальным
        # `purge_config_scope`, который сам открывает только существующий
        # sidecar, физически удаляет строки конфигурации и фиксирует
        # обязательство перестроить индекс при будущем включении.

        purge = purge_config_scope(
            self._bsl_scope(),
            row["config_name"],
            bsl_enabled=self._bsl_enabled(),
        )
        result.bsl_outcome = purge.outcome.value
        result.bsl_routines_purged = purge.routines_purged
        if purge.reindex_requested:
            result.bsl_full_phase_a_required = True
            if not result.bsl_fallback_reason:
                result.bsl_fallback_reason = "legacy_purge"
        if not purge.stage_may_advance:
            logger.info(
                "Extension cleanup BSL stage deferred: scope=%s config=%s detail=%s",
                row["source_scope"], row["config_qn"], purge.detail,
            )
            return False
        self.state.advance_extension_cleanup_stage(
            row["source_scope"], row["config_qn"], CLEANUP_STAGE_BSL_PURGED,
        )
        return True

    def _stage_finalize(
        self, row: Dict[str, Any], result: ExtensionCleanupResult
    ) -> None:
        source_scope = row["source_scope"]
        config_qn = row["config_qn"]
        deleted_rows = self.state.delete_scope(source_scope)
        if self.state.scope_has_any_state(source_scope):
            raise RuntimeError(
                f"extension scope state for {source_scope!r} still present after delete_scope"
            )
        self.state.advance_extension_cleanup_stage(
            source_scope, config_qn, CLEANUP_STAGE_FINALIZED,
        )
        self.state.delete_extension_cleanup(source_scope, config_qn)
        self.invalidate_blocked_cache()
        # Граница поколений держится до конца цикла, а не до конца этой функции.
        # Строка очереди — retry identity, и её удаление стёрло бы факт, что
        # цикл начинался с destructive recovery: вернувшийся каталог загрузился
        # бы тем же циклом, смешав работу двух поколений.
        #
        # Rename — исключение и единственный случай, когда загрузка в том же
        # цикле обязательна: там снос старого поколения существует ровно ради
        # немедленной загрузки нового, и паритет с прежним синхронным
        # поведением требует уложиться в один цикл.
        if row["reason"] != CLEANUP_REASON_CONFIGURATION_RENAMED:
            self.mark_destroyed_this_cycle(source_scope)
        result.finalized = True
        logger.info(
            "Extension scope removed: scope=%s config=%s nodes=%d bsl=%s state_rows=%s",
            source_scope, config_qn, result.neo4j_nodes_deleted,
            result.bsl_outcome, deleted_rows,
        )
