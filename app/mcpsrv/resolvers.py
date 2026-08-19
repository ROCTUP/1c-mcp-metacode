"""
Shared resolver helpers for typed MCP tools.

Copied (not moved) from template_ops.py / tools.py — originals untouched.
New: normalize_qn_ref, resolve_config, resolve_object_ref, resolve_owner_ref.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from config import settings
from graphdb.category_canon import canon_category
from .queries import _run_query

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .neo4j_init import GraphDatabaseLoader


# Labels carrying a UNIQUE constraint on `qualified_name` (see IndexManagementMixin.create_indexes).
# The constraint provides the index that makes an exact/prefix lookup a seek instead of a scan;
# an unlabeled `MATCH (n)` cannot use any of them and degrades to AllNodesScan.
# Module/Routine are absent on purpose: they are keyed by `id`, not `qualified_name`.
_QN_LABELS: Tuple[str, ...] = (
    "MetadataObject", "Form", "Command", "Configuration", "MetadataCategory",
    "TabularPart", "Attribute", "Resource", "Dimension", "FormControl",
    "FormEvent", "FormEventAction", "FormAttribute", "Layout", "Characteristic",
    "EnumValue", "UrlTemplate", "UrlMethod", "JournalGraph", "AccountingFlag",
    "DimensionAccountingFlag", "PredefinedItem",
)

# Each UNION part carries its own LIMIT 1: a trailing LIMIT would bind only to the last
# part, letting the other branches enumerate every match. One row per label is all the
# caller needs — it reads rows[0].
_QN_EXACT_CYPHER = "\nUNION\n".join(
    f"MATCH (n:{label} {{qualified_name: $ref}}) RETURN n.qualified_name AS qn LIMIT 1"
    for label in _QN_LABELS
)

# `left(...)` trims a descendant QN back to the requested prefix, so a ref pointing at a
# subtree without its own node still yields the graph's canonical casing.
_QN_PREFIX_CYPHER = "\nUNION\n".join(
    f"MATCH (n:{label}) WHERE n.qualified_name STARTS WITH $ref_prefix "
    f"RETURN left(n.qualified_name, size($ref)) AS qn LIMIT 1"
    for label in _QN_LABELS
)


def _canon_category_or_raw(raw: str) -> str:
    """Canonicalize a category segment (Справочник -> Справочники) via the shared
    canon_category. Returns the single canonical name when unambiguous; otherwise (unknown
    or ambiguous like 'Регистры' -> two categories) returns the input unchanged so the
    Cypher match simply fails with an honest not-found rather than guessing."""
    cats = canon_category(raw)
    return cats[0] if len(cats) == 1 else raw


def _canon_leading_category(ref: str) -> str:
    """For a multi-segment ref `Категория.Остальное` / `Категория/Остальное`, canonicalize
    the leading category segment in place. Plain single-segment refs are returned as-is."""
    parts = re.split(r"[./]", ref, maxsplit=1)
    if len(parts) != 2:
        return ref
    canon = _canon_category_or_raw(parts[0])
    if canon == parts[0]:
        return ref
    sep = ref[len(parts[0])]  # the actual '.' or '/' that followed the category
    return canon + sep + ref[len(parts[0]) + 1:]


def _form_ref_or_conditions(ref_param: str) -> str:
    """OR-conditions matching a form ref `Категория.Объект[.<seg>].ИмяФормы` against
    $<ref_param>. Accepts '.' or '/' separators, no middle segment (Cat.Obj.Form), or a
    Формы/Форма/forms/form segment — so form refs resolve without exact-segment guessing."""
    conds = []
    for sep in (".", "/"):
        for seg in ("", "формы", "форма", "forms", "form"):
            mid = sep if not seg else f"{sep}{seg}{sep}"
            conds.append(
                f"toLower(m.category_name) + '{sep}' + toLower(m.name) + '{mid}' "
                f"+ toLower(f.name) = toLower(${ref_param})"
            )
    return "\n   OR ".join(conds)


# ---------------------------------------------------------------------------
# Copied verbatim from template_ops.py
# ---------------------------------------------------------------------------

def parse_category_and_name(object_ref: str) -> Tuple[Optional[str], str]:
    """
    Parse "Категория.Имя" or "Категория/Имя" into (category_name, name).
    Returns (None, object_ref) if no separator found.
    """
    obj = (object_ref or "").strip()
    if not obj:
        return None, ""
    if "." in obj:
        parts = obj.split(".", 1)
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
    if "/" in obj:
        parts = obj.split("/", 1)
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
    return None, obj


def _resolve_object_strictly(
    loader: "GraphDatabaseLoader",
    object_ref: str,
    project_name: str,
    config_name: Optional[str] = None,
) -> Dict[str, str]:
    """
    Resolve object reference to unique MetadataObject with strict validation.

    Returns dict with 'category_name', 'name', 'qualified_name'.
    Raises ValueError on not-found or ambiguous (with candidates list).
    """
    category_name, name = parse_category_and_name(object_ref)
    if category_name:
        category_name = _canon_category_or_raw(category_name)

    if not name or not name.strip():
        raise ValueError(
            f"Invalid object reference '{object_ref}': object name cannot be empty. "
            "Expected format: 'ObjectName', 'Category.ObjectName', or 'Category/ObjectName'"
        )

    if category_name:
        cypher_check = (
            "MATCH (m:MetadataObject {name:$name, category_name:$category, project_name:$project"
            + (", config_name:$config_name" if config_name else "")
            + "}) RETURN m.category_name AS category_name, m.name AS name, "
              "m.qualified_name AS qualified_name LIMIT 2"
        )
        params = {"name": name, "category": category_name, "project": project_name}
        if config_name:
            params["config_name"] = config_name
        check_result = loader.execute_query_readonly(cypher_check, params)

        if not check_result:
            raise ValueError(
                f"Object '{category_name}.{name}' not found in project '{project_name}'"
                + (f", config '{config_name}'" if config_name else "")
                + ". Please check the category and object name."
            )
        if len(check_result) > 1 and config_name:
            raise ValueError(
                f"Ambiguous object '{category_name}.{name}' in config '{config_name}': "
                f"found {len(check_result)} entries."
            )
        row = check_result[0]
        return {"category_name": row["category_name"], "name": row["name"], "qualified_name": row["qualified_name"]}

    config_map = ", config_name:$config_name" if config_name else ""
    candidates_cypher = f"""
MATCH (m:MetadataObject {{name:$name, project_name:$project{config_map}}})
RETURN m.category_name AS category, m.qualified_name AS qn, m.name AS name
ORDER BY category
"""
    params = {"name": name, "project": project_name}
    if config_name:
        params["config_name"] = config_name
    results = loader.execute_query_readonly(candidates_cypher, params)

    if not results:
        raise ValueError(
            f"Object '{name}' not found in project '{project_name}'"
            + (f", config '{config_name}'" if config_name else "")
            + ". Please check the object name or try specifying the full qualified_name."
        )

    if len(results) == 1:
        return {"category_name": results[0]["category"], "name": name, "qualified_name": results[0]["qn"]}

    if not config_name:
        distinct_categories = {r["category"] for r in results}
        if len(distinct_categories) == 1:
            return {"category_name": results[0]["category"], "name": name, "qualified_name": results[0]["qn"]}

    candidates_list = "\n".join([
        f"  - {r['category']}.{name}  (qualified_name: {r['qn']})"
        for r in results
    ])
    raise ValueError(
        f"Ambiguous object name '{name}'. Found {len(results)} objects with this name:\n"
        f"{candidates_list}\n\n"
        f"Please specify category explicitly using one of these formats:\n"
        f"  - '{results[0]['category']}.{name}'\n"
        f"  - '{results[0]['category']}/{name}'\n"
        f"  - Full qualified_name from the list above"
    )


def _resolve_config_name(loader: "GraphDatabaseLoader", config_ref: str, project_name: str) -> str:
    """Resolve config_ref to config_name stored in Neo4j (accepts name without $ext$ suffix)."""
    results = loader.execute_query_readonly(
        "MATCH (c:Configuration {project_name: $p}) RETURN c.name AS name ORDER BY name",
        {"p": project_name},
    )
    names = [r["name"] for r in results]
    if config_ref in names:
        return config_ref
    ext = f"{config_ref}$ext$"
    if ext in names:
        return ext
    readable = [n.replace("$ext$", " (extension)") for n in names]
    raise ValueError(f"Config '{config_ref}' not found. Available: {readable}")


@dataclass(frozen=True)
class ConfigScope:
    enabled: bool
    name: Optional[str] = None

    @property
    def metadata_map(self) -> str:
        return ", config_name: $config_name" if self.enabled else ""

    def and_alias(self, alias: str) -> str:
        return f"\n  AND {alias}.config_name = $config_name" if self.enabled else ""

    def map_for(self, extra: str = "") -> str:
        extra = extra.strip().rstrip(",").strip()
        sep = ", " if extra else ""
        return f"{{{extra}{sep}project_name: $project_name{self.metadata_map}}}"


# ---------------------------------------------------------------------------
# New: normalize_qn_ref — single-field QN resolver (extracted from tools._normalize_template_refs)
# ---------------------------------------------------------------------------

def normalize_qn_ref(
    loader: "GraphDatabaseLoader",
    ref: str,
    project_name: str,
    config_name: Optional[str] = None,
) -> str:
    """
    Resolve a short ref to its full qualified_name.

    Supported formats: absolute QN, config-relative prefix, Category.Object,
    Category/Object, form paths (Category.Object.Формы.Form), plain object name.

    Always resolves through DB — no short-circuit on '/'.
    Raises ValueError on not-found or ambiguous.
    """
    ref = (ref or "").strip()
    if not ref:
        raise ValueError("Empty reference")

    project_prefix = (project_name or "").strip() + "/"

    # Already absolute QN
    if ref.startswith(project_prefix):
        if config_name:
            config_prefix = project_prefix + config_name + "/"
            if not ref.startswith(config_prefix):
                raise ValueError(f"QN {ref!r} does not belong to config {config_name!r}")
        # Return the graph's own spelling, not the caller's: downstream queries compare
        # `r.owner_qn` directly against this value, and an index seek needs an exact match.
        # Fast paths first (index seeks), case-insensitive scan only as a fallback for
        # mistyped casing — which is what the previous unlabeled query cost on every call.
        rows = loader.execute_query_readonly(_QN_EXACT_CYPHER, {"ref": ref}) or []
        if rows and rows[0].get("qn"):
            return rows[0]["qn"]

        rows = loader.execute_query_readonly(
            _QN_PREFIX_CYPHER, {"ref": ref, "ref_prefix": ref + "/"},
        ) or []
        if rows and rows[0].get("qn"):
            return rows[0]["qn"]

        fallback_cypher = """
MATCH (n)
WHERE toLower(n.qualified_name) = toLower($ref)
   OR toLower(n.qualified_name) STARTS WITH toLower($ref) + '/'
RETURN left(n.qualified_name, size($ref)) AS qn LIMIT 1
""".strip()
        rows = loader.execute_query_readonly(fallback_cypher, {"ref": ref}) or []
        if rows and rows[0].get("qn"):
            return rows[0]["qn"]
        raise ValueError(f"Unknown owner_ref: {ref!r} not found in graph")

    # Canonicalize a leading category segment (Справочник.X -> Справочники.X) so refs with
    # singular/alias categories match the canonical category_name stored in the graph.
    ref = _canon_leading_category(ref)

    if config_name:
        config_prefix = project_prefix + config_name + "/"
        raw_config = config_name.replace("$ext$", "")

        # Canonicalize config-relative prefix → full absolute QN, then fall through to DB lookup
        for pfx in (config_name + "/", raw_config + "/"):
            if ref.startswith(pfx):
                ref = project_prefix + config_name + "/" + ref[len(pfx):]
                break

        cypher = """
MATCH (n)
WHERE toLower(n.qualified_name) = toLower($ref)
  AND n.qualified_name STARTS WITH $config_prefix
RETURN n.qualified_name AS qn
UNION
MATCH (n)
WHERE toLower(n.qualified_name) = toLower($project_name + '/' + $ref)
  AND n.qualified_name STARTS WITH $config_prefix
RETURN n.qualified_name AS qn
UNION
MATCH (m:MetadataObject)
WHERE (toLower(m.category_name) + '.' + toLower(m.name) = toLower($ref)
   OR toLower(m.category_name) + '/' + toLower(m.name) = toLower($ref))
  AND m.qualified_name STARTS WITH $config_prefix
RETURN m.qualified_name AS qn
UNION
MATCH (m:MetadataObject)-[:HAS_FORM]->(f:Form)
WHERE (__FORM_OR__)
  AND f.qualified_name STARTS WITH $config_prefix
RETURN f.qualified_name AS qn
UNION
MATCH (m:MetadataObject)
WHERE toLower(m.name) = toLower($ref)
  AND m.qualified_name STARTS WITH $config_prefix
RETURN m.qualified_name AS qn
ORDER BY qn LIMIT 5
""".strip().replace("__FORM_OR__", _form_ref_or_conditions("ref"))
        params = {"ref": ref, "project_name": project_name, "config_prefix": config_prefix}
        rows = loader.execute_query_readonly(cypher, params) or []
        qns = sorted({r.get("qn") for r in rows if isinstance(r, dict) and r.get("qn")})

        if len(qns) == 1:
            return qns[0]
        if len(qns) == 0:
            sfx_cypher = """
MATCH (n)
WHERE n.qualified_name ENDS WITH $ref
  AND n.qualified_name STARTS WITH $config_prefix
RETURN n.qualified_name AS qn ORDER BY qn LIMIT 5
""".strip()
            rows2 = loader.execute_query_readonly(sfx_cypher, params) or []
            cands = sorted({r.get("qn") for r in rows2 if isinstance(r, dict) and r.get("qn")})
            if len(cands) == 1:
                return cands[0]
            hint = ", ".join(cands[:5]) if cands else "нет кандидатов"
            raise ValueError(
                f"Cannot resolve {ref!r} within config {config_name!r}. Candidates: {hint}"
            )
        raise ValueError(
            f"Ambiguous reference {ref!r} within config {config_name!r}; candidates: "
            + ", ".join(qns[:5])
        )

    # No config_name — project-wide lookup
    cypher = """
MATCH (n)
WHERE toLower(n.qualified_name) = toLower($ref)
RETURN n.qualified_name AS qn
UNION
MATCH (n)
WHERE toLower(n.qualified_name) = toLower($project_name + '/' + $ref)
RETURN n.qualified_name AS qn
UNION
MATCH (m:MetadataObject)
WHERE toLower(m.category_name) + '.' + toLower(m.name) = toLower($ref)
   OR toLower(m.category_name) + '/' + toLower(m.name) = toLower($ref)
RETURN m.qualified_name AS qn
UNION
MATCH (m:MetadataObject)-[:HAS_FORM]->(f:Form)
WHERE (__FORM_OR__)
RETURN f.qualified_name AS qn
UNION
MATCH (m:MetadataObject)
WHERE toLower(m.name) = toLower($ref)
RETURN m.qualified_name AS qn
ORDER BY qn LIMIT 5
""".strip().replace("__FORM_OR__", _form_ref_or_conditions("ref"))
    params = {"ref": ref, "project_name": project_name}
    rows = loader.execute_query_readonly(cypher, params) or []
    qns = sorted({r.get("qn") for r in rows if isinstance(r, dict) and r.get("qn")})

    if len(qns) == 1:
        return qns[0]
    if len(qns) == 0:
        sfx_cypher = """
MATCH (n)
WHERE n.qualified_name ENDS WITH $ref
RETURN n.qualified_name AS qn ORDER BY qn LIMIT 5
""".strip()
        rows2 = loader.execute_query_readonly(sfx_cypher, params) or []
        cands = sorted({r.get("qn") for r in rows2 if isinstance(r, dict) and r.get("qn")})
        if len(cands) == 1:
            return cands[0]
        hint = ", ".join(cands[:5]) if cands else "нет кандидатов"
        raise ValueError(
            f"Cannot resolve {ref!r} to a unique qualified_name. Candidates: {hint}. "
            "Use 'Категория.Имя', 'Категория/Имя', plain 'Имя', or config-relative 'Config/Категория/Имя'."
        )
    raise ValueError(f"Ambiguous reference {ref!r}; candidates: " + ", ".join(qns[:5]))


# ---------------------------------------------------------------------------
# Thin wrappers (public API for typed_tools.py)
# ---------------------------------------------------------------------------

def resolve_config(
    loader: "GraphDatabaseLoader",
    config: Optional[str],
    project_name: str,
) -> Optional[str]:
    """Resolve config ref to stored config_name. Returns None if config is falsy."""
    if not config or not config.strip():
        return None
    return _resolve_config_name(loader, config.strip(), project_name)


def resolve_object_ref(
    loader: "GraphDatabaseLoader",
    ref: str,
    project_name: str,
    config_name: Optional[str] = None,
) -> Dict[str, str]:
    """
    Resolve MetadataObject ref to {category_name, name, qualified_name}.
    Accepts: plain name, 'Category.Name', 'Category/Name', or full qualified_name.
    Raises ValueError with readable message on failure.
    """
    ref = (ref or "").strip()
    project_prefix = (project_name or "").strip() + "/"
    if ref.startswith(project_prefix):
        # Full QN passed — look up directly by qualified_name
        params: Dict = {"qn": ref, "project": project_name}
        cypher = "MATCH (m:MetadataObject {qualified_name:$qn, project_name:$project}) RETURN m.category_name AS category_name, m.name AS name, m.qualified_name AS qualified_name LIMIT 1"
        if config_name:
            params["config_name"] = config_name
            cypher = "MATCH (m:MetadataObject {qualified_name:$qn, project_name:$project, config_name:$config_name}) RETURN m.category_name AS category_name, m.name AS name, m.qualified_name AS qualified_name LIMIT 1"
        rows = loader.execute_query_readonly(cypher, params) or []
        if not rows:
            raise ValueError(f"MetadataObject with qualified_name '{ref}' not found.")
        r = rows[0]
        return {"category_name": r["category_name"], "name": r["name"], "qualified_name": r["qualified_name"]}
    return _resolve_object_strictly(loader, ref, project_name, config_name=config_name)


def resolve_element_ref(
    loader: "GraphDatabaseLoader",
    ref: str,
    project_name: str,
    config_name: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    """
    Resolve element short ref to (qualified_name, label).

    Accepted formats:
      Категория.Объект.Элемент           (Attribute/Resource/Dimension/etc. or TabularPart)
      Категория.Объект.ТЧ.РеквизитТЧ    (Attribute inside TabularPart)

    Returns None if the pattern is not recognised or if the owner MetadataObject
    is not found (allows callers to fall through to resolve_object_ref).
    Raises ValueError if owner is found but the element is not found or ambiguous.
    """
    ref = (ref or "").strip()
    if not ref or ref.startswith(project_name + "/"):
        return None

    parts = re.split(r"[./]", ref)
    if len(parts) not in (3, 4):
        return None

    owner_params: Dict[str, Any] = {
        "project_name": project_name,
        "category_name": _canon_category_or_raw(parts[0]),
        "name": parts[1],
        "config_name": config_name,
    }

    def _owner_check_cypher(exact: bool) -> str:
        if exact:
            return (
                "MATCH (m:MetadataObject {project_name: $project_name,"
                " category_name: $category_name, name: $name})\n"
                "WHERE $config_name IS NULL OR m.config_name = $config_name\n"
                "RETURN m.qualified_name AS qn LIMIT 1"
            )
        return (
            "MATCH (m:MetadataObject {project_name: $project_name})\n"
            "WHERE toLower(m.category_name) = toLower($category_name)\n"
            "  AND toLower(m.name) = toLower($name)\n"
            "  AND ($config_name IS NULL OR m.config_name = $config_name)\n"
            "RETURN m.qualified_name AS qn LIMIT 1"
        )

    _exact_owner_rows = loader.execute_query_readonly(_owner_check_cypher(exact=True), owner_params) or []
    _exact_owner_worked = bool(_exact_owner_rows)
    if _exact_owner_worked:
        owner_rows = _exact_owner_rows
    else:
        owner_rows = loader.execute_query_readonly(_owner_check_cypher(exact=False), owner_params) or []

    if not owner_rows:
        return None  # owner not found — may be a nested MetadataObject ref

    def _owner_match(exact: bool) -> str:
        if exact:
            return (
                "MATCH (m:MetadataObject {project_name: $project_name,"
                " category_name: $category_name, name: $name})\n"
                "WHERE $config_name IS NULL OR m.config_name = $config_name"
            )
        return (
            "MATCH (m:MetadataObject {project_name: $project_name})\n"
            "WHERE toLower(m.category_name) = toLower($category_name)\n"
            "  AND toLower(m.name) = toLower($name)\n"
            "  AND ($config_name IS NULL OR m.config_name = $config_name)"
        )

    def _build_element_cypher(owner_match: str, element_name_filter: str) -> str:
        return f"""{owner_match}
CALL {{
  WITH m
  MATCH (m)-[:HAS_ATTRIBUTE]->(n:Attribute)
  WHERE {element_name_filter}
  RETURN n.qualified_name AS ref, 'Attribute' AS label
  UNION
  WITH m
  MATCH (m)-[:HAS_RESOURCE]->(n:Resource)
  WHERE {element_name_filter}
  RETURN n.qualified_name AS ref, 'Resource' AS label
  UNION
  WITH m
  MATCH (m)-[:HAS_DIMENSION]->(n:Dimension)
  WHERE {element_name_filter}
  RETURN n.qualified_name AS ref, 'Dimension' AS label
  UNION
  WITH m
  MATCH (m)-[:HAS_ACCOUNTING_FLAG]->(n:AccountingFlag)
  WHERE {element_name_filter}
  RETURN n.qualified_name AS ref, 'AccountingFlag' AS label
  UNION
  WITH m
  MATCH (m)-[:HAS_DIMENSION_ACCOUNTING_FLAG]->(n:DimensionAccountingFlag)
  WHERE {element_name_filter}
  RETURN n.qualified_name AS ref, 'DimensionAccountingFlag' AS label
  UNION
  WITH m
  MATCH (m)-[:HAS_TABULAR_PART]->(n:TabularPart)
  WHERE {element_name_filter}
  RETURN n.qualified_name AS ref, 'TabularPart' AS label
}}
RETURN ref, label
ORDER BY label LIMIT 5"""

    if len(parts) == 3:
        element_name = parts[2]
        en_filter = "toLower(n.name) = toLower($element_name)"
        cypher = _build_element_cypher(_owner_match(exact=_exact_owner_worked), en_filter)
        params: Dict[str, Any] = {**owner_params, "element_name": element_name}
        rows = loader.execute_query_readonly(cypher, params) or []
        if not rows and _exact_owner_worked:
            cypher = _build_element_cypher(_owner_match(exact=False), en_filter)
            rows = loader.execute_query_readonly(cypher, params) or []
    else:
        tp_name = parts[2]
        element_name = parts[3]

        def _tp_cypher(owner_match: str) -> str:
            return (
                f"{owner_match}\n"
                "MATCH (m)-[:HAS_TABULAR_PART]->(tp:TabularPart)-[:HAS_ATTRIBUTE]->(n:Attribute)\n"
                "WHERE toLower(tp.name) = toLower($tp_name)\n"
                "  AND toLower(n.name) = toLower($element_name)\n"
                "RETURN n.qualified_name AS ref, 'Attribute' AS label\n"
                "LIMIT 5"
            )

        cypher = _tp_cypher(_owner_match(exact=_exact_owner_worked))
        params = {**owner_params, "tp_name": tp_name, "element_name": element_name}
        rows = loader.execute_query_readonly(cypher, params) or []
        if not rows and _exact_owner_worked:
            cypher = _tp_cypher(_owner_match(exact=False))
            rows = loader.execute_query_readonly(cypher, params) or []

    results: List[Tuple[str, str]] = [
        (r["ref"], r["label"]) for r in rows if r.get("ref") and r.get("label")
    ]

    if not results:
        raise ValueError(
            f"Element '{ref}' not found in project '{project_name}'"
            + (f", config '{config_name}'" if config_name else "")
            + ". Check element name."
        )
    if len(results) > 1:
        candidates = "\n".join(f"  - {label}: {qn}" for qn, label in results)
        raise ValueError(
            f"Ambiguous element reference '{ref}': found {len(results)} candidates:\n"
            f"{candidates}"
        )
    return results[0]


def resolve_owner_ref(
    loader: "GraphDatabaseLoader",
    ref: Optional[str],
    project_name: str,
    config_name: Optional[str] = None,
) -> Optional[str]:
    """
    Resolve owner ref (supports MetadataObject, Form, Command, CommonModule, form paths)
    to full qualified_name. Returns None if ref is falsy.
    Uses normalize_qn_ref — NOT _resolve_object_strictly (which is MetadataObject-only).
    """
    if not ref or not ref.strip():
        return None
    return normalize_qn_ref(loader, ref.strip(), project_name, config_name=config_name)


# ---------------------------------------------------------------------------
# strict metadata refs (get_metadata_details, find_dependency_paths)
#
# Строгий контракт form/tabular ссылок: отвергает неоднозначный shorthand
# `Категория.Объект.ИмяФормы` (без form-маркера), в отличие от permissive
# normalize_qn_ref/_form_ref_or_conditions выше. Для form-start в
# find_dependency_paths используются ИМЕННО эти strict-resolver-ы, а не
# normalize_qn_ref. Перенесено из typed_tools.py (originals заменены импортом).
# ---------------------------------------------------------------------------

# Section-маркеры (общий словарь для find_dependency_paths и get_metadata_details).
_MD_SEC_FORM_CHILD: Dict[str, frozenset] = {
    "form_attribute": frozenset({"formattribute", "реквизит", "реквизиты"}),
    "form_command": frozenset({"command", "команда", "команды"}),
    "form_event": frozenset({"event", "событие", "события"}),
    "control": frozenset({"control", "элемент", "элементы",
                          "элементуправления", "элементыуправления"}),
}
_MD_SEC_TABPART = frozenset({"tabularpart", "табличнаячасть", "табличныечасти"})
_MD_SEC_FORM_MARKER = frozenset({"форма", "формы"})
_MD_SEC_CONTROL = _MD_SEC_FORM_CHILD["control"]
_MD_SEC_EVENT = frozenset({"event", "событие", "события"})
_MD_SEC_ACTION = frozenset({"action", "действие", "действия"})

_MD_ERR_TABULAR_OWNER = (
    "owner_ref must identify a tabular part: "
    "<Категория>.<Объект>.<ИмяТабличнойЧасти> or full qualified_name."
)
_MD_ERR_FORM_OWNER = (
    "owner_ref must identify a form: <Категория>.<Объект>.Форма.<ИмяФормы>, "
    "<Категория>.<Объект>.Формы.<ИмяФормы>, ОбщиеФормы.<ИмяФормы>, or full qualified_name."
)
_MD_ERR_FORM_EVENT_OWNER = (
    "owner_ref must identify a form event: "
    "<Категория>.<Объект>.Форма.<ИмяФормы>.Событие.<ИмяСобытия>, "
    "<Категория>.<Объект>.Форма.<ИмяФормы>.<Элемент>.<ИмяЭлемента>.Событие.<ИмяСобытия>, "
    "or full qualified_name."
)


def _md_node_labels(loader, qn: str, pn: str) -> List[str]:
    """labels() of the node with this exact qualified_name (matched by QN only, since
    form-child nodes carry no project_name). [] if absent."""
    rows = _run_query(
        loader,
        "MATCH (n) WHERE n.qualified_name = $qn RETURN labels(n) AS labels LIMIT 1",
        {"qn": qn}, pn,
    )
    labels = rows[0].get("labels") if rows else None
    return labels if isinstance(labels, list) else []


def _md_qn_in_config(qn: str, pn: str, config_name: Optional[str]) -> bool:
    """True when a full QN belongs to the selected config (under project/config/).
    When no config is selected, any project QN passes."""
    if not config_name:
        return True
    return qn.startswith(f"{pn}/{config_name}/")


def _md_raise_qn_type_error(qn: str, labels: List[str], expected: str) -> None:
    """Distinguish a missing full QN from a wrong-typed one (for owner-resolver full-QN branches)."""
    if not labels:
        raise ValueError(f"qualified_name {qn!r} was not found as {expected}.")
    raise ValueError(f"qualified_name {qn!r} is {'/'.join(labels)}, not {expected}.")


def _md_is_form_path(low_segs: List[str]) -> bool:
    """A form path is a common form (`ОбщиеФормы.<name>`) or an object form carrying an explicit
    .Форма./.Формы. segment. Used to reject section-style form-child refs whose owner is not a form."""
    if low_segs[:1] == ["общиеформы"]:
        return True
    return any(t in _MD_SEC_FORM_MARKER for t in low_segs)


def resolve_tabular_part_ref(loader, ref: str, project_name: str,
                             config_name: Optional[str] = None) -> str:
    """Resolve a tabular part ref to its qualified_name.

    Accepts `<Категория>.<Объект>.<ИмяТЧ>` or a full TabularPart QN. An optional section marker
    segment (TabularPart/ТабличнаяЧасть/ТабличныеЧасти) that an agent may copy from a qualified_name
    or 1С path is tolerated: `<Категория>.<Объект>.<marker>.<ИмяТЧ>` resolves the same as the
    3-segment form. Raises ValueError on a non-tabular-part QN, an unrecognised shape, not-found or
    ambiguity."""
    ref = (ref or "").strip()
    if not ref:
        raise ValueError(_MD_ERR_TABULAR_OWNER)
    if ref.startswith(project_name + "/"):
        if not _md_qn_in_config(ref, project_name, config_name):
            raise ValueError(f"qualified_name {ref!r} does not belong to config {config_name!r}.")
        labels = _md_node_labels(loader, ref, project_name)
        if "TabularPart" not in labels:
            _md_raise_qn_type_error(ref, labels, "TabularPart")
        return ref
    parts = re.split(r"[./]", ref)
    # tolerate an explicit marker segment: <Категория>.<Объект>.<TabularPart marker>.<ИмяТЧ> -> drop it
    if len(parts) == 4 and parts[2].lower() in _MD_SEC_TABPART:
        parts = [parts[0], parts[1], parts[3]]
    if len(parts) != 3:
        raise ValueError(_MD_ERR_TABULAR_OWNER)
    cfg = "\n  AND m.config_name = $config_name" if config_name else ""
    cypher = (
        "MATCH (m:MetadataObject {project_name:$project_name})-[:HAS_TABULAR_PART]->(tp:TabularPart)\n"
        "WHERE toLower(m.category_name) = toLower($cat) AND toLower(m.name) = toLower($obj)\n"
        f"  AND toLower(tp.name) = toLower($tp){cfg}\n"
        "RETURN tp.qualified_name AS qn ORDER BY qn LIMIT 5"
    )
    params: Dict[str, Any] = {"cat": _canon_category_or_raw(parts[0]), "obj": parts[1], "tp": parts[2]}
    if config_name:
        params["config_name"] = config_name
    qns = sorted({r["qn"] for r in _run_query(loader, cypher, params, project_name) if r.get("qn")})
    if len(qns) == 1:
        return qns[0]
    if not qns:
        raise ValueError(_MD_ERR_TABULAR_OWNER)
    raise ValueError(f"Ambiguous tabular part {ref!r}; candidates: " + ", ".join(qns[:5]))


def resolve_form_owner_ref(loader, ref: str, project_name: str,
                           config_name: Optional[str] = None) -> str:
    """Resolve a form owner ref to its qualified_name.

    Accepts an object form with an explicit `.Форма.`/`.Формы.` segment, a common form
    (`ОбщиеФормы.FormName`), or a full QN of either. The bad shorthand
    `Category.Object.FormName` (no form segment) is rejected. Returns the Form QN for
    object forms and the MetadataObject QN for common forms."""
    ref = (ref or "").strip()
    if not ref:
        raise ValueError(_MD_ERR_FORM_OWNER)
    if ref.startswith(project_name + "/"):
        if not _md_qn_in_config(ref, project_name, config_name):
            raise ValueError(f"qualified_name {ref!r} does not belong to config {config_name!r}.")
        labels = _md_node_labels(loader, ref, project_name)
        if "Form" in labels:
            return ref
        if "MetadataObject" in labels and "/ОбщиеФормы/" in ref:
            return ref
        _md_raise_qn_type_error(ref, labels, "Form")
    ref = _canon_leading_category(ref)
    cfg = "\n  AND m.config_name = $config_name" if config_name else ""
    seg_conds = []
    for sep in (".", "/"):
        for seg in ("форма", "формы"):
            seg_conds.append(
                f"toLower(m.category_name) + '{sep}' + toLower(m.name) + '{sep}{seg}{sep}' "
                f"+ toLower(f.name) = toLower($ref)"
            )
    obj_form_or = "\n   OR ".join(seg_conds)
    cypher = (
        "MATCH (m:MetadataObject {project_name:$project_name})-[:HAS_FORM]->(f:Form)\n"
        f"WHERE ({obj_form_or}){cfg}\n"
        "RETURN f.qualified_name AS qn\n"
        "UNION\n"
        "MATCH (m:MetadataObject {project_name:$project_name, category_name:'ОбщиеФормы'})\n"
        "WHERE (toLower(m.category_name) + '.' + toLower(m.name) = toLower($ref)\n"
        f"   OR toLower(m.category_name) + '/' + toLower(m.name) = toLower($ref)){cfg}\n"
        "RETURN m.qualified_name AS qn\n"
        "ORDER BY qn LIMIT 5"
    )
    params: Dict[str, Any] = {"ref": ref}
    if config_name:
        params["config_name"] = config_name
    qns = sorted({r["qn"] for r in _run_query(loader, cypher, params, project_name) if r.get("qn")})
    if len(qns) == 1:
        return qns[0]
    if not qns:
        raise ValueError(_MD_ERR_FORM_OWNER)
    raise ValueError(f"Ambiguous form ref {ref!r}; candidates: " + ", ".join(qns[:5]))


def resolve_form_event_ref(loader, ref: str, project_name: str,
                           config_name: Optional[str] = None) -> str:
    """Resolve a form event owner ref to its qualified_name.

    Accepts a full FormEvent QN or a human-readable section-style path with `.`/`/` separators and
    case-insensitive section markers (matched via _MD_SEC_EVENT/_MD_SEC_CONTROL membership, mirroring
    _md_parse_section_ref):
      - form-level: `Category.Object.Форма.FormName.Событие.EventName`;
      - control-level: `Category.Object.Форма.FormName.<Элемент>.CtrlName.Событие.EventName`
        (owner resolved through resolve_control_ref)."""
    ref = (ref or "").strip()
    if not ref:
        raise ValueError(_MD_ERR_FORM_EVENT_OWNER)
    if ref.startswith(project_name + "/"):
        if not _md_qn_in_config(ref, project_name, config_name):
            raise ValueError(f"qualified_name {ref!r} does not belong to config {config_name!r}.")
        labels = _md_node_labels(loader, ref, project_name)
        if "FormEvent" not in labels:
            _md_raise_qn_type_error(ref, labels, "FormEvent")
        return ref
    segs = re.split(r"[./]", ref)
    low = [s.lower() for s in segs]
    if len(segs) < 4 or low[-2] not in _MD_SEC_EVENT:
        raise ValueError(_MD_ERR_FORM_EVENT_OWNER)
    event_name = segs[-1].strip()
    if len(segs) >= 6 and low[-4] in _MD_SEC_CONTROL and _md_is_form_path(low[:-4]):
        # control-level event: <form path>.<Control>.<ctrl>.<Event>.<event>
        form_path = ".".join(segs[:-4])
        owner_qn = resolve_control_ref(loader, form_path, segs[-3], project_name, config_name=config_name)
        if owner_qn is None:
            raise ValueError(f"control {segs[-3]!r} not found in form {form_path!r}.")
    else:
        # form-level event: <form path>.<Event>.<event>
        owner_qn = resolve_form_owner_ref(loader, ".".join(segs[:-2]), project_name, config_name=config_name)
    cypher = (
        "MATCH (owner)-[:HAS_EVENT]->(fe:FormEvent)\n"
        "WHERE owner.qualified_name = $owner_qn AND toLower(fe.name) = toLower($ev)\n"
        "RETURN fe.qualified_name AS qn ORDER BY qn LIMIT 5"
    )
    params = {"owner_qn": owner_qn, "ev": event_name}
    qns = sorted({r["qn"] for r in _run_query(loader, cypher, params, project_name) if r.get("qn")})
    if len(qns) == 1:
        return qns[0]
    if not qns:
        raise ValueError(f"Form event {event_name!r} not found on owner {owner_qn!r}.")
    raise ValueError(f"Ambiguous form event {ref!r}; candidates: " + ", ".join(qns[:5]))


def resolve_control_ref(loader, owner_ref: str, control_name: str, project_name: str,
                        config_name: Optional[str] = None) -> Optional[str]:
    """Resolve a control by name within a form. Called only when owner_ref is given.

    Resolves owner_ref to a form (object or common) and looks up a FormControl by name
    anywhere in that form's control tree. Returns the control QN, or None when the form
    resolves but no control with this name exists (caller emits an empty result). Raises
    ValueError on an unresolvable form owner or on duplicate control names (ambiguity)."""
    form_qn = resolve_form_owner_ref(loader, owner_ref, project_name, config_name=config_name)
    cypher = (
        "MATCH (owner)-[:HAS_CONTROL]->(:FormControl)-[:HAS_CHILD*0..]->(fc:FormControl)\n"
        "WHERE owner.qualified_name = $form_qn AND toLower(fc.name) = toLower($name)\n"
        "RETURN fc.qualified_name AS qn ORDER BY qn LIMIT 5"
    )
    params = {"form_qn": form_qn, "name": (control_name or "").strip()}
    qns = sorted({r["qn"] for r in _run_query(loader, cypher, params, project_name) if r.get("qn")})
    if len(qns) == 1:
        return qns[0]
    if not qns:
        return None
    raise ValueError(
        f"Ambiguous control {control_name!r} in form {form_qn!r}; candidates: " + ", ".join(qns[:5])
    )
