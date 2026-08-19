"""
Reusable Cypher templates for Neo4j loader.
Consolidates often repeated multi-line Cypher statements as constants or small factories.
"""
from __future__ import annotations
from typing import Final

# General maintenance
CYPHER_CLEAR_DATABASE: Final[str] = "MATCH (n) DETACH DELETE n"

# NOTE: CYPHER_CLEAR_PROJECT was removed - clear_project() now uses batched deletion
# directly in core.py to avoid OOM on large projects (4GB transaction memory limit)

# Project / Configuration upserts
CYPHER_MERGE_PROJECT: Final[str] = """
MERGE (p:Project {name: $project_name})
RETURN p
"""

CYPHER_UPSERT_CONFIGURATION: Final[str] = """
MATCH (p:Project {name: $project_name})
MERGE (c:Configuration {qualified_name: $config_qn})
ON CREATE SET
    c.name = $config_name,
    c.project_name = $project_name,
    c.is_extension = $is_extension
SET c += $properties
MERGE (p)-[:HAS_CONFIGURATION]->(c)
"""

# Categories
CYPHER_UPSERT_CATEGORIES: Final[str] = """
UNWIND $rows AS row
MATCH (c:Configuration {qualified_name: row.config_qn})
MERGE (cat:MetadataCategory {qualified_name: row.category_qn})
ON CREATE SET
    cat.name = row.category_name,
    cat.project_name = $project_name,
    cat.config_name = c.name
MERGE (c)-[:HAS_CATEGORY]->(cat)
"""

# MetadataObject and related entities
CYPHER_UPSERT_METADATA_OBJECT: Final[str] = """
UNWIND $rows AS row
MATCH (cat:MetadataCategory {qualified_name: row.category_qn})
MERGE (m:MetadataObject {qualified_name: row.obj_qn})
ON CREATE SET
    m.name = row.obj_name,
    m.project_name = row.project_name,
    m.config_name = row.config_name,
    m.category_name = row.category_name
SET m += row.properties
SET m:ConsoleSearchable
FOREACH (_ IN CASE WHEN NOT (row.category_name = 'Подсистемы' AND row.properties.`РодительскаяПодсистема` IS NOT NULL) THEN [1] ELSE [] END |
    MERGE (cat)-[:CONTAINS_OBJECT]->(m)
)
"""

CYPHER_UPSERT_SUBSYSTEM_EDGE: Final[str] = """
UNWIND $rows AS row
MATCH (p:MetadataObject {qualified_name: row.parent_qn})
MATCH (c:MetadataObject {qualified_name: row.child_qn})
MERGE (p)-[:CONTAINS_OBJECT]->(c)
"""

CYPHER_UPSERT_FORM: Final[str] = """
UNWIND $rows AS row
MATCH (m:MetadataObject {qualified_name: row.obj_qn})
MERGE (f:Form {qualified_name: row.form_qn})
ON CREATE SET
    f.name = row.form_name,
    f.project_name = row.project_name,
    f.config_name = row.config_name,
    f.category_name = row.category_name,
    f.object_name = row.object_name
SET f += row.properties
SET f:ConsoleSearchable
MERGE (m)-[rel:HAS_FORM]->(f)
SET rel.role = row.role,
    rel.is_default = coalesce(row.is_default, false)
"""

CYPHER_FORMS_DEFAULT_CLEANUP: Final[str] = """
UNWIND $rows AS row
MATCH (m:MetadataObject {qualified_name: row.obj_qn})-[r:HAS_FORM]->(f:Form)
WHERE r.role = row.role AND f.qualified_name <> row.form_qn AND r.is_default = true
SET r.is_default = false
"""

CYPHER_UPSERT_COMMAND: Final[str] = """
UNWIND $rows AS row
MATCH (m:MetadataObject {qualified_name: row.obj_qn})
MERGE (c:Command {qualified_name: row.cmd_qn})
ON CREATE SET
    c.name = row.cmd_name,
    c.project_name = row.project_name,
    c.config_name = row.config_name,
    c.category_name = row.category_name,
    c.object_name = row.object_name
SET c += row.properties
SET c:ConsoleSearchable
MERGE (m)-[:HAS_COMMAND]->(c)
"""

CYPHER_UPSERT_LAYOUT: Final[str] = """
UNWIND $rows AS row
MATCH (m:MetadataObject {qualified_name: row.obj_qn})
MERGE (l:Layout {qualified_name: row.layout_qn})
ON CREATE SET
    l.name = row.layout_name,
    l.project_name = row.project_name,
    l.config_name = row.config_name,
    l.category_name = row.category_name,
    l.object_name = row.object_name
SET l += row.properties
SET l:ConsoleSearchable
MERGE (m)-[:HAS_LAYOUT]->(l)
"""

CYPHER_UPSERT_CHARACTERISTIC: Final[str] = """
UNWIND $rows AS row
MATCH (m:MetadataObject {qualified_name: row.obj_qn})
MERGE (s:Characteristic {qualified_name: row.scheme_qn})
ON CREATE SET
    s.name = row.scheme_name,
    s.project_name = row.project_name,
    s.config_name = row.config_name,
    s.category_name = row.category_name,
    s.object_name = row.object_name
SET s += row.properties
MERGE (m)-[:HAS_CHARACTERISTIC]->(s)
"""

CYPHER_UPSERT_ENUM_VALUE: Final[str] = """
UNWIND $rows AS row
MATCH (m:MetadataObject {qualified_name: row.obj_qn})
MERGE (v:EnumValue {qualified_name: row.value_qn})
ON CREATE SET
    v.name = row.value_name,
    v.project_name = row.project_name,
    v.config_name = row.config_name,
    v.category_name = row.category_name,
    v.object_name = row.object_name
SET v += row.properties
SET v:ConsoleSearchable
MERGE (m)-[:HAS_ENUM_VALUE]->(v)
"""

CYPHER_UPSERT_URL_TEMPLATE: Final[str] = """
UNWIND $rows AS row
MATCH (m:MetadataObject {qualified_name: row.obj_qn})
MERGE (t:UrlTemplate {qualified_name: row.template_qn})
ON CREATE SET
    t.name = row.template_name,
    t.project_name = row.project_name,
    t.config_name = row.config_name,
    t.category_name = row.category_name,
    t.object_name = row.object_name
SET t += row.properties
MERGE (m)-[:HAS_URL_TEMPLATE]->(t)
"""

CYPHER_UPSERT_URL_METHOD: Final[str] = """
UNWIND $rows AS row
MATCH (t:UrlTemplate {qualified_name: row.template_qn})
MERGE (m:UrlMethod {qualified_name: row.method_qn})
ON CREATE SET
    m.name = row.method_name,
    m.project_name = row.project_name,
    m.config_name = row.config_name,
    m.category_name = row.category_name,
    m.object_name = row.object_name
SET m += row.properties
MERGE (t)-[:HAS_URL_METHOD]->(m)
"""

# Link UrlMethod to Routine by explicit handler name (owner = the HTTP service MetadataObject).
# Only when handler is non-empty; take last token if handler contains dots.
CYPHER_LINK_URLMETHOD_HANDLER_EXPLICIT: Final[str] = """
MATCH (:MetadataCategory {name:'HTTPСервисы'})-[:CONTAINS_OBJECT]->(s:MetadataObject {project_name: $project_name, config_name: $config_name})
-[:HAS_URL_TEMPLATE]->(:UrlTemplate)-[:HAS_URL_METHOD]->(m:UrlMethod)
WHERE m.`Обработчик` IS NOT NULL AND trim(m.`Обработчик`) <> ''
WITH s, m, split(m.`Обработчик`, '.') AS hp
WITH s, m, CASE WHEN size(hp) >= 1 THEN hp[size(hp)-1] ELSE m.`Обработчик` END AS routine_name
MATCH (r:Routine {project_name: $project_name, config_name: $config_name, owner_qn: s.qualified_name, name: routine_name})
WITH m, r
MERGE (m)-[:HAS_HANDLER]->(r)
RETURN count(m) AS matched
"""

CYPHER_UPSERT_JOURNAL_GRAPH: Final[str] = """
UNWIND $rows AS row
MATCH (m:MetadataObject {qualified_name: row.obj_qn})
MERGE (g:JournalGraph {qualified_name: row.graph_qn})
ON CREATE SET
    g.name = row.graph_name,
    g.project_name = row.project_name,
    g.config_name = row.config_name,
    g.category_name = row.category_name,
    g.object_name = row.object_name
SET g += row.properties
SET g:ConsoleSearchable
MERGE (m)-[:HAS_GRAPH]->(g)
"""

CYPHER_UPSERT_DO_MOVEMENTS_IN: Final[str] = """
UNWIND $rows AS row
MATCH (d:MetadataObject {qualified_name: row.doc_qn})
MATCH (r:MetadataObject {qualified_name: row.reg_qn})
MERGE (d)-[rel:DO_MOVEMENTS_IN]->(r)
SET rel.`Проведение` = row.`Проведение`,
    rel.`ЗаписьДвиженийПриПроведении` = row.`ЗаписьДвиженийПриПроведении`,
    rel.`УдалениеДвижений` = row.`УдалениеДвижений`,
    rel.`ВидРегистра` = row.`ВидРегистра`
"""

CYPHER_UPSERT_ACCOUNTING_FLAG: Final[str] = """
UNWIND $rows AS row
MATCH (m:MetadataObject {qualified_name: row.obj_qn})
MERGE (af:AccountingFlag {qualified_name: row.flag_qn})
ON CREATE SET
    af.name = row.flag_name,
    af.project_name = row.project_name,
    af.config_name = row.config_name,
    af.category_name = row.category_name,
    af.object_name = row.object_name
SET af += row.properties
MERGE (m)-[:HAS_ACCOUNTING_FLAG]->(af)
"""

CYPHER_UPSERT_DIMENSION_ACCOUNTING_FLAG: Final[str] = """
UNWIND $rows AS row
MATCH (m:MetadataObject {qualified_name: row.obj_qn})
MERGE (sf:DimensionAccountingFlag {qualified_name: row.flag_qn})
ON CREATE SET
    sf.name = row.flag_name,
    sf.project_name = row.project_name,
    sf.config_name = row.config_name,
    sf.category_name = row.category_name,
    sf.object_name = row.object_name
SET sf += row.properties
MERGE (m)-[:HAS_DIMENSION_ACCOUNTING_FLAG]->(sf)
"""

CYPHER_UPSERT_TABULAR_PART: Final[str] = """
UNWIND $rows AS row
MATCH (m:MetadataObject {qualified_name: row.obj_qn})
MERGE (t:TabularPart {qualified_name: row.tab_qn})
ON CREATE SET
    t.name = row.tabular_name,
    t.project_name = row.project_name,
    t.config_name = row.config_name,
    t.category_name = row.category_name,
    t.object_name = row.obj_name
SET t += row.properties
SET t:ConsoleSearchable
MERGE (m)-[:HAS_TABULAR_PART]->(t)
"""

CYPHER_UPSERT_OBJECT_ATTRIBUTE: Final[str] = """
UNWIND $rows AS row
MATCH (m:MetadataObject {qualified_name: row.obj_qn})
MERGE (a:Attribute {qualified_name: row.attr_qn})
ON CREATE SET
    a.name = row.attr_name,
    a.project_name = row.project_name,
    a.config_name = row.config_name,
    a.category_name = row.category_name,
    a.object_name = row.object_name
SET a += row.properties
SET a:ConsoleSearchable
MERGE (m)-[:HAS_ATTRIBUTE]->(a)
"""

CYPHER_UPSERT_RESOURCE: Final[str] = """
UNWIND $rows AS row
MATCH (m:MetadataObject {qualified_name: row.obj_qn})
MERGE (r:Resource {qualified_name: row.res_qn})
ON CREATE SET
    r.name = row.res_name,
    r.project_name = row.project_name,
    r.config_name = row.config_name,
    r.category_name = row.category_name,
    r.object_name = row.object_name
SET r += row.properties
SET r:ConsoleSearchable
MERGE (m)-[:HAS_RESOURCE]->(r)
"""

CYPHER_UPSERT_DIMENSION: Final[str] = """
UNWIND $rows AS row
MATCH (m:MetadataObject {qualified_name: row.obj_qn})
MERGE (d:Dimension {qualified_name: row.dim_qn})
ON CREATE SET
    d.name = row.dim_name,
    d.project_name = row.project_name,
    d.config_name = row.config_name,
    d.category_name = row.category_name,
    d.object_name = row.object_name
SET d += row.properties
SET d:ConsoleSearchable
MERGE (m)-[:HAS_DIMENSION]->(d)
"""

CYPHER_UPSERT_TABULAR_ATTRIBUTE: Final[str] = """
UNWIND $rows AS row
MATCH (t:TabularPart {qualified_name: row.tab_qn})
MERGE (a:Attribute {qualified_name: row.attr_qn})
ON CREATE SET
    a.name = row.attr_name,
    a.project_name = row.project_name,
    a.config_name = row.config_name,
    a.category_name = row.category_name,
    a.object_name = row.object_name,
    a.tabular_name = row.tabular_name
SET a += row.properties
SET a:ConsoleSearchable
MERGE (t)-[:HAS_ATTRIBUTE]->(a)
"""

def cypher_used_in(consumer_label: str) -> str:
    """
    Build USED_IN template for consumer label: Attribute, Resource, Dimension,
    AccountingFlag, DimensionAccountingFlag, FormAttribute.
    """
    return f"""
    UNWIND $rows AS row
    MATCH (consumer:{consumer_label} {{qualified_name: row.consumer_qn}})
    MATCH (target:MetadataObject {{qualified_name: row.target_qn}})
    MERGE (target)-[r:USED_IN]->(consumer)
    ON CREATE SET r.context = row.context,
                  r.prop_key = row.prop_key,
                  r.type_literal = row.type_literal
    """

# Predefined items
CYPHER_PREDEFINED_UPSERT_ITEM: Final[str] = """
UNWIND $rows AS row
MATCH (m:MetadataObject {qualified_name: row.obj_qn})
MERGE (p:PredefinedItem {qualified_name: row.predef_qn})
ON CREATE SET
    p.project_name = row.project_name,
    p.config_name  = row.config_name,
    p.category_name = row.category_name,
    p.object_name   = row.object_name,
    p.name          = coalesce(row.properties['Имя'], row.predef_qn)
SET p += row.properties
SET p:ConsoleSearchable
MERGE (m)-[:HAS_PREDEFINED]->(p)
"""

CYPHER_PREDEFINED_LINK_CHILD: Final[str] = """
UNWIND $rows AS row
WITH row,
     row.project_name + '/' + row.config_name + '/' + row.category_name + '/' + row.object_name AS obj_qn
MATCH (parent:PredefinedItem {qualified_name: obj_qn + '/Predef/' + row.parent_local_id})
MATCH (child:PredefinedItem  {qualified_name: obj_qn + '/Predef/' + row.local_id})
MERGE (parent)-[:HAS_CHILD]->(child)
"""

# Role rights
CYPHER_ROLE_RIGHTS_GRANTS_ACCESS_SIMPLE: Final[str] = """
UNWIND $rows AS row
MATCH (r:MetadataObject {qualified_name: row.role_qn})
MATCH (o:MetadataObject {qualified_name: row.object_qn})
MERGE (r)-[rel:GRANTS_ACCESS_TO {object_qn: row.object_qn, right: row.right_ru}]->(o)
SET rel.allowed   = row.allowed,
    rel.right_en  = row.right_en,
    rel.condition = row.condition,
    rel.object_full = row.object_full,
    rel.has_condition = CASE WHEN row.condition IS NULL OR trim(row.condition) = '' THEN false ELSE true END
"""

CYPHER_ROLE_RIGHTS_UPDATE_ROLE_FLAGS: Final[str] = """
UNWIND $rows AS row
MATCH (r:MetadataObject {qualified_name: row.role_qn})
SET r.setForNewObjects = row.setForNewObjects,
    r.setForAttributesByDefault = row.setForAttributesByDefault,
    r.independentRightsOfChildObjects = row.independentRightsOfChildObjects
"""

def cypher_role_rights_grants_access_to(target_label: str) -> str:
    return f"""
    UNWIND $rows AS row
    MATCH (r:MetadataObject {{qualified_name: row.role_qn}})
    MATCH (t:{target_label} {{qualified_name: row.target_qn}})
    MERGE (r)-[rel:GRANTS_ACCESS_TO]->(t)
    SET rel += row.props
    """

def cypher_role_rights_grants_access_to_ext(label: str) -> str:
    # Configuration nodes use EXTENDS, not ADOPTED_FROM — always resolve to base directly.
    if label == "Configuration":
        return f"""
UNWIND $rows AS row
MATCH (r:MetadataObject {{qualified_name: row.role_qn}})
OPTIONAL MATCH (tb:{label} {{qualified_name: row.target_qn_base}})
WITH r, row, tb AS t
WHERE t IS NOT NULL
MERGE (r)-[rel:GRANTS_ACCESS_TO]->(t)
SET rel += row.props
SET rel.target_qn = t.qualified_name
"""
    return f"""
UNWIND $rows AS row
MATCH (r:MetadataObject {{qualified_name: row.role_qn}})
OPTIONAL MATCH (te:{label} {{qualified_name: row.target_qn}})
OPTIONAL MATCH (te)-[:ADOPTED_FROM]->(tb_adopted:{label})
WITH r, row, te, tb_adopted,
     CASE WHEN te IS NULL THEN row.target_qn_base ELSE null END AS fallback_qn
OPTIONAL MATCH (tb_direct:{label} {{qualified_name: fallback_qn}})
WITH r, row, coalesce(tb_adopted, te, tb_direct) AS t
WHERE t IS NOT NULL
MERGE (r)-[rel:GRANTS_ACCESS_TO]->(t)
SET rel += row.props
SET rel.target_qn = t.qualified_name
"""

# Event subscriptions
CYPHER_EVENT_SUBSCRIPTION_UPSERT_NODE: Final[str] = """
UNWIND $rows AS row
MATCH (es:MetadataObject {qualified_name: row.meta_qn})
SET es += row.properties
"""

CYPHER_EVENT_SUBSCRIPTION_LINK_TO_OBJECT: Final[str] = """
UNWIND $rows AS row
MATCH (es:MetadataObject {qualified_name: row.meta_qn})
MATCH (target:MetadataObject {qualified_name: row.target_qn})
MERGE (target)-[rel:HAS_EVENT_SUBSCRIPTION]->(es)
SET rel.source_type = row.source_type
"""

# Cleanup canonical event subscription edges под `MetadataObject {category_name='ПодпискиНаСобытия'}`
# + `USES_HANDLER` + входящее `HAS_EVENT_SUBSCRIPTION`. SUBSCRIBES_TO и HAS_SOURCE —
# legacy ребра, никем не создаются (см. grep по app/graphdb).
CYPHER_DELETE_EVENT_SUBSCRIPTION_LINKS: Final[str] = """
UNWIND $qns AS sub_qn
MATCH (es:MetadataObject {qualified_name: sub_qn})
WHERE es.project_name = $project_name
  AND es.config_name = $config_name
  AND es.category_name = 'ПодпискиНаСобытия'
OPTIONAL MATCH (es)-[r1:USES_HANDLER]->()
OPTIONAL MATCH ()-[r2:HAS_EVENT_SUBSCRIPTION]->(es)
DELETE r1, r2
"""

# Scoped handler relink для конкретных subscription QN (используется в
# `replace_event_subscriptions` для optimization). Не correctness mechanism —
# same-cycle BSL Routine introduction закрывается config-level pass в
# PostLinkingSync phase 4 после BSL apply.
CYPHER_LINK_AFFECTED_EVENT_SUBSCRIPTIONS_TO_HANDLERS: Final[str] = """
UNWIND $qns AS sub_qn
MATCH (es:MetadataObject {qualified_name: sub_qn})
WHERE es.project_name = $project_name
  AND es.config_name = $config_name
  AND es.category_name = 'ПодпискиНаСобытия'
  AND es.`Обработчик` IS NOT NULL
WITH es, split(es.Обработчик, '.') AS hp
WITH es,
     CASE WHEN size(hp) >= 3 AND hp[0] = 'CommonModule' THEN hp[1]
          WHEN size(hp) >= 2 THEN hp[size(hp) - 2]
          ELSE '' END AS module_name,
     CASE WHEN size(hp) >= 1 THEN hp[size(hp) - 1] ELSE '' END AS routine_name
WHERE module_name <> '' AND routine_name <> ''
MATCH (r:Routine {name: routine_name, project_name: $project_name, config_name: $config_name})
WHERE r.file_path CONTAINS module_name
MERGE (es)-[:USES_HANDLER]->(r)
"""

# Help content update for MetadataObject
CYPHER_HELP_UPDATE_OBJECT: Final[str] = """
UNWIND $rows AS row
MATCH (m:MetadataObject {qualified_name: row.obj_qn})
SET m.`Справка` = row.help_content
"""

# ----- Forms (Ext/Form.xml) templates -----

CYPHER_FORM_UPDATE_PROPS_REGULAR = """
UNWIND $rows AS row
MATCH (f:Form {qualified_name: row.form_qn})
SET f += row.properties
WITH f,
     toString(coalesce(f.`Синоним`, f.`Заголовок`, '')) AS syn_final,
     toString(coalesce(f.`ТипФормы`, f.`Действие`, '')) AS type_final
SET f.console_search_synonym = syn_final,
    f.console_search_synonym_norm = replace(replace(replace(replace(toLower(syn_final), 'ё', 'е'), ' ', ''), '_', ''), '-', ''),
    f.console_search_type = type_final,
    f.console_search_type_norm = replace(replace(replace(replace(toLower(type_final), 'ё', 'е'), ' ', ''), '_', ''), '-', '')
"""

CYPHER_FORM_UPDATE_PROPS_COMMONFORM = """
UNWIND $rows AS row
WITH row, split(row.form_qn, '/Form/') AS parts
WITH row, parts[0] AS obj_qn
MATCH (m:MetadataObject {qualified_name: obj_qn})
SET m += row.properties
WITH m,
     toString(coalesce(m.`Синоним`, m.`Заголовок`, '')) AS syn_final,
     toString(coalesce(m.`ТипФормы`, m.`Действие`, '')) AS type_final
SET m.console_search_synonym = syn_final,
    m.console_search_synonym_norm = replace(replace(replace(replace(toLower(syn_final), 'ё', 'е'), ' ', ''), '_', ''), '-', ''),
    m.console_search_type = type_final,
    m.console_search_type_norm = replace(replace(replace(replace(toLower(type_final), 'ё', 'е'), ' ', ''), '_', ''), '-', '')
"""

CYPHER_UPSERT_FORMCONTROL = """
UNWIND $rows AS row
MERGE (fc:FormControl {qualified_name: row.qn})
ON CREATE SET
    fc.name = row.name,
    fc.type = row.type
SET fc += row.properties
SET fc:ConsoleSearchable
"""

CYPHER_LINK_HAS_CONTROL_REGULAR = """
UNWIND $rows AS row
MATCH (f:Form {qualified_name: row.form_qn})
MATCH (c:FormControl {qualified_name: row.control_qn})
MERGE (f)-[r:HAS_CONTROL]->(c)
SET r.order = row.order
"""

CYPHER_LINK_HAS_CONTROL_COMMONFORM = """
UNWIND $rows AS row
WITH row, split(row.form_qn, '/Form/') AS parts
WITH row, parts[0] AS obj_qn
MATCH (m:MetadataObject {qualified_name: obj_qn})
MATCH (c:FormControl {qualified_name: row.control_qn})
MERGE (m)-[r:HAS_CONTROL]->(c)
SET r.order = row.order
"""

CYPHER_LINK_HAS_CHILD_CONTROL = """
UNWIND $rows AS row
MATCH (p:FormControl {qualified_name: row.parent_qn})
MATCH (c:FormControl {qualified_name: row.child_qn})
MERGE (p)-[r:HAS_CHILD]->(c)
SET r.order = row.order
"""

CYPHER_UPSERT_FORMEVENT = """
UNWIND $rows AS row
MERGE (e:FormEvent {qualified_name: row.qn})
SET e += row.properties,
    e.name = coalesce(row.properties['Имя'], row.properties['name'], e.name)
"""

CYPHER_LINK_HAS_EVENT_FORM = """
UNWIND $rows AS row
MATCH (src:Form {qualified_name: row.source_qn})
MATCH (e:FormEvent {qualified_name: row.event_qn})
MERGE (src)-[:HAS_EVENT]->(e)
"""

CYPHER_LINK_HAS_EVENT_COMMONFORM = """
UNWIND $rows AS row
WITH row, split(row.source_qn, '/Form/') AS parts
WITH row, parts[0] AS obj_qn
MATCH (src:MetadataObject {qualified_name: obj_qn})
MATCH (e:FormEvent {qualified_name: row.event_qn})
MERGE (src)-[:HAS_EVENT]->(e)
"""

CYPHER_LINK_HAS_EVENT_CONTROL = """
UNWIND $rows AS row
MATCH (src:FormControl {qualified_name: row.source_qn})
MATCH (e:FormEvent {qualified_name: row.event_qn})
MERGE (src)-[:HAS_EVENT]->(e)
"""

CYPHER_UPSERT_FORMATTR_AND_LINK_REGULAR = """
UNWIND $rows AS row
MATCH (f:Form {qualified_name: row.form_qn})
MERGE (fa:FormAttribute {qualified_name: row.qn})
ON CREATE SET fa.name = coalesce(row.name, '')
SET fa += row.properties
SET fa:ConsoleSearchable
MERGE (f)-[:HAS_FORM_ATTRIBUTE]->(fa)
"""

CYPHER_UPSERT_FORMATTR_AND_LINK_COMMONFORM = """
UNWIND $rows AS row
WITH row, split(row.form_qn, '/Form/') AS parts
WITH row, parts[0] AS obj_qn
MATCH (m:MetadataObject {qualified_name: obj_qn})
MERGE (fa:FormAttribute {qualified_name: row.qn})
ON CREATE SET fa.name = coalesce(row.name, '')
SET fa += row.properties
SET fa:ConsoleSearchable
MERGE (m)-[:HAS_FORM_ATTRIBUTE]->(fa)
"""

CYPHER_UPSERT_FORM_COMMAND_REGULAR = """
UNWIND $rows AS row
MATCH (f:Form {qualified_name: row.form_qn})
MERGE (c:Command {qualified_name: row.cmd_qn})
ON CREATE SET c.name = row.cmd_name
SET c += row.properties
SET c:ConsoleSearchable
MERGE (f)-[:HAS_COMMAND]->(c)
"""

CYPHER_UPSERT_FORM_COMMAND_COMMONFORM = """
UNWIND $rows AS row
WITH row, split(row.form_qn, '/Form/') AS parts
WITH row, parts[0] AS obj_qn
MATCH (m:MetadataObject {qualified_name: obj_qn})
MERGE (c:Command {qualified_name: row.cmd_qn})
ON CREATE SET c.name = row.cmd_name
SET c += row.properties
SET c:ConsoleSearchable
MERGE (m)-[:HAS_COMMAND]->(c)
"""

CYPHER_LINKS_TO_COMMAND = """
UNWIND $rows AS row
MATCH (fc:FormControl {qualified_name: row.container_qn})
MATCH (cmd:Command {qualified_name: row.cmd_qn})
MERGE (fc)-[r:LINKS_TO_COMMAND {key: row.rel_key}]->(cmd)
SET r.via = row.via,
    r.button_id = row.button_id,
    r.button_name = row.button_name
"""

def cypher_binds_to(target_label: str) -> str:
    return f"""
    UNWIND $rows AS row
    MATCH (fc:FormControl {{qualified_name: row.container_qn}})
    MATCH (t:{target_label} {{qualified_name: row.target_qn}})
    MERGE (fc)-[r:BINDS_TO]->(t)
    SET r.via = row.via,
        r.raw = row.raw,
        r.resolved = coalesce(row.resolved, true),
        r.resolution = row.resolution
    """

# ----- BSL (modules/routines) templates -----

CYPHER_UPSERT_MODULE = """
UNWIND $rows AS row
MERGE (m:Module {id: row.id})
ON CREATE SET
    m.project_name = row.project_name,
    m.config_name  = row.config_name,
    m.module_type  = row.module_type
SET m.path = row.path,
    m.owner_kind = row.owner_kind,
    m.owner_name = row.owner_name,
    m.owner_qn   = row.owner_qn,
    m.name       = row.name
"""

CYPHER_LINK_MODULE_OWNER_FORM = """
UNWIND $rows AS row
WITH row WHERE row.owner_label = 'Form'
MATCH (m:Module {id: row.id})
MATCH (o:Form {qualified_name: row.owner_qn})
MERGE (o)-[:HAS_MODULE]->(m)
"""

CYPHER_LINK_MODULE_OWNER_METADATAOBJECT = """
UNWIND $rows AS row
WITH row WHERE row.owner_label = 'MetadataObject'
MATCH (m:Module {id: row.id})
MATCH (o:MetadataObject {qualified_name: row.owner_qn})
MERGE (o)-[:HAS_MODULE]->(m)
"""

CYPHER_LINK_MODULE_OWNER_CONFIGURATION = """
UNWIND $rows AS row
WITH row WHERE row.owner_label = 'Configuration'
MATCH (m:Module {id: row.id})
MATCH (o:Configuration {qualified_name: row.owner_qn})
MERGE (o)-[:HAS_MODULE]->(m)
"""

CYPHER_LINK_MODULE_OWNER_COMMAND = """
UNWIND $rows AS row
WITH row WHERE row.owner_label = 'Command'
MATCH (m:Module {id: row.id})
MATCH (o:Command {qualified_name: row.owner_qn})
MERGE (o)-[:HAS_MODULE]->(m)
"""

CYPHER_UPSERT_ROUTINE = """
UNWIND $rows AS row
MERGE (r:Routine {id: row.id})
ON CREATE SET
    r.project_name = row.project_name,
    r.config_name  = row.config_name
SET r.name            = row.name,
    // Lowercased copies for search predicates: `toLower(r.name) = ...` cannot use an index
    // (Neo4j has no functional indexes), so the lowering is done once here at write time.
    // Plain toLower only — no ё/space/punctuation folding, that would change match results.
    r.name_norm       = toLower(coalesce(row.name, '')),
    r.signature_norm  = toLower(coalesce(row.signature, '')),
    r.routine_type    = row.routine_type,
    r.export          = coalesce(row.export, false),
    r.params_text     = coalesce(row.params_text, ''),
    r.param_names     = coalesce(row.param_names, []),
    r.params_json_str = coalesce(row.params_json_str, ''),
    r.directives      = coalesce(row.directives, []),
    r.decorator_type   = coalesce(row.decorator_type, ''),
    r.decorator_target = coalesce(row.decorator_target, ''),
    r.signature       = coalesce(row.signature, ''),
    r.doc_description = coalesce(row.doc_description, ''),
    r.doc_params_text = coalesce(row.doc_params_text, ''),
    r.doc_return_text = coalesce(row.doc_return_text, ''),
    r.area_path       = coalesce(row.area_path, ''),
    r.is_ssl_api      = coalesce(row.is_ssl_api, false),
    r.body            = coalesce(row.body, ''),
    r.body_hash       = coalesce(row.body_hash, ''),
    r.owner_qn        = row.owner_qn,
    r.module_type     = coalesce(row.module_type, ''),
    r.owner_category  = coalesce(row.owner_category, ''),
    r.file_path       = row.file_path,
    r.line            = row.line
"""


# =====================================================================================
# BSL code search (semantic search by routine body)
# =====================================================================================
# Two-shape model:
#   * small routine (whole body fits one retrieval unit): Routine itself gets the
#     :BslCodeSearchUnit label and stores code_embedding/code_embedding_epoch.
#   * large routine: dedicated (:RoutineCodeUnit:BslCodeSearchUnit) nodes per chunk,
#     linked back to parent Routine via [:HAS_CODE_UNIT]. Filterable properties are
#     denormalized from parent so Neo4j SEARCH WHERE works locally.
#
# All cypher below assumes UNWIND $rows AS row contract.

# Mark a Routine as small BSL search unit + set code embedding for the current epoch.
# is_regulated_report is denormalized so the vector SEARCH defensive WHERE works
# without traversing back through Routine properties (rel_path lives on Routine,
# but searches must filter locally on the indexed unit).
CYPHER_UPSERT_BSL_SMALL_UNIT = """
UNWIND $rows AS row
MATCH (r:Routine {id: row.routine_id})
SET r:BslCodeSearchUnit
SET r.code_embedding = row.code_embedding,
    r.code_embedding_epoch = row.epoch,
    r.code_embedding_visible = coalesce(row.visible, true),
    r.is_regulated_report = coalesce(row.is_regulated_report, false)
"""

# Mark a Routine as small BSL search unit WITHOUT embedding (RLM-only path):
# label is needed for SEARCH index membership only when embeddings are written.
# Use this cypher when Phase A finishes but Phase B (embedding) is skipped/failed.
CYPHER_CLEAR_BSL_SMALL_UNIT_EMBEDDING = """
UNWIND $routine_ids AS rid
MATCH (r:Routine {id: rid})
REMOVE r:BslCodeSearchUnit
REMOVE r.code_embedding
REMOVE r.code_embedding_epoch
REMOVE r.code_embedding_visible
"""

# Create or refresh a large-routine code unit. Properties denormalize parent so the
# Neo4j filterable vector SEARCH (`WHERE node.project_name = $pn AND ...`) works
# without traversing back to (:Routine).
CYPHER_UPSERT_BSL_LARGE_UNIT = """
UNWIND $rows AS row
MATCH (parent:Routine {id: row.routine_id})
MERGE (u:RoutineCodeUnit {id: row.unit_id})
SET u:BslCodeSearchUnit
SET u.routine_id            = row.routine_id,
    u.project_name          = row.project_name,
    u.config_name           = row.config_name,
    u.owner_qn              = row.owner_qn,
    u.owner_qn_prefix       = coalesce(row.owner_qn_prefix, ''),
    u.owner_category        = coalesce(row.owner_category, ''),
    u.module_type           = coalesce(row.module_type, ''),
    u.routine_type          = coalesce(row.routine_type, ''),
    u.export                = coalesce(row.export, false),
    u.line_start            = row.line_start,
    u.line_end              = row.line_end,
    u.part_index            = row.part_index,
    u.part_total            = row.part_total,
    u.body_hash             = coalesce(row.body_hash, ''),
    u.is_regulated_report   = coalesce(row.is_regulated_report, false),
    u.code_embedding        = row.code_embedding,
    u.code_embedding_epoch  = row.epoch,
    u.code_embedding_visible = coalesce(row.visible, true)
MERGE (parent)-[:HAS_CODE_UNIT]->(u)
"""

# Remove all BSL code units & search-unit labels for a scope (project_name).
# Used at fingerprint mismatch / version bump to clear the old generation before
# the new Phase A/B writes the new epoch. Batched via $limit for OOM safety.
CYPHER_DELETE_BSL_LARGE_UNITS_BATCH = """
MATCH (u:RoutineCodeUnit)
WHERE u.project_name = $project_name
WITH u LIMIT $limit
DETACH DELETE u
RETURN count(*) AS deleted
"""

CYPHER_CLEAR_BSL_SMALL_UNITS_BATCH = """
MATCH (r:Routine:BslCodeSearchUnit)
WHERE r.project_name = $project_name
WITH r LIMIT $limit
REMOVE r:BslCodeSearchUnit
REMOVE r.code_embedding
REMOVE r.code_embedding_epoch
REMOVE r.code_embedding_visible
RETURN count(*) AS cleared
"""

# Stale-only counterparts: keep nodes whose code_embedding_epoch matches the
# committed current_epoch. Used by resumable Phase A finalize so that any
# embeddings already written by a partial Phase B before a crash survive
# across the restart.
CYPHER_DELETE_BSL_LARGE_UNITS_STALE_BATCH = """
MATCH (u:RoutineCodeUnit)
WHERE u.project_name = $project_name
  AND (u.code_embedding_epoch IS NULL
       OR u.code_embedding_epoch <> $current_epoch)
WITH u LIMIT $limit
DETACH DELETE u
RETURN count(*) AS deleted
"""

CYPHER_CLEAR_BSL_SMALL_UNITS_STALE_BATCH = """
MATCH (r:Routine:BslCodeSearchUnit)
WHERE r.project_name = $project_name
  AND (r.code_embedding_epoch IS NULL
       OR r.code_embedding_epoch <> $current_epoch)
WITH r LIMIT $limit
REMOVE r:BslCodeSearchUnit
REMOVE r.code_embedding
REMOVE r.code_embedding_epoch
REMOVE r.code_embedding_visible
RETURN count(*) AS cleared
"""

# Pending-overlap cleanup: delete large / clear small BSL units that a startup
# overlap Phase B wrote for a pending epoch but that were NEVER made
# search-visible (code_embedding_visible = false) at exactly that epoch. The
# epoch+visible guard protects committed, visible vectors of the same epoch
# number (visible = true) from being removed. `$routine_ids` empty ([]) means
# "all pending-overlap units of this epoch"; a non-empty list scopes the
# cleanup to a reprocessed set of routines. Batched by $limit.
CYPHER_DELETE_BSL_LARGE_PENDING_OVERLAP_BATCH = """
MATCH (u:RoutineCodeUnit)
WHERE u.project_name = $project_name
  AND u.code_embedding_epoch = $epoch
  AND coalesce(u.code_embedding_visible, true) = false
  AND (size($routine_ids) = 0 OR u.routine_id IN $routine_ids)
WITH u LIMIT $limit
DETACH DELETE u
RETURN count(*) AS deleted
"""

CYPHER_CLEAR_BSL_SMALL_PENDING_OVERLAP_BATCH = """
MATCH (r:Routine:BslCodeSearchUnit)
WHERE r.project_name = $project_name
  AND r.code_embedding_epoch = $epoch
  AND coalesce(r.code_embedding_visible, true) = false
  AND (size($routine_ids) = 0 OR r.id IN $routine_ids)
WITH r LIMIT $limit
REMOVE r:BslCodeSearchUnit
REMOVE r.code_embedding
REMOVE r.code_embedding_epoch
REMOVE r.code_embedding_visible
RETURN count(*) AS cleared
"""

# Visibility sync: recompute `code_embedding_visible` for all units of
# `project_name` at `vector_epoch` according to the runtime coverage
# policy. Plain (non-SEARCH) Cypher, so `IN` / `coalesce` predicates are
# allowed here — unlike inside restricted vector `SEARCH WHERE`.
#
# Idempotent: only nodes whose current value differs from the freshly
# computed `visible` are updated, batched by $limit. Caller runs in a
# loop until `updated == 0`.
CYPHER_SYNC_BSL_CODE_EMBEDDING_VISIBLE = """
MATCH (n:BslCodeSearchUnit)
WHERE n.project_name = $project_name
  AND n.code_embedding_epoch = $vector_epoch
WITH n,
     (
       NOT (coalesce(n.owner_category, '') IN $excluded_owner_categories)
       AND (
         NOT $exclude_regulated_reports
         OR coalesce(n.is_regulated_report, false) = false
       )
     ) AS visible
WHERE n.code_embedding_visible IS NULL
   OR n.code_embedding_visible <> visible
WITH n, visible LIMIT $limit
SET n.code_embedding_visible = visible
RETURN count(*) AS updated
"""

# Retag old-epoch BSL embeddings to the new epoch without re-embedding.
# Mirrors `SET ...` of the corresponding upsert except for the vector itself
# (`code_embedding`) and `code_embedding_visible` (recomputed by
# CYPHER_SYNC_BSL_CODE_EMBEDDING_VISIBLE after Phase B). The `WHERE
# code_embedding_epoch = $prev_epoch` guard makes the retag idempotent across
# partial-transfer resume: once a node is on $new_epoch it is no longer
# matched. The `code_embedding IS NOT NULL` guard skips RLM-only Routines.
CYPHER_RETAG_BSL_SMALL_UNIT_EPOCH = """
UNWIND $rows AS row
MATCH (r:Routine {id: row.routine_id})
WHERE r.code_embedding IS NOT NULL
  AND r.code_embedding_epoch = $prev_epoch
SET r:BslCodeSearchUnit
SET r.code_embedding_epoch = $new_epoch,
    r.is_regulated_report  = coalesce(row.is_regulated_report, false)
RETURN count(r) AS retagged
"""

CYPHER_RETAG_BSL_LARGE_UNIT_EPOCH = """
UNWIND $rows AS row
MATCH (u:RoutineCodeUnit {id: row.unit_id})
WHERE u.code_embedding IS NOT NULL
  AND u.code_embedding_epoch = $prev_epoch
SET u:BslCodeSearchUnit
SET u.routine_id            = row.routine_id,
    u.project_name          = row.project_name,
    u.config_name           = row.config_name,
    u.owner_qn              = row.owner_qn,
    u.owner_qn_prefix       = coalesce(row.owner_qn_prefix, ''),
    u.owner_category        = coalesce(row.owner_category, ''),
    u.module_type           = coalesce(row.module_type, ''),
    u.routine_type          = coalesce(row.routine_type, ''),
    u.export                = coalesce(row.export, false),
    u.line_start            = row.line_start,
    u.line_end              = row.line_end,
    u.part_index            = row.part_index,
    u.part_total            = row.part_total,
    u.body_hash             = coalesce(row.body_hash, ''),
    u.is_regulated_report   = coalesce(row.is_regulated_report, false),
    u.code_embedding_epoch  = $new_epoch
RETURN count(u) AS retagged
"""

# Read body + body_hash for a batch of routines (fragment slicing path).
# Used after vector/RLM search returns final routine_ids to slice raw text per line ranges.
CYPHER_FETCH_ROUTINE_BODY_BATCH = """
UNWIND $routine_ids AS rid
MATCH (r:Routine {id: rid})
RETURN r.id AS routine_id, r.body AS body, r.body_hash AS body_hash
"""

# Lightweight pass: read routine metadata WITHOUT body for
# (a) total_routines count, (b) source_state_hash computation. Ordered by
# (rel_path, routine_id) for module-boundary-aware Phase A streaming.
CYPHER_FETCH_ROUTINES_LIGHTWEIGHT = """
MATCH (r:Routine)
WHERE r.project_name = $project_name
RETURN r.id AS routine_id,
       r.config_name AS config_name,
       r.body_hash AS body_hash,
       r.owner_category AS owner_category,
       r.module_type AS module_type,
       r.routine_type AS routine_type,
       r.export AS export,
       r.file_path AS rel_path
ORDER BY r.file_path, r.id
"""

# Keyset-paginated body batch read: returns routines with body, starting
# strictly after the given (last_rel_path, last_routine_id) cursor.
# Ordered by (rel_path, routine_id) to match the lightweight pass.
# Used by Phase A streaming + prefetch and (resume) Phase A from cleaned
# state.
CYPHER_FETCH_ROUTINES_BODY_BATCH = """
MATCH (r:Routine)
WHERE r.project_name = $project_name
  AND (r.file_path > $last_rel_path
       OR (r.file_path = $last_rel_path AND r.id > $last_routine_id))
RETURN r.id AS routine_id,
       r.config_name AS config_name,
       r.name AS name,
       r.signature AS signature,
       r.body AS body,
       r.body_hash AS body_hash,
       r.owner_qn AS owner_qn,
       r.owner_qn_prefix AS owner_qn_prefix,
       r.owner_category AS owner_category,
       r.module_type AS module_type,
       r.routine_type AS routine_type,
       r.export AS export,
       r.file_path AS file_path,
       r.line AS line
ORDER BY r.file_path, r.id
LIMIT $batch_size
"""

# Fetch FULL routine records (body + all metadata worker needs) for a fixed
# set of routine_ids. Mirrors CYPHER_FETCH_ROUTINES_BODY_BATCH shape so the
# worker can consume the same record dict.
CYPHER_FETCH_ROUTINE_RECORDS_BY_IDS = """
UNWIND $routine_ids AS rid
MATCH (r:Routine {id: rid})
WHERE r.project_name = $project_name
RETURN r.id AS routine_id,
       r.config_name AS config_name,
       r.name AS name,
       r.signature AS signature,
       r.body AS body,
       r.body_hash AS body_hash,
       r.owner_qn AS owner_qn,
       r.owner_qn_prefix AS owner_qn_prefix,
       r.owner_category AS owner_category,
       r.module_type AS module_type,
       r.routine_type AS routine_type,
       r.export AS export,
       r.file_path AS file_path,
       r.line AS line
"""

# Все Routine id одной конфигурации. Нужен config-delete подготовке: множество
# из графа шире serving-множества (routines с пустым телом и исключённые
# политикой BSL в units не попадают), и именно оно позволяет доказать
# `serving_ids ⊆ graph_ids` и снять residual-строки прогресса без остатка.
CYPHER_FETCH_ROUTINE_IDS_FOR_CONFIG = """
MATCH (r:Routine)
WHERE r.project_name = $project_name AND r.config_name = $config_name
RETURN r.id AS routine_id
"""

# Lightweight pass restricted to a fixed set of routine_ids — used for
# source_state_hash recompute and metadata-only updates without scanning
# the whole scope.
CYPHER_FETCH_ROUTINES_LIGHTWEIGHT_BY_IDS = """
UNWIND $routine_ids AS rid
MATCH (r:Routine {id: rid})
WHERE r.project_name = $project_name
RETURN r.id AS routine_id,
       r.config_name AS config_name,
       r.body_hash AS body_hash,
       r.owner_qn AS owner_qn,
       r.owner_category AS owner_category,
       r.module_type AS module_type,
       r.routine_type AS routine_type,
       r.export AS export,
       r.file_path AS rel_path,
       r.line AS line
ORDER BY r.file_path, r.id
"""

# Set code_embedding_visible=false on every node (small Routine or large
# RoutineCodeUnit) belonging to the given routine_ids. Used before scoped
# Phase 5A SQLite tx so the vector prefilter starts excluding affected
# units immediately, even before REMOVE / DETACH / Phase B writes.
#
# Two independent subqueries (`CALL { ... }`) — Cypher's only way to run
# two MATCH legs whose result counts must not depend on each other. A bare
# linear two-leg query with `WITH 1 AS _dummy` between them collapses to
# zero rows whenever the first MATCH finds 0 rows (the WITH propagates
# nothing), so a large-only routine would silently skip the second leg.
CYPHER_HIDE_BSL_UNITS_FOR_ROUTINES = """
CALL {
  WITH $routine_ids AS ids, $project_name AS pn
  UNWIND ids AS rid
  MATCH (r:Routine {id: rid})
  WHERE r.project_name = pn AND r:BslCodeSearchUnit
  SET r.code_embedding_visible = false
  RETURN count(*) AS small_updated
}
CALL {
  WITH $routine_ids AS ids, $project_name AS pn
  UNWIND ids AS rid
  MATCH (u:RoutineCodeUnit {routine_id: rid})
  WHERE u.project_name = pn
  SET u.code_embedding_visible = false
  RETURN count(*) AS large_updated
}
RETURN small_updated, large_updated
"""

# DETACH DELETE RoutineCodeUnit nodes by denormalized routine_id (NOT via
# the relationship — write contract uses HAS_CODE_UNIT but legacy code
# used non-existent OF_ROUTINE; query by `u.routine_id` is the source of
# truth).
CYPHER_DELETE_ROUTINE_CODE_UNITS_BY_IDS = """
UNWIND $routine_ids AS rid
MATCH (u:RoutineCodeUnit {routine_id: rid})
WHERE u.project_name = $project_name
DETACH DELETE u
"""

# Scoped variant of CYPHER_SYNC_BSL_CODE_EMBEDDING_VISIBLE — only nodes whose
# parent routine_id is in the committed set. Used by scoped apply step 9.5
# to restore visibility after Phase B (which writes with visible=false).
CYPHER_SYNC_BSL_CODE_EMBEDDING_VISIBLE_BY_IDS = """
MATCH (n:BslCodeSearchUnit)
WHERE n.project_name = $project_name
  AND n.code_embedding_epoch = $vector_epoch
  AND (
        ('Routine' IN labels(n) AND n.id IN $routine_ids)
        OR ('RoutineCodeUnit' IN labels(n) AND n.routine_id IN $routine_ids)
      )
WITH n,
     (
       NOT (coalesce(n.owner_category, '') IN $excluded_owner_categories)
       AND (
         NOT $exclude_regulated_reports
         OR coalesce(n.is_regulated_report, false) = false
       )
     ) AS visible
WHERE n.code_embedding_visible IS NULL
   OR n.code_embedding_visible <> visible
SET n.code_embedding_visible = visible
RETURN count(*) AS updated
"""

CYPHER_DECLARES_MODULE_TO_ROUTINE = """
UNWIND $rows AS row
MATCH (m:Module {id: row.module_id})
MATCH (r:Routine {id: row.routine_id})
MERGE (m)-[:DECLARES]->(r)
"""

CYPHER_DECLARES_COMMONMODULE_OWNER_TO_ROUTINE = """
UNWIND $rows AS row
MATCH (owner:MetadataObject {qualified_name: row.owner_qn})
MATCH (r:Routine {id: row.routine_id})
MERGE (owner)-[:DECLARES]->(r)
"""

# ----- Extension routine decorator links -----

CYPHER_CREATE_EXTENDS_ROUTINE = """
UNWIND $rows AS row
MATCH (ext_r:Routine {id: row.ext_routine_id})
MATCH (base_r:Routine {
    project_name: $project_name,
    owner_qn: row.base_owner_qn,
    name: row.decorator_target
})
MERGE (ext_r)-[rel:EXTENDS_ROUTINE]->(base_r)
SET rel.decorator = row.decorator_type
"""

CYPHER_CREATE_EXTENDS_MODULE = """
UNWIND $rows AS row
MATCH (ext_m:Module {id: row.ext_module_id})
MATCH (base_m:Module {
    project_name: $project_name,
    owner_qn:    row.base_owner_qn,
    module_type: row.module_type,
    name:        row.module_name
})
MERGE (ext_m)-[:EXTENDS_MODULE]->(base_m)
"""

# ----- BSL CALL GRAPH templates -----

CYPHER_DELETE_CALLS_BY_CALLERS = """
UNWIND $ids AS rid
MATCH (r:Routine {id: rid, project_name: $project_name})-[c:CALLS]->()
DELETE c
"""

CYPHER_MERGE_CALLS = """
UNWIND $rows AS row
MATCH (src:Routine {id: row.caller_id, project_name: row.project_name})
MATCH (dst:Routine {id: row.callee_id, project_name: row.project_name})
MERGE (src)-[c:CALLS]->(dst)
SET c.kind  = row.kind,
    c.count = row.count,
    c.lines = coalesce(row.lines, [])
"""

# ----- SSL (Standard Subsystems Library) API marking (by explicit owners list) -----
CYPHER_MARK_SSL_API_ROUTINES_BY_OWNERS: Final[str] = """
UNWIND $owners_qn AS oqn
MATCH (r:Routine {project_name: $project_name, owner_qn: oqn})
WHERE r.area_path IS NOT NULL
  AND (r.area_path = 'ПрограммныйИнтерфейс' OR r.area_path STARTS WITH 'ПрограммныйИнтерфейс.')
SET r.is_ssl_api = true
RETURN count(DISTINCT r) AS marked_count
"""

# ----- Extensions support: EXTENDS and ADOPTED_FROM -----
CYPHER_CREATE_EXTENDS: Final[str] = """
MATCH (ext:Configuration {qualified_name: $ext_qn})
MATCH (base:Configuration {qualified_name: $base_qn})
MERGE (ext)-[:EXTENDS]->(base)
"""

# NOTE: CYPHER_CREATE_ADOPTED_FROM_BATCH was removed - ADOPTED_FROM relationships
# are now created with dynamically generated queries that include node type labels
# for better performance (see ExtensionRelationshipsBuilder._execute_adopted_from_batch)

CYPHER_FIND_DUPLICATE_EXTENSIONS: Final[str] = """
MATCH (c:Configuration {is_extension: true, project_name: $project_name})
WITH c.name AS ext_name, COUNT(*) AS cnt
WHERE cnt > 1
RETURN ext_name, cnt
"""

# ----- Extension forms: hash sync detection -----

CYPHER_UPDATE_FORM_HASHES: Final[str] = """
UNWIND $rows AS row
MATCH (f:Form {qualified_name: row.form_qn})
SET f.form_content_hash = CASE
      WHEN row.form_content_hash IS NOT NULL THEN row.form_content_hash
      ELSE f.form_content_hash END,
    f.base_form_hash = CASE
      WHEN row.base_form_hash IS NOT NULL THEN row.base_form_hash
      ELSE f.base_form_hash END
"""

# ----- Extension forms: ADOPTED_FROM for FormControl -----
# Matches by ctrl_id (integer) stored after normalize_properties_values.
# Идентификатор is NOT used: normalize_properties_values("1") = "Истина".

CYPHER_ADOPTED_FROM_FORMCONTROL: Final[str] = """
MATCH (ext_form:Form)-[:ADOPTED_FROM]->(base_form:Form)
WHERE ext_form.config_name = $ext_config_name
  AND base_form.config_name = $base_config_name
  AND ext_form.project_name = $project_name
  AND (size($scope_ext_qns) = 0 OR ext_form.qualified_name IN $scope_ext_qns)
MATCH (ext_form)-[:HAS_CONTROL|HAS_CHILD*]->(ext_ctrl:FormControl)
WHERE ext_ctrl.ext_source IN ['adopted_unchanged', 'adopted_modified']
  AND ext_ctrl.base_control_id IS NOT NULL
MATCH (base_form)-[:HAS_CONTROL|HAS_CHILD*]->(base_ctrl:FormControl)
WHERE base_ctrl.ctrl_id = ext_ctrl.base_control_id
MERGE (ext_ctrl)-[:ADOPTED_FROM]->(base_ctrl)
RETURN count(*) AS created
"""

# ----- Extension forms: ADOPTED_FROM for FormAttribute -----
# FormAttribute classification is post-load (BaseForm has no attribute snapshot).
# Run CYPHER_ADOPTED_FROM_FORMATTRIBUTE first, then CYPHER_MARK_OWN_FORMATTRIBUTES.

CYPHER_ADOPTED_FROM_FORMATTRIBUTE: Final[str] = """
MATCH (ext_form:Form)-[:ADOPTED_FROM]->(base_form:Form)
WHERE ext_form.config_name = $ext_config_name
  AND base_form.config_name = $base_config_name
  AND ext_form.project_name = $project_name
  AND (size($scope_ext_qns) = 0 OR ext_form.qualified_name IN $scope_ext_qns)
MATCH (ext_form)-[:HAS_FORM_ATTRIBUTE]->(ext_attr:FormAttribute)
MATCH (base_form)-[:HAS_FORM_ATTRIBUTE]->(base_attr:FormAttribute)
WHERE ext_attr.name = base_attr.name
SET ext_attr.ext_source = CASE
      WHEN ext_attr.content_hash = base_attr.content_hash THEN 'adopted_unchanged'
      ELSE 'adopted_modified' END
MERGE (ext_attr)-[:ADOPTED_FROM]->(base_attr)
RETURN count(*) AS created
"""

CYPHER_MARK_OWN_FORMATTRIBUTES: Final[str] = """
MATCH (ext_form:Form)
WHERE ext_form.config_name = $ext_config_name
  AND ext_form.project_name = $project_name
  AND (size($scope_ext_qns) = 0 OR ext_form.qualified_name IN $scope_ext_qns)
MATCH (ext_form)-[:HAS_FORM_ATTRIBUTE]->(attr:FormAttribute)
WHERE attr.ext_source IS NULL
SET attr.ext_source = 'own'
"""

CYPHER_ADOPTED_FROM_FORM_COMMAND: Final[str] = """
MATCH (ext_form:Form)-[:ADOPTED_FROM]->(base_form:Form)
WHERE ext_form.config_name = $ext_config_name
  AND base_form.config_name = $base_config_name
  AND ext_form.project_name = $project_name
  AND (size($scope_ext_qns) = 0 OR ext_form.qualified_name IN $scope_ext_qns)
MATCH (ext_form)-[:HAS_COMMAND]->(ext_cmd:Command)
MATCH (base_form)-[:HAS_COMMAND]->(base_cmd:Command)
WHERE ext_cmd.name = base_cmd.name
  AND coalesce(ext_cmd.ext_source, '') <> 'own'
MERGE (ext_cmd)-[:ADOPTED_FROM]->(base_cmd)
RETURN count(*) AS created
"""

CYPHER_MARK_OWN_FORM_COMMANDS: Final[str] = """
MATCH (ext_form:Form)
WHERE ext_form.config_name = $ext_config_name
  AND ext_form.project_name = $project_name
  AND (size($scope_ext_qns) = 0 OR ext_form.qualified_name IN $scope_ext_qns)
MATCH (ext_form)-[:HAS_COMMAND]->(cmd:Command)
WHERE cmd.ext_source IS NULL
SET cmd.ext_source = 'own'
"""

CYPHER_SET_FORMATTRIBUTE_MODIFIED_PROPS: Final[str] = """
MATCH (ext_form:Form {project_name: $project_name})
WHERE size($scope_ext_qns) = 0 OR ext_form.qualified_name IN $scope_ext_qns
MATCH (ext_form)-[:HAS_FORM_ATTRIBUTE]->(ext_attr:FormAttribute)
      -[:ADOPTED_FROM]->(base_attr:FormAttribute)
WHERE ext_attr.ext_source = 'adopted_modified'
WITH ext_attr, base_attr,
     [k IN keys(ext_attr)
      WHERE NOT k IN ['content_hash','ext_source','config_name','project_name',
                      'qualified_name','name','Идентификатор','modified_properties']
        AND (NOT k IN keys(base_attr) OR ext_attr[k] <> base_attr[k])
     ] +
     [k IN keys(base_attr)
      WHERE NOT k IN ['content_hash','ext_source','config_name','project_name',
                      'qualified_name','name','Идентификатор','modified_properties']
        AND NOT k IN keys(ext_attr)
     ] AS changed
WHERE size(changed) > 0
SET ext_attr.modified_properties = changed
"""

# ----- Extension CommonForms: ADOPTED_FROM via MetadataObject (no Form node) -----
# ОбщиеФормы не имеют отдельных Form узлов — форма = MetadataObject с category_name='ОбщиеФормы'.

CYPHER_ADOPTED_FROM_FORMCONTROL_COMMONFORM: Final[str] = """
MATCH (ext_mo:MetadataObject)-[:ADOPTED_FROM]->(base_mo:MetadataObject)
WHERE ext_mo.config_name = $ext_config_name
  AND base_mo.config_name = $base_config_name
  AND ext_mo.project_name = $project_name
  AND ext_mo.category_name = 'ОбщиеФормы'
  AND (size($scope_ext_owner_qns) = 0 OR ext_mo.qualified_name IN $scope_ext_owner_qns)
MATCH (ext_mo)-[:HAS_CONTROL|HAS_CHILD*]->(ext_ctrl:FormControl)
WHERE ext_ctrl.ext_source IN ['adopted_unchanged', 'adopted_modified']
  AND ext_ctrl.base_control_id IS NOT NULL
MATCH (base_mo)-[:HAS_CONTROL|HAS_CHILD*]->(base_ctrl:FormControl)
WHERE base_ctrl.ctrl_id = ext_ctrl.base_control_id
MERGE (ext_ctrl)-[:ADOPTED_FROM]->(base_ctrl)
RETURN count(*) AS created
"""

CYPHER_ADOPTED_FROM_FORMATTRIBUTE_COMMONFORM: Final[str] = """
MATCH (ext_mo:MetadataObject)-[:ADOPTED_FROM]->(base_mo:MetadataObject)
WHERE ext_mo.config_name = $ext_config_name
  AND base_mo.config_name = $base_config_name
  AND ext_mo.project_name = $project_name
  AND ext_mo.category_name = 'ОбщиеФормы'
  AND (size($scope_ext_owner_qns) = 0 OR ext_mo.qualified_name IN $scope_ext_owner_qns)
MATCH (ext_mo)-[:HAS_FORM_ATTRIBUTE]->(ext_attr:FormAttribute)
MATCH (base_mo)-[:HAS_FORM_ATTRIBUTE]->(base_attr:FormAttribute)
WHERE ext_attr.name = base_attr.name
SET ext_attr.ext_source = CASE
      WHEN ext_attr.content_hash = base_attr.content_hash THEN 'adopted_unchanged'
      ELSE 'adopted_modified' END
MERGE (ext_attr)-[:ADOPTED_FROM]->(base_attr)
RETURN count(*) AS created
"""

CYPHER_MARK_OWN_FORMATTRIBUTES_COMMONFORM: Final[str] = """
MATCH (ext_mo:MetadataObject)
WHERE ext_mo.config_name = $ext_config_name
  AND ext_mo.project_name = $project_name
  AND ext_mo.category_name = 'ОбщиеФормы'
  AND (size($scope_ext_owner_qns) = 0 OR ext_mo.qualified_name IN $scope_ext_owner_qns)
MATCH (ext_mo)-[:HAS_FORM_ATTRIBUTE]->(attr:FormAttribute)
WHERE attr.ext_source IS NULL
SET attr.ext_source = 'own'
"""

CYPHER_SET_FORMATTRIBUTE_MODIFIED_PROPS_COMMONFORM: Final[str] = """
MATCH (ext_mo:MetadataObject {project_name: $project_name, category_name: 'ОбщиеФормы'})
WHERE size($scope_ext_owner_qns) = 0 OR ext_mo.qualified_name IN $scope_ext_owner_qns
MATCH (ext_mo)-[:HAS_FORM_ATTRIBUTE]->(ext_attr:FormAttribute)
      -[:ADOPTED_FROM]->(base_attr:FormAttribute)
WHERE ext_attr.ext_source = 'adopted_modified'
WITH ext_attr, base_attr,
     [k IN keys(ext_attr)
      WHERE NOT k IN ['content_hash','ext_source','config_name','project_name',
                      'qualified_name','name','Идентификатор','modified_properties']
        AND (NOT k IN keys(base_attr) OR ext_attr[k] <> base_attr[k])
     ] +
     [k IN keys(base_attr)
      WHERE NOT k IN ['content_hash','ext_source','config_name','project_name',
                      'qualified_name','name','Идентификатор','modified_properties']
        AND NOT k IN keys(ext_attr)
     ] AS changed
WHERE size(changed) > 0
SET ext_attr.modified_properties = changed
"""

CYPHER_ADOPTED_FROM_FORM_COMMAND_COMMONFORM: Final[str] = """
MATCH (ext_mo:MetadataObject)-[:ADOPTED_FROM]->(base_mo:MetadataObject)
WHERE ext_mo.config_name = $ext_config_name
  AND base_mo.config_name = $base_config_name
  AND ext_mo.project_name = $project_name
  AND ext_mo.category_name = 'ОбщиеФормы'
  AND (size($scope_ext_owner_qns) = 0 OR ext_mo.qualified_name IN $scope_ext_owner_qns)
MATCH (ext_mo)-[:HAS_COMMAND]->(ext_cmd:Command)
MATCH (base_mo)-[:HAS_COMMAND]->(base_cmd:Command)
WHERE ext_cmd.name = base_cmd.name
  AND coalesce(ext_cmd.ext_source, '') <> 'own'
MERGE (ext_cmd)-[:ADOPTED_FROM]->(base_cmd)
RETURN count(*) AS created
"""

CYPHER_MARK_OWN_FORM_COMMANDS_COMMONFORM: Final[str] = """
MATCH (ext_mo:MetadataObject)
WHERE ext_mo.config_name = $ext_config_name
  AND ext_mo.project_name = $project_name
  AND ext_mo.category_name = 'ОбщиеФормы'
  AND (size($scope_ext_owner_qns) = 0 OR ext_mo.qualified_name IN $scope_ext_owner_qns)
MATCH (ext_mo)-[:HAS_COMMAND]->(cmd:Command)
WHERE cmd.ext_source IS NULL
SET cmd.ext_source = 'own'
"""

# ----- Extension forms: ADOPTED_FROM for FormEvent -----
# Form-level events: QN replacement works (form path doesn't change on adoption).
# Control-level events: go through FormControl ADOPTED_FROM to handle renamed controls.

CYPHER_ADOPTED_FROM_FORMEVENT: Final[str] = """
MATCH (ext_form:Form)-[:ADOPTED_FROM]->(base_form:Form)
WHERE ext_form.config_name = $ext_config_name
  AND base_form.config_name = $base_config_name
  AND ext_form.project_name = $project_name
  AND (size($scope_ext_qns) = 0 OR ext_form.qualified_name IN $scope_ext_qns)
MATCH (ext_form)-[:HAS_EVENT]->(ext_evt:FormEvent)
WITH base_form, ext_evt,
     replace(ext_evt.qualified_name,
             '/' + $ext_config_name + '/',
             '/' + $base_config_name + '/') AS base_evt_qn
MATCH (base_form)-[:HAS_EVENT]->(base_evt:FormEvent {qualified_name: base_evt_qn})
MERGE (ext_evt)-[:ADOPTED_FROM]->(base_evt)
RETURN count(*) AS created
"""

CYPHER_ADOPTED_FROM_FORMEVENT_COMMONFORM: Final[str] = """
MATCH (ext_mo:MetadataObject)-[:ADOPTED_FROM]->(base_mo:MetadataObject)
WHERE ext_mo.config_name = $ext_config_name
  AND base_mo.config_name = $base_config_name
  AND ext_mo.project_name = $project_name
  AND ext_mo.category_name = 'ОбщиеФормы'
  AND (size($scope_ext_owner_qns) = 0 OR ext_mo.qualified_name IN $scope_ext_owner_qns)
MATCH (ext_mo)-[:HAS_EVENT]->(ext_evt:FormEvent)
WITH base_mo, ext_evt,
     replace(ext_evt.qualified_name,
             '/' + $ext_config_name + '/',
             '/' + $base_config_name + '/') AS base_evt_qn
MATCH (base_mo)-[:HAS_EVENT]->(base_evt:FormEvent {qualified_name: base_evt_qn})
MERGE (ext_evt)-[:ADOPTED_FROM]->(base_evt)
RETURN count(*) AS created
"""

CYPHER_ADOPTED_FROM_FORMEVENT_CONTROL: Final[str] = """
MATCH (ext_form:Form {config_name: $ext_config_name, project_name: $project_name})
WHERE size($scope_ext_qns) = 0 OR ext_form.qualified_name IN $scope_ext_qns
MATCH (ext_form)-[:HAS_CONTROL|HAS_CHILD*]->(ext_ctrl:FormControl)-[:ADOPTED_FROM]->(base_ctrl:FormControl)
MATCH (ext_ctrl)-[:HAS_EVENT]->(ext_evt:FormEvent)
MATCH (base_ctrl)-[:HAS_EVENT]->(base_evt:FormEvent)
WHERE ext_evt.name = base_evt.name
MERGE (ext_evt)-[:ADOPTED_FROM]->(base_evt)
RETURN count(*) AS created
"""

CYPHER_ADOPTED_FROM_FORMEVENT_CONTROL_COMMONFORM: Final[str] = """
MATCH (ext_mo:MetadataObject {config_name: $ext_config_name, project_name: $project_name,
                               category_name: 'ОбщиеФормы'})
WHERE size($scope_ext_owner_qns) = 0 OR ext_mo.qualified_name IN $scope_ext_owner_qns
MATCH (ext_mo)-[:HAS_CONTROL|HAS_CHILD*]->(ext_ctrl:FormControl)-[:ADOPTED_FROM]->(base_ctrl:FormControl)
MATCH (ext_ctrl)-[:HAS_EVENT]->(ext_evt:FormEvent)
MATCH (base_ctrl)-[:HAS_EVENT]->(base_evt:FormEvent)
WHERE ext_evt.name = base_evt.name
MERGE (ext_evt)-[:ADOPTED_FROM]->(base_evt)
RETURN count(*) AS created
"""

# ----- FormEventAction: upsert + HAS_EVENT_ACTION -----

CYPHER_UPSERT_FORMEVENTACTION: Final[str] = """
UNWIND $rows AS row
MERGE (a:FormEventAction {qualified_name: row.action_qn})
SET a += row.properties
WITH row, a
MATCH (e:FormEvent {qualified_name: row.event_qn})
MERGE (e)-[:HAS_EVENT_ACTION]->(a)
"""

# ----- Extension forms: EXTENDS_ACTION for FormEventAction -----
# Four variants mirroring CYPHER_ADOPTED_FROM_FORMEVENT_* pattern.

CYPHER_EXTENDS_ACTION_FORMEVENT: Final[str] = """
MATCH (ext_form:Form {config_name: $ext_config_name, project_name: $project_name})
WHERE size($scope_ext_qns) = 0 OR ext_form.qualified_name IN $scope_ext_qns
MATCH (ext_form)-[:HAS_EVENT]->(ext_evt:FormEvent)-[:ADOPTED_FROM]->(base_evt:FormEvent)
MATCH (ext_evt)-[:HAS_EVENT_ACTION]->(ext_action:FormEventAction)
WHERE ext_action.call_type IN ['Before', 'After', 'Override']
MATCH (base_evt)-[:HAS_EVENT_ACTION]->(base_action:FormEventAction {call_type: 'Main'})
MERGE (ext_action)-[:EXTENDS_ACTION]->(base_action)
RETURN count(*) AS created
"""

CYPHER_EXTENDS_ACTION_FORMEVENT_COMMONFORM: Final[str] = """
MATCH (ext_mo:MetadataObject {config_name: $ext_config_name, project_name: $project_name,
                               category_name: 'ОбщиеФормы'})
WHERE size($scope_ext_owner_qns) = 0 OR ext_mo.qualified_name IN $scope_ext_owner_qns
MATCH (ext_mo)-[:HAS_EVENT]->(ext_evt:FormEvent)-[:ADOPTED_FROM]->(base_evt:FormEvent)
MATCH (ext_evt)-[:HAS_EVENT_ACTION]->(ext_action:FormEventAction)
WHERE ext_action.call_type IN ['Before', 'After', 'Override']
MATCH (base_evt)-[:HAS_EVENT_ACTION]->(base_action:FormEventAction {call_type: 'Main'})
MERGE (ext_action)-[:EXTENDS_ACTION]->(base_action)
RETURN count(*) AS created
"""

CYPHER_EXTENDS_ACTION_FORMEVENT_CONTROL: Final[str] = """
MATCH (ext_form:Form {config_name: $ext_config_name, project_name: $project_name})
WHERE size($scope_ext_qns) = 0 OR ext_form.qualified_name IN $scope_ext_qns
MATCH (ext_form)-[:HAS_CONTROL|HAS_CHILD*]->(ext_ctrl:FormControl)
MATCH (ext_ctrl)-[:HAS_EVENT]->(ext_evt:FormEvent)-[:ADOPTED_FROM]->(base_evt:FormEvent)
MATCH (ext_evt)-[:HAS_EVENT_ACTION]->(ext_action:FormEventAction)
WHERE ext_action.call_type IN ['Before', 'After', 'Override']
MATCH (base_evt)-[:HAS_EVENT_ACTION]->(base_action:FormEventAction {call_type: 'Main'})
MERGE (ext_action)-[:EXTENDS_ACTION]->(base_action)
RETURN count(*) AS created
"""

CYPHER_EXTENDS_ACTION_FORMEVENT_CONTROL_COMMONFORM: Final[str] = """
MATCH (ext_mo:MetadataObject {config_name: $ext_config_name, project_name: $project_name,
                               category_name: 'ОбщиеФормы'})
WHERE size($scope_ext_owner_qns) = 0 OR ext_mo.qualified_name IN $scope_ext_owner_qns
MATCH (ext_mo)-[:HAS_CONTROL|HAS_CHILD*]->(ext_ctrl:FormControl)
MATCH (ext_ctrl)-[:HAS_EVENT]->(ext_evt:FormEvent)-[:ADOPTED_FROM]->(base_evt:FormEvent)
MATCH (ext_evt)-[:HAS_EVENT_ACTION]->(ext_action:FormEventAction)
WHERE ext_action.call_type IN ['Before', 'After', 'Override']
MATCH (base_evt)-[:HAS_EVENT_ACTION]->(base_action:FormEventAction {call_type: 'Main'})
MERGE (ext_action)-[:EXTENDS_ACTION]->(base_action)
RETURN count(*) AS created
"""