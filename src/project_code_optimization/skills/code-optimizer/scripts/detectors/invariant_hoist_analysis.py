"""Detect loop-invariant name loads safe to hoist to pre-loop locals.

Pattern (kind ``invariant_hoist``)::

    total = 0
    for x in items:
        total += SCALE * x        # -> _hoisted_SCALE = SCALE (before loop)
                                  #    total += _hoisted_SCALE * x

A module-global read costs a dict lookup per iteration; a local read does
not. The rewrite is a single alias assignment plus read-only substitution,
so the loop structure, call counts, and control flow are untouched.

Safety proof (anything ambiguous is skipped, never guessed at). For a
name ``X`` read in one loop subtree, hoisting is sound when:

* ``X`` is bound by a direct module-level statement (plain Assign /
  AnnAssign / def / class / import -- never inside ``if``/``try``/loops,
  so conditional bindings like ``try: import`` idioms do not qualify),
  with no module-level ``del X`` and no ``from m import *`` anywhere
  (unknowable bindings void the proof);
* ``X`` is bound nowhere in the function (params, stores, deletes,
  walrus, ``as`` targets, local imports, ``global``/``nonlocal``
  declarations -- a function-local ``X`` needs no hoisting anyway);
* the loop subtree contains no nested scopes, no ``yield``/``await``
  (suspension lets outside code run between iterations), no ``exec`` /
  ``eval`` / ``globals`` / ``locals`` / ``vars`` / ``setattr`` /
  ``delattr`` / ``__import__`` calls, and no ``async for`` (implicit
  suspension points). A vetoed loop vetoes its whole subtree;
* no function anywhere in the module declares ``global X`` while the
  loop subtree contains any call (a callee could rebind it; transitive
  reachability is over-approximated by banning all calls in that case);
* calls to ``X(...)`` itself never qualify (only pure loads hoist).

Deliberately out of scope (documented, not overlooked): attribute loads
(``self.cfg`` -- getter purity is unprovable locally), threads/signals,
and deliberate cross-module poking (``sys.modules[__name__].X = ...``).
The threat model matches the rest of the tool: conservative syntactic
proof plus keep-or-revert measurement, not adversarial code.

Detection is pure/read-only (this module never writes a file) so it can be
shared by `classify_findings.py` (read-only triage) and
`resolvers/apply_invariant_hoist.py` (the actual codemod) without
duplicating logic.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

#: Calls that can rebind module state from inside a loop body.
_REFLECTIVE_CALLS = frozenset({
    "exec", "eval", "globals", "locals", "vars",
    "setattr", "delattr", "__import__",
})


@dataclass
class HoistCandidate:
    """One loop-invariant name safe to hoist above its loop."""

    func_name: str
    name: str
    alias: str
    loop_lineno: int  # 1-indexed line of the `for`/`while` statement
    loads: list[tuple[int, int, int]]  # (lineno, col, end_col) per load
    indent: str = ""


@dataclass
class HoistPlan:
    safe: list[HoistCandidate] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (name, reason)


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _bound_target_names(target: ast.expr) -> set[str]:
    """Names a binding target actually binds (subscript/attribute bind none)."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return _bound_target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        found: set[str] = set()
        for elt in target.elts:
            found.update(_bound_target_names(elt))
        return found
    return set()


def _module_bindings(tree: ast.Module) -> tuple[set[str], set[str], bool]:
    """Return (bound names, `global`-declared names, star_import_present).

    Only direct module-level binding statements count -- anything nested
    in ``if``/``try``/loops is conditional and does not qualify.
    """
    bound: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                bound.update(_bound_target_names(target))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    bound.add(alias.asname or alias.name)
    declared_global: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Global):
                    declared_global.update(sub.names)
    star = any(
        isinstance(node, ast.ImportFrom)
        and any(alias.name == "*" for alias in node.names)
        for node in tree.body
    )
    deleted = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Delete)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    return bound - deleted, declared_global, star


def _function_bindings(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names bound anywhere inside *func* (params, stores, imports, ...).

    Over-approximates (e.g. comprehension targets count) -- that only
    ever skips a hoist, never admits an unsafe one.
    """
    bound = {a.arg for a in (*func.args.args, *func.args.kwonlyargs)}
    for extra in (func.args.vararg, func.args.kwarg):
        if extra is not None:
            bound.add(extra.arg)
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            # Subscript/attribute stores are not Name nodes, so a bare
            # Store-context Name here is a true rebinding (tuple
            # destructuring included).
            bound.add(node.id)
        elif isinstance(node, ast.NamedExpr):
            bound.add(node.target.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    bound.add(alias.asname or alias.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, ast.withitem):
            if node.optional_vars is not None:
                bound.update(_bound_target_names(node.optional_vars))
    return bound


def _loop_veto(loop: ast.For | ast.AsyncFor | ast.While) -> str | None:
    """Return None when the loop subtree is hoist-compatible, else a reason."""
    if isinstance(loop, ast.AsyncFor):
        return "async-for suspends between iterations — not touched"
    for node in ast.walk(loop):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Lambda)):
            return "nested scope in loop — not touched"
        if isinstance(node, (ast.Yield, ast.YieldFrom, ast.Await)):
            return "suspension point in loop — not touched"
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _REFLECTIVE_CALLS:
                return f"reflective call '{func.id}' in loop — not touched"
    return None


def _analyze_loop(loop: ast.For | ast.AsyncFor | ast.While,
                  func: ast.FunctionDef | ast.AsyncFunctionDef,
                  module_bound: set[str], module_global_declared: set[str],
                  func_bound: set[str],
                  source_lines: list[str]) -> list[HoistCandidate | tuple[str, str]]:
    out: list[HoistCandidate | tuple[str, str]] = []
    loads: dict[str, list[tuple[int, int, int]]] = {}
    has_calls = False
    for node in ast.walk(loop):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            loads.setdefault(node.id, []).append(
                (node.lineno, node.col_offset, node.end_col_offset or 0))
        elif isinstance(node, ast.Call):
            has_calls = True
    for name in sorted(loads):
        if name in func_bound:
            continue  # function-local: already a fast load, nothing to win
        if name not in module_bound:
            continue  # not provably bound at import; silent (usually a builtin)
        if has_calls and name in module_global_declared:
            out.append((name, "a callee may rebind this global — not touched"))
            continue
        alias = f"_hoisted_{name}"
        if alias in _names(func):
            out.append((name, f"alias '{alias}' already used — not touched"))
            continue
        first = source_lines[loop.lineno - 1]
        indent = first[: len(first) - len(first.lstrip())]
        out.append(HoistCandidate(
            func_name=func.name, name=name, alias=alias,
            loop_lineno=loop.lineno, loads=sorted(loads[name]), indent=indent,
        ))
    return out


def analyze_source(source: str, filename: str = "<string>") -> HoistPlan:
    """Analyze *source* text directly (no filesystem access) — testable core."""
    source_lines = source.splitlines()
    tree = ast.parse(source, filename=filename)
    plan = HoistPlan()
    module_bound, module_global_declared, star = _module_bindings(tree)
    if star:
        return plan  # unknowable star-import bindings void every proof
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        func_bound = _function_bindings(node)
        claimed: set[str] = set()  # names hoisted by an ancestor loop

        def visit(sub: ast.AST, vetoed: bool) -> None:
            for child in ast.iter_child_nodes(sub):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.ClassDef, ast.Lambda)):
                    continue  # nested scope: analyzed under its own function node
                if isinstance(child, (ast.For, ast.AsyncFor, ast.While)):
                    veto = _loop_veto(child)
                    now_vetoed = vetoed or veto is not None
                    if veto is not None and not vetoed:
                        plan.skipped.append(("", veto))
                    if not now_vetoed:
                        for item in _analyze_loop(
                                child, node, module_bound,
                                module_global_declared, func_bound, source_lines):
                            if isinstance(item, HoistCandidate):
                                if item.name in claimed:
                                    continue  # ancestor hoist covers these loads
                                claimed.add(item.name)
                                plan.safe.append(item)
                            else:
                                plan.skipped.append(item)
                    visit(child, now_vetoed)
                else:
                    visit(child, vetoed)

        visit(node, False)
    plan.safe.sort(key=lambda c: (c.loop_lineno, c.name))
    return plan


def analyze_file(file_path: str) -> HoistPlan:
    source = Path(file_path).read_text(encoding="utf-8")
    return analyze_source(source, filename=file_path)
