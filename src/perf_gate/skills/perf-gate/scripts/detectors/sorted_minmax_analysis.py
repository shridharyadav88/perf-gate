"""Detect ``sorted(...)`` results subscripted for an extreme.

``sorted(xs)[0]`` pays O(n log n) for an O(n) question; ``min(xs)`` asks
it directly (likewise ``[-1]``/``max``, mirrored for ``reverse=True``)::

    first = sorted(xs)[0]                    ->   first = min(xs)
    last = sorted(xs)[-1]                    ->   last = max(xs)
    first = sorted(xs, reverse=True)[0]      ->   first = max(xs)

Detection is pure/read-only (this module never writes a file) so it can
be shared by `classify_findings.py` (read-only triage) and
`resolvers/apply_sorted_minmax.py` (the actual codemod) without
duplicating logic.

Safety rules (anything ambiguous is skipped, never guessed at):

* Exactly one subscript with a constant index of ``0`` or ``-1``.
  Any other index (or slice) is a different question — skipped.
* The ``sorted`` call takes exactly one positional argument and no
  ``key=`` function: a key changes *what* is extreme, and only the
  no-key order matches ``min``/``max`` exactly. A ``reverse=`` keyword
  is allowed, but only as a constant boolean (it just flips the
  min/max mapping).
* The module must not rebind the names ``sorted``, ``min``, or
  ``max``: all three perform runtime lookups.
* Only code inside functions is analyzed (module-level statements are
  left alone, matching the other detectors).
* A ``#`` character anywhere in the replaced span skips the candidate.
"""

from __future__ import annotations

import ast
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from detectors import span_dedupe  # noqa: E402


@dataclass
class SortedMinmaxCandidate:
    """One ``sorted(...)[0/-1]`` safe to collapse to min/max."""

    func_name: str
    kind: str  # always "sorted_minmax"
    lineno: int
    end_lineno: int
    edits: list = field(default_factory=list)
    # each edit: (start_line, start_col, end_line, end_col, text)
    detail: str = ""


@dataclass
class SortedMinmaxPlan:
    safe: list[SortedMinmaxCandidate] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (func, reason)


def _shadowed(tree: ast.Module) -> set[str]:
    """Module-level names rebound by def/class/assign/import."""
    bound: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound.add(alias.asname or alias.name)
    return bound & {"sorted", "min", "max"}


def _extreme(node: ast.Subscript) -> tuple[str, str] | tuple[None, str] | None:
    """Match ``sorted(x[, reverse=B])[0/-1]``; return (replacement, kind-note).

    Returns ``(None, reason)`` for a recognized-but-unsafe shape and
    ``None`` for a non-match.
    """
    value = node.value
    if not (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id == "sorted"):
        return None
    index: object = None
    if isinstance(node.slice, ast.Constant):
        index = node.slice.value
    elif (isinstance(node.slice, ast.UnaryOp)
            and isinstance(node.slice.op, ast.USub)
            and isinstance(node.slice.operand, ast.Constant)
            and isinstance(node.slice.operand.value, int)):
        index = -node.slice.operand.value
    if type(index) is not int or index not in (0, -1):
        return None, "subscript is not [0] or [-1] — not an extreme"
    if len(value.args) != 1 or isinstance(value.args[0], ast.Starred):
        return None, "sorted(...) takes more than its single iterable"
    reverse = False
    for kw in value.keywords:
        if kw.arg == "reverse" and isinstance(kw.value, ast.Constant) \
                and type(kw.value.value) is bool:
            reverse = kw.value.value
        else:
            return None, "sorted(...) has key= or a non-constant reverse="
    func = "max" if (index == -1) != reverse else "min"
    return f"{func}({ast.unparse(value.args[0])})", func


def _span_has_hash(source_lines: list[str], node: ast.expr) -> bool:
    end = node.end_lineno or node.lineno
    return any("#" in source_lines[n - 1] for n in range(node.lineno, end + 1))


def _find_in_function(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[str],
    rebound: set[str],
) -> list[SortedMinmaxCandidate | tuple[str, str]]:
    out: list[SortedMinmaxCandidate | tuple[str, str]] = []
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Subscript) or not isinstance(node.ctx, ast.Load):
            continue
        matched = _extreme(node)
        if matched is None:
            continue
        replacement, func_or_reason = matched
        if replacement is None:
            out.append((func_node.name, func_or_reason))
            continue
        if rebound:
            out.append((func_node.name,
                        "one of sorted/min/max is rebound in this module — not touched"))
            continue
        if _span_has_hash(source_lines, node):
            out.append((func_node.name, "comment inside the replaced span — not touched"))
            continue
        end = node.end_lineno or node.lineno
        out.append(SortedMinmaxCandidate(
            func_name=func_node.name,
            kind="sorted_minmax",
            lineno=node.lineno,
            end_lineno=end,
            edits=[(node.lineno, node.col_offset, end,
                    node.end_col_offset or node.col_offset, replacement)],
            detail=(f"take {replacement.split('(')[0]}(...) directly instead of "
                    f"sorting first (line {node.lineno})"),
        ))
    return out


def analyze_source(source: str, filename: str = "<string>") -> SortedMinmaxPlan:
    """Analyze *source* text directly (no filesystem access) — testable core."""
    source_lines = source.splitlines()
    tree = ast.parse(source, filename=filename)
    plan = SortedMinmaxPlan()
    rebound = _shadowed(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for item in _find_in_function(node, source_lines, rebound):
            if isinstance(item, SortedMinmaxCandidate):
                plan.safe.append(item)
            else:
                plan.skipped.append(item)
    plan.safe = span_dedupe.dedupe_nested_spans(tree, plan.safe)
    plan.safe, shadow_skipped = span_dedupe.filter_shadowed(
        tree, plan.safe, {"sorted_minmax": {"min", "max"}})
    plan.skipped.extend(shadow_skipped)
    plan.safe.sort(key=lambda c: (c.lineno, c.kind))
    return plan


def analyze_file(file_path: str) -> SortedMinmaxPlan:
    source = Path(file_path).read_text(encoding="utf-8")
    return analyze_source(source, filename=file_path)
