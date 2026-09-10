"""Static AST performance audit: nested loops + in-loop global attribute loads.

Pure static analysis (never executes or writes files). Produces an
:class:`AuditPlan` of warn-level hints, so results can feed
``classify_findings.py`` and CSV reporting directly instead of being printed.

Two checks, both version-agnostic (``ast`` behavior is stable across the
supported 3.9+ range):

* ``nested_loop`` -- a ``for``/``async for``/``while`` loop nested inside
  another loop (O(N*M) scaling risk).
* ``attr_cache`` -- a read of ``<root>.<attr>`` inside a loop where *root*
  is a known module/global alias (default ``math``/``np``/``sys``,
  configurable). Such loads pay global/dict lookup on every iteration and
  are cheap to hoist to a local before the loop.

Deliberately NOT flagged (near-miss negatives, covered by tests):

* attribute access on locals, arguments, or loop variables (``item.name``)
  -- only configured roots are candidates;
* stores/deletes (``obj.attr = x``) -- recorded in ``skipped``, never as a
  hint, since caching does not apply to writes;
* loops inside a nested ``def`` -- attributed to the inner function, never
  counted as nested inside the outer one.

Static hints are advisory only: they never escalate a finding's tier by
themselves (no execution evidence). The classifier surfaces them on
``tier1_review`` rows; ``tier0``/``tier2``/``not_actionable`` rows are
untouched.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tier_gate import fingerprint as _fingerprint  # noqa: E402

DEFAULT_CACHED_ATTR_ROOTS = frozenset({"math", "np", "sys"})

_LOOP_NODES = (ast.For, ast.AsyncFor, ast.While)
_FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass
class AuditHint:
    """One warn-level static finding, attributed to its innermost function."""

    func_name: str
    lineno: int
    col: int
    kind: str  # "nested_loop" | "attr_cache"
    detail: str
    symbol: str = ""  # e.g. "math.pi" for attr_cache; "" otherwise

    @property
    def fingerprint(self) -> str:
        """Stable identity (P5): same hint after a line shift, same print."""
        return _fingerprint(self.kind, self.func_name, self.detail)


@dataclass
class AuditSkipped:
    """A candidate examined and dismissed, with the reason -- never acted on."""

    func_name: str
    lineno: int
    reason: str


@dataclass
class AuditPlan:
    hints: list[AuditHint] = field(default_factory=list)
    skipped: list[AuditSkipped] = field(default_factory=list)


def _iter_scope_nodes(tree: ast.AST):
    """Yield every node in *tree*, pruning nested function scopes.

    Loops inside a nested ``def`` belong to that function, so the outer
    analysis must not see them (neither as loop depth nor as nesting
    evidence). Class bodies execute inline and are NOT pruned.
    """
    stack = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, _FUNC_NODES) and node is not tree:
            continue
        stack.extend(ast.iter_child_nodes(node))
        yield node


class _FuncAuditor(ast.NodeVisitor):
    """Collect hints for one function body (nested ``def``s excluded)."""

    def __init__(self, func_name: str, cached_attr_roots: frozenset, plan: AuditPlan):
        self._func_name = func_name
        self._roots = cached_attr_roots
        self._plan = plan
        self._in_loop = 0
        self._warned_loops: set[tuple[int, int]] = set()

    # -- loop tracking --------------------------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return  # nested def: analyzed separately under its own name

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return  # nested def: analyzed separately under its own name

    def _enter_loop(self, node: ast.For | ast.AsyncFor | ast.While) -> None:
        self._in_loop += 1
        self.check_complexity(node)
        self.generic_visit(node)
        self._in_loop -= 1

    def visit_For(self, node: ast.For) -> None:
        self._enter_loop(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._enter_loop(node)

    def visit_While(self, node: ast.While) -> None:
        self._enter_loop(node)

    def check_complexity(self, node: ast.AST) -> None:
        for child in _iter_scope_nodes(node):
            if isinstance(child, _LOOP_NODES) and child is not node:
                key = (child.lineno, child.col_offset)
                if key in self._warned_loops:
                    continue
                self._warned_loops.add(key)
                self._plan.hints.append(AuditHint(
                    func_name=self._func_name,
                    lineno=child.lineno,
                    col=child.col_offset,
                    kind="nested_loop",
                    detail=(
                        f"nested '{type(child).__name__}' loop inside a loop "
                        f"in '{self._func_name}' -- O(N*M) scaling risk"
                    ),
                ))

    # -- attribute loads ------------------------------------------------------

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            self._in_loop > 0
            and isinstance(node.value, ast.Name)
            and node.value.id in self._roots
        ):
            if isinstance(node.ctx, ast.Load):
                symbol = f"{node.value.id}.{node.attr}"
                self._plan.hints.append(AuditHint(
                    func_name=self._func_name,
                    lineno=node.lineno,
                    col=node.col_offset,
                    kind="attr_cache",
                    detail=(
                        f"cache '{symbol}' to a local "
                        f"before the loop in '{self._func_name}'"
                    ),
                    symbol=symbol,
                ))
            else:
                self._plan.skipped.append(AuditSkipped(
                    func_name=self._func_name,
                    lineno=node.lineno,
                    reason=(
                        f"'{node.value.id}.{node.attr}' is written, not read "
                        "-- caching does not apply"
                    ),
                ))
        self.generic_visit(node)


def analyze_tree(
    tree: ast.AST,
    filename: str = "<string>",
    cached_attr_roots: frozenset | set | None = None,
) -> AuditPlan:
    """Audit an already-parsed *tree* (P8: share one parse across detectors)."""
    roots = frozenset(cached_attr_roots) if cached_attr_roots else DEFAULT_CACHED_ATTR_ROOTS
    plan = AuditPlan()
    for node in ast.walk(tree):
        if isinstance(node, _FUNC_NODES):
            auditor = _FuncAuditor(node.name, roots, plan)
            auditor.generic_visit(node)
    plan.hints.sort(key=lambda h: (h.func_name, h.lineno, h.col))
    return plan


def analyze_source(
    source: str,
    filename: str = "<string>",
    cached_attr_roots: frozenset | set | None = None,
) -> AuditPlan:
    """Analyze *source* text directly (no filesystem access) -- testable core."""
    tree = ast.parse(source, filename=filename)
    return analyze_tree(tree, filename=filename, cached_attr_roots=cached_attr_roots)


def analyze_file(
    file_path: str,
    cached_attr_roots: frozenset | set | None = None,
) -> AuditPlan:
    """Analyze the file at *file_path*. Raises OSError/SyntaxError like siblings."""
    source = Path(file_path).read_text(encoding="utf-8")
    return analyze_source(source, filename=file_path, cached_attr_roots=cached_attr_roots)


def main(argv: list[str] | None = None) -> None:
    """CLI: audit files, print hints, exit 0/1/2 per the gate contract.

    Default is advisory (exit 0 even with hints -- pre-commit fail-open);
    ``--strict`` exits 1 when any hint is found. Unreadable/unparseable
    files are harness errors (exit 2), never silent passes. ``--json``
    emits hints with fingerprints for CI diffing.
    """
    parser = argparse.ArgumentParser(
        description="Static AST performance audit (nested loops, in-loop global loads).",
    )
    parser.add_argument(
        "--files", nargs="+", required=True, help="Python files to audit.",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="Exit 1 when any hint is found (default: advisory, exit 0).",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit hints as JSON instead of text.",
    )
    args = parser.parse_args(argv)

    plans: dict[str, AuditPlan] = {}
    harness_errors: list[str] = []
    for path in args.files:
        try:
            plans[path] = analyze_file(path)
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            harness_errors.append(f"{path}: cannot audit ({exc})")

    if args.json:
        print(json.dumps([
            {
                "file": path, "function": h.func_name, "line": h.lineno,
                "check": h.kind, "detail": h.detail,
                "fingerprint": h.fingerprint,
            }
            for path, plan in plans.items()
            for h in plan.hints
        ], indent=2))
    else:
        for path, plan in plans.items():
            for h in plan.hints:
                print(f"[WARN] {path}:{h.lineno}: {h.detail}")
            for s in plan.skipped:
                print(f"[SKIP] {path}:{s.lineno}: {s.reason}")

    for err in harness_errors:
        print(f"Error: {err}", file=sys.stderr)

    if harness_errors:
        sys.exit(2)
    if args.strict and any(plan.hints for plan in plans.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
