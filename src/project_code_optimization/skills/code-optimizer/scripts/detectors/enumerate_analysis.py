"""Detect ``for i in range(len(x))`` loops safe to write with ``enumerate``.

``for i in range(len(xs)): use(xs[i])`` re-derives the index on every
pass and re-subscripts; ``for i, v in enumerate(xs)`` binds both at
once::

    for i in range(len(xs)):     ->   for i, xs_item in enumerate(xs):
        use(xs[i])                            use(xs_item)

Detection is pure/read-only (this module never writes a file) so it can
be shared by `classify_findings.py` (read-only triage) and
`resolvers/apply_enumerate.py` (the actual codemod) without duplicating
logic.

Safety rules (anything ambiguous is skipped, never guessed at):

* The iterator is exactly ``range(len(x))`` (or ``range(0, len(x))``)
  with ``x`` a bare name. Steps, extra keywords, and non-name
  sequences are skipped.
* The index target is a single plain name ``i``.
* ``i`` is read at least once in the body (an unread index has no
  visible value to preserve), and never bound there (assign/augmented/
  delete/named-expression/loop-target/import/except binding — a rebound
  index would change meaning under either form, so both stay manual).
  Reads are always safe: ``enumerate`` yields the identical integers.
* At least one ``x[i]`` load must exist: without a subscript to
  eliminate, the rewrite only adds an unused value name for no gain,
  so index-only loops stay manual.
* The sequence ``x`` is never length-mutated in the body: no
  ``x.append/pop/insert/remove/clear/extend/popitem`` call, no
  ``del x[...]`` / ``del x``, no ``x += ...``, and ``x`` is never
  passed bare to a call (an unknown callee could mutate it) nor used
  as a method-call receiver (same reason). Rebinding ``x`` itself and
  writing ``x[i] = ...`` are both fine — the iterated range and the
  enumerated iterator are each fixed before the first pass either way.
* The fresh value name (``{x}_item``, then ``{x}_value``, then
  ``_enumerate_value``) must not collide with any name in the
  function; otherwise the candidate is skipped.
* The module and the function must not rebind ``range``, ``len``, or
  ``enumerate``: the match, the length check, and the rewrite all
  perform those runtime lookups.
* ``async for`` / async comprehensions never fire.
* Only code inside functions is analyzed (module-level statements are
  left alone, matching the other detectors).
* A ``#`` character anywhere in a replaced span skips the candidate.
"""

from __future__ import annotations

import ast
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from detectors import span_dedupe  # noqa: E402

LENGTH_MUTATORS = frozenset(
    {"append", "pop", "insert", "remove", "clear", "extend", "popitem"}
)


@dataclass
class EnumerateCandidate:
    """One ``range(len(...))`` loop safe to convert to ``enumerate``."""

    func_name: str
    kind: str  # always "enumerate"
    lineno: int
    end_lineno: int
    edits: list = field(default_factory=list)
    # each edit: (start_line, start_col, end_line, end_col, text)
    detail: str = ""


@dataclass
class EnumeratePlan:
    safe: list[EnumerateCandidate] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (func, reason)


def _seq_name(iter_node: ast.expr) -> str | None:
    """Sequence name for ``range(len(x))`` / ``range(0, len(x))``, else None."""
    if not (isinstance(iter_node, ast.Call) and isinstance(iter_node.func, ast.Name)
            and iter_node.func.id == "range" and not iter_node.keywords):
        return None
    args = iter_node.args
    if len(args) == 1:
        (length_call,) = args
    elif len(args) == 2 and isinstance(args[0], ast.Constant) \
            and args[0].value == 0:
        length_call = args[1]
    else:
        return None
    if not (isinstance(length_call, ast.Call)
            and isinstance(length_call.func, ast.Name)
            and length_call.func.id == "len" and len(length_call.args) == 1
            and not length_call.keywords
            and isinstance(length_call.args[0], ast.Name)
            and not isinstance(length_call.args[0], ast.Starred)):
        return None
    return length_call.args[0].id


def _body_uses(
    body_roots: list[ast.AST], index: str, seq: str
) -> tuple[bool, bool, list[ast.Subscript]]:
    """(index_read, ok, index_subscripts) over the loop body.

    ``ok`` is False when the index is bound or the sequence may change
    length. ``index_subscripts`` are the ``seq[index]`` load sites the
    rewrite replaces with the value name.
    """
    parents: dict[int, ast.AST] = {}
    for root in body_roots:
        for node in ast.walk(root):
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node
    index_read = False
    sites: list[ast.Subscript] = []
    nested_ids: set[int] = set()
    for root in body_roots:
        for node in ast.walk(root):
            if node is not root and isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
            ):
                for child in ast.walk(node):
                    nested_ids.add(id(child))
    for root in body_roots:
        for node in ast.walk(root):
            if id(node) in nested_ids:
                continue  # nested scope: binds/reads its own names
            if isinstance(node, ast.Name) and node.id == index:
                if isinstance(node.ctx, (ast.Store, ast.Del)):
                    return None, False, []
                index_read = True
            elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name) \
                    and node.target.id == index:
                return None, False, []
            elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) \
                    and node.target.id == index:
                return None, False, []
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                targets = [node.target] if isinstance(node.target, ast.Name) \
                    else (list(node.target.elts) if isinstance(node.target, ast.Tuple) else [])
                if any(isinstance(t, ast.Name) and t.id == index for t in targets):
                    return None, False, []
                if any(isinstance(t, ast.Name) and t.id == seq for t in targets):
                    return None, False, []
            elif isinstance(node, ast.comprehension):
                if isinstance(node.target, ast.Name) and node.target.id in (index, seq):
                    return None, False, []
                if isinstance(node.target, ast.Tuple) and any(
                        isinstance(e, ast.Name) and e.id in (index, seq)
                        for e in node.target.elts):
                    return None, False, []
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if (a.asname or a.name.split(".")[0]) in (index, seq):
                        return None, False, []
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if (a.asname or a.name) in (index, seq):
                        return None, False, []
            elif isinstance(node, ast.ExceptHandler) and node.name in (index, seq):
                return None, False, []
            elif isinstance(node, ast.With):
                for item in node.items:
                    if isinstance(item.optional_vars, ast.Name) \
                            and item.optional_vars.id in (index, seq):
                        return None, False, []
            if isinstance(node, ast.Name) and node.id == seq \
                    and isinstance(node.ctx, ast.Load):
                parent = parents.get(id(node))
                if isinstance(parent, ast.Subscript) and parent.value is node:
                    if isinstance(parent.ctx, ast.Del):
                        return None, False, []
                    # seq[...] reads and writes are length-preserving.
                elif isinstance(parent, ast.Attribute) and parent.value is node:
                    # seq.method(...) may mutate the length.
                    return None, False, []
                elif isinstance(parent, ast.Call) and (
                    any(node is a for a in parent.args)
                    or any(node is k.value for k in parent.keywords)
                ) and not (
                    isinstance(parent.func, ast.Name) and parent.func.id == "len"
                ):
                    # Bare seq passed to a call — the callee could do
                    # anything, including mutating the length. len()
                    # cannot, so it stays allowed.
                    return None, False, []
                elif isinstance(parent, ast.AugAssign) and parent.target is node:
                    return None, False, []
                elif isinstance(parent, ast.Delete):
                    return None, False, []
                # Other loads (comparisons, len(seq), arithmetic) are fine.
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) \
                    and isinstance(node.value, ast.Name) and node.value.id == seq \
                    and isinstance(node.slice, ast.Name) and node.slice.id == index:
                sites.append(node)
    if not index_read:
        return False, True, []
    return True, True, sites


def _function_names(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    return {n.id for n in ast.walk(func_node) if isinstance(n, ast.Name)}


def _module_shadowed(tree: ast.Module) -> bool:
    """True if the module rebinds range, len, or enumerate."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.name in ("range", "len", "enumerate"):
            return True
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in ("range", "len", "enumerate")
                for t in node.targets):
            return True
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id in ("range", "len", "enumerate"):
            return True
        if isinstance(node, ast.Import):
            for a in node.names:
                if (a.asname or a.name.split(".")[0]) in ("range", "len", "enumerate"):
                    return True
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if (a.asname or a.name) in ("range", "len", "enumerate"):
                    return True
    return False


def _locally_shadowed(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the function scope binds range, len, or enumerate."""
    args = func_node.args
    for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
        if arg.arg in ("range", "len", "enumerate"):
            return True
    if args.vararg is not None and args.vararg.arg in ("range", "len", "enumerate"):
        return True
    if args.kwarg is not None and args.kwarg.arg in ("range", "len", "enumerate"):
        return True
    nested_ids: set[int] = set()
    for node in ast.walk(func_node):
        if node is func_node:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            for child in ast.walk(node):
                nested_ids.add(id(child))
    for node in ast.walk(func_node):
        if id(node) in nested_ids or node is func_node:
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)) \
                and node.id in ("range", "len", "enumerate"):
            return True
        if isinstance(node, ast.Import):
            for a in node.names:
                if (a.asname or a.name.split(".")[0]) in ("range", "len", "enumerate"):
                    return True
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if (a.asname or a.name) in ("range", "len", "enumerate"):
                    return True
    return False


def _fresh_value_name(func_node: ast.FunctionDef | ast.AsyncFunctionDef,
                      seq: str) -> str | None:
    taken = _function_names(func_node)
    for candidate in (f"{seq}_item", f"{seq}_value", "_enumerate_value"):
        if candidate not in taken:
            return candidate
    return None


def _span(node: ast.expr) -> tuple[int, int, int, int]:
    return (node.lineno, node.col_offset, node.end_lineno or node.lineno,
            node.end_col_offset or node.col_offset)


def _span_has_hash(source_lines: list[str], span: tuple[int, int, int, int]) -> bool:
    return any("#" in source_lines[n - 1]
               for n in range(span[0], span[2] + 1))


def _find_in_function(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[str],
    builtins_ok: bool,
) -> list[EnumerateCandidate | tuple[str, str]]:
    out: list[EnumerateCandidate | tuple[str, str]] = []

    def consider(target: ast.expr, iter_node: ast.expr,
                 body_roots: list[ast.AST], where: str) -> None:
        if not isinstance(target, ast.Name):
            return
        seq = _seq_name(iter_node)
        if seq is None:
            return
        if not builtins_ok:
            out.append((func_node.name,
                        "range/len/enumerate is rebound — not touched"))
            return
        index = target.id
        if index == seq:
            return  # `for x in range(len(x))` — pathological, leave alone
        read, ok, sites = _body_uses(body_roots, index, seq)
        if not ok:
            out.append((func_node.name,
                        f"{where}: index rebound or sequence may change length — not touched"))
            return
        if not read:
            out.append((func_node.name, f"{where}: index is never read — not touched"))
            return
        if not sites:
            out.append((func_node.name,
                        f"{where}: index never subscripts '{seq}' — "
                        "enumerate would add an unused name — not touched"))
            return
        value_name = _fresh_value_name(func_node, seq)
        if value_name is None:
            out.append((func_node.name, "no fresh value name available — not touched"))
            return
        spans = [_span(target), _span(iter_node)]
        spans += [_span(site) for site in sites]
        if any(_span_has_hash(source_lines, span) for span in spans):
            out.append((func_node.name, "comment inside a replaced span — not touched"))
            return
        edits = [(target.lineno, target.col_offset,
                  target.end_lineno or target.lineno,
                  target.end_col_offset or target.col_offset,
                  f"{index}, {value_name}")]
        edits.append((iter_node.lineno, iter_node.col_offset,
                      iter_node.end_lineno or iter_node.lineno,
                      iter_node.end_col_offset or iter_node.col_offset,
                      f"enumerate({seq})"))
        for site in sites:
            edits.append((site.lineno, site.col_offset,
                          site.end_lineno or site.lineno,
                          site.end_col_offset or site.col_offset, value_name))
        out.append(EnumerateCandidate(
            func_name=func_node.name,
            kind="enumerate",
            lineno=target.lineno,
            end_lineno=iter_node.end_lineno or iter_node.lineno,
            edits=edits,
            detail=(f"iterate {seq} with enumerate instead of "
                    f"range(len(...)) (line {target.lineno})"),
        ))

    for node in ast.walk(func_node):
        if isinstance(node, ast.AsyncFor):
            continue
        if isinstance(node, ast.For):
            consider(node.target, node.iter, list(node.body), "loop body")
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for gen in node.generators:
                if gen.is_async:
                    continue
                if isinstance(node, ast.DictComp):
                    roots: list[ast.AST] = [node.key, node.value]
                else:
                    roots = [node.elt]
                roots += [c for g in node.generators for c in g.ifs]
                roots += [g.iter for g in node.generators if g is not gen]
                consider(gen.target, gen.iter, roots, "comprehension")
    return out


def analyze_source(source: str, filename: str = "<string>") -> EnumeratePlan:
    """Analyze *source* text directly (no filesystem access) — testable core."""
    source_lines = source.splitlines()
    tree = ast.parse(source, filename=filename)
    plan = EnumeratePlan()
    module_ok = not _module_shadowed(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        builtins_ok = module_ok and not _locally_shadowed(node)
        for item in _find_in_function(node, source_lines, builtins_ok):
            if isinstance(item, EnumerateCandidate):
                plan.safe.append(item)
            else:
                plan.skipped.append(item)
    plan.safe = span_dedupe.dedupe_nested_spans(tree, plan.safe)
    plan.safe, shadow_skipped = span_dedupe.filter_shadowed(
        tree, plan.safe, {"enumerate": {"range", "len", "enumerate"}})
    plan.skipped.extend(shadow_skipped)
    plan.safe.sort(key=lambda c: (c.lineno, c.kind))
    return plan


def analyze_file(file_path: str) -> EnumeratePlan:
    source = Path(file_path).read_text(encoding="utf-8")
    return analyze_source(source, filename=file_path)
