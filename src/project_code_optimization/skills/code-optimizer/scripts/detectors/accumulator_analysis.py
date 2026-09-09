"""Detect accumulator loops safe to collapse (no Ruff PERF overlap).

Three mechanical patterns: str_join/sum_reduce share the shape "neutral
init, adjacent `for` loop, single augmented-assignment body", while
set_build shares it with a single `.add()` call body instead (all checked
against PERF101/102/203/401/402/403 -- none covers these):

str_join::

    s = ""
    for x in items:
        s += f(x)              # -> s = "".join(f(x) for x in items)

sum_reduce (integer start only -- see below)::

    total = 0
    for x in items:
        total += f(x)          # -> total = sum(f(x) for x in items)

set_build::

    seen = set()
    for x in items:
        seen.add(f(x))         # -> seen = set(f(x) for x in items)

Detection is pure/read-only (this module never writes a file) so it can be
shared by `classify_findings.py` (read-only triage) and
`resolvers/apply_accumulator.py` (the actual codemod) without duplicating
logic.

Safety rules (anything ambiguous is skipped, never guessed at):

* The neutral init (`s = ""` / `total = 0`, optionally annotated) and the
  `for` loop live in the same statement block with the init immediately
  preceding the loop.
* The loop has no `else` clause and its body is exactly one augmented
  assignment (`s += e` / `total += e`), optionally wrapped in a single
  `if` with no `else`.
* The accumulated expression never references the accumulator itself.
* Loop variables are not read anywhere after the loop in the same function
  (a `for` loop leaks its variable; a genexp does not).
* `async for` never fires -- neither `"".join` nor `sum` can consume an
  async iterable.
* str_join accepts any element expression: if it isn't a string at
  runtime the original raises `TypeError` too, so only raising executions
  could differ (and then only in error type).
* sum_reduce accepts integer `0` inits only. A `0.0` init would change the
  empty-iterable result's type (`0.0` vs `sum([]) == 0`), and a nonzero
  start is a different (if easy) rewrite -- both skipped with reasons.
* set_build accepts `seen = set()` inits only, and only when the module
  does not rebind the name `set`: unlike the `""` / `0` literals the init
  performs a runtime lookup, so a shadowing binding would change the
  rewrite's meaning. Unhashable elements raise `TypeError` in the original
  too, so only raising executions could differ (message, never outcome)
  -- the same argument as str_join. Empty input is exact (`set() == set()`).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AccumulateCandidate:
    """One init+loop pair safe to collapse into a single assignment."""

    var_name: str
    kind: str  # "str_join" | "sum_reduce" | "set_build"
    func_name: str
    lineno: int  # 1-indexed inclusive range covering init..loop
    end_lineno: int
    replacement: str  # single statement text, no leading indent, trailing newline
    indent: str = ""


@dataclass
class AccumulatePlan:
    safe: list[AccumulateCandidate] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (var_name, reason)


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _loads_after(func_node: ast.FunctionDef | ast.AsyncFunctionDef, lineno: int) -> set[str]:
    """Names loaded anywhere in the function after the given line."""
    found: set[str] = set()
    for node in ast.walk(func_node):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            node_lineno = getattr(node, "lineno", 0) or 0
            if node_lineno > lineno:
                found.add(node.id)
    return found


def _loop_targets(target: ast.expr) -> list[str] | None:
    """Bound loop-variable names, or None for exotic targets (starred, attr)."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Tuple):
        names: list[str] = []
        for elt in target.elts:
            if not isinstance(elt, ast.Name):
                return None
            names.append(elt.id)
        return names
    return None


def _neutral_init(stmt: ast.stmt) -> tuple[str, object, str | None] | None:
    """Match `s = ""` / `total = 0` / `seen = set()` (or annotated).

    Return (name, value, annotation) where value is "" / 0 / "set".
    """
    target: ast.expr | None = None
    value: ast.expr | None = None
    annotation: str | None = None
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        target, value = stmt.targets[0], stmt.value
    elif (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            and stmt.value is not None and stmt.simple):
        target, value = stmt.target, stmt.value
        annotation = ast.unparse(stmt.annotation)
    if not isinstance(target, ast.Name):
        return None
    if isinstance(value, ast.Constant) and type(value.value) is str and value.value == "":
        return target.id, "", annotation
    if isinstance(value, ast.Constant) and type(value.value) is int and value.value == 0:
        return target.id, 0, annotation
    if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id == "set" and not value.args and not value.keywords):
        return target.id, "set", annotation
    return None


def _single_augassign(
    stmts: list[ast.stmt], var_name: str
) -> tuple[ast.AugAssign, ast.expr | None] | None:
    """Match `var += e` or `if c: var += e`; return (augassign, cond)."""
    if len(stmts) != 1:
        return None
    stmt = stmts[0]
    cond: ast.expr | None = None
    if isinstance(stmt, ast.If) and not stmt.orelse and len(stmt.body) == 1:
        cond = stmt.test
        stmt = stmt.body[0]
    if not isinstance(stmt, ast.AugAssign):
        return None
    if not isinstance(stmt.op, ast.Add):
        return None
    if not isinstance(stmt.target, ast.Name) or stmt.target.id != var_name:
        return None
    return stmt, cond


def _set_shadowed(tree: ast.Module) -> bool:
    """True if the module rebinds the name `set` (def/class/assign/import).

    Unlike the `""` / `0` literals, the `set()` init performs a runtime
    name lookup, so a shadowing binding would change the rewrite's
    meaning -- those modules are skipped, never guessed at.
    """
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.name == "set":
            return True
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "set" for t in node.targets):
            return True
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "set":
            return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) == "set":
                    return True
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name) == "set":
                    return True
    return False


def _single_addcall(
    stmts: list[ast.stmt], var_name: str
) -> tuple[ast.Call, ast.expr | None] | None:
    """Match `var.add(e)` or `if c: var.add(e)`; return (call, cond)."""
    if len(stmts) != 1:
        return None
    stmt = stmts[0]
    cond: ast.expr | None = None
    if isinstance(stmt, ast.If) and not stmt.orelse and len(stmt.body) == 1:
        cond = stmt.test
        stmt = stmt.body[0]
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return None
    call = stmt.value
    func = call.func
    if not (
        isinstance(func, ast.Attribute)
        and func.attr == "add"
        and isinstance(func.value, ast.Name)
        and func.value.id == var_name
    ):
        return None
    if len(call.args) != 1 or call.keywords:
        return None
    if isinstance(call.args[0], ast.Starred):
        return None
    return call, cond


def _find_in_block(
    stmts: list[ast.stmt],
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[str],
    set_ok: bool = True,
) -> list[AccumulateCandidate | tuple[str, str]]:
    """Scan one statement block for adjacent init+accumulator-loop pairs."""
    out: list[AccumulateCandidate | tuple[str, str]] = []
    for i, stmt in enumerate(stmts):
        if not isinstance(stmt, (ast.For, ast.AsyncFor)):
            continue
        if stmt.orelse:
            out.append(("", "for-else loop — not touched"))
            continue
        if isinstance(stmt, ast.AsyncFor):
            out.append(("", "async loop — join/sum/set cannot consume it"))
            continue
        bound = _loop_targets(stmt.target)
        if bound is None:
            out.append(("", "exotic loop target — not touched"))
            continue
        if i == 0:
            continue
        parsed = _neutral_init(stmts[i - 1])
        if parsed is None:
            continue
        init_idx = i - 1
        init_name, init_value, init_ann = parsed
        if init_value == "set":
            if not set_ok:
                out.append((init_name, "name 'set' is rebound in this module — not touched"))
                continue
            add = _single_addcall(stmt.body, init_name)
            if add is None:
                out.append((init_name, "loop body is not a single .add() — not touched"))
                continue
            node, cond = add
            expr = node.args[0]
        else:
            aug = _single_augassign(stmt.body, init_name)
            if aug is None:
                out.append((init_name, "loop body is not a single += — not touched"))
                continue
            node, cond = aug
            expr = node.value
        if init_name in _names(expr) or init_name in _names(stmt.iter):
            out.append((init_name, "loop references the accumulator itself — not touched"))
            continue
        if any(b in _names(stmt.iter) for b in bound):
            continue  # `for x in x` — pathological, leave alone
        if set(bound) & _loads_after(func_node, stmt.end_lineno or stmt.lineno):
            out.append((init_name, "loop variable is read after the loop — not touched"))
            continue
        if init_value == "set":
            kind = "set_build"
            rhs = _set_rhs(bound, stmt, expr, cond)
        elif isinstance(init_value, str):
            kind = "str_join"
            rhs = _join_rhs(bound, stmt, expr, cond)
        else:
            kind = "sum_reduce"
            rhs = _sum_rhs(bound, stmt, expr, cond)
        first_line = source_lines[stmts[init_idx].lineno - 1]
        indent = first_line[: len(first_line) - len(first_line.lstrip())]
        lhs = f"{init_name}: {init_ann} =" if init_ann else f"{init_name} ="
        out.append(
            AccumulateCandidate(
                var_name=init_name,
                kind=kind,
                func_name=func_node.name,
                lineno=stmts[init_idx].lineno,
                end_lineno=stmt.end_lineno or stmt.lineno,
                replacement=f"{lhs} {rhs}\n",
                indent=indent,
            )
        )
    return out


def _genexp(bound: list[ast.expr], stmt: ast.For | ast.AsyncFor,
            expr: ast.expr, cond: ast.expr | None) -> str:
    """`(expr for target in iter [if cond])` source for the accumulator body."""
    target_src = ast.unparse(stmt.target)
    iter_src = ast.unparse(stmt.iter)
    expr_src = ast.unparse(expr)
    cond_src = f" if {ast.unparse(cond)}" if cond is not None else ""
    return f"({expr_src} for {target_src} in {iter_src}{cond_src})"


def _join_rhs(bound: list[ast.expr], stmt: ast.For | ast.AsyncFor,
              expr: ast.expr, cond: ast.expr | None) -> str:
    return f'"".join({_genexp(bound, stmt, expr, cond)})'


def _sum_rhs(bound: list[ast.expr], stmt: ast.For | ast.AsyncFor,
             expr: ast.expr, cond: ast.expr | None) -> str:
    return f"sum({_genexp(bound, stmt, expr, cond)})"


def _set_rhs(bound: list[ast.expr], stmt: ast.For | ast.AsyncFor,
             expr: ast.expr, cond: ast.expr | None) -> str:
    return f"set({_genexp(bound, stmt, expr, cond)})"


def _blocks_of(stmts: list[ast.stmt]) -> list[list[ast.stmt]]:
    """Yield every plain statement-list block nested in *stmts* (incl. itself)."""
    yield stmts
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue  # nested scope: analyzed under its own function node
        for fname in ("body", "orelse", "finalbody"):
            block = getattr(stmt, fname, None)
            if isinstance(block, list) and block and all(isinstance(s, ast.stmt) for s in block):
                yield from _blocks_of(block)
        for handler in getattr(stmt, "handlers", []) or []:
            if isinstance(getattr(handler, "body", None), list):
                yield from _blocks_of(handler.body)
        for fname in ("items",):
            for item in getattr(stmt, fname, []) or []:
                body = getattr(item, "body", None)
                if isinstance(body, list) and body and all(isinstance(s, ast.stmt) for s in body):
                    yield from _blocks_of(body)


def analyze_source(source: str, filename: str = "<string>") -> AccumulatePlan:
    """Analyze *source* text directly (no filesystem access) — testable core."""
    source_lines = source.splitlines()
    tree = ast.parse(source, filename=filename)
    plan = AccumulatePlan()
    set_ok = not _set_shadowed(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for block in _blocks_of(list(node.body)):
            for item in _find_in_block(block, node, source_lines, set_ok):
                if isinstance(item, AccumulateCandidate):
                    plan.safe.append(item)
                else:
                    plan.skipped.append(item)
    plan.safe.sort(key=lambda c: (c.lineno, c.var_name))
    return plan


def analyze_file(file_path: str) -> AccumulatePlan:
    source = Path(file_path).read_text(encoding="utf-8")
    return analyze_source(source, filename=file_path)
