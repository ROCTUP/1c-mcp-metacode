"""
XML incremental walker (stage 1).

Обходит metadata XML descriptors, фильтрует candidate files по mtime+content_hash,
группирует по owner_object_qn, перепарсивает affected object-scope (root + Forms + Templates + Commands).

Также реализует full XML scan (поиск удалённых XML по разнице с source_manifest).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .hashing import compute_file_hash
from .metadata_sync import (
    _apply_ext_removed,
    _configuration_qn,
    _refresh_extension_links_scoped,
    apply_configuration_diff,
    apply_deleted_object,
    diff_and_apply_configuration,
)
from .report import IncrementalReport
from .state import IncrementalLoadingState

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------
# BaseImpact — контракт overlay refresh для XML расширений
# --------------------------------------------------------------------


@dataclass(slots=True)
class BaseImpact:
    """Полный набор изменений базы, влияющих на XML overlay расширений.

    Заполняется в scheduler-е после base sync + base full-scan и передаётся в
    `xml_incremental_run_extensions` / `xml_full_scan_run_extensions`.

    `base_configuration` намеренно отсутствует: единственный канал base config
    для overlay расширений — `ScopedBaseOverlayProvider` через `XmlCycleContext`.
    """

    added_qns: Set[str] = field(default_factory=set)
    changed_qns: Set[str] = field(default_factory=set)
    deleted_qns: Set[str] = field(default_factory=set)  # включая deletions из base full-scan
    configuration_changed: bool = False

    @property
    def has_object_impact(self) -> bool:
        return bool(self.added_qns or self.changed_qns or self.deleted_qns)


# --------------------------------------------------------------------
# QN projection: base QN ↔ extension QN
# --------------------------------------------------------------------


def _project_base_qn_to_ext(
    base_qn: str,
    project_name: str,
    base_config_name: str,
    ext_graph_config_name: str,
) -> Optional[str]:
    """String-replace `config_name` сегмента в QN.

    Используется overlay refresh для проекции base impact на extension scope.
    Сохраняет вложенные подсистемы (path-based QN).
    """
    prefix = f"{project_name}/{base_config_name}/"
    if not base_qn.startswith(prefix):
        return None
    return f"{project_name}/{ext_graph_config_name}/" + base_qn[len(prefix) :]


def _project_ext_qn_to_base(
    ext_qn: str,
    project_name: str,
    base_config_name: str,
    ext_graph_config_name: str,
) -> Optional[str]:
    """Inverse of `_project_base_qn_to_ext` — для scoped overlay provider."""
    prefix = f"{project_name}/{ext_graph_config_name}/"
    if not ext_qn.startswith(prefix):
        return None
    return f"{project_name}/{base_config_name}/" + ext_qn[len(prefix) :]


def _collect_adopted_base_qns(
    scoped_config: Any, project_name: str, base_config_name: str
) -> Set[str]:
    """Для full-init: вернуть base QN всех adopted объектов parsed ext-конфигурации.

    Использует существующий `_object_qn` из `metadata_sync` — это критично для
    `Подсистемы`, у которых QN path-based из `obj.properties["ПутьПодсистемы"]`.
    Без этого `_files_for_object` не найдёт base XML вложенной подсистемы.
    """
    from .metadata_sync import _object_qn

    result: Set[str] = set()
    if scoped_config is None:
        return result
    for category in getattr(scoped_config, "categories", []):
        for obj in getattr(category, "metadata_objects", []):
            if not _is_adopted_props(getattr(obj, "properties", None) or {}):
                continue
            result.add(_object_qn(project_name, base_config_name, category.name, obj))
    return result


def _is_adopted_props(props: Dict[str, Any]) -> bool:
    """Зеркало parser._is_adopted_props (избегаем cross-import)."""
    val = (props or {}).get("ПринадлежностьОбъекта")
    return val in ("Заимствованный", "Adopted")


# --------------------------------------------------------------------
# Owner object QN resolution
# --------------------------------------------------------------------


def _resolve_owner_object_qn(
    rel_path: str, project_name: str, config_name: str
) -> Optional[str]:
    """Сопоставить XML file path с object_qn владельца.

    Использует FOLDER_TO_RU_CATEGORY и path segments. None если файл не привязан к owner
    (например, Configuration.xml, CommonModules без object).

    Для подсистем (`Subsystems/`) поддерживает path-based QN с chain — XML дамп размещает
    вложенные подсистемы в `Subsystems/<A>/Subsystems/<B>/B.xml`, что симметрично graph QN
    `project/config/Подсистемы/A/B` (см. RowsBuilderMixin).
    """
    from xml_metadata.folder_map import FOLDER_TO_RU_CATEGORY

    parts = rel_path.replace("\\", "/").strip("/").split("/")
    if len(parts) < 2:
        return None
    folder = parts[0]
    if folder not in FOLDER_TO_RU_CATEGORY:
        return None
    category_ru = FOLDER_TO_RU_CATEGORY[folder]
    if folder == "Subsystems":
        # Extract chain from path segments: Subsystems/A/Subsystems/B/B.xml → chain=[A, B].
        # Top-level case `Subsystems/A.xml` → chain=[A].
        # Nested with descriptor `Subsystems/A/Subsystems/B.xml` → chain=[A, B].
        chain: list = []
        i = 1
        while i < len(parts):
            segment = parts[i]
            if segment.endswith(".xml"):
                segment = segment[: -len(".xml")]
            chain.append(segment)
            # next segment should be either "Subsystems" (recurse) or filename — break either way.
            if i + 1 < len(parts) and parts[i + 1] == "Subsystems":
                i += 2  # skip "Subsystems" wrapper
            else:
                break
        if not chain:
            return None
        return f"{project_name}/{config_name}/{category_ru}/" + "/".join(chain)
    # Top-level descriptor `<Category>/<Object>.xml` → object name = filename stem;
    # nested file `<Category>/<Object>/Forms/<F>.xml` → object name = directory segment.
    object_name = parts[1]
    if object_name.endswith(".xml"):
        object_name = object_name[: -len(".xml")]
    return f"{project_name}/{config_name}/{category_ru}/{object_name}"


def _is_configuration_xml(rel_path: str) -> bool:
    return rel_path.replace("\\", "/").strip("/") == "Configuration.xml"


def _is_object_root_descriptor(
    rel_path: str, owner_qn: Optional[str], config_name: str
) -> bool:
    """Корневой ли это descriptor самого объекта (его удаление = удаление объекта)
    или это вложенный child (Form/Command/Template/StorageGroup и т.п., чьё удаление
    не приводит к удалению owner-объекта).

    Опирается на ту же логику что `_resolve_owner_object_qn`:
    - `Configuration.xml` → False (это не object root).
    - Top-level `<Category>/<Object>.xml` (ровно 2 path segment-а) → True.
    - `Subsystems`: root формы — `Subsystems/A.xml` или вложенные
      `Subsystems/A/Subsystems/B.xml` и т.д. Финальный сегмент — `<Name>.xml`,
      каждый промежуточный шаг — пара `<Name>` + `Subsystems` (как в
      `_resolve_owner_object_qn`).
    - Любая другая глубина (`<Category>/<Object>/<SubDir>/...`) → False.
    """
    from xml_metadata.folder_map import FOLDER_TO_RU_CATEGORY

    norm = rel_path.replace("\\", "/").strip("/")
    if not norm:
        return False
    if norm == "Configuration.xml":
        return False
    parts = norm.split("/")
    if len(parts) < 2:
        return False
    folder = parts[0]
    if folder not in FOLDER_TO_RU_CATEGORY:
        return False
    if folder == "Subsystems":
        # Шаги: <Name>, [Subsystems, <Name>]*, последний <Name>.xml.
        if not parts[-1].endswith(".xml"):
            return False
        i = 1
        while i < len(parts) - 1:
            # Промежуточный шаг: <Name>, далее обязательно "Subsystems".
            if i + 1 >= len(parts):
                return False
            if parts[i + 1] != "Subsystems":
                return False
            i += 2
        return i == len(parts) - 1
    # Top-level <Category>/<Object>.xml — ровно 2 сегмента.
    return len(parts) == 2 and parts[1].endswith(".xml")


# --------------------------------------------------------------------
# Full XML scan window check
# --------------------------------------------------------------------


def _within_full_reconcile_window(now: datetime, start_str: str, end_str: str) -> bool:
    """Проверка попадания now в окно (HH:MM формат)."""
    try:
        start = dtime.fromisoformat(start_str)
        end = dtime.fromisoformat(end_str)
    except Exception:
        return False
    now_t = now.time()
    if start <= end:
        return start <= now_t <= end
    # wrap around midnight
    return now_t >= start or now_t <= end


# --------------------------------------------------------------------
# Configuration name detection
# --------------------------------------------------------------------


def _detect_configuration_name(code_directory: Path) -> Optional[str]:
    """Best-effort: имя Configuration из <Configuration.xml> или из имени корня."""
    config_xml = code_directory / "Configuration.xml"
    if config_xml.exists():
        try:
            import xml.etree.ElementTree as ET

            tree = ET.parse(str(config_xml))
            root = tree.getroot()
            # Имена 1С обычно через <Properties><Name>...</Name></Properties>.
            for elem in root.iter():
                tag = elem.tag.rsplit("}", 1)[-1] if "}" in elem.tag else elem.tag
                if tag == "Name" and elem.text:
                    return elem.text.strip()
        except Exception:
            pass
    return code_directory.name


# --------------------------------------------------------------------
# Parse helpers
# --------------------------------------------------------------------


def _parse_xml_full(code_directory: Path, project_name: str) -> Any:
    """Полный parse XML конфигурации.

    Только для full reload/baseline и аварийного recovery. В штатном incremental
    цикле использовать запрещено — используйте `ScopedBaseOverlayProvider`
    через `XmlCycleContext`.

    Возвращает первый Configuration или None.
    """
    from indexer.code_file_index import CodeFileIndexer
    from indexer.metadata_loader import MetadataLoader

    ml = MetadataLoader()
    code_index = CodeFileIndexer.scan(code_directory)
    if code_index is None:
        return None
    configs = ml.load_configurations(code_directory, code_index=code_index, source="xml")
    return configs[0] if configs else None


def _parse_xml_full_extension(
    ext_code_dir: Path, base_configuration: Optional[Any]
) -> Optional[Any]:
    """Полный parse XML расширения с `is_extension=True` + overlay.

    Только для full reload/baseline и аварийного recovery. В штатном incremental
    цикле использовать запрещено — full XML scan расширений делает scoped reparse
    через `xml_context`.

    `is_extension=True` ОБЯЗАТЕЛЕН: иначе `ext_config.name` не получит `$ext$`-суффикса,
    parsed QN не совпадут со state QN scope-а `xml_ext:*`.

    `apply_extension_base_overlay` тоже обязателен: baseline расширения создаётся
    после overlay в полной загрузке (extensions_loader.py:231-234), и full parse
    без overlay даст `compute_object_hash` другой → diff даст false-positive churn.
    """
    from indexer.code_file_index import CodeFileIndexer
    from indexer.metadata_loader import MetadataLoader

    code_index = CodeFileIndexer.scan(ext_code_dir)
    if code_index is None or getattr(code_index, "config_xml", None) is None:
        return None
    ml = MetadataLoader()
    configs = ml.load_configurations(
        ext_code_dir, code_index=code_index, source="xml", is_extension=True
    )
    if not configs:
        return None
    ext_config = configs[0]
    if base_configuration is not None:
        try:
            from xml_metadata import apply_extension_base_overlay

            apply_extension_base_overlay(ext_config, base_configuration)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "_parse_xml_full_extension: apply_extension_base_overlay failed: %s", e
            )
    return ext_config


def _parse_xml_scoped_extension(
    ext_code_dir: Path,
    files: List[Path],
) -> Optional[Any]:
    """Object-scope parse XML расширения с `is_extension=True`.

    Аналог `_parse_xml_scoped`, но root = `ext_code_dir`, и `is_extension=True`
    чтобы парсер добавил `$ext$` и проставил ownership stamping.
    """
    if not files:
        return None
    from config import resolve_xml_standard_attributes_mode, settings as _settings
    from xml_metadata import XmlMetadataParser

    xml_materialize, xml_preserve = resolve_xml_standard_attributes_mode(
        _settings.xml_standard_attributes_mode
    )
    configs = XmlMetadataParser(
        materialize_standard_attributes=xml_materialize,
        preserve_listed_standard_attributes=xml_preserve,
    ).parse_files(files, ext_code_dir, is_extension=True)
    return configs[0] if configs else None


def _parse_xml_scoped(
    code_directory: Path,
    files: List[Path],
) -> Any:
    """Object-scope parse XML — передаёт ТОЛЬКО подмножество файлов в XmlMetadataParser.

    Возвращает первый Configuration с парсингом только переданных descriptor-ов.
    Используется обычным XML incremental run для object-scope reparse.
    """
    if not files:
        return None
    from config import resolve_xml_standard_attributes_mode, settings
    from xml_metadata import XmlMetadataParser

    xml_materialize, xml_preserve = resolve_xml_standard_attributes_mode(
        settings.xml_standard_attributes_mode
    )
    configs = XmlMetadataParser(
        materialize_standard_attributes=xml_materialize,
        preserve_listed_standard_attributes=xml_preserve,
    ).parse_files(files, code_directory, is_extension=False)
    return configs[0] if configs else None


def _files_for_object(
    code_directory: Path,
    metadata_xml_files: List[Path],
    object_qn: str,
    project_name: str,
    config_name: str,
) -> List[Path]:
    """Собрать все XML descriptors объекта (root + nested) из code_index.

    Использует обратный mapping через `_resolve_owner_object_qn`.
    """
    result: List[Path] = []
    for xml_path in metadata_xml_files:
        try:
            rel_path = str(xml_path.relative_to(code_directory)).replace("\\", "/")
        except ValueError:
            continue
        owner_qn = _resolve_owner_object_qn(rel_path, project_name, config_name)
        if owner_qn == object_qn:
            result.append(xml_path)
    return result


# --------------------------------------------------------------------
# Per-cycle context + scoped base overlay provider
# --------------------------------------------------------------------


class ScopedBaseOverlayProvider:
    """Scoped parse базы под overlay расширений.

    Не владеет scan-ом — берёт `code_index` у `XmlCycleContext`. Кэш по
    sorted tuple(base_qns). `get_configuration_only()` — отдельный путь
    для `INHERITED_EXTENSION_CONFIGURATION_PROPS` merge без object overlay.
    """

    def __init__(self, context: "XmlCycleContext") -> None:
        self._ctx = context
        self._cache: Dict[Tuple[Any, ...], Optional[Any]] = {}
        self._config_only: Any = None
        self._config_only_loaded: bool = False
        self._stats_scoped_parses: int = 0
        self._stats_objects: int = 0
        self._stats_files: int = 0

    def get_scoped_base(
        self, base_qns: Set[str], include_configuration: bool = True
    ) -> Optional[Any]:
        key = (include_configuration, tuple(sorted(base_qns)))
        if key in self._cache:
            return self._cache[key]
        files: List[Path] = []
        if include_configuration:
            cfg_xml = self._ctx.code_directory / "Configuration.xml"
            if cfg_xml.exists():
                files.append(cfg_xml)
        if base_qns:
            metadata_files = self._ctx.metadata_xml_files
            for qn in base_qns:
                for f in _files_for_object(
                    self._ctx.code_directory,
                    metadata_files,
                    qn,
                    self._ctx.project_name,
                    self._ctx.base_config_name,
                ):
                    if f not in files:
                        files.append(f)
        if not files:
            self._cache[key] = None
            return None
        parsed = _parse_xml_scoped(self._ctx.code_directory, files)
        self._cache[key] = parsed
        self._stats_scoped_parses += 1
        self._stats_objects += len(base_qns)
        self._stats_files += len(files)
        return parsed

    def get_configuration_only(self) -> Optional[Any]:
        if self._config_only_loaded:
            return self._config_only
        cfg_xml = self._ctx.code_directory / "Configuration.xml"
        if not cfg_xml.exists():
            self._config_only = None
            self._config_only_loaded = True
            return None
        self._config_only = _parse_xml_scoped(self._ctx.code_directory, [cfg_xml])
        self._config_only_loaded = True
        if self._config_only is not None:
            self._stats_scoped_parses += 1
            self._stats_files += 1
        return self._config_only

    def stats(self) -> Dict[str, int]:
        return {
            "scoped_parses": self._stats_scoped_parses,
            "objects_parsed": self._stats_objects,
            "files_parsed": self._stats_files,
        }


class XmlCycleContext:
    """Per-cycle owner XML-инкрементала.

    Один `CodeFileIndexer.scan(base)` за весь tick + lazy-кэш
    `CodeFileIndexer.scan(ext_code_dir)` per extension (incremental и full-scan
    фазы расширения шарят один scan через `get_extension_code_index`).

    Создаётся в `MetadataIncrementalSync._sync_xml_impl` и пробрасывается через
    `IncrementalReport.xml_context` в `_run_xml_extensions_phase`.
    """

    __slots__ = (
        "code_directory",
        "project_name",
        "base_config_name",
        "use_startup_probe_for_vectors",
        "_code_index",
        "_code_index_loaded",
        "_ext_indexes",
        "_overlay_provider",
        "_base_scans",
        "_ext_scans",
    )

    def __init__(
        self, code_directory: Path, project_name: str, base_config_name: str,
        *, use_startup_probe_for_vectors: bool = False,
    ) -> None:
        self.code_directory = code_directory
        self.project_name = project_name
        self.base_config_name = base_config_name
        # Startup XML cycles set this so vector DDL (via create_indexes inside
        # the config-only apply_configuration_diff and object-scope
        # diff_and_apply_configuration) uses the bounded embedding probe.
        self.use_startup_probe_for_vectors = bool(use_startup_probe_for_vectors)
        self._code_index: Any = None
        self._code_index_loaded: bool = False
        self._ext_indexes: Dict[str, Any] = {}
        self._overlay_provider: Optional[ScopedBaseOverlayProvider] = None
        self._base_scans: int = 0
        self._ext_scans: int = 0

    @property
    def code_index(self) -> Any:
        if not self._code_index_loaded:
            from indexer.code_file_index import CodeFileIndexer

            self._code_index = CodeFileIndexer.scan(self.code_directory)
            self._code_index_loaded = True
            self._base_scans += 1
        return self._code_index

    @property
    def metadata_xml_files(self) -> List[Path]:
        idx = self.code_index
        if idx is None:
            return []
        return list(getattr(idx, "metadata_xml_files", []) or [])

    @property
    def overlay_provider(self) -> ScopedBaseOverlayProvider:
        if self._overlay_provider is None:
            self._overlay_provider = ScopedBaseOverlayProvider(self)
        return self._overlay_provider

    def get_extension_code_index(self, ext_code_dir: Path) -> Any:
        """Lazy-кэш scan-а каталога расширения. Один scan на ext root за cycle."""
        key = _normalize_path_key(ext_code_dir)
        if key in self._ext_indexes:
            return self._ext_indexes[key]
        from indexer.code_file_index import CodeFileIndexer

        idx = CodeFileIndexer.scan(ext_code_dir)
        self._ext_indexes[key] = idx
        self._ext_scans += 1
        return idx

    def base_scans_count(self) -> int:
        return self._base_scans

    def ext_scans_count(self) -> int:
        return self._ext_scans


def _normalize_path_key(p: Path) -> str:
    """Нормализованный ключ кэша Path: resolve + normcase для Windows."""
    try:
        resolved = p.resolve(strict=False)
    except OSError:
        resolved = p
    import os as _os

    return _os.path.normcase(str(resolved))


# --------------------------------------------------------------------
# XML incremental run (ordinary, by watermark + content_hash)
# --------------------------------------------------------------------


def xml_incremental_run(
    *,
    loader: Any,
    state: IncrementalLoadingState,
    settings_obj: Any,
    report: IncrementalReport,
    xml_context: Optional[XmlCycleContext] = None,
) -> None:
    """Обычный XML incremental run (object-scope).

    Алгоритм:
    1. watermark_ns = stage_state.watermark_ns - OVERLAP * 1e9.
    2. Обход metadata XML files.
    3. Candidate files: mtime_ns >= watermark_ns AND sha256(file) != manifest.content_hash.
    4. Configuration.xml — особый случай: apply_configuration_diff.
    5. Группировка по owner_object_qn → affected_objects.
    6. Для каждого affected_object — перепарсить весь набор XML descriptors объекта.
    7. Применить scoped diff (deleted НЕ детектируется).
    """
    project_name = settings_obj.project_name
    code_directory = Path(settings_obj.code_directory)

    if xml_context is not None:
        config_name = xml_context.base_config_name or "Configuration"
    else:
        config_name = _detect_configuration_name(code_directory) or "Configuration"

    # 1. Watermark.
    prev_stage = state.get_stage_source_type("metadata_xml")
    if prev_stage is None:
        report.notes.append("no baseline for metadata_xml stage; skipping")
        return
    now_ns = time.time_ns()

    # 2-3. Обход metadata XML files — переиспользуем scan из xml_context, если он есть.
    if xml_context is not None:
        code_index = xml_context.code_index
    else:
        from indexer.code_file_index import CodeFileIndexer

        code_index = CodeFileIndexer.scan(code_directory)
    if code_index is None or not getattr(code_index, "metadata_xml_files", None):
        report.notes.append("no metadata XML files")
        return

    # Batch-read всего manifest для XML scope: один SELECT вместо N per-file.
    manifest_by_rel = state.get_source_manifest_map("xml")

    candidate_files: List[Path] = []
    has_config_change = False
    affected_objects: Set[str] = set()
    pending_manifest_rows: List[Dict[str, Any]] = []
    # discovery_info хранит уже посчитанные size/mtime_ns/content_hash/owner_qn для
    # changed файлов, чтобы финальный manifest update не делал второй stat+read+hash.
    discovery_info: Dict[str, Dict[str, Any]] = {}

    files_total = 0
    unchanged_count = 0
    hashed_count = 0
    changed_count = 0
    discovery_start = time.perf_counter()

    for xml_path in code_index.metadata_xml_files:
        files_total += 1
        rel_path = str(xml_path.relative_to(code_directory)).replace("\\", "/")
        try:
            stat = xml_path.stat()
        except OSError:
            continue
        mtime_ns = stat.st_mtime_ns
        size = stat.st_size

        manifest = manifest_by_rel.get(rel_path)
        if (
            manifest is not None
            and manifest["mtime_ns"] == mtime_ns
            and manifest["size"] == size
        ):
            unchanged_count += 1
            continue
        try:
            data = xml_path.read_bytes()
        except OSError:
            continue
        content_hash = compute_file_hash(data)
        hashed_count += 1
        if manifest is not None and manifest["content_hash"] == content_hash:
            # Mtime touched but content same — добавим в pending batch (без rename
            # семантики в base scope `emitted_qns` валидны как есть).
            pending_manifest_rows.append({
                "source_type": "xml",
                "rel_path": rel_path,
                "size": size,
                "mtime_ns": mtime_ns,
                "content_hash": content_hash,
                "emitted_qns": manifest.get("emitted_qns") or [],
            })
            continue
        # Real change.
        changed_count += 1
        candidate_files.append(xml_path)
        if _is_configuration_xml(rel_path):
            has_config_change = True
            owner_qn: Optional[str] = None
        else:
            owner_qn = _resolve_owner_object_qn(rel_path, project_name, config_name)
            if owner_qn:
                affected_objects.add(owner_qn)
        discovery_info[rel_path] = {
            "size": size,
            "mtime_ns": mtime_ns,
            "content_hash": content_hash,
            "owner_qn": owner_qn,
        }

    discovery_duration_ms = int((time.perf_counter() - discovery_start) * 1000)

    # 4. Object-scope reparse — собираем минимально-необходимый набор XML файлов.
    #    Не делаем full parse всего metadata_xml_files (это был бы full reload по стоимости).
    #
    #    `Configuration.xml` ВСЕГДА включается в scoped_files когда есть что парсить —
    #    иначе scoped Configuration возвращается с `properties={}` и `name=root.name`,
    #    что (а) триггерит `apply_configuration_diff` стирая properties; (б) ломает
    #    object QN если имя папки != имя конфигурации.
    scoped_files: List[Path] = []
    config_xml = code_directory / "Configuration.xml"
    needs_parse = has_config_change or bool(affected_objects)
    if needs_parse and config_xml.exists():
        scoped_files.append(config_xml)
    if affected_objects:
        for obj_qn in affected_objects:
            scoped_files.extend(
                _files_for_object(
                    code_directory,
                    list(code_index.metadata_xml_files),
                    obj_qn,
                    project_name,
                    config_name,
                )
            )

    if scoped_files:
        # XmlMetadataParser.parse_files merge-ит descriptors одного объекта.
        # Для Configuration.xml — отдельная конфигурация без объектов.
        scoped_config = _parse_xml_scoped(code_directory, scoped_files)
        if scoped_config is None:
            report.errors.append("xml scoped parse returned no configuration")
            return

        if has_config_change:
            apply_configuration_diff(
                loader=loader,
                state=state,
                source_type="xml",
                project_name=project_name,
                full_config=scoped_config,
                use_startup_probe_for_vectors=bool(
                    getattr(xml_context, "use_startup_probe_for_vectors", False)
                ),
            )

        if affected_objects:
            diff_and_apply_configuration(
                loader=loader,
                state=state,
                source_type="xml",
                project_name=project_name,
                full_config=scoped_config,
                report=report,
                affected_object_qns=affected_objects,
                use_startup_probe_for_vectors=bool(
                    getattr(xml_context, "use_startup_probe_for_vectors", False)
                ),
            )

    # 7. Update source_manifest for candidate files из discovery_info (без второго
    #    stat/read/hash).
    for xml_path in candidate_files:
        rel_path = str(xml_path.relative_to(code_directory)).replace("\\", "/")
        info = discovery_info.get(rel_path)
        if info is None:
            continue
        owner_qn = info["owner_qn"]
        emitted_qns = [owner_qn] if owner_qn else []
        pending_manifest_rows.append({
            "source_type": "xml",
            "rel_path": rel_path,
            "size": info["size"],
            "mtime_ns": info["mtime_ns"],
            "content_hash": info["content_hash"],
            "emitted_qns": emitted_qns,
        })

    # Атомарно: manifest batch + watermark. Без транзакции краш между ними
    # оставит manifest впереди watermark → следующий цикл пропустит реально-изменённые файлы.
    with state.transaction():
        state.upsert_source_manifest_many(pending_manifest_rows)
        state.upsert_stage_state("metadata_xml", "xml", now_ns)

    logger.info(
        "XML base discovery: files=%d unchanged=%d hashed=%d changed=%d "
        "manifest_rows=%d sqlite_reads=1 sqlite_writes=1 duration_ms=%d",
        files_total,
        unchanged_count,
        hashed_count,
        changed_count,
        len(pending_manifest_rows),
        discovery_duration_ms,
    )


# --------------------------------------------------------------------
# Full XML scan (deleted detection)
# --------------------------------------------------------------------


def xml_full_scan_run(
    *,
    loader: Any,
    state: IncrementalLoadingState,
    settings_obj: Any,
    report: IncrementalReport,
    xml_context: Optional[XmlCycleContext] = None,
) -> None:
    """Full XML scan: ищет удалённые XML files по разнице с manifest.

    Алгоритм (без full parse базы):
    1. `missing_paths` = manifest paths, которых больше нет на диске.
    2. Классификация root vs child descriptor через `_is_object_root_descriptor`,
       включая path-based Subsystems.
    3. Survivor-check для потенциально удалённых объектов: если у `qn` не осталось
       ни одного XML descriptor-а на диске → `apply_deleted_object`.
    4. Для owner-affected (пропал child descriptor, объект жив) — scoped reparse
       owner-а + `diff_and_apply_configuration`. Это покрывает «форма удалена,
       но объект жив» через `delete_removed_forms`.
    """
    project_name = settings_obj.project_name
    code_directory = Path(settings_obj.code_directory)
    if xml_context is not None:
        config_name = xml_context.base_config_name or "Configuration"
        current_xml_files = xml_context.metadata_xml_files
    else:
        config_name = _detect_configuration_name(code_directory) or "Configuration"
        from indexer.code_file_index import CodeFileIndexer

        idx = CodeFileIndexer.scan(code_directory)
        current_xml_files = list(getattr(idx, "metadata_xml_files", []) or [])

    manifest_paths = state.all_source_manifest_rel_paths("xml")
    if not manifest_paths:
        report.notes.append("no manifest baseline; full scan skipped")
        return

    # 1. Какие manifest-paths больше нет на диске?
    missing_paths: List[str] = []
    for rel_path in manifest_paths:
        if not (code_directory / rel_path).exists():
            missing_paths.append(rel_path)

    if not missing_paths:
        return

    from .metadata_sync import diff_and_apply_configuration

    state_qns = state.get_all_object_qns("xml")

    # 2. Классификация missing descriptors.
    potentially_deleted: Set[str] = set()
    owner_affected: Set[str] = set()
    for rel_path in missing_paths:
        manifest_entry = state.get_source_manifest("xml", rel_path)
        emitted: List[str] = (manifest_entry or {}).get("emitted_qns", []) or []
        if emitted:
            for qn in emitted:
                if _is_object_root_descriptor(rel_path, qn, config_name):
                    potentially_deleted.add(qn)
                else:
                    owner_affected.add(qn)
            continue
        owner_qn = _resolve_owner_object_qn(rel_path, project_name, config_name)
        if owner_qn is None:
            report.notes.append(
                f"xml full scan: cannot resolve owner for {rel_path}"
            )
            continue
        if _is_object_root_descriptor(rel_path, owner_qn, config_name):
            potentially_deleted.add(owner_qn)
        else:
            owner_affected.add(owner_qn)

    # 3. Survivor-check.
    truly_deleted: Set[str] = set()
    for qn in potentially_deleted:
        if qn not in state_qns:
            continue
        survivors = _files_for_object(
            code_directory, current_xml_files, qn, project_name, config_name
        )
        if not survivors:
            truly_deleted.add(qn)

    for obj_qn in sorted(truly_deleted):
        apply_deleted_object(
            loader=loader,
            state=state,
            source_type="xml",
            project_name=project_name,
            object_qn=obj_qn,
            report=report,
        )
        report.deleted_qns.append(obj_qn)

    owner_affected -= truly_deleted

    # 4. Scoped reparse owner-affected.
    if owner_affected:
        scoped_files: List[Path] = []
        cfg_xml = code_directory / "Configuration.xml"
        if cfg_xml.exists():
            scoped_files.append(cfg_xml)
        for owner in owner_affected:
            for f in _files_for_object(
                code_directory, current_xml_files, owner, project_name, config_name
            ):
                if f not in scoped_files:
                    scoped_files.append(f)
        if scoped_files:
            scoped_config = _parse_xml_scoped(code_directory, scoped_files)
            if scoped_config is None:
                report.errors.append("xml full scan: scoped parse returned no config")
            else:
                diff_and_apply_configuration(
                    loader=loader,
                    state=state,
                    source_type="xml",
                    project_name=project_name,
                    full_config=scoped_config,
                    report=report,
                    affected_object_qns=owner_affected,
                    use_startup_probe_for_vectors=bool(
                        getattr(xml_context, "use_startup_probe_for_vectors", False)
                    ),
                )

    # Remove orphan manifest entries.
    state.delete_source_manifest("xml", missing_paths)
    report.notes.append(
        f"full scan: removed {len(missing_paths)} missing XML files, "
        f"affected {len(owner_affected)} surviving objects, "
        f"deleted {len(truly_deleted)} objects"
    )


# --------------------------------------------------------------------
# Extension XML incremental run + full-scan
# --------------------------------------------------------------------


def _extract_ext_cfg_name_from_state(
    state: IncrementalLoadingState, source_scope: str, project_name: str
) -> str:
    """Module-level вариант для xml_walker — извлекает graph cfg name из QN в state."""
    full_qn = state.get_extension_scope_config_qn(source_scope)
    if not full_qn:
        return ""
    prefix = f"{project_name}/"
    if full_qn.startswith(prefix):
        return full_qn[len(prefix) :]
    return full_qn


def _detect_base_config_name_xml(
    state: IncrementalLoadingState, project_name: str
) -> str:
    """Имя базовой конфигурации из state ('xml' source_type)."""
    conn = state._connect()
    row = conn.execute(
        "SELECT configuration_qn FROM configuration_state "
        "WHERE project_name=? AND source_type=? LIMIT 1",
        (state.project_name, "xml"),
    ).fetchone()
    if not row or not row[0]:
        return ""
    prefix = f"{project_name}/"
    return row[0][len(prefix) :] if row[0].startswith(prefix) else row[0]


def _files_for_object_in_dir(
    code_directory: Path,
    metadata_xml_files: List[Path],
    object_qn: str,
    project_name: str,
    config_name: str,
) -> List[Path]:
    """Аналог `_files_for_object`, но через `_resolve_owner_object_qn` с конкретным
    `config_name` (имя расширения с `$ext$`)."""
    result: List[Path] = []
    for xml_path in metadata_xml_files:
        try:
            rel_path = str(xml_path.relative_to(code_directory)).replace("\\", "/")
        except ValueError:
            continue
        owner_qn = _resolve_owner_object_qn(rel_path, project_name, config_name)
        if owner_qn == object_qn:
            result.append(xml_path)
    return result


def xml_incremental_run_extensions(
    *,
    loader: Any,
    state: IncrementalLoadingState,
    settings_obj: Any,
    report: IncrementalReport,
    base_impact: BaseImpact,
    xml_context: Optional[XmlCycleContext] = None,
) -> None:
    """XML-инкрементал расширений (один цикл).

    Шаги (см. план §6):
    1. LOAD_EXTENSIONS=false или нет extensions_directory → выход.
    2. Top-level diff: удалённые scope регистрируются для deferred purge после
       artifact/BSL-фаз.
    3. Для каждого живого `<ext_dir>`:
       - validation `code/Configuration.xml`;
       - listing `metadata_xml_files`, candidate detection через mtime+size+hash;
       - overlay-affected_qns от `base_impact.added∪changed∪deleted`;
       - configuration_changed=True ветка: узкий reparse `<ext_code_dir>/Configuration.xml`
         + overlay + `apply_configuration_diff(is_extension=True)`;
       - scoped reparse (`is_extension=True`) + overlay → `diff_and_apply_configuration(is_extension=True)`;
       - `_refresh_extension_links` после применения;
       - manifest/stage update.
    """
    extensions_dir = getattr(settings_obj, "extensions_directory", None)
    if not getattr(settings_obj, "load_extensions", True):
        return
    if extensions_dir is None or not extensions_dir.exists():
        return

    project_name = settings_obj.project_name
    base_cfg_name = _detect_base_config_name_xml(state, project_name)

    ext_dirs = [d for d in extensions_dir.iterdir() if d.is_dir()]
    on_disk_names = {d.name for d in ext_dirs}

    # 2. Top-level удалённые каталоги.
    known_scopes = state.list_extension_scopes("xml")
    for scope in sorted(known_scopes):
        ext_dir_name = scope.split("xml_ext:", 1)[-1]
        if ext_dir_name in on_disk_names:
            continue
        ext_graph_config_name = _extract_ext_cfg_name_from_state(
            state, scope, project_name
        )
        if ext_graph_config_name:
            report.removed_extension_scopes[scope] = ext_graph_config_name
            report.notes.append(
                f"extension removal deferred: scope={scope} "
                f"config={ext_graph_config_name}"
            )
        else:
            # Do not drop state: without the graph config name a scoped purge
            # cannot be proven safe, and retaining the scope enables retry.
            report.errors.append(
                f"cannot defer extension removal without config name: scope={scope}"
            )

    # 3. Per-extension.
    for ext_dir in sorted(ext_dirs, key=lambda d: d.name):
        ext_dir_name = ext_dir.name
        source_scope = f"xml_ext:{ext_dir_name}"
        scope_exists = source_scope in known_scopes
        ext_code_dir = ext_dir / "code"
        config_xml = ext_code_dir / "Configuration.xml"

        # Validation: required code/Configuration.xml.
        if not config_xml.exists():
            if scope_exists:
                ext_graph_config_name = _extract_ext_cfg_name_from_state(
                    state, source_scope, project_name
                )
                if ext_graph_config_name:
                    report.removed_extension_scopes[source_scope] = (
                        ext_graph_config_name
                    )
                    report.notes.append(
                        f"extension removal deferred after validation failure: "
                        f"scope={source_scope} config={ext_graph_config_name}"
                    )
                else:
                    report.errors.append(
                        f"cannot defer invalid extension removal without config "
                        f"name: scope={source_scope}"
                    )
            continue

        sub_report = IncrementalReport(source_type=source_scope)
        try:
            _process_xml_extension_scope(
                loader=loader,
                state=state,
                report=sub_report,
                project_name=project_name,
                source_scope=source_scope,
                ext_dir_name=ext_dir_name,
                ext_code_dir=ext_code_dir,
                base_cfg_name=base_cfg_name,
                base_impact=base_impact,
                xml_context=xml_context,
                settings_obj=settings_obj,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("XML extension sync failed for %s", ext_dir_name)
            sub_report.errors.append(repr(exc))
        report.extension_reports[ext_dir_name] = sub_report


def _process_xml_extension_scope(
    *,
    loader: Any,
    state: IncrementalLoadingState,
    report: IncrementalReport,
    project_name: str,
    source_scope: str,
    ext_dir_name: str,
    ext_code_dir: Path,
    base_cfg_name: str,
    base_impact: BaseImpact,
    xml_context: Optional[XmlCycleContext] = None,
    settings_obj: Any = None,
) -> None:
    """Один scope расширения XML — обнаружение изменений + overlay refresh + apply.

    Алгоритм:
    1. Discovery: scan дерева расширения, candidate_files = реально изменённые
       XML (mtime+size+hash).
    2. Early-exit если изменений нет и overlay не нужен и scope baseline есть.
    3. Pre-parse Configuration.xml (один файл) → получить parsed `ext_graph_config_name`,
       detect rename.
    4. Decide full-init режим:
       - scope baseline пуст ИЛИ rename detected → full-init: использовать ВСЕ
         metadata_xml_files как `scoped_files`, affected_object_qns=None
         (полный diff: state vs parsed, включая deletions).
       - иначе incremental: для каждого affected owner (из candidate_files и из
         base impact projection) — добавить все его descriptor-ы через
         `_files_for_object_in_dir`. Это симметрично базовому `xml_incremental_run`
         ([xml_walker.py:286-313](app/incremental/xml_walker.py#L286-L313)).
    5. Parse финального scoped_config (`is_extension=True`).
    6. Overlay → Configuration-level diff → object-level diff → refresh links.
    """
    if xml_context is not None:
        code_index = xml_context.get_extension_code_index(ext_code_dir)
    else:
        from indexer.code_file_index import CodeFileIndexer

        code_index = CodeFileIndexer.scan(ext_code_dir)
    if code_index is None or not getattr(code_index, "metadata_xml_files", None):
        return

    config_xml = ext_code_dir / "Configuration.xml"
    metadata_xml_files = list(code_index.metadata_xml_files)

    # Batch-read всего manifest для scope: один SELECT вместо N per-file.
    manifest_by_rel = state.get_source_manifest_map(source_scope)

    # 1. Discovery — реально изменённые XML.
    candidate_files: List[Path] = []
    has_config_change = False
    # discovery_info фиксирует для каждого файла, прошедшего discovery, актуальные
    # size/mtime_ns/content_hash. Флаг `reused`: True — stat-match (hash из
    # manifest, файл не читался); False — hash был посчитан в этом раунде
    # (touched mtime/size либо real change). В финальном batch:
    # `reused=True` → запись попадает в manifest только если файл в scoped_files
    # (is_full_init); `reused=False` → всегда refresh.
    discovery_info: Dict[str, Dict[str, Any]] = {}

    files_total = 0
    unchanged_count = 0
    hashed_count = 0
    changed_count = 0
    discovery_start = time.perf_counter()

    for xml_path in metadata_xml_files:
        files_total += 1
        try:
            rel_path = str(xml_path.relative_to(ext_code_dir)).replace("\\", "/")
        except ValueError:
            continue
        try:
            stat = xml_path.stat()
        except OSError:
            continue
        mtime_ns = stat.st_mtime_ns
        size = stat.st_size
        manifest = manifest_by_rel.get(rel_path)
        if (
            manifest is not None
            and manifest["mtime_ns"] == mtime_ns
            and manifest["size"] == size
        ):
            unchanged_count += 1
            discovery_info[rel_path] = {
                "size": size,
                "mtime_ns": mtime_ns,
                "content_hash": manifest["content_hash"],
                "reused": True,
            }
            continue
        try:
            data = xml_path.read_bytes()
        except OSError:
            continue
        content_hash = compute_file_hash(data)
        hashed_count += 1
        hash_match = (
            manifest is not None and manifest["content_hash"] == content_hash
        )
        discovery_info[rel_path] = {
            "size": size,
            "mtime_ns": mtime_ns,
            "content_hash": content_hash,
            "reused": False,
            # `refresh_only`: True — touched mtime/size, hash совпал с manifest,
            # safe to flush в любых путях выхода (включая `not config_xml.exists()`),
            # потому что content не менялся. False — real change либо новый файл
            # без manifest baseline; такой row нельзя записывать ДО parse/apply,
            # иначе следующий cycle пропустит файл по stat-match и потеряет правку.
            "refresh_only": hash_match,
        }
        if hash_match:
            continue
        # Real change.
        changed_count += 1
        candidate_files.append(xml_path)
        if rel_path.lower() == "configuration.xml":
            has_config_change = True

    discovery_duration_ms = int((time.perf_counter() - discovery_start) * 1000)

    scope_baseline_empty = (
        state.get_extension_scope_config_qn(source_scope) is None
    )
    overlay_needs_run = (
        base_impact.has_object_impact or base_impact.configuration_changed
    )

    # Helper: build manifest rows для путей выхода ДО parse Configuration.xml.
    # Включаем ТОЛЬКО `refresh_only` записи (hash matched с manifest) — их безопасно
    # продвинуть, content не менялся. Real-change файлы НЕ flush'аем здесь:
    # их manifest должен обновиться только после успешного parse/apply, иначе
    # следующий цикл пропустит файл по stat-match и потеряет правку.
    # ext_graph_config_name ещё неизвестен, rename detect не делался — emitted_qns
    # берём из старого manifest (он валиден, так как scope не wipнут).
    def _build_pre_parse_rows() -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for rp, info in discovery_info.items():
            if info["reused"] or not info.get("refresh_only"):
                continue
            prev = manifest_by_rel.get(rp)
            emitted = prev.get("emitted_qns") if prev else []
            rows.append({
                "source_type": source_scope,
                "rel_path": rp,
                "size": info["size"],
                "mtime_ns": info["mtime_ns"],
                "content_hash": info["content_hash"],
                "emitted_qns": emitted or [],
            })
        return rows

    # 2. Early exit.
    if (
        not candidate_files
        and not overlay_needs_run
        and not scope_baseline_empty
    ):
        pre_parse_rows = _build_pre_parse_rows()
        with state.transaction():
            state.upsert_source_manifest_many(pre_parse_rows)
            state.upsert_stage_state(
                f"metadata_{source_scope}", source_scope, time.time_ns()
            )
        logger.info(
            "XML ext discovery (early-exit) scope=%s files=%d unchanged=%d "
            "hashed=%d changed=%d manifest_rows=%d duration_ms=%d",
            source_scope, files_total, unchanged_count, hashed_count,
            changed_count, len(pre_parse_rows), discovery_duration_ms,
        )
        return

    if not config_xml.exists():
        # Парсить Configuration.xml нельзя → rename detect невозможен → можем
        # безопасно flush hash-matched refresh (emitted_qns из старого manifest).
        pre_parse_rows = _build_pre_parse_rows()
        if pre_parse_rows:
            with state.transaction():
                state.upsert_source_manifest_many(pre_parse_rows)
        report.errors.append(
            f"xml extension {ext_dir_name}: missing Configuration.xml"
        )
        return

    # 3. Pre-parse Configuration.xml → parsed name + rename detection.
    cfg_only_config = _parse_xml_scoped_extension(ext_code_dir, [config_xml])
    if cfg_only_config is None:
        report.errors.append(
            f"xml extension {ext_dir_name}: Configuration.xml parse failed"
        )
        return
    ext_graph_config_name = cfg_only_config.name
    parsed_qn = _configuration_qn(project_name, ext_graph_config_name)
    old_qn = state.get_extension_scope_config_qn(source_scope)
    rename_detected = False
    if old_qn is not None and old_qn != parsed_qn:
        old_ext_cfg_name = old_qn.split("/", 1)[1] if "/" in old_qn else old_qn
        _apply_ext_removed(
            loader=loader,
            state=state,
            source_scope=source_scope,
            project_name=project_name,
            ext_graph_config_name=old_ext_cfg_name,
        )
        rename_detected = True
        scope_baseline_empty = True  # после wipe scope пуст

    # 4. Build scoped_files.
    is_full_init = scope_baseline_empty or rename_detected
    if is_full_init:
        report.adopted_from_impact.full_refresh_required = True
    scoped_files: List[Path] = [config_xml]
    overlay_affected_qns: Set[str] = set()
    owners_from_changes: Set[str] = set()

    if is_full_init:
        # Full init: ВСЕ XML файлы расширения.
        for f in metadata_xml_files:
            if f not in scoped_files:
                scoped_files.append(f)
    else:
        # Incremental: expand affected owners из candidate_files + base impact projection.
        for f in candidate_files:
            try:
                rel = str(f.relative_to(ext_code_dir)).replace("\\", "/")
            except ValueError:
                continue
            if rel.lower() == "configuration.xml":
                continue
            owner = _resolve_owner_object_qn(rel, project_name, ext_graph_config_name)
            if owner:
                owners_from_changes.add(owner)

        if overlay_needs_run and base_cfg_name:
            for base_qn in (
                base_impact.added_qns
                | base_impact.changed_qns
                | base_impact.deleted_qns
            ):
                ext_qn = _project_base_qn_to_ext(
                    base_qn, project_name, base_cfg_name, ext_graph_config_name
                )
                if ext_qn is None:
                    continue
                if state.get_object_state(source_scope, ext_qn) is None:
                    continue
                overlay_affected_qns.add(ext_qn)
                # BaseImpact → AdoptedFromImpact channel (план §D'):
                # base added/changed мог изменить matchability ADOPTED_FROM target
                # (новый target ИЛИ новый base child под существующий target). Для
                # extension descriptor-а нужен subtree-refresh через prefix.
                # `deleted` не добавляем — DETACH старой base ноды уже снёс edge,
                # MERGE не найдёт target.
                if base_qn in base_impact.added_qns or base_qn in base_impact.changed_qns:
                    report.adopted_from_impact.add_prefix(ext_qn)

        all_affected_owners = owners_from_changes | overlay_affected_qns
        for owner in all_affected_owners:
            for f in _files_for_object_in_dir(
                ext_code_dir,
                metadata_xml_files,
                owner,
                project_name,
                ext_graph_config_name,
            ):
                if f not in scoped_files:
                    scoped_files.append(f)

    # 5. Final scoped parse (`is_extension=True`).
    scoped_config = _parse_xml_scoped_extension(ext_code_dir, scoped_files)
    if scoped_config is None:
        report.errors.append(
            f"xml extension {ext_dir_name}: scoped parse returned no configuration"
        )
        return

    # 6. Overlay — scoped base config через xml_context.overlay_provider.
    #    Reverse-projection ext QN → base QN покрывает три источника:
    #    (a) base impact, проецированный в ext scope (overlay_affected_qns);
    #    (b) ext-changes в заимствованных объектах (owners_from_changes);
    #    (c) full-init: все adopted объекты parsed конфигурации расширения.
    if xml_context is not None and base_cfg_name:
        needed_base_qns: Set[str] = set()
        if is_full_init:
            needed_base_qns |= _collect_adopted_base_qns(
                scoped_config, project_name, base_cfg_name
            )
        else:
            for ext_qn in (overlay_affected_qns | owners_from_changes):
                base_qn = _project_ext_qn_to_base(
                    ext_qn, project_name, base_cfg_name, ext_graph_config_name
                )
                if base_qn:
                    needed_base_qns.add(base_qn)
        provider = xml_context.overlay_provider
        base_cfg: Any = None
        if needed_base_qns:
            base_cfg = provider.get_scoped_base(needed_base_qns, include_configuration=True)
        elif base_impact.configuration_changed:
            # Config-level overlay: INHERITED_EXTENSION_CONFIGURATION_PROPS merge.
            base_cfg = provider.get_configuration_only()
        if base_cfg is not None:
            try:
                from xml_metadata import apply_extension_base_overlay

                apply_extension_base_overlay(scoped_config, base_cfg)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "xml ext %s: apply_extension_base_overlay failed: %s",
                    ext_dir_name,
                    e,
                )

    # 7. Configuration-level diff.
    if (
        has_config_change
        or scope_baseline_empty
        or rename_detected
        or base_impact.configuration_changed
    ):
        apply_configuration_diff(
            loader=loader,
            state=state,
            source_type=source_scope,
            project_name=project_name,
            full_config=scoped_config,
            is_extension=True,
            report=report,
            use_startup_probe_for_vectors=bool(
                getattr(xml_context, "use_startup_probe_for_vectors", False)
            ),
        )

    # 8. Object-level diff.
    # Extension GUID map изоляция (parity с full-load и TXT extension fix):
    # перед diff_and_apply_configuration ставим extension-specific map из
    # <ext_dir>/code/ConfigDumpInfo.xml; в finally восстанавливаем prev (base) map.
    _prev_guid_map_xml = getattr(loader, "_guid_map", None)
    _ext_guid_outcome_xml = None
    if getattr(settings_obj, "load_metadata_guids", True):
        try:
            from .guid_sync import GuidIncrementalSync
            _ext_guid_outcome_xml = GuidIncrementalSync().apply_for_extension(
                loader, settings_obj, state,
                mode="xml", ext_dir=ext_dir_name, code_dir=ext_code_dir,
            )
            if _ext_guid_outcome_xml.enabled:
                loader.set_guid_map(_ext_guid_outcome_xml.map)
        except Exception:
            logger.exception("XML extension GUID map setup failed for %s", ext_dir_name)
    try:
        if is_full_init:
            # Полный diff: affected_object_qns=None → diff_and_apply сравнивает
            # parsed_qns vs state.get_all_object_qns(scope). После _apply_ext_removed
            # state пуст → все parsed объекты будут added (что и нужно).
            diff_and_apply_configuration(
                loader=loader,
                state=state,
                source_type=source_scope,
                project_name=project_name,
                full_config=scoped_config,
                report=report,
                affected_object_qns=None,
                is_extension=True,
                use_startup_probe_for_vectors=bool(
                    getattr(xml_context, "use_startup_probe_for_vectors", False)
                ),
            )
        else:
            affected_objects = owners_from_changes | overlay_affected_qns
            if affected_objects:
                diff_and_apply_configuration(
                    loader=loader,
                    state=state,
                    source_type=source_scope,
                    project_name=project_name,
                    full_config=scoped_config,
                    report=report,
                    affected_object_qns=affected_objects,
                    is_extension=True,
                    use_startup_probe_for_vectors=bool(
                        getattr(xml_context, "use_startup_probe_for_vectors", False)
                    ),
                )
    finally:
        if _ext_guid_outcome_xml is not None and _ext_guid_outcome_xml.enabled:
            try:
                loader.set_guid_map(_prev_guid_map_xml)
            except Exception:
                pass

    # 9. Refresh links (EXTENDS + scoped ADOPTED_FROM).
    if base_cfg_name:
        _refresh_extension_links_scoped(
            loader=loader,
            project_name=project_name,
            ext_graph_config_name=ext_graph_config_name,
            base_config_name=base_cfg_name,
            impact=report.adopted_from_impact,
        )

    # Финальный batch manifest update — rename-safe.
    # Стратегия:
    # 1. Все hashed-в-этом-раунде файлы (reused=False) — refresh. emitted_qns
    #    пересчитывается через текущий ext_graph_config_name. Это закрывает
    #    rename: после `_apply_ext_removed` manifest_by_rel в памяти содержит
    #    старые префиксы; здесь мы их игнорируем.
    # 2. Все scoped_files — manifest row после apply. Для stat-matched (reused=True)
    #    файлов в is_full_init берём cached size/mtime_ns/content_hash;
    #    emitted_qns ВСЕГДА через текущий ext_graph_config_name.
    # 3. Дедупликация через rows_by_rel.
    rows_by_rel: Dict[str, Dict[str, Any]] = {}

    for rel_path, info in discovery_info.items():
        if info["reused"]:
            continue
        owner_qn = _resolve_owner_object_qn(
            rel_path, project_name, ext_graph_config_name
        )
        emitted_qns = [owner_qn] if owner_qn else []
        rows_by_rel[rel_path] = {
            "source_type": source_scope,
            "rel_path": rel_path,
            "size": info["size"],
            "mtime_ns": info["mtime_ns"],
            "content_hash": info["content_hash"],
            "emitted_qns": emitted_qns,
        }

    for xml_path in scoped_files:
        try:
            rel_path = str(xml_path.relative_to(ext_code_dir)).replace("\\", "/")
        except ValueError:
            continue
        info = discovery_info.get(rel_path)
        if info is None:
            # Fallback: файл не прошёл discovery (stat OSError).
            try:
                stat = xml_path.stat()
                data = xml_path.read_bytes()
            except OSError:
                continue
            size = stat.st_size
            mtime_ns = stat.st_mtime_ns
            content_hash = compute_file_hash(data)
        else:
            size = info["size"]
            mtime_ns = info["mtime_ns"]
            content_hash = info["content_hash"]
        owner_qn = _resolve_owner_object_qn(
            rel_path, project_name, ext_graph_config_name
        )
        emitted_qns = [owner_qn] if owner_qn else []
        rows_by_rel[rel_path] = {
            "source_type": source_scope,
            "rel_path": rel_path,
            "size": size,
            "mtime_ns": mtime_ns,
            "content_hash": content_hash,
            "emitted_qns": emitted_qns,
        }

    with state.transaction():
        state.upsert_source_manifest_many(list(rows_by_rel.values()))
        state.upsert_stage_state(
            f"metadata_{source_scope}", source_scope, time.time_ns()
        )

    logger.info(
        "XML ext discovery scope=%s files=%d unchanged=%d hashed=%d changed=%d "
        "manifest_rows=%d full_init=%s rename=%s sqlite_reads=1 sqlite_writes=1 "
        "duration_ms=%d",
        source_scope, files_total, unchanged_count, hashed_count, changed_count,
        len(rows_by_rel), is_full_init, rename_detected, discovery_duration_ms,
    )


def xml_full_scan_run_extensions(
    *,
    loader: Any,
    state: IncrementalLoadingState,
    settings_obj: Any,
    report: IncrementalReport,
    xml_context: Optional[XmlCycleContext] = None,
) -> None:
    """Full XML scan для расширений: ищет удалённые XML descriptors внутри
    каталогов расширений по разнице с manifest.

    Use `xml_context.overlay_provider` для scoped base config — никаких
    `_parse_xml_full` в hot path.
    """
    if not getattr(settings_obj, "load_extensions", True):
        return
    extensions_dir = getattr(settings_obj, "extensions_directory", None)
    if extensions_dir is None or not extensions_dir.exists():
        return

    project_name = settings_obj.project_name
    code_directory = Path(settings_obj.code_directory)

    scopes = state.list_extension_scopes("xml")
    on_disk_names = {d.name for d in extensions_dir.iterdir() if d.is_dir()}

    base_cfg_name = ""
    if xml_context is not None:
        base_cfg_name = xml_context.base_config_name
    if not base_cfg_name:
        base_cfg_name = _detect_base_config_name_xml(state, project_name)

    for scope in sorted(scopes):
        ext_dir_name = scope.split("xml_ext:", 1)[-1]
        if ext_dir_name not in on_disk_names:
            # Top-level deletion обрабатывается в xml_incremental_run_extensions, skip.
            continue
        ext_code_dir = extensions_dir / ext_dir_name / "code"
        if not ext_code_dir.exists():
            continue

        ext_graph_config_name = _extract_ext_cfg_name_from_state(
            state, scope, project_name
        )
        if not ext_graph_config_name:
            continue

        sub_report = report.extension_reports.setdefault(
            ext_dir_name, IncrementalReport(source_type=scope)
        )

        try:
            _xml_full_scan_one_scope(
                loader=loader,
                state=state,
                report=sub_report,
                project_name=project_name,
                code_directory=code_directory,
                source_scope=scope,
                ext_code_dir=ext_code_dir,
                ext_graph_config_name=ext_graph_config_name,
                base_cfg_name=base_cfg_name,
                xml_context=xml_context,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("XML extension full-scan failed for %s", ext_dir_name)
            sub_report.errors.append(repr(exc))


def _xml_full_scan_one_scope(
    *,
    loader: Any,
    state: IncrementalLoadingState,
    report: IncrementalReport,
    project_name: str,
    code_directory: Path,
    source_scope: str,
    ext_code_dir: Path,
    ext_graph_config_name: str,
    base_cfg_name: str,
    xml_context: Optional[XmlCycleContext] = None,
) -> None:
    """Full scan одного scope расширения. Параллель `xml_full_scan_run`,
    но scoped: никаких `_parse_xml_full` ни базы, ни расширения."""
    # 1. Missing paths.
    manifest_paths = set(state.all_source_manifest_rel_paths(source_scope))
    if not manifest_paths:
        return
    missing_paths = [
        rel_path
        for rel_path in manifest_paths
        if not (ext_code_dir / rel_path).exists()
    ]
    if not missing_paths:
        return

    # 2. Свежий ext scan через context.
    if xml_context is not None:
        code_index = xml_context.get_extension_code_index(ext_code_dir)
    else:
        from indexer.code_file_index import CodeFileIndexer

        code_index = CodeFileIndexer.scan(ext_code_dir)
    if code_index is None:
        report.errors.append(
            f"xml ext full scan: ext scan failed for scope={source_scope}"
        )
        return
    current_xml_files = list(getattr(code_index, "metadata_xml_files", []) or [])

    # 3. Классификация missing descriptors.
    state_qns = state.get_all_object_qns(source_scope)
    potentially_deleted: Set[str] = set()
    owner_affected: Set[str] = set()
    for rel_path in missing_paths:
        manifest_entry = state.get_source_manifest(source_scope, rel_path)
        emitted: List[str] = (manifest_entry or {}).get("emitted_qns", []) or []
        if emitted:
            for qn in emitted:
                if _is_object_root_descriptor(rel_path, qn, ext_graph_config_name):
                    potentially_deleted.add(qn)
                else:
                    owner_affected.add(qn)
            continue
        owner_qn = _resolve_owner_object_qn(
            rel_path, project_name, ext_graph_config_name
        )
        if owner_qn is None:
            report.notes.append(
                f"xml ext full scan: cannot resolve owner for {rel_path}"
            )
            continue
        if _is_object_root_descriptor(rel_path, owner_qn, ext_graph_config_name):
            potentially_deleted.add(owner_qn)
        else:
            owner_affected.add(owner_qn)

    # 4. Survivor-check.
    truly_deleted: Set[str] = set()
    for qn in potentially_deleted:
        if qn not in state_qns:
            continue
        survivors = _files_for_object_in_dir(
            ext_code_dir, current_xml_files, qn, project_name, ext_graph_config_name
        )
        if not survivors:
            truly_deleted.add(qn)

    for obj_qn in sorted(truly_deleted):
        apply_deleted_object(
            loader=loader,
            state=state,
            source_type=source_scope,
            project_name=project_name,
            object_qn=obj_qn,
            report=report,
        )
        report.deleted_qns.append(obj_qn)

    owner_affected -= truly_deleted

    # 5. Scoped reparse owner-affected + scoped base overlay.
    if owner_affected:
        scoped_files: List[Path] = []
        cfg_xml = ext_code_dir / "Configuration.xml"
        if cfg_xml.exists():
            scoped_files.append(cfg_xml)
        for owner in owner_affected:
            for f in _files_for_object_in_dir(
                ext_code_dir,
                current_xml_files,
                owner,
                project_name,
                ext_graph_config_name,
            ):
                if f not in scoped_files:
                    scoped_files.append(f)
        scoped_config = _parse_xml_scoped_extension(ext_code_dir, scoped_files)
        if scoped_config is None:
            report.errors.append(
                f"xml ext full scan: scoped parse returned no config for {source_scope}"
            )
        else:
            # Scoped base overlay через provider.
            if xml_context is not None and base_cfg_name:
                needed_base_qns: Set[str] = set()
                for ext_qn in owner_affected:
                    base_qn = _project_ext_qn_to_base(
                        ext_qn, project_name, base_cfg_name, ext_graph_config_name
                    )
                    if base_qn:
                        needed_base_qns.add(base_qn)
                if needed_base_qns:
                    base_cfg = xml_context.overlay_provider.get_scoped_base(
                        needed_base_qns, include_configuration=True
                    )
                    if base_cfg is not None:
                        try:
                            from xml_metadata import apply_extension_base_overlay

                            apply_extension_base_overlay(scoped_config, base_cfg)
                        except Exception as e:  # noqa: BLE001
                            logger.warning(
                                "xml ext full scan %s: overlay failed: %s",
                                source_scope,
                                e,
                            )
            # Extension GUID map изоляция (parity с full-load) — see xml_walker
            # main extension path и TXT fix.
            _prev_guid_map_fs = getattr(loader, "_guid_map", None)
            _ext_guid_outcome_fs = None
            if getattr(settings_obj, "load_metadata_guids", True):
                try:
                    from .guid_sync import GuidIncrementalSync
                    _ext_guid_outcome_fs = GuidIncrementalSync().apply_for_extension(
                        loader, settings_obj, state,
                        mode="xml", ext_dir=ext_dir_name, code_dir=ext_code_dir,
                    )
                    if _ext_guid_outcome_fs.enabled:
                        loader.set_guid_map(_ext_guid_outcome_fs.map)
                except Exception:
                    logger.exception(
                        "XML extension full-scan GUID map setup failed for %s",
                        ext_dir_name,
                    )
            try:
                diff_and_apply_configuration(
                    loader=loader,
                    state=state,
                    source_type=source_scope,
                    project_name=project_name,
                    full_config=scoped_config,
                    report=report,
                    affected_object_qns=owner_affected,
                    is_extension=True,
                    use_startup_probe_for_vectors=bool(
                        getattr(xml_context, "use_startup_probe_for_vectors", False)
                    ),
                )
            finally:
                if _ext_guid_outcome_fs is not None and _ext_guid_outcome_fs.enabled:
                    try:
                        loader.set_guid_map(_prev_guid_map_fs)
                    except Exception:
                        pass

    state.delete_source_manifest(source_scope, missing_paths)
    report.notes.append(
        f"ext full scan: removed {len(missing_paths)} missing XML files, "
        f"affected {len(owner_affected)} surviving objects, "
        f"deleted {len(truly_deleted)} objects"
    )
