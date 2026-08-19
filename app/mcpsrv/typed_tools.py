"""
Typed MCP tools — 16 new tools with proper JSON Schema parameters.
Registered alongside the legacy string-based tools; old tools are NOT removed here.
"""

from __future__ import annotations

import inspect
import json
import logging
import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from config import settings
from graphdb.category_canon import canon_categories
from graphdb.routine_substring_queries import SubstringTarget
from graphdb.types import normalize_type_for_display
from .neo4j_init import initialize_neo4j, get_loader, get_search_ready_loader
from .queries import _run_query, apply_match, apply_match_norm, clamp_limit, clamp_offset
from .resolvers import (
    ConfigScope,
    _canon_category_or_raw,
    _canon_leading_category,
    _resolve_config_name,
    _resolve_object_strictly,
    normalize_qn_ref,
    parse_category_and_name,
    resolve_config,
    resolve_element_ref,
    resolve_object_ref,
    resolve_owner_ref,
    # strict metadata refs (moved out of this module; imported back for get_metadata_details)
    _md_is_form_path,
    _md_node_labels,
    _md_qn_in_config,
    _md_raise_qn_type_error,
    _MD_ERR_TABULAR_OWNER,
    _MD_ERR_FORM_OWNER,
    _MD_ERR_FORM_EVENT_OWNER,
    _MD_SEC_FORM_CHILD,
    _MD_SEC_TABPART,
    _MD_SEC_FORM_MARKER,
    _MD_SEC_CONTROL,
    _MD_SEC_EVENT,
    _MD_SEC_ACTION,
    resolve_tabular_part_ref,
    resolve_form_owner_ref,
    resolve_form_event_ref,
    resolve_control_ref,
)
from .summarization import compact_refs, compact_refs_dict, filter_for_summarization, format_results_simple
from .encoding import results_to_json, results_to_toon
from .dep_traversal import (
    resolve_start_node,
    traverse,
    dedup_paths,
    path_to_text_row,
)
from .tool_return_schema import build_tool_return_schema

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _init_loader():
    if not initialize_neo4j():
        return None
    return get_loader()


# Tools whose queries can reach a `Routine.name_norm` / `signature_norm` predicate must
# take their loader from here instead of _init_loader, so they are gated while the search
# schema migrates. Current consumers:
#   search_bsl_routines   — mode=name / mode=signature / routine_name filter (any mode)
#   get_bsl_routine_body  — routine_ref_type = name / signature
#   get_bsl_modules       — routine_name filters
#   find_form_links       — mode=events_handled_by_routine
# Everything else keeps using _init_loader: those tools read MetadataObject, forms, rights
# or Routine.id, and gating them on this migration would break unrelated features.
# get_bsl_call_graph is deliberately absent — between_owners compares the canonical
# owner_qn against idx_routine_owner_qn and reads no denormalized field.
def _init_search_ready_loader():
    return get_search_ready_loader()


def _fmt(results: list, max_n: int) -> str:
    fmt = (getattr(settings, "response_format", "text") or "text").lower()
    if fmt == "text":
        return format_results_simple(results, max_results=max_n)
    data = results[:max_n]
    if settings.response_compact_refs:
        data = compact_refs(data)
    if fmt == "toon":
        return results_to_toon(data)
    return results_to_json(data, compact=True)


def _fmt_dict(
    data: Any,
    apply_compact_refs: bool = False,
    compact_types: bool = False,
    normalize_arrays_for_toon: bool = False,
    compact_property_names: bool = False,
    compact_section_kind_names: bool = False,
) -> str:
    fmt = (getattr(settings, "response_format", "json") or "json").lower()
    if fmt == "text":
        return json.dumps(data, ensure_ascii=False, indent=2)
    if apply_compact_refs and getattr(settings, "response_compact_refs", False):
        data = compact_refs_dict(data, compact_types=compact_types, compact_property_names=compact_property_names, compact_section_kind_names=compact_section_kind_names)
    if fmt == "toon":
        if normalize_arrays_for_toon:
            from object_summary.toon import normalize_for_toon
            data = normalize_for_toon(data)
        return results_to_toon(data)
    return results_to_json(data, compact=True)


def _search_bsl_code_query_description() -> str:
    try:
        from graphdb.embedding_text_format import resolve_bsl_code_prompt_profile

        profile = resolve_bsl_code_prompt_profile(
            settings.embedding_model or "",
            settings.bsl_code_embedding_prompt_mode or "auto",
        )
    except Exception:
        profile = "none"

    if profile in {"qwen3", "f2llm_v2", "harrier"}:
        return (
            "Returns top routines whose code best matches `query`: a "
            "natural-language question\n"
            "describing what the code does. The query should be formed as a "
            "question, for example \"где формируется и отправляется "
            "уведомление пользователю\"."
        )

    return (
        "Returns top routines whose code best matches `query`: a "
        "natural-language phrase\n"
        "describing what the code does, for example \"формирование и "
        "отправка уведомления пользователю\"."
    )


def _search_bsl_code_docstring() -> str:
    from graphdb.bsl_code_search_service import compute_hard_ceiling

    hard_ceiling = compute_hard_ceiling()
    base = f"""Semantic search by BSL routine BODY.

{_search_bsl_code_query_description()} When include_fragments=true (default),
each result contains code excerpts (start_line, end_line, code, fragment_id).
When false, only ranges (start_line, end_line, fragment_id) are returned
without the code text. All line numbers are 1-based file lines.

Filters:
  config_name        — only routines in the given 1C configuration (base or extension).
  owner_qn           — exact owner qualified name (e.g. "Project/Config/Справочники/Контрагенты").
  owner_qn_prefix    — owner_qn starts with this prefix.
  owner_categories   — list of categories (e.g. ["ОбщиеМодули", "Справочники"]).
  module_type        — e.g. "CommonModule", "ObjectModule", "FormModule".
  routine_type       — "Procedure" or "Function".
  export             — true to keep only exported routines.

Default limit is 5.

Pagination:
  excluded_fragment_ids — fragment_id values from previous responses to skip.
  Use to fetch the next fragments for the same query.
  A routine may reappear with different fragment_id values (another code unit
  of the same routine — not a duplicate). count may be less than limit.
  The query has up to {hard_ceiling} candidates total; once excluded covers
  most of them, the response shrinks and eventually becomes empty.
"""

    extras: List[str] = []
    try:
        from graphdb.bsl_code_search_policy import normalize_excluded_categories
        excluded_norm = normalize_excluded_categories(
            settings.bsl_code_embedding_excluded_owner_categories or []
        )
    except Exception:
        excluded_norm = tuple(
            settings.bsl_code_embedding_excluded_owner_categories or []
        )
    if excluded_norm:
        joined = ", ".join(str(c) for c in excluded_norm if c)
        if joined:
            extras.append(
                "\nExcluded categories: " + joined + ".\n"
                "These categories are outside the default search scope.\n"
                "To search them, pass only the needed excluded categories in "
                "owner_categories.\n"
                "Do not mix excluded categories with other categories in "
                "owner_categories.\n"
            )
    if settings.bsl_code_search_exclude_regulated_reports:
        extras.append(
            "\nRegulated reports are excluded from search_bsl_code results.\n"
        )
    return base + "".join(extras)


def _done(results: list) -> str:
    max_n = min(int(settings.query_max_results), len(results))
    return _fmt(results, max_n)


def _scope(config_name: Optional[str]) -> ConfigScope:
    return ConfigScope(enabled=bool(config_name), name=config_name)


def _resolve_project(project_name: Optional[str]) -> str:
    pn = project_name or settings.project_name
    if pn not in settings.allowed_projects:
        allowed = ", ".join(settings.allowed_projects)
        raise ValueError(f"project_name '{pn}' not allowed. Allowed: {allowed}")
    return pn


def _patch_project_name_annotation(fn) -> None:
    """Patch project_name annotation and default so schema shows Literal[...] = primary_project."""
    allowed = settings.allowed_projects
    fn.__annotations__["project_name"] = Literal.__getitem__(tuple(allowed))

    params = list(inspect.signature(fn).parameters.values())
    param_names = [p.name for p in params]
    if "project_name" not in param_names:
        return
    idx = param_names.index("project_name")
    defaults = list(fn.__defaults__ or ())
    di = idx - (len(params) - len(defaults))
    if 0 <= di < len(defaults):
        defaults[di] = settings.project_name
        fn.__defaults__ = tuple(defaults)


def _patch_limit_default(fn) -> None:
    """If parameter `limit` has default=None, replace with settings.query_default_limit
    so MCP JSON Schema exposes the concrete default to the agent.

    Ownership contract: if a tool's downstream service applies its own
    *_default_limit that differs from query_default_limit, the tool MUST hard-code
    the matching default in its signature (e.g. `limit: Optional[int] = 5`).
    The patch only fires when default is None, so such tools are left untouched.
    """
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    names = [p.name for p in params]
    if "limit" not in names:
        return
    idx = names.index("limit")
    defaults = list(fn.__defaults__ or ())
    di = idx - (len(params) - len(defaults))
    if not (0 <= di < len(defaults)):
        return
    if defaults[di] is None:
        defaults[di] = int(settings.query_default_limit)
        fn.__defaults__ = tuple(defaults)


def _patch_tool_defaults(fn) -> None:
    """Apply project_name + limit default patches in one call."""
    _patch_project_name_annotation(fn)
    _patch_limit_default(fn)


def _min_score_adaptive(text: str, user_score: Optional[float]) -> float:
    if user_score is not None:
        try:
            return float(user_score)
        except Exception:
            pass
    try:
        tokens = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", str(text or ""))
        n = len(tokens)
        if n <= int(getattr(settings, "ft_min_score_short_tokens", 2)):
            return float(getattr(settings, "ft_min_score_short_value", 0.5))
        if n <= int(getattr(settings, "ft_min_score_medium_tokens", 5)):
            return float(getattr(settings, "ft_min_score_medium_value", 0.3))
    except Exception:
        pass
    return float(getattr(settings, "ft_min_score_default", 0.1))


def _node_props_cypher(cypher_match: str, scope: ConfigScope) -> tuple[str, dict]:
    cypher = f"""
{cypher_match}
WITH n, properties(n) AS _all_props
RETURN coalesce(n.id, n.qualified_name, n.name) AS node_id,
       labels(n) AS node_labels,
       _all_props AS _raw_props
LIMIT 1
""".strip()
    return cypher, {}


def _filter_node_props(rows: list) -> list:
    result = []
    for row in rows:
        raw = row.get("_raw_props") or {}
        filtered = {k: v for k, v in raw.items() if k != "body" and "embedding" not in k.lower()}
        result.append({
            "node_id": row.get("node_id"),
            "node_labels": row.get("node_labels") or [],
            "properties": filtered,
        })
    return result


# ---------------------------------------------------------------------------
# get_metadata_details: grouped paged shape ({page, nodes, properties, help?})
# ---------------------------------------------------------------------------

# Service fields always dropped from properties[] (in addition to the configured
# settings.metadata_summarize_exclude_fields). Empty-string values of NON-excluded
# keys are preserved (see _metadata_details_filter_props): "" is a valid metadata
# property value, so we filter by key name only — never via filter_for_summarization,
# which drops empty strings when metadata_summarize_drop_empty_strings is True.
_MD_DETAILS_ALWAYS_EXCLUDE = {
    "body", "name", "qualified_name", "config_name", "category_name",
    "project_name", "Справка",
}
_MD_DETAILS_EXCLUDE_PREFIXES = ("console_", "object_summary_")


def _md_join_array(value: list) -> str:
    """Join a flat primitive array into a single string (TOON `_join_primitives`
    convention). Neo4j node properties are scalars or flat primitive arrays, so this
    is the only non-scalar case to scalarize for properties[].value."""
    return "|".join("" if item is None else str(item) for item in value)


def _metadata_details_node_qn(row: dict) -> str:
    """Canonical join key for a node row: qualified_name, else "" (e.g. Routine has
    no qualified_name). Kept "" rather than `id` on purpose — the key ends with `_qn`,
    so compact_refs would intern a non-"" value into the qualified-name table; "" is
    passed through untouched. Routine identity is carried by nodes[].id instead."""
    qn = row.get("qualified_name")
    if not (isinstance(qn, str) and qn):
        qn = (row.get("props") or {}).get("qualified_name")
    return qn if (isinstance(qn, str) and qn) else ""


def _metadata_details_filter_props(raw_props: dict, *, include_help: bool) -> Tuple[Dict[str, Any], Optional[str]]:
    """Filter node props for properties[] by key name only and scalarize values.

    Returns (props, help_text). `Справка` is extracted into help_text (non-empty
    only) and never kept in props. Empty-string values are preserved. Flat arrays
    are joined into a single string; dicts never occur in properties(n) but any
    non-scalar is coerced via str() defensively.
    """
    exclude = set(getattr(settings, "metadata_summarize_exclude_fields", []) or [])
    exclude |= _MD_DETAILS_ALWAYS_EXCLUDE
    help_text: Optional[str] = None
    out: Dict[str, Any] = {}
    for k, v in (raw_props or {}).items():
        if k == "Справка":
            if isinstance(v, str) and v.strip():
                help_text = v
            continue
        if k in exclude:
            continue
        kl = k.lower()
        if "embedding" in kl or kl.startswith(_MD_DETAILS_EXCLUDE_PREFIXES):
            continue
        if isinstance(v, list):
            out[k] = _md_join_array(v)
        elif v is None or isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            out[k] = str(v)
    return out, help_text


def _metadata_details_node_from_row(row: dict, *, include_help: bool) -> Tuple[Dict[str, Any], Dict[str, Any], Optional[str]]:
    """Build a nodes[] entry from a result row plus its filtered props.

    Returns (node, filtered_props, help_text). Node-context fields (name,
    config_name, qualified_name) are read top-level first, falling back to the raw
    props — branches like `control` (config_name) and `guid` (name) carry these only
    inside properties(n), and the props filter always strips them.
    """
    props = row.get("props") or {}
    filtered, help_text = _metadata_details_filter_props(props, include_help=include_help)

    def _ctx(key: str) -> Any:
        v = row.get(key)
        if v is None or v == "":
            v = props.get(key)
        return v

    kind = row.get("kind")
    if not kind:
        labels = row.get("node_labels")
        if isinstance(labels, list) and labels:
            kind = labels[0]

    node: Dict[str, Any] = {
        "kind": kind,
        "node_qn": _metadata_details_node_qn(row),
        "qualified_name": _ctx("qualified_name"),
        "name": _ctx("name"),
        "config_name": _ctx("config_name"),
    }
    for opt in ("category", "object", "tabular"):
        if row.get(opt) is not None:
            node[opt] = row.get(opt)
    if row.get("id"):
        node["id"] = row.get("id")
    node["property_count"] = len(filtered)
    node["help_available"] = help_text is not None
    if row.get("adoption") is not None:
        node["adoption"] = row.get("adoption")
    if row.get("interception") is not None:
        node["interception"] = row.get("interception")
    return node, filtered, help_text


def _shape_get_metadata_details_properties_result(
    rows: list, *, lim: int, off: int, include_help: bool,
) -> Dict[str, Any]:
    """Shape properties-mode rows into {page, nodes, properties, help?}.

    Caller fetches lim + 1 rows for multi-node branches; helper trims to lim and
    derives has_more. node_qn ties each properties[]/help[] row to its nodes[] entry.
    """
    has_more = len(rows) > lim
    page_rows = rows[:lim] if has_more else rows
    nodes: List[Dict[str, Any]] = []
    properties: List[Dict[str, Any]] = []
    help_list: List[Dict[str, Any]] = []
    for row in page_rows:
        node, props, help_text = _metadata_details_node_from_row(row, include_help=include_help)
        nqn = node["node_qn"]
        nodes.append(node)
        for pname, pval in props.items():
            properties.append({"node_qn": nqn, "property": pname, "value": pval})
        if include_help and help_text is not None:
            help_list.append({"node_qn": nqn, "text": help_text})
    page: Dict[str, Any] = {"limit": lim, "offset": off, "returned": len(nodes), "has_more": has_more}
    if has_more:
        page["next_offset"] = off + len(nodes)
    out: Dict[str, Any] = {"page": page, "nodes": nodes, "properties": properties}
    if include_help:
        out["help"] = help_list
    return out


def _shape_get_metadata_details_resolve_result(rows: list, *, lim: int, off: int) -> Dict[str, Any]:
    """Shape resolve-mode rows into {page, nodes}. node fields: kind, qualified_name,
    name, config_name, plus category/object/tabular when the branch provides them."""
    has_more = len(rows) > lim
    page_rows = rows[:lim] if has_more else rows
    nodes: List[Dict[str, Any]] = []
    for row in page_rows:
        kind = row.get("kind")
        if not kind:
            labels = row.get("node_labels")
            if isinstance(labels, list) and labels:
                kind = labels[0]
        node: Dict[str, Any] = {"kind": kind}
        qn = row.get("qualified_name")
        if isinstance(qn, str) and qn:  # omitted for Routine (no qualified_name)
            node["qualified_name"] = qn
        node["name"] = row.get("name")
        node["config_name"] = row.get("config_name")
        for opt in ("category", "object", "tabular"):
            if row.get(opt) is not None:
                node[opt] = row.get(opt)
        if row.get("id"):
            node["id"] = row.get("id")
        if row.get("owner_qn"):
            node["owner_qn"] = row.get("owner_qn")
        nodes.append(node)
    page: Dict[str, Any] = {"limit": lim, "offset": off, "returned": len(nodes), "has_more": has_more}
    if has_more:
        page["next_offset"] = off + len(nodes)
    return {"page": page, "nodes": nodes}


def _resolve_object_ref_canon(loader, ref: str, pn: str, config_name: Optional[str]) -> Dict[str, str]:
    """Resolve an object ref to {category_name, name, qualified_name}, accepting the
    full owner-ref format set (short / Category.Name / Category/Name / full QN /
    config-relative QN). Canonicalizes via normalize_qn_ref first (it handles
    config-relative prefixes), then resolves the resulting QN; on failure falls back
    to resolve_object_ref so short names / categories keep working."""
    try:
        qn = normalize_qn_ref(loader, ref, pn, config_name=config_name)
        return resolve_object_ref(loader, qn, pn, config_name)
    except ValueError:
        return resolve_object_ref(loader, ref, pn, config_name)


@dataclass
class _MdTargetSpec:
    """Describes one pointed-target branch for get_metadata_details: which node to
    return and where its mandatory node-card fields come from. `_md_node_card_return`
    turns it into the single RETURN used by both `resolve` and `properties`, so the
    two modes cannot drift on the node-card column set (the cause of the kind:null
    defect was a branch that found the node but omitted mandatory context)."""
    kind: str                                  # literal label, e.g. "Attribute"
    node_var: str                              # alias of the target node, e.g. "a"
    name_expr: str                             # expression for name, e.g. "a.name"
    config_expr: str                           # source of config_name, e.g. "m.config_name"
    qn_expr: Optional[str] = None              # None only for Routine (no qualified_name)
    extra: Optional[Dict[str, str]] = None     # alias -> expr (category/id/owner_qn/...)


def _md_node_card_return(spec: "_MdTargetSpec", *, with_props: bool, tail_col: str = "") -> str:
    """Single node-card RETURN builder. `with_props` prepends `properties(<var>) AS
    props` (properties mode only). `tail_col` appends an already comma-prefixed extra
    block (e.g. ", adoption" / ", interception"). The column set (kind, qualified_name,
    name, config_name, +extra) is identical for both modes — resolve simply drops
    props/adoption at the shaping stage."""
    cols: List[str] = []
    if with_props:
        cols.append(f"properties({spec.node_var}) AS props")
    cols.append(f"'{spec.kind}' AS kind")
    if spec.qn_expr:
        cols.append(f"{spec.qn_expr} AS qualified_name")
    cols.append(f"{spec.name_expr} AS name")
    cols.append(f"{spec.config_expr} AS config_name")
    for alias, expr in (spec.extra or {}).items():
        cols.append(f"{expr} AS {alias}")
    return "RETURN " + ", ".join(cols) + tail_col


def _md_scope_where(node_var: str, *, config_name: Optional[str]) -> str:
    """Project-scope predicate shared by all node lookups in get_metadata_details:
    a node belongs to the project if it carries project_name OR its qualified_name is
    under the project prefix. Form-child nodes (FormControl/FormEvent/FormEventAction
    of common forms) do not carry project_name, so a project_name-only filter misses
    them — this keeps reachability of a node identical across ref_type. Adds the config
    filter when a configuration is selected."""
    w = (f"({node_var}.project_name = $project_name "
         f"OR {node_var}.qualified_name STARTS WITH $project_prefix)")
    if config_name:
        w += f"\n  AND {node_var}.config_name = $config_name"
    return w


# ref_type -> (relationship, child label) for object-owned children resolved via owner.
_MD_OWNED_CHILD: Dict[str, Tuple[str, str]] = {
    "form": ("HAS_FORM", "Form"),
    "command": ("HAS_COMMAND", "Command"),
    "attribute": ("HAS_ATTRIBUTE", "Attribute"),
    "resource": ("HAS_RESOURCE", "Resource"),
    "dimension": ("HAS_DIMENSION", "Dimension"),
    "enum_value": ("HAS_ENUM_VALUE", "EnumValue"),
    "tabular_part": ("HAS_TABULAR_PART", "TabularPart"),
}

# requested ref_type -> element label that resolve_element_ref must return for a
# combined `Category.Object.Element` ref to be accepted (type-guard). tabular_attribute
# additionally requires the QN to be nested inside a TabularPart (checked separately).
_MD_COMBINED_REF_LABEL: Dict[str, str] = {
    "attribute": "Attribute",
    "resource": "Resource",
    "dimension": "Dimension",
    "tabular_part": "TabularPart",
    "tabular_attribute": "Attribute",
}


def _md_resolve_combined_ref(loader, ref_type: str, ref: str, owner_ref: Optional[str],
                             pn: str, config_name: Optional[str]) -> Optional[Tuple[str, str]]:
    """Combined-ref convenience for get_metadata_details: when no owner_ref is given
    and ref is `Category.Object.Element` (or `Category.Object.ТЧ.Реквизит`), resolve
    it to the child node QN and signal a switch to the qualified_name branch.

    Returns ("qualified_name", <child QN>) on success, or None if the combined-ref
    shortcut does not apply. Enforces a type-guard: the element label found by
    resolve_element_ref must match the requested ref_type, so e.g. ref_type="resource"
    cannot silently return an Attribute. Raises ValueError on type mismatch."""
    if (owner_ref and owner_ref.strip()) or ref_type not in _MD_COMBINED_REF_LABEL:
        return None
    el = resolve_element_ref(loader, ref, pn, config_name)
    if not el:
        return None
    el_qn, el_label = el
    expected = _MD_COMBINED_REF_LABEL[ref_type]
    if el_label != expected:
        raise ValueError(
            f"ref_type={ref_type!r} expects a {expected}, but ref {ref!r} resolves to "
            f"a {el_label} ({el_qn}). Pass a matching ref or the correct ref_type."
        )
    if ref_type == "tabular_attribute" and "/TabularPart/" not in el_qn:
        raise ValueError(
            f"ref_type='tabular_attribute' expects an attribute inside a TabularPart, "
            f"but ref {ref!r} resolves to a top-level attribute ({el_qn})."
        )
    return ("qualified_name", el_qn)


# requested ref_type -> graph label of the target node (for full-QN / form-path guards).
_MD_REF_TYPE_LABELS: Dict[str, str] = {
    "object": "MetadataObject", "form": "Form", "command": "Command",
    "attribute": "Attribute", "resource": "Resource", "dimension": "Dimension",
    "enum_value": "EnumValue", "tabular_part": "TabularPart",
    "tabular_attribute": "Attribute", "form_attribute": "FormAttribute",
    "form_command": "Command", "form_event": "FormEvent",
    "form_event_action": "FormEventAction", "control": "FormControl",
}


def _md_owner_kind_ok(loader, ref_type: str, owner_qn: str, pn: str) -> bool:
    """Validate owner_qn as an acceptable owner for a form-child ref_type — by node label,
    not a bare substring (a child node under a common form must not pass as the form).
    A form owner is an object form (label Form) or a common form (a MetadataObject living
    under `/ОбщиеФормы/`). form_event additionally accepts a FormControl owner, since its
    branch supports control-level events `(fc:FormControl)-[:HAS_EVENT]->(fe:FormEvent)`."""
    labels = _md_node_labels(loader, owner_qn, pn)
    if "Form" in labels:
        return True
    if "MetadataObject" in labels and "/ОбщиеФормы/" in owner_qn:
        return True
    if ref_type == "form_event" and "FormControl" in labels:
        return True
    return False


def _md_qn_guard_ok(ref_type: str, qn: str) -> bool:
    """Disambiguate ref_types that share a label by QN structure: object command vs form
    command (both label Command), object attribute vs tabular attribute (both Attribute).
    A form command lives under a form: either `…/Form/<f>/Command/…` (object form) or
    `…/ОбщиеФормы/<f>/Command/…` (common form, no `/Form/` segment)."""
    _on_form = ("/Form/" in qn) or ("/ОбщиеФормы/" in qn)
    if ref_type == "command":
        return not _on_form
    if ref_type == "form_command":
        return _on_form
    if ref_type == "attribute":
        return "/TabularPart/" not in qn
    if ref_type == "tabular_attribute":
        return "/TabularPart/" in qn
    return True


# ---------------------------------------------------------------------------
# Section-style refs for get_metadata_details.
#
# An agent often assembles a self-contained ref from a qualified_name (English section tokens,
# e.g. .../Command/...) or a human-readable 1С path (Russian aliases, e.g. ...Команда...). A
# section marker separates the owner path from the child name, e.g.
#   Документы.ПриемНаРаботу.Command.Провести          (object command)
#   Документы.ПриемНаРаботу.TabularPart.ТЧ.Реквизит.X  (tabular attribute)
#   Справочники.Орг.Форма.Ф.Event.ПриОткрытии.Action.Main (form event action)
#
# Tables are per-ref_type allow-lists, NOT a flat token->ref_type map: Command/Реквизит belong to
# both object and form children, so dispatch is by the requested ref_type, then the marker is
# validated against that ref_type's set. _MD_KNOWN_SECTION_TOKENS (the union) distinguishes a
# wrong-section-for-this-ref_type (mismatch -> ValueError) from a non-section ref (-> None,
# existing combined-ref/split logic runs). Owner resolution is delegated to the typed resolvers
# (config-scoped); the parser performs no raw queries except via resolve_control_ref.
# ---------------------------------------------------------------------------

_MD_SEC_OBJECT_CHILD: Dict[str, frozenset] = {
    "command": frozenset({"command", "команда", "команды"}),
    "attribute": frozenset({"attribute", "реквизит", "реквизиты"}),
    "resource": frozenset({"resource", "ресурс", "ресурсы"}),
    "dimension": frozenset({"dimension", "измерение", "измерения"}),
    "enum_value": frozenset({"enumvalue", "значение", "значения",
                             "значениеперечисления", "значенияперечисления"}),
    "tabular_part": frozenset({"tabularpart", "табличнаячасть", "табличныечасти"}),
}
# _MD_SEC_FORM_CHILD/_MD_SEC_TABPART/_MD_SEC_FORM_MARKER/_MD_SEC_CONTROL/_MD_SEC_EVENT/
# _MD_SEC_ACTION moved to resolvers.py (imported above); shared with find_dependency_paths.
_MD_SEC_TAB_ATTR = frozenset({"attribute", "реквизит", "реквизиты"})
_MD_FORM_EVENT_ACTION_CALL_TYPES = frozenset({"main", "before", "after", "override"})

_MD_KNOWN_SECTION_TOKENS: frozenset = frozenset().union(
    *_MD_SEC_OBJECT_CHILD.values(), *_MD_SEC_FORM_CHILD.values(),
    _MD_SEC_TABPART, _MD_SEC_FORM_MARKER, _MD_SEC_EVENT, _MD_SEC_ACTION,
)


def _md_parse_section_ref(loader, ref_type: str, ref: str, owner_ref: Optional[str],
                          pn: str, config_name: Optional[str]
                          ) -> Optional[Tuple[str, str, Optional[str]]]:
    """Parse a section-style ref into one of get_metadata_details' existing routing triples.

    Returns the triple on success, or None when no recognized section marker applies (the caller's
    combined-ref / split logic then runs). Raises ValueError on a section token that does not match
    the requested ref_type (mismatch) or an object-child ref_type whose path contains a form
    segment. Only fires for self-contained refs (owner_ref empty) that are not absolute QNs."""
    if owner_ref and owner_ref.strip():
        return None
    ref = (ref or "").strip()
    if not ref or ref.startswith(pn + "/"):
        return None
    segs = re.split(r"[./]", ref)
    low = [s.lower() for s in segs]

    def _mismatch(tok: str) -> ValueError:
        return ValueError(f"ref_type={ref_type!r} does not match section {tok!r} in ref.")

    # object children: [Категория, Объект, <SECTION>, <Имя>]
    if ref_type in _MD_SEC_OBJECT_CHILD:
        if len(segs) < 4:
            return None
        marker = low[-2]
        if marker not in _MD_KNOWN_SECTION_TOKENS:
            return None
        if any(t in _MD_SEC_FORM_MARKER for t in low[:-2]):
            raise ValueError(
                f"ref_type={ref_type!r} targets an object child, but ref {ref!r} contains a form "
                f"segment; use a form_* ref_type."
            )
        if marker not in _MD_SEC_OBJECT_CHILD[ref_type]:
            raise _mismatch(segs[-2])
        if len(segs) != 4:
            return None
        return (ref_type, segs[-1], ".".join(segs[:-2]))

    # tabular attribute: [Категория, Объект, <TabularPart>, <ТЧ>, <Attribute>, <Имя>]
    if ref_type == "tabular_attribute":
        if len(segs) < 6:
            return None
        attr_marker, tp_marker = low[-2], low[-4]
        if attr_marker not in _MD_KNOWN_SECTION_TOKENS and tp_marker not in _MD_KNOWN_SECTION_TOKENS:
            return None
        if tp_marker not in _MD_SEC_TABPART or attr_marker not in _MD_SEC_TAB_ATTR:
            raise _mismatch(segs[-2] if attr_marker not in _MD_SEC_TAB_ATTR else segs[-4])
        if len(segs) != 6:
            return None
        return ("tabular_attribute", segs[-1], ".".join([segs[0], segs[1], segs[3]]))

    # form children: <form path>.<SECTION>.<Имя>; form_event may be control-level
    if ref_type in ("form_attribute", "form_command", "form_event"):
        if len(segs) < 4:
            return None
        marker = low[-2]
        if marker not in _MD_KNOWN_SECTION_TOKENS:
            return None
        if (ref_type == "form_event" and marker in _MD_SEC_EVENT
                and len(segs) >= 6 and low[-4] in _MD_SEC_CONTROL):
            # control-level event: <form path>.<Control>.<ctrl>.<Event>.<event>
            if not _md_is_form_path(low[:-4]):
                return None
            form_path = ".".join(segs[:-4])
            ctrl_qn = resolve_control_ref(loader, form_path, segs[-3], pn, config_name)
            if ctrl_qn is None:
                raise ValueError(f"control {segs[-3]!r} not found in form {form_path!r}.")
            return ("form_event", segs[-1], ctrl_qn)
        if marker not in _MD_SEC_FORM_CHILD[ref_type]:
            raise _mismatch(segs[-2])
        if not _md_is_form_path(low[:-2]):
            return None
        return (ref_type, segs[-1], ".".join(segs[:-2]))

    # bare control: <form path>.<Control>.<Имя> -> existing control branch (None -> empty result)
    if ref_type == "control":
        if len(segs) < 4:
            return None
        marker = low[-2]
        if marker not in _MD_KNOWN_SECTION_TOKENS:
            return None
        if marker not in _MD_SEC_CONTROL:
            raise _mismatch(segs[-2])
        if not _md_is_form_path(low[:-2]):
            return None
        return ("control", segs[-1], ".".join(segs[:-2]))

    # form event action: <form path>.<Event>.<event>.<Action>.<call_type>
    if ref_type == "form_event_action":
        if len(segs) < 6:
            return None
        action_marker = low[-2]
        if action_marker not in _MD_KNOWN_SECTION_TOKENS:
            return None
        if action_marker not in _MD_SEC_ACTION:
            raise _mismatch(segs[-2])
        return ("form_event_action", segs[-1], ".".join(segs[:-2]))

    return None


def _md_resolve_target_ref(loader, ref_type: str, ref: str, owner_ref: Optional[str],
                           pn: str, config_name: Optional[str]) -> Tuple[str, str, Optional[str]]:
    """Normalize (ref_type, ref, owner_ref) for get_metadata_details before dispatch.

    May rewrite to ("qualified_name", <qn>, None) for a full target QN, a form-path ref,
    or an element combined ref; or split a combined Category.Object.Child into
    (ref_type, child, owner). Returns the triple unchanged when no shortcut applies (the
    pointed branch then raises a clear owner_ref-required error). Raises ValueError on a
    full-QN type mismatch."""
    project_prefix = pn + "/"
    expected = _MD_REF_TYPE_LABELS.get(ref_type)

    # ref_type="form" without owner_ref: resolve strictly via resolve_form_owner_ref BEFORE the
    # generic full-QN guard (1) and before normalize_qn_ref (3). This rejects the bad shorthand
    # Category.Object.FormName and accepts common forms (MetadataObject under /ОбщиеФормы/), which
    # the (1) guard (expected='Form') would otherwise reject. owner_ref+name keeps the explicit
    # _MD_OWNED_CHILD fallback (handled at the owner_ref-given passthrough below).
    if ref_type == "form" and not (owner_ref and owner_ref.strip()):
        try:
            form_qn = resolve_form_owner_ref(loader, ref, pn, config_name=config_name)
        except ValueError:
            raise ValueError(
                "Form ref must include .Форма. or .Формы.: <Категория>.<Объект>.Форма.<ИмяФормы>, "
                "<Категория>.<Объект>.Формы.<ИмяФормы>, ОбщиеФормы.<ИмяФормы>, or full qualified_name."
            )
        return ("qualified_name", form_qn, None)

    # (1) Full QN of the target child node — owner_ref not needed. `object` is excluded:
    # its own branch already accepts a full QN via _resolve_object_ref_canon and returns the
    # object-card with `category`, which the generic qualified_name branch would drop.
    if (expected and ref.startswith(project_prefix)
            and ref_type not in ("qualified_name", "qualified_name_prefix", "guid",
                                 "routine_id", "object")):
        labels = _md_node_labels(loader, ref, pn)
        if labels:
            if expected in labels and _md_qn_guard_ok(ref_type, ref):
                return ("qualified_name", ref, None)
            raise ValueError(
                f"ref {ref!r} is a {'/'.join(labels)}, not compatible with "
                f"ref_type={ref_type!r} (expected {expected})."
            )
        # typed full QN that does not exist as a node — raise instead of falling into the
        # split fallback (which would emit a misleading "Object '...' not found").
        raise ValueError(f"qualified_name {ref!r} was not found as {expected}.")

    # section-style refs (Category.Object.Command.Name, ...Форма.F.Event.E.Action.Main, ...):
    # parse into an existing routing triple before the owner_ref passthrough and the combined/split
    # steps so the section marker is not swallowed by the fallback split. Returns None when no
    # recognized section marker applies (or owner_ref is given / ref is an absolute QN).
    section = _md_parse_section_ref(loader, ref_type, ref, owner_ref, pn, config_name)
    if section is not None:
        return section

    # owner_ref given: keep the explicit owner+child path, do not guess from ref.
    if owner_ref and owner_ref.strip():
        return (ref_type, ref, owner_ref)

    # (2) element combined ref (attribute/resource/dimension/tabular_part/tabular_attribute)
    combined = _md_resolve_combined_ref(loader, ref_type, ref, owner_ref, pn, config_name)
    if combined:
        return (combined[0], combined[1], None)

    # (3) object-children without owner_ref: form via form-path normalize, others via split
    if ref_type in _MD_OWNED_CHILD:
        # step 1: normalize (resolves form-paths + objects); accept only if label matches.
        try:
            qn = normalize_qn_ref(loader, ref, pn, config_name=config_name)
        except ValueError:
            qn = None
        if qn and expected:
            labels = _md_node_labels(loader, qn, pn)
            if expected in labels and _md_qn_guard_ok(ref_type, qn):
                return ("qualified_name", qn, None)
        # mismatch guard: for non-element children (form/command/enum_value), if the combined
        # ref actually resolves to a concrete element of another type, surface a clear
        # mismatch instead of silently splitting and searching by that name (element types
        # are already handled/raised in step (2) above).
        if ref_type not in _MD_COMBINED_REF_LABEL:
            try:
                _el = resolve_element_ref(loader, ref, pn, config_name)
            except ValueError:
                _el = None
            if _el:
                raise ValueError(
                    f"ref_type={ref_type!r} expects a {expected}, but ref {ref!r} resolves "
                    f"to a {_el[1]} ({_el[0]}). Pass a matching ref or the correct ref_type."
                )
        # step 2: split Category.Object.Child -> (owner, child)
        parts = re.split(r"[./]", ref)
        if len(parts) >= 3:
            return (ref_type, parts[-1], ".".join(parts[:-1]))

    return (ref_type, ref, owner_ref)


# ---------------------------------------------------------------------------
# Tool-specific typed owner/target resolvers for get_metadata_details.
#
# These encode the strict ref/owner_ref contract that is specific to this tool
# (tabular parts, object/common forms with explicit .Форма./.Формы. segments,
# form events, controls) and are NOT part of the public resolvers.py API. They
# reuse the module-level _run_query / _md_node_labels and resolvers helpers.
#
# Config-scope invariant (mirrors normalize_qn_ref's absolute-QN guard): when a
# config is selected, a full QN owner must live under project/config/, and a
# name-based resolve is scoped to that config. Downstream queries in
# _md_pointed_target_query trust $owner_qn and do not re-scope the owner by
# config, so the check must happen here.
# ---------------------------------------------------------------------------

# _md_qn_in_config / _md_raise_qn_type_error / resolve_tabular_part_ref / resolve_form_owner_ref /
# resolve_form_event_ref / resolve_control_ref moved to resolvers.py (imported above); shared with
# find_dependency_paths so both tools use one strict form/tabular ref contract.


_ADOPTED_COLLECT = """WITH DISTINCT m
MATCH (cfg:Configuration {project_name: $project_name, name: m.config_name})
OPTIONAL MATCH (ext_m:MetadataObject {project_name: $project_name})-[:ADOPTED_FROM]->(m)
WITH m, cfg, collect(DISTINCT ext_m.config_name) AS _ext_names
OPTIONAL MATCH (m)-[:ADOPTED_FROM]->(base_m:MetadataObject {project_name: $project_name})
WITH m, cfg, _ext_names, base_m.config_name AS _base_cn
WITH m,
     CASE
       WHEN NOT coalesce(cfg.is_extension, false) AND size(_ext_names) > 0
         THEN {role: 'base', extension_config_names: _ext_names}
       WHEN coalesce(cfg.is_extension, false) AND _base_cn IS NOT NULL
         THEN {role: 'extension', base_config_name: _base_cn}
       ELSE {role: 'none'}
     END AS adoption"""


def _owner_adoption_block(carry_vars: str = "", parent_var: str = "m") -> str:
    """Cypher block for MetadataObject-level adoption that carries extra element vars through WITH chain."""
    c = f", {carry_vars}" if carry_vars else ""
    return (
        f"MATCH (cfg:Configuration {{project_name: $project_name, name: {parent_var}.config_name}})\n"
        f"OPTIONAL MATCH (ext_m:MetadataObject {{project_name: $project_name}})-[:ADOPTED_FROM]->({parent_var})\n"
        f"WITH {parent_var}{c}, cfg, collect(DISTINCT ext_m.config_name) AS _ext_names\n"
        f"OPTIONAL MATCH ({parent_var})-[:ADOPTED_FROM]->(base_m:MetadataObject {{project_name: $project_name}})\n"
        f"WITH {parent_var}{c}, cfg, _ext_names, base_m.config_name AS _base_cn\n"
        f"WITH {parent_var}{c},\n"
        f"     CASE\n"
        f"       WHEN NOT coalesce(cfg.is_extension, false) AND size(_ext_names) > 0\n"
        f"         THEN {{role: 'base', extension_config_names: _ext_names}}\n"
        f"       WHEN coalesce(cfg.is_extension, false) AND _base_cn IS NOT NULL\n"
        f"         THEN {{role: 'extension', base_config_name: _base_cn}}\n"
        f"       ELSE {{role: 'none'}}\n"
        f"     END AS adoption"
    )


def _full_elem_adoption_block(
    elem_var: str, elem_label: str, carry_vars: str,
    parent_var: str = "m", parent_label: str = "MetadataObject",
) -> str:
    """Cypher block inserted before RETURN in element section queries.

    Computes adoption per row:
    - parent adoption role (via parent_var/parent_label, defaults to MetadataObject m)
    - element adoption role (only if parent role != 'none')
    Returns adoption=null when parent role is 'none'; caller strips nulls via
    Python post-processing.

    parent_label must match the actual node label of parent_var — ADOPTED_FROM
    edges are always between nodes of the same label (see ExtensionRelationships
    Builder._build_adopted_from_for_type), so a wrong label silently produces
    adoption=null for every row.
    """
    return f"""
MATCH (cfg:Configuration {{project_name: $project_name, name: {parent_var}.config_name}})
OPTIONAL MATCH (ext_par:{parent_label} {{project_name: $project_name}})-[:ADOPTED_FROM]->({parent_var})
WITH {carry_vars}, cfg, collect(DISTINCT ext_par.config_name) AS _parent_ext_names
OPTIONAL MATCH ({parent_var})-[:ADOPTED_FROM]->(base_par:{parent_label} {{project_name: $project_name}})
WITH {carry_vars}, cfg, _parent_ext_names, base_par.config_name AS _parent_base_cn
WITH {carry_vars},
     CASE
       WHEN NOT coalesce(cfg.is_extension, false) AND size(_parent_ext_names) > 0
         THEN 'base'
       WHEN coalesce(cfg.is_extension, false) AND _parent_base_cn IS NOT NULL
         THEN 'extension'
       ELSE 'none'
     END AS _parent_role
OPTIONAL MATCH (ext_el:{elem_label} {{project_name: $project_name}})-[:ADOPTED_FROM]->({elem_var})
WITH {carry_vars}, _parent_role, collect(DISTINCT ext_el.config_name) AS _ext_el_names
OPTIONAL MATCH ({elem_var})-[:ADOPTED_FROM]->(base_el:{elem_label} {{project_name: $project_name}})
WITH {carry_vars}, _parent_role, _ext_el_names, base_el.config_name AS _base_el_cn
WITH {carry_vars},
     CASE
       WHEN _parent_role = 'none' THEN null
       WHEN size(_ext_el_names) > 0 THEN {{role: 'base', extension_config_names: _ext_el_names}}
       WHEN _base_el_cn IS NOT NULL THEN {{role: 'extension', base_config_name: _base_el_cn}}
       ELSE {{role: 'none'}}
     END AS adoption"""


def _form_child_adoption_block(elem_var: str, elem_label: str, carry_vars: str) -> str:
    """Adoption block for elements whose direct parent is Form `f` (not MetadataObject).
    Gate: if Form f has no adoption in any direction, returns adoption=null (stripped by caller).
    Uses qualified_name STARTS WITH $project_prefix because form child nodes (FormControl,
    FormAttribute, FormEvent, Command) do not carry project_name property.
    """
    return f"""
OPTIONAL MATCH (ext_par:Form {{project_name: $project_name}})-[:ADOPTED_FROM]->(f)
WITH {carry_vars}, collect(DISTINCT ext_par.config_name) AS _parent_ext_names
OPTIONAL MATCH (f)-[:ADOPTED_FROM]->(base_par:Form {{project_name: $project_name}})
WITH {carry_vars}, _parent_ext_names, base_par.config_name AS _parent_base_cn
WITH {carry_vars},
     CASE
       WHEN size(_parent_ext_names) > 0 THEN 'base'
       WHEN _parent_base_cn IS NOT NULL THEN 'extension'
       ELSE 'none'
     END AS _parent_role
OPTIONAL MATCH (ext_el:{elem_label})-[:ADOPTED_FROM]->({elem_var})
WHERE ext_el.qualified_name STARTS WITH $project_prefix
WITH {carry_vars}, _parent_role, collect(DISTINCT ext_el.config_name) AS _ext_el_names
OPTIONAL MATCH ({elem_var})-[:ADOPTED_FROM]->(base_el:{elem_label})
WHERE base_el.qualified_name STARTS WITH $project_prefix
WITH {carry_vars}, _parent_role, _ext_el_names, base_el.config_name AS _base_el_cn
WITH {carry_vars},
     CASE
       WHEN _parent_role = 'none' THEN null
       WHEN size(_ext_el_names) > 0 THEN {{role: 'base', extension_config_names: _ext_el_names}}
       WHEN _base_el_cn IS NOT NULL THEN {{role: 'extension', base_config_name: _base_el_cn}}
       ELSE {{role: 'none'}}
     END AS adoption"""


def _cf_child_adoption_block(elem_var: str, elem_label: str, carry_vars: str) -> str:
    """Adoption block for CommonForms child elements (parent is MetadataObject m, not Form f).
    Gate: if MetadataObject m has no adoption, returns adoption=null (stripped by caller).
    Elements lack project_name — uses qualified_name STARTS WITH $project_prefix.
    """
    return f"""
OPTIONAL MATCH (ext_par:MetadataObject {{project_name: $project_name}})-[:ADOPTED_FROM]->(m)
WITH {carry_vars}, collect(DISTINCT ext_par.config_name) AS _parent_ext_names
OPTIONAL MATCH (m)-[:ADOPTED_FROM]->(base_par:MetadataObject {{project_name: $project_name}})
WITH {carry_vars}, _parent_ext_names, base_par.config_name AS _parent_base_cn
WITH {carry_vars},
     CASE
       WHEN size(_parent_ext_names) > 0 THEN 'base'
       WHEN _parent_base_cn IS NOT NULL THEN 'extension'
       ELSE 'none'
     END AS _parent_role
OPTIONAL MATCH (ext_el:{elem_label})-[:ADOPTED_FROM]->({elem_var})
WHERE ext_el.qualified_name STARTS WITH $project_prefix
WITH {carry_vars}, _parent_role, collect(DISTINCT ext_el.config_name) AS _ext_el_names
OPTIONAL MATCH ({elem_var})-[:ADOPTED_FROM]->(base_el:{elem_label})
WHERE base_el.qualified_name STARTS WITH $project_prefix
WITH {carry_vars}, _parent_role, _ext_el_names, base_el.config_name AS _base_el_cn
WITH {carry_vars},
     CASE
       WHEN _parent_role = 'none' THEN null
       WHEN size(_ext_el_names) > 0 THEN {{role: 'base', extension_config_names: _ext_el_names}}
       WHEN _base_el_cn IS NOT NULL THEN {{role: 'extension', base_config_name: _base_el_cn}}
       ELSE {{role: 'none'}}
     END AS adoption"""


def _strip_null_adoption(rows: list) -> list:
    """Remove 'adoption' key from rows where its value is None."""
    return [
        {k: v for k, v in row.items() if not (k == "adoption" and v is None)}
        for row in rows
    ]


def _strip_null_interception(rows: list) -> list:
    return [
        {k: v for k, v in row.items() if not (k == "interception" and v is None)}
        for row in rows
    ]


_INTERCEPTION_UNWIND_CYPHER = """
UNWIND $ids AS _id
MATCH (r:Routine {id:_id})
OPTIONAL MATCH (r)-[:EXTENDS_ROUTINE]->(base_r:Routine)
WITH r, base_r.config_name AS _base_cfg
OPTIONAL MATCH (ext_r:Routine {project_name: $project_name})-[ext_rel:EXTENDS_ROUTINE]->(r)
WITH r, _base_cfg, collect(DISTINCT {extension_config_name: ext_r.config_name, decorator: ext_rel.decorator, extension_routine_name: ext_r.name}) AS _ext_list
WITH r,
  CASE
    WHEN _base_cfg IS NOT NULL
      THEN {role: 'extension', base_config_name: _base_cfg, decorator: r.decorator_type, base_routine_name: r.decorator_target}
    WHEN size([x IN _ext_list WHERE x.extension_config_name IS NOT NULL]) > 0
      THEN {role: 'base', extensions: [x IN _ext_list WHERE x.extension_config_name IS NOT NULL]}
    ELSE null
  END AS interception
RETURN r.id AS id, interception
""".strip()


_MODULE_TYPE_UNWIND_CYPHER = """
UNWIND $ids AS _id
MATCH (r:Routine {id:_id})
OPTIONAL MATCH (mod:Module)-[:DECLARES]->(r)
RETURN _id AS id, coalesce(mod.module_type,'CommonModule') AS module_type
""".strip()


def _enrich_module_type(rows: list, id_field: str, loader: Any, project_name: str) -> list:
    _ids = [row[id_field] for row in rows]
    if not _ids:
        return rows
    _mt_rows = _run_query(loader, _MODULE_TYPE_UNWIND_CYPHER, {"ids": _ids}, project_name)
    _mt_map = {row["id"]: row["module_type"] for row in _mt_rows}
    return [{**row, "module_type": _mt_map.get(row[id_field], "")} for row in rows]


def _enrich_interception(rows: list, id_field: str, loader: Any, project_name: str) -> list:
    _ids = [row[id_field] for row in rows]
    if not _ids:
        return rows
    _icp_rows = _run_query(loader, _INTERCEPTION_UNWIND_CYPHER, {"ids": _ids}, project_name)
    _icp_map = {row["id"]: row["interception"] for row in _icp_rows}
    return [
        {**row, "interception": _icp_map[row[id_field]]}
        if row.get(id_field) in _icp_map and _icp_map[row[id_field]] is not None
        else row
        for row in rows
    ]


def _enrich_call_context(
    rows: list,
    call_context_mode: str,
    call_context_limit: Optional[int],
    loader: Any,
    project_name: str,
    config_name: Optional[str],
) -> list:
    if call_context_mode == "none" or not rows:
        return rows
    lim = max(1, int(call_context_limit or 5))
    ids = [row["id"] for row in rows if row.get("id")]
    if not ids:
        return rows
    cfg_filter = (
        "AND src.config_name = $config_name AND dst.config_name = $config_name"
        if config_name
        else ""
    )
    params: Dict[str, Any] = {"ids": ids, "project_name": project_name}
    if config_name:
        params["config_name"] = config_name

    callees_map: Dict[str, list] = {}
    callers_map: Dict[str, list] = {}

    if call_context_mode in ("callees", "both"):
        cypher = f"""
UNWIND $ids AS rid
MATCH (src:Routine {{id: rid}})-[:CALLS]->(dst:Routine)
WHERE coalesce(src.owner_qn,'') STARTS WITH ($project_name + '/')
  AND coalesce(dst.owner_qn,'') STARTS WITH ($project_name + '/')
  {cfg_filter}
RETURN rid AS source_id, dst.id AS id,
       coalesce(dst.name,'') AS name,
       coalesce(dst.owner_qn,'') AS owner_qn
ORDER BY source_id, name
""".strip()
        for r in _run_query(loader, cypher, params, project_name):
            sid = r["source_id"]
            bucket = callees_map.setdefault(sid, [])
            if len(bucket) < lim:
                bucket.append({"id": r["id"], "name": r["name"], "owner_qn": r["owner_qn"]})

    if call_context_mode in ("callers", "both"):
        cypher = f"""
UNWIND $ids AS rid
MATCH (src:Routine)-[:CALLS]->(dst:Routine {{id: rid}})
WHERE coalesce(src.owner_qn,'') STARTS WITH ($project_name + '/')
  AND coalesce(dst.owner_qn,'') STARTS WITH ($project_name + '/')
  {cfg_filter}
RETURN rid AS target_id, src.id AS id,
       coalesce(src.name,'') AS name,
       coalesce(src.owner_qn,'') AS owner_qn
ORDER BY target_id, name
""".strip()
        for r in _run_query(loader, cypher, params, project_name):
            tid = r["target_id"]
            bucket = callers_map.setdefault(tid, [])
            if len(bucket) < lim:
                bucket.append({"id": r["id"], "name": r["name"], "owner_qn": r["owner_qn"]})

    result = []
    for row in rows:
        rid = row.get("id")
        extra: Dict[str, Any] = {}
        if call_context_mode in ("callees", "both"):
            extra["callees"] = callees_map.get(rid, [])
        if call_context_mode in ("callers", "both"):
            extra["callers"] = callers_map.get(rid, [])
        result.append({**row, **extra})
    return result


def _routine_interception_block() -> str:
    """Cypher block inserted before RETURN in search_bsl_routines queries.
    Variable r must be the only variable in scope before this block.
    Ends with r + interception in scope for the subsequent RETURN.
    """
    return """
OPTIONAL MATCH (r)-[:EXTENDS_ROUTINE]->(base_r:Routine)
WITH r, base_r.config_name AS _base_cfg
OPTIONAL MATCH (ext_r:Routine {project_name: $project_name})-[ext_rel:EXTENDS_ROUTINE]->(r)
WITH r, _base_cfg, collect(DISTINCT {extension_config_name: ext_r.config_name, decorator: ext_rel.decorator, extension_routine_name: ext_r.name}) AS _ext_list
WITH r,
  CASE
    WHEN _base_cfg IS NOT NULL
      THEN {role: 'extension', base_config_name: _base_cfg, decorator: r.decorator_type, base_routine_name: r.decorator_target}
    WHEN size([x IN _ext_list WHERE x.extension_config_name IS NOT NULL]) > 0
      THEN {role: 'base', extensions: [x IN _ext_list WHERE x.extension_config_name IS NOT NULL]}
    ELSE null
  END AS interception"""


class PageSourcePolicy(Enum):
    """How the page subquery obtains and orders its rows.

    DEFAULT — the caller supplies the whole row source (a MATCH or a fulltext CALL) and the
    page is ordered by name/id, as before.

    PROJECT_ORDERED_SCAN — the helper owns the row source: it emits the index hint, both
    index predicates and the matching ORDER BY as one unit. They only work together. The
    hint with the old `ORDER BY r.name, r.id` makes the query *slower* than no hint at all
    (1636 ms vs 536 ms, 194 919 rows scanned) while still returning correct rows, so a
    mismatched combination is a regression nothing but a benchmark would reveal. The caller
    supplies only its own branch condition, never the ordering.
    """

    DEFAULT = "default"
    PROJECT_ORDERED_SCAN = "project_ordered_scan"


def _paged_routine_query(
    source: str, extra: str, icp: str, mt: str, return_fields: str,
    policy: PageSourcePolicy = PageSourcePolicy.DEFAULT,
) -> str:
    """Wrap a row source in a page subquery so enrichment runs on the page, not the match.

    With DEFAULT, `source` is the complete row source ending in its leading WHERE predicate.
    With PROJECT_ORDERED_SCAN, `source` is only this branch's boolean condition. `extra`
    appends the common `AND ...` filters in both cases.

    The outer ORDER BY is mandatory rather than cosmetic. `_shape_search_bsl_routines_result`
    trims the page by position (`rows[:lim]`), and `_routine_interception_block` aggregates,
    after which row order is not guaranteed — without the outer sort the lookahead row could
    displace a row of the page and produce duplicates and gaps across offsets.
    """
    if policy is PageSourcePolicy.PROJECT_ORDERED_SCAN:
        head = (
            "MATCH (r:Routine)\n"
            "USING INDEX r:Routine(project_name, name)\n"
            "WHERE r.project_name = $project_name\n"
            "  AND r.name IS NOT NULL\n"
            f"  AND {source}"
        )
        # Must start with the index columns in index order, otherwise the planner does not
        # recognise the ordering and falls back to materialising every match.
        inner_order = "r.project_name, r.name, r.id"
    else:
        head = source
        inner_order = "r.name, r.id"

    return f"""
CALL () {{
{head}
  {extra}
  RETURN r ORDER BY {inner_order} SKIP $offset LIMIT $limit
}}
WITH r
{icp}{mt}
RETURN {return_fields}
ORDER BY name, id
""".strip()


def _run_routine_query_with_fallback(loader, pn, params, build_cypher, src, scan_src):
    """Run a substring query, retrying on the scan path if the accelerator misbehaves.

    Catches any ProcedureCallFailed, not only Lucene parse errors: querying a fulltext
    index that no longer exists raises the same code with a message carrying no Lucene
    markers, and returning correct-but-slower rows beats returning an error. The verifier
    predicate is identical in both sources, so the fallback cannot change the result.
    """
    try:
        return _run_query(loader, build_cypher(src.cypher), {**params, **src.params}, pn)
    except Exception as e:
        code = getattr(e, "code", "") or ""
        if src.used_index is None or code != "Neo.ClientError.Procedure.ProcedureCallFailed":
            raise
        logger.warning(
            "Substring accelerator %s failed, falling back to scan: %s", src.used_index, e,
        )
        from .neo4j_init import invalidate_accelerator
        invalidate_accelerator(src.used_index)
        return _run_query(
            loader, build_cypher(scan_src.cypher), {**params, **scan_src.params}, pn,
        )


def _substring_sources(target, text: str, param_name: str):
    """Accelerated source (when its index is ONLINE) plus the scan source used as fallback."""
    from graphdb.routine_substring_queries import build_routine_substring_source
    from .neo4j_init import is_accelerator_ready

    src = build_routine_substring_source(
        target=target, text=text, param_name=param_name,
        accelerator_ready=is_accelerator_ready(target),
    )
    scan_src = build_routine_substring_source(
        target=target, text=text, param_name=param_name, accelerator_ready=False,
    )
    return src, scan_src


def _module_type_block(has_interception: bool) -> str:
    if has_interception:
        return (
            "\nOPTIONAL MATCH (mod:Module)-[:DECLARES]->(r)"
            "\nWITH r, interception, coalesce(mod.module_type,'CommonModule') AS module_type"
        )
    return (
        "\nOPTIONAL MATCH (mod:Module)-[:DECLARES]->(r)"
        "\nWITH r, coalesce(mod.module_type,'CommonModule') AS module_type"
    )


def _form_event_action_interception_block() -> str:
    """Cypher block for FormEventAction — analogue of _routine_interception_block().
    Variable fea must be the only variable in scope before this block.
    Ends with fea + interception in scope for the subsequent RETURN.
    Scoping via qualified_name prefix (FormEventAction has no project_name property).
    """
    return """
OPTIONAL MATCH (fea)-[:EXTENDS_ACTION]->(base_fea:FormEventAction)
WITH fea, base_fea.config_name AS _base_cfg
OPTIONAL MATCH (ext_fea:FormEventAction)-[:EXTENDS_ACTION]->(fea)
WHERE ext_fea.qualified_name STARTS WITH $project_prefix
WITH fea, _base_cfg,
  collect(DISTINCT {extension_config_name: ext_fea.config_name, call_type: ext_fea.call_type}) AS _ext_list
WITH fea,
  CASE
    WHEN _base_cfg IS NOT NULL
      THEN {role: 'extension', base_config_name: _base_cfg, call_type: fea.call_type}
    WHEN size([x IN _ext_list WHERE x.extension_config_name IS NOT NULL]) > 0
      THEN {role: 'base', extensions: [x IN _ext_list WHERE x.extension_config_name IS NOT NULL]}
    ELSE null
  END AS interception"""


# ---------------------------------------------------------------------------
# Tool 1: get_metadata
# ---------------------------------------------------------------------------

def _shape_get_metadata_result(
    mode: str,
    rows: list,
    *,
    lim: Optional[int] = None,
    off: int = 0,
    pageable: bool = False,
) -> Dict[str, Any]:
    """Build normalized response for get_metadata.

    Pageable modes (configurations/categories/objects): caller is expected to
    have fetched `lim + 1` rows; helper trims to `lim` and sets has_more.

    Summary mode: applies a hard cap by settings.query_max_results and exposes
    page.truncated (bool) — keeps the protection that the legacy _done provided.
    """
    summary_mode = mode == "summary"
    truncated = False

    if summary_mode:
        max_n = int(settings.query_max_results)
        truncated = len(rows) > max_n
        if truncated:
            rows = rows[:max_n]
    elif pageable and lim is not None:
        has_more_local = len(rows) > lim
        if has_more_local:
            rows = rows[:lim]
    else:
        has_more_local = False

    # Build local config_id map by first appearance in already-sorted rows.
    # For mode="configurations", each row IS a configuration and qualified_name is
    # the Configuration's QN. For other modes, rows carry config_qn alongside the
    # MetadataObject/MetadataCategory qualified_name.
    config_id_map: Dict[str, str] = {}
    configurations: List[Dict[str, Any]] = []
    for row in rows:
        cname = row.get("config_name")
        if not isinstance(cname, str) or not cname:
            continue
        if cname in config_id_map:
            continue
        cid = f"cfg{len(config_id_map) + 1}"
        config_id_map[cname] = cid
        if mode == "configurations":
            cfg_qn = row.get("qualified_name")
        else:
            cfg_qn = row.get("config_qn")
        configurations.append({
            "config_id": cid,
            "config_name": cname,
            "qualified_name": cfg_qn,
            "is_extension": bool(row.get("is_extension", False)),
        })

    shaped: Dict[str, Any] = {}

    if summary_mode:
        category_counts = [
            {
                "config_id": config_id_map.get(row.get("config_name", ""), ""),
                "category": row.get("category"),
                "object_count": row.get("object_count"),
            }
            for row in rows
        ]
        page = {"returned": len(category_counts), "has_more": False, "truncated": truncated}
        shaped["page"] = page
        shaped["configurations"] = configurations
        shaped["category_counts"] = category_counts
        return shaped

    if mode == "configurations":
        page = {
            "limit": lim,
            "offset": off,
            "returned": len(configurations),
            "has_more": has_more_local,
        }
        if has_more_local:
            page["next_offset"] = off + len(configurations)
        shaped["page"] = page
        shaped["configurations"] = configurations
        return shaped

    if mode == "categories":
        # Group categories by (config_id, qualified_name_prefix) so the shared
        # config QN lives once on the group and category rows carry only the name.
        # Full category QN is restored as qualified_name_prefix + "/" + category.
        category_groups: List[Dict[str, Any]] = []
        cat_group_index: Dict[Tuple[str, Any], Dict[str, Any]] = {}
        total_categories = 0
        for row in rows:
            config_id = config_id_map.get(row.get("config_name", ""), "")
            category = row.get("category")
            qn = row.get("qualified_name")
            # Strip the trailing "/<category>" to obtain the shared prefix; 1C
            # category names never contain "/", so this is unambiguous. Fall back
            # to the full qualified_name if it does not end with the expected suffix.
            prefix = qn
            if isinstance(qn, str) and isinstance(category, str):
                suffix = "/" + category
                if qn.endswith(suffix):
                    prefix = qn[: -len(suffix)]
            key = (config_id, prefix)
            group = cat_group_index.get(key)
            if group is None:
                group = {
                    "config_id": config_id,
                    "qualified_name_prefix": prefix,
                    "categories": [],
                }
                cat_group_index[key] = group
                category_groups.append(group)
            group["categories"].append({"category": category})
            total_categories += 1
        page = {
            "limit": lim,
            "offset": off,
            "returned": total_categories,
            "has_more": has_more_local,
        }
        if has_more_local:
            page["next_offset"] = off + total_categories
        shaped["page"] = page
        shaped["configurations"] = configurations
        shaped["category_groups"] = category_groups
        return shaped

    # mode == "objects"
    # Group objects by (config_id, category, qualified_name_prefix) so the shared
    # context lives once on the group and object rows carry only name + adoption.
    # Full object QN is restored as qualified_name_prefix + "/" + name.
    object_groups: List[Dict[str, Any]] = []
    group_index: Dict[Tuple[str, Any, Any], Dict[str, Any]] = {}
    total_objects = 0
    for row in rows:
        config_id = config_id_map.get(row.get("config_name", ""), "")
        category = row.get("category")
        name = row.get("name")
        qn = row.get("qualified_name")
        # Strip the trailing "/<name>" to obtain the shared prefix; 1C object
        # names never contain "/", so this is unambiguous. Fall back to the full
        # qualified_name if it does not end with the expected suffix.
        prefix = qn
        if isinstance(qn, str) and isinstance(name, str):
            suffix = "/" + name
            if qn.endswith(suffix):
                prefix = qn[: -len(suffix)]
        key = (config_id, category, prefix)
        group = group_index.get(key)
        if group is None:
            group = {
                "config_id": config_id,
                "category": category,
                "qualified_name_prefix": prefix,
                "objects": [],
            }
            group_index[key] = group
            object_groups.append(group)
        obj: Dict[str, Any] = {"name": name}
        if "adoption" in row and row.get("adoption") is not None:
            obj["adoption"] = row.get("adoption")
        group["objects"].append(obj)
        total_objects += 1
    page = {
        "limit": lim,
        "offset": off,
        "returned": total_objects,
        "has_more": has_more_local,
    }
    if has_more_local:
        page["next_offset"] = off + total_objects
    shaped["page"] = page
    shaped["configurations"] = configurations
    shaped["object_groups"] = object_groups
    return shaped


def _shape_find_metadata_usages_result(
    mode: str,
    rows: list,
    *,
    lim: int,
    off: int,
) -> Dict[str, Any]:
    """Normalize find_metadata_usages output into a compact grouped shape.

    objects / register_movements reuse the get_metadata "objects" grouping
    ({page, configurations, object_groups}); rows must carry config_name,
    category, name, qualified_name, config_qn, is_extension and optional adoption.
    paths returns {page, paths} preserving target_config_name/target_qn/config_name/path.
    Caller is expected to have fetched lim + 1 rows; helper trims to lim and sets has_more.
    """
    if mode in ("objects", "register_movements"):
        return _shape_get_metadata_result("objects", rows, lim=lim, off=off, pageable=True)

    # mode == "paths"
    has_more_local = len(rows) > lim
    if has_more_local:
        rows = rows[:lim]
    paths: List[Dict[str, Any]] = []
    for row in rows:
        paths.append({
            "target_config_name": row.get("target_config_name"),
            "target_qn": row.get("target_qn"),
            "config_name": row.get("config_name"),
            "path": row.get("path"),
        })
    page: Dict[str, Any] = {
        "limit": lim,
        "offset": off,
        "returned": len(paths),
        "has_more": has_more_local,
    }
    if has_more_local:
        page["next_offset"] = off + len(paths)
    return {"page": page, "paths": paths}


def _shape_find_metadata_elements_result(rows: list, *, lim: int, off: int) -> Dict[str, Any]:
    """Wrap find_metadata_elements rows into the flat paged shape {page, elements}.

    Caller fetches lim + 1 rows; helper trims to lim and derives has_more.
    Element fields are branch-specific and left untouched: rows are homogeneous
    within one response because each call runs a single element_type branch.
    """
    has_more = len(rows) > lim
    elements = rows[:lim] if has_more else rows
    page: Dict[str, Any] = {
        "limit": lim,
        "offset": off,
        "returned": len(elements),
        "has_more": has_more,
    }
    if has_more:
        page["next_offset"] = off + len(elements)
    return {"page": page, "elements": elements}


def _shape_event_subscriptions_result(rows: list, *, lim: int, off: int) -> Dict[str, Any]:
    """Wrap get_event_subscriptions rows into the flat paged shape {page, subscriptions}.

    Caller fetches lim + 1 rows; helper trims to lim and derives has_more.
    All modes emit rows under the single `subscriptions` key; rows are homogeneous
    within one response because each call runs a single mode branch.
    """
    has_more = len(rows) > lim
    subscriptions = rows[:lim] if has_more else rows
    page: Dict[str, Any] = {
        "limit": lim,
        "offset": off,
        "returned": len(subscriptions),
        "has_more": has_more,
    }
    if has_more:
        page["next_offset"] = off + len(subscriptions)
    return {"page": page, "subscriptions": subscriptions}


def _shape_find_form_links_result(rows: list, *, lim: int, off: int) -> Dict[str, Any]:
    """Wrap find_form_links rows into the flat paged shape {page, links}.

    Caller fetches lim + 1 rows; helper trims to lim and derives has_more.
    Link fields are branch-specific and left untouched: rows are homogeneous
    within one response because each call runs a single mode branch.
    """
    has_more = len(rows) > lim
    links = rows[:lim] if has_more else rows
    page: Dict[str, Any] = {
        "limit": lim,
        "offset": off,
        "returned": len(links),
        "has_more": has_more,
    }
    if has_more:
        page["next_offset"] = off + len(links)
    return {"page": page, "links": links}


def _shape_find_dependency_paths_result(paths: list, *, lim: int, off: int) -> Dict[str, Any]:
    """Wrap a window of DependencyPath into the flat paged shape {page, paths, multi_steps, _hint}.

    Caller passes lim + 1 deduped paths; helper trims to lim and derives has_more.
    Rows are built by the shared _dependencies_to_tables helper (path_id join key,
    step_count, multi_steps only for paths with >1 step, *_qn for every node incl.
    Routine id). relationship_chain is joined to a scalar string so paths[] and
    multi_steps[] fold into TOON tables.
    """
    has_more = len(paths) > lim
    visible = paths[:lim] if has_more else paths
    page: Dict[str, Any] = {
        "limit": lim,
        "offset": off,
        "returned": len(visible),
        "has_more": has_more,
    }
    if has_more:
        page["next_offset"] = off + len(visible)
    tables = _dependencies_to_tables(visible)
    for row in tables["paths"]:
        row["relationship_chain"] = " -> ".join(row["relationship_chain"])
    return {
        "page": page,
        "paths": tables["paths"],
        "multi_steps": tables["multi_steps"],
        "_hint": tables["_hint"],
    }


# Owner-context key of a find_predefined_values row: the group identity.
_PREDEFINED_OWNER_KEYS = ("config_name", "category", "object", "owner_qn")
# Whitelist of predefined-value fields exposed in object_groups[].predefined[].
# The public MCP shape is fixed by this tuple, NOT by whatever aliases the Cypher
# RETURN happens to carry: a stray sort/carry alias must never leak into the wire
# response without an explicit change here (and in return-docs + tests).
_PREDEFINED_VALUE_KEYS = (
    "name", "qualified_name", "code", "description",
    "flag_name", "flag_value", "account_type", "subconto_kind", "adoption",
)


def _shape_find_predefined_values_result(rows: list, *, lim: int, off: int) -> Dict[str, Any]:
    """Wrap find_predefined_values rows into the grouped paged shape {page, object_groups}.

    Caller fetches lim + 1 rows; helper trims to lim and derives has_more. Pagination
    is row-based over predefined values, so page.returned counts predefined values, not
    groups (len(object_groups) <= page.returned). A single owner group may be split
    across a page boundary — next_offset continues the value list correctly.

    Each visible row is split by owner key (_PREDEFINED_OWNER_KEYS) into a group, and
    only whitelisted value fields (_PREDEFINED_VALUE_KEYS) move into group["predefined"].
    None-valued fields are dropped so the TOON table stays homogeneous.
    """
    has_more = len(rows) > lim
    visible = rows[:lim] if has_more else rows
    page: Dict[str, Any] = {
        "limit": lim,
        "offset": off,
        "returned": len(visible),
        "has_more": has_more,
    }
    if has_more:
        page["next_offset"] = off + len(visible)

    groups: Dict[tuple, Dict[str, Any]] = {}  # insertion order = first-seen owner order
    for row in visible:
        key = tuple(row.get(k) for k in _PREDEFINED_OWNER_KEYS)
        group = groups.get(key)
        if group is None:
            group = {k: row.get(k) for k in _PREDEFINED_OWNER_KEYS}
            group["predefined"] = []
            groups[key] = group
        item = {
            k: row[k]
            for k in _PREDEFINED_VALUE_KEYS
            if k in row and row[k] is not None
        }
        group["predefined"].append(item)

    return {"page": page, "object_groups": list(groups.values())}


# Snake-case binding target types, inverse of the target_label_map used at fetch time.
_BINDING_LABEL_TO_TYPE = {
    "Attribute": "attribute",
    "Dimension": "dimension",
    "Resource": "resource",
    "FormAttribute": "form_attribute",
    "MetadataObject": "metadata_object",
}


def _shape_get_form_structure_result(
    section_rows: Dict[str, list], *, context: Dict[str, Any], lim: int, off: int,
) -> Dict[str, Any]:
    """Shape per-section fetch rows into {context, pages, <section>[]}.

    Caller fetches lim + 1 rows per section; helper trims each to lim and derives
    has_more/next_offset. events splits into flat form_events[] + event_actions[];
    only form_events is paged — event_actions is derived from the current
    form_events page and gets no pages entry. bindings emits forms[] + local
    form_id when context.forms_scope == "all" (multi-form scan), otherwise flat
    single-form bindings[]. Fields already carried by context (config_name, form,
    form_qn, owner_qn) are stripped from rows so the wire tables stay compact."""
    out: Dict[str, Any] = {"context": context, "pages": {}}
    single_form_qn = context.get("form_qn")
    object_qn = context.get("object_qn")

    def _page(section: str, page_rows: list, has_more: bool) -> None:
        page: Dict[str, Any] = {
            "limit": lim, "offset": off, "returned": len(page_rows), "has_more": has_more,
        }
        if has_more:
            page["next_offset"] = off + len(page_rows)
        out["pages"][section] = page

    def _strip_repeats(row: Dict[str, Any]) -> Dict[str, Any]:
        r = dict(row)
        r.pop("config_name", None)
        r.pop("form", None)
        r.pop("form_qn", None)
        owner_qn = r.get("owner_qn")
        if owner_qn is not None and owner_qn in (single_form_qn, object_qn):
            r.pop("owner_qn", None)
        return r

    for sec, rows in section_rows.items():
        has_more = len(rows) > lim
        page_rows = rows[:lim] if has_more else rows

        if sec == "controls":
            out["controls"] = [_strip_repeats(r) for r in page_rows]
            _page("controls", page_rows, has_more)

        elif sec == "events":
            form_events: List[Dict[str, Any]] = []
            event_actions: List[Dict[str, Any]] = []
            for r in page_rows:
                event_qn = r.get("qualified_name")
                fe: Dict[str, Any] = {
                    "event_qn": event_qn,
                    "event": r.get("event"),
                    "source": r.get("source"),
                    "source_qn": r.get("source_qn", ""),
                }
                if "adoption" in r:
                    fe["adoption"] = r.get("adoption")
                form_events.append(fe)
                for a in (r.get("actions") or []):
                    event_actions.append({
                        "event_qn": event_qn,
                        "call_type": a.get("call_type"),
                        "handler_name": a.get("handler_name"),
                    })
            out["form_events"] = form_events
            out["event_actions"] = event_actions
            # Only form_events is paged; event_actions is a derived detail table.
            _page("form_events", page_rows, has_more)

        elif sec == "event_handlers":
            out["event_handlers"] = [_strip_repeats(r) for r in page_rows]
            _page("event_handlers", page_rows, has_more)

        elif sec == "attributes":
            out["form_attributes"] = [_strip_repeats(r) for r in page_rows]
            _page("form_attributes", page_rows, has_more)

        elif sec == "commands":
            out["form_commands"] = [_strip_repeats(r) for r in page_rows]
            _page("form_commands", page_rows, has_more)

        elif sec == "command_usages":
            out["command_usages"] = [_strip_repeats(r) for r in page_rows]
            _page("command_usages", page_rows, has_more)

        elif sec == "bindings":
            if context.get("forms_scope") == "all":
                forms: List[Dict[str, Any]] = []
                form_ids: Dict[Any, str] = {}
                bindings: List[Dict[str, Any]] = []
                for r in page_rows:
                    fqn = r.get("form_qn")
                    if fqn not in form_ids:
                        fid = f"form{len(form_ids) + 1}"
                        form_ids[fqn] = fid
                        forms.append({"form_id": fid, "name": r.get("form"),
                                      "qualified_name": fqn})
                    b = dict(r)
                    b.pop("form", None)
                    b.pop("form_qn", None)
                    b.pop("config_name", None)
                    if "target_label" in b:
                        b["target_type"] = _BINDING_LABEL_TO_TYPE.get(b.pop("target_label"))
                    b["form_id"] = form_ids[fqn]
                    bindings.append(b)
                out["forms"] = forms
                out["bindings"] = bindings
            else:
                bindings = []
                for r in page_rows:
                    b = _strip_repeats(r)
                    b.pop("form_id", None)
                    if "target_label" in b:
                        b["target_type"] = _BINDING_LABEL_TO_TYPE.get(b.pop("target_label"))
                    bindings.append(b)
                out["bindings"] = bindings
            _page("bindings", page_rows, has_more)

    return out


# Stable per-object field set for search_by="description"; missing values become None
# so every description object row carries an identical key set (predictable TOON tables).
# Split into text vs score fields: score fields are compacted (rounded) for the response,
# text fields are passed through. _FIND_OBJECTS_DESCRIPTION_FIELDS stays as the union so
# the stable-key contract (and its test import) is unchanged.
_FIND_OBJECTS_DESCRIPTION_TEXT_FIELDS = ("synonym", "comment", "explanation")
_FIND_OBJECTS_DESCRIPTION_SCORE_FIELDS = (
    "score", "similarity", "fulltext_score", "vector_score", "hybrid_score",
)
_FIND_OBJECTS_DESCRIPTION_FIELDS = (
    _FIND_OBJECTS_DESCRIPTION_TEXT_FIELDS + _FIND_OBJECTS_DESCRIPTION_SCORE_FIELDS
)


def _enrich_find_metadata_objects_context(
    loader: Any,
    rows: list,
    pn: str,
    has_extensions: bool,
) -> list:
    """Attach config_qn + is_extension (batched by config_name) to find_metadata_objects
    rows so the grouped shaper can build configurations[]. Description rows arrive from the
    search service without full config context, so for them adoption is also backfilled to
    {role: 'none'} where missing — an already-computed adoption is never overwritten.
    """
    if not rows:
        return rows
    names = sorted({
        r.get("config_name") for r in rows
        if isinstance(r, dict) and isinstance(r.get("config_name"), str) and r.get("config_name")
    })
    cfg_map: Dict[str, Dict[str, Any]] = {}
    if names:
        cfg_rows = _run_query(
            loader,
            "MATCH (c:Configuration {project_name: $project_name})\n"
            "WHERE c.name IN $names\n"
            "RETURN c.name AS config_name, c.qualified_name AS config_qn, "
            "coalesce(c.is_extension, false) AS is_extension",
            {"names": names},
            pn,
        )
        cfg_map = {
            r["config_name"]: r for r in cfg_rows
            if isinstance(r, dict) and r.get("config_name")
        }
    for r in rows:
        if not isinstance(r, dict):
            continue
        info = cfg_map.get(r.get("config_name"))
        if info is not None:
            r.setdefault("config_qn", info.get("config_qn"))
            r.setdefault("is_extension", bool(info.get("is_extension", False)))
        if has_extensions and r.get("adoption") is None:
            r["adoption"] = {"role": "none"}
    return rows


def _compact_response_score(value: Any) -> Optional[float]:
    """Round a description-result score field to 4 decimals for a compact MCP response.

    None -> None. Non-finite (NaN/inf) -> None (invalid for JSON, useless as a score).
    Otherwise round(float(value), 4); the result is always a float, so 1 -> 1.0.
    Matches the existing round(float(...), 4) convention used elsewhere in this module.
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, 4)


def _shape_find_metadata_objects_result(
    search_by: str,
    rows: list,
    *,
    lim: int,
    off: int,
    include_help_text: bool = False,
) -> Dict[str, Any]:
    """Normalize find_metadata_objects output into the compact grouped shape
    {page, configurations, object_groups} shared with get_metadata / find_metadata_usages.

    Rows must carry config_name, category, name, qualified_name, config_qn, is_extension and
    optional adoption (see _enrich_find_metadata_objects_context). search_by selects the extra
    per-object fields:
      - description: stable synonym/comment/explanation + score family (missing -> None);
        help_text only when include_help_text is True.
      - form_control: form_qn.
    Caller is expected to have fetched lim + 1 rows; helper trims to lim and sets has_more.
    """
    has_more_local = len(rows) > lim
    if has_more_local:
        rows = rows[:lim]

    config_id_map: Dict[str, str] = {}
    configurations: List[Dict[str, Any]] = []
    for row in rows:
        cname = row.get("config_name")
        if not isinstance(cname, str) or not cname:
            continue
        if cname in config_id_map:
            continue
        cid = f"cfg{len(config_id_map) + 1}"
        config_id_map[cname] = cid
        configurations.append({
            "config_id": cid,
            "config_name": cname,
            "qualified_name": row.get("config_qn"),
            "is_extension": bool(row.get("is_extension", False)),
        })

    # Group objects by (config_id, category, qualified_name_prefix) so the shared context
    # lives once on the group; full object QN = qualified_name_prefix + "/" + name.
    object_groups: List[Dict[str, Any]] = []
    group_index: Dict[Tuple[str, Any, Any], Dict[str, Any]] = {}
    total_objects = 0
    for row in rows:
        config_id = config_id_map.get(row.get("config_name", ""), "")
        category = row.get("category")
        name = row.get("name")
        qn = row.get("qualified_name")
        prefix = qn
        if isinstance(qn, str) and isinstance(name, str):
            suffix = "/" + name
            if qn.endswith(suffix):
                prefix = qn[: -len(suffix)]
        key = (config_id, category, prefix)
        group = group_index.get(key)
        if group is None:
            group = {
                "config_id": config_id,
                "category": category,
                "qualified_name_prefix": prefix,
                "objects": [],
            }
            group_index[key] = group
            object_groups.append(group)
        obj: Dict[str, Any] = {"name": name}
        if "adoption" in row and row.get("adoption") is not None:
            obj["adoption"] = row.get("adoption")
        if search_by == "description":
            for k in _FIND_OBJECTS_DESCRIPTION_TEXT_FIELDS:
                obj[k] = row.get(k)
            for k in _FIND_OBJECTS_DESCRIPTION_SCORE_FIELDS:
                obj[k] = _compact_response_score(row.get(k))
            if include_help_text:
                obj["help_text"] = row.get("help_text")
        elif search_by == "form_control":
            obj["form_qn"] = row.get("form_qn")
        group["objects"].append(obj)
        total_objects += 1

    page: Dict[str, Any] = {
        "limit": lim,
        "offset": off,
        "returned": total_objects,
        "has_more": has_more_local,
    }
    if has_more_local:
        page["next_offset"] = off + total_objects
    return {
        "page": page,
        "configurations": configurations,
        "object_groups": object_groups,
    }


def get_metadata(
    mode: Literal["summary", "configurations", "categories", "objects"] = "summary",
    category: Optional[str] = None,
    object_name: Optional[str] = None,
    object_match: Optional[Literal["exact", "starts_with", "contains"]] = None,
    config: Optional[str] = None,
    only_adopted: bool = False,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    project_name: Optional[str] = None,
) -> str:
    """Browse top-level 1C metadata inventory.

Modes:
- summary: object counts by configuration and category.
- configurations: list base configuration and extensions.
- categories: list metadata categories per configuration.
- objects: list metadata objects; filter by category and/or object_name.

config scopes to one configuration or extension.
only_adopted applies only to objects.

Response note: cfgN ids are local join keys within one response; use
qualified_name to correlate across pages. List modes use page.has_more/next_offset.
Summary is not paged; page.truncated=true means the overview was capped.
"""
    loader = _init_loader()
    if loader is None:
        return "Error: Neo4j database connection not available."
    try:
        pn = _resolve_project(project_name)
        config_name = resolve_config(loader, config, pn)
        scope = _scope(config_name)
        lim = clamp_limit(limit)
        off = clamp_offset(offset)
        fetch_limit = lim + 1

        if mode == "configurations":
            cypher = """
MATCH (c:Configuration {project_name: $project_name})
RETURN c.name AS config_name, coalesce(c.is_extension, false) AS is_extension, c.qualified_name AS qualified_name
ORDER BY c.is_extension, c.name
SKIP $offset LIMIT $limit
""".strip()
            results = _run_query(loader, cypher, {"limit": fetch_limit, "offset": off}, pn)
            shaped = _shape_get_metadata_result(mode, results, lim=lim, off=off, pageable=True)
            return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

        elif mode == "categories":
            cypher = f"""
MATCH (c:MetadataCategory {{project_name: $project_name{scope.metadata_map}}})
MATCH (cfg:Configuration {{project_name: $project_name, name: c.config_name}})
RETURN c.name AS category, c.config_name AS config_name, c.qualified_name AS qualified_name,
       cfg.qualified_name AS config_qn, coalesce(cfg.is_extension, false) AS is_extension
ORDER BY config_name, category
SKIP $offset LIMIT $limit
""".strip()
            params: Dict[str, Any] = {"limit": fetch_limit, "offset": off}
            if config_name:
                params["config_name"] = config_name
            results = _run_query(loader, cypher, params, pn)
            shaped = _shape_get_metadata_result(mode, results, lim=lim, off=off, pageable=True)
            return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

        elif mode == "objects":
            has_extensions = bool(_run_query(
                loader,
                "MATCH (c:Configuration {project_name: $project_name, is_extension: true}) RETURN c LIMIT 1",
                {},
                pn,
            ))
            if only_adopted and not has_extensions:
                shaped = _shape_get_metadata_result(mode, [], lim=lim, off=off, pageable=True)
                return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

            params = {"limit": fetch_limit, "offset": off}
            if config_name:
                params["config_name"] = config_name

            _ONLY_ADOPTED_COND = (
                "(EXISTS { (:MetadataObject {project_name: $project_name})-[:ADOPTED_FROM]->(m) }"
                " OR EXISTS { (m)-[:ADOPTED_FROM]->(:MetadataObject {project_name: $project_name}) })"
            )

            only_adopted_and = f"AND {_ONLY_ADOPTED_COND}" if only_adopted else ""
            only_adopted_where = f"WHERE {_ONLY_ADOPTED_COND}" if only_adopted else ""
            adoption_col = ", adoption" if has_extensions else ""
            adopted_block = _ADOPTED_COLLECT if has_extensions else ""

            # `_ADOPTED_COLLECT` finishes with `WITH m, ... AS adoption` and drops cfg
            # from scope. Re-match Configuration before RETURN so cfg fields are
            # available without modifying the shared _ADOPTED_COLLECT constant.
            if has_extensions:
                cfg_match = "WITH m, adoption\nMATCH (cfg:Configuration {project_name: $project_name, name: m.config_name})"
            else:
                cfg_match = "WITH m\nMATCH (cfg:Configuration {project_name: $project_name, name: m.config_name})"
            cfg_cols = ", cfg.qualified_name AS config_qn, coalesce(cfg.is_extension, false) AS is_extension"

            if category and category.strip() == "HTTPСервисы":
                cypher = f"""
MATCH (:MetadataCategory {{name:'HTTPСервисы'}})-[:CONTAINS_OBJECT]->(m:MetadataObject {scope.map_for()})
{only_adopted_where}
{adopted_block}
{cfg_match}
RETURN m.config_name AS config_name, m.category_name AS category,
       m.name AS name, m.qualified_name AS qualified_name{adoption_col}{cfg_cols}
ORDER BY name
SKIP $offset LIMIT $limit
""".strip()
            elif config_name and not object_name and not category:
                cypher = f"""
MATCH (m:MetadataObject {{project_name: $project_name, config_name: $config_name}})
{only_adopted_where}
{adopted_block}
{cfg_match}
RETURN m.name AS name, m.category_name AS category,
       m.config_name AS config_name, m.qualified_name AS qualified_name{adoption_col}{cfg_cols}
ORDER BY category, name
SKIP $offset LIMIT $limit
""".strip()
            elif object_name:
                params["name"] = object_name.strip()
                match_mode = (object_match or "exact").lower()
                name_cond = apply_match("m.name", "name", match_mode)
                if category:
                    params["category"] = category.strip()
                    base = f"MATCH (:MetadataCategory {{name:$category}})-[:CONTAINS_OBJECT]->(m:MetadataObject {scope.map_for()})"
                else:
                    base = f"MATCH (m:MetadataObject {scope.map_for()})"
                cypher = f"""
{base}
WHERE {name_cond} {only_adopted_and}
{adopted_block}
{cfg_match}
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category,
       m.name AS name, m.qualified_name AS qualified_name{adoption_col}{cfg_cols}
ORDER BY config_name, name
SKIP $offset LIMIT $limit
""".strip()
            elif category:
                params["category"] = category.strip()
                cypher = f"""
MATCH (:MetadataCategory {{name:$category}})-[:CONTAINS_OBJECT]->(m:MetadataObject {scope.map_for()})
{only_adopted_where}
{adopted_block}
{cfg_match}
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category,
       m.name AS name, m.qualified_name AS qualified_name{adoption_col}{cfg_cols}
ORDER BY config_name, name
SKIP $offset LIMIT $limit
""".strip()
            else:
                cypher = f"""
MATCH (m:MetadataObject {scope.map_for()})
{only_adopted_where}
{adopted_block}
{cfg_match}
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category,
       m.name AS name, m.qualified_name AS qualified_name{adoption_col}{cfg_cols}
ORDER BY config_name, category, name
SKIP $offset LIMIT $limit
""".strip()

            results = _run_query(loader, cypher, params, pn)
            shaped = _shape_get_metadata_result(mode, results, lim=lim, off=off, pageable=True)
            return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

        else:  # summary
            cypher = """
MATCH (c:Configuration {project_name: $project_name})
WHERE ($config_name IS NULL OR c.name = $config_name)
MATCH (m:MetadataObject {project_name: $project_name, config_name: c.name})
RETURN c.name AS config_name, c.qualified_name AS config_qn,
       coalesce(c.is_extension, false) AS is_extension,
       m.category_name AS category, count(m) AS object_count
ORDER BY config_name, category
""".strip()
            results = _run_query(loader, cypher, {"config_name": config_name}, pn)
            shaped = _shape_get_metadata_result(mode, results)
            return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception("Error in get_metadata")
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Tool 2: find_metadata_objects
# ---------------------------------------------------------------------------

def find_metadata_objects(
    search_by: Literal[
        "description", "attribute", "tabular_part", "tabular_attribute",
        "resource", "dimension", "form", "form_control", "form_attribute",
        "form_event", "command", "layout", "predefined_name", "journal_graph",
    ] = "attribute",
    search_text: Optional[str] = None,
    search_match: Optional[Literal["exact", "starts_with", "contains"]] = None,
    categories: Optional[List[str]] = None,
    tabular_part: Optional[str] = None,
    tabular_part_match: Optional[Literal["exact", "starts_with", "contains"]] = None,
    within_object: Optional[str] = None,
    within_form: Optional[str] = None,
    form_role: Optional[Literal["object", "group", "list", "picker", "group_picker"]] = None,
    default_form_only: Optional[bool] = None,
    form_event_source: Optional[Literal["form", "controls", "all"]] = None,
    min_score: Optional[float] = None,
    include_help_text: bool = False,
    config: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    project_name: Optional[str] = None,
) -> str:
    """Find metadata objects by description, child elements, forms, commands, layouts, or predefined items.

Use search_by to choose what search_text means:
- description: natural-language purpose/meaning of an object.
- attribute/tabular_part/resource/dimension/command/layout/predefined_name/journal_graph: element name.
- tabular_attribute: attribute name; pass tabular_part to scope the tabular part.
- form/form_control/form_attribute/form_event: form, control, attribute, or event name.

search_match applies to name-based modes, not description.
Use categories, config, within_object, within_form, form_role, default_form_only, and form_event_source to narrow results.
For description search, include_help_text=true adds full help text; default false keeps results compact.
"""
    loader = _init_loader()
    if loader is None:
        return "Error: Neo4j database connection not available."
    try:
        pn = _resolve_project(project_name)
        config_name = resolve_config(loader, config, pn)
        scope = _scope(config_name)
        lim = clamp_limit(limit)
        off = clamp_offset(offset)
        # Fetch lim + 1 so page.has_more can be derived explicitly by the shaper.
        params: Dict[str, Any] = {"limit": lim + 1, "offset": off}
        if config_name:
            params["config_name"] = config_name

        mm = (search_match or "exact").lower()

        has_extensions = bool(_run_query(
            loader,
            "MATCH (c:Configuration {project_name: $project_name, is_extension: true}) RETURN c LIMIT 1",
            {},
            pn,
        ))
        adoption_col = ", adoption" if has_extensions else ""
        adopted_block = _ADOPTED_COLLECT if has_extensions else ""

        def _emit(rows_in: list, sb: str) -> str:
            rows_out = _enrich_find_metadata_objects_context(loader, rows_in, pn, has_extensions)
            shaped = _shape_find_metadata_objects_result(
                sb, rows_out, lim=lim, off=off, include_help_text=include_help_text,
            )
            return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

        if search_by == "description":
            if not search_text or not search_text.strip():
                return "Error: search_text is required for search_by='description'."
            text = search_text.strip()
            cats = [c.strip() for c in (categories or []) if isinstance(c, str) and c.strip()]
            # Validate category count after canonicalize/dedupe (before embedding round-trip).
            cats_canon = canon_categories(cats) if cats else []
            max_cats = int(getattr(settings, "vec_max_category_filters", 5) or 5)
            if len(cats_canon) > max_cats:
                return (
                    f"Error: too many categories after canonicalization ({len(cats_canon)} > {max_cats}). "
                    f"Reduce `categories` or raise VEC_MAX_CATEGORY_FILTERS."
                )
            ms = _min_score_adaptive(text, min_score)

            metadata_rerank_enabled = bool(getattr(settings, "metadata_description_rerank_enabled", False))
            if bool(settings.enable_metadata_description_embedding) or metadata_rerank_enabled:
                try:
                    from graphdb.metadata_search_service import MetadataSearchService
                    from graphdb.embedding_service import get_embedding_service
                    drv = loader._get_read_driver()
                    try:
                        embed = get_embedding_service()
                    except Exception:
                        embed = None
                    svc = MetadataSearchService(drv, embedding_service=embed)
                    rows = svc.search_by_description_hybrid(
                        text=text,
                        categories=cats,
                        limit=lim + 1,
                        offset=off,
                        min_score=ms,
                        project_name=pn,
                        config_name=config_name,
                    ) or []
                    for r in rows:
                        if isinstance(r, dict) and "score" not in r:
                            hs = r.get("hybrid_score", r.get("similarity"))
                            if hs is not None:
                                r["score"] = hs
                    if has_extensions and rows:
                        qns = [r["qualified_name"] for r in rows if isinstance(r, dict) and r.get("qualified_name")]
                        if qns:
                            enrich_cypher = (
                                "MATCH (m:MetadataObject {project_name: $project_name})\n"
                                "WHERE m.qualified_name IN $qns\n"
                                + _ADOPTED_COLLECT
                                + "\nRETURN m.qualified_name AS qualified_name, adoption"
                            )
                            enrich_rows = _run_query(loader, enrich_cypher, {"qns": qns}, pn)
                            adoption_map = {r["qualified_name"]: r["adoption"] for r in enrich_rows if "qualified_name" in r}
                            for r in rows:
                                if isinstance(r, dict):
                                    r["adoption"] = adoption_map.get(r.get("qualified_name"), {"role": "none"})
                    return _emit(rows, "description")
                except Exception as e:
                    logger.warning(f"Embedding description search failed, falling back: {e}")

            from graphdb.metadata_description_queries import build_metadata_description_fulltext_cypher
            from graphdb.fulltext_query import (
                build_fulltext_query_candidates,
                is_lucene_fulltext_parse_error,
            )

            ft_candidates = build_fulltext_query_candidates(text)
            if not ft_candidates:
                return _emit([], "description")

            ft_params = {
                "categories": cats_canon, "min_score": ms,
                "limit": lim + 1, "offset": off,
                "project_name": pn,
                "project_prefix": pn + "/",
            }
            if config_name:
                ft_params["config_name"] = config_name
            cypher = build_metadata_description_fulltext_cypher(config_name=config_name).strip()

            results: list = []
            last_parse_error: Optional[BaseException] = None
            for candidate in ft_candidates:
                attempt_params = dict(ft_params)
                attempt_params["text"] = candidate
                try:
                    results = loader.execute_query_readonly(cypher, attempt_params) or []
                except Exception as ft_err:
                    if is_lucene_fulltext_parse_error(ft_err):
                        logger.warning(
                            "Metadata fallback: Lucene parse error on candidate %r: %s",
                            candidate, ft_err,
                        )
                        last_parse_error = ft_err
                        continue
                    raise
                break
            else:
                logger.warning(
                    "Metadata fallback: all fulltext candidates failed Lucene parse for %r (last: %s)",
                    text, last_parse_error,
                )
                return _emit([], "description")

            if has_extensions and results:
                qns = [r["qualified_name"] for r in results if isinstance(r, dict) and r.get("qualified_name")]
                if qns:
                    enrich_cypher = (
                        "MATCH (m:MetadataObject {project_name: $project_name})\n"
                        "WHERE m.qualified_name IN $qns\n"
                        + _ADOPTED_COLLECT
                        + "\nRETURN m.qualified_name AS qualified_name, adoption"
                    )
                    enrich_rows = _run_query(loader, enrich_cypher, {"qns": qns}, pn)
                    adoption_map = {r["qualified_name"]: r["adoption"] for r in enrich_rows if "qualified_name" in r}
                    for r in results:
                        if isinstance(r, dict):
                            r["adoption"] = adoption_map.get(r.get("qualified_name"), {"role": "none"})
            return _emit(results, "description")

        if not search_text or not search_text.strip():
            return "Error: search_text is required."
        params["search_text"] = search_text.strip()

        # Category filter is applied inside Cypher (before SKIP/LIMIT) so pagination stays
        # correct. canon_categories() maps user input to graph category names and expands
        # generic inputs (e.g. "Регистры"); an unknown value canonicalizes to [] and must
        # mean "no match" (empty page), NOT "filter disabled". The size($cat_filter)=0 escape
        # below is therefore only reachable when no categories were requested at all.
        cats_raw = [c.strip() for c in (categories or []) if isinstance(c, str) and c.strip()]
        cats_canon = canon_categories(cats_raw) if cats_raw else []
        if cats_raw and not cats_canon:
            return _emit([], search_by)
        params["cat_filter"] = cats_canon
        _cat_node = "(size($cat_filter) = 0 OR m.category_name IN $cat_filter)"
        _cat_call = "WITH *\nWHERE (size($cat_filter) = 0 OR category IN $cat_filter)\n"

        if search_by == "attribute":
            cond = apply_match("a.name", "search_text", mm)
            cypher = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_ATTRIBUTE]->(a:Attribute)
WHERE {cond} AND {_cat_node}
{adopted_block}
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name{adoption_col}
ORDER BY config_name, name
SKIP $offset LIMIT $limit
""".strip()

        elif search_by == "tabular_part":
            cond = apply_match("t.name", "search_text", mm)
            cypher = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_TABULAR_PART]->(t:TabularPart)
WHERE {cond} AND {_cat_node}
{adopted_block}
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name{adoption_col}
ORDER BY config_name, name
SKIP $offset LIMIT $limit
""".strip()

        elif search_by == "tabular_attribute":
            if not tabular_part or not tabular_part.strip():
                return "Error: tabular_part is required for search_by='tabular_attribute'."
            params["tabular_part"] = tabular_part.strip()
            t_mm = (tabular_part_match or "exact").lower()
            t_cond = apply_match("t.name", "tabular_part", t_mm)
            a_cond = apply_match("a.name", "search_text", mm)
            cypher = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_TABULAR_PART]->(t:TabularPart)-[:HAS_ATTRIBUTE]->(a:Attribute)
WHERE {t_cond} AND {a_cond} AND {_cat_node}
{adopted_block}
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name{adoption_col}
ORDER BY config_name, name
SKIP $offset LIMIT $limit
""".strip()

        elif search_by == "resource":
            cond = apply_match("r.name", "search_text", mm)
            cypher = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_RESOURCE]->(r:Resource)
WHERE {cond} AND {_cat_node}
{adopted_block}
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name{adoption_col}
ORDER BY config_name, name
SKIP $offset LIMIT $limit
""".strip()

        elif search_by == "dimension":
            cond = apply_match("d.name", "search_text", mm)
            cypher = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_DIMENSION]->(d:Dimension)
WHERE {cond} AND {_cat_node}
{adopted_block}
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name{adoption_col}
ORDER BY config_name, name
SKIP $offset LIMIT $limit
""".strip()

        elif search_by == "form":
            name_cond = apply_match("f.name", "search_text", mm)
            name_cond_m = apply_match("m.name", "search_text", mm)
            params["role"] = form_role if form_role in ("object", "group", "list", "picker", "group_picker") else None
            params["default_only"] = bool(default_form_only) if default_form_only is not None else False
            cf_map = scope.map_for("category_name: 'ОбщиеФормы'")
            cypher = f"""
CALL {{
  MATCH (m:MetadataObject {scope.map_for()})-[r:HAS_FORM]->(f:Form)
  WHERE {name_cond}
    AND ($role IS NULL OR r.role = $role)
    AND (coalesce($default_only, false) = false OR r.is_default = true)
  {adopted_block}
  RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name{adoption_col}
  UNION
  MATCH (m:MetadataObject {cf_map})
  WHERE {name_cond_m}
  {adopted_block}
  RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name{adoption_col}
}}
{_cat_call}RETURN *
ORDER BY config_name, name
SKIP $offset LIMIT $limit
""".strip()

        elif search_by == "form_control":
            cond = apply_match("fc.name", "search_text", mm)
            params["within_object"] = within_object or None
            params["within_form"] = within_form or None
            cf_map = scope.map_for("category_name: 'ОбщиеФормы'")
            if has_extensions:
                adopted_block_fc = (
                    "WITH DISTINCT m, f.qualified_name AS form_qn\n"
                    "MATCH (cfg:Configuration {project_name: $project_name, name: m.config_name})\n"
                    "OPTIONAL MATCH (ext_m:MetadataObject {project_name: $project_name})-[:ADOPTED_FROM]->(m)\n"
                    "WITH m, form_qn, cfg, collect(DISTINCT ext_m.config_name) AS _ext_names\n"
                    "OPTIONAL MATCH (m)-[:ADOPTED_FROM]->(base_m:MetadataObject {project_name: $project_name})\n"
                    "WITH m, form_qn, cfg, _ext_names, base_m.config_name AS _base_cn\n"
                    "WITH m, form_qn,\n"
                    "     CASE\n"
                    "       WHEN NOT coalesce(cfg.is_extension, false) AND size(_ext_names) > 0\n"
                    "         THEN {role: 'base', extension_config_names: _ext_names}\n"
                    "       WHEN coalesce(cfg.is_extension, false) AND _base_cn IS NOT NULL\n"
                    "         THEN {role: 'extension', base_config_name: _base_cn}\n"
                    "       ELSE {role: 'none'}\n"
                    "     END AS adoption"
                )
                adopted_block_cf = (
                    "WITH DISTINCT m, m.qualified_name AS form_qn\n"
                    "MATCH (cfg:Configuration {project_name: $project_name, name: m.config_name})\n"
                    "OPTIONAL MATCH (ext_m:MetadataObject {project_name: $project_name})-[:ADOPTED_FROM]->(m)\n"
                    "WITH m, form_qn, cfg, collect(DISTINCT ext_m.config_name) AS _ext_names\n"
                    "OPTIONAL MATCH (m)-[:ADOPTED_FROM]->(base_m:MetadataObject {project_name: $project_name})\n"
                    "WITH m, form_qn, cfg, _ext_names, base_m.config_name AS _base_cn\n"
                    "WITH m, form_qn,\n"
                    "     CASE\n"
                    "       WHEN NOT coalesce(cfg.is_extension, false) AND size(_ext_names) > 0\n"
                    "         THEN {role: 'base', extension_config_names: _ext_names}\n"
                    "       WHEN coalesce(cfg.is_extension, false) AND _base_cn IS NOT NULL\n"
                    "         THEN {role: 'extension', base_config_name: _base_cn}\n"
                    "       ELSE {role: 'none'}\n"
                    "     END AS adoption"
                )
                form_qn_expr = "form_qn"
                form_qn_cf_expr = "form_qn"
            else:
                adopted_block_fc = ""
                adopted_block_cf = ""
                form_qn_expr = "f.qualified_name AS form_qn"
                form_qn_cf_expr = "m.qualified_name AS form_qn"
            cypher = f"""
CALL {{
  MATCH (m:MetadataObject {scope.map_for()})-[:HAS_FORM]->(f:Form)
  WHERE ($within_object IS NULL OR toLower(m.name) = toLower($within_object))
    AND ($within_form IS NULL OR toLower(f.name) = toLower($within_form))
  MATCH (f)-[:HAS_CONTROL]->(root:FormControl)
  MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)
  WHERE {cond}
  {adopted_block_fc}
  RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name, {form_qn_expr}{adoption_col}
  UNION
  MATCH (m:MetadataObject {cf_map})
  WHERE ($within_object IS NULL OR toLower(m.name) = toLower($within_object))
    AND ($within_form IS NULL OR toLower(m.name) = toLower($within_form))
  MATCH (m)-[:HAS_CONTROL]->(root:FormControl)
  MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)
  WHERE {cond}
  {adopted_block_cf}
  RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name, {form_qn_cf_expr}{adoption_col}
}}
{_cat_call}RETURN *
ORDER BY config_name, name
SKIP $offset LIMIT $limit
""".strip()

        elif search_by == "form_attribute":
            cond = apply_match("coalesce(fa.name,'')", "search_text", mm)
            cf_map = scope.map_for("category_name: 'ОбщиеФормы'")
            cypher = f"""
CALL {{
  MATCH (m:MetadataObject {scope.map_for()})-[:HAS_FORM]->(f:Form)-[:HAS_FORM_ATTRIBUTE]->(fa:FormAttribute)
  WHERE {cond}
  {adopted_block}
  RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name{adoption_col}
  UNION
  MATCH (m:MetadataObject {cf_map})-[:HAS_FORM_ATTRIBUTE]->(fa:FormAttribute)
  WHERE {cond}
  {adopted_block}
  RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name{adoption_col}
}}
{_cat_call}RETURN *
ORDER BY config_name, name
SKIP $offset LIMIT $limit
""".strip()

        elif search_by == "form_event":
            e_cond = apply_match("coalesce(e.`name`, e.name, '')", "search_text", mm)
            ev_src = (form_event_source or "all").lower()
            cf_map = scope.map_for("category_name: 'ОбщиеФормы'")
            form_branch = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_FORM]->(f:Form)-[:HAS_EVENT]->(e:FormEvent)
WHERE {e_cond}
{adopted_block}
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name{adoption_col}
""".strip()
            controls_branch = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_FORM]->(f:Form)-[:HAS_CONTROL]->(root:FormControl)
MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)-[:HAS_EVENT]->(e:FormEvent)
WHERE {e_cond}
{adopted_block}
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name{adoption_col}
""".strip()
            cf_form_branch = f"""
MATCH (m:MetadataObject {cf_map})-[:HAS_EVENT]->(e:FormEvent)
WHERE {e_cond}
{adopted_block}
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name{adoption_col}
""".strip()
            cf_controls_branch = f"""
MATCH (m:MetadataObject {cf_map})-[:HAS_CONTROL]->(root:FormControl)
MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)-[:HAS_EVENT]->(e:FormEvent)
WHERE {e_cond}
{adopted_block}
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name{adoption_col}
""".strip()
            if ev_src == "form":
                cypher = f"CALL {{\n{form_branch}\nUNION\n{cf_form_branch}\n}}\n{_cat_call}RETURN *\nORDER BY config_name, name\nSKIP $offset LIMIT $limit"
            elif ev_src == "controls":
                cypher = f"CALL {{\n{controls_branch}\nUNION\n{cf_controls_branch}\n}}\n{_cat_call}RETURN *\nORDER BY config_name, name\nSKIP $offset LIMIT $limit"
            else:
                cypher = f"""CALL {{\n{form_branch}\nUNION\n{controls_branch}\nUNION\n{cf_form_branch}\nUNION\n{cf_controls_branch}\n}}\n{_cat_call}RETURN *\nORDER BY config_name, name\nSKIP $offset LIMIT $limit"""

        elif search_by == "command":
            cond = apply_match("c.name", "search_text", mm)
            cypher = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_COMMAND]->(c:Command)
WHERE {cond} AND {_cat_node}
{adopted_block}
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name{adoption_col}
ORDER BY config_name, name
SKIP $offset LIMIT $limit
""".strip()

        elif search_by == "layout":
            cond = apply_match("l.name", "search_text", mm)
            cypher = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_LAYOUT]->(l:Layout)
WHERE {cond} AND {_cat_node}
{adopted_block}
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name{adoption_col}
ORDER BY config_name, name
SKIP $offset LIMIT $limit
""".strip()

        elif search_by == "predefined_name":
            cond = apply_match("p.`Имя`", "search_text", mm)
            cypher = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_PREDEFINED]->(p:PredefinedItem)
WHERE {cond} AND {_cat_node}
{adopted_block}
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name{adoption_col}
ORDER BY config_name, name
SKIP $offset LIMIT $limit
""".strip()

        elif search_by == "journal_graph":
            cond = apply_match("g.name", "search_text", mm)
            cypher = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_GRAPH]->(g:JournalGraph)
WHERE toLower(m.category_name) = toLower('ЖурналыДокументов') AND {cond} AND {_cat_node}
{adopted_block}
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name{adoption_col}
ORDER BY config_name, name
SKIP $offset LIMIT $limit
""".strip()

        else:
            return f"Error: unknown search_by='{search_by}'."

        # Category filter is enforced inside Cypher (before SKIP/LIMIT) via $cat_filter.
        results = _run_query(loader, cypher, params, pn)
        return _emit(results, search_by)

    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception("Error in find_metadata_objects")
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Tool 3: get_metadata_object_structure
# ---------------------------------------------------------------------------

def get_metadata_object_structure(
    object_ref: str,
    sections: Optional[List[Literal[
        "overview", "attributes", "tabular_parts", "tabular_attributes",
        "characteristics", "resources", "dimensions", "forms", "default_forms",
        "commands", "layouts", "enum_values", "journal_graphs", "predefined",
        "url_templates", "url_methods",
    ]]] = None,
    element_name: Optional[str] = None,
    element_match: Optional[Literal["exact", "starts_with", "contains"]] = None,
    tabular_part: Optional[str] = None,
    url_template: Optional[str] = None,
    form_role: Optional[Literal["object", "group", "list", "picker", "group_picker"]] = None,
    default_form_only: Optional[bool] = None,
    config: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    project_name: Optional[str] = None,
) -> str:
    """Get structure and child elements of a MetadataObject.

object_ref: short name ("Контрагенты"), "Category.Name", or full qualified_name.
sections (default ["overview"]):
  overview                          — summary: attributes/resources/dimensions/tabular_parts as nested arrays.
  attributes | tabular_parts |
  resources | dimensions |
  commands | layouts |
  enum_values | journal_graphs      — rows {name, qualified_name, config_name, owner_qn}.
  forms                             — adds {role, is_default}; filter via form_role, default_form_only.
  default_forms                     — rows {role, name, qualified_name, ...}.
  predefined                        — adds {code, description}.
  characteristics                   — same shape as attributes.
  tabular_attributes                — requires tabular_part; attributes of that part.
  url_templates                     — for HTTP services; adds {pattern}.
  url_methods                       — requires url_template; adds {httpMethod, handler}.
Use element_name with element_match to filter elements by name.
"""
    loader = _init_loader()
    if loader is None:
        return "Error: Neo4j database connection not available."
    try:
        pn = _resolve_project(project_name)
        config_name = resolve_config(loader, config, pn)
        scope = _scope(config_name)
        lim = clamp_limit(limit)
        off = clamp_offset(offset)
        mm = (element_match or "exact").lower()

        secs = list(dict.fromkeys(s.lower() for s in sections)) if sections else ["overview"]

        has_extensions = bool(_run_query(
            loader,
            "MATCH (c:Configuration {project_name: $project_name, is_extension: true}) RETURN c LIMIT 1",
            {},
            pn,
        ))

        def _fetch_sec(sec: str) -> list:
            if sec in ("url_templates", "url_methods"):
                from .resolvers import parse_category_and_name as _pcn
                _cat, _name = _pcn(object_ref)
                _pinned_ref = f"HTTPСервисы.{_name}" if not _cat else object_ref
                svc = _resolve_object_strictly(loader, _pinned_ref, pn, config_name=config_name)
                p: Dict[str, Any] = {"service": svc["name"], "limit": lim, "offset": off}
                if config_name:
                    p["config_name"] = config_name
                if sec == "url_templates":
                    _adp = _full_elem_adoption_block("t", "UrlTemplate", "s, t", parent_var="s") if has_extensions else ""
                    _adp_col = ", adoption" if has_extensions else ""
                    cypher = f"""
MATCH (:MetadataCategory {{name:'HTTPСервисы'}})-[:CONTAINS_OBJECT]->(s:MetadataObject {{name:$service, project_name:$project_name{scope.metadata_map}}})-[:HAS_URL_TEMPLATE]->(t:UrlTemplate)
{_adp}
RETURN t.name AS name, t.qualified_name AS qualified_name, s.config_name AS config_name, s.qualified_name AS owner_qn, coalesce(t.`Шаблон`, t.`pattern`, t.pattern) AS pattern{_adp_col}
ORDER BY name
SKIP $offset LIMIT $limit
""".strip()
                else:
                    if not url_template or not url_template.strip():
                        raise ValueError("url_template is required for sections=['url_methods'].")
                    p["template"] = url_template.strip()
                    _adp = _full_elem_adoption_block("m", "UrlMethod", "s, t, m", parent_var="s") if has_extensions else ""
                    _adp_col = ", adoption" if has_extensions else ""
                    cypher = f"""
MATCH (:MetadataCategory {{name:'HTTPСервисы'}})-[:CONTAINS_OBJECT]->(s:MetadataObject {{name:$service, project_name:$project_name{scope.metadata_map}}})-[:HAS_URL_TEMPLATE]->(t:UrlTemplate {{name:$template}})-[:HAS_URL_METHOD]->(m:UrlMethod)
{_adp}
RETURN m.name AS name, m.qualified_name AS qualified_name, s.config_name AS config_name, t.qualified_name AS owner_qn,
       coalesce(m.`HTTPМетод`, m.`httpMethod`) AS httpMethod, coalesce(m.`Обработчик`, m.`handler`) AS handler{_adp_col}
ORDER BY name
SKIP $offset LIMIT $limit
""".strip()
                rows = _run_query(loader, cypher, p, pn)
                if has_extensions:
                    rows = _strip_null_adoption(rows)
                return rows

            p = {"object_name": on, "category_name": cat, "limit": lim, "offset": off}
            if config_name:
                p["config_name"] = config_name

            if sec == "overview":
                if has_extensions:
                    cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})
{_ADOPTED_COLLECT}
OPTIONAL MATCH (m)-[:HAS_ATTRIBUTE]->(a:Attribute)
WITH m, adoption, collect(DISTINCT a.name) AS attributes
OPTIONAL MATCH (m)-[:HAS_RESOURCE]->(r:Resource)
WITH m, adoption, attributes, collect(DISTINCT r.name) AS resources
OPTIONAL MATCH (m)-[:HAS_DIMENSION]->(d:Dimension)
WITH m, adoption, attributes, resources, collect(DISTINCT d.name) AS dimensions
OPTIONAL MATCH (m)-[:HAS_TABULAR_PART]->(t:TabularPart)
WITH m, adoption, attributes, resources, dimensions, collect(DISTINCT t) AS tps
RETURN m.config_name AS config_name, m.qualified_name AS qualified_name, m.name AS object,
  attributes, resources, dimensions,
  [t IN tps WHERE t IS NOT NULL | {{name: t.name, attributes: [(t)-[:HAS_ATTRIBUTE]->(ta:Attribute) | ta.name]}}] AS tabularParts,
  adoption
LIMIT 1
""".strip()
                else:
                    cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})
OPTIONAL MATCH (m)-[:HAS_ATTRIBUTE]->(a:Attribute)
WITH m, collect(DISTINCT a.name) AS attributes
OPTIONAL MATCH (m)-[:HAS_RESOURCE]->(r:Resource)
WITH m, attributes, collect(DISTINCT r.name) AS resources
OPTIONAL MATCH (m)-[:HAS_DIMENSION]->(d:Dimension)
WITH m, attributes, resources, collect(DISTINCT d.name) AS dimensions
OPTIONAL MATCH (m)-[:HAS_TABULAR_PART]->(t:TabularPart)
WITH m, attributes, resources, dimensions, collect(DISTINCT t) AS tps
RETURN m.config_name AS config_name, m.qualified_name AS qualified_name, m.name AS object,
  attributes, resources, dimensions,
  [t IN tps WHERE t IS NOT NULL | {{name: t.name, attributes: [(t)-[:HAS_ATTRIBUTE]->(ta:Attribute) | ta.name]}}] AS tabularParts
LIMIT 1
""".strip()
                return _run_query(loader, cypher, p, pn)

            elif sec == "attributes":
                e_cond = f"\nWHERE {apply_match('a.name', 'element_name', mm)}" if element_name else ""
                if element_name:
                    p["element_name"] = element_name.strip()
                _adp = _full_elem_adoption_block("a", "Attribute", "m, a") if has_extensions else ""
                _adp_col = ", adoption" if has_extensions else ""
                cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[:HAS_ATTRIBUTE]->(a:Attribute)
{e_cond}
{_adp}
RETURN a.name AS name, a.qualified_name AS qualified_name, m.config_name AS config_name, m.qualified_name AS owner_qn{_adp_col}
ORDER BY name
SKIP $offset LIMIT $limit
""".strip()

            elif sec == "tabular_parts":
                e_cond = f"\nWHERE {apply_match('t.name', 'element_name', mm)}" if element_name else ""
                if element_name:
                    p["element_name"] = element_name.strip()
                _adp = _full_elem_adoption_block("t", "TabularPart", "m, t") if has_extensions else ""
                _adp_col = ", adoption" if has_extensions else ""
                cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[:HAS_TABULAR_PART]->(t:TabularPart)
{e_cond}
{_adp}
RETURN t.name AS name, t.qualified_name AS qualified_name, m.config_name AS config_name, m.qualified_name AS owner_qn{_adp_col}
ORDER BY name
SKIP $offset LIMIT $limit
""".strip()

            elif sec == "tabular_attributes":
                if not tabular_part or not tabular_part.strip():
                    raise ValueError("tabular_part is required for sections=['tabular_attributes'].")
                p["tabular"] = tabular_part.strip()
                e_cond = f"\nWHERE {apply_match('a.name', 'element_name', mm)}" if element_name else ""
                if element_name:
                    p["element_name"] = element_name.strip()
                _adp = _full_elem_adoption_block(
                    "a", "Attribute", "m, t, a",
                    parent_var="t", parent_label="TabularPart",
                ) if has_extensions else ""
                _adp_col = ", adoption" if has_extensions else ""
                cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[:HAS_TABULAR_PART]->(t:TabularPart {{name:$tabular}})
MATCH (t)-[:HAS_ATTRIBUTE]->(a:Attribute)
{e_cond}
{_adp}
RETURN a.name AS name, a.qualified_name AS qualified_name, m.config_name AS config_name, t.qualified_name AS owner_qn{_adp_col}
ORDER BY name
SKIP $offset LIMIT $limit
""".strip()

            elif sec == "characteristics":
                cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[:HAS_CHARACTERISTIC]->(s:Characteristic)
RETURN s.name AS name, s.qualified_name AS qualified_name, m.config_name AS config_name, m.qualified_name AS owner_qn
ORDER BY name
SKIP $offset LIMIT $limit
""".strip()

            elif sec == "resources":
                e_cond = f"\nWHERE {apply_match('r.name', 'element_name', mm)}" if element_name else ""
                if element_name:
                    p["element_name"] = element_name.strip()
                _adp = _full_elem_adoption_block("r", "Resource", "m, r") if has_extensions else ""
                _adp_col = ", adoption" if has_extensions else ""
                cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[:HAS_RESOURCE]->(r:Resource)
{e_cond}
{_adp}
RETURN r.name AS name, r.qualified_name AS qualified_name, m.config_name AS config_name, m.qualified_name AS owner_qn{_adp_col}
ORDER BY name
SKIP $offset LIMIT $limit
""".strip()

            elif sec == "dimensions":
                e_cond = f"\nWHERE {apply_match('d.name', 'element_name', mm)}" if element_name else ""
                if element_name:
                    p["element_name"] = element_name.strip()
                _adp = _full_elem_adoption_block("d", "Dimension", "m, d") if has_extensions else ""
                _adp_col = ", adoption" if has_extensions else ""
                cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[:HAS_DIMENSION]->(d:Dimension)
{e_cond}
{_adp}
RETURN d.name AS name, d.qualified_name AS qualified_name, m.config_name AS config_name, m.qualified_name AS owner_qn{_adp_col}
ORDER BY name
SKIP $offset LIMIT $limit
""".strip()

            elif sec == "forms":
                p["role"] = form_role if form_role in ("object", "group", "list", "picker", "group_picker") else None
                p["default_only"] = bool(default_form_only) if default_form_only is not None else False
                e_cond = f"\n  AND {apply_match('f.name', 'element_name', mm)}" if element_name else ""
                if element_name:
                    p["element_name"] = element_name.strip()
                _adp = _full_elem_adoption_block("f", "Form", "m, f, r") if has_extensions else ""
                _adp_col = ", adoption" if has_extensions else ""
                cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[r:HAS_FORM]->(f:Form)
WHERE ($role IS NULL OR r.role = $role)
  AND (coalesce($default_only, false) = false OR r.is_default = true){e_cond}
{_adp}
RETURN f.name AS name, f.qualified_name AS qualified_name, m.config_name AS config_name, m.qualified_name AS owner_qn, r.role AS role, r.is_default AS is_default{_adp_col}
ORDER BY name
SKIP $offset LIMIT $limit
""".strip()

            elif sec == "default_forms":
                cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[r:HAS_FORM {{is_default:true}}]->(f:Form)
RETURN r.role AS role, f.name AS name, f.qualified_name AS qualified_name, m.config_name AS config_name, m.qualified_name AS owner_qn
ORDER BY role
""".strip()

            elif sec == "commands":
                e_cond = f"\nWHERE {apply_match('c.name', 'element_name', mm)}" if element_name else ""
                if element_name:
                    p["element_name"] = element_name.strip()
                _adp = _full_elem_adoption_block("c", "Command", "m, c") if has_extensions else ""
                _adp_col = ", adoption" if has_extensions else ""
                cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[:HAS_COMMAND]->(c:Command)
{e_cond}
{_adp}
RETURN c.name AS name, c.qualified_name AS qualified_name, m.config_name AS config_name, m.qualified_name AS owner_qn{_adp_col}
ORDER BY name
SKIP $offset LIMIT $limit
""".strip()

            elif sec == "layouts":
                e_cond = f"\nWHERE {apply_match('l.name', 'element_name', mm)}" if element_name else ""
                if element_name:
                    p["element_name"] = element_name.strip()
                _adp = _full_elem_adoption_block("l", "Layout", "m, l") if has_extensions else ""
                _adp_col = ", adoption" if has_extensions else ""
                cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[:HAS_LAYOUT]->(l:Layout)
{e_cond}
{_adp}
RETURN l.name AS name, l.qualified_name AS qualified_name, m.config_name AS config_name, m.qualified_name AS owner_qn{_adp_col}
ORDER BY name
SKIP $offset LIMIT $limit
""".strip()

            elif sec == "enum_values":
                _adp = _full_elem_adoption_block("v", "EnumValue", "m, v") if has_extensions else ""
                _adp_col = ", adoption" if has_extensions else ""
                cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[:HAS_ENUM_VALUE]->(v:EnumValue)
{_adp}
RETURN v.name AS name, v.qualified_name AS qualified_name, m.config_name AS config_name, m.qualified_name AS owner_qn{_adp_col}
ORDER BY name
SKIP $offset LIMIT $limit
""".strip()

            elif sec == "journal_graphs":
                _adp = _full_elem_adoption_block("g", "JournalGraph", "m, g") if has_extensions else ""
                _adp_col = ", adoption" if has_extensions else ""
                cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[:HAS_GRAPH]->(g:JournalGraph)
{_adp}
RETURN g.name AS name, g.qualified_name AS qualified_name, m.config_name AS config_name, m.qualified_name AS owner_qn{_adp_col}
ORDER BY name
SKIP $offset LIMIT $limit
""".strip()

            elif sec == "predefined":
                cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[:HAS_PREDEFINED]->(p:PredefinedItem)
RETURN p.`Имя` AS name, m.config_name AS config_name, m.qualified_name AS owner_qn,
       p.qualified_name AS qualified_name, coalesce(p.`Код`,'') AS code, coalesce(p.`Наименование`,'') AS description
ORDER BY name
SKIP $offset LIMIT $limit
""".strip()

            else:
                raise ValueError(f"unknown section '{sec}'.")

            rows = _run_query(loader, cypher, p, pn)
            if has_extensions:
                rows = _strip_null_adoption(rows)
            return rows

        _url_secs = {"url_templates", "url_methods"}
        if any(s not in _url_secs for s in secs):
            resolved = resolve_object_ref(loader, object_ref, pn, config_name)
            on = resolved["name"]
            cat = resolved["category_name"]
        else:
            on = cat = ""

        if len(secs) == 1:
            return _done(_fetch_sec(secs[0]))

        combined: Dict[str, list] = {}
        for s in secs:
            combined[s] = _fetch_sec(s)
        return _fmt_dict(combined, apply_compact_refs=True)

    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception("Error in get_metadata_object_structure")
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Tool 4: find_metadata_elements
# ---------------------------------------------------------------------------

def find_metadata_elements(
    element_type: Literal[
        "attribute", "attributes_of_matching_objects", "tabular_attribute",
        "form", "form_attribute", "command", "layout", "journal_graph",
    ],
    element_name: Optional[str] = None,
    element_match: Optional[Literal["exact", "starts_with", "contains"]] = None,
    owner_object: Optional[str] = None,
    owner_object_match: Optional[Literal["exact", "starts_with", "contains"]] = None,
    tabular_part: Optional[str] = None,
    form_role: Optional[Literal["object", "group", "list", "picker", "group_picker"]] = None,
    default_form_only: Optional[bool] = None,
    config: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    project_name: Optional[str] = None,
) -> str:
    """Find child metadata elements across the project and return them with owner context.

Choose element_type:
- attribute: ordinary object attributes by element_name; element_name required.
- attributes_of_matching_objects: attributes of objects matched by owner_object; use owner_object_match.
- tabular_attribute: attributes of one tabular part; owner_object and tabular_part required; element_name optional.
- form: forms across the project; element_name optional; form_role/default_form_only narrow regular object forms.
- form_attribute: form attributes across regular and common forms; element_name optional; form_role/default_form_only narrow regular object forms.
- command/layout/journal_graph: by element_name; element_name required.

owner_object is used by attributes_of_matching_objects and tabular_attribute.

element_match controls element_name matching. config scopes to one configuration or extension.
Use get_metadata_element_type when you need attribute/resource/dimension type values.
"""
    loader = _init_loader()
    if loader is None:
        return "Error: Neo4j database connection not available."
    try:
        pn = _resolve_project(project_name)
        config_name = resolve_config(loader, config, pn)
        scope = _scope(config_name)
        lim = clamp_limit(limit)
        off = clamp_offset(offset)
        # Fetch lim + 1 so the shaper can derive page.has_more explicitly.
        params: Dict[str, Any] = {"limit": lim + 1, "offset": off}
        if config_name:
            params["config_name"] = config_name
        mm = (element_match or "exact").lower()
        omm = (owner_object_match or "exact").lower()
        has_extensions = bool(_run_query(
            loader,
            "MATCH (c:Configuration {project_name: $project_name, is_extension: true}) RETURN c LIMIT 1",
            {},
            pn,
        ))
        _adp_col = ", adoption" if has_extensions else ""

        if element_type == "attribute":
            if not element_name or not element_name.strip():
                return "Error: element_name is required for element_type='attribute'."
            params["name"] = element_name.strip()
            cond = apply_match("a.name", "name", mm)
            _adp = _owner_adoption_block("a") if has_extensions else ""
            cypher = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_ATTRIBUTE]->(a:Attribute)
WHERE {cond}
{_adp}
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS object,
  m.qualified_name AS owner_qn, a.name AS name, a.qualified_name AS qualified_name{_adp_col}
ORDER BY config_name, object, name
SKIP $offset LIMIT $limit
""".strip()

        elif element_type == "attributes_of_matching_objects":
            if not owner_object or not owner_object.strip():
                return "Error: owner_object is required for element_type='attributes_of_matching_objects'."
            params["object"] = owner_object.strip()
            o_cond = apply_match("m.name", "object", omm)
            _adp = _owner_adoption_block("a") if has_extensions else ""
            cypher = f"""
MATCH (m:MetadataObject {scope.map_for()})
WHERE {o_cond}
MATCH (m)-[:HAS_ATTRIBUTE]->(a:Attribute)
{_adp}
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS object,
  m.qualified_name AS owner_qn, a.name AS name, a.qualified_name AS qualified_name{_adp_col}
ORDER BY config_name, object, name
SKIP $offset LIMIT $limit
""".strip()

        elif element_type == "tabular_attribute":
            if not owner_object or not owner_object.strip():
                return "Error: owner_object is required for element_type='tabular_attribute'."
            if not tabular_part or not tabular_part.strip():
                return "Error: tabular_part is required for element_type='tabular_attribute'."
            resolved = resolve_object_ref(loader, owner_object, pn, config_name)
            params["object_name"] = resolved["name"]
            params["category_name"] = resolved["category_name"]
            params["tabular"] = tabular_part.strip()
            t_mm = (tabular_part_match if tabular_part_match else "exact").lower() if hasattr(locals(), "tabular_part_match") else "exact"
            e_cond = f"\nWHERE {apply_match('a.name', 'name', mm)}" if element_name else ""
            if element_name:
                params["name"] = element_name.strip()
            _adp = _owner_adoption_block("a") if has_extensions else ""
            cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[:HAS_TABULAR_PART]->(t:TabularPart {{name:$tabular}})
MATCH (t)-[:HAS_ATTRIBUTE]->(a:Attribute)
{e_cond}
{_adp}
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS object,
  m.qualified_name AS owner_qn, a.name AS name, a.qualified_name AS qualified_name{_adp_col}
ORDER BY config_name, object, name
SKIP $offset LIMIT $limit
""".strip()

        elif element_type == "form":
            params["role"] = form_role if form_role in ("object", "group", "list", "picker", "group_picker") else None
            params["default_only"] = bool(default_form_only) if default_form_only is not None else False
            e_cond_parts = [f"{apply_match('f.name', 'name', mm)}"] if element_name else []
            if element_name:
                params["name"] = element_name.strip()
            name_where = f"AND {e_cond_parts[0]}" if e_cond_parts else ""
            _adp = _owner_adoption_block("r, f") if has_extensions else ""
            cypher = f"""
MATCH (m:MetadataObject {scope.map_for()})-[r:HAS_FORM]->(f:Form)
WHERE ($role IS NULL OR r.role = $role)
  AND (coalesce($default_only, false) = false OR r.is_default = true) {name_where}
{_adp}
RETURN f.name AS name, f.qualified_name AS qualified_name, m.config_name AS config_name,
  m.qualified_name AS owner_qn, m.name AS object, r.role AS role, r.is_default AS is_default{_adp_col}
ORDER BY object, name
SKIP $offset LIMIT $limit
""".strip()

        elif element_type == "form_attribute":
            params["role"] = form_role if form_role in ("object", "group", "list", "picker", "group_picker") else None
            name_where = f"AND {apply_match('fa.name', 'name', mm)}" if element_name else ""
            if element_name:
                params["name"] = element_name.strip()
            _adp = _owner_adoption_block("r, f, fa") if has_extensions else ""
            _adp_cf = _owner_adoption_block("fa") if has_extensions else ""
            cf_map = scope.map_for("category_name: 'ОбщиеФормы'")
            cypher = f"""
CALL {{
  MATCH (m:MetadataObject {scope.map_for()})-[r:HAS_FORM]->(f:Form)-[:HAS_FORM_ATTRIBUTE]->(fa:FormAttribute)
  WHERE ($role IS NULL OR r.role = $role) {name_where}
  {_adp}
  RETURN fa.name AS name, fa.qualified_name AS qualified_name, m.config_name AS config_name,
    f.qualified_name AS owner_qn, m.name AS object, m.qualified_name AS object_qn{_adp_col}
  UNION
  MATCH (m:MetadataObject {cf_map})-[:HAS_FORM_ATTRIBUTE]->(fa:FormAttribute)
  WHERE true {name_where}
  {_adp_cf}
  RETURN fa.name AS name, fa.qualified_name AS qualified_name, m.config_name AS config_name,
    m.qualified_name AS owner_qn, m.name AS object, m.qualified_name AS object_qn{_adp_col}
}}
RETURN *
ORDER BY object, name
SKIP $offset LIMIT $limit
""".strip()

        elif element_type == "command":
            if not element_name or not element_name.strip():
                return "Error: element_name is required for element_type='command'."
            params["name"] = element_name.strip()
            cond = apply_match("c.name", "name", mm)
            _adp = _owner_adoption_block("c") if has_extensions else ""
            cypher = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_COMMAND]->(c:Command)
WHERE {cond}
{_adp}
RETURN c.name AS name, c.qualified_name AS qualified_name, m.config_name AS config_name,
  m.qualified_name AS owner_qn, m.name AS object{_adp_col}
ORDER BY object, name
SKIP $offset LIMIT $limit
""".strip()

        elif element_type == "layout":
            if not element_name or not element_name.strip():
                return "Error: element_name is required for element_type='layout'."
            params["name"] = element_name.strip()
            cond = apply_match("l.name", "name", mm)
            _adp = _owner_adoption_block("l") if has_extensions else ""
            cypher = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_LAYOUT]->(l:Layout)
WHERE {cond}
{_adp}
RETURN l.name AS name, l.qualified_name AS qualified_name, m.config_name AS config_name,
  m.qualified_name AS owner_qn, m.name AS object{_adp_col}
ORDER BY object, name
SKIP $offset LIMIT $limit
""".strip()

        elif element_type == "journal_graph":
            if not element_name or not element_name.strip():
                return "Error: element_name is required for element_type='journal_graph'."
            params["name"] = element_name.strip()
            cond = apply_match("g.name", "name", mm)
            _adp = _owner_adoption_block("g") if has_extensions else ""
            cypher = f"""
MATCH (:MetadataCategory {{name:'ЖурналыДокументов'}})-[:CONTAINS_OBJECT]->(m:MetadataObject {scope.map_for()})-[:HAS_GRAPH]->(g:JournalGraph)
WHERE {cond}
{_adp}
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS object,
  m.qualified_name AS owner_qn, g.name AS name, g.qualified_name AS qualified_name{_adp_col}
ORDER BY config_name, object, name
SKIP $offset LIMIT $limit
""".strip()

        else:
            return f"Error: unknown element_type='{element_type}'."

        results = _run_query(loader, cypher, params, pn)
        shaped = _shape_find_metadata_elements_result(results, lim=lim, off=off)
        return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception("Error in find_metadata_elements")
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Tool 5: find_metadata_usages
# ---------------------------------------------------------------------------

def find_metadata_usages(
    mode: Literal["objects", "paths", "register_movements"],
    target_ref: str,
    target_category: Optional[str] = None,
    result_category: Optional[str] = None,
    include_tabular: Optional[bool] = None,
    target_match: Optional[Literal["exact", "starts_with", "contains"]] = None,
    config: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    project_name: Optional[str] = None,
) -> str:
    """Find metadata object usages or documents that write movements to registers.

Modes:
- objects: find objects whose typed fields reference target_ref. target_ref is a short
  object name/pattern, not qualified_name. target_category is optional but recommended:
  it builds the 1C type name, e.g. Справочники + Организации -> СправочникСсылка.Организации.
  include_tabular defaults true; false excludes tabular-part attributes.
- paths: find 1C-style field paths where target_ref is used. target_category filters
  the target category; result_category filters the consumer category.
- register_movements: find documents that write movements to a register. target_ref is
  a short register name/pattern. target_category filters РегистрыСведений or
  РегистрыНакопления; omit to search both.

target_match controls exact/starts_with/contains.
CommonAttribute usages are currently not indexed.
"""
    loader = _init_loader()
    if loader is None:
        return "Error: Neo4j database connection not available."
    try:
        pn = _resolve_project(project_name)
        config_name = resolve_config(loader, config, pn)
        scope = _scope(config_name)
        lim = clamp_limit(limit)
        off = clamp_offset(offset)
        has_extensions = bool(_run_query(
            loader,
            "MATCH (c:Configuration {project_name: $project_name, is_extension: true}) RETURN c LIMIT 1",
            {},
            pn,
        ))
        adoption_col = ", adoption" if has_extensions else ""
        mm = (target_match or ("contains" if mode == "objects" else "exact")).lower()
        params: Dict[str, Any] = {"limit": lim + 1, "offset": off, "target": target_ref.strip()}
        if config_name:
            params["config_name"] = config_name

        if mode == "objects":
            prefix_map = {
                "Справочники": "СправочникСсылка.", "Документы": "ДокументСсылка.",
                "Перечисления": "ПеречислениеСсылка.", "ПланыСчетов": "ПланСчетовСсылка.",
                "ПланыВидовХарактеристик": "ПланВидовХарактеристикСсылка.",
                "ПланыВидовРасчета": "ПланВидовРасчетаСсылка.",
                "ПланыВидовРасчёта": "ПланВидовРасчетаСсылка.",
                "БизнесПроцессы": "БизнесПроцессСсылка.", "Задачи": "ЗадачаСсылка.",
            }
            prefix = prefix_map.get(target_category or "", "")
            needle = f"{prefix}{target_ref.strip()}" if prefix else target_ref.strip()
            params["needle"] = needle
            if mm == "exact":
                cond = "any(v IN coalesce(props[k], []) WHERE toLower(toString(v)) = toLower($needle))"
            elif mm == "starts_with":
                cond = "any(v IN coalesce(props[k], []) WHERE toLower(toString(v)) STARTS WITH toLower($needle))"
            else:
                cond = "any(v IN coalesce(props[k], []) WHERE toLower(toString(v)) CONTAINS toLower($needle))"
            in_cat = result_category or None
            params["in_category"] = in_cat
            inc_tab = include_tabular if include_tabular is not None else True
            part1 = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_ATTRIBUTE]->(a:Attribute)
WITH m, properties(a) AS props, ['Тип','type','ValueType','ТипЗначения'] AS keys
WHERE any(k IN keys WHERE props[k] IS NOT NULL AND {cond})
  AND ($in_category IS NULL OR toLower(m.category_name) = toLower($in_category))
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name, m AS _m
""".strip()
            parts = [part1]
            if inc_tab:
                part2 = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_TABULAR_PART]->(t:TabularPart)-[:HAS_ATTRIBUTE]->(a:Attribute)
WITH m, properties(a) AS props, ['Тип','type','ValueType','ТипЗначения'] AS keys
WHERE any(k IN keys WHERE props[k] IS NOT NULL AND {cond})
  AND ($in_category IS NULL OR toLower(m.category_name) = toLower($in_category))
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name, m AS _m
""".strip()
                parts.append(part2)
            part3 = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_RESOURCE]->(x:Resource)
WITH m, properties(x) AS props, ['Тип','type','ValueType','ТипЗначения'] AS keys
WHERE any(k IN keys WHERE props[k] IS NOT NULL AND {cond})
  AND ($in_category IS NULL OR toLower(m.category_name) = toLower($in_category))
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name, m AS _m
""".strip()
            part4 = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_DIMENSION]->(x:Dimension)
WITH m, properties(x) AS props, ['Тип','type','ValueType','ТипЗначения'] AS keys
WHERE any(k IN keys WHERE props[k] IS NOT NULL AND {cond})
  AND ($in_category IS NULL OR toLower(m.category_name) = toLower($in_category))
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name, m AS _m
""".strip()
            part5 = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_ACCOUNTING_FLAG]->(x:AccountingFlag)
WITH m, properties(x) AS props, ['Тип','type','ValueType','ТипЗначения'] AS keys
WHERE any(k IN keys WHERE props[k] IS NOT NULL AND {cond})
  AND ($in_category IS NULL OR toLower(m.category_name) = toLower($in_category))
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name, m AS _m
""".strip()
            part6 = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_DIMENSION_ACCOUNTING_FLAG]->(x:DimensionAccountingFlag)
WITH m, properties(x) AS props, ['Тип','type','ValueType','ТипЗначения'] AS keys
WHERE any(k IN keys WHERE props[k] IS NOT NULL AND {cond})
  AND ($in_category IS NULL OR toLower(m.category_name) = toLower($in_category))
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name, m AS _m
""".strip()
            part7a = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_FORM]->(f:Form)-[:HAS_FORM_ATTRIBUTE]->(fa:FormAttribute)
WITH m, properties(fa) AS props, ['Тип','type','ValueType','ТипЗначения'] AS keys
WHERE any(k IN keys WHERE props[k] IS NOT NULL AND {cond})
  AND ($in_category IS NULL OR toLower(m.category_name) = toLower($in_category))
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name, m AS _m
""".strip()
            part7b = f"""
MATCH (m:MetadataObject {scope.map_for("category_name: 'ОбщиеФормы'")})-[:HAS_FORM_ATTRIBUTE]->(fa:FormAttribute)
WITH m, properties(fa) AS props, ['Тип','type','ValueType','ТипЗначения'] AS keys
WHERE any(k IN keys WHERE props[k] IS NOT NULL AND {cond})
  AND ($in_category IS NULL OR toLower(m.category_name) = toLower($in_category))
RETURN DISTINCT m.config_name AS config_name, m.category_name AS category, m.name AS name, m.qualified_name AS qualified_name, m AS _m
""".strip()
            parts.extend([part3, part4, part5, part6, part7a, part7b])
            union_body = "\nUNION\n".join(parts)
            _adp = _owner_adoption_block(
                carry_vars="config_name, category, name, qualified_name",
                parent_var="_m",
            ) if has_extensions else ""
            # Enrich with owner Configuration QN / is_extension for grouped shaping.
            # In this mode the CALL projects (and the adoption block carries) the
            # column `config_name`, so it is bound here and can drive the MATCH.
            enrich = "MATCH (ucfg:Configuration {project_name: $project_name, name: config_name})"
            cypher = (
                f"CALL {{\n{union_body}\n}}\n"
                f"{_adp}\n"
                f"{enrich}\n"
                f"RETURN config_name, category, name, qualified_name, "
                f"ucfg.qualified_name AS config_qn, coalesce(ucfg.is_extension, false) AS is_extension{adoption_col}\n"
                f"ORDER BY config_name, name\nSKIP $offset LIMIT $limit"
            ).strip()

        elif mode == "paths":
            in_cat = result_category or None
            tc = target_category or None
            params["in_category"] = in_cat
            params["target_category"] = tc
            t_cond = apply_match("target.name", "target", mm)
            cat_filter = f" AND ($in_category IS NULL OR toLower(m.category_name)=toLower($in_category)) AND m.project_name = $project_name{scope.and_alias('m')}"
            target_cat_filter = f" AND ($target_category IS NULL OR toLower(target.category_name)=toLower($target_category)) AND target.project_name = $project_name{scope.and_alias('target')}"
            cypher = f"""
CALL {{
MATCH (target:MetadataObject)
WHERE {t_cond}{target_cat_filter}
MATCH (target)-[:USED_IN]->(a:Attribute)<-[:HAS_ATTRIBUTE]-(m:MetadataObject)
WHERE true{cat_filter}
RETURN DISTINCT target.config_name AS target_config_name, target.qualified_name AS target_qn, m.config_name AS config_name, m.category_name + '.' + m.name + '.Реквизиты.' + a.name AS path
UNION
MATCH (target:MetadataObject)
WHERE {t_cond}{target_cat_filter}
MATCH (target)-[:USED_IN]->(a:Attribute)<-[:HAS_ATTRIBUTE]-(tp:TabularPart)<-[:HAS_TABULAR_PART]-(m:MetadataObject)
WHERE true{cat_filter}
RETURN DISTINCT target.config_name AS target_config_name, target.qualified_name AS target_qn, m.config_name AS config_name, m.category_name + '.' + m.name + '.ТабличныеЧасти.' + tp.name + '.Реквизиты.' + a.name AS path
UNION
MATCH (target:MetadataObject)
WHERE {t_cond}{target_cat_filter}
MATCH (target)-[:USED_IN]->(r:Resource)<-[:HAS_RESOURCE]-(m:MetadataObject)
WHERE true{cat_filter}
RETURN DISTINCT target.config_name AS target_config_name, target.qualified_name AS target_qn, m.config_name AS config_name, m.category_name + '.' + m.name + '.Ресурсы.' + r.name AS path
UNION
MATCH (target:MetadataObject)
WHERE {t_cond}{target_cat_filter}
MATCH (target)-[:USED_IN]->(d:Dimension)<-[:HAS_DIMENSION]-(m:MetadataObject)
WHERE true{cat_filter}
RETURN DISTINCT target.config_name AS target_config_name, target.qualified_name AS target_qn, m.config_name AS config_name, m.category_name + '.' + m.name + '.Измерения.' + d.name AS path
UNION
MATCH (target:MetadataObject)
WHERE {t_cond}{target_cat_filter}
MATCH (target)-[:USED_IN]->(af:AccountingFlag)<-[:HAS_ACCOUNTING_FLAG]-(m:MetadataObject)
WHERE true{cat_filter}
RETURN DISTINCT target.config_name AS target_config_name, target.qualified_name AS target_qn, m.config_name AS config_name, m.category_name + '.' + m.name + '.ПризнакиУчета.' + af.name AS path
UNION
MATCH (target:MetadataObject)
WHERE {t_cond}{target_cat_filter}
MATCH (target)-[:USED_IN]->(sf:DimensionAccountingFlag)<-[:HAS_DIMENSION_ACCOUNTING_FLAG]-(m:MetadataObject)
WHERE true{cat_filter}
RETURN DISTINCT target.config_name AS target_config_name, target.qualified_name AS target_qn, m.config_name AS config_name, m.category_name + '.' + m.name + '.ПризнакиУчетаСубконто.' + sf.name AS path
UNION
MATCH (target:MetadataObject)
WHERE {t_cond}{target_cat_filter}
MATCH (target)-[:USED_IN]->(fa:FormAttribute)<-[:HAS_FORM_ATTRIBUTE]-(f:Form)<-[:HAS_FORM]-(m:MetadataObject)
WHERE true{cat_filter}
RETURN DISTINCT target.config_name AS target_config_name, target.qualified_name AS target_qn, m.config_name AS config_name, m.category_name + '.' + m.name + '.Формы.' + f.name + '.Реквизиты.' + fa.name AS path
UNION
MATCH (target:MetadataObject)
WHERE {t_cond}{target_cat_filter}
MATCH (target)-[:USED_IN]->(fa:FormAttribute)<-[:HAS_FORM_ATTRIBUTE]-(m:MetadataObject {{category_name:'ОбщиеФормы'}})
WHERE true{cat_filter}
RETURN DISTINCT target.config_name AS target_config_name, target.qualified_name AS target_qn, m.config_name AS config_name, 'ОбщиеФормы.' + m.name + '.Реквизиты.' + fa.name AS path
}}
RETURN *
ORDER BY target_config_name, target_qn, config_name, path
SKIP $offset LIMIT $limit
""".strip()

        elif mode == "register_movements":
            reg_cat = target_category or None
            params["register_category"] = reg_cat
            name_cond = apply_match("r.name", "target", mm)
            _adp = _owner_adoption_block(parent_var="d") if has_extensions else ""
            # Enrich with owner Configuration QN / is_extension for grouped shaping.
            # Here `config_name` is not a bound variable before RETURN, but the
            # document node `d` is in scope both after the bare MATCH and after the
            # adoption block (carry_vars=""), so drive the MATCH from d.config_name.
            enrich = "MATCH (ucfg:Configuration {project_name: $project_name, name: d.config_name})"
            cypher = f"""
MATCH (r:MetadataObject {scope.map_for()})
WHERE ({name_cond})
  AND (
        ($register_category IS NULL AND r.category_name IN ['РегистрыСведений','РегистрыНакопления'])
     OR (toLower(r.category_name) = toLower($register_category))
  )
MATCH (d:MetadataObject {scope.map_for("category_name:'Документы'")})-[:DO_MOVEMENTS_IN]->(r)
{_adp}
{enrich}
RETURN DISTINCT d.config_name AS config_name, d.category_name AS category, d.name AS name, d.qualified_name AS qualified_name, ucfg.qualified_name AS config_qn, coalesce(ucfg.is_extension, false) AS is_extension{adoption_col}
ORDER BY config_name, name
SKIP $offset LIMIT $limit
""".strip()

        else:
            return f"Error: unknown mode='{mode}'."

        results = _run_query(loader, cypher, params, pn)
        shaped = _shape_find_metadata_usages_result(mode, results, lim=lim, off=off)
        return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception("Error in find_metadata_usages")
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Tool 6: get_metadata_element_type
# ---------------------------------------------------------------------------

_ELEMENT_TYPE_ORDER: Tuple[str, ...] = (
    "attribute",
    "addressing_attribute",
    "tabular_attribute",
    "resource",
    "dimension",
    "accounting_flag",
    "dimension_accounting_flag",
    "form_attribute",
)

_DEFAULT_ELEMENT_TYPES: Tuple[str, ...] = tuple(
    k for k in _ELEMENT_TYPE_ORDER if k != "form_attribute"
)

_TYPE_COALESCE = "coalesce(x.`Тип`, x.`type`, x.`ValueType`, x.`ТипЗначения`)"


def get_metadata_element_type(
    object_ref: str,
    element_type: Optional[Union[
        Literal[
            "attribute", "addressing_attribute", "tabular_attribute",
            "resource", "dimension", "accounting_flag",
            "dimension_accounting_flag", "form_attribute",
        ],
        List[Literal[
            "attribute", "addressing_attribute", "tabular_attribute",
            "resource", "dimension", "accounting_flag",
            "dimension_accounting_flag", "form_attribute",
        ]],
    ]] = None,
    element_name: Optional[str] = None,
    container_ref: Optional[str] = None,
    element_match: Optional[Literal["exact", "starts_with", "contains"]] = None,
    container_match: Optional[Literal["exact", "starts_with", "contains"]] = None,
    config: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    project_name: Optional[str] = None,
) -> str:
    """Возвращает типы (Тип) типизированных дочерних элементов объекта метаданных одним вызовом.

Когда звать:
  Нужны типы реквизитов / реквизитов ТЧ / ресурсов / измерений / признаков учёта /
  реквизитов адресации задач / реквизитов форм. Если нужен только inventory без
  типов — звать get_metadata_object_structure. Общий обзор объекта — inspect_metadata_object.

object_ref: короткое имя ("Контрагенты"), "Категория.Имя" или полный qualified_name.

element_type: одно значение или массив. По умолчанию = все категории кроме form_attribute.
  attribute                  — обычные реквизиты (без признака адресации).
  addressing_attribute       — реквизиты адресации (Задачи).
  tabular_attribute          — реквизиты табличных частей.
  resource                   — ресурсы регистров.
  dimension                  — измерения регистров.
  accounting_flag            — признаки учёта (План счетов).
  dimension_accounting_flag  — признаки учёта субконто (План счетов).
  form_attribute             — реквизиты форм (обычные + ОбщиеФормы). В дефолт не включён.

element_name + element_match ("exact"|"starts_with"|"contains"): глобальный фильтр по имени
  элемента, применяется ко всем выбранным категориям.

container_ref + container_match: применяется только к tabular_attribute и form_attribute
  (имя ТЧ или формы). Если не задан — обходятся все ТЧ / формы объекта.

limit / offset: best-effort сокращение объёма выдачи. Не предназначены для надёжного полного
  обхода — состав страниц зависит от набора выбранных категорий.
"""
    loader = _init_loader()
    if loader is None:
        return "Error: Neo4j database connection not available."
    try:
        pn = _resolve_project(project_name)
        config_name = resolve_config(loader, config, pn)
        scope = _scope(config_name)
        lim = clamp_limit(limit)
        off = clamp_offset(offset)
        resolved = resolve_object_ref(loader, object_ref, pn, config_name)
        on = resolved["name"]
        cat = resolved["category_name"]
        mm = (element_match or "exact").lower()
        cmm = (container_match or "exact").lower()

        # Normalize element_type — accept str, list, or None.
        if element_type is None:
            kinds = list(_DEFAULT_ELEMENT_TYPES)
        elif isinstance(element_type, str):
            kinds = [element_type]
        else:
            kinds = list(element_type) if element_type else list(_DEFAULT_ELEMENT_TYPES)
        # Deduplicate but preserve canonical order.
        kinds_set = {k for k in kinds}
        unknown = [k for k in kinds_set if k not in _ELEMENT_TYPE_ORDER]
        if unknown:
            return f"Error: unknown element_type value(s): {sorted(unknown)}. Allowed: {list(_ELEMENT_TYPE_ORDER)}."
        kinds_ordered = [k for k in _ELEMENT_TYPE_ORDER if k in kinds_set]

        has_extensions = bool(_run_query(
            loader,
            "MATCH (c:Configuration {project_name: $project_name, is_extension: true}) RETURN c LIMIT 1",
            {},
            pn,
        ))

        # Per-query cap: each Cypher fetches up to query_max_results items (system-wide
        # absolute cap), the global offset/limit is applied later in Python over the
        # concatenated ordered stream. Using query_default_limit here would cut data
        # before the global slice; query_max_results gives the actual ceiling.
        per_query_lim = int(settings.query_max_results)

        flat_rows: List[Dict[str, Any]] = []
        for kind in kinds_ordered:
            params: Dict[str, Any] = {
                "object_name": on,
                "category_name": cat,
                "limit": per_query_lim,
                "offset": 0,
            }
            if config_name:
                params["config_name"] = config_name
            if element_name:
                params["element_name"] = element_name.strip()
            elem_cond = (
                f"\nWHERE {apply_match('x.name', 'element_name', mm)}"
                if element_name else ""
            )

            cypher: str
            if kind == "attribute":
                _adp = _full_elem_adoption_block("x", "Attribute", "m, x") if has_extensions else ""
                _adp_col = ", adoption" if has_extensions else ""
                cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[:HAS_ATTRIBUTE]->(x:Attribute)
WHERE coalesce(x.`ЭтоРеквизитАдресации`, false) = false{(' AND ' + apply_match('x.name', 'element_name', mm)) if element_name else ''}
{_adp}
RETURN x.name AS name, x.qualified_name AS qualified_name,
       m.config_name AS config_name, m.qualified_name AS owner_qn,
       {_TYPE_COALESCE} AS type{_adp_col}
ORDER BY name
SKIP $offset LIMIT $limit
""".strip()
                rows = _run_query(loader, cypher, params, pn)
                for r in rows:
                    r["_kind"] = kind
                if has_extensions:
                    rows = _strip_null_adoption(rows)
                flat_rows.extend(rows)

            elif kind == "addressing_attribute":
                _adp = _full_elem_adoption_block("x", "Attribute", "m, x") if has_extensions else ""
                _adp_col = ", adoption" if has_extensions else ""
                cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[:HAS_ATTRIBUTE]->(x:Attribute)
WHERE x.`ЭтоРеквизитАдресации` = true{(' AND ' + apply_match('x.name', 'element_name', mm)) if element_name else ''}
{_adp}
RETURN x.name AS name, x.qualified_name AS qualified_name,
       m.config_name AS config_name, m.qualified_name AS owner_qn,
       {_TYPE_COALESCE} AS type{_adp_col}
ORDER BY name
SKIP $offset LIMIT $limit
""".strip()
                rows = _run_query(loader, cypher, params, pn)
                for r in rows:
                    r["_kind"] = kind
                if has_extensions:
                    rows = _strip_null_adoption(rows)
                flat_rows.extend(rows)

            elif kind == "tabular_attribute":
                if container_ref and container_ref.strip():
                    params["tabular"] = container_ref.strip()
                    t_cond = f"WHERE {apply_match('t.name', 'tabular', cmm)}"
                else:
                    t_cond = ""
                _adp = _full_elem_adoption_block(
                    "x", "Attribute", "m, t, x",
                    parent_var="t", parent_label="TabularPart",
                ) if has_extensions else ""
                _adp_col = ", adoption" if has_extensions else ""
                cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[:HAS_TABULAR_PART]->(t:TabularPart)
{t_cond}
MATCH (t)-[:HAS_ATTRIBUTE]->(x:Attribute)
{elem_cond}
{_adp}
RETURN x.name AS name, x.qualified_name AS qualified_name,
       m.config_name AS config_name, t.qualified_name AS owner_qn,
       t.name AS _container,
       {_TYPE_COALESCE} AS type{_adp_col}
ORDER BY _container, name
SKIP $offset LIMIT $limit
""".strip()
                rows = _run_query(loader, cypher, params, pn)
                for r in rows:
                    r["_kind"] = kind
                if has_extensions:
                    rows = _strip_null_adoption(rows)
                flat_rows.extend(rows)

            elif kind == "resource":
                _adp = _full_elem_adoption_block("x", "Resource", "m, x") if has_extensions else ""
                _adp_col = ", adoption" if has_extensions else ""
                cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[:HAS_RESOURCE]->(x:Resource)
{elem_cond}
{_adp}
RETURN x.name AS name, x.qualified_name AS qualified_name,
       m.config_name AS config_name, m.qualified_name AS owner_qn,
       {_TYPE_COALESCE} AS type{_adp_col}
ORDER BY name
SKIP $offset LIMIT $limit
""".strip()
                rows = _run_query(loader, cypher, params, pn)
                for r in rows:
                    r["_kind"] = kind
                if has_extensions:
                    rows = _strip_null_adoption(rows)
                flat_rows.extend(rows)

            elif kind == "dimension":
                _adp = _full_elem_adoption_block("x", "Dimension", "m, x") if has_extensions else ""
                _adp_col = ", adoption" if has_extensions else ""
                cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[:HAS_DIMENSION]->(x:Dimension)
{elem_cond}
{_adp}
RETURN x.name AS name, x.qualified_name AS qualified_name,
       m.config_name AS config_name, m.qualified_name AS owner_qn,
       {_TYPE_COALESCE} AS type{_adp_col}
ORDER BY name
SKIP $offset LIMIT $limit
""".strip()
                rows = _run_query(loader, cypher, params, pn)
                for r in rows:
                    r["_kind"] = kind
                if has_extensions:
                    rows = _strip_null_adoption(rows)
                flat_rows.extend(rows)

            elif kind == "accounting_flag":
                _adp = _full_elem_adoption_block("x", "AccountingFlag", "m, x") if has_extensions else ""
                _adp_col = ", adoption" if has_extensions else ""
                cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[:HAS_ACCOUNTING_FLAG]->(x:AccountingFlag)
{elem_cond}
{_adp}
RETURN x.name AS name, x.qualified_name AS qualified_name,
       m.config_name AS config_name, m.qualified_name AS owner_qn,
       {_TYPE_COALESCE} AS type{_adp_col}
ORDER BY name
SKIP $offset LIMIT $limit
""".strip()
                rows = _run_query(loader, cypher, params, pn)
                for r in rows:
                    r["_kind"] = kind
                if has_extensions:
                    rows = _strip_null_adoption(rows)
                flat_rows.extend(rows)

            elif kind == "dimension_accounting_flag":
                _adp = _full_elem_adoption_block("x", "DimensionAccountingFlag", "m, x") if has_extensions else ""
                _adp_col = ", adoption" if has_extensions else ""
                cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[:HAS_DIMENSION_ACCOUNTING_FLAG]->(x:DimensionAccountingFlag)
{elem_cond}
{_adp}
RETURN x.name AS name, x.qualified_name AS qualified_name,
       m.config_name AS config_name, m.qualified_name AS owner_qn,
       {_TYPE_COALESCE} AS type{_adp_col}
ORDER BY name
SKIP $offset LIMIT $limit
""".strip()
                rows = _run_query(loader, cypher, params, pn)
                for r in rows:
                    r["_kind"] = kind
                if has_extensions:
                    rows = _strip_null_adoption(rows)
                flat_rows.extend(rows)

            elif kind == "form_attribute":
                if has_extensions:
                    params["project_prefix"] = pn + "/"
                is_common_form = cat == "ОбщиеФормы"
                if container_ref and container_ref.strip():
                    params["form"] = container_ref.strip()
                if is_common_form:
                    # CommonForms: HAS_FORM_ATTRIBUTE directly on the MetadataObject.
                    # Both container (m.name) and element (x.name) filters apply after
                    # a single MATCH — combine them into one WHERE with AND.
                    where_atoms: List[str] = []
                    if container_ref and container_ref.strip():
                        where_atoms.append(apply_match('m.name', 'form', cmm))
                    if element_name:
                        where_atoms.append(apply_match('x.name', 'element_name', mm))
                    where_clause = (
                        "WHERE " + " AND ".join(where_atoms) if where_atoms else ""
                    )
                    _adp = _cf_child_adoption_block("x", "FormAttribute", "m, x") if has_extensions else ""
                    _adp_col = ", adoption" if has_extensions else ""
                    cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[:HAS_FORM_ATTRIBUTE]->(x:FormAttribute)
{where_clause}
{_adp}
RETURN coalesce(x.name,'') AS name, x.qualified_name AS qualified_name,
       m.config_name AS config_name, m.qualified_name AS owner_qn,
       m.name AS _container,
       {_TYPE_COALESCE} AS type{_adp_col}
ORDER BY _container, name
SKIP $offset LIMIT $limit
""".strip()
                else:
                    # Regular forms: MetadataObject -> HAS_FORM -> Form -> HAS_FORM_ATTRIBUTE.
                    f_cond_clause = ""
                    if container_ref and container_ref.strip():
                        f_cond_clause = f"WHERE {apply_match('f.name', 'form', cmm)}"
                    _adp = _form_child_adoption_block("x", "FormAttribute", "m, f, x") if has_extensions else ""
                    _adp_col = ", adoption" if has_extensions else ""
                    cypher = f"""
MATCH (m:MetadataObject {{name:$object_name, category_name:$category_name, project_name:$project_name{scope.metadata_map}}})-[:HAS_FORM]->(f:Form)
{f_cond_clause}
MATCH (f)-[:HAS_FORM_ATTRIBUTE]->(x:FormAttribute)
{elem_cond}
{_adp}
RETURN coalesce(x.name,'') AS name, x.qualified_name AS qualified_name,
       m.config_name AS config_name, f.qualified_name AS owner_qn,
       f.name AS _container,
       {_TYPE_COALESCE} AS type{_adp_col}
ORDER BY _container, name
SKIP $offset LIMIT $limit
""".strip()
                rows = _run_query(loader, cypher, params, pn)
                for r in rows:
                    r["_kind"] = kind
                if has_extensions:
                    rows = _strip_null_adoption(rows)
                flat_rows.extend(rows)

        # Normalize `type`: array → scalar string (single atom or "|"-joined atoms).
        for r in flat_rows:
            r["type"] = normalize_type_for_display(r.get("type"))

        # Global pagination (best-effort) over the flat ordered stream.
        sliced = flat_rows[off: off + lim] if lim else flat_rows[off:]

        # Group into output structure.
        output: Dict[str, Any] = {
            "overview": {
                "object": on,
                "qualified_name": resolved.get("qualified_name", ""),
            }
        }
        if config_name:
            output["overview"]["config"] = config_name

        grouped_nested: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
            "tabular_attribute": {},
            "form_attribute": {},
        }
        grouped_flat: Dict[str, List[Dict[str, Any]]] = {}
        for r in sliced:
            kind = r.pop("_kind", None)
            container = r.pop("_container", None)
            if kind in ("tabular_attribute", "form_attribute"):
                key = container or ""
                grouped_nested[kind].setdefault(key, []).append(r)
            else:
                grouped_flat.setdefault(kind, []).append(r)

        # Emit in canonical category order. Empty categories are skipped.
        for kind in _ELEMENT_TYPE_ORDER:
            if kind in ("tabular_attribute", "form_attribute"):
                nested = grouped_nested.get(kind) or {}
                if not nested:
                    continue
                out_key = "tabular_attributes" if kind == "tabular_attribute" else "form_attributes"
                output[out_key] = {
                    container: nested[container]
                    for container in sorted(nested.keys())
                }
            else:
                rows = grouped_flat.get(kind) or []
                if not rows:
                    continue
                output[kind] = rows

        return _fmt_dict(output, apply_compact_refs=True, compact_types=True)

    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception("Error in get_metadata_element_type")
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Tool 7: find_predefined_values
# ---------------------------------------------------------------------------

def find_predefined_values(
    mode: Literal["name", "flag", "account_type", "subconto_type"],
    owner_object: Optional[str] = None,
    criterion: Optional[str] = None,
    criterion_match: Optional[Literal["exact", "starts_with", "contains"]] = None,
    flag_value: Optional[bool] = None,
    config: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    project_name: Optional[str] = None,
) -> str:
    """Find predefined values of metadata objects.

Modes:
- name: search predefined item names; criterion required, criterion_match applies.
- flag: find accounting predefined items by boolean flag; criterion is the flag name
  (Валютный|Количественный|УчетПоПодразделениям|НалоговыйУчет), flag_value defaults to true.
- account_type: find predefined accounts by account type (ТипСчета); criterion required.
- subconto_type: find predefined accounts by subconto kind (ВидыСубконто); criterion required.

owner_object optionally scopes search to one metadata object.
criterion_match is supported only in mode="name".
config scopes to one configuration or extension.
"""
    if criterion_match is not None and mode != "name":
        return "Error: criterion_match is supported only for mode='name'."
    loader = _init_loader()
    if loader is None:
        return "Error: Neo4j database connection not available."
    try:
        pn = _resolve_project(project_name)
        config_name = resolve_config(loader, config, pn)
        scope = _scope(config_name)
        lim = clamp_limit(limit)
        off = clamp_offset(offset)
        has_extensions = bool(_run_query(
            loader,
            "MATCH (c:Configuration {project_name: $project_name, is_extension: true}) RETURN c LIMIT 1",
            {},
            pn,
        ))
        adoption_col = ", adoption" if has_extensions else ""
        # Fetch lim + 1 rows; the shaper trims to lim and derives has_more/next_offset.
        params: Dict[str, Any] = {"limit": lim + 1, "offset": off}
        if config_name:
            params["config_name"] = config_name
        # Unified owner scoping across all modes: resolve owner_object once and filter
        # every branch by m.qualified_name, so behavior matches other metadata tools.
        if owner_object and owner_object.strip():
            resolved = resolve_object_ref(loader, owner_object, pn, config_name)
            params["owner_qn"] = resolved["qualified_name"]
        else:
            params["owner_qn"] = None
        owner_filter = "($owner_qn IS NULL OR m.qualified_name = $owner_qn)"
        owner_return = (
            "m.config_name AS config_name, m.category_name AS category, "
            "m.name AS object, m.qualified_name AS owner_qn"
        )
        mm = (criterion_match or "exact").lower()

        if mode == "name":
            if not criterion or not criterion.strip():
                return "Error: criterion (predefined item name) is required for mode='name'."
            params["name"] = criterion.strip()
            cond = apply_match("p.`Имя`", "name", mm)
            _adp = _full_elem_adoption_block("p", "PredefinedItem", "m, p") if has_extensions else ""
            cypher = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_PREDEFINED]->(p:PredefinedItem)
WHERE {cond}
  AND {owner_filter}
{_adp}
RETURN {owner_return},
  p.`Имя` AS name, p.qualified_name AS qualified_name,
  coalesce(p.`Код`,'') AS code, coalesce(p.`Наименование`,'') AS description{adoption_col}
ORDER BY config_name, object, name
SKIP $offset LIMIT $limit
""".strip()

        elif mode == "flag":
            allowed = {"Валютный", "Количественный", "УчетПоПодразделениям", "НалоговыйУчет"}
            if not criterion or criterion.strip() not in allowed:
                return f"Error: criterion must be one of {sorted(allowed)} for mode='flag'."
            flag = criterion.strip()
            val = bool(flag_value) if flag_value is not None else True
            params["value"] = val
            _adp = _full_elem_adoption_block("p", "PredefinedItem", "m, p") if has_extensions else ""
            cypher = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_PREDEFINED]->(p:PredefinedItem)
WHERE {owner_filter}
  AND coalesce(p.`{flag}`, false) = $value
{_adp}
RETURN {owner_return},
  p.`Имя` AS name, p.qualified_name AS qualified_name,
  '{flag}' AS flag_name, p.`{flag}` AS flag_value{adoption_col}
ORDER BY config_name, object, name
SKIP $offset LIMIT $limit
""".strip()

        elif mode == "account_type":
            if not criterion or not criterion.strip():
                return "Error: criterion (account type) is required for mode='account_type'."
            params["type"] = criterion.strip()
            _adp = _full_elem_adoption_block("p", "PredefinedItem", "m, p") if has_extensions else ""
            cypher = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_PREDEFINED]->(p:PredefinedItem)
WHERE {owner_filter}
  AND toLower(p.`ТипСчета`) = toLower($type)
{_adp}
RETURN {owner_return},
  p.`Имя` AS name, p.qualified_name AS qualified_name,
  p.`ТипСчета` AS account_type{adoption_col}
ORDER BY config_name, object, name
SKIP $offset LIMIT $limit
""".strip()

        elif mode == "subconto_type":
            if not criterion or not criterion.strip():
                return "Error: criterion (subconto kind) is required for mode='subconto_type'."
            params["kind"] = criterion.strip()
            # Unwind ВидыСубконто and keep only the matched kind as a scalar; sk is carried
            # through the adoption block's carry_vars so it survives to RETURN.
            _adp = _full_elem_adoption_block("p", "PredefinedItem", "m, p, sk") if has_extensions else ""
            cypher = f"""
MATCH (m:MetadataObject {scope.map_for()})-[:HAS_PREDEFINED]->(p:PredefinedItem)
WHERE {owner_filter}
UNWIND coalesce(p.`ВидыСубконто`,[]) AS sk
WITH m, p, sk WHERE toLower(toString(sk)) = toLower($kind)
{_adp}
RETURN {owner_return},
  p.`Имя` AS name, p.qualified_name AS qualified_name,
  toString(sk) AS subconto_kind{adoption_col}
ORDER BY config_name, object, name
SKIP $offset LIMIT $limit
""".strip()

        else:
            return f"Error: unknown mode='{mode}'."

        results = _run_query(loader, cypher, params, pn)
        if has_extensions:
            results = _strip_null_adoption(results)
        shaped = _shape_find_predefined_values_result(results, lim=lim, off=off)
        return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception("Error in find_predefined_values")
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Tool 8: get_access_rights
# ---------------------------------------------------------------------------

def _register_get_access_rights(mcp):
    def get_access_rights(
        mode: Literal["roles_for_target", "targets_of_role", "role_rights_to_target"],
        target_ref: Optional[str] = None,
        role_ref: Optional[str] = None,
        role_match: Optional[Literal["exact", "starts_with", "contains"]] = None,
        config: Optional[str] = None,
        include_conditions: Optional[bool] = False,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        project_name: Optional[str] = None,
    ) -> str:
        """Query role-based access rights for roles and metadata targets.

mode="roles_for_target": list roles that have rights to target_ref.
mode="targets_of_role": list targets available to role_ref.
mode="role_rights_to_target": get rights of role_ref to target_ref.

target_ref accepts object name or qualified_name.
role_ref accepts role name or qualified_name.
role_match is the comparison mode for role_ref in mode="targets_of_role".
Use include_conditions=true with mode="role_rights_to_target" to include right condition text.
Use config to scope to one configuration or extension.
"""
        if include_conditions and mode != "role_rights_to_target":
            return "Error: include_conditions is supported only for mode='role_rights_to_target'."
        loader = _init_loader()
        if loader is None:
            return "Error: Neo4j database connection not available."
        try:
            pn = _resolve_project(project_name)
            config_name = resolve_config(loader, config, pn)
            scope = _scope(config_name)
            lim = clamp_limit(limit)
            off = clamp_offset(offset)

            rights_expr = (
                "[k IN coalesce(rel.rights_present_en, []) | {"
                "right_ru: m[k + '_ru'], allowed: m[k + '_allowed'], "
                "has_condition: coalesce(m[k + '_has_condition'], false)}]"
            )
            rights_expr_with_conditions = (
                "[k IN coalesce(rel.rights_present_en, []) | {"
                "right_ru: m[k + '_ru'], allowed: m[k + '_allowed'], "
                "has_condition: coalesce(m[k + '_has_condition'], false), "
                "condition: coalesce(m[k + '_condition'], '')}]"
            )

            if mode == "roles_for_target":
                if not target_ref or not target_ref.strip():
                    return "Error: target_ref is required for mode='roles_for_target'."
                target_qn = normalize_qn_ref(loader, target_ref.strip(), pn, config_name)
                cypher = f"""
MATCH (r:MetadataObject {{category_name:'Роли', project_name:$project_name{scope.metadata_map}}})-[rel:GRANTS_ACCESS_TO]->(t {{qualified_name:$target_qn}})
WITH r, rel, properties(rel) AS m
WITH r, {rights_expr} AS rights
RETURN r.name AS role, r.qualified_name AS role_qn, r.config_name AS config_name, rights
ORDER BY role
SKIP $offset LIMIT $limit
""".strip()
                params: Dict[str, Any] = {"target_qn": target_qn, "offset": off, "limit": lim}
                if config_name:
                    params["config_name"] = config_name

            elif mode == "targets_of_role":
                if not role_ref or not role_ref.strip():
                    return "Error: role_ref is required for mode='targets_of_role'."
                rref = role_ref.strip()
                rm = (role_match or "exact").lower()
                params = {"offset": off, "limit": lim}
                if config_name:
                    params["config_name"] = config_name

                # Pattern search — bypass resolve
                if role_match and role_match != "exact":
                    params["role_ref"] = rref
                    role_where = f"WHERE {apply_match('r.name', 'role_ref', rm)}"
                    scope_filter = f"AND r.project_name = $project_name{scope.and_alias('r')}"
                    cypher = f"""
MATCH (r:MetadataObject {{category_name:'Роли'}})-[rel:GRANTS_ACCESS_TO]->(t)
{role_where} {scope_filter}
WITH r, rel, t, properties(rel) AS m
WITH r, t, {rights_expr} AS rights
RETURN r.name AS role, r.qualified_name AS role_qn, r.config_name AS config_name,
  head([l IN labels(t) WHERE l IN ['Attribute','Dimension','Resource','FormAttribute','MetadataObject','Form','Command','TabularPart','Configuration']]) AS target_label,
  coalesce(t.name,'') AS target_name, t.qualified_name AS target_qn, rights
ORDER BY role, target_label, target_name
SKIP $offset LIMIT $limit
""".strip()
                elif "/" in rref or config_name:
                    norm_role = normalize_qn_ref(loader, rref, pn, config_name)
                    params["role_qn"] = norm_role
                    scope_filter = f"AND r.project_name = $project_name"
                    cypher = f"""
MATCH (r:MetadataObject {{category_name:'Роли', qualified_name:$role_qn}})-[rel:GRANTS_ACCESS_TO]->(t)
WHERE r.project_name = $project_name
WITH r, rel, t, properties(rel) AS m
WITH r, t, {rights_expr} AS rights
RETURN r.name AS role, r.qualified_name AS role_qn, r.config_name AS config_name,
  head([l IN labels(t) WHERE l IN ['Attribute','Dimension','Resource','FormAttribute','MetadataObject','Form','Command','TabularPart','Configuration']]) AS target_label,
  coalesce(t.name,'') AS target_name, t.qualified_name AS target_qn, rights
ORDER BY role, target_label, target_name
SKIP $offset LIMIT $limit
""".strip()
                else:
                    params["role_ref"] = rref
                    cypher = f"""
MATCH (r:MetadataObject {{category_name:'Роли', project_name:$project_name{scope.metadata_map}}})-[rel:GRANTS_ACCESS_TO]->(t)
WHERE toLower(r.name) = toLower($role_ref)
WITH r, rel, t, properties(rel) AS m
WITH r, t, {rights_expr} AS rights
RETURN r.name AS role, r.qualified_name AS role_qn, r.config_name AS config_name,
  head([l IN labels(t) WHERE l IN ['Attribute','Dimension','Resource','FormAttribute','MetadataObject','Form','Command','TabularPart','Configuration']]) AS target_label,
  coalesce(t.name,'') AS target_name, t.qualified_name AS target_qn, rights
ORDER BY role, target_label, target_name
SKIP $offset LIMIT $limit
""".strip()

            elif mode == "role_rights_to_target":
                if not role_ref or not role_ref.strip():
                    return "Error: role_ref is required for mode='role_rights_to_target'."
                if not target_ref or not target_ref.strip():
                    return "Error: target_ref is required for mode='role_rights_to_target'."
                rref = role_ref.strip()
                target_qn = normalize_qn_ref(loader, target_ref.strip(), pn, config_name)
                params = {"target_qn": target_qn, "offset": off, "limit": lim}
                if config_name:
                    params["config_name"] = config_name

                if "/" in rref or config_name:
                    norm_role = normalize_qn_ref(loader, rref, pn, config_name)
                    params["role_qn"] = norm_role
                    role_filter = "r.qualified_name = $role_qn"
                else:
                    params["role_ref"] = rref
                    role_filter = "toLower(r.name) = toLower($role_ref)"

                rexpr = rights_expr_with_conditions if include_conditions else rights_expr
                cypher = f"""
MATCH (r:MetadataObject {{category_name:'Роли', project_name:$project_name{scope.metadata_map}}})-[rel:GRANTS_ACCESS_TO]->(t {{qualified_name:$target_qn}})
WHERE {role_filter}
WITH r, rel, t, properties(rel) AS m
WITH r, t, {rexpr} AS rights
RETURN r.name AS role, r.qualified_name AS role_qn, r.config_name AS config_name,
  head([l IN labels(t) WHERE l IN ['Attribute','Dimension','Resource','FormAttribute','MetadataObject','Form','Command','TabularPart','Configuration']]) AS target_label,
  coalesce(t.name,'') AS target_name, t.qualified_name AS target_qn, rights
SKIP $offset LIMIT $limit
""".strip()

            else:
                return f"Error: unknown mode='{mode}'."

            results = _run_query(loader, cypher, params, pn)
            return _done(results)

        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.exception("Error in get_access_rights")
            return f"Error: {e}"
    _patch_tool_defaults(get_access_rights)
    mcp.tool()(get_access_rights)


# ---------------------------------------------------------------------------
# Tool 9: get_metadata_details
# ---------------------------------------------------------------------------

# _filter_node_props is defined above (line ~88) and used here too.


def _register_get_metadata_details(mcp):
    def get_metadata_details(
        mode: Literal["resolve", "properties"],
        ref_type: Literal[
            "qualified_name", "qualified_name_prefix", "guid",
            "routine_id", "object", "form", "command",
            "attribute", "resource", "dimension", "control", "enum_value",
            "tabular_part", "tabular_attribute",
            "form_attribute", "form_command", "form_event", "form_event_action",
        ],
        ref: str,
        owner_ref: Optional[str] = None,
        config: Optional[str] = None,
        include_help: Optional[bool] = False,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        project_name: Optional[str] = None,
    ) -> str:
        """Resolve metadata references or return filtered node properties.

Notation: <...> is a placeholder you substitute (do not type the angle brackets); plain
segments are literal.

Modes:
- resolve: return matched node cards.
- properties: return matched node cards plus filtered properties.

ref_type selects the target node type; ref identifies the target.

Objects:
- object: ref = <Имя>, <Категория>.<Объект>, <Категория>/<Объект>, or full qualified_name.

Object children:
- command, attribute, resource, dimension, enum_value, tabular_part:
  owner_ref = <Объект>; ref = <ИмяДочернегоЭлемента>. Or pass its full qualified_name.
- form: ref = <Категория>.<Объект>.Форма.<ИмяФормы>, <Категория>.<Объект>.Формы.<ИмяФормы>,
  ОбщиеФормы.<ИмяФормы>, or full qualified_name.

Form children:
- form_attribute, form_command: owner_ref = <Форма> or common form; ref = <Имя>.
- form_event: owner_ref = <Форма>, common form, or form control; ref = <ИмяСобытия>.
- control: owner_ref = <Форма> or common form; ref = <ИмяЭлемента>. Or pass full control qualified_name.

Other child refs:
- tabular_attribute: owner_ref = <Категория>.<Объект>.<ИмяТабличнойЧасти> or full tabular part
  qualified_name; ref = <ИмяРеквизита>.
- form_event_action: owner_ref = form event path; ref = Main, Before, After, or Override, not the event name.

If ref is a full qualified_name, do not pass owner_ref.
If owner_ref is passed, ref must be only the final name/action, not a path.
routine_id uses ref as routine id; guid uses ref as GUID.
include_help=true affects mode="properties" only.
"""
        loader = _init_loader()
        if loader is None:
            return "Error: Neo4j database connection not available."
        try:
            pn = _resolve_project(project_name)
            config_name = resolve_config(loader, config, pn)
            scope = _scope(config_name)
            lim = clamp_limit(limit)
            off = clamp_offset(offset)
            inc_help = bool(include_help)
            ref = (ref or "").strip()

            if not ref:
                return "Error: ref cannot be empty."

            project_prefix = pn + "/"

            def _adp_block(node_var: str, label: str, carry: str) -> str:
                return f"""MATCH (cfg:Configuration {{project_name: $project_name, name: {node_var}.config_name}})
OPTIONAL MATCH (ext_n:{label})-[:ADOPTED_FROM]->({node_var})
WHERE ext_n.qualified_name STARTS WITH $project_prefix
WITH {carry}, cfg, collect(DISTINCT ext_n.config_name) AS _ext_names
OPTIONAL MATCH ({node_var})-[:ADOPTED_FROM]->(base_n:{label})
WHERE base_n.qualified_name STARTS WITH $project_prefix
WITH {carry}, cfg, _ext_names, base_n.config_name AS _base_cn
WITH {carry},
     CASE
       WHEN NOT coalesce(cfg.is_extension, false) AND size(_ext_names) > 0
         THEN {{role: 'base', extension_config_names: _ext_names}}
       WHEN coalesce(cfg.is_extension, false) AND _base_cn IS NOT NULL
         THEN {{role: 'extension', base_config_name: _base_cn}}
       ELSE {{role: 'none'}}
     END AS adoption"""

            def _md_pointed_target_query(ref_type, ref, owner_ref, *, with_props, has_extensions):
                """Build (cypher, params, point) for one pointed target node. Each branch
                declares an _MdTargetSpec and its MATCH/WHERE; the node-card column set is
                produced once by _md_node_card_return, so resolve and properties cannot
                drift. props + adoption/interception are added only when with_props."""
                params: Dict[str, Any] = {"project_prefix": project_prefix}
                if config_name:
                    params["config_name"] = config_name

                def _adp(node_var, label, carry):
                    if with_props and has_extensions:
                        return "\n" + _adp_block(node_var, label, carry), ", adoption"
                    return "", ""

                def _arm(match_where, spec, adp_block_str="", tail_col=""):
                    return (match_where + adp_block_str + "\n"
                            + _md_node_card_return(spec, with_props=with_props, tail_col=tail_col))

                if ref_type == "routine_id":
                    extra_filter = (
                        "\n  AND (coalesce(r.owner_qn,'') STARTS WITH ($project_name + '/') "
                        "OR r.project_name = $project_name)"
                    )
                    if config_name:
                        extra_filter += "\n  AND r.config_name = $config_name"
                    spec = _MdTargetSpec(
                        kind="Routine", node_var="r", name_expr="coalesce(r.name,'')",
                        config_expr="coalesce(r.config_name,'')", qn_expr=None,
                        extra={"id": "r.id", "owner_qn": "coalesce(r.owner_qn,'')"},
                    )
                    cypher = (f"MATCH (r:Routine {{id: $ref}})\nWHERE true{extra_filter}\n"
                              + _md_node_card_return(spec, with_props=with_props) + "\nLIMIT 1")
                    params["ref"] = ref
                    return cypher, params, True

                if ref_type == "control":
                    spec = _MdTargetSpec(kind="FormControl", node_var="fc",
                                         name_expr="coalesce(fc.name,'')",
                                         config_expr="coalesce(fc.config_name,'')",
                                         qn_expr="fc.qualified_name")
                    adp, col = _adp("fc", "FormControl", "fc")
                    if owner_ref and owner_ref.strip():
                        # owner_ref given: resolve the control by name within the form.
                        # None => form resolved but no such control => empty paged result;
                        # ambiguity / bad form owner => ValueError surfaces as Error.
                        fc_qn = resolve_control_ref(loader, owner_ref.strip(), ref, pn, config_name)
                        if fc_qn is None:
                            mw = "MATCH (fc:FormControl)\nWHERE false"
                            params["ref"] = ref
                            return _arm(mw, spec, adp, col) + "\nLIMIT 1", params, True
                        params["ref"] = fc_qn
                    else:
                        # no owner_ref: ref must be the full qualified_name of the control.
                        params["ref"] = ref
                    mw = ("MATCH (fc:FormControl)\nWHERE fc.qualified_name STARTS WITH "
                          "$project_prefix AND toLower(fc.qualified_name) = toLower($ref)")
                    return _arm(mw, spec, adp, col) + "\nLIMIT 1", params, True

                if ref_type == "object":
                    resolved = _resolve_object_ref_canon(loader, ref, pn, config_name)
                    spec = _MdTargetSpec(kind="MetadataObject", node_var="m",
                                         name_expr="m.name", config_expr="m.config_name",
                                         qn_expr="m.qualified_name",
                                         extra={"category": "m.category_name"})
                    adp, col = _adp("m", "MetadataObject", "m")
                    mw = (f"MATCH (m:MetadataObject {{project_name:$project_name{scope.metadata_map}}})\n"
                          "WHERE toLower(m.qualified_name) = toLower($qn)")
                    params["qn"] = resolved["qualified_name"]
                    return _arm(mw, spec, adp, col) + "\nLIMIT 1", params, True

                if ref_type in ("tabular_attribute", "form_attribute", "form_command",
                                "form_event", "form_event_action"):
                    if not owner_ref or not owner_ref.strip():
                        _owner = ("the form event" if ref_type == "form_event_action"
                                  else "the tabular part" if ref_type == "tabular_attribute"
                                  else "the form")
                        raise ValueError(
                            f"owner_ref is required for ref_type='{ref_type}': pass {_owner} "
                            f"as owner_ref (or the full qualified_name of the target in ref)."
                        )
                    params["ref"] = ref
                    _owner_ref = owner_ref.strip()
                    # Typed owner resolution per ref_type. Each resolver raises a clean,
                    # tool-specific message and enforces the config-belonging invariant for
                    # full QNs (downstream queries trust $owner_qn and do not re-scope it).
                    if ref_type == "tabular_attribute":
                        owner_qn = resolve_tabular_part_ref(loader, _owner_ref, pn, config_name)
                    elif ref_type == "form_event_action":
                        owner_qn = resolve_form_event_ref(loader, _owner_ref, pn, config_name)
                    elif (ref_type == "form_event" and _owner_ref.startswith(project_prefix)
                          and "FormControl" in _md_node_labels(loader, _owner_ref, pn)):
                        # control-level form events: accept a full FormControl QN directly, but
                        # only within the selected config (downstream mw2 does not filter config).
                        if not _md_qn_in_config(_owner_ref, pn, config_name):
                            raise ValueError(
                                f"owner_ref {_owner_ref!r} does not belong to config {config_name!r}."
                            )
                        owner_qn = _owner_ref
                    else:
                        owner_qn = resolve_form_owner_ref(loader, _owner_ref, pn, config_name)
                    params["owner_qn"] = owner_qn

                    if ref_type == "tabular_attribute":
                        spec = _MdTargetSpec(kind="Attribute", node_var="a", name_expr="a.name",
                                             config_expr="t.config_name", qn_expr="a.qualified_name")
                        adp, col = _adp("a", "Attribute", "t, a")
                        cfg = "\n  AND a.config_name = $config_name" if config_name else ""
                        mw = ("MATCH (t:TabularPart {project_name:$project_name})-[:HAS_ATTRIBUTE]->(a:Attribute)\n"
                              "WHERE toLower(t.qualified_name) = toLower($owner_qn)\n"
                              f"  AND toLower(a.name) = toLower($ref){cfg}")
                        return _arm(mw, spec, adp, col) + "\nLIMIT 1", params, True

                    if ref_type == "form_attribute":
                        s1 = _MdTargetSpec(kind="FormAttribute", node_var="fa", name_expr="fa.name",
                                           config_expr="f.config_name", qn_expr="fa.qualified_name")
                        s2 = _MdTargetSpec(kind="FormAttribute", node_var="fa", name_expr="fa.name",
                                           config_expr="m.config_name", qn_expr="fa.qualified_name")
                        a1, c1 = _adp("fa", "FormAttribute", "f, fa")
                        a2, c2 = _adp("fa", "FormAttribute", "m, fa")
                        mw1 = ("MATCH (f:Form {project_name:$project_name})-[:HAS_FORM_ATTRIBUTE]->(fa:FormAttribute)\n"
                               "WHERE toLower(f.qualified_name) = toLower($owner_qn)\n"
                               "  AND toLower(fa.name) = toLower($ref)")
                        mw2 = ("MATCH (m:MetadataObject {project_name:$project_name, category_name:'ОбщиеФормы'})-[:HAS_FORM_ATTRIBUTE]->(fa:FormAttribute)\n"
                               "WHERE toLower(m.qualified_name) = toLower($owner_qn)\n"
                               "  AND toLower(fa.name) = toLower($ref)")
                        cypher = _arm(mw1, s1, a1, c1) + "\nUNION\n" + _arm(mw2, s2, a2, c2) + "\nLIMIT 1"
                        return cypher, params, True

                    if ref_type == "form_command":
                        s1 = _MdTargetSpec(kind="Command", node_var="c", name_expr="c.name",
                                           config_expr="f.config_name", qn_expr="c.qualified_name")
                        s2 = _MdTargetSpec(kind="Command", node_var="c", name_expr="c.name",
                                           config_expr="m.config_name", qn_expr="c.qualified_name")
                        a1, c1 = _adp("c", "Command", "f, c")
                        a2, c2 = _adp("c", "Command", "m, c")
                        mw1 = ("MATCH (f:Form {project_name:$project_name})-[:HAS_COMMAND]->(c:Command)\n"
                               "WHERE toLower(f.qualified_name) = toLower($owner_qn)\n"
                               "  AND toLower(c.name) = toLower($ref)")
                        mw2 = ("MATCH (m:MetadataObject {project_name:$project_name, category_name:'ОбщиеФормы'})-[:HAS_COMMAND]->(c:Command)\n"
                               "WHERE toLower(m.qualified_name) = toLower($owner_qn)\n"
                               "  AND toLower(c.name) = toLower($ref)")
                        cypher = _arm(mw1, s1, a1, c1) + "\nUNION\n" + _arm(mw2, s2, a2, c2) + "\nLIMIT 1"
                        return cypher, params, True

                    if ref_type == "form_event":
                        s1 = _MdTargetSpec(kind="FormEvent", node_var="fe", name_expr="fe.name",
                                           config_expr="f.config_name", qn_expr="fe.qualified_name")
                        s2 = _MdTargetSpec(kind="FormEvent", node_var="fe", name_expr="fe.name",
                                           config_expr="fc.config_name", qn_expr="fe.qualified_name")
                        s3 = _MdTargetSpec(kind="FormEvent", node_var="fe", name_expr="fe.name",
                                           config_expr="m.config_name", qn_expr="fe.qualified_name")
                        a1, c1 = _adp("fe", "FormEvent", "f, fe")
                        a2, c2 = _adp("fe", "FormEvent", "fc, fe")
                        a3, c3 = _adp("fe", "FormEvent", "m, fe")
                        mw1 = ("MATCH (f:Form {project_name:$project_name})-[:HAS_EVENT]->(fe:FormEvent)\n"
                               "WHERE toLower(f.qualified_name) = toLower($owner_qn)\n"
                               "  AND toLower(fe.name) = toLower($ref)")
                        mw2 = ("MATCH (fc:FormControl)-[:HAS_EVENT]->(fe:FormEvent)\n"
                               "WHERE fc.qualified_name STARTS WITH $project_prefix\n"
                               "  AND toLower(fc.qualified_name) = toLower($owner_qn)\n"
                               "  AND toLower(fe.name) = toLower($ref)")
                        mw3 = ("MATCH (m:MetadataObject {project_name:$project_name, category_name:'ОбщиеФормы'})-[:HAS_EVENT]->(fe:FormEvent)\n"
                               "WHERE toLower(m.qualified_name) = toLower($owner_qn)\n"
                               "  AND toLower(fe.name) = toLower($ref)")
                        cypher = (_arm(mw1, s1, a1, c1) + "\nUNION\n" + _arm(mw2, s2, a2, c2)
                                  + "\nUNION\n" + _arm(mw3, s3, a3, c3) + "\nLIMIT 1")
                        return cypher, params, True

                    # form_event_action — interception block instead of adoption
                    if (ref or "").strip().lower() not in _MD_FORM_EVENT_ACTION_CALL_TYPES:
                        raise ValueError(
                            "ref must be Main, Before, After, or Override for "
                            "ref_type='form_event_action'."
                        )
                    spec = _MdTargetSpec(kind="FormEventAction", node_var="fea",
                                         name_expr="coalesce(fea.handler_name,'')",
                                         config_expr="fea.config_name", qn_expr="fea.qualified_name")
                    if with_props and has_extensions:
                        icp, icp_col = "\n" + _form_event_action_interception_block(), ", interception"
                    else:
                        icp, icp_col = "", ""
                    mw = ("MATCH (fe:FormEvent {qualified_name: $owner_qn})-[:HAS_EVENT_ACTION]->(fea:FormEventAction)\n"
                          "WHERE fe.qualified_name STARTS WITH $project_prefix\n"
                          "  AND toLower(fea.call_type) = toLower($ref)")
                    return _arm(mw, spec, icp, icp_col) + "\nLIMIT 1", params, True

                if ref_type in _MD_OWNED_CHILD:
                    rel, label = _MD_OWNED_CHILD[ref_type]
                    if not owner_ref or not owner_ref.strip():
                        raise ValueError(
                            f"owner_ref is required for ref_type='{ref_type}' when ref is only "
                            f"a child name. Pass owner_ref as the object, or ref as "
                            f"Category.Object.{label}."
                        )
                    resolved_owner = _resolve_object_ref_canon(loader, owner_ref.strip(), pn, config_name)
                    spec = _MdTargetSpec(kind=label, node_var="x", name_expr="x.name",
                                         config_expr="m.config_name", qn_expr="x.qualified_name")
                    adp, col = _adp("x", label, "m, x")
                    mw = (f"MATCH (m:MetadataObject {{name:$obj_name, category_name:$obj_cat, project_name:$project_name{scope.metadata_map}}})-[:{rel}]->(x:{label})\n"
                          "WHERE toLower(x.name) = toLower($ref)")
                    params["ref"] = ref
                    params["obj_name"] = resolved_owner["name"]
                    params["obj_cat"] = resolved_owner["category_name"]
                    return _arm(mw, spec, adp, col) + "\nLIMIT 1", params, True

                raise ValueError(f"unsupported ref_type='{ref_type}'.")

            # Normalize the target ref for both modes: full QN / form-path / element combined
            # refs switch to the qualified_name path; Category.Object.Child splits into
            # owner_ref + child; a ref_type type-guard prevents silent mismatches.
            ref_type, ref, owner_ref = _md_resolve_target_ref(
                loader, ref_type, ref, owner_ref, pn, config_name)

            if mode == "resolve":
                res_lim, res_off = lim, off
                if ref_type == "qualified_name":
                    match_ref = ref
                    try:
                        match_ref = normalize_qn_ref(loader, ref, pn, config_name=config_name)
                    except ValueError:
                        match_ref = ref
                    cypher = f"""
MATCH (n)
WHERE {_md_scope_where('n', config_name=config_name)}
  AND toLower(n.qualified_name) = toLower($ref)
RETURN head(labels(n)) AS kind, n.qualified_name AS qualified_name,
  coalesce(n.name, '') AS name, coalesce(n.config_name,'') AS config_name
LIMIT 1
""".strip()
                    params: Dict[str, Any] = {"ref": match_ref, "project_prefix": project_prefix}
                    if config_name:
                        params["config_name"] = config_name
                    res_lim, res_off = 1, 0

                elif ref_type == "qualified_name_prefix":
                    cypher = f"""
MATCH (n {{project_name:$project_name{scope.metadata_map}}})
WHERE n.qualified_name STARTS WITH $ref
RETURN n.qualified_name AS qualified_name, head(labels(n)) AS kind,
  coalesce(n.name, '') AS name, coalesce(n.config_name,'') AS config_name
UNION
MATCH (m:MetadataObject {{project_name:$project_name, category_name:'ОбщиеФормы'{scope.metadata_map}}})
      -[:HAS_FORM_ATTRIBUTE|HAS_COMMAND|HAS_EVENT]->(n)
WHERE n.qualified_name STARTS WITH $ref
RETURN n.qualified_name AS qualified_name, head(labels(n)) AS kind,
  coalesce(n.name,'') AS name, coalesce(m.config_name,'') AS config_name
UNION
MATCH (m:MetadataObject {{project_name:$project_name, category_name:'ОбщиеФормы'{scope.metadata_map}}})
      -[:HAS_CONTROL]->(root:FormControl)-[:HAS_CHILD*0..]->(n:FormControl)
WHERE n.qualified_name STARTS WITH $ref
RETURN n.qualified_name AS qualified_name, 'FormControl' AS kind,
  coalesce(n.name,'') AS name, coalesce(m.config_name,'') AS config_name
UNION
MATCH (m:MetadataObject {{project_name:$project_name, category_name:'ОбщиеФормы'{scope.metadata_map}}})
      -[:HAS_CONTROL]->(root:FormControl)-[:HAS_CHILD*0..]->(fc:FormControl)-[:HAS_EVENT]->(n:FormEvent)
WHERE n.qualified_name STARTS WITH $ref
RETURN n.qualified_name AS qualified_name, 'FormEvent' AS kind,
  coalesce(n.name,'') AS name, coalesce(m.config_name,'') AS config_name
UNION
MATCH (m:MetadataObject {{project_name:$project_name, category_name:'ОбщиеФормы'{scope.metadata_map}}})
      -[:HAS_EVENT]->(fe:FormEvent)-[:HAS_EVENT_ACTION]->(n:FormEventAction)
WHERE n.qualified_name STARTS WITH $ref
RETURN n.qualified_name AS qualified_name, 'FormEventAction' AS kind,
  coalesce(n.name,'') AS name, coalesce(m.config_name,'') AS config_name
UNION
MATCH (m:MetadataObject {{project_name:$project_name, category_name:'ОбщиеФормы'{scope.metadata_map}}})
      -[:HAS_CONTROL]->(root:FormControl)-[:HAS_CHILD*0..]->(fc:FormControl)
      -[:HAS_EVENT]->(fe:FormEvent)-[:HAS_EVENT_ACTION]->(n:FormEventAction)
WHERE n.qualified_name STARTS WITH $ref
RETURN n.qualified_name AS qualified_name, 'FormEventAction' AS kind,
  coalesce(n.name,'') AS name, coalesce(m.config_name,'') AS config_name
ORDER BY qualified_name
SKIP $offset LIMIT $limit
""".strip()
                    params = {"ref": ref, "offset": off, "limit": lim + 1}
                    if config_name:
                        params["config_name"] = config_name

                elif ref_type == "guid":
                    guid_norm = ref.replace("-", "").lower()
                    cypher = f"""
MATCH (m:MetadataObject)
WHERE toLower(replace(coalesce(m.meta_uuid,''),'-','')) = $guid_norm
  AND m.project_name = $project_name{scope.and_alias('m')}
RETURN 'MetadataObject' AS kind, m.category_name AS category, m.name AS object,
  NULL AS tabular, m.name AS name, m.qualified_name AS qualified_name, m.config_name AS config_name
UNION
MATCH (m:MetadataObject)-[:HAS_ATTRIBUTE]->(a:Attribute)
WHERE toLower(replace(coalesce(a.meta_uuid,''),'-','')) = $guid_norm
  AND m.project_name = $project_name{scope.and_alias('m')}
RETURN 'Attribute' AS kind, m.category_name AS category, m.name AS object,
  NULL AS tabular, a.name AS name, a.qualified_name AS qualified_name, m.config_name AS config_name
UNION
MATCH (m:MetadataObject)-[:HAS_TABULAR_PART]->(t:TabularPart)
WHERE toLower(replace(coalesce(t.meta_uuid,''),'-','')) = $guid_norm
  AND m.project_name = $project_name{scope.and_alias('m')}
RETURN 'TabularPart' AS kind, m.category_name AS category, m.name AS object,
  NULL AS tabular, t.name AS name, t.qualified_name AS qualified_name, m.config_name AS config_name
UNION
MATCH (m:MetadataObject)-[:HAS_RESOURCE]->(r:Resource)
WHERE toLower(replace(coalesce(r.meta_uuid,''),'-','')) = $guid_norm
  AND m.project_name = $project_name{scope.and_alias('m')}
RETURN 'Resource' AS kind, m.category_name AS category, m.name AS object,
  NULL AS tabular, r.name AS name, r.qualified_name AS qualified_name, m.config_name AS config_name
UNION
MATCH (m:MetadataObject)-[:HAS_DIMENSION]->(d:Dimension)
WHERE toLower(replace(coalesce(d.meta_uuid,''),'-','')) = $guid_norm
  AND m.project_name = $project_name{scope.and_alias('m')}
RETURN 'Dimension' AS kind, m.category_name AS category, m.name AS object,
  NULL AS tabular, d.name AS name, d.qualified_name AS qualified_name, m.config_name AS config_name
UNION
MATCH (m:MetadataObject)-[:HAS_FORM]->(f:Form)
WHERE toLower(replace(coalesce(f.meta_uuid,''),'-','')) = $guid_norm
  AND m.project_name = $project_name{scope.and_alias('m')}
RETURN 'Form' AS kind, m.category_name AS category, m.name AS object,
  NULL AS tabular, f.name AS name, f.qualified_name AS qualified_name, m.config_name AS config_name
ORDER BY category, object, kind, name
SKIP $offset LIMIT $limit
""".strip()
                    params = {"guid_norm": guid_norm, "offset": off, "limit": lim + 1}
                    if config_name:
                        params["config_name"] = config_name

                else:
                    cypher, params, _point = _md_pointed_target_query(
                        ref_type, ref, owner_ref, with_props=False, has_extensions=False)
                    res_lim, res_off = 1, 0

                results = _run_query(loader, cypher, params, pn)
                shaped = _shape_get_metadata_details_resolve_result(results, lim=res_lim, off=res_off)
                return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

            elif mode == "properties":
                has_extensions = bool(_run_query(
                    loader,
                    "MATCH (c:Configuration {project_name: $project_name, is_extension: true}) RETURN c LIMIT 1",
                    {},
                    pn,
                ))
                _adp_col = ", adoption" if has_extensions else ""

                def _emit_props(rows: list, *, point: bool) -> str:
                    """Shape properties rows into {page, nodes, properties, help?} and format.
                    point=True for single-node branches (page.limit=1, offset=0); point=False
                    for multi-node branches (qualified_name_prefix, guid) fetched with lim+1."""
                    page_lim = 1 if point else lim
                    page_off = 0 if point else off
                    shaped = _shape_get_metadata_details_properties_result(
                        rows, lim=page_lim, off=page_off, include_help=inc_help,
                    )
                    return _fmt_dict(
                        shaped, apply_compact_refs=True, normalize_arrays_for_toon=True,
                        compact_property_names=True,
                    )

                if ref_type == "guid":
                    guid_norm = ref.replace("-", "").lower()
                    _adp_mo = _adp_block("m", "MetadataObject", "m") if has_extensions else ""
                    _adp_a = _adp_block("a", "Attribute", "m, a") if has_extensions else ""
                    _adp_t = _adp_block("t", "TabularPart", "m, t") if has_extensions else ""
                    _adp_r = _adp_block("r", "Resource", "m, r") if has_extensions else ""
                    _adp_d = _adp_block("d", "Dimension", "m, d") if has_extensions else ""
                    _adp_f = _adp_block("f", "Form", "m, f") if has_extensions else ""
                    resolve_cypher = f"""
MATCH (m:MetadataObject)
WHERE toLower(replace(coalesce(m.meta_uuid,''),'-','')) = $guid_norm
  AND m.project_name = $project_name{scope.and_alias('m')}
{_adp_mo}
RETURN 'MetadataObject' AS kind, m.qualified_name AS qualified_name, m.config_name AS config_name, properties(m) AS props{_adp_col}
UNION
MATCH (m:MetadataObject)-[:HAS_ATTRIBUTE]->(a:Attribute)
WHERE toLower(replace(coalesce(a.meta_uuid,''),'-','')) = $guid_norm
  AND m.project_name = $project_name{scope.and_alias('m')}
{_adp_a}
RETURN 'Attribute' AS kind, a.qualified_name AS qualified_name, m.config_name AS config_name, properties(a) AS props{_adp_col}
UNION
MATCH (m:MetadataObject)-[:HAS_TABULAR_PART]->(t:TabularPart)
WHERE toLower(replace(coalesce(t.meta_uuid,''),'-','')) = $guid_norm
  AND m.project_name = $project_name{scope.and_alias('m')}
{_adp_t}
RETURN 'TabularPart' AS kind, t.qualified_name AS qualified_name, m.config_name AS config_name, properties(t) AS props{_adp_col}
UNION
MATCH (m:MetadataObject)-[:HAS_RESOURCE]->(r:Resource)
WHERE toLower(replace(coalesce(r.meta_uuid,''),'-','')) = $guid_norm
  AND m.project_name = $project_name{scope.and_alias('m')}
{_adp_r}
RETURN 'Resource' AS kind, r.qualified_name AS qualified_name, m.config_name AS config_name, properties(r) AS props{_adp_col}
UNION
MATCH (m:MetadataObject)-[:HAS_DIMENSION]->(d:Dimension)
WHERE toLower(replace(coalesce(d.meta_uuid,''),'-','')) = $guid_norm
  AND m.project_name = $project_name{scope.and_alias('m')}
{_adp_d}
RETURN 'Dimension' AS kind, d.qualified_name AS qualified_name, m.config_name AS config_name, properties(d) AS props{_adp_col}
UNION
MATCH (m:MetadataObject)-[:HAS_FORM]->(f:Form)
WHERE toLower(replace(coalesce(f.meta_uuid,''),'-','')) = $guid_norm
  AND m.project_name = $project_name{scope.and_alias('m')}
{_adp_f}
RETURN 'Form' AS kind, f.qualified_name AS qualified_name, m.config_name AS config_name, properties(f) AS props{_adp_col}
ORDER BY qualified_name
SKIP $offset LIMIT $limit
""".strip()
                    params_g: Dict[str, Any] = {"guid_norm": guid_norm, "offset": off, "limit": lim + 1, "project_prefix": project_prefix}
                    if config_name:
                        params_g["config_name"] = config_name
                    rows = _run_query(loader, resolve_cypher, params_g, pn)
                    if has_extensions:
                        rows = _strip_null_adoption(rows)
                    return _emit_props(rows, point=False)

                elif ref_type in ("qualified_name", "qualified_name_prefix"):
                    cfg_filter = "\n  AND n.config_name = $config_name" if config_name else ""
                    if ref_type == "qualified_name":
                        qn_filter = "AND toLower(n.qualified_name) = toLower($ref)"
                    else:
                        qn_filter = "AND n.qualified_name STARTS WITH $ref"
                    if has_extensions:
                        _adp_qn = f"""
OPTIONAL MATCH (cfg:Configuration {{project_name: $project_name, name: n.config_name}})
OPTIONAL MATCH (ext_n)-[:ADOPTED_FROM]->(n)
WHERE ext_n.qualified_name STARTS WITH $project_prefix
WITH n, cfg, collect(DISTINCT ext_n.config_name) AS _ext_names
OPTIONAL MATCH (n)-[:ADOPTED_FROM]->(base_n)
WHERE base_n.qualified_name STARTS WITH $project_prefix
WITH n, cfg, _ext_names, base_n.config_name AS _base_cn
WITH n,
     CASE
       WHEN cfg IS NULL THEN null
       WHEN NOT coalesce(cfg.is_extension, false) AND size(_ext_names) > 0
         THEN {{role: 'base', extension_config_names: _ext_names}}
       WHEN coalesce(cfg.is_extension, false) AND _base_cn IS NOT NULL
         THEN {{role: 'extension', base_config_name: _base_cn}}
       ELSE {{role: 'none'}}
     END AS adoption"""
                    else:
                        _adp_qn = ""
                    cypher = f"""
MATCH (n)
WHERE (n.project_name = $project_name OR n.qualified_name STARTS WITH $project_prefix)
  {qn_filter}{cfg_filter}
{_adp_qn}
RETURN properties(n) AS props, labels(n) AS node_labels,
  n.qualified_name AS qualified_name, coalesce(n.name,'') AS name,
  coalesce(n.config_name,'') AS config_name{_adp_col}
ORDER BY qualified_name
SKIP $offset LIMIT $limit
""".strip()
                    _is_prefix = ref_type == "qualified_name_prefix"
                    # Exact QN: accept full / config-relative / Category.Name / short ref by
                    # canonicalizing to a full project QN (config-relative needs config_name).
                    # Prefix stays raw — normalize_qn_ref resolves to whole nodes, not prefixes.
                    match_ref = ref
                    if not _is_prefix:
                        try:
                            match_ref = normalize_qn_ref(loader, ref, pn, config_name=config_name)
                        except ValueError:
                            match_ref = ref
                    # Exact QN is a point lookup: pin SKIP to 0 so a non-zero offset
                    # cannot skip the single matching row. Prefix stays pageable.
                    p2: Dict[str, Any] = {
                        "ref": match_ref, "project_prefix": project_prefix,
                        "offset": off if _is_prefix else 0,
                        "limit": (lim + 1) if _is_prefix else 1,
                    }
                    if config_name:
                        p2["config_name"] = config_name
                    rows = _run_query(loader, cypher, p2, pn)
                    if has_extensions:
                        rows = _strip_null_adoption(rows)
                    return _emit_props(rows, point=not _is_prefix)

                else:
                    cypher, params, point = _md_pointed_target_query(
                        ref_type, ref, owner_ref, with_props=True, has_extensions=has_extensions)
                    rows = _run_query(loader, cypher, params, pn)
                    if has_extensions:
                        rows = (_strip_null_interception(rows)
                                if ref_type == "form_event_action" else _strip_null_adoption(rows))
                    return _emit_props(rows, point=point)

            else:
                return f"Error: unknown mode='{mode}'."

        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.exception("Error in get_metadata_details")
            return f"Error: {e}"
    _patch_tool_defaults(get_metadata_details)
    mcp.tool()(get_metadata_details)


# ---------------------------------------------------------------------------
# Tool 10: get_form_structure
# ---------------------------------------------------------------------------

def _register_get_form_structure(mcp):
    def get_form_structure(
        object_ref: str,
        form_name: Optional[str] = None,
        sections: Optional[List[Literal[
            "controls", "events", "event_handlers", "attributes",
            "commands", "command_usages", "bindings",
        ]]] = None,
        element_name: Optional[str] = None,
        element_match: Optional[Literal["exact", "starts_with", "contains"]] = None,
        form_event_source: Optional[Literal["form", "controls", "all"]] = None,
        target_type: Optional[Literal["attribute", "dimension", "resource", "form_attribute", "metadata_object"]] = None,
        config: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        project_name: Optional[str] = None,
    ) -> str:
        """Get controls, events, handlers, commands, attributes, and bindings of a form.

object_ref identifies the metadata object or common form.
For regular object forms pass form_name, except bindings can omit form_name to scan all forms of the object.
For common forms form_name can be omitted.

sections selects what to return:
- controls: form controls tree.
- events: form/control events and assigned actions.
- event_handlers: routines assigned as event handlers.
- attributes: form attributes.
- commands: form commands.
- command_usages: controls/buttons that invoke form commands.
- bindings: controls bound to attributes, resources, dimensions, form attributes, or metadata objects.

element_name/element_match filter returned elements.
form_event_source filters events and handlers: form, controls, or all.
target_type filters bindings.
config scopes to one configuration or extension.
"""
        loader = _init_loader()
        if loader is None:
            return "Error: Neo4j database connection not available."
        try:
            pn = _resolve_project(project_name)
            config_name = resolve_config(loader, config, pn)
            scope = _scope(config_name)
            lim = clamp_limit(limit)
            off = clamp_offset(offset)

            has_extensions = bool(_run_query(
                loader,
                "MATCH (c:Configuration {project_name: $project_name, is_extension: true}) RETURN c LIMIT 1",
                {},
                pn,
            ))
            project_prefix = pn + "/"

            resolved = resolve_object_ref(loader, object_ref, pn, config_name)
            obj_name = resolved["name"]
            obj_cat = resolved["category_name"]
            is_common_form = (obj_cat == "ОбщиеФормы")
            em = (element_match or "exact").lower()

            object_qn = resolved.get("qualified_name", "")

            if is_common_form and form_name and form_name.lower() != obj_name.lower():
                return _fmt_dict(
                    {"context": {"object": obj_name, "category": obj_cat, "object_qn": object_qn},
                     "pages": {}},
                    apply_compact_refs=True, normalize_arrays_for_toon=True,
                )

            target_label_map = {
                "attribute": "Attribute",
                "dimension": "Dimension",
                "resource": "Resource",
                "form_attribute": "FormAttribute",
                "metadata_object": "MetadataObject",
            }
            tgt_label = target_label_map.get(target_type or "") if target_type else None

            # Normalize requested sections: preserve order, drop dups, validate.
            valid_sections = ("controls", "events", "event_handlers", "attributes",
                              "commands", "command_usages", "bindings")
            requested = sections or ["controls"]
            secs: List[str] = []
            for _s in requested:
                if _s not in valid_sections:
                    return f"Error: unknown section='{_s}'."
                if _s not in secs:
                    secs.append(_s)

            fetch_limit = lim + 1
            # Anchor object match on the resolved object_qn so context stays
            # authoritative and rows are homogeneous per object (safe to strip repeats).
            obj_match = "{qualified_name:$object_qn, project_name:$project_name}"
            params: Dict[str, Any] = {"object_qn": object_qn, "offset": off,
                                      "limit": fetch_limit, "project_prefix": project_prefix}

            # Build the authoritative response context header.
            _qn_parts = object_qn.split("/")
            ctx_config = config_name or (_qn_parts[1] if len(_qn_parts) >= 2 else None)
            ctx: Dict[str, Any] = {"object": obj_name, "category": obj_cat, "object_qn": object_qn}
            if ctx_config:
                ctx["config_name"] = ctx_config
            if is_common_form:
                ctx["form_name"] = obj_name
                ctx["form_qn"] = object_qn
            elif form_name:
                ctx["form_name"] = form_name
                _fq = _run_query(
                    loader,
                    f"MATCH (m:MetadataObject {obj_match})-[:HAS_FORM]->(f:Form)\n"
                    "WHERE toLower(f.name) = toLower($form_name)\n"
                    "RETURN f.qualified_name AS form_qn LIMIT 1",
                    {"object_qn": object_qn, "form_name": form_name},
                    pn,
                )
                if _fq:
                    ctx["form_qn"] = _fq[0].get("form_qn")
            else:
                # bindings across all forms of the resolved object.
                ctx["forms_scope"] = "all"

            section_rows: Dict[str, list] = {}
            for sec in secs:
                if sec == "bindings":
                    # bindings: form_name optional
                    tgt_filter = ""
                    if tgt_label:
                        params["tgt_label"] = tgt_label
                        tgt_filter = f"\nAND '{tgt_label}' IN labels(t)"
                    if is_common_form:
                        cypher = f"""
MATCH (m:MetadataObject {obj_match})-[:HAS_CONTROL]->(root:FormControl)
MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)-[r:BINDS_TO]->(t)
WHERE true{tgt_filter}
RETURN m.name AS form, m.qualified_name AS form_qn, m.config_name AS config_name,
  coalesce(fc.name,'') AS control, fc.qualified_name AS control_qn,
  head([l IN labels(t) WHERE l IN ['Attribute','Dimension','Resource','FormAttribute','MetadataObject']]) AS target_label,
  coalesce(t.name,'') AS target_name, t.qualified_name AS target_qn,
  coalesce(r.via,'') AS via
ORDER BY form, control, target_label, target_name
SKIP $offset LIMIT $limit
""".strip()
                    else:
                        form_filter = ""
                        if form_name:
                            params["form_name"] = form_name
                            form_filter = "WHERE toLower(f.name) = toLower($form_name)"
                        cypher = f"""
MATCH (m:MetadataObject {obj_match})-[:HAS_FORM]->(f:Form)
{form_filter}
MATCH (f)-[:HAS_CONTROL]->(root:FormControl)
MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)-[r:BINDS_TO]->(t)
WHERE true{tgt_filter}
RETURN f.name AS form, f.qualified_name AS form_qn, m.config_name AS config_name,
  coalesce(fc.name,'') AS control, fc.qualified_name AS control_qn,
  head([l IN labels(t) WHERE l IN ['Attribute','Dimension','Resource','FormAttribute','MetadataObject']]) AS target_label,
  coalesce(t.name,'') AS target_name, t.qualified_name AS target_qn,
  coalesce(r.via,'') AS via
ORDER BY form, control, target_label, target_name
SKIP $offset LIMIT $limit
""".strip()

                elif sec == "controls":
                    if not form_name and not is_common_form:
                        return "Error: form_name is required for sections=['controls']."
                    name_filter = ""
                    if element_name:
                        params["element_name"] = element_name
                        name_filter = f"\nAND {apply_match('fc.name', 'element_name', em)}"
                    _adp_col = ", adoption" if has_extensions else ""
                    if is_common_form:
                        _adp = _cf_child_adoption_block("fc", "FormControl", "m, fc, p") if has_extensions else ""
                        cypher = f"""
MATCH (m:MetadataObject {obj_match})-[:HAS_CONTROL]->(root:FormControl)
MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)
OPTIONAL MATCH (p:FormControl)-[:HAS_CHILD]->(fc)
WHERE true{name_filter}
{_adp}
RETURN DISTINCT coalesce(fc.name,'') AS name, fc.qualified_name AS qualified_name,
  m.config_name AS config_name, m.qualified_name AS owner_qn,
  coalesce(fc.`Тип`, fc.`ТипКонтрола`) AS type,
  coalesce(fc.`Идентификатор`,'') AS id,
  coalesce(p.name,'') AS parent, coalesce(p.`Идентификатор`,'') AS parent_id{_adp_col}
ORDER BY name, qualified_name
SKIP $offset LIMIT $limit
""".strip()
                    else:
                        if not form_name:
                            return "Error: form_name is required for sections=['controls']."
                        params["form_name"] = form_name
                        _adp = _form_child_adoption_block("fc", "FormControl", "m, f, fc, p") if has_extensions else ""
                        cypher = f"""
MATCH (m:MetadataObject {obj_match})-[:HAS_FORM]->(f:Form)
WHERE toLower(f.name) = toLower($form_name)
MATCH (f)-[:HAS_CONTROL]->(root:FormControl)
MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)
OPTIONAL MATCH (p:FormControl)-[:HAS_CHILD]->(fc)
WHERE true{name_filter}
{_adp}
RETURN DISTINCT coalesce(fc.name,'') AS name, fc.qualified_name AS qualified_name,
  m.config_name AS config_name, f.qualified_name AS owner_qn,
  coalesce(fc.`Тип`, fc.`ТипКонтрола`) AS type,
  coalesce(fc.`Идентификатор`,'') AS id,
  coalesce(p.name,'') AS parent, coalesce(p.`Идентификатор`,'') AS parent_id{_adp_col}
ORDER BY name, qualified_name
SKIP $offset LIMIT $limit
""".strip()

                elif sec == "events":
                    if not form_name and not is_common_form:
                        return "Error: form_name is required for sections=['events']."
                    if not is_common_form:
                        params["form_name"] = form_name
                    src = (form_event_source or "all").lower()
                    name_filter = ""
                    if element_name:
                        params["element_name"] = element_name
                        name_filter = f"AND {apply_match('e.name', 'element_name', em)}"

                    _adp_col = ", adoption" if has_extensions else ""
                    if is_common_form:
                        if src == "form":
                            if has_extensions:
                                _adp = _cf_child_adoption_block("e", "FormEvent", "m, e, actions")
                                cypher = f"""
MATCH (m:MetadataObject {obj_match})-[:HAS_EVENT]->(e:FormEvent)
WHERE true {name_filter}
WITH m, e, [(e)-[:HAS_EVENT_ACTION]->(a:FormEventAction) | {{call_type: a.call_type, handler_name: a.handler_name}}] AS actions
{_adp}
RETURN coalesce(e.name,'') AS event, e.qualified_name AS qualified_name,
  actions, m.config_name AS config_name, 'form' AS source, '' AS source_qn{_adp_col}
ORDER BY event SKIP $offset LIMIT $limit
""".strip()
                            else:
                                cypher = f"""
MATCH (m:MetadataObject {obj_match})-[:HAS_EVENT]->(e:FormEvent)
WHERE true {name_filter}
OPTIONAL MATCH (e)-[:HAS_EVENT_ACTION]->(a:FormEventAction)
RETURN coalesce(e.name,'') AS event, e.qualified_name AS qualified_name,
  collect({{call_type: a.call_type, handler_name: a.handler_name}}) AS actions,
  m.config_name AS config_name, 'form' AS source, '' AS source_qn
ORDER BY event SKIP $offset LIMIT $limit
""".strip()
                        elif src == "controls":
                            if has_extensions:
                                _adp = _cf_child_adoption_block("e", "FormEvent", "m, fc, e, actions")
                                cypher = f"""
MATCH (m:MetadataObject {obj_match})-[:HAS_CONTROL]->(root:FormControl)
MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)-[:HAS_EVENT]->(e:FormEvent)
WHERE true {name_filter}
WITH m, fc, e, [(e)-[:HAS_EVENT_ACTION]->(a:FormEventAction) | {{call_type: a.call_type, handler_name: a.handler_name}}] AS actions
{_adp}
RETURN DISTINCT coalesce(e.name,'') AS event, e.qualified_name AS qualified_name,
  actions, m.config_name AS config_name, coalesce(fc.name,'') AS source, fc.qualified_name AS source_qn{_adp_col}
ORDER BY event SKIP $offset LIMIT $limit
""".strip()
                            else:
                                cypher = f"""
MATCH (m:MetadataObject {obj_match})-[:HAS_CONTROL]->(root:FormControl)
MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)-[:HAS_EVENT]->(e:FormEvent)
WHERE true {name_filter}
OPTIONAL MATCH (e)-[:HAS_EVENT_ACTION]->(a:FormEventAction)
RETURN DISTINCT coalesce(e.name,'') AS event, e.qualified_name AS qualified_name,
  collect({{call_type: a.call_type, handler_name: a.handler_name}}) AS actions,
  m.config_name AS config_name, coalesce(fc.name,'') AS source, fc.qualified_name AS source_qn
ORDER BY event SKIP $offset LIMIT $limit
""".strip()
                        else:  # all
                            if has_extensions:
                                _adp_f = _cf_child_adoption_block("e", "FormEvent", "m, e, actions")
                                _adp_c = _cf_child_adoption_block("e", "FormEvent", "m, fc, e, actions")
                                cypher = f"""
CALL {{
  MATCH (m:MetadataObject {obj_match})-[:HAS_EVENT]->(e:FormEvent)
  WHERE true {name_filter}
  WITH m, e, [(e)-[:HAS_EVENT_ACTION]->(a:FormEventAction) | {{call_type: a.call_type, handler_name: a.handler_name}}] AS actions
  {_adp_f}
  RETURN coalesce(e.name,'') AS event, e.qualified_name AS qualified_name,
    actions, m.config_name AS config_name, 'form' AS source, '' AS source_qn, adoption
  UNION
  MATCH (m:MetadataObject {obj_match})-[:HAS_CONTROL]->(root:FormControl)
  MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)-[:HAS_EVENT]->(e:FormEvent)
  WHERE true {name_filter}
  WITH m, fc, e, [(e)-[:HAS_EVENT_ACTION]->(a:FormEventAction) | {{call_type: a.call_type, handler_name: a.handler_name}}] AS actions
  {_adp_c}
  RETURN DISTINCT coalesce(e.name,'') AS event, e.qualified_name AS qualified_name,
    actions, m.config_name AS config_name, coalesce(fc.name,'') AS source, fc.qualified_name AS source_qn, adoption
}}
RETURN event, qualified_name, actions, config_name, source, source_qn, adoption
ORDER BY event SKIP $offset LIMIT $limit
""".strip()
                            else:
                                cypher = f"""
CALL {{
  MATCH (m:MetadataObject {obj_match})-[:HAS_EVENT]->(e:FormEvent)
  WHERE true {name_filter}
  OPTIONAL MATCH (e)-[:HAS_EVENT_ACTION]->(a:FormEventAction)
  RETURN coalesce(e.name,'') AS event, e.qualified_name AS qualified_name,
    collect({{call_type: a.call_type, handler_name: a.handler_name}}) AS actions,
    m.config_name AS config_name, 'form' AS source, '' AS source_qn
  UNION
  MATCH (m:MetadataObject {obj_match})-[:HAS_CONTROL]->(root:FormControl)
  MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)-[:HAS_EVENT]->(e:FormEvent)
  WHERE true {name_filter}
  OPTIONAL MATCH (e)-[:HAS_EVENT_ACTION]->(a:FormEventAction)
  RETURN DISTINCT coalesce(e.name,'') AS event, e.qualified_name AS qualified_name,
    collect({{call_type: a.call_type, handler_name: a.handler_name}}) AS actions,
    m.config_name AS config_name, coalesce(fc.name,'') AS source, fc.qualified_name AS source_qn
}}
RETURN event, qualified_name, actions, config_name, source, source_qn
ORDER BY event SKIP $offset LIMIT $limit
""".strip()
                    elif src == "form":
                        if has_extensions:
                            _adp = _form_child_adoption_block("e", "FormEvent", "m, f, e, actions")
                            cypher = f"""
MATCH (m:MetadataObject {obj_match})-[:HAS_FORM]->(f:Form)
WHERE toLower(f.name) = toLower($form_name)
MATCH (f)-[:HAS_EVENT]->(e:FormEvent)
WHERE true {name_filter}
WITH m, f, e, [(e)-[:HAS_EVENT_ACTION]->(a:FormEventAction) | {{call_type: a.call_type, handler_name: a.handler_name}}] AS actions
{_adp}
RETURN coalesce(e.name,'') AS event, e.qualified_name AS qualified_name,
  actions, m.config_name AS config_name, 'form' AS source, '' AS source_qn{_adp_col}
ORDER BY event SKIP $offset LIMIT $limit
""".strip()
                        else:
                            cypher = f"""
MATCH (m:MetadataObject {obj_match})-[:HAS_FORM]->(f:Form)
WHERE toLower(f.name) = toLower($form_name)
MATCH (f)-[:HAS_EVENT]->(e:FormEvent)
WHERE true {name_filter}
OPTIONAL MATCH (e)-[:HAS_EVENT_ACTION]->(a:FormEventAction)
RETURN coalesce(e.name,'') AS event, e.qualified_name AS qualified_name,
  collect({{call_type: a.call_type, handler_name: a.handler_name}}) AS actions,
  m.config_name AS config_name, 'form' AS source, '' AS source_qn
ORDER BY event SKIP $offset LIMIT $limit
""".strip()
                    elif src == "controls":
                        if has_extensions:
                            _adp = _form_child_adoption_block("e", "FormEvent", "m, f, fc, e, actions")
                            cypher = f"""
MATCH (m:MetadataObject {obj_match})-[:HAS_FORM]->(f:Form)
WHERE toLower(f.name) = toLower($form_name)
MATCH (f)-[:HAS_CONTROL]->(root:FormControl)
MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)-[:HAS_EVENT]->(e:FormEvent)
WHERE true {name_filter}
WITH m, f, fc, e, [(e)-[:HAS_EVENT_ACTION]->(a:FormEventAction) | {{call_type: a.call_type, handler_name: a.handler_name}}] AS actions
{_adp}
RETURN DISTINCT coalesce(e.name,'') AS event, e.qualified_name AS qualified_name,
  actions, m.config_name AS config_name, coalesce(fc.name,'') AS source, fc.qualified_name AS source_qn{_adp_col}
ORDER BY event SKIP $offset LIMIT $limit
""".strip()
                        else:
                            cypher = f"""
MATCH (m:MetadataObject {obj_match})-[:HAS_FORM]->(f:Form)
WHERE toLower(f.name) = toLower($form_name)
MATCH (f)-[:HAS_CONTROL]->(root:FormControl)
MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)-[:HAS_EVENT]->(e:FormEvent)
WHERE true {name_filter}
OPTIONAL MATCH (e)-[:HAS_EVENT_ACTION]->(a:FormEventAction)
RETURN DISTINCT coalesce(e.name,'') AS event, e.qualified_name AS qualified_name,
  collect({{call_type: a.call_type, handler_name: a.handler_name}}) AS actions,
  m.config_name AS config_name, coalesce(fc.name,'') AS source, fc.qualified_name AS source_qn
ORDER BY event SKIP $offset LIMIT $limit
""".strip()
                    else:  # all
                        if has_extensions:
                            _adp_f = _form_child_adoption_block("e", "FormEvent", "m, f, e, actions")
                            _adp_c = _form_child_adoption_block("e", "FormEvent", "m, f, fc, e, actions")
                            cypher = f"""
CALL {{
  MATCH (m:MetadataObject {obj_match})-[:HAS_FORM]->(f:Form)
  WHERE toLower(f.name) = toLower($form_name)
  MATCH (f)-[:HAS_EVENT]->(e:FormEvent)
  WHERE true {name_filter}
  WITH m, f, e, [(e)-[:HAS_EVENT_ACTION]->(a:FormEventAction) | {{call_type: a.call_type, handler_name: a.handler_name}}] AS actions
  {_adp_f}
  RETURN coalesce(e.name,'') AS event, e.qualified_name AS qualified_name,
    actions, m.config_name AS config_name, 'form' AS source, '' AS source_qn, adoption
  UNION
  MATCH (m:MetadataObject {obj_match})-[:HAS_FORM]->(f:Form)
  WHERE toLower(f.name) = toLower($form_name)
  MATCH (f)-[:HAS_CONTROL]->(root:FormControl)
  MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)-[:HAS_EVENT]->(e:FormEvent)
  WHERE true {name_filter}
  WITH m, f, fc, e, [(e)-[:HAS_EVENT_ACTION]->(a:FormEventAction) | {{call_type: a.call_type, handler_name: a.handler_name}}] AS actions
  {_adp_c}
  RETURN DISTINCT coalesce(e.name,'') AS event, e.qualified_name AS qualified_name,
    actions, m.config_name AS config_name, coalesce(fc.name,'') AS source, fc.qualified_name AS source_qn, adoption
}}
RETURN event, qualified_name, actions, config_name, source, source_qn, adoption
ORDER BY event SKIP $offset LIMIT $limit
""".strip()
                        else:
                            cypher = f"""
CALL {{
  MATCH (m:MetadataObject {obj_match})-[:HAS_FORM]->(f:Form)
  WHERE toLower(f.name) = toLower($form_name)
  MATCH (f)-[:HAS_EVENT]->(e:FormEvent)
  WHERE true {name_filter}
  OPTIONAL MATCH (e)-[:HAS_EVENT_ACTION]->(a:FormEventAction)
  RETURN coalesce(e.name,'') AS event, e.qualified_name AS qualified_name,
    collect({{call_type: a.call_type, handler_name: a.handler_name}}) AS actions,
    m.config_name AS config_name, 'form' AS source, '' AS source_qn
  UNION
  MATCH (m:MetadataObject {obj_match})-[:HAS_FORM]->(f:Form)
  WHERE toLower(f.name) = toLower($form_name)
  MATCH (f)-[:HAS_CONTROL]->(root:FormControl)
  MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)-[:HAS_EVENT]->(e:FormEvent)
  WHERE true {name_filter}
  OPTIONAL MATCH (e)-[:HAS_EVENT_ACTION]->(a:FormEventAction)
  RETURN DISTINCT coalesce(e.name,'') AS event, e.qualified_name AS qualified_name,
    collect({{call_type: a.call_type, handler_name: a.handler_name}}) AS actions,
    m.config_name AS config_name, coalesce(fc.name,'') AS source, fc.qualified_name AS source_qn
}}
RETURN event, qualified_name, actions, config_name, source, source_qn
ORDER BY event SKIP $offset LIMIT $limit
""".strip()

                elif sec == "event_handlers":
                    if not form_name and not is_common_form:
                        return "Error: form_name is required for sections=['event_handlers']."
                    if not is_common_form:
                        params["form_name"] = form_name
                    src = (form_event_source or "all").lower()
                    name_filter = ""
                    if element_name:
                        params["element_name"] = element_name
                        name_filter = f"AND {apply_match('e.name', 'element_name', em)}"

                    if is_common_form:
                        if src == "form":
                            cypher = f"""
MATCH (m:MetadataObject {obj_match})-[:HAS_EVENT]->(e:FormEvent)-[:HAS_EVENT_ACTION]->(a:FormEventAction)-[:HAS_HANDLER]->(r:Routine)
WHERE true {name_filter}
RETURN coalesce(e.name,'') AS event, e.qualified_name AS event_qn,
  a.call_type AS call_type, a.handler_name AS handler_name,
  r.id AS routine_id, coalesce(r.name,'') AS routine, coalesce(r.owner_qn,'') AS routine_owner_qn,
  m.config_name AS config_name
ORDER BY event SKIP $offset LIMIT $limit
""".strip()
                        elif src == "controls":
                            cypher = f"""
MATCH (m:MetadataObject {obj_match})-[:HAS_CONTROL]->(root:FormControl)
MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)-[:HAS_EVENT]->(e:FormEvent)-[:HAS_EVENT_ACTION]->(a:FormEventAction)-[:HAS_HANDLER]->(r:Routine)
WHERE true {name_filter}
RETURN DISTINCT coalesce(e.name,'') AS event, e.qualified_name AS event_qn,
  a.call_type AS call_type, a.handler_name AS handler_name,
  r.id AS routine_id, coalesce(r.name,'') AS routine, coalesce(r.owner_qn,'') AS routine_owner_qn,
  m.config_name AS config_name, coalesce(fc.name,'ControlEvent') AS source_kind
ORDER BY event SKIP $offset LIMIT $limit
""".strip()
                        else:
                            cypher = f"""
CALL {{
  MATCH (m:MetadataObject {obj_match})-[:HAS_EVENT]->(e:FormEvent)-[:HAS_EVENT_ACTION]->(a:FormEventAction)-[:HAS_HANDLER]->(r:Routine)
  WHERE true {name_filter}
  RETURN coalesce(e.name,'') AS event, e.qualified_name AS event_qn,
    a.call_type AS call_type, a.handler_name AS handler_name,
    r.id AS routine_id, coalesce(r.name,'') AS routine, coalesce(r.owner_qn,'') AS routine_owner_qn,
    m.config_name AS config_name, 'Form' AS source_kind
  UNION
  MATCH (m:MetadataObject {obj_match})-[:HAS_CONTROL]->(root:FormControl)
  MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)-[:HAS_EVENT]->(e:FormEvent)-[:HAS_EVENT_ACTION]->(a:FormEventAction)-[:HAS_HANDLER]->(r:Routine)
  WHERE true {name_filter}
  RETURN DISTINCT coalesce(e.name,'') AS event, e.qualified_name AS event_qn,
    a.call_type AS call_type, a.handler_name AS handler_name,
    r.id AS routine_id, coalesce(r.name,'') AS routine, coalesce(r.owner_qn,'') AS routine_owner_qn,
    m.config_name AS config_name, coalesce(fc.name,'ControlEvent') AS source_kind
}}
RETURN event, event_qn, call_type, handler_name, routine_id, routine, routine_owner_qn, config_name, source_kind
ORDER BY event SKIP $offset LIMIT $limit
""".strip()
                    elif src == "form":
                        evt_match = f"""
MATCH (m:MetadataObject {obj_match})-[:HAS_FORM]->(f:Form)
WHERE toLower(f.name) = toLower($form_name)
MATCH (f)-[:HAS_EVENT]->(e:FormEvent)-[:HAS_EVENT_ACTION]->(a:FormEventAction)-[:HAS_HANDLER]->(r:Routine)
WHERE true {name_filter}
RETURN coalesce(e.name,'') AS event, e.qualified_name AS event_qn,
  a.call_type AS call_type, a.handler_name AS handler_name,
  r.id AS routine_id, coalesce(r.name,'') AS routine, coalesce(r.owner_qn,'') AS routine_owner_qn,
  m.config_name AS config_name
ORDER BY event SKIP $offset LIMIT $limit
""".strip()
                        cypher = evt_match
                    elif src == "controls":
                        cypher = f"""
MATCH (m:MetadataObject {obj_match})-[:HAS_FORM]->(f:Form)
WHERE toLower(f.name) = toLower($form_name)
MATCH (f)-[:HAS_CONTROL]->(root:FormControl)
MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)-[:HAS_EVENT]->(e:FormEvent)-[:HAS_EVENT_ACTION]->(a:FormEventAction)-[:HAS_HANDLER]->(r:Routine)
WHERE true {name_filter}
RETURN DISTINCT coalesce(e.name,'') AS event, e.qualified_name AS event_qn,
  a.call_type AS call_type, a.handler_name AS handler_name,
  r.id AS routine_id, coalesce(r.name,'') AS routine, coalesce(r.owner_qn,'') AS routine_owner_qn,
  m.config_name AS config_name, coalesce(fc.name,'ControlEvent') AS source_kind
ORDER BY event SKIP $offset LIMIT $limit
""".strip()
                    else:
                        cypher = f"""
CALL {{
  MATCH (m:MetadataObject {obj_match})-[:HAS_FORM]->(f:Form)
  WHERE toLower(f.name) = toLower($form_name)
  MATCH (f)-[:HAS_EVENT]->(e:FormEvent)-[:HAS_EVENT_ACTION]->(a:FormEventAction)-[:HAS_HANDLER]->(r:Routine)
  WHERE true {name_filter}
  RETURN coalesce(e.name,'') AS event, e.qualified_name AS event_qn,
    a.call_type AS call_type, a.handler_name AS handler_name,
    r.id AS routine_id, coalesce(r.name,'') AS routine, coalesce(r.owner_qn,'') AS routine_owner_qn,
    m.config_name AS config_name, 'Form' AS source_kind
  UNION
  MATCH (m:MetadataObject {obj_match})-[:HAS_FORM]->(f:Form)
  WHERE toLower(f.name) = toLower($form_name)
  MATCH (f)-[:HAS_CONTROL]->(root:FormControl)
  MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)-[:HAS_EVENT]->(e:FormEvent)-[:HAS_EVENT_ACTION]->(a:FormEventAction)-[:HAS_HANDLER]->(r:Routine)
  WHERE true {name_filter}
  RETURN DISTINCT coalesce(e.name,'') AS event, e.qualified_name AS event_qn,
    a.call_type AS call_type, a.handler_name AS handler_name,
    r.id AS routine_id, coalesce(r.name,'') AS routine, coalesce(r.owner_qn,'') AS routine_owner_qn,
    m.config_name AS config_name, coalesce(fc.name,'ControlEvent') AS source_kind
}}
RETURN event, event_qn, call_type, handler_name, routine_id, routine, routine_owner_qn, config_name, source_kind
ORDER BY event SKIP $offset LIMIT $limit
""".strip()

                elif sec == "attributes":
                    if not form_name and not is_common_form:
                        return "Error: form_name is required for sections=['attributes']."
                    if not is_common_form:
                        params["form_name"] = form_name
                    name_filter = ""
                    if element_name:
                        params["element_name"] = element_name
                        name_filter = f"AND {apply_match('fa.name', 'element_name', em)}"
                    _adp_col = ", adoption" if has_extensions else ""
                    if is_common_form:
                        _adp = _cf_child_adoption_block("fa", "FormAttribute", "m, fa") if has_extensions else ""
                        cypher = f"""
MATCH (m:MetadataObject {obj_match})-[:HAS_FORM_ATTRIBUTE]->(fa:FormAttribute)
WHERE true {name_filter}
{_adp}
RETURN coalesce(fa.name,'') AS name, fa.qualified_name AS qualified_name,
  m.config_name AS config_name{_adp_col}
ORDER BY name SKIP $offset LIMIT $limit
""".strip()
                    else:
                        _adp = _form_child_adoption_block("fa", "FormAttribute", "m, f, fa") if has_extensions else ""
                        cypher = f"""
MATCH (m:MetadataObject {obj_match})-[:HAS_FORM]->(f:Form)
WHERE toLower(f.name) = toLower($form_name)
MATCH (f)-[:HAS_FORM_ATTRIBUTE]->(fa:FormAttribute)
WHERE true {name_filter}
{_adp}
RETURN coalesce(fa.name,'') AS name, fa.qualified_name AS qualified_name,
  m.config_name AS config_name{_adp_col}
ORDER BY name SKIP $offset LIMIT $limit
""".strip()

                elif sec == "commands":
                    if not form_name and not is_common_form:
                        return "Error: form_name is required for sections=['commands']."
                    if not is_common_form:
                        params["form_name"] = form_name
                    name_filter = ""
                    if element_name:
                        params["element_name"] = element_name
                        name_filter = f"AND {apply_match('c.name', 'element_name', em)}"
                    _adp_col = ", adoption" if has_extensions else ""
                    if is_common_form:
                        _adp = _cf_child_adoption_block("c", "Command", "m, c") if has_extensions else ""
                        cypher = f"""
MATCH (m:MetadataObject {obj_match})-[:HAS_COMMAND]->(c:Command)
WHERE true {name_filter}
{_adp}
RETURN coalesce(c.name,'') AS name, c.qualified_name AS qualified_name,
  m.config_name AS config_name{_adp_col}
ORDER BY name SKIP $offset LIMIT $limit
""".strip()
                    else:
                        _adp = _form_child_adoption_block("c", "Command", "m, f, c") if has_extensions else ""
                        cypher = f"""
MATCH (m:MetadataObject {obj_match})-[:HAS_FORM]->(f:Form)
WHERE toLower(f.name) = toLower($form_name)
MATCH (f)-[:HAS_COMMAND]->(c:Command)
WHERE true {name_filter}
{_adp}
RETURN coalesce(c.name,'') AS name, c.qualified_name AS qualified_name,
  m.config_name AS config_name{_adp_col}
ORDER BY name SKIP $offset LIMIT $limit
""".strip()

                elif sec == "command_usages":
                    if not form_name and not is_common_form:
                        return "Error: form_name is required for sections=['command_usages']."
                    if not is_common_form:
                        params["form_name"] = form_name
                    name_filter = ""
                    if element_name:
                        params["element_name"] = element_name
                        name_filter = f"AND {apply_match('c.name', 'element_name', em)}"
                    if is_common_form:
                        cypher = f"""
MATCH (m:MetadataObject {obj_match})-[:HAS_CONTROL]->(root:FormControl)
MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)-[lnk:LINKS_TO_COMMAND]->(c:Command)
WHERE true {name_filter}
RETURN coalesce(fc.name,'') AS control, fc.qualified_name AS control_qn,
  coalesce(lnk.`Идентификатор`,'') AS button_id,
  coalesce(lnk.`Представление`,'') AS button_name,
  coalesce(c.name,'') AS command, c.qualified_name AS command_qn,
  m.config_name AS config_name
ORDER BY control, command SKIP $offset LIMIT $limit
""".strip()
                    else:
                        cypher = f"""
MATCH (m:MetadataObject {obj_match})-[:HAS_FORM]->(f:Form)
WHERE toLower(f.name) = toLower($form_name)
MATCH (f)-[:HAS_CONTROL]->(root:FormControl)
MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)-[lnk:LINKS_TO_COMMAND]->(c:Command)
WHERE true {name_filter}
RETURN coalesce(fc.name,'') AS control, fc.qualified_name AS control_qn,
  coalesce(lnk.`Идентификатор`,'') AS button_id,
  coalesce(lnk.`Представление`,'') AS button_name,
  coalesce(c.name,'') AS command, c.qualified_name AS command_qn,
  m.config_name AS config_name
ORDER BY control, command SKIP $offset LIMIT $limit
""".strip()

                else:
                    return f"Error: unknown section='{sec}'."

                rows = _run_query(loader, cypher, params, pn)
                if has_extensions and sec in ("controls", "attributes", "commands", "events"):
                    rows = _strip_null_adoption(rows)
                section_rows[sec] = rows

            shaped = _shape_get_form_structure_result(section_rows, context=ctx, lim=lim, off=off)
            return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.exception("Error in get_form_structure")
            return f"Error: {e}"
    _patch_tool_defaults(get_form_structure)
    mcp.tool()(get_form_structure)


# ---------------------------------------------------------------------------
# Tool 11: find_form_links
# ---------------------------------------------------------------------------

def _register_find_form_links(mcp):
    def find_form_links(
        mode: Literal["controls_bound_to", "events_handled_by_routine"],
        binding_target: Optional[str] = None,
        target_type: Optional[Literal["attribute", "dimension", "resource", "form_attribute", "metadata_object"]] = None,
        binding_target_match: Optional[Literal["exact", "starts_with", "contains"]] = None,
        routine_ref: Optional[str] = None,
        routine_owner_ref: Optional[str] = None,
        config: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        project_name: Optional[str] = None,
    ) -> str:
        """Find form-control bindings and form/control event handlers.

Modes:
- controls_bound_to: find controls bound to a metadata target by name; binding_target required.
  target_type narrows the target kind; binding_target_match controls name matching.
- events_handled_by_routine: find form/control events handled by a BSL routine; routine_ref required.
  routine_ref accepts routine id or routine name; routine_owner_ref narrows routine-name search.

Use config to scope to one configuration or extension.
"""
        loader = _init_search_ready_loader()
        if loader is None:
            return ("Error: Neo4j database connection or BSL routine search index "
                    "not available yet.")
        try:
            pn = _resolve_project(project_name)
            config_name = resolve_config(loader, config, pn)
            scope = _scope(config_name)
            lim = clamp_limit(limit)
            off = clamp_offset(offset)
            # Fetch lim + 1 so the shaper can derive page.has_more explicitly.
            params: Dict[str, Any] = {"offset": off, "limit": lim + 1}
            if config_name:
                params["config_name"] = config_name

            target_label_map = {
                "attribute": "Attribute", "dimension": "Dimension",
                "resource": "Resource", "form_attribute": "FormAttribute",
                "metadata_object": "MetadataObject",
            }

            if mode == "controls_bound_to":
                if not binding_target or not binding_target.strip():
                    return "Error: binding_target is required for mode='controls_bound_to'."
                params["binding_target"] = binding_target.strip()
                btm = (binding_target_match or "exact").lower()
                tgt_label = target_label_map.get(target_type or "") if target_type else None
                tgt_filter = f"\nAND '{tgt_label}' IN labels(t)" if tgt_label else ""
                name_cond = apply_match("coalesce(t.name,'')", "binding_target", btm)
                cypher = f"""
MATCH (fc:FormControl)-[r:BINDS_TO]->(t)
WHERE {name_cond}{tgt_filter}
MATCH (f:Form)-[:HAS_CONTROL]->(root:FormControl)
MATCH (root)-[:HAS_CHILD*0..]->(fc)
MATCH (m:MetadataObject)-[:HAS_FORM]->(f)
WHERE m.project_name = $project_name{scope.and_alias('m')}
RETURN m.name AS object, f.name AS form, m.config_name AS config_name,
  coalesce(fc.name,'') AS control, fc.qualified_name AS control_qn,
  head([l IN labels(t) WHERE l IN ['Attribute','Dimension','Resource','FormAttribute','MetadataObject']]) AS target_label,
  coalesce(t.name,'') AS target_name, t.qualified_name AS target_qn,
  coalesce(r.via,'') AS via
ORDER BY object, form, control
SKIP $offset LIMIT $limit
""".strip()

            elif mode == "events_handled_by_routine":
                if not routine_ref or not routine_ref.strip():
                    return "Error: routine_ref is required for mode='events_handled_by_routine'."
                rref = routine_ref.strip()
                # Routine ids are 40-hex sha1 digests; UUID kept for compatibility.
                _routine_id_re = re.compile(
                    r'^(?:[0-9a-f]{40}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$',
                    re.I,
                )
                if _routine_id_re.match(rref):
                    params["routine_id"] = rref
                    routine_filter = "r.id = $routine_id"
                else:
                    params["routine_name"] = rref
                    routine_filter = "r.name_norm = toLower($routine_name)"
                    if routine_owner_ref and routine_owner_ref.strip():
                        owner_qn = normalize_qn_ref(loader, routine_owner_ref.strip(), pn, config_name)
                        params["owner_qn"] = owner_qn
                        routine_filter += " AND r.owner_qn = $owner_qn"

                cypher = f"""
CALL {{
  MATCH (f:Form)-[:HAS_EVENT]->(e:FormEvent)-[:HAS_EVENT_ACTION]->(a:FormEventAction)-[:HAS_HANDLER]->(r:Routine)
  WHERE {routine_filter}
  MATCH (m:MetadataObject)-[:HAS_FORM]->(f)
  WHERE m.project_name = $project_name{scope.and_alias('m')}
  RETURN m.name AS object, f.name AS form, 'Form' AS source,
    coalesce(e.name,'') AS event, a.call_type AS call_type, r.id AS routine_id,
    coalesce(r.name,'') AS routine, coalesce(r.owner_qn,'') AS routine_owner_qn,
    m.config_name AS config_name
  UNION
  MATCH (f:Form)-[:HAS_CONTROL]->(root:FormControl)
  MATCH (root)-[:HAS_CHILD*0..]->(fc:FormControl)-[:HAS_EVENT]->(e:FormEvent)-[:HAS_EVENT_ACTION]->(a:FormEventAction)-[:HAS_HANDLER]->(r:Routine)
  WHERE {routine_filter}
  MATCH (m:MetadataObject)-[:HAS_FORM]->(f)
  WHERE m.project_name = $project_name{scope.and_alias('m')}
  RETURN DISTINCT m.name AS object, f.name AS form,
    coalesce(fc.name,'ControlEvent') AS source,
    coalesce(e.name,'') AS event, a.call_type AS call_type, r.id AS routine_id,
    coalesce(r.name,'') AS routine, coalesce(r.owner_qn,'') AS routine_owner_qn,
    m.config_name AS config_name
}}
RETURN object, form, source, event, call_type, routine_id, routine, routine_owner_qn, config_name
ORDER BY object, form, source, event
SKIP $offset LIMIT $limit
""".strip()

            else:
                return f"Error: unknown mode='{mode}'."

            results = _run_query(loader, cypher, params, pn)
            shaped = _shape_find_form_links_result(results, lim=lim, off=off)
            return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.exception("Error in find_form_links")
            return f"Error: {e}"
    _patch_tool_defaults(find_form_links)
    mcp.tool()(find_form_links)


# ---------------------------------------------------------------------------
# Tool 12: get_event_subscriptions
# ---------------------------------------------------------------------------

def _register_get_event_subscriptions(mcp):
    def get_event_subscriptions(
        mode: Literal["list", "of_object", "sources", "handlers"],
        source_object: Optional[str] = None,
        source_category: Optional[str] = None,
        subscription_ref: Optional[str] = None,
        subscription_match: Optional[Literal["exact", "starts_with", "contains"]] = None,
        config: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        project_name: Optional[str] = None,
    ) -> str:
        """Query 1C event subscriptions (ПодпискиНаСобытия), their sources, and handler routines.

Modes:
- list: list event subscriptions.
- of_object: find subscriptions reacting to events of source_object; source_category narrows the object type.
- sources: list source objects of one subscription; subscription_ref required.
- handlers: list BSL routines used as subscription handlers; filters are optional.

subscription_ref accepts a subscription name, pattern, or qualified_name.
subscription_match controls subscription_ref comparison in sources/handlers.
Use config to scope to one configuration or extension.
"""
        loader = _init_loader()
        if loader is None:
            return "Error: Neo4j database connection not available."
        try:
            pn = _resolve_project(project_name)
            config_name = resolve_config(loader, config, pn)
            scope = _scope(config_name)
            lim = clamp_limit(limit)
            off = clamp_offset(offset)
            params: Dict[str, Any] = {"offset": off, "limit": lim + 1}
            if config_name:
                params["config_name"] = config_name

            if subscription_match is not None and mode in ("list", "of_object"):
                return "Error: subscription_match is supported only for modes 'sources' and 'handlers'."

            sm = (subscription_match or "exact").lower()

            has_extensions = bool(_run_query(
                loader,
                "MATCH (c:Configuration {project_name: $project_name, is_extension: true}) RETURN c LIMIT 1",
                {},
                pn,
            ))
            adoption_col = ", adoption" if has_extensions else ""

            if mode == "list":
                _adp = _owner_adoption_block(parent_var="es") if has_extensions else ""
                cypher = f"""
MATCH (es:MetadataObject {{category_name:'ПодпискиНаСобытия', project_name:$project_name{scope.metadata_map}}})
{_adp}
RETURN es.name AS subscription, es.config_name AS config_name, es.qualified_name AS qualified_name,
  coalesce(es.`Событие`, es.event_en, '') AS event,
  coalesce(es.`Обработчик`, es.handler_en, '') AS handler{adoption_col}
ORDER BY subscription SKIP $offset LIMIT $limit
""".strip()

            elif mode == "of_object":
                if not source_object or not source_object.strip():
                    return "Error: source_object is required for mode='of_object'."
                params["source_object"] = source_object.strip()
                cat_filter = ""
                if source_category and source_category.strip():
                    params["source_cat"] = source_category.strip()
                    cat_filter = "AND toLower(m.category_name) = toLower($source_cat)"
                _adp = _owner_adoption_block(carry_vars="m", parent_var="es") if has_extensions else ""
                cypher = f"""
MATCH (m:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
WHERE toLower(m.name) = toLower($source_object) {cat_filter}
MATCH (m)-[:HAS_EVENT_SUBSCRIPTION]->(es:MetadataObject)
WHERE es.category_name = 'ПодпискиНаСобытия'
{_adp}
RETURN es.name AS subscription, es.config_name AS config_name, es.qualified_name AS qualified_name,
  coalesce(es.`Событие`, es.event_en, '') AS event,
  m.name AS source_object, m.category_name AS source_category, m.qualified_name AS source_qn{adoption_col}
ORDER BY subscription SKIP $offset LIMIT $limit
""".strip()

            elif mode == "sources":
                if not subscription_ref or not subscription_ref.strip():
                    return "Error: subscription_ref is required for mode='sources'."
                sref = subscription_ref.strip()
                if "/" in sref or config_name:
                    norm_sub = normalize_qn_ref(loader, sref, pn, config_name)
                    params["sub_qn"] = norm_sub
                    sub_filter = "es.qualified_name = $sub_qn"
                else:
                    params["sub_name"] = sref
                    sub_filter = f"{apply_match('es.name', 'sub_name', sm)}"
                cypher = f"""
MATCH (es:MetadataObject {{category_name:'ПодпискиНаСобытия', project_name:$project_name{scope.metadata_map}}})
WHERE {sub_filter}
UNWIND coalesce(es.`Источник`, es.sources_en, []) AS source_val
RETURN es.name AS subscription, es.qualified_name AS subscription_qn,
  es.config_name AS config_name, source_val AS source
ORDER BY subscription, source SKIP $offset LIMIT $limit
""".strip()

            elif mode == "handlers":
                obj_filter = ""
                if source_object and source_object.strip():
                    params["source_object"] = source_object.strip()
                    obj_filter = "AND toLower(m.name) = toLower($source_object)"
                    if source_category and source_category.strip():
                        params["source_cat"] = source_category.strip()
                        obj_filter += " AND toLower(m.category_name) = toLower($source_cat)"

                sub_filter = ""
                if subscription_ref and subscription_ref.strip():
                    params["sub_name"] = subscription_ref.strip()
                    sub_filter = f"AND {apply_match('es.name', 'sub_name', sm)}"

                _adp = _owner_adoption_block(carry_vars="m, r", parent_var="es") if has_extensions else ""
                cypher = f"""
MATCH (m:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
WHERE true {obj_filter}
MATCH (m)-[:HAS_EVENT_SUBSCRIPTION]->(es:MetadataObject)
WHERE es.category_name = 'ПодпискиНаСобытия' {sub_filter}
MATCH (es)-[:USES_HANDLER]->(r:Routine)
{_adp}
RETURN m.name AS object, m.category_name AS source_category, m.qualified_name AS source_qn,
  es.name AS subscription, es.qualified_name AS subscription_qn,
  coalesce(es.`Событие`, es.event_en, '') AS event,
  r.id AS routine_id, coalesce(r.name,'') AS routine,
  coalesce(r.owner_qn,'') AS routine_owner_qn,
  m.config_name AS source_config_name, es.config_name AS subscription_config_name,
  r.config_name AS routine_config_name{adoption_col}
ORDER BY object, subscription, routine
SKIP $offset LIMIT $limit
""".strip()

            else:
                return f"Error: unknown mode='{mode}'."

            results = _run_query(loader, cypher, params, pn)
            shaped = _shape_event_subscriptions_result(results, lim=lim, off=off)
            return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.exception("Error in get_event_subscriptions")
            return f"Error: {e}"
    _patch_tool_defaults(get_event_subscriptions)
    mcp.tool()(get_event_subscriptions)


# ---------------------------------------------------------------------------
# Tool 13: search_bsl_routines  (BSL only)
# ---------------------------------------------------------------------------

def _register_search_bsl_routines(mcp):
    def search_bsl_routines(
        mode: Literal["description", "name", "signature", "unused", "exported"],
        search_text: Optional[str] = None,
        search_match: Optional[Literal["exact", "starts_with", "contains"]] = None,
        owner_ref: Optional[str] = None,
        routine_type: Optional[Literal["Procedure", "Function"]] = None,
        export: Optional[bool] = None,
        directive: Optional[str] = None,
        is_ssl_api: Optional[bool] = None,
        routine_name: Optional[str] = None,
        owner_categories: Optional[List[str]] = None,
        module_type: Optional[Literal[
            "CommonModule", "CommonFormModule", "FormModule", "CommandModule",
            "ObjectModule", "ManagerModule", "ValueManagerModule", "RecordSetModule",
            "ConfigurationModule",
        ]] = None,
        min_score: Optional[float] = None,
        config: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        call_context_mode: Literal["none", "callees", "callers", "both"] = "none",
        call_context_limit: Optional[int] = 5,
        project_name: Optional[str] = None,
    ) -> str:
        """Search/list BSL routines (procedures/functions) across the project.

mode:
  description — semantic/fulltext search by doc comment. search_text required. min_score applies.
  name        — search by routine name. search_text required. search_match default: exact.
  signature   — substring search over signature. search_text required. search_match default: contains.
  unused      — routines without callers/handlers. export=true narrows to exported-unused.
  exported    — all exported routines. export param ignored.

Filters (combined with AND, applied in all modes unless noted):
  owner_ref         — owner QN or short ref. Matches the owner itself and everything below it
                      (e.g. ".../ОбщиеМодули" → all common modules; full module QN → module + its forms).
                      Raises if owner_ref doesn't exist in the graph.
  owner_categories  — list of canonical categories, e.g. ["ОбщиеМодули", "Справочники"].
  routine_name      — case-insensitive substring on routine name.
  directive         — exact directive name (e.g. "&НаКлиенте").
  export            — routine declared with "Экспорт". Ignored in unused/exported (see mode notes).
  is_ssl_api        — routine belongs to an object inside the "СтандартныеПодсистемы" subsystem
                      (БСП / SSL API marker).
  config            — config name within the project.

call_context_mode: "none" (default) | "callees" | "callers" | "both".
  Adds caller/callee context for found routines, capped by call_context_limit (default 5),
  scoped to the same config as the search.
"""
        loader = _init_search_ready_loader()
        if loader is None:
            return ("Error: Neo4j database connection or BSL routine search index "
                    "not available yet.")
        try:
            pn = _resolve_project(project_name)
            config_name = resolve_config(loader, config, pn)
            scope = _scope(config_name)
            lim = clamp_limit(limit)
            off = clamp_offset(offset)

            has_extensions = bool(_run_query(
                loader,
                "MATCH (c:Configuration {project_name: $project_name, is_extension: true}) RETURN c LIMIT 1",
                {},
                pn,
            ))
            interception_col = ", interception" if has_extensions else ""
            module_type_col = ", module_type"

            owner_qn: Optional[str] = None
            if owner_ref and owner_ref.strip():
                owner_qn = normalize_qn_ref(loader, owner_ref.strip(), pn, config_name)

            # Canonicalize owner_categories and validate count (before any embedding call).
            raw_owner_cats = [c.strip() for c in (owner_categories or []) if isinstance(c, str) and c.strip()]
            owner_cats_canon = canon_categories(raw_owner_cats) if raw_owner_cats else []
            max_cats = int(getattr(settings, "vec_max_category_filters", 5) or 5)
            if len(owner_cats_canon) > max_cats:
                return (
                    f"Error: too many owner_categories after canonicalization ({len(owner_cats_canon)} > {max_cats}). "
                    f"Reduce `owner_categories` or raise VEC_MAX_CATEGORY_FILTERS."
                )
            module_type_norm = module_type.strip() if isinstance(module_type, str) and module_type.strip() else None

            routine_fields = (
                "coalesce(r.name,'') AS name, coalesce(r.routine_type,'') AS routine_type, "
                "coalesce(r.export,false) AS export, coalesce(r.directives,[]) AS directives, "
                "r.id AS id, coalesce(r.config_name,'') AS config_name, "
                "coalesce(r.owner_qn,'') AS owner_qn, "
                "coalesce(r.owner_category,'') AS owner_category, "
                "coalesce(r.file_path,'') AS file_path, coalesce(r.line,0) AS line"
            )

            def _build_common_filters(
                effective_export: Optional[bool],
                directive_substring: bool = False,
            ) -> Tuple[List[str], Dict[str, Any]]:
                # owner_qn is never null and always carries the graph's canonical casing
                # (normalize_qn_ref returns it), so compare the bare property: toLower()
                # or coalesce() around it would rule out idx_routine_owner_qn.
                _filters: List[str] = ["AND r.owner_qn STARTS WITH ($project_name + '/')"]
                _params: Dict[str, Any] = {}
                if owner_qn:
                    _params["owner_qn"] = owner_qn
                    _params["owner_qn_prefix"] = owner_qn + "/"
                    _filters.append(
                        "AND (r.owner_qn = $owner_qn"
                        " OR r.owner_qn STARTS WITH $owner_qn_prefix)"
                    )
                if routine_type:
                    _params["rtype"] = routine_type
                    _filters.append("AND toLower(coalesce(r.routine_type,'')) = toLower($rtype)")
                if effective_export is not None:
                    _params["export"] = effective_export
                    _filters.append("AND coalesce(r.export,false) = $export")
                if directive:
                    _params["directive"] = directive
                    _directive_op = "CONTAINS" if directive_substring else "="
                    _filters.append(f"AND ANY(d IN coalesce(r.directives,[]) WHERE toLower(d) {_directive_op} toLower($directive))")
                if is_ssl_api is not None:
                    _params["is_ssl_api"] = is_ssl_api
                    _filters.append("AND coalesce(r.is_ssl_api,false) = $is_ssl_api")
                if routine_name and routine_name.strip():
                    _params["rname"] = routine_name.strip()
                    _filters.append("AND r.name_norm CONTAINS toLower($rname)")
                if config_name:
                    _params["config_name"] = config_name
                    _filters.append("AND r.config_name = $config_name")
                if module_type_norm:
                    _params["module_type"] = module_type_norm
                    _filters.append("AND r.module_type = $module_type")
                if owner_cats_canon:
                    _params["owner_categories"] = owner_cats_canon
                    _filters.append("AND r.owner_category IN $owner_categories")
                return _filters, _params

            if mode == "description":
                if not search_text or not search_text.strip():
                    return "Error: search_text is required for mode='description'."
                try:
                    from graphdb.routine_search_service import RoutineSearchService as _RSS
                    try:
                        from graphdb.embedding_service import get_embedding_service as _ges
                        _embed = _ges()
                    except Exception:
                        _embed = None
                    try:
                        _drv = loader._get_read_driver()
                    except Exception:
                        _drv = getattr(loader, "driver", None)
                    svc = _RSS(_drv, embedding_service=_embed)
                    ms = min_score if min_score is not None else _min_score_adaptive(search_text, None)
                    # owner_qn_prefix scopes results to this owner's subtree (or whole project).
                    # Trailing '/' prevents sibling-prefix collisions (e.g. "Module1" vs "Module1Sub").
                    _owner_prefix = (owner_qn + "/") if owner_qn else (pn + "/")
                    # with_pagination keeps the ranking budget at `lim` (unchanged relevance)
                    # while the service reports has_more from candidate-pool saturation.
                    rows, desc_has_more = svc.search_by_description_hybrid(
                        text=search_text.strip(),
                        limit=lim,
                        offset=off,
                        min_score=ms,
                        owner_qn=owner_qn,
                        owner_qn_prefix=_owner_prefix,
                        routine_type=routine_type,
                        export=export,
                        directive=directive,
                        is_ssl_api=is_ssl_api,
                        name=routine_name,
                        config_name=config_name,
                        project_name=pn,
                        owner_categories=owner_cats_canon if owner_cats_canon else None,
                        module_type=module_type_norm,
                        with_pagination=True,
                    )
                    rows = rows or []
                    if has_extensions and rows:
                        _ids = [row["id"] for row in rows if row.get("id")]
                        _icp_rows = _run_query(loader, """
UNWIND $ids AS rid
MATCH (r:Routine {id: rid})
OPTIONAL MATCH (r)-[:EXTENDS_ROUTINE]->(base_r:Routine)
WITH r, rid, base_r.config_name AS _base_cfg
OPTIONAL MATCH (ext_r:Routine {project_name: $project_name})-[ext_rel:EXTENDS_ROUTINE]->(r)
WITH r, rid, _base_cfg,
     collect(DISTINCT {extension_config_name: ext_r.config_name, decorator: ext_rel.decorator, extension_routine_name: ext_r.name}) AS _ext_list
RETURN rid AS id,
  CASE
    WHEN _base_cfg IS NOT NULL
      THEN {role: 'extension', base_config_name: _base_cfg, decorator: r.decorator_type, base_routine_name: r.decorator_target}
    WHEN size([x IN _ext_list WHERE x.extension_config_name IS NOT NULL]) > 0
      THEN {role: 'base', extensions: [x IN _ext_list WHERE x.extension_config_name IS NOT NULL]}
    ELSE null
  END AS interception
""", {"ids": _ids}, pn)
                        _icp_map = {row["id"]: row["interception"] for row in _icp_rows}
                        rows = [
                            {**row, "interception": _icp_map[row["id"]]}
                            if row.get("id") in _icp_map and _icp_map[row["id"]] is not None
                            else row
                            for row in rows
                        ]
                    if rows:
                        rows = _enrich_module_type(rows, "id", loader, pn)
                    rows = _enrich_call_context(rows, call_context_mode, call_context_limit, loader, pn, config_name)
                    shaped = _shape_search_bsl_routines_result(
                        mode, "description_service", rows, lim=lim, off=off, has_more=desc_has_more,
                    )
                    return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)
                except Exception as e:
                    logger.warning("RoutineSearchService unavailable, falling back to fulltext: %s", e)
                    # Fallback: simple fulltext on doc_description.
                    # directive uses substring semantics here to match the main description
                    # path (Cypher templates + RoutineSearchService contract use CONTAINS).
                    # Fetch lim+1 for an exact has_more lookahead (LIMIT after ORDER BY is safe).
                    filters, fb_params = _build_common_filters(effective_export=export, directive_substring=True)
                    params: Dict[str, Any] = {"text": search_text.strip(), "offset": off, "limit": lim + 1, **fb_params}
                    extra = "\n  ".join(filters)
                    _icp_fb = _routine_interception_block() if has_extensions else ""
                    _mt_fb = _module_type_block(has_interception=has_extensions)
                    cypher = _paged_routine_query(
                        "toLower(coalesce(r.doc_description,'')) CONTAINS toLower($text)",
                        extra, _icp_fb, _mt_fb,
                        f"{routine_fields}{interception_col}{module_type_col}",
                        PageSourcePolicy.PROJECT_ORDERED_SCAN,
                    )
                    results = _run_query(loader, cypher, params, pn)
                    if has_extensions:
                        results = _strip_null_interception(results)
                    results = _enrich_call_context(results, call_context_mode, call_context_limit, loader, pn, config_name)
                    shaped = _shape_search_bsl_routines_result(
                        mode, "routine_fields", results, lim=lim, off=off,
                    )
                    return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

            # export is ignored in unused/exported (those modes enforce export themselves).
            # Fetch lim+1 for an exact has_more lookahead (LIMIT applied after ORDER BY).
            effective_export = None if mode in ("unused", "exported") else export
            filters, common_params = _build_common_filters(effective_export=effective_export)
            params = {"offset": off, "limit": lim + 1, **common_params}

            extra = "\n  ".join(filters)

            _icp = _routine_interception_block() if has_extensions else ""
            _mt = _module_type_block(has_interception=has_extensions)

            _return_fields = f"{routine_fields}{interception_col}{module_type_col}"
            substring_src = substring_scan_src = None
            page_policy = PageSourcePolicy.DEFAULT

            if mode in ("name", "signature"):
                if not search_text or not search_text.strip():
                    return f"Error: search_text is required for mode='{mode}'."
                params["text"] = search_text.strip()
                if mode == "name":
                    sm = (search_match or "exact").lower()
                    target = SubstringTarget.NAME
                    field_norm = "r.name_norm"
                else:
                    sm = (search_match or "contains").lower()
                    target = SubstringTarget.SIGNATURE
                    field_norm = "r.signature_norm"

                if sm == "contains":
                    # The fulltext index only narrows; the CONTAINS verifier inside the
                    # returned section is what decides, so the result is unchanged.
                    substring_src, substring_scan_src = _substring_sources(
                        target, params["text"], "text",
                    )
                    source_cypher = substring_src.cypher
                else:
                    # exact / starts_with are index seeks on the range index already;
                    # a leading-wildcard fulltext query would only slow them down.
                    source_cypher = (
                        f"MATCH (r:Routine)\nWHERE {apply_match_norm(field_norm, 'text', sm)}"
                    )

            elif mode == "unused":
                if export:
                    filters.append("AND coalesce(r.export,false) = true")
                    extra = "\n  ".join(filters)
                # HAS_HANDLER is emitted by FormEventAction, Command and UrlMethod alike;
                # the source label used to be pinned to FormEventAction, which reported
                # every command and HTTP-service handler as unused. Any incoming
                # HAS_HANDLER means something invokes the routine, so match it unlabelled.
                source_cypher = """NOT ((:Routine)-[:CALLS]->(r))
  AND NOT (()-[:HAS_HANDLER]->(r))
  AND NOT ((:MetadataObject)-[:USES_HANDLER]->(r))"""
                page_policy = PageSourcePolicy.PROJECT_ORDERED_SCAN

            elif mode == "exported":
                source_cypher = "coalesce(r.export,false) = true"
                page_policy = PageSourcePolicy.PROJECT_ORDERED_SCAN

            else:
                return f"Error: unknown mode='{mode}'."

            def _build(source: str) -> str:
                return _paged_routine_query(
                    source, extra, _icp, _mt, _return_fields, page_policy,
                )

            if substring_src is not None:
                results = _run_routine_query_with_fallback(
                    loader, pn, params, _build, substring_src, substring_scan_src,
                )
            else:
                results = _run_query(loader, _build(source_cypher), params, pn)
            if has_extensions:
                results = _strip_null_interception(results)
            results = _enrich_call_context(results, call_context_mode, call_context_limit, loader, pn, config_name)
            shaped = _shape_search_bsl_routines_result(
                mode, "routine_fields", results, lim=lim, off=off,
            )
            return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.exception("Error in search_bsl_routines")
            return f"Error: {e}"
    _patch_tool_defaults(search_bsl_routines)
    mcp.tool()(search_bsl_routines)


# ---------------------------------------------------------------------------
# Tool 14: get_bsl_routine_body  (BSL only)
# ---------------------------------------------------------------------------

def _register_get_bsl_routine_body(mcp):
    def get_bsl_routine_body(
        routine_ref: str,
        routine_ref_type: Literal["id", "name", "signature"] = "name",
        routine_owner_ref: Optional[str] = None,
        body_limit: Optional[int] = None,
        body_offset: Optional[int] = None,
        limit: Optional[int] = 1,
        offset: Optional[int] = None,
        config: Optional[str] = None,
        project_name: Optional[str] = None,
    ) -> str:
        """Get BSL routine metadata and body text.

Use routine_ref_type="id" with a routine id when possible.
Use routine_ref_type="name" for an exact routine name; pass routine_owner_ref when the name
is not unique.
Use routine_ref_type="signature" to search by signature substring.

limit/offset page matching routines for name/signature searches.
body_limit/body_offset read a large routine body in chunks.
config scopes to one configuration or extension.
"""
        loader = _init_search_ready_loader()
        if loader is None:
            return ("Error: Neo4j database connection or BSL routine search index "
                    "not available yet.")
        try:
            pn = _resolve_project(project_name)
            config_name = resolve_config(loader, config, pn)
            scope = _scope(config_name)
            rref = (routine_ref or "").strip()
            if not rref:
                return "Error: routine_ref cannot be empty."

            max_rows = min(clamp_limit(limit, default=1), 3)
            off = clamp_offset(offset)
            max_body = int(body_limit) if body_limit and int(body_limit) > 0 else 10000
            body_off = clamp_offset(body_offset)

            owner_qn: Optional[str] = None
            if routine_owner_ref and routine_owner_ref.strip():
                owner_qn = normalize_qn_ref(loader, routine_owner_ref.strip(), pn, config_name)

            owner_short = (
                "CASE WHEN size(p)>=4 AND (p[size(p)-2]='Form' OR p[size(p)-2]='Command') "
                "THEN p[size(p)-4]+'.'+p[size(p)-3] "
                "WHEN size(p)>=4 THEN p[size(p)-2]+'.'+p[size(p)-1] "
                "WHEN size(p)>=2 THEN 'Конфигурация.'+p[size(p)-1] "
                "ELSE coalesce(r.owner_qn,'') END"
            )
            select_fields = (
                f"r.id AS id, coalesce(r.name,'') AS name, {owner_short} AS owner, "
                f"module_type, form_name, coalesce(r.owner_qn,'') AS owner_qn, "
                f"coalesce(r.signature,'') AS signature, coalesce(r.directives,[]) AS directives, "
                f"coalesce(r.doc_description,'') AS doc_description, "
                f"coalesce(r.doc_params_text,'') AS doc_params_text, "
                f"coalesce(r.doc_return_text,'') AS doc_return_text, "
                f"coalesce(r.file_path,'') AS file_path, coalesce(r.line,0) AS line, "
                # body chunk + cursor metrics computed in the same Neo4j string layer as the
                # substring slice, so body_next_offset always feeds back into substring correctly.
                "body_chunk AS body, body_total_chars, "
                "size(body_chunk) AS body_returned_chars, "
                "($body_off + size(body_chunk) < body_total_chars) AS body_truncated, "
                "CASE WHEN $body_off + size(body_chunk) < body_total_chars "
                "THEN $body_off + size(body_chunk) ELSE null END AS body_next_offset"
            )
            owner_setup = (
                "OPTIONAL MATCH (mod:Module)-[:DECLARES]->(r) "
                "WITH r, coalesce(mod.module_type,'CommonModule') AS module_type "
                "WITH r, module_type, split(r.owner_qn,'/') AS p "
                "WITH r, module_type, p, "
                "CASE WHEN module_type='FormModule' AND size(p)>=1 THEN p[size(p)-1] ELSE '' END AS form_name "
                "WITH r, module_type, p, form_name, "
                "substring(coalesce(r.body,''), $body_off, $max_body) AS body_chunk, "
                "size(coalesce(r.body,'')) AS body_total_chars"
            )

            params: Dict[str, Any] = {
                "body_off": body_off,
                "max_body": max_body,
                "offset": off,
                # lookahead for has_more on name/signature; id branch uses a literal LIMIT 1.
                "limit": max_rows + 1,
            }
            if config_name:
                params["config_name"] = config_name

            config_filter = f"\n  AND r.config_name = $config_name" if config_name else ""

            if routine_ref_type == "id":
                params["routine_id"] = rref
                cypher = f"""
MATCH (r:Routine {{id:$routine_id}})
WHERE r.owner_qn STARTS WITH ($project_name + '/'){config_filter}
{owner_setup}
RETURN {select_fields}
LIMIT 1
""".strip()
            elif routine_ref_type == "name":
                params["rname"] = rref
                owner_filter = "\n  AND r.owner_qn STARTS WITH ($project_name + '/')"
                if owner_qn:
                    params["owner_qn"] = owner_qn
                    owner_filter += "\n  AND r.owner_qn = $owner_qn"
                cypher = f"""
MATCH (r:Routine)
WHERE r.name_norm = toLower($rname){owner_filter}{config_filter}
{owner_setup}
RETURN {select_fields}
ORDER BY id SKIP $offset LIMIT $limit
""".strip()
            elif routine_ref_type == "signature":
                params["sig"] = rref
                proj_filter = "\n  AND r.owner_qn STARTS WITH ($project_name + '/')"
                sig_src, sig_scan_src = _substring_sources(
                    SubstringTarget.SIGNATURE, rref, "sig",
                )

                def _build_sig(source: str) -> str:
                    return f"""
{source}{proj_filter}{config_filter}
{owner_setup}
RETURN {select_fields}
ORDER BY id SKIP $offset LIMIT $limit
""".strip()

                results = _run_routine_query_with_fallback(
                    loader, pn, params, _build_sig, sig_src, sig_scan_src,
                )
                cypher = None
            else:
                return f"Error: unknown routine_ref_type='{routine_ref_type}'."

            if cypher is not None:
                results = _run_query(loader, cypher, params, pn)
            if routine_ref_type == "id":
                # id lookup returns at most one row (LIMIT 1); pagination is disabled.
                shaped = _shape_get_bsl_routine_body_result(
                    results, lim=1, off=0, body_off=body_off, max_body=max_body, has_more=False,
                )
            else:
                shaped = _shape_get_bsl_routine_body_result(
                    results, lim=max_rows, off=off, body_off=body_off, max_body=max_body,
                )
            return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.exception("Error in get_bsl_routine_body")
            return f"Error: {e}"
    _patch_tool_defaults(get_bsl_routine_body)
    mcp.tool()(get_bsl_routine_body)


# ---------------------------------------------------------------------------
# Tool 15: get_bsl_modules  (BSL only)
# ---------------------------------------------------------------------------

def _shape_routine_basic(r: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": r.get("name") or "",
        "routine_type": r.get("routine_type") or "",
        "export": bool(r.get("export", False)),
        "directives": r.get("directives") or [],
        "id": r.get("id"),
        "line": r.get("line", 0),
    }


def _shape_extract_interceptions(
    routines: List[Dict[str, Any]],
    id_field: str = "id",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    cleaned: List[Dict[str, Any]] = []
    interceptions: List[Dict[str, Any]] = []
    for r in routines:
        r2 = {k: v for k, v in r.items() if k != "interception"}
        cleaned.append(r2)
        icp = r.get("interception")
        if not icp:
            continue
        role = icp.get("role")
        entry: Dict[str, Any] = {"routine_id": r.get(id_field), "role": role}
        if role == "extension":
            entry["base_config_name"] = icp.get("base_config_name") or ""
            entry["decorator"] = icp.get("decorator") or ""
            entry["base_routine_name"] = icp.get("base_routine_name") or ""
        elif role == "base":
            exts = icp.get("extensions") or []
            entry["extension_config_names"] = [e.get("extension_config_name") or "" for e in exts]
            entry["extension_decorators"] = [e.get("decorator") or "" for e in exts]
            entry["extension_routine_names"] = [e.get("extension_routine_name") or "" for e in exts]
        interceptions.append(entry)
    return cleaned, interceptions


# search_bsl_routines: fixed per-source-dialect field sets. `description_service` rows come
# from RoutineSearchService (owner/form_name/doc/score, no file_path/line/routine_type/export);
# `routine_fields` rows come from the Cypher branches AND the description fulltext fallback
# (file_path/line/routine_type/export, no owner/form_name/score). Keeping the column set fixed
# per dialect (instead of dropping globally-empty columns) makes the response schema depend on
# the source, not on the current page's data.
_SEARCH_BSL_MODULE_KEYS_ROUTINE_FIELDS = (
    "config_name", "owner_qn", "owner_category", "module_type", "file_path",
)
_SEARCH_BSL_MODULE_KEYS_DESCRIPTION = (
    "config_name", "owner_qn", "owner", "owner_category", "form_name", "module_type",
)
# Module identity is the union of both dialects' context keys, so grouping is stable
# regardless of dialect (missing keys collapse to "").
_SEARCH_BSL_MODULE_IDENTITY_KEYS = (
    "config_name", "owner_qn", "owner", "owner_category",
    "form_name", "module_type", "file_path",
)
_SEARCH_BSL_SCORE_KEYS = (
    "score", "similarity", "fulltext_score", "vector_score", "hybrid_score",
)
_SEARCH_BSL_DESCRIPTION_TEXT_KEYS = (
    "signature", "doc_description", "doc_params_text", "doc_return_text",
)


def _normalize_search_bsl_routine_row(
    dialect: str, row: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any], Optional[Dict[str, Any]]]:
    """Split one raw search_bsl_routines row into (module_context, routine_core, scores).

    The only place that knows the two source dialects. module_context columns are fixed by
    `dialect`; routine_core carries just routine-specific fields; scores is None unless the
    row comes from the description hybrid service."""
    if dialect == "description_service":
        module_context = {k: row.get(k) or "" for k in _SEARCH_BSL_MODULE_KEYS_DESCRIPTION}
        routine_core: Dict[str, Any] = {
            "id": row.get("id"),
            "name": row.get("name") or "",
            "directives": row.get("directives") or [],
        }
        for k in _SEARCH_BSL_DESCRIPTION_TEXT_KEYS:
            routine_core[k] = row.get(k) or ""
        scores: Optional[Dict[str, Any]] = {
            k: _compact_response_score(row.get(k)) for k in _SEARCH_BSL_SCORE_KEYS
        }
    else:
        module_context = {k: row.get(k) or "" for k in _SEARCH_BSL_MODULE_KEYS_ROUTINE_FIELDS}
        routine_core = {
            "id": row.get("id"),
            "name": row.get("name") or "",
            "routine_type": row.get("routine_type") or "",
            "export": bool(row.get("export", False)),
            "directives": row.get("directives") or [],
            "line": row.get("line", 0),
        }
        scores = None
    return module_context, routine_core, scores


def _shape_search_bsl_routines_result(
    mode: str,
    dialect: str,
    rows: List[Dict[str, Any]],
    *,
    lim: int,
    off: int,
    has_more: Optional[bool] = None,
) -> Dict[str, Any]:
    """Wrap search_bsl_routines rows into {context, page, module_contexts, routines, ...}.

    has_more=None: caller fetched lim+1 rows (Cypher branches + description fallback); trim to
    lim and derive has_more from the lookahead. has_more given: rows are already exactly the
    top-lim page (description main path; the service computes has_more from pool saturation),
    so do not trim.

    module_contexts[] lifts the shared module/file context out of each row (referenced by a
    response-local module_key — NOT a real Module.id). interception/callees/callers are lifted
    into side tables so routines[] stays a flat TOON table."""
    if has_more is None:
        has_more = len(rows) > lim
        rows = rows[:lim] if has_more else rows

    rows, interceptions = _shape_extract_interceptions(rows)

    callees: List[Dict[str, Any]] = []
    callers: List[Dict[str, Any]] = []
    cleaned_rows: List[Dict[str, Any]] = []
    for r in rows:
        rid = r.get("id")
        for c in (r.get("callees") or []):
            callees.append({
                "routine_id": rid,
                "callee_id": c.get("id"),
                "callee": c.get("name") or "",
                "callee_owner_qn": c.get("owner_qn") or "",
            })
        for c in (r.get("callers") or []):
            callers.append({
                "routine_id": rid,
                "caller_id": c.get("id"),
                "caller": c.get("name") or "",
                "caller_owner_qn": c.get("owner_qn") or "",
            })
        cleaned_rows.append({k: v for k, v in r.items() if k not in ("callees", "callers")})

    module_index: Dict[tuple, str] = {}
    module_contexts: List[Dict[str, Any]] = []
    routines_out: List[Dict[str, Any]] = []
    for r in cleaned_rows:
        module_context, routine_core, scores = _normalize_search_bsl_routine_row(dialect, r)
        identity = tuple(r.get(k) or "" for k in _SEARCH_BSL_MODULE_IDENTITY_KEYS)
        mkey = module_index.get(identity)
        if mkey is None:
            mkey = f"module{len(module_contexts) + 1}"
            module_index[identity] = mkey
            module_contexts.append({"module_key": mkey, **module_context})
        routine = {"module_key": mkey, **routine_core}
        if scores is not None:
            routine.update(scores)
        routines_out.append(routine)

    page: Dict[str, Any] = {
        "limit": lim, "offset": off, "returned": len(routines_out), "has_more": has_more,
    }
    if has_more:
        page["next_offset"] = off + len(routines_out)

    out: Dict[str, Any] = {
        "context": {"mode": mode, "dialect": dialect},
        "page": page,
        "module_contexts": module_contexts,
        "routines": routines_out,
    }
    if interceptions:
        out["interceptions"] = interceptions
    if callees:
        out["callees"] = callees
    if callers:
        out["callers"] = callers
    return out


_GET_BSL_ROUTINE_BODY_FIELD_ORDER = (
    "id", "name", "owner", "module_type", "form_name", "owner_qn", "signature",
    "directives", "doc_description", "doc_params_text", "doc_return_text",
    "file_path", "line", "body",
)


def _shape_get_bsl_routine_body_result(
    rows: List[Dict[str, Any]],
    *,
    lim: int,
    off: int,
    body_off: int,
    max_body: int,
    has_more: Optional[bool] = None,
) -> Dict[str, Any]:
    """Wrap get_bsl_routine_body rows into {page, routines}.

    has_more=None: caller fetched lim+1 rows (name/signature lookahead); trim to lim and derive
    has_more. has_more given: rows are already the exact page (id lookup, LIMIT 1); do not trim.

    body cursor metrics (body_total_chars/body_returned_chars/body_truncated/body_next_offset)
    are computed in Cypher on the same string layer as the substring slice, so the helper only
    packs them: it echoes body_offset/body_limit (query params) and emits body_next_offset only
    when body_truncated is true (Cypher returns null otherwise)."""
    if has_more is None:
        has_more = len(rows) > lim
        rows = rows[:lim] if has_more else rows

    routines_out: List[Dict[str, Any]] = []
    for r in rows:
        routine: Dict[str, Any] = {k: r.get(k) for k in _GET_BSL_ROUTINE_BODY_FIELD_ORDER}
        routine["body_offset"] = body_off
        routine["body_limit"] = max_body
        routine["body_total_chars"] = r.get("body_total_chars")
        routine["body_returned_chars"] = r.get("body_returned_chars")
        body_truncated = bool(r.get("body_truncated"))
        routine["body_truncated"] = body_truncated
        if body_truncated:
            routine["body_next_offset"] = r.get("body_next_offset")
        routines_out.append(routine)

    page: Dict[str, Any] = {
        "limit": lim, "offset": off, "returned": len(routines_out), "has_more": has_more,
    }
    if has_more:
        page["next_offset"] = off + len(routines_out)

    return {"page": page, "routines": routines_out}


def _shape_call_graph_page(
    mode: str,
    context: Dict[str, Any],
    rows: List[Dict[str, Any]],
    *,
    lim: int,
    off: int,
    extract_interceptions: bool = False,
    id_field: str = "id",
) -> Dict[str, Any]:
    """Wrap direct call-graph rows into {context, page, calls, interceptions?}.

    Caller fetches lim + 1 rows; helper trims to lim and derives has_more/next_offset.
    When extract_interceptions is set, the per-row interception object is lifted into a
    separate interceptions[] section (keyed by id_field) so calls[] stays a flat TOON table."""
    has_more = len(rows) > lim
    rows = rows[:lim] if has_more else rows
    interceptions: List[Dict[str, Any]] = []
    if extract_interceptions:
        rows, interceptions = _shape_extract_interceptions(rows, id_field=id_field)
    page: Dict[str, Any] = {"limit": lim, "offset": off, "returned": len(rows), "has_more": has_more}
    if has_more:
        page["next_offset"] = off + len(rows)
    out: Dict[str, Any] = {"context": context, "page": page, "calls": rows}
    if interceptions:
        out["interceptions"] = interceptions
    return out


def _shape_get_bsl_modules_result(
    mode: str,
    *,
    routine_rows: Optional[List[Dict[str, Any]]] = None,
    module_rows: Optional[List[Dict[str, Any]]] = None,
    owner_rows: Optional[List[Dict[str, Any]]] = None,
    module_routines_variant: Optional[Literal["module_id", "owner_ref"]] = None,
    requested_module_id: Optional[str] = None,
) -> Dict[str, Any]:
    rr = routine_rows or []
    mr = module_rows or []
    or_ = owner_rows or []

    if mode == "modules_of_owner":
        modules: List[Dict[str, Any]] = []
        owner: Dict[str, Any] = {}
        for row in mr:
            if not owner:
                owner = {
                    "config_name": row.get("owner_config_name") or "",
                    "owner_qn": row.get("owner_qn") or "",
                }
            modules.append({
                "name": row.get("name") or "",
                "module_type": row.get("module_type") or "",
                "path": row.get("path") or "",
                "id": row.get("id"),
            })
        return {"owner": owner, "modules": modules}

    if mode == "modules_by_owner_name":
        owners: List[Dict[str, Any]] = []
        owner_index: Dict[str, str] = {}
        modules_out: List[Dict[str, Any]] = []
        for row in mr:
            o_qn = row.get("owner_qn") or ""
            if o_qn not in owner_index:
                oid = f"o{len(owners) + 1}"
                owner_index[o_qn] = oid
                owners.append({
                    "owner_id": oid,
                    "owner_name": row.get("owner_name") or "",
                    "config_name": row.get("owner_config_name") or "",
                    "owner_qn": o_qn,
                })
            modules_out.append({
                "owner_id": owner_index[o_qn],
                "name": row.get("name") or "",
                "module_type": row.get("module_type") or "",
                "path": row.get("path") or "",
                "id": row.get("id"),
            })
        return {"owners": owners, "modules": modules_out}

    if mode == "module_routines":
        if module_routines_variant not in ("module_id", "owner_ref"):
            raise ValueError(
                f"module_routines_variant required for mode='module_routines', "
                f"got {module_routines_variant!r}"
            )
        cleaned, interceptions = _shape_extract_interceptions(rr)
        if module_routines_variant == "module_id":
            if mr:
                card = mr[0]
                module = {
                    "id": card.get("id"),
                    "name": card.get("name") or "",
                    "module_type": card.get("module_type") or "",
                    "file_path": card.get("file_path") or "",
                    "config_name": card.get("config_name") or "",
                    "owner_qn": card.get("owner_qn") or "",
                }
            else:
                module = {"id": requested_module_id}
            routines_out = [_shape_routine_basic(r) for r in cleaned]
            res: Dict[str, Any] = {"module": module, "routines": routines_out}
            if interceptions:
                res["interceptions"] = interceptions
            return res
        # Branch B (variant == "owner_ref"): owner_rows has one owner-info row;
        # routines may carry module_id etc.
        owner_info = or_[0] if or_ else {}
        # Detect CommonModule owner: every routine row has module_id is None/empty
        all_no_module = bool(cleaned) and all(
            (r.get("module_id") in (None, "")) for r in cleaned
        )
        if all_no_module:
            first = cleaned[0]
            module = {
                "name": owner_info.get("owner_name") or "",
                "module_type": "CommonModule",
                "file_path": first.get("r_file_path") or "",
                "config_name": owner_info.get("config_name") or "",
                "owner_qn": owner_info.get("owner_qn") or "",
            }
            routines_out = [_shape_routine_basic(r) for r in cleaned]
            res = {"module": module, "routines": routines_out}
            if interceptions:
                res["interceptions"] = interceptions
            return res
        # Regular owner: multi-modules shape
        owner = {
            "config_name": owner_info.get("config_name") or "",
            "owner_qn": owner_info.get("owner_qn") or "",
        }
        # Dedup routines by id, prefer rows with non-null module_id
        by_id: Dict[Any, Dict[str, Any]] = {}
        order: List[Any] = []
        for r in cleaned:
            rid = r.get("id")
            existing = by_id.get(rid)
            if existing is None:
                by_id[rid] = r
                order.append(rid)
                continue
            if (existing.get("module_id") in (None, "")) and (r.get("module_id") not in (None, "")):
                by_id[rid] = r
        deduped = [by_id[rid] for rid in order]
        modules_by_id: Dict[Any, Dict[str, Any]] = {}
        modules_out = []
        for r in deduped:
            mid = r.get("module_id")
            if mid in (None, ""):
                continue
            if mid not in modules_by_id:
                entry = {
                    "id": mid,
                    "name": r.get("module_name") or "",
                    "module_type": r.get("module_type") or "",
                    "file_path": r.get("module_path") or "",
                }
                modules_by_id[mid] = entry
                modules_out.append(entry)
        routines_out = []
        for r in deduped:
            row = _shape_routine_basic(r)
            routines_out.append({"module_id": r.get("module_id"), **row})
        res = {"owner": owner, "modules": modules_out, "routines": routines_out}
        if interceptions:
            res["interceptions"] = interceptions
        return res

    if mode == "common_module_routines":
        cleaned, interceptions = _shape_extract_interceptions(rr)
        modules_by_qn: Dict[str, Dict[str, Any]] = {}
        qn_order: List[str] = []
        for mrow in mr:
            qn = mrow.get("owner_qn") or ""
            if qn in modules_by_qn:
                continue
            modules_by_qn[qn] = {
                "name": mrow.get("name") or "",
                "config_name": mrow.get("config_name") or "",
                "owner_qn": qn,
            }
            qn_order.append(qn)
        file_path_by_qn: Dict[str, str] = {}
        for r in cleaned:
            qn = r.get("module_owner_qn") or ""
            if qn not in file_path_by_qn:
                file_path_by_qn[qn] = r.get("file_path") or ""
        if len(modules_by_qn) == 1:
            qn = qn_order[0]
            m = modules_by_qn[qn]
            module = {
                "name": m["name"],
                "module_type": "CommonModule",
                "file_path": file_path_by_qn.get(qn, ""),
                "config_name": m["config_name"],
                "owner_qn": m["owner_qn"],
            }
            routines_out = [_shape_routine_basic(r) for r in cleaned]
            res = {"module": module, "routines": routines_out}
            if interceptions:
                res["interceptions"] = interceptions
            return res
        modules_out = []
        for qn in qn_order:
            m = modules_by_qn[qn]
            modules_out.append({
                "name": m["name"],
                "file_path": file_path_by_qn.get(qn, ""),
                "config_name": m["config_name"],
                "owner_qn": m["owner_qn"],
            })
        routines_out = []
        for r in cleaned:
            row = _shape_routine_basic(r)
            routines_out.append({"module_owner_qn": r.get("module_owner_qn") or "", **row})
        res = {"modules": modules_out, "routines": routines_out}
        if interceptions:
            res["interceptions"] = interceptions
        return res

    return {}


def _register_get_bsl_modules(mcp):
    def get_bsl_modules(
        mode: Literal["modules_of_owner", "modules_by_owner_name", "module_routines", "common_module_routines"],
        owner_ref: Optional[str] = None,
        owner_kind: Optional[Literal["Form", "MetadataObject", "Configuration", "Command"]] = None,
        owner_match: Optional[Literal["exact", "starts_with", "contains"]] = None,
        module_ref: Optional[str] = None,
        module_match: Optional[Literal["exact", "starts_with", "contains"]] = None,
        routine_name: Optional[str] = None,
        routine_name_match: Optional[Literal["exact", "starts_with", "contains"]] = None,
        config: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        project_name: Optional[str] = None,
    ) -> str:
        """List indexed BSL modules and routines.

Use this tool to find modules of metadata objects/forms and routine ids
for get_bsl_routine_body.

Modes:
- modules_of_owner: modules attached to one owner; pass owner_ref.
- modules_by_owner_name: modules by owner name; pass owner_ref,
  optionally owner_match and owner_kind.
- module_routines: routines from a module or owner. Pass module_ref =
  module.id from a previous get_bsl_modules response, or pass owner_ref.
- common_module_routines: routines from common modules; pass module_ref
  as common module name or pattern.

owner_ref accepts: full qualified_name, "Category.Object" /
"Category/Object", "Category.Object.Формы.Form", or a unique object name.

Use routine_name/routine_name_match to filter routines.
"""
        loader = _init_search_ready_loader()
        if loader is None:
            return ("Error: Neo4j database connection or BSL routine search index "
                    "not available yet.")
        try:
            pn = _resolve_project(project_name)
            config_name = resolve_config(loader, config, pn)
            scope = _scope(config_name)
            lim = clamp_limit(limit)
            off = clamp_offset(offset)
            params: Dict[str, Any] = {"offset": off, "limit": lim}
            if config_name:
                params["config_name"] = config_name

            config_filter = "\n  AND m.config_name = $config_name" if config_name else ""
            r_config_filter = "\n  AND r.config_name = $config_name" if config_name else ""

            routine_fields = (
                "coalesce(r.name,'') AS name, coalesce(r.routine_type,'') AS routine_type, "
                "coalesce(r.export,false) AS export, coalesce(r.directives,[]) AS directives, "
                "r.id AS id, coalesce(r.line,0) AS line"
            )

            has_extensions = bool(_run_query(
                loader,
                "MATCH (c:Configuration {project_name: $project_name, is_extension: true}) RETURN c LIMIT 1",
                {},
                pn,
            ))
            interception_col = ", interception" if has_extensions else ""
            _icp = _routine_interception_block() if has_extensions else ""

            if mode == "modules_of_owner":
                if not owner_ref or not owner_ref.strip():
                    return "Error: owner_ref is required for mode='modules_of_owner'."
                owner_qn = normalize_qn_ref(loader, owner_ref.strip(), pn, config_name)
                params["owner_qn"] = owner_qn
                cypher = f"""
MATCH (o {{qualified_name:$owner_qn}})-[:HAS_MODULE]->(m:Module)
WHERE true{config_filter}
RETURN coalesce(m.name,'') AS name, coalesce(m.module_type,'') AS module_type,
  coalesce(m.path,'') AS path, m.id AS id,
  coalesce(o.config_name, o.name) AS owner_config_name,
  o.qualified_name AS owner_qn
ORDER BY name, id SKIP $offset LIMIT $limit
""".strip()
                module_rows = _run_query(loader, cypher, params, pn)
                shaped = _shape_get_bsl_modules_result("modules_of_owner", module_rows=module_rows)
                return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

            if mode == "modules_by_owner_name":
                if not owner_ref or not owner_ref.strip():
                    return "Error: owner_ref is required for mode='modules_by_owner_name'."
                params["owner_ref"] = owner_ref.strip()
                om = (owner_match or "exact").lower()
                name_cond = apply_match("o.name", "owner_ref", om)
                kind_filter = f"\n  AND '{owner_kind}' IN labels(o)" if owner_kind else ""
                cypher = f"""
MATCH (o)-[:HAS_MODULE]->(m:Module)
WHERE {name_cond}{kind_filter}
  AND o.project_name = $project_name{config_filter}
RETURN coalesce(m.name,'') AS name, coalesce(m.module_type,'') AS module_type,
  coalesce(m.path,'') AS path, m.id AS id,
  o.qualified_name AS owner_qn,
  coalesce(o.name,'') AS owner_name,
  coalesce(o.config_name, o.name) AS owner_config_name
ORDER BY owner_name, name, id SKIP $offset LIMIT $limit
""".strip()
                module_rows = _run_query(loader, cypher, params, pn)
                shaped = _shape_get_bsl_modules_result("modules_by_owner_name", module_rows=module_rows)
                return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

            if mode == "module_routines":
                rn_filter = ""
                if routine_name and routine_name.strip():
                    params["rname"] = routine_name.strip()
                    rnm = (routine_name_match or "exact").lower()
                    rn_filter = f"\n  AND {apply_match_norm('r.name_norm', 'rname', rnm)}"

                _module_id_re = re.compile(
                    r'^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{40})$',
                    re.I,
                )
                if module_ref and _module_id_re.match(module_ref.strip()):
                    # Branch A: by module id
                    params["module_id"] = module_ref.strip()
                    cypher = f"""
MATCH (o {{project_name:$project_name}})-[:HAS_MODULE|DECLARES]->(m:Module {{id:$module_id}})-[:DECLARES]->(r:Routine)
WHERE true{rn_filter}{r_config_filter}
{_icp}
RETURN {routine_fields}{interception_col}
ORDER BY name, id SKIP $offset LIMIT $limit
""".strip()
                    routine_rows = _run_query(loader, cypher, params, pn)
                    card_config_filter = "\n  AND m.config_name = $config_name" if config_name else ""
                    card_cypher = f"""
MATCH (m:Module {{id:$module_id, project_name:$project_name}})
WHERE true{card_config_filter}
RETURN coalesce(m.name,'') AS name, coalesce(m.module_type,'') AS module_type,
  coalesce(m.path,'') AS file_path, coalesce(m.config_name,'') AS config_name,
  head([(o)-[:HAS_MODULE|DECLARES]->(m) | o.qualified_name]) AS owner_qn,
  m.id AS id
LIMIT 1
""".strip()
                    card_params: Dict[str, Any] = {"module_id": params["module_id"]}
                    if config_name:
                        card_params["config_name"] = config_name
                    module_rows = _run_query(loader, card_cypher, card_params, pn)
                    shaped = _shape_get_bsl_modules_result(
                        "module_routines",
                        routine_rows=routine_rows,
                        module_rows=module_rows,
                        module_routines_variant="module_id",
                        requested_module_id=params["module_id"],
                    )
                    return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

                # Branch B: owner_ref (or module_ref as QN)
                ref_val = (owner_ref or module_ref or "").strip()
                if not ref_val:
                    return "Error: owner_ref or module_ref is required for mode='module_routines'."
                owner_qn = normalize_qn_ref(loader, ref_val, pn, config_name)
                params["owner_qn"] = owner_qn

                if has_extensions:
                    rebind = (
                        "\nOPTIONAL MATCH (m_back:Module)-[:DECLARES]->(r)"
                        "\nWITH r, interception, m_back.id AS module_id, m_back.name AS module_name,"
                        " m_back.module_type AS module_type, m_back.path AS module_path"
                    )
                else:
                    rebind = (
                        "\nOPTIONAL MATCH (m_back:Module)-[:DECLARES]->(r)"
                        "\nWITH r, m_back.id AS module_id, m_back.name AS module_name,"
                        " m_back.module_type AS module_type, m_back.path AS module_path"
                    )
                cypher = f"""
CALL {{
  MATCH (o:MetadataObject {{qualified_name:$owner_qn}})-[:DECLARES]->(r:Routine)
  WHERE true{rn_filter}{r_config_filter}
  RETURN r
  UNION
  MATCH (o {{qualified_name:$owner_qn}})-[:HAS_MODULE]->(m:Module)-[:DECLARES]->(r:Routine)
  WHERE true{rn_filter}{r_config_filter}
  RETURN r
}}
{_icp}{rebind}
RETURN {routine_fields}{interception_col},
  module_id, module_name, module_type, module_path,
  coalesce(r.module_type,'') AS r_module_type,
  coalesce(r.owner_qn,'') AS r_owner_qn,
  coalesce(r.file_path,'') AS r_file_path
ORDER BY name, id SKIP $offset LIMIT $limit
""".strip()
                routine_rows = _run_query(loader, cypher, params, pn)
                owner_cypher = """
MATCH (o {qualified_name:$owner_qn, project_name:$project_name})
RETURN coalesce(o.config_name, o.name) AS config_name,
  o.qualified_name AS owner_qn,
  coalesce(o.name,'') AS owner_name
LIMIT 1
""".strip()
                owner_rows = _run_query(loader, owner_cypher, {"owner_qn": owner_qn}, pn)
                shaped = _shape_get_bsl_modules_result(
                    "module_routines",
                    routine_rows=routine_rows,
                    owner_rows=owner_rows,
                    module_routines_variant="owner_ref",
                )
                return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

            if mode == "common_module_routines":
                if not module_ref or not module_ref.strip():
                    return "Error: module_ref (common module name) is required for mode='common_module_routines'."
                params["module_ref"] = module_ref.strip()
                mm = (module_match or "exact").lower()
                mod_cond = apply_match("m.name", "module_ref", mm)
                rn_filter = ""
                if routine_name and routine_name.strip():
                    params["rname"] = routine_name.strip()
                    rnm = (routine_name_match or "exact").lower()
                    rn_filter = f"\n  AND {apply_match_norm('r.name_norm', 'rname', rnm)}"
                cypher = f"""
MATCH (m:MetadataObject {{category_name:'ОбщиеМодули', project_name:$project_name{scope.metadata_map}}})-[:DECLARES]->(r:Routine)
WHERE {mod_cond}{rn_filter}{r_config_filter}
{_icp}
RETURN {routine_fields}{interception_col},
  coalesce(r.owner_qn,'') AS module_owner_qn,
  coalesce(r.file_path,'') AS file_path
ORDER BY name, id SKIP $offset LIMIT $limit
""".strip()
                routine_rows = _run_query(loader, cypher, params, pn)
                unique_qns = sorted({
                    (r.get("module_owner_qn") or "")
                    for r in routine_rows
                    if r.get("module_owner_qn")
                })
                module_rows: List[Dict[str, Any]] = []
                if unique_qns:
                    minfo_cypher = """
MATCH (m:MetadataObject {category_name:'ОбщиеМодули', project_name:$project_name})
WHERE m.qualified_name IN $owner_qns
RETURN coalesce(m.name,'') AS name,
  coalesce(m.config_name,'') AS config_name,
  m.qualified_name AS owner_qn
""".strip()
                    module_rows = _run_query(loader, minfo_cypher, {"owner_qns": list(unique_qns)}, pn)
                shaped = _shape_get_bsl_modules_result(
                    "common_module_routines",
                    routine_rows=routine_rows,
                    module_rows=module_rows,
                )
                return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

            return f"Error: unknown mode='{mode}'."

        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.exception("Error in get_bsl_modules")
            return f"Error: {e}"
    _patch_tool_defaults(get_bsl_modules)
    mcp.tool()(get_bsl_modules)


def _build_subtree_graph(raw_edges: List[Dict], routine_id: str) -> Dict:
    nodes_props: Dict[str, Dict] = {}
    edges: List[Dict] = []
    seen_edges: set = set()

    for row in raw_edges:
        for prefix in ("caller", "callee"):
            nid = row[f"{prefix}_id"]
            if nid not in nodes_props:
                nodes_props[nid] = {
                    "id": nid,
                    "name": row[f"{prefix}_name"],
                    "owner_qn": row[f"{prefix}_owner_qn"],
                    "routine_type": row[f"{prefix}_routine_type"],
                    "directives": row[f"{prefix}_directives"],
                    "area_path": row[f"{prefix}_area_path"],
                }
        edge_key = (row["caller_id"], row["callee_id"], row["side"])
        if edge_key not in seen_edges:
            seen_edges.add(edge_key)
            edges.append({"caller_id": row["caller_id"], "callee_id": row["callee_id"], "side": row["side"]})

    adj: Dict[str, set] = {}
    for e in edges:
        adj.setdefault(e["caller_id"], set()).add(e["callee_id"])
        adj.setdefault(e["callee_id"], set()).add(e["caller_id"])

    depths: Dict[str, int] = {routine_id: 0}
    queue = [routine_id]
    while queue:
        nxt = []
        for nid in queue:
            for nbr in adj.get(nid, set()):
                if nbr not in depths:
                    depths[nbr] = depths[nid] + 1
                    nxt.append(nbr)
        queue = nxt

    routines = [{**props, "depth": depths.get(nid, -1)} for nid, props in nodes_props.items()]
    routines.sort(key=lambda r: (r["depth"], r["name"]))

    return {"routines": routines, "calls": edges}


# ---------------------------------------------------------------------------
# Tool 16: get_bsl_call_graph  (BSL only)
# ---------------------------------------------------------------------------

def _register_get_bsl_call_graph(mcp):
    def get_bsl_call_graph(
        mode: Literal["callees", "callers", "subtree", "between_owners"],
        routine_id: Optional[str] = None,
        direction: Optional[Literal["out", "in", "both"]] = None,
        depth: Optional[int] = None,
        from_owner_qn: Optional[str] = None,
        to_owner_qn: Optional[str] = None,
        config: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        project_name: Optional[str] = None,
    ) -> str:
        """Explore BSL routine call relationships.

Use routine_id from get_bsl_modules, search_bsl_routines, or get_bsl_routine_body.

Modes:
- callees: routines directly called by routine_id.
- callers: routines that directly call routine_id.
- subtree: multi-step callers/callees around routine_id; use direction and depth.
- between_owners: calls from one owner_qn to another; pass from_owner_qn and to_owner_qn.
"""
        loader = _init_loader()
        if loader is None:
            return "Error: Neo4j database connection not available."
        try:
            pn = _resolve_project(project_name)
            config_name = resolve_config(loader, config, pn)
            scope = _scope(config_name)
            lim = clamp_limit(limit)
            off = clamp_offset(offset)
            # Fetch one extra row/path so shaping can derive has_more without a count query.
            params: Dict[str, Any] = {"offset": off, "limit": lim + 1}
            if config_name:
                params["config_name"] = config_name

            proj_prefix_filter = "coalesce(r.owner_qn,'') STARTS WITH ($project_name + '/')"
            # Optional config scope applied to each Routine node when config is set
            _cfg_src = " AND src.config_name = $config_name" if config_name else ""
            _cfg_dst = " AND dst.config_name = $config_name" if config_name else ""

            has_extensions = mode in ("callees", "callers") and bool(_run_query(
                loader,
                "MATCH (c:Configuration {project_name: $project_name, is_extension: true}) RETURN c LIMIT 1",
                {},
                pn,
            ))

            if mode == "callees":
                if not routine_id:
                    return "Error: routine_id is required for mode='callees'."
                params["routine_id"] = routine_id
                cypher = f"""
MATCH (src:Routine {{id:$routine_id}})-[c:CALLS]->(dst:Routine)
WHERE {proj_prefix_filter.replace('r.', 'src.', 1)}{_cfg_src}
  AND {proj_prefix_filter.replace('r.', 'dst.', 1)}{_cfg_dst}
RETURN dst.id AS callee_id, coalesce(dst.name,'') AS callee,
  coalesce(dst.owner_qn,'') AS callee_owner_qn, coalesce(dst.config_name,'') AS config_name,
  coalesce(c.kind,'') AS kind, coalesce(c.count,1) AS count, coalesce(c.lines,[]) AS lines
ORDER BY callee, callee_id SKIP $offset LIMIT $limit
""".strip()
                results = _run_query(loader, cypher, params, pn)
                if has_extensions:
                    results = _enrich_interception(results, "callee_id", loader, pn)
                context = {"mode": "callees", "routine_id": routine_id}
                shaped = _shape_call_graph_page(
                    "callees", context, results, lim=lim, off=off,
                    extract_interceptions=has_extensions, id_field="callee_id",
                )
                return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

            elif mode == "callers":
                if not routine_id:
                    return "Error: routine_id is required for mode='callers'."
                params["routine_id"] = routine_id
                cypher = f"""
MATCH (src:Routine)-[c:CALLS]->(dst:Routine {{id:$routine_id}})
WHERE {proj_prefix_filter.replace('r.', 'src.', 1)}{_cfg_src}
  AND {proj_prefix_filter.replace('r.', 'dst.', 1)}{_cfg_dst}
RETURN src.id AS caller_id, coalesce(src.name,'') AS caller,
  coalesce(src.owner_qn,'') AS caller_owner_qn, coalesce(src.config_name,'') AS config_name,
  coalesce(c.kind,'') AS kind, coalesce(c.count,1) AS count, coalesce(c.lines,[]) AS lines
ORDER BY caller, caller_id SKIP $offset LIMIT $limit
""".strip()
                results = _run_query(loader, cypher, params, pn)
                if has_extensions:
                    results = _enrich_interception(results, "caller_id", loader, pn)
                context = {"mode": "callers", "routine_id": routine_id}
                shaped = _shape_call_graph_page(
                    "callers", context, results, lim=lim, off=off,
                    extract_interceptions=has_extensions, id_field="caller_id",
                )
                return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

            elif mode == "subtree":
                if not routine_id:
                    return "Error: routine_id is required for mode='subtree'."
                params["routine_id"] = routine_id
                d = max(1, min(int(depth or 2), 10))
                dir_ = (direction or "out").lower()
                _cfg_path = " AND n.config_name = $config_name" if config_name else ""
                _edge_cols = """
  caller.id AS caller_id, coalesce(caller.name,'') AS caller_name,
  coalesce(caller.owner_qn,'') AS caller_owner_qn,
  coalesce(caller.routine_type,'') AS caller_routine_type,
  coalesce(caller.directives,[]) AS caller_directives,
  coalesce(caller.area_path,'') AS caller_area_path,
  callee.id AS callee_id, coalesce(callee.name,'') AS callee_name,
  coalesce(callee.owner_qn,'') AS callee_owner_qn,
  coalesce(callee.routine_type,'') AS callee_routine_type,
  coalesce(callee.directives,[]) AS callee_directives,
  coalesce(callee.area_path,'') AS callee_area_path""".strip()

                if dir_ == "out":
                    cypher = f"""
MATCH path=(src:Routine {{id:$routine_id}})-[:CALLS*1..{d}]->(dst:Routine)
WHERE all(n IN nodes(path) WHERE coalesce(n.owner_qn,'') STARTS WITH ($project_name + '/'){_cfg_path})
WITH path, nodes(path) AS ns, size(nodes(path))-1 AS path_depth
ORDER BY path_depth, coalesce(ns[-1].name,''), ns[-1].id
SKIP $offset LIMIT $limit
WITH collect(path) AS win
UNWIND range(0, size(win)-1) AS pi
WITH pi, win[pi] AS path
UNWIND range(0, size(relationships(path))-1) AS i
WITH pi, nodes(path)[i] AS caller, nodes(path)[i+1] AS callee
RETURN DISTINCT {_edge_cols},
  'out' AS side, pi AS path_index
""".strip()
                elif dir_ == "in":
                    cypher = f"""
MATCH path=(caller:Routine)-[:CALLS*1..{d}]->(dst:Routine {{id:$routine_id}})
WHERE all(n IN nodes(path) WHERE coalesce(n.owner_qn,'') STARTS WITH ($project_name + '/'){_cfg_path})
WITH path, nodes(path) AS ns, size(nodes(path))-1 AS path_depth
ORDER BY path_depth, coalesce(ns[0].name,''), ns[0].id
SKIP $offset LIMIT $limit
WITH collect(path) AS win
UNWIND range(0, size(win)-1) AS pi
WITH pi, win[pi] AS path
UNWIND range(0, size(relationships(path))-1) AS i
WITH pi, nodes(path)[i] AS caller, nodes(path)[i+1] AS callee
RETURN DISTINCT {_edge_cols},
  'in' AS side, pi AS path_index
""".strip()
                else:
                    cypher = f"""
CALL {{
  MATCH path=(src:Routine {{id:$routine_id}})-[:CALLS*1..{d}]->(dst:Routine)
  WHERE all(n IN nodes(path) WHERE coalesce(n.owner_qn,'') STARTS WITH ($project_name + '/'){_cfg_path})
  WITH path, nodes(path) AS ns, size(nodes(path))-1 AS path_depth
  RETURN path, 'out' AS side, path_depth, coalesce(ns[-1].name,'') AS sort_name, ns[-1].id AS sort_id
  UNION
  MATCH path=(caller:Routine)-[:CALLS*1..{d}]->(dst:Routine {{id:$routine_id}})
  WHERE all(n IN nodes(path) WHERE coalesce(n.owner_qn,'') STARTS WITH ($project_name + '/'){_cfg_path})
  WITH path, nodes(path) AS ns, size(nodes(path))-1 AS path_depth
  RETURN path, 'in' AS side, path_depth, coalesce(ns[0].name,'') AS sort_name, ns[0].id AS sort_id
}}
WITH path, side, path_depth, sort_name, sort_id
ORDER BY path_depth, side, sort_name, sort_id
SKIP $offset LIMIT $limit
WITH collect({{p: path, s: side}}) AS win
UNWIND range(0, size(win)-1) AS pi
WITH pi, win[pi].p AS path, win[pi].s AS side
UNWIND range(0, size(relationships(path))-1) AS i
WITH pi, side, nodes(path)[i] AS caller, nodes(path)[i+1] AS callee
RETURN DISTINCT {_edge_cols},
  side, pi AS path_index
""".strip()

                raw_edges = _run_query(loader, cypher, params, pn)
                # Path-level pagination: the DB window holds up to lim + 1 ordered paths;
                # path_index == lim marks the extra look-ahead path used only for has_more.
                path_indices = {row["path_index"] for row in raw_edges}
                has_more = lim in path_indices
                kept_edges = [row for row in raw_edges if row["path_index"] < lim]
                used_paths = {row["path_index"] for row in kept_edges}
                graph = _build_subtree_graph(kept_edges, routine_id)
                context = {
                    "mode": "subtree", "routine_id": routine_id,
                    "direction": dir_, "max_depth": d,
                }
                page: Dict[str, Any] = {
                    "unit": "paths", "limit": lim, "offset": off,
                    "returned": len(used_paths), "has_more": has_more,
                }
                if has_more:
                    page["next_offset"] = off + len(used_paths)
                return _fmt_dict(
                    {"context": context, "page": page, **graph},
                    apply_compact_refs=True, normalize_arrays_for_toon=True,
                )

            elif mode == "between_owners":
                if not from_owner_qn or not to_owner_qn:
                    return "Error: from_owner_qn and to_owner_qn are required for mode='between_owners'."
                from_qn = normalize_qn_ref(loader, from_owner_qn.strip(), pn, config_name)
                to_qn = normalize_qn_ref(loader, to_owner_qn.strip(), pn, config_name)
                params["from_qn"] = from_qn
                params["to_qn"] = to_qn
                cypher = """
MATCH (src:Routine)-[:CALLS]->(dst:Routine)
WHERE src.owner_qn = $from_qn
  AND dst.owner_qn = $to_qn
RETURN src.id AS caller_id, coalesce(src.name,'') AS caller,
  dst.id AS callee_id, coalesce(dst.name,'') AS callee,
  coalesce(src.config_name,'') AS config_name
ORDER BY caller, callee SKIP $offset LIMIT $limit
""".strip()
                results = _run_query(loader, cypher, params, pn)
                context = {
                    "mode": "between_owners",
                    "from_owner_qn": from_qn, "to_owner_qn": to_qn,
                }
                shaped = _shape_call_graph_page(
                    "between_owners", context, results, lim=lim, off=off,
                )
                return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)

            return f"Error: unknown mode='{mode}'."

        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.exception("Error in get_bsl_call_graph")
            return f"Error: {e}"
    _patch_tool_defaults(get_bsl_call_graph)
    mcp.tool()(get_bsl_call_graph)


# ---------------------------------------------------------------------------
# Tool 17: find_dependency_paths
# ---------------------------------------------------------------------------

def _register_find_dependency_paths(mcp) -> None:
    def find_dependency_paths(
        start_ref: str,
        direction: Literal["downstream", "upstream", "both"] = "downstream",
        relationship_types: Optional[List[Literal[
            "USED_IN", "DO_MOVEMENTS_IN", "CALLS",
            "BINDS_TO", "LINKS_TO_COMMAND", "HAS_HANDLER", "USES_HANDLER",
        ]]] = None,
        depth: Optional[int] = None,
        config: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        project_name: Optional[str] = None,
    ) -> str:
        """Find dependency paths from a metadata object, element, form/control/event, command, URL method, or BSL routine.

        start_ref accepts object/element refs, full qualified_name, Routine id, and form refs:
        <Категория>.<Объект>.Форма.<ИмяФормы> (или .Формы.). Для контролов/событий предпочтителен
        полный qualified_name из get_form_structure / get_metadata_details.
        direction controls path direction: downstream, upstream, or both.
        relationship_types filters dependency kinds: USED_IN, DO_MOVEMENTS_IN, CALLS,
        BINDS_TO, LINKS_TO_COMMAND, HAS_HANDLER, USES_HANDLER.
        depth limits dependency hops; internal owner bridge hops are not counted.
        config scopes the search to one configuration or extension.
        """
        loader = _init_loader()
        if loader is None:
            return "Error: Neo4j database connection not available."
        try:
            pn = _resolve_project(project_name)
            config_name = resolve_config(loader, config, pn)
            lim = clamp_limit(limit)
            off = clamp_offset(offset)
            d = max(1, min(10, int(depth or 3)))
            rel_types: List[str] = list(relationship_types or [
                "USED_IN", "DO_MOVEMENTS_IN", "CALLS",
                "BINDS_TO", "LINKS_TO_COMMAND", "HAS_HANDLER", "USES_HANDLER",
            ])
            start_resolved, start_label = resolve_start_node(
                loader, start_ref, pn, config_name
            )
            paths = traverse(
                loader, start_resolved, start_label, direction, rel_types,
                d, pn, config_name,
                max_paths=off + lim + 1,
            )
            deduped = dedup_paths(paths)
            window = deduped[off: off + lim + 1]
            fmt = (getattr(settings, "response_format", "text") or "text").lower()
            if fmt == "text":
                visible = window[:lim]
                return format_results_simple(
                    [path_to_text_row(p) for p in visible], max_results=len(visible)
                )
            shaped = _shape_find_dependency_paths_result(window, lim=lim, off=off)
            return _fmt_dict(shaped, apply_compact_refs=True, normalize_arrays_for_toon=True)
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.exception("Error in find_dependency_paths")
            return f"Error: {e}"
    _patch_tool_defaults(find_dependency_paths)
    mcp.tool()(find_dependency_paths)


# ---------------------------------------------------------------------------
# Tool 18: inspect_metadata_object
# ---------------------------------------------------------------------------

_OVERVIEW_PROPS_EXTRA_EXCLUDE = [
    "name", "category_name", "project_name",
    "Имя", "Синоним", "Комментарий",
]


def _dependencies_to_tables(paths_list):
    paths_rows = []
    multi_steps_rows = []
    for path_id, p in enumerate(paths_list, start=1):
        paths_rows.append({
            "path_id": path_id,
            "depth": p.depth,
            "step_count": len(p.steps),
            "start_qn": p.start_ref,
            "start_label": p.start_label,
            "end_qn": p.end_ref,
            "end_label": p.end_label,
            "end_name": p.end_name,
            "end_owner_qn": p.end_owner_ref or "",
            "relationship_chain": list(p.relationship_chain),
        })
        if len(p.steps) > 1:
            for step_no, s in enumerate(p.steps, start=1):
                multi_steps_rows.append({
                    "path_id": path_id,
                    "step_no": step_no,
                    "from_qn": s.from_ref,
                    "from_label": s.from_label,
                    "to_qn": s.to_ref,
                    "to_label": s.to_label,
                    "to_name": s.to_name,
                    "relationship_type": s.relationship_type,
                    "owner_step": s.owner_step,
                })
    return {
        "paths": paths_rows,
        "multi_steps": multi_steps_rows,
        "_hint": "Fields *_qn hold qualified_name, or Routine id when *_label is 'Routine'",
    }


def _register_inspect_metadata_object(mcp):
    def inspect_metadata_object(
        object_ref: str,
        sections: Optional[List[Literal[
            "overview", "structure", "forms", "form_events", "form_attributes",
            "usages", "dependencies", "access", "subscriptions", "bsl", "predefined"
        ]]] = None,
        detail: Literal["brief", "standard", "extended"] = "brief",
        limit_per_section: int = 10,
        config: Optional[str] = None,
        project_name: Optional[str] = None,
    ) -> str:
        """First-step inventory tool: returns what data exists for a metadata object, optionally with limited lists per section.

sections=None or [] → compact card with counts/flags only. NOT equivalent to requesting all sections.
sections=[...], detail="brief"    → same counts, filtered to the requested sections.
sections=[...], detail="standard" → lists of key fields, up to limit_per_section per section.
sections=[...], detail="extended" → lists with extended fields, up to limit_per_section per section.
  extended ≠ unlimited — for procedure bodies, full form trees, full dependency graph use specialized tools.

object_ref accepts: full qualified_name (e.g. "Project/Config/Category/Name"),
  "Category.Name" or "Category/Name", or plain object name (must be unique across categories).

Errors are returned as "Error: ..." strings (not raised).

Note: type usages through ОбщиеРеквизиты (CommonAttribute) are NOT tracked in the underlying graph
and will not appear in usages/dependencies of any tool — known schema-loader limitation.
"""
        loader = _init_loader()
        if loader is None:
            return "Error: Neo4j database connection not available."
        try:
            pn = _resolve_project(project_name)
            VALID_SECTIONS = {
                "overview", "structure", "forms", "form_events", "form_attributes",
                "usages", "dependencies", "access", "subscriptions", "bsl", "predefined"
            }
            VALID_DETAILS = {"brief", "standard", "extended"}
            detail = (detail or "brief").lower()
            if detail not in VALID_DETAILS:
                return f"Error: unknown detail='{detail}'. Allowed: {sorted(VALID_DETAILS)}."
            if isinstance(sections, str):
                sections = [sections]
            secs = list(dict.fromkeys(s.lower() for s in sections)) if sections else None
            if secs:
                bad = [s for s in secs if s not in VALID_SECTIONS]
                if bad:
                    return f"Error: unknown section(s) {bad}. Allowed: {sorted(VALID_SECTIONS)}."
            try:
                lim = max(1, min(100, int(limit_per_section if limit_per_section is not None else 10)))
            except (TypeError, ValueError):
                return f"Error: invalid limit_per_section='{limit_per_section}'. Expected integer."

            config_name = resolve_config(loader, config, pn)
            scope = _scope(config_name)
            load_bsl = bool(getattr(settings, "load_bsl_signatures", False))
            resolved = resolve_object_ref(loader, object_ref, pn, config_name)
            on, cat = resolved["name"], resolved["category_name"]
            has_extensions = bool(_run_query(
                loader,
                "MATCH (c:Configuration {project_name: $project_name, is_extension: true}) RETURN c LIMIT 1",
                {},
                pn,
            ))
            p_base: Dict[str, Any] = {
                "object_name": on, "category_name": cat,
                "object_qn": resolved.get("qualified_name", ""),
                "project_name": pn,
            }
            if config_name:
                p_base["config_name"] = config_name

            def _build_card(requested_sections):
                q1 = f"""
MATCH (m:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
RETURN
  m.name AS object_name, m.category_name AS category,
  m.config_name AS config_name, m.qualified_name AS qualified_name,
  coalesce(m.`Синоним`, m.synonym, '') AS synonym,
  coalesce(m.`Комментарий`, m.comment, '') AS comment,
  size([(m)-[:HAS_ATTRIBUTE]->(:Attribute) | 1])      AS attr_count,
  size([(m)-[:HAS_RESOURCE]->(:Resource) | 1])        AS resource_count,
  size([(m)-[:HAS_DIMENSION]->(:Dimension) | 1])      AS dim_count,
  size([(m)-[:HAS_TABULAR_PART]->(:TabularPart) | 1]) AS tp_count,
  size([(m)-[:HAS_COMMAND]->(:Command) | 1])          AS command_count,
  size([(m)-[:HAS_LAYOUT]->(:Layout) | 1])            AS layout_count,
  size([(m)-[:HAS_FORM]->(:Form) | 1])                AS form_count,
  size([(m)-[:HAS_FORM {{is_default:true}}]->(:Form) | 1]) AS default_form_count,
  size([(m)-[:HAS_FORM]->(:Form)-[:HAS_EVENT]->(:FormEvent) | 1]) AS form_level_event_count,
  size([(m)-[:HAS_PREDEFINED]->(:PredefinedItem) | 1]) AS predefined_count,
  size([(m)-[:HAS_ENUM_VALUE]->(:EnumValue) | 1])     AS enum_value_count,
  size([(m)-[:HAS_TABULAR_PART]->(:TabularPart)-[:HAS_ATTRIBUTE]->(:Attribute) | 1]) AS tabular_attr_count,
  size([(m)-[:HAS_FORM]->(:Form)-[:HAS_FORM_ATTRIBUTE]->(:FormAttribute) | 1])
    + size([(m)-[:HAS_FORM_ATTRIBUTE]->(:FormAttribute) | 1])                       AS form_attr_count,
  size([(m)-[:DO_MOVEMENTS_IN]->(:MetadataObject) | 1]) AS outgoing_movements_count
LIMIT 1
""".strip()
                rows1 = _run_query(loader, q1, p_base, pn)
                if not rows1:
                    raise ValueError(f"MetadataObject '{object_ref}' not found.")
                s = rows1[0]

                q2 = f"""
MATCH (m:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
CALL {{
  WITH m
  CALL {{
    WITH m
    MATCH (m)-[:USED_IN]->(a:Attribute)<-[:HAS_ATTRIBUTE]-(owner:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
    RETURN a AS place, owner
    UNION
    WITH m
    MATCH (m)-[:USED_IN]->(a:Attribute)<-[:HAS_ATTRIBUTE]-(tp:TabularPart)<-[:HAS_TABULAR_PART]-(owner:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
    RETURN a AS place, owner
    UNION
    WITH m
    MATCH (m)-[:USED_IN]->(r:Resource)<-[:HAS_RESOURCE]-(owner:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
    RETURN r AS place, owner
    UNION
    WITH m
    MATCH (m)-[:USED_IN]->(d:Dimension)<-[:HAS_DIMENSION]-(owner:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
    RETURN d AS place, owner
    UNION
    WITH m
    MATCH (m)-[:USED_IN]->(af:AccountingFlag)<-[:HAS_ACCOUNTING_FLAG]-(owner:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
    RETURN af AS place, owner
    UNION
    WITH m
    MATCH (m)-[:USED_IN]->(sf:DimensionAccountingFlag)<-[:HAS_DIMENSION_ACCOUNTING_FLAG]-(owner:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
    RETURN sf AS place, owner
    UNION
    WITH m
    MATCH (m)-[:USED_IN]->(fa:FormAttribute)<-[:HAS_FORM_ATTRIBUTE]-(form:Form)<-[:HAS_FORM]-(owner:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
    RETURN fa AS place, owner
    UNION
    WITH m
    MATCH (m)-[:USED_IN]->(fa:FormAttribute)<-[:HAS_FORM_ATTRIBUTE]-(owner:MetadataObject {{category_name:'ОбщиеФормы', project_name:$project_name{scope.metadata_map}}})
    RETURN fa AS place, owner
  }}
  RETURN count(DISTINCT place) AS usage_paths_count, count(DISTINCT owner) AS usage_objects_count
}}
WITH m, usage_paths_count, usage_objects_count
OPTIONAL MATCH (doc:MetadataObject {{category_name:'Документы', project_name:$project_name{scope.metadata_map}}})-[:DO_MOVEMENTS_IN]->(m)
WITH m, usage_paths_count, usage_objects_count, count(DISTINCT doc) AS movements_count
OPTIONAL MATCH (role:MetadataObject {{category_name:'Роли', project_name:$project_name{scope.metadata_map}}})-[ga:GRANTS_ACCESS_TO]->(m)
WITH m, usage_paths_count, usage_objects_count, movements_count,
     role, collect(DISTINCT ga) AS role_grants
WITH m, usage_paths_count, usage_objects_count, movements_count,
     count(DISTINCT role) AS roles_count,
     count(DISTINCT CASE WHEN any(ga2 IN role_grants
                                  WHERE any(k IN coalesce(ga2.rights_present_en,[])
                                            WHERE ga2[k+'_has_condition'] = true))
                         THEN role END) AS cond_roles_count
OPTIONAL MATCH (m)-[:HAS_EVENT_SUBSCRIPTION]->(es)
RETURN usage_paths_count, usage_objects_count, movements_count,
       roles_count, cond_roles_count, count(es) AS sub_count
""".strip()
                rows2 = _run_query(loader, q2, p_base, pn)
                soc = rows2[0] if rows2 else {
                    "usage_paths_count": 0, "usage_objects_count": 0,
                    "movements_count": 0, "roles_count": 0,
                    "cond_roles_count": 0, "sub_count": 0,
                }

                bsl: Dict[str, Any] = {}
                if load_bsl:
                    q3 = f"""
MATCH (m:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
WITH m
OPTIONAL MATCH (m)-[:DECLARES]->(dr:Routine)
WITH m, [r IN collect(DISTINCT dr) WHERE r IS NOT NULL] AS direct_rts
OPTIONAL MATCH (m)-[:HAS_MODULE]->(mod1:Module)
WITH m, direct_rts, [x IN collect(DISTINCT mod1) WHERE x IS NOT NULL] AS obj_mods
OPTIONAL MATCH (m)-[:HAS_FORM]->(f:Form)-[:HAS_MODULE]->(mod2:Module)
WITH m, direct_rts, obj_mods, [x IN collect(DISTINCT mod2) WHERE x IS NOT NULL] AS form_mods
WITH direct_rts, size(obj_mods) AS object_module_count, size(form_mods) AS form_module_count,
     obj_mods + form_mods AS all_mods
UNWIND CASE WHEN size(all_mods) = 0 THEN [null] ELSE all_mods END AS mod
OPTIONAL MATCH (mod)-[:DECLARES]->(r:Routine) WHERE mod IS NOT NULL
WITH direct_rts, object_module_count, form_module_count,
     [r IN collect(r) WHERE r IS NOT NULL] AS mod_rts
WITH object_module_count, form_module_count,
     direct_rts + [r IN mod_rts WHERE NOT r IN direct_rts] AS routines
RETURN object_module_count, form_module_count,
       size(routines) AS routine_count,
       size([r IN routines WHERE r.export = true | r]) AS exported_count
""".strip()
                    rows3 = _run_query(loader, q3, p_base, pn)
                    bsl = rows3[0] if rows3 else {
                        "object_module_count": 0, "form_module_count": 0,
                        "routine_count": 0, "exported_count": 0,
                    }

                def _include(sec):
                    return requested_sections is None or sec in requested_sections

                sec_data: Dict[str, Any] = {}

                if _include("structure"):
                    sec_data["structure"] = {
                        "attributes": s["attr_count"],
                        "tabular_parts": s["tp_count"],
                        "tabular_attributes": s["tabular_attr_count"],
                        "resources": s["resource_count"],
                        "dimensions": s["dim_count"],
                        "commands": s["command_count"],
                        "layouts": s["layout_count"],
                        "enum_values": s["enum_value_count"],
                    }
                if _include("forms"):
                    sec_data["forms"] = {
                        "count": s["form_count"],
                        "default_forms": s["default_form_count"],
                    }
                if _include("form_events"):
                    sec_data["form_events"] = {"form_level_events": s["form_level_event_count"]}
                if _include("form_attributes"):
                    sec_data["form_attributes"] = {"count": s["form_attr_count"]}
                if _include("usages"):
                    sec_data["usages"] = {
                        "referencing_objects": soc["usage_objects_count"],
                        "referencing_fields": soc["usage_paths_count"],
                        "register_movement_documents": soc["movements_count"],
                    }
                if _include("dependencies"):
                    sec_data["dependencies"] = {
                        "available": (
                            soc["usage_paths_count"] + soc["movements_count"]
                            + s["outgoing_movements_count"]
                        ) > 0,
                        "standard_max_depth": 1,
                        "extended_max_depth": 2,
                    }
                if _include("access"):
                    sec_data["access"] = {
                        "roles_with_access": soc["roles_count"],
                        "roles_with_conditional_rights": soc["cond_roles_count"],
                    }
                if _include("subscriptions"):
                    sec_data["subscriptions"] = {"count": soc["sub_count"]}
                if _include("bsl"):
                    if load_bsl:
                        sec_data["bsl"] = {
                            "available": True,
                            "object_modules": bsl["object_module_count"],
                            "form_modules": bsl["form_module_count"],
                            "routines": bsl["routine_count"],
                            "exported": bsl["exported_count"],
                        }
                    else:
                        sec_data["bsl"] = {
                            "available": False,
                            "reason": "LOAD_BSL_SIGNATURES=false",
                        }
                if _include("predefined"):
                    sec_data["predefined"] = {"count": s["predefined_count"]}
                if requested_sections is not None and "overview" in requested_sections:
                    _ov_synonym = s.get("synonym", "")
                    _ov_comment = s.get("comment", "")
                    if _ov_synonym or _ov_comment:
                        sec_data["overview"] = {
                            "synonym": _ov_synonym,
                            "comment": _ov_comment,
                        }

                _has_data = {
                    "structure": (
                        s["attr_count"] + s["resource_count"] + s["dim_count"]
                        + s["tp_count"] + s["command_count"] + s["layout_count"]
                        + s["enum_value_count"] + s["tabular_attr_count"]
                    ) > 0,
                    "forms": s["form_count"] > 0,
                    "form_events": s["form_level_event_count"] > 0,
                    "form_attributes": s["form_attr_count"] > 0,
                    "usages": soc["usage_objects_count"] > 0,
                    "dependencies": (
                        soc["usage_paths_count"] + soc["movements_count"]
                        + s["outgoing_movements_count"]
                    ) > 0,
                    "access": soc["roles_count"] > 0,
                    "subscriptions": soc["sub_count"] > 0,
                    "bsl": load_bsl and (
                        bsl.get("object_module_count", 0) + bsl.get("form_module_count", 0) > 0
                        or bsl.get("routine_count", 0) > 0
                    ),
                    "predefined": s["predefined_count"] > 0,
                }
                _next_tool = {
                    "structure": "get_metadata_object_structure",
                    "forms": "get_metadata_object_structure",
                    "form_events": "get_form_structure",
                    "form_attributes": "get_form_structure",
                    "usages": "find_metadata_usages",
                    "dependencies": "find_dependency_paths",
                    "access": "get_access_rights",
                    "subscriptions": "get_event_subscriptions",
                    "bsl": "get_bsl_modules",
                    "predefined": "get_metadata_object_structure",
                }
                next_actions = [
                    {"section": sec, "tool": _next_tool[sec]}
                    for sec in _next_tool
                    if _include(sec) and sec in _has_data and _has_data[sec]
                ]
                card: Dict[str, Any] = {
                    "object": s["object_name"],
                    "category": s["category"],
                    "config_name": s["config_name"],
                    "qualified_name": s["qualified_name"],
                    "sections": sec_data,
                }
                if next_actions:
                    card["next_actions"] = next_actions
                if requested_sections is None:
                    card["hint"] = (
                        "sections=None returns this inventory card only (counts/flags). "
                        "Pass sections=[...] with detail='standard' or 'extended' for lists."
                    )
                return _fmt_dict(card)

            def _build_detail(secs):
                _DEP_REL_TYPES = [
                    "USED_IN", "DO_MOVEMENTS_IN", "CALLS",
                    "BINDS_TO", "LINKS_TO_COMMAND", "HAS_HANDLER", "USES_HANDLER",
                ]

                def _fetch_detail_sec(sec):
                    p = dict(p_base)
                    p["limit"] = lim
                    p["offset"] = 0

                    if sec == "overview":
                        _adp = _owner_adoption_block(carry_vars="", parent_var="m") if has_extensions else ""
                        _adp_col = ", adoption" if has_extensions else ""
                        cypher = f"""
MATCH (m:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
{_adp}
RETURN m.name AS name, m.category_name AS category,
       m.qualified_name AS qualified_name, m.config_name AS config_name,
       coalesce(m.`Синоним`, m.synonym, '') AS synonym,
       coalesce(m.`Комментарий`, m.comment, '') AS comment{_adp_col}
LIMIT 1
""".strip()
                        rows = _run_query(loader, cypher, p, pn)
                        if not rows:
                            raise ValueError(f"MetadataObject '{object_ref}' not found.")
                        result = dict(rows[0])
                        if detail == "extended":
                            pq = f"""
MATCH (m:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
RETURN properties(m) AS _raw_props
LIMIT 1
""".strip()
                            prows = _run_query(loader, pq, p_base, pn)
                            if prows:
                                raw = prows[0].get("_raw_props") or {}
                                pruned = {
                                    k: v for k, v in raw.items()
                                    if k != "body" and "embedding" not in k.lower()
                                }
                                result["properties"] = filter_for_summarization(
                                    pruned,
                                    exclude_override=list(
                                        getattr(settings, "metadata_summarize_exclude_fields", []) or []
                                    ) + _OVERVIEW_PROPS_EXTRA_EXCLUDE,
                                )
                        return result

                    elif sec == "structure":
                        subs = ["attributes", "tabular_parts", "resources", "dimensions", "enum_values"]
                        if detail == "extended":
                            subs += ["commands", "layouts"]
                        # (elem_var, elem_label, rel_clause, carry_vars_for_adoption)
                        _elem_cfg = {
                            "attributes":    ("a",  "Attribute",   "[:HAS_ATTRIBUTE]->(a:Attribute)",     "m, a"),
                            "tabular_parts": ("t",  "TabularPart", "[:HAS_TABULAR_PART]->(t:TabularPart)","m, t"),
                            "resources":     ("r",  "Resource",    "[:HAS_RESOURCE]->(r:Resource)",       "m, r"),
                            "dimensions":    ("d",  "Dimension",   "[:HAS_DIMENSION]->(d:Dimension)",     "m, d"),
                            "enum_values":   ("ev", "EnumValue",   "[:HAS_ENUM_VALUE]->(ev:EnumValue)",   "m, ev"),
                            "commands":      ("c",  "Command",     "[:HAS_COMMAND]->(c:Command)",         "m, c"),
                            "layouts":       ("l",  "Layout",      "[:HAS_LAYOUT]->(l:Layout)",           "m, l"),
                        }
                        out = {}
                        for sub in subs:
                            ev, el, rel_cl, carry = _elem_cfg[sub]
                            _adp = _full_elem_adoption_block(ev, el, carry) if has_extensions else ""
                            _adp_col = ", adoption" if has_extensions else ""
                            q = f"""
MATCH (m:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})-{rel_cl}
{_adp}
RETURN {ev}.name AS name, {ev}.qualified_name AS qualified_name,
       m.config_name AS config_name, m.qualified_name AS owner_qn{_adp_col}
ORDER BY name SKIP $offset LIMIT $limit""".strip()
                            rows = _run_query(loader, q, p, pn)
                            if has_extensions:
                                rows = _strip_null_adoption(rows)
                            out[sub] = rows
                        _adp_ta = _full_elem_adoption_block(
                            "a", "Attribute", "m, t, a",
                            parent_var="t", parent_label="TabularPart",
                        ) if has_extensions else ""
                        _adp_col_ta = ", adoption" if has_extensions else ""
                        q_ta = f"""
MATCH (m:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})-[:HAS_TABULAR_PART]->(t:TabularPart)-[:HAS_ATTRIBUTE]->(a:Attribute)
{_adp_ta}
RETURN t.name AS tabular_part, a.name AS name, a.qualified_name AS qualified_name,
       m.config_name AS config_name, t.qualified_name AS owner_qn{_adp_col_ta}
ORDER BY tabular_part, name SKIP $offset LIMIT $limit""".strip()
                        ta_rows = _run_query(loader, q_ta, p, pn)
                        if has_extensions:
                            ta_rows = _strip_null_adoption(ta_rows)
                        out["tabular_attributes"] = ta_rows
                        return out

                    elif sec == "forms":
                        _adp = _full_elem_adoption_block("f", "Form", "m, f, r") if has_extensions else ""
                        _adp_col = ", adoption" if has_extensions else ""
                        cypher = f"""
MATCH (m:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})-[r:HAS_FORM]->(f:Form)
{_adp}
RETURN f.name AS name, f.qualified_name AS qualified_name,
       m.config_name AS config_name, m.qualified_name AS owner_qn,
       r.role AS role, r.is_default AS is_default{_adp_col}
ORDER BY name SKIP $offset LIMIT $limit
""".strip()
                        rows = _run_query(loader, cypher, p, pn)
                        if has_extensions:
                            rows = _strip_null_adoption(rows)
                        return rows

                    elif sec == "form_events":
                        cypher = f"""
MATCH (m:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})-[:HAS_FORM]->(f:Form)-[:HAS_EVENT]->(e:FormEvent)
RETURN f.name AS form_name, e.name AS event_name, e.qualified_name AS qualified_name
ORDER BY form_name, event_name SKIP $offset LIMIT $limit
""".strip()
                        return _run_query(loader, cypher, p, pn)

                    elif sec == "usages":
                        _adp_usages = _owner_adoption_block(
                            carry_vars="config_name, category, name, qualified_name",
                            parent_var="_m",
                        ) if has_extensions else ""
                        _adp_col_usages = ", adoption" if has_extensions else ""
                        union_body = f"""
MATCH (target:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
MATCH (target)-[:USED_IN]->(e)<-[:HAS_ATTRIBUTE]-(owner:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
RETURN DISTINCT owner.config_name AS config_name, owner.category_name AS category,
       owner.name AS name, owner.qualified_name AS qualified_name, owner AS _m
UNION
MATCH (target:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
MATCH (target)-[:USED_IN]->(e)<-[:HAS_ATTRIBUTE]-(tp:TabularPart)<-[:HAS_TABULAR_PART]-(owner:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
RETURN DISTINCT owner.config_name AS config_name, owner.category_name AS category,
       owner.name AS name, owner.qualified_name AS qualified_name, owner AS _m
UNION
MATCH (target:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
MATCH (target)-[:USED_IN]->(r:Resource)<-[:HAS_RESOURCE]-(owner:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
RETURN DISTINCT owner.config_name AS config_name, owner.category_name AS category,
       owner.name AS name, owner.qualified_name AS qualified_name, owner AS _m
UNION
MATCH (target:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
MATCH (target)-[:USED_IN]->(d:Dimension)<-[:HAS_DIMENSION]-(owner:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
RETURN DISTINCT owner.config_name AS config_name, owner.category_name AS category,
       owner.name AS name, owner.qualified_name AS qualified_name, owner AS _m
UNION
MATCH (target:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
MATCH (target)-[:USED_IN]->(af:AccountingFlag)<-[:HAS_ACCOUNTING_FLAG]-(owner:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
RETURN DISTINCT owner.config_name AS config_name, owner.category_name AS category,
       owner.name AS name, owner.qualified_name AS qualified_name, owner AS _m
UNION
MATCH (target:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
MATCH (target)-[:USED_IN]->(sf:DimensionAccountingFlag)<-[:HAS_DIMENSION_ACCOUNTING_FLAG]-(owner:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
RETURN DISTINCT owner.config_name AS config_name, owner.category_name AS category,
       owner.name AS name, owner.qualified_name AS qualified_name, owner AS _m
UNION
MATCH (target:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
MATCH (target)-[:USED_IN]->(fa:FormAttribute)<-[:HAS_FORM_ATTRIBUTE]-(f:Form)<-[:HAS_FORM]-(owner:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
RETURN DISTINCT owner.config_name AS config_name, owner.category_name AS category,
       owner.name AS name, owner.qualified_name AS qualified_name, owner AS _m
UNION
MATCH (target:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
MATCH (target)-[:USED_IN]->(fa:FormAttribute)<-[:HAS_FORM_ATTRIBUTE]-(owner:MetadataObject {{category_name:'ОбщиеФормы', project_name:$project_name{scope.metadata_map}}})
RETURN DISTINCT owner.config_name AS config_name, owner.category_name AS category,
       owner.name AS name, owner.qualified_name AS qualified_name, owner AS _m
""".strip()
                        obj_cypher = (
                            f"CALL {{\n{union_body}\n}}\n"
                            f"{_adp_usages}\n"
                            f"RETURN config_name, category, name, qualified_name{_adp_col_usages}\n"
                            f"ORDER BY config_name, name SKIP $offset LIMIT $limit"
                        ).strip()
                        result: Dict[str, Any] = {"objects": _run_query(loader, obj_cypher, p, pn)}
                        if detail == "extended":
                            paths_body = f"""
MATCH (target:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
MATCH (target)-[:USED_IN]->(a:Attribute)<-[:HAS_ATTRIBUTE]-(m:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
RETURN DISTINCT target.qualified_name AS target_qn, m.config_name AS config_name,
       m.category_name + '.' + m.name + '.Реквизиты.' + a.name AS path
UNION
MATCH (target:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
MATCH (target)-[:USED_IN]->(a:Attribute)<-[:HAS_ATTRIBUTE]-(tp:TabularPart)<-[:HAS_TABULAR_PART]-(m:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
RETURN DISTINCT target.qualified_name AS target_qn, m.config_name AS config_name,
       m.category_name + '.' + m.name + '.ТабличныеЧасти.' + tp.name + '.Реквизиты.' + a.name AS path
UNION
MATCH (target:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
MATCH (target)-[:USED_IN]->(r:Resource)<-[:HAS_RESOURCE]-(m:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
RETURN DISTINCT target.qualified_name AS target_qn, m.config_name AS config_name,
       m.category_name + '.' + m.name + '.Ресурсы.' + r.name AS path
UNION
MATCH (target:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
MATCH (target)-[:USED_IN]->(d:Dimension)<-[:HAS_DIMENSION]-(m:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
RETURN DISTINCT target.qualified_name AS target_qn, m.config_name AS config_name,
       m.category_name + '.' + m.name + '.Измерения.' + d.name AS path
UNION
MATCH (target:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
MATCH (target)-[:USED_IN]->(af:AccountingFlag)<-[:HAS_ACCOUNTING_FLAG]-(m:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
RETURN DISTINCT target.qualified_name AS target_qn, m.config_name AS config_name,
       m.category_name + '.' + m.name + '.ПризнакиУчета.' + af.name AS path
UNION
MATCH (target:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
MATCH (target)-[:USED_IN]->(sf:DimensionAccountingFlag)<-[:HAS_DIMENSION_ACCOUNTING_FLAG]-(m:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
RETURN DISTINCT target.qualified_name AS target_qn, m.config_name AS config_name,
       m.category_name + '.' + m.name + '.ПризнакиУчетаСубконто.' + sf.name AS path
UNION
MATCH (target:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
MATCH (target)-[:USED_IN]->(fa:FormAttribute)<-[:HAS_FORM_ATTRIBUTE]-(f:Form)<-[:HAS_FORM]-(m:MetadataObject {{project_name:$project_name{scope.metadata_map}}})
RETURN DISTINCT target.qualified_name AS target_qn, m.config_name AS config_name,
       m.category_name + '.' + m.name + '.Формы.' + f.name + '.Реквизиты.' + fa.name AS path
UNION
MATCH (target:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
MATCH (target)-[:USED_IN]->(fa:FormAttribute)<-[:HAS_FORM_ATTRIBUTE]-(m:MetadataObject {{category_name:'ОбщиеФормы', project_name:$project_name{scope.metadata_map}}})
RETURN DISTINCT target.qualified_name AS target_qn, m.config_name AS config_name,
       'ОбщиеФормы.' + m.name + '.Реквизиты.' + fa.name AS path
""".strip()
                            paths_cypher = (
                                f"CALL {{\n{paths_body}\n}}\n"
                                f"RETURN target_qn, config_name, path\n"
                                f"ORDER BY config_name, path SKIP $offset LIMIT $limit"
                            )
                            result["paths"] = _run_query(loader, paths_cypher, p, pn)
                        return result

                    elif sec == "dependencies":
                        start_ref_val, start_label = resolve_start_node(
                            loader, resolved["qualified_name"],
                            pn, config_name,
                        )
                        max_d = 1 if detail == "standard" else 2
                        paths = traverse(
                            loader, start_ref_val, start_label, "downstream",
                            _DEP_REL_TYPES, max_d, pn, config_name,
                            max_paths=lim,
                        )
                        deduped = dedup_paths(paths)[:lim]
                        tables = _dependencies_to_tables(deduped)
                        out_fmt = (getattr(settings, "response_format", "json") or "json").lower()
                        if out_fmt == "toon":
                            for row in tables["paths"]:
                                row["relationship_chain"] = " → ".join(row["relationship_chain"])
                        return tables

                    elif sec == "access":
                        rights_expr = (
                            "[k IN coalesce(rel.rights_present_en, []) | {"
                            "right_ru: m[k + '_ru'], allowed: m[k + '_allowed'], "
                            "has_condition: coalesce(m[k + '_has_condition'], false)}]"
                        )
                        p_acc = dict(p_base)
                        p_acc["target_qn"] = resolved.get("qualified_name", "")
                        p_acc["limit"] = lim
                        p_acc["offset"] = 0
                        if detail == "standard":
                            cypher = f"""
MATCH (r:MetadataObject {{category_name:'Роли', project_name:$project_name{scope.metadata_map}}})-[rel:GRANTS_ACCESS_TO]->(t {{qualified_name:$target_qn}})
WITH r, rel, properties(rel) AS m
WITH r, {rights_expr} AS rights
RETURN r.name AS role, r.qualified_name AS role_qn, r.config_name AS config_name,
       size(rights) AS rights_count,
       any(ri IN rights WHERE ri.has_condition = true) AS has_conditions
ORDER BY role SKIP $offset LIMIT $limit
""".strip()
                            return _run_query(loader, cypher, p_acc, pn)
                        cypher = f"""
MATCH (r:MetadataObject {{category_name:'Роли', project_name:$project_name{scope.metadata_map}}})-[rel:GRANTS_ACCESS_TO]->(t {{qualified_name:$target_qn}})
WITH r, rel, properties(rel) AS m
WITH r, {rights_expr} AS rights
RETURN r.name AS role, r.qualified_name AS role_qn, r.config_name AS config_name, rights
ORDER BY role SKIP $offset LIMIT $limit
""".strip()
                        rows = _run_query(loader, cypher, p_acc, pn)
                        roles_out: List[Dict[str, Any]] = []
                        rights_out: List[Dict[str, Any]] = []
                        for row in rows:
                            role_qn_val = row.get("role_qn", "")
                            role_rights = row.get("rights") or []
                            roles_out.append({
                                "role": row.get("role", ""),
                                "role_qn": role_qn_val,
                                "config_name": row.get("config_name", ""),
                                "rights_count": len(role_rights),
                            })
                            for ri in role_rights:
                                rights_out.append({
                                    "role_qn": role_qn_val,
                                    "right_ru": ri.get("right_ru", ""),
                                    "allowed": ri.get("allowed", False),
                                    "has_condition": ri.get("has_condition", False),
                                })
                        return {"roles": roles_out, "rights": rights_out}

                    elif sec == "subscriptions":
                        p_sub = dict(p_base)
                        p_sub["limit"] = lim
                        p_sub["offset"] = 0
                        cypher = f"""
MATCH (m:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
MATCH (m)-[:HAS_EVENT_SUBSCRIPTION]->(es:MetadataObject)
WHERE es.category_name = 'ПодпискиНаСобытия'
RETURN es.name AS subscription, es.config_name AS config_name, es.qualified_name AS qualified_name,
  coalesce(es.`Событие`, es.event_en, '') AS event,
  m.name AS source_object, m.category_name AS source_category, m.qualified_name AS source_qn
ORDER BY subscription SKIP $offset LIMIT $limit
""".strip()
                        return _run_query(loader, cypher, p_sub, pn)

                    elif sec == "bsl":
                        if not load_bsl:
                            return {"available": False, "reason": "LOAD_BSL_SIGNATURES=false"}
                        p_bsl = dict(p_base)
                        p_bsl["limit"] = lim
                        p_bsl["offset"] = 0
                        info_q = f"""
MATCH (m:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
WITH m
OPTIONAL MATCH (m)-[:DECLARES]->(dr:Routine)
WITH m, [r IN collect(DISTINCT dr) WHERE r IS NOT NULL] AS direct_rts
OPTIONAL MATCH (m)-[:HAS_MODULE]->(mod1:Module)
WITH m, direct_rts, [x IN collect(DISTINCT mod1) WHERE x IS NOT NULL] AS obj_mods
OPTIONAL MATCH (m)-[:HAS_FORM]->(f:Form)-[:HAS_MODULE]->(mod2:Module)
WITH m, direct_rts, obj_mods, [x IN collect(DISTINCT mod2) WHERE x IS NOT NULL] AS form_mods
WITH direct_rts, [x IN obj_mods + form_mods WHERE x IS NOT NULL] AS all_mods
UNWIND CASE WHEN size(all_mods) = 0 THEN [null] ELSE all_mods END AS mod
OPTIONAL MATCH (mod)-[:DECLARES]->(r:Routine) WHERE mod IS NOT NULL
WITH direct_rts, all_mods, [r IN collect(r) WHERE r IS NOT NULL] AS mod_rts
WITH all_mods,
     direct_rts + [r IN mod_rts WHERE NOT r IN direct_rts] AS routines
RETURN [x IN all_mods | coalesce(x.name, '')] AS module_names,
       size(routines) AS routine_count,
       size([r IN routines WHERE r.export = true]) AS exported_count
""".strip()
                        info_rows = _run_query(loader, info_q, p_bsl, pn)
                        info = info_rows[0] if info_rows else {
                            "module_names": [], "routine_count": 0, "exported_count": 0,
                        }
                        if detail == "standard":
                            return {
                                "available": True,
                                "modules": info.get("module_names") or [],
                                "routine_count": info["routine_count"],
                                "exported_count": info["exported_count"],
                            }
                        else:
                            rout_q = f"""
CALL {{
MATCH (m:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
MATCH (m)-[:DECLARES]->(r:Routine)
RETURN r
UNION
MATCH (m:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})
WITH m
OPTIONAL MATCH (m)-[:HAS_MODULE]->(mod1:Module)
WITH m, [x IN collect(DISTINCT mod1) WHERE x IS NOT NULL] AS obj_mods
OPTIONAL MATCH (m)-[:HAS_FORM]->(f:Form)-[:HAS_MODULE]->(mod2:Module)
WITH obj_mods, [x IN collect(DISTINCT mod2) WHERE x IS NOT NULL] AS form_mods
WITH [x IN obj_mods + form_mods WHERE x IS NOT NULL] AS all_mods
UNWIND CASE WHEN size(all_mods) = 0 THEN [null] ELSE all_mods END AS mod
OPTIONAL MATCH (mod)-[:DECLARES]->(r:Routine) WHERE mod IS NOT NULL
WITH r WHERE r IS NOT NULL
RETURN r
}}
RETURN r.name AS name, coalesce(r.routine_type, '') AS routine_type,
       coalesce(r.export, false) AS export
ORDER BY name SKIP $offset LIMIT $limit
""".strip()
                            return {
                                "available": True,
                                "modules": info.get("module_names") or [],
                                "routines": _run_query(loader, rout_q, p_bsl, pn),
                            }

                    elif sec == "form_attributes":
                        p_fa = dict(p)
                        is_common_form = (cat == "ОбщиеФормы")
                        if has_extensions:
                            p_fa["project_prefix"] = pn + "/"
                        if is_common_form:
                            _adp = _cf_child_adoption_block(
                                "fa", "FormAttribute", "m, fa",
                            ) if has_extensions else ""
                            _adp_col = ", adoption" if has_extensions else ""
                            cypher = f"""
MATCH (m:MetadataObject {{category_name: 'ОбщиеФормы', qualified_name: $object_qn, project_name: $project_name}})-[:HAS_FORM_ATTRIBUTE]->(fa:FormAttribute)
{_adp}
RETURN m.name AS form, fa.name AS name, fa.qualified_name AS qualified_name,
       m.config_name AS config_name, m.qualified_name AS owner_qn{_adp_col}
ORDER BY form, name SKIP $offset LIMIT $limit""".strip()
                        else:
                            _adp = _form_child_adoption_block(
                                "fa", "FormAttribute", "m, f, fa",
                            ) if has_extensions else ""
                            _adp_col = ", adoption" if has_extensions else ""
                            cypher = f"""
MATCH (m:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})-[:HAS_FORM]->(f:Form)-[:HAS_FORM_ATTRIBUTE]->(fa:FormAttribute)
{_adp}
RETURN f.name AS form, fa.name AS name, fa.qualified_name AS qualified_name,
       m.config_name AS config_name, f.qualified_name AS owner_qn{_adp_col}
ORDER BY form, name SKIP $offset LIMIT $limit""".strip()
                        rows = _run_query(loader, cypher, p_fa, pn)
                        if has_extensions:
                            rows = _strip_null_adoption(rows)
                        return rows

                    elif sec == "predefined":
                        cypher = f"""
MATCH (m:MetadataObject {{qualified_name: $object_qn, project_name: $project_name}})-[:HAS_PREDEFINED]->(pi:PredefinedItem)
RETURN pi.`Имя` AS name, m.config_name AS config_name, m.qualified_name AS owner_qn,
       pi.qualified_name AS qualified_name,
       coalesce(pi.`Код`, '') AS code, coalesce(pi.`Наименование`, '') AS description
ORDER BY name SKIP $offset LIMIT $limit
""".strip()
                        return _run_query(loader, cypher, p, pn)

                    else:
                        raise ValueError(f"unknown section '{sec}'.")

                combined: Dict[str, Any] = {}
                for sec in secs:
                    combined[sec] = _fetch_detail_sec(sec)
                return _fmt_dict(combined, apply_compact_refs=True)

            if not secs:
                return _build_card(None)
            if detail == "brief":
                return _build_card(secs)
            return _build_detail(secs)

        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.exception("Error in inspect_metadata_object")
            return f"Error: {e}"
    _patch_tool_defaults(inspect_metadata_object)
    mcp.tool()(inspect_metadata_object)


# ---------------------------------------------------------------------------
# Tool 19: get_extension_object_diff
# ---------------------------------------------------------------------------

def _is_scalar_diff_value(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _diff_value_stable_str(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _shape_get_extension_object_diff_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten the nested per-extension diff into top-level tables.

    extension_id (ext1, ext2, ...) and change_id (ch1, ch2, ...) are
    response-local keys linking the tables; they are not graph ids. Property
    diffs where both sides are scalar go to property_changes; a diff with an
    array on either side is exploded into complex_property_values rows keyed
    by (change_id, property, side) and ordered by index.
    """
    shaped: Dict[str, Any] = {
        "object": {
            "object_ref": result.get("object_ref"),
            "object_name": result.get("object_name"),
            "category": result.get("category"),
        },
        "extensions": [],
        "counts": [],
        "metadata_changes": [],
        "property_changes": [],
        "complex_property_values": [],
        "code_changes": [],
    }
    change_seq = 0

    def _next_change_id() -> str:
        nonlocal change_seq
        change_seq += 1
        return f"ch{change_seq}"

    def _emit_property(change_id: str, prop: Any, base_value: Any, extension_value: Any) -> None:
        if _is_scalar_diff_value(base_value) and _is_scalar_diff_value(extension_value):
            shaped["property_changes"].append({
                "change_id": change_id, "property": prop,
                "base_value": base_value, "extension_value": extension_value,
            })
            return
        for side, value in (("base", base_value), ("extension", extension_value)):
            if value is None:
                continue
            if isinstance(value, list):
                items = value
            else:
                items = [value]
            for idx, item in enumerate(items):
                shaped["complex_property_values"].append({
                    "change_id": change_id, "property": prop, "side": side, "index": idx,
                    "value": item if _is_scalar_diff_value(item) else _diff_value_stable_str(item),
                })

    _COUNT_SECTIONS = {"code": "bsl"}
    _CODE_COUNT_KINDS = {"modules": "Module", "routines": "Routine"}

    for i, ext in enumerate(result.get("extensions") or [], 1):
        ext_id = f"ext{i}"
        shaped["extensions"].append({
            "extension_id": ext_id,
            "extension_config_name": ext.get("extension_config_name"),
            "base_config_name": ext.get("base_config_name"),
            "object_state": ext.get("object_state"),
            "extension_qn": ext.get("extension_qn"),
            "base_qn": ext.get("base_qn"),
            "truncated": bool(ext.get("truncated", False)),
        })

        for section, kinds in (ext.get("counts") or {}).items():
            sec = _COUNT_SECTIONS.get(section, section)
            if section == "forms":
                shaped["counts"].append({"extension_id": ext_id, "section": sec, "kind": "Form", **kinds})
            else:
                kind_map = _CODE_COUNT_KINDS if section == "code" else {}
                for kind, cnt in kinds.items():
                    shaped["counts"].append({
                        "extension_id": ext_id, "section": sec,
                        "kind": kind_map.get(kind, kind), **cnt,
                    })

        for row in ext.get("metadata_changes") or []:
            change_id = _next_change_id()
            shaped["metadata_changes"].append({
                "change_id": change_id,
                "extension_id": ext_id,
                "section": row.get("section"),
                "kind": row.get("kind"),
                "name": row.get("name"),
                "change": row.get("change"),
                "form_name": row.get("form_name"),
                "extension_qn": row.get("extension_qn"),
                "base_qn": row.get("base_qn"),
            })
            for pc in row.get("property_changes") or []:
                _emit_property(change_id, pc.get("property"),
                               pc.get("base_value"), pc.get("extension_value"))

        for row in ext.get("code_changes") or []:
            interception = row.get("interception") or {}
            name = row.get("name")
            if name is None:
                name = row.get("module_name")
            ext_node_id = row.get("extension_module_id") or row.get("extension_routine_id")
            base_node_id = row.get("base_module_id") or row.get("base_routine_id")
            shaped["code_changes"].append({
                "change_id": _next_change_id(),
                "extension_id": ext_id,
                "kind": row.get("kind"),
                "name": name,
                "module_type": row.get("module_type"),
                "change": row.get("change"),
                "owner_qn": row.get("owner_qn"),
                "extension_node_id": ext_node_id,
                "base_node_id": base_node_id,
                "decorator_type": interception.get("decorator_type"),
                "target": interception.get("target"),
            })

    return shaped


def _register_get_extension_object_diff(mcp):

    _STRUCT_TYPES = {
        "Attribute":   "HAS_ATTRIBUTE",
        "TabularPart": "HAS_TABULAR_PART",
        "Resource":    "HAS_RESOURCE",
        "Dimension":   "HAS_DIMENSION",
        "EnumValue":   "HAS_ENUM_VALUE",
        "Layout":      "HAS_LAYOUT",
        "Command":     "HAS_COMMAND",
    }

    _DIFF_EXCL = [
        "content_hash", "ext_source", "config_name", "project_name",
        "qualified_name", "name", "Идентификатор", "modified_properties",
        "id", "owner_qn", "base_control_id",
    ]

    def _base_cn(loader, ext_cn, pn):
        rows = _run_query(loader,
            "MATCH (ec:Configuration {name:$ext_cn, project_name:$pn})-[:EXTENDS]->(bc:Configuration)"
            " RETURN bc.name AS base_cn LIMIT 1",
            {"ext_cn": ext_cn, "pn": pn}, pn)
        return rows[0]["base_cn"] if rows else None

    def _all_extensions(loader, pn):
        rows = _run_query(loader,
            "MATCH (c:Configuration {project_name:$pn, is_extension:true})"
            " RETURN c.name AS ext_cn ORDER BY c.name",
            {"pn": pn}, pn)
        return [r["ext_cn"] for r in rows]

    def _object_state(loader, obj_name, cat_name, ext_cn, bcn, pn):
        rows = _run_query(loader, """
OPTIONAL MATCH (eo:MetadataObject {name:$obj_name, category_name:$cat_name, config_name:$ext_cn, project_name:$pn})
OPTIONAL MATCH (eo)-[:ADOPTED_FROM]->(lb:MetadataObject)
OPTIONAL MATCH (bo:MetadataObject {name:$obj_name, category_name:$cat_name, config_name:$bcn, project_name:$pn})
RETURN eo.qualified_name AS extension_qn, (lb IS NOT NULL) AS has_adoption, bo.qualified_name AS base_qn
LIMIT 1""".strip(),
            {"obj_name": obj_name, "cat_name": cat_name, "ext_cn": ext_cn,
             "bcn": bcn or "", "pn": pn}, pn)
        if not rows:
            return "not_found", None, None
        r = rows[0]
        extension_qn = r.get("extension_qn")
        if extension_qn is None:
            return "not_found", None, r.get("base_qn")
        return ("adopted" if r.get("has_adoption") else "extension_only"), extension_qn, r.get("base_qn")

    def _struct_counts(loader, obj_name, cat_name, ext_cn, bcn, pn):
        result = {}
        p = {"obj_name": obj_name, "cat": cat_name, "ext_cn": ext_cn, "bcn": bcn or "", "pn": pn}
        for label, rel in _STRUCT_TYPES.items():
            q = f"""
OPTIONAL MATCH (eo:MetadataObject {{name:$obj_name, category_name:$cat, config_name:$ext_cn, project_name:$pn}})
OPTIONAL MATCH (bo:MetadataObject {{name:$obj_name, category_name:$cat, config_name:$bcn, project_name:$pn}})
OPTIONAL MATCH (eo)-[:{rel}]->(ee:{label})-[:ADOPTED_FROM]->(be:{label})
WITH eo, bo, count(DISTINCT ee) AS adp
OPTIONAL MATCH (eo)-[:{rel}]->(oe:{label}) WHERE NOT EXISTS {{ (oe)-[:ADOPTED_FROM]->() }}
WITH eo, bo, adp, count(DISTINCT oe) AS ext
OPTIONAL MATCH (bo)-[:{rel}]->(b0:{label})
  WHERE NOT EXISTS {{
    (:MetadataObject {{name:$obj_name, category_name:$cat, config_name:$ext_cn, project_name:$pn}})
    -[:{rel}]->(:{label})-[:ADOPTED_FROM]->(b0)
  }}
RETURN adp, ext, count(DISTINCT b0) AS base_only""".strip()
            rows = _run_query(loader, q, p, pn)
            r = rows[0] if rows else {}
            result[label] = {
                "extension_only": int(r.get("ext") or 0),
                "base_only": int(r.get("base_only") or 0),
                "adopted": int(r.get("adp") or 0),
            }
        return result

    def _form_counts(loader, obj_name, cat_name, ext_cn, bcn, pn):
        p = {"obj_name": obj_name, "cat": cat_name, "ext_cn": ext_cn, "bcn": bcn or "", "pn": pn}

        def _cnt(q):
            rows = _run_query(loader, q, p, pn)
            return int(rows[0].get("n") or 0) if rows else 0

        adp = _cnt(
            "MATCH (eo:MetadataObject {name:$obj_name, category_name:$cat, config_name:$ext_cn,"
            " project_name:$pn})-[:HAS_FORM]->(f:Form)-[:ADOPTED_FROM]->(:Form)"
            " RETURN count(f) AS n"
        )
        ext = _cnt(
            "MATCH (eo:MetadataObject {name:$obj_name, category_name:$cat, config_name:$ext_cn,"
            " project_name:$pn})-[:HAS_FORM]->(f:Form)"
            " WHERE NOT EXISTS { (f)-[:ADOPTED_FROM]->() } RETURN count(f) AS n"
        )
        base_only = _cnt("""
MATCH (bo:MetadataObject {name:$obj_name, category_name:$cat, config_name:$bcn, project_name:$pn})
      -[:HAS_FORM]->(f:Form)
WHERE NOT EXISTS {
  (:MetadataObject {name:$obj_name, category_name:$cat, config_name:$ext_cn, project_name:$pn})
  -[:HAS_FORM]->(:Form)-[:ADOPTED_FROM]->(f)
}
RETURN count(f) AS n""".strip()) if bcn else 0
        return {"adopted": adp, "extension_only": ext, "base_only": base_only}

    def _fi_counts(loader, obj_name, cat_name, ext_cn, pn):
        is_cf = cat_name == "ОбщиеФормы"
        p = {"obj_name": obj_name, "cat": cat_name, "ext_cn": ext_cn, "pn": pn}

        def _r(q):
            return _run_query(loader, q, p, pn)

        if is_cf:
            _fp = (
                "MATCH (eo:MetadataObject {name:$obj_name, category_name:'ОбщиеФормы',"
                " config_name:$ext_cn, project_name:$pn})-[:ADOPTED_FROM]->(:MetadataObject)\n"
            )
            _q_fa = _fp + "MATCH (eo)-[:HAS_FORM_ATTRIBUTE]->(fa:FormAttribute) RETURN fa.ext_source AS s, count(fa) AS n"
            _q_fc = _fp + "MATCH (eo)-[:HAS_CONTROL|HAS_CHILD*]->(fc:FormControl) RETURN coalesce(fc.ext_source,'') AS s, count(fc) AS n"
            _q_cmd = _fp + "MATCH (eo)-[:HAS_COMMAND]->(cmd:Command) RETURN coalesce(cmd.ext_source,'') AS s, count(cmd) AS n"
            _q_fe_fl = _fp + "MATCH (eo)-[:HAS_EVENT]->(evt:FormEvent)\nOPTIONAL MATCH (evt)-[:ADOPTED_FROM]->(be:FormEvent)\nRETURN evt.qualified_name AS qn, (be IS NOT NULL) AS is_adp"
            _q_fe_cl = _fp + "MATCH (eo)-[:HAS_CONTROL|HAS_CHILD*]->(fc:FormControl)-[:HAS_EVENT]->(evt:FormEvent)\nOPTIONAL MATCH (evt)-[:ADOPTED_FROM]->(be:FormEvent)\nRETURN evt.qualified_name AS qn, (be IS NOT NULL) AS is_adp"
        else:
            _fp = (
                "MATCH (eo:MetadataObject {name:$obj_name, category_name:$cat,"
                " config_name:$ext_cn, project_name:$pn})-[:HAS_FORM]->(ef:Form)-[:ADOPTED_FROM]->(:Form)\n"
            )
            _q_fa = _fp + "MATCH (ef)-[:HAS_FORM_ATTRIBUTE]->(fa:FormAttribute) RETURN fa.ext_source AS s, count(fa) AS n"
            _q_fc = _fp + "MATCH (ef)-[:HAS_CONTROL|HAS_CHILD*]->(fc:FormControl) RETURN coalesce(fc.ext_source,'') AS s, count(fc) AS n"
            _q_cmd = _fp + "MATCH (ef)-[:HAS_COMMAND]->(cmd:Command) RETURN coalesce(cmd.ext_source,'') AS s, count(cmd) AS n"
            _q_fe_fl = _fp + "MATCH (ef)-[:HAS_EVENT]->(evt:FormEvent)\nOPTIONAL MATCH (evt)-[:ADOPTED_FROM]->(be:FormEvent)\nRETURN evt.qualified_name AS qn, (be IS NOT NULL) AS is_adp"
            _q_fe_cl = _fp + "MATCH (ef)-[:HAS_CONTROL|HAS_CHILD*]->(fc:FormControl)-[:HAS_EVENT]->(evt:FormEvent)\nOPTIONAL MATCH (evt)-[:ADOPTED_FROM]->(be:FormEvent)\nRETURN evt.qualified_name AS qn, (be IS NOT NULL) AS is_adp"

        def _src_cnt(rows, has_unch=True):
            d: Dict[str, int] = {"extension_only": 0, "modified": 0, "adopted": 0}
            if has_unch:
                d["unchanged"] = 0
            for r in rows:
                s, n = r.get("s") or "", int(r.get("n") or 0)
                if s == "own":
                    d["extension_only"] += n
                elif s == "adopted_modified":
                    d["modified"] += n
                elif s == "adopted_unchanged" and has_unch:
                    d["unchanged"] += n
                else:
                    d["adopted"] += n
            return d

        seen_evt: Dict[str, bool] = {}
        for q_fe in [_q_fe_fl, _q_fe_cl]:
            for r in _r(q_fe):
                qn = r.get("qn") or ""
                if qn not in seen_evt:
                    seen_evt[qn] = bool(r.get("is_adp"))
        fe_cnt: Dict[str, int] = {"extension_only": 0, "adopted": 0}
        for is_adp in seen_evt.values():
            if is_adp:
                fe_cnt["adopted"] += 1
            else:
                fe_cnt["extension_only"] += 1

        return {
            "FormAttribute": _src_cnt(_r(_q_fa), has_unch=True),
            "FormControl":   _src_cnt(_r(_q_fc), has_unch=True),
            "FormEvent":     fe_cnt,
            "Command":       _src_cnt(_r(_q_cmd), has_unch=False),
        }

    def _bsl_counts(loader, obj_name, cat_name, ext_cn, pn):
        p = {"obj_name": obj_name, "cat": cat_name, "ext_cn": ext_cn, "pn": pn}
        q_mods = """
MATCH (eo:MetadataObject {name:$obj_name, category_name:$cat, config_name:$ext_cn, project_name:$pn})
OPTIONAL MATCH (eo)-[:HAS_MODULE]->(m1:Module)
WITH eo, [m IN collect(DISTINCT m1) WHERE m IS NOT NULL] AS obj_mods
OPTIONAL MATCH (eo)-[:HAS_FORM]->(:Form)-[:HAS_MODULE]->(m2:Module)
WITH obj_mods, [m IN collect(DISTINCT m2) WHERE m IS NOT NULL] AS form_mods
WITH obj_mods + form_mods AS all_mods
UNWIND CASE WHEN size(all_mods) = 0 THEN [null] ELSE all_mods END AS m
OPTIONAL MATCH (m)-[:EXTENDS_MODULE]->(bm:Module) WHERE m IS NOT NULL
RETURN count(DISTINCT m) AS total_mods, count(DISTINCT bm) AS extends_mods""".strip()
        mod_rows = _run_query(loader, q_mods, p, pn)
        total_mods = int((mod_rows[0].get("total_mods") or 0) if mod_rows else 0)
        extends_mods = int((mod_rows[0].get("extends_mods") or 0) if mod_rows else 0)
        seen_rid: set = set()
        for q_r in [
            "MATCH (eo:MetadataObject {name:$obj_name, category_name:$cat, config_name:$ext_cn,"
            " project_name:$pn})-[:HAS_MODULE]->(m:Module)-[:DECLARES]->(r:Routine) RETURN r.id AS rid",
            "MATCH (eo:MetadataObject {name:$obj_name, category_name:$cat, config_name:$ext_cn,"
            " project_name:$pn})-[:HAS_FORM]->(:Form)-[:HAS_MODULE]->(m:Module)-[:DECLARES]->(r:Routine) RETURN r.id AS rid",
            "MATCH (eo:MetadataObject {name:$obj_name, category_name:$cat, config_name:$ext_cn,"
            " project_name:$pn})-[:DECLARES]->(r:Routine) RETURN r.id AS rid",
        ]:
            for r in _run_query(loader, q_r, p, pn):
                rid = r.get("rid")
                if rid:
                    seen_rid.add(rid)
        total_rout = len(seen_rid)
        extends_rout = 0
        if seen_rid:
            er = _run_query(loader,
                "UNWIND $ids AS rid MATCH (r:Routine {id:rid})-[:EXTENDS_ROUTINE]->(:Routine)"
                " RETURN count(DISTINCT rid) AS cnt",
                {"ids": list(seen_rid)}, pn)
            extends_rout = int((er[0].get("cnt") or 0) if er else 0)
        return {
            "modules":  {"extension_only": total_mods - extends_mods, "extends": extends_mods},
            "routines": {"extension_only": total_rout - extends_rout, "intercepts": extends_rout},
        }

    def _build_counts(loader, obj_name, cat_name, ext_cn, bcn, pn):
        counts: Dict[str, Any] = {
            "structure":  _struct_counts(loader, obj_name, cat_name, ext_cn, bcn, pn),
            "forms":      _form_counts(loader, obj_name, cat_name, ext_cn, bcn, pn),
            "form_items": _fi_counts(loader, obj_name, cat_name, ext_cn, pn),
        }
        if bool(getattr(settings, "load_bsl_signatures", False)):
            counts["code"] = _bsl_counts(loader, obj_name, cat_name, ext_cn, pn)
        return counts

    def _build_structure(loader, obj_name, cat_name, ext_cn, bcn, pn, detail, lim):
        items: List[Dict] = []
        truncated = False
        p = {"obj_name": obj_name, "cat": cat_name, "ext_cn": ext_cn, "bcn": bcn or "",
             "pn": pn, "lim": lim, "excl_keys": _DIFF_EXCL}
        for label, rel in _STRUCT_TYPES.items():
            if detail == "extended":
                q_adp = f"""
MATCH (eo:MetadataObject {{name:$obj_name, category_name:$cat, config_name:$ext_cn, project_name:$pn}})
      -[:{rel}]->(ee:{label})-[:ADOPTED_FROM]->(be:{label})
WITH ee, be,
     [k IN keys(ee) WHERE NOT k IN $excl_keys AND (NOT k IN keys(be) OR ee[k] <> be[k])] +
     [k IN keys(be) WHERE NOT k IN $excl_keys AND NOT k IN keys(ee)] AS chg
RETURN ee.name AS name, ee.qualified_name AS extension_qn, be.qualified_name AS base_qn,
       CASE WHEN size(chg) > 0 THEN 'modified' ELSE 'adopted' END AS change,
       [k IN chg | {{
         property: k,
         extension_value: CASE WHEN k IN keys(ee) THEN ee[k] ELSE null END,
         base_value:      CASE WHEN k IN keys(be) THEN be[k] ELSE null END
       }}] AS property_changes
ORDER BY name LIMIT $lim""".strip()
            else:
                q_adp = f"""
MATCH (eo:MetadataObject {{name:$obj_name, category_name:$cat, config_name:$ext_cn, project_name:$pn}})
      -[:{rel}]->(ee:{label})-[:ADOPTED_FROM]->(be:{label})
RETURN ee.name AS name, ee.qualified_name AS extension_qn, be.qualified_name AS base_qn, 'adopted' AS change
ORDER BY name LIMIT $lim""".strip()
            q_ext = f"""
MATCH (eo:MetadataObject {{name:$obj_name, category_name:$cat, config_name:$ext_cn, project_name:$pn}})
      -[:{rel}]->(ee:{label})
WHERE NOT EXISTS {{ (ee)-[:ADOPTED_FROM]->() }}
RETURN ee.name AS name, ee.qualified_name AS extension_qn, null AS base_qn, 'extension_only' AS change
ORDER BY name LIMIT $lim""".strip()
            for q in [q_adp, q_ext]:
                rows = _run_query(loader, q, p, pn)
                if len(rows) >= lim:
                    truncated = True
                for r in rows[:lim]:
                    items.append({
                        "kind": label,
                        "name": r.get("name"),
                        "change": r.get("change"),
                        "form_name": None,
                        "extension_qn": r.get("extension_qn"),
                        "base_qn": r.get("base_qn"),
                        "property_changes": list(r.get("property_changes") or []),
                    })
            if bcn:
                q_base = f"""
MATCH (bo:MetadataObject {{name:$obj_name, category_name:$cat, config_name:$bcn, project_name:$pn}})
      -[:{rel}]->(b0:{label})
WHERE NOT EXISTS {{
  (:MetadataObject {{name:$obj_name, category_name:$cat, config_name:$ext_cn, project_name:$pn}})
  -[:{rel}]->(:{label})-[:ADOPTED_FROM]->(b0)
}}
RETURN b0.name AS name, null AS extension_qn, b0.qualified_name AS base_qn, 'base_only' AS change
ORDER BY name LIMIT $lim""".strip()
                rows = _run_query(loader, q_base, p, pn)
                if len(rows) >= lim:
                    truncated = True
                for r in rows[:lim]:
                    items.append({
                        "kind": label,
                        "name": r.get("name"),
                        "change": "base_only",
                        "form_name": None,
                        "extension_qn": None,
                        "base_qn": r.get("base_qn"),
                        "property_changes": [],
                    })
        return items, truncated

    def _build_forms(loader, obj_name, cat_name, ext_cn, bcn, pn, lim):
        items: List[Dict] = []
        truncated = False
        p = {"obj_name": obj_name, "cat": cat_name, "ext_cn": ext_cn, "bcn": bcn or "", "pn": pn, "lim": lim}
        for q in [
            """
MATCH (eo:MetadataObject {name:$obj_name, category_name:$cat, config_name:$ext_cn, project_name:$pn})
      -[:HAS_FORM]->(ef:Form)-[:ADOPTED_FROM]->(bf:Form)
RETURN ef.name AS name, ef.qualified_name AS extension_qn, bf.qualified_name AS base_qn, 'adopted' AS change
ORDER BY name LIMIT $lim""".strip(),
            """
MATCH (eo:MetadataObject {name:$obj_name, category_name:$cat, config_name:$ext_cn, project_name:$pn})
      -[:HAS_FORM]->(ef:Form)
WHERE NOT EXISTS { (ef)-[:ADOPTED_FROM]->() }
RETURN ef.name AS name, ef.qualified_name AS extension_qn, null AS base_qn, 'extension_only' AS change
ORDER BY name LIMIT $lim""".strip(),
        ]:
            rows = _run_query(loader, q, p, pn)
            if len(rows) >= lim:
                truncated = True
            for r in rows[:lim]:
                items.append({"kind": "Form", "name": r.get("name"), "change": r.get("change"),
                               "form_name": None, "extension_qn": r.get("extension_qn"), "base_qn": r.get("base_qn"),
                               "property_changes": []})
        if bcn:
            rows = _run_query(loader, """
MATCH (bo:MetadataObject {name:$obj_name, category_name:$cat, config_name:$bcn, project_name:$pn})
      -[:HAS_FORM]->(bf:Form)
WHERE NOT EXISTS {
  (:MetadataObject {name:$obj_name, category_name:$cat, config_name:$ext_cn, project_name:$pn})
  -[:HAS_FORM]->(:Form)-[:ADOPTED_FROM]->(bf)
}
RETURN bf.name AS name, null AS extension_qn, bf.qualified_name AS base_qn, 'base_only' AS change
ORDER BY name LIMIT $lim""".strip(), p, pn)
            if len(rows) >= lim:
                truncated = True
            for r in rows[:lim]:
                items.append({"kind": "Form", "name": r.get("name"), "change": "base_only",
                               "form_name": None, "extension_qn": None, "base_qn": r.get("base_qn"),
                               "property_changes": []})
        return items, truncated

    def _build_form_items(loader, obj_name, cat_name, ext_cn, pn, detail, include_unchanged, lim):
        items: List[Dict] = []
        truncated = False
        is_cf = cat_name == "ОбщиеФормы"
        p = {"obj_name": obj_name, "cat": cat_name, "ext_cn": ext_cn, "pn": pn,
             "lim": lim, "excl_keys": _DIFF_EXCL}

        # FormAttribute
        q_fa = ("""
MATCH (eo:MetadataObject {name:$obj_name, category_name:'ОбщиеФормы', config_name:$ext_cn, project_name:$pn})
      -[:ADOPTED_FROM]->(:MetadataObject)
MATCH (eo)-[:HAS_FORM_ATTRIBUTE]->(fa:FormAttribute)
OPTIONAL MATCH (fa)-[:ADOPTED_FROM]->(bfa:FormAttribute)
RETURN fa.name AS name, fa.qualified_name AS extension_qn, bfa.qualified_name AS base_qn,
       fa.ext_source AS ext_source, coalesce(fa.modified_properties, []) AS modified_properties,
       null AS form_name
ORDER BY name LIMIT $lim""".strip() if is_cf else """
MATCH (eo:MetadataObject {name:$obj_name, category_name:$cat, config_name:$ext_cn, project_name:$pn})
      -[:HAS_FORM]->(ef:Form)-[:ADOPTED_FROM]->(:Form)
MATCH (ef)-[:HAS_FORM_ATTRIBUTE]->(fa:FormAttribute)
OPTIONAL MATCH (fa)-[:ADOPTED_FROM]->(bfa:FormAttribute)
RETURN fa.name AS name, fa.qualified_name AS extension_qn, bfa.qualified_name AS base_qn,
       fa.ext_source AS ext_source, coalesce(fa.modified_properties, []) AS modified_properties,
       ef.name AS form_name
ORDER BY form_name, name LIMIT $lim""".strip())
        rows = _run_query(loader, q_fa, p, pn)
        if len(rows) >= lim:
            truncated = True
        for r in rows[:lim]:
            s = r.get("ext_source") or ""
            if s == "own":
                change = "extension_only"
            elif s == "adopted_modified":
                change = "modified"
            elif s == "adopted_unchanged":
                change = "unchanged"
                if not include_unchanged:
                    continue
            else:
                change = "adopted"
            mp = list(r.get("modified_properties") or [])
            if change == "modified":
                if detail == "extended":
                    diff_rows = _run_query(loader, """
MATCH (fa:FormAttribute {qualified_name:$qn})-[:ADOPTED_FROM]->(bfa:FormAttribute)
WITH fa, bfa,
     [k IN keys(fa) WHERE NOT k IN $excl_keys AND (NOT k IN keys(bfa) OR fa[k] <> bfa[k])] +
     [k IN keys(bfa) WHERE NOT k IN $excl_keys AND NOT k IN keys(fa)] AS chg
RETURN [k IN chg | {
  property: k,
  extension_value: CASE WHEN k IN keys(fa) THEN fa[k] ELSE null END,
  base_value:      CASE WHEN k IN keys(bfa) THEN bfa[k] ELSE null END
}] AS property_changes LIMIT 1""".strip(),
                        {"qn": r.get("extension_qn"), "excl_keys": _DIFF_EXCL}, pn)
                    prop_changes = list(diff_rows[0].get("property_changes") or []) if diff_rows else []
                else:
                    prop_changes = [{"property": pr, "base_value": None, "extension_value": None} for pr in mp]
            else:
                prop_changes = []
            items.append({"kind": "FormAttribute", "name": r.get("name"), "change": change,
                           "form_name": r.get("form_name"), "extension_qn": r.get("extension_qn"),
                           "base_qn": r.get("base_qn"), "property_changes": prop_changes})

        # FormControl
        q_fc = ("""
MATCH (eo:MetadataObject {name:$obj_name, category_name:'ОбщиеФормы', config_name:$ext_cn, project_name:$pn})
      -[:ADOPTED_FROM]->(:MetadataObject)
MATCH (eo)-[:HAS_CONTROL|HAS_CHILD*]->(fc:FormControl)
OPTIONAL MATCH (fc)-[:ADOPTED_FROM]->(bfc:FormControl)
RETURN fc.name AS name, fc.qualified_name AS extension_qn, bfc.qualified_name AS base_qn,
       coalesce(fc.ext_source, '') AS ext_source,
       coalesce(fc.modified_properties, []) AS modified_properties, null AS form_name
ORDER BY name LIMIT $lim""".strip() if is_cf else """
MATCH (eo:MetadataObject {name:$obj_name, category_name:$cat, config_name:$ext_cn, project_name:$pn})
      -[:HAS_FORM]->(ef:Form)-[:ADOPTED_FROM]->(:Form)
MATCH (ef)-[:HAS_CONTROL|HAS_CHILD*]->(fc:FormControl)
OPTIONAL MATCH (fc)-[:ADOPTED_FROM]->(bfc:FormControl)
RETURN fc.name AS name, fc.qualified_name AS extension_qn, bfc.qualified_name AS base_qn,
       coalesce(fc.ext_source, '') AS ext_source,
       coalesce(fc.modified_properties, []) AS modified_properties, ef.name AS form_name
ORDER BY form_name, name LIMIT $lim""".strip())
        rows = _run_query(loader, q_fc, p, pn)
        if len(rows) >= lim:
            truncated = True
        for r in rows[:lim]:
            s = r.get("ext_source") or ""
            if s == "own":
                change = "extension_only"
            elif s == "adopted_modified":
                change = "modified"
            elif s == "adopted_unchanged":
                change = "unchanged"
                if not include_unchanged:
                    continue
            else:
                change = "adopted"
            mp = list(r.get("modified_properties") or [])
            if change == "modified":
                if detail == "extended":
                    diff_rows = _run_query(loader, """
MATCH (fc:FormControl {qualified_name:$qn})-[:ADOPTED_FROM]->(bfc:FormControl)
WITH fc, bfc,
     [k IN keys(fc) WHERE NOT k IN $excl_keys AND (NOT k IN keys(bfc) OR fc[k] <> bfc[k])] +
     [k IN keys(bfc) WHERE NOT k IN $excl_keys AND NOT k IN keys(fc)] AS chg
RETURN [k IN chg | {
  property: k,
  extension_value: CASE WHEN k IN keys(fc) THEN fc[k] ELSE null END,
  base_value:      CASE WHEN k IN keys(bfc) THEN bfc[k] ELSE null END
}] AS property_changes LIMIT 1""".strip(),
                        {"qn": r.get("extension_qn"), "excl_keys": _DIFF_EXCL}, pn)
                    prop_changes = list(diff_rows[0].get("property_changes") or []) if diff_rows else []
                else:
                    prop_changes = [{"property": pr, "base_value": None, "extension_value": None} for pr in mp]
            else:
                prop_changes = []
            items.append({"kind": "FormControl", "name": r.get("name"), "change": change,
                           "form_name": r.get("form_name"), "extension_qn": r.get("extension_qn"),
                           "base_qn": r.get("base_qn"), "property_changes": prop_changes})

        # FormEvent — 2 queries (form-level + control-level) + dedup by extension_qn
        if is_cf:
            fe_queries = [
                """
MATCH (eo:MetadataObject {name:$obj_name, category_name:'ОбщиеФормы', config_name:$ext_cn, project_name:$pn})
      -[:ADOPTED_FROM]->(:MetadataObject)
MATCH (eo)-[:HAS_EVENT]->(evt:FormEvent)
OPTIONAL MATCH (evt)-[:ADOPTED_FROM]->(be:FormEvent)
RETURN evt.name AS name, evt.qualified_name AS extension_qn, be.qualified_name AS base_qn,
       (be IS NOT NULL) AS is_adopted, null AS form_name
ORDER BY name LIMIT $lim""".strip(),
                """
MATCH (eo:MetadataObject {name:$obj_name, category_name:'ОбщиеФормы', config_name:$ext_cn, project_name:$pn})
      -[:ADOPTED_FROM]->(:MetadataObject)
MATCH (eo)-[:HAS_CONTROL|HAS_CHILD*]->(fc:FormControl)-[:HAS_EVENT]->(evt:FormEvent)
OPTIONAL MATCH (evt)-[:ADOPTED_FROM]->(be:FormEvent)
RETURN evt.name AS name, evt.qualified_name AS extension_qn, be.qualified_name AS base_qn,
       (be IS NOT NULL) AS is_adopted, null AS form_name
ORDER BY name LIMIT $lim""".strip(),
            ]
        else:
            fe_queries = [
                """
MATCH (eo:MetadataObject {name:$obj_name, category_name:$cat, config_name:$ext_cn, project_name:$pn})
      -[:HAS_FORM]->(ef:Form)-[:ADOPTED_FROM]->(:Form)
MATCH (ef)-[:HAS_EVENT]->(evt:FormEvent)
OPTIONAL MATCH (evt)-[:ADOPTED_FROM]->(be:FormEvent)
RETURN evt.name AS name, evt.qualified_name AS extension_qn, be.qualified_name AS base_qn,
       (be IS NOT NULL) AS is_adopted, ef.name AS form_name
ORDER BY form_name, name LIMIT $lim""".strip(),
                """
MATCH (eo:MetadataObject {name:$obj_name, category_name:$cat, config_name:$ext_cn, project_name:$pn})
      -[:HAS_FORM]->(ef:Form)-[:ADOPTED_FROM]->(:Form)
MATCH (ef)-[:HAS_CONTROL|HAS_CHILD*]->(fc:FormControl)-[:HAS_EVENT]->(evt:FormEvent)
OPTIONAL MATCH (evt)-[:ADOPTED_FROM]->(be:FormEvent)
RETURN evt.name AS name, evt.qualified_name AS extension_qn, be.qualified_name AS base_qn,
       (be IS NOT NULL) AS is_adopted, ef.name AS form_name
ORDER BY form_name, name LIMIT $lim""".strip(),
            ]
        seen_evt: Dict[str, Any] = {}
        for q_fe in fe_queries:
            for r in _run_query(loader, q_fe, p, pn):
                eq = r.get("extension_qn") or ""
                if eq not in seen_evt:
                    seen_evt[eq] = r
        if len(seen_evt) >= lim:
            truncated = True
        for eq, r in list(seen_evt.items())[:lim]:
            change = "adopted" if r.get("is_adopted") else "extension_only"
            items.append({"kind": "FormEvent", "name": r.get("name"), "change": change,
                           "form_name": r.get("form_name"), "extension_qn": eq,
                           "base_qn": r.get("base_qn"), "property_changes": []})

        # Form Commands
        q_cmd = ("""
MATCH (eo:MetadataObject {name:$obj_name, category_name:'ОбщиеФормы', config_name:$ext_cn, project_name:$pn})
      -[:ADOPTED_FROM]->(:MetadataObject)
MATCH (eo)-[:HAS_COMMAND]->(cmd:Command)
OPTIONAL MATCH (cmd)-[:ADOPTED_FROM]->(bcmd:Command)
RETURN cmd.name AS name, cmd.qualified_name AS extension_qn, bcmd.qualified_name AS base_qn,
       coalesce(cmd.ext_source, '') AS ext_source,
       coalesce(cmd.modified_properties, []) AS modified_properties, null AS form_name
ORDER BY name LIMIT $lim""".strip() if is_cf else """
MATCH (eo:MetadataObject {name:$obj_name, category_name:$cat, config_name:$ext_cn, project_name:$pn})
      -[:HAS_FORM]->(ef:Form)-[:ADOPTED_FROM]->(:Form)
MATCH (ef)-[:HAS_COMMAND]->(cmd:Command)
OPTIONAL MATCH (cmd)-[:ADOPTED_FROM]->(bcmd:Command)
RETURN cmd.name AS name, cmd.qualified_name AS extension_qn, bcmd.qualified_name AS base_qn,
       coalesce(cmd.ext_source, '') AS ext_source,
       coalesce(cmd.modified_properties, []) AS modified_properties, ef.name AS form_name
ORDER BY form_name, name LIMIT $lim""".strip())
        rows = _run_query(loader, q_cmd, p, pn)
        if len(rows) >= lim:
            truncated = True
        for r in rows[:lim]:
            s = r.get("ext_source") or ""
            if s == "own":
                change = "extension_only"
            elif s == "adopted_modified":
                change = "modified"
            else:
                change = "adopted"
            mp = list(r.get("modified_properties") or [])
            if change == "modified":
                if detail == "extended" and mp:
                    diff_rows = _run_query(loader, """
MATCH (cmd:Command {qualified_name:$qn})-[:ADOPTED_FROM]->(bcmd:Command)
RETURN [k IN $mp_keys | {
  property: k,
  extension_value: CASE WHEN k IN keys(cmd) THEN cmd[k] ELSE null END,
  base_value:      CASE WHEN k IN keys(bcmd) THEN bcmd[k] ELSE null END
}] AS property_changes LIMIT 1""".strip(),
                        {"qn": r.get("extension_qn"), "mp_keys": mp}, pn)
                    prop_changes = list(diff_rows[0].get("property_changes") or []) if diff_rows else []
                else:
                    prop_changes = [{"property": pr, "base_value": None, "extension_value": None} for pr in mp]
            else:
                prop_changes = []
            items.append({"kind": "Command", "name": r.get("name"), "change": change,
                           "form_name": r.get("form_name"), "extension_qn": r.get("extension_qn"),
                           "base_qn": r.get("base_qn"), "property_changes": prop_changes})
        return items, truncated

    def _build_bsl(loader, obj_name, cat_name, ext_cn, pn, lim):
        code_changes: List[Dict] = []
        truncated = False
        p = {"obj_name": obj_name, "cat": cat_name, "ext_cn": ext_cn, "pn": pn, "lim": lim}

        # Modules: 4 queries, dedup by ext_id
        seen_mod: Dict[str, Any] = {}
        for q_m in [
            f"""
MATCH (eo:MetadataObject {{name:$obj_name, category_name:$cat, config_name:$ext_cn, project_name:$pn}})
      -[:HAS_MODULE]->(em:Module)-[:EXTENDS_MODULE]->(bm:Module)
RETURN em.name AS name, em.id AS ext_id, bm.id AS base_id,
       em.owner_qn AS owner_qn, em.module_type AS module_type, 'extends' AS change
ORDER BY name LIMIT $lim""".strip(),
            f"""
MATCH (eo:MetadataObject {{name:$obj_name, category_name:$cat, config_name:$ext_cn, project_name:$pn}})
      -[:HAS_FORM]->(:Form)-[:HAS_MODULE]->(em:Module)-[:EXTENDS_MODULE]->(bm:Module)
RETURN em.name AS name, em.id AS ext_id, bm.id AS base_id,
       em.owner_qn AS owner_qn, em.module_type AS module_type, 'extends' AS change
ORDER BY name LIMIT $lim""".strip(),
            f"""
MATCH (eo:MetadataObject {{name:$obj_name, category_name:$cat, config_name:$ext_cn, project_name:$pn}})
      -[:HAS_MODULE]->(em:Module)
WHERE NOT EXISTS {{ (em)-[:EXTENDS_MODULE]->() }}
RETURN em.name AS name, em.id AS ext_id, null AS base_id,
       em.owner_qn AS owner_qn, em.module_type AS module_type, 'extension_only' AS change
ORDER BY name LIMIT $lim""".strip(),
            f"""
MATCH (eo:MetadataObject {{name:$obj_name, category_name:$cat, config_name:$ext_cn, project_name:$pn}})
      -[:HAS_FORM]->(:Form)-[:HAS_MODULE]->(em:Module)
WHERE NOT EXISTS {{ (em)-[:EXTENDS_MODULE]->() }}
RETURN em.name AS name, em.id AS ext_id, null AS base_id,
       em.owner_qn AS owner_qn, em.module_type AS module_type, 'extension_only' AS change
ORDER BY name LIMIT $lim""".strip(),
        ]:
            for r in _run_query(loader, q_m, p, pn):
                eid = r.get("ext_id") or ""
                if eid and eid not in seen_mod:
                    seen_mod[eid] = r
        if len(seen_mod) >= lim:
            truncated = True
        for eid, r in list(seen_mod.items())[:lim]:
            code_changes.append({"kind": "Module", "module_name": r.get("name"), "change": r.get("change"),
                                  "extension_module_id": r.get("ext_id"), "base_module_id": r.get("base_id"),
                                  "owner_qn": r.get("owner_qn"), "module_type": r.get("module_type"),
                                  "interception": None})

        # Routines: 3 queries + dedup + batch EXTENDS_ROUTINE check
        seen_rid: Dict[str, Any] = {}
        for q_r in [
            "MATCH (eo:MetadataObject {name:$obj_name, category_name:$cat, config_name:$ext_cn,"
            " project_name:$pn})-[:HAS_MODULE]->(m:Module)-[:DECLARES]->(r:Routine)"
            " RETURN r.id AS rid, r.name AS name, r.owner_qn AS owner_qn, m.module_type AS module_type",
            "MATCH (eo:MetadataObject {name:$obj_name, category_name:$cat, config_name:$ext_cn,"
            " project_name:$pn})-[:HAS_FORM]->(:Form)-[:HAS_MODULE]->(m:Module)-[:DECLARES]->(r:Routine)"
            " RETURN r.id AS rid, r.name AS name, r.owner_qn AS owner_qn, m.module_type AS module_type",
            "MATCH (eo:MetadataObject {name:$obj_name, category_name:$cat, config_name:$ext_cn,"
            " project_name:$pn})-[:DECLARES]->(r:Routine)"
            " RETURN r.id AS rid, r.name AS name, r.owner_qn AS owner_qn, 'CommonModule' AS module_type",
        ]:
            for r in _run_query(loader, q_r, p, pn):
                rid = r.get("rid")
                if rid and rid not in seen_rid:
                    seen_rid[rid] = r
        ext_map: Dict[str, Any] = {}
        if seen_rid:
            ext_rows = _run_query(loader, """
UNWIND $ids AS rid
MATCH (r:Routine {id:rid})-[rel:EXTENDS_ROUTINE]->(br:Routine)
RETURN rid, br.id AS base_id, br.name AS target, rel.decorator AS decorator_type""".strip(),
                {"ids": list(seen_rid.keys())}, pn)
            ext_map = {r.get("rid"): r for r in ext_rows}
        if len(seen_rid) >= lim:
            truncated = True
        for rid, r in list(seen_rid.items())[:lim]:
            ei = ext_map.get(rid)
            if ei:
                code_changes.append({"kind": "Routine", "name": r.get("name"), "change": "intercepts",
                                      "extension_routine_id": rid, "base_routine_id": ei.get("base_id"),
                                      "owner_qn": r.get("owner_qn"), "module_type": r.get("module_type"),
                                      "interception": {"decorator_type": ei.get("decorator_type"),
                                                        "target": ei.get("target")}})
            else:
                code_changes.append({"kind": "Routine", "name": r.get("name"), "change": "extension_only",
                                      "extension_routine_id": rid, "base_routine_id": None,
                                      "owner_qn": r.get("owner_qn"), "module_type": r.get("module_type"),
                                      "interception": None})
        return code_changes, truncated

    def get_extension_object_diff(
        object_ref: str,
        extension_ref: Optional[str] = None,
        sections: Optional[List[Literal[
            "overview", "structure", "forms", "form_items", "bsl", "all"
        ]]] = None,
        detail: Literal["brief", "standard", "extended"] = "standard",
        include_unchanged: bool = False,
        limit_per_section: int = 50,
        project_name: Optional[str] = None,
    ) -> str:
        """Compare a metadata object between its base configuration and extension(s).

Returns an extension-by-extension diff.
object_state per extension: "adopted" (extension borrows the base object),
  "extension_only" (extension defines the object independently, not borrowing),
  "not_found" (object absent from this extension).
Sections structure/forms/form_items/bsl are only populated when object_state="adopted".

sections: None or [] — counts only (fast aggregation queries, no lists).
  "overview" — included in every extension entry by default.
  "structure" — Attribute, TabularPart, Resource, Dimension, EnumValue, Layout, Command.
  "forms" — form adoption status (adopted/extension_only/base_only).
  "form_items" — FormAttribute, FormControl, FormEvent, Command within forms.
  "bsl" — module and routine differences when BSL index data is available.
  "all" — all sections above.
detail: "brief" — counts only even if sections specified;
  "standard" — change rows without property values (for form_items — names of
  modified properties without values);
  "extended" — adds property_changes and complex_property_values.
include_unchanged: include adopted_unchanged FormAttribute/FormControl in form_items.
extension_ref: None — compare against all project extensions.
limit_per_section: max items per element type per change direction.
"""
        loader = _init_loader()
        if loader is None:
            return "Error: Neo4j database connection not available."
        try:
            pn = _resolve_project(project_name)
            load_bsl = bool(getattr(settings, "load_bsl_signatures", False))

            VALID_SECS = {"overview", "structure", "forms", "form_items", "bsl"}
            if isinstance(sections, str):
                sections = [sections]
            if sections and "all" in [s.lower() for s in sections]:
                secs: Optional[List[str]] = list(VALID_SECS)
            elif sections:
                secs = list(dict.fromkeys(s.lower() for s in sections))
                bad = [s for s in secs if s not in VALID_SECS]
                if bad:
                    return f"Error: unknown section(s) {bad}. Allowed: {sorted(VALID_SECS | {'all'})}."
            else:
                secs = None

            detail = (detail or "standard").lower()
            if detail not in {"brief", "standard", "extended"}:
                return f"Error: unknown detail='{detail}'. Allowed: brief, standard, extended."

            try:
                lim = max(1, min(int(getattr(settings, "query_max_results", 200)), int(limit_per_section)))
            except (TypeError, ValueError):
                return f"Error: invalid limit_per_section='{limit_per_section}'."

            # Resolve extension list
            if extension_ref is not None:
                cn = resolve_config(loader, extension_ref, pn)
                is_ext_rows = _run_query(loader,
                    "MATCH (c:Configuration {name:$cn, project_name:$pn})"
                    " RETURN coalesce(c.is_extension, false) AS is_ext",
                    {"cn": cn, "pn": pn}, pn)
                if not is_ext_rows or not is_ext_rows[0].get("is_ext"):
                    return f"Error: '{extension_ref}' is not an extension configuration."
                ext_list = [cn]
            else:
                ext_list = _all_extensions(loader, pn)
                if not ext_list:
                    return "Error: No extension configurations found in project."

            # Resolve object: get canonical name and category
            obj_name = cat_name = None
            _err = None
            _try_cns: List[Optional[str]] = [ext_list[0]] if ext_list else []
            if ext_list:
                _fb = _base_cn(loader, ext_list[0], pn)
                if _fb:
                    _try_cns.append(_fb)
            _try_cns.append(None)
            for _try_cn in _try_cns:
                try:
                    _res = resolve_object_ref(loader, object_ref, pn, _try_cn)
                    obj_name = _res["name"]
                    cat_name = _res["category_name"]
                    break
                except Exception as e:
                    _err = str(e)
            if not obj_name:
                return f"Error: Object not found: '{object_ref}'. {_err or ''}"

            extension_results: List[Dict] = []
            for ext_cn in ext_list:
                bcn = _base_cn(loader, ext_cn, pn)
                state, extension_qn, base_qn = _object_state(loader, obj_name, cat_name, ext_cn, bcn, pn)
                ext_result: Dict[str, Any] = {
                    "extension_config_name": ext_cn,
                    "base_config_name": bcn,
                    "object_state": state,
                    "extension_qn": extension_qn,
                    "base_qn": base_qn,
                }
                if state == "adopted":
                    ext_result["counts"] = _build_counts(loader, obj_name, cat_name, ext_cn, bcn, pn)
                    truncated = False
                    if secs is not None and detail != "brief":
                        metadata_changes: List[Dict] = []
                        code_changes: List[Dict] = []
                        if "structure" in secs:
                            si, st = _build_structure(loader, obj_name, cat_name, ext_cn, bcn, pn, detail, lim)
                            for _row in si:
                                _row["section"] = "structure"
                            metadata_changes.extend(si)
                            truncated = truncated or st
                        if "forms" in secs:
                            fi, ft = _build_forms(loader, obj_name, cat_name, ext_cn, bcn, pn, lim)
                            for _row in fi:
                                _row["section"] = "forms"
                            metadata_changes.extend(fi)
                            truncated = truncated or ft
                        if "form_items" in secs:
                            fii, fit = _build_form_items(loader, obj_name, cat_name, ext_cn, pn,
                                                         detail, include_unchanged, lim)
                            for _row in fii:
                                _row["section"] = "form_items"
                            metadata_changes.extend(fii)
                            truncated = truncated or fit
                        if "bsl" in secs and load_bsl:
                            bi, bt = _build_bsl(loader, obj_name, cat_name, ext_cn, pn, lim)
                            code_changes.extend(bi)
                            truncated = truncated or bt
                        ext_result["metadata_changes"] = metadata_changes
                        ext_result["code_changes"] = code_changes
                    ext_result["truncated"] = truncated
                extension_results.append(ext_result)

            result: Dict[str, Any] = {
                "object_ref": object_ref,
                "object_name": obj_name,
                "category": cat_name,
                "extensions": extension_results,
            }
            shaped = _shape_get_extension_object_diff_result(result)
            return _fmt_dict(
                shaped, apply_compact_refs=True,
                normalize_arrays_for_toon=True, compact_property_names=True,
                compact_section_kind_names=True,
            )

        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.exception("Error in get_extension_object_diff")
            return f"Error: {e}"

    _patch_tool_defaults(get_extension_object_diff)
    mcp.tool()(get_extension_object_diff)


# ---------------------------------------------------------------------------
# Entry point: register_tools(mcp, load_bsl)
# ---------------------------------------------------------------------------

def get_tool_return_schema(tool_name: str) -> str:
    """Return the documented return schema of the specified MCP tool.

    This describes the SHAPE of the tool's response, not its availability:
    a schema is served for every documented tool, even if that tool is not
    currently registered/visible. Use tools/list to check availability.
    """
    name = str(tool_name or "").strip()
    if not name:
        return "Error: tool_name is required."
    try:
        return _fmt_dict(build_tool_return_schema(name), normalize_arrays_for_toon=True)
    except KeyError:
        return f"Error: unknown tool_name '{name}'."


def _register_metadata_tools(mcp) -> None:
    # Tools 1–7 + meta-tool: top-level functions registered directly via mcp.tool()
    _top_level_tools = [
        get_metadata,
        find_metadata_objects,
        get_metadata_object_structure,
        find_metadata_elements,
        find_metadata_usages,
        get_metadata_element_type,
        find_predefined_values,
        get_tool_return_schema,
    ]
    for _fn in _top_level_tools:
        _patch_tool_defaults(_fn)
        mcp.tool()(_fn)
    # Tools 8–18: closures that capture mcp via _register_* wrappers
    _register_get_access_rights(mcp)
    _register_get_metadata_details(mcp)
    _register_get_form_structure(mcp)
    _register_find_form_links(mcp)
    _register_get_event_subscriptions(mcp)
    _register_find_dependency_paths(mcp)        # Tool 17
    _register_inspect_metadata_object(mcp)      # Tool 18
    _register_get_extension_object_diff(mcp)    # Tool 19


def _register_search_bsl_code(mcp) -> None:
    """
    Register search_bsl_code — semantic + lexical search by routine BODY.

    Distinct from search_bsl_routines (which searches doc_description / name /
    signature). This tool runs against the BSL code search sidecar built by
    BslCodeSearchIndexer (SQLite FTS5 + Neo4j vec_bsl_code_unit) and returns
    code fragments matching the natural-language query.

    Internal engine selection (vector vs RLM fallback) is intentionally not
    exposed via parameters or response fields — the contract is one tool, one
    shape.
    """
    def search_bsl_code(
        query: str,
        limit: Optional[int] = settings.bsl_code_search_default_limit,
        config_name: Optional[str] = None,
        owner_qn: Optional[str] = None,
        owner_qn_prefix: Optional[str] = None,
        owner_categories: Optional[List[str]] = None,
        module_type: Optional[str] = None,
        routine_type: Optional[Literal["Procedure", "Function"]] = None,
        export: Optional[bool] = None,
        include_fragments: bool = True,
        excluded_fragment_ids: Optional[List[str]] = None,
    ) -> str:
        """Semantic search by BSL routine BODY.

Returns top routines whose code best matches `query`: a natural-language phrase
describing what the code does, for example "где формируется и отправляется
уведомление пользователю". When include_fragments=true (default), each result
contains code excerpts (start_line, end_line, code). When false, only line
ranges are returned without the code text.

Filters:
  config_name        — only routines in the given 1C configuration (base or extension).
  owner_qn           — exact owner qualified name (e.g. "Project/Config/Справочники/Контрагенты").
  owner_qn_prefix    — owner_qn starts with this prefix.
  owner_categories   — list of categories (e.g. ["ОбщиеМодули", "Справочники"]).
  module_type        — e.g. "CommonModule", "ObjectModule", "FormModule".
  routine_type       — "Procedure" or "Function".
  export             — true to keep only exported routines.

Default limit is 5.
"""
        # Reuse the loader-managed Neo4j driver (same path as other BSL tools).
        from .neo4j_init import get_loader as _get_loader
        loader = _get_loader()
        if not loader or not getattr(loader, "driver", None):
            return _fmt_dict({"items": [], "count": 0})

        from graphdb.bsl_code_search_service import (
            BslCodeSearchIndexNotReady,
            BslCodeSearchService,
        )
        service = BslCodeSearchService(loader.driver)

        # Normalize empty list -> None to keep sub-pipelines on the non-cursor
        # branch (service has its own bounded normalization, but skipping the
        # call entirely makes cypher/SQL stay on the no-excluded path).
        excluded_unit_ids = excluded_fragment_ids if excluded_fragment_ids else None

        try:
            response = service.search_with_notice(
                query=query,
                limit=limit,
                config_name=config_name,
                owner_qn=owner_qn,
                owner_qn_prefix=owner_qn_prefix,
                owner_categories=owner_categories,
                module_type=module_type,
                routine_type=routine_type,
                export=export,
                include_fragments=include_fragments,
                excluded_unit_ids=excluded_unit_ids,
            )
        except BslCodeSearchIndexNotReady:
            return _fmt_dict({
                "items": [],
                "count": 0,
                "status": "index_not_ready",
                "message": (
                    "BSL code search index is not ready yet. "
                    "Indexing is in progress."
                ),
            })
        except Exception as e:
            logging.exception("search_bsl_code failed: %s", e)
            return _fmt_dict({"items": [], "count": 0})

        rows: List[Dict[str, Any]] = []
        for r in response.items:
            row: Dict[str, Any] = {
                "routine_id": r.routine_id,
                "name": r.name,
                "signature": r.signature,
                "owner_qn": r.owner_qn,
                "module_type": r.module_type,
                "file_path": r.file_path,
                "line": r.line,
                "score": r.score,
            }
            if include_fragments:
                row["fragments"] = r.fragments
            else:
                row["ranges"] = r.ranges
            rows.append(row)

        payload: Dict[str, Any] = {"items": rows, "count": len(rows)}
        if response.notice:
            payload["notice"] = response.notice
        return _fmt_dict(payload)

    search_bsl_code.__doc__ = _search_bsl_code_docstring()
    _patch_tool_defaults(search_bsl_code)
    mcp.tool()(search_bsl_code)


def _register_bsl_tools(mcp) -> None:
    _register_search_bsl_routines(mcp)
    _register_get_bsl_routine_body(mcp)
    _register_get_bsl_modules(mcp)
    _register_get_bsl_call_graph(mcp)
    if getattr(settings, "enable_bsl_code_search", False):
        _register_search_bsl_code(mcp)


def register_tools(mcp, load_bsl: bool = False) -> None:
    """Register all typed MCP tools. BSL tools only registered when load_bsl=True."""
    _register_metadata_tools(mcp)
    if load_bsl:
        _register_bsl_tools(mcp)
    if getattr(settings, "object_summary_enabled", False):
        _register_find_objects_by_summary(mcp)


# ---------------------------------------------------------------------------
# Object summary MCP tool
# ---------------------------------------------------------------------------

def _patch_object_summary_categories_annotation(fn) -> None:
    """Annotate `categories` as `Optional[List[Literal[*active_categories]]]`.

    Mirror of `_patch_project_name_annotation` — but for the categories
    selector. The active set is the intersection of `OBJECT_SUMMARY_CATEGORIES`
    and `SUPPORTED_CATEGORIES`; if the intersection is empty we fall back to
    `Optional[List[str]]` so the tool still loads.
    """
    try:
        from object_summary.constants import filter_supported_categories
    except Exception:
        return
    active = filter_supported_categories(list(settings.object_summary_categories or []))
    if not active:
        return
    literal_type = Literal.__getitem__(tuple(active))
    fn.__annotations__["categories"] = Optional[List[literal_type]]


def _register_find_objects_by_summary(mcp) -> None:
    from object_summary.constants import filter_supported_categories
    from graphdb.category_canon import canon_categories
    from graphdb.object_summary_search_service import search_objects_by_summary
    from graphdb.object_summary_queries import get_object_summary_path_by_qn
    from pathlib import Path as _Path
    import json as _json

    def _brief_capability(item: Dict[str, Any]) -> Optional[str]:
        desc = str(item.get("description") or "").strip()
        return desc or None

    def _human_summary(payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        human = payload.get("human_summary")
        return human if isinstance(human, dict) else {}

    def _config_from_qn(qn: str) -> Optional[str]:
        parts = str(qn or "").split("/", 2)
        return parts[1] if len(parts) >= 2 else None

    async def find_objects_by_summary(
        query: Optional[str] = None,
        object_ref: Optional[str] = None,
        categories: Optional[List[str]] = None,
        config: Optional[str] = None,
        include_summary: Literal["none", "brief", "full"] = "brief",
        limit: int = 10,
        offset: int = 0,
        project_name: Optional[str] = None,
    ) -> str:
        """Find 1C metadata objects by summary, or retrieve the full summary of a specific object.

        Exactly one of `query` or `object_ref` must be provided.

        Args:
            query: Natural language query to search summaries (e.g. "что отвечает за резервирование товаров").
            object_ref: Object to retrieve — "Category.Name" or "Category/Name"
                (e.g. "Справочники.Контрагенты" or "Документы/НачислениеЗарплаты"),
                or full qualified name. Mutually exclusive with `query`.
            categories: Limit search to these object categories. Defaults to all enabled categories.
            config: Configuration to search in. Defaults to all configurations in the project.
            include_summary: Summary detail level per result:
                "none" — object metadata and score only,
                "brief" — adds core_idea and capability descriptions,
                "full" — adds the complete human-readable summary.
            limit: Maximum number of results (default 10).
            offset: Pagination offset.
            project_name: Project to search in. Defaults to the primary project.
        """
        if (not query and not object_ref) or (query and object_ref):
            raise ValueError("Provide either 'query' or 'object_ref', not both.")

        loader = _init_loader()
        if loader is None:
            raise RuntimeError("Neo4j loader is not available")

        pn = _resolve_project(project_name)
        config_name = resolve_config(loader, config, pn)

        active_categories = filter_supported_categories(list(settings.object_summary_categories or []))
        if not active_categories:
            raise ValueError(
                "OBJECT_SUMMARY_CATEGORIES contains no supported categories — "
                "search would otherwise fall back to all summaries built earlier."
            )
        if categories:
            user_norm = canon_categories(categories)
            chosen = [c for c in user_norm if c in active_categories]
            if not chosen:
                raise ValueError(
                    "None of the requested categories are enabled in OBJECT_SUMMARY_CATEGORIES."
                )
        else:
            chosen = list(active_categories)

        if object_ref:
            resolved = resolve_object_ref(loader, object_ref, pn, config_name)
            qn = resolved["qualified_name"]
            path_str = get_object_summary_path_by_qn(
                loader.driver, project_name=pn, qualified_name=qn,
            )
            if not path_str:
                raise ValueError(f"Object summary is not built for '{object_ref}'.")
            json_path = _Path(path_str)
            payload = _json.loads(json_path.read_text(encoding="utf-8"))
            qn = resolved["qualified_name"]
            return _fmt_dict({
                "category": resolved.get("category_name"),
                "name": resolved.get("name"),
                "qualified_name": qn,
                "config_name": _config_from_qn(qn),
                "summary": _human_summary(payload),
            }, apply_compact_refs=True)

        ranked = search_objects_by_summary(
            loader.driver,
            project_name=pn,
            query=query or "",
            categories=chosen,
            config_name=config_name,
            limit=int(limit),
            offset=int(offset),
        )

        results: List[Dict[str, Any]] = []
        for row in ranked:
            entry: Dict[str, Any] = {
                "category": row.get("category"),
                "name": row.get("name"),
                "qualified_name": row.get("qualified_name"),
                "config_name": row.get("config_name"),
                "score": round(float(row.get("score") or 0.0), 4),
            }
            if include_summary != "none":
                path_str = row.get("path")
                if path_str:
                    try:
                        payload = _json.loads(_Path(path_str).read_text(encoding="utf-8"))
                    except Exception:
                        payload = None
                    if payload is not None:
                        human = _human_summary(payload)
                        if include_summary == "full":
                            entry["summary"] = human
                        else:
                            entry["summary"] = {
                                "core_idea": human.get("core_idea"),
                                "capabilities": [
                                    cap
                                    for item in human.get("capabilities", []) or []
                                    if isinstance(item, dict)
                                    for cap in (_brief_capability(item),)
                                    if cap is not None
                                ],
                            }
            results.append(entry)

        return _fmt_dict({"count": len(results), "results": results}, apply_compact_refs=True)

    _patch_tool_defaults(find_objects_by_summary)
    _patch_object_summary_categories_annotation(find_objects_by_summary)
    mcp.tool()(find_objects_by_summary)
