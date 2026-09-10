"""Detect eager list materialization fed to single-pass consumers.

``sum([x for ...])`` builds a whole list just so ``sum`` can throw it
away element by element. Feeding the consumer a generator (or the
underlying iterable directly) removes the allocation and, for
``any``/``all``, restores short-circuiting:

    sum([f(x) for x in items])      -> sum(f(x) for x in items)
    sum(list(items))                -> sum(items)
    len([x for x in items if p(x)]) -> sum(1 for x in items if p(x))
    "".join([s for s in parts])     -> "".join(s for s in parts)

Covered consumers: ``any``, ``all``, ``sum``, ``min``, ``max``,
``tuple``, ``set``, ``sorted``, ``str.join`` (any string receiver),
and ``len`` (rewritten to ``sum(1 for ...)``, the only consumer form
that counts without materializing).

Detection is pure/read-only (this module never writes a file) so it can
be shared by `classify_findings.py` (read-only triage) and
`resolvers/apply_consumer.py` (the actual codemod) without duplicating
logic.

Safety rules (anything ambiguous is skipped, never guessed at):

* Exactly one positional argument, no keywords, no ``*args``. (``min``/
  ``max`` with ``default=``/``key=`` are different calls — skipped.)
* The module must not rebind any consumed builtin name (``sum``,
  ``list``, ...): every one of these performs a runtime lookup, so a
  shadowing binding would change the rewrite's meaning.
* Full-consumption calls (``sum``/``min``/``max``/``tuple``/``set``/
  ``sorted``/``join``/``len``) iterate the input exactly once either
  way, so element side effects run in the same order — no purity
  proof needed. Raising executions can differ in message only (e.g.
  ``sum(list(5))`` vs ``sum(5)`` both raise ``TypeError``), the same
  standard the accumulator tier documents.
* Short-circuiting calls (``any``/``all``) only fire when the
  materialized elements are provably side-effect-free: a list
  comprehension whose element and every ``if`` condition are pure
  (constants, loads, arithmetic/comparison/boolean operators over pure
  values — no calls, awaits, lambdas, or nested comprehensions that
  could observe evaluation order). Impure elements are skipped with a
  reason: the generator form would run fewer of them.
* The ``list(...)``-stripping form only fires for full-consumption
  calls (never ``any``/``all`` over an arbitrary iterable, whose live
  iteration could observe side effects a snapshot would not).
* Async comprehensions never fire — a sync genexp cannot carry
  ``async for``.
* Only code inside functions is analyzed (module-level statements are
  left alone, matching the other detectors).
* A candidate strictly nested inside another candidate's span is
  dropped in favor of the outermost one, whose replacement already
  contains the inner fix — so one apply reaches fixpoint and a second
  apply is a no-op (idempotency).
* A ``#`` character anywhere in the replaced span skips the candidate:
  comments inside the span would not survive the rewrite, and a missed
  fix is always safer than a silently dropped comment.
"""

from __future__ import annotations

import ast
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from detectors import span_dedupe  # noqa: E402

FULL_CONSUMERS = frozenset({"sum", "min", "max", "tuple", "set", "sorted"})
SHORT_CONSUMERS = frozenset({"any", "all"})
BUILTIN_NAMES = frozenset(
    FULL_CONSUMERS | SHORT_CONSUMERS | {"len", "list"}
)

JOIN_METHODS = frozenset({"join"})


@dataclass
class ConsumerCandidate:
    """One consumer call safe to de-materialize."""

    func_name: str  # enclosing function
    kind: str  # consumer name, or "len" / "join"
    lineno: int
    end_lineno: int
    edits: list = field(default_factory=list)
    # each edit: (start_line, start_col, end_line, end_col, text)
    detail: str = ""


@dataclass
class ConsumerPlan:
    safe: list[ConsumerCandidate] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (func, reason)


def _pure(node: ast.expr) -> bool:
    """True if evaluating *node* provably performs no calls or awaits."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Name):
        return isinstance(node.ctx, ast.Load)
    if isinstance(node, ast.Attribute):
        return isinstance(node.ctx, ast.Load) and _pure(node.value)
    if isinstance(node, ast.Subscript):
        return (_pure(node.value) and _pure(node.slice)
                and isinstance(node.ctx, ast.Load))
    if isinstance(node, ast.BinOp):
        return _pure(node.left) and _pure(node.right)
    if isinstance(node, ast.BoolOp):
        return all(_pure(v) for v in node.values)
    if isinstance(node, ast.UnaryOp):
        return _pure(node.operand)
    if isinstance(node, ast.Compare):
        return _pure(node.left) and all(_pure(c) for c in node.comparators)
    if isinstance(node, ast.IfExp):
        return _pure(node.test) and _pure(node.body) and _pure(node.orelse)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_pure(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            (k is None or _pure(k)) and _pure(v)
            for k, v in zip(node.keys, node.values)
        )
    if isinstance(node, ast.Slice):
        return all(
            p is None or _pure(p) for p in (node.lower, node.upper, node.step))
    return False


def _shadowed(tree: ast.Module, names: frozenset) -> set[str]:
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
    return bound & set(names)


def _has_async_gen(comp: ast.ListComp) -> bool:
    return any(gen.is_async for gen in comp.generators)


def _arg_text(arg: ast.ListComp | ast.Call, rebound: frozenset) -> str:
    """Replacement source for the argument span (never the outer call).

    List comprehensions become generator expressions; ``list(X)`` becomes
    ``X``. Nested matches fold first (via the transformer), so the outer
    replacement already contains every inner fix.
    """
    transformed = _ConsumerTransformer(rebound).visit(arg)
    if isinstance(arg, ast.ListComp):
        assert isinstance(transformed, ast.ListComp)
        return ast.unparse(ast.GeneratorExp(elt=transformed.elt,
                                            generators=transformed.generators))
    assert isinstance(transformed, ast.Call)  # the list(...) wrapper
    return ast.unparse(transformed.args[0])


def _len_text(arg: ast.ListComp, rebound: frozenset) -> str:
    """``sum(1 for ...)`` for a list comprehension, inner fixes folded."""
    transformed = _ConsumerTransformer(rebound).visit(arg)
    assert isinstance(transformed, ast.ListComp)
    gens = " ".join(
        f"for {ast.unparse(gen.target)} in {ast.unparse(gen.iter)}"
        + "".join(f" if {ast.unparse(c)}" for c in gen.ifs)
        for gen in transformed.generators
    )
    return f"sum(1 {gens})"


def _span_has_hash(source_lines: list[str], node: ast.expr) -> bool:
    """True if a ``#`` appears anywhere in the node's source span."""
    end = node.end_lineno or node.lineno
    for lineno in range(node.lineno, end + 1):
        if "#" in source_lines[lineno - 1]:
            return True
    return False


def _match(
    node: ast.Call, rebound: frozenset
) -> tuple[str, str] | tuple[None, str] | None:
    """Match one consumer call; return (kind, replacement-arg-text).

    Returns ``(None, reason)`` for a recognized-but-unsafe shape and
    ``None`` for a non-match. The replacement text covers the single
    argument span (``len`` excepted — see the caller).
    """
    func = node.func
    is_join = (
        isinstance(func, ast.Attribute) and func.attr in JOIN_METHODS
        and isinstance(func.value, ast.Constant)
        and type(func.value.value) is str
    )
    if isinstance(func, ast.Name) and func.id in (FULL_CONSUMERS | SHORT_CONSUMERS | {"len"}):
        kind = func.id
    elif is_join:
        kind = "join"
    else:
        return None
    if len(node.args) != 1 or node.keywords:
        return None, "consumer takes more than its single iterable"
    arg = node.args[0]
    if isinstance(arg, ast.Starred):
        return None, "starred argument — not touched"
    if kind in SHORT_CONSUMERS:
        if not isinstance(arg, ast.ListComp):
            return None, f"{kind}(...) over a live iterable — snapshot semantics kept"
        if _has_async_gen(arg):
            return None, "async comprehension — no sync genexp form"
        if not _pure(arg.elt) or not all(_pure(c) for gen in arg.generators for c in gen.ifs):
            return None, f"{kind}(...) element may run fewer times — not touched"
        return kind, _arg_text(arg, rebound)
    if kind == "len":
        if not isinstance(arg, ast.ListComp):
            return None, "len(...) is not over a list comprehension"
        if _has_async_gen(arg):
            return None, "async comprehension — no sync genexp form"
        return kind, _len_text(arg, rebound)
    # Full-consumption calls (and join): list-comp arg or list(...) strip.
    if isinstance(arg, ast.ListComp):
        if _has_async_gen(arg):
            return None, "async comprehension — no sync genexp form"
        return kind, _arg_text(arg, rebound)
    if (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)
            and arg.func.id == "list" and len(arg.args) == 1
            and not arg.keywords and not isinstance(arg.args[0], ast.Starred)):
        return kind, _arg_text(arg, rebound)
    return None


def _find_in_function(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[str],
    rebound: frozenset,
) -> list[ConsumerCandidate | tuple[str, str]]:
    out: list[ConsumerCandidate | tuple[str, str]] = []
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(func_node):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    matches: list[tuple[ast.Call, str, str]] = []  # (node, kind, arg_text)
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        # Attribute calls other than str.join are not ours.
        if isinstance(node.func, ast.Attribute) and not (
            node.func.attr in JOIN_METHODS
            and isinstance(node.func.value, ast.Constant)
            and type(node.func.value.value) is str
        ):
            continue
        if isinstance(node.func, ast.Name) and node.func.id not in (
            FULL_CONSUMERS | SHORT_CONSUMERS | {"len"}
        ):
            continue
        matched = _match(node, rebound)
        if matched is None:
            continue
        kind, payload = matched
        if kind is None:
            out.append((func_node.name, payload))
            continue
        matches.append((node, kind, payload))
    # Keep outermost matches only; inner ones are folded into the outer
    # replacement by _arg_text/_len_text, so one apply reaches fixpoint.
    matched_ids = {id(node) for node, _, _ in matches}
    for node, kind, payload in matches:
        ancestor = parents.get(id(node))
        nested = False
        while ancestor is not None:
            if id(ancestor) in matched_ids:
                nested = True
                break
            ancestor = parents.get(id(ancestor))
        if nested:
            continue
        if _span_has_hash(source_lines, node):
            out.append((func_node.name, "comment inside the replaced span — not touched"))
            continue
        if kind == "len":
            edit = (node.lineno, node.col_offset,
                    node.end_lineno or node.lineno,
                    node.end_col_offset or node.col_offset, payload)
        else:
            arg = node.args[0]
            edit = (arg.lineno, arg.col_offset,
                    arg.end_lineno or arg.lineno,
                    arg.end_col_offset or arg.col_offset, payload)
        out.append(ConsumerCandidate(
            func_name=func_node.name,
            kind=kind,
            lineno=node.lineno,
            end_lineno=node.end_lineno or node.lineno,
            edits=[edit],
            detail=_detail(kind, node.lineno),
        ))
    return out


class _ConsumerTransformer(ast.NodeTransformer):
    """Fold nested ``func([...])`` shapes while rebuilding an expression."""

    def __init__(self, rebound: frozenset = frozenset()):
        self.rebound = rebound

    def visit_Call(self, node: ast.Call) -> ast.expr:
        self.generic_visit(node)
        func = node.func
        is_join = (
            isinstance(func, ast.Attribute) and func.attr in JOIN_METHODS
            and isinstance(func.value, ast.Constant)
            and type(func.value.value) is str
        )
        if isinstance(func, ast.Name) and func.id in (
            FULL_CONSUMERS | SHORT_CONSUMERS | {"len"}
        ):
            if func.id in self.rebound:
                return node
            kind = func.id
        elif is_join:
            kind = "join"
        else:
            return node
        if len(node.args) != 1 or node.keywords:
            return node
        arg = node.args[0]
        if isinstance(arg, ast.Starred):
            return node
        if kind == "len":
            return node  # len() folding is handled at the top level only
        if kind in SHORT_CONSUMERS and not isinstance(arg, ast.ListComp):
            return node
        if isinstance(arg, ast.ListComp):
            if _has_async_gen(arg):
                return node
            if kind in SHORT_CONSUMERS and (
                not _pure(arg.elt)
                or not all(_pure(c) for gen in arg.generators for c in gen.ifs)
            ):
                return node
            new_arg = ast.GeneratorExp(elt=arg.elt, generators=arg.generators)
            return ast.Call(func=node.func, args=[new_arg], keywords=[])
        if (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)
                and arg.func.id == "list" and len(arg.args) == 1
                and not arg.keywords
                and not isinstance(arg.args[0], ast.Starred)
                and kind not in SHORT_CONSUMERS):
            return ast.Call(func=node.func, args=[arg.args[0]], keywords=[])
        return node


def _detail(kind: str, lineno: int) -> str:
    if kind == "len":
        return f"count with sum(1 for ...) instead of len([...]) (line {lineno})"
    if kind == "join":
        return f"feed str.join a generator instead of a list (line {lineno})"
    return f"feed {kind}(...) a generator instead of a list (line {lineno})"


def analyze_source(source: str, filename: str = "<string>") -> ConsumerPlan:
    """Analyze *source* text directly (no filesystem access) — testable core."""
    source_lines = source.splitlines()
    tree = ast.parse(source, filename=filename)
    plan = ConsumerPlan()
    rebound = _shadowed(tree, BUILTIN_NAMES)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for item in _find_in_function(node, source_lines, rebound):
            if isinstance(item, ConsumerCandidate):
                if item.kind in BUILTIN_NAMES and item.kind in rebound:
                    plan.skipped.append(
                        (node.name, f"name '{item.kind}' is rebound in this module"))
                    continue
                plan.safe.append(item)
            else:
                plan.skipped.append(item)
    plan.safe = span_dedupe.dedupe_nested_spans(tree, plan.safe)
    plan.safe, shadow_skipped = span_dedupe.filter_shadowed(
        tree, plan.safe, {"len": {"sum"}})
    plan.skipped.extend(shadow_skipped)
    plan.safe.sort(key=lambda c: (c.lineno, c.kind))
    return plan


def analyze_file(file_path: str) -> ConsumerPlan:
    source = Path(file_path).read_text(encoding="utf-8")
    return analyze_source(source, filename=file_path)
