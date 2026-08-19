"""
BSL code splitter.

Supported strategies:
    ast_safe_sliding_3600_720_min480   (default)
    ast_safe_sliding_2200_440_min300

If len(body) <= window_chars — a single unit covers the whole routine.
Otherwise — ast-safe sliding window via tree-sitter-bsl. The boundary-point
discovery and overlap selection mirror the validated reference chunking
algorithm so produced ranges match it byte-for-byte for the same input.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class SplitProgressError(RuntimeError):
    """Скользящее окно не сошлось за отведённое число итераций.

    Ловится `process_batch`, поэтому тело уходит в `split_failed`, а Phase A
    продолжает работу. Существует как класс-барьер: любая ошибка непрогресса
    обязана стать счётчиком с логом, а не бесконечным циклом внутри воркера.
    """


# Tree-sitter is a hard dependency for the strict best-approach path. Import is
# lazy + cached so an import-time failure surfaces only when the splitter is
# actually used (BSL code search is opt-in via ENABLE_BSL_CODE_SEARCH).
_TREE_SITTER_PARSER = None
_TREE_SITTER_IMPORT_ERROR: Optional[Exception] = None


# SDBL (1C query language) parser is a best-effort enrichment, NOT a hard
# dependency: it sharpens unit boundaries inside static query-string literals but
# never blocks the split. If it is unavailable or fails, the splitter falls back
# to BSL-only boundaries. Lazy + cached like the BSL parser.
_TREE_SITTER_SDBL_PARSER = None
_TREE_SITTER_SDBL_IMPORT_ERROR: Optional[Exception] = None


# Node types that participate in safe boundary points.
_TREE_SITTER_BOUNDARY_TYPES = frozenset({
    "assignment_statement",
    "break_statement",
    "call_statement",
    "continue_statement",
    "execute_statement",
    "for_each_statement",
    "for_statement",
    "if_statement",
    "line_comment",
    "raise_error_statement",
    "return_statement",
    "try_statement",
    "while_statement",
})


# Control-flow block node types: used to weight boundary candidates higher and
# to compute block_depth for nested statements.
_BLOCK_NODE_TYPES = frozenset({
    "if_statement",
    "for_statement",
    "for_each_statement",
    "while_statement",
    "try_statement",
})


# Opening-block detection for the end-selector tail guard.
# A unit must not end on a line that opens a block (Если ... Тогда, Для ... Цикл,
# Попытка, ...). AST source (ast_open_lines) catches block_node start positions;
# the lexical regex catches branches that share an AST node with their parent
# (ИначеЕсли/Иначе are part of if_statement; Исключение is part of try_statement),
# and acts as a safety net when line fallback lands on a line without an AST
# start-boundary at that exact position.
# Longest alternatives go first inside the regex to win the leftmost-longest
# match (иначеесли before иначе; для каждого before для; for each before for;
# elsif before else).
_OPENING_BLOCK_REGEX = re.compile(
    r"^\s*(?:"
    r"иначеесли|иначе|если|для\s+каждого|для|пока|попытка|исключение"
    r"|elsif|else|if|for\s+each|for|while|try|except"
    r")\b",
    re.IGNORECASE,
)


# Encoded strategy name -> (window_chars, overlap_chars, min_overlap_chars,
#                          safe_cut_tolerance, safe_start_tolerance,
#                          forward_cut_tolerance)
_STRATEGY_PARAMS = {
    "ast_safe_sliding_3600_720_min480": (3600, 720, 480, 500, 240, 240),
    "ast_safe_sliding_2200_440_min300": (2200, 440, 300, 500, 240, 240),
}


def validate_strategy(strategy: str) -> None:
    if strategy not in _STRATEGY_PARAMS:
        raise ValueError(
            f"BSL_CODE_SPLIT_STRATEGY: {strategy!r} is not supported. "
            f"Allowed: {', '.join(_STRATEGY_PARAMS)}."
        )


@dataclass(frozen=True)
class UnitRange:
    """One retrieval unit: a contiguous slice of routine.body."""
    char_start: int
    char_end: int
    line_start: int
    line_end: int
    part_index: int
    part_total: int


@dataclass(frozen=True)
class _BoundaryPoint:
    pos: int
    node_type: str
    side: str
    block_depth: int
    priority: int


def _boundary_priority(node_type: str, side: str, block_depth: int) -> int:
    if node_type in _BLOCK_NODE_TYPES:
        return 90 if side == "end" else 80
    if node_type == "line_comment":
        return 20
    if block_depth == 0:
        return 70 if side == "end" else 60
    return 45 if side == "end" else 40


# ---------- SDBL (1C query language) boundary enrichment ----------
#
# SDBL boundary points are appended to the SAME _BoundaryPoint list as BSL ones,
# so both share a single numeric ordering contract that the end/start selectors
# compare blindly (they do not know a point's origin). BSL values live in
# _boundary_priority above and are intentionally NOT changed here. To keep the
# cross-domain rule explicit (so future tuning of _boundary_priority cannot break
# query-aware splitting silently), the intended ordering is documented and locked
# by relationship tests:
#
#   END boundaries (choosing the end of the current unit):
#     sdbl_query_end(96) > bsl_block_end(90) > sdbl_clause_end(74)
#       > bsl_top_stmt_end(70) > fallback(10)
#   START boundaries (choosing the start of the next unit):
#     sdbl_query_start(86) > bsl_block_start(80) > sdbl_nested_query_start(78);
#     sdbl_clause_start(64) > bsl_top_stmt_start(60) > bsl_in_block_start(40)
#
# Any change to a number in _boundary_priority must re-run the relationship tests.
_SDBL_QUERY_END_PRIORITY = 96      # > BSL block end 90
_SDBL_QUERY_START_PRIORITY = 86    # > BSL block start 80, < query end
_SDBL_NESTED_QUERY_END_PRIORITY = 88
_SDBL_NESTED_QUERY_START_PRIORITY = 78   # < BSL block start 80 (nested = weak start)
_SDBL_DESTROY_END_PRIORITY = 80
_SDBL_DESTROY_START_PRIORITY = 70
_SDBL_CLAUSE_END_PRIORITY = 74     # > top-stmt end 70, < block end 90
_SDBL_CLAUSE_START_PRIORITY = 64   # > top-stmt start 60


# SDBL clause node types that make useful intra-query boundaries.
_SDBL_CLAUSE_NODE_TYPES = frozenset({
    "select_section",
    "union_clause",
    "from_clause",
    "where_clause",
    "group_by_clause",
    "having_clause",
    "order_by_clause",
    "totals_clause",
    "index_by_clause",
    "for_update_clause",
})


# SDBL node types whose presence as an ancestor increases query nesting depth.
_SDBL_NESTING_ANCESTOR_TYPES = frozenset({
    "query",
    "nested_query_source",
    "subquery_expression",
})


# Static query-string detection: content after lstrip must start with one of
# these keywords (ru/en, case-insensitive). Mirrors injections.scm intent.
_SDBL_QUERY_KEYWORDS = (
    "ВЫБРАТЬ", "УНИЧТОЖИТЬ", "SELECT", "DROP",
)


# ---------- tree-sitter bootstrap ----------


def _get_parser():
    global _TREE_SITTER_PARSER, _TREE_SITTER_IMPORT_ERROR
    if _TREE_SITTER_PARSER is not None:
        return _TREE_SITTER_PARSER
    if _TREE_SITTER_IMPORT_ERROR is not None:
        raise _TREE_SITTER_IMPORT_ERROR
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_bsl as tsbsl
        _TREE_SITTER_PARSER = Parser(Language(tsbsl.language()))
        return _TREE_SITTER_PARSER
    except Exception as e:
        _TREE_SITTER_IMPORT_ERROR = RuntimeError(
            "BSL code splitter requires tree-sitter + tree-sitter-bsl. "
            "Install them (locally: `pip install -e third_party/tree-sitter-bsl`; "
            "Docker: the image must build the C extension — see Dockerfile). "
            f"Underlying import error: {e}"
        )
        raise _TREE_SITTER_IMPORT_ERROR


def _get_sdbl_parser():
    """Lazy SDBL parser for query-string enrichment. Best-effort: returns None
    (never raises) if tree-sitter / tree-sitter-bsl / the SDBL language is
    unavailable, so the splitter degrades to BSL-only boundaries."""
    global _TREE_SITTER_SDBL_PARSER, _TREE_SITTER_SDBL_IMPORT_ERROR
    if _TREE_SITTER_SDBL_PARSER is not None:
        return _TREE_SITTER_SDBL_PARSER
    if _TREE_SITTER_SDBL_IMPORT_ERROR is not None:
        return None
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_bsl as tsbsl
        _TREE_SITTER_SDBL_PARSER = Parser(Language(tsbsl.sdbl_language()))
        return _TREE_SITTER_SDBL_PARSER
    except Exception as e:
        _TREE_SITTER_SDBL_IMPORT_ERROR = e
        logger.debug("SDBL parser unavailable; query-aware boundaries disabled: %s", e)
        return None


# ---------- pure helpers (1:1 port from reference) ----------


def _line_offsets(text: str) -> List[Tuple[int, int]]:
    """Per-line (char_start, char_end) inclusive of trailing newline char."""
    result: List[Tuple[int, int]] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        start = pos
        pos += len(line)
        result.append((start, pos))
    if not result:
        result.append((0, 0))
    return result


def _line_for_char(lines: List[Tuple[int, int]], char_pos: int) -> int:
    """1-based line number that contains char_pos. Binary search."""
    lo = 0
    hi = len(lines)
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if lines[mid][0] <= char_pos:
            lo = mid
        else:
            hi = mid
    return lo + 1


def _trim_range(text: str, start: int, end: int) -> Tuple[int, int]:
    start = max(0, start)
    end = min(len(text), end)
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _trimmed_start_of(text: str, start: int, end: int) -> int:
    start = max(0, start)
    end = min(len(text), end)
    while start < end and text[start].isspace():
        start += 1
    return start


def _dedupe_ranges(ranges: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    result: List[Tuple[int, int]] = []
    seen = set()
    for start, end in ranges:
        if end <= start:
            continue
        key = (start, end)
        if key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def _nearest_line_start(lines: List[Tuple[int, int]], char_pos: int) -> int:
    starts = [s for s, _ in lines]
    return min(starts, key=lambda value: abs(value - char_pos))


def _nearest_line_end_before(lines: List[Tuple[int, int]], char_pos: int) -> int:
    ends = [e for _, e in lines if e <= char_pos]
    if not ends:
        return min(char_pos, lines[-1][1])
    return max(ends)


def _byte_to_char_offsets(text: str) -> dict:
    result: dict = {}
    byte_pos = 0
    for char_pos, char in enumerate(text):
        result[byte_pos] = char_pos
        byte_pos += len(char.encode("utf-8"))
    result[byte_pos] = len(text)
    return result


# ---------- SDBL query-string decode + boundary extraction ----------


@dataclass(frozen=True)
class _DecodedQueryString:
    """Reconstructed SDBL query text from a BSL string literal, plus a mapping
    from each query-text char offset back to a char offset in the BSL body.

    query_char_to_body_char has length len(query_text)+1; index len(query_text)
    is the exclusive-end and equals body_end."""
    query_text: str
    query_char_to_body_char: List[int]
    body_start: int
    body_end: int


def _looks_like_static_query_text(query_text: str) -> bool:
    """True if content (after lstrip) starts with an SDBL statement keyword."""
    stripped = query_text.lstrip()
    if not stripped:
        return False
    upper = stripped.upper()
    for kw in _SDBL_QUERY_KEYWORDS:
        if upper.startswith(kw):
            rest = upper[len(kw):]
            if not rest or not rest[0].isalnum():
                return True
    return False


def _decode_bsl_string_node(text: str, node, byte_to_char: dict) -> Optional[_DecodedQueryString]:
    """Decode a BSL `string` node (single- or multi-line) into SDBL query text.

    Walks `string_content` descendants, collapses doubled BSL quotes `""` into a
    single `"`, joins multiline segments with `\\n`, and records for every emitted
    query-text char its originating BSL-body char offset. Returns None if the node
    has no content segments, an offset cannot be mapped, or the decoded text does
    not look like a static query."""
    segments = []
    stack = [node]
    while stack:
        cur = stack.pop()
        for child in reversed(cur.children):
            if child.type == "string_content":
                segments.append(child)
            else:
                stack.append(child)
    # string_content nodes were discovered via a reversed-children DFS; restore
    # document order by start_byte.
    segments.sort(key=lambda n: n.start_byte)
    if not segments:
        return None

    body_start = byte_to_char.get(node.start_byte)
    body_end = byte_to_char.get(node.end_byte)
    if body_start is None or body_end is None:
        return None

    query_chars: List[str] = []
    mapping: List[int] = []
    prev_ce: Optional[int] = None
    for seg_index, seg in enumerate(segments):
        cs = byte_to_char.get(seg.start_byte)
        ce = byte_to_char.get(seg.end_byte)
        if cs is None or ce is None:
            return None
        if seg_index > 0:
            # Multiline segments are joined by a synthetic newline; map it to the
            # end of the previous segment (a real body position inside the literal).
            query_chars.append("\n")
            mapping.append(prev_ce if prev_ce is not None else cs)
        i = cs
        while i < ce:
            ch = text[i]
            if ch == '"' and i + 1 < ce and text[i + 1] == '"':
                # Doubled BSL quote -> single quote for SDBL. The next mapping
                # entry (exclusive-end of this char) lands past the second quote.
                query_chars.append('"')
                mapping.append(i)
                i += 2
            else:
                query_chars.append(ch)
                mapping.append(i)
                i += 1
        prev_ce = ce
    mapping.append(body_end)  # exclusive end of the whole query_text

    query_text = "".join(query_chars)
    if not _looks_like_static_query_text(query_text):
        return None
    return _DecodedQueryString(
        query_text=query_text,
        query_char_to_body_char=mapping,
        body_start=body_start,
        body_end=body_end,
    )


def _query_end_with_semicolon(
    query_text: str,
    end_qc: Optional[int],
    mapping: List[int],
    map_len: int,
) -> Optional[int]:
    """If a `;` package separator follows query.end across only SDBL extras
    (whitespace / line_comment), return the BODY position just past that `;` so
    the separator stays in the previous unit. Otherwise None (use default end).

    `end_qc` is the query-text char offset of query.end (exclusive)."""
    if end_qc is None or end_qc < 0 or end_qc >= map_len:
        return None
    n = len(query_text)
    i = end_qc
    while i < n:
        ch = query_text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "/" and i + 1 < n and query_text[i + 1] == "/":
            nl = query_text.find("\n", i + 2)
            i = n if nl == -1 else nl + 1
            continue
        break
    if i < n and query_text[i] == ";":
        sc_end_qc = i + 1
        if 0 <= sc_end_qc < map_len:
            return mapping[sc_end_qc]
    return None


def _sdbl_boundary_points_from_query_text(
    decoded: _DecodedQueryString, parser
) -> List[_BoundaryPoint]:
    """Parse decoded query text with the SDBL grammar and emit boundary points
    translated back into BSL-body char offsets. Full-tree traversal tolerant to
    error recovery, but a candidate validity gate ensures only sound nodes emit
    points (so error-recovered junk cannot win a high SDBL priority)."""
    if parser is None:
        return []
    try:
        tree = parser.parse(decoded.query_text.encode("utf-8"))
    except Exception as e:
        logger.debug("SDBL parse failed (%d chars): %s", len(decoded.query_text), e)
        return []

    q_byte_to_char = _byte_to_char_offsets(decoded.query_text)
    mapping = decoded.query_char_to_body_char
    map_len = len(mapping)
    points: List[_BoundaryPoint] = []

    def emit(node, node_type: str, end_priority: int, start_priority: int,
             end_override: Optional[int] = None) -> None:
        # candidate validity gate
        if node.end_byte <= node.start_byte:
            return
        if getattr(node, "has_error", False) or getattr(node, "is_missing", False):
            return
        start_qc = q_byte_to_char.get(node.start_byte)
        end_qc = q_byte_to_char.get(node.end_byte)
        if start_qc is None or end_qc is None:
            return
        if not (0 <= start_qc < map_len) or not (0 <= end_qc < map_len):
            return
        start_body = mapping[start_qc]
        end_body = end_override if end_override is not None else mapping[end_qc]
        if end_body <= start_body:
            return
        points.append(_BoundaryPoint(
            pos=start_body, node_type=node_type, side="start",
            block_depth=0, priority=start_priority,
        ))
        points.append(_BoundaryPoint(
            pos=end_body, node_type=node_type, side="end",
            block_depth=0, priority=end_priority,
        ))

    def visit(node, query_depth: int) -> None:
        t = node.type
        if t == "query":
            nested = query_depth > 0
            end_pri = _SDBL_NESTED_QUERY_END_PRIORITY if nested else _SDBL_QUERY_END_PRIORITY
            start_pri = _SDBL_NESTED_QUERY_START_PRIORITY if nested else _SDBL_QUERY_START_PRIORITY
            end_override = _query_end_with_semicolon(
                decoded.query_text, q_byte_to_char.get(node.end_byte), mapping, map_len,
            )
            emit(node, "sdbl_query", end_pri, start_pri, end_override=end_override)
        elif t == "destroy_statement":
            emit(node, "sdbl_destroy",
                 _SDBL_DESTROY_END_PRIORITY, _SDBL_DESTROY_START_PRIORITY)
        elif t in _SDBL_CLAUSE_NODE_TYPES:
            emit(node, "sdbl_" + t,
                 _SDBL_CLAUSE_END_PRIORITY, _SDBL_CLAUSE_START_PRIORITY)
        child_depth = query_depth + 1 if t in _SDBL_NESTING_ANCESTOR_TYPES else query_depth
        for child in node.children:
            visit(child, child_depth)

    visit(tree.root_node, 0)
    return points


def _sdbl_boundary_points_from_bsl_strings(
    text: str, root_node, byte_to_char: dict
) -> List[_BoundaryPoint]:
    """Best-effort: find BSL string literals that look like static queries, decode
    them, and emit SDBL boundary points. A failure on one string skips only that
    string; the SDBL parser being unavailable yields an empty list (BSL-only)."""
    parser = _get_sdbl_parser()
    if parser is None:
        return []
    points: List[_BoundaryPoint] = []

    def visit(node) -> None:
        if node.type == "string":
            try:
                decoded = _decode_bsl_string_node(text, node, byte_to_char)
                if decoded is not None:
                    points.extend(_sdbl_boundary_points_from_query_text(decoded, parser))
            except Exception as e:
                logger.debug("SDBL enrichment skipped for one string node: %s", str(e)[:200])
        for child in node.children:
            visit(child)

    visit(root_node)
    return points


def _tree_sitter_boundary_points(text: str, parser) -> List[_BoundaryPoint]:
    if parser is None:
        return []
    src = text.encode("utf-8")
    try:
        tree = parser.parse(src)
    except Exception as e:
        logger.warning("tree-sitter parse failed for BSL body (%d chars): %s", len(text), e)
        return []
    byte_to_char = _byte_to_char_offsets(text)
    points: List[_BoundaryPoint] = []

    def visit(node, depth: int) -> None:
        if node.type in _TREE_SITTER_BOUNDARY_TYPES:
            start = byte_to_char.get(node.start_byte)
            end = byte_to_char.get(node.end_byte)
            if start is not None and end is not None and end > start:
                points.append(_BoundaryPoint(
                    pos=start,
                    node_type=node.type,
                    side="start",
                    block_depth=depth,
                    priority=_boundary_priority(node.type, "start", depth),
                ))
                points.append(_BoundaryPoint(
                    pos=end,
                    node_type=node.type,
                    side="end",
                    block_depth=depth,
                    priority=_boundary_priority(node.type, "end", depth),
                ))
        child_depth = depth + 1 if node.type in _BLOCK_NODE_TYPES else depth
        for child in node.children:
            visit(child, child_depth)

    visit(tree.root_node, 0)
    # SDBL-aware enrichment: sharpen boundaries inside static query-string
    # literals. Best-effort — yields [] when the SDBL parser is unavailable.
    points.extend(_sdbl_boundary_points_from_bsl_strings(text, tree.root_node, byte_to_char))
    points.sort(key=lambda bp: bp.pos)
    return points


def _line_text(text: str, lines: List[Tuple[int, int]], line_no: int) -> str:
    """Return text of 1-based line `line_no` with trailing CR/LF stripped."""
    if line_no < 1 or line_no > len(lines):
        return ""
    s, e = lines[line_no - 1]
    raw = text[s:e]
    if raw.endswith("\r\n"):
        return raw[:-2]
    if raw.endswith("\n") or raw.endswith("\r"):
        return raw[:-1]
    return raw


def _is_blank_or_comment_line(line: str) -> bool:
    stripped = line.lstrip()
    return not stripped or stripped.startswith("//")


def _last_significant_line(
    text: str,
    lines: List[Tuple[int, int]],
    start: int,
    end_exclusive: int,
) -> Optional[int]:
    """1-based number of the last non-blank/non-comment line in [start, end_exclusive)."""
    if end_exclusive <= start or end_exclusive <= 0:
        return None
    last_char = min(end_exclusive - 1, len(text) - 1)
    if last_char < 0:
        return None
    line_no = _line_for_char(lines, last_char)
    start_line = _line_for_char(lines, max(0, start))
    while line_no >= start_line:
        if not _is_blank_or_comment_line(_line_text(text, lines, line_no)):
            return line_no
        line_no -= 1
    return None


def _is_opening_block_line(
    text: str,
    lines: List[Tuple[int, int]],
    line_no: int,
    ast_open_lines: Set[int],
) -> bool:
    if line_no in ast_open_lines:
        return True
    return bool(_OPENING_BLOCK_REGEX.match(_line_text(text, lines, line_no)))


def _find_safe_line_end(
    text: str,
    lines: List[Tuple[int, int]],
    start: int,
    upper_inclusive: int,
    lower_inclusive: int,
    ast_open_lines: Set[int],
) -> Optional[int]:
    """Largest line_end in [lower_inclusive, upper_inclusive], > start, whose
    last significant line of [start, line_end) is NOT an opening-block line.

    Returns None if no such line_end exists.
    """
    line_ends = sorted(
        {e for _, e in lines if start < e <= upper_inclusive and e >= lower_inclusive},
        reverse=True,
    )
    for le in line_ends:
        sig = _last_significant_line(text, lines, start, le)
        if sig is None:
            continue
        if not _is_opening_block_line(text, lines, sig, ast_open_lines):
            return le
    return None


# Ранги разделителей для вырожденной ступени реза. Строка длиннее окна не
# содержит ни концов операторов, ни переносов, поэтому безопасной границы внутри
# неё нет вообще — режем по символам-разделителям. Ранг важнее близости к цели:
# разделитель пакета сильнее разделителя строки запроса, тот сильнее разделителя
# полей. Для всех, кроме вертикальной черты, рез идёт ПОСЛЕ символа; черта
# открывает строку текста запроса, поэтому рез ставится ПЕРЕД ней.
_SEPARATOR_SEMICOLON_RANK = 4
_SEPARATOR_PIPE_RANK = 3
_SEPARATOR_COMMA_RANK = 2
_SEPARATOR_SPACE_RANK = 1


def _separator_cut(text: str, lo: int, hi: int, target: int) -> Optional[int]:
    """Позиция реза в [lo, hi], ближайшая к target среди разделителей
    максимального доступного ранга. None — разделителей в интервале нет."""
    lo = max(0, lo)
    hi = min(len(text), hi)
    best: Optional[int] = None
    best_rank = -1
    best_dist = 0
    for i in range(lo, hi):
        ch = text[i]
        if ch == ";":
            rank, pos = _SEPARATOR_SEMICOLON_RANK, i + 1
        elif ch == "|":
            rank, pos = _SEPARATOR_PIPE_RANK, i
        elif ch == ",":
            rank, pos = _SEPARATOR_COMMA_RANK, i + 1
        elif ch.isspace():
            rank, pos = _SEPARATOR_SPACE_RANK, i + 1
        else:
            continue
        if pos < lo or pos > hi:
            continue
        dist = abs(pos - target)
        if rank > best_rank or (rank == best_rank and dist < best_dist):
            best, best_rank, best_dist = pos, rank, dist
    return best


def _select_best_end_boundary(
    boundaries: List[_BoundaryPoint],
    lines: List[Tuple[int, int]],
    text: str,
    start: int,
    target_end: int,
    search_from: int,
    search_to: int,
    ast_open_lines: Set[int],
) -> int:
    # AST candidates: only side="end" — block-start must not be picked as char_end.
    candidates: List[_BoundaryPoint] = [
        bp for bp in boundaries
        if bp.side == "end"
        and search_from <= bp.pos <= search_to
        and bp.pos > start
    ]

    # Line fallback with lexical opening-line guard: prefer the nearest line_end
    # whose tail is not an opening-block line; if none qualifies, keep the
    # original nearest-line-end-before-target so we still make progress.
    line_end = _nearest_line_end_before(lines, target_end)
    safe_le = _find_safe_line_end(
        text, lines, start, line_end, search_from, ast_open_lines,
    )
    fallback_pos = safe_le if safe_le is not None else line_end
    if search_from <= fallback_pos <= search_to and fallback_pos > start:
        candidates.append(_BoundaryPoint(
            pos=fallback_pos,
            node_type="",
            side="end",
            block_depth=0,
            priority=10,
        ))
    if not candidates:
        # Пустой candidates означает ровно одно: ни AST-границы, ни конца строки
        # в [search_from, search_to] нет — попадающий в интервал конец строки уже
        # добавлен фолбэком выше. Возврат позиции вне интервала ломает инвариант
        # вызывающего цикла (окно схлопывается, перекрытие перестаёт помещаться),
        # поэтому идём вниз по лестнице: разделитель, затем жёсткий рез по цели.
        # Назад за search_from не уходим никогда.
        separator = _separator_cut(text, search_from, search_to, target_end)
        return separator if separator is not None else target_end

    def score(bp: _BoundaryPoint) -> float:
        if bp.pos <= target_end:
            penalty = (target_end - bp.pos) / 100.0
        else:
            penalty = (bp.pos - target_end) / 50.0
        return bp.priority - penalty

    best = max(
        candidates,
        key=lambda bp: (score(bp), bp.priority, -abs(bp.pos - target_end), bp.pos),
    )
    end = best.pos

    # Post-selection tail-check: if the chosen end leaves an opening-line as the
    # last significant line, shift back to the nearest earlier safe line_end
    # within [search_from, end). If none — keep the chosen end (progress).
    # Final unit (end >= len(text)) is intentionally not protected: it would
    # otherwise drop the trailing block opener with nowhere to relocate it.
    if end < len(text):
        sig = _last_significant_line(text, lines, start, end)
        if sig is not None and _is_opening_block_line(text, lines, sig, ast_open_lines):
            shifted = _find_safe_line_end(
                text, lines, start, end - 1, search_from, ast_open_lines,
            )
            if shifted is not None and shifted > start:
                end = shifted
    return end


def _select_best_start_boundary(
    boundaries: List[_BoundaryPoint],
    lines: List[Tuple[int, int]],
    text: str,
    previous_start: int,
    end: int,
    target_start: int,
    min_start: int,
    max_start: int,
    min_overlap_chars: int,
) -> int:
    candidates: List[_BoundaryPoint] = [
        bp for bp in boundaries
        if bp.side == "start"
        and min_start <= bp.pos <= max_start
        and bp.pos > previous_start
        and bp.pos < end
        and (min_overlap_chars <= 0 or _trimmed_start_of(text, bp.pos, end) <= max_start)
    ]
    if candidates:
        best = max(
            candidates,
            key=lambda bp: (
                bp.priority - abs(bp.pos - target_start) / 100.0,
                bp.priority,
                -abs(bp.pos - target_start),
                -bp.pos,
            ),
        )
        return best.pos

    # tree-sitter end-byte is exclusive — last char of КонецЕсли/КонецЦикла is at bp.pos - 1.
    closing_block_lines = {
        _line_for_char(lines, max(0, bp.pos - 1))
        for bp in boundaries
        if bp.side == "end" and bp.node_type in _BLOCK_NODE_TYPES
    }

    line_candidates = [
        ls
        for ls, _ in lines
        if min_start <= ls <= max_start
        and (min_overlap_chars <= 0 or _trimmed_start_of(text, ls, end) <= max_start)
    ]
    preferred = [
        ls for ls in line_candidates
        if _line_for_char(lines, ls) not in closing_block_lines
    ]
    if preferred:
        chosen = min(preferred, key=lambda p: abs(p - target_start))
    elif line_candidates:
        chosen = min(line_candidates, key=lambda p: abs(p - target_start))
    else:
        chosen = _nearest_line_start(lines, min(target_start, max_start))
    if chosen <= previous_start or chosen >= end or (
        min_overlap_chars > 0 and _trimmed_start_of(text, chosen, end) > max_start
    ):
        chosen = min(target_start, max_start)
    return chosen


def _ast_safe_sliding_ranges(
    text: str,
    lines: List[Tuple[int, int]],
    parser,
    window_chars: int,
    overlap_chars: int,
    min_overlap_chars: int,
    cut_tolerance: int,
    start_tolerance: int,
    forward_cut_tolerance: int,
) -> List[Tuple[int, int]]:
    if len(text) <= window_chars:
        return [(0, len(text))]

    boundary_points = _tree_sitter_boundary_points(text, parser)
    ast_open_lines: Set[int] = {
        _line_for_char(lines, bp.pos)
        for bp in boundary_points
        if bp.side == "start" and bp.node_type in _BLOCK_NODE_TYPES
    }

    ranges: List[Tuple[int, int]] = []
    start = 0
    # Каждая итерация обязана двигать start минимум на (window - overlap); запас
    # покрывает вырожденные окна, но не бесконечность.
    max_iterations = 4 * (len(text) // max(1, window_chars - overlap_chars) + 8)
    iterations = 0
    while start < len(text):
        iterations += 1
        if iterations > max_iterations:
            raise SplitProgressError(
                f"ast_safe_sliding did not converge: len={len(text)}, "
                f"start={start}, iterations={iterations}"
            )
        target_end = min(len(text), start + window_chars)
        if target_end >= len(text):
            end = len(text)
        else:
            search_from = max(start + 1, target_end - cut_tolerance)
            search_to = min(len(text), target_end + forward_cut_tolerance)
            end = _select_best_end_boundary(
                boundaries=boundary_points,
                lines=lines,
                text=text,
                start=start,
                target_end=target_end,
                search_from=search_from,
                search_to=search_to,
                ast_open_lines=ast_open_lines,
            )
            if end <= start:
                end = target_end

        ranges.append((start, end))
        if end >= len(text):
            break

        _trimmed_start, effective_end = _trim_range(text, start, end)
        target_start = max(start + 1, effective_end - overlap_chars)
        min_start = max(start + 1, target_start - start_tolerance)
        if min_overlap_chars > 0:
            max_start = min(end - 1, effective_end - min_overlap_chars)
        else:
            max_start = min(end - 1, target_start + start_tolerance)

        next_start = _select_best_start_boundary(
            boundaries=boundary_points,
            lines=lines,
            text=text,
            previous_start=ranges[-1][0],
            end=end,
            target_start=target_start,
            min_start=min_start,
            max_start=max_start,
            min_overlap_chars=min_overlap_chars,
        )
        # Инвариант прогресса. Все ветки отбора старта могут отдать точку вне
        # (start, end] — например когда min_overlap делает интервал
        # [min_start, max_start] пустым. Тогда следующий unit начинается ровно на
        # границе текущего: перекрытия нет, но и разрыва (потери кода) тоже нет.
        if not (start < next_start <= end):
            next_start = end
        start = next_start

    return _dedupe_ranges(ranges)


# ---------- public entry point ----------


def split_routine(body: str, strategy: str) -> List[UnitRange]:
    """
    Slice a routine body into retrieval units according to `strategy`.

    For body shorter than the window — a single unit covering the whole body.
    For longer bodies — ast_safe_sliding via tree-sitter-bsl. Raises
    RuntimeError if tree-sitter-bsl is not installed (no degraded fallback).
    """
    if body is None:
        return []
    params = _STRATEGY_PARAMS.get(strategy)
    if params is None:
        raise ValueError(
            f"BSL_CODE_SPLIT_STRATEGY: {strategy!r} is not supported. "
            f"Allowed: ast_safe_sliding_3600_720_min480, "
            f"ast_safe_sliding_2200_440_min300."
        )
    window_chars, overlap_chars, min_overlap_chars, cut_tol, start_tol, forward_cut_tol = params

    if len(body) <= window_chars:
        line_start, line_end = _whole_body_lines(body)
        return [UnitRange(
            char_start=0,
            char_end=len(body),
            line_start=line_start,
            line_end=line_end,
            part_index=0,
            part_total=1,
        )]

    parser = _get_parser()
    lines = _line_offsets(body)
    char_ranges = _ast_safe_sliding_ranges(
        body, lines, parser,
        window_chars, overlap_chars, min_overlap_chars,
        cut_tol, start_tol, forward_cut_tol,
    )

    units: List[UnitRange] = []
    total = len(char_ranges)
    for idx, (c_start, c_end) in enumerate(char_ranges):
        ls = _line_for_char(lines, c_start)
        le = _line_for_char(lines, max(c_start, c_end - 1))
        units.append(UnitRange(
            char_start=c_start,
            char_end=c_end,
            line_start=ls,
            line_end=le,
            part_index=idx,
            part_total=total,
        ))
    return units


def _whole_body_lines(body: str) -> Tuple[int, int]:
    if not body:
        return 1, 1
    # 1-based inclusive [start, end] line range covering the entire body.
    line_count = body.count("\n") + (0 if body.endswith("\n") else 1)
    return 1, max(1, line_count)


def slice_body(body: str, unit: UnitRange) -> str:
    """Return the raw substring of body for a given unit. Defensive bounds check."""
    if not body:
        return ""
    return body[max(0, unit.char_start): min(len(body), unit.char_end)]
