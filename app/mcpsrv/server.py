"""
FastMCP server wiring: registers tools and exposes run_server().
"""

from __future__ import annotations

import logging
import os, sys
import asyncio
import threading
from typing import Optional

from fastmcp import FastMCP

from config import settings
from .neo4j_init import initialize_neo4j, get_loader
from . import runtime_state


# Setup logging (mimics original behavior). Force reconfigure to honor DEBUG env and stream to stdout.
logging.basicConfig(
    level=logging.DEBUG if settings.enable_debug else logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)

# Create FastMCP server instance
mcp = FastMCP()


def _register_tools():
    from . import typed_tools
    from .tool_usage_metrics import install_tool_usage_metrics
    from .tool_visibility import initialize_tool_visibility

    typed_tools.register_tools(mcp, load_bsl=settings.load_bsl_signatures)
    install_tool_usage_metrics(mcp)
    initialize_tool_visibility(mcp)
    logging.info("Typed MCP tools registered (load_bsl=%s)", settings.load_bsl_signatures)


def _run_startup_incremental() -> tuple:
    """Run one incremental cycle one-shot at process startup.

    При обычном рестарте вызывается из daemon-нити `startup_incremental_pipeline`
    параллельно с уже поднятым MCP endpoint; metadata-dependent индексеры стартуют
    после возврата. Cycle включает MetadataIncrementalSync.run() и xml_full_scan_run
    (если активен XML и попали в окно). Post-sync embedding re-pass пропускается —
    vector indexer стартует следом и сам обработает изменённое состояние.

    Возвращает (last_full_scan_at, embedding_availability, fatal_validation):
    - last_full_scan_at: timestamp успешного XML full scan (или None), чтобы
      periodic scheduler не повторял full scan при рестарте посреди окна.
    - embedding_availability: один bounded probe на весь startup pass, вычисленный
      ДО `run_incremental_once` (BSL Phase 5 исполняется внутри цикла) и прокинутый
      далее в post-bootstrap pipeline, чтобы не пробить endpoint повторно.
    - fatal_validation: True ТОЛЬКО когда `run_incremental_once` вернул success=False
      из-за проваленной baseline/source-валидации. В этом случае caller обязан НЕ
      запускать post-bootstrap pipeline (fail-closed). lock-held и прочие мягкие
      исходы → False (degraded ok, как раньше).
    """
    # One bounded availability probe for the whole restart startup pass.
    try:
        from graphdb.embedding_service import probe_embedding_availability
        status = probe_embedding_availability()
    except Exception as e:
        logging.warning("Startup embedding availability probe failed: %s", e)
        status = None

    if not getattr(settings, "incremental_loading_enabled", False):
        return None, status, False
    loader = get_loader()
    if not loader:
        logging.warning("Cannot run startup incremental: Neo4j loader not initialized")
        return None, status, False
    try:
        from incremental.scheduler import run_incremental_once
        from pathlib import Path as _Path

        state_path = _Path(settings.incremental_loading_state_path)
        logging.info("Startup incremental loading started before background indexers")
        success, last_full_scan_at, cycle_ran = run_incremental_once(
            loader=loader,
            settings_obj=settings,
            state_path=state_path,
            embedding_availability=status,
        )
        if not success:
            # Fail-closed: baseline/source validation провалена. Не запускать
            # post-bootstrap pipeline (embedding/BSL/summary/scheduler).
            logging.error(
                "Startup incremental loading failed: baseline/source validation aborted; "
                "post-bootstrap pipeline не запускается (fail-closed)"
            )
            return None, status, True
        if not cycle_ran:
            # Конкурентный worker удерживает scheduler_lock и сам синхронизирует state.
            # Vector/BSL стартуют по тому состоянию Neo4j, которое тот worker уже закоммитил;
            # это same-or-better, чем dropping incremental entirely.
            logging.warning(
                "Startup incremental skipped: scheduler_lock held by concurrent worker; "
                "background indexers may see partially synced Neo4j state"
            )
            return last_full_scan_at, status, False
        logging.info("Startup incremental loading complete")
        return last_full_scan_at, status, False
    except Exception as e:
        logging.error("Startup incremental loading failed: %s", e, exc_info=True)
        return None, status, False


def _start_incremental_loading_background(*, last_full_scan_at: Optional[float] = None):
    """Start incremental loading scheduler daemon-thread for periodic cycles only.

    Owner of periodic incremental lifecycle. Startup one-shot уже выполнен синхронно
    в run_server до старта фоновых индексеров — поэтому scheduler запускается с
    run_first_cycle=False. При INCREMENTAL_LOADING_SCHEDULE_ENABLED=false daemon
    не стартует вовсе.
    """
    if not getattr(settings, "incremental_loading_enabled", False):
        return
    if not getattr(settings, "incremental_loading_schedule_enabled", False):
        logging.info(
            "Incremental loading scheduler not started "
            "(INCREMENTAL_LOADING_SCHEDULE_ENABLED=false); startup one-shot already done"
        )
        return
    loader = get_loader()
    if not loader:
        logging.warning("Cannot start incremental loading: Neo4j loader not initialized")
        return
    try:
        from incremental.scheduler import IncrementalLoadingScheduler
        from pathlib import Path as _Path

        # BSL-symmetric: use path as-is (CWD-relative or absolute). В docker CWD=/app,
        # поэтому 'storage/incremental/incremental_loading.sqlite' → '/app/storage/...' → named volume.
        state_path = _Path(settings.incremental_loading_state_path)
        scheduler = IncrementalLoadingScheduler(
            loader=loader,
            settings_obj=settings,
            state_path=state_path,
            run_first_cycle=False,
            last_full_scan_at=last_full_scan_at,
        )
        scheduler.start()
        logging.info(
            "Incremental loading scheduler started (source=%s, interval=%dmin, run_first_cycle=False)",
            getattr(settings, "metadata_source", "txt"),
            getattr(settings, "incremental_loading_interval_minutes", 60),
        )
    except Exception as e:
        logging.error("Failed to start incremental loading scheduler: %s", e, exc_info=True)


def _refresh_console_stats_after(source: str) -> None:
    """Safe console-stats refresh for lifecycle hooks. Gated on web_console_enabled;
    never propagates — a stats refresh failure must not block startup readiness."""
    if not settings.web_console_enabled:
        return
    try:
        from console.cache import refresh_console_stats_cache
        refresh_console_stats_cache(source=source, block=True, raise_on_error=False)
    except Exception:
        logging.error("Console stats refresh (%s) failed", source, exc_info=True)


def _start_readiness_barrier(startup_threads: list) -> None:
    """Flip runtime_state to "ready" once startup indexers complete.

    Independent of INCREMENTAL_LOADING_* — the web console manual job runner
    must know when metadata-dependent startup work has finished regardless
    of whether periodic scheduling is on or off. See plan §2.
    """
    threads = [t for t in startup_threads if t is not None]
    if not threads:
        _refresh_console_stats_after("startup_indexers_complete")
        runtime_state.mark_startup_ready()
        return

    for t in threads:
        runtime_state.register_startup_task(t.name)

    def _barrier() -> None:
        for t in threads:
            t.join()
            runtime_state.unregister_startup_task(t.name)
            logging.info("Readiness barrier: startup indexer completed: %s", t.name)
        _refresh_console_stats_after("startup_indexers_complete")
        runtime_state.mark_startup_ready()
        logging.info("Startup readiness: ready")

    barrier_thread = threading.Thread(
        target=_barrier, name="object_summary_readiness_barrier", daemon=True,
    )
    barrier_thread.start()


def _start_accelerators_after_startup_indexers(startup_threads: list) -> None:
    """Build the substring accelerators once the startup indexers are done.

    Deliberately sequential after them, and deliberately outside
    `_start_readiness_barrier`: nothing waits on these indexes (until one is ONLINE the
    matching `contains` search runs as a scan, exactly as before), but populating a
    fulltext index over every Routine while BSL/vector/summary stream the same label
    would trade startup latency for resource contention.
    """
    threads = [t for t in startup_threads if t is not None]

    def _wait_then_start() -> None:
        for t in threads:
            try:
                t.join()
            except Exception:  # noqa: BLE001 - a dead thread must not block the DDL
                pass
        try:
            from .neo4j_init import start_accelerator_ensure
            start_accelerator_ensure()
        except Exception as e:
            logging.error("Failed to start substring accelerator ensure: %s", e, exc_info=True)

    threading.Thread(
        target=_wait_then_start, name="accelerator_ensure_barrier", daemon=True,
    ).start()


def _start_incremental_loading_after_startup_indexers(
    startup_threads: list,
    *,
    last_full_scan_at: Optional[float] = None,
) -> None:
    """Defer scheduled incremental loading until startup indexers complete.

    Filters out None entries from startup_threads. If scheduler itself is
    disabled (INCREMENTAL_LOADING_ENABLED=false or
    INCREMENTAL_LOADING_SCHEDULE_ENABLED=false), delegates immediately to
    _start_incremental_loading_background so the existing early-return logging
    fires symmetrically. If there are no live startup threads, also delegates
    immediately. Otherwise spawns a daemon barrier thread that joins each
    startup thread and only then starts the scheduler.
    """
    threads = [t for t in startup_threads if t is not None]

    if (
        not getattr(settings, "incremental_loading_enabled", False)
        or not getattr(settings, "incremental_loading_schedule_enabled", False)
    ):
        _start_incremental_loading_background(last_full_scan_at=last_full_scan_at)
        return

    if not threads:
        logging.info("No startup indexers running; starting incremental scheduler immediately")
        _start_incremental_loading_background(last_full_scan_at=last_full_scan_at)
        return

    thread_names = ", ".join(t.name for t in threads)
    logging.info(
        "Incremental scheduler delayed until startup indexers complete: %s",
        thread_names,
    )

    def _barrier() -> None:
        for t in threads:
            t.join()
            logging.info("Startup indexer completed: %s", t.name)
        logging.info("Startup indexers complete; starting incremental scheduler")
        _start_incremental_loading_background(last_full_scan_at=last_full_scan_at)

    barrier_thread = threading.Thread(
        target=_barrier,
        name="incremental_scheduler_startup_barrier",
        daemon=True,
    )
    barrier_thread.start()


def _start_vector_indexing_background(embedding_availability=None) -> Optional[threading.Thread]:
    """Start vector indexing in a background thread.

    Returns the spawned thread, or None when indexing is disabled, loader is
    unavailable, or the thread could not be started. Caller may use the
    returned handle for startup-barrier coordination.

    `embedding_availability` (optional startup EmbeddingAvailability) is passed
    to the indexer so a known-unavailable endpoint short-circuits the pass.
    """
    # Check if at least one embedding feature is enabled
    if not settings.enable_routine_description_embedding and not settings.enable_metadata_description_embedding:
        logging.info("Vector indexing is disabled (both ENABLE_ROUTINE_DESCRIPTION_EMBEDDING and ENABLE_METADATA_DESCRIPTION_EMBEDDING are false)")
        return None

    # Log which phases are active for diagnostics
    active_phases = []
    if settings.enable_routine_description_embedding:
        active_phases.append("routine descriptions")
    if settings.enable_metadata_description_embedding:
        active_phases.append("metadata objects")
    logging.info(f"Vector indexing will process: {' -> '.join(active_phases)}")

    loader = get_loader()
    if not loader:
        logging.warning("Cannot start vector indexing: Neo4j loader not initialized")
        return None

    try:
        from graphdb.vector_indexer import VectorIndexer

        # Create indexer (it will handle individual phase flags internally)
        indexer = VectorIndexer(loader.driver, embedding_availability=embedding_availability)

        # Start indexing in a separate thread with async event loop
        def run_indexing():
            """Run async indexing in dedicated thread"""
            try:
                # Create new event loop for this thread
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                # Run indexing to natural completion (no long-lived background tasks)
                loop.run_until_complete(indexer.start_indexing())

            except Exception as e:
                logging.error(f"Vector indexing thread failed: {e}", exc_info=True)
            finally:
                loop.close()

        # Start background thread with low priority
        indexing_thread = threading.Thread(
            target=run_indexing,
            name="vector_indexing",
            daemon=True  # Thread will be killed when main process exits
        )
        indexing_thread.start()

        logging.info(f"Vector indexing started in background thread for: {', '.join(active_phases)}")
        return indexing_thread

    except Exception as e:
        logging.error(f"Failed to start vector indexing: {e}", exc_info=True)
        return None


def _start_object_summary_background(embedding_availability=None) -> Optional[threading.Thread]:
    """Start the object_summary pipeline in a background daemon thread.

    Returns the thread handle so the caller can include it in the startup
    barrier together with `vector_thread` and `bsl_thread`. Returns `None`
    when the feature is disabled or the loader is not available — the barrier
    filters `None` out.

    `embedding_availability` (optional startup EmbeddingAvailability) is passed
    to the indexer so a known-unavailable endpoint skips the fingerprint and S2.
    """
    if not getattr(settings, "object_summary_enabled", False):
        return None
    loader = get_loader()
    if not loader:
        logging.warning("Cannot start object summary pipeline: Neo4j loader not initialized")
        return None
    try:
        from graphdb.object_summary_pipeline import ObjectSummaryIndexer
        indexer = ObjectSummaryIndexer(loader.driver, embedding_availability=embedding_availability)

        def _run():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(indexer.start())
            except Exception as e:
                logging.error("Object summary pipeline thread failed: %s", e, exc_info=True)
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

        thread = threading.Thread(target=_run, name="object_summary_indexing", daemon=True)
        thread.start()
        logging.info("Object summary indexing started in background thread")
        return thread
    except Exception as e:
        logging.error("Failed to start object summary pipeline: %s", e, exc_info=True)
        return None


def _ensure_startup_vector_indexes(status=None) -> None:
    """Idempotently (re)create enabled Neo4j vector indexes on container restart.

    `status` (optional startup EmbeddingAvailability): when provided, reuse its
    probe result — create indexes with the known dimension when available, or
    skip with one warning when the endpoint is unavailable. When None, fall back
    to the bounded self-probe path.

    Repairs the case where the embedding service was unavailable during the
    initial database load, so vector indexes were never created, and on a plain
    restart create_indexes() is not called again. Reuses
    loader.create_vector_indexes(): it inspects feature flags, probes the
    embedding service for the vector dimension, creates/aligns the missing
    indexes and silently skips when embeddings are unavailable.

    Synchronous by design so indexes exist before the embedding/indexing tasks
    start (VectorIndexer.start_indexing() does a one-shot existence check and
    aborts when indexes are missing). To keep a slow/unavailable embedding
    endpoint from stalling startup, the dimension probe uses the bounded
    use_startup_probe path (short timeout, single attempt, no model-info call).
    """
    loader = get_loader()
    if not loader:
        logging.warning("Cannot ensure vector indexes on startup: Neo4j loader not initialized")
        return
    try:
        if status is not None:
            if not status.enabled:
                return
            if not status.available:
                logging.warning(
                    "Startup vector indexes not created this pass: embedding "
                    "unavailable (%s); will be created on the next start once the "
                    "endpoint is reachable", status.reason,
                )
                return
            # Availability already probed once this startup — reuse its dimension
            # instead of probing the embedding endpoint a second time.
            loader.create_vector_indexes(dimension=status.dimension)
        else:
            loader.create_vector_indexes(use_startup_probe=True)
    except Exception as e:
        logging.error("Startup vector index ensure failed: %s", e, exc_info=True)


def _apply_startup_degraded_reasons(status) -> None:
    """Startup probe is the primary writer of embedding degraded reasons.

    When the endpoint is unavailable, set a per-feature key for each enabled
    phase that lacks its own persistent status (routine/metadata descriptions,
    object summary). Set here — not only in the consumer — so the key is present
    even when a consumer early-returns before its own set (e.g. VectorIndexer
    aborting on a missing vector index, which is exactly the restart-repair
    scenario). Consumers clear their key on a successful pass (recovery). BSL is
    excluded: its degradation is owned by the sidecar `vector_status`.
    """
    if status is None or not getattr(status, "enabled", False) or getattr(status, "available", True):
        return
    try:
        from mcpsrv import runtime_state
    except Exception:
        return
    reason = f"embedding unavailable: {getattr(status, 'reason', '') or 'endpoint unreachable'}"
    if settings.enable_routine_description_embedding or settings.enable_metadata_description_embedding:
        runtime_state.set_degraded_reason("embedding:routine_metadata", reason)
    if getattr(settings, "object_summary_enabled", False):
        runtime_state.set_degraded_reason("embedding:object_summary", reason)


def _start_post_bootstrap_pipeline(
    *, last_full_scan_at: Optional[float], ensure_vector_indexes: bool = True,
    embedding_availability=None,
) -> None:
    """Spawn metadata-dependent indexers + readiness barrier + scheduled incremental.

    Вызывается либо в main thread (skip_startup_incremental=True), либо из
    daemon-нити `startup_incremental_pipeline` после её завершения.

    ensure_vector_indexes: на restart-пути (daemon, MCP уже поднят) — True, чтобы
    досоздать отсутствующие vector indexes перед индексаторами. На inline
    full-reload пути (skip_startup_incremental=True) — False: индексы уже
    отреконсайлены загрузкой через create_indexes(), а синхронная проба здесь
    предшествовала бы подъёму MCP endpoint.

    embedding_availability: один owned EmbeddingAvailability на startup pass. На
    restart-пути он вычислен в `_run_startup_incremental` (до BSL Phase 5) и
    прокинут сюда. На inline-пути (None) вычисляется здесь одним bounded probe
    до старта фоновых индексаторов, чтобы object summary / BSL не переоткрывали
    недоступность через production timeout.
    """
    status = embedding_availability
    if status is None:
        try:
            from graphdb.embedding_service import probe_embedding_availability
            status = probe_embedding_availability()
        except Exception as e:
            logging.warning("Startup embedding availability probe failed: %s", e)
            status = None
    # Primary writer of degraded reasons: set before consumers so a missing-index
    # early-return (the restart-repair case) still surfaces the degraded key.
    _apply_startup_degraded_reasons(status)
    if ensure_vector_indexes:
        _ensure_startup_vector_indexes(status)
    vector_thread = _start_vector_indexing_background(embedding_availability=status)
    bsl_thread = None
    try:
        from graphdb.bsl_code_indexer import start_bsl_code_indexing_background
        bsl_thread = start_bsl_code_indexing_background(
            get_loader(), embedding_availability=status
        )
    except Exception as e:
        logging.error("Failed to start BSL code search indexer: %s", e, exc_info=True)
    summary_thread = _start_object_summary_background(embedding_availability=status)

    _start_readiness_barrier([vector_thread, bsl_thread, summary_thread])
    _start_accelerators_after_startup_indexers([vector_thread, bsl_thread, summary_thread])
    _start_incremental_loading_after_startup_indexers(
        [vector_thread, bsl_thread, summary_thread],
        last_full_scan_at=last_full_scan_at,
    )


def run_server(skip_startup_incremental: bool = False):
    """Run the FastMCP server.

    skip_startup_incremental — при full reload / initial load metadata
    свежий baseline уже создан в _init_incremental_baseline, повторный scan
    ничего не даст. main.py передаёт True в этом случае.

    При skip_startup_incremental=False (обычный рестарт) startup incremental
    уходит в daemon thread, MCP endpoint поднимается сразу после bootstrap;
    metadata-dependent индексеры и scheduled scheduler стартуют внутри той же
    daemon-нити после завершения цикла.
    """
    # Initialize Neo4j connection on startup
    if initialize_neo4j():
        logging.info("Neo4j connection initialized successfully")

        # Pay the search-schema migration here, before the endpoint accepts traffic,
        # rather than inside the first tool call. A failure is deliberately non-fatal:
        # only the four norm-dependent tools are gated, and aborting startup would take
        # console plus ~15 unaffected tools down over a capability they do not use.
        from .neo4j_init import ensure_search_schema_blocking
        if not ensure_search_schema_blocking():
            logging.error(
                "Routine search schema not ready at startup; BSL routine name/signature "
                "search is unavailable until it recovers (see degraded reasons)"
            )

        # Get configuration name from database (after metadata is loaded)
        try:
            import config as config_module
            loader = get_loader()
            if loader:
                result = loader.execute_query_readonly(
                    """
                    MATCH (c:Configuration)
                    WHERE c.project_name = $project_name
                      AND (c.is_extension IS NULL OR c.is_extension = false)
                    RETURN c.name AS config_name
                    LIMIT 1
                    """,
                    {"project_name": settings.project_name}
                )
                if result and len(result) > 0:
                    config_name_value = result[0].get('config_name')
                    if config_name_value:
                        config_module.onec_config_name = config_name_value
                        logging.info(f"Configuration name loaded: {config_name_value}")
                    else:
                        logging.warning("Configuration node found but name is empty")
                else:
                    logging.warning("Configuration node not found in database")
        except Exception as e:
            logging.warning(f"Failed to retrieve configuration name: {e}")
            # Not critical, onec_config_name will remain None

        if settings.web_console_enabled:
            from console.cache import refresh_console_stats_cache
            refresh_console_stats_cache(source="bootstrap")

    else:
        logging.warning(
            "Failed to initialize Neo4j connection. Some tools may not work until connection is established."
        )

    # Register tools
    _register_tools()

    # Initial / full reload — синхронно по уже свежему baseline. Иначе startup
    # incremental уходит в daemon-нить, и MCP endpoint поднимается ниже без
    # ожидания её завершения.
    if skip_startup_incremental:
        # Full reload / initial load: vector indexes were just reconciled during the
        # load's create_indexes(); skip the ensure so this inline (pre-MCP) path does
        # not block on an embedding probe.
        _start_post_bootstrap_pipeline(last_full_scan_at=None, ensure_vector_indexes=False)
    else:
        runtime_state.register_startup_task("startup_incremental")

        def _startup_incremental_pipeline() -> None:
            try:
                try:
                    last_full_scan_at, embedding_availability, fatal_validation = (
                        _run_startup_incremental()
                    )
                finally:
                    runtime_state.unregister_startup_task("startup_incremental")
                if fatal_validation:
                    # Fail-closed: baseline/source validation провалена — post-bootstrap
                    # pipeline (embedding/BSL/summary/scheduler) НЕ запускаем.
                    runtime_state.mark_startup_failed(
                        "startup incremental validation failed (fail-closed): "
                        "run FULL_METADATA_RELOAD=true"
                    )
                    return
                _start_post_bootstrap_pipeline(
                    last_full_scan_at=last_full_scan_at,
                    embedding_availability=embedding_availability,
                )
            except Exception as exc:
                logging.error(
                    "Startup pipeline failed; MCP остаётся доступным, "
                    "scheduled scheduler не стартует: %s", exc, exc_info=True,
                )
                runtime_state.mark_startup_failed(f"startup pipeline failed: {exc}")

        threading.Thread(
            target=_startup_incremental_pipeline,
            name="startup_incremental_pipeline",
            daemon=True,
        ).start()

    uses_sse_env = os.getenv("MCP_USE_SSE", "false")
    uses_sse = str(uses_sse_env).strip().lower() in ("1", "true", "yes", "on")
    transport = "sse" if uses_sse else "streamable-http"

    logging.info(
        "Transport: %s | MCP: %s:%s%s | Console: %s (enabled=%s)",
        transport,
        settings.mcp_host,
        settings.mcp_port,
        settings.mcp_path,
        settings.web_console_path,
        settings.web_console_enabled,
    )

    _start_mcp_server(transport, uses_sse)


def _start_mcp_server(transport: str, uses_sse: bool) -> None:
    if not settings.web_console_enabled:
        mcp.run(
            transport=transport,
            host=settings.mcp_host,
            port=settings.mcp_port,
            path=settings.mcp_path,
        )
        return

    if not settings.web_console_admin_token:
        logging.warning("WEB_CONSOLE_ADMIN_TOKEN is empty — admin console routes will return 403")
    logging.info(
        "Web console enabled at %s:%s%s",
        settings.mcp_host, settings.mcp_port, settings.web_console_path,
    )

    # Strategy 1: custom_route decorator (FastMCP >= 2.3)
    if hasattr(mcp, "custom_route"):
        _attach_console_via_custom_route(transport)
        mcp.run(
            transport=transport,
            host=settings.mcp_host,
            port=settings.mcp_port,
            path=settings.mcp_path,
        )
        return

    # Strategy 2: ASGI composition
    _run_with_asgi_composition(transport, uses_sse)


def _attach_console_via_custom_route(transport: str) -> None:
    from console.routes import build_console_routes
    for route in build_console_routes(transport):
        mcp.custom_route(route.path, methods=list(route.methods))(route.endpoint)
    logging.info("Console routes attached via FastMCP custom_route")


def _run_with_asgi_composition(transport: str, uses_sse: bool) -> None:
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from console.routes import build_console_routes

    mcp_asgi = _extract_mcp_asgi(uses_sse)
    console_routes = build_console_routes(transport)
    app = Starlette(routes=console_routes + [Mount("/", app=mcp_asgi)])
    logging.info("Console routes attached via ASGI composition")
    uvicorn.run(app, host=settings.mcp_host, port=settings.mcp_port)


def _extract_mcp_asgi(uses_sse: bool):
    if uses_sse:
        candidates = [("sse_app", {})]
    else:
        candidates = [
            ("http_app",            {"path": settings.mcp_path}),
            ("streamable_http_app", {"path": settings.mcp_path}),
            ("http_app",            {}),
        ]

    for method_name, kwargs in candidates:
        if hasattr(mcp, method_name):
            logging.info("Using FastMCP ASGI method: %s(%s)", method_name, kwargs)
            return getattr(mcp, method_name)(**kwargs)

    available = [a for a in dir(mcp) if not a.startswith("_")]
    raise RuntimeError(
        f"FastMCP does not expose a known ASGI method. "
        f"Available public attrs: {available}. "
        f"Pin fastmcp>=2.3 or set WEB_CONSOLE_ENABLED=false."
    )
