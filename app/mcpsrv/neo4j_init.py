"""
Neo4j initialization helpers.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from neo4j_loader import Neo4jLoader
from config import settings

neo4j_loader: Optional[Neo4jLoader] = None

# ---------------------------------------------------------------------------
# Routine search schema readiness.
#
# `get_loader()` publishes the raw connection and says nothing about the search
# schema — console stats, object summary and the ~15 typed tools that never read
# `Routine.name_norm` keep working while the schema migrates.
# `get_search_ready_loader()` is the gate for the paths that DO read those fields.
#
# The migration runs off the request path: a caller that arrives during MIGRATING
# gets an immediate None rather than waiting out a minute-long backfill.
# ---------------------------------------------------------------------------

SCHEMA_DEGRADED_KEY = "schema:routine_search"

_SCHEMA_DISCONNECTED = "disconnected"
_SCHEMA_MIGRATING = "migrating"
_SCHEMA_READY = "ready"
_SCHEMA_RETRY_AFTER = "retry_after"

_SCHEMA_RETRY_BACKOFF_SECONDS = 60.0

_schema_lock = threading.Lock()
_schema_state: str = _SCHEMA_DISCONNECTED
_schema_retry_at: float = 0.0


def initialize_neo4j() -> bool:
    """Initialize Neo4j connection (idempotent)."""
    global neo4j_loader
    if neo4j_loader is not None:
        return True
    try:
        neo4j_loader = Neo4jLoader()
        logging.debug("Neo4j connection established")
        return True
    except Exception as e:
        logging.error(f"Failed to connect to Neo4j: {str(e)}")
        neo4j_loader = None
        return False


def get_loader() -> Optional[Neo4jLoader]:
    """Get current Neo4jLoader instance (may be None)."""
    return neo4j_loader


def _set_schema_degraded(reason: str) -> None:
    try:
        from . import runtime_state
        runtime_state.set_degraded_reason(SCHEMA_DEGRADED_KEY, reason)
    except Exception:
        pass


def _clear_schema_degraded() -> None:
    try:
        from . import runtime_state
        runtime_state.clear_degraded_reason(SCHEMA_DEGRADED_KEY)
    except Exception:
        pass


def _run_schema_ensure() -> bool:
    """Run ensure_routine_search_schema against the live loader. Returns readiness."""
    loader = neo4j_loader
    if loader is None:
        return False
    from graphdb.indexes import ensure_routine_search_schema
    with loader.driver.session(database=settings.neo4j_database) as session:
        return ensure_routine_search_schema(session)


def _finish_schema_ensure(ok: bool) -> None:
    global _schema_state, _schema_retry_at
    with _schema_lock:
        if ok:
            _schema_state = _SCHEMA_READY
        else:
            _schema_state = _SCHEMA_RETRY_AFTER
            _schema_retry_at = time.monotonic() + _SCHEMA_RETRY_BACKOFF_SECONDS
    # Paired set/clear per the runtime_state contract: recovery inside the same
    # process must drop the reason instead of leaving a stale one in /health.
    if ok:
        _clear_schema_degraded()
    else:
        _set_schema_degraded("routine search schema not ready (backfill/index)")


def ensure_search_schema_blocking() -> bool:
    """Run the schema ensure synchronously. For bootstrap only, before the endpoint is up.

    Failure is non-fatal: the process keeps starting and only the search-ready
    consumers are gated, exactly as on the late-reconnect path. Otherwise two
    processes in the same backend state would differ in availability purely by
    startup history.
    """
    global _schema_state
    with _schema_lock:
        if _schema_state == _SCHEMA_READY:
            return True
        _schema_state = _SCHEMA_MIGRATING
    try:
        ok = _run_schema_ensure()
    except Exception as e:
        logging.error("Routine search schema ensure failed: %s", e)
        ok = False
    _finish_schema_ensure(ok)
    return ok


def _start_background_schema_ensure() -> None:
    def _worker() -> None:
        try:
            ok = _run_schema_ensure()
        except Exception as e:
            logging.error("Background routine search schema ensure failed: %s", e)
            ok = False
        _finish_schema_ensure(ok)

    threading.Thread(
        target=_worker, name="routine_search_schema_ensure", daemon=True,
    ).start()


def is_search_schema_ready() -> bool:
    """True iff the Routine search schema is ready.

    Never blocks. When the schema is not ready this kicks off a single background
    ensure (respecting the retry backoff) and returns False immediately, so a tool
    call reports "backend unavailable" instead of hanging on the migration.
    """
    global _schema_state
    with _schema_lock:
        if _schema_state == _SCHEMA_READY:
            return True
        if _schema_state == _SCHEMA_MIGRATING:
            return False
        if _schema_state == _SCHEMA_RETRY_AFTER and time.monotonic() < _schema_retry_at:
            return False
        _schema_state = _SCHEMA_MIGRATING
    _start_background_schema_ensure()
    return False


def get_search_ready_loader() -> Optional[Neo4jLoader]:
    """Loader for queries reading `Routine.name_norm` / `signature_norm`.

    Returns None until the search schema is ready. Use `get_loader()` for paths
    that do not touch those fields — gating them would take unrelated features
    down for the duration of the backfill.
    """
    if not initialize_neo4j():
        return None
    if not is_search_schema_ready():
        return None
    return neo4j_loader


# ---------------------------------------------------------------------------
# Substring accelerators (fulltext indexes).
#
# Separate from the correctness schema above on purpose. A missing `name_norm` would make
# results silently wrong, so that capability blocks bootstrap and gates tools. A missing
# fulltext index only makes one query slower — the scan path stays correct — so it must
# never block startup or gate a tool. State is tracked per index so one unavailable
# accelerator does not disable the other.
# ---------------------------------------------------------------------------

_ACC_UNKNOWN = "unknown"
_ACC_ENSURING = "ensuring"
_ACC_ONLINE = "online"
_ACC_RETRY_AFTER = "retry_after"

_ACC_RETRY_BACKOFF_SECONDS = 300.0

_acc_lock = threading.Lock()
_acc_state: dict = {}
_acc_retry_at: dict = {}


def _acc_index_name(target) -> str:
    from graphdb.routine_substring_queries import accelerator_index_name
    return accelerator_index_name(target)


def is_accelerator_ready(target) -> bool:
    """True iff the accelerator for `target` is known ONLINE.

    Pure, non-blocking state check — never runs DDL and never waits. A False here just
    routes the caller to the scan path.
    """
    with _acc_lock:
        return _acc_state.get(_acc_index_name(target)) == _ACC_ONLINE


def _enter_retry_after(index_name: str) -> bool:
    """Move an index to RETRY_AFTER. Returns True if this call owns scheduling the retry.

    Caller must hold no lock. Only the transition *into* RETRY_AFTER schedules, so
    concurrent failures cannot pile up retry threads for the same index.
    """
    with _acc_lock:
        was_pending = _acc_state.get(index_name) == _ACC_RETRY_AFTER
        _acc_state[index_name] = _ACC_RETRY_AFTER
        _acc_retry_at[index_name] = time.monotonic() + _ACC_RETRY_BACKOFF_SECONDS
    return not was_pending


def _schedule_accelerator_retry(index_name: str) -> None:
    """Re-run the ensure once the backoff expires.

    Required for the accelerator to come back without restarting the process: the startup
    pass runs once, so a stored deadline on its own would never fire and a single
    transient failure would keep the index on the scan path for the process lifetime.
    """
    def _wait_then_ensure() -> None:
        while True:
            with _acc_lock:
                if _acc_state.get(index_name) != _ACC_RETRY_AFTER:
                    return  # someone else took ownership (ENSURING/ONLINE)
                delay = _acc_retry_at.get(index_name, 0.0) - time.monotonic()
            if delay > 0:
                time.sleep(min(delay, 30.0))
                continue
            with _acc_lock:
                if _acc_state.get(index_name) != _ACC_RETRY_AFTER:
                    return
                _acc_state[index_name] = _ACC_ENSURING
            _ensure_accelerator(index_name)
            return

    threading.Thread(
        target=_wait_then_ensure, name=f"accelerator_retry_{index_name}", daemon=True,
    ).start()


def invalidate_accelerator(index_name: str) -> None:
    """Drop a cached ONLINE after a query against that index failed, and schedule a retry.

    Without the invalidation a dropped index would keep its cached readiness and every
    subsequent call would retry it, fail and fall back — an exception per request forever.
    Without the retry the index would never come back inside this process.
    """
    with _acc_lock:
        if _acc_state.get(index_name) == _ACC_ONLINE:
            logging.warning(
                "Accelerator %s failed at query time; falling back to scan", index_name,
            )
    if _enter_retry_after(index_name):
        _schedule_accelerator_retry(index_name)


def _ensure_accelerator(index_name: str) -> None:
    loader = neo4j_loader
    if loader is None:
        with _acc_lock:
            _acc_state[index_name] = _ACC_UNKNOWN
        return
    ok = False
    try:
        from graphdb.indexes import ensure_accelerator_index
        with loader.driver.session(database=settings.neo4j_database) as session:
            ok = ensure_accelerator_index(session, index_name)
    except Exception as e:
        logging.warning("Accelerator ensure failed for %s: %s", index_name, e)
    if ok:
        with _acc_lock:
            _acc_state[index_name] = _ACC_ONLINE
        return
    # A failed ensure must arm the next attempt too, otherwise one bad startup pass is
    # as terminal as a runtime failure used to be.
    if _enter_retry_after(index_name):
        _schedule_accelerator_retry(index_name)


def _demote_vanished_accelerators() -> None:
    """Drop cached ONLINE for indexes that no longer exist in the database.

    Range accelerators give no runtime signal when they disappear: querying a missing
    hinted index yields a notification rather than an exception, and
    `execute_query_readonly` does not surface notifications. Without this re-check a
    dropped index would keep its cached readiness for the lifetime of the process, and the
    query would silently stay on the slow plan.
    """
    loader = neo4j_loader
    if loader is None:
        return
    with _acc_lock:
        cached = {n for n, s in _acc_state.items() if s == _ACC_ONLINE}
    if not cached:
        return
    try:
        from graphdb.indexes import online_accelerator_indexes
        with loader.driver.session(database=settings.neo4j_database) as session:
            still_online = online_accelerator_indexes(session, cached)
    except Exception as e:
        logging.warning("Could not verify accelerator index state: %s", e)
        return
    for index_name in cached - still_online:
        logging.warning(
            "Accelerator %s is no longer ONLINE; scheduling re-create", index_name,
        )
        if _enter_retry_after(index_name):
            _schedule_accelerator_retry(index_name)


def start_accelerator_ensure() -> None:
    """Kick off DDL + ONLINE wait for every accelerator, one thread each.

    Call sites must schedule this *after* the startup indexers finish: populating a
    fulltext index over every Routine while BSL/vector/summary are streaming the same
    label would just move the rollout cost from startup latency to resource contention.
    Nothing waits on the result — until an index is ONLINE the matching search is a scan.

    Safe to call repeatedly: cached ONLINE is re-verified against the database first, so
    this doubles as the periodic reconcile for indexes lost at runtime.
    """
    from graphdb.indexes import ROUTINE_SEARCH_ACCELERATORS

    _demote_vanished_accelerators()

    for index_name in ROUTINE_SEARCH_ACCELERATORS:
        with _acc_lock:
            state = _acc_state.get(index_name, _ACC_UNKNOWN)
            if state in (_ACC_ONLINE, _ACC_ENSURING):
                continue
            if state == _ACC_RETRY_AFTER and time.monotonic() < _acc_retry_at.get(index_name, 0.0):
                continue
            _acc_state[index_name] = _ACC_ENSURING
        threading.Thread(
            target=_ensure_accelerator, args=(index_name,),
            name=f"accelerator_ensure_{index_name}", daemon=True,
        ).start()


def check_neo4j_connection() -> bool:
    """Check if Neo4j is connected."""
    global neo4j_loader
    if neo4j_loader:
        try:
            neo4j_loader.execute_query_readonly("RETURN 1")
            return True
        except Exception:
            return False
    return False