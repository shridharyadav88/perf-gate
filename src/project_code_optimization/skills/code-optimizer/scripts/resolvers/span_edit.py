"""Shared span-splice primitive for the expression-level resolvers.

Several tier0 rewrites replace an exact AST expression span (not whole
lines), so every such applier splices ``(start_line, start_col, end_line,
end_col, text)`` edits — 1-indexed lines, 0-indexed columns, matching
``ast`` ``col_offset``/``end_col_offset`` conventions. One implementation
here keeps the eleven appliers from drifting apart on overlap handling.

Usage (mirrors the other resolver modules):
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from resolvers import span_edit  # noqa: E402
"""

from __future__ import annotations


def node_span(node) -> tuple[int, int, int, int]:
    """(lineno, col, end_lineno, end_col) for an AST node with positions."""
    return (
        node.lineno, node.col_offset,
        node.end_lineno or node.lineno,
        node.end_col_offset if node.end_col_offset is not None else node.col_offset,
    )


def splice(
    source: str,
    edits: list[tuple[int, int, int, int, str]],
) -> str:
    """Return *source* with every span edit applied.

    Each edit is ``(start_line, start_col, end_line, end_col, text)``.
    Edits must be pairwise disjoint; overlapping edits raise
    :exc:`ValueError` (callers treat that as a plan defect, never silently
    pick an order). Application is bottom-up so earlier offsets stay valid.
    """
    lines = source.splitlines(keepends=True)
    line_starts = [0]
    for line in lines:
        line_starts.append(line_starts[-1] + len(line))

    def absolute(lineno: int, col: int) -> int:
        if lineno < 1 or lineno > len(lines):
            raise ValueError(f"line {lineno} out of range (1..{len(lines)})")
        line_text = lines[lineno - 1]
        stripped = line_text.rstrip("\r\n")
        if col < 0 or col > len(stripped):
            raise ValueError(
                f"column {col} out of range on line {lineno}")
        return line_starts[lineno - 1] + col

    absolute_edits = []
    for start_line, start_col, end_line, end_col, text in edits:
        start = absolute(start_line, start_col)
        end = absolute(end_line, end_col)
        if end < start:
            raise ValueError(
                f"inverted span {start_line}:{start_col}-{end_line}:{end_col}")
        absolute_edits.append((start, end, text))
    absolute_edits.sort(key=lambda e: (e[0], e[1]))
    for (_, prev_end, _), (start, _, _) in zip(absolute_edits, absolute_edits[1:]):
        if start < prev_end:
            raise ValueError("overlapping span edits — refusing to apply")
    out = source
    for start, end, text in reversed(absolute_edits):
        out = out[:start] + text + out[end:]
    return out
