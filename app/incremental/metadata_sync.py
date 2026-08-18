"""
MetadataIncrementalSync — оркеструет diff + apply incremental loading.

Public API:
- run(loader, state, settings) — диспатчит по settings.metadata_source.
- sync_txt / sync_xml — entry-points для двух источников.
- apply_added_object / apply_changed_object / apply_deleted_object — одиночный
  apply (используется в тестах и edge case-ах). diff_and_apply_configuration в
  общем потоке использует двух-фазный batch path: per-object prepare → один
  bulk `load_configurations` для всех added+changed → per-object finalize +
  state update со snapshot.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Dict, List, Optional, Set, Tuple

from config import settings

from .child_identity import ChildGraphImpact, resolve_child_graph_identity
from .hashing import (
    ChildDomainImpact,
    build_object_snapshot,
    compute_child_diff,
    compute_configuration_hash,
    compute_file_hash,
    compute_object_hash,
)
from .report import AdoptedFromImpact, IncrementalReport
from .state import IncrementalLoadingState

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------
# QN helpers (зеркало rows_builder.py:54-63)
# --------------------------------------------------------------------


def _object_qn(
    project_name: str,
    config_name: str,
    category_name: str,
    obj: Any,
) -> str:
    """Зеркало `RowsBuilderMixin._build_rows_for_configuration`.

    Для `Подсистемы` использует path-based QN из `obj.properties['ПутьПодсистемы']`,
    для остальных категорий — простой `project/config/category/name`.
    """
    if category_name == "Подсистемы":
        chain = (obj.properties or {}).get("ПутьПодсистемы")
        if isinstance(chain, list) and chain:
            return f"{project_name}/{config_name}/{category_name}/" + "/".join(chain)
    return f"{project_name}/{config_name}/{category_name}/{obj.name}"


def _form_qn(object_qn: str, form_name: str) -> str:
    return f"{object_qn}/Form/{form_name}"


def _command_qn(object_qn: str, command_name: str) -> str:
    return f"{object_qn}/Command/{command_name}"


def _configuration_qn(project_name: str, config_name: str) -> str:
    return f"{project_name}/{config_name}"


# --------------------------------------------------------------------
# Thin configuration builder
# --------------------------------------------------------------------


def _build_thin_configuration(full_config: Any, picked_objects: Dict[str, List[Any]]) -> Any:
    """Создать «тонкий» Configuration, содержащий только заданные объекты.

    `picked_objects` — dict category_name → list[MetadataObject].
    Это minimum, нужный `loader.load_configurations` для apply одного объекта.
    """
    from parsers.metadata_parser import Configuration, MetadataCategory

    new_categories: List[MetadataCategory] = []
    for cat in full_config.categories:
        objects = picked_objects.get(cat.name, [])
        if not objects:
            continue
        new_cat = MetadataCategory(name=cat.name)
        for obj in objects:
            new_cat.add_metadata_object(obj)
        new_categories.append(new_cat)

    thin = Configuration(name=full_config.name, file_path=full_config.file_path)
    thin.properties = dict(full_config.properties or {})
    for cat in new_categories:
        thin.add_category(cat)
    return thin


def _empty_thin_configuration(full_config: Any) -> Any:
    """Configuration с актуальными properties, но без categories — для Configuration-level diff."""
    from parsers.metadata_parser import Configuration

    thin = Configuration(name=full_config.name, file_path=full_config.file_path)
    thin.properties = dict(full_config.properties or {})
    return thin


# --------------------------------------------------------------------
# Per-object helpers (cleanup + finalize). Используются и одиночными
# apply_added/changed_object, и batch path.
# --------------------------------------------------------------------


def _resolve_child_graph_impacts(
    object_qn: str, domain_impacts: List[ChildDomainImpact]
) -> List[ChildGraphImpact]:
    """Map list of domain impacts → list of graph impacts (label + exact/prefix QN).

    None-результаты от `resolve_child_graph_identity` отбрасываются (например,
    `movements` — relationship, не graph-узел).
    """
    out: List[ChildGraphImpact] = []
    for d in domain_impacts:
        g = resolve_child_graph_identity(object_qn, d)
        if g is not None:
            out.append(g)
    return out


def _accumulate_post_linking_impact(
    impact: "PostLinkingImpact",  # type: ignore[name-defined]
    config_name: str,
    object_qn: str,
    category_name: str,
    new_snapshot: Dict[str, Any],
    prev_snapshot: Optional[Dict[str, Any]],
    child_graph_impacts: List[ChildGraphImpact],
    is_added: bool,
) -> None:
    """Накопить QN затронутых child entities в PostLinkingImpact.

    Правила:
    - added object: пройти new_snapshot и добавить все commands/url_methods/forms.
      Дополнительно: если category_name == 'ПодпискиНаСобытия' — добавить
      object_qn в event_subscriptions (подписка моделируется как MetadataObject).
      Marker `mark_handler_relink(config_name)`.
    - changed object без prev_snapshot (legacy): full subtree был перезалит, добавить
      все current commands/url_methods/forms из new_snapshot. Mark relink.
    - changed object с prev_snapshot: использовать `child_graph_impacts` для
      added/changed groups commands/url_methods/forms; для deleted children пропустить
      (edge ушёл через DETACH). Для подписки события — если object changed и
      category=='ПодпискиНаСобытия' → object_qn в event_subscriptions + mark relink.
    """
    if not config_name:
        return

    def _add_all_from_snapshot(snapshot: Dict[str, Any]) -> None:
        marked = False
        for cmd in (snapshot.get("commands") or []):
            name = cmd.get("name") if isinstance(cmd, Mapping) else None
            if name:
                impact.add_command(config_name, f"{object_qn}/Command/{name}")
                marked = True
        for form in (snapshot.get("forms") or []):
            name = form.get("name") if isinstance(form, Mapping) else None
            if name:
                impact.add_form(config_name, f"{object_qn}/Form/{name}")
                marked = True
        # URL methods хранятся в obj.properties под русскими ключами:
        # `ШаблоныURL` (list) → `Имя` (template) + `Методы` (list) → `Имя` (method).
        # См. [hashing.py:_extract_url_methods] и [rows_builder.py:345-386] для
        # того же contract. Английские ключи использовать нельзя — snapshot их
        # не содержит.
        props = snapshot.get("properties") or {}
        url_tpls = props.get("ШаблоныURL") or []
        if isinstance(url_tpls, list):
            for tpl in url_tpls:
                if not isinstance(tpl, Mapping):
                    continue
                tpl_name = tpl.get("Имя")
                if not tpl_name:
                    continue
                methods = tpl.get("Методы") or []
                if not isinstance(methods, list):
                    continue
                for method in methods:
                    if not isinstance(method, Mapping):
                        continue
                    method_name = method.get("Имя")
                    if method_name:
                        impact.add_url_method(
                            config_name,
                            f"{object_qn}/UrlTemplate/{tpl_name}/Method/{method_name}",
                        )
                        marked = True
        if marked:
            impact.mark_handler_relink(config_name)

    if category_name == "ПодпискиНаСобытия":
        impact.add_event_subscription(config_name, object_qn)
        impact.mark_handler_relink(config_name)
        return

    if is_added or prev_snapshot is None:
        _add_all_from_snapshot(new_snapshot)
        return

    marked = False
    for gi in child_graph_impacts:
        if gi.action == "deleted":
            continue
        label = gi.label
        if label == "Command":
            impact.add_command(config_name, gi.exact_qn)
            marked = True
        elif label == "UrlMethod":
            impact.add_url_method(config_name, gi.exact_qn)
            marked = True
        elif label == "Form":
            impact.add_form(config_name, gi.exact_qn)
            marked = True
    if marked:
        impact.mark_handler_relink(config_name)


def _accumulate_adopted_from_impact(
    impact: AdoptedFromImpact,
    object_qn: str,
    prev_snapshot: Optional[Dict[str, Any]],
    new_snapshot: Dict[str, Any],
    child_graph_impacts: List[ChildGraphImpact],
    is_added: bool,
) -> None:
    """Накопить exact + prefix QN в AdoptedFromImpact для одного объекта.

    Правила (см. план §D):
    - added object → prefix_qns.add(object_qn): покрывает MetadataObject + все
      metadata-level children. child_graph_impacts для added=пустые (prev=None).
    - changed object, у которого prev_snapshot отсутствует (legacy state) →
      prefix_qns.add(object_qn). Child-diff невозможен, и blanket cleanup
      пересоздаст subtree через `delete_metadata_owned_children` — refresh должен
      покрыть его целиком, иначе stale-edges от удалённых children останутся.
    - changed object с changed `ПринадлежностьОбъекта` → prefix_qns.add(object_qn).
    - changed object без изменения ПринадлежностьОбъекта → impact только от
      child_graph_impacts (exact для leaf, prefix для TabularPart/UrlTemplate-changed).
    - action='deleted' child — узел уже удалён DETACH-ом, edge ушёл сам, impact
      добавлять не нужно.
    """
    if is_added:
        impact.add_prefix(object_qn)
        return

    # Legacy state без snapshot: blanket cleanup в _prepare_changed_in_neo4j
    # снёс весь subtree → refresh должен покрыть его целиком через prefix.
    if prev_snapshot is None:
        impact.add_prefix(object_qn)
        return

    # changed object: проверить ПринадлежностьОбъекта.
    prev_props = prev_snapshot.get("properties") or {}
    new_props = new_snapshot.get("properties") or {}
    prev_belonging = prev_props.get("ПринадлежностьОбъекта")
    new_belonging = new_props.get("ПринадлежностьОбъекта")
    if prev_belonging != new_belonging:
        impact.add_prefix(object_qn)
        # Children тоже могут получить новый contract — prefix их покрывает.

    for gi in child_graph_impacts:
        if gi.action == "deleted":
            continue  # узел снесён вместе с edge через DETACH.
        impact.add_exact(gi.label, gi.exact_qn)
        if gi.prefix_qn is not None:
            impact.add_prefix(gi.prefix_qn)


def _prepare_changed_in_neo4j(
    *,
    loader: Any,
    state: IncrementalLoadingState,
    source_type: str,
    project_name: str,
    object_qn: str,
    obj: Any,
    previous_keys: Set[str],
    new_keys: Set[str],
    child_graph_impacts: List[ChildGraphImpact],
    prev_snapshot_missing: bool = False,
) -> Tuple[Any, bool]:
    """Шаги 1–6 changed-flow: snapshot grants + targeted cleanup до bulk load.

    Targeted cleanup: вместо blanket `delete_metadata_owned_children([object_qn])`
    удаляются только реально изменившиеся/удалённые children по их exact QN или
    prefix subtree (для changed TabularPart/UrlTemplate). Это сохраняет Form/BSL
    subtree, FormControl/FormAttribute/FormEvent для survived форм и т.п.

    `prev_snapshot_missing=True` — legacy state без `object_snapshot_json` (миграция
    оставляет колонку NULL до первого реального upsert). Для такого объекта
    `compute_child_diff` возвращает пустой impacts list, что означает «не знаю,
    что именно изменилось». В этом случае нужен blanket cleanup через
    `delete_metadata_owned_children([object_qn])` — иначе stale child properties
    останутся в графе (CYPHER_UPSERT через `SET +=`).

    Returns (grants_snapshot, embedding_invalidated).
    """
    current_form_names: Set[str] = {f.name for f in (obj.forms or [])}
    current_command_names: Set[str] = {c.name for c in (obj.commands or [])}

    # 1. Snapshot grants по всему объекту (replay восстановит для уцелевших).
    grants_snapshot = loader.snapshot_grants_to_metadata_children(project_name, [object_qn])

    # 2. Cleanup whitelist children. Targeted при наличии snapshot, blanket для legacy.
    if prev_snapshot_missing:
        # Legacy fallback: child-diff не доступен, удаляем все children как раньше.
        loader.delete_metadata_owned_children(project_name, [object_qn])
    else:
        exact_qns: List[str] = []
        prefix_qns: List[str] = []
        for gi in child_graph_impacts:
            if gi.action == "added":
                continue  # MERGE создаст узел.
            # changed/deleted: удаляем узел перед re-MERGE (или окончательно для deleted).
            exact_qns.append(gi.exact_qn)
            if gi.prefix_qn is not None:
                prefix_qns.append(gi.prefix_qn)
        if exact_qns or prefix_qns:
            loader.delete_child_nodes_by_qn(project_name, exact_qns, prefix_qns)

    # 3. Delete removed object-level commands + their BSL subtree + state cleanup.
    removed_cmd_qns = loader.delete_removed_commands(object_qn, current_command_names)
    if removed_cmd_qns:
        state.delete_command_property_keys(source_type, removed_cmd_qns)

    # 3a. Cleanup_command_node + state update for survived commands.
    for cmd in obj.commands or []:
        cmd_qn = _command_qn(object_qn, cmd.name)
        prev_cmd_keys = state.get_command_property_keys(source_type, cmd_qn) or set()
        new_cmd_keys = set((cmd.properties or {}).keys())
        loader.cleanup_command_node(project_name, cmd_qn, prev_cmd_keys, new_cmd_keys)
        state.upsert_command_property_keys(source_type, cmd_qn, new_cmd_keys)

    # 4. Delete removed forms + Form.xml subtree + BSL form-modules + state cleanup.
    removed_form_qns = loader.delete_removed_forms(object_qn, current_form_names)
    if removed_form_qns:
        state.delete_form_property_keys(source_type, removed_form_qns)

    # 4a. Cleanup_form_node + state update for survived forms.
    for form in obj.forms or []:
        form_qn = _form_qn(object_qn, form.name)
        prev_form_keys = state.get_form_property_keys(source_type, form_qn) or set()
        new_form_keys = set((form.properties or {}).keys())
        loader.cleanup_form_node(project_name, form_qn, prev_form_keys, new_form_keys)
        state.upsert_form_property_keys(source_type, form_qn, new_form_keys)

    # 5. Cleanup MetadataObject (properties + DO_MOVEMENTS_IN + subsystem CONTAINS_OBJECT).
    loader.cleanup_metadata_object_node(project_name, object_qn, previous_keys, new_keys)

    # 6. Invalidate description embedding.
    loader.invalidate_metadata_description_embedding([object_qn])
    return grants_snapshot, True


def _finalize_added_state(
    *,
    state: IncrementalLoadingState,
    source_type: str,
    object_qn: str,
    obj: Any,
    new_hash: str,
    new_keys: Set[str],
    new_snapshot: Dict[str, Any],
) -> None:
    """Init metadata_object_hashes + form/command property_keys для added object.

    Без полной инициализации `form_property_keys`/`command_property_keys` следующий
    incremental cycle не сможет посчитать `keys_to_remove` в `cleanup_form_node`/
    `cleanup_command_node` (previous_keys=∅), и удалённое свойство останется в Neo4j
    (`CYPHER_UPSERT_FORM`/`CYPHER_UPSERT_COMMAND` используют SET ... += $properties).
    """
    state.upsert_object_state(
        source_type, object_qn, new_hash, new_keys, snapshot=new_snapshot
    )
    for form in obj.forms or []:
        state.upsert_form_property_keys(
            source_type,
            _form_qn(object_qn, form.name),
            set((form.properties or {}).keys()),
        )
    for cmd in obj.commands or []:
        state.upsert_command_property_keys(
            source_type,
            _command_qn(object_qn, cmd.name),
            set((cmd.properties or {}).keys()),
        )


# --------------------------------------------------------------------
# Single-object apply helpers (back-compat public API)
# --------------------------------------------------------------------


def apply_added_object(
    *,
    loader: Any,
    state: IncrementalLoadingState,
    source_type: str,
    project_name: str,
    config_name: str,
    category_name: str,
    obj: Any,
    full_config: Any,
    is_extension: bool = False,
    report: Optional[IncrementalReport] = None,
) -> None:
    """ADDED-flow для одного объекта: load + init state со snapshot.

    Если `report` передан — накапливает `prefix_qns.add(object_qn)` в
    `report.adopted_from_impact` (покрывает MetadataObject + все children
    нового adopted-объекта).
    """
    object_qn = _object_qn(project_name, config_name, category_name, obj)
    new_snapshot = build_object_snapshot(obj)
    thin = _build_thin_configuration(full_config, {category_name: [obj]})
    loader.load_configurations([thin], is_extension=is_extension)

    _finalize_added_state(
        state=state,
        source_type=source_type,
        object_qn=object_qn,
        obj=obj,
        new_hash=compute_object_hash(obj),
        new_keys=set((obj.properties or {}).keys()),
        new_snapshot=new_snapshot,
    )

    if report is not None:
        _accumulate_adopted_from_impact(
            report.adopted_from_impact,
            object_qn=object_qn,
            prev_snapshot=None,
            new_snapshot=new_snapshot,
            child_graph_impacts=[],
            is_added=True,
        )
        _accumulate_post_linking_impact(
            report.post_linking_impact,
            config_name=config_name,
            object_qn=object_qn,
            category_name=category_name,
            new_snapshot=new_snapshot,
            prev_snapshot=None,
            child_graph_impacts=[],
            is_added=True,
        )
        if category_name == "Подсистемы":
            report.ssl_owners_dirty = True

    # guid_state upsert для добавленного объекта + children — invariant
    # "guid_state хранит все GUID-eligible nodes".
    scope = _guid_scope_for_source_type(source_type)
    if scope:
        rows = _collect_guid_state_rows_for_object(
            scope, project_name, config_name, category_name, obj,
            getattr(loader, "_guid_map", None),
        )
        if rows:
            state.upsert_guid_state_many(rows)


def apply_changed_object(
    *,
    loader: Any,
    state: IncrementalLoadingState,
    source_type: str,
    project_name: str,
    config_name: str,
    category_name: str,
    obj: Any,
    full_config: Any,
    is_extension: bool = False,
    report: Optional[IncrementalReport] = None,
) -> bool:
    """CHANGED-flow (полные 9 шагов) для одного объекта.

    Единая семантика с batch path: ВСЕГДА вычисляет child-diff и идёт через
    targeted cleanup. `report` (optional) — для накопления child_stats и
    AdoptedFromImpact. Без `report` impacts всё равно используются для cleanup,
    но никуда не аккумулируются.

    Returns: True если embedding был invalidated (для report.embedding_repass_needed_qns).
    """
    object_qn = _object_qn(project_name, config_name, category_name, obj)

    prev = state.get_object_state(source_type, object_qn)
    previous_keys: Set[str] = prev[1] if prev else set()
    prev_snapshot: Optional[Dict[str, Any]] = prev[2] if prev else None
    new_keys: Set[str] = set((obj.properties or {}).keys())
    new_snapshot = build_object_snapshot(obj)

    stats, domain_impacts = compute_child_diff(prev_snapshot, new_snapshot)
    child_graph_impacts = _resolve_child_graph_impacts(object_qn, domain_impacts)

    grants_snapshot, embedding_invalidated = _prepare_changed_in_neo4j(
        loader=loader,
        state=state,
        source_type=source_type,
        project_name=project_name,
        object_qn=object_qn,
        obj=obj,
        previous_keys=previous_keys,
        new_keys=new_keys,
        child_graph_impacts=child_graph_impacts,
        prev_snapshot_missing=prev_snapshot is None,
    )

    # 7. Re-MERGE via load_configurations.
    thin = _build_thin_configuration(full_config, {category_name: [obj]})
    loader.load_configurations([thin], is_extension=is_extension)

    # 8. Replay grants.
    loader.replay_grants_to_metadata_children(grants_snapshot)

    # 9. Update state со snapshot.
    state.upsert_object_state(
        source_type,
        object_qn,
        compute_object_hash(obj),
        new_keys,
        snapshot=new_snapshot,
    )

    if report is not None:
        report.child_stats.merge(stats)
        _accumulate_adopted_from_impact(
            report.adopted_from_impact,
            object_qn=object_qn,
            prev_snapshot=prev_snapshot,
            new_snapshot=new_snapshot,
            child_graph_impacts=child_graph_impacts,
            is_added=False,
        )
        _accumulate_post_linking_impact(
            report.post_linking_impact,
            config_name=config_name,
            object_qn=object_qn,
            category_name=category_name,
            new_snapshot=new_snapshot,
            prev_snapshot=prev_snapshot,
            child_graph_impacts=child_graph_impacts,
            is_added=False,
        )
        if category_name == "Подсистемы":
            report.ssl_owners_dirty = True

    # guid_state object-subtree replace-by-subtree: delete старые rows, затем upsert
    # новые — invariant "guid_state хранит все GUID-eligible nodes".
    scope = _guid_scope_for_source_type(source_type)
    if scope:
        state.delete_guid_state_for_object_subtree(scope, object_qn)
        rows = _collect_guid_state_rows_for_object(
            scope, project_name, config_name, category_name, obj,
            getattr(loader, "_guid_map", None),
        )
        if rows:
            state.upsert_guid_state_many(rows)

    return embedding_invalidated


def apply_deleted_object(
    *,
    loader: Any,
    state: IncrementalLoadingState,
    source_type: str,
    project_name: str,
    object_qn: str,
    report: Optional[IncrementalReport] = None,
) -> None:
    """DELETED-flow: full subtree wipe + DETACH DELETE MetadataObject + state cleanup."""
    loader.delete_object_subtree(project_name, [object_qn])
    loader.delete_metadata_object_node(project_name, [object_qn])
    state.delete_object_state(source_type, object_qn)
    # guid_state subtree cleanup — symmetric with delete_object_state.
    scope = _guid_scope_for_source_type(source_type)
    if scope:
        state.delete_guid_state_for_object_subtree(scope, object_qn)
    # При удалении объекта категории Подсистемы поднять ssl_owners_dirty,
    # чтобы scheduler запустил project-wide SSL refresh.
    # object_qn для подсистемы — `<proj>/<config>/Подсистемы/...`; категорию
    # извлекаем из QN.
    if report is not None:
        try:
            parts = object_qn.split("/")
            if len(parts) >= 3 and parts[2] == "Подсистемы":
                report.ssl_owners_dirty = True
        except Exception:
            pass


def _collect_guid_state_rows_for_object(
    scope: str,
    project_name: str,
    config_name: str,
    category_name: str,
    obj: Any,
    guid_map: Optional[Dict[str, str]],
) -> List[Dict[str, Any]]:
    """Identity rows для guid_state для одного metadata-объекта + его children.

    Зеркало `_collect_guid_baseline_rows` (main.py) для одного объекта. Используется
    в incremental apply_added_object / apply_changed_object для пополнения sidecar.
    """
    from xcf_utils import (
        xcf_name_object,
        xcf_name_attribute,
        xcf_name_tabular_part,
        xcf_name_tabular_attribute,
        xcf_name_resource,
        xcf_name_dimension,
        xcf_name_form,
    )

    out: List[Dict[str, Any]] = []
    obj_name = getattr(obj, "name", "")
    gm = guid_map or {}

    if category_name == "Подсистемы":
        chain = (obj.properties or {}).get("ПутьПодсистемы")
        if isinstance(chain, list) and chain:
            obj_qn = f"{project_name}/{config_name}/{category_name}/" + "/".join(chain)
        else:
            obj_qn = f"{project_name}/{config_name}/{category_name}/{obj_name}"
    else:
        obj_qn = f"{project_name}/{config_name}/{category_name}/{obj_name}"

    def _emit(label: str, qn: str, xcf_name: Optional[str]) -> None:
        if not xcf_name or not qn:
            return
        out.append({
            "scope": scope,
            "label": label,
            "qualified_name": qn,
            "xcf_name": xcf_name,
            "current_guid": gm.get(xcf_name),
        })

    _emit("MetadataObject", obj_qn, xcf_name_object(category_name, obj_name))
    for attr in getattr(obj, "attributes", []) or []:
        _emit("Attribute", f"{obj_qn}/Attribute/{attr.name}",
              xcf_name_attribute(category_name, obj_name, attr.name))
    for tp in getattr(obj, "tabular_parts", []) or []:
        _emit("TabularPart", f"{obj_qn}/TabularPart/{tp.name}",
              xcf_name_tabular_part(category_name, obj_name, tp.name))
        for tpa in getattr(tp, "attributes", []) or []:
            _emit(
                "Attribute",
                f"{obj_qn}/TabularPart/{tp.name}/Attribute/{tpa.name}",
                xcf_name_tabular_attribute(category_name, obj_name, tp.name, tpa.name),
            )
    for res in getattr(obj, "resources", []) or []:
        _emit("Resource", f"{obj_qn}/Resource/{res.name}",
              xcf_name_resource(category_name, obj_name, res.name))
    for dim in getattr(obj, "dimensions", []) or []:
        _emit("Dimension", f"{obj_qn}/Dimension/{dim.name}",
              xcf_name_dimension(category_name, obj_name, dim.name))
    for form in getattr(obj, "forms", []) or []:
        _emit("Form", f"{obj_qn}/Form/{form.name}",
              xcf_name_form(category_name, obj_name, form.name))
    return out


def _guid_scope_for_source_type(source_type: str) -> Optional[str]:
    """Map metadata source_type → guid scope.

    txt → guid:base
    xml → guid:base
    txt_ext:<dir> → guid_ext:txt:<dir>
    xml_ext:<dir> → guid_ext:xml:<dir>
    """
    if source_type in ("txt", "xml"):
        return "guid:base"
    if source_type.startswith("txt_ext:"):
        return f"guid_ext:txt:{source_type[len('txt_ext:'):]}"
    if source_type.startswith("xml_ext:"):
        return f"guid_ext:xml:{source_type[len('xml_ext:'):]}"
    return None


# --------------------------------------------------------------------
# Configuration-level diff
# --------------------------------------------------------------------


def apply_configuration_diff(
    *,
    loader: Any,
    state: IncrementalLoadingState,
    source_type: str,
    project_name: str,
    full_config: Any,
    ensure_indexes: bool = True,
    is_extension: bool = False,
    report: Optional[IncrementalReport] = None,
    use_startup_probe_for_vectors: bool = False,
) -> None:
    """Configuration-level diff. `is_extension` критичен для extension scope:
    без него `_load_configuration` запишет `Configuration.is_extension = false`
    и сломает поиск базовой конфигурации в mcpsrv/server.py:200-218.

    `report.configuration_changed = True` выставляется когда base Configuration hash
    реально менялся — используется scheduler-ом для построения BaseImpact.
    """
    cfg_qn = _configuration_qn(project_name, full_config.name)
    new_hash = compute_configuration_hash(full_config)
    new_keys: Set[str] = set((full_config.properties or {}).keys())

    prev = state.get_configuration_state(source_type, cfg_qn)
    if prev is not None:
        prev_hash, prev_keys = prev
        if prev_hash == new_hash:
            return
        loader.cleanup_configuration_node(project_name, cfg_qn, prev_keys, new_keys)

    thin_empty = _empty_thin_configuration(full_config)
    loader.load_configurations(
        [thin_empty], ensure_indexes=ensure_indexes, is_extension=is_extension,
        use_startup_probe_for_vectors=use_startup_probe_for_vectors,
    )
    state.upsert_configuration_state(source_type, cfg_qn, new_hash, new_keys)
    if report is not None and not is_extension:
        report.configuration_changed = True


# --------------------------------------------------------------------
# Batch apply (added + changed → один load_configurations)
# --------------------------------------------------------------------


@dataclass
class _ChangedItem:
    object_qn: str
    category_name: str
    obj: Any
    previous_keys: Set[str]
    new_keys: Set[str]
    new_hash: str
    new_snapshot: Dict[str, Any]
    prev_snapshot: Optional[Dict[str, Any]]
    child_graph_impacts: List[ChildGraphImpact] = field(default_factory=list)
    grants_snapshot: Any = None
    embedding_invalidated: bool = False


@dataclass
class _AddedItem:
    object_qn: str
    category_name: str
    obj: Any
    new_hash: str
    new_keys: Set[str]
    new_snapshot: Dict[str, Any]


def _collect_parsed_objects(
    project_name: str, config: Any
) -> Dict[str, Tuple[str, Any]]:
    """Returns: dict object_qn → (category_name, MetadataObject)."""
    result: Dict[str, Tuple[str, Any]] = {}
    for cat in config.categories:
        for obj in cat.metadata_objects:
            qn = _object_qn(project_name, config.name, cat.name, obj)
            result[qn] = (cat.name, obj)
    return result


def diff_and_apply_configuration(
    *,
    loader: Any,
    state: IncrementalLoadingState,
    source_type: str,
    project_name: str,
    full_config: Any,
    report: IncrementalReport,
    affected_object_qns: Optional[Set[str]] = None,
    is_extension: bool = False,
    use_startup_probe_for_vectors: bool = False,
) -> None:
    """Сравнить parsed configuration со state и применить все три flow.

    Если affected_object_qns передан (XML object-scope), diff ограничен этим set-ом
    и `deleted` НЕ детектируется (нельзя различить «не парсили» и «удалено»).
    Если affected_object_qns == None (TXT full reparse), `deleted` = state − parsed.

    Added + changed применяются одним bulk `load_configurations`. `create_indexes()`
    вызывается ровно один раз в начале (если есть что грузить), последующие вызовы
    `load_configurations` идут с `ensure_indexes=False` — чтобы не повторять
    "Database indexes ensured" / "Ensuring vector indexes" на каждом цикле.
    """
    indexes_ensured = False

    def _ensure_indexes_once() -> None:
        nonlocal indexes_ensured
        if indexes_ensured:
            return
        try:
            loader.create_indexes(
                use_startup_probe_for_vectors=use_startup_probe_for_vectors
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("create_indexes failed or partially applied: %s", e)
        indexes_ensured = True

    # Configuration-level diff (если changed — поднимает create_indexes сам через
    # ensure_indexes=True в первый раз).
    cfg_qn = _configuration_qn(project_name, full_config.name)
    cfg_prev = state.get_configuration_state(source_type, cfg_qn)
    cfg_new_hash = compute_configuration_hash(full_config)
    if cfg_prev is None or cfg_prev[0] != cfg_new_hash:
        _ensure_indexes_once()
        apply_configuration_diff(
            loader=loader,
            state=state,
            source_type=source_type,
            project_name=project_name,
            full_config=full_config,
            ensure_indexes=False,
            is_extension=is_extension,
            report=report,
            use_startup_probe_for_vectors=use_startup_probe_for_vectors,
        )

    parsed_objects = _collect_parsed_objects(project_name, full_config)

    if affected_object_qns is not None:
        candidate_qns = affected_object_qns & set(parsed_objects.keys())
        existing_state_qns: Set[str] = set()
    else:
        candidate_qns = set(parsed_objects.keys())
        existing_state_qns = state.get_all_object_qns(source_type)

    added_items: List[_AddedItem] = []
    changed_items: List[_ChangedItem] = []

    for obj_qn in sorted(candidate_qns):
        category_name, obj = parsed_objects[obj_qn]
        prev = state.get_object_state(source_type, obj_qn)
        new_hash = compute_object_hash(obj)
        new_keys = set((obj.properties or {}).keys())
        new_snapshot = build_object_snapshot(obj)

        if prev is None:
            added_items.append(_AddedItem(
                object_qn=obj_qn,
                category_name=category_name,
                obj=obj,
                new_hash=new_hash,
                new_keys=new_keys,
                new_snapshot=new_snapshot,
            ))
            report.added_qns.append(obj_qn)
            # Added adopted-object subtree: prefix покрывает MetadataObject +
            # все metadata-level children (Attribute, TabularPart, Resource, ...).
            _accumulate_adopted_from_impact(
                report.adopted_from_impact,
                object_qn=obj_qn,
                prev_snapshot=None,
                new_snapshot=new_snapshot,
                child_graph_impacts=[],
                is_added=True,
            )
            _accumulate_post_linking_impact(
                report.post_linking_impact,
                config_name=full_config.name,
                object_qn=obj_qn,
                category_name=category_name,
                new_snapshot=new_snapshot,
                prev_snapshot=None,
                child_graph_impacts=[],
                is_added=True,
            )
            if category_name == "Подсистемы":
                report.ssl_owners_dirty = True
        elif prev[0] != new_hash:
            prev_keys = prev[1]
            prev_snapshot = prev[2]
            # Child-level diff (skipped for legacy state с prev_snapshot=None).
            stats, domain_impacts = compute_child_diff(prev_snapshot, new_snapshot)
            child_graph_impacts = _resolve_child_graph_impacts(obj_qn, domain_impacts)
            changed_items.append(_ChangedItem(
                object_qn=obj_qn,
                category_name=category_name,
                obj=obj,
                previous_keys=prev_keys,
                new_keys=new_keys,
                new_hash=new_hash,
                new_snapshot=new_snapshot,
                prev_snapshot=prev_snapshot,
                child_graph_impacts=child_graph_impacts,
            ))
            report.changed_qns.append(obj_qn)
            report.child_stats.merge(stats)
            _accumulate_adopted_from_impact(
                report.adopted_from_impact,
                object_qn=obj_qn,
                prev_snapshot=prev_snapshot,
                new_snapshot=new_snapshot,
                child_graph_impacts=child_graph_impacts,
                is_added=False,
            )
            _accumulate_post_linking_impact(
                report.post_linking_impact,
                config_name=full_config.name,
                object_qn=obj_qn,
                category_name=category_name,
                new_snapshot=new_snapshot,
                prev_snapshot=prev_snapshot,
                child_graph_impacts=child_graph_impacts,
                is_added=False,
            )
            if category_name == "Подсистемы":
                report.ssl_owners_dirty = True
        else:
            report.unchanged_count += 1

    if added_items or changed_items:
        _ensure_indexes_once()

        # Per-object prepare phase для changed (Neo4j cleanup + state update для
        # forms/commands). Added в prepare не участвуют — у них в graph пока ничего нет.
        for item in changed_items:
            item.grants_snapshot, item.embedding_invalidated = _prepare_changed_in_neo4j(
                loader=loader,
                state=state,
                source_type=source_type,
                project_name=project_name,
                object_qn=item.object_qn,
                obj=item.obj,
                previous_keys=item.previous_keys,
                new_keys=item.new_keys,
                child_graph_impacts=item.child_graph_impacts,
                prev_snapshot_missing=item.prev_snapshot is None,
            )

        # Один bulk load для всех added+changed по их категориям.
        picked: Dict[str, List[Any]] = {}
        for item in added_items:
            picked.setdefault(item.category_name, []).append(item.obj)
        for item in changed_items:
            picked.setdefault(item.category_name, []).append(item.obj)
        thin = _build_thin_configuration(full_config, picked)
        loader.load_configurations([thin], ensure_indexes=False, is_extension=is_extension)

        # Per-object finalize.
        for item in changed_items:
            loader.replay_grants_to_metadata_children(item.grants_snapshot)
            if item.embedding_invalidated:
                report.changed_qns_with_invalidated_embedding.append(item.object_qn)
            state.upsert_object_state(
                source_type,
                item.object_qn,
                item.new_hash,
                item.new_keys,
                snapshot=item.new_snapshot,
            )
        for item in added_items:
            _finalize_added_state(
                state=state,
                source_type=source_type,
                object_qn=item.object_qn,
                obj=item.obj,
                new_hash=item.new_hash,
                new_keys=item.new_keys,
                new_snapshot=item.new_snapshot,
            )

        # guid_state upsert для всех added+changed: invariant "registry всех
        # GUID-eligible nodes". Changed objects replace-by-subtree предварительно
        # очищены в apply_changed_object — здесь только batch path не имеет
        # такого cleanup, поэтому сначала explicit delete subtree per item.
        _scope = _guid_scope_for_source_type(source_type)
        if _scope:
            _gm = getattr(loader, "_guid_map", None)
            _guid_rows: List[Dict[str, Any]] = []
            for item in changed_items:
                state.delete_guid_state_for_object_subtree(_scope, item.object_qn)
                _guid_rows.extend(_collect_guid_state_rows_for_object(
                    _scope, project_name, full_config.name,
                    item.category_name, item.obj, _gm,
                ))
            for item in added_items:
                _guid_rows.extend(_collect_guid_state_rows_for_object(
                    _scope, project_name, full_config.name,
                    item.category_name, item.obj, _gm,
                ))
            if _guid_rows:
                state.upsert_guid_state_many(_guid_rows)

    if affected_object_qns is None:
        deleted = existing_state_qns - candidate_qns
        for obj_qn in sorted(deleted):
            apply_deleted_object(
                loader=loader,
                state=state,
                source_type=source_type,
                project_name=project_name,
                object_qn=obj_qn,
                report=report,
            )
            report.deleted_qns.append(obj_qn)


# --------------------------------------------------------------------
# Extension lifecycle helpers
# --------------------------------------------------------------------


# Labels дочерних узлов расширения с обязательным полем `config_name`.
# `Configuration` сюда НЕ входит — у неё нет `config_name`, удаляется отдельно по
# `qualified_name`. См. план §7.
_EXT_CHILD_LABELS: Tuple[str, ...] = (
    "MetadataCategory",
    "MetadataObject",
    "Attribute",
    "TabularPart",
    "Resource",
    "Dimension",
    "Form",
    "Command",
    "Layout",
    "Characteristic",
    "EnumValue",
    "UrlTemplate",
    "UrlMethod",
    "JournalGraph",
    "PredefinedItem",
    "AccountingFlag",
    "DimensionAccountingFlag",
)


def _apply_ext_removed(
    *,
    loader: Any,
    state: IncrementalLoadingState,
    source_scope: str,
    project_name: str,
    ext_graph_config_name: str,
) -> None:
    """Снести всё, что относится к scope расширения.

    Используется в трёх случаях:
    - удалён каталог расширения (top-level deletion);
    - валидация структуры расширения провалилась И scope существует;
    - rename конфигурации расширения внутри `<ext_dir>` (старый scope сносится,
      потом инициализируется заново под новым QN).

    `ext_graph_config_name` — имя конфигурации с `$ext$` (НЕ имя каталога).
    Извлекается из `state.get_extension_scope_config_qn(scope)` для удаления-после-baseline.
    """
    if not ext_graph_config_name:
        # На всякий случай — без graph config name мы не можем точечно почистить graph.
        # Просто чистим state, чтобы избежать вечного reapply одной и той же ошибки.
        state.delete_scope(source_scope)
        return

    ext_cfg_qn = _configuration_qn(project_name, ext_graph_config_name)

    with loader.driver.session(database=settings.neo4j_database) as session:
        # 1. Дочерние узлы по config_name (batched).
        for label in _EXT_CHILD_LABELS:
            cypher = (
                f"MATCH (n:{label}) "
                "WHERE n.project_name = $project_name AND n.config_name = $ext_cfg_name "
                "DETACH DELETE n"
            )
            try:
                session.run(
                    cypher,
                    project_name=project_name,
                    ext_cfg_name=ext_graph_config_name,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "_apply_ext_removed: %s DETACH DELETE failed: %s", label, e
                )

        # 2. Сам Configuration node (у него НЕТ config_name — фильтр по qualified_name).
        try:
            session.run(
                "MATCH (c:Configuration {qualified_name: $cfg_qn}) DETACH DELETE c",
                cfg_qn=ext_cfg_qn,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "_apply_ext_removed: Configuration DETACH DELETE failed: %s", e
            )

    # 3. Snapshot scope-а из SQLite.
    state.delete_scope(source_scope)
    logger.info(
        "Extension scope removed: scope=%s ext_graph_config_name=%s",
        source_scope,
        ext_graph_config_name,
    )


def _refresh_extension_links(
    *,
    loader: Any,
    project_name: str,
    ext_graph_config_name: str,
    base_config_name: str,
) -> None:
    """Перевыставить EXTENDS + пересоздать ADOPTED_FROM для metadata-level узлов.

    Идемпотентно: можно вызывать на каждом incremental cycle, который что-то менял
    в scope расширения, или при изменении базы.

    1. Configuration EXTENDS Configuration — MERGE, безопасно вызывать повторно.
    2. ADOPTED_FROM:
       - сначала снести старые рёбра для этого расширения (по child labels),
         чтобы не остались ребра от уже удалённых базовых объектов;
       - затем пересоздать через ExtensionRelationshipsBuilder.
    """
    ext_cfg_qn = _configuration_qn(project_name, ext_graph_config_name)
    base_cfg_qn = _configuration_qn(project_name, base_config_name)

    # 1. EXTENDS.
    try:
        loader.create_extends_link(ext_cfg_qn, base_cfg_qn)
    except Exception as e:  # noqa: BLE001
        logger.warning("_refresh_extension_links: EXTENDS link failed: %s", e)

    # 2. Delete-then-rebuild ADOPTED_FROM.
    adopted_labels: Tuple[str, ...] = (
        "MetadataObject",
        "Attribute",
        "TabularPart",
        "Dimension",
        "Resource",
        "Layout",
        "Command",
        "EnumValue",
        "Form",
        "Characteristic",
        "AccountingFlag",
        "DimensionAccountingFlag",
        "UrlTemplate",
        "UrlMethod",
        "JournalGraph",
    )
    with loader.driver.session(database=settings.neo4j_database) as session:
        for label in adopted_labels:
            try:
                session.run(
                    f"MATCH (n:{label}) "
                    "WHERE n.project_name = $project_name AND n.config_name = $ext_cfg_name "
                    "MATCH (n)-[r:ADOPTED_FROM]->() DELETE r",
                    project_name=project_name,
                    ext_cfg_name=ext_graph_config_name,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "_refresh_extension_links: %s ADOPTED_FROM delete failed: %s",
                    label,
                    e,
                )

    try:
        from graphdb.extension_relationships_builder import (
            ExtensionRelationshipsBuilder,
        )

        rel_builder = ExtensionRelationshipsBuilder(loader)
        rel_builder.build_adopted_from_for_extension(ext_cfg_qn, base_cfg_qn)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "_refresh_extension_links: build_adopted_from_for_extension failed: %s", e
        )


def _refresh_extension_links_scoped(
    *,
    loader: Any,
    project_name: str,
    ext_graph_config_name: str,
    base_config_name: str,
    impact: AdoptedFromImpact,
) -> None:
    """Scoped EXTENDS + ADOPTED_FROM refresh.

    Алгоритм:
    1. Всегда `loader.create_extends_link(...)` — MERGE-идемпотентен, дёшево.
    2. Если `impact.full_refresh_required` → fallback на `_refresh_extension_links`
       (delete-all + full rebuild). Используется при baseline-from-scratch, rename,
       аварийном fallback.
    3. Иначе если есть exact или prefix impact → scoped builder.
    4. Иначе — DEBUG лог, ничего не делать.
    """
    ext_cfg_qn = _configuration_qn(project_name, ext_graph_config_name)
    base_cfg_qn = _configuration_qn(project_name, base_config_name)

    # 1. EXTENDS — всегда.
    try:
        loader.create_extends_link(ext_cfg_qn, base_cfg_qn)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "_refresh_extension_links_scoped: EXTENDS link failed: %s", e
        )

    # 2. Full refresh fallback.
    if impact.full_refresh_required:
        _refresh_extension_links(
            loader=loader,
            project_name=project_name,
            ext_graph_config_name=ext_graph_config_name,
            base_config_name=base_config_name,
        )
        return

    # 3. Scoped refresh.
    if not impact.is_empty():
        try:
            from graphdb.extension_relationships_builder import (
                ExtensionRelationshipsBuilder,
            )

            rel_builder = ExtensionRelationshipsBuilder(loader)
            rel_builder.build_adopted_from_for_qns(
                ext_cfg_qn,
                base_cfg_qn,
                exact_qns_by_label={
                    label: sorted(qns)
                    for label, qns in impact.exact_qns_by_label.items()
                    if qns
                },
                prefix_qns=sorted(impact.prefix_qns),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "_refresh_extension_links_scoped: scoped builder failed: %s; "
                "falling back to full refresh",
                e,
            )
            _refresh_extension_links(
                loader=loader,
                project_name=project_name,
                ext_graph_config_name=ext_graph_config_name,
                base_config_name=base_config_name,
            )
        return

    # 4. No-op.
    logger.debug(
        "ADOPTED_FROM scoped refresh skipped: no relationship-impacting changes (ext=%s)",
        ext_cfg_qn,
    )


# --------------------------------------------------------------------
# Public sync entry points
# --------------------------------------------------------------------


class MetadataIncrementalSync:
    """Public entry-point. Диспатчит по settings.metadata_source."""

    def __init__(
        self, loader: Any, state: IncrementalLoadingState,
        *, use_startup_probe_for_vectors: bool = False,
    ) -> None:
        self.loader = loader
        self.state = state
        # Startup cycles set this so vector DDL (via create_indexes inside
        # apply/diff funcs) uses the bounded embedding probe. Scheduled cycles
        # leave it False. Carried as instance state to avoid threading a flag
        # through every sync method; read at the diff/apply call sites and set
        # on XmlCycleContext for the XML path.
        self._use_startup_probe_for_vectors = bool(use_startup_probe_for_vectors)

    def run(self, settings_obj: Any) -> IncrementalReport:
        source_type = getattr(settings_obj, "metadata_source", "txt")
        if source_type == "txt":
            metadata_dir = settings_obj.metadata_directory
            return self.sync_txt(metadata_dir, settings_obj)
        if source_type == "xml":
            return self.sync_xml(settings_obj)
        report = IncrementalReport(source_type=source_type)
        report.errors.append(f"Unknown metadata_source: {source_type}")
        return report

    # ----------------- TXT -----------------

    def sync_txt(self, metadata_dir: Path, settings_obj: Any) -> IncrementalReport:
        report = IncrementalReport(source_type="txt")
        start = time.perf_counter()
        try:
            project_name = settings_obj.project_name
            # Phase 0: GUID sync для base + extensions. Запускается ДО metadata
            # fast-path (mtime+size continue в _sync_txt_impl), чтобы ConfigDumpInfo-only
            # изменения подхватились даже без metadata diff.
            self._phase0_guid_sync(settings_obj, mode="txt")

            self._sync_txt_impl(metadata_dir, project_name, report)
            self.state.upsert_stage_state("metadata_txt", "txt", time.time_ns())

            if getattr(settings_obj, "load_extensions", True):
                self._sync_txt_extensions_impl(settings_obj, project_name, report)
        except Exception as exc:  # noqa: BLE001
            logger.exception("TXT incremental sync failed")
            report.errors.append(repr(exc))
        report.duration_seconds = time.perf_counter() - start
        return report

    def _phase0_guid_sync(self, settings_obj: Any, mode: str) -> None:
        """Phase 0: запустить GUID sync для base + всех extension dirs.

        Соблюдает settings.load_metadata_guids guard (parity с full load).
        Выполняется до metadata fast-path в _sync_txt_impl / xml_walker.
        """
        from .guid_sync import GuidIncrementalSync

        if not getattr(settings_obj, "load_metadata_guids", True):
            return  # выключено — ничего не делаем

        try:
            guid_sync = GuidIncrementalSync()
            # base
            base_outcome = guid_sync.apply_for_base(
                self.loader, settings_obj, self.state
            )
            if base_outcome.enabled:
                self.loader.set_guid_map(base_outcome.map)
                if base_outcome.changed:
                    guid_sync.scoped_refresh(
                        self.loader, settings_obj, self.state,
                        scope="guid:base",
                        guid_map=base_outcome.map,
                        file_stats=base_outcome.file_stats,
                    )

            # extensions — итерируем по каталогам ext_dir-ов, известных текущей
            # incremental раскладке. Источник — settings.ext_metadata_directory
            # или extensions_dir.
            ext_dirs = self._discover_extension_dirs(settings_obj, mode)
            for ext_dir_name, ext_code_dir in ext_dirs:
                ext_outcome = guid_sync.apply_for_extension(
                    self.loader, settings_obj, self.state,
                    mode=mode, ext_dir=ext_dir_name, code_dir=ext_code_dir,
                )
                if ext_outcome.enabled and ext_outcome.changed:
                    guid_sync.scoped_refresh(
                        self.loader, settings_obj, self.state,
                        scope=f"guid_ext:{mode}:{ext_dir_name}",
                        guid_map=ext_outcome.map,
                        file_stats=ext_outcome.file_stats,
                    )
        except Exception:  # noqa: BLE001
            logger.exception("Phase 0 GUID sync failed (mode=%s)", mode)

    def _discover_extension_dirs(
        self, settings_obj: Any, mode: str
    ) -> List[Tuple[str, Path]]:
        """Список (ext_dir_name, ext_code_dir) для GUID sync.

        Не парсит metadata — только ищет каталоги с code/ConfigDumpInfo.xml.
        Source root — settings.extensions_directory (TXT) или extensions_xml_directory (XML).
        """
        out: List[Tuple[str, Path]] = []
        if mode == "txt":
            base_dir = getattr(settings_obj, "extensions_directory", None)
        else:
            base_dir = getattr(settings_obj, "extensions_xml_directory", None) \
                or getattr(settings_obj, "extensions_directory", None)
        if not base_dir:
            return out
        base = Path(base_dir)
        if not base.exists():
            return out
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            code_dir = child / "code"
            if code_dir.exists():
                out.append((child.name, code_dir))
        return out

    def _sync_txt_impl(
        self, metadata_dir: Path, project_name: str, report: IncrementalReport
    ) -> None:
        txt_files = sorted(metadata_dir.glob("*.txt"))
        if not txt_files:
            report.notes.append("no .txt files in metadata directory")
            return

        for txt_path in txt_files:
            rel_path = txt_path.name
            stat = txt_path.stat()
            mtime_ns = stat.st_mtime_ns
            size = stat.st_size
            manifest = self.state.get_source_manifest("txt", rel_path)
            if (
                manifest is not None
                and manifest["mtime_ns"] == mtime_ns
                and manifest["size"] == size
            ):
                self.state.upsert_source_manifest(
                    "txt", rel_path, size, mtime_ns, manifest["content_hash"]
                )
                continue
            data = txt_path.read_bytes()
            content_hash = compute_file_hash(data)
            if manifest is not None and manifest["content_hash"] == content_hash:
                self.state.upsert_source_manifest(
                    "txt", rel_path, size, mtime_ns, content_hash
                )
                continue

            from indexer.metadata_loader import MetadataLoader

            ml = MetadataLoader()
            configs = ml.load_configurations(metadata_dir, source="txt")
            if not configs:
                report.errors.append(f"failed to parse {rel_path}")
                continue
            for full_config in configs:
                diff_and_apply_configuration(
                    loader=self.loader,
                    state=self.state,
                    source_type="txt",
                    project_name=project_name,
                    full_config=full_config,
                    report=report,
                    affected_object_qns=None,
                    use_startup_probe_for_vectors=self._use_startup_probe_for_vectors,
                )
            self.state.upsert_source_manifest(
                "txt", rel_path, size, mtime_ns, content_hash
            )

    def _sync_txt_extensions_impl(
        self, settings_obj: Any, project_name: str, report: IncrementalReport
    ) -> None:
        """TXT-инкрементал расширений.

        Шаги (см. план §5):
        1. extensions_directory не существует → выход.
        2. Top-level diff: scope-ы в state vs фактические каталоги на диске →
           удалённые регистрируются для deferred purge после artifact/BSL-фаз.
        3. Для каждого фактического `<ext_dir>`:
           - validation структуры (`metadata/` + единственный `.txt`);
           - при failed validation И существующем scope — `_apply_ext_removed`;
           - manifest-fast-path (size+mtime → hash);
           - при изменении hash или пустом scope — full reparse + rename detect +
             diff_and_apply + refresh ext links + manifest/stage update.
        """
        extensions_dir = getattr(settings_obj, "extensions_directory", None)
        if extensions_dir is None or not extensions_dir.exists():
            return

        # base config name нужен для refresh ADOPTED_FROM / EXTENDS.
        base_cfg_name = self._detect_base_config_name("txt")

        ext_dirs = [d for d in extensions_dir.iterdir() if d.is_dir()]
        on_disk_names = {d.name for d in ext_dirs}

        # 2. Top-level удалённые каталоги.
        known_scopes = self.state.list_extension_scopes("txt")
        for scope in sorted(known_scopes):
            ext_dir_name = scope.split("txt_ext:", 1)[-1]
            if ext_dir_name in on_disk_names:
                continue
            ext_graph_config_name = self._extract_ext_cfg_name_from_state(
                scope, project_name
            )
            if ext_graph_config_name:
                report.removed_extension_scopes[scope] = ext_graph_config_name
                report.notes.append(
                    f"extension removal deferred: scope={scope} "
                    f"config={ext_graph_config_name}"
                )
            else:
                report.errors.append(
                    f"cannot defer extension removal without config name: scope={scope}"
                )

        # 3. Per-extension.
        for ext_dir in sorted(ext_dirs, key=lambda d: d.name):
            ext_dir_name = ext_dir.name
            source_scope = f"txt_ext:{ext_dir_name}"
            scope_exists = source_scope in known_scopes

            ext_metadata_dir = ext_dir / "metadata"
            ext_code_dir = ext_dir / "code"

            # Validation as full load does.
            if not ext_metadata_dir.exists():
                if scope_exists:
                    ext_graph_config_name = self._extract_ext_cfg_name_from_state(
                        source_scope, project_name
                    )
                    if ext_graph_config_name:
                        report.removed_extension_scopes[source_scope] = (
                            ext_graph_config_name
                        )
                    else:
                        report.errors.append(
                            f"cannot defer invalid extension removal without "
                            f"config name: scope={source_scope}"
                        )
                continue
            txt_files = list(ext_metadata_dir.glob("*.txt"))
            if not txt_files or len(txt_files) > 1:
                if scope_exists:
                    ext_graph_config_name = self._extract_ext_cfg_name_from_state(
                        source_scope, project_name
                    )
                    if ext_graph_config_name:
                        report.removed_extension_scopes[source_scope] = (
                            ext_graph_config_name
                        )
                    else:
                        report.errors.append(
                            f"cannot defer invalid extension removal without "
                            f"config name: scope={source_scope}"
                        )
                continue
            txt_path = txt_files[0]
            rel_path = txt_path.name

            try:
                stat = txt_path.stat()
            except OSError:
                continue
            mtime_ns = stat.st_mtime_ns
            size = stat.st_size

            manifest = self.state.get_source_manifest(source_scope, rel_path)
            if (
                manifest is not None
                and manifest["mtime_ns"] == mtime_ns
                and manifest["size"] == size
            ):
                # No change → ensure manifest/stage stamp.
                self.state.upsert_source_manifest(
                    source_scope, rel_path, size, mtime_ns, manifest["content_hash"]
                )
                self.state.upsert_stage_state(
                    f"metadata_{source_scope}", source_scope, time.time_ns()
                )
                continue

            try:
                data = txt_path.read_bytes()
            except OSError:
                continue
            content_hash = compute_file_hash(data)
            if manifest is not None and manifest["content_hash"] == content_hash:
                self.state.upsert_source_manifest(
                    source_scope, rel_path, size, mtime_ns, content_hash
                )
                self.state.upsert_stage_state(
                    f"metadata_{source_scope}", source_scope, time.time_ns()
                )
                continue

            # Real change OR new scope → full reparse.
            from indexer.code_file_index import CodeFileIndexer
            from indexer.metadata_loader import MetadataLoader

            ml = MetadataLoader()
            ext_code_index = None
            if ext_code_dir.exists():
                try:
                    ext_code_index = CodeFileIndexer.scan(ext_code_dir)
                except Exception:
                    ext_code_index = None
            configs = ml.load_configurations(
                ext_metadata_dir,
                code_index=ext_code_index,
                source="txt",
                is_extension=True,
            )
            if not configs:
                report.errors.append(f"failed to parse extension {ext_dir_name}")
                continue
            ext_config = configs[0]
            # Полная загрузка TXT-расширения добавляет $ext$ вручную ПОСЛЕ парсера
            # (extensions_loader.py:251-253). Повторяем то же самое.
            if not ext_config.name.endswith("$ext$"):
                ext_config.name = f"{ext_config.name}$ext$"
            ext_graph_config_name = ext_config.name
            parsed_qn = _configuration_qn(project_name, ext_graph_config_name)

            # Rename detection: сравниваем full QN с full QN.
            old_qn = self.state.get_extension_scope_config_qn(source_scope)
            if old_qn is not None and old_qn != parsed_qn:
                old_ext_cfg_name = old_qn.split("/", 1)[1] if "/" in old_qn else old_qn
                _apply_ext_removed(
                    loader=self.loader,
                    state=self.state,
                    source_scope=source_scope,
                    project_name=project_name,
                    ext_graph_config_name=old_ext_cfg_name,
                )

            # Sub-report для этого расширения.
            sub_report = IncrementalReport(source_type=source_scope)
            # Scope-baseline-empty (новое расширение в runtime) → full refresh
            # fallback: scoped builder не оптимален при массовом added, проще full.
            scope_baseline_empty = not self.state.get_all_object_qns(source_scope)
            if scope_baseline_empty:
                sub_report.adopted_from_impact.full_refresh_required = True
            # Extension GUID map изоляция: parity с full-load ExtensionsLoader
            # ([extensions_loader.py:266-287]) — adopted nodes не должны получать
            # base GUID. Wrap diff_and_apply_configuration в try/finally restore.
            _prev_guid_map = getattr(self.loader, "_guid_map", None)
            _ext_guid_outcome = None
            if getattr(settings_obj, "load_metadata_guids", True):
                try:
                    from .guid_sync import GuidIncrementalSync
                    _ext_guid_outcome = GuidIncrementalSync().apply_for_extension(
                        self.loader, settings_obj, self.state,
                        mode="txt", ext_dir=ext_dir.name,
                        code_dir=ext_dir / "code",
                    )
                    if _ext_guid_outcome.enabled:
                        self.loader.set_guid_map(_ext_guid_outcome.map)
                except Exception:
                    logger.exception(
                        "Extension GUID map setup failed for %s", ext_dir.name
                    )
            try:
                diff_and_apply_configuration(
                    loader=self.loader,
                    state=self.state,
                    source_type=source_scope,
                    project_name=project_name,
                    full_config=ext_config,
                    report=sub_report,
                    affected_object_qns=None,
                    is_extension=True,
                    use_startup_probe_for_vectors=self._use_startup_probe_for_vectors,
                )
                # Refresh links (EXTENDS + scoped ADOPTED_FROM).
                if base_cfg_name:
                    _refresh_extension_links_scoped(
                        loader=self.loader,
                        project_name=project_name,
                        ext_graph_config_name=ext_graph_config_name,
                        base_config_name=base_cfg_name,
                        impact=sub_report.adopted_from_impact,
                    )
                self.state.upsert_source_manifest(
                    source_scope, rel_path, size, mtime_ns, content_hash
                )
                self.state.upsert_stage_state(
                    f"metadata_{source_scope}", source_scope, time.time_ns()
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("TXT extension sync failed for %s", ext_dir_name)
                sub_report.errors.append(repr(exc))
            finally:
                # Restore prev GUID map (обычно base) — следующее расширение
                # получит свою через apply_for_extension, base flow продолжится с base.
                if _ext_guid_outcome is not None and _ext_guid_outcome.enabled:
                    try:
                        self.loader.set_guid_map(_prev_guid_map)
                    except Exception:
                        pass
            report.extension_reports[ext_dir_name] = sub_report

    def _extract_ext_cfg_name_from_state(
        self, source_scope: str, project_name: str
    ) -> str:
        """Извлечь `ext_graph_config_name` из `configuration_state.configuration_qn`.

        Возвращает пустую строку если scope ещё не имеет stored QN — caller должен
        учесть это (без graph cfg name `_apply_ext_removed` пропустит graph cleanup).
        """
        full_qn = self.state.get_extension_scope_config_qn(source_scope)
        if not full_qn:
            return ""
        prefix = f"{project_name}/"
        if full_qn.startswith(prefix):
            return full_qn[len(prefix) :]
        return full_qn

    def _detect_base_config_name(self, source_type: str) -> str:
        """Определить имя базовой конфигурации из state.

        Используется TXT-расширениями для `_refresh_extension_links`. Если базы
        нет в state — возвращает пустую строку (caller пропустит refresh links).
        """
        # configuration_state хранит full QN базы под source_type ∈ {"txt","xml"}.
        # Берём первый row (база ровно одна).
        conn = self.state._connect()
        row = conn.execute(
            "SELECT configuration_qn FROM configuration_state "
            "WHERE project_name=? AND source_type=? LIMIT 1",
            (self.state.project_name, source_type),
        ).fetchone()
        if not row or not row[0]:
            return ""
        prefix = f"{self.state.project_name}/"
        full_qn = row[0]
        if full_qn.startswith(prefix):
            return full_qn[len(prefix) :]
        return full_qn

    # ----------------- XML -----------------

    def sync_xml(self, settings_obj: Any) -> IncrementalReport:
        report = IncrementalReport(source_type="xml")
        start = time.perf_counter()
        try:
            # Phase 0: GUID sync до xml walker (он не итерирует ConfigDumpInfo.xml,
            # см. CodeFileIndexer.scan, поэтому без отдельного hook GUID-only
            # изменения не подхватились бы).
            self._phase0_guid_sync(settings_obj, mode="xml")

            self._sync_xml_impl(settings_obj, report)
            self.state.upsert_stage_state("metadata_xml", "xml", time.time_ns())
        except Exception as exc:  # noqa: BLE001
            logger.exception("XML incremental sync failed")
            report.errors.append(repr(exc))
        report.duration_seconds = time.perf_counter() - start
        return report

    def _sync_xml_impl(self, settings_obj: Any, report: IncrementalReport) -> None:
        from pathlib import Path as _Path

        from .xml_walker import (
            XmlCycleContext,
            _detect_base_config_name_xml,
            xml_incremental_run,
        )

        # Per-cycle XML context: один scan базы + lazy ext scans + scoped overlay.
        base_cfg_name = (
            _detect_base_config_name_xml(self.state, settings_obj.project_name)
            or "Configuration"
        )
        xml_context = XmlCycleContext(
            code_directory=_Path(settings_obj.code_directory),
            project_name=settings_obj.project_name,
            base_config_name=base_cfg_name,
            use_startup_probe_for_vectors=self._use_startup_probe_for_vectors,
        )
        report.xml_context = xml_context

        xml_incremental_run(
            loader=self.loader,
            state=self.state,
            settings_obj=settings_obj,
            report=report,
            xml_context=xml_context,
        )
