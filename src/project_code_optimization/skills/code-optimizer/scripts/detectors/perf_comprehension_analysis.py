"""Detect manual list-build loops safe to rewrite as comprehensions (PERF401),
plain `list(...)` copies (PERF402), or manual dict-build loops safe to
rewrite as dict comprehensions / `dict(...)` copies (PERF403) — Ruff
perflint equivalents (our applier + measurement win the overlap, same
partnership as 401/402).

PERF401 pattern::

    out = []
    for x in items:
        out.append(f(x))          # -> out = [f(x) for x in items]
    for x in items:               # -> out = [f(x) for x in items if c(x)]
        if c(x):
            out.append(f(x))

PERF402 pattern::

    out = []
    for x in items:
        out.append(x)             # -> out = list(items)

PERF403 pattern::

    d = {}
    for k, v in items:
        d[k] = v                  # -> d = dict(items)
    for x in items:               # -> d = {x.id: x for x in items if x}
        if x:
            d[x.id] = x

Detection is pure/read-only (this module never writes a file) so it can be
shared by `classify_findings.py` (read-only triage) and
`resolvers/apply_perf_comprehension.py` (the actual codemod) without
duplicating logic.

Safety rules (anything ambiguous is skipped, never guessed at):

* The empty-list init (`out = []` or `out: list[...] = []`) and the `for`
  loop live in the same statement block, with nothing between them that
  references `out`.
* The loop has no `else` clause and its body is exactly one `out.append(...)`
  call, optionally wrapped in a single `if` with no `else`.
* The iterable and the appended expression never reference `out` itself.
* The loop variable(s) are not read anywhere after the loop in the same
  function (a `for` loop leaks its variable; a comprehension does not, so a
  later read would change meaning).
* `async for` only yields PERF401 with an `async for` comprehension — plain
  `list(...)` cannot consume an async iterable, so PERF402 never fires there.
* One level of nesting folds into a nested comprehension
  (`for a: for b: out.append(f)`), with each level's guard carried into
  place; deeper nests, mixed sync/async levels, and loop `else` stay out.
* PERF403 mirrors the same rules with `d = {}` inits and single `d[k] = v`
  stores (optionally `if`-guarded): key/value/iterable never reference `d`,
  loop variables not read after, `async for` yields an `async for` dict
  comprehension. An identity `for k, v in items: d[k] = v` becomes
  `dict(items)` instead — but only when the module does not rebind the
  name `dict`, since that replacement (unlike the literals) performs a
  runtime lookup. Unhashable keys raise `TypeError` in the original too.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RewriteCandidate:
    """One init+loop pair safe to collapse into a single assignment."""

    var_name: str
    kind: str  # "perf401" | "perf402" | "perf403"
    func_name: str
    lineno: int  # 1-indexed inclusive range covering init..loop
    end_lineno: int
    replacement: str  # single statement text, no leading indent, trailing newline
    indent: str = ""


@dataclass
class RewritePlan:
    safe: list[RewriteCandidate] = field(default_factory=list)
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


def _single_append(
    stmts: list[ast.stmt], var_name: str
) -> tuple[ast.Call, ast.expr | None] | None:
    """Match `out.append(e)` or `if c: out.append(e)`; return (call, cond)."""
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
        and func.attr == "append"
        and isinstance(func.value, ast.Name)
        and func.value.id == var_name
    ):
        return None
    if len(call.args) != 1 or call.keywords:
        return None
    if isinstance(call.args[0], ast.Starred):
        return None
    return call, cond


def _empty_list_init(
    stmt: ast.stmt,
) -> tuple[str, str | None] | None:
    """Match `out = []` / `out: ... = []`; return (var_name, annotation_src)."""
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        target = stmt.targets[0]
        if (isinstance(target, ast.Name) and isinstance(stmt.value, ast.List)
                and not stmt.value.elts):
            return target.id, None
    if (
        isinstance(stmt, ast.AnnAssign)
        and isinstance(stmt.target, ast.Name)
        and isinstance(stmt.value, ast.List)
        and stmt.value is not None
        and not stmt.value.elts
        and stmt.simple
    ):
        return stmt.target.id, ast.unparse(stmt.annotation)
    return None


def _empty_dict_init(
    stmt: ast.stmt,
) -> tuple[str, str | None] | None:
    """Match `d = {}` / `d: ... = {}`; return (var_name, annotation_src)."""
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        target = stmt.targets[0]
        if (isinstance(target, ast.Name) and isinstance(stmt.value, ast.Dict)
                and not stmt.value.keys):
            return target.id, None
    if (
        isinstance(stmt, ast.AnnAssign)
        and isinstance(stmt.target, ast.Name)
        and isinstance(stmt.value, ast.Dict)
        and stmt.value is not None
        and not stmt.value.keys
        and stmt.simple
    ):
        return stmt.target.id, ast.unparse(stmt.annotation)
    return None


def _single_subscript_store(
    stmts: list[ast.stmt], var_name: str
) -> tuple[ast.Assign, ast.expr | None] | None:
    """Match `d[k] = v` or `if c: d[k] = v`; return (assign, cond)."""
    if len(stmts) != 1:
        return None
    stmt = stmts[0]
    cond: ast.expr | None = None
    if isinstance(stmt, ast.If) and not stmt.orelse and len(stmt.body) == 1:
        cond = stmt.test
        stmt = stmt.body[0]
    if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
        return None
    target = stmt.targets[0]
    if not (
        isinstance(target, ast.Subscript)
        and isinstance(target.value, ast.Name)
        and target.value.id == var_name
    ):
        return None
    if isinstance(target.slice, ast.Starred) or isinstance(stmt.value, ast.Starred):
        return None
    return stmt, cond


def _rebinds(tree: ast.Module, name: str) -> bool:
    """True if the module rebinds *name* (def/class/assign/import).

    Only consulted for rewrites whose replacement performs a runtime name
    lookup (`dict(...)` copies) -- literal-init rewrites cannot be shadowed.
    """
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.name == name:
            return True
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return True
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == name:
            return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) == name:
                    return True
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name) == name:
                    return True
    return False


def _match_loop_nest(
    stmt: ast.For | ast.AsyncFor, var_name: str
) -> tuple[ast.expr, list[tuple[ast.expr, ast.expr, ast.expr | None]]] | None:
    """Match `out.append(e)` / `if c: out.append(e)` over 1-2 loop levels.

    Return (appended expr, [(target, iter, cond), ...] outer-first), or
    None. The nested shape covers `for a: for b: out.append(f)` only --
    deeper nests, mixed sync/async levels, and loop `else` stay out.
    """
    first = _single_append(stmt.body, var_name)
    if first is not None:
        call, cond = first
        return call.args[0], [(stmt.target, stmt.iter, cond)]
    if len(stmt.body) != 1:
        return None
    inner = stmt.body[0]
    if not isinstance(inner, (ast.For, ast.AsyncFor)):
        return None
    if isinstance(inner, ast.AsyncFor) != isinstance(stmt, ast.AsyncFor):
        return None
    if inner.orelse:
        return None
    second = _single_append(inner.body, var_name)
    if second is None:
        return None
    call, inner_cond = second
    return call.args[0], [(stmt.target, stmt.iter, None),
                          (inner.target, inner.iter, inner_cond)]


def _find_in_block(
    stmts: list[ast.stmt],
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[str],
    dict_ok: bool = True,
) -> list[RewriteCandidate | tuple[str, str]]:
    """Scan one statement block for adjacent init+loop pairs."""
    out: list[RewriteCandidate | tuple[str, str]] = []
    for i, stmt in enumerate(stmts):
        if not isinstance(stmt, (ast.For, ast.AsyncFor)):
            continue
        if stmt.orelse:
            out.append(("", "for-else loop — not touched"))
            continue
        bound = _loop_targets(stmt.target)
        if bound is None:
            out.append(("", "exotic loop target — not touched"))
            continue
        # The empty init must be the immediately preceding statement:
        # anything between init and loop would be swallowed by the rewrite
        # or reordered around the comprehension's evaluation.
        if i == 0:
            continue
        init_idx = i - 1
        parsed = _empty_list_init(stmts[init_idx])
        dict_parsed = None
        if parsed is None:
            dict_parsed = _empty_dict_init(stmts[init_idx])
            if dict_parsed is None:
                continue
        is_async = isinstance(stmt, ast.AsyncFor)
        target_src = ast.unparse(stmt.target)
        iter_src = ast.unparse(stmt.iter)
        first_line = source_lines[stmts[init_idx].lineno - 1]
        indent = first_line[: len(first_line) - len(first_line.lstrip())]
        if dict_parsed is not None:
            init_name, init_ann = dict_parsed
            store = _single_subscript_store(stmt.body, init_name)
            if store is None:
                out.append((init_name, "loop body is not a single dict store — not touched"))
                continue
            assign, cond = store
            key_expr = assign.targets[0].slice
            val_expr = assign.value
            key_names = _names(key_expr) | _names(val_expr)
            iter_names = _names(stmt.iter)
            if init_name in iter_names or init_name in key_names:
                out.append((init_name, "loop references the accumulator itself — not touched"))
                continue
            if any(b in iter_names for b in bound):
                continue  # `for x in x` — pathological, leave alone
            if set(bound) & _loads_after(func_node, stmt.end_lineno or stmt.lineno):
                out.append((init_name, "loop variable is read after the loop — not touched"))
                continue
            lhs = f"{init_name}: {init_ann} =" if init_ann else f"{init_name} ="
            identity_dict = (
                len(bound) == 2 and cond is None
                and isinstance(key_expr, ast.Name) and key_expr.id == bound[0]
                and isinstance(val_expr, ast.Name) and val_expr.id == bound[1]
            )
            if identity_dict and not is_async:
                if not dict_ok:
                    out.append((init_name, "name 'dict' is rebound in this module — not touched"))
                    continue
                kind = "perf403"
                replacement = f"{lhs} dict({iter_src})\n"
            else:
                kind = "perf403"
                joiner = "async for" if is_async else "for"
                cond_src = f" if {ast.unparse(cond)}" if cond is not None else ""
                replacement = (
                    f"{lhs} {{{ast.unparse(key_expr)}: {ast.unparse(val_expr)} "
                    f"{joiner} {target_src} in {iter_src}{cond_src}}}\n"
                )
            out.append(
                RewriteCandidate(
                    var_name=init_name,
                    kind=kind,
                    func_name=func_node.name,
                    lineno=stmts[init_idx].lineno,
                    end_lineno=stmt.end_lineno or stmt.lineno,
                    replacement=replacement,
                    indent=indent,
                )
            )
            continue
        init_name, init_ann = parsed
        matched = _match_loop_nest(stmt, init_name)
        if matched is None:
            out.append((init_name, "loop body is not a single append — not touched"))
            continue
        expr, levels = matched
        level_bounds = [_loop_targets(target) for target, _, _ in levels]
        if any(b is None for b in level_bounds):
            out.append(("", "exotic loop target — not touched"))
            continue
        bound = [name for names in level_bounds for name in names]
        scoped = _names(expr)
        for _, it, cond in levels:
            scoped |= _names(it)
            if cond is not None:
                scoped |= _names(cond)
        if init_name in scoped:
            out.append((init_name, "loop references the accumulator itself — not touched"))
            continue
        if any(b in _names(it) for names, (_, it, _) in zip(level_bounds, levels) for b in names):
            continue  # `for x in x` — pathological, leave alone
        if set(bound) & _loads_after(func_node, stmt.end_lineno or stmt.lineno):
            out.append((init_name, "loop variable is read after the loop — not touched"))
            continue
        expr_src = ast.unparse(expr)
        lhs = f"{init_name}: {init_ann} =" if init_ann else f"{init_name} ="
        joiner = "async for" if is_async else "for"
        nest_src = "".join(
            f" {joiner} {ast.unparse(target)} in {ast.unparse(it)}"
            f"{f' if {ast.unparse(cond)}' if cond is not None else ''}"
            for target, it, cond in levels
        )
        identity = (
            len(levels) == 1 and levels[0][2] is None
            and isinstance(expr, ast.Name) and bound == [expr.id]
        )
        if identity and not is_async:
            kind = "perf402"
            replacement = f"{lhs} list({iter_src})\n"
        else:
            kind = "perf401"
            replacement = f"{lhs} [{expr_src}{nest_src}]\n"
        out.append(
            RewriteCandidate(
                var_name=init_name,
                kind=kind,
                func_name=func_node.name,
                lineno=stmts[init_idx].lineno,
                end_lineno=stmt.end_lineno or stmt.lineno,
                replacement=replacement,
                indent=indent,
            )
        )
    return out


def _blocks_of(
    stmts: list[ast.stmt],
) -> list[list[ast.stmt]]:
    """Yield every plain statement-list block nested in *stmts* (incl. itself)."""
    yield stmts
    for stmt in stmts:
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


def analyze_source(source: str, filename: str = "<string>") -> RewritePlan:
    """Analyze *source* text directly (no filesystem access) — testable core."""
    source_lines = source.splitlines()
    tree = ast.parse(source, filename=filename)
    plan = RewritePlan()
    dict_ok = not _rebinds(tree, "dict")
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for block in _blocks_of(list(node.body)):
            for item in _find_in_block(block, node, source_lines, dict_ok):
                if isinstance(item, RewriteCandidate):
                    plan.safe.append(item)
                else:
                    plan.skipped.append(item)
    plan.safe.sort(key=lambda c: (c.lineno, c.var_name))
    return plan


def analyze_file(file_path: str) -> RewritePlan:
    source = Path(file_path).read_text(encoding="utf-8")
    return analyze_source(source, filename=file_path)
