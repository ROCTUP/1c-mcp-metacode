"""
Incremental Neo4j loader methods (stage 1).

Mixin для Neo4jLoader, добавляющий методы для apply_added_object /
apply_changed_object / apply_deleted_object orchestration.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from config import settings
from .cypher_templates import cypher_role_rights_grants_access_to

logger = logging.getLogger(__name__)


# Whitelist меток metadata-owned children (Architectural decision 1).
# `Command` обрабатывается ОТДЕЛЬНО через delete_removed_commands (нужен NOT '/Form/' filter),
# `Form` — через delete_removed_forms (selective by current_form_names).
INCREMENTAL_METADATA_CHILD_LABELS: List[str] = [
    "Attribute",
    "TabularPart",
    "Resource",
    "Dimension",
    "AccountingFlag",
    "DimensionAccountingFlag",
    "EnumValue",
    "Characteristic",
    "UrlTemplate",
    "UrlMethod",
    "JournalGraph",
    "Layout",
]

# PROTECTED_KEYS для MetadataObject (Architectural decision 1.b).
# Не удаляются property-cleanup-ом, даже если попали в previous_keys.
METADATA_OBJECT_PROTECTED_KEYS: Set[str] = {
    "name",
    "project_name",
    "config_name",
    "category_name",
    "qualified_name",
    "meta_uuid",
    "doc_description",
    "doc_description_embedding",
    "description_embedding",
    # role flags (от RoleRightsLoader)
    "setForNewObjects",
    "setForAttributesByDefault",
    "independentRightsOfChildObjects",
    # help (от help_loader)
    "Справка",
    # console search fields (от ConsoleSearchable, см. console_search.py)
    "console_search_section",
    "console_search_name",
    "console_search_synonym",
    "console_search_type",
    "console_search_name_norm",
    "console_search_synonym_norm",
    "console_search_type_norm",
}

CONFIGURATION_PROTECTED_KEYS: Set[str] = {
    "name",
    "project_name",
    "qualified_name",
}

FORM_PROTECTED_KEYS: Set[str] = {
    "name",
    "project_name",
    "config_name",
    "qualified_name",
    "form_content_hash",
    "base_form_hash",
}

COMMAND_PROTECTED_KEYS: Set[str] = {
    "name",
    "project_name",
    "config_name",
    "qualified_name",
}


class IncrementalLoaderMixin:
    """Mixin для Neo4jLoader. Предполагает self.driver и self._chunked() от Neo4jClient."""

    # ------------------------------------------------------------------
    # snapshot / replay GRANTS_ACCESS_TO
    # ------------------------------------------------------------------

    def snapshot_grants_to_metadata_children(
        self, project_name: str, object_qns: List[str]
    ) -> List[Dict[str, Any]]:
        """Собрать все GRANTS_ACCESS_TO от Roles к metadata-owned children этих объектов.

        Returns: list of {role_qn, target_qn, target_label, props}.
        """
        if not object_qns:
            return []
        cypher = """
        UNWIND $qns AS qn
        MATCH (role:MetadataObject)-[g:GRANTS_ACCESS_TO]->(child)
        WHERE child.qualified_name IS NOT NULL
          AND child.qualified_name STARTS WITH qn + '/'
          AND any(l IN labels(child) WHERE l IN $whitelist)
        RETURN
            role.qualified_name AS role_qn,
            child.qualified_name AS target_qn,
            [l IN labels(child) WHERE l IN $whitelist][0] AS target_label,
            properties(g) AS props
        """
        snapshot: List[Dict[str, Any]] = []
        with self.driver.session(database=settings.neo4j_database) as session:
            result = session.run(
                cypher,
                qns=object_qns,
                whitelist=INCREMENTAL_METADATA_CHILD_LABELS,
            )
            for record in result:
                snapshot.append(
                    {
                        "role_qn": record["role_qn"],
                        "target_qn": record["target_qn"],
                        "target_label": record["target_label"],
                        "props": dict(record["props"] or {}),
                    }
                )
        return snapshot

    def replay_grants_to_metadata_children(
        self, snapshot: List[Dict[str, Any]]
    ) -> None:
        """Восстановить GRANTS_ACCESS_TO после re-MERGE children.

        Узлы Role это `:MetadataObject` категории "Роли" — переиспользуем existing factory.
        """
        if not snapshot:
            return
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for item in snapshot:
            groups.setdefault(item["target_label"], []).append(
                {
                    "role_qn": item["role_qn"],
                    "target_qn": item["target_qn"],
                    "props": item["props"],
                }
            )
        with self.driver.session(database=settings.neo4j_database) as session:
            for target_label, rows in groups.items():
                cypher = cypher_role_rights_grants_access_to(target_label)
                for chunk in self._chunked(rows):
                    session.run(cypher, rows=chunk)

    # ------------------------------------------------------------------
    # delete metadata-owned children (batched)
    # ------------------------------------------------------------------

    def delete_child_nodes_by_qn(
        self,
        project_name: str,
        exact_qns: List[str],
        prefix_qns: List[str],
    ) -> int:
        """Targeted cleanup whitelist-children по точечным QN или prefix-subtree.

        Сценарий: changed object с известными impacts из `compute_child_diff`.
        В отличие от blanket `delete_metadata_owned_children` удаляет только реально
        изменившиеся/удалённые children + опционально parent-узлы (TabularPart,
        UrlTemplate) вместе с их subtree.

        Две независимые `session.run(...)` — иначе при пустом `exact_qns` единый
        UNWIND-блок обнулил бы rows и prefix-ветка не выполнилась бы вовсе.

        Whitelist (`INCREMENTAL_METADATA_CHILD_LABELS`) — защита от случайного
        удаления MetadataObject через prefix.
        """
        if not exact_qns and not prefix_qns:
            return 0
        total = 0
        with self.driver.session(database=settings.neo4j_database) as session:
            if exact_qns:
                rec = session.run(
                    """
                    UNWIND $exact_qns AS qn
                    MATCH (n) WHERE n.qualified_name = qn
                      AND any(l IN labels(n) WHERE l IN $whitelist)
                    DETACH DELETE n
                    RETURN count(*) AS deleted
                    """,
                    exact_qns=exact_qns,
                    whitelist=INCREMENTAL_METADATA_CHILD_LABELS,
                ).single()
                total += rec["deleted"] if rec else 0

            if prefix_qns:
                # Двойное условие: parent (qn = p) + subtree (qn STARTS WITH p+'/').
                # Без первой ветки сам TabularPart/UrlTemplate остался бы stale.
                rec = session.run(
                    """
                    UNWIND $prefix_qns AS p
                    MATCH (n)
                    WHERE (n.qualified_name = p
                           OR n.qualified_name STARTS WITH p + '/')
                      AND any(l IN labels(n) WHERE l IN $whitelist)
                    DETACH DELETE n
                    RETURN count(*) AS deleted
                    """,
                    prefix_qns=prefix_qns,
                    whitelist=INCREMENTAL_METADATA_CHILD_LABELS,
                ).single()
                total += rec["deleted"] if rec else 0
        return total

    def delete_metadata_owned_children(
        self,
        project_name: str,
        object_qns: List[str],
        batch_size: Optional[int] = None,
    ) -> int:
        """Удаляет узлы с whitelist-метками под `object_qn/` prefix."""
        if not object_qns:
            return 0
        bs = batch_size or settings.neo4j_batch_size
        total = 0
        cypher = """
        UNWIND $qns AS qn
        MATCH (n)
        WHERE n.qualified_name IS NOT NULL
          AND n.qualified_name STARTS WITH qn + '/'
          AND any(l IN labels(n) WHERE l IN $whitelist)
        WITH n LIMIT $batch_size
        DETACH DELETE n
        RETURN count(n) AS deleted
        """
        with self.driver.session(database=settings.neo4j_database) as session:
            while True:
                rec = session.run(
                    cypher,
                    qns=object_qns,
                    whitelist=INCREMENTAL_METADATA_CHILD_LABELS,
                    batch_size=bs,
                ).single()
                deleted = rec["deleted"] if rec else 0
                total += deleted
                if deleted < bs:
                    break
        return total

    # ------------------------------------------------------------------
    # Selective removed COMMANDS (object-level only, with BSL subtree)
    # ------------------------------------------------------------------

    def delete_removed_commands(
        self,
        object_qn: str,
        current_command_names: Set[str],
        batch_size: Optional[int] = None,
    ) -> List[str]:
        """Удалить object-level Command + их BSL subtree.

        Pure string operations (no regex) — устойчиво к regex-метасимволам в QN.
        Returns: list of removed command_qns (для state cleanup).
        """
        bs = batch_size or settings.neo4j_batch_size
        # Pass 1 — собрать removed command_qns.
        collect_cypher = """
        MATCH (m:MetadataObject {qualified_name: $object_qn})-[:HAS_COMMAND]->(c:Command)
        WHERE NOT c.name IN $current
          AND c.qualified_name STARTS WITH $object_qn + '/Command/'
          AND NOT substring(c.qualified_name, size($object_qn) + size('/Command/')) CONTAINS '/'
        RETURN collect(c.qualified_name) AS command_qns
        """
        with self.driver.session(database=settings.neo4j_database) as session:
            rec = session.run(
                collect_cypher,
                object_qn=object_qn,
                current=list(current_command_names),
            ).single()
            command_qns: List[str] = list(rec["command_qns"] or []) if rec else []
            if not command_qns:
                return []

            # Pass 2 — subtree wipe.
            wipe_cypher = """
            UNWIND $command_qns AS cmd_qn
            MATCH (n)
            WHERE n.qualified_name = cmd_qn
               OR n.owner_qn = cmd_qn
               OR (n.owner_qn IS NOT NULL AND n.owner_qn STARTS WITH cmd_qn + '/')
            WITH n LIMIT $batch_size
            DETACH DELETE n
            RETURN count(n) AS deleted
            """
            while True:
                rec = session.run(
                    wipe_cypher, command_qns=command_qns, batch_size=bs
                ).single()
                deleted = rec["deleted"] if rec else 0
                if deleted < bs:
                    break
        return command_qns

    # ------------------------------------------------------------------
    # Selective removed FORMS (with Form.xml subtree + BSL form-modules)
    # ------------------------------------------------------------------

    def delete_removed_forms(
        self,
        object_qn: str,
        current_form_names: Set[str],
        batch_size: Optional[int] = None,
    ) -> List[str]:
        """Удалить Form + Form.xml subtree + BSL form-modules для removed forms.

        Returns: list of removed form_qns (для state cleanup).
        """
        bs = batch_size or settings.neo4j_batch_size
        collect_cypher = """
        MATCH (m:MetadataObject {qualified_name: $object_qn})-[:HAS_FORM]->(f:Form)
        WHERE NOT f.name IN $current
        RETURN collect(f.qualified_name) AS form_qns
        """
        with self.driver.session(database=settings.neo4j_database) as session:
            rec = session.run(
                collect_cypher,
                object_qn=object_qn,
                current=list(current_form_names),
            ).single()
            form_qns: List[str] = list(rec["form_qns"] or []) if rec else []
            if not form_qns:
                return []

            # Pass 2 — 4-filter subtree wipe (qualified_name + owner_qn).
            wipe_cypher = """
            UNWIND $form_qns AS form_qn
            MATCH (n)
            WHERE n.qualified_name = form_qn
               OR (n.qualified_name IS NOT NULL AND n.qualified_name STARTS WITH form_qn + '/')
               OR n.owner_qn = form_qn
               OR (n.owner_qn IS NOT NULL AND n.owner_qn STARTS WITH form_qn + '/')
            WITH n LIMIT $batch_size
            DETACH DELETE n
            RETURN count(n) AS deleted
            """
            while True:
                rec = session.run(
                    wipe_cypher, form_qns=form_qns, batch_size=bs
                ).single()
                deleted = rec["deleted"] if rec else 0
                if deleted < bs:
                    break
        return form_qns

    # ------------------------------------------------------------------
    # cleanup_metadata_object_node
    # ------------------------------------------------------------------

    def cleanup_metadata_object_node(
        self,
        project_name: str,
        object_qn: str,
        previous_property_keys: Set[str],
        new_property_keys: Set[str],
    ) -> None:
        """Три действия для changed-object:
        1. REMOVE properties (previous - new - PROTECTED).
        2. DELETE outgoing DO_MOVEMENTS_IN.
        3. DELETE incoming subsystem CONTAINS_OBJECT (MetadataObject parents only).
        """
        keys_to_remove = previous_property_keys - new_property_keys - METADATA_OBJECT_PROTECTED_KEYS
        with self.driver.session(database=settings.neo4j_database) as session:
            if keys_to_remove:
                # Build REMOVE list dynamically. Keys come from internal state,
                # not user input — safe to interpolate (but use backticks for Cypher).
                remove_clauses = ", ".join(
                    f"m.`{k.replace('`', '``')}`" for k in sorted(keys_to_remove)
                )
                session.run(
                    f"MATCH (m:MetadataObject {{qualified_name: $qn}}) REMOVE {remove_clauses}",
                    qn=object_qn,
                )
            # Outgoing DO_MOVEMENTS_IN — пересоздаст load_configurations.
            session.run(
                """
                MATCH (m:MetadataObject {qualified_name: $qn})-[r:DO_MOVEMENTS_IN]->()
                DELETE r
                """,
                qn=object_qn,
            )
            # Subsystem reparenting: incoming CONTAINS_OBJECT от MetadataObject-родителей.
            session.run(
                """
                MATCH (parent:MetadataObject)-[r:CONTAINS_OBJECT]->(m:MetadataObject {qualified_name: $qn})
                DELETE r
                """,
                qn=object_qn,
            )

    # ------------------------------------------------------------------
    # cleanup_configuration_node
    # ------------------------------------------------------------------

    def cleanup_configuration_node(
        self,
        project_name: str,
        configuration_qn: str,
        previous_property_keys: Set[str],
        new_property_keys: Set[str],
    ) -> None:
        keys_to_remove = previous_property_keys - new_property_keys - CONFIGURATION_PROTECTED_KEYS
        if not keys_to_remove:
            return
        remove_clauses = ", ".join(
            f"c.`{k.replace('`', '``')}`" for k in sorted(keys_to_remove)
        )
        with self.driver.session(database=settings.neo4j_database) as session:
            session.run(
                f"MATCH (c:Configuration {{qualified_name: $qn}}) REMOVE {remove_clauses}",
                qn=configuration_qn,
            )

    # ------------------------------------------------------------------
    # cleanup_form_node / cleanup_command_node (survived child cleanup)
    # ------------------------------------------------------------------

    def cleanup_form_node(
        self,
        project_name: str,
        form_qn: str,
        previous_property_keys: Set[str],
        new_property_keys: Set[str],
    ) -> None:
        keys_to_remove = previous_property_keys - new_property_keys - FORM_PROTECTED_KEYS
        if not keys_to_remove:
            return
        remove_clauses = ", ".join(
            f"f.`{k.replace('`', '``')}`" for k in sorted(keys_to_remove)
        )
        with self.driver.session(database=settings.neo4j_database) as session:
            session.run(
                f"MATCH (f:Form {{qualified_name: $qn}}) REMOVE {remove_clauses}",
                qn=form_qn,
            )

    def cleanup_command_node(
        self,
        project_name: str,
        command_qn: str,
        previous_property_keys: Set[str],
        new_property_keys: Set[str],
    ) -> None:
        keys_to_remove = previous_property_keys - new_property_keys - COMMAND_PROTECTED_KEYS
        if not keys_to_remove:
            return
        remove_clauses = ", ".join(
            f"c.`{k.replace('`', '``')}`" for k in sorted(keys_to_remove)
        )
        with self.driver.session(database=settings.neo4j_database) as session:
            session.run(
                f"MATCH (c:Command {{qualified_name: $qn}}) REMOVE {remove_clauses}",
                qn=command_qn,
            )

    # ------------------------------------------------------------------
    # invalidate description embedding
    # ------------------------------------------------------------------

    def invalidate_metadata_description_embedding(
        self, object_qns: List[str]
    ) -> None:
        if not object_qns:
            return
        with self.driver.session(database=settings.neo4j_database) as session:
            session.run(
                """
                UNWIND $qns AS qn
                MATCH (m:MetadataObject {qualified_name: qn})
                REMOVE m.description_embedding
                """,
                qns=object_qns,
            )

    # ------------------------------------------------------------------
    # delete_object_subtree (deleted-flow)
    # ------------------------------------------------------------------

    def delete_object_subtree(
        self,
        project_name: str,
        object_qns: List[str],
        batch_size: Optional[int] = None,
    ) -> int:
        """Full subtree wipe для deleted-object (3 фильтра).

        Симметрично clear_project но scoped по объекту.
        Ловит metadata children (qualified_name STARTS WITH) + BSL Module/Routine/RoutineCodeUnit
        (owner_qn = qn / owner_qn STARTS WITH qn + '/').
        """
        if not object_qns:
            return 0
        bs = batch_size or settings.neo4j_batch_size
        total = 0
        cypher = """
        UNWIND $qns AS qn
        MATCH (n)
        WHERE (n.qualified_name IS NOT NULL AND n.qualified_name STARTS WITH qn + '/')
           OR n.owner_qn = qn
           OR (n.owner_qn IS NOT NULL AND n.owner_qn STARTS WITH qn + '/')
        WITH n LIMIT $batch_size
        DETACH DELETE n
        RETURN count(n) AS deleted
        """
        with self.driver.session(database=settings.neo4j_database) as session:
            while True:
                rec = session.run(cypher, qns=object_qns, batch_size=bs).single()
                deleted = rec["deleted"] if rec else 0
                total += deleted
                if deleted < bs:
                    break
        return total

    def delete_extension_scope(
        self,
        project_name: str,
        config_name: str,
        batch_size: Optional[int] = None,
    ) -> int:
        """Delete every node owned by one extension configuration.

        Extension form internals do not consistently carry ``project_name``.
        Ownership is therefore proven by the exact ``config_name`` together
        with at least one project-qualified property (project, QN or owner QN).
        The Configuration node itself has no ``config_name`` and is matched by
        its exact qualified name.
        """
        if not project_name or not config_name:
            raise ValueError("project_name and config_name are required")
        bs = batch_size or settings.neo4j_batch_size
        config_qn = f"{project_name}/{config_name}"
        config_prefix = config_qn + "/"
        total = 0
        cypher = """
        MATCH (n)
        WHERE (n.project_name = $project_name AND n.config_name = $config_name)
           OR n.qualified_name = $config_qn
           OR (n.qualified_name IS NOT NULL
               AND n.qualified_name STARTS WITH $config_prefix)
           OR n.owner_qn = $config_qn
           OR (n.owner_qn IS NOT NULL
               AND n.owner_qn STARTS WITH $config_prefix)
        WITH n LIMIT $batch_size
        DETACH DELETE n
        RETURN count(n) AS deleted
        """
        with self.driver.session(database=settings.neo4j_database) as session:
            while True:
                rec = session.run(
                    cypher,
                    project_name=project_name,
                    config_name=config_name,
                    config_qn=config_qn,
                    config_prefix=config_prefix,
                    batch_size=bs,
                ).single()
                deleted = int(rec["deleted"] if rec else 0)
                total += deleted
                if deleted < bs:
                    break
        return total

    def count_extension_scope_nodes(
        self,
        project_name: str,
        config_name: str,
    ) -> int:
        """Return nodes matching the same ownership predicate as purge."""
        if not project_name or not config_name:
            return 0
        config_qn = f"{project_name}/{config_name}"
        config_prefix = config_qn + "/"
        with self.driver.session(database=settings.neo4j_database) as session:
            rec = session.run(
                """
                MATCH (n)
                WHERE (n.project_name = $project_name
                       AND n.config_name = $config_name)
                   OR n.qualified_name = $config_qn
                   OR (n.qualified_name IS NOT NULL
                       AND n.qualified_name STARTS WITH $config_prefix)
                   OR n.owner_qn = $config_qn
                   OR (n.owner_qn IS NOT NULL
                       AND n.owner_qn STARTS WITH $config_prefix)
                RETURN count(n) AS count
                """,
                project_name=project_name,
                config_name=config_name,
                config_qn=config_qn,
                config_prefix=config_prefix,
            ).single()
        return int(rec["count"] if rec else 0)

    # ------------------------------------------------------------------
    # delete_metadata_object_node + empty category cleanup
    # ------------------------------------------------------------------

    def delete_metadata_object_node(
        self, project_name: str, object_qns: List[str]
    ) -> int:
        """Удаляет сам MetadataObject + проверяет/удаляет пустые MetadataCategory."""
        if not object_qns:
            return 0
        deleted_total = 0
        with self.driver.session(database=settings.neo4j_database) as session:
            # Шаг 1 — собрать категории до удаления.
            cats = session.run(
                """
                UNWIND $qns AS qn
                MATCH (cat:MetadataCategory)-[:CONTAINS_OBJECT]->(m:MetadataObject {qualified_name: qn})
                RETURN DISTINCT cat.qualified_name AS cat_qn
                """,
                qns=object_qns,
            ).values()
            category_qns = [r[0] for r in cats]

            # Шаг 2 — удалить MetadataObject узлы.
            rec = session.run(
                """
                UNWIND $qns AS qn
                MATCH (m:MetadataObject {qualified_name: qn})
                DETACH DELETE m
                RETURN count(m) AS deleted
                """,
                qns=object_qns,
            ).single()
            deleted_total = rec["deleted"] if rec else 0

            # Шаг 3 — удалить пустые категории.
            for cat_qn in category_qns:
                session.run(
                    """
                    MATCH (cat:MetadataCategory {qualified_name: $cat_qn})
                    WHERE NOT EXISTS { (cat)-[:CONTAINS_OBJECT]->(:MetadataObject) }
                    DETACH DELETE cat
                    """,
                    cat_qn=cat_qn,
                )
        return deleted_total

    # ------------------------------------------------------------------
    # Scoped artifact cleanup (Phase 2/3) — все методы scoped по
    # project_name + config_name + qn-filter; никаких глобальных
    # MATCH ... DETACH DELETE без scope-фильтра.
    # ------------------------------------------------------------------

    def delete_form_xcf_subtree(
        self,
        project_name: str,
        config_name: str,
        form_qns: List[str],
    ) -> None:
        """Удалить XCF subtree формы (FormControl / FormAttribute / FormEvent /
        FormEventAction / form-level Command), но НЕ саму ноду Form / MetadataObject.

        Регулярная форма: root = (:Form {qualified_name: form_qn}).
        Common form: root = (:MetadataObject {qualified_name: form_owner_qn,
        category_name: 'ОбщиеФормы'}), где form_owner_qn = split(form_qn, '/Form/')[0].

        Leaf-first порядок: FormEventAction → FormEvent → FormAttribute → Command
        → FormControl. `DETACH DELETE` удаляет рёбра deleted node, но не соседние;
        удаление контейнера первым оставило бы leaf-ноды orphan.
        """
        if not form_qns:
            return

        regular_qns: List[str] = []
        common_owner_qns: List[str] = []
        for fq in form_qns:
            if "/ОбщиеФормы/" in fq and "/Form/" in fq:
                owner = fq.split("/Form/", 1)[0]
                common_owner_qns.append(owner)
            else:
                regular_qns.append(fq)

        with self.driver.session(database=settings.neo4j_database) as session:
            if regular_qns:
                self._delete_form_xcf_subtree_for_root(
                    session,
                    project_name=project_name,
                    config_name=config_name,
                    qns=regular_qns,
                    root_label="Form",
                    root_filter="",
                )
            if common_owner_qns:
                self._delete_form_xcf_subtree_for_root(
                    session,
                    project_name=project_name,
                    config_name=config_name,
                    qns=common_owner_qns,
                    root_label="MetadataObject",
                    root_filter=" AND root.category_name = 'ОбщиеФормы'",
                )

    def _delete_form_xcf_subtree_for_root(
        self,
        session: Any,
        *,
        project_name: str,
        config_name: str,
        qns: List[str],
        root_label: str,
        root_filter: str,
    ) -> None:
        """Leaf-first cleanup для одной формы (regular или common).

        Statements выполняются по одному, потому что DETACH DELETE рвёт пути и
        следующее statement должно работать на ещё-уцелевших children.
        """
        # 1. FormEventAction: form-level + control-level
        session.run(
            f"""
            UNWIND $qns AS form_qn
            MATCH (root:{root_label} {{qualified_name: form_qn}})
            WHERE root.project_name = $project_name AND root.config_name = $config_name{root_filter}
            MATCH (root)-[:HAS_EVENT]->(:FormEvent)-[:HAS_EVENT_ACTION]->(fea:FormEventAction)
            DETACH DELETE fea
            """,
            qns=list(qns), project_name=project_name, config_name=config_name,
        )
        session.run(
            f"""
            UNWIND $qns AS form_qn
            MATCH (root:{root_label} {{qualified_name: form_qn}})
            WHERE root.project_name = $project_name AND root.config_name = $config_name{root_filter}
            MATCH (root)-[:HAS_CONTROL|HAS_CHILD*]->(:FormControl)-[:HAS_EVENT]->(:FormEvent)-[:HAS_EVENT_ACTION]->(fea:FormEventAction)
            DETACH DELETE fea
            """,
            qns=list(qns), project_name=project_name, config_name=config_name,
        )

        # 2. FormEvent: form-level + control-level
        session.run(
            f"""
            UNWIND $qns AS form_qn
            MATCH (root:{root_label} {{qualified_name: form_qn}})
            WHERE root.project_name = $project_name AND root.config_name = $config_name{root_filter}
            MATCH (root)-[:HAS_EVENT]->(fe:FormEvent)
            DETACH DELETE fe
            """,
            qns=list(qns), project_name=project_name, config_name=config_name,
        )
        session.run(
            f"""
            UNWIND $qns AS form_qn
            MATCH (root:{root_label} {{qualified_name: form_qn}})
            WHERE root.project_name = $project_name AND root.config_name = $config_name{root_filter}
            MATCH (root)-[:HAS_CONTROL|HAS_CHILD*]->(:FormControl)-[:HAS_EVENT]->(fe:FormEvent)
            DETACH DELETE fe
            """,
            qns=list(qns), project_name=project_name, config_name=config_name,
        )

        # 3. FormAttribute
        session.run(
            f"""
            UNWIND $qns AS form_qn
            MATCH (root:{root_label} {{qualified_name: form_qn}})
            WHERE root.project_name = $project_name AND root.config_name = $config_name{root_filter}
            MATCH (root)-[:HAS_FORM_ATTRIBUTE]->(fa:FormAttribute)
            DETACH DELETE fa
            """,
            qns=list(qns), project_name=project_name, config_name=config_name,
        )

        # 4. Command (form-level) — DETACH DELETE забирает HAS_HANDLER и LINKS_TO_COMMAND.
        session.run(
            f"""
            UNWIND $qns AS form_qn
            MATCH (root:{root_label} {{qualified_name: form_qn}})
            WHERE root.project_name = $project_name AND root.config_name = $config_name{root_filter}
            MATCH (root)-[:HAS_COMMAND]->(c:Command)
            DETACH DELETE c
            """,
            qns=list(qns), project_name=project_name, config_name=config_name,
        )

        # 5. FormControl (вся иерархия) — последним.
        session.run(
            f"""
            UNWIND $qns AS form_qn
            MATCH (root:{root_label} {{qualified_name: form_qn}})
            WHERE root.project_name = $project_name AND root.config_name = $config_name{root_filter}
            MATCH (root)-[:HAS_CONTROL|HAS_CHILD*]->(fc:FormControl)
            DETACH DELETE fc
            """,
            qns=list(qns), project_name=project_name, config_name=config_name,
        )

    def clear_form_content_hash(
        self,
        project_name: str,
        config_name: str,
        form_qns: List[str],
    ) -> None:
        if not form_qns:
            return
        with self.driver.session(database=settings.neo4j_database) as session:
            session.run(
                """
                UNWIND $qns AS form_qn
                MATCH (f:Form {qualified_name: form_qn})
                WHERE f.project_name = $project_name AND f.config_name = $config_name
                REMOVE f.form_content_hash
                """,
                qns=list(form_qns),
                project_name=project_name,
                config_name=config_name,
            )

    def delete_form_bin_routines(
        self,
        project_name: str,
        config_name: str,
        form_qns: List[str],
    ) -> None:
        """Удалить Routine/Module-узлы, связанные с Form.bin модулем формы.

        Form.bin парсится `BSLProcessor.process_formbin_result` и привязывается к Form по
        owner_qn = form_qn. Удаляем все Routine/Module с таким owner_qn.
        """
        if not form_qns:
            return
        with self.driver.session(database=settings.neo4j_database) as session:
            session.run(
                """
                UNWIND $qns AS form_qn
                MATCH (r:Routine)
                WHERE r.project_name = $project_name
                  AND r.config_name = $config_name
                  AND r.owner_qn = form_qn
                DETACH DELETE r
                """,
                qns=list(form_qns),
                project_name=project_name,
                config_name=config_name,
            )
            session.run(
                """
                UNWIND $qns AS form_qn
                MATCH (m:Module)
                WHERE m.project_name = $project_name
                  AND m.config_name = $config_name
                  AND m.owner_qn = form_qn
                DETACH DELETE m
                """,
                qns=list(form_qns),
                project_name=project_name,
                config_name=config_name,
            )

    def delete_predefined_for_owner_qns(
        self,
        project_name: str,
        config_name: str,
        owner_qns: List[str],
    ) -> None:
        if not owner_qns:
            return
        with self.driver.session(database=settings.neo4j_database) as session:
            # PredefinedItem QN: project/config/category/object/Predef/<local>
            session.run(
                """
                UNWIND $qns AS owner_qn
                MATCH (p:PredefinedItem)
                WHERE p.project_name = $project_name
                  AND p.config_name = $config_name
                  AND p.qualified_name STARTS WITH (owner_qn + '/Predef/')
                DETACH DELETE p
                """,
                qns=list(owner_qns),
                project_name=project_name,
                config_name=config_name,
            )

    def clear_help_content(
        self,
        project_name: str,
        config_name: str,
        object_qns: List[str],
    ) -> None:
        if not object_qns:
            return
        with self.driver.session(database=settings.neo4j_database) as session:
            session.run(
                """
                UNWIND $qns AS obj_qn
                MATCH (m:MetadataObject {qualified_name: obj_qn})
                WHERE m.project_name = $project_name AND m.config_name = $config_name
                REMOVE m.`Справка`
                """,
                qns=list(object_qns),
                project_name=project_name,
                config_name=config_name,
            )

    def delete_event_subscription_links(
        self,
        project_name: str,
        config_name: str,
        subscription_qns: List[str],
    ) -> None:
        """Cleanup canonical edges подписки на события: USES_HANDLER (исходящее
        к Routine) + HAS_EVENT_SUBSCRIPTION (входящее от источника-объекта).

        SUBSCRIBES_TO и HAS_SOURCE — legacy edges, никем не создаются."""
        if not subscription_qns:
            return
        from .cypher_templates import CYPHER_DELETE_EVENT_SUBSCRIPTION_LINKS
        with self.driver.session(database=settings.neo4j_database) as session:
            session.run(
                CYPHER_DELETE_EVENT_SUBSCRIPTION_LINKS,
                qns=list(subscription_qns),
                project_name=project_name,
                config_name=config_name,
            )

    def delete_role_rights_for_roles(
        self,
        project_name: str,
        config_name: str,
        role_qns: List[str],
    ) -> None:
        if not role_qns:
            return
        with self.driver.session(database=settings.neo4j_database) as session:
            session.run(
                """
                UNWIND $qns AS role_qn
                MATCH (role:Role {qualified_name: role_qn})
                WHERE role.project_name = $project_name AND role.config_name = $config_name
                MATCH (role)-[r:GRANTS_ACCESS_TO]->()
                DELETE r
                """,
                qns=list(role_qns),
                project_name=project_name,
                config_name=config_name,
            )

    def delete_bsl_by_file_paths(
        self,
        project_name: str,
        config_name: str,
        file_paths: List[str],
    ) -> None:
        """Удалить Routine/Module узлы для заданных BSL POSIX-путей.

        Path-схема та же, что у full-load BSL scanner ([bsl_signature_scanner.py:950-985](app/bsl_signature_scanner.py#L950)):
        `Routine.file_path` и `Module.path` хранятся как POSIX-relative от
        `settings.data_directory` (fallback — code_root). Caller обязан передавать
        path в том же виде.
        """
        if not file_paths:
            return
        with self.driver.session(database=settings.neo4j_database) as session:
            session.run(
                """
                UNWIND $paths AS fp
                MATCH (r:Routine)
                WHERE r.project_name = $project_name
                  AND r.config_name = $config_name
                  AND r.file_path = fp
                DETACH DELETE r
                """,
                paths=list(file_paths),
                project_name=project_name,
                config_name=config_name,
            )
            session.run(
                """
                UNWIND $paths AS fp
                MATCH (m:Module)
                WHERE m.project_name = $project_name
                  AND m.config_name = $config_name
                  AND m.path = fp
                DETACH DELETE m
                """,
                paths=list(file_paths),
                project_name=project_name,
                config_name=config_name,
            )

    def delete_bsl_routines_by_ids(
        self,
        project_name: str,
        config_name: str,
        routine_ids: List[str],
    ) -> None:
        """Routine-level scoped delete для incremental routine-level diff.

        Используется `_apply_bsl`/`build_delta` для deleted/signature_changed-old
        routines. В отличие от `delete_bsl_by_file_paths`, **не удаляет** Module
        и не трогает других routines файла — неизменные процедуры сохраняют
        `doc_description_embedding`/`code_embedding`.
        """
        if not routine_ids:
            return
        with self.driver.session(database=settings.neo4j_database) as session:
            session.run(
                """
                UNWIND $ids AS rid
                MATCH (r:Routine {id: rid})
                WHERE r.project_name = $project_name
                  AND r.config_name = $config_name
                DETACH DELETE r
                """,
                ids=list(routine_ids),
                project_name=project_name,
                config_name=config_name,
            )

    def delete_bsl_modules_by_ids(
        self,
        project_name: str,
        config_name: str,
        module_ids: List[str],
    ) -> None:
        """Module-level scoped delete для случаев module-orphan (файл стал пустым).

        Caller обязан удостовериться, что модуль действительно осиротевший — мы
        не проверяем наличие `DECLARES` от него: это решение принимает
        `build_delta` через trackings `new_module_ids` vs `removed_module_ids`.
        """
        if not module_ids:
            return
        with self.driver.session(database=settings.neo4j_database) as session:
            session.run(
                """
                UNWIND $ids AS mid
                MATCH (m:Module {id: mid})
                WHERE m.project_name = $project_name
                  AND m.config_name = $config_name
                DETACH DELETE m
                """,
                ids=list(module_ids),
                project_name=project_name,
                config_name=config_name,
            )

    def clear_routine_doc_embeddings(
        self,
        project_name: str,
        routine_ids: List[str],
    ) -> None:
        """Снять `doc_description_embedding` у заданных Routine.

        Используется routine-level diff: при `doc_changed` / `signature_changed`
        для конкретных routines doc embedding устаревает, но Routine.body /
        code_embedding (если есть) трогать не нужно. Сам doc-embedding потом
        пересоздаст `routine_indexer` штатным path-ом (он перебирает routines
        с `doc_description_embedding IS NULL`).
        """
        if not routine_ids:
            return
        with self.driver.session(database=settings.neo4j_database) as session:
            session.run(
                """
                UNWIND $ids AS rid
                MATCH (r:Routine {id: rid})
                WHERE r.project_name = $project_name
                  AND r.doc_description_embedding IS NOT NULL
                REMOVE r.doc_description_embedding
                """,
                ids=list(routine_ids),
                project_name=project_name,
            )

    def delete_extension_property_analysis(
        self,
        project_name: str,
        ext_config_name: str,
        object_qns: List[str],
    ) -> None:
        """Сбросить controlled/modified property-маркеры на дочерних узлах extension объекта.

        Идемпотентно — удаляет свойства `_extension_status` и пр. на children. Сами children
        не удаляются; они принадлежат extension metadata layer.
        """
        if not object_qns:
            return
        with self.driver.session(database=settings.neo4j_database) as session:
            for label in (
                "Attribute",
                "TabularPart",
                "Resource",
                "Dimension",
                "AccountingFlag",
                "DimensionAccountingFlag",
                "Form",
                "Command",
                "Layout",
            ):
                session.run(
                    f"""
                    UNWIND $qns AS owner_qn
                    MATCH (m:MetadataObject {{qualified_name: owner_qn}})
                    WHERE m.project_name = $project_name AND m.config_name = $ext_config_name
                    MATCH (m)-[*1..2]->(n:{label})
                    WHERE n.project_name = $project_name AND n.config_name = $ext_config_name
                    SET n.controlled_properties = NULL, n.modified_properties = NULL
                    """,
                    qns=list(object_qns),
                    project_name=project_name,
                    ext_config_name=ext_config_name,
                )

    def clear_extension_property_classification(
        self,
        session,
        project_name: str,
        rows: List[Dict[str, Any]],
    ) -> None:
        """Per-node cleanup classification: rows = [{label, qualified_name}].

        В отличие от delete_extension_property_analysis (owner-level через MATCH (m)-[*1..2]->n),
        этот метод чистит classification ровно на тех node, которые отдал sidecar diff
        — включая случай, когда classification была на самом MetadataObject.
        """
        if not rows:
            return
        by_label: Dict[str, List[Dict[str, str]]] = {}
        for r in rows:
            label = r.get("label")
            qn = r.get("qualified_name")
            if not label or not qn:
                continue
            by_label.setdefault(label, []).append({"qn": qn})
        for label, label_rows in by_label.items():
            session.run(
                f"""
                UNWIND $rows AS row
                MATCH (n:`{label}` {{qualified_name: row.qn, project_name: $project_name}})
                SET n.controlled_properties = NULL, n.modified_properties = NULL
                """,
                rows=label_rows,
                project_name=project_name,
            )

    def clear_extension_property_values(
        self,
        session,
        project_name: str,
        rows: List[Dict[str, Any]],
    ) -> None:
        """Guarded REMOVE analyzer-owned property values.

        Каждая row: {label, qualified_name, property_key, expected_value}.
        REMOVE применяется только если текущее значение узла == expected_value (защита
        от stomp пользовательских правок). Группировка по (label, property_key) — имя
        свойства — controlled set из analyzer.
        """
        if not rows:
            return
        groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for r in rows:
            label = r.get("label")
            key = r.get("property_key")
            if not label or not key:
                continue
            groups.setdefault((label, key), []).append(r)

        for (label, prop_key), grp_rows in groups.items():
            session.run(
                f"""
                UNWIND $rows AS row
                MATCH (n:`{label}` {{qualified_name: row.qn, project_name: $project_name}})
                WHERE n.`{prop_key}` = row.expected_value
                REMOVE n.`{prop_key}`
                """,
                rows=[
                    {"qn": r["qualified_name"], "expected_value": r.get("expected_value")}
                    for r in grp_rows
                ],
                project_name=project_name,
            )

    # ------------------------------------------------------------------
    # Refresh handlers (delete-then-merge) — каждая API удаляет старые edge-ы
    # у affected узлов перед re-merge. Иначе incremental на грязном графе
    # оставляет сирот после rename handler-а.
    # ------------------------------------------------------------------

    def refresh_form_event_handlers(
        self,
        project_name: str,
        config_name: str,
        form_qns: List[str],
        command_qns: List[str],
        is_extension: bool = False,  # noqa: ARG002
    ) -> None:
        """Удалить старые HAS_HANDLER у affected form events / commands.

        После этого caller (`PostLinkingSync`) вызывает существующий
        `link_form_events_and_commands(project_name, config_name, form_routines)` —
        он re-merge-ит связи, теперь без сирот.
        """
        if not (form_qns or command_qns):
            return
        with self.driver.session(database=settings.neo4j_database) as session:
            if form_qns:
                session.run(
                    """
                    UNWIND $qns AS form_qn
                    MATCH (f:Form {qualified_name: form_qn})
                    WHERE f.project_name = $project_name AND f.config_name = $config_name
                    MATCH (f)-[*1..3]->(a:FormEventAction)-[r:HAS_HANDLER]->()
                    DELETE r
                    """,
                    qns=list(form_qns),
                    project_name=project_name,
                    config_name=config_name,
                )
            if command_qns:
                session.run(
                    """
                    UNWIND $qns AS cmd_qn
                    MATCH (c:Command {qualified_name: cmd_qn})
                    WHERE c.project_name = $project_name AND c.config_name = $config_name
                    MATCH (c)-[r:HAS_HANDLER]->()
                    DELETE r
                    """,
                    qns=list(command_qns),
                    project_name=project_name,
                    config_name=config_name,
                )

    def refresh_url_method_handlers(
        self,
        project_name: str,
        config_name: str,
        url_method_qns: List[str],
        is_extension: bool = False,  # noqa: ARG002
    ) -> None:
        if not url_method_qns:
            return
        with self.driver.session(database=settings.neo4j_database) as session:
            session.run(
                """
                UNWIND $qns AS um_qn
                MATCH (u:UrlMethod {qualified_name: um_qn})
                WHERE u.project_name = $project_name AND u.config_name = $config_name
                MATCH (u)-[r:HAS_HANDLER]->()
                DELETE r
                """,
                qns=list(url_method_qns),
                project_name=project_name,
                config_name=config_name,
            )

    def refresh_event_subscription_handlers(
        self,
        project_name: str,
        config_name: str,
        subscription_qns: List[str],
        is_extension: bool = False,  # noqa: ARG002
    ) -> None:
        """Сбросить USES_HANDLER для указанных подписок (canonical contract:
        `MetadataObject {category_name='ПодпискиНаСобытия'}` + `USES_HANDLER`).
        Реальное создание новых USES_HANDLER делает `link_event_subscriptions_to_handlers`
        или scoped variant в `replace_event_subscriptions`."""
        if not subscription_qns:
            return
        with self.driver.session(database=settings.neo4j_database) as session:
            session.run(
                """
                UNWIND $qns AS sub_qn
                MATCH (es:MetadataObject {qualified_name: sub_qn})
                WHERE es.project_name = $project_name
                  AND es.config_name = $config_name
                  AND es.category_name = 'ПодпискиНаСобытия'
                MATCH (es)-[r:USES_HANDLER]->()
                DELETE r
                """,
                qns=list(subscription_qns),
                project_name=project_name,
                config_name=config_name,
            )

    def rebuild_form_level_extension_relationships(
        self,
        *,
        project_name: str,
        base_config_name: str,
        known_extension_configs: Dict[str, str],
        base_forms: List[str],
        ext_forms_by_config: Dict[str, List[str]],
    ) -> None:
        """Per-form rebuild form-level extension relationships для затронутых форм.

        Источники импакта:
        - base_forms — base form QN, у которых менялись internals (Form.xml в base).
          Найти все extension-формы, которые их `ADOPTED_FROM`, через Cypher
          lookup в графе. Никакого config-level rebuild для невзятых расширений.
        - ext_forms_by_config — per-extension QN-ы форм, у которых менялись
          internals напрямую (Form.xml в расширении). Сюда же fallback для
          новой extension-формы: если `ADOPTED_FROM` ещё не существует, deterministic
          base-QN projection (replace ext_config_name на base_config_name).

        Сcope паттернов:
        - Regular form: `Form.qualified_name = ext_form_qn`.
        - Common form: `MetadataObject {category_name='ОбщиеФормы'}, qualified_name = ext_form_owner_qn`.

        Cleanup `ADOPTED_FROM`/`EXTENDS_ACTION` ребер выполняется per-form в scope
        затронутых extension form пар; затем builders пересобирают связи через
        опциональный `form_pairs` параметр.
        """
        from incremental.report import FormRebuildPair

        pairs_by_config: Dict[str, List[FormRebuildPair]] = {}

        with self.driver.session(database=settings.neo4j_database) as session:
            # (а) Direct extension form changes.
            for ext_config_name, ext_form_qns in ext_forms_by_config.items():
                if not ext_form_qns:
                    continue
                for ext_form_qn in ext_form_qns:
                    pair = self._resolve_form_pair(
                        session,
                        project_name=project_name,
                        ext_config_name=ext_config_name,
                        base_config_name=base_config_name,
                        ext_form_qn=ext_form_qn,
                    )
                    if pair is not None:
                        pairs_by_config.setdefault(ext_config_name, []).append(pair)

            # (б) Base form changes → найти все extension-формы adopted_from через graph.
            if base_forms:
                # Regular forms.
                res = session.run(
                    """
                    UNWIND $base_qns AS base_qn
                    MATCH (ext_f:Form)-[:ADOPTED_FROM]->(base_f:Form {qualified_name: base_qn})
                    WHERE ext_f.project_name = $project_name
                    RETURN ext_f.config_name AS ext_config,
                           ext_f.qualified_name AS ext_form_qn,
                           base_qn AS base_form_qn
                    """,
                    base_qns=list(base_forms),
                    project_name=project_name,
                )
                for rec in res or []:
                    cfg = rec["ext_config"]
                    pair = FormRebuildPair(
                        ext_form_qn=rec["ext_form_qn"],
                        base_form_qn=rec["base_form_qn"],
                        ext_form_owner_qn=rec["ext_form_qn"],
                        base_form_owner_qn=rec["base_form_qn"],
                        is_common_form=False,
                    )
                    pairs_by_config.setdefault(cfg, []).append(pair)

                # CommonForms — base form_qn для common form вида `.../ОбщиеФормы/<name>/Form/Форма`.
                # Owner QN = base form_qn без '/Form/Форма'. Lookup ext common form через MetadataObject ADOPTED_FROM.
                common_base_owners = [
                    qn.split("/Form/", 1)[0] for qn in base_forms if "/ОбщиеФормы/" in qn and "/Form/" in qn
                ]
                if common_base_owners:
                    res = session.run(
                        """
                        UNWIND $base_owners AS base_owner_qn
                        MATCH (ext_mo:MetadataObject)-[:ADOPTED_FROM]->(base_mo:MetadataObject {qualified_name: base_owner_qn})
                        WHERE ext_mo.project_name = $project_name
                          AND ext_mo.category_name = 'ОбщиеФормы'
                        RETURN ext_mo.config_name AS ext_config,
                               ext_mo.qualified_name AS ext_form_owner_qn,
                               base_owner_qn AS base_form_owner_qn
                        """,
                        base_owners=common_base_owners,
                        project_name=project_name,
                    )
                    for rec in res or []:
                        cfg = rec["ext_config"]
                        ext_owner = rec["ext_form_owner_qn"]
                        base_owner = rec["base_form_owner_qn"]
                        pair = FormRebuildPair(
                            ext_form_qn=f"{ext_owner}/Form/Форма",
                            base_form_qn=f"{base_owner}/Form/Форма",
                            ext_form_owner_qn=ext_owner,
                            base_form_owner_qn=base_owner,
                            is_common_form=True,
                        )
                        pairs_by_config.setdefault(cfg, []).append(pair)

            # Per-config rebuild — cleanup + builders с form_pairs scope.
            from .extension_relationships_builder import ExtensionRelationshipsBuilder
            builder = ExtensionRelationshipsBuilder(self)

            for ext_config_name, pairs in pairs_by_config.items():
                # Dedup по (ext_form_qn, base_form_qn).
                seen = set()
                deduped: List[FormRebuildPair] = []
                for p in pairs:
                    key = (p.ext_form_qn, p.base_form_qn)
                    if key in seen:
                        continue
                    seen.add(key)
                    deduped.append(p)

                self._cleanup_form_level_relationships_per_form(
                    session, project_name=project_name, ext_config_name=ext_config_name,
                    pairs=deduped,
                )

                builder.build_adopted_from_for_formcontrols(
                    ext_config_name, base_config_name, form_pairs=deduped,
                )
                builder.build_adopted_from_for_formattributes(
                    ext_config_name, base_config_name, form_pairs=deduped,
                )
                builder.build_adopted_from_for_formcommands(
                    ext_config_name, base_config_name, form_pairs=deduped,
                )
                builder.build_adopted_from_for_formevents(
                    ext_config_name, base_config_name, form_pairs=deduped,
                )
                builder.build_extends_action_for_formevent_actions(
                    ext_config_name, base_config_name, form_pairs=deduped,
                )

    def _resolve_form_pair(
        self,
        session: Any,
        *,
        project_name: str,
        ext_config_name: str,
        base_config_name: str,
        ext_form_qn: str,
    ) -> Optional[Any]:
        """Найти base_form_qn через ADOPTED_FROM, либо deterministic projection.

        Если ADOPTED_FROM lookup пуст — пытаемся вычислить ожидаемый base QN
        заменой ext_config_name на base_config_name в QN и проверить существование
        соответствующего узла. Если ни graph link, ни projected узел не дают base
        — pair не создаётся (форма считается own).
        """
        from incremental.report import FormRebuildPair

        is_common = "/ОбщиеФормы/" in ext_form_qn and "/Form/" in ext_form_qn
        ext_owner = ext_form_qn.split("/Form/", 1)[0] if is_common else ext_form_qn

        if is_common:
            rec = session.run(
                """
                MATCH (ext_mo:MetadataObject {qualified_name: $ext_owner, project_name: $project_name})
                OPTIONAL MATCH (ext_mo)-[:ADOPTED_FROM]->(base_mo:MetadataObject)
                RETURN base_mo.qualified_name AS base_owner_qn
                """,
                ext_owner=ext_owner, project_name=project_name,
            ).single()
            base_owner = rec["base_owner_qn"] if rec else None
            if not base_owner:
                # Deterministic projection.
                expected = ext_owner.replace(f"/{ext_config_name}/", f"/{base_config_name}/", 1)
                if expected == ext_owner:
                    return None
                exists = session.run(
                    """
                    MATCH (b:MetadataObject {qualified_name: $expected, category_name: 'ОбщиеФормы'})
                    RETURN count(b) > 0 AS exists
                    """,
                    expected=expected,
                ).single()
                if not (exists and exists["exists"]):
                    return None
                base_owner = expected
            return FormRebuildPair(
                ext_form_qn=ext_form_qn,
                base_form_qn=f"{base_owner}/Form/Форма",
                ext_form_owner_qn=ext_owner,
                base_form_owner_qn=base_owner,
                is_common_form=True,
            )

        # Regular form.
        rec = session.run(
            """
            MATCH (ext_f:Form {qualified_name: $ext_form_qn, project_name: $project_name})
            OPTIONAL MATCH (ext_f)-[:ADOPTED_FROM]->(base_f:Form)
            RETURN base_f.qualified_name AS base_form_qn
            """,
            ext_form_qn=ext_form_qn, project_name=project_name,
        ).single()
        base_qn = rec["base_form_qn"] if rec else None
        if not base_qn:
            expected = ext_form_qn.replace(f"/{ext_config_name}/", f"/{base_config_name}/", 1)
            if expected == ext_form_qn:
                return None
            exists = session.run(
                """
                MATCH (b:Form {qualified_name: $expected})
                RETURN count(b) > 0 AS exists
                """,
                expected=expected,
            ).single()
            if not (exists and exists["exists"]):
                return None
            base_qn = expected

        return FormRebuildPair(
            ext_form_qn=ext_form_qn,
            base_form_qn=base_qn,
            ext_form_owner_qn=ext_form_qn,
            base_form_owner_qn=base_qn,
            is_common_form=False,
        )

    def _cleanup_form_level_relationships_per_form(
        self,
        session: Any,
        *,
        project_name: str,
        ext_config_name: str,
        pairs: List[Any],
    ) -> None:
        """Удалить ADOPTED_FROM / EXTENDS_ACTION для конкретных extension-форм.

        Cleanup-паттерны зеркалят rebuild builders (R3 finding 2: form-level
        Command — только через Form/MetadataObject ОбщиеФормы, не сносим
        metadata-level object commands). Cleanup scope-ed per-form чтобы не
        задеть формы вне `form_pairs`.
        """
        regular_ext_qns = [p.ext_form_qn for p in pairs if not p.is_common_form]
        common_ext_owner_qns = [p.ext_form_owner_qn for p in pairs if p.is_common_form]

        if regular_ext_qns:
            session.run(
                """
                UNWIND $qns AS ext_form_qn
                MATCH (ext_f:Form {qualified_name: ext_form_qn, project_name: $project_name, config_name: $ext_config_name})
                OPTIONAL MATCH (ext_f)-[:HAS_CONTROL|HAS_CHILD*]->(fc:FormControl)-[r1:ADOPTED_FROM]->()
                OPTIONAL MATCH (ext_f)-[:HAS_FORM_ATTRIBUTE]->(fa:FormAttribute)-[r2:ADOPTED_FROM]->()
                OPTIONAL MATCH (ext_f)-[:HAS_COMMAND]->(cmd:Command)-[r3:ADOPTED_FROM]->()
                OPTIONAL MATCH (ext_f)-[:HAS_EVENT]->(fe1:FormEvent)-[r4:ADOPTED_FROM]->()
                OPTIONAL MATCH (ext_f)-[:HAS_CONTROL|HAS_CHILD*]->(:FormControl)-[:HAS_EVENT]->(fe2:FormEvent)-[r5:ADOPTED_FROM]->()
                OPTIONAL MATCH (ext_f)-[:HAS_EVENT]->(:FormEvent)-[:HAS_EVENT_ACTION]->(fea1:FormEventAction)-[r6:EXTENDS_ACTION]->()
                OPTIONAL MATCH (ext_f)-[:HAS_CONTROL|HAS_CHILD*]->(:FormControl)-[:HAS_EVENT]->(:FormEvent)-[:HAS_EVENT_ACTION]->(fea2:FormEventAction)-[r7:EXTENDS_ACTION]->()
                DELETE r1, r2, r3, r4, r5, r6, r7
                """,
                qns=list(regular_ext_qns),
                project_name=project_name,
                ext_config_name=ext_config_name,
            )

        if common_ext_owner_qns:
            session.run(
                """
                UNWIND $owner_qns AS owner_qn
                MATCH (mo:MetadataObject {qualified_name: owner_qn, project_name: $project_name,
                                          config_name: $ext_config_name, category_name: 'ОбщиеФормы'})
                OPTIONAL MATCH (mo)-[:HAS_CONTROL|HAS_CHILD*]->(fc:FormControl)-[r1:ADOPTED_FROM]->()
                OPTIONAL MATCH (mo)-[:HAS_FORM_ATTRIBUTE]->(fa:FormAttribute)-[r2:ADOPTED_FROM]->()
                OPTIONAL MATCH (mo)-[:HAS_COMMAND]->(cmd:Command)-[r3:ADOPTED_FROM]->()
                OPTIONAL MATCH (mo)-[:HAS_EVENT]->(fe1:FormEvent)-[r4:ADOPTED_FROM]->()
                OPTIONAL MATCH (mo)-[:HAS_CONTROL|HAS_CHILD*]->(:FormControl)-[:HAS_EVENT]->(fe2:FormEvent)-[r5:ADOPTED_FROM]->()
                OPTIONAL MATCH (mo)-[:HAS_EVENT]->(:FormEvent)-[:HAS_EVENT_ACTION]->(fea1:FormEventAction)-[r6:EXTENDS_ACTION]->()
                OPTIONAL MATCH (mo)-[:HAS_CONTROL|HAS_CHILD*]->(:FormControl)-[:HAS_EVENT]->(:FormEvent)-[:HAS_EVENT_ACTION]->(fea2:FormEventAction)-[r7:EXTENDS_ACTION]->()
                DELETE r1, r2, r3, r4, r5, r6, r7
                """,
                owner_qns=list(common_ext_owner_qns),
                project_name=project_name,
                ext_config_name=ext_config_name,
            )

    def refresh_extension_routine_links(
        self,
        project_name: str,
        ext_config_name: str,
        base_config_name: str,  # noqa: ARG002 — используется существующим create_extension_routine_links
        routine_ids: List[str],
        module_ids: List[str],  # noqa: ARG002
    ) -> None:
        """Удалить старые EXTENDS_ROUTINE у affected ext routines. Re-merge зовёт caller
        через [create_extension_routine_links](app/graphdb/bsl_loader.py#L615).
        """
        if not routine_ids:
            return
        with self.driver.session(database=settings.neo4j_database) as session:
            session.run(
                """
                UNWIND $ids AS rid
                MATCH (r:Routine {id: rid})
                WHERE r.project_name = $project_name AND r.config_name = $ext_config_name
                MATCH (r)-[ext:EXTENDS_ROUTINE]->()
                DELETE ext
                """,
                ids=list(routine_ids),
                project_name=project_name,
                ext_config_name=ext_config_name,
            )

    def refresh_extension_module_links(
        self,
        project_name: str,
        ext_config_name: str,
        base_config_name: str,  # noqa: ARG002
        module_ids: List[str],
    ) -> None:
        if not module_ids:
            return
        with self.driver.session(database=settings.neo4j_database) as session:
            session.run(
                """
                UNWIND $ids AS mid
                MATCH (m:Module {id: mid})
                WHERE m.project_name = $project_name AND m.config_name = $ext_config_name
                MATCH (m)-[ext:EXTENDS_MODULE]->()
                DELETE ext
                """,
                ids=list(module_ids),
                project_name=project_name,
                ext_config_name=ext_config_name,
            )
