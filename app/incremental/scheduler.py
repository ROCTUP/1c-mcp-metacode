"""
IncrementalLoadingScheduler — daemon thread, owning incremental lifecycle.

Поведение:
1. Source mismatch detection (before any cycle).
2. First cycle — сразу (one-shot at startup).
3. Subsequent cycles — каждые INCREMENTAL_LOADING_INTERVAL_MINUTES (если schedule_enabled).
4. SQLite cooperative lock между concurrent runs.
5. Post-sync metadata embedding re-pass: VectorIndexer.run_metadata_descriptions_pass(),
   если added_qns ∪ changed_qns_with_invalidated_embedding не пуст.

Ownership rule: scheduler — единственный владелец long-lived incremental lifecycle.
main.py отвечает только за full reload / bootstrap empty / baseline init.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from graphdb.embedding_service import EmbeddingAvailability

from .metadata_sync import MetadataIncrementalSync
from .report import IncrementalReport
from .state import ArtifactBaselineReadiness, IncrementalLoadingState, LockLease
from .xml_walker import (
    BaseImpact,
    _within_full_reconcile_window,
    xml_full_scan_run,
    xml_full_scan_run_extensions,
    xml_incremental_run_extensions,
)
from .artifact_sync import (
    ArtifactSync,
    CodeArtifactCycleContext,
    PostLinkingSync,
    BslCodeSearchSync,
)

logger = logging.getLogger(__name__)


SCHEDULER_LOCK_NAME = "incremental_main"


def _cycle_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {sec}s"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def _log_cycle_boundary(kind: str, **fields: Any) -> None:
    details = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    logger.info(
        "========== INCREMENTAL CYCLE %s %s==========",
        kind,
        f"{details} " if details else "",
    )


class IncrementalLoadingScheduler(threading.Thread):
    """Daemon thread."""

    def __init__(
        self,
        *,
        loader: Any,
        settings_obj: Any,
        state_path: Path,
        stop_event: Optional[threading.Event] = None,
        run_first_cycle: bool = True,
        last_full_scan_at: Optional[float] = None,
    ) -> None:
        super().__init__(name="IncrementalLoadingScheduler", daemon=True)
        self.loader = loader
        self.settings_obj = settings_obj
        self.state_path = state_path
        self.stop_event = stop_event or threading.Event()
        self.owner = f"pid:{os.getpid()}/tid:{threading.get_ident()}"
        self._last_full_scan_at: Optional[float] = last_full_scan_at
        self.run_first_cycle = run_first_cycle

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        try:
            self._main_loop()
        except Exception:
            logger.exception("IncrementalLoadingScheduler crashed")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _main_loop(self) -> None:
        state = IncrementalLoadingState(self.state_path, self.settings_obj.project_name)

        # Source mismatch detection.
        if not self._detect_and_validate_source(state):
            state.close()
            return

        # First cycle — immediately, если caller не подавил его (startup one-shot
        # уже отработал в run_server до старта scheduler-а).
        if self.run_first_cycle:
            self._cycle(state)

        # Subsequent cycles only if schedule_enabled.
        if not getattr(self.settings_obj, "incremental_loading_schedule_enabled", False):
            state.close()
            return

        interval_sec = max(
            60, int(self.settings_obj.incremental_loading_interval_minutes * 60)
        )
        while not self.stop_event.wait(interval_sec):
            self._cycle(state)

        state.close()

    # ------------------------------------------------------------------
    # Source mismatch
    # ------------------------------------------------------------------

    def _detect_and_validate_source(self, state: IncrementalLoadingState) -> bool:
        """Единый барьер готовности baseline перед циклом (defense-in-depth).

        Делегирует в `evaluate_artifact_baseline_readiness` — тот же владелец политики,
        что и main-preflight, поэтому source/baseline/artifact-семантика не расходится.
        READY → допускать; FULL_RELOAD_REQUIRED / SOURCE_MISMATCH /
        BASELINE_NOT_INITIALIZED → abort (fail-closed).
        """
        current_source = getattr(self.settings_obj, "metadata_source", "txt")
        readiness = state.evaluate_artifact_baseline_readiness(current_source)
        if readiness == ArtifactBaselineReadiness.READY:
            return True
        if readiness == ArtifactBaselineReadiness.BASELINE_NOT_INITIALIZED:
            logger.error(
                "Incremental loading aborted: baseline not initialized. "
                "Run FULL_METADATA_RELOAD=true once."
            )
        elif readiness == ArtifactBaselineReadiness.SOURCE_MISMATCH:
            other = "xml" if current_source == "txt" else "txt"
            other_stored = state.get_stage_source_type(f"metadata_{other}")
            logger.error(
                "Incremental loading aborted: METADATA_SOURCE changed from %s to %s. "
                "Run FULL_METADATA_RELOAD=true to switch source.",
                other_stored or other,
                current_source,
            )
        else:  # FULL_RELOAD_REQUIRED
            logger.error(
                "Incremental loading aborted: artifact baseline incomplete "
                "(completion stage missing). Run FULL_METADATA_RELOAD=true once."
            )
        return False

    # ------------------------------------------------------------------
    # Cycle
    # ------------------------------------------------------------------

    def _cycle(
        self,
        state: IncrementalLoadingState,
        *,
        include_xml_full_scan: bool = True,
        include_post_sync_embedding: bool = True,
        use_startup_probe_for_vectors: bool = False,
        embedding_availability: Optional["EmbeddingAvailability"] = None,
    ) -> bool:
        """Return True if cycle actually ran (lock acquired and sync executed).
        Return False if skipped — stop_event set or lock held by concurrent worker."""
        if self.stop_event.is_set():
            return False
        cycle_id = _cycle_id()
        cycle_started_at = time.monotonic()
        source = getattr(self.settings_obj, "metadata_source", "txt")
        mode = "scheduled" if include_post_sync_embedding else "startup"

        # Acquire lock. Startup-режим с включённым takeover перехватывает свежий lock
        # без ожидания stale_after; scheduled-режим уважает lock как раньше.
        takeover_enabled = bool(
            getattr(self.settings_obj, "incremental_startup_lock_takeover", False)
        )
        if mode == "startup" and takeover_enabled:
            prev = state.takeover_lock(SCHEDULER_LOCK_NAME, self.owner)
            if prev is not None:
                age = int(time.time()) - int(prev["heartbeat_at"])
                logger.warning(
                    "Startup takeover scheduler_lock: prev_owner=%s acquired_at=%s "
                    "heartbeat_age=%ss",
                    prev["owner"], prev["acquired_at"], age,
                )
        else:
            stale_after = max(
                300, 2 * 60 * int(self.settings_obj.incremental_loading_interval_minutes)
            )
            if not state.try_acquire_lock(SCHEDULER_LOCK_NAME, self.owner, stale_after):
                _log_cycle_boundary(
                    "SKIP",
                    id=cycle_id,
                    mode=mode,
                    source=source,
                    reason="lock_held",
                )
                return False
        _log_cycle_boundary(
            "START",
            id=cycle_id,
            mode=mode,
            source=source,
            project=self.settings_obj.project_name,
        )
        lease = LockLease(state, SCHEDULER_LOCK_NAME, self.owner)
        embedding_repass_needed: set = set()
        routine_doc_ids: set = set()
        stats_refresh_needed = False
        try:
            sync = MetadataIncrementalSync(
                self.loader, state,
                use_startup_probe_for_vectors=use_startup_probe_for_vectors,
            )
            report = sync.run(self.settings_obj)
            embedding_repass_needed |= report.embedding_repass_needed_qns
            lease.heartbeat()

            # XML-специфичная часть: base full-scan → BaseImpact → extension cycle.
            # TXT: sync.run() уже сделал base + extensions внутри _sync_txt_extensions_impl.
            if getattr(self.settings_obj, "metadata_source", "txt") == "xml":
                self._run_xml_extensions_phase(
                    state=state,
                    report=report,
                    include_xml_full_scan=include_xml_full_scan,
                    embedding_repass_needed=embedding_repass_needed,
                )

            # Phase 2-4: artifacts + post-linking. Запускаются ВНУТРИ scheduler_lock
            # после metadata sync. Heartbeat поддерживается через lease.
            metadata_qns, routine_doc_ids, artifact_graph_changed = self._run_artifact_phases(
                state=state, report=report, lease=lease,
                embedding_availability=embedding_availability,
            )
            embedding_repass_needed |= metadata_qns
            stats_refresh_needed = report.has_graph_changes or artifact_graph_changed

            for line in report.detailed_summary_lines():
                logger.info("Incremental cycle complete: %s", line)
        except Exception:
            _log_cycle_boundary(
                "FAILED",
                id=cycle_id,
                mode=mode,
                source=source,
                duration=_format_duration(time.monotonic() - cycle_started_at),
            )
            raise
        finally:
            state.release_lock(SCHEDULER_LOCK_NAME, self.owner)

        # Post-sync embedding re-passes (without scheduler lock).
        if include_post_sync_embedding:
            if (
                routine_doc_ids
                and getattr(self.settings_obj, "enable_routine_description_embedding", False)
            ):
                self._run_post_sync_routine_embedding_pass()
            if (
                embedding_repass_needed
                and getattr(self.settings_obj, "enable_metadata_description_embedding", False)
            ):
                self._run_post_sync_embedding_pass()

            # Актуализация статистики Web Console после успешного scheduled cycle,
            # изменившего Neo4j-граф. Только scheduled-режим: startup one-shot
            # (include_post_sync_embedding=False) не рефрешит — финальный refresh делает
            # post-bootstrap readiness barrier. Ошибка refresh проглатывается внутри и
            # не роняет cycle.
            if stats_refresh_needed and getattr(self.settings_obj, "web_console_enabled", False):
                try:
                    from console.cache import refresh_console_stats_cache
                    refresh_console_stats_cache(
                        source="scheduled_incremental", block=True, raise_on_error=False,
                    )
                except Exception:
                    logger.exception("Console stats refresh (scheduled_incremental) failed")

        _log_cycle_boundary(
            "END",
            id=cycle_id,
            mode=mode,
            source=source,
            duration=_format_duration(time.monotonic() - cycle_started_at),
        )
        return True

    def _run_xml_extensions_phase(
        self,
        *,
        state: IncrementalLoadingState,
        report: IncrementalReport,
        include_xml_full_scan: bool,
        embedding_repass_needed: set,
    ) -> None:
        """XML-цикл порядок:
        1. (уже сделано sync.sync_xml) base sync — собрал added/changed/configuration_changed в report.
        2. base full-scan (xml_full_scan_run) условно — добавляет deleted в report.
        3. build BaseImpact (без base_configuration — overlay через provider у context).
        4. xml_incremental_run_extensions(base_impact, xml_context) — scoped overlay + extension changed-files.
        5. xml_full_scan_run_extensions(xml_context) условно.
        """
        xml_context = getattr(report, "xml_context", None)
        in_window = (
            include_xml_full_scan
            and getattr(self.settings_obj, "incremental_full_reconcile_enabled", False)
            and self._should_run_full_reconcile()
        )

        # 2. Base full-scan.
        if in_window:
            try:
                xml_full_scan_run(
                    loader=self.loader,
                    state=state,
                    settings_obj=self.settings_obj,
                    report=report,
                    xml_context=xml_context,
                )
                self._last_full_scan_at = time.time()
            except Exception:
                logger.exception("XML full scan failed")
            embedding_repass_needed |= report.embedding_repass_needed_qns

        # 3. BaseImpact (без base_configuration — overlay через xml_context.overlay_provider).
        base_impact = BaseImpact(
            added_qns=set(report.added_qns),
            changed_qns=set(report.changed_qns),
            deleted_qns=set(report.deleted_qns),
            configuration_changed=report.configuration_changed,
        )

        # 4. Extension incremental + scoped overlay.
        try:
            xml_incremental_run_extensions(
                loader=self.loader,
                state=state,
                settings_obj=self.settings_obj,
                report=report,
                base_impact=base_impact,
                xml_context=xml_context,
            )
        except Exception:
            logger.exception("XML extension incremental run failed")
        embedding_repass_needed |= report.embedding_repass_needed_qns

        # 5. Extension full-scan.
        if in_window:
            try:
                xml_full_scan_run_extensions(
                    loader=self.loader,
                    state=state,
                    settings_obj=self.settings_obj,
                    report=report,
                    xml_context=xml_context,
                )
            except Exception:
                logger.exception("XML extension full scan failed")
            embedding_repass_needed |= report.embedding_repass_needed_qns

        # Cycle stats.
        if xml_context is not None:
            stats = xml_context.overlay_provider.stats()
            logger.info(
                "xml cycle: base_scans=%d ext_scans=%d scoped_parses=%d objects=%d files=%d",
                xml_context.base_scans_count(),
                xml_context.ext_scans_count(),
                stats["scoped_parses"],
                stats["objects_parsed"],
                stats["files_parsed"],
            )

    def _run_artifact_phases(
        self,
        *,
        state: IncrementalLoadingState,
        report: IncrementalReport,
        lease: LockLease,
        embedding_availability: Optional["EmbeddingAvailability"] = None,
    ) -> tuple:
        """Phase 2 (base artifacts) → Phase 3 (extension artifacts) → Phase 4 (post-linking).

        Если artifact_manifest для всех scope пуст и phase 1 baseline уже есть,
        логируется явный WARN — пользователю нужен full reload для baseline артефактов.

        Возвращает tuple (metadata_qns, routine_doc_ids, artifact_graph_changed):
        - metadata_qns: множество MetadataObject QN с инвалидированным description_embedding.
        - routine_doc_ids: множество Routine id-ов, которым нужен doc embedding re-pass.
        - artifact_graph_changed: True, если artifact/BSL/post-linking фаза реально изменила
          counted Neo4j-граф (для актуализации статистики Web Console).
        """
        # Quick guard — пропустить, если нет ни одного artifact baseline (ни base, ни ext).
        # Это не блокирует первый цикл после full reload (тогда run_base сам создаст baseline).
        from .artifact_sync import ART_BASE_BSL  # avoid circular at import time

        has_baseline = state.has_any_artifact_baseline(ART_BASE_BSL)
        if not has_baseline and state.has_any_baseline():
            logger.warning(
                "Artifact incremental requires full reload baseline; "
                "current state has metadata phase 1 baseline but no artifact_manifest entries. "
                "Run FULL_METADATA_RELOAD=true once to bootstrap artifact baseline."
            )
            return set(), set(), False

        base_config_name = self._detect_base_config_name(state)
        if not base_config_name:
            logger.info("Phase 2-4: cannot detect base_config_name; skipping")
            return set(), set(), False

        from pathlib import Path as _Path

        # XML context может быть в report — если есть, переиспользуем code_index.
        xml_context = getattr(report, "xml_context", None)
        base_code_index = None
        ext_code_indexes: Dict[str, Any] = {}
        if xml_context is not None:
            base_code_index = xml_context.code_index
            ext_code_indexes = dict(xml_context._ext_indexes)  # type: ignore[attr-defined]
        else:
            # TXT mode: один scan code/ для базы (consumers не нужны — artifact_sync сам
            # делает diff по каждому bucket из готового index).
            try:
                from indexer.code_file_index import CodeFileIndexer

                code_dir = _Path(self.settings_obj.code_directory)
                if code_dir.exists():
                    base_code_index = CodeFileIndexer.scan(code_dir)
            except Exception:
                logger.exception("Phase 2-4: base code index scan failed")
                return set(), set(), False

        if base_code_index is None:
            logger.info("Phase 2-4: no base code index available; skipping")
            return set(), set(), False

        data_dir = getattr(self.settings_obj, "data_directory", None)
        # Parse Configuration objects — нужны form workers для `resolve_datapath_bindings`
        # → `data_bindings` rows → `BINDS_TO` edges. Без этого incremental delete+merge
        # формы теряет binding-связи с Attribute. См. [orchestrator.py:286-288] +
        # [scanner.py:_merge_form_result] для full-load параллели.
        base_configuration, ext_configurations = self._parse_configurations_for_artifacts(
            base_code_index, ext_code_indexes
        )

        context = CodeArtifactCycleContext(
            project_name=self.settings_obj.project_name,
            base_config_name=base_config_name,
            base_code_directory=_Path(self.settings_obj.code_directory),
            full_reconcile_allowed=(
                getattr(self.settings_obj, "incremental_full_reconcile_enabled", False)
                and self._should_run_full_reconcile()
            ),
            data_directory=_Path(data_dir) if data_dir is not None else None,
            base_code_index=base_code_index,
            ext_code_indexes=ext_code_indexes,
            base_configuration=base_configuration,
            ext_configurations=ext_configurations,
            source_mode=getattr(self.settings_obj, "metadata_source", "txt"),
            removed_extension_scopes=dict(report.removed_extension_scopes),
        )

        # Propagate ssl_owners_dirty из report (изменения подсистем в metadata sync)
        # в context для Phase 4.5. Reading из root report и из всех extension sub-reports.
        try:
            if report.ssl_owners_dirty:
                context.ssl_owners_dirty = True
            for sub_report in report.extension_reports.values():
                if getattr(sub_report, "ssl_owners_dirty", False):
                    context.ssl_owners_dirty = True
                    break
        except Exception:
            logger.exception("Phase 2-4: ssl_owners_dirty propagation failed")

        # State-backed known extension configs registry — заполняется до run_base.
        # Post-link consumers (CALLS / EXTENDS_ROUTINE / form-level extension
        # rebuild) читают его, а не `affected_extension_configs` (последний —
        # diagnostic поле, зависит от успешности filesystem traversal).
        source_mode = getattr(self.settings_obj, "metadata_source", "txt")
        try:
            ext_scopes = state.list_extension_scopes(source_mode)
            for scope in ext_scopes:
                ext_cfg_qn = state.get_extension_scope_config_qn(scope)
                if not ext_cfg_qn:
                    continue
                # scope формат: `{txt_ext|xml_ext}:{ext_dir_name}` — извлекаем ext_dir.
                if ":" in scope:
                    ext_dir_name = scope.split(":", 1)[1]
                else:
                    ext_dir_name = scope
                # ext_cfg_qn = ProjectName/Config$ext$ExtName → берём последний сегмент.
                ext_config_name = ext_cfg_qn.rsplit("/", 1)[-1]
                context.known_extension_configs[ext_dir_name] = ext_config_name
        except Exception:
            logger.exception("Phase 2-4: known_extension_configs build failed")

        # Aggregated post_linking_impact для Phase 4: root + всех extension_reports.
        # Existing pattern (metadata_sync, xml_walker) кладёт extension impact в
        # extension_reports[ext_dir].post_linking_impact без merge в root; agg
        # делаем здесь, чтобы PostLinkingSync видел impact из metadata phase
        # для всех configs включая расширения. Pattern reuse —
        # `embedding_repass_needed_qns` ([report.py:117-121]) делает то же без
        # flatten в root.
        try:
            context.post_linking_impact.merge(report.post_linking_impact)
            for sub_report in report.extension_reports.values():
                context.post_linking_impact.merge(sub_report.post_linking_impact)
        except Exception:
            logger.exception("Phase 2-4: post_linking_impact aggregation failed")

        # Phase 5: BSL code search delta applier injection.
        # `_apply_bsl` (phase 2/3) использует applier для scoped Neo4j+sqlite
        # invalidation code embeddings; `BslCodeSearchSync` (phase 5) применяет
        # `context.code_search_delta` поверх существующего sidecar или триггерит
        # controlled rebuild через `indexer.start_indexing(lease)`.
        # Если ENABLE_BSL_CODE_SEARCH=false — applier остаётся None, phase 5
        # тихо skip-ается, phase 2/3 не делают scoped code embedding invalidation
        # (но `clear_routine_doc_embeddings` всё равно работает — это отдельный
        # owner в incremental_loader).
        if getattr(self.settings_obj, "enable_bsl_code_search", False):
            try:
                from graphdb.bsl_code_indexer import BslCodeSearchIndexer
                from graphdb.bsl_code_search_delta import BslCodeSearchDeltaApplier

                # Construction-time ownership: the startup EmbeddingAvailability
                # is set here so every call site in this cycle (the _apply_bsl
                # SCOPED_RETRY drain and the Phase 5 BslCodeSearchSync) inherits
                # it. Scheduled cycles pass None → current production behaviour.
                _indexer = BslCodeSearchIndexer(
                    self.loader.driver, embedding_availability=embedding_availability
                )
                # The applier delegates scoped Phase B to _indexer, which already
                # carries the availability, so both the _apply_bsl drain and
                # Phase 5 inherit it through the shared indexer instance.
                context.bsl_code_search_delta_applier = BslCodeSearchDeltaApplier(
                    sqlite=_indexer.sqlite,
                    indexer=_indexer,
                )
                context.bsl_code_search_scope = _indexer.scope
                # Also expose the underlying indexer/sqlite so `_apply_bsl`
                # can capture the OLD routine context (step 4.5 snapshot+ledger)
                # without re-instantiating the indexer.
                context.bsl_code_search_indexer = _indexer
                context.bsl_code_search_sqlite = _indexer.sqlite
            except Exception:
                logger.exception("Phase 5 setup: BslCodeSearchDeltaApplier injection failed")

        artifact_sync = ArtifactSync(self.loader, state)
        try:
            artifact_sync.run_base(
                settings_obj=self.settings_obj, context=context, lease=lease
            )
        except Exception:
            logger.exception("Phase 2 (base artifacts) failed")
        lease.heartbeat()
        try:
            artifact_sync.run_extensions(
                settings_obj=self.settings_obj, context=context, lease=lease
            )
        except Exception:
            logger.exception("Phase 3 (extension artifacts) failed")
        lease.heartbeat()

        post = PostLinkingSync(self.loader, state)
        post_linking_changed = False
        try:
            post_stats = post.run(settings_obj=self.settings_obj, context=context, lease=lease)
            # stage-2 idempotent EXTENDS_* repair мог создать counted Relationships без
            # свежего diff'а — фактически созданные рёбра сигналят об изменении графа.
            post_linking_changed = bool((post_stats or {}).get("extends_relationships_created", 0))
        except Exception:
            logger.exception("Phase 4 (post-linking) failed")
        lease.heartbeat()

        # Phase 4.5: SSL API marker refresh.
        # Scoped — для affected routines из BSL apply (body/doc/signature/line/added).
        # Project-wide — когда incremental изменения затронули подсистемы
        # СтандартныеПодсистемы (поднимается флаг context.ssl_owners_dirty).
        try:
            with self.loader.driver.session(
                database=self.settings_obj.neo4j_database
            ) as session:
                if context.ssl_owners_dirty:
                    self.loader.refresh_ssl_api_for_project(
                        session, self.settings_obj.project_name
                    )
                elif context.affected_routines:
                    self.loader.refresh_ssl_api_for_routines(
                        session,
                        self.settings_obj.project_name,
                        context.affected_routines,
                    )
        except Exception:
            logger.exception("Phase 4.5 (SSL API refresh) failed")
        lease.heartbeat()

        # Phase 5: BSL code search delta apply (sidecar sync). Отдельная phase —
        # `PostLinkingSync` владеет graph relinking, а BSL code search lifecycle
        # — отдельная subsystem (Neo4j search nodes + sqlite + epoch/vector
        # status). PostLinkingSync только заполняет `context.code_search_delta`
        # (это делает уже `_apply_bsl`).
        code_search_sync = BslCodeSearchSync(state, self.loader)
        bsl_changed = False
        try:
            bsl_changed = code_search_sync.run(context=context, lease=lease)
        except Exception:
            logger.exception("Phase 5 (BSL code search sync) failed")

        removed_extensions_changed = False
        if context.removed_extension_scopes:
            try:
                removed_extensions_changed = bool(
                    artifact_sync.finalize_removed_extensions(
                        settings_obj=self.settings_obj,
                        context=context,
                        report=report,
                        lease=lease,
                    )
                )
            except Exception as exc:
                logger.exception("Extension removal finalization failed")
                report.errors.append(
                    f"extension removal finalization failed: {exc!r}"
                )

        artifact_graph_changed = (
            context.graph_changed()
            or bool(bsl_changed)
            or bool(post_linking_changed)
            or removed_extensions_changed
        )
        return (
            set(context.metadata_embedding_repass_qns),
            set(context.routine_doc_embedding_repass_ids),
            artifact_graph_changed,
        )

    def _parse_configurations_for_artifacts(
        self,
        base_code_index: Any,
        ext_code_indexes: Dict[str, Any],
    ) -> tuple:
        """Парсит base + extension Configurations для подачи в form workers.

        Использует тот же `MetadataLoader.load_configurations` путь, что full-load
        ([orchestrator.py](app/indexer/orchestrator.py)). Падения логируются — phase 2/3
        продолжатся с None и BINDS_TO ребра не пересоздадутся (graceful degradation,
        но это лучше, чем дроп всего цикла).
        """
        base_cfg: Any = None
        ext_cfgs: Dict[str, Any] = {}
        source = getattr(self.settings_obj, "metadata_source", "txt")
        try:
            from indexer.metadata_loader import MetadataLoader

            ml = MetadataLoader()
            if source == "xml":
                from pathlib import Path as _P

                code_dir = _P(self.settings_obj.code_directory)
                configs = ml.load_configurations(
                    code_dir, code_index=base_code_index, source="xml"
                )
                if configs:
                    base_cfg = configs[0]
            else:
                metadata_dir = getattr(self.settings_obj, "metadata_directory", None)
                if metadata_dir is not None:
                    configs = ml.load_configurations(metadata_dir, source="txt")
                    if configs:
                        base_cfg = configs[0]
        except Exception:
            logger.exception("Phase 2-4: base Configuration parse failed")

        # Extensions.
        try:
            from indexer.metadata_loader import MetadataLoader
            from pathlib import Path as _P

            ml_ext = MetadataLoader()
            extensions_dir = getattr(self.settings_obj, "extensions_directory", None)
            if extensions_dir is not None and extensions_dir.exists():
                for ext_dir_name, ext_idx in ext_code_indexes.items():
                    ext_dir = extensions_dir / ext_dir_name
                    if source == "xml":
                        ext_code_dir = ext_dir / "code"
                        if ext_code_dir.exists():
                            try:
                                configs = ml_ext.load_configurations(
                                    ext_code_dir, code_index=ext_idx, source="xml",
                                    is_extension=True,
                                )
                                if configs:
                                    ext_cfgs[ext_dir_name] = configs[0]
                            except Exception:
                                logger.exception(
                                    "Phase 2-4: ext=%s Configuration parse failed (XML)",
                                    ext_dir_name,
                                )
                    else:
                        ext_meta_dir = ext_dir / "metadata"
                        if ext_meta_dir.exists():
                            try:
                                configs = ml_ext.load_configurations(
                                    ext_meta_dir, source="txt", is_extension=True,
                                )
                                if configs:
                                    ext_cfgs[ext_dir_name] = configs[0]
                            except Exception:
                                logger.exception(
                                    "Phase 2-4: ext=%s Configuration parse failed (TXT)",
                                    ext_dir_name,
                                )
        except Exception:
            logger.exception("Phase 2-4: extension Configuration parse failed")

        return base_cfg, ext_cfgs

    @staticmethod
    def _detect_base_config_name(state: IncrementalLoadingState) -> str:
        """Best-effort: имя базовой конфигурации из configuration_state."""
        try:
            conn = state._connect()
            for source_type in ("xml", "txt"):
                row = conn.execute(
                    "SELECT configuration_qn FROM configuration_state "
                    "WHERE project_name=? AND source_type=? LIMIT 1",
                    (state.project_name, source_type),
                ).fetchone()
                if row and row[0]:
                    full = row[0]
                    prefix = f"{state.project_name}/"
                    return full[len(prefix):] if full.startswith(prefix) else full
        except Exception:
            logger.exception("Phase 2-4: _detect_base_config_name failed")
        return ""

    def _should_run_full_reconcile(self) -> bool:
        # Interval check.
        if self._last_full_scan_at is not None:
            interval_hours = max(
                1, int(self.settings_obj.incremental_full_reconcile_interval_hours)
            )
            elapsed_hours = (time.time() - self._last_full_scan_at) / 3600.0
            if elapsed_hours < interval_hours:
                return False
        # Window check.
        return _within_full_reconcile_window(
            datetime.now(),
            self.settings_obj.incremental_full_reconcile_window_start,
            self.settings_obj.incremental_full_reconcile_window_end,
        )

    # ------------------------------------------------------------------
    # Embedding re-pass
    # ------------------------------------------------------------------

    def _run_post_sync_embedding_pass(self) -> None:
        try:
            from graphdb.vector_indexer import VectorIndexer
        except Exception:
            logger.warning("VectorIndexer unavailable; skipping post-sync embedding re-pass")
            return
        try:
            vi = VectorIndexer(self.loader.driver)
            # Эта обёртка добавлена в фазе 5 (vector_indexer.py).
            if hasattr(vi, "run_metadata_descriptions_pass"):
                asyncio.run(vi.run_metadata_descriptions_pass(self.settings_obj.project_name))
            else:
                logger.warning(
                    "VectorIndexer.run_metadata_descriptions_pass not implemented; "
                    "post-sync embedding re-pass skipped"
                )
        except Exception:
            logger.exception("Post-sync embedding re-pass failed")

    def _run_post_sync_routine_embedding_pass(self) -> None:
        try:
            from graphdb.vector_indexer import VectorIndexer
        except Exception:
            logger.warning("VectorIndexer unavailable; skipping routine embedding re-pass")
            return
        try:
            vi = VectorIndexer(self.loader.driver)
            asyncio.run(vi.run_routine_descriptions_pass(self.settings_obj.project_name))
        except Exception:
            logger.exception("Post-sync routine embedding re-pass failed")


def run_incremental_once(
    *,
    loader: Any,
    settings_obj: Any,
    state_path: Path,
    embedding_availability: Optional["EmbeddingAvailability"] = None,
) -> tuple[bool, Optional[float], bool]:
    """Синхронный startup-цикл инкрементальной загрузки.

    open state -> validate source -> один _cycle под локом -> close state.
    Cycle включает MetadataIncrementalSync.run() и xml_full_scan_run (если
    активен XML и попали в окно). Post-sync metadata embedding re-pass
    пропускается: vector indexer стартует следом и сам покрывает изменённые
    метаданные.

    Возвращает (success, last_full_scan_at, cycle_ran).
    - success: validation source прошла; ничего не сломалось.
    - last_full_scan_at: не None, если XML full scan действительно отработал
      в этом процессе. Пробрасывается в periodic scheduler, чтобы избежать
      дубль full scan при рестарте посреди окна.
    - cycle_ran: True если этот процесс реально взял scheduler_lock и
      выполнил sync.run(). False если конкурентный worker удерживал lock —
      этот процесс не блокируется ожиданием, но caller должен залогировать
      пропуск явно (фоновые индексеры стартуют как degraded mode).
    """
    scheduler = IncrementalLoadingScheduler(
        loader=loader,
        settings_obj=settings_obj,
        state_path=state_path,
        run_first_cycle=False,
    )
    state = IncrementalLoadingState(state_path, settings_obj.project_name)
    try:
        if not scheduler._detect_and_validate_source(state):
            return False, None, False
        # Startup cycle: vector DDL uses the bounded embedding probe, and the
        # owned EmbeddingAvailability flows to BSL Phase 5 (which runs inside
        # this cycle, before the post-bootstrap pipeline).
        cycle_ran = scheduler._cycle(
            state,
            include_xml_full_scan=True,
            include_post_sync_embedding=False,
            use_startup_probe_for_vectors=True,
            embedding_availability=embedding_availability,
        )
        return True, scheduler._last_full_scan_at, cycle_ran
    finally:
        state.close()
