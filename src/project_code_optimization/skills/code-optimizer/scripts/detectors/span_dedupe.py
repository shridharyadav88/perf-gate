"""Drop nested-function duplicate span candidates — read-only, never writes.

Every span-edit detector walks ``ast.walk(tree)`` over *all* function
definitions, including ones nested inside other functions. A hit inside a
nested function is therefore reported twice: once attributed to the outer
function (whose body walk reaches into the nested scope) and once to the
inner function — with identical edit spans. Feeding both to
``resolvers/span_edit.splice`` raises "overlapping span edits" and refuses
the whole file, and classifiers double-count the finding
(e.g. ``async_configs.py`` line 63 reported under both ``_with_defaults``
and ``wrapped_init``).

``dedupe_nested_spans`` removes the duplicates, keeping the candidate
attributed to the innermost enclosing function — the scope whose name,
rebinding, and freshness checks actually govern the edited code. Distinct
hits that merely share a line (different edit columns) are never merged:
identity is the full set of edit positions, not the line number.
"""

from __future__ import annotations

import ast


def _target_names(target: ast.expr) -> set[str]:
    """Names a binding target actually binds (subscript/attribute bind none)."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        found: set[str] = set()
        for elt in target.elts:
            found.update(_target_names(elt))
        return found
    return set()


def scope_binds(func_node: ast.AST, skip_ids: frozenset = frozenset()) -> set[str]:
    """Names bound in *func_node*'s own scope (read-only).

    Counts parameters, assignments, imports, ``except``/``with``/``for``
    targets, walrus targets, deletions, and nested ``def``/``class`` names
    (which bind in this scope). Nested bodies are *not* descended into —
    their stores bind nested locals — except for three things that escape
    them: nested ``def``/``class`` names (above), ``lambda`` parameter
    names, and ``global``/``nonlocal`` declarations (an over-approximation:
    they bind *some* enclosing scope, and treating them as bound here only
    ever skips a rewrite, never admits an unsafe one).

    *skip_ids* holds ``id()``s of subtrees to ignore entirely — used when
    the rewrite itself deletes the binding being tested (a hoisted
    assignment must not count as shadowing its own hoisted name; pass
    every descendant id, since the target ``Name`` is a distinct node
    from the statement holding it).
    """
    bound: set[str] = set()
    args = func_node.args  # type: ignore[attr-defined]
    for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
        bound.add(arg.arg)
    if args.vararg is not None:
        bound.add(args.vararg.arg)
    if args.kwarg is not None:
        bound.add(args.kwarg.arg)
    nested_body_ids: set[int] = set()
    for node in ast.walk(func_node):
        if node is func_node or id(node) in skip_ids:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name:
                bound.add(node.name)
            for child in ast.walk(node):
                nested_body_ids.add(id(child))
        elif isinstance(node, ast.Lambda):
            for arg in list(node.args.posonlyargs) + list(node.args.args) \
                    + list(node.args.kwonlyargs):
                bound.add(arg.arg)
            if node.args.vararg is not None:
                bound.add(node.args.vararg.arg)
            if node.args.kwarg is not None:
                bound.add(node.args.kwarg.arg)
            for child in ast.walk(node):
                nested_body_ids.add(id(child))
    for node in ast.walk(func_node):
        if node is func_node or id(node) in skip_ids:
            continue
        if id(node) in nested_body_ids:
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                bound.update(node.names)
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                bound.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name != "*":
                    bound.add(a.asname or a.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.With):
            for item in node.items:
                bound.update(_target_names(item.optional_vars))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            bound.update(_target_names(node.target))
        elif isinstance(node, ast.comprehension):
            bound.update(_target_names(node.target))
    return bound


def chain_blocked(tree: ast.Module, lineno: int,
                  skip_ids: frozenset = frozenset()) -> set[str]:
    """Names bound in the innermost function holding *lineno*, plus every
    enclosing function scope (read-only).

    Class scopes contribute nothing: methods cannot see class attributes
    as bare names, but an outer function past the class still closes over
    them, so the walk passes *through* classes without collecting. Module
    scope is not included — union with :func:`module_binds` for the full
    chain.
    """
    parent: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent.setdefault(child, node)
    best: ast.AST | None = None
    best_depth = -1
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not (node.lineno <= lineno <= (node.end_lineno or node.lineno)):
            continue
        depth = 0
        cursor = parent.get(node)
        while cursor is not None:
            if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                depth += 1
            cursor = parent.get(cursor)
        if depth > best_depth:
            best, best_depth = node, depth
    blocked: set[str] = set()
    cursor = best
    while cursor is not None:
        if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef)):
            blocked.update(scope_binds(cursor, skip_ids))
        cursor = parent.get(cursor)
    return blocked


def module_binds(tree: ast.Module) -> set[str]:
    """Names bound by top-level statements (import-time bindings)."""
    bound: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                bound.update(_target_names(target))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                bound.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name != "*":
                    bound.add(a.asname or a.name)
    return bound


def filter_shadowed(tree: ast.Module, candidates: list,
                    needs: dict[str, set[str]],
                    check_module: bool = False) -> tuple[list, list]:
    """Drop candidates whose rewrite names are rebound on the scope chain.

    *needs* maps candidate kind to the runtime names the rewrite depends
    on (introduced builtins, or builtins whose call is dropped). A
    candidate is dropped when any needed name is bound in the innermost
    function holding its line or any enclosing function — and, when
    *check_module* is set, when bound by a top-level statement. Leave
    *check_module* off whenever the detector already proves the module
    binding itself (a plain ``import asyncio``/``import re`` up there is
    the legitimate binding, not shadowing). Returns ``(kept, skipped)``
    where skipped entries are ``(func_name, reason)`` notes in the
    detectors' usual shape. Order of the survivors is preserved; anything
    malformed is kept.
    """
    try:
        mod = module_binds(tree) if check_module else set()
    except Exception:
        return list(candidates), []
    kept: list = []
    skipped: list = []
    chain_cache: dict = {}
    for cand in candidates:
        try:
            names = set(needs.get(cand.kind, ()))
            lineno = cand.lineno
            func_name = cand.func_name
        except AttributeError:
            kept.append(cand)
            continue
        if not names:
            kept.append(cand)
            continue
        mod_hit = sorted(n for n in names if n in mod)
        chain_hit: list = []
        try:
            if lineno not in chain_cache:
                chain_cache[lineno] = chain_blocked(tree, lineno)
            chain_hit = sorted(n for n in names if n in chain_cache[lineno])
        except Exception:
            chain_hit = []
        if mod_hit:
            skipped.append((func_name,
                            f"name '{mod_hit[0]}' is rebound in this module — not touched"))
        elif chain_hit:
            skipped.append((func_name,
                            f"name '{chain_hit[0]}' is rebound in a function scope — not touched"))
        else:
            kept.append(cand)
    return kept, skipped


def _positions(candidate) -> tuple | None:
    """Edit positions identifying one rewrite, or None (keep unconditionally)."""
    try:
        edits = candidate.edits
    except AttributeError:
        return None
    if not edits:
        return None
    try:
        return tuple((e[0], e[1], e[2], e[3]) for e in edits)
    except (IndexError, TypeError):
        return None


def _function_defs(tree: ast.Module) -> list:
    """(name, first_line, last_line, depth) for every function definition."""
    out: list = []

    def visit(node: ast.AST, depth: int) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((child.name, child.lineno,
                            child.end_lineno or child.lineno, depth))
                visit(child, depth + 1)
            else:
                visit(child, depth)

    visit(tree, 0)
    return out


def _innermost(defs: list, lineno: int) -> str | None:
    """Name of the deepest function definition containing *lineno*."""
    best: str | None = None
    best_depth = -1
    for name, first, last, depth in defs:
        if first <= lineno <= last and depth > best_depth:
            best, best_depth = name, depth
    return best


def dedupe_nested_spans(tree: ast.Module, candidates: list) -> list:
    """Return *candidates* minus nested-function exact-span duplicates.

    Candidates sharing ``(kind, edit positions)`` describe the same rewrite
    reached through enclosing scopes; exactly one is kept — the one
    attributed to the innermost enclosing function. When no candidate
    matches the innermost scope (should not happen), the first is kept so
    a finding is never silently lost. Order of the survivors is preserved.
    """
    defs = _function_defs(tree)
    groups: dict = {}
    order: list = []
    singles: list = []
    for index, cand in enumerate(candidates):
        try:
            key = (cand.kind, _positions(cand))
        except AttributeError:
            singles.append(index)
            continue
        if key[1] is None:
            singles.append(index)
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(index)
    keep: set = set(singles)
    for key in order:
        idxs = groups[key]
        if len(idxs) == 1:
            keep.add(idxs[0])
            continue
        try:
            inner = _innermost(defs, candidates[idxs[0]].lineno)
        except AttributeError:
            inner = None
        chosen = idxs[0]
        if inner is not None:
            for i in idxs:
                try:
                    if candidates[i].func_name == inner:
                        chosen = i
                        break
                except AttributeError:
                    continue
        keep.add(chosen)
    return [c for i, c in enumerate(candidates) if i in keep]
