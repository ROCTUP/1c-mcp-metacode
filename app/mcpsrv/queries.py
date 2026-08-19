"""
Query utilities for typed MCP tools.

_run_query always injects project_name — project-scope is enforced here, not via string guard.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from config import settings


def clamp_limit(limit_val: Any, default: Optional[int] = None) -> int:
    """Clamp limit to [1, query_max_results]; use query_default_limit when None/invalid."""
    try:
        lv = int(limit_val)
    except Exception:
        lv = int(default if default is not None else settings.query_default_limit)
    lv = max(1, lv)
    lv = min(lv, int(settings.query_max_results))
    return lv


def clamp_offset(offset_val: Any) -> int:
    """Clamp offset to >= 0; defaults to 0."""
    try:
        ov = int(offset_val)
    except Exception:
        ov = 0
    return max(0, ov)


def apply_match(field_expr: str, param_name: str, mode: Optional[str] = "exact") -> str:
    """
    Return a Cypher WHERE predicate fragment for case-insensitive pattern matching.

    Example: apply_match("a.name", "attr", "contains")
    → "toLower(a.name) CONTAINS toLower($attr)"
    """
    mode = (mode or "exact").lower()
    if mode == "starts_with":
        return f"toLower({field_expr}) STARTS WITH toLower(${param_name})"
    elif mode == "contains":
        return f"toLower({field_expr}) CONTAINS toLower(${param_name})"
    else:
        return f"toLower({field_expr}) = toLower(${param_name})"


def apply_match_norm(field_expr: str, param_name: str, mode: Optional[str] = "exact") -> str:
    """Same predicate as apply_match, but `field_expr` must already be a `*_norm` property.

    Example: apply_match_norm("r.name_norm", "text", "contains")
    → "r.name_norm CONTAINS toLower($text)"

    Only the parameter is lowercased. Wrapping the property in toLower()/coalesce()
    turns an index seek into a full label scan, which is the whole reason the
    denormalized field exists — so pass the raw property here, never an expression.
    """
    mode = (mode or "exact").lower()
    if mode == "starts_with":
        return f"{field_expr} STARTS WITH toLower(${param_name})"
    elif mode == "contains":
        return f"{field_expr} CONTAINS toLower(${param_name})"
    else:
        return f"{field_expr} = toLower(${param_name})"


def _run_query(loader: Any, cypher: str, params: Dict[str, Any], project_name: str) -> List[Dict[str, Any]]:
    """
    Execute a read-only Cypher query, always injecting project_name.
    This is the single point where project-scope is enforced for typed tools.
    """
    full_params = {"project_name": project_name, **params}
    return loader.execute_query_readonly(cypher, full_params) or []
