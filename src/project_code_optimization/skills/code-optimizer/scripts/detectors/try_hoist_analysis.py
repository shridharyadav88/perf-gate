"""Detect ``for`` loops whose whole body is a ``try`` safe to hoist.

``try``/``except`` setup executes on every iteration, but when every
handler abandons the loop anyway (``raise`` / ``return``), one setup
around the loop observes exactly the same handler runs::

    for chunk in chunks:         ->   try:
        try:                                  for chunk in chunks:
            parse(chunk)                          parse(chunk)
        except ValueError:                    except ValueError:
            raise                                     raise

Detection is pure/read-only (this module never writes a file) so it can
be shared by `classify_findings.py` (read-only triage) and
`resolvers/apply_try_hoist.py` (the actual codemod) without duplicating
logic.

Safety rules (anything ambiguous is skipped, never guessed at):

* The ``for`` body is exactly one ``try`` statement (sync ``for``
  only); the ``for`` has no ``else`` clause (an ``else`` would need to
  move with the loop, which this rewrite does not attempt).
* The ``try`` has handlers, no ``else``, and no ``finally`` (a
  ``finally`` runs per-iteration inside the loop but once outside —
  different behavior).
* Every handler body is non-empty and ends with ``raise`` or
  ``return``. ``break`` / ``continue`` would be a ``SyntaxError``
  outside the loop, and a handler that falls through would let the
  loop continue inside but abort it outside — both skipped.
* Everything is in block form (no ``try: x()`` one-liners): the
  rewrite moves whole lines.
* The ``try:`` line carries no trailing comment (that line is rebuilt,
  so the comment would be lost). Comments on moved lines travel with
  them.
* Every moved non-blank line starts with its block's indent (a
  dedented comment inside the moved region would be corrupted
  otherwise).
* Only code inside functions is analyzed (module-level statements are
  left alone, matching the other detectors). ``return`` in a handler
  is therefore always valid at the hoisted position.
* A ``#`` character anywhere else in the replaced span skips the
  candidate.
"""

from __future__ import annotations

import ast
import io
import os
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from detectors import span_dedupe  # noqa: E402


@dataclass
class TryHoistCandidate:
    """One for-try safe to hoist the ``try`` around."""

    func_name: str
    kind: str  # always "except_hoist"
    lineno: int
    end_lineno: int
    edits: list = field(default_factory=list)
    # each edit: (start_line, start_col, end_line, end_col, text);
    # always exactly one edit replacing the whole `for` statement.
    detail: str = ""


@dataclass
class TryHoistPlan:
    safe: list[TryHoistCandidate] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (func, reason)


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _code_part(line: str) -> str | None:
    """Line minus a real trailing comment; None if it cannot be tokenized."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(line).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    cut = len(line)
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            cut = min(cut, tok.start[1])
    return line[:cut]


def _reindent(lines: list[str], old_base: str, new_base: str) -> list[str] | None:
    """Shift a line block from *old_base* to *new_base*; None if misshapen."""
    out = []
    for line in lines:
        if not line.strip():
            out.append(line)
        elif line.startswith(old_base):
            out.append(new_base + line[len(old_base):])
        else:
            return None  # dedented line (e.g. a col-0 comment) — unsafe to move
    return out


def _match_for(
    stmt: ast.For, source_lines: list[str]
) -> str | tuple[None, str] | None:
    """New source for the whole ``for`` statement, or a skip.

    Returns the replacement text, ``(None, reason)`` for a
    recognized-but-unsafe shape, or ``None`` for a non-match.
    """
    if len(stmt.body) != 1 or not isinstance(stmt.body[0], ast.Try):
        return None
    if stmt.orelse:
        return None, "for-else loop — not touched"
    attempt = stmt.body[0]
    if attempt.orelse or attempt.finalbody:
        return None, "try/except/else/finally — only bare try/except hoists"
    if not attempt.handlers:
        return None, "try without handlers — not touched"
    for handler in attempt.handlers:
        if not handler.body:
            return None, "empty handler body — not touched"
        if not isinstance(handler.body[-1], (ast.Raise, ast.Return)):
            return None, "handler falls through or breaks/continues — not touched"
        if handler.body[0].lineno == handler.lineno:
            return None, "one-liner handler — block form only"
    if attempt.body[0].lineno == attempt.lineno:
        return None, "one-liner try body — block form only"
    for_header = source_lines[stmt.lineno - 1]
    header_code = _code_part(for_header)
    if header_code is None or not header_code.rstrip().endswith(":"):
        return None, "multi-line for header — not touched"
    try_line = source_lines[attempt.lineno - 1]
    if "#" in try_line:
        return None, "comment on the try: line — not touched"
    indent = _indent_of(for_header)
    body_indent = indent + "    "
    moved_indent = body_indent + "    "
    try_body_lines = source_lines[attempt.body[0].lineno - 1: attempt.body[-1].end_lineno]
    old_body_base = _indent_of(source_lines[attempt.body[0].lineno - 1])
    new_body = _reindent(try_body_lines, old_body_base, moved_indent)
    if new_body is None:
        return None, "try body has a dedented line — not touched"
    handler_lines = source_lines[attempt.handlers[0].lineno - 1:
                                 attempt.handlers[-1].end_lineno]
    old_handler_base = _indent_of(source_lines[attempt.handlers[0].lineno - 1])
    new_handlers = _reindent(handler_lines, old_handler_base, indent)
    if new_handlers is None:
        return None, "handler has a dedented line — not touched"
    # Note: the assembled text must NOT end with a newline — the edit
    # span covers line content only, so the original line terminator
    # stays in place (adding one would double it; omitting it at EOF
    # preserves a missing terminator byte-for-byte).
    text = (
        indent + "try:\n"
        + body_indent + for_header.strip() + "\n"
        + "".join(new_body)
        + "".join(new_handlers)
    )
    return text.removesuffix("\n").removesuffix("\r")


def _span_has_hash(source_lines: list[str], start: int, end: int) -> bool:
    return any("#" in source_lines[n - 1] for n in range(start, end + 1))


def _find_in_function(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[str],
) -> list[TryHoistCandidate | tuple[str, str]]:
    out: list[TryHoistCandidate | tuple[str, str]] = []
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(func_node):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    loops = [node for node in ast.walk(func_node) if isinstance(node, ast.For)]
    matched_all: dict[int, str | tuple[None, str] | None] = {}
    for node in loops:
        matched_all[id(node)] = _match_for(node, source_lines)
    matched_ids = {nid for nid, m in matched_all.items() if isinstance(m, str)}
    for node in loops:
        # A for-try nested inside another *matched* for-try's span is
        # left for a later pass: emitting both would overlap at apply.
        ancestor = parents.get(id(node))
        nested = False
        while ancestor is not None:
            if id(ancestor) in matched_ids:
                nested = True
                break
            ancestor = parents.get(id(ancestor))
        if nested:
            continue
        matched = matched_all[id(node)]
        if matched is None:
            continue
        if isinstance(matched, tuple):
            out.append((func_node.name, matched[1]))
            continue
        end = node.end_lineno or node.lineno
        # The for-header line travels verbatim (comment included); the
        # try: line was already comment-checked. Anything else commented
        # still vetoes the move.
        if _span_has_hash(source_lines, node.lineno + 1, end):
            out.append((func_node.name, "comment inside the replaced span — not touched"))
            continue
        out.append(TryHoistCandidate(
            func_name=func_node.name,
            kind="except_hoist",
            lineno=node.lineno,
            end_lineno=end,
            edits=[(node.lineno, 0, end, len(source_lines[end - 1].rstrip("\r\n")),
                    matched)],
            detail=(f"hoist try/except around the loop instead of "
                    f"per-iteration (line {node.lineno})"),
        ))
    return out


def analyze_source(source: str, filename: str = "<string>") -> TryHoistPlan:
    """Analyze *source* text directly (no filesystem access) — testable core."""
    # keepends: _match_for slices these lines as replacement text.
    source_lines = source.splitlines(keepends=True)
    tree = ast.parse(source, filename=filename)
    plan = TryHoistPlan()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for item in _find_in_function(node, source_lines):
            if isinstance(item, TryHoistCandidate):
                plan.safe.append(item)
            else:
                plan.skipped.append(item)
    plan.safe = span_dedupe.dedupe_nested_spans(tree, plan.safe)
    plan.safe.sort(key=lambda c: (c.lineno, c.kind))
    return plan


def analyze_file(file_path: str) -> TryHoistPlan:
    source = Path(file_path).read_text(encoding="utf-8")
    return analyze_source(source, filename=file_path)
