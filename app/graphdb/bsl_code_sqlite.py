"""
SQLite sidecar for BSL code search.

Storage contract:
- Raw routine body lives ONLY in Neo4j (Routine.body). SQLite stores metadata,
  inverted indexes, field tables for lexical scoring, structural FTS tables
  used by RLM intent-routing, global IDF/avgdl tables for the hybrid scorer,
  and per-scope fingerprint state.
- FTS5 is created with `contentless_delete=1` so retired epochs can be
  physically removed during GC.
- Logical unit_id is TEXT, FTS5 rowid is the integer surrogate fts_rowid.
- Scope = project_name; config_name is a filterable attribute on units.
- Double-buffered rebuild: pending_epoch -> current_epoch (atomic switch).
- Vector readiness is decoupled from SQLite epoch via vector_status/vector_epoch.

`bsl_code_units_fts.text` content = chunk_search_text(row, metadata_weight=6,
metadata_symbol_weight=1.0) — see bsl_code_indexer.build_search_text. This is
what reference IDF/avgdl is built over.

`bsl_code_unit_fields.field_kind` accepts 6 values: symbol, object, form,
metadata_type, path, body — these are the per-field token buckets the
hybrid scorer reads for field BM25, metadata boost and fuzzy boost.

Structural FTS tables (per RLM intent-routing): bsl_code_module_fts,
bsl_code_body_fts, bsl_code_feature_fts, bsl_code_metadata_refs_fts,
bsl_code_query_tables_fts, bsl_code_method_calls_fts,
bsl_code_string_literals_fts, bsl_code_assignments_fts,
bsl_code_identifiers_fts.

IDF caches: bsl_code_corpus_idf(field_kind, token, df) and
bsl_code_corpus_stats(field_kind, doc_count, avgdl). field_kind="_doc"
captures the chunk_search_text corpus used for body BM25.

Public API exposed by the BslCodeSqlite class:

    Read:
        current_epoch(scope)
        vector_state(scope)
        read_fingerprint(scope)
        fetch_unit_metadata(unit_ids, epoch)
        all_units_by_parent(routine_id, epoch)
        fts_bm25(scope, query, epoch, ...)              # over bsl_code_units_fts
        body_bm25(scope, query, epoch, ...)             # over bsl_code_body_fts (RLM)
        structural_bm25(scope, table, query, epoch, ...) # one of intent FTS tables
        field_lookup(unit_ids, epoch)                   # raw field texts for scorer
        read_corpus_stats(epoch, field_kind)
        read_idf(epoch, field_kind, tokens)

    Write:
        begin_pending(scope) -> int
        write_unit(scope, epoch, unit, text_for_fts, fields, structural)
        write_units_batch(scope, epoch, items) -> int
        write_method(scope, epoch, method)
        write_methods_batch(scope, epoch, methods) -> int
        commit_phase_a_module(scope, epoch, rel_path, columns, idf_increments, stats_increments)
        commit_phase_a_module_with_writes(scope, epoch, rel_path, columns, units, done_routines, methods, idf_increments, stats_increments)
        commit_phase_a_modules_batch_with_writes(scope, epoch, modules)
        write_module(scope, epoch, module)
        commit_pending(scope, fingerprint, source_state_hash)
        store_corpus_stats(scope, epoch, field_kind, doc_count, total_length)
        set_vector_status(scope, status, vector_epoch=None)
        request_reindex(scope)

    Maintenance:
        gc_retired_epochs(scope)
        reset_after_full_reload(scope)
        close()
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from config import settings

logger = logging.getLogger(__name__)


BSL_SQLITE_CLEANUP_PROGRESS_ROWS = 10000
BSL_SQLITE_CLEANUP_PROGRESS_SECONDS = 30.0
BSL_SQLITE_DEBUG_PROGRESS_SECONDS = 30.0


def _format_elapsed(seconds: float) -> str:
    total = int(max(0, seconds))
    if total < 60:
        return f"{total}s"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {sec}s"


_MIN_SQLITE_VERSION = (3, 43, 0)
# Schema version stored in PRAGMA user_version. Deploy ships a physically
# fresh sqlite file for this release, so v1 is the baseline. On any FUTURE
# breaking DDL change bump this and _init_schema will drop the legacy tables
# and rebuild. Source of truth for raw BSL is Neo4j Routine.body, so a
# drop+rebuild is non-destructive — Phase A re-populates from Neo4j.
_SCHEMA_VERSION = 1

# Потолок, до которого WAL усекается после чекпойнта. В штатной работе он держится
# около 4 МБ (авточекпойнт на 1000 страницах по 4 КБ), так что это страховка от
# аномального выброса, а не рабочий режим.
_WAL_SIZE_LIMIT_BYTES = 256 * 1024 * 1024


# Per-unit FTS sidecars used by the structural / intent-routing legs. Module
# FTS is module-level (see bsl_code_module_fts below) and is not in this list.
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

# Module-level FTS table. Indexed columns and BM25 weights mirror reference
# 10 BM25 weights for the 10 indexed module-level columns.
MODULE_FTS_TABLE = "bsl_code_module_fts"
_MODULE_FTS_BM25_WEIGHTS = (1.0, 4.0, 2.5, 0.8, 0.7, 1.5, 1.0, 1.2, 1.0, 0.7)
_MODULE_FTS_INDEXED_COLUMNS = (
    "rel_path",
    "object_name",
    "form_name",
    "metadata_type_ru",
    "module_kind",
    "symbols",
    "region_names",
    "headers",
    "comments",
    "body",
)

# Field kinds for bsl_code_unit_fields (6 fields, port of chunk_field_texts).
FIELD_KINDS: Tuple[str, ...] = (
    "symbol",
    "object",
    "form",
    "metadata_type",
    "path",
    "body",
)

# Reserved field_kind for the chunk_search_text corpus (body BM25 IDF/avgdl).
DOC_FIELD_KIND = "_doc"


# Project-scoped tables cleaned by a plain `WHERE project_name = ?` delete.
# Single source of truth for the generic leg of reset_after_full_reload; the
# completeness test iterates this list to catch a future table that is added to
# the write path but forgotten in full-reload cleanup. Tables with special
# cleanup semantics are intentionally NOT here and are handled explicitly:
#   - contentless FTS (bsl_code_units_fts, STRUCTURAL_FTS_TABLES, MODULE_FTS_TABLE)
#     are deleted by rowid;
#   - bsl_code_unit_fields is keyed by fts_rowid (no project_name column);
#   - bsl_code_search_fingerprints is the scope row itself (deleted last).
PROJECT_SCOPED_TABLES: Tuple[str, ...] = (
    "bsl_code_units",
    "bsl_code_methods",
    "bsl_code_modules",
    "bsl_code_module_fts_rows",
    "bsl_code_module_fragments",
    "bsl_code_corpus_idf",
    "bsl_code_corpus_stats",
    "bsl_code_phase_a_routine_state",
    "bsl_code_phase_b_unit_state",
    "bsl_code_pending_reverse_snapshot",
    "bsl_code_pending_scoped_delta",
    # Config-delete операция тоже project-scoped: full reload сносит очередь
    # `extension_scope_cleanup` (её владельца), поэтому оставшиеся здесь строки
    # стали бы вечным `CONFIG_DELETE_IN_PROGRESS` без возможности teardown.
    "bsl_code_config_delete_ops",
    "bsl_code_config_delete_routines",
)


class _SqlitePhaseADebugProfiler:
    def __init__(self, seconds_interval: float = BSL_SQLITE_DEBUG_PROGRESS_SECONDS) -> None:
        self.seconds_interval = max(1.0, float(seconds_interval))
        self.started_at = time.perf_counter()
        self.last_logged_at = self.started_at
        self.section_ms: Dict[str, float] = {}
        self.row_counts: Dict[str, int] = {}

    def start(self) -> float:
        return time.perf_counter()

    def add_ms(self, section: str, started_at: float) -> float:
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        self.section_ms[section] = self.section_ms.get(section, 0.0) + elapsed_ms
        return elapsed_ms

    def add_rows(self, **rows: int) -> None:
        for key, value in rows.items():
            self.row_counts[key] = self.row_counts.get(key, 0) + int(value)

    def maybe_log(self, *, final: bool = False) -> None:
        now = time.perf_counter()
        if not final and now - self.last_logged_at < self.seconds_interval:
            return
        logger.debug(
            "BSL sqlite Phase A profile: "
            "units_write=%d done_state=%d pending_progress_update=%d "
            "corpus_idf_upsert=%d corpus_stats_upsert=%d "
            "methods_insert=%d module_row_insert=%d module_fts_insert=%d "
            "commit=%d, rows units=%d methods=%d idf=%d stats=%d modules=%d",
            int(self.section_ms.get("units_write", 0.0)),
            int(self.section_ms.get("done_state", 0.0)),
            int(self.section_ms.get("pending_progress_update", 0.0)),
            int(self.section_ms.get("corpus_idf_upsert", 0.0)),
            int(self.section_ms.get("corpus_stats_upsert", 0.0)),
            int(self.section_ms.get("methods_insert", 0.0)),
            int(self.section_ms.get("module_row_insert", 0.0)),
            int(self.section_ms.get("module_fts_insert", 0.0)),
            int(self.section_ms.get("commit", 0.0)),
            self.row_counts.get("units", 0),
            self.row_counts.get("methods", 0),
            self.row_counts.get("idf", 0),
            self.row_counts.get("stats", 0),
            self.row_counts.get("modules", 0),
        )
        self.last_logged_at = now


@dataclass
class UnitMeta:
    unit_id: str
    routine_id: str
    routine_name: str
    project_name: str
    config_name: str
    owner_qn: str
    owner_qn_prefix: str
    owner_category: str
    module_type: str
    module_kind: str
    routine_type: str
    export: bool
    line_start: int
    line_end: int
    char_start: int
    char_end: int
    part_index: int
    part_total: int
    body_hash: str
    rel_path: str
    index_epoch: int
    unit_kind: str  # "routine" or "routine_code_unit"
    is_regulated_report: bool = False


@dataclass
class PhaseAModuleCommit:
    rel_path: str
    columns: Dict[str, str]
    units: List[Dict[str, Any]]
    done_routines: List[Dict[str, Any]]
    methods: List[Dict[str, Any]]
    idf_increments: Dict[str, Dict[str, int]]
    stats_increments: Dict[str, Tuple[int, int]]
    module_fragments: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class _UnitSidecarRows:
    units_fts: List[Tuple[int, str]] = field(default_factory=list)
    fields: List[Tuple[int, str, str]] = field(default_factory=list)
    structural: Dict[str, List[Tuple[Any, ...]]] = field(default_factory=dict)


@dataclass
class VectorState:
    status: str   # 'not_started' | 'building' | 'ready' | 'failed'
    vector_epoch: Optional[int]
    embedding_fingerprint: str = ''


class BslCodeSqliteError(RuntimeError):
    pass


class CommitOutcome(Enum):
    """Исход CAS-финала scoped mutation.

    Причина отказа нужна вызывающему не для лога, а для выбора владельца
    восстановления: pending разрешится сам, устаревшая epoch означает, что
    rebuild уже всё построил, а reindex/fingerprint требуют передать
    восстановление полной перестройке (иначе операция за destructive-границей
    остаётся без выхода).
    """

    COMMITTED = "committed"
    REJECTED_PENDING = "rejected_pending"
    REJECTED_EPOCH = "rejected_epoch"
    REJECTED_FINGERPRINT = "rejected_fingerprint"
    REJECTED_REINDEX = "rejected_reindex"

    @property
    def ok(self) -> bool:
        return self is CommitOutcome.COMMITTED


class BslCodeSqlite:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._db_path = Path(db_path or settings.bsl_code_search_sqlite_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._phase_a_debug_profiler: Optional[_SqlitePhaseADebugProfiler] = None

        self._check_sqlite_version()

        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row

        self._init_pragmas()
        self._init_schema()
        self._ensure_additive_tables()

    # ------------------------------------------------------------------ init

    def _debug_phase_a_profiler(self) -> Optional[_SqlitePhaseADebugProfiler]:
        if not logger.isEnabledFor(logging.DEBUG):
            return None
        if self._phase_a_debug_profiler is None:
            self._phase_a_debug_profiler = _SqlitePhaseADebugProfiler()
        return self._phase_a_debug_profiler

    def _check_sqlite_version(self) -> None:
        if sqlite3.sqlite_version_info < _MIN_SQLITE_VERSION:
            raise BslCodeSqliteError(
                f"BSL code search requires SQLite >= "
                f"{'.'.join(map(str, _MIN_SQLITE_VERSION))} (for FTS5 "
                f"contentless_delete=1), but linked sqlite is "
                f"{sqlite3.sqlite_version}."
            )

    def _init_pragmas(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA foreign_keys=ON")
            # Умолчание SQLite — 2 МБ страничного кэша, что на базе в сотни
            # мегабайт даёт постоянный промах на записи в FTS-индексы.
            cache_kib = max(1, int(settings.bsl_code_sqlite_cache_mb)) * 1024
            cur.execute(f"PRAGMA cache_size=-{cache_kib}")
            # После чекпойнта WAL усекается до этого размера. Без лимита файл
            # навсегда остаётся на пиковом размере, даже когда чекпойнты проходят
            # штатно: SQLite переиспользует его, а не обрезает.
            cur.execute(f"PRAGMA journal_size_limit={_WAL_SIZE_LIMIT_BYTES}")
            # `synchronous` намеренно оставлен в умолчании (FULL). Соединение одно
            # на весь класс, поэтому послабление применилось бы и к записям, которые
            # служат durable-барьером перед мутациями Neo4j: снапшот обратных
            # счётчиков и ledger перед перезаписью тел, gate и scoped_retry_pending
            # перед сменой visibility. Потеря хвоста коммитов при отказе питания
            # оставила бы наполовину применённую операцию без возможности доиграть.

    def _user_version(self) -> int:
        with self._lock:
            row = self._conn.execute("PRAGMA user_version").fetchone()
            return int(row[0]) if row else 0

    def _set_user_version(self, v: int) -> None:
        with self._lock:
            self._conn.execute(f"PRAGMA user_version = {int(v)}")

    def _init_schema(self) -> None:
        # Detection contract: PRAGMA user_version is the schema generation
        # marker. Fresh deploy of this release ships an empty sqlite file →
        # existing == 0 → we apply the v1 schema. FUTURE breaking DDL changes
        # bump _SCHEMA_VERSION, and this branch then drops legacy tables and
        # rebuilds — no data migration. Raw BSL lives in Neo4j, so a rebuild
        # is non-destructive (Phase A re-populates from Routine.body).
        with self._lock:
            existing = self._user_version()
            if existing == _SCHEMA_VERSION:
                return
            if existing > _SCHEMA_VERSION:
                raise BslCodeSqliteError(
                    f"BSL code search SQLite at {self._db_path} has schema "
                    f"version {existing}, but this code understands only up "
                    f"to {_SCHEMA_VERSION}."
                )
            if existing > 0:
                # Older schema generation present — drop all known BSL code
                # search tables and rebuild from scratch.
                self._drop_legacy_bsl_tables()
            self._apply_schema()
            self._set_user_version(_SCHEMA_VERSION)

    def _ensure_additive_tables(self) -> None:
        """Additive-миграция: догнать таблицы, добавленные ПОСЛЕ выпуска схемы.

        Отдельный шаг, а не строки в `_apply_schema`, потому что для уже
        существующего sidecar ни одна DDL оттуда не выполнится: `_init_schema`
        выходит при `user_version == _SCHEMA_VERSION`, а `_apply_schema`
        дополнительно выходит, если `bsl_code_units` уже есть.

        Повышение `_SCHEMA_VERSION` здесь недопустимо: ветка version bump
        вызывает `_drop_legacy_bsl_tables()` и перестраивает индекс с нуля,
        то есть запускает ровно тот полный Phase A, который config-delete и
        призван устранить. Приём тот же, что у
        `IncrementalLoadingState._migrate_metadata_object_hashes`.

        Идемпотентен: только `IF NOT EXISTS`, выполняется при каждом открытии.
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bsl_code_config_delete_ops (
                        project_name   TEXT NOT NULL,
                        operation_id   TEXT NOT NULL,
                        config_name    TEXT NOT NULL,
                        index_epoch    INTEGER NOT NULL,
                        -- nullable и НЕ участвует ни в одном решении: при
                        -- ENABLE_BSL_CODE_EMBEDDING=false (значение по
                        -- умолчанию) vector_epoch штатно NULL. Только диагностика.
                        vector_epoch   INTEGER,
                        fingerprint    TEXT NOT NULL DEFAULT '',
                        rel_paths_json TEXT NOT NULL DEFAULT '[]',
                        serving_count  INTEGER NOT NULL DEFAULT 0,
                        graph_count    INTEGER NOT NULL DEFAULT 0,
                        state          TEXT NOT NULL,
                        fallback_reason TEXT NOT NULL DEFAULT '',
                        created_at     INTEGER NOT NULL,
                        updated_at     INTEGER NOT NULL,
                        PRIMARY KEY (project_name, operation_id)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bsl_code_config_delete_routines (
                        project_name TEXT NOT NULL,
                        operation_id TEXT NOT NULL,
                        routine_id   TEXT NOT NULL,
                        rel_path     TEXT NOT NULL DEFAULT '',
                        in_serving   INTEGER NOT NULL DEFAULT 1,
                        idf_json     TEXT NOT NULL DEFAULT '{}',
                        stats_json   TEXT NOT NULL DEFAULT '{}',
                        stage        TEXT NOT NULL,
                        PRIMARY KEY (project_name, operation_id, routine_id)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cfg_delete_routines_stage "
                    "ON bsl_code_config_delete_routines("
                    "project_name, operation_id, stage)"
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def _drop_legacy_bsl_tables(self) -> None:
        # Drop everything the BSL code search subsystem owns. Used only when
        # a future schema bump is detected; on a fresh DB this is unreachable.
        known_tables = (
            "bsl_code_units",
            "bsl_code_units_fts",
            "bsl_code_unit_fields",
            "bsl_code_modules",
            "bsl_code_methods",
            "bsl_code_module_fts",
            "bsl_code_module_fts_rows",
            "bsl_code_module_fts_fragments",
            "bsl_code_corpus_idf",
            "bsl_code_corpus_stats",
            "bsl_code_phase_a_routine_state",
            "bsl_code_phase_b_unit_state",
            "bsl_code_search_fingerprints",
            "bsl_code_pending_reverse_snapshot",
            "bsl_code_pending_scoped_delta",
            "bsl_code_config_delete_ops",
            "bsl_code_config_delete_routines",
            "bsl_code_module_fragments",
            *STRUCTURAL_FTS_TABLES,
        )
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                for tbl in known_tables:
                    cur.execute(f"DROP TABLE IF EXISTS {tbl}")
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def _apply_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            # Idempotent guard: if the foundational table already exists, the
            # schema was applied earlier (same process or prior run on a fresh
            # DB). No detection-of-old-version logic — fresh deploy gives a
            # fresh file.
            row = cur.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='bsl_code_units'"
            ).fetchone()
            if row is not None:
                return
            cur.execute("BEGIN")
            try:
                cur.execute(
                    """
                    CREATE TABLE bsl_code_units (
                        fts_rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
                        unit_id         TEXT NOT NULL,
                        routine_id      TEXT NOT NULL,
                        routine_name    TEXT NOT NULL DEFAULT '',
                        project_name    TEXT NOT NULL,
                        config_name     TEXT NOT NULL DEFAULT '',
                        owner_qn        TEXT NOT NULL DEFAULT '',
                        owner_qn_prefix TEXT NOT NULL DEFAULT '',
                        owner_category  TEXT NOT NULL DEFAULT '',
                        module_type     TEXT NOT NULL DEFAULT '',
                        module_kind     TEXT NOT NULL DEFAULT '',
                        routine_type    TEXT NOT NULL DEFAULT '',
                        export          INTEGER NOT NULL DEFAULT 0,
                        line_start      INTEGER NOT NULL DEFAULT 0,
                        line_end        INTEGER NOT NULL DEFAULT 0,
                        char_start      INTEGER NOT NULL DEFAULT 0,
                        char_end        INTEGER NOT NULL DEFAULT 0,
                        part_index      INTEGER NOT NULL DEFAULT 0,
                        part_total      INTEGER NOT NULL DEFAULT 1,
                        body_hash       TEXT NOT NULL DEFAULT '',
                        rel_path        TEXT NOT NULL DEFAULT '',
                        size_chars      INTEGER NOT NULL DEFAULT 0,
                        size_lines      INTEGER NOT NULL DEFAULT 0,
                        index_epoch     INTEGER NOT NULL,
                        unit_kind       TEXT NOT NULL DEFAULT 'routine_code_unit',
                        is_regulated_report INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(index_epoch, unit_id)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX idx_units_scope_epoch "
                    "ON bsl_code_units(project_name, index_epoch)"
                )
                cur.execute(
                    "CREATE INDEX idx_units_routine_epoch "
                    "ON bsl_code_units(routine_id, index_epoch)"
                )
                cur.execute(
                    "CREATE INDEX idx_units_config "
                    "ON bsl_code_units(project_name, config_name, index_epoch)"
                )
                cur.execute(
                    "CREATE INDEX idx_units_scope_epoch_rel_path "
                    "ON bsl_code_units(project_name, index_epoch, rel_path, routine_id)"
                )

                # Weighted chunk_search_text corpus (used by body BM25 in hybrid scorer).
                cur.execute(
                    """
                    CREATE VIRTUAL TABLE bsl_code_units_fts USING fts5(
                        text,
                        content='',
                        contentless_delete=1,
                        tokenize='unicode61'
                    )
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE bsl_code_unit_fields (
                        fts_rowid    INTEGER NOT NULL,
                        field_kind   TEXT NOT NULL,
                        field_text   TEXT NOT NULL,
                        PRIMARY KEY (fts_rowid, field_kind),
                        FOREIGN KEY (fts_rowid) REFERENCES bsl_code_units(fts_rowid)
                            ON DELETE CASCADE
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX idx_unit_fields_kind "
                    "ON bsl_code_unit_fields(field_kind)"
                )

                cur.execute(
                    """
                    CREATE TABLE bsl_code_modules (
                        module_id       TEXT NOT NULL,
                        project_name    TEXT NOT NULL,
                        config_name     TEXT NOT NULL DEFAULT '',
                        module_type     TEXT NOT NULL DEFAULT '',
                        module_kind     TEXT NOT NULL DEFAULT '',
                        owner_qn        TEXT NOT NULL DEFAULT '',
                        owner_category  TEXT NOT NULL DEFAULT '',
                        rel_path        TEXT NOT NULL DEFAULT '',
                        index_epoch     INTEGER NOT NULL,
                        PRIMARY KEY (index_epoch, module_id)
                    )
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE bsl_code_methods (
                        routine_id      TEXT NOT NULL,
                        module_id       TEXT NOT NULL DEFAULT '',
                        project_name    TEXT NOT NULL,
                        config_name     TEXT NOT NULL DEFAULT '',
                        name            TEXT NOT NULL DEFAULT '',
                        signature       TEXT NOT NULL DEFAULT '',
                        routine_type    TEXT NOT NULL DEFAULT '',
                        symbol_kind     TEXT NOT NULL DEFAULT '',
                        export          INTEGER NOT NULL DEFAULT 0,
                        owner_qn        TEXT NOT NULL DEFAULT '',
                        body_hash       TEXT NOT NULL DEFAULT '',
                        size_chars      INTEGER NOT NULL DEFAULT 0,
                        size_lines      INTEGER NOT NULL DEFAULT 0,
                        index_epoch     INTEGER NOT NULL,
                        PRIMARY KEY (index_epoch, routine_id)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX idx_methods_scope_epoch "
                    "ON bsl_code_methods(project_name, index_epoch)"
                )

                # Module-level FTS: contentless. Scope metadata
                # (project_name / index_epoch / module_key / rel_path) lives
                # in the side table bsl_code_module_fts_rows, joined via
                # FTS rowid. Avoids storing raw BSL outside FTS inverted
                # index (plan invariant).
                module_columns_sql = ",\n                            ".join(
                    f"{col}" for col in _MODULE_FTS_INDEXED_COLUMNS
                )
                cur.execute(
                    f"""
                    CREATE VIRTUAL TABLE {MODULE_FTS_TABLE} USING fts5(
                        {module_columns_sql},
                        content='',
                        contentless_delete=1,
                        tokenize='unicode61'
                    )
                    """
                )

                # Side-table holding scope/lookup metadata for module FTS.
                # module_rowid == rowid in bsl_code_module_fts (we insert
                # explicitly with the same rowid in both tables).
                cur.execute(
                    """
                    CREATE TABLE bsl_code_module_fts_rows (
                        module_rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                        project_name TEXT NOT NULL,
                        index_epoch  INTEGER NOT NULL,
                        module_key   TEXT NOT NULL,
                        rel_path     TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX idx_module_fts_rows_scope_epoch "
                    "ON bsl_code_module_fts_rows(project_name, index_epoch)"
                )
                cur.execute(
                    "CREATE INDEX idx_module_fts_rows_module_key "
                    "ON bsl_code_module_fts_rows(project_name, index_epoch, module_key)"
                )
                cur.execute(
                    "CREATE INDEX idx_module_fts_rows_rel_path "
                    "ON bsl_code_module_fts_rows(project_name, index_epoch, rel_path)"
                )

                for table in STRUCTURAL_FTS_TABLES:
                    if table == "bsl_code_structural_fts":
                        # Multi-column FTS used by the structural base RLM leg.
                        # Reference structural_fts has 5 indexed columns with
                        # BM25 weights 2.5/3.0/1.8/1.2/1.0.
                        cur.execute(
                            f"""
                            CREATE VIRTUAL TABLE {table} USING fts5(
                                metadata_refs,
                                query_tables,
                                method_calls,
                                string_literals,
                                assignments,
                                content='',
                                contentless_delete=1,
                                tokenize='unicode61'
                            )
                            """
                        )
                    else:
                        cur.execute(
                            f"""
                            CREATE VIRTUAL TABLE {table} USING fts5(
                                text,
                                content='',
                                contentless_delete=1,
                                tokenize='unicode61'
                            )
                            """
                        )

                # Global IDF cache, scoped by (project_name, index_epoch,
                # field_kind). epoch numbers are local to a scope, so the
                # composite key must include project_name to keep different
                # project rebuilds from clobbering each other's IDF/stats.
                # field_kind = "_doc" holds the chunk_search_text corpus IDF
                # used by body BM25; other rows match FIELD_KINDS entries.
                cur.execute(
                    """
                    CREATE TABLE bsl_code_corpus_idf (
                        project_name TEXT    NOT NULL,
                        index_epoch  INTEGER NOT NULL,
                        field_kind   TEXT    NOT NULL,
                        token        TEXT    NOT NULL,
                        df           INTEGER NOT NULL,
                        PRIMARY KEY (project_name, index_epoch, field_kind, token)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX idx_corpus_idf_lookup "
                    "ON bsl_code_corpus_idf(project_name, index_epoch, field_kind)"
                )

                # Internal storage is (doc_count, total_length); avgdl is
                # computed on read as total_length / max(doc_count, 1). This
                # keeps module-boundary UPSERT additive (avgdl is not
                # additive across modules).
                cur.execute(
                    """
                    CREATE TABLE bsl_code_corpus_stats (
                        project_name TEXT    NOT NULL,
                        index_epoch  INTEGER NOT NULL,
                        field_kind   TEXT    NOT NULL,
                        doc_count    INTEGER NOT NULL,
                        total_length INTEGER NOT NULL,
                        PRIMARY KEY (project_name, index_epoch, field_kind)
                    )
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE bsl_code_search_fingerprints (
                        project_name              TEXT PRIMARY KEY,
                        current_epoch             INTEGER NOT NULL DEFAULT 0,
                        pending_epoch             INTEGER,
                        vector_status             TEXT NOT NULL DEFAULT 'not_started',
                        vector_epoch              INTEGER,
                        fingerprint               TEXT NOT NULL DEFAULT '',
                        source_state_hash         TEXT NOT NULL DEFAULT '',
                        reindex_requested         INTEGER NOT NULL DEFAULT 0,
                        indexed_at                TEXT,
                        retired_epochs            TEXT NOT NULL DEFAULT '[]',
                        pending_fingerprint       TEXT NOT NULL DEFAULT '',
                        pending_source_state_hash TEXT NOT NULL DEFAULT '',
                        pending_status            TEXT NOT NULL DEFAULT 'idle',
                        pending_started_at        TEXT,
                        pending_updated_at        TEXT,
                        pending_total_routines    INTEGER NOT NULL DEFAULT 0,
                        pending_done_routines     INTEGER NOT NULL DEFAULT 0,
                        coverage_policy_json      TEXT NOT NULL DEFAULT '',
                        coverage_fingerprint      TEXT NOT NULL DEFAULT '',
                        scoped_apply_in_progress  INTEGER NOT NULL DEFAULT 0,
                        scoped_retry_pending      INTEGER NOT NULL DEFAULT 0,
                        visibility_flip_done      INTEGER NOT NULL DEFAULT 0,
                        pending_routine_ids_json  TEXT    NOT NULL DEFAULT '[]',
                        pending_rel_paths_json    TEXT    NOT NULL DEFAULT '[]',
                        embedding_fingerprint     TEXT    NOT NULL DEFAULT '',
                        phase_b_transfer_prev_current_epoch         INTEGER,
                        phase_b_transfer_prev_vector_status         TEXT NOT NULL DEFAULT '',
                        phase_b_transfer_prev_vector_epoch          INTEGER,
                        phase_b_transfer_prev_phase_a_fingerprint   TEXT NOT NULL DEFAULT '',
                        phase_b_transfer_prev_embedding_fingerprint TEXT NOT NULL DEFAULT ''
                    )
                    """
                )

                # Per-routine reverse contribution counters captured before a
                # scoped apply rewrites/deletes the routine's persisted units.
                # Source: computed via compute_contributions_from_routine_record
                # over the OLD Neo4j routine record. Lifetime: from snapshot
                # write to the SQLite tx that applies the matching units (the
                # tx clears the snapshot row in the same transaction).
                cur.execute(
                    """
                    CREATE TABLE bsl_code_pending_reverse_snapshot (
                        project_name TEXT NOT NULL,
                        routine_id   TEXT NOT NULL,
                        idf_json     TEXT NOT NULL DEFAULT '{}',
                        stats_json   TEXT NOT NULL DEFAULT '{}',
                        created_at   INTEGER NOT NULL,
                        PRIMARY KEY (project_name, routine_id)
                    )
                    """
                )

                # Durable ledger for in-flight scoped apply. Replay reads this
                # table to figure out which routines are at which stage and
                # what work is still pending after a crash.
                cur.execute(
                    """
                    CREATE TABLE bsl_code_pending_scoped_delta (
                        project_name        TEXT NOT NULL,
                        routine_id          TEXT NOT NULL,
                        change_kind         TEXT NOT NULL,
                        old_rel_path        TEXT NOT NULL DEFAULT '',
                        new_rel_path        TEXT NOT NULL DEFAULT '',
                        vector_epoch_target INTEGER NOT NULL DEFAULT 0,
                        stage               TEXT NOT NULL,
                        updated_at          INTEGER NOT NULL,
                        PRIMARY KEY (project_name, routine_id)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX idx_scoped_delta_stage "
                    "ON bsl_code_pending_scoped_delta(project_name, stage)"
                )

                # Per-routine module fragments emitted by Phase A worker. Used
                # by scoped module FTS rebuild — module aggregate cannot be
                # restored from contentless bsl_code_module_fts.
                cur.execute(
                    """
                    CREATE TABLE bsl_code_module_fragments (
                        project_name    TEXT NOT NULL,
                        index_epoch     INTEGER NOT NULL,
                        routine_id      TEXT NOT NULL,
                        rel_path        TEXT NOT NULL,
                        routine_ordinal INTEGER NOT NULL DEFAULT 0,
                        fragment_json   TEXT NOT NULL,
                        PRIMARY KEY (project_name, index_epoch, routine_id)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX idx_module_fragments_rel_path "
                    "ON bsl_code_module_fragments(project_name, index_epoch, "
                    "rel_path, routine_ordinal)"
                )

                cur.execute(
                    """
                    CREATE TABLE bsl_code_phase_a_routine_state (
                        project_name  TEXT NOT NULL,
                        index_epoch   INTEGER NOT NULL,
                        routine_id    TEXT NOT NULL,
                        body_hash     TEXT NOT NULL DEFAULT '',
                        units_written INTEGER NOT NULL DEFAULT 0,
                        done_at       TEXT NOT NULL,
                        PRIMARY KEY (project_name, index_epoch, routine_id)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX idx_phase_a_state_scope_epoch "
                    "ON bsl_code_phase_a_routine_state(project_name, index_epoch)"
                )

                # Phase B unit-level done markers (resume tracking).
                cur.execute(
                    """
                    CREATE TABLE bsl_code_phase_b_unit_state (
                        project_name TEXT NOT NULL,
                        vector_epoch INTEGER NOT NULL,
                        unit_id      TEXT NOT NULL,
                        routine_id   TEXT NOT NULL,
                        unit_kind    TEXT NOT NULL,
                        body_hash    TEXT NOT NULL DEFAULT '',
                        done_at      TEXT NOT NULL,
                        PRIMARY KEY (project_name, vector_epoch, unit_id)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX idx_phase_b_state_scope_epoch "
                    "ON bsl_code_phase_b_unit_state(project_name, vector_epoch)"
                )
                cur.execute(
                    "CREATE INDEX idx_phase_b_state_routine "
                    "ON bsl_code_phase_b_unit_state(project_name, vector_epoch, routine_id)"
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------ helpers

    def _retention_seconds(self) -> int:
        return int(getattr(settings, "effective_sqlite_epoch_retention_seconds", 60))

    def _ensure_fingerprint_row(self, scope: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO bsl_code_search_fingerprints(project_name) "
                "VALUES(?)",
                (scope,),
            )

    # ------------------------------------------------------------------ read API

    def current_epoch(self, scope: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT current_epoch FROM bsl_code_search_fingerprints "
                "WHERE project_name = ?",
                (scope,),
            ).fetchone()
            return int(row["current_epoch"]) if row else 0

    def vector_state(self, scope: str) -> VectorState:
        with self._lock:
            row = self._conn.execute(
                "SELECT vector_status, vector_epoch, embedding_fingerprint "
                "FROM bsl_code_search_fingerprints WHERE project_name = ?",
                (scope,),
            ).fetchone()
            if not row:
                return VectorState(
                    status="not_started", vector_epoch=None,
                    embedding_fingerprint='',
                )
            return VectorState(
                status=str(row["vector_status"] or "not_started"),
                vector_epoch=int(row["vector_epoch"]) if row["vector_epoch"] is not None else None,
                embedding_fingerprint=str(row["embedding_fingerprint"] or ''),
            )

    def read_fingerprint(self, scope: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM bsl_code_search_fingerprints WHERE project_name = ?",
                (scope,),
            ).fetchone()
            return dict(row) if row else {}

    def fetch_unit_metadata(
        self, unit_ids: Sequence[str], epoch: int
    ) -> List[UnitMeta]:
        if not unit_ids:
            return []
        placeholders = ",".join("?" * len(unit_ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM bsl_code_units "
                f"WHERE index_epoch = ? AND unit_id IN ({placeholders})",
                (epoch, *unit_ids),
            ).fetchall()
        return [self._row_to_unit_meta(r) for r in rows]

    def all_units_by_parent(self, routine_id: str, epoch: int) -> List[UnitMeta]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM bsl_code_units "
                "WHERE routine_id = ? AND index_epoch = ? "
                "ORDER BY part_index",
                (routine_id, epoch),
            ).fetchall()
        return [self._row_to_unit_meta(r) for r in rows]

    @staticmethod
    def _row_to_unit_meta(row: sqlite3.Row) -> UnitMeta:
        # routine_name / char_start / char_end may be absent in rows written
        # before the drift-fix migration; fall back to schema defaults.
        keys = row.keys() if hasattr(row, "keys") else ()
        return UnitMeta(
            unit_id=row["unit_id"],
            routine_id=row["routine_id"],
            routine_name=(row["routine_name"] if "routine_name" in keys else "") or "",
            project_name=row["project_name"],
            config_name=row["config_name"] or "",
            owner_qn=row["owner_qn"] or "",
            owner_qn_prefix=row["owner_qn_prefix"] or "",
            owner_category=row["owner_category"] or "",
            module_type=row["module_type"] or "",
            module_kind=row["module_kind"] or "",
            routine_type=row["routine_type"] or "",
            export=bool(row["export"]),
            line_start=int(row["line_start"]),
            line_end=int(row["line_end"]),
            char_start=int(row["char_start"]) if "char_start" in keys else 0,
            char_end=int(row["char_end"]) if "char_end" in keys else 0,
            part_index=int(row["part_index"]),
            part_total=int(row["part_total"]),
            body_hash=row["body_hash"] or "",
            rel_path=row["rel_path"] or "",
            index_epoch=int(row["index_epoch"]),
            unit_kind=row["unit_kind"] or "routine_code_unit",
            is_regulated_report=bool(
                row["is_regulated_report"] if "is_regulated_report" in keys else 0
            ),
        )

    def fts_bm25(
        self,
        scope: str,
        query: str,
        epoch: int,
        limit: int = 50,
        config_name: Optional[str] = None,
        module_type: Optional[str] = None,
        routine_type: Optional[str] = None,
        export: Optional[bool] = None,
        owner_categories: Optional[Sequence[str]] = None,
        unit_ids: Optional[Sequence[str]] = None,
        excluded_owner_categories: Optional[Sequence[str]] = None,
        exclude_regulated_reports: bool = False,
    ) -> List[Tuple[str, float]]:
        """BM25 over bsl_code_units_fts (weighted chunk_search_text corpus)."""
        return self._bm25_over_fts(
            "bsl_code_units_fts", scope, query, epoch, limit,
            config_name, module_type, routine_type, export,
            owner_categories, unit_ids, prefix=False,
            excluded_owner_categories=excluded_owner_categories,
            exclude_regulated_reports=exclude_regulated_reports,
        )

    def body_bm25(
        self,
        scope: str,
        query: str,
        epoch: int,
        limit: int = 100,
        config_name: Optional[str] = None,
        module_type: Optional[str] = None,
        routine_type: Optional[str] = None,
        export: Optional[bool] = None,
        owner_categories: Optional[Sequence[str]] = None,
        prefix: bool = False,
    ) -> List[Tuple[str, float]]:
        """BM25 over bsl_code_body_fts (raw body corpus) — RLM source leg."""
        return self._bm25_over_fts(
            "bsl_code_body_fts", scope, query, epoch, limit,
            config_name, module_type, routine_type, export,
            owner_categories, None, prefix=prefix,
        )

    def module_fts_search(
        self,
        scope: str,
        query: str,
        epoch: int,
        limit: int = 20,
        prefix: bool = False,
        max_terms: int = 12,
        rel_paths: Optional[Sequence[str]] = None,
    ) -> List[Tuple[str, float]]:
        """
        Search the module-level FTS and return (module_key, raw_bm25).
        Joins the contentless FTS table with the side table for scope/epoch
        filtering, because the FTS itself stores only indexed-only columns.
        """
        sanitized = _sanitize_fts_query(query, prefix=prefix, max_terms=max_terms)
        if not sanitized:
            return []
        if rel_paths is not None and not rel_paths:
            return []
        weights = ", ".join(f"{w}" for w in _MODULE_FTS_BM25_WEIGHTS)
        sql = (
            f"SELECT rows.module_key AS module_key, "
            f"       bm25({MODULE_FTS_TABLE}, {weights}) AS score "
            f"FROM {MODULE_FTS_TABLE} AS fts "
            f"JOIN bsl_code_module_fts_rows AS rows "
            f"  ON rows.module_rowid = fts.rowid "
            f"WHERE {MODULE_FTS_TABLE} MATCH ? "
            f"  AND rows.project_name = ? "
            f"  AND rows.index_epoch = ? "
        )
        params: List[Any] = [sanitized, scope, epoch]
        if rel_paths is not None:
            placeholders = ",".join("?" * len(rel_paths))
            sql += f"  AND rows.module_key IN ({placeholders}) "
            params.extend(rel_paths)
        sql += "ORDER BY score LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [(r["module_key"], float(r["score"])) for r in rows]

    def eligible_rel_paths(
        self,
        scope: str,
        epoch: int,
        *,
        config_name: Optional[str] = None,
        module_type: Optional[str] = None,
        routine_type: Optional[str] = None,
        export: Optional[bool] = None,
        owner_categories: Optional[Sequence[str]] = None,
        excluded_owner_categories: Optional[Sequence[str]] = None,
        exclude_regulated_reports: bool = False,
    ) -> List[str]:
        """
        Return the distinct rel_paths of units that pass the MCP filters in
        this (scope, epoch). Used to constrain module-level FTS search before
        topK ranking.

        `excluded_owner_categories` and `exclude_regulated_reports` are
        negative filters applied at the same source-SQL stage as the positive
        ones, so module_fts_search never sees rel_paths whose every unit is
        excluded.
        """
        sql = (
            "SELECT DISTINCT rel_path FROM bsl_code_units "
            "WHERE project_name = ? AND index_epoch = ? "
        )
        params: List[Any] = [scope, epoch]
        if config_name:
            sql += "  AND config_name = ? "
            params.append(config_name)
        if module_type:
            sql += "  AND module_type = ? "
            params.append(module_type)
        if routine_type:
            sql += "  AND routine_type = ? "
            params.append(routine_type)
        if export is not None:
            sql += "  AND export = ? "
            params.append(1 if export else 0)
        if owner_categories:
            placeholders = ",".join("?" * len(owner_categories))
            sql += f"  AND owner_category IN ({placeholders}) "
            params.extend(owner_categories)
        if excluded_owner_categories:
            placeholders = ",".join("?" * len(excluded_owner_categories))
            sql += (
                f"  AND (owner_category IS NULL "
                f"       OR owner_category NOT IN ({placeholders})) "
            )
            params.extend(excluded_owner_categories)
        if exclude_regulated_reports:
            sql += "  AND is_regulated_report = 0 "
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [r["rel_path"] for r in rows if r["rel_path"]]

    def units_by_rel_paths(
        self,
        scope: str,
        epoch: int,
        rel_paths: Sequence[str],
    ) -> List[str]:
        """Return unit_ids that belong to any of the given rel_paths."""
        if not rel_paths:
            return []
        placeholders = ",".join("?" * len(rel_paths))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT unit_id FROM bsl_code_units "
                f"WHERE project_name = ? AND index_epoch = ? "
                f"  AND rel_path IN ({placeholders})",
                (scope, epoch, *rel_paths),
            ).fetchall()
        return [r["unit_id"] for r in rows]

    def rel_paths_for_units(
        self,
        unit_ids: Sequence[str],
        epoch: int,
    ) -> Dict[str, str]:
        """Return {unit_id: rel_path} for the given unit_ids."""
        if not unit_ids:
            return {}
        placeholders = ",".join("?" * len(unit_ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT unit_id, rel_path FROM bsl_code_units "
                f"WHERE index_epoch = ? AND unit_id IN ({placeholders})",
                (epoch, *unit_ids),
            ).fetchall()
        return {r["unit_id"]: r["rel_path"] or "" for r in rows}

    def structural_bm25(
        self,
        scope: str,
        table: str,
        query: str,
        epoch: int,
        limit: int = 50,
        unit_ids: Optional[Sequence[str]] = None,
        prefix: bool = False,
        max_terms: int = 12,
        config_name: Optional[str] = None,
        module_type: Optional[str] = None,
        routine_type: Optional[str] = None,
        export: Optional[bool] = None,
        owner_categories: Optional[Sequence[str]] = None,
        excluded_owner_categories: Optional[Sequence[str]] = None,
        exclude_regulated_reports: bool = False,
    ) -> List[Tuple[str, float]]:
        """BM25 over one of the structural FTS tables (intent-routing leg)."""
        if table not in STRUCTURAL_FTS_TABLES:
            raise BslCodeSqliteError(f"unknown structural FTS table: {table!r}")
        return self._bm25_over_fts(
            table, scope, query, epoch, limit,
            config_name, module_type, routine_type, export,
            owner_categories, unit_ids,
            prefix=prefix, max_terms=max_terms,
            excluded_owner_categories=excluded_owner_categories,
            exclude_regulated_reports=exclude_regulated_reports,
        )

    def _bm25_over_fts(
        self,
        table: str,
        scope: str,
        query: str,
        epoch: int,
        limit: int,
        config_name: Optional[str],
        module_type: Optional[str],
        routine_type: Optional[str],
        export: Optional[bool],
        owner_categories: Optional[Sequence[str]],
        unit_ids: Optional[Sequence[str]],
        *,
        prefix: bool = False,
        max_terms: int = 12,
        excluded_owner_categories: Optional[Sequence[str]] = None,
        exclude_regulated_reports: bool = False,
    ) -> List[Tuple[str, float]]:
        sanitized = _sanitize_fts_query(query, prefix=prefix, max_terms=max_terms)
        if not sanitized:
            return []
        # Reference structural_fts uses BM25 column weights 2.5/3.0/1.8/1.2/1.0
        # across (metadata_refs, query_tables, method_calls, string_literals,
        # assignments). All other FTS tables here are single-column and use the
        # default BM25.
        if table == "bsl_code_structural_fts":
            bm25_expr = f"bm25({table}, 2.5, 3.0, 1.8, 1.2, 1.0)"
            match_target = table  # whole-table MATCH applies to all columns
        else:
            bm25_expr = f"bm25({table})"
            match_target = f"{table}"
        sql = (
            f"SELECT u.unit_id AS unit_id, {bm25_expr} AS score "
            f"FROM {table} AS fts "
            f"JOIN bsl_code_units AS u ON u.fts_rowid = fts.rowid "
            f"WHERE {match_target} MATCH ? "
            f"  AND u.project_name = ? "
            f"  AND u.index_epoch = ? "
        )
        params: List[Any] = [sanitized, scope, epoch]
        if config_name:
            sql += "  AND u.config_name = ? "
            params.append(config_name)
        if module_type:
            sql += "  AND u.module_type = ? "
            params.append(module_type)
        if routine_type:
            sql += "  AND u.routine_type = ? "
            params.append(routine_type)
        if export is not None:
            sql += "  AND u.export = ? "
            params.append(1 if export else 0)
        if owner_categories:
            placeholders = ",".join("?" * len(owner_categories))
            sql += f"  AND u.owner_category IN ({placeholders}) "
            params.extend(owner_categories)
        if excluded_owner_categories:
            placeholders = ",".join("?" * len(excluded_owner_categories))
            sql += (
                f"  AND (u.owner_category IS NULL "
                f"       OR u.owner_category NOT IN ({placeholders})) "
            )
            params.extend(excluded_owner_categories)
        if exclude_regulated_reports:
            sql += "  AND u.is_regulated_report = 0 "
        if unit_ids:
            placeholders = ",".join("?" * len(unit_ids))
            sql += f"  AND u.unit_id IN ({placeholders}) "
            params.extend(unit_ids)
        sql += "ORDER BY score LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [(r["unit_id"], float(r["score"])) for r in rows]

    def units_by_owner_qns(
        self,
        scope: str,
        epoch: int,
        owner_qns: Sequence[str],
    ) -> List[str]:
        """Return unit_ids that belong to any of the given owner_qns."""
        if not owner_qns:
            return []
        placeholders = ",".join("?" * len(owner_qns))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT unit_id FROM bsl_code_units "
                f"WHERE project_name = ? AND index_epoch = ? "
                f"  AND owner_qn IN ({placeholders})",
                (scope, epoch, *owner_qns),
            ).fetchall()
        return [r["unit_id"] for r in rows]

    def owner_qns_for_units(
        self,
        unit_ids: Sequence[str],
        epoch: int,
    ) -> Dict[str, str]:
        """Return {unit_id: owner_qn} for the given unit_ids."""
        if not unit_ids:
            return {}
        placeholders = ",".join("?" * len(unit_ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT unit_id, owner_qn FROM bsl_code_units "
                f"WHERE index_epoch = ? AND unit_id IN ({placeholders})",
                (epoch, *unit_ids),
            ).fetchall()
        return {r["unit_id"]: r["owner_qn"] or "" for r in rows}

    def field_lookup(
        self,
        unit_ids: Sequence[str],
        epoch: int,
    ) -> Dict[str, Dict[str, str]]:
        """
        Return {unit_id: {field_kind: field_text}} for the given epoch.
        Phase A no longer writes field_kind='body' (raw BSL lives only in
        Neo4j Routine.body). The returned dicts contain metadata fields
        only: symbol, object, form, metadata_type, path. The hybrid scorer
        fetches body for top-K candidates directly from Neo4j (see
        bsl_code_search_service._fetch_body_batch_from_neo4j).
        """
        if not unit_ids:
            return {}
        placeholders = ",".join("?" * len(unit_ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT u.unit_id AS unit_id, f.field_kind AS field_kind, "
                f"f.field_text AS field_text "
                f"FROM bsl_code_units AS u "
                f"JOIN bsl_code_unit_fields AS f ON f.fts_rowid = u.fts_rowid "
                f"WHERE u.index_epoch = ? AND u.unit_id IN ({placeholders})",
                (epoch, *unit_ids),
            ).fetchall()
        result: Dict[str, Dict[str, str]] = {}
        for row in rows:
            uid = row["unit_id"]
            result.setdefault(uid, {})[row["field_kind"]] = row["field_text"] or ""
        return result

    def read_corpus_stats(
        self, scope: str, epoch: int, field_kind: str,
    ) -> Tuple[int, float]:
        """
        Return (doc_count, avgdl). Internal storage is
        (doc_count, total_length); avgdl is computed here as
        total_length / max(doc_count, 1) so the external scorer contract
        stays (doc_count, avgdl). (0, 0.0) when missing.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT doc_count, total_length FROM bsl_code_corpus_stats "
                "WHERE project_name = ? AND index_epoch = ? AND field_kind = ?",
                (scope, epoch, field_kind),
            ).fetchone()
            if not row:
                return 0, 0.0
            doc_count = int(row["doc_count"])
            total_length = int(row["total_length"])
            avgdl = total_length / doc_count if doc_count > 0 else 0.0
            return doc_count, avgdl

    def read_idf(
        self, scope: str, epoch: int, field_kind: str, tokens: Sequence[str],
    ) -> Dict[str, int]:
        """Return {token: df} for the given (scope, epoch, field_kind, tokens) tuple."""
        tokens = [t for t in tokens if t]
        if not tokens:
            return {}
        chunk = 500
        out: Dict[str, int] = {}
        with self._lock:
            for start in range(0, len(tokens), chunk):
                slice_ = tokens[start: start + chunk]
                placeholders = ",".join("?" * len(slice_))
                rows = self._conn.execute(
                    f"SELECT token, df FROM bsl_code_corpus_idf "
                    f"WHERE project_name = ? AND index_epoch = ? AND field_kind = ? "
                    f"  AND token IN ({placeholders})",
                    (scope, epoch, field_kind, *slice_),
                ).fetchall()
                for r in rows:
                    out[r["token"]] = int(r["df"])
        return out

    # ------------------------------------------------------------------ write API

    def begin_or_resume_pending(
        self,
        scope: str,
        fingerprint: str,
        source_state_hash: str,
        total_routines: int,
        force_fresh: bool = False,
    ) -> Tuple[int, str]:
        """
        Allocate a fresh pending epoch OR resume a compatible one.

        Returns (pending_epoch, mode) where mode is one of:
            'fresh'              — new pending epoch allocated
            'resume_writing'     — compatible pending, status=writing
            'resume_finalizing'  — compatible pending, status=finalizing

        Compatibility = pending_fingerprint AND pending_source_state_hash
        match the provided values. Incompatible pending is wiped (epoch rows
        + phase_a_routine_state rows) and a fresh pending is allocated.

        `force_fresh=True` treats any existing pending as incompatible —
        wipes it and allocates a new epoch unconditionally. Used when the
        caller explicitly demands a full rebuild (e.g., reindex_requested=1).
        """
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock:
            self._ensure_fingerprint_row(scope)
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                row = cur.execute(
                    "SELECT current_epoch, pending_epoch, pending_fingerprint, "
                    "pending_source_state_hash, pending_status, "
                    "vector_status, vector_epoch, fingerprint, "
                    "embedding_fingerprint, "
                    "phase_b_transfer_prev_current_epoch "
                    "FROM bsl_code_search_fingerprints WHERE project_name = ?",
                    (scope,),
                ).fetchone()
                current = int(row["current_epoch"]) if row and row["current_epoch"] is not None else 0
                old_pending = (
                    int(row["pending_epoch"])
                    if row and row["pending_epoch"] is not None
                    else None
                )
                pending_fp = (row["pending_fingerprint"] if row else "") or ""
                pending_src = (row["pending_source_state_hash"] if row else "") or ""
                pending_status = (row["pending_status"] if row else "idle") or "idle"
                cur_vector_status = (row["vector_status"] if row else "") or ""
                cur_vector_epoch = (
                    int(row["vector_epoch"])
                    if row and row["vector_epoch"] is not None
                    else None
                )
                cur_fingerprint = (row["fingerprint"] if row else "") or ""
                cur_embedding_fp = (row["embedding_fingerprint"] if row else "") or ""
                snap_prev_current = (
                    int(row["phase_b_transfer_prev_current_epoch"])
                    if row and row["phase_b_transfer_prev_current_epoch"] is not None
                    else None
                )

                if old_pending is not None:
                    compatible = (
                        not force_fresh
                        and pending_fp == (fingerprint or "")
                        and pending_src == (source_state_hash or "")
                    )
                    if compatible:
                        mode = (
                            "resume_finalizing"
                            if pending_status == "finalizing"
                            else "resume_writing"
                        )
                        cur.execute(
                            "UPDATE bsl_code_search_fingerprints "
                            "SET pending_updated_at = ?, "
                            "    pending_total_routines = ?, "
                            "    pending_status = CASE "
                            "        WHEN pending_status = 'finalizing' THEN 'finalizing' "
                            "        ELSE 'writing' END, "
                            "    pending_done_routines = ("
                            "        SELECT COUNT(*) FROM bsl_code_phase_a_routine_state "
                            "        WHERE project_name = ? AND index_epoch = ?) "
                            "WHERE project_name = ?",
                            (now, int(total_routines), scope, old_pending, scope),
                        )
                        cur.execute("COMMIT")
                        return old_pending, mode
                    # Incompatible (or force_fresh): wipe and re-allocate.
                    wipe_reason = "force_fresh" if force_fresh else "incompatible_pending"
                    self._wipe_epoch_rows(
                        cur, scope, old_pending, reason=wipe_reason,
                    )
                    cur.execute(
                        "DELETE FROM bsl_code_phase_a_routine_state "
                        "WHERE project_name = ? AND index_epoch = ?",
                        (scope, old_pending),
                    )
                    # Drop any Phase B done-markers left by a startup overlap on
                    # the abandoned pending epoch. Monotonic epoch allocation
                    # means a NEW epoch is always higher, so these markers can
                    # never falsely mark a new-epoch unit done; this is hygiene
                    # to keep the abandoned epoch's SQLite state clean. Matching
                    # Neo4j pending vectors are stale-epoch and swept later.
                    cur.execute(
                        "DELETE FROM bsl_code_phase_b_unit_state "
                        "WHERE project_name = ? AND vector_epoch = ?",
                        (scope, old_pending),
                    )

                pending = current + 1
                if old_pending is not None and old_pending >= pending:
                    pending = old_pending + 1

                self._wipe_epoch_rows(cur, scope, pending, reason="new_pending")

                # Durable Phase B transfer snapshot: capture prior committed
                # Phase B state (vector_status/vector_epoch/embedding_fp +
                # Phase A fingerprint) BEFORE the caller resets vector_status
                # to 'not_started' or commit_pending clears embedding_fp.
                # Only refresh the snapshot when it does not already point at
                # the current committed epoch — otherwise a recursive call
                # (e.g. invariant recovery + force_fresh) would overwrite a
                # still-valid snapshot with already-reset live fields.
                snapshot_sets: List[str] = []
                snapshot_params: List[Any] = []
                if current > 0 and snap_prev_current != current:
                    snapshot_sets = [
                        "phase_b_transfer_prev_current_epoch = ?",
                        "phase_b_transfer_prev_vector_status = ?",
                        "phase_b_transfer_prev_vector_epoch = ?",
                        "phase_b_transfer_prev_phase_a_fingerprint = ?",
                        "phase_b_transfer_prev_embedding_fingerprint = ?",
                    ]
                    snapshot_params = [
                        current,
                        cur_vector_status,
                        cur_vector_epoch,
                        cur_fingerprint,
                        cur_embedding_fp,
                    ]

                set_clauses = [
                    "pending_epoch = ?",
                    "pending_fingerprint = ?",
                    "pending_source_state_hash = ?",
                    "pending_status = 'writing'",
                    "pending_started_at = ?",
                    "pending_updated_at = ?",
                    "pending_total_routines = ?",
                    "pending_done_routines = 0",
                ]
                params: List[Any] = [
                    pending,
                    fingerprint or "",
                    source_state_hash or "",
                    now,
                    now,
                    int(total_routines),
                ]
                if snapshot_sets:
                    set_clauses.extend(snapshot_sets)
                    params.extend(snapshot_params)
                params.append(scope)
                cur.execute(
                    "UPDATE bsl_code_search_fingerprints SET "
                    + ", ".join(set_clauses)
                    + " WHERE project_name = ?",
                    tuple(params),
                )
                cur.execute("COMMIT")
                return pending, "fresh"
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def _wipe_epoch_rows(self, cur, scope: str, epoch: int, reason: str = "") -> int:
        started_at = time.monotonic()
        rowids = [
            r["fts_rowid"]
            for r in cur.execute(
                "SELECT fts_rowid FROM bsl_code_units "
                "WHERE project_name = ? AND index_epoch = ?",
                (scope, epoch),
            ).fetchall()
        ]
        total = len(rowids)
        if total:
            logger.info(
                "BSL sqlite cleanup: wiping epoch=%d reason=%s units=%d",
                epoch, reason or "unspecified", total,
            )
        removed_fts = 0
        last_logged_rows = 0
        last_logged_at = started_at
        for start in range(0, len(rowids), 500):
            chunk = rowids[start: start + 500]
            placeholders = ",".join("?" * len(chunk))
            cur.execute(
                f"DELETE FROM bsl_code_units_fts WHERE rowid IN ({placeholders})",
                chunk,
            )
            for table in STRUCTURAL_FTS_TABLES:
                cur.execute(
                    f"DELETE FROM {table} WHERE rowid IN ({placeholders})",
                    chunk,
                )
            removed_fts += len(chunk)
            now = time.monotonic()
            row_due = removed_fts - last_logged_rows >= BSL_SQLITE_CLEANUP_PROGRESS_ROWS
            time_due = now - last_logged_at >= BSL_SQLITE_CLEANUP_PROGRESS_SECONDS
            if total and (row_due or time_due):
                pct = removed_fts / total * 100.0
                logger.info(
                    "BSL sqlite cleanup: wiping epoch=%d progress=%d/%d (%.1f%%) "
                    "elapsed=%s",
                    epoch, removed_fts, total, pct,
                    _format_elapsed(now - started_at),
                )
                last_logged_rows = removed_fts
                last_logged_at = now
        deleted_units = cur.execute(
            "DELETE FROM bsl_code_units WHERE project_name = ? AND index_epoch = ?",
            (scope, epoch),
        ).rowcount
        cur.execute(
            "DELETE FROM bsl_code_methods WHERE project_name = ? AND index_epoch = ?",
            (scope, epoch),
        )
        cur.execute(
            "DELETE FROM bsl_code_modules WHERE project_name = ? AND index_epoch = ?",
            (scope, epoch),
        )
        # module FTS is contentless: delete by rowid via the side table,
        # then drop the side rows themselves.
        cur.execute(
            f"DELETE FROM {MODULE_FTS_TABLE} WHERE rowid IN ("
            f"  SELECT module_rowid FROM bsl_code_module_fts_rows "
            f"  WHERE project_name = ? AND index_epoch = ?"
            f")",
            (scope, epoch),
        )
        cur.execute(
            "DELETE FROM bsl_code_module_fts_rows "
            "WHERE project_name = ? AND index_epoch = ?",
            (scope, epoch),
        )
        cur.execute(
            "DELETE FROM bsl_code_corpus_idf "
            "WHERE project_name = ? AND index_epoch = ?",
            (scope, epoch),
        )
        cur.execute(
            "DELETE FROM bsl_code_corpus_stats "
            "WHERE project_name = ? AND index_epoch = ?",
            (scope, epoch),
        )
        cur.execute(
            "DELETE FROM bsl_code_phase_a_routine_state "
            "WHERE project_name = ? AND index_epoch = ?",
            (scope, epoch),
        )
        # Phase B done markers carry vector_epoch (not index_epoch). They
        # share the integer with index_epoch for the lifetime of a Phase A
        # → B cycle, so a retired index_epoch's Phase B state has the same
        # value. Drop it here to keep _wipe_epoch_rows a single source of
        # truth for "epoch X is gone", instead of letting done markers
        # accumulate forever.
        cur.execute(
            "DELETE FROM bsl_code_phase_b_unit_state "
            "WHERE project_name = ? AND vector_epoch = ?",
            (scope, epoch),
        )
        if total or deleted_units:
            logger.info(
                "BSL sqlite cleanup: wiped epoch=%d units=%d elapsed=%s",
                epoch, int(deleted_units or 0),
                _format_elapsed(time.monotonic() - started_at),
            )
        return int(deleted_units or 0)

    def reset_after_full_reload(self, scope: str) -> int:
        """Clear all BSL code search sidecar state for one project after Neo4j bulk reload.

        After a FULL_METADATA_RELOAD / initial load the Neo4j graph is
        regenerated, but the sidecar (on persistent storage) may still hold the
        prior epoch, fingerprint and vector state — enough for start_indexing()
        to route to "up to date" against the fresh graph. This wipes every
        project-scoped row across ALL epochs and removes the scope's fingerprint
        row, so the next start_indexing() reads an empty state and takes the
        full-rebuild path.

        Only `project_name = scope` is touched; other projects sharing the same
        SQLite file are left intact. No DROP TABLE, no PRAGMA user_version
        change. Single transaction: BEGIN → deletes → COMMIT, ROLLBACK on error.

        Post-conditions: current_epoch(scope) == 0,
        vector_state(scope).status == "not_started", read_fingerprint(scope) == {}
        (the row is deleted), and the next begin_or_resume_pending(scope, ...)
        starts at epoch 1.

        Returns the number of bsl_code_units rows removed (for logging).
        """
        started_at = time.monotonic()
        with self._lock:
            cur = self._conn.cursor()
            rowids = [
                r["fts_rowid"]
                for r in cur.execute(
                    "SELECT fts_rowid FROM bsl_code_units WHERE project_name = ?",
                    (scope,),
                ).fetchall()
            ]
            total_units = len(rowids)
            cur.execute("BEGIN")
            try:
                # Contentless per-unit FTS + field side table: delete by
                # fts_rowid collected above (all epochs), chunked like
                # _wipe_epoch_rows. bsl_code_unit_fields has no project_name
                # column and is keyed by fts_rowid (FK cascade also exists, but
                # we delete explicitly for determinism).
                for start in range(0, len(rowids), 500):
                    chunk = rowids[start: start + 500]
                    placeholders = ",".join("?" * len(chunk))
                    cur.execute(
                        f"DELETE FROM bsl_code_units_fts WHERE rowid IN ({placeholders})",
                        chunk,
                    )
                    for table in STRUCTURAL_FTS_TABLES:
                        cur.execute(
                            f"DELETE FROM {table} WHERE rowid IN ({placeholders})",
                            chunk,
                        )
                    cur.execute(
                        f"DELETE FROM bsl_code_unit_fields "
                        f"WHERE fts_rowid IN ({placeholders})",
                        chunk,
                    )

                # Contentless module FTS: delete by module_rowid via the scoped
                # side table, then the generic loop drops the side rows.
                cur.execute(
                    f"DELETE FROM {MODULE_FTS_TABLE} WHERE rowid IN ("
                    f"  SELECT module_rowid FROM bsl_code_module_fts_rows "
                    f"  WHERE project_name = ?"
                    f")",
                    (scope,),
                )

                # Generic project-scoped tables (single source of truth).
                for table in PROJECT_SCOPED_TABLES:
                    cur.execute(
                        f"DELETE FROM {table} WHERE project_name = ?",
                        (scope,),
                    )

                # Scope row itself — removed so read_fingerprint(scope) == {}
                # and the next begin_or_resume_pending starts at epoch 1.
                cur.execute(
                    "DELETE FROM bsl_code_search_fingerprints WHERE project_name = ?",
                    (scope,),
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        logger.info(
            "BSL code search sidecar reset after bulk load: scope=%s units=%d elapsed=%s",
            scope, total_units, _format_elapsed(time.monotonic() - started_at),
        )
        return total_units

    def _insert_unit_row_in_tx(
        self,
        cur: sqlite3.Cursor,
        scope: str,
        epoch: int,
        unit: Dict[str, Any],
    ) -> int:
        cur.execute(
            """
            INSERT INTO bsl_code_units(
                unit_id, routine_id, routine_name, project_name, config_name,
                owner_qn, owner_qn_prefix, owner_category,
                module_type, module_kind, routine_type, export,
                line_start, line_end, char_start, char_end,
                part_index, part_total,
                body_hash, rel_path, size_chars, size_lines,
                index_epoch, unit_kind, is_regulated_report
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                unit["unit_id"],
                unit["routine_id"],
                unit.get("routine_name", "") or "",
                scope,
                unit.get("config_name", "") or "",
                unit.get("owner_qn", "") or "",
                unit.get("owner_qn_prefix", "") or "",
                unit.get("owner_category", "") or "",
                unit.get("module_type", "") or "",
                unit.get("module_kind", "") or "",
                unit.get("routine_type", "") or "",
                1 if unit.get("export") else 0,
                int(unit.get("line_start", 0) or 0),
                int(unit.get("line_end", 0) or 0),
                int(unit.get("char_start", 0) or 0),
                int(unit.get("char_end", 0) or 0),
                int(unit.get("part_index", 0) or 0),
                int(unit.get("part_total", 1) or 1),
                unit.get("body_hash", "") or "",
                unit.get("rel_path", "") or "",
                int(unit.get("size_chars", 0) or 0),
                int(unit.get("size_lines", 0) or 0),
                int(epoch),
                unit.get("unit_kind", "routine_code_unit"),
                1 if unit.get("is_regulated_report") else 0,
            ),
        )
        return int(cur.lastrowid)

    def _collect_unit_sidecars(
        self,
        sidecars: _UnitSidecarRows,
        fts_rowid: int,
        text_for_fts: str,
        fields: Optional[Dict[str, str]] = None,
        structural: Optional[Dict[str, str]] = None,
    ) -> None:
        sidecars.units_fts.append((int(fts_rowid), text_for_fts or ""))
        if fields:
            sidecars.fields.extend(
                (int(fts_rowid), kind, value or "")
                for kind, value in fields.items()
            )
        if structural:
            for table, payload in structural.items():
                if table not in STRUCTURAL_FTS_TABLES:
                    continue
                if table == "bsl_code_structural_fts":
                    if isinstance(payload, (list, tuple)) and len(payload) == 5:
                        cols = [p or "" for p in payload]
                    else:
                        cols = [str(payload or ""), "", "", "", ""]
                    sidecars.structural.setdefault(table, []).append(
                        (int(fts_rowid), *cols)
                    )
                else:
                    sidecars.structural.setdefault(table, []).append(
                        (int(fts_rowid), str(payload or ""))
                    )

    def _flush_unit_sidecars_in_tx(
        self,
        cur: sqlite3.Cursor,
        sidecars: _UnitSidecarRows,
    ) -> None:
        if sidecars.units_fts:
            cur.executemany(
                "INSERT INTO bsl_code_units_fts(rowid, text) VALUES (?, ?)",
                sidecars.units_fts,
            )
        if sidecars.fields:
            cur.executemany(
                "INSERT INTO bsl_code_unit_fields(fts_rowid, field_kind, field_text) "
                "VALUES (?, ?, ?)",
                sidecars.fields,
            )
        structural_rows = sidecars.structural.get("bsl_code_structural_fts")
        if structural_rows:
            cur.executemany(
                "INSERT INTO bsl_code_structural_fts(rowid, metadata_refs, "
                "query_tables, method_calls, string_literals, assignments) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                structural_rows,
            )
        for table, rows in sidecars.structural.items():
            if table == "bsl_code_structural_fts":
                continue
            if table not in STRUCTURAL_FTS_TABLES or not rows:
                continue
            cur.executemany(
                f"INSERT INTO {table}(rowid, text) VALUES (?, ?)",
                rows,
            )

    def _write_unit_in_tx(
        self,
        cur,
        scope: str,
        epoch: int,
        unit: Dict[str, Any],
        text_for_fts: str,
        fields: Optional[Dict[str, str]] = None,
        structural: Optional[Dict[str, str]] = None,
    ) -> int:
        """Insert one unit's rows using an already-open transaction cursor. Returns fts_rowid."""
        fts_rowid = self._insert_unit_row_in_tx(cur, scope, epoch, unit)
        sidecars = _UnitSidecarRows()
        self._collect_unit_sidecars(
            sidecars, fts_rowid, text_for_fts, fields, structural,
        )
        self._flush_unit_sidecars_in_tx(cur, sidecars)
        return fts_rowid

    def write_unit(
        self,
        scope: str,
        epoch: int,
        unit: Dict[str, Any],
        text_for_fts: str,
        fields: Optional[Dict[str, str]] = None,
        structural: Optional[Dict[str, str]] = None,
    ) -> int:
        """
        Insert one unit with its FTS posting in bsl_code_units_fts, its 6 field
        rows, and structural-FTS postings in the per-table sidecars.

        `structural` keys must be one of STRUCTURAL_FTS_TABLES names; values
        are the concatenated tokens / phrases to index for that intent leg.
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                fts_rowid = self._write_unit_in_tx(
                    cur, scope, epoch, unit, text_for_fts, fields, structural,
                )
                cur.execute("COMMIT")
                return fts_rowid
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def write_units_batch(
        self,
        scope: str,
        epoch: int,
        items: List[Dict[str, Any]],
    ) -> int:
        """
        Insert multiple units in a single BEGIN/COMMIT transaction.

        Each item in `items` is a dict with keys: unit, text_for_fts,
        fields (optional), structural (optional). Returns count written.
        """
        if not items:
            return 0
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                self._write_units_batch_in_tx(cur, scope, epoch, items)
                cur.execute("COMMIT")
                return len(items)
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def flush_phase_a_units_batch(
        self,
        scope: str,
        epoch: int,
        units: List[Dict[str, Any]],
        done_routines: List[Dict[str, Any]],
        methods: Optional[List[Dict[str, Any]]] = None,
        module_fragments: Optional[List[Dict[str, Any]]] = None,
    ) -> int:
        """
        Atomic flush for resumable Phase A unit state: writes units,
        per-routine done markers, best-effort methods AND module fragments
        in one BEGIN/COMMIT.

        Args:
            units: list of {unit, text_for_fts, fields, structural}.
            done_routines: list of {routine_id, body_hash, units_written}.
            methods: optional list of method rows. Method writes are isolated
                behind a SAVEPOINT so their failure does not roll back
                units/done.
            module_fragments: optional list of per-routine fragments. Persisted
                via INSERT OR REPLACE so that for every unit row in this batch
                there is a matching fragment row (1:1 invariant — required by
                scoped module FTS rebuild).

        IDF/stats are committed only at module boundary by
        commit_phase_a_module().

        Returns count of units written.
        """
        methods = methods or []
        module_fragments = module_fragments or []
        if (not units and not done_routines and not methods and not module_fragments):
            return 0
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        profiler = self._debug_phase_a_profiler()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                self._write_units_batch_in_tx(
                    cur, scope, epoch, units, profiler=profiler,
                )
                self._write_phase_a_done_in_tx(
                    cur, scope, epoch, done_routines, now, profiler=profiler,
                )
                self._write_methods_best_effort_in_tx(
                    cur, scope, epoch, methods, profiler=profiler,
                )
                self._write_module_fragments_in_tx(
                    cur, scope, epoch, module_fragments,
                )
                section_started = profiler.start() if profiler is not None else None
                cur.execute("COMMIT")
                if profiler is not None and section_started is not None:
                    profiler.add_ms("commit", section_started)
                    profiler.maybe_log()
                return len(units)
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def _write_module_fragments_in_tx(
        self,
        cur: sqlite3.Cursor,
        scope: str,
        epoch: int,
        fragments: Sequence[Dict[str, Any]],
    ) -> None:
        if not fragments:
            return
        rows = [
            (
                scope,
                int(epoch),
                f["routine_id"],
                f.get("rel_path") or "",
                int(f.get("routine_ordinal") or 0),
                json.dumps(f, ensure_ascii=False, sort_keys=True),
            )
            for f in fragments
        ]
        cur.executemany(
            "INSERT INTO bsl_code_module_fragments("
            "project_name, index_epoch, routine_id, rel_path, "
            "routine_ordinal, fragment_json"
            ") VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project_name, index_epoch, routine_id) DO UPDATE SET "
            "    rel_path = excluded.rel_path, "
            "    routine_ordinal = excluded.routine_ordinal, "
            "    fragment_json = excluded.fragment_json",
            rows,
        )

    def _write_units_batch_in_tx(
        self,
        cur: sqlite3.Cursor,
        scope: str,
        epoch: int,
        units: List[Dict[str, Any]],
        *,
        profiler: Optional[_SqlitePhaseADebugProfiler] = None,
    ) -> int:
        if not units:
            return 0
        section_started = profiler.start() if profiler is not None else None
        sidecars = _UnitSidecarRows()
        for item in units:
            fts_rowid = self._insert_unit_row_in_tx(
                cur, scope, epoch, item["unit"],
            )
            self._collect_unit_sidecars(
                sidecars,
                fts_rowid,
                item["text_for_fts"],
                item.get("fields"),
                item.get("structural"),
            )
        self._flush_unit_sidecars_in_tx(cur, sidecars)
        if profiler is not None and section_started is not None:
            profiler.add_ms("units_write", section_started)
            profiler.add_rows(units=len(units))
        return len(units)

    def _write_phase_a_done_in_tx(
        self,
        cur: sqlite3.Cursor,
        scope: str,
        epoch: int,
        done_routines: List[Dict[str, Any]],
        now: str,
        *,
        profiler: Optional[_SqlitePhaseADebugProfiler] = None,
    ) -> int:
        if not done_routines:
            return 0
        progress_started = profiler.start() if profiler is not None else None
        done_by_rid: Dict[str, Dict[str, Any]] = {}
        for d in done_routines:
            rid = (d.get("routine_id") or "").strip()
            if rid:
                row = dict(d)
                row["routine_id"] = rid
                done_by_rid[rid] = row
        if not done_by_rid:
            return 0

        existing_done_ids = self._existing_phase_a_done_ids_in_tx(
            cur, scope, epoch, list(done_by_rid.keys()),
        )
        new_done_count = len(set(done_by_rid) - existing_done_ids)

        section_started = profiler.start() if profiler is not None else None
        cur.executemany(
            """
            INSERT OR REPLACE INTO bsl_code_phase_a_routine_state(
                project_name, index_epoch, routine_id,
                body_hash, units_written, done_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    scope,
                    int(epoch),
                    d["routine_id"],
                    d.get("body_hash", "") or "",
                    int(d.get("units_written", 0) or 0),
                    now,
                )
                for d in done_by_rid.values()
            ],
        )
        if profiler is not None and section_started is not None:
            profiler.add_ms("done_state", section_started)

        cur.execute(
            "UPDATE bsl_code_search_fingerprints "
            "SET pending_done_routines = pending_done_routines + ?, "
            "    pending_updated_at = ? "
            "WHERE project_name = ?",
            (int(new_done_count), now, scope),
        )
        if profiler is not None and progress_started is not None:
            profiler.add_ms("pending_progress_update", progress_started)
        return int(new_done_count)

    def _existing_phase_a_done_ids_in_tx(
        self,
        cur: sqlite3.Cursor,
        scope: str,
        epoch: int,
        routine_ids: Sequence[str],
    ) -> set:
        if not routine_ids:
            return set()
        existing = set()
        for start in range(0, len(routine_ids), 500):
            chunk = list(routine_ids[start: start + 500])
            placeholders = ",".join("?" * len(chunk))
            rows = cur.execute(
                "SELECT routine_id FROM bsl_code_phase_a_routine_state "
                "WHERE project_name = ? AND index_epoch = ? "
                f"AND routine_id IN ({placeholders})",
                (scope, int(epoch), *chunk),
            ).fetchall()
            existing.update(r["routine_id"] for r in rows)
        return existing

    def _write_methods_best_effort_in_tx(
        self,
        cur: sqlite3.Cursor,
        scope: str,
        epoch: int,
        methods: List[Dict[str, Any]],
        *,
        profiler: Optional[_SqlitePhaseADebugProfiler] = None,
    ) -> int:
        if not methods:
            return 0
        cur.execute("SAVEPOINT phase_a_methods")
        try:
            written = self._write_methods_in_tx(
                cur, scope, epoch, methods, profiler=profiler,
            )
            cur.execute("RELEASE phase_a_methods")
            return written
        except Exception as e:
            cur.execute("ROLLBACK TO phase_a_methods")
            cur.execute("RELEASE phase_a_methods")
            logger.debug(
                "BSL Phase A: write methods failed in savepoint: %s",
                e,
            )
            return 0

    def _upsert_corpus_idf_in_tx(
        self,
        cur: sqlite3.Cursor,
        scope: str,
        epoch: int,
        idf_increments: Dict[str, Dict[str, int]],
        *,
        profiler: Optional[_SqlitePhaseADebugProfiler] = None,
    ) -> int:
        idf_rows: List[Tuple[Any, ...]] = []
        for fk, token_to_df in idf_increments.items():
            for tok, df_delta in token_to_df.items():
                if tok and df_delta:
                    idf_rows.append((scope, int(epoch), fk, tok, int(df_delta)))
        if not idf_rows:
            return 0
        section_started = profiler.start() if profiler is not None else None
        cur.executemany(
            """
            INSERT INTO bsl_code_corpus_idf(
                project_name, index_epoch, field_kind, token, df
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_name, index_epoch, field_kind, token)
            DO UPDATE SET df = df + excluded.df
            """,
            idf_rows,
        )
        if profiler is not None and section_started is not None:
            profiler.add_ms("corpus_idf_upsert", section_started)
            profiler.add_rows(idf=len(idf_rows))
        return len(idf_rows)

    def _upsert_corpus_stats_in_tx(
        self,
        cur: sqlite3.Cursor,
        scope: str,
        epoch: int,
        stats_increments: Dict[str, Tuple[int, int]],
        *,
        profiler: Optional[_SqlitePhaseADebugProfiler] = None,
    ) -> int:
        stats_rows: List[Tuple[Any, ...]] = []
        for fk, (dc_delta, tl_delta) in stats_increments.items():
            if dc_delta or tl_delta:
                stats_rows.append(
                    (scope, int(epoch), fk, int(dc_delta), int(tl_delta))
                )
        if not stats_rows:
            return 0
        section_started = profiler.start() if profiler is not None else None
        cur.executemany(
            """
            INSERT INTO bsl_code_corpus_stats(
                project_name, index_epoch, field_kind,
                doc_count, total_length
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_name, index_epoch, field_kind)
            DO UPDATE SET
                doc_count    = doc_count    + excluded.doc_count,
                total_length = total_length + excluded.total_length
            """,
            stats_rows,
        )
        if profiler is not None and section_started is not None:
            profiler.add_ms("corpus_stats_upsert", section_started)
            profiler.add_rows(stats=len(stats_rows))
        return len(stats_rows)

    def _insert_module_fts_in_tx(
        self,
        cur: sqlite3.Cursor,
        scope: str,
        epoch: int,
        rel_path: str,
        columns: Dict[str, str],
        *,
        profiler: Optional[_SqlitePhaseADebugProfiler] = None,
    ) -> int:
        values = [columns.get(col, "") or "" for col in _MODULE_FTS_INDEXED_COLUMNS]
        col_list = ", ".join(_MODULE_FTS_INDEXED_COLUMNS)
        placeholders = ", ".join("?" * (1 + len(_MODULE_FTS_INDEXED_COLUMNS)))
        section_started = profiler.start() if profiler is not None else None
        cur.execute(
            "INSERT INTO bsl_code_module_fts_rows("
            "project_name, index_epoch, module_key, rel_path"
            ") VALUES (?, ?, ?, ?)",
            (scope, int(epoch), rel_path, rel_path),
        )
        module_rowid = int(cur.lastrowid)
        if profiler is not None and section_started is not None:
            profiler.add_ms("module_row_insert", section_started)
            profiler.add_rows(modules=1)

        section_started = profiler.start() if profiler is not None else None
        cur.execute(
            f"INSERT INTO {MODULE_FTS_TABLE}(rowid, {col_list}) "
            f"VALUES ({placeholders})",
            (module_rowid, *values),
        )
        if profiler is not None and section_started is not None:
            profiler.add_ms("module_fts_insert", section_started)
        return module_rowid

    def commit_phase_a_module(
        self,
        scope: str,
        epoch: int,
        rel_path: str,
        columns: Dict[str, str],
        idf_increments: Optional[Dict[str, Dict[str, int]]] = None,
        stats_increments: Optional[Dict[str, Tuple[int, int]]] = None,
    ) -> None:
        """
        Atomic per-module commit at Phase A module boundary:
          1. UPSERT module-scoped corpus IDF/stats deltas.
          2. INSERT side-table row (auto-assigned module_rowid) + the
             contentless FTS row with the same explicit rowid.
        Done in one transaction, so on crash either both states are durable
        or neither — the state-aware resume taxonomy then correctly classifies
        the rel_path as fully-flushed vs in-progress.
        """
        idf_inc = idf_increments or {}
        stats_inc = stats_increments or {}
        profiler = self._debug_phase_a_profiler()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                self._upsert_corpus_idf_in_tx(
                    cur, scope, epoch, idf_inc, profiler=profiler,
                )
                self._upsert_corpus_stats_in_tx(
                    cur, scope, epoch, stats_inc, profiler=profiler,
                )
                self._insert_module_fts_in_tx(
                    cur, scope, epoch, rel_path, columns, profiler=profiler,
                )

                section_started = profiler.start() if profiler is not None else None
                cur.execute("COMMIT")
                if profiler is not None and section_started is not None:
                    profiler.add_ms("commit", section_started)
                    profiler.maybe_log()
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def commit_phase_a_module_with_writes(
        self,
        scope: str,
        epoch: int,
        rel_path: str,
        columns: Dict[str, str],
        units: List[Dict[str, Any]],
        done_routines: List[Dict[str, Any]],
        methods: Optional[List[Dict[str, Any]]] = None,
        idf_increments: Optional[Dict[str, Dict[str, int]]] = None,
        stats_increments: Optional[Dict[str, Tuple[int, int]]] = None,
        module_fragments: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """
        Atomic Phase A module-boundary commit including any pending unit
        writes for the same rel_path. Method rows remain best-effort inside
        a SAVEPOINT; units/done/corpus/module FTS/module fragments roll back
        together.
        """
        methods = methods or []
        idf_inc = idf_increments or {}
        stats_inc = stats_increments or {}
        module_fragments = module_fragments or []
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        profiler = self._debug_phase_a_profiler()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                self._write_units_batch_in_tx(
                    cur, scope, epoch, units, profiler=profiler,
                )
                self._write_phase_a_done_in_tx(
                    cur, scope, epoch, done_routines, now, profiler=profiler,
                )
                self._write_methods_best_effort_in_tx(
                    cur, scope, epoch, methods, profiler=profiler,
                )
                self._upsert_corpus_idf_in_tx(
                    cur, scope, epoch, idf_inc, profiler=profiler,
                )
                self._upsert_corpus_stats_in_tx(
                    cur, scope, epoch, stats_inc, profiler=profiler,
                )
                self._insert_module_fts_in_tx(
                    cur, scope, epoch, rel_path, columns, profiler=profiler,
                )
                self._write_module_fragments_in_tx(
                    cur, scope, epoch, module_fragments,
                )

                section_started = profiler.start() if profiler is not None else None
                cur.execute("COMMIT")
                if profiler is not None and section_started is not None:
                    profiler.add_ms("commit", section_started)
                    profiler.maybe_log()
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def commit_phase_a_modules_batch_with_writes(
        self,
        scope: str,
        epoch: int,
        modules: Sequence[PhaseAModuleCommit],
    ) -> None:
        """
        Atomic Phase A commit for a batch of small modules that have no
        mid-module unit flushes. Method rows are best-effort per module;
        units/done/corpus/module FTS roll back together for the whole batch.
        """
        module_batch = list(modules)
        if not module_batch:
            return

        all_units: List[Dict[str, Any]] = []
        all_done: List[Dict[str, Any]] = []
        aggregated_idf: Dict[str, Dict[str, int]] = {}
        aggregated_stats: Dict[str, Tuple[int, int]] = {}

        for module in module_batch:
            all_units.extend(module.units)
            all_done.extend(module.done_routines)
            for field_kind, token_to_df in module.idf_increments.items():
                dst = aggregated_idf.setdefault(field_kind, {})
                for token, df in token_to_df.items():
                    dst[token] = dst.get(token, 0) + int(df)
            for field_kind, (doc_count, total_length) in module.stats_increments.items():
                prev_doc_count, prev_total_length = aggregated_stats.get(
                    field_kind, (0, 0)
                )
                aggregated_stats[field_kind] = (
                    prev_doc_count + int(doc_count),
                    prev_total_length + int(total_length),
                )

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        profiler = self._debug_phase_a_profiler()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                self._write_units_batch_in_tx(
                    cur, scope, epoch, all_units, profiler=profiler,
                )
                self._write_phase_a_done_in_tx(
                    cur, scope, epoch, all_done, now, profiler=profiler,
                )
                for module in module_batch:
                    self._write_methods_best_effort_in_tx(
                        cur, scope, epoch, module.methods, profiler=profiler,
                    )
                self._upsert_corpus_idf_in_tx(
                    cur, scope, epoch, aggregated_idf, profiler=profiler,
                )
                self._upsert_corpus_stats_in_tx(
                    cur, scope, epoch, aggregated_stats, profiler=profiler,
                )
                for module in module_batch:
                    self._insert_module_fts_in_tx(
                        cur,
                        scope,
                        epoch,
                        module.rel_path,
                        module.columns,
                        profiler=profiler,
                    )
                    self._write_module_fragments_in_tx(
                        cur,
                        scope,
                        epoch,
                        getattr(module, "module_fragments", None) or [],
                    )

                section_started = profiler.start() if profiler is not None else None
                cur.execute("COMMIT")
                if profiler is not None and section_started is not None:
                    profiler.add_ms("commit", section_started)
                    profiler.maybe_log()
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def classify_last_rel_path_state(
        self, scope: str, epoch: int, rel_path: str,
    ) -> str:
        """
        State-aware classification for resume (architecture decision #2).
        Returns one of:
            "in_progress"   — units/done exist, module FTS row absent.
            "fully_flushed" — units/done exist, module FTS row present.
            "invariant_violation" — any other combination; caller MUST drop
                the pending epoch and restart Phase A from scratch.
        """
        with self._lock:
            has_units_done = self._conn.execute(
                "SELECT 1 FROM bsl_code_phase_a_routine_state AS s "
                "JOIN bsl_code_units AS u "
                "  ON u.routine_id = s.routine_id "
                " AND u.index_epoch = s.index_epoch "
                " AND u.project_name = s.project_name "
                "WHERE s.project_name = ? AND s.index_epoch = ? "
                "  AND u.rel_path = ? LIMIT 1",
                (scope, int(epoch), rel_path),
            ).fetchone() is not None
            has_module_fts = self._conn.execute(
                "SELECT 1 FROM bsl_code_module_fts_rows "
                "WHERE project_name = ? AND index_epoch = ? AND rel_path = ? "
                "LIMIT 1",
                (scope, int(epoch), rel_path),
            ).fetchone() is not None
        if has_units_done and not has_module_fts:
            return "in_progress"
        if has_units_done and has_module_fts:
            return "fully_flushed"
        return "invariant_violation"

    def read_last_in_progress_rel_path(
        self, scope: str, epoch: int,
    ) -> Optional[str]:
        """Return MAX(rel_path) of done routines for resume — that is the
        last in-progress (or fully-flushed) module. None when no progress."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(u.rel_path) AS rel_path "
                "FROM bsl_code_phase_a_routine_state AS s "
                "JOIN bsl_code_units AS u "
                "  ON u.routine_id = s.routine_id "
                " AND u.index_epoch = s.index_epoch "
                " AND u.project_name = s.project_name "
                "WHERE s.project_name = ? AND s.index_epoch = ?",
                (scope, int(epoch)),
            ).fetchone()
        if not row or row["rel_path"] is None:
            return None
        return row["rel_path"]

    def drop_pending_epoch(self, scope: str) -> None:
        """Clear pending epoch markers in fingerprints (invariant-violation
        recovery: caller restarts Phase A from scratch with force_fresh=True)."""
        with self._lock:
            self._conn.execute(
                "UPDATE bsl_code_search_fingerprints "
                "SET pending_epoch = NULL, pending_status = 'idle', "
                "    pending_fingerprint = '', pending_source_state_hash = '', "
                "    pending_started_at = NULL, pending_updated_at = NULL, "
                "    pending_total_routines = 0, pending_done_routines = 0 "
                "WHERE project_name = ?",
                (scope,),
            )

    def cleanup_in_progress_rel_path(
        self, scope: str, epoch: int, rel_path: str,
    ) -> List[str]:
        """
        Atomic cleanup of an in-progress rel_path on resume_writing
        (architecture decision #2):
          - DELETE all unit / methods / structural / done-marker rows for
            this rel_path
          - DELETE Phase B done-markers for the same routine_ids at
            vector_epoch=epoch (startup overlap may have written them; without
            this a same-epoch reprocess would treat re-embedded units as done)
          - DELETE module FTS row (if any — should not exist in
            in_progress state but DELETE is safe)
        Single transaction; safe to re-resume after crash mid-cleanup.

        Returns the list of routine_ids that were cleaned so the caller can
        drop matching same-epoch pending Neo4j vectors (epoch+visible guard).
        """
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                # Collect routine_ids of this rel_path (their done markers
                # may exist; their unit/structural rows certainly may).
                rid_rows = cur.execute(
                    "SELECT DISTINCT routine_id FROM bsl_code_units "
                    "WHERE project_name = ? AND index_epoch = ? "
                    "  AND rel_path = ?",
                    (scope, int(epoch), rel_path),
                ).fetchall()
                routine_ids = [r["routine_id"] for r in rid_rows]

                # Delete unit / methods / FTS / done-marker rows. Reuse the
                # per-routine helper for each routine; it already handles
                # contentless FTS row delete by rowid + cascade on
                # bsl_code_unit_fields and structural sidecars.
                for rid in routine_ids:
                    self._delete_phase_a_routine_in_tx(cur, scope, epoch, rid)
                # Delete done markers for routines we just dropped.
                deleted_done_count = 0
                if routine_ids:
                    placeholders = ",".join("?" * len(routine_ids))
                    cur.execute(
                        f"DELETE FROM bsl_code_phase_a_routine_state "
                        f"WHERE project_name = ? AND index_epoch = ? "
                        f"  AND routine_id IN ({placeholders})",
                        (scope, int(epoch), *routine_ids),
                    )
                    deleted_done_count = int(cur.rowcount or 0)
                    cur.execute(
                        "UPDATE bsl_code_search_fingerprints "
                        "SET pending_done_routines = "
                        "    MAX(pending_done_routines - ?, 0), "
                        "    pending_updated_at = ? "
                        "WHERE project_name = ? AND pending_epoch = ?",
                        (deleted_done_count, now, scope, int(epoch)),
                    )
                    # Drop Phase B done-markers for these routines at the SAME
                    # vector_epoch=epoch (startup overlap writes markers before
                    # commit). Same transaction as Phase A row deletion so no
                    # window leaves a marker for a re-embedded unit.
                    cur.execute(
                        f"DELETE FROM bsl_code_phase_b_unit_state "
                        f"WHERE project_name = ? AND vector_epoch = ? "
                        f"  AND routine_id IN ({placeholders})",
                        (scope, int(epoch), *routine_ids),
                    )
                # Defensive: drop module FTS row (should not exist in
                # in_progress state — but cleanup is idempotent).
                cur.execute(
                    "DELETE FROM bsl_code_module_fts AS fts "
                    "WHERE fts.rowid IN ("
                    "  SELECT module_rowid FROM bsl_code_module_fts_rows "
                    "  WHERE project_name = ? AND index_epoch = ? "
                    "    AND rel_path = ?"
                    ")",
                    (scope, int(epoch), rel_path),
                )
                cur.execute(
                    "DELETE FROM bsl_code_module_fts_rows "
                    "WHERE project_name = ? AND index_epoch = ? "
                    "  AND rel_path = ?",
                    (scope, int(epoch), rel_path),
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        return routine_ids

    def get_phase_a_done_routine_ids(self, scope: str, epoch: int) -> set:
        with self._lock:
            rows = self._conn.execute(
                "SELECT routine_id FROM bsl_code_phase_a_routine_state "
                "WHERE project_name = ? AND index_epoch = ?",
                (scope, int(epoch)),
            ).fetchall()
        return {r["routine_id"] for r in rows}

    def _delete_phase_a_routine_in_tx(
        self, cur: sqlite3.Cursor, scope: str, epoch: int, routine_id: str,
    ) -> None:
        """In-transaction body of delete_phase_a_routine_rows. The caller
        owns BEGIN/COMMIT so this method can compose with other DELETEs."""
        rowids = [
            r["fts_rowid"]
            for r in cur.execute(
                "SELECT fts_rowid FROM bsl_code_units "
                "WHERE project_name = ? AND index_epoch = ? AND routine_id = ?",
                (scope, int(epoch), routine_id),
            ).fetchall()
        ]
        for start in range(0, len(rowids), 500):
            chunk = rowids[start: start + 500]
            placeholders = ",".join("?" * len(chunk))
            cur.execute(
                f"DELETE FROM bsl_code_units_fts WHERE rowid IN ({placeholders})",
                chunk,
            )
            cur.execute(
                f"DELETE FROM bsl_code_unit_fields WHERE fts_rowid IN ({placeholders})",
                chunk,
            )
            for table in STRUCTURAL_FTS_TABLES:
                cur.execute(
                    f"DELETE FROM {table} WHERE rowid IN ({placeholders})",
                    chunk,
                )
        cur.execute(
            "DELETE FROM bsl_code_units "
            "WHERE project_name = ? AND index_epoch = ? AND routine_id = ?",
            (scope, int(epoch), routine_id),
        )
        cur.execute(
            "DELETE FROM bsl_code_methods "
            "WHERE project_name = ? AND index_epoch = ? AND routine_id = ?",
            (scope, int(epoch), routine_id),
        )
        # NOTE: `bsl_code_phase_a_routine_state` is owned by the caller —
        # `cleanup_in_progress_rel_path` deletes it together with the
        # `pending_done_routines` counter adjustment, scoped path lets the
        # routine_state row stay (it's per-routine progress, not per-unit
        # bookkeeping for the ledger).
        cur.execute(
            "DELETE FROM bsl_code_module_fragments "
            "WHERE project_name = ? AND index_epoch = ? AND routine_id = ?",
            (scope, int(epoch), routine_id),
        )

    def delete_phase_a_routine_rows(
        self, scope: str, epoch: int, routine_id: str,
    ) -> None:
        """
        Remove a single routine's pending-epoch rows: units (and cascaded
        unit_fields), units_fts and all structural FTS sidecars by rowid,
        methods. Used on resume_writing before re-processing a routine that
        is not in phase_a_routine_state (defensive against any partial state).
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                self._delete_phase_a_routine_in_tx(cur, scope, epoch, routine_id)
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def mark_phase_a_writing_complete(self, scope: str, epoch: int) -> None:
        """Transition pending_status: writing -> finalizing. Idempotent."""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock:
            self._ensure_fingerprint_row(scope)
            self._conn.execute(
                "UPDATE bsl_code_search_fingerprints "
                "SET pending_status = 'finalizing', pending_updated_at = ? "
                "WHERE project_name = ? AND pending_epoch = ?",
                (now, scope, int(epoch)),
            )

    _PHASE_A_FINALIZE_CHUNK = 500

    def iter_module_metadata_for_rebuild(
        self, scope: str, epoch: int,
    ) -> Iterable[Dict[str, Any]]:
        """
        Stream distinct module rows from bsl_code_units for `epoch` so
        Phase A finalize can repopulate bsl_code_modules without going back
        to Neo4j. Generator: only the current chunk is buffered.

        The two-step shape is deliberate. The consumer writes bsl_code_modules
        from inside the consumption loop, and any active statement holds a read
        transaction that blocks WAL checkpointing entirely — `PRAGMA
        wal_checkpoint` fails with `database table is locked`. So the source
        DISTINCT is materialized into a TEMP table in a single pass, and that
        table is then read in `rowid` pages, each page fetched in full before it
        is yielded: no cursor stays open across a `yield`.

        The TEMP table name is unique per call. The connection is process-wide
        and the lock is released between pages, so a fixed name would be shared
        state: two live generators would drop and recreate each other's table.
        """
        sql = """
            SELECT DISTINCT owner_qn, config_name, module_type,
                            module_kind, owner_category, rel_path
            FROM bsl_code_units
            WHERE project_name = ? AND index_epoch = ? AND owner_qn != ''
        """
        table = "module_rebuild_" + uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                f'CREATE TEMP TABLE "{table}" AS {sql}', (scope, int(epoch)),
            )
        try:
            last_rowid = 0
            while True:
                with self._lock:
                    rows = self._conn.execute(
                        f'SELECT rowid, owner_qn, config_name, module_type, '
                        f'       module_kind, owner_category, rel_path '
                        f'FROM temp."{table}" '
                        f'WHERE rowid > ? ORDER BY rowid LIMIT ?',
                        (last_rowid, self._PHASE_A_FINALIZE_CHUNK),
                    ).fetchall()
                if not rows:
                    return
                last_rowid = int(rows[-1]["rowid"])
                for r in rows:
                    yield {
                        "module_id": r["owner_qn"],
                        "owner_qn": r["owner_qn"],
                        "config_name": r["config_name"] or "",
                        "module_type": r["module_type"] or "",
                        "module_kind": r["module_kind"] or "",
                        "owner_category": r["owner_category"] or "",
                        "rel_path": r["rel_path"] or "",
                    }
        finally:
            with self._lock:
                self._conn.execute(f'DROP TABLE IF EXISTS temp."{table}"')

    # ---- Phase B unit state ----

    def count_phase_b_units(
        self,
        scope: str,
        epoch: int,
        *,
        excluded_owner_categories: Optional[Sequence[str]] = None,
        exclude_regulated_reports: bool = False,
    ) -> int:
        """Count units in the committed SQLite epoch for current Phase B scope."""
        sql = (
            "SELECT COUNT(*) AS cnt FROM bsl_code_units AS u "
            "WHERE u.project_name = ? AND u.index_epoch = ?"
        )
        params: List[Any] = [scope, int(epoch)]
        excluded = tuple(excluded_owner_categories or ())
        if excluded:
            placeholders = ",".join("?" * len(excluded))
            sql += f" AND u.owner_category NOT IN ({placeholders})"
            params.extend(excluded)
        if exclude_regulated_reports:
            sql += " AND u.is_regulated_report = 0"
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["cnt"] if row else 0)

    def count_phase_b_done_units(
        self,
        scope: str,
        vector_epoch: int,
        *,
        epoch: Optional[int] = None,
        excluded_owner_categories: Optional[Sequence[str]] = None,
        exclude_regulated_reports: bool = False,
    ) -> int:
        """Count Phase B done markers for the current embeddable scope."""
        unit_epoch = int(vector_epoch if epoch is None else epoch)
        excluded = tuple(excluded_owner_categories or ())
        if not excluded and not exclude_regulated_reports and epoch is None:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS cnt FROM bsl_code_phase_b_unit_state "
                    "WHERE project_name = ? AND vector_epoch = ?",
                    (scope, int(vector_epoch)),
                ).fetchone()
            return int(row["cnt"] if row else 0)

        sql = (
            "SELECT COUNT(*) AS cnt "
            "FROM bsl_code_phase_b_unit_state AS s "
            "JOIN bsl_code_units AS u "
            "  ON u.project_name = s.project_name "
            " AND u.unit_id = s.unit_id "
            " AND u.index_epoch = ? "
            "WHERE s.project_name = ? AND s.vector_epoch = ?"
        )
        params: List[Any] = [unit_epoch, scope, int(vector_epoch)]
        if excluded:
            placeholders = ",".join("?" * len(excluded))
            sql += f" AND u.owner_category NOT IN ({placeholders})"
            params.extend(excluded)
        if exclude_regulated_reports:
            sql += " AND u.is_regulated_report = 0"
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["cnt"] if row else 0)

    def count_phase_b_not_done_units(
        self,
        scope: str,
        epoch: int,
        vector_epoch: int,
        *,
        excluded_owner_categories: Optional[Sequence[str]] = None,
        exclude_regulated_reports: bool = False,
    ) -> int:
        """Count units still pending for Phase B, matching the iterator filter."""
        excluded = tuple(excluded_owner_categories or ())
        sql = (
            """
            SELECT COUNT(*) AS cnt
            FROM bsl_code_units AS u
            WHERE u.project_name = ? AND u.index_epoch = ?
              AND NOT EXISTS (
                  SELECT 1 FROM bsl_code_phase_b_unit_state AS s
                  WHERE s.project_name = u.project_name
                    AND s.vector_epoch = ?
                    AND s.unit_id = u.unit_id
              )
            """
        )
        params: List[Any] = [scope, int(epoch), int(vector_epoch)]
        if excluded:
            placeholders = ",".join("?" * len(excluded))
            sql += f" AND u.owner_category NOT IN ({placeholders})"
            params.extend(excluded)
        if exclude_regulated_reports:
            sql += " AND u.is_regulated_report = 0"
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["cnt"] if row else 0)

    def iter_phase_b_not_done_units(
        self,
        scope: str,
        epoch: int,
        vector_epoch: int,
        worker_id: int,
        total_workers: int,
        batch_size: int = 100,
        excluded_owner_categories: Optional[Sequence[str]] = None,
        exclude_regulated_reports: bool = False,
    ) -> Iterable[List[Dict[str, Any]]]:
        """
        Stream units for Phase B partition (fts_rowid % total_workers ==
        worker_id), excluding units already marked done in phase_b state.
        Yields batches of up to `batch_size`. Uses fts_rowid keyset pagination
        so RAM holds only one batch at a time.

        `excluded_owner_categories` and `exclude_regulated_reports` apply the
        search-visible coverage policy: matching units are skipped — they
        will not be embedded, so vectors for them are not written.
        """
        size = max(1, int(batch_size))
        last_rowid = 0
        excluded = tuple(excluded_owner_categories or ())
        while True:
            sql = (
                "SELECT u.fts_rowid AS fts_rowid, u.unit_id, u.routine_id, "
                "       u.routine_name, "
                "       u.config_name, u.owner_qn, u.owner_qn_prefix, "
                "       u.owner_category, u.module_type, u.module_kind, "
                "       u.routine_type, u.export, u.line_start, u.line_end, "
                "       u.char_start, u.char_end, "
                "       u.part_index, u.part_total, u.body_hash, u.rel_path, "
                "       u.unit_kind, u.is_regulated_report "
                "FROM bsl_code_units AS u "
                "WHERE u.project_name = ? AND u.index_epoch = ? "
                "  AND u.fts_rowid > ? "
                "  AND (u.fts_rowid % ?) = ? "
                "  AND NOT EXISTS ( "
                "      SELECT 1 FROM bsl_code_phase_b_unit_state AS s "
                "      WHERE s.project_name = u.project_name "
                "        AND s.vector_epoch = ? "
                "        AND s.unit_id = u.unit_id "
                "  ) "
            )
            params: List[Any] = [
                scope, int(epoch), int(last_rowid),
                int(total_workers), int(worker_id),
                int(vector_epoch),
            ]
            if excluded:
                placeholders = ",".join("?" * len(excluded))
                sql += f"  AND u.owner_category NOT IN ({placeholders}) "
                params.extend(excluded)
            if exclude_regulated_reports:
                sql += "  AND u.is_regulated_report = 0 "
            sql += "ORDER BY u.fts_rowid LIMIT ?"
            params.append(size)
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
            if not rows:
                return
            yield [dict(r) for r in rows]
            last_rowid = int(rows[-1]["fts_rowid"])

    def get_phase_b_done_unit_ids(self, scope: str, vector_epoch: int) -> set:
        with self._lock:
            rows = self._conn.execute(
                "SELECT unit_id FROM bsl_code_phase_b_unit_state "
                "WHERE project_name = ? AND vector_epoch = ?",
                (scope, int(vector_epoch)),
            ).fetchall()
        return {r["unit_id"] for r in rows}

    def filter_phase_b_still_not_done(
        self, scope: str, vector_epoch: int, rows: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Return the subset of `rows` whose unit_id has NO done-marker for
        (scope, vector_epoch). Used by the startup overlap consumer as an
        at-least-once transport recheck: a unit row queued from Phase A may have
        been marked done meanwhile (by _maybe_transfer_phase_b_state or a prior
        catch-up round) while the worker waited on the endpoint/round. Dropping
        already-done rows before embedding avoids redundant provider calls.

        Done contract keys on unit_id only (mirrors iter_phase_b_not_done_units'
        NOT EXISTS clause); body_hash drift is still caught downstream in
        _phase_b_process_batch against the live Neo4j body_hash.
        """
        if not rows:
            return rows
        unit_ids = [r.get("unit_id") for r in rows if r.get("unit_id")]
        if not unit_ids:
            return rows
        done: set = set()
        with self._lock:
            for start in range(0, len(unit_ids), 500):
                chunk = unit_ids[start: start + 500]
                placeholders = ",".join("?" * len(chunk))
                found = self._conn.execute(
                    f"SELECT unit_id FROM bsl_code_phase_b_unit_state "
                    f"WHERE project_name = ? AND vector_epoch = ? "
                    f"  AND unit_id IN ({placeholders})",
                    (scope, int(vector_epoch), *chunk),
                ).fetchall()
                done.update(r["unit_id"] for r in found)
        if not done:
            return rows
        return [r for r in rows if r.get("unit_id") not in done]

    def mark_phase_b_units_done(
        self, scope: str, vector_epoch: int, items: List[Dict[str, Any]],
    ) -> None:
        """INSERT OR REPLACE done markers for phase B units in one transaction."""
        if not items:
            return
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                cur.executemany(
                    """
                    INSERT OR REPLACE INTO bsl_code_phase_b_unit_state(
                        project_name, vector_epoch, unit_id, routine_id,
                        unit_kind, body_hash, done_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            scope,
                            int(vector_epoch),
                            it["unit_id"],
                            it.get("routine_id", "") or "",
                            it.get("unit_kind", "") or "",
                            it.get("body_hash", "") or "",
                            now,
                        )
                        for it in items
                    ],
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

    # ------------------ Phase B transfer (post-Phase-A rebuild reuse) --
    # SQL fragments shared by count_phase_b_transferable_units and
    # iter_phase_b_transferable_units. JOIN criterion = fields that drive
    # embedding input in _build_phase_b_embedding_text + `char_end > char_start`
    # guard (excludes the line-range fallback branch). Fields NOT in the JOIN
    # criterion (line_start/line_end, owner_category, module_type, export,
    # config_name, is_regulated_report, owner_qn_prefix) are still selected
    # from the NEW epoch row so the caller can refresh denormalized Neo4j
    # node properties via CYPHER_RETAG_BSL_{SMALL,LARGE}_UNIT_EPOCH.
    _PHASE_B_TRANSFER_FROM_WHERE = (
        "FROM bsl_code_units n "
        "JOIN bsl_code_units o "
        "  ON  o.project_name = n.project_name "
        "  AND o.index_epoch  = ? "
        "  AND o.unit_id      = n.unit_id "
        "JOIN bsl_code_phase_b_unit_state d "
        "  ON  d.project_name = n.project_name "
        "  AND d.vector_epoch = ? "
        "  AND d.unit_id      = n.unit_id "
        "LEFT JOIN bsl_code_phase_b_unit_state dn "
        "  ON  dn.project_name = n.project_name "
        "  AND dn.vector_epoch = ? "
        "  AND dn.unit_id      = n.unit_id "
        "WHERE n.project_name = ? "
        "  AND n.index_epoch  = ? "
        "  AND dn.unit_id IS NULL "
        "  AND n.char_end > n.char_start "
        "  AND n.routine_id   = o.routine_id "
        "  AND n.unit_kind    = o.unit_kind "
        "  AND n.body_hash    = o.body_hash "
        "  AND n.part_index   = o.part_index "
        "  AND n.part_total   = o.part_total "
        "  AND n.char_start   = o.char_start "
        "  AND n.char_end     = o.char_end "
        "  AND n.owner_qn     = o.owner_qn "
        "  AND n.routine_name = o.routine_name "
        "  AND lower(coalesce(n.routine_type,'')) "
        "    = lower(coalesce(o.routine_type,'')) "
    )

    def count_phase_b_transferable_units(
        self,
        scope: str,
        *,
        prev_epoch: int,
        new_epoch: int,
        prev_vector_epoch: int,
    ) -> int:
        sql = "SELECT COUNT(*) AS cnt " + self._PHASE_B_TRANSFER_FROM_WHERE
        params: List[Any] = [
            int(prev_epoch), int(prev_vector_epoch), int(new_epoch),
            scope, int(new_epoch),
        ]
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["cnt"] if row else 0)

    def iter_phase_b_transferable_units(
        self,
        scope: str,
        *,
        prev_epoch: int,
        new_epoch: int,
        prev_vector_epoch: int,
        batch_size: int,
    ) -> Iterable[List[Dict[str, Any]]]:
        """Stream batches of units transferable from prev_epoch to new_epoch.

        A unit is transferable when:
        - new and old epoch both have a row with the same `unit_id`;
        - all embedding-input fields match (see JOIN criterion);
        - old vector_epoch has a done-marker for this unit;
        - new vector_epoch has NO done-marker yet;
        - new row has a valid char range (`char_end > char_start`).

        Yielded rows include both criterion fields and all denormalized fields
        consumed by CYPHER_RETAG_BSL_{SMALL,LARGE}_UNIT_EPOCH, taken from the
        NEW epoch row (`n.*`).
        """
        size = max(1, int(batch_size))
        last_rowid = 0
        select_cols = (
            "SELECT n.fts_rowid, n.unit_id, n.routine_id, n.unit_kind, "
            "       n.body_hash, n.project_name, n.config_name, "
            "       n.routine_name, n.owner_qn, n.owner_qn_prefix, "
            "       n.owner_category, n.module_type, n.routine_type, "
            "       n.export, n.line_start, n.line_end, "
            "       n.part_index, n.part_total, n.is_regulated_report "
        )
        while True:
            sql = (
                select_cols
                + self._PHASE_B_TRANSFER_FROM_WHERE
                + "  AND n.fts_rowid > ? "
                + "ORDER BY n.fts_rowid LIMIT ?"
            )
            params: List[Any] = [
                int(prev_epoch), int(prev_vector_epoch), int(new_epoch),
                scope, int(new_epoch),
                int(last_rowid), size,
            ]
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
            if not rows:
                return
            yield [dict(r) for r in rows]
            last_rowid = int(rows[-1]["fts_rowid"])

    def reset_phase_b_state(self, scope: str, vector_epoch: int) -> int:
        """Delete all phase B state rows for given vector_epoch. Returns count."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM bsl_code_phase_b_unit_state "
                "WHERE project_name = ? AND vector_epoch = ?",
                (scope, int(vector_epoch)),
            )
            return int(cur.rowcount or 0)

    def write_method(self, scope: str, epoch: int, method: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO bsl_code_methods(
                    routine_id, module_id, project_name, config_name,
                    name, signature, routine_type, symbol_kind, export, owner_qn,
                    body_hash, size_chars, size_lines, index_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    method["routine_id"],
                    method.get("module_id", "") or "",
                    scope,
                    method.get("config_name", "") or "",
                    method.get("name", "") or "",
                    method.get("signature", "") or "",
                    method.get("routine_type", "") or "",
                    method.get("symbol_kind", "") or "",
                    1 if method.get("export") else 0,
                    method.get("owner_qn", "") or "",
                    method.get("body_hash", "") or "",
                    int(method.get("size_chars", 0) or 0),
                    int(method.get("size_lines", 0) or 0),
                    int(epoch),
                ),
            )

    def write_methods_batch(
        self,
        scope: str,
        epoch: int,
        methods: List[Dict[str, Any]],
    ) -> int:
        """Insert multiple method rows in a single BEGIN/COMMIT transaction. Returns count written."""
        if not methods:
            return 0
        profiler = self._debug_phase_a_profiler()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                self._write_methods_in_tx(
                    cur, scope, epoch, methods, profiler=profiler,
                )
                section_started = profiler.start() if profiler is not None else None
                cur.execute("COMMIT")
                if profiler is not None and section_started is not None:
                    profiler.add_ms("commit", section_started)
                    profiler.maybe_log()
                return len(methods)
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def _write_methods_in_tx(
        self,
        cur: sqlite3.Cursor,
        scope: str,
        epoch: int,
        methods: List[Dict[str, Any]],
        *,
        profiler: Optional[_SqlitePhaseADebugProfiler] = None,
    ) -> int:
        if not methods:
            return 0
        section_started = profiler.start() if profiler is not None else None
        cur.executemany(
            """
            INSERT OR REPLACE INTO bsl_code_methods(
                routine_id, module_id, project_name, config_name,
                name, signature, routine_type, symbol_kind, export, owner_qn,
                body_hash, size_chars, size_lines, index_epoch
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    m["routine_id"],
                    m.get("module_id", "") or "",
                    scope,
                    m.get("config_name", "") or "",
                    m.get("name", "") or "",
                    m.get("signature", "") or "",
                    m.get("routine_type", "") or "",
                    m.get("symbol_kind", "") or "",
                    1 if m.get("export") else 0,
                    m.get("owner_qn", "") or "",
                    m.get("body_hash", "") or "",
                    int(m.get("size_chars", 0) or 0),
                    int(m.get("size_lines", 0) or 0),
                    int(epoch),
                )
                for m in methods
            ],
        )
        if profiler is not None and section_started is not None:
            profiler.add_ms("methods_insert", section_started)
            profiler.add_rows(methods=len(methods))
        return len(methods)

    def write_module(self, scope: str, epoch: int, module: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO bsl_code_modules(
                    module_id, project_name, config_name, module_type,
                    module_kind, owner_qn, owner_category, rel_path, index_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    module["module_id"],
                    scope,
                    module.get("config_name", "") or "",
                    module.get("module_type", "") or "",
                    module.get("module_kind", "") or "",
                    module.get("owner_qn", "") or "",
                    module.get("owner_category", "") or "",
                    module.get("rel_path", "") or "",
                    int(epoch),
                ),
            )

    def write_modules_batch(
        self, scope: str, epoch: int, modules: Sequence[Dict[str, Any]],
    ) -> int:
        """Insert multiple module rows in a single BEGIN/COMMIT transaction. Returns count written."""
        if not modules:
            return 0
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                cur.executemany(
                    """
                    INSERT OR REPLACE INTO bsl_code_modules(
                        module_id, project_name, config_name, module_type,
                        module_kind, owner_qn, owner_category, rel_path, index_epoch
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            m["module_id"],
                            scope,
                            m.get("config_name", "") or "",
                            m.get("module_type", "") or "",
                            m.get("module_kind", "") or "",
                            m.get("owner_qn", "") or "",
                            m.get("owner_category", "") or "",
                            m.get("rel_path", "") or "",
                            int(epoch),
                        )
                        for m in modules
                    ],
                )
                cur.execute("COMMIT")
                return len(modules)
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def store_corpus_stats(
        self,
        scope: str,
        epoch: int,
        field_kind: str,
        doc_count: int,
        total_length: int,
    ) -> None:
        """
        Replace stats row for (scope, epoch, field_kind). Internal storage
        is additive (doc_count, total_length) — avgdl is derived on read.
        For incremental per-batch UPSERT during Phase A use
        upsert_corpus_stats_delta instead.
        """
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO bsl_code_corpus_stats("
                "project_name, index_epoch, field_kind, doc_count, total_length"
                ") VALUES (?, ?, ?, ?, ?)",
                (scope, int(epoch), field_kind, int(doc_count), int(total_length)),
            )

    def commit_pending(
        self,
        scope: str,
        new_fingerprint: str,
        new_source_state_hash: str,
    ) -> int:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                row = cur.execute(
                    "SELECT current_epoch, pending_epoch, retired_epochs "
                    "FROM bsl_code_search_fingerprints WHERE project_name = ?",
                    (scope,),
                ).fetchone()
                if not row or row["pending_epoch"] is None:
                    raise BslCodeSqliteError(
                        f"commit_pending: no pending_epoch for scope={scope!r}"
                    )
                old_current = int(row["current_epoch"]) if row["current_epoch"] is not None else 0
                new_current = int(row["pending_epoch"])
                retired = json.loads(row["retired_epochs"] or "[]")
                if old_current and old_current != new_current:
                    retired.append({"epoch": old_current, "retired_at": time.time()})
                cur.execute(
                    """
                    UPDATE bsl_code_search_fingerprints
                    SET current_epoch = ?,
                        pending_epoch = NULL,
                        fingerprint = ?,
                        source_state_hash = ?,
                        reindex_requested = 0,
                        indexed_at = ?,
                        retired_epochs = ?,
                        pending_fingerprint = '',
                        pending_source_state_hash = '',
                        pending_status = 'idle',
                        pending_started_at = NULL,
                        pending_updated_at = NULL,
                        pending_total_routines = 0,
                        pending_done_routines = 0,
                        embedding_fingerprint = ''
                    WHERE project_name = ?
                    """,
                    (
                        new_current,
                        new_fingerprint,
                        new_source_state_hash,
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        json.dumps(retired),
                        scope,
                    ),
                )
                # Phase A pending routine markers for the just-committed
                # epoch are no longer needed.
                cur.execute(
                    "DELETE FROM bsl_code_phase_a_routine_state "
                    "WHERE project_name = ? AND index_epoch = ?",
                    (scope, new_current),
                )
                cur.execute("COMMIT")
                return new_current
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def read_phase_b_transfer_snapshot(self, scope: str) -> Dict[str, Any]:
        """Read the durable Phase B transfer snapshot captured by
        begin_or_resume_pending. Returns the prior committed Phase B state
        regardless of how many times vector_status has been reset since.
        Empty/NULL fields signal "no snapshot" (fresh deploy or already
        cleared after Phase B finalize)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT phase_b_transfer_prev_current_epoch, "
                "phase_b_transfer_prev_vector_status, "
                "phase_b_transfer_prev_vector_epoch, "
                "phase_b_transfer_prev_phase_a_fingerprint, "
                "phase_b_transfer_prev_embedding_fingerprint "
                "FROM bsl_code_search_fingerprints WHERE project_name = ?",
                (scope,),
            ).fetchone()
            if not row:
                return {
                    "prev_current_epoch": None,
                    "prev_vector_status": "",
                    "prev_vector_epoch": None,
                    "prev_phase_a_fingerprint": "",
                    "prev_embedding_fingerprint": "",
                }
            return {
                "prev_current_epoch": (
                    int(row["phase_b_transfer_prev_current_epoch"])
                    if row["phase_b_transfer_prev_current_epoch"] is not None
                    else None
                ),
                "prev_vector_status": str(
                    row["phase_b_transfer_prev_vector_status"] or ""
                ),
                "prev_vector_epoch": (
                    int(row["phase_b_transfer_prev_vector_epoch"])
                    if row["phase_b_transfer_prev_vector_epoch"] is not None
                    else None
                ),
                "prev_phase_a_fingerprint": str(
                    row["phase_b_transfer_prev_phase_a_fingerprint"] or ""
                ),
                "prev_embedding_fingerprint": str(
                    row["phase_b_transfer_prev_embedding_fingerprint"] or ""
                ),
            }

    def clear_phase_b_transfer_snapshot(self, scope: str) -> None:
        """Clear the durable Phase B transfer snapshot. Called after a
        successful Phase B finalize (or after a SKIPPED finalize when the
        vector subsystem is disabled) — the snapshot has done its job. Not
        called on Phase B exception: the next pending epoch will overwrite
        it if it goes stale, and retry can still benefit from it otherwise.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE bsl_code_search_fingerprints SET "
                "phase_b_transfer_prev_current_epoch = NULL, "
                "phase_b_transfer_prev_vector_status = '', "
                "phase_b_transfer_prev_vector_epoch = NULL, "
                "phase_b_transfer_prev_phase_a_fingerprint = '', "
                "phase_b_transfer_prev_embedding_fingerprint = '' "
                "WHERE project_name = ?",
                (scope,),
            )

    def set_vector_status(
        self,
        scope: str,
        status: str,
        vector_epoch: Optional[int] = None,
        *,
        embedding_fingerprint: Optional[str] = None,
    ) -> None:
        if status not in ("not_started", "building", "ready", "failed"):
            raise BslCodeSqliteError(f"set_vector_status: invalid status {status!r}")
        with self._lock:
            self._ensure_fingerprint_row(scope)
            sets = ["vector_status = ?"]
            params: List[Any] = [status]
            if vector_epoch is not None:
                sets.append("vector_epoch = ?")
                params.append(int(vector_epoch))
            if embedding_fingerprint is not None:
                sets.append("embedding_fingerprint = ?")
                params.append(str(embedding_fingerprint))
            params.append(scope)
            self._conn.execute(
                "UPDATE bsl_code_search_fingerprints SET "
                + ", ".join(sets)
                + " WHERE project_name = ?",
                tuple(params),
            )

    def read_coverage_state(self, scope: str) -> Dict[str, Any]:
        """Read stored coverage policy + fingerprint for scope.

        Returns dict with `coverage_policy_json` (str, "" if absent) and
        `coverage_fingerprint` (str, "" if absent). Empty values signal that
        no coverage state has ever been committed (first deploy).
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT coverage_policy_json, coverage_fingerprint "
                "FROM bsl_code_search_fingerprints WHERE project_name = ?",
                (scope,),
            ).fetchone()
            if not row:
                return {"coverage_policy_json": "", "coverage_fingerprint": ""}
            return {
                "coverage_policy_json": row["coverage_policy_json"] or "",
                "coverage_fingerprint": row["coverage_fingerprint"] or "",
            }

    def commit_coverage_state(
        self,
        scope: str,
        policy_json: str,
        fingerprint: str,
        vector_status: Optional[str] = None,
        vector_epoch: Optional[int] = None,
        *,
        embedding_fingerprint: Optional[str] = None,
    ) -> None:
        """Atomically write coverage payload + fingerprint, optionally also
        vector_status / vector_epoch / embedding_fingerprint in the same
        transaction.

        Used both for hidden-only transitions (no vector_status change) and
        after a successful visible-delta Phase B (status='ready', epoch
        carried through unchanged, embedding_fingerprint set to current).

        `embedding_fingerprint=None` leaves the column untouched; pass an empty
        string to clear it explicitly.
        """
        with self._lock:
            self._ensure_fingerprint_row(scope)
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                sets = [
                    "coverage_policy_json = ?",
                    "coverage_fingerprint = ?",
                ]
                params: List[Any] = [policy_json, fingerprint]
                if vector_status is not None:
                    sets.append("vector_status = ?")
                    params.append(vector_status)
                if vector_epoch is not None:
                    sets.append("vector_epoch = ?")
                    params.append(int(vector_epoch))
                if embedding_fingerprint is not None:
                    sets.append("embedding_fingerprint = ?")
                    params.append(str(embedding_fingerprint))
                params.append(scope)
                cur.execute(
                    "UPDATE bsl_code_search_fingerprints SET "
                    + ", ".join(sets)
                    + " WHERE project_name = ?",
                    tuple(params),
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def _request_reindex_in_tx(self, cur: sqlite3.Cursor, scope: str) -> None:
        """In-tx близнец `request_reindex` (пара как у
        `_delete_phase_a_routine_in_tx` / `delete_phase_a_routine_rows`).

        Нужен, чтобы удаление строк конфигурации и обязательство перестроить
        индекс попадали в ОДНУ транзакцию. Раздельные вызовы оставляли бы
        crash-окно: падение между ними приводит к тому, что повтор видит уже
        очищенные таблицы, получает `deleted == 0` и по правилу «reindex только
        при deleted > 0» флаг не ставит — corpus IDF/stats остаются от старого
        поколения без сигнала на восстановление.
        """
        cur.execute(
            "INSERT OR IGNORE INTO bsl_code_search_fingerprints(project_name) VALUES (?)",
            (scope,),
        )
        cur.execute(
            "UPDATE bsl_code_search_fingerprints "
            "SET reindex_requested = 1 WHERE project_name = ?",
            (scope,),
        )

    def request_reindex(self, scope: str) -> None:
        with self._lock:
            self._ensure_fingerprint_row(scope)
            self._conn.execute(
                "UPDATE bsl_code_search_fingerprints "
                "SET reindex_requested = 1 WHERE project_name = ?",
                (scope,),
            )

    def request_reindex_if_pending_active(self, scope: str) -> bool:
        """Поставить `reindex_requested = 1`, но только если pending epoch активна.

        Это тот самый сигнал от cleanup-очереди к владельцу прогресса. Без него
        escape в `BslCodeSearchSync` недостижим в каноническом сценарии удаления:
        в установившемся состоянии `reindex_requested = 0`, graph stage флага не
        трогает, а атомарный purge до флага не доходит, потому что операция
        выходит раньше на `PENDING_REBUILD`. Отметка `pending_updated_at` стареет,
        но escape так и не срабатывает, и очередь стоит вечно.

        Перепроверка активности ВНУТРИ транзакции обязательна: без неё гонка
        «pending завершилась между чтением readiness и записью флага» приводила
        бы к лишней полной перестройке здорового индекса.

        Возвращает True, если флаг был выставлен.
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                row = cur.execute(
                    "SELECT pending_epoch, pending_status "
                    "FROM bsl_code_search_fingerprints WHERE project_name = ?",
                    (scope,),
                ).fetchone()
                if row is None:
                    cur.execute("COMMIT")
                    return False
                pending_epoch = row["pending_epoch"] if "pending_epoch" in row.keys() else row[0]
                pending_status = (
                    row["pending_status"] if "pending_status" in row.keys() else row[1]
                ) or ""
                if pending_epoch is None or pending_status not in ("writing", "finalizing"):
                    cur.execute("COMMIT")
                    return False
                self._request_reindex_in_tx(cur, scope)
                cur.execute("COMMIT")
                return True
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def pending_is_stale(self, scope: str, stale_after_seconds: int) -> bool:
        """Устарела ли отметка живости pending epoch.

        `pending_updated_at` обновляется на каждом flush прогресса Phase A,
        поэтому живой rebuild держит её свежей, а аварийно завершившийся —
        оставляет стареть. Это позволяет отличить мёртвый поток от живого по
        данным, без доступа к самому потоку.

        При отсутствии `pending_updated_at` берётся `pending_started_at`; если
        нет и его — считаем НЕ устаревшим (fail closed: не трогать эпоху,
        про которую ничего не известно).
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT pending_epoch, pending_status, pending_started_at, pending_updated_at "
                "FROM bsl_code_search_fingerprints WHERE project_name = ?",
                (scope,),
            ).fetchone()
        if row is None:
            return False
        keys = row.keys() if hasattr(row, "keys") else []

        def _get(name: str, idx: int):
            return row[name] if name in keys else row[idx]

        if _get("pending_epoch", 0) is None:
            return False
        if (_get("pending_status", 1) or "") not in ("writing", "finalizing"):
            return False
        stamp = _get("pending_updated_at", 3) or _get("pending_started_at", 2)
        if not stamp:
            return False
        try:
            marker = time.mktime(time.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        except (ValueError, OverflowError):
            return False
        return (time.time() - marker) > max(1, int(stale_after_seconds))

    def commit_scoped_delta(
        self,
        scope: str,
        source_state_hash: str,
        fingerprint: str,
        *,
        clear_ledger_routine_ids: Optional[Iterable[str]] = None,
        clear_pending_rel_paths: Optional[Iterable[str]] = None,
        expected_epoch: Optional[int] = None,
        expected_fingerprint: Optional[str] = None,
        expect_reindex_cleared: bool = False,
        config_delete_operation_id: Optional[str] = None,
    ) -> "CommitOutcome":
        """Атомарный финал успешного scoped delta apply.

        Делает в одной tx:
        - обновляет source_state_hash/fingerprint;
        - сбрасывает ВСЕ scoped flags (reindex_requested, scoped_apply_in_progress,
          scoped_retry_pending, visibility_flip_done, pending_routine_ids_json,
          pending_rel_paths_json);
        - удаляет ledger rows для `clear_ledger_routine_ids`;
        - удаляет snapshot rows для `clear_ledger_routine_ids` (на случай если
          они остались — повторная очистка идемпотентна);
        - при `config_delete_operation_id` переводит header операции в
          `committed` ТУТ ЖЕ. Отдельный вызов рядом создавал бы окно, в котором
          любой из двух порядков даёт неверное восстановление после рестарта:
          пометить раньше — принять невычтенные counters за согласованные;
          позже — отправить уже согласованный индекс в legacy purge с полной
          перестройкой.

        CAS-условия (все необязательные, без них поведение прежнее):
        `expected_epoch`, `expected_fingerprint`, `expect_reindex_cleared`.
        Последнее важно само по себе: безусловная запись `reindex_requested = 0`
        молча стирала бы чужое обязательство на полную перестройку, возникшее во
        время apply.

        Возвращает `CommitOutcome` — причина отказа вычисляется ВНУТРИ той же
        транзакции, иначе классификация liveness сама была бы гонкой.
        """
        ledger_ids = list(clear_ledger_routine_ids or [])
        with self._lock:
            self._ensure_fingerprint_row(scope)
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                sql = (
                    "UPDATE bsl_code_search_fingerprints "
                    "SET source_state_hash = ?, fingerprint = ?, "
                    "    reindex_requested = 0, "
                    "    scoped_apply_in_progress = 0, "
                    "    scoped_retry_pending = 0, "
                    "    visibility_flip_done = 0, "
                    "    pending_routine_ids_json = '[]', "
                    "    pending_rel_paths_json = '[]' "
                    "WHERE project_name = ? AND pending_epoch IS NULL"
                )
                params: List[Any] = [source_state_hash, fingerprint, scope]
                if expected_epoch is not None:
                    sql += " AND current_epoch = ?"
                    params.append(int(expected_epoch))
                if expected_fingerprint is not None:
                    sql += " AND fingerprint = ?"
                    params.append(expected_fingerprint)
                if expect_reindex_cleared:
                    sql += " AND reindex_requested = 0"
                upd = cur.execute(sql, tuple(params))
                if (upd.rowcount or 0) == 0:
                    outcome = self._classify_commit_rejection_in_tx(
                        cur, scope,
                        expected_epoch=expected_epoch,
                        expected_fingerprint=expected_fingerprint,
                        expect_reindex_cleared=expect_reindex_cleared,
                    )
                    cur.execute("ROLLBACK")
                    return outcome
                if config_delete_operation_id:
                    cur.execute(
                        "UPDATE bsl_code_config_delete_ops "
                        "SET state = 'committed', updated_at = ? "
                        "WHERE project_name = ? AND operation_id = ?",
                        (int(time.time()), scope, config_delete_operation_id),
                    )
                if ledger_ids:
                    for start in range(0, len(ledger_ids), 500):
                        chunk = ledger_ids[start: start + 500]
                        placeholders = ",".join("?" * len(chunk))
                        cur.execute(
                            f"DELETE FROM bsl_code_pending_scoped_delta "
                            f"WHERE project_name = ? AND routine_id IN ({placeholders})",
                            (scope, *chunk),
                        )
                        cur.execute(
                            f"DELETE FROM bsl_code_pending_reverse_snapshot "
                            f"WHERE project_name = ? AND routine_id IN ({placeholders})",
                            (scope, *chunk),
                        )
                cur.execute("COMMIT")
                return CommitOutcome.COMMITTED
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def _classify_commit_rejection_in_tx(
        self,
        cur: sqlite3.Cursor,
        scope: str,
        *,
        expected_epoch: Optional[int],
        expected_fingerprint: Optional[str],
        expect_reindex_cleared: bool,
    ) -> "CommitOutcome":
        """Почему CAS не прошёл. Читается в той же транзакции, что и UPDATE.

        Порядок проверок = порядку владения восстановлением: активная pending
        epoch важнее устаревшей epoch, та важнее fingerprint, и только потом
        reindex-флаг.
        """
        row = cur.execute(
            "SELECT current_epoch, pending_epoch, fingerprint, reindex_requested "
            "FROM bsl_code_search_fingerprints WHERE project_name = ?",
            (scope,),
        ).fetchone()
        if row is None:
            return CommitOutcome.REJECTED_EPOCH
        if row["pending_epoch"] is not None:
            return CommitOutcome.REJECTED_PENDING
        if expected_epoch is not None and int(row["current_epoch"] or 0) != int(expected_epoch):
            return CommitOutcome.REJECTED_EPOCH
        if expected_fingerprint is not None and (row["fingerprint"] or "") != expected_fingerprint:
            return CommitOutcome.REJECTED_FINGERPRINT
        if expect_reindex_cleared and int(row["reindex_requested"] or 0):
            return CommitOutcome.REJECTED_REINDEX
        return CommitOutcome.REJECTED_PENDING

    def classify_delta_readiness(self, scope: str):
        """Семантическая классификация fingerprint state для phase 5.

        Возможные значения:
          READY            — scoped delta можно применить.
          PENDING_REBUILD  — background full rebuild активен; scoped delta skip.
          REINDEX_REQUIRED — fingerprint mismatch; controlled full rebuild через start_indexing.
          CONFIG_DELETE_IN_PROGRESS — идёт удаление конфигурации; чужие
                              потребители обязаны отступить, не трогая ни gate,
                              ни counters.
          SCOPED_RETRY     — unfinished scoped apply (любой из gating флагов /
                              ledger rows / scoped_apply_in_progress=1); replay scoped.

        Приоритет `REINDEX_REQUIRED` ВЫШЕ `CONFIG_DELETE_IN_PROGRESS` не
        косметика: config delete не имеет права запирать восстановление
        индекса, иначе операция, уже пересёкшая destructive-границу, остаётся
        без выхода. Операция в `fallback_rebuild_required` этого состояния не
        даёт вовсе — она сама передала владение rebuild'у.

        Это сигнал для ЧУЖИХ потребителей. Владелец операции (координатор,
        знающий её `operation_id`) обязан отличать собственное постусловие от
        конфликта, иначе не сможет усыновить свою же подготовку после сбоя.
        """
        from .bsl_code_search_delta import DeltaReadiness  # local import: avoid cycle
        with self._lock:
            row = self._conn.execute(
                "SELECT pending_epoch, pending_status, reindex_requested, "
                "       scoped_retry_pending, scoped_apply_in_progress "
                "FROM bsl_code_search_fingerprints WHERE project_name = ?",
                (scope,),
            ).fetchone()
            ledger_row = self._conn.execute(
                "SELECT 1 FROM bsl_code_pending_scoped_delta "
                "WHERE project_name = ? LIMIT 1",
                (scope,),
            ).fetchone()
            config_delete_row = self._conn.execute(
                "SELECT 1 FROM bsl_code_config_delete_ops "
                "WHERE project_name = ? AND state IN ('prepared', 'committed') "
                "LIMIT 1",
                (scope,),
            ).fetchone()
        if row is None:
            if config_delete_row is not None:
                return DeltaReadiness.CONFIG_DELETE_IN_PROGRESS
            return (DeltaReadiness.SCOPED_RETRY if ledger_row is not None
                    else DeltaReadiness.READY)
        keys = row.keys() if hasattr(row, "keys") else []
        def _get(k, idx):
            return row[k] if k in keys else row[idx]
        pending_epoch = _get("pending_epoch", 0)
        pending_status = (_get("pending_status", 1) or "")
        reindex_requested = bool(_get("reindex_requested", 2))
        scoped_retry_pending = bool(_get("scoped_retry_pending", 3))
        scoped_apply_in_progress = bool(_get("scoped_apply_in_progress", 4))
        if pending_epoch is not None and pending_status in ("writing", "finalizing"):
            return DeltaReadiness.PENDING_REBUILD
        if reindex_requested:
            return DeltaReadiness.REINDEX_REQUIRED
        if config_delete_row is not None:
            return DeltaReadiness.CONFIG_DELETE_IN_PROGRESS
        if scoped_retry_pending or scoped_apply_in_progress or ledger_row is not None:
            return DeltaReadiness.SCOPED_RETRY
        return DeltaReadiness.READY

    # ---------------------------------------------------------- scoped helpers

    def get_current_epoch(self, scope: str) -> Optional[int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT current_epoch FROM bsl_code_search_fingerprints "
                "WHERE project_name = ?",
                (scope,),
            ).fetchone()
        if row is None:
            return None
        val = row["current_epoch"] if "current_epoch" in row.keys() else row[0]
        return int(val) if val is not None else None

    def has_active_pending(self, scope: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT pending_epoch, pending_status "
                "FROM bsl_code_search_fingerprints WHERE project_name = ?",
                (scope,),
            ).fetchone()
        if row is None:
            return False
        pending_epoch = row["pending_epoch"] if "pending_epoch" in row.keys() else row[0]
        pending_status = (row["pending_status"] if "pending_status" in row.keys() else row[1]) or ""
        return pending_epoch is not None and pending_status in ("writing", "finalizing")

    def set_scoped_apply_in_progress_atomic(
        self,
        scope: str,
        in_progress: bool,
        *,
        routine_ids: Optional[Iterable[str]] = None,
        rel_paths: Optional[Iterable[str]] = None,
        also_set_scoped_retry_pending: bool = False,
        visibility_flip_done: bool = False,
    ) -> None:
        """Атомарно (одна tx) выставляет / снимает scoped reader-consistency gate.

        Когда `in_progress=True`:
          - scoped_apply_in_progress = 1
          - pending_routine_ids_json / pending_rel_paths_json = json(routine_ids/rel_paths)
          - scoped_retry_pending = 1 если also_set_scoped_retry_pending
          - visibility_flip_done = 0 (по умолчанию; восстанавливается через mark_visibility_flip_done)

        Когда `in_progress=False`:
          - scoped_apply_in_progress = 0, pending_*_json = '[]', visibility_flip_done = 0
            (scoped_retry_pending не трогаем — он управляется отдельным API / commit).
        """
        with self._lock:
            self._ensure_fingerprint_row(scope)
            self._set_scoped_gate_in_tx(
                self._conn, scope, in_progress,
                routine_ids=routine_ids, rel_paths=rel_paths,
                also_set_scoped_retry_pending=also_set_scoped_retry_pending,
                visibility_flip_done=visibility_flip_done,
                ensure_row=False,
            )

    def _set_scoped_gate_in_tx(
        self,
        cur: Any,
        scope: str,
        in_progress: bool,
        *,
        routine_ids: Optional[Iterable[str]] = None,
        rel_paths: Optional[Iterable[str]] = None,
        also_set_scoped_retry_pending: bool = False,
        visibility_flip_done: bool = False,
        ensure_row: bool = True,
    ) -> None:
        """In-tx близнец `set_scoped_apply_in_progress_atomic`.

        Нужен, чтобы поднятие/снятие gate попадало в ОДНУ транзакцию с записью
        или удалением config-delete операции: раздельные вызовы оставляли бы
        окно, где снимок уже есть, а поиск ещё показывает расширение (или
        наоборот — операции нет, а gate висит).

        `visibility_flip_done=True` у config delete осмысленно: консервативный
        режим векторной ноги не нужен, потому что удаляемые routines и так
        вычитаются из кандидатов обеих ног.
        """
        ids_json = json.dumps(sorted(routine_ids or []), ensure_ascii=False)
        rel_paths_json = json.dumps(sorted(rel_paths or []), ensure_ascii=False)
        if ensure_row:
            cur.execute(
                "INSERT OR IGNORE INTO bsl_code_search_fingerprints(project_name) "
                "VALUES (?)",
                (scope,),
            )
        if in_progress:
            sets = [
                "scoped_apply_in_progress = 1",
                "pending_routine_ids_json = ?",
                "pending_rel_paths_json = ?",
                "visibility_flip_done = ?",
            ]
            params: List[Any] = [
                ids_json, rel_paths_json, 1 if visibility_flip_done else 0,
            ]
            if also_set_scoped_retry_pending:
                sets.append("scoped_retry_pending = 1")
            params.append(scope)
            cur.execute(
                f"UPDATE bsl_code_search_fingerprints SET {', '.join(sets)} "
                f"WHERE project_name = ?",
                tuple(params),
            )
        else:
            cur.execute(
                "UPDATE bsl_code_search_fingerprints "
                "SET scoped_apply_in_progress = 0, "
                "    pending_routine_ids_json = '[]', "
                "    pending_rel_paths_json = '[]', "
                "    visibility_flip_done = 0 "
                "WHERE project_name = ?",
                (scope,),
            )

    def mark_visibility_flip_done(self, scope: str, value: bool) -> None:
        with self._lock:
            self._ensure_fingerprint_row(scope)
            self._conn.execute(
                "UPDATE bsl_code_search_fingerprints "
                "SET visibility_flip_done = ? WHERE project_name = ?",
                (1 if value else 0, scope),
            )

    def set_scoped_retry_pending(self, scope: str, value: bool) -> None:
        with self._lock:
            self._ensure_fingerprint_row(scope)
            self._conn.execute(
                "UPDATE bsl_code_search_fingerprints "
                "SET scoped_retry_pending = ? WHERE project_name = ?",
                (1 if value else 0, scope),
            )

    def read_scoped_pending_state(self, scope: str) -> Dict[str, Any]:
        """Single SELECT, удобный для search service: возвращает все scoped flags
        + parsed JSON sets. Используется для conservative path / gate filters."""
        with self._lock:
            row = self._conn.execute(
                "SELECT scoped_apply_in_progress, scoped_retry_pending, "
                "       visibility_flip_done, pending_routine_ids_json, "
                "       pending_rel_paths_json "
                "FROM bsl_code_search_fingerprints WHERE project_name = ?",
                (scope,),
            ).fetchone()
        if row is None:
            return {
                "scoped_apply_in_progress": False,
                "scoped_retry_pending": False,
                "visibility_flip_done": False,
                "pending_routine_ids": set(),
                "pending_rel_paths": set(),
            }
        keys = row.keys() if hasattr(row, "keys") else []
        def _get(k, idx):
            return row[k] if k in keys else row[idx]
        try:
            rids = json.loads(_get("pending_routine_ids_json", 3) or "[]")
        except Exception:
            rids = []
        try:
            rps = json.loads(_get("pending_rel_paths_json", 4) or "[]")
        except Exception:
            rps = []
        return {
            "scoped_apply_in_progress": bool(_get("scoped_apply_in_progress", 0)),
            "scoped_retry_pending": bool(_get("scoped_retry_pending", 1)),
            "visibility_flip_done": bool(_get("visibility_flip_done", 2)),
            "pending_routine_ids": set(rids or []),
            "pending_rel_paths": set(rps or []),
        }

    # ---------------------------------------------------------- snapshot + ledger

    def write_pending_snapshot_and_ledger(
        self,
        scope: str,
        snapshot_entries: Sequence[Dict[str, Any]],
        ledger_rows: Sequence[Dict[str, Any]],
    ) -> None:
        now = int(time.time())
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                if snapshot_entries:
                    cur.executemany(
                        "INSERT INTO bsl_code_pending_reverse_snapshot("
                        "project_name, routine_id, idf_json, stats_json, created_at"
                        ") VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(project_name, routine_id) DO UPDATE SET "
                        "    idf_json = excluded.idf_json, "
                        "    stats_json = excluded.stats_json, "
                        "    created_at = excluded.created_at",
                        [
                            (
                                scope,
                                e["routine_id"],
                                e.get("idf_json") or "{}",
                                e.get("stats_json") or "{}",
                                now,
                            )
                            for e in snapshot_entries
                        ],
                    )
                if ledger_rows:
                    cur.executemany(
                        "INSERT INTO bsl_code_pending_scoped_delta("
                        "project_name, routine_id, change_kind, old_rel_path, "
                        "new_rel_path, vector_epoch_target, stage, updated_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(project_name, routine_id) DO UPDATE SET "
                        "    change_kind = excluded.change_kind, "
                        "    old_rel_path = excluded.old_rel_path, "
                        "    new_rel_path = excluded.new_rel_path, "
                        "    vector_epoch_target = excluded.vector_epoch_target, "
                        "    stage = excluded.stage, "
                        "    updated_at = excluded.updated_at",
                        [
                            (
                                scope,
                                r["routine_id"],
                                r["change_kind"],
                                r.get("old_rel_path") or "",
                                r.get("new_rel_path") or "",
                                int(r.get("vector_epoch_target") or 0),
                                r["stage"],
                                now,
                            )
                            for r in ledger_rows
                        ],
                    )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def read_pending_reverse_snapshot(
        self, scope: str, routine_ids: Optional[Iterable[str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Returns {routine_id: {"idf": dict, "stats": dict}}.
        If routine_ids is None, returns all rows for the scope."""
        result: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            if routine_ids is None:
                cur = self._conn.execute(
                    "SELECT routine_id, idf_json, stats_json "
                    "FROM bsl_code_pending_reverse_snapshot WHERE project_name = ?",
                    (scope,),
                )
                rows = cur.fetchall()
            else:
                ids = list(routine_ids)
                if not ids:
                    return {}
                rows = []
                for start in range(0, len(ids), 500):
                    chunk = ids[start: start + 500]
                    placeholders = ",".join("?" * len(chunk))
                    cur = self._conn.execute(
                        f"SELECT routine_id, idf_json, stats_json "
                        f"FROM bsl_code_pending_reverse_snapshot "
                        f"WHERE project_name = ? AND routine_id IN ({placeholders})",
                        (scope, *chunk),
                    )
                    rows.extend(cur.fetchall())
        for r in rows:
            rid = r["routine_id"] if "routine_id" in r.keys() else r[0]
            idf_json = r["idf_json"] if "idf_json" in r.keys() else r[1]
            stats_json = r["stats_json"] if "stats_json" in r.keys() else r[2]
            try:
                idf = json.loads(idf_json or "{}")
            except Exception:
                idf = {}
            try:
                stats_raw = json.loads(stats_json or "{}")
            except Exception:
                stats_raw = {}
            stats = {fk: tuple(v) if isinstance(v, list) else v for fk, v in stats_raw.items()}
            result[rid] = {"idf": idf, "stats": stats}
        return result

    def read_pending_scoped_delta(self, scope: str) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT routine_id, change_kind, old_rel_path, new_rel_path, "
                "       vector_epoch_target, stage, updated_at "
                "FROM bsl_code_pending_scoped_delta WHERE project_name = ? "
                "ORDER BY routine_id",
                (scope,),
            )
            rows = cur.fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            keys = r.keys() if hasattr(r, "keys") else []
            def _get(k, idx):
                return r[k] if k in keys else r[idx]
            out.append({
                "routine_id": _get("routine_id", 0),
                "change_kind": _get("change_kind", 1),
                "old_rel_path": _get("old_rel_path", 2) or "",
                "new_rel_path": _get("new_rel_path", 3) or "",
                "vector_epoch_target": int(_get("vector_epoch_target", 4) or 0),
                "stage": _get("stage", 5),
                "updated_at": int(_get("updated_at", 6) or 0),
            })
        return out

    def update_pending_scoped_delta_stage(
        self, scope: str, routine_ids: Iterable[str], stage: str,
    ) -> int:
        ids = list(routine_ids)
        if not ids:
            return 0
        now = int(time.time())
        total = 0
        with self._lock:
            cur = self._conn.cursor()
            for start in range(0, len(ids), 500):
                chunk = ids[start: start + 500]
                placeholders = ",".join("?" * len(chunk))
                cur.execute(
                    f"UPDATE bsl_code_pending_scoped_delta "
                    f"SET stage = ?, updated_at = ? "
                    f"WHERE project_name = ? AND routine_id IN ({placeholders})",
                    (stage, now, scope, *chunk),
                )
                total += cur.rowcount or 0
        return total

    # ---------------------------------------------------------- module fragments

    def write_module_fragments(
        self, scope: str, epoch: int, fragments: Sequence[Dict[str, Any]],
    ) -> int:
        if not fragments:
            return 0
        rows = [
            (
                scope,
                int(epoch),
                f["routine_id"],
                f.get("rel_path") or "",
                int(f.get("routine_ordinal") or 0),
                json.dumps(f, ensure_ascii=False, sort_keys=True),
            )
            for f in fragments
        ]
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                cur.executemany(
                    "INSERT INTO bsl_code_module_fragments("
                    "project_name, index_epoch, routine_id, rel_path, "
                    "routine_ordinal, fragment_json"
                    ") VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(project_name, index_epoch, routine_id) DO UPDATE SET "
                    "    rel_path = excluded.rel_path, "
                    "    routine_ordinal = excluded.routine_ordinal, "
                    "    fragment_json = excluded.fragment_json",
                    rows,
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        return len(rows)

    def delete_module_fragments_by_routine_ids(
        self, scope: str, epoch: int, routine_ids: Iterable[str],
    ) -> int:
        ids = list(routine_ids)
        if not ids:
            return 0
        total = 0
        with self._lock:
            cur = self._conn.cursor()
            for start in range(0, len(ids), 500):
                chunk = ids[start: start + 500]
                placeholders = ",".join("?" * len(chunk))
                cur.execute(
                    f"DELETE FROM bsl_code_module_fragments "
                    f"WHERE project_name = ? AND index_epoch = ? "
                    f"AND routine_id IN ({placeholders})",
                    (scope, int(epoch), *chunk),
                )
                total += cur.rowcount or 0
        return total

    def iter_module_fragments_for_rel_paths(
        self, scope: str, epoch: int, rel_paths: Iterable[str],
    ) -> Iterable[Dict[str, Any]]:
        rps = list(rel_paths)
        if not rps:
            return
        with self._lock:
            for start in range(0, len(rps), 500):
                chunk = rps[start: start + 500]
                placeholders = ",".join("?" * len(chunk))
                cur = self._conn.execute(
                    f"SELECT routine_id, rel_path, routine_ordinal, fragment_json "
                    f"FROM bsl_code_module_fragments "
                    f"WHERE project_name = ? AND index_epoch = ? "
                    f"AND rel_path IN ({placeholders}) "
                    f"ORDER BY rel_path, routine_ordinal, routine_id",
                    (scope, int(epoch), *chunk),
                )
                for r in cur.fetchall():
                    keys = r.keys() if hasattr(r, "keys") else []
                    def _get(k, idx):
                        return r[k] if k in keys else r[idx]
                    try:
                        frag = json.loads(_get("fragment_json", 3) or "{}")
                    except Exception:
                        frag = {}
                    yield {
                        "routine_id": _get("routine_id", 0),
                        "rel_path": _get("rel_path", 1),
                        "routine_ordinal": int(_get("routine_ordinal", 2) or 0),
                        "fragment": frag,
                    }

    # ----------------------------------- scoped unit ops (Phase 5A applier)

    def _upsert_idf_stats_in_tx(
        self,
        cur: sqlite3.Cursor,
        scope: str,
        epoch: int,
        idf_increments: Optional[Dict[str, Dict[str, int]]],
        stats_increments: Optional[Dict[str, Tuple[int, int]]],
    ) -> None:
        if idf_increments:
            self._upsert_corpus_idf_in_tx(cur, scope, int(epoch), idf_increments)
        if stats_increments:
            self._upsert_corpus_stats_in_tx(cur, scope, int(epoch), stats_increments)

    def _settle_reverse_counters_in_tx(
        self,
        cur: sqlite3.Cursor,
        scope: str,
        epoch: int,
        idf_reverse: Optional[Dict[str, Dict[str, int]]],
        stats_reverse: Optional[Dict[str, Tuple[int, int]]],
    ) -> None:
        """Постобработка обратного вычитания в ТОЙ ЖЕ транзакции.

        1. Отрицательное значение — признак недоказанного дрейфа: вычли
           больше, чем было добавлено. Тихо продолжать нельзя, поэтому
           `RuntimeError` → ROLLBACK. Исход детерминированный (тот же снимок
           даст тот же underflow), поэтому вызывающий обязан перевести
           операцию в fallback, а не бесконечно ретраить.
        2. Строки `df = 0` удаляются: у токена, который жил только в удаляемой
           конфигурации, эталонный полный rebuild строки не создаёт вовсе.
           Без этого состояние отличалось бы от эталона (поведенчески такие
           строки инертны, но побайтового совпадения нет) и копило мусор.

        Проверяются только затронутые `(field_kind, token)` — стоимость
        пропорциональна удаляемому, а не корпусу.
        """
        if idf_reverse:
            for fk, token_to_df in idf_reverse.items():
                tokens = [t for t, d in token_to_df.items() if t and d]
                for start in range(0, len(tokens), 500):
                    chunk = tokens[start: start + 500]
                    placeholders = ",".join("?" * len(chunk))
                    row = cur.execute(
                        f"SELECT token, df FROM bsl_code_corpus_idf "
                        f"WHERE project_name = ? AND index_epoch = ? "
                        f"  AND field_kind = ? AND df < 0 "
                        f"  AND token IN ({placeholders}) LIMIT 1",
                        (scope, int(epoch), fk, *chunk),
                    ).fetchone()
                    if row is not None:
                        raise BslCodeSqliteError(
                            f"corpus_idf underflow: field_kind={fk!r} "
                            f"token={row[0]!r} df={row[1]}"
                        )
                    cur.execute(
                        f"DELETE FROM bsl_code_corpus_idf "
                        f"WHERE project_name = ? AND index_epoch = ? "
                        f"  AND field_kind = ? AND df = 0 "
                        f"  AND token IN ({placeholders})",
                        (scope, int(epoch), fk, *chunk),
                    )
        if stats_reverse:
            for fk in stats_reverse:
                row = cur.execute(
                    "SELECT doc_count, total_length FROM bsl_code_corpus_stats "
                    "WHERE project_name = ? AND index_epoch = ? AND field_kind = ?",
                    (scope, int(epoch), fk),
                ).fetchone()
                if row is not None and (int(row[0]) < 0 or int(row[1]) < 0):
                    raise BslCodeSqliteError(
                        f"corpus_stats underflow: field_kind={fk!r} "
                        f"doc_count={row[0]} total_length={row[1]}"
                    )

    def _clear_snapshot_in_tx(
        self,
        cur: sqlite3.Cursor,
        scope: str,
        routine_ids: Iterable[str],
    ) -> None:
        ids = list(routine_ids or ())
        if not ids:
            return
        for start in range(0, len(ids), 500):
            chunk = ids[start: start + 500]
            placeholders = ",".join("?" * len(chunk))
            cur.execute(
                f"DELETE FROM bsl_code_pending_reverse_snapshot "
                f"WHERE project_name = ? AND routine_id IN ({placeholders})",
                (scope, *chunk),
            )

    def _set_ledger_stage_in_tx(
        self,
        cur: sqlite3.Cursor,
        scope: str,
        routine_ids: Iterable[str],
        stage: str,
    ) -> None:
        ids = list(routine_ids or ())
        if not ids or not stage:
            return
        now = int(time.time())
        for start in range(0, len(ids), 500):
            chunk = ids[start: start + 500]
            placeholders = ",".join("?" * len(chunk))
            cur.execute(
                f"UPDATE bsl_code_pending_scoped_delta "
                f"SET stage = ?, updated_at = ? "
                f"WHERE project_name = ? AND routine_id IN ({placeholders})",
                (stage, now, scope, *chunk),
            )

    def _insert_units_methods_in_tx(
        self,
        cur: sqlite3.Cursor,
        scope: str,
        epoch: int,
        units: Sequence[Dict[str, Any]],
        methods: Sequence[Dict[str, Any]],
        done_routines: Sequence[Dict[str, Any]],
        module_fragments: Sequence[Dict[str, Any]],
    ) -> None:
        """Insert NEW unit rows / fields / FTS / structural / methods /
        done markers / module fragments emitted by Phase A worker for
        a scoped routine set. Reuses full-pipeline `_write_units_batch_in_tx`."""
        if not units and not methods and not done_routines and not module_fragments:
            return
        if units:
            self._write_units_batch_in_tx(cur, scope, int(epoch), list(units))
        if methods:
            method_rows = [
                (
                    m.get("routine_id"),
                    "",  # module_id — best-effort, как и в full pipeline
                    scope,
                    m.get("config_name") or "",
                    m.get("name") or "",
                    m.get("signature") or "",
                    m.get("routine_type") or "",
                    m.get("symbol_kind") or "",
                    1 if m.get("export") else 0,
                    m.get("owner_qn") or "",
                    m.get("body_hash") or "",
                    int(m.get("size_chars") or 0),
                    int(m.get("size_lines") or 0),
                    int(epoch),
                )
                for m in methods
            ]
            cur.executemany(
                "INSERT OR REPLACE INTO bsl_code_methods("
                "routine_id, module_id, project_name, config_name, name, "
                "signature, routine_type, symbol_kind, export, owner_qn, "
                "body_hash, size_chars, size_lines, index_epoch"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                method_rows,
            )
        if done_routines:
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            done_rows = [
                (
                    scope,
                    int(epoch),
                    d["routine_id"],
                    d.get("body_hash") or "",
                    int(d.get("units_written") or 0),
                    now_iso,
                )
                for d in done_routines
            ]
            cur.executemany(
                "INSERT OR REPLACE INTO bsl_code_phase_a_routine_state("
                "project_name, index_epoch, routine_id, body_hash, "
                "units_written, done_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                done_rows,
            )
        if module_fragments:
            frag_rows = [
                (
                    scope,
                    int(epoch),
                    f["routine_id"],
                    f.get("rel_path") or "",
                    int(f.get("routine_ordinal") or 0),
                    json.dumps(f, ensure_ascii=False, sort_keys=True),
                )
                for f in module_fragments
            ]
            cur.executemany(
                "INSERT INTO bsl_code_module_fragments("
                "project_name, index_epoch, routine_id, rel_path, "
                "routine_ordinal, fragment_json"
                ") VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(project_name, index_epoch, routine_id) DO UPDATE SET "
                "    rel_path = excluded.rel_path, "
                "    routine_ordinal = excluded.routine_ordinal, "
                "    fragment_json = excluded.fragment_json",
                frag_rows,
            )

    def delete_units_by_routine_ids(
        self,
        scope: str,
        epoch: int,
        routine_ids: Iterable[str],
        *,
        idf_reverse: Optional[Dict[str, Dict[str, int]]] = None,
        stats_reverse: Optional[Dict[str, Tuple[int, int]]] = None,
        clear_snapshot_ids: Optional[Iterable[str]] = None,
        set_ledger_stage: Optional[str] = "sqlite_applied",
        config_delete_operation_id: Optional[str] = None,
    ) -> int:
        """Atomic scoped delete: reverse counters + per-routine delete +
        snapshot clear + ledger stage transition. Returns count of routines
        actually processed.

        `config_delete_operation_id` переключает durable-владельца: вместо
        общего ledger/snapshot в той же транзакции продвигаются строки
        config-delete операции. Тот же примитив, тот же инвариант «вычитание
        counters и удаление строк — одна транзакция», разные хранилища
        прогресса."""
        ids = list(routine_ids or ())
        if not ids:
            return 0
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                self._upsert_idf_stats_in_tx(cur, scope, int(epoch), idf_reverse, stats_reverse)
                self._settle_reverse_counters_in_tx(
                    cur, scope, int(epoch), idf_reverse, stats_reverse,
                )
                for rid in ids:
                    self._delete_phase_a_routine_in_tx(cur, scope, int(epoch), rid)
                if config_delete_operation_id:
                    self._set_config_delete_stage_in_tx(
                        cur, scope, config_delete_operation_id, ids, "sqlite_applied",
                    )
                else:
                    self._clear_snapshot_in_tx(cur, scope, clear_snapshot_ids or ids)
                    if set_ledger_stage:
                        self._set_ledger_stage_in_tx(cur, scope, ids, set_ledger_stage)
                cur.execute("COMMIT")
                return len(ids)
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def _set_config_delete_stage_in_tx(
        self,
        cur: sqlite3.Cursor,
        scope: str,
        operation_id: str,
        routine_ids: Iterable[str],
        stage: str,
    ) -> None:
        ids = list(routine_ids or ())
        if not ids:
            return
        for start in range(0, len(ids), 500):
            chunk = ids[start: start + 500]
            placeholders = ",".join("?" * len(chunk))
            cur.execute(
                f"UPDATE bsl_code_config_delete_routines SET stage = ? "
                f"WHERE project_name = ? AND operation_id = ? "
                f"  AND routine_id IN ({placeholders})",
                (stage, scope, operation_id, *chunk),
            )

    # ------------------------------------------------------------------
    # config-scoped purge (удаление расширения)
    # ------------------------------------------------------------------

    def _delete_config_modules_and_fts_in_tx(
        self, cur: sqlite3.Cursor, scope: str, epoch: int, config_name: str,
    ) -> int:
        """Снести модули конфигурации и их module FTS. Возвращает число модулей.

        `rel_path` сравнивается с чужими конфигурациями: у расширения и базы
        пути не пересекаются, но полагаться на это при физическом удалении
        нельзя — FTS базы снесло бы вместе с расширением.

        Общий для legacy purge и residual-стадии scoped-удаления. Второй
        обязан вызывать именно это: scoped apply пересобирает module FTS по
        затронутым путям и вставляет ПУСТУЮ строку для пути, у которого не
        осталось ни одного фрагмента, — её убирает этот же helper.
        """
        own_rel_paths = {
            r[0] for r in cur.execute(
                "SELECT DISTINCT rel_path FROM bsl_code_modules "
                "WHERE project_name = ? AND config_name = ? AND index_epoch = ?",
                (scope, config_name, int(epoch)),
            ).fetchall() if r[0]
        }
        foreign_rel_paths = {
            r[0] for r in cur.execute(
                "SELECT DISTINCT rel_path FROM bsl_code_modules "
                "WHERE project_name = ? AND config_name != ? AND index_epoch = ?",
                (scope, config_name, int(epoch)),
            ).fetchall() if r[0]
        }
        for rel_path in sorted(own_rel_paths - foreign_rel_paths):
            cur.execute(
                f"DELETE FROM {MODULE_FTS_TABLE} "
                "WHERE rowid IN ("
                "  SELECT module_rowid FROM bsl_code_module_fts_rows "
                "  WHERE project_name = ? AND index_epoch = ? AND rel_path = ?"
                ")",
                (scope, int(epoch), rel_path),
            )
            cur.execute(
                "DELETE FROM bsl_code_module_fts_rows "
                "WHERE project_name = ? AND index_epoch = ? AND rel_path = ?",
                (scope, int(epoch), rel_path),
            )
        cur.execute(
            "DELETE FROM bsl_code_modules "
            "WHERE project_name = ? AND config_name = ? AND index_epoch = ?",
            (scope, config_name, int(epoch)),
        )
        return max(0, cur.rowcount or 0)

    def routine_ids_for_config(
        self, scope: str, epoch: int, config_name: str
    ) -> List[str]:
        """Routine IDs указанной конфигурации в указанной эпохе.

        Опирается на существующий индекс (project_name, config_name, index_epoch).
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT routine_id FROM bsl_code_units "
                "WHERE project_name = ? AND config_name = ? AND index_epoch = ?",
                (scope, config_name, int(epoch)),
            ).fetchall()
        return [r[0] for r in rows]

    def purge_config_and_request_reindex(
        self, scope: str, epoch: int, config_name: str
    ) -> Dict[str, int]:
        """Снести из serving epoch все строки конфигурации и, если что-то
        реально удалено, в ТОЙ ЖЕ транзакции запросить полную перестройку.

        Атомарность обязательна: corpus IDF/stats намеренно не откатываются
        (их пересчитает rebuild), поэтому обязательство перестроить индекс —
        единственный сигнал восстановления. Разрыв purge и `request_reindex`
        на две транзакции создавал бы окно, после падения в котором повтор
        получил бы `deleted == 0`, флаг бы не поставил и продвинул стадию с
        устаревшими статистиками.

        `reindex_requested` НЕ выставляется при нулевом удалении: идемпотентный
        повтор не должен провоцировать бесполезную полную перестройку.

        Возвращает счётчики удалённых строк по группам.
        """
        epoch = int(epoch)
        deleted: Dict[str, int] = {}
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                routine_ids = [
                    r[0] for r in cur.execute(
                        "SELECT DISTINCT routine_id FROM bsl_code_units "
                        "WHERE project_name = ? AND config_name = ? AND index_epoch = ?",
                        (scope, config_name, epoch),
                    ).fetchall()
                ]
                unit_ids = [
                    r[0] for r in cur.execute(
                        "SELECT unit_id FROM bsl_code_units "
                        "WHERE project_name = ? AND config_name = ? AND index_epoch = ?",
                        (scope, config_name, epoch),
                    ).fetchall()
                ]

                # 1. Per-routine удаление: units + contentless units_fts по rowid +
                # unit_fields + structural FTS + methods + module_fragments.
                for rid in routine_ids:
                    self._delete_phase_a_routine_in_tx(cur, scope, epoch, rid)
                deleted["routines"] = len(routine_ids)
                deleted["units"] = len(unit_ids)

                # 2. Phase B markers удалённых юнитов — по всем vector-эпохам:
                # сами юниты исчезли, маркер про них смысла не имеет.
                for start in range(0, len(unit_ids), 500):
                    chunk = unit_ids[start: start + 500]
                    placeholders = ",".join("?" * len(chunk))
                    cur.execute(
                        f"DELETE FROM bsl_code_phase_b_unit_state "
                        f"WHERE project_name = ? AND unit_id IN ({placeholders})",
                        (scope, *chunk),
                    )

                # 3. Phase A progress rows.
                for start in range(0, len(routine_ids), 500):
                    chunk = routine_ids[start: start + 500]
                    placeholders = ",".join("?" * len(chunk))
                    cur.execute(
                        f"DELETE FROM bsl_code_phase_a_routine_state "
                        f"WHERE project_name = ? AND index_epoch = ? "
                        f"  AND routine_id IN ({placeholders})",
                        (scope, epoch, *chunk),
                    )

                # 4. Модули конфигурации и их module FTS.
                deleted["modules"] = self._delete_config_modules_and_fts_in_tx(
                    cur, scope, epoch, config_name,
                )

                # Defensive: methods могут остаться, если routine_id пропал из
                # units, но строка метода уцелела от старой эпохи записи.
                cur.execute(
                    "DELETE FROM bsl_code_methods "
                    "WHERE project_name = ? AND config_name = ? AND index_epoch = ?",
                    (scope, config_name, epoch),
                )
                deleted["methods_residual"] = max(0, cur.rowcount or 0)

                # 5. Verification в той же транзакции: остаток → rollback.
                for table in ("bsl_code_units", "bsl_code_methods", "bsl_code_modules"):
                    left = cur.execute(
                        f"SELECT count(*) FROM {table} "
                        "WHERE project_name = ? AND config_name = ? AND index_epoch = ?",
                        (scope, config_name, epoch),
                    ).fetchone()[0]
                    if left:
                        raise RuntimeError(
                            f"purge_config: {left} row(s) of config {config_name!r} "
                            f"remain in {table} after purge"
                        )

                total = sum(deleted.values())
                if total > 0:
                    self._request_reindex_in_tx(cur, scope)
                    deleted["reindex_requested"] = 1
                cur.execute("COMMIT")
                return deleted
            except Exception:
                cur.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------
    # config delete: durable-подготовка обратного вычитания
    # ------------------------------------------------------------------

    def list_config_serving_routines(
        self, scope: str, epoch: int, config_name: str,
    ) -> Dict[str, Dict[str, Any]]:
        """Serving-множество конфигурации вместе с authoritative-разбиением.

        Возвращает {routine_id: {..., "units": [UnitRange-подобные dict-ы]}}.

        Это единственный источник, по которому считается обратный вклад:
        `bsl_code_units` хранит и build-time контекст (`routine_name`,
        `owner_qn`, `module_type`, `module_kind`, `rel_path`, `body_hash`), и
        границы окон (`char_start/char_end/line_start/line_end/part_index/
        part_total`). Пересчитывать разбиение через `split_routine` нельзя: оно
        зависит от доступности tree-sitter/SDBL в момент вызова, а она не
        входит в config fingerprint.

        `bsl_code_phase_a_routine_state.body_hash` добавляется как
        дополнительная сверка ТОЛЬКО там, где строка существует: у
        закоммиченной эпохи `commit_pending` эти строки удаляет, поэтому их
        отсутствие не является признаком дрейфа.
        """
        epoch = int(epoch)
        out: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            rows = self._conn.execute(
                self._SERVING_UNIT_COLUMNS
                + "WHERE project_name = ? AND config_name = ? AND index_epoch = ? "
                + "ORDER BY routine_id, part_index",
                (scope, config_name, epoch),
            ).fetchall()
            progress = {
                r["routine_id"]: r["body_hash"]
                for r in self._conn.execute(
                    "SELECT routine_id, body_hash FROM bsl_code_phase_a_routine_state "
                    "WHERE project_name = ? AND index_epoch = ?",
                    (scope, epoch),
                ).fetchall()
            }
        self._assemble_serving_entries(rows, progress, out)
        return out

    # Колонки serving-разбиения. Один список на оба публичных чтения, чтобы
    # формат entry не разъехался между ними.
    _SERVING_UNIT_COLUMNS = (
        "SELECT routine_id, routine_name, owner_qn, module_type, "
        "       module_kind, rel_path, body_hash, unit_id, "
        "       char_start, char_end, line_start, line_end, "
        "       part_index, part_total "
        "FROM bsl_code_units "
    )

    @staticmethod
    def _assemble_serving_entries(
        rows: Iterable[Any],
        progress: Dict[str, Any],
        out: Dict[str, Dict[str, Any]],
    ) -> None:
        """Сборка строк `bsl_code_units` в entry-структуру serving-разбиения."""
        for r in rows:
            rid = r["routine_id"]
            entry = out.get(rid)
            if entry is None:
                entry = {
                    "routine_id": rid,
                    "routine_name": r["routine_name"] or "",
                    "owner_qn": r["owner_qn"] or "",
                    "module_type": r["module_type"] or "",
                    "module_kind": r["module_kind"] or "",
                    "rel_path": r["rel_path"] or "",
                    "body_hash": r["body_hash"] or "",
                    "progress_body_hash": progress.get(rid),
                    "units": [],
                }
                out[rid] = entry
            entry["units"].append({
                "unit_id": r["unit_id"],
                "char_start": int(r["char_start"] or 0),
                "char_end": int(r["char_end"] or 0),
                "line_start": int(r["line_start"] or 0),
                "line_end": int(r["line_end"] or 0),
                "part_index": int(r["part_index"] or 0),
                "part_total": int(r["part_total"] or 0),
            })

    def list_serving_routines_by_ids(
        self, scope: str, epoch: int, routine_ids: Iterable[str],
    ) -> Dict[str, Dict[str, Any]]:
        """То же serving-разбиение, что `list_config_serving_routines`, но
        отобранное по множеству `routine_id`, а не по конфигурации.

        Нужно scoped-снапшоту шага 4.5: обратный вклад изменившихся/удалённых
        routine обязан считаться по ТЕМ ЖЕ границам, по которым он добавлялся,
        а `split_routine` пересчитать их не может — он не является чистой
        функцией от (body, strategy).

        Отсутствие routine в результате — законный случай (тело было пустым или
        split не удался), он означает нулевой вклад, а не повреждение.
        """
        epoch = int(epoch)
        ids = [str(rid) for rid in routine_ids if rid]
        out: Dict[str, Dict[str, Any]] = {}
        if not ids:
            return out
        # Чанкование под лимит переменных SQLite (SQLITE_MAX_VARIABLE_NUMBER);
        # два параметра занимает scope+epoch.
        chunk_size = 400
        with self._lock:
            for start in range(0, len(ids), chunk_size):
                chunk = ids[start: start + chunk_size]
                placeholders = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    self._SERVING_UNIT_COLUMNS
                    + "WHERE project_name = ? AND index_epoch = ? "
                    + f"  AND routine_id IN ({placeholders}) "
                    + "ORDER BY routine_id, part_index",
                    (scope, epoch, *chunk),
                ).fetchall()
                progress = {
                    r["routine_id"]: r["body_hash"]
                    for r in self._conn.execute(
                        "SELECT routine_id, body_hash "
                        "FROM bsl_code_phase_a_routine_state "
                        "WHERE project_name = ? AND index_epoch = ? "
                        f"  AND routine_id IN ({placeholders})",
                        (scope, epoch, *chunk),
                    ).fetchall()
                }
                self._assemble_serving_entries(rows, progress, out)
        return out

    def config_has_serving_rows(
        self, scope: str, epoch: int, config_name: str,
    ) -> bool:
        """Есть ли у конфигурации хоть что-то в serving epoch (units/methods/modules)."""
        epoch = int(epoch)
        with self._lock:
            for table in ("bsl_code_units", "bsl_code_methods", "bsl_code_modules"):
                row = self._conn.execute(
                    f"SELECT 1 FROM {table} "
                    "WHERE project_name = ? AND config_name = ? AND index_epoch = ? "
                    "LIMIT 1",
                    (scope, config_name, epoch),
                ).fetchone()
                if row is not None:
                    return True
        return False

    def routine_ids_for_config_methods(
        self, scope: str, epoch: int, config_name: str,
    ) -> List[str]:
        """Defensive-дополнение к serving-множеству: routine_id из methods.

        Строка метода может пережить исчезновение units (см. `methods_residual`
        в legacy purge), и такой routine обязан попасть в множество операции,
        иначе residual оставит его строки.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT routine_id FROM bsl_code_methods "
                "WHERE project_name = ? AND config_name = ? AND index_epoch = ?",
                (scope, config_name, int(epoch)),
            ).fetchall()
        return [r[0] for r in rows if r[0]]

    def write_config_delete_preparation(
        self,
        scope: str,
        operation_id: str,
        *,
        config_name: str,
        index_epoch: int,
        vector_epoch: Optional[int],
        fingerprint: str,
        rel_paths: Iterable[str],
        routines: Sequence[Dict[str, Any]],
        graph_count: int,
    ) -> None:
        """Атомарно записать подготовку и поднять reader gate.

        Одна транзакция на всё: строки операции + gate. Иначе существовало бы
        состояние «снимок есть, а поиск ещё показывает удаляемое расширение»
        или наоборот.

        Idempotent replay того же `operation_id` разрешён, пока ни одна строка
        не перешла в `sqlite_applied`: после первого применённого чанка
        перезапись снимка означала бы потерю уже вычтенного вклада, поэтому
        такая попытка — ошибка, а не «подготовим заново».
        """
        rel_paths_list = sorted({p for p in (rel_paths or ()) if p})
        routine_ids = sorted({r["routine_id"] for r in routines})
        now = int(time.time())
        serving_count = sum(1 for r in routines if r.get("in_serving"))
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                applied = cur.execute(
                    "SELECT count(*) FROM bsl_code_config_delete_routines "
                    "WHERE project_name = ? AND operation_id = ? AND stage = ?",
                    (scope, operation_id, "sqlite_applied"),
                ).fetchone()[0]
                if applied:
                    raise BslCodeSqliteError(
                        f"config delete {operation_id!r}: {applied} routine(s) "
                        f"already applied; preparation cannot be rewritten"
                    )
                cur.execute(
                    "DELETE FROM bsl_code_config_delete_routines "
                    "WHERE project_name = ? AND operation_id = ?",
                    (scope, operation_id),
                )
                cur.execute(
                    "DELETE FROM bsl_code_config_delete_ops "
                    "WHERE project_name = ? AND operation_id = ?",
                    (scope, operation_id),
                )
                cur.execute(
                    "INSERT INTO bsl_code_config_delete_ops("
                    "project_name, operation_id, config_name, index_epoch, "
                    "vector_epoch, fingerprint, rel_paths_json, serving_count, "
                    "graph_count, state, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        scope, operation_id, config_name, int(index_epoch),
                        None if vector_epoch is None else int(vector_epoch),
                        fingerprint or "",
                        json.dumps(rel_paths_list, ensure_ascii=False),
                        serving_count, int(graph_count), "prepared", now, now,
                    ),
                )
                cur.executemany(
                    "INSERT INTO bsl_code_config_delete_routines("
                    "project_name, operation_id, routine_id, rel_path, "
                    "in_serving, idf_json, stats_json, stage"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            scope, operation_id, r["routine_id"],
                            r.get("rel_path") or "",
                            1 if r.get("in_serving") else 0,
                            r.get("idf_json") or "{}",
                            r.get("stats_json") or "{}",
                            "snapshot_written",
                        )
                        for r in routines
                    ],
                )
                # Reader gate — в той же транзакции. Расширение перестаёт быть
                # видимым до удаления графа, поэтому окна «граф пуст, SQLite
                # ещё нет» не существует.
                self._set_scoped_gate_in_tx(
                    cur, scope, True,
                    routine_ids=routine_ids, rel_paths=rel_paths_list,
                    also_set_scoped_retry_pending=False,
                    visibility_flip_done=True,
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def read_config_delete_operation(
        self, scope: str, operation_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Header + агрегаты строк одним чтением — вход валидации `ADOPTED`."""
        with self._lock:
            row = self._conn.execute(
                "SELECT operation_id, config_name, index_epoch, vector_epoch, "
                "       fingerprint, rel_paths_json, serving_count, graph_count, "
                "       state, fallback_reason, created_at, updated_at "
                "FROM bsl_code_config_delete_ops "
                "WHERE project_name = ? AND operation_id = ?",
                (scope, operation_id),
            ).fetchone()
            if row is None:
                return None
            routine_rows = self._conn.execute(
                "SELECT routine_id, rel_path, in_serving, stage "
                "FROM bsl_code_config_delete_routines "
                "WHERE project_name = ? AND operation_id = ?",
                (scope, operation_id),
            ).fetchall()
        try:
            rel_paths = set(json.loads(row["rel_paths_json"] or "[]") or [])
        except Exception:
            rel_paths = set()
        stages: Dict[str, int] = {}
        routine_ids: Set[str] = set()
        pending_ids: Set[str] = set()
        for r in routine_rows:
            stages[r["stage"]] = stages.get(r["stage"], 0) + 1
            routine_ids.add(r["routine_id"])
            if r["stage"] != "sqlite_applied" and int(r["in_serving"] or 0):
                pending_ids.add(r["routine_id"])
        return {
            "operation_id": row["operation_id"],
            "config_name": row["config_name"],
            "index_epoch": int(row["index_epoch"]),
            "vector_epoch": row["vector_epoch"],
            "fingerprint": row["fingerprint"] or "",
            "rel_paths": rel_paths,
            "serving_count": int(row["serving_count"] or 0),
            "graph_count": int(row["graph_count"] or 0),
            "state": row["state"],
            "fallback_reason": row["fallback_reason"] or "",
            "routine_ids": routine_ids,
            "pending_serving_ids": pending_ids,
            "stage_counts": stages,
        }

    def find_active_config_delete_operations(self, scope: str) -> List[Dict[str, Any]]:
        """Операции, которые ещё владеют serving-состоянием проекта.

        `fallback_rebuild_required` сюда НЕ входит: такая операция уже передала
        владение восстановлением полному rebuild и не должна его блокировать.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT operation_id, config_name, state FROM bsl_code_config_delete_ops "
                "WHERE project_name = ? AND state IN ('prepared', 'committed') "
                "ORDER BY created_at",
                (scope,),
            ).fetchall()
        return [
            {
                "operation_id": r["operation_id"],
                "config_name": r["config_name"],
                "state": r["state"],
            }
            for r in rows
        ]

    def read_config_delete_snapshot(
        self, scope: str, operation_id: str, routine_ids: Iterable[str],
    ) -> Dict[str, Dict[str, Any]]:
        """{routine_id: {"idf": ..., "stats": ...}} — форма `_invert_snapshot`."""
        ids = list(routine_ids or ())
        if not ids:
            return {}
        rows = []
        with self._lock:
            for start in range(0, len(ids), 500):
                chunk = ids[start: start + 500]
                placeholders = ",".join("?" * len(chunk))
                rows.extend(self._conn.execute(
                    f"SELECT routine_id, idf_json, stats_json "
                    f"FROM bsl_code_config_delete_routines "
                    f"WHERE project_name = ? AND operation_id = ? "
                    f"  AND routine_id IN ({placeholders})",
                    (scope, operation_id, *chunk),
                ).fetchall())
        result: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            try:
                idf = json.loads(r["idf_json"] or "{}")
            except Exception:
                idf = {}
            try:
                stats_raw = json.loads(r["stats_json"] or "{}")
            except Exception:
                stats_raw = {}
            stats = {
                fk: tuple(v) if isinstance(v, list) else v
                for fk, v in stats_raw.items()
            }
            result[r["routine_id"]] = {"idf": idf, "stats": stats}
        return result

    def mark_config_delete_state(
        self,
        scope: str,
        operation_id: str,
        state: str,
        *,
        fallback_reason: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE bsl_code_config_delete_ops "
                "SET state = ?, fallback_reason = ?, updated_at = ? "
                "WHERE project_name = ? AND operation_id = ?",
                (state, fallback_reason, int(time.time()), scope, operation_id),
            )

    def mark_config_delete_fallback_rebuild(
        self, scope: str, operation_id: str, reason: str,
    ) -> None:
        """Передать восстановление полному rebuild — ОДНОЙ транзакцией.

        Два отдельных autocommit-запроса здесь недопустимы: при частичном
        успехе операция уже перестала давать `CONFIG_DELETE_IN_PROGRESS`
        (значит, никто её не ждёт как активную), а обязательства перестроить
        индекс ещё нет. Получается состояние без выхода — readiness
        `SCOPED_RETRY` с поднятым gate, пустой ledger и epoch, которую сменить
        уже некому.

        Gate намеренно НЕ снимается: строки расширения ещё лежат в старой
        epoch, и поиск не должен их показывать до публикации новой.
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                cur.execute(
                    "UPDATE bsl_code_config_delete_ops "
                    "SET state = 'fallback_rebuild_required', fallback_reason = ?, "
                    "    updated_at = ? "
                    "WHERE project_name = ? AND operation_id = ?",
                    (reason, int(time.time()), scope, operation_id),
                )
                self._request_reindex_in_tx(cur, scope)
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def cancel_config_delete_operation(
        self, scope: str, operation_id: str, *, release_gate: bool = True,
    ) -> None:
        """Удалить операцию и (по умолчанию) снять gate — одной транзакцией.

        Отказ, если хоть одна строка уже `sqlite_applied`: после начала
        вычитания отменять нечего — вклад назад не вернуть, и такое состояние
        обязано идти по liveness-веткам, а не по отмене.

        `release_gate=False` — для случая, когда gate обязан остаться поднятым
        до публикации новой serving epoch (передача восстановления полному
        rebuild).
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                applied = cur.execute(
                    "SELECT count(*) FROM bsl_code_config_delete_routines "
                    "WHERE project_name = ? AND operation_id = ? AND stage = ?",
                    (scope, operation_id, "sqlite_applied"),
                ).fetchone()[0]
                if applied:
                    raise BslCodeSqliteError(
                        f"config delete {operation_id!r}: cannot cancel, "
                        f"{applied} routine(s) already applied"
                    )
                cur.execute(
                    "DELETE FROM bsl_code_config_delete_routines "
                    "WHERE project_name = ? AND operation_id = ?",
                    (scope, operation_id),
                )
                cur.execute(
                    "DELETE FROM bsl_code_config_delete_ops "
                    "WHERE project_name = ? AND operation_id = ?",
                    (scope, operation_id),
                )
                if release_gate:
                    self._set_scoped_gate_in_tx(cur, scope, False)
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def drop_config_delete_operation(
        self, scope: str, operation_id: str, *, release_gate: bool = True,
    ) -> None:
        """Безусловно снять операцию — финализация или obsolete-после-rebuild.

        В отличие от `cancel_config_delete_operation`, не проверяет стадии:
        вызывается, когда вычитание либо уже полностью учтено, либо потеряло
        смысл вместе со старой epoch.
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                cur.execute(
                    "DELETE FROM bsl_code_config_delete_routines "
                    "WHERE project_name = ? AND operation_id = ?",
                    (scope, operation_id),
                )
                cur.execute(
                    "DELETE FROM bsl_code_config_delete_ops "
                    "WHERE project_name = ? AND operation_id = ?",
                    (scope, operation_id),
                )
                if release_gate:
                    self._set_scoped_gate_in_tx(cur, scope, False)
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def replace_units_for_routines(
        self,
        scope: str,
        epoch: int,
        routine_ids: Iterable[str],
        *,
        units: Sequence[Dict[str, Any]],
        methods: Sequence[Dict[str, Any]],
        done_routines: Sequence[Dict[str, Any]],
        module_fragments: Sequence[Dict[str, Any]],
        idf_increments: Optional[Dict[str, Dict[str, int]]] = None,
        stats_increments: Optional[Dict[str, Tuple[int, int]]] = None,
        idf_reverse: Optional[Dict[str, Dict[str, int]]] = None,
        stats_reverse: Optional[Dict[str, Tuple[int, int]]] = None,
        clear_snapshot_ids: Optional[Iterable[str]] = None,
        set_ledger_stage: Optional[str] = "sqlite_applied",
    ) -> None:
        """Atomic scoped replace in a single SQLite tx (see §11/§13 plan):
        1. reverse upsert IDF/stats
        2. per-routine delete (units + fields + FTS + structural + methods +
           phase_a state + module_fragments)
        3. insert new units + methods + done markers + module_fragments
        4. positive upsert IDF/stats
        5. clear snapshot rows for clear_snapshot_ids (default: routine_ids)
        6. ledger stage transition (default: 'sqlite_applied')
        """
        ids = list(routine_ids or ())
        if not ids:
            return
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                self._upsert_idf_stats_in_tx(cur, scope, int(epoch), idf_reverse, stats_reverse)
                for rid in ids:
                    self._delete_phase_a_routine_in_tx(cur, scope, int(epoch), rid)
                self._insert_units_methods_in_tx(
                    cur, scope, int(epoch), units, methods, done_routines, module_fragments,
                )
                self._upsert_idf_stats_in_tx(cur, scope, int(epoch), idf_increments, stats_increments)
                # Итоговое состояние затронутых токенов: отрицательных быть не
                # должно, нулевые — удалить. Проверяется ПОСЛЕ положительного
                # upsert: внутри транзакции «минус, потом плюс» — норма.
                self._settle_reverse_counters_in_tx(
                    cur, scope, int(epoch), idf_reverse, stats_reverse,
                )
                self._clear_snapshot_in_tx(cur, scope, clear_snapshot_ids or ids)
                if set_ledger_stage:
                    self._set_ledger_stage_in_tx(cur, scope, ids, set_ledger_stage)
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def update_unit_metadata_for_routines(
        self,
        scope: str,
        epoch: int,
        rows: Sequence[Dict[str, Any]],
        *,
        set_ledger_stage: Optional[str] = "sqlite_applied",
    ) -> int:
        """Safe metadata-only UPDATE for line_only routines: only updates
        rel_path / config_name / owner_qn / owner_category / module_type /
        routine_type / export. Does NOT touch text_for_fts / FTS /
        structural / IDF / stats / fragments. line_start/line_end are
        excluded because per-unit ranges cannot be reconstructed from a
        single Routine.line (a large routine has different ranges per unit);
        line ranges stay stale until the next body/signature change."""
        if not rows:
            return 0
        total = 0
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                for r in rows:
                    rid = r.get("routine_id")
                    if not rid:
                        continue
                    # NOTE: line_start/line_end are intentionally NOT updated
                    # here — per-unit ranges cannot be reconstructed from a
                    # single Routine.line. Stale ranges remain until the
                    # next body/signature change triggers a full rebuild.
                    cur.execute(
                        "UPDATE bsl_code_units SET "
                        "    rel_path = ?, "
                        "    config_name = ?, "
                        "    owner_qn = ?, "
                        "    owner_category = ?, "
                        "    module_type = ?, "
                        "    routine_type = ?, "
                        "    export = ? "
                        "WHERE project_name = ? AND index_epoch = ? "
                        "AND routine_id = ?",
                        (
                            r.get("rel_path") or "",
                            r.get("config_name") or "",
                            r.get("owner_qn") or "",
                            r.get("owner_category") or "",
                            r.get("module_type") or "",
                            r.get("routine_type") or "",
                            1 if r.get("export") else 0,
                            scope, int(epoch), rid,
                        ),
                    )
                    if cur.rowcount:
                        total += cur.rowcount
                    cur.execute(
                        "UPDATE bsl_code_methods SET "
                        "    config_name = ?, "
                        "    routine_type = ?, "
                        "    export = ?, "
                        "    owner_qn = ? "
                        "WHERE project_name = ? AND index_epoch = ? "
                        "AND routine_id = ?",
                        (
                            r.get("config_name") or "",
                            r.get("routine_type") or "",
                            1 if r.get("export") else 0,
                            r.get("owner_qn") or "",
                            scope, int(epoch), rid,
                        ),
                    )
                if set_ledger_stage:
                    self._set_ledger_stage_in_tx(
                        cur, scope, [r["routine_id"] for r in rows if r.get("routine_id")],
                        set_ledger_stage,
                    )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        return total

    def replace_module_fts_for_rel_paths(
        self,
        scope: str,
        epoch: int,
        columns_by_rel_path: Dict[str, Dict[str, str]],
    ) -> int:
        """Atomic DELETE + INSERT module FTS row per rel_path. Used by
        scoped module FTS rebuild (§8)."""
        if not columns_by_rel_path:
            return 0
        written = 0
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                for rel_path, columns in columns_by_rel_path.items():
                    # find existing fts_rowid for this rel_path
                    old_rows = cur.execute(
                        "SELECT module_rowid FROM bsl_code_module_fts_rows "
                        "WHERE project_name = ? AND index_epoch = ? "
                        "AND rel_path = ?",
                        (scope, int(epoch), rel_path),
                    ).fetchall()
                    old_rowids = [int(r[0]) for r in old_rows]
                    for start in range(0, len(old_rowids), 500):
                        chunk = old_rowids[start: start + 500]
                        placeholders = ",".join("?" * len(chunk))
                        cur.execute(
                            f"DELETE FROM {MODULE_FTS_TABLE} "
                            f"WHERE rowid IN ({placeholders})",
                            chunk,
                        )
                        cur.execute(
                            f"DELETE FROM bsl_code_module_fts_rows "
                            f"WHERE module_rowid IN ({placeholders})",
                            chunk,
                        )
                    self._insert_module_fts_in_tx(
                        cur, scope, int(epoch), rel_path, columns,
                    )
                    written += 1
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        return written

    # --------------------------------------------- scoped Phase B helpers

    def iter_phase_b_units_for_routine_ids(
        self,
        scope: str,
        epoch: int,
        vector_epoch: int,
        routine_ids: Iterable[str],
        *,
        batch_size: int = 200,
        filters: Optional[Dict[str, Any]] = None,
        worker_id: int = 0,
        total_workers: int = 1,
    ) -> Iterable[List[Dict[str, Any]]]:
        """Stream unit rows for scoped Phase B, filtered by routine_id set
        and by the same exclusion filters as the full Phase B (owner_categories,
        regulated reports). Already-done units (per bsl_code_phase_b_unit_state)
        are skipped. Yields lists of size up to `batch_size`.

        When `total_workers > 1`, only rows with `(u.fts_rowid % total_workers)
        = worker_id` are yielded — the same partitioning strategy as full
        Phase B's `iter_phase_b_not_done_units`. Uses keyset pagination over
        `fts_rowid` within each chunk of routine_ids so per-worker RAM stays
        bounded by `batch_size`."""
        ids = list(routine_ids or ())
        if not ids:
            return
        f = filters or {}
        excl_cats = list(f.get("excluded_owner_categories") or [])
        excl_regulated = bool(f.get("exclude_regulated_reports"))
        size = max(1, int(batch_size))
        total_w = max(1, int(total_workers))
        for start in range(0, len(ids), 500):
            chunk = ids[start: start + 500]
            id_placeholders = ",".join("?" * len(chunk))
            where_extra: List[str] = []
            extra_params: List[Any] = []
            if excl_cats:
                cat_placeholders = ",".join("?" * len(excl_cats))
                where_extra.append(
                    f"COALESCE(u.owner_category, '') NOT IN ({cat_placeholders})"
                )
                extra_params.extend(excl_cats)
            if excl_regulated:
                where_extra.append("COALESCE(u.is_regulated_report, 0) = 0")
            extra_sql = (" AND " + " AND ".join(where_extra)) if where_extra else ""
            last_rowid = 0
            while True:
                params: List[Any] = [
                    scope, int(epoch),
                    scope, int(vector_epoch),
                    *chunk,
                    int(last_rowid),
                    total_w, int(worker_id),
                    *extra_params,
                    size,
                ]
                sql = (
                    f"SELECT u.* FROM bsl_code_units AS u "
                    f"WHERE u.project_name = ? AND u.index_epoch = ? "
                    f"AND NOT EXISTS ("
                    f"  SELECT 1 FROM bsl_code_phase_b_unit_state AS s "
                    f"  WHERE s.project_name = ? AND s.vector_epoch = ? "
                    f"  AND s.unit_id = u.unit_id"
                    f") "
                    f"AND u.routine_id IN ({id_placeholders}) "
                    f"AND u.fts_rowid > ? "
                    f"AND (u.fts_rowid % ?) = ?"
                    f"{extra_sql} "
                    f"ORDER BY u.fts_rowid LIMIT ?"
                )
                with self._lock:
                    rows = self._conn.execute(sql, params).fetchall()
                if not rows:
                    break
                batch: List[Dict[str, Any]] = []
                for r in rows:
                    keys = r.keys() if hasattr(r, "keys") else []
                    rec: Dict[str, Any] = {}
                    for k in keys:
                        rec[k] = r[k]
                    batch.append(rec)
                yield batch
                last_rowid = int(rows[-1]["fts_rowid"])

    def count_phase_b_units_for_routine_ids(
        self,
        scope: str,
        epoch: int,
        vector_epoch: int,
        routine_ids: Iterable[str],
        *,
        filters: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Count units pending scoped Phase B for the given routine_ids,
        matching the iterator's filter set (excluded_owner_categories,
        exclude_regulated_reports, phase_b done markers). Used by the scoped
        Phase B coordinator to seed `_PhaseBProgress.total`."""
        ids = list(routine_ids or ())
        if not ids:
            return 0
        f = filters or {}
        excl_cats = list(f.get("excluded_owner_categories") or [])
        excl_regulated = bool(f.get("exclude_regulated_reports"))
        total = 0
        for start in range(0, len(ids), 500):
            chunk = ids[start: start + 500]
            id_placeholders = ",".join("?" * len(chunk))
            where_extra: List[str] = []
            extra_params: List[Any] = []
            if excl_cats:
                cat_placeholders = ",".join("?" * len(excl_cats))
                where_extra.append(
                    f"COALESCE(u.owner_category, '') NOT IN ({cat_placeholders})"
                )
                extra_params.extend(excl_cats)
            if excl_regulated:
                where_extra.append("COALESCE(u.is_regulated_report, 0) = 0")
            extra_sql = (" AND " + " AND ".join(where_extra)) if where_extra else ""
            sql = (
                f"SELECT COUNT(*) AS cnt FROM bsl_code_units AS u "
                f"WHERE u.project_name = ? AND u.index_epoch = ? "
                f"AND NOT EXISTS ("
                f"  SELECT 1 FROM bsl_code_phase_b_unit_state AS s "
                f"  WHERE s.project_name = ? AND s.vector_epoch = ? "
                f"  AND s.unit_id = u.unit_id"
                f") "
                f"AND u.routine_id IN ({id_placeholders}){extra_sql}"
            )
            params: List[Any] = [
                scope, int(epoch),
                scope, int(vector_epoch),
                *chunk,
                *extra_params,
            ]
            with self._lock:
                row = self._conn.execute(sql, params).fetchone()
            total += int(row["cnt"] if row else 0)
        return total

    def delete_phase_b_state_by_routine_ids(
        self,
        scope: str,
        vector_epoch: Optional[int],
        routine_ids: Iterable[str],
    ) -> int:
        """`vector_epoch=None` — по ВСЕМ vector-эпохам.

        Нужно физическому удалению конфигурации: routines исчезают навсегда, и
        маркер про них не имеет смысла ни в одной эпохе, а перебирать эпохи
        снаружи означало бы знать их список.
        """
        ids = list(routine_ids or ())
        if not ids:
            return 0
        total = 0
        with self._lock:
            cur = self._conn.cursor()
            for start in range(0, len(ids), 500):
                chunk = ids[start: start + 500]
                placeholders = ",".join("?" * len(chunk))
                if vector_epoch is None:
                    cur.execute(
                        f"DELETE FROM bsl_code_phase_b_unit_state "
                        f"WHERE project_name = ? "
                        f"AND routine_id IN ({placeholders})",
                        (scope, *chunk),
                    )
                else:
                    cur.execute(
                        f"DELETE FROM bsl_code_phase_b_unit_state "
                        f"WHERE project_name = ? AND vector_epoch = ? "
                        f"AND routine_id IN ({placeholders})",
                        (scope, int(vector_epoch), *chunk),
                    )
                total += cur.rowcount or 0
        return total

    def finalize_config_delete_residuals(
        self,
        scope: str,
        epoch: int,
        config_name: str,
        routine_ids: Iterable[str],
        *,
        operation_id: Optional[str] = None,
        drop_operation: bool = True,
    ) -> Dict[str, int]:
        """Idempotent housekeeping после успешного вычитания.

        НИКОГДА не трогает `bsl_code_corpus_idf`, `bsl_code_corpus_stats`,
        `reindex_requested` и serving epoch — в этом весь смысл отдельной
        стадии: падение после вычитания не должно приводить ко второму
        вычитанию, поэтому повторяется только housekeeping.

        Что снимается (то, чего не делает `_delete_phase_a_routine_in_tx`):
        Phase A progress rows, Phase B markers во всех vector-эпохах, модули
        конфигурации вместе с module FTS (включая ПУСТУЮ строку, которую
        оставляет пересборка FTS для пути без фрагментов), residual methods.
        """
        epoch = int(epoch)
        ids = sorted(set(routine_ids or ()))
        stats: Dict[str, int] = {}
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                removed_phase_a = 0
                for start in range(0, len(ids), 500):
                    chunk = ids[start: start + 500]
                    placeholders = ",".join("?" * len(chunk))
                    cur.execute(
                        f"DELETE FROM bsl_code_phase_a_routine_state "
                        f"WHERE project_name = ? AND index_epoch = ? "
                        f"  AND routine_id IN ({placeholders})",
                        (scope, epoch, *chunk),
                    )
                    removed_phase_a += cur.rowcount or 0
                stats["phase_a_state"] = removed_phase_a

                removed_phase_b = 0
                for start in range(0, len(ids), 500):
                    chunk = ids[start: start + 500]
                    placeholders = ",".join("?" * len(chunk))
                    cur.execute(
                        f"DELETE FROM bsl_code_phase_b_unit_state "
                        f"WHERE project_name = ? AND routine_id IN ({placeholders})",
                        (scope, *chunk),
                    )
                    removed_phase_b += cur.rowcount or 0
                stats["phase_b_state"] = removed_phase_b

                stats["modules"] = self._delete_config_modules_and_fts_in_tx(
                    cur, scope, epoch, config_name,
                )
                cur.execute(
                    "DELETE FROM bsl_code_methods "
                    "WHERE project_name = ? AND config_name = ? AND index_epoch = ?",
                    (scope, config_name, epoch),
                )
                stats["methods_residual"] = max(0, cur.rowcount or 0)

                for table in ("bsl_code_units", "bsl_code_methods", "bsl_code_modules"):
                    left = cur.execute(
                        f"SELECT count(*) FROM {table} "
                        "WHERE project_name = ? AND config_name = ? AND index_epoch = ?",
                        (scope, config_name, epoch),
                    ).fetchone()[0]
                    if left:
                        raise BslCodeSqliteError(
                            f"config delete residuals: {left} row(s) of config "
                            f"{config_name!r} remain in {table}"
                        )

                if operation_id and drop_operation:
                    cur.execute(
                        "DELETE FROM bsl_code_config_delete_routines "
                        "WHERE project_name = ? AND operation_id = ?",
                        (scope, operation_id),
                    )
                    cur.execute(
                        "DELETE FROM bsl_code_config_delete_ops "
                        "WHERE project_name = ? AND operation_id = ?",
                        (scope, operation_id),
                    )
                cur.execute("COMMIT")
                return stats
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def recover_config_delete_operation_disabled(
        self,
        scope: str,
        operation_id: str,
        config_name: str,
        epoch: int,
    ) -> Dict[str, Any]:
        """Teardown операции при выключенном BSL — одной транзакцией.

        Ситуация: процесс перезапустили с `ENABLE_BSL_CODE_SEARCH=false`, а
        операция уже пересекла destructive-границу. Bundle не поднимается, но
        оставить строки операции и поднятый gate нельзя: gate переживёт даже
        последующий полный rebuild (`commit_pending` его не трогает), а
        `classify_delta_readiness` будет вечно видеть активную операцию.

        Ветвление по состоянию:
        - `committed`/`residual_done` — counters уже вычтены корректно, значит
          достаточно housekeeping без `reindex_requested`: согласованный индекс
          не должен платить полной перестройкой при повторном включении BSL;
        - иначе — legacy purge с обязательством перестроить. Точное довычитание
          здесь ничего не покупает: при выключенном BSL sidecar никого не
          обслуживает, а `source_state_hash` уже разошёлся с графом, поэтому
          при включении rebuild всё равно неизбежен.
        """
        epoch = int(epoch)
        header = self.read_config_delete_operation(scope, operation_id)
        state = (header or {}).get("state") or ""
        ids = sorted((header or {}).get("routine_ids") or ())
        if state in ("committed", "residual_done"):
            stats = self.finalize_config_delete_residuals(
                scope, epoch, config_name, ids,
                operation_id=operation_id, drop_operation=True,
            )
            with self._lock:
                self._ensure_fingerprint_row(scope)
                self._set_scoped_gate_in_tx(self._conn, scope, False, ensure_row=False)
            return {"mode": "residual_only", "reindex_requested": 0, **stats}

        stats = self.purge_config_and_request_reindex(scope, epoch, config_name)
        self.drop_config_delete_operation(scope, operation_id, release_gate=True)
        return {"mode": "legacy_purge", **stats}

    def delete_phase_b_state_for_epoch(
        self,
        scope: str,
        vector_epoch: int,
    ) -> int:
        """Drop every Phase B done marker for (scope, vector_epoch).

        Used by start_indexing Phase B-only path when stored embedding_fingerprint
        diverges from the current one and Phase B must re-embed every unit
        regardless of prior `done` markers.
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM bsl_code_phase_b_unit_state "
                "WHERE project_name = ? AND vector_epoch = ?",
                (scope, int(vector_epoch)),
            )
            return cur.rowcount or 0

    # ------------------------------------------------------------------ GC

    def gc_retired_epochs(self, scope: Optional[str] = None) -> int:
        retention = self._retention_seconds()
        cutoff = time.time() - max(0, retention)
        removed = 0
        with self._lock:
            scopes = (
                [scope]
                if scope
                else [
                    r["project_name"]
                    for r in self._conn.execute(
                        "SELECT project_name FROM bsl_code_search_fingerprints"
                    ).fetchall()
                ]
            )
            for s in scopes:
                removed += self._gc_one_scope(s, cutoff)
        return removed

    def _gc_one_scope(self, scope: str, cutoff: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT retired_epochs FROM bsl_code_search_fingerprints "
                "WHERE project_name = ?",
                (scope,),
            ).fetchone()
            if not row:
                return 0
            retired = json.loads(row["retired_epochs"] or "[]")
            keep: List[Dict[str, Any]] = []
            to_drop_epochs: List[int] = []
            for entry in retired:
                ts = float(entry.get("retired_at", 0))
                ep = int(entry.get("epoch", 0))
                if ts <= cutoff:
                    to_drop_epochs.append(ep)
                else:
                    keep.append(entry)
            if not to_drop_epochs:
                return 0
            total_removed = 0
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                for ep in to_drop_epochs:
                    total_removed += self._wipe_epoch_rows(
                        cur, scope, ep, reason="retired_epoch_gc",
                    )
                cur.execute(
                    "UPDATE bsl_code_search_fingerprints "
                    "SET retired_epochs = ? WHERE project_name = ?",
                    (json.dumps(keep), scope),
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
            if total_removed:
                logger.info(
                    "BSL sqlite GC: scope=%s removed %d units across epochs=%s "
                    "(retention=%ds)",
                    scope, total_removed, to_drop_epochs, int(self._retention_seconds()),
                )
            return total_removed

    # ------------------------------------------------------------------ close

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


# ---------- module-level helpers ----------

# FTS5-reserved characters that must not enter a MATCH expression.
_FTS_SPECIAL = set('"\'()[]{}:^*+-')


def _sanitize_fts_query(query: str, *, prefix: bool = False, max_terms: int = 12) -> str:
    """
    Sanitize a user query into a safe FTS5 MATCH expression: split on
    whitespace once, for each whitespace-token replace FTS5-special chars
    with a space (keeping the intra-token whitespace in place so the
    resulting sub-tokens stay inside one implicit-AND FTS expression),
    lowercase, dedup, OR-join. With `prefix=True` each entry gets a trailing
    `*` so the LAST word of the entry becomes a prefix term.
    """
    if not query:
        return ""
    cleaned: List[str] = []
    seen: set = set()
    for raw in query.split():
        token = "".join(" " if ch in _FTS_SPECIAL else ch for ch in raw)
        token = token.strip().lower()
        if len(token) < 3 or token in seen:
            continue
        seen.add(token)
        cleaned.append(token)
        if len(cleaned) >= max_terms:
            break
    if not cleaned:
        return ""
    suffix = "*" if prefix else ""
    return " OR ".join(f"{token}{suffix}" for token in cleaned)


_INSTANCES: Dict[str, "BslCodeSqlite"] = {}
_INSTANCES_LOCK = threading.Lock()


def get_bsl_code_sqlite(db_path: Optional[str] = None) -> BslCodeSqlite:
    path = str(Path(db_path or settings.bsl_code_search_sqlite_path).resolve())
    with _INSTANCES_LOCK:
        inst = _INSTANCES.get(path)
        if inst is None:
            inst = BslCodeSqlite(db_path=path)
            _INSTANCES[path] = inst
        return inst


def open_existing_bsl_sidecar(db_path: Optional[str] = None) -> Optional[BslCodeSqlite]:
    """Открыть УЖЕ существующий sidecar; не создавать файл и схему.

    Нужен единственному потребителю — teardown config-delete операции при
    выключенном `ENABLE_BSL_CODE_SEARCH`. Обычная фабрика там недопустима:
    она материализовала бы пустой sidecar для выключенной подсистемы, и
    позднее включение доверилось бы этому ready-state (та же причина, по
    которой `purge_config_scope` проверяет существование файла до
    инстанцирования).

    Возвращает `None`, если файла нет — закрывать нечего.
    """
    try:
        resolved = Path(db_path or settings.bsl_code_search_sqlite_path)
    except Exception:  # noqa: BLE001
        logger.exception("open_existing_bsl_sidecar: sqlite path unavailable")
        return None
    if not resolved.exists():
        return None
    return get_bsl_code_sqlite(str(resolved))
