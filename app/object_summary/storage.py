"""File storage for object_summary artifacts.

Path scheme:
  {settings.object_summary_directory}/{config_name}/{category}/{object_name}/
    profile.toon
    summary.json
    summary.md
    meta.json

`summary.json` is the source of truth; Neo4j only stores the path, the
embedding and the search text. `meta.json` carries version stamps used by S0
reconcile (see `object_summary_pipeline`).

All writes go through `atomic_write_text` — a temporary file in the same
directory followed by `os.replace`, so an interrupted run cannot leave a
half-written `summary.json` visible to readers or to a future S0 cycle.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

from config import settings

logger = logging.getLogger(__name__)

PROFILE_FILE = "profile.toon"
SUMMARY_JSON_FILE = "summary.json"
SUMMARY_MD_FILE = "summary.md"
META_FILE = "meta.json"

HISTORY_DIR = "_history"
PUBLISH_ID_FIELD = "_publish_id"

_ARTIFACT_FILES = (PROFILE_FILE, SUMMARY_JSON_FILE, SUMMARY_MD_FILE, META_FILE)


_FS_SAFE_RE = re.compile(r"[<>:\"|?*\\/\x00-\x1f]")


def _sanitize(name: str) -> str:
    """Replace characters that are illegal on Windows/Linux file systems."""
    cleaned = _FS_SAFE_RE.sub("_", (name or "").strip())
    return cleaned or "_"


def object_dir(config_name: str, category: str, object_name: str) -> Path:
    base = settings.object_summary_directory
    return base / _sanitize(config_name) / _sanitize(category) / _sanitize(object_name)


def summary_json_path(config_name: str, category: str, object_name: str) -> Path:
    return object_dir(config_name, category, object_name) / SUMMARY_JSON_FILE


def summary_md_path(config_name: str, category: str, object_name: str) -> Path:
    return object_dir(config_name, category, object_name) / SUMMARY_MD_FILE


def profile_path(config_name: str, category: str, object_name: str) -> Path:
    return object_dir(config_name, category, object_name) / PROFILE_FILE


def meta_path(config_name: str, category: str, object_name: str) -> Path:
    return object_dir(config_name, category, object_name) / META_FILE


def summary_exists(path: str | os.PathLike[str]) -> bool:
    try:
        return Path(path).is_file()
    except OSError:
        return False


def atomic_write_text(target: Path, content: str) -> None:
    """Write `content` to `target` atomically.

    Uses a NamedTemporaryFile in the same directory and `os.replace` so the
    file is either fully present with the new content or unchanged from before.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(target: Path, payload: Any) -> None:
    atomic_write_text(target, json.dumps(payload, ensure_ascii=False, indent=2))


def read_json(target: Path) -> Optional[Dict[str, Any]]:
    try:
        with target.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Cannot read %s: %s", target, exc)
        return None
    return data if isinstance(data, dict) else None


def read_text(target: Path) -> Optional[str]:
    try:
        return target.read_text(encoding="utf-8")
    except OSError:
        return None


def iter_summary_dirs_on_disk() -> Iterator[Tuple[str, str, str, Path]]:
    """Yield (config_name, category, object_name, dir_path) candidates.

    Walks `{settings.object_summary_directory}/{config_name}/{category}/
    {object_name}/` three levels deep. Yields only directories that contain
    both `summary.json` and `meta.json`. Skips `_history` and any name
    starting with `_old_`. Source of truth for graph linkage is
    `meta.object_qualified_name` — the folder names are used only to locate
    the files.
    """
    base = settings.object_summary_directory
    try:
        if not base.is_dir():
            return
    except OSError:
        return

    def _skip(name: str) -> bool:
        return name == HISTORY_DIR or name.startswith("_old_")

    try:
        config_entries = list(base.iterdir())
    except OSError:
        return
    for config_dir in config_entries:
        if not config_dir.is_dir() or _skip(config_dir.name):
            continue
        try:
            category_entries = list(config_dir.iterdir())
        except OSError:
            continue
        for category_dir in category_entries:
            if not category_dir.is_dir() or _skip(category_dir.name):
                continue
            try:
                object_entries = list(category_dir.iterdir())
            except OSError:
                continue
            for object_dir_ in object_entries:
                if not object_dir_.is_dir() or _skip(object_dir_.name):
                    continue
                summary = object_dir_ / SUMMARY_JSON_FILE
                meta = object_dir_ / META_FILE
                if summary.is_file() and meta.is_file():
                    yield (
                        config_dir.name,
                        category_dir.name,
                        object_dir_.name,
                        object_dir_,
                    )


def quarantine_configuration(config_name: str) -> Optional[Path]:
    """Атомарно увести каталог конфигурации в `_old_<config>_<UTC-stamp>`.

    Нужно при полном удалении расширения. Сами файлы не удаляются, но
    перестают быть кандидатами для S0 bootstrap: `iter_summary_dirs_on_disk`
    уже пропускает всё, что начинается с `_old_`. Без этого при повторном
    появлении расширения с тем же QN S0 переподключил бы устаревшую сводку к
    новому объекту — он ищет по `meta.object_qualified_name`, а не по времени.

    Возвращает путь карантина или None, если каталога не было (идемпотентный
    no-op). Содержимое не удаляется — retention/GC это отдельная задача.
    """
    base = settings.object_summary_directory
    sanitized = _sanitize(config_name)
    target = base / sanitized

    # Containment: `_sanitize` уже вырезает разделители пути, но проверка
    # обязательна — от неё зависит, что мы не переместим каталог вне корня.
    try:
        base_resolved = base.resolve()
        target_resolved = target.resolve()
        if target_resolved == base_resolved or base_resolved not in target_resolved.parents:
            logger.error(
                "quarantine_configuration: refusing to touch %s — outside %s",
                target, base,
            )
            return None
    except OSError as exc:
        logger.warning("quarantine_configuration: cannot resolve %s: %s", target, exc)
        return None

    if not target.is_dir():
        return None

    base_stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    quarantine = base / f"_old_{sanitized}_{base_stamp}"
    suffix = 0
    while quarantine.exists():
        suffix += 1
        quarantine = base / f"_old_{sanitized}_{base_stamp}-{suffix:02d}"

    # os.replace, а не shutil.move: перемещение внутри одного тома должно быть
    # атомарным, иначе прерванный cleanup оставил бы половину сводок видимой.
    os.replace(target, quarantine)
    logger.info(
        "Object summaries quarantined: %s -> %s", target.name, quarantine.name
    )
    return quarantine


def archive_summary_files(
    config_name: str, category: str, object_name: str
) -> Optional[Path]:
    """Copy the four current artifact files into _history/<UTC-stamp>/.

    Returns the created archive directory or None when no source file exists.
    On timestamp collision appends suffix -01, -02, ...
    """
    src_dir = object_dir(config_name, category, object_name)
    if not src_dir.is_dir():
        return None
    present = [name for name in _ARTIFACT_FILES if (src_dir / name).is_file()]
    if not present:
        return None

    base_stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    history_root = src_dir / HISTORY_DIR
    history_root.mkdir(parents=True, exist_ok=True)

    archive_dir = history_root / base_stamp
    suffix = 0
    while archive_dir.exists():
        suffix += 1
        archive_dir = history_root / f"{base_stamp}-{suffix:02d}"
    archive_dir.mkdir(parents=True, exist_ok=False)

    for name in present:
        try:
            shutil.copy2(src_dir / name, archive_dir / name)
        except OSError as exc:
            logger.warning("archive_summary_files: failed to copy %s: %s", name, exc)
    return archive_dir


def restore_summary_files_from_archive(archive_dir: Path, target_dir: Path) -> int:
    """Best-effort restore of the four artifact files from archive_dir.

    Each `shutil.copy2` failure is logged but does not abort the rest.
    Returns the number of files successfully restored. The caller is
    expected to treat a return value < len(_ARTIFACT_FILES) as a
    cascade-failure case.
    """
    restored = 0
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in _ARTIFACT_FILES:
        src = archive_dir / name
        if not src.is_file():
            continue
        try:
            shutil.copy2(src, target_dir / name)
            restored += 1
        except OSError as exc:
            logger.warning(
                "restore_summary_files_from_archive: failed to restore %s: %s", name, exc
            )
    return restored
