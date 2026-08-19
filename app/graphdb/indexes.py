"""
Index and constraint management for Neo4j.
Provides DDL alignment used by loaders.
"""
from __future__ import annotations

import logging
import time
from typing import Optional, Dict, List, Any, Callable
from neo4j.exceptions import Neo4jError
from config import settings

logger = logging.getLogger(__name__)


# Vector index specs (shared by initial DDL and re-creation paths).
# Filterable properties are used as index-level prefilter via Neo4j SEARCH clause.
# `owner_qn` is intentionally NOT filterable (high cardinality / long values).
VECTOR_INDEX_SPECS: Dict[str, Dict[str, Any]] = {
    "vec_routine_doc_description": {
        "label": "Routine",
        "vector_property": "doc_description_embedding",
        "filterable_properties": [
            "project_name",
            "config_name",
            "owner_category",
            "module_type",
            "routine_type",
            "export",
            "is_ssl_api",
        ],
    },
    "vec_metadataobject_description": {
        "label": "MetadataObject",
        "vector_property": "description_embedding",
        "filterable_properties": ["project_name", "config_name", "category_name"],
    },
    # BSL code search: built on shared label BslCodeSearchUnit (small=Routine, large=RoutineCodeUnit).
    # All filterable properties must exist as local node properties; they are denormalized onto
    # RoutineCodeUnit at indexing time (Neo4j SEARCH WHERE does not traverse parents).
    # `code_embedding_epoch` participates in the prefilter so that a stale vector epoch in the
    # candidate pool is excluded at index-level, not via post-filter.
    "vec_bsl_code_unit": {
        "label": "BslCodeSearchUnit",
        "vector_property": "code_embedding",
        "filterable_properties": [
            "project_name",
            "config_name",
            "owner_category",
            "module_type",
            "routine_type",
            "export",
            "code_embedding_epoch",
            "code_embedding_visible",
        ],
    },
    "vec_object_summary_embedding": {
        "label": "MetadataObject",
        "vector_property": "object_summary_embedding",
        "filterable_properties": ["project_name", "config_name", "category_name"],
    },
}


def _bsl_vector_enabled() -> bool:
    """
    Effective gate for the BSL code vector index. Both master flag
    (enable_bsl_code_search) and sub-flag (enable_bsl_code_embedding) must be on —
    otherwise BSL vector side effects (dimension probe + index creation) are skipped.
    """
    return bool(
        getattr(settings, "enable_bsl_code_search", False)
        and getattr(settings, "enable_bsl_code_embedding", False)
    )


# Process-wide cache: does this Neo4j accept `CREATE VECTOR INDEX ... WITH [filterable...]` DDL?
# None = not yet probed, True/False = result of one-time probe.
_FILTERABLE_DDL_SUPPORTED: Optional[bool] = None


def _detect_filterable_support(session) -> bool:
    """
    Probe whether the connected Neo4j accepts vector index DDL with filterable properties
    (`WITH [...]`). Creates and drops a tiny throwaway index to test parsing without touching
    existing project indexes. Result is cached process-wide.
    """
    global _FILTERABLE_DDL_SUPPORTED
    if _FILTERABLE_DDL_SUPPORTED is not None:
        return _FILTERABLE_DDL_SUPPORTED
    probe = "_vec_filterable_capability_probe"
    try:
        session.run(f"DROP INDEX {probe} IF EXISTS")
        session.run(
            f"CREATE VECTOR INDEX {probe} IF NOT EXISTS\n"
            f"FOR (n:_VecCapabilityProbe) ON n.embedding\n"
            f"WITH [n.project_name]\n"
            "OPTIONS { indexConfig: { `vector.dimensions`: 8, `vector.similarity_function`: 'cosine' } }"
        )
        session.run(f"DROP INDEX {probe} IF EXISTS")
        _FILTERABLE_DDL_SUPPORTED = True
        logger.info("Vector index filterable DDL is supported by this Neo4j.")
    except Neo4jError as e:
        _FILTERABLE_DDL_SUPPORTED = False
        logger.info("Vector index filterable DDL not supported, will use plain DDL: %s", e)
        # Best-effort cleanup if probe partially landed
        try:
            session.run(f"DROP INDEX {probe} IF EXISTS")
        except Neo4jError:
            pass
    return _FILTERABLE_DDL_SUPPORTED


def _get_existing_vector_index_info(session, index_name: str) -> Optional[Dict[str, Any]]:
    """
    Return {'dimension': int, 'properties': [str, ...]} for an existing VECTOR index, or None.
    Uses SHOW VECTOR INDEXES which exposes the full properties list (vector + filterable).
    """
    try:
        record = session.run(
            """
            SHOW VECTOR INDEXES YIELD name, type, properties, options
            WHERE name = $index_name AND type = 'VECTOR'
            RETURN options.indexConfig.`vector.dimensions` AS dimension, properties AS properties
            """,
            index_name=index_name,
        ).single()
    except Neo4jError:
        # Older Neo4j without SHOW VECTOR INDEXES — fall back to SHOW INDEXES
        record = session.run(
            """
            SHOW INDEXES YIELD name, type, options
            WHERE name = $index_name AND type = 'VECTOR'
            RETURN options.indexConfig.`vector.dimensions` AS dimension, [] AS properties
            """,
            index_name=index_name,
        ).single()
    if not record:
        return None
    return {
        "dimension": record["dimension"],
        "properties": list(record["properties"] or []),
    }


def _vector_index_state(session, index_name: str) -> Optional[str]:
    """Return the `state` of a vector index (e.g. 'ONLINE', 'POPULATING',
    'FAILED') via SHOW INDEXES, or None if the index does not exist."""
    try:
        record = session.run(
            """
            SHOW INDEXES YIELD name, state
            WHERE name = $index_name
            RETURN state AS state
            """,
            index_name=index_name,
        ).single()
    except Neo4jError as e:
        logger.warning("Could not read state for index %s: %s", index_name, e)
        return None
    if not record:
        return None
    return record["state"]


def wait_index_online(
    session,
    index_name: str,
    timeout_seconds: float,
    *,
    sleep_fn: Optional[Callable[[float], None]] = None,
    poll_interval_seconds: float = 2.0,
) -> bool:
    """Poll SHOW INDEXES until `index_name` reaches state 'ONLINE' or timeout.

    Returns True iff the index reached ONLINE within `timeout_seconds`.
    A newly (re)created index is not queryable until it finishes populating;
    declaring a search capability ready before ONLINE would expose a path that is
    still unindexed (or, for vector indexes, whose query can fail/be unstable).

    Index-type agnostic: `SHOW INDEXES YIELD name, state` covers RANGE, TEXT and
    VECTOR alike.

    `sleep_fn(seconds)` lets callers inject a heartbeat-aware sleep (keeps a
    scheduler lease alive); defaults to time.sleep.
    """
    sleeper = sleep_fn or time.sleep
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        state = _vector_index_state(session, index_name)
        if state == "ONLINE":
            return True
        if state == "FAILED":
            logger.warning("Vector index %s is in FAILED state", index_name)
            return False
        if time.monotonic() >= deadline:
            logger.warning(
                "Vector index %s did not reach ONLINE within %.0fs (state=%s)",
                index_name, float(timeout_seconds), state,
            )
            return False
        sleeper(min(poll_interval_seconds, max(0.0, deadline - time.monotonic())))


def wait_vector_index_online(
    session,
    index_name: str,
    timeout_seconds: float,
    *,
    sleep_fn: Optional[Callable[[float], None]] = None,
    poll_interval_seconds: float = 2.0,
) -> bool:
    """Vector-index alias of `wait_index_online` (kept for existing call sites)."""
    return wait_index_online(
        session, index_name, timeout_seconds,
        sleep_fn=sleep_fn, poll_interval_seconds=poll_interval_seconds,
    )


# ---------------------------------------------------------------------------
# Routine search schema: denormalized *_norm fields + their index.
# ---------------------------------------------------------------------------

# Bumped when the backfill contract changes; the marker node stores the applied version.
ROUTINE_SEARCH_SCHEMA_VERSION: int = 1
ROUTINE_SEARCH_SCHEMA_KEY: str = "routine_search_fields"
IDX_ROUTINE_NAME_NORM: str = "idx_routine_name_norm"

_ROUTINE_NORM_BACKFILL = """
CALL {
  MATCH (r:Routine) WHERE r.name_norm IS NULL
  SET r.name_norm      = toLower(coalesce(r.name, '')),
      r.signature_norm = toLower(coalesce(r.signature, ''))
} IN TRANSACTIONS OF 10000 ROWS
"""

# Substring-search accelerators. Unlike the correctness schema above these are optional:
# a missing or POPULATING index only means the matching `contains` query runs as a scan,
# exactly as it does today. They are listed here rather than in the generic index list of
# create_indexes() so one owner can both create them and verify their state — the generic
# loop swallows per-statement DDL errors, which would let a capability report itself ready
# while a candidate driver is absent.
ROUTINE_SEARCH_ACCELERATORS: Dict[str, str] = {
    "ftx_routine_name": (
        "CREATE FULLTEXT INDEX ftx_routine_name IF NOT EXISTS "
        "FOR (r:Routine) ON EACH [r.name]"
    ),
    "ftx_routine_signature": (
        "CREATE FULLTEXT INDEX ftx_routine_signature IF NOT EXISTS "
        "FOR (r:Routine) ON EACH [r.signature]"
    ),
    # Ordered page source for the modes with no selective name predicate (unused,
    # exported, description fallback). Column order matters: the query orders by
    # (project_name, name, id), and only a matching index prefix lets the planner stream
    # rows instead of materialising every match.
    "idx_routine_proj_name": (
        "CREATE INDEX idx_routine_proj_name IF NOT EXISTS "
        "FOR (r:Routine) ON (r.project_name, r.name)"
    ),
}


def online_accelerator_indexes(session, names) -> set:
    """Names among `names` whose index currently exists and is ONLINE."""
    rows = session.run(
        "SHOW INDEXES YIELD name, state WHERE name IN $names AND state = 'ONLINE' "
        "RETURN name",
        names=list(names),
    )
    return {r["name"] for r in rows}


def ensure_accelerator_index(
    session,
    index_name: str,
    *,
    online_timeout_seconds: float = 1800.0,
    sleep_fn: Optional[Callable[[float], None]] = None,
) -> bool:
    """Create one substring accelerator and wait for it to come ONLINE.

    Returns True iff the index is queryable. Failure is not an error condition for the
    caller: the corresponding search simply keeps using the scan path.

    The timeout is generous because populating a fulltext index over every Routine takes
    minutes on large configurations; nothing waits on this call, so a long wait costs
    only the background thread.
    """
    ddl = ROUTINE_SEARCH_ACCELERATORS.get(index_name)
    if ddl is None:
        raise ValueError(f"Unknown routine search accelerator: {index_name}")
    try:
        session.run(ddl)
    except Neo4jError as e:
        logger.warning("Could not create accelerator index %s: %s", index_name, e)
        return False
    if not wait_index_online(
        session, index_name, online_timeout_seconds, sleep_fn=sleep_fn,
    ):
        logger.warning("Accelerator index %s did not reach ONLINE", index_name)
        return False
    logger.info("Accelerator index %s is ONLINE", index_name)
    return True


def _routine_schema_version(session) -> int:
    record = session.run(
        "MATCH (s:GraphSchemaState {key: $key}) RETURN s.version AS version",
        key=ROUTINE_SEARCH_SCHEMA_KEY,
    ).single()
    if not record or record["version"] is None:
        return 0
    return int(record["version"])


def ensure_routine_search_schema(
    session,
    *,
    online_timeout_seconds: float = 300.0,
    sleep_fn: Optional[Callable[[float], None]] = None,
) -> bool:
    """Make Routine name search usable: backfill `*_norm`, create the index, wait ONLINE.

    Three stages, and the capability is ready only after all three:

    1. data     — batched backfill, guarded by a :GraphSchemaState marker. Idempotent and
                  restartable via `WHERE r.name_norm IS NULL`; the marker is written only
                  after the backfill completes.
    2. schema   — index DDL. Unlike the generic create_indexes() loop this does NOT swallow
                  errors: a missing index leaves the search path working but slow, which is
                  exactly the symptom this schema exists to remove.
    3. readiness— poll until ONLINE. Fields plus a POPULATING index are not ready.

    The marker covers stage 1 only; stages 2-3 are re-verified on every call (a cheap
    SHOW INDEXES by name) so a crash between backfill and DDL cannot produce false readiness.

    Returns True iff the capability is ready. Raises nothing — failures are reported
    via the return value and logged.
    """
    try:
        if _routine_schema_version(session) < ROUTINE_SEARCH_SCHEMA_VERSION:
            logger.info("Routine search schema: backfilling name_norm/signature_norm")
            session.run(_ROUTINE_NORM_BACKFILL)
            session.run(
                "MERGE (s:GraphSchemaState {key: $key}) SET s.version = $version",
                key=ROUTINE_SEARCH_SCHEMA_KEY, version=ROUTINE_SEARCH_SCHEMA_VERSION,
            )
            logger.info("Routine search schema: backfill complete")

        session.run(
            f"CREATE INDEX {IDX_ROUTINE_NAME_NORM} IF NOT EXISTS "
            "FOR (r:Routine) ON (r.name_norm)"
        )
    except Neo4jError as e:
        logger.error("Routine search schema not ready: %s", e)
        return False

    if not wait_index_online(
        session, IDX_ROUTINE_NAME_NORM, online_timeout_seconds, sleep_fn=sleep_fn,
    ):
        logger.error(
            "Routine search schema not ready: index %s did not reach ONLINE",
            IDX_ROUTINE_NAME_NORM,
        )
        return False
    return True


def ensure_bsl_code_vector_index_online(
    session,
    dimension: int,
    timeout_seconds: float,
    *,
    sleep_fn: Optional[Callable[[float], None]] = None,
) -> bool:
    """Ensure `vec_bsl_code_unit` exists (DDL) AND is ONLINE/queryable.

    Used before committing vector_status='ready' for BSL code search: on a
    startup where the embedding endpoint was down, `create_indexes` skipped the
    vector index (dimension probe returned None); a later Phase B recovery must
    create the index and wait for it to populate before flipping to ready.

    Returns True iff the index is ONLINE. `ensure_vector_index` is idempotent:
    if the index already exists ONLINE this returns quickly ('kept' + immediate
    ONLINE poll).
    """
    try:
        ensure_vector_index(session, "vec_bsl_code_unit", dimension)
    except Neo4jError as e:
        logger.warning("Could not ensure vector index vec_bsl_code_unit: %s", e)
        return False
    return wait_vector_index_online(
        session, "vec_bsl_code_unit", timeout_seconds, sleep_fn=sleep_fn,
    )


def ensure_vector_index(session, index_name: str, dimension: int) -> str:
    """
    Ensure a vector index exists with the expected dimension and set of filterable properties.

    Drops + recreates when dimension OR properties set differ. Returns 'kept'/'recreated'/'created'.

    Tries the new DDL with `WITH [...]` (filterable properties); on Neo4jError falls back to the
    legacy DDL without filterable properties (still works with `db.index.vector.queryNodes`).
    """
    global _FILTERABLE_DDL_SUPPORTED
    spec = VECTOR_INDEX_SPECS.get(index_name)
    if spec is None:
        raise ValueError(f"Unknown vector index name: {index_name}")
    label: str = spec["label"]
    vector_property: str = spec["vector_property"]
    filterable: List[str] = list(spec["filterable_properties"])
    plain_properties = {vector_property}
    full_properties = {vector_property, *filterable}

    filterable_supported = _detect_filterable_support(session)

    existing = _get_existing_vector_index_info(session, index_name)
    if existing is not None:
        dim_matches = existing["dimension"] == dimension
        existing_set = set(existing["properties"] or [])
        props_known = bool(existing_set)
        if not props_known:
            # SHOW INDEXES fallback (no `properties` column) — accept dim-only to avoid spurious recreates.
            props_match = True
        elif filterable_supported:
            # On modern Neo4j only the full filterable set is the target state. Plain index is drift
            # (would silently degrade vector services to legacy queryNodes path).
            props_match = existing_set == full_properties
        else:
            # On legacy Neo4j the only valid state we can build is the plain index.
            props_match = existing_set == plain_properties
        if dim_matches and props_match:
            return "kept"
        logger.warning(
            "Vector index %s schema drift (dim=%s vs %s, properties=%s vs target=%s) — dropping",
            index_name, existing["dimension"], dimension,
            sorted(existing_set),
            sorted(full_properties if filterable_supported else plain_properties),
        )
        session.run(f"DROP INDEX {index_name} IF EXISTS")
        action = "recreated"
    else:
        action = "created"

    if filterable_supported:
        with_clause = ", ".join(f"n.{p}" for p in filterable)
        new_ddl = (
            f"CREATE VECTOR INDEX {index_name} IF NOT EXISTS\n"
            f"FOR (n:{label}) ON n.{vector_property}\n"
            f"WITH [{with_clause}]\n"
            "OPTIONS {\n"
            "  indexConfig: {\n"
            f"    `vector.dimensions`: {dimension},\n"
            "    `vector.similarity_function`: 'cosine'\n"
            "  }\n"
            "}\n"
        )
        try:
            session.run(new_ddl)
            logger.info(
                "Vector index %s %s with filterable %s (dim=%s)",
                index_name, action, filterable, dimension,
            )
            return action
        except Neo4jError as e:
            # Probe was optimistic — downgrade capability for the rest of the process.
            logger.warning(
                "Filterable-properties DDL rejected for %s despite positive probe, "
                "downgrading capability to plain DDL: %s",
                index_name, e,
            )
            _FILTERABLE_DDL_SUPPORTED = False

    old_ddl = (
        f"CREATE VECTOR INDEX {index_name} IF NOT EXISTS\n"
        f"FOR (n:{label}) ON (n.{vector_property})\n"
        "OPTIONS {\n"
        "  indexConfig: {\n"
        f"    `vector.dimensions`: {dimension},\n"
        "    `vector.similarity_function`: 'cosine'\n"
        "  }\n"
        "}\n"
    )
    session.run(old_ddl)
    logger.info(
        "Vector index %s %s (plain DDL, no filterable properties) dim=%s",
        index_name, action, dimension,
    )
    return action


class IndexManagementMixin:
    """DDL helpers for constraints and indexes."""

    def create_indexes(self, *, use_startup_probe_for_vectors: bool = False) -> None:
        """Create/align constraints and indexes for better query correctness and performance.

        `use_startup_probe_for_vectors` (default False) makes the vector-index
        part probe the embedding endpoint with a bounded startup timeout instead
        of the production one. Startup/load call sites (full-load orchestrator,
        startup incremental) pass True so an unavailable endpoint cannot stall
        startup; scheduled incremental keeps the default so its behaviour is
        unchanged.
        """
        with self.driver.session(database=settings.neo4j_database) as session:
            # 1) Ensure correct UNIQUE constraints (context-unique via qualified_name)
            constraints = [
                # Keep unique project.name
                "CREATE CONSTRAINT uq_project_name IF NOT EXISTS FOR (p:Project) REQUIRE (p.name) IS UNIQUE",

                # Context-unique by qualified_name
                "CREATE CONSTRAINT uq_configuration_qn IF NOT EXISTS FOR (c:Configuration) REQUIRE (c.qualified_name) IS UNIQUE",
                "CREATE CONSTRAINT uq_metadatacategory_qn IF NOT EXISTS FOR (cat:MetadataCategory) REQUIRE (cat.qualified_name) IS UNIQUE",
                "CREATE CONSTRAINT uq_metadataobject_qn IF NOT EXISTS FOR (m:MetadataObject) REQUIRE (m.qualified_name) IS UNIQUE",
                "CREATE CONSTRAINT uq_tabularpart_qn IF NOT EXISTS FOR (t:TabularPart) REQUIRE (t.qualified_name) IS UNIQUE",
                "CREATE CONSTRAINT uq_attribute_qn IF NOT EXISTS FOR (a:Attribute) REQUIRE (a.qualified_name) IS UNIQUE",
                "CREATE CONSTRAINT uq_resource_qn IF NOT EXISTS FOR (r:Resource) REQUIRE (r.qualified_name) IS UNIQUE",
                "CREATE CONSTRAINT uq_dimension_qn IF NOT EXISTS FOR (d:Dimension) REQUIRE (d.qualified_name) IS UNIQUE",
                "CREATE CONSTRAINT uq_form_qn IF NOT EXISTS FOR (f:Form) REQUIRE (f.qualified_name) IS UNIQUE",
                "CREATE CONSTRAINT uq_formcontrol_qn IF NOT EXISTS FOR (fc:FormControl) REQUIRE (fc.qualified_name) IS UNIQUE",
                "CREATE CONSTRAINT uq_formevent_qn IF NOT EXISTS FOR (fe:FormEvent) REQUIRE (fe.qualified_name) IS UNIQUE",
                "CREATE CONSTRAINT uq_formeventaction_qn IF NOT EXISTS FOR (a:FormEventAction) REQUIRE (a.qualified_name) IS UNIQUE",
                "CREATE CONSTRAINT uq_formattribute_qn IF NOT EXISTS FOR (fa:FormAttribute) REQUIRE (fa.qualified_name) IS UNIQUE",
                "CREATE CONSTRAINT uq_command_qn IF NOT EXISTS FOR (c:Command) REQUIRE (c.qualified_name) IS UNIQUE",
                "CREATE CONSTRAINT uq_layout_qn IF NOT EXISTS FOR (l:Layout) REQUIRE (l.qualified_name) IS UNIQUE",
                "CREATE CONSTRAINT uq_characteristic_qn IF NOT EXISTS FOR (s:Characteristic) REQUIRE (s.qualified_name) IS UNIQUE",
                "CREATE CONSTRAINT uq_enumvalue_qn IF NOT EXISTS FOR (v:EnumValue) REQUIRE (v.qualified_name) IS UNIQUE",
                "CREATE CONSTRAINT uq_urltemplate_qn IF NOT EXISTS FOR (t:UrlTemplate) REQUIRE (t.qualified_name) IS UNIQUE",
                "CREATE CONSTRAINT uq_urlmethod_qn IF NOT EXISTS FOR (m:UrlMethod) REQUIRE (m.qualified_name) IS UNIQUE",
                "CREATE CONSTRAINT uq_journalgraph_qn IF NOT EXISTS FOR (g:JournalGraph) REQUIRE (g.qualified_name) IS UNIQUE",
                "CREATE CONSTRAINT uq_accountingflag_qn IF NOT EXISTS FOR (af:AccountingFlag) REQUIRE (af.qualified_name) IS UNIQUE",
                "CREATE CONSTRAINT uq_dimensionaccountingflag_qn IF NOT EXISTS FOR (sf:DimensionAccountingFlag) REQUIRE (sf.qualified_name) IS UNIQUE",
                "CREATE CONSTRAINT uq_predefineditem_qn IF NOT EXISTS FOR (p:PredefinedItem) REQUIRE (p.qualified_name) IS UNIQUE",
                # EventSubscription nodes are stored as MetadataObject (covered by uq_metadataobject_qn)

                # BSL: critical ids for fast MERGE/MATCH
                "CREATE CONSTRAINT uq_module_id IF NOT EXISTS FOR (m:Module) REQUIRE (m.id) IS UNIQUE",
                "CREATE CONSTRAINT uq_routine_id IF NOT EXISTS FOR (r:Routine) REQUIRE (r.id) IS UNIQUE",
                "CREATE CONSTRAINT uq_routine_code_unit_id IF NOT EXISTS FOR (u:RoutineCodeUnit) REQUIRE (u.id) IS UNIQUE",
            ]

            # 2) Property indexes for common lookups
            indexes = [
                "CREATE INDEX idx_metadataobject_name IF NOT EXISTS FOR (m:MetadataObject) ON (m.name)",
                "CREATE INDEX idx_tabularpart_name IF NOT EXISTS FOR (t:TabularPart) ON (t.name)",
                "CREATE INDEX idx_attribute_name IF NOT EXISTS FOR (a:Attribute) ON (a.name)",
                "CREATE INDEX idx_resource_name IF NOT EXISTS FOR (r:Resource) ON (r.name)",
                "CREATE INDEX idx_dimension_name IF NOT EXISTS FOR (d:Dimension) ON (d.name)",
                "CREATE INDEX idx_form_name IF NOT EXISTS FOR (f:Form) ON (f.name)",
                "CREATE INDEX idx_command_name IF NOT EXISTS FOR (c:Command) ON (c.name)",
                "CREATE INDEX idx_layout_name IF NOT EXISTS FOR (l:Layout) ON (l.name)",
                "CREATE INDEX idx_characteristic_name IF NOT EXISTS FOR (s:Characteristic) ON (s.name)",
                "CREATE INDEX idx_enumvalue_name IF NOT EXISTS FOR (v:EnumValue) ON (v.name)",
                "CREATE INDEX idx_urltemplate_name IF NOT EXISTS FOR (t:UrlTemplate) ON (t.name)",
                "CREATE INDEX idx_urlmethod_name IF NOT EXISTS FOR (m:UrlMethod) ON (m.name)",
                "CREATE INDEX idx_urlmethod_handler_ru IF NOT EXISTS FOR (m:UrlMethod) ON (m.`Обработчик`)",
                "CREATE INDEX idx_journalgraph_name IF NOT EXISTS FOR (g:JournalGraph) ON (g.name)",
                "CREATE INDEX idx_accountingflag_name IF NOT EXISTS FOR (af:AccountingFlag) ON (af.name)",
                "CREATE INDEX idx_dimensionaccountingflag_name IF NOT EXISTS FOR (sf:DimensionAccountingFlag) ON (sf.name)",
                "CREATE INDEX idx_predefineditem_name IF NOT EXISTS FOR (p:PredefinedItem) ON (p.name)",
                "CREATE INDEX idx_formcontrol_name IF NOT EXISTS FOR (fc:FormControl) ON (fc.name)",
                "CREATE INDEX idx_formattribute_name IF NOT EXISTS FOR (fa:FormAttribute) ON (fa.name)",
                # EventSubscription nodes are stored as MetadataObject (covered by idx_metadataobject_name)

                # FormEvent: properties used in handler linking
                "CREATE INDEX idx_formevent_name IF NOT EXISTS FOR (fe:FormEvent) ON (fe.name)",

                # FormEventAction: indexes for BSL handler linking
                "CREATE INDEX idx_formeventaction_handler IF NOT EXISTS FOR (a:FormEventAction) ON (a.handler_name)",
                "CREATE INDEX idx_formeventaction_call_type IF NOT EXISTS FOR (a:FormEventAction) ON (a.call_type)",

                # Form lookup by config (ADOPTED_FROM extension queries filter by config_name + project_name)
                "CREATE INDEX idx_form_config_proj IF NOT EXISTS FOR (f:Form) ON (f.config_name, f.project_name)",

                # FormControl integer id used as join key in ADOPTED_FROM (ctrl_id = base_control_id)
                "CREATE INDEX idx_formcontrol_ctrl_id IF NOT EXISTS FOR (fc:FormControl) ON (fc.ctrl_id)",

                # Optional: fast owner scan for CommonForms
                "CREATE INDEX idx_metadataobject_proj_category IF NOT EXISTS FOR (m:MetadataObject) ON (m.project_name, m.category_name)",
                "CREATE INDEX idx_metadataobject_proj_category_name IF NOT EXISTS FOR (m:MetadataObject) ON (m.project_name, m.category_name, m.name)",

                # BSL: helpful for read-time and diagnostics
                "CREATE INDEX idx_module_name IF NOT EXISTS FOR (m:Module) ON (m.name)",
                "CREATE INDEX idx_module_type IF NOT EXISTS FOR (m:Module) ON (m.module_type)",
                "CREATE INDEX idx_module_owner_qn IF NOT EXISTS FOR (m:Module) ON (m.owner_qn)",
                "CREATE INDEX idx_routine_name IF NOT EXISTS FOR (r:Routine) ON (r.name)",
                "CREATE INDEX idx_routine_owner_qn IF NOT EXISTS FOR (r:Routine) ON (r.owner_qn)",
                "CREATE INDEX idx_routine_type IF NOT EXISTS FOR (r:Routine) ON (r.routine_type)",
                "CREATE INDEX idx_routine_export IF NOT EXISTS FOR (r:Routine) ON (r.export)",
                "CREATE INDEX idx_routine_directives IF NOT EXISTS FOR (r:Routine) ON (r.directives)",
                "CREATE INDEX idx_routine_is_ssl_api IF NOT EXISTS FOR (r:Routine) ON (r.is_ssl_api)",

                # BSL large-unit lookup (used in retag/hide/delete/visibility paths)
                "CREATE INDEX idx_routine_code_unit_routine_id IF NOT EXISTS FOR (u:RoutineCodeUnit) ON (u.routine_id)",

                # BSL linking accelerators
                "CREATE INDEX idx_routine_proj_owner_name IF NOT EXISTS FOR (r:Routine) ON (r.project_name, r.owner_qn, r.name)",
                # Extension decorator scan: filter routines by project + config (used in create_extension_routine_links)
                "CREATE INDEX idx_routine_proj_config IF NOT EXISTS FOR (r:Routine) ON (r.project_name, r.config_name)",

                # ADOPTED_FROM extension queries: filter by (config_name, project_name) for each node type
                "CREATE INDEX idx_metadataobject_config_proj IF NOT EXISTS FOR (m:MetadataObject) ON (m.config_name, m.project_name)",
                "CREATE INDEX idx_attribute_config_proj IF NOT EXISTS FOR (a:Attribute) ON (a.config_name, a.project_name)",
                "CREATE INDEX idx_tabularpart_config_proj IF NOT EXISTS FOR (t:TabularPart) ON (t.config_name, t.project_name)",
                "CREATE INDEX idx_dimension_config_proj IF NOT EXISTS FOR (d:Dimension) ON (d.config_name, d.project_name)",
                "CREATE INDEX idx_resource_config_proj IF NOT EXISTS FOR (r:Resource) ON (r.config_name, r.project_name)",
                "CREATE INDEX idx_layout_config_proj IF NOT EXISTS FOR (l:Layout) ON (l.config_name, l.project_name)",
                "CREATE INDEX idx_command_config_proj IF NOT EXISTS FOR (c:Command) ON (c.config_name, c.project_name)",
                "CREATE INDEX idx_enumvalue_config_proj IF NOT EXISTS FOR (v:EnumValue) ON (v.config_name, v.project_name)",
                "CREATE INDEX idx_formattribute_config_proj IF NOT EXISTS FOR (fa:FormAttribute) ON (fa.config_name, fa.project_name)",
                "CREATE INDEX idx_form_config_proj IF NOT EXISTS FOR (f:Form) ON (f.config_name, f.project_name)",
                "CREATE INDEX idx_characteristic_config_proj IF NOT EXISTS FOR (n:Characteristic) ON (n.config_name, n.project_name)",
                "CREATE INDEX idx_accountingflag_config_proj IF NOT EXISTS FOR (n:AccountingFlag) ON (n.config_name, n.project_name)",
                "CREATE INDEX idx_dimensionaccountingflag_config_proj IF NOT EXISTS FOR (n:DimensionAccountingFlag) ON (n.config_name, n.project_name)",
                "CREATE INDEX idx_urltemplate_config_proj IF NOT EXISTS FOR (n:UrlTemplate) ON (n.config_name, n.project_name)",
                "CREATE INDEX idx_urlmethod_config_proj IF NOT EXISTS FOR (n:UrlMethod) ON (n.config_name, n.project_name)",
                "CREATE INDEX idx_journalgraph_config_proj IF NOT EXISTS FOR (n:JournalGraph) ON (n.config_name, n.project_name)",

                # Console search: single-column project_name indexes (planner does not pick
                # composite indexes for single-predicate queries on these labels — verified by EXPLAIN).
                "CREATE INDEX idx_routine_project_name IF NOT EXISTS FOR (r:Routine) ON (r.project_name)",
                "CREATE INDEX idx_module_project_name IF NOT EXISTS FOR (m:Module) ON (m.project_name)",
                "CREATE INDEX idx_metadataobject_project_name IF NOT EXISTS FOR (m:MetadataObject) ON (m.project_name)",

                # Console search: scope indexes for fallback CONTAINS path.
                "CREATE INDEX idx_console_search_scope IF NOT EXISTS FOR (n:ConsoleSearchable) ON (n.project_name, n.config_name, n.console_search_section)",
                "CREATE INDEX idx_console_search_project_section IF NOT EXISTS FOR (n:ConsoleSearchable) ON (n.project_name, n.console_search_section)",
            ]

            # 3) Fulltext indexes for flexible name search (Neo4j 5 requires index name)
            fulltext = [
                "CREATE FULLTEXT INDEX ftx_metadataobject_name IF NOT EXISTS FOR (m:MetadataObject) ON EACH [m.name]",
                "CREATE FULLTEXT INDEX ftx_attribute_name IF NOT EXISTS FOR (a:Attribute) ON EACH [a.name]",
                "CREATE FULLTEXT INDEX ftx_tabularpart_name IF NOT EXISTS FOR (t:TabularPart) ON EACH [t.name]",
                "CREATE FULLTEXT INDEX ftx_resource_name IF NOT EXISTS FOR (r:Resource) ON EACH [r.name]",
                "CREATE FULLTEXT INDEX ftx_dimension_name IF NOT EXISTS FOR (d:Dimension) ON EACH [d.name]",
                "CREATE FULLTEXT INDEX ftx_form_name IF NOT EXISTS FOR (f:Form) ON EACH [f.name]",
                "CREATE FULLTEXT INDEX ftx_command_name IF NOT EXISTS FOR (c:Command) ON EACH [c.name]",
                "CREATE FULLTEXT INDEX ftx_layout_name IF NOT EXISTS FOR (l:Layout) ON EACH [l.name]",
                "CREATE FULLTEXT INDEX ftx_characteristic_name IF NOT EXISTS FOR (s:Characteristic) ON EACH [s.name]",
                "CREATE FULLTEXT INDEX ftx_enumvalue_name IF NOT EXISTS FOR (v:EnumValue) ON EACH [v.name]",
                "CREATE FULLTEXT INDEX ftx_urltemplate_name IF NOT EXISTS FOR (t:UrlTemplate) ON EACH [t.name]",
                "CREATE FULLTEXT INDEX ftx_urlmethod_name IF NOT EXISTS FOR (m:UrlMethod) ON EACH [m.name]",
                "CREATE FULLTEXT INDEX ftx_journalgraph_name IF NOT EXISTS FOR (g:JournalGraph) ON EACH [g.name]",
                "CREATE FULLTEXT INDEX ftx_accountingflag_name IF NOT EXISTS FOR (af:AccountingFlag) ON EACH [af.name]",
                "CREATE FULLTEXT INDEX ftx_dimensionaccountingflag_name IF NOT EXISTS FOR (sf:DimensionAccountingFlag) ON EACH [sf.name]",
                "CREATE FULLTEXT INDEX ftx_predefineditem_name IF NOT EXISTS FOR (p:PredefinedItem) ON EACH [p.name]",
                # ftx_routine_name lives in ROUTINE_SEARCH_ACCELERATORS: substring search
                # depends on it, so it needs an owner that also verifies its state, and
                # this loop only logs DDL failures.
                "CREATE FULLTEXT INDEX ftx_routine_directives IF NOT EXISTS FOR (r:Routine) ON EACH [r.directives]",
                "CREATE FULLTEXT INDEX ftx_routine_doc_description IF NOT EXISTS FOR (r:Routine) ON EACH [r.doc_description] OPTIONS { indexConfig: { `fulltext.analyzer`: 'russian', `fulltext.eventually_consistent`: true } }",
                "CREATE FULLTEXT INDEX ftx_metadataobject_description IF NOT EXISTS FOR (m:MetadataObject) ON EACH [m.name, m.`Синоним`, m.`Комментарий`, m.`Справка`, m.`Пояснение`] OPTIONS { indexConfig: { `fulltext.analyzer`: 'russian', `fulltext.eventually_consistent`: true } }",
            ]
            if getattr(settings, "object_summary_enabled", False):
                fulltext.append(
                    "CREATE FULLTEXT INDEX ftx_object_summary_search_text IF NOT EXISTS "
                    "FOR (m:MetadataObject) ON EACH [m.object_summary_search_text] "
                    "OPTIONS { indexConfig: { `fulltext.analyzer`: 'russian', `fulltext.eventually_consistent`: true } }"
                )

            # Console search: composite fulltext over three separate fields, so the
            # query builder can target specific UI 'fields' via Lucene `console_search_name:.../console_search_synonym:.../console_search_type:...`.
            fulltext.append(
                "CREATE FULLTEXT INDEX ftx_console_search_text IF NOT EXISTS "
                "FOR (n:ConsoleSearchable) "
                "ON EACH [n.console_search_name, n.console_search_synonym, n.console_search_type] "
                "OPTIONS { indexConfig: { `fulltext.analyzer`: 'russian', `fulltext.eventually_consistent`: true } }"
            )

            # Drop obsolete indexes (FormEvent.Обработчик moved to FormEventAction.handler_name)
            try:
                session.run("DROP INDEX idx_formevent_handler_ru IF EXISTS")
            except Neo4jError as e:
                logger.debug("Could not drop idx_formevent_handler_ru: %s", e)

            # Apply all definitions
            for stmt in constraints + indexes + fulltext:
                try:
                    session.run(stmt)
                except Neo4jError as e:
                    code = getattr(e, "code", "") or ""
                    msg = str(e)
                    if not settings.enable_debug and (
                        "EquivalentSchemaRuleAlreadyExists" in code
                        or "EquivalentSchemaRuleAlreadyExists" in msg
                        or "An equivalent index already exists" in msg
                        or "already exists" in msg
                        or "already exists" in code
                    ):
                        logger.debug("Index/constraint already exists (suppressed): %s", e)
                    else:
                        logger.warning("Could not create constraint/index: %s", e)

            # NOTE: We intentionally do NOT enforce uniqueness on m.name anymore
            # If an old unique constraint on (m.name) exists from previous versions,
            # it should be dropped manually.
            logger.info("Database indexes and constraints ensured")

            # Routine search schema owns its own DDL + readiness (see ensure_routine_search_schema):
            # it must not go through the error-swallowing loop above.
            ensure_routine_search_schema(session)

        # Create vector indexes if embeddings are enabled
        self.create_vector_indexes(use_startup_probe=use_startup_probe_for_vectors)

    def ensure_bsl_indexes(self) -> None:
        """Ensure constraints/indexes required for fast BSL module/routine loading."""
        with self.driver.session(database=settings.neo4j_database) as session:
            stmts = [
                "CREATE CONSTRAINT uq_module_id IF NOT EXISTS FOR (m:Module) REQUIRE (m.id) IS UNIQUE",
                "CREATE CONSTRAINT uq_routine_id IF NOT EXISTS FOR (r:Routine) REQUIRE (r.id) IS UNIQUE",
                "CREATE CONSTRAINT uq_routine_code_unit_id IF NOT EXISTS FOR (u:RoutineCodeUnit) REQUIRE (u.id) IS UNIQUE",
                "CREATE INDEX idx_routine_code_unit_routine_id IF NOT EXISTS FOR (u:RoutineCodeUnit) ON (u.routine_id)",
            ]
            for stmt in stmts:
                try:
                    session.run(stmt)
                except Neo4jError as e:
                    code = getattr(e, "code", "") or ""
                    msg = str(e)
                    if not settings.enable_debug and (
                        "EquivalentSchemaRuleAlreadyExists" in code
                        or "EquivalentSchemaRuleAlreadyExists" in msg
                        or "An equivalent index already exists" in msg
                        or "already exists" in msg
                        or "already exists" in code
                    ):
                        logger.debug("BSL constraint/index already exists (suppressed): %s", e)
                    else:
                        logger.warning("Could not create BSL constraint/index: %s", e)

    def get_embedding_dimension_from_config(self, *, use_startup_probe: bool = False) -> Optional[int]:
        """
        Automatically determines embedding vector dimension from configured model.
        Makes a test request with a simple word to determine vector size.

        When use_startup_probe is True the probe goes through a short-lived
        EmbeddingService built with a bounded timeout, a single attempt and
        model-info detection disabled (embedding_startup_probe_timeout_seconds),
        instead of the shared singleton. This is used by the restart-path vector
        index repair so an unavailable/slow embedding endpoint cannot stall the
        synchronous startup ensure. The shared singleton is left untouched.

        Returns:
            int: vector dimension or None if embeddings are not configured
        """
        from graphdb.embedding_service import (
            any_embedding_feature_enabled,
            probe_embedding_availability,
            get_embedding_service,
        )

        # Check if ANY embedding feature is enabled (description for routine/metadata,
        # BSL code search via effective gate, or object summary).
        if not any_embedding_feature_enabled():
            logger.debug("All embedding features are disabled (routine description, metadata description, BSL code, object summary)")
            return None

        if use_startup_probe:
            # Bounded startup probe (single source of truth): does not touch the
            # shared singleton, cannot stall synchronous startup on a slow endpoint.
            status = probe_embedding_availability()
            if not status.available:
                logger.warning(
                    "Startup embedding probe unavailable, vector dimension unknown: %s",
                    status.reason,
                )
                return None
            logger.info(
                "Determined embedding dimension: %s (startup probe, model: %s)",
                status.dimension, settings.embedding_model,
            )
            return status.dimension

        embedding_model = settings.embedding_model
        embedding_api_base = settings.embedding_api_base

        if not embedding_model or not embedding_api_base:
            logger.info("Embedding API not fully configured (model/base/key required), vector indexes will not be created")
            return None

        # Validate API base URL
        if not embedding_api_base.startswith(('http://', 'https://')):
            logger.error(
                f"Invalid EMBEDDING_API_BASE: '{embedding_api_base}'. "
                f"URL must start with 'http://' or 'https://'. "
                f"Example: https://api.openai.com/v1"
            )
            return None

        try:
            # Probe goes through EmbeddingService so Perplexity decode and gemini_native
            # transport behave identically to the production embedding path.
            service = get_embedding_service()
            if service is None:
                return None
            vec = service.embed_for_fingerprint("test")
            if not vec:
                logger.warning("Embedding probe returned no vector for model: %s", embedding_model)
                return None
            dimension = len(vec)
            logger.info(f"Determined embedding dimension: {dimension} (model: {embedding_model})")
            return dimension
        except Exception as e:
            logger.warning(f"Could not determine embedding dimension: {e}")
            return None

    def create_vector_indexes(
        self, *, use_startup_probe: bool = False, dimension: Optional[int] = None,
    ) -> None:
        """Creates vector indexes based on configured embedding model.

        use_startup_probe forwards to get_embedding_dimension_from_config so the
        restart-path startup ensure probes the embedding service with a bounded
        timeout instead of the production one. Default False keeps the load and
        incremental call sites on the normal singleton path.

        When `dimension` is provided (e.g. from a startup EmbeddingAvailability
        probe already run by the caller), no second embedding probe is issued.
        """
        if dimension is None:
            dimension = self.get_embedding_dimension_from_config(use_startup_probe=use_startup_probe)

        if dimension is None:
            logger.info("Skipping vector index creation - embeddings not configured or disabled")
            return

        with self.driver.session(database=settings.neo4j_database) as session:
            # Build list of indexes to create based on enabled features
            index_names: List[str] = []
            if settings.enable_routine_description_embedding:
                index_names.append("vec_routine_doc_description")
            if settings.enable_metadata_description_embedding:
                index_names.append("vec_metadataobject_description")
            if _bsl_vector_enabled():
                index_names.append("vec_bsl_code_unit")
            if getattr(settings, "object_summary_enabled", False):
                index_names.append("vec_object_summary_embedding")

            if not index_names:
                logger.info("No vector indexes to create (all embedding features disabled)")
                return

            logger.info(f"Ensuring vector indexes: {', '.join(index_names)} (dimension={dimension})")
            for index_name in index_names:
                try:
                    ensure_vector_index(session, index_name, dimension)
                except Neo4jError as e:
                    logger.warning(f"Could not create vector index {index_name}: {e}")