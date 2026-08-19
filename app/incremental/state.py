"""
SQLite-backed state for incremental loading (stage 1).

Schema:
- stage_state:           один row per (project_name, stage_name) с watermark.
- metadata_object_hashes: object_hash + property_keys для MetadataObject.
- configuration_state:    configuration_hash + property_keys для Configuration.
- form_property_keys:     property_keys для :Form (survived-cleanup).
- command_property_keys:  property_keys для object-level :Command (survived-cleanup).
- source_manifest:        file-level baseline (size/mtime/content_hash) для txt и xml.
- scheduler_lock:         cooperative lock между конкурентными запусками.

Все DELETE/UPDATE scoped по project_name + source_type.
QN-prefix DELETE использует LIKE ? ESCAPE '\' для устойчивости к спецсимволам.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Потоковая запись artifact baseline: батч копится до этого объёма сериализованного
# JSON ИЛИ до лимита строк; артефакт крупнее лимита байт пишется отдельным батчем.
_ARTIFACT_BATCH_MAX_BYTES = 32 * 1024 * 1024
_ARTIFACT_BATCH_MAX_ROWS = 500


class ArtifactBaselineReadiness(enum.Enum):
    """Исход единой проверки готовности artifact baseline (см.
    IncrementalLoadingState.evaluate_artifact_baseline_readiness).

    READY / отсутствуют оба baseline → допускать; остальные → fail-closed.
    """

    READY = "ready"
    FULL_RELOAD_REQUIRED = "full_reload_required"
    SOURCE_MISMATCH = "source_mismatch"
    BASELINE_NOT_INITIALIZED = "baseline_not_initialized"


def escape_like(s: str) -> str:
    """Escape %, _, \\ для безопасного использования в LIKE pattern (с ESCAPE '\\')."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS stage_state (
    project_name TEXT NOT NULL,
    stage_name   TEXT NOT NULL,
    source_type  TEXT NOT NULL,
    watermark_ns INTEGER NOT NULL DEFAULT 0,
    last_success_at INTEGER,
    last_full_scan_at INTEGER,
    PRIMARY KEY (project_name, stage_name)
);

CREATE TABLE IF NOT EXISTS metadata_object_hashes (
    project_name      TEXT NOT NULL,
    object_qn         TEXT NOT NULL,
    source_type       TEXT NOT NULL,
    object_hash       TEXT NOT NULL,
    property_keys_json TEXT NOT NULL,
    object_snapshot_json TEXT,
    last_seen_at      INTEGER NOT NULL,
    PRIMARY KEY (project_name, source_type, object_qn)
);

CREATE TABLE IF NOT EXISTS configuration_state (
    project_name       TEXT NOT NULL,
    configuration_qn   TEXT NOT NULL,
    source_type        TEXT NOT NULL,
    configuration_hash TEXT NOT NULL,
    property_keys_json TEXT NOT NULL,
    last_seen_at       INTEGER NOT NULL,
    PRIMARY KEY (project_name, source_type, configuration_qn)
);

CREATE TABLE IF NOT EXISTS form_property_keys (
    project_name       TEXT NOT NULL,
    form_qn            TEXT NOT NULL,
    source_type        TEXT NOT NULL,
    property_keys_json TEXT NOT NULL,
    last_seen_at       INTEGER NOT NULL,
    PRIMARY KEY (project_name, source_type, form_qn)
);

CREATE TABLE IF NOT EXISTS command_property_keys (
    project_name       TEXT NOT NULL,
    command_qn         TEXT NOT NULL,
    source_type        TEXT NOT NULL,
    property_keys_json TEXT NOT NULL,
    last_seen_at       INTEGER NOT NULL,
    PRIMARY KEY (project_name, source_type, command_qn)
);

CREATE TABLE IF NOT EXISTS source_manifest (
    project_name        TEXT NOT NULL,
    source_type         TEXT NOT NULL,
    rel_path            TEXT NOT NULL,
    size                INTEGER NOT NULL,
    mtime_ns            INTEGER NOT NULL,
    content_hash        TEXT NOT NULL,
    emitted_qn_json     TEXT NOT NULL DEFAULT '[]',
    last_seen_at        INTEGER NOT NULL,
    last_seen_full_scan_at INTEGER,
    PRIMARY KEY (project_name, source_type, rel_path)
);

CREATE TABLE IF NOT EXISTS scheduler_lock (
    name         TEXT PRIMARY KEY,
    owner        TEXT NOT NULL,
    acquired_at  INTEGER NOT NULL,
    heartbeat_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS artifact_manifest (
    project_name      TEXT NOT NULL,
    source_scope      TEXT NOT NULL,
    rel_path          TEXT NOT NULL,
    size              INTEGER NOT NULL,
    mtime_ns          INTEGER NOT NULL,
    content_hash      TEXT NOT NULL,
    last_seen_at      INTEGER NOT NULL,
    last_seen_full_reconcile_at INTEGER,
    PRIMARY KEY (project_name, source_scope, rel_path)
);

CREATE TABLE IF NOT EXISTS bsl_file_artifacts (
    project_name        TEXT NOT NULL,
    source_scope        TEXT NOT NULL,
    config_name         TEXT NOT NULL,
    rel_path            TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    routine_ids_json    TEXT NOT NULL,
    module_ids_json     TEXT NOT NULL,
    routines_index_json TEXT NOT NULL,
    callsites_json      TEXT NOT NULL,
    form_links_json     TEXT NOT NULL,
    updated_at          INTEGER NOT NULL,
    PRIMARY KEY (project_name, source_scope, rel_path)
);

CREATE TABLE IF NOT EXISTS bsl_deferred_changes (
    project_name TEXT NOT NULL,
    source_scope TEXT NOT NULL,
    deferred_at  INTEGER NOT NULL,
    diff_json    TEXT NOT NULL,
    PRIMARY KEY (project_name, source_scope)
);

CREATE TABLE IF NOT EXISTS guid_state (
    project_name   TEXT NOT NULL,
    scope          TEXT NOT NULL,
    label          TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    xcf_name       TEXT NOT NULL,
    current_guid   TEXT,
    PRIMARY KEY (project_name, scope, label, qualified_name)
);

CREATE TABLE IF NOT EXISTS ext_analyzer_outputs (
    project_name   TEXT NOT NULL,
    source_scope   TEXT NOT NULL,
    rel_path       TEXT NOT NULL,
    label          TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    output_kind    TEXT NOT NULL,
    property_key   TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    PRIMARY KEY (project_name, source_scope, rel_path, label, qualified_name, output_kind, property_key)
);

CREATE TABLE IF NOT EXISTS extension_scope_cleanup (
    project_name   TEXT NOT NULL,
    source_scope   TEXT NOT NULL,
    config_qn      TEXT NOT NULL,
    source_mode    TEXT NOT NULL,
    ext_dir_name   TEXT NOT NULL,
    config_name    TEXT NOT NULL,
    reason         TEXT NOT NULL,
    stage          TEXT NOT NULL,
    graph_mutation_started INTEGER NOT NULL DEFAULT 0,
    graph_refresh_required INTEGER NOT NULL DEFAULT 0,
    confirmations  INTEGER NOT NULL DEFAULT 1,
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    discovered_at  INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL,
    -- Идентичность BSL-операции удаления. Генерируется ОДИН раз при INSERT и
    -- переживает повторный enqueue: именно поэтому durable-подготовку можно
    -- писать в BSL SQLite раньше incremental-состояния — retry придёт с тем же
    -- id и усыновит её. Пустое значение = строка создана версией без scoped
    -- удаления, для неё действует прежний legacy-маршрут.
    cleanup_uid    TEXT NOT NULL DEFAULT '',
    bsl_mode       TEXT NOT NULL DEFAULT '',
    bsl_fallback_reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project_name, source_scope, config_qn)
);
"""

# Стадии cleanup — монотонные, назад не откатываются.
CLEANUP_STAGE_DISCOVERED = "discovered"
CLEANUP_STAGE_BSL_DELETE_PREPARED = "bsl_delete_prepared"
CLEANUP_STAGE_GRAPH_DELETED = "graph_deleted"
CLEANUP_STAGE_SUMMARIES_QUARANTINED = "summaries_quarantined"
CLEANUP_STAGE_BSL_DELTA_APPLIED = "bsl_delta_applied"
CLEANUP_STAGE_BSL_PURGED = "bsl_purged"
CLEANUP_STAGE_FINALIZED = "finalized"

# Порядок важен: `_stage_rank` сравнивает по индексу, чтобы upsert не понижал
# уже достигнутую стадию. Quarantine стоит ДО BSL сознательно — файловая стадия
# ни от чего не зависит, а BSL-стадия единственная может законно отложиться.
#
# `bsl_delete_prepared` стоит ДО graph delete: durable-снимок обратного вклада
# обязан существовать раньше, чем исчезнет граф, иначе восстановить вклад
# будет неоткуда. `bsl_delta_applied` — после quarantine, вместе с прежним
# `bsl_purged` (тот сохраняет имя и позицию, чтобы уже выпущенные строки
# очереди финализировались без спецкода; legacy-строки просто перепрыгивают
# новые стадии — сравнение по рангу это допускает).
CLEANUP_STAGE_ORDER: Tuple[str, ...] = (
    CLEANUP_STAGE_DISCOVERED,
    CLEANUP_STAGE_BSL_DELETE_PREPARED,
    CLEANUP_STAGE_GRAPH_DELETED,
    CLEANUP_STAGE_SUMMARIES_QUARANTINED,
    CLEANUP_STAGE_BSL_DELTA_APPLIED,
    CLEANUP_STAGE_BSL_PURGED,
    CLEANUP_STAGE_FINALIZED,
)

# Режимы BSL-стадии для отчёта и диагностики.
CLEANUP_BSL_MODE_SCOPED_DELTA = "scoped_delta"
CLEANUP_BSL_MODE_ABSENT = "absent"
CLEANUP_BSL_MODE_DISABLED = "disabled"
CLEANUP_BSL_MODE_LEGACY_PURGE = "legacy_purge"
CLEANUP_BSL_MODE_DEFERRED = "deferred"
CLEANUP_BSL_MODE_FULL_REBUILD_RECOVERY = "full_rebuild_recovery"

CLEANUP_REASON_DIRECTORY_REMOVED = "directory_removed"
CLEANUP_REASON_STRUCTURE_INVALID = "structure_invalid"
CLEANUP_REASON_CONFIGURATION_RENAMED = "configuration_renamed"


def cleanup_stage_rank(stage: str) -> int:
    """Порядковый номер стадии; неизвестная стадия — -1 (ниже любой известной)."""
    try:
        return CLEANUP_STAGE_ORDER.index(stage)
    except ValueError:
        return -1


class IncrementalLoadingState:
    """SQLite wrapper для incremental state.

    Не thread-safe для одного instance; scheduler-thread держит свой instance.
    Все операции scoped по project_name.
    """

    def __init__(self, state_path: Path, project_name: str) -> None:
        self.state_path = Path(state_path)
        self.project_name = project_name
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------
    # Connection / schema
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.state_path),
            isolation_level=None,  # autocommit
            check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.executescript(_SCHEMA_SQL)
        self._migrate_metadata_object_hashes(conn)
        self._migrate_extension_scope_cleanup(conn)
        self._conn = conn
        return conn

    @staticmethod
    def _migrate_extension_scope_cleanup(conn: sqlite3.Connection) -> None:
        """Догнать колонки очереди в уже существующей БД.

        `CREATE TABLE IF NOT EXISTS` существующую таблицу не обновляет, поэтому
        без явного `ALTER TABLE` строки очереди старой версии не получили бы ни
        `cleanup_uid`, ни полей режима. Пустой `cleanup_uid` при этом —
        осмысленное значение: он и есть признак «строка создана версией без
        scoped удаления».
        """
        cols = {row[1] for row in conn.execute("PRAGMA table_info(extension_scope_cleanup)")}
        for name in ("cleanup_uid", "bsl_mode", "bsl_fallback_reason"):
            if name not in cols:
                conn.execute(
                    f"ALTER TABLE extension_scope_cleanup "
                    f"ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                )

    @staticmethod
    def _migrate_metadata_object_hashes(conn: sqlite3.Connection) -> None:
        """Add `object_snapshot_json` to legacy `metadata_object_hashes` tables.

        Существующие rows остаются с NULL — это сигнал «legacy baseline»; child-diff
        для них пропускается и заполняется на первом же реальном incremental change.
        """
        cols = {row[1] for row in conn.execute("PRAGMA table_info(metadata_object_hashes)")}
        if "object_snapshot_json" not in cols:
            conn.execute("ALTER TABLE metadata_object_hashes ADD COLUMN object_snapshot_json TEXT")

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Транзакция для группы изменений (apply_*_object orchestration).

        Использует BEGIN/COMMIT/ROLLBACK; isolation_level=None означает manual control.
        """
        conn = self._connect()
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------
    # stage_state
    # ------------------------------------------------------------------

    def get_stage_source_type(self, stage_name: str) -> Optional[str]:
        conn = self._connect()
        row = conn.execute(
            "SELECT source_type FROM stage_state WHERE project_name=? AND stage_name=?",
            (self.project_name, stage_name),
        ).fetchone()
        return row[0] if row else None

    def get_stage_state(self, stage_name: str) -> Optional[Dict[str, Any]]:
        """Полная stage_state row для диагностики (watermark/last_success).

        Используется только для логирования incremental discovery; hard-skip
        по watermark на этом этапе НЕ применяется (старый mtime у нового файла
        может попасть под watermark cutoff).
        """
        conn = self._connect()
        row = conn.execute(
            "SELECT source_type, watermark_ns, last_success_at, last_full_scan_at "
            "FROM stage_state WHERE project_name=? AND stage_name=?",
            (self.project_name, stage_name),
        ).fetchone()
        if not row:
            return None
        return {
            "source_type": row[0],
            "watermark_ns": row[1],
            "last_success_at": row[2],
            "last_full_scan_at": row[3],
        }

    def has_any_baseline(self) -> bool:
        """True если для проекта есть хотя бы один stage_state row."""
        conn = self._connect()
        row = conn.execute(
            "SELECT 1 FROM stage_state WHERE project_name=? LIMIT 1",
            (self.project_name,),
        ).fetchone()
        return row is not None

    def upsert_stage_state(
        self,
        stage_name: str,
        source_type: str,
        watermark_ns: int,
        full_scan_at: Optional[int] = None,
    ) -> None:
        now = int(time.time())
        conn = self._connect()
        if full_scan_at is None:
            conn.execute(
                """
                INSERT INTO stage_state (project_name, stage_name, source_type, watermark_ns, last_success_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_name, stage_name) DO UPDATE SET
                    source_type=excluded.source_type,
                    watermark_ns=excluded.watermark_ns,
                    last_success_at=excluded.last_success_at
                """,
                (self.project_name, stage_name, source_type, watermark_ns, now),
            )
        else:
            conn.execute(
                """
                INSERT INTO stage_state (project_name, stage_name, source_type, watermark_ns, last_success_at, last_full_scan_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_name, stage_name) DO UPDATE SET
                    source_type=excluded.source_type,
                    watermark_ns=excluded.watermark_ns,
                    last_success_at=excluded.last_success_at,
                    last_full_scan_at=excluded.last_full_scan_at
                """,
                (self.project_name, stage_name, source_type, watermark_ns, now, full_scan_at),
            )

    # ------------------------------------------------------------------
    # metadata_object_hashes
    # ------------------------------------------------------------------

    def get_object_state(
        self, source_type: str, object_qn: str
    ) -> Optional[Tuple[str, Set[str], Optional[Dict[str, Any]]]]:
        """Returns (object_hash, property_keys, object_snapshot) или None.

        `object_snapshot` is `None` для legacy rows без сохранённого snapshot_json
        (миграция оставляет колонку NULL до первого реального upsert).
        """
        conn = self._connect()
        row = conn.execute(
            "SELECT object_hash, property_keys_json, object_snapshot_json "
            "FROM metadata_object_hashes "
            "WHERE project_name=? AND source_type=? AND object_qn=?",
            (self.project_name, source_type, object_qn),
        ).fetchone()
        if not row:
            return None
        snapshot: Optional[Dict[str, Any]] = None
        if row[2]:
            try:
                snapshot = json.loads(row[2])
            except (TypeError, ValueError):
                snapshot = None
        return row[0], set(json.loads(row[1])), snapshot

    def get_all_object_qns(self, source_type: str) -> Set[str]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT object_qn FROM metadata_object_hashes WHERE project_name=? AND source_type=?",
            (self.project_name, source_type),
        ).fetchall()
        return {r[0] for r in rows}

    def upsert_object_state(
        self,
        source_type: str,
        object_qn: str,
        object_hash: str,
        property_keys: Set[str],
        *,
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> None:
        """UPSERT object state.

        `snapshot` keyword-only, backward-compatible default `None` — legacy call sites
        не ломаются. Новый код обязан передавать `snapshot=build_object_snapshot(obj)`,
        иначе child-diff не сможет посчитать stats при следующем изменении.
        """
        now = int(time.time())
        conn = self._connect()
        snapshot_json: Optional[str] = (
            json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
            if snapshot is not None
            else None
        )
        conn.execute(
            """
            INSERT INTO metadata_object_hashes
                (project_name, source_type, object_qn, object_hash, property_keys_json,
                 object_snapshot_json, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_name, source_type, object_qn) DO UPDATE SET
                object_hash=excluded.object_hash,
                property_keys_json=excluded.property_keys_json,
                object_snapshot_json=excluded.object_snapshot_json,
                last_seen_at=excluded.last_seen_at
            """,
            (
                self.project_name,
                source_type,
                object_qn,
                object_hash,
                json.dumps(sorted(property_keys), ensure_ascii=False),
                snapshot_json,
                now,
            ),
        )

    def delete_object_state(self, source_type: str, object_qn: str) -> None:
        """Удалить state для deleted object: metadata_object_hashes + form_property_keys + command_property_keys."""
        conn = self._connect()
        conn.execute(
            "DELETE FROM metadata_object_hashes "
            "WHERE project_name=? AND source_type=? AND object_qn=?",
            (self.project_name, source_type, object_qn),
        )
        form_prefix = escape_like(object_qn) + "/Form/%"
        cmd_prefix = escape_like(object_qn) + "/Command/%"
        conn.execute(
            "DELETE FROM form_property_keys "
            "WHERE project_name=? AND source_type=? AND form_qn LIKE ? ESCAPE '\\'",
            (self.project_name, source_type, form_prefix),
        )
        conn.execute(
            "DELETE FROM command_property_keys "
            "WHERE project_name=? AND source_type=? AND command_qn LIKE ? ESCAPE '\\'",
            (self.project_name, source_type, cmd_prefix),
        )

    # ------------------------------------------------------------------
    # guid_state — sidecar для meta_uuid enrichment
    # ------------------------------------------------------------------

    def upsert_guid_state_many(
        self, rows: Iterable[Dict[str, Any]]
    ) -> None:
        """Upsert rows: каждая строка {scope, label, qualified_name, xcf_name, current_guid}."""
        items = [
            (
                self.project_name,
                r["scope"],
                r["label"],
                r["qualified_name"],
                r["xcf_name"],
                r.get("current_guid"),
            )
            for r in rows
        ]
        if not items:
            return
        conn = self._connect()
        conn.executemany(
            """
            INSERT INTO guid_state
                (project_name, scope, label, qualified_name, xcf_name, current_guid)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_name, scope, label, qualified_name) DO UPDATE SET
                xcf_name=excluded.xcf_name,
                current_guid=excluded.current_guid
            """,
            items,
        )

    def get_guid_state_for_scope(self, scope: str) -> List[Dict[str, Any]]:
        """Вернуть все rows для scope: [{label, qualified_name, xcf_name, current_guid}]."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT label, qualified_name, xcf_name, current_guid "
            "FROM guid_state WHERE project_name=? AND scope=?",
            (self.project_name, scope),
        ).fetchall()
        return [
            {
                "label": r[0],
                "qualified_name": r[1],
                "xcf_name": r[2],
                "current_guid": r[3],
            }
            for r in rows
        ]

    def delete_guid_state_for_object_subtree(
        self, scope: str, object_qn: str
    ) -> None:
        """Удалить guid_state rows для object subtree: сам object и все его children
        (qualified_name STARTS WITH object_qn + '/')."""
        conn = self._connect()
        like = escape_like(object_qn + "/") + "%"
        conn.execute(
            "DELETE FROM guid_state "
            "WHERE project_name=? AND scope=? AND ("
            "    qualified_name=? "
            " OR qualified_name LIKE ? ESCAPE '\\'"
            ")",
            (self.project_name, scope, object_qn, like),
        )

    def update_guid_state_current_guids(
        self, scope: str, rows: Iterable[Tuple[str, str, Optional[str]]]
    ) -> None:
        """Обновить current_guid батчем: rows = [(label, qualified_name, new_guid)]."""
        items = [
            (new_guid, self.project_name, scope, label, qn)
            for (label, qn, new_guid) in rows
        ]
        if not items:
            return
        conn = self._connect()
        conn.executemany(
            "UPDATE guid_state SET current_guid=? "
            "WHERE project_name=? AND scope=? AND label=? AND qualified_name=?",
            items,
        )

    # ------------------------------------------------------------------
    # ext_analyzer_outputs — sidecar для extension property analysis
    # ------------------------------------------------------------------

    def upsert_ext_analyzer_outputs_many(
        self, rows: Iterable[Dict[str, Any]]
    ) -> None:
        """Upsert rows: {source_scope, rel_path, label, qualified_name, output_kind, property_key, payload_json}."""
        items = [
            (
                self.project_name,
                r["source_scope"],
                r["rel_path"],
                r["label"],
                r["qualified_name"],
                r["output_kind"],
                r["property_key"],
                r["payload_json"],
            )
            for r in rows
        ]
        if not items:
            return
        conn = self._connect()
        conn.executemany(
            """
            INSERT INTO ext_analyzer_outputs
                (project_name, source_scope, rel_path, label, qualified_name,
                 output_kind, property_key, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_name, source_scope, rel_path, label, qualified_name,
                        output_kind, property_key) DO UPDATE SET
                payload_json=excluded.payload_json
            """,
            items,
        )

    def get_ext_analyzer_outputs_for_files(
        self, source_scope: str, rel_paths: Iterable[str]
    ) -> List[Dict[str, Any]]:
        """Вернуть все rows для (scope, rel_path in rel_paths)."""
        paths = list(rel_paths)
        if not paths:
            return []
        placeholders = ",".join("?" * len(paths))
        conn = self._connect()
        params = [self.project_name, source_scope, *paths]
        cur = conn.execute(
            f"SELECT rel_path, label, qualified_name, output_kind, property_key, payload_json "
            f"FROM ext_analyzer_outputs "
            f"WHERE project_name=? AND source_scope=? AND rel_path IN ({placeholders})",
            params,
        )
        return [
            {
                "rel_path": r[0],
                "label": r[1],
                "qualified_name": r[2],
                "output_kind": r[3],
                "property_key": r[4],
                "payload_json": r[5],
            }
            for r in cur.fetchall()
        ]

    def delete_ext_analyzer_outputs_for_files(
        self, source_scope: str, rel_paths: Iterable[str]
    ) -> None:
        paths = list(rel_paths)
        if not paths:
            return
        placeholders = ",".join("?" * len(paths))
        conn = self._connect()
        conn.execute(
            f"DELETE FROM ext_analyzer_outputs "
            f"WHERE project_name=? AND source_scope=? AND rel_path IN ({placeholders})",
            [self.project_name, source_scope, *paths],
        )

    # ------------------------------------------------------------------
    # configuration_state
    # ------------------------------------------------------------------

    def get_configuration_state(
        self, source_type: str, configuration_qn: str
    ) -> Optional[Tuple[str, Set[str]]]:
        conn = self._connect()
        row = conn.execute(
            "SELECT configuration_hash, property_keys_json FROM configuration_state "
            "WHERE project_name=? AND source_type=? AND configuration_qn=?",
            (self.project_name, source_type, configuration_qn),
        ).fetchone()
        if not row:
            return None
        return row[0], set(json.loads(row[1]))

    def upsert_configuration_state(
        self,
        source_type: str,
        configuration_qn: str,
        configuration_hash: str,
        property_keys: Set[str],
    ) -> None:
        now = int(time.time())
        conn = self._connect()
        conn.execute(
            """
            INSERT INTO configuration_state
                (project_name, source_type, configuration_qn, configuration_hash, property_keys_json, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_name, source_type, configuration_qn) DO UPDATE SET
                configuration_hash=excluded.configuration_hash,
                property_keys_json=excluded.property_keys_json,
                last_seen_at=excluded.last_seen_at
            """,
            (
                self.project_name,
                source_type,
                configuration_qn,
                configuration_hash,
                json.dumps(sorted(property_keys), ensure_ascii=False),
                now,
            ),
        )

    # ------------------------------------------------------------------
    # form_property_keys / command_property_keys
    # ------------------------------------------------------------------

    def get_form_property_keys(self, source_type: str, form_qn: str) -> Optional[Set[str]]:
        conn = self._connect()
        row = conn.execute(
            "SELECT property_keys_json FROM form_property_keys "
            "WHERE project_name=? AND source_type=? AND form_qn=?",
            (self.project_name, source_type, form_qn),
        ).fetchone()
        return set(json.loads(row[0])) if row else None

    def upsert_form_property_keys(
        self, source_type: str, form_qn: str, property_keys: Set[str]
    ) -> None:
        now = int(time.time())
        conn = self._connect()
        conn.execute(
            """
            INSERT INTO form_property_keys
                (project_name, source_type, form_qn, property_keys_json, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_name, source_type, form_qn) DO UPDATE SET
                property_keys_json=excluded.property_keys_json,
                last_seen_at=excluded.last_seen_at
            """,
            (
                self.project_name,
                source_type,
                form_qn,
                json.dumps(sorted(property_keys), ensure_ascii=False),
                now,
            ),
        )

    def delete_form_property_keys(self, source_type: str, form_qns: Iterable[str]) -> None:
        conn = self._connect()
        rows = [(self.project_name, source_type, qn) for qn in form_qns]
        if not rows:
            return
        conn.executemany(
            "DELETE FROM form_property_keys WHERE project_name=? AND source_type=? AND form_qn=?",
            rows,
        )

    def get_command_property_keys(self, source_type: str, command_qn: str) -> Optional[Set[str]]:
        conn = self._connect()
        row = conn.execute(
            "SELECT property_keys_json FROM command_property_keys "
            "WHERE project_name=? AND source_type=? AND command_qn=?",
            (self.project_name, source_type, command_qn),
        ).fetchone()
        return set(json.loads(row[0])) if row else None

    def upsert_command_property_keys(
        self, source_type: str, command_qn: str, property_keys: Set[str]
    ) -> None:
        now = int(time.time())
        conn = self._connect()
        conn.execute(
            """
            INSERT INTO command_property_keys
                (project_name, source_type, command_qn, property_keys_json, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_name, source_type, command_qn) DO UPDATE SET
                property_keys_json=excluded.property_keys_json,
                last_seen_at=excluded.last_seen_at
            """,
            (
                self.project_name,
                source_type,
                command_qn,
                json.dumps(sorted(property_keys), ensure_ascii=False),
                now,
            ),
        )

    def delete_command_property_keys(self, source_type: str, command_qns: Iterable[str]) -> None:
        conn = self._connect()
        rows = [(self.project_name, source_type, qn) for qn in command_qns]
        if not rows:
            return
        conn.executemany(
            "DELETE FROM command_property_keys WHERE project_name=? AND source_type=? AND command_qn=?",
            rows,
        )

    # ------------------------------------------------------------------
    # source_manifest
    # ------------------------------------------------------------------

    def get_source_manifest(
        self, source_type: str, rel_path: str
    ) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        row = conn.execute(
            "SELECT size, mtime_ns, content_hash, emitted_qn_json FROM source_manifest "
            "WHERE project_name=? AND source_type=? AND rel_path=?",
            (self.project_name, source_type, rel_path),
        ).fetchone()
        if not row:
            return None
        return {
            "size": row[0],
            "mtime_ns": row[1],
            "content_hash": row[2],
            "emitted_qns": json.loads(row[3]),
        }

    def all_source_manifest_rel_paths(self, source_type: str) -> Set[str]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT rel_path FROM source_manifest WHERE project_name=? AND source_type=?",
            (self.project_name, source_type),
        ).fetchall()
        return {r[0] for r in rows}

    def get_source_manifest_map(
        self, source_type: str
    ) -> Dict[str, Dict[str, Any]]:
        """Batch-read всех manifest rows одного source_type, keyed by rel_path.

        Используется в discovery-фазах incremental loading: один SELECT вместо
        тысяч per-file `get_source_manifest(...)` вызовов.
        """
        conn = self._connect()
        rows = conn.execute(
            "SELECT rel_path, size, mtime_ns, content_hash, emitted_qn_json "
            "FROM source_manifest WHERE project_name=? AND source_type=?",
            (self.project_name, source_type),
        ).fetchall()
        return {
            r[0]: {
                "size": r[1],
                "mtime_ns": r[2],
                "content_hash": r[3],
                "emitted_qns": json.loads(r[4]),
            }
            for r in rows
        }

    def upsert_source_manifest(
        self,
        source_type: str,
        rel_path: str,
        size: int,
        mtime_ns: int,
        content_hash: str,
        emitted_qns: Optional[List[str]] = None,
        full_scan_at: Optional[int] = None,
    ) -> None:
        now = int(time.time())
        emitted_json = json.dumps(emitted_qns or [], ensure_ascii=False)
        conn = self._connect()
        if full_scan_at is None:
            conn.execute(
                """
                INSERT INTO source_manifest
                    (project_name, source_type, rel_path, size, mtime_ns, content_hash, emitted_qn_json, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_name, source_type, rel_path) DO UPDATE SET
                    size=excluded.size,
                    mtime_ns=excluded.mtime_ns,
                    content_hash=excluded.content_hash,
                    emitted_qn_json=excluded.emitted_qn_json,
                    last_seen_at=excluded.last_seen_at
                """,
                (self.project_name, source_type, rel_path, size, mtime_ns, content_hash, emitted_json, now),
            )
        else:
            conn.execute(
                """
                INSERT INTO source_manifest
                    (project_name, source_type, rel_path, size, mtime_ns, content_hash, emitted_qn_json, last_seen_at, last_seen_full_scan_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_name, source_type, rel_path) DO UPDATE SET
                    size=excluded.size,
                    mtime_ns=excluded.mtime_ns,
                    content_hash=excluded.content_hash,
                    emitted_qn_json=excluded.emitted_qn_json,
                    last_seen_at=excluded.last_seen_at,
                    last_seen_full_scan_at=excluded.last_seen_full_scan_at
                """,
                (
                    self.project_name,
                    source_type,
                    rel_path,
                    size,
                    mtime_ns,
                    content_hash,
                    emitted_json,
                    now,
                    full_scan_at,
                ),
            )

    def delete_source_manifest(self, source_type: str, rel_paths: Iterable[str]) -> None:
        conn = self._connect()
        rows = [(self.project_name, source_type, rp) for rp in rel_paths]
        if not rows:
            return
        conn.executemany(
            "DELETE FROM source_manifest WHERE project_name=? AND source_type=? AND rel_path=?",
            rows,
        )

    # ------------------------------------------------------------------
    # scheduler_lock
    # ------------------------------------------------------------------

    def try_acquire_lock(self, name: str, owner: str, stale_after_seconds: int) -> bool:
        """INSERT OR FAIL семантика с возможностью перехвата stale lock."""
        now = int(time.time())
        conn = self._connect()
        # Try insert fresh.
        try:
            conn.execute(
                "INSERT INTO scheduler_lock (name, owner, acquired_at, heartbeat_at) "
                "VALUES (?, ?, ?, ?)",
                (name, owner, now, now),
            )
            return True
        except sqlite3.IntegrityError:
            pass
        # Check stale.
        row = conn.execute(
            "SELECT heartbeat_at FROM scheduler_lock WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            # Race; retry insert.
            try:
                conn.execute(
                    "INSERT INTO scheduler_lock (name, owner, acquired_at, heartbeat_at) "
                    "VALUES (?, ?, ?, ?)",
                    (name, owner, now, now),
                )
                return True
            except sqlite3.IntegrityError:
                return False
        if now - row[0] > stale_after_seconds:
            # Stale — steal.
            conn.execute(
                "UPDATE scheduler_lock SET owner=?, acquired_at=?, heartbeat_at=? WHERE name=?",
                (owner, now, now, name),
            )
            return True
        return False

    def takeover_lock(self, name: str, new_owner: str) -> Optional[dict]:
        """Безусловный перехват lock.

        Возвращает прежнее (owner, acquired_at, heartbeat_at) — None если row не было.
        После вызова владельцем lock становится new_owner; release_lock(name, new_owner)
        удалит row штатно. Применяется для startup-режима в single-container deployment,
        где любой существующий lock — остаток предыдущего процесса.
        """
        now = int(time.time())
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT owner, acquired_at, heartbeat_at FROM scheduler_lock WHERE name=?",
                (name,),
            ).fetchone()
            conn.execute(
                "INSERT INTO scheduler_lock (name, owner, acquired_at, heartbeat_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "owner=excluded.owner, acquired_at=excluded.acquired_at, "
                "heartbeat_at=excluded.heartbeat_at",
                (name, new_owner, now, now),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        if row is None:
            return None
        return {"owner": row[0], "acquired_at": row[1], "heartbeat_at": row[2]}

    def heartbeat_lock(self, name: str, owner: str) -> None:
        now = int(time.time())
        conn = self._connect()
        conn.execute(
            "UPDATE scheduler_lock SET heartbeat_at=? WHERE name=? AND owner=?",
            (now, name, owner),
        )

    def release_lock(self, name: str, owner: str) -> None:
        conn = self._connect()
        conn.execute(
            "DELETE FROM scheduler_lock WHERE name=? AND owner=?",
            (name, owner),
        )

    # ------------------------------------------------------------------
    # Extension scope discovery / identity
    # ------------------------------------------------------------------

    def list_extension_scopes(self, base_source_type: str) -> Set[str]:
        """Все scope-ы расширений для активного source_type.

        Pattern: `txt_ext:%` или `xml_ext:%`. `%` экранируется в источниках,
        здесь его в pattern нет — просто prefix-match.
        """
        prefix = f"{base_source_type}_ext:"
        conn = self._connect()
        like_pattern = escape_like(prefix) + "%"
        rows = conn.execute(
            "SELECT DISTINCT source_type FROM stage_state "
            "WHERE project_name=? AND source_type LIKE ? ESCAPE '\\'",
            (self.project_name, like_pattern),
        ).fetchall()
        return {r[0] for r in rows}

    def get_extension_scope_config_qn(self, source_scope: str) -> Optional[str]:
        """Full QN конфигурации расширения для scope.

        Источник истины — `configuration_state.configuration_qn`, где для расширений
        лежит ровно один row с `f"{project_name}/{ext_graph_config_name}"`.
        Возвращает None если scope ещё не инициализирован.
        """
        conn = self._connect()
        row = conn.execute(
            "SELECT configuration_qn FROM configuration_state "
            "WHERE project_name=? AND source_type=? LIMIT 1",
            (self.project_name, source_scope),
        ).fetchone()
        return row[0] if row else None

    def delete_scope(self, source_scope: str) -> Dict[str, int]:
        """Снести весь state одного scope (используется при удалении каталога
        расширения и при rename конфигурации внутри `<ext_dir>`).

        Удаляет rows из всех per-scope таблиц по `source_type = source_scope`.
        `scheduler_lock` не трогаем — он не per-project/per-scope.
        `extension_scope_cleanup` тоже НЕ трогаем: строка очереди — это retry
        identity, её снимает координатор после перехода в `finalized`.

        Всё выполняется одной транзакцией: частично снесённый state оставил бы
        scope в состоянии, из которого следующий цикл не может ни продолжить,
        ни корректно переинициализироваться.

        Возвращает число удалённых строк по таблицам — для отчёта и тестов.
        """
        params = (self.project_name, source_scope)
        deleted: Dict[str, int] = {}

        def _run(conn: sqlite3.Connection, table: str, sql: str, args: Tuple) -> None:
            cur = conn.execute(sql, args)
            if cur.rowcount and cur.rowcount > 0:
                deleted[table] = deleted.get(table, 0) + cur.rowcount

        with self.transaction() as conn:
            _run(conn, "stage_state",
                 "DELETE FROM stage_state WHERE project_name=? AND source_type=?", params)
            _run(conn, "metadata_object_hashes",
                 "DELETE FROM metadata_object_hashes WHERE project_name=? AND source_type=?",
                 params)
            _run(conn, "configuration_state",
                 "DELETE FROM configuration_state WHERE project_name=? AND source_type=?",
                 params)
            _run(conn, "form_property_keys",
                 "DELETE FROM form_property_keys WHERE project_name=? AND source_type=?",
                 params)
            _run(conn, "command_property_keys",
                 "DELETE FROM command_property_keys WHERE project_name=? AND source_type=?",
                 params)
            _run(conn, "source_manifest",
                 "DELETE FROM source_manifest WHERE project_name=? AND source_type=?",
                 params)
            # Artifact state расширения снимается по prefix `artifact:ext:*:<ext_dir>:*`.
            # Извлекаем `<ext_dir>` из phase 1 source_scope: txt_ext:<dir> / xml_ext:<dir>.
            if ":" in source_scope:
                mode_dir = source_scope.split(":", 1)
                scope_kind = mode_dir[0]
                ext_dir = mode_dir[1] if len(mode_dir) > 1 else ""
                if scope_kind in ("txt_ext", "xml_ext") and ext_dir:
                    mode = "txt" if scope_kind == "txt_ext" else "xml"
                    artifact_prefix = f"artifact:ext:{mode}:{ext_dir}:"
                    like_pattern = escape_like(artifact_prefix) + "%"
                    _run(conn, "artifact_manifest",
                         "DELETE FROM artifact_manifest "
                         "WHERE project_name=? AND source_scope LIKE ? ESCAPE '\\'",
                         (self.project_name, like_pattern))
                    _run(conn, "bsl_file_artifacts",
                         "DELETE FROM bsl_file_artifacts "
                         "WHERE project_name=? AND source_scope LIKE ? ESCAPE '\\'",
                         (self.project_name, like_pattern))
                    # Отложенный BSL-дифф того же scope. Без этого удаления он
                    # пережил бы cleanup и был бы переигран на новом поколении,
                    # воскресив сигнатуры удалённой конфигурации.
                    _run(conn, "bsl_deferred_changes",
                         "DELETE FROM bsl_deferred_changes "
                         "WHERE project_name=? AND source_scope LIKE ? ESCAPE '\\'",
                         (self.project_name, like_pattern))
                    # guid_state и file-manifest для GUID — exact-scope, без LIKE.
                    guid_scope = f"guid_ext:{mode}:{ext_dir}"
                    _run(conn, "guid_state",
                         "DELETE FROM guid_state WHERE project_name=? AND scope=?",
                         (self.project_name, guid_scope))
                    _run(conn, "source_manifest",
                         "DELETE FROM source_manifest "
                         "WHERE project_name=? AND source_type='guid' AND rel_path=?",
                         (self.project_name, guid_scope))
                    # ext_analyzer_outputs — exact scope артефакта property_analysis.
                    pa_scope = f"artifact:ext:{mode}:{ext_dir}:property_analysis"
                    _run(conn, "ext_analyzer_outputs",
                         "DELETE FROM ext_analyzer_outputs "
                         "WHERE project_name=? AND source_scope=?",
                         (self.project_name, pa_scope))
        return deleted

    def scope_has_any_state(self, source_scope: str) -> bool:
        """Остались ли активные строки scope после `delete_scope`.

        Используется координатором как verification перед финализацией: очередь
        не должна помечаться завершённой, пока в state что-то живо.
        """
        conn = self._connect()
        params = (self.project_name, source_scope)
        for table in (
            "stage_state",
            "metadata_object_hashes",
            "configuration_state",
            "form_property_keys",
            "command_property_keys",
            "source_manifest",
        ):
            row = conn.execute(
                f"SELECT 1 FROM {table} WHERE project_name=? AND source_type=? LIMIT 1",
                params,
            ).fetchone()
            if row:
                return True
        if ":" in source_scope:
            scope_kind, _, ext_dir = source_scope.partition(":")
            if scope_kind in ("txt_ext", "xml_ext") and ext_dir:
                mode = "txt" if scope_kind == "txt_ext" else "xml"
                like_pattern = escape_like(f"artifact:ext:{mode}:{ext_dir}:") + "%"
                for table in ("artifact_manifest", "bsl_file_artifacts", "bsl_deferred_changes"):
                    row = conn.execute(
                        f"SELECT 1 FROM {table} "
                        "WHERE project_name=? AND source_scope LIKE ? ESCAPE '\\' LIMIT 1",
                        (self.project_name, like_pattern),
                    ).fetchone()
                    if row:
                        return True
        return False

    # ------------------------------------------------------------------
    # Baseline reset after full reload
    # ------------------------------------------------------------------

    def reset_after_full_reload(self) -> None:
        """Стереть весь state проекта. Вызывается перед записью baseline после full reload."""
        conn = self._connect()
        params = (self.project_name,)
        conn.execute("DELETE FROM stage_state WHERE project_name=?", params)
        conn.execute("DELETE FROM metadata_object_hashes WHERE project_name=?", params)
        conn.execute("DELETE FROM configuration_state WHERE project_name=?", params)
        conn.execute("DELETE FROM form_property_keys WHERE project_name=?", params)
        conn.execute("DELETE FROM command_property_keys WHERE project_name=?", params)
        conn.execute("DELETE FROM source_manifest WHERE project_name=?", params)
        conn.execute("DELETE FROM artifact_manifest WHERE project_name=?", params)
        conn.execute("DELETE FROM bsl_file_artifacts WHERE project_name=?", params)
        conn.execute("DELETE FROM bsl_deferred_changes WHERE project_name=?", params)
        conn.execute("DELETE FROM guid_state WHERE project_name=?", params)
        conn.execute("DELETE FROM ext_analyzer_outputs WHERE project_name=?", params)
        # Очередь cleanup тоже снимается: full reload перестраивает граф целиком,
        # поэтому незавершённые удаления отдельных scope теряют смысл — их работу
        # выполняет сам reload.
        conn.execute("DELETE FROM extension_scope_cleanup WHERE project_name=?", params)
        # scheduler_lock не трогаем — он не per-project.

    # ------------------------------------------------------------------
    # extension_scope_cleanup — durable-очередь удаления extension scope
    # ------------------------------------------------------------------

    def enqueue_extension_cleanup(
        self,
        *,
        source_scope: str,
        config_qn: str,
        source_mode: str,
        ext_dir_name: str,
        config_name: str,
        reason: str,
    ) -> int:
        """Поставить scope в очередь на удаление (idempotent upsert).

        Повторный enqueue той же identity поднимает `confirmations` — на этом
        построено требование второго подтверждения для `structure_invalid`, —
        но НЕ понижает достигнутую стадию, НЕ сбрасывает durable-флаги и НЕ
        перевыпускает `cleanup_uid`: идентичность BSL-операции обязана быть
        стабильной, иначе retry не смог бы усыновить уже записанную подготовку.

        Возвращает текущее значение `confirmations`.
        """
        now = int(time.time())
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT stage, confirmations FROM extension_scope_cleanup "
                "WHERE project_name=? AND source_scope=? AND config_qn=?",
                (self.project_name, source_scope, config_qn),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO extension_scope_cleanup("
                    " project_name, source_scope, config_qn, source_mode, ext_dir_name,"
                    " config_name, reason, stage, confirmations, discovered_at, updated_at,"
                    " cleanup_uid"
                    ") VALUES (?,?,?,?,?,?,?,?,1,?,?,?)",
                    (
                        self.project_name, source_scope, config_qn, source_mode,
                        ext_dir_name, config_name, reason,
                        CLEANUP_STAGE_DISCOVERED, now, now,
                        uuid.uuid4().hex,
                    ),
                )
                return 1
            confirmations = int(row[1] or 0) + 1
            conn.execute(
                "UPDATE extension_scope_cleanup "
                "SET confirmations=?, reason=?, updated_at=? "
                "WHERE project_name=? AND source_scope=? AND config_qn=?",
                (confirmations, reason, now, self.project_name, source_scope, config_qn),
            )
            return confirmations

    def list_pending_extension_cleanups(self) -> List[Dict[str, Any]]:
        """Все нефинализированные записи очереди, старые первыми."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT source_scope, config_qn, source_mode, ext_dir_name, config_name,"
            "       reason, stage, graph_mutation_started, graph_refresh_required,"
            "       confirmations, attempts, last_error, discovered_at, updated_at,"
            "       cleanup_uid, bsl_mode, bsl_fallback_reason "
            "FROM extension_scope_cleanup "
            "WHERE project_name=? AND stage != ? "
            "ORDER BY discovered_at, source_scope, config_qn",
            (self.project_name, CLEANUP_STAGE_FINALIZED),
        ).fetchall()
        keys = (
            "source_scope", "config_qn", "source_mode", "ext_dir_name", "config_name",
            "reason", "stage", "graph_mutation_started", "graph_refresh_required",
            "confirmations", "attempts", "last_error", "discovered_at", "updated_at",
            "cleanup_uid", "bsl_mode", "bsl_fallback_reason",
        )
        return [dict(zip(keys, r)) for r in rows]

    def list_pending_cleanup_scopes(self) -> Set[str]:
        """`source_scope` всех нефинализированных записей.

        Единый источник блокировки для всех фаз цикла. `delete_scope` бьёт по
        `source_scope`, а не по `config_qn`, поэтому пока запись жива, scope
        нельзя загружать ни одной фазой — иначе отложенная финализация снесёт
        baseline уже нового поколения, а artifact phase успеет воссоздать узлы
        старого под уже отработавшей стадией `graph_deleted`.
        """
        conn = self._connect()
        rows = conn.execute(
            "SELECT DISTINCT source_scope FROM extension_scope_cleanup "
            "WHERE project_name=? AND stage != ?",
            (self.project_name, CLEANUP_STAGE_FINALIZED),
        ).fetchall()
        return {r[0] for r in rows}

    def mark_extension_cleanup_graph_mutation_started(
        self, source_scope: str, config_qn: str
    ) -> None:
        """Выставить оба durable-флага одной транзакцией ДО первого DETACH DELETE.

        `graph_mutation_started` — признак пересечения destructive-границы, на
        нём построен stage-aware guard: после него вернувшийся каталог уже не
        отменяет saga, её нужно доводить до конца.

        `graph_refresh_required` — сигнал downstream-потребителю (кэш статистики
        Web Console). Точный счётчик удалённых узлов живёт только в памяти
        цикла, поэтому retry после падения между батчами увидел бы zero-delete
        и финализировал бы cleanup без обновления кэша.

        Оба выставляются ДО удаления, а не после: первый батч может упасть, и
        запись «мы уже начали ломать граф» обязана это пережить.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE extension_scope_cleanup "
                "SET graph_mutation_started=1, graph_refresh_required=1, updated_at=? "
                "WHERE project_name=? AND source_scope=? AND config_qn=?",
                (int(time.time()), self.project_name, source_scope, config_qn),
            )

    def advance_extension_cleanup_stage(
        self, source_scope: str, config_qn: str, stage: str
    ) -> None:
        """Продвинуть стадию, если новая строго выше достигнутой.

        Монотонность — не косметика: она гарантирует, что уже выполненная
        destructive-операция не повторится после рестарта.
        """
        rank = cleanup_stage_rank(stage)
        if rank < 0:
            raise ValueError(f"unknown cleanup stage: {stage!r}")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT stage FROM extension_scope_cleanup "
                "WHERE project_name=? AND source_scope=? AND config_qn=?",
                (self.project_name, source_scope, config_qn),
            ).fetchone()
            if row is None or cleanup_stage_rank(row[0]) >= rank:
                return
            conn.execute(
                "UPDATE extension_scope_cleanup SET stage=?, last_error=NULL, updated_at=? "
                "WHERE project_name=? AND source_scope=? AND config_qn=?",
                (stage, int(time.time()), self.project_name, source_scope, config_qn),
            )

    def record_extension_cleanup_bsl_mode(
        self,
        source_scope: str,
        config_qn: str,
        bsl_mode: str,
        *,
        fallback_reason: str = "",
    ) -> None:
        """Зафиксировать выбранный BSL-маршрут и причину отката.

        Отдельно от стадии: стадия говорит «докуда дошли», режим — «каким
        путём». Пустой `fallback_reason` при legacy-режиме недопустим по
        контракту отчёта: полная перестройка корпуса обязана иметь объяснение.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE extension_scope_cleanup "
                "SET bsl_mode=?, bsl_fallback_reason=?, updated_at=? "
                "WHERE project_name=? AND source_scope=? AND config_qn=?",
                (
                    bsl_mode, fallback_reason, int(time.time()),
                    self.project_name, source_scope, config_qn,
                ),
            )

    def record_extension_cleanup_error(
        self, source_scope: str, config_qn: str, error: str
    ) -> None:
        """Зафиксировать сбой стадии: attempts++ и текст ошибки.

        Запись очереди при этом сохраняется — она и есть retry identity.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE extension_scope_cleanup "
                "SET attempts=attempts+1, last_error=?, updated_at=? "
                "WHERE project_name=? AND source_scope=? AND config_qn=?",
                (error[:2000], int(time.time()), self.project_name, source_scope, config_qn),
            )

    def delete_extension_cleanup(self, source_scope: str, config_qn: str) -> None:
        """Снять запись очереди (финализация или supersede)."""
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM extension_scope_cleanup "
                "WHERE project_name=? AND source_scope=? AND config_qn=?",
                (self.project_name, source_scope, config_qn),
            )

    # ------------------------------------------------------------------
    # artifact_manifest — per-file state артефактов (Form.xml/.bsl/Predefined/Help/...)
    # ------------------------------------------------------------------

    def get_artifact_manifest(
        self, source_scope: str, rel_path: str
    ) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        row = conn.execute(
            "SELECT size, mtime_ns, content_hash, last_seen_full_reconcile_at "
            "FROM artifact_manifest "
            "WHERE project_name=? AND source_scope=? AND rel_path=?",
            (self.project_name, source_scope, rel_path),
        ).fetchone()
        if not row:
            return None
        return {
            "size": row[0],
            "mtime_ns": row[1],
            "content_hash": row[2],
            "last_seen_full_reconcile_at": row[3],
        }

    def all_artifact_manifest_rel_paths(self, source_scope: str) -> Set[str]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT rel_path FROM artifact_manifest WHERE project_name=? AND source_scope=?",
            (self.project_name, source_scope),
        ).fetchall()
        return {r[0] for r in rows}

    def has_any_artifact_baseline(self, source_scope: str) -> bool:
        conn = self._connect()
        row = conn.execute(
            "SELECT 1 FROM artifact_manifest "
            "WHERE project_name=? AND source_scope=? LIMIT 1",
            (self.project_name, source_scope),
        ).fetchone()
        return row is not None

    def evaluate_artifact_baseline_readiness(
        self, metadata_source: str
    ) -> ArtifactBaselineReadiness:
        """Единый владелец политики готовности artifact baseline (чистое чтение).

        Возвращает:
          - READY                    — completion stage активного source подтверждён
            (в т.ч. пустой manifest — artifact-флаги off/нет файлов);
          - FULL_RELOAD_REQUIRED     — metadata baseline активного source есть, а
            artifact completion stage нет (оборван/не создан);
          - SOURCE_MISMATCH          — completion/metadata baseline принадлежит другому
            source (METADATA_SOURCE переключён);
          - BASELINE_NOT_INITIALIZED — нет metadata baseline ни одного source
            (incremental state потерян/не создавался).

        Legacy-adoption не поддерживается (деплой всегда через full reload с нуля):
        completion stage — единственный сигнал достоверности baseline. Метод не пишет
        в SQLite. Интерпретация исхода (allow / fail-closed) — на стороне caller.
        """
        comp = self.get_stage_state("artifact_baseline")
        if comp is not None:
            if comp.get("source_type") == metadata_source:
                return ArtifactBaselineReadiness.READY
            return ArtifactBaselineReadiness.SOURCE_MISMATCH
        meta_active = self.get_stage_state(f"metadata_{metadata_source}")
        other = "xml" if metadata_source == "txt" else "txt"
        meta_other = self.get_stage_state(f"metadata_{other}")
        if meta_active is not None:
            return ArtifactBaselineReadiness.FULL_RELOAD_REQUIRED
        if meta_other is not None:
            return ArtifactBaselineReadiness.SOURCE_MISMATCH
        return ArtifactBaselineReadiness.BASELINE_NOT_INITIALIZED

    def upsert_artifact_manifest(
        self,
        source_scope: str,
        rel_path: str,
        size: int,
        mtime_ns: int,
        content_hash: str,
        full_reconcile_at: Optional[int] = None,
    ) -> None:
        now = int(time.time())
        conn = self._connect()
        if full_reconcile_at is None:
            conn.execute(
                """
                INSERT INTO artifact_manifest
                    (project_name, source_scope, rel_path, size, mtime_ns, content_hash, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_name, source_scope, rel_path) DO UPDATE SET
                    size=excluded.size,
                    mtime_ns=excluded.mtime_ns,
                    content_hash=excluded.content_hash,
                    last_seen_at=excluded.last_seen_at
                """,
                (self.project_name, source_scope, rel_path, size, mtime_ns, content_hash, now),
            )
        else:
            conn.execute(
                """
                INSERT INTO artifact_manifest
                    (project_name, source_scope, rel_path, size, mtime_ns, content_hash,
                     last_seen_at, last_seen_full_reconcile_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_name, source_scope, rel_path) DO UPDATE SET
                    size=excluded.size,
                    mtime_ns=excluded.mtime_ns,
                    content_hash=excluded.content_hash,
                    last_seen_at=excluded.last_seen_at,
                    last_seen_full_reconcile_at=excluded.last_seen_full_reconcile_at
                """,
                (
                    self.project_name,
                    source_scope,
                    rel_path,
                    size,
                    mtime_ns,
                    content_hash,
                    now,
                    full_reconcile_at,
                ),
            )

    def delete_artifact_manifest(self, source_scope: str, rel_paths: Iterable[str]) -> None:
        conn = self._connect()
        rows = [(self.project_name, source_scope, rp) for rp in rel_paths]
        if not rows:
            return
        conn.executemany(
            "DELETE FROM artifact_manifest "
            "WHERE project_name=? AND source_scope=? AND rel_path=?",
            rows,
        )

    def upsert_artifact_manifest_many(
        self,
        rows: Iterable[Dict[str, Any]],
    ) -> None:
        """Batch upsert. Каждый row — dict с ключами source_scope, rel_path,
        size, mtime_ns, content_hash, опционально full_reconcile_at.

        Соединение работает в autocommit (`isolation_level=None`); если нужна
        атомарность нескольких write-вызовов вместе, caller оборачивает их в
        `with state.transaction():`. Существующий per-file `upsert_artifact_manifest`
        остаётся для точечных вызовов (например, апдейт `last_seen_at` для unchanged).
        """
        materialized = list(rows)
        if not materialized:
            return
        now = int(time.time())
        conn = self._connect()
        plain = [
            (
                self.project_name,
                r["source_scope"],
                r["rel_path"],
                int(r["size"]),
                int(r["mtime_ns"]),
                r["content_hash"],
                now,
            )
            for r in materialized
            if r.get("full_reconcile_at") is None
        ]
        with_reconcile = [
            (
                self.project_name,
                r["source_scope"],
                r["rel_path"],
                int(r["size"]),
                int(r["mtime_ns"]),
                r["content_hash"],
                now,
                int(r["full_reconcile_at"]),
            )
            for r in materialized
            if r.get("full_reconcile_at") is not None
        ]
        if plain:
            conn.executemany(
                """
                INSERT INTO artifact_manifest
                    (project_name, source_scope, rel_path, size, mtime_ns, content_hash, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_name, source_scope, rel_path) DO UPDATE SET
                    size=excluded.size,
                    mtime_ns=excluded.mtime_ns,
                    content_hash=excluded.content_hash,
                    last_seen_at=excluded.last_seen_at
                """,
                plain,
            )
        if with_reconcile:
            conn.executemany(
                """
                INSERT INTO artifact_manifest
                    (project_name, source_scope, rel_path, size, mtime_ns, content_hash,
                     last_seen_at, last_seen_full_reconcile_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_name, source_scope, rel_path) DO UPDATE SET
                    size=excluded.size,
                    mtime_ns=excluded.mtime_ns,
                    content_hash=excluded.content_hash,
                    last_seen_at=excluded.last_seen_at,
                    last_seen_full_reconcile_at=excluded.last_seen_full_reconcile_at
                """,
                with_reconcile,
            )

    # ------------------------------------------------------------------
    # Batch upserts для phase-1 metadata baseline
    # ------------------------------------------------------------------

    def upsert_configuration_state_many(
        self,
        rows: Iterable[Dict[str, Any]],
    ) -> None:
        """Batch upsert configuration_state. Каждый row — dict с ключами
        source_type, configuration_qn, configuration_hash, property_keys.
        """
        materialized = list(rows)
        if not materialized:
            return
        now = int(time.time())
        conn = self._connect()
        params = [
            (
                self.project_name,
                r["source_type"],
                r["configuration_qn"],
                r["configuration_hash"],
                json.dumps(sorted(r["property_keys"]), ensure_ascii=False),
                now,
            )
            for r in materialized
        ]
        conn.executemany(
            """
            INSERT INTO configuration_state
                (project_name, source_type, configuration_qn, configuration_hash, property_keys_json, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_name, source_type, configuration_qn) DO UPDATE SET
                configuration_hash=excluded.configuration_hash,
                property_keys_json=excluded.property_keys_json,
                last_seen_at=excluded.last_seen_at
            """,
            params,
        )

    def upsert_object_state_many(
        self,
        rows: Iterable[Dict[str, Any]],
    ) -> None:
        """Batch upsert metadata_object_hashes. Каждый row — dict с ключами
        source_type, object_qn, object_hash, property_keys, snapshot
        (Optional[Dict], None если не нужно сохранять для child-diff).
        """
        materialized = list(rows)
        if not materialized:
            return
        now = int(time.time())
        conn = self._connect()
        params = [
            (
                self.project_name,
                r["source_type"],
                r["object_qn"],
                r["object_hash"],
                json.dumps(sorted(r["property_keys"]), ensure_ascii=False),
                (
                    json.dumps(r["snapshot"], sort_keys=True, ensure_ascii=False)
                    if r.get("snapshot") is not None
                    else None
                ),
                now,
            )
            for r in materialized
        ]
        conn.executemany(
            """
            INSERT INTO metadata_object_hashes
                (project_name, source_type, object_qn, object_hash, property_keys_json,
                 object_snapshot_json, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_name, source_type, object_qn) DO UPDATE SET
                object_hash=excluded.object_hash,
                property_keys_json=excluded.property_keys_json,
                object_snapshot_json=excluded.object_snapshot_json,
                last_seen_at=excluded.last_seen_at
            """,
            params,
        )

    def upsert_form_property_keys_many(
        self,
        rows: Iterable[Dict[str, Any]],
    ) -> None:
        """Batch upsert form_property_keys. Row keys: source_type, form_qn, property_keys."""
        materialized = list(rows)
        if not materialized:
            return
        now = int(time.time())
        conn = self._connect()
        params = [
            (
                self.project_name,
                r["source_type"],
                r["form_qn"],
                json.dumps(sorted(r["property_keys"]), ensure_ascii=False),
                now,
            )
            for r in materialized
        ]
        conn.executemany(
            """
            INSERT INTO form_property_keys
                (project_name, source_type, form_qn, property_keys_json, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_name, source_type, form_qn) DO UPDATE SET
                property_keys_json=excluded.property_keys_json,
                last_seen_at=excluded.last_seen_at
            """,
            params,
        )

    def upsert_command_property_keys_many(
        self,
        rows: Iterable[Dict[str, Any]],
    ) -> None:
        """Batch upsert command_property_keys. Row keys: source_type, command_qn, property_keys."""
        materialized = list(rows)
        if not materialized:
            return
        now = int(time.time())
        conn = self._connect()
        params = [
            (
                self.project_name,
                r["source_type"],
                r["command_qn"],
                json.dumps(sorted(r["property_keys"]), ensure_ascii=False),
                now,
            )
            for r in materialized
        ]
        conn.executemany(
            """
            INSERT INTO command_property_keys
                (project_name, source_type, command_qn, property_keys_json, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_name, source_type, command_qn) DO UPDATE SET
                property_keys_json=excluded.property_keys_json,
                last_seen_at=excluded.last_seen_at
            """,
            params,
        )

    def upsert_source_manifest_many(
        self,
        rows: Iterable[Dict[str, Any]],
    ) -> None:
        """Batch upsert source_manifest. Row keys: source_type, rel_path, size,
        mtime_ns, content_hash, emitted_qns (List[str], default []),
        опционально full_scan_at.

        Разводит rows на два bucket: без full_scan_at и c full_scan_at — двойной
        INSERT pattern зеркалит single-row upsert_source_manifest и
        upsert_artifact_manifest_many.
        """
        materialized = list(rows)
        if not materialized:
            return
        now = int(time.time())
        conn = self._connect()
        plain = [
            (
                self.project_name,
                r["source_type"],
                r["rel_path"],
                int(r["size"]),
                int(r["mtime_ns"]),
                r["content_hash"],
                json.dumps(r.get("emitted_qns") or [], ensure_ascii=False),
                now,
            )
            for r in materialized
            if r.get("full_scan_at") is None
        ]
        with_full_scan = [
            (
                self.project_name,
                r["source_type"],
                r["rel_path"],
                int(r["size"]),
                int(r["mtime_ns"]),
                r["content_hash"],
                json.dumps(r.get("emitted_qns") or [], ensure_ascii=False),
                now,
                int(r["full_scan_at"]),
            )
            for r in materialized
            if r.get("full_scan_at") is not None
        ]
        if plain:
            conn.executemany(
                """
                INSERT INTO source_manifest
                    (project_name, source_type, rel_path, size, mtime_ns, content_hash, emitted_qn_json, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_name, source_type, rel_path) DO UPDATE SET
                    size=excluded.size,
                    mtime_ns=excluded.mtime_ns,
                    content_hash=excluded.content_hash,
                    emitted_qn_json=excluded.emitted_qn_json,
                    last_seen_at=excluded.last_seen_at
                """,
                plain,
            )
        if with_full_scan:
            conn.executemany(
                """
                INSERT INTO source_manifest
                    (project_name, source_type, rel_path, size, mtime_ns, content_hash, emitted_qn_json, last_seen_at, last_seen_full_scan_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_name, source_type, rel_path) DO UPDATE SET
                    size=excluded.size,
                    mtime_ns=excluded.mtime_ns,
                    content_hash=excluded.content_hash,
                    emitted_qn_json=excluded.emitted_qn_json,
                    last_seen_at=excluded.last_seen_at,
                    last_seen_full_scan_at=excluded.last_seen_full_scan_at
                """,
                with_full_scan,
            )

    # ------------------------------------------------------------------
    # bsl_file_artifacts — per-BSL-file payload для scoped CALLS
    # ------------------------------------------------------------------

    def get_bsl_file_artifact(
        self, source_scope: str, rel_path: str
    ) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        row = conn.execute(
            "SELECT config_name, content_hash, routine_ids_json, module_ids_json, "
            "routines_index_json, callsites_json, form_links_json "
            "FROM bsl_file_artifacts "
            "WHERE project_name=? AND source_scope=? AND rel_path=?",
            (self.project_name, source_scope, rel_path),
        ).fetchone()
        if not row:
            return None
        return {
            "config_name": row[0],
            "content_hash": row[1],
            "routine_ids": json.loads(row[2]),
            "module_ids": json.loads(row[3]),
            "routines_index": json.loads(row[4]),
            "callsites": json.loads(row[5]),
            "form_links": json.loads(row[6]),
        }

    def all_bsl_file_artifacts(self, source_scope: str) -> List[Dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT rel_path, config_name, content_hash, routine_ids_json, module_ids_json, "
            "routines_index_json, callsites_json, form_links_json "
            "FROM bsl_file_artifacts "
            "WHERE project_name=? AND source_scope=?",
            (self.project_name, source_scope),
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({
                "rel_path": r[0],
                "config_name": r[1],
                "content_hash": r[2],
                "routine_ids": json.loads(r[3]),
                "module_ids": json.loads(r[4]),
                "routines_index": json.loads(r[5]),
                "callsites": json.loads(r[6]),
                "form_links": json.loads(r[7]),
            })
        return out

    def upsert_bsl_file_artifact(
        self,
        source_scope: str,
        config_name: str,
        rel_path: str,
        content_hash: str,
        routine_ids: List[str],
        module_ids: List[str],
        routines_index: List[Dict[str, Any]],
        callsites: List[Dict[str, Any]],
        form_links: List[Dict[str, Any]],
    ) -> None:
        now = int(time.time())
        conn = self._connect()
        conn.execute(
            """
            INSERT INTO bsl_file_artifacts
                (project_name, source_scope, config_name, rel_path, content_hash,
                 routine_ids_json, module_ids_json, routines_index_json,
                 callsites_json, form_links_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_name, source_scope, rel_path) DO UPDATE SET
                config_name=excluded.config_name,
                content_hash=excluded.content_hash,
                routine_ids_json=excluded.routine_ids_json,
                module_ids_json=excluded.module_ids_json,
                routines_index_json=excluded.routines_index_json,
                callsites_json=excluded.callsites_json,
                form_links_json=excluded.form_links_json,
                updated_at=excluded.updated_at
            """,
            (
                self.project_name,
                source_scope,
                config_name,
                rel_path,
                content_hash,
                json.dumps(routine_ids, ensure_ascii=False),
                json.dumps(module_ids, ensure_ascii=False),
                json.dumps(routines_index, ensure_ascii=False),
                json.dumps(callsites, ensure_ascii=False),
                json.dumps(form_links, ensure_ascii=False),
                now,
            ),
        )

    def delete_bsl_file_artifacts(
        self, source_scope: str, rel_paths: Iterable[str]
    ) -> None:
        conn = self._connect()
        rows = [(self.project_name, source_scope, rp) for rp in rel_paths]
        if not rows:
            return
        conn.executemany(
            "DELETE FROM bsl_file_artifacts "
            "WHERE project_name=? AND source_scope=? AND rel_path=?",
            rows,
        )

    def serialize_one_bsl_file_artifact(
        self,
        artifact: Any,
        *,
        now: Optional[int] = None,
    ) -> Tuple[Tuple[Any, ...], Dict[str, int]]:
        """Сериализовать один `BSLFileArtifact` в SQL-tuple + per-record stats.

        Чистая функция: **не мутирует** `artifact` (payload не очищается — это
        обязанность orchestrator-а после подтверждённой записи). `stats` содержит
        `routines/callsites/form_links/json_bytes` для одной записи; `json_bytes` —
        сумма len() фактических JSON-строк (мера размера батча).
        """
        get = (lambda k, default=None: artifact.get(k, default)) if isinstance(artifact, dict) else (lambda k, default=None: getattr(artifact, k, default))
        ts = int(time.time()) if now is None else int(now)
        routine_ids = get("routine_ids", []) or []
        module_ids = get("module_ids", []) or []
        routines_index = get("routines_index", []) or []
        callsites = get("callsites", []) or []
        form_links = get("form_links", []) or []
        routine_ids_json = json.dumps(routine_ids, ensure_ascii=False)
        module_ids_json = json.dumps(module_ids, ensure_ascii=False)
        routines_index_json = json.dumps(routines_index, ensure_ascii=False)
        callsites_json = json.dumps(callsites, ensure_ascii=False)
        form_links_json = json.dumps(form_links, ensure_ascii=False)
        stats: Dict[str, int] = {
            "routines": len(routines_index),
            "callsites": len(callsites),
            "form_links": len(form_links),
            "json_bytes": (
                len(routine_ids_json)
                + len(module_ids_json)
                + len(routines_index_json)
                + len(callsites_json)
                + len(form_links_json)
            ),
        }
        row = (
            self.project_name,
            get("source_scope"),
            get("config_name") or "",
            get("rel_path"),
            get("content_hash"),
            routine_ids_json,
            module_ids_json,
            routines_index_json,
            callsites_json,
            form_links_json,
            ts,
        )
        return row, stats

    def serialize_bsl_file_artifact_rows(
        self,
        artifacts: Iterable[Any],
        *,
        now: Optional[int] = None,
    ) -> Tuple[List[Tuple[Any, ...]], Dict[str, int]]:
        """Совместимая обёртка над `serialize_one_bsl_file_artifact` для небольших
        incremental-операций. Материализует все rows + агрегирует stats
        (artifacts, routines, callsites, form_links, json_bytes).

        Для baseline full reload использовать потоковый `iter_bsl_file_artifact_batches`.
        """
        materialized = list(artifacts)
        stats: Dict[str, int] = {
            "artifacts": len(materialized),
            "routines": 0,
            "callsites": 0,
            "form_links": 0,
            "json_bytes": 0,
        }
        if not materialized:
            return [], stats
        ts = int(time.time()) if now is None else int(now)
        rows: List[Tuple[Any, ...]] = []
        for a in materialized:
            row, one = self.serialize_one_bsl_file_artifact(a, now=ts)
            rows.append(row)
            stats["routines"] += one["routines"]
            stats["callsites"] += one["callsites"]
            stats["form_links"] += one["form_links"]
            stats["json_bytes"] += one["json_bytes"]
        return rows, stats

    def iter_bsl_file_artifact_batches(
        self,
        artifacts_list: List[Any],
        *,
        max_bytes: int = _ARTIFACT_BATCH_MAX_BYTES,
        max_rows: int = _ARTIFACT_BATCH_MAX_ROWS,
        now: Optional[int] = None,
    ) -> Iterator[Tuple[List[Tuple[Any, ...]], List[Any], Dict[str, int]]]:
        """Потоково сериализовать `artifacts_list` в батчи `(rows, consumed, stats)`.

        Деструктивно `pop()`-ит из переданного `list` (передача ownership: список
        пустеет по мере продвижения). Батч копится до `max_bytes` сериализованного
        JSON **или** `max_rows` строк, затем отдаётся. Артефакт с собственным
        `json_bytes > max_bytes` отдаётся отдельным батчем. `consumed` — исходные
        `BSLFileArtifact` этого батча, чтобы caller освободил их payload **после**
        подтверждённой записи. Сам генератор payload не очищает.
        """
        ts = int(time.time()) if now is None else int(now)

        def _empty_stats() -> Dict[str, int]:
            return {"artifacts": 0, "routines": 0, "callsites": 0, "form_links": 0, "json_bytes": 0}

        rows: List[Tuple[Any, ...]] = []
        consumed: List[Any] = []
        batch_bytes = 0
        batch_stats = _empty_stats()

        while artifacts_list:
            art = artifacts_list.pop()
            row, one = self.serialize_one_bsl_file_artifact(art, now=ts)
            one_bytes = one["json_bytes"]
            # Флашим текущий (непустой) батч до переполнения по байтам/строкам.
            if rows and (batch_bytes + one_bytes > max_bytes or len(rows) >= max_rows):
                yield rows, consumed, batch_stats
                rows, consumed, batch_bytes, batch_stats = [], [], 0, _empty_stats()
            rows.append(row)
            consumed.append(art)
            batch_bytes += one_bytes
            batch_stats["artifacts"] += 1
            batch_stats["routines"] += one["routines"]
            batch_stats["callsites"] += one["callsites"]
            batch_stats["form_links"] += one["form_links"]
            batch_stats["json_bytes"] += one_bytes
            # Oversized одиночный артефакт (или ровно заполнивший лимит) — отдельным батчем.
            if batch_bytes >= max_bytes or len(rows) >= max_rows:
                yield rows, consumed, batch_stats
                rows, consumed, batch_bytes, batch_stats = [], [], 0, _empty_stats()

        if rows:
            yield rows, consumed, batch_stats

    def upsert_bsl_file_artifacts_rows(
        self,
        rows: Iterable[Tuple[Any, ...]],
    ) -> None:
        """Записать уже подготовленные tuples в `bsl_file_artifacts`.

        Tuples должны соответствовать порядку колонок из `serialize_bsl_file_artifact_rows`.
        """
        materialized = list(rows)
        if not materialized:
            return
        conn = self._connect()
        conn.executemany(
            """
            INSERT INTO bsl_file_artifacts
                (project_name, source_scope, config_name, rel_path, content_hash,
                 routine_ids_json, module_ids_json, routines_index_json,
                 callsites_json, form_links_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_name, source_scope, rel_path) DO UPDATE SET
                config_name=excluded.config_name,
                content_hash=excluded.content_hash,
                routine_ids_json=excluded.routine_ids_json,
                module_ids_json=excluded.module_ids_json,
                routines_index_json=excluded.routines_index_json,
                callsites_json=excluded.callsites_json,
                form_links_json=excluded.form_links_json,
                updated_at=excluded.updated_at
            """,
            materialized,
        )

    def upsert_bsl_file_artifacts_many(
        self,
        artifacts: Iterable[Any],
    ) -> None:
        """Batch upsert per-file BSL artifacts.

        Тонкая обёртка над `serialize_bsl_file_artifact_rows` +
        `upsert_bsl_file_artifacts_rows`. Соединение в autocommit — для атомарности
        нескольких write-вызовов вместе caller оборачивает их в
        `with state.transaction():`.
        """
        rows, _stats = self.serialize_bsl_file_artifact_rows(artifacts)
        self.upsert_bsl_file_artifacts_rows(rows)

    # ---- bsl_deferred_changes — per-scope BSL diff carried across cycles when
    # inline drain of pending scoped code-search apply could not complete and
    # `_diff_scope` (full-reconcile only) discovered `diff.deleted`. Split read/clear:
    # cleared only after successful BSL apply, so a crash between read and apply
    # preserves the deferred row for the next cycle (graph operations are idempotent).

    def defer_bsl_changes_for_next_cycle(
        self, project_name: str, source_scope: str, diff: Any,
    ) -> None:
        payload = {
            "added": [str(p) for p in (diff.added or [])],
            "changed": [str(p) for p in (diff.changed or [])],
            "deleted": list(diff.deleted or []),
        }
        conn = self._connect()
        conn.execute(
            "INSERT INTO bsl_deferred_changes(project_name, source_scope, deferred_at, diff_json) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(project_name, source_scope) DO UPDATE SET "
            "    deferred_at = excluded.deferred_at, "
            "    diff_json = excluded.diff_json",
            (project_name, source_scope, int(time.time()),
             json.dumps(payload, ensure_ascii=False)),
        )

    def read_deferred_bsl_diff(
        self, project_name: str, source_scope: str,
    ) -> Optional[Dict[str, Any]]:
        """Returns {'added': [paths], 'changed': [paths], 'deleted': [rel_paths]}
        or None if no row. Does NOT clear — see `clear_deferred_bsl_diff`."""
        conn = self._connect()
        row = conn.execute(
            "SELECT diff_json FROM bsl_deferred_changes "
            "WHERE project_name = ? AND source_scope = ?",
            (project_name, source_scope),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0] or "{}")
        except Exception:
            return None
        return {
            "added": list(payload.get("added", []) or []),
            "changed": list(payload.get("changed", []) or []),
            "deleted": list(payload.get("deleted", []) or []),
        }

    def clear_deferred_bsl_diff(
        self, project_name: str, source_scope: str,
    ) -> None:
        conn = self._connect()
        conn.execute(
            "DELETE FROM bsl_deferred_changes "
            "WHERE project_name = ? AND source_scope = ?",
            (project_name, source_scope),
        )


# ----------------------------------------------------------------------
# LockLease — main-thread-only heartbeat helper для scheduler_lock
# ----------------------------------------------------------------------


class LockLease:
    """Helper для удержания `scheduler_lock` во время длинных artifact фаз.

    Thread-ownership: `heartbeat()` обязан вызываться ТОЛЬКО из главного scheduler
    thread-а (того же, что владеет `IncrementalLoadingState` instance). Это сохраняет
    инвариант state.py: `IncrementalLoadingState` не thread-safe для одного instance.

    BSL `BSLProcessor.finalize(lease=lease)` вызывает heartbeat между poll-итерациями
    своего main-thread wait-loop — не через callback в worker/collector thread.
    """

    def __init__(self, state: "IncrementalLoadingState", name: str, owner: str) -> None:
        self._state = state
        self._name = name
        self._owner = owner

    def heartbeat(self) -> None:
        try:
            self._state.heartbeat_lock(self._name, self._owner)
        except Exception:
            # Heartbeat не должен ронять цикл; следующая итерация попробует снова.
            logger.debug("LockLease.heartbeat failed", exc_info=True)
