"""Detect membership tests against list/tuple literals safe to make sets.

``x in ["a", "b", "c", "d"]`` scans linearly on every test; ``x in
{"a", "b", "c", "d"}`` hashes once. The rewrite::

    if mode in ["fast", "slow", "auto"]:   ->   if mode in {"fast", "slow", "auto"}:

Detection is pure/read-only (this module never writes a file) so it can
be shared by `classify_findings.py` (read-only triage) and
`resolvers/apply_membership.py` (the actual codemod) without duplicating
logic.

Safety rules (anything ambiguous is skipped, never guessed at):

* Three or more elements: below that the set build costs more than the
  scan it saves, so there is no reliably measurable gain for the
  keep-or-revert loop to confirm.
* Every element is a hashable constant (``str`` / ``int`` / ``float`` /
  ``bool`` / ``bytes`` / ``None``): membership semantics are then
  identical, because set lookup agrees with ``==`` comparison on equal
  hashes, and builtin constants cannot carry pathological ``__eq__`` /
  ``__hash__`` pairs. Anything else (names, calls, subscripts,
  non-constant expressions) is skipped — hashing an arbitrary object
  can raise where comparison would not.
* Duplicate elements are fine: ``1 == True`` shares a hash, so dedup
  preserves the outcome.
* Only code inside functions is analyzed (module-level statements are
  left alone, matching the other detectors).
* A ``#`` character anywhere in the replaced span skips the candidate:
  comments inside the span would not survive the rewrite.
"""

from __future__ import annotations

import ast
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from detectors import span_dedupe  # noqa: E402

MIN_ELEMENTS = 3

_HASHABLE_CONSTANT_TYPES = (str, int, float, bool, bytes, type(None))


@dataclass
class MembershipCandidate:
    """One ``in``/``not in`` test safe to convert to a set literal."""

    func_name: str
    kind: str  # always "membership"
    lineno: int
    end_lineno: int
    edits: list = field(default_factory=list)
    # each edit: (start_line, start_col, end_line, end_col, text)
    detail: str = ""


@dataclass
class MembershipPlan:
    safe: list[MembershipCandidate] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (func, reason)


def _hashable_constant(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, _HASHABLE_CONSTANT_TYPES)
    )


def _find_in_function(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[str],
) -> list[MembershipCandidate | tuple[str, str]]:
    out: list[MembershipCandidate | tuple[str, str]] = []
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Compare):
            continue
        if len(node.ops) != 1 or not isinstance(
            node.ops[0], (ast.In, ast.NotIn)
        ):
            continue
        if len(node.comparators) != 1:
            continue
        rhs = node.comparators[0]
        if not isinstance(rhs, (ast.List, ast.Tuple)):
            continue
        if len(rhs.elts) < MIN_ELEMENTS:
            continue
        if not all(_hashable_constant(e) for e in rhs.elts):
            out.append((
                func_node.name,
                "membership literal has non-constant elements — not touched",
            ))
            continue
        end = rhs.end_lineno or rhs.lineno
        if any("#" in source_lines[n - 1]
               for n in range(rhs.lineno, end + 1)):
            out.append((
                func_node.name,
                "comment inside the replaced span — not touched",
            ))
            continue
        replacement = "{" + ", ".join(ast.unparse(e) for e in rhs.elts) + "}"
        out.append(MembershipCandidate(
            func_name=func_node.name,
            kind="membership",
            lineno=rhs.lineno,
            end_lineno=end,
            edits=[(rhs.lineno, rhs.col_offset, end,
                    rhs.end_col_offset or rhs.col_offset, replacement)],
            detail=(f"test membership against a set literal instead of a "
                    f"{'list' if isinstance(rhs, ast.List) else 'tuple'} "
                    f"literal (line {rhs.lineno})"),
        ))
    return out


def analyze_source(source: str, filename: str = "<string>") -> MembershipPlan:
    """Analyze *source* text directly (no filesystem access) — testable core."""
    source_lines = source.splitlines()
    tree = ast.parse(source, filename=filename)
    plan = MembershipPlan()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for item in _find_in_function(node, source_lines):
            if isinstance(item, MembershipCandidate):
                plan.safe.append(item)
            else:
                plan.skipped.append(item)
    plan.safe = span_dedupe.dedupe_nested_spans(tree, plan.safe)
    plan.safe.sort(key=lambda c: (c.lineno, c.kind))
    return plan


def analyze_file(file_path: str) -> MembershipPlan:
    source = Path(file_path).read_text(encoding="utf-8")
    return analyze_source(source, filename=file_path)
