"""Single source of truth for substring (CONTAINS) search over Routine.

The fulltext index only narrows candidates; the CONTAINS predicate decides. Both halves
are emitted as one insertable Cypher section, so a caller cannot take the accelerator
without the verifier — that would silently return rows the old scan never returned.

Index, indexed property and verifier field come from a closed manifest keyed by
`SubstringTarget`. Pairing a candidate source with the verifier of a *different* field
would be worse than extra rows: it would drop matches the post-filter cannot recover.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class SubstringTarget(Enum):
    """Routine fields that support accelerated substring search."""

    NAME = "name"
    SIGNATURE = "signature"


@dataclass(frozen=True)
class _Spec:
    index_name: str
    indexed_property: str
    field_norm: str


_TARGETS: Dict[SubstringTarget, _Spec] = {
    SubstringTarget.NAME: _Spec("ftx_routine_name", "name", "r.name_norm"),
    SubstringTarget.SIGNATURE: _Spec("ftx_routine_signature", "signature", "r.signature_norm"),
}

# Only extracted tokens ever reach Lucene, so reserved characters in the user's text
# (parentheses, quotes, dashes) cannot make the procedure raise.
_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_]+")


@dataclass(frozen=True)
class SubstringSource:
    """One insertable Cypher section: candidate source plus its mandatory verifier."""

    cypher: str
    params: Dict[str, Any]
    used_index: Optional[str]  # None = plain scan; drives logging and invalidation


def accelerator_index_name(target: SubstringTarget) -> str:
    return _TARGETS[target].index_name


def build_candidate_query(text: str) -> Optional[str]:
    """`*tok1* AND *tok2*` for Lucene, or None when `text` has no usable token.

    ANDed tokens cannot drop a match: a value containing the literal substring contains
    every token of it. Whitespace is what gets decomposed here, which is precisely why
    a multi-word search no longer has to fall back to a scan.
    """
    tokens = _TOKEN_RE.findall(text or "")
    if not tokens:
        return None
    return " AND ".join(f"*{t.lower()}*" for t in tokens)


def build_routine_substring_source(
    *,
    target: SubstringTarget,
    text: str,
    param_name: str,
    accelerator_ready: bool,
) -> SubstringSource:
    """Candidate source + mandatory verifier for a CONTAINS search over Routine.

    Returns one insertable Cypher section, not two composable halves: the fulltext index
    may only narrow, never decide.

    There is no `mode` parameter on purpose: this builder owns substring search only.
    `exact` and `starts_with` are served by the range index and must not go through a
    leading-wildcard fulltext query.
    """
    from mcpsrv.queries import apply_match_norm

    spec = _TARGETS[target]
    verifier = apply_match_norm(spec.field_norm, param_name, "contains")

    candidate_query = build_candidate_query(text) if accelerator_ready else None
    if candidate_query is None:
        return SubstringSource(
            cypher=f"MATCH (r:Routine)\nWHERE {verifier}",
            params={},
            used_index=None,
        )

    ft_param = f"{param_name}_ft"
    driver = (
        f"CALL db.index.fulltext.queryNodes('{spec.index_name}', ${ft_param}) YIELD node AS r"
    )
    return SubstringSource(
        cypher=f"{driver}\nWHERE {verifier}",
        params={ft_param: candidate_query},
        used_index=spec.index_name,
    )
