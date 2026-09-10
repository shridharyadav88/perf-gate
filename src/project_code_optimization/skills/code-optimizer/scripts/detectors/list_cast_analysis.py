"""Detect ``list(...)`` wrappers around already-fresh iterables.

``for x in list(items)`` materializes a copy just to iterate it once;
``for x in items`` walks the original. The copy buys nothing unless the
loop needs a snapshot (mutation during iteration — guarded below)::

    for x in list(items):        ->   for x in items:
    total = sum(list(f(x)        ->   total = sum(f(x)
                for x in xs))                    for x in xs)

Detection is pure/read-only (this module never writes a file) so it can
be shared by `classify_findings.py` (read-only triage) and
`resolvers/apply_list_cast.py` (the actual codemod) without duplicating
logic.

Safety rules (anything ambiguous is skipped, never guessed at):

* The ``list(...)`` call takes exactly one positional argument and no
  keywords. Anything else is not this pattern.
* The inner expression is provably a fresh temporary — a tuple / list /
  set / dict literal, a comprehension, a generator expression, a
  ``range(...)`` call, or a ``sorted(...)`` / ``reversed(...)`` call.
  A bare name (or anything else) may alias live state the loop
  mutates, so the snapshot is kept. Fresh temporaries have no other
  handle, so nothing can observe whether the loop walks them directly
  or through a copy.
* The module must not rebind the name ``list``.
* The ``list(...)`` call must sit directly in a ``for``/``async for``
  iterator or a comprehension generator iterator: single-pass
  positions. A ``list(...)`` anywhere else (an assignment, a call
  argument, a return) may rely on having a real list.
* ``async for`` / async comprehensions never fire.
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
class ListCastCandidate:
    """One ``list(...)`` wrapper safe to drop."""

    func_name: str
    kind: str  # always "list_cast"
    lineno: int
    end_lineno: int
    edits: list = field(default_factory=list)
    # each edit: (start_line, start_col, end_line, end_col, text)
    detail: str = ""


@dataclass
class ListCastPlan:
    safe: list[ListCastCandidate] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (func, reason)


def _shadowed(tree: ast.Module) -> bool:
    """True if the module rebinds the name ``list``."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.name == "list":
            return True
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "list" for t in node.targets):
            return True
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "list":
            return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) == "list":
                    return True
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name) == "list":
                    return True
    return False


def _fresh_temporary(node: ast.expr) -> bool:
    """True if *node* provably builds a value nothing else can observe."""
    if isinstance(node, (ast.Tuple, ast.List, ast.Set, ast.Dict,
                         ast.ListComp, ast.SetComp, ast.DictComp,
                         ast.GeneratorExp)):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in ("range", "sorted", "reversed"):
        return True
    return False


def _span_has_hash(source_lines: list[str], node: ast.expr) -> bool:
    end = node.end_lineno or node.lineno
    return any("#" in source_lines[n - 1] for n in range(node.lineno, end + 1))


def _is_list_call(node: ast.expr) -> bool:
    """Single-argument, keyword-free ``list(...)`` shape (any inner)."""
    return (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "list" and len(node.args) == 1
        and not node.keywords and not isinstance(node.args[0], ast.Starred)
    )


def _unwrap(node: ast.Call) -> ast.expr | None:
    """Strip every ``list(...)`` layer; None unless the core is fresh.

    ``list(list([1, 2]))`` unwraps to ``[1, 2]`` in one rewrite so a
    single apply reaches fixpoint.
    """
    current: ast.expr = node
    while _is_list_call(current):
        assert isinstance(current, ast.Call)
        current = current.args[0]
    if isinstance(current, ast.Call):
        return None  # bottomed out in a non-list call — not provably fresh
    if not _fresh_temporary(current):
        return None
    return current


def _find_in_function(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[str],
    list_ok: bool,
) -> list[ListCastCandidate | tuple[str, str]]:
    out: list[ListCastCandidate | tuple[str, str]] = []
    iters: list[tuple[ast.expr, bool]] = []  # (iter, async_context)
    for node in ast.walk(func_node):
        if isinstance(node, (ast.For, ast.AsyncFor)):
            iters.append((node.iter, isinstance(node, ast.AsyncFor)))
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp,
                               ast.GeneratorExp)):
            iters.extend((gen.iter, gen.is_async) for gen in node.generators)
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(func_node):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    for iter_node, is_async in iters:
        node = iter_node
        if not _is_list_call(node):
            continue
        assert isinstance(node, ast.Call)
        if is_async:
            out.append((func_node.name, "async iteration — not touched"))
            continue
        ancestor = parents.get(id(node))
        nested = False
        while ancestor is not None:
            if _is_list_call(ancestor):
                nested = True
                break
            ancestor = parents.get(id(ancestor))
        if nested:
            continue  # the outermost list(...) unwraps every layer at once
        core = _unwrap(node)
        if core is None:
            out.append((func_node.name,
                        "list(...) wraps a possibly-aliased value — snapshot kept"))
            continue
        if not list_ok:
            out.append((func_node.name,
                        "name 'list' is rebound in this module — not touched"))
            continue
        if _span_has_hash(source_lines, node):
            out.append((func_node.name, "comment inside the replaced span — not touched"))
            continue
        end = node.end_lineno or node.lineno
        out.append(ListCastCandidate(
            func_name=func_node.name,
            kind="list_cast",
            lineno=node.lineno,
            end_lineno=end,
            edits=[(node.lineno, node.col_offset, end,
                    node.end_col_offset or node.col_offset,
                    ast.unparse(core))],
            detail=(f"iterate {ast.unparse(core)} directly instead of "
                    f"copying it with list(...) (line {node.lineno})"),
        ))
    return out


def analyze_source(source: str, filename: str = "<string>") -> ListCastPlan:
    """Analyze *source* text directly (no filesystem access) — testable core."""
    source_lines = source.splitlines()
    tree = ast.parse(source, filename=filename)
    plan = ListCastPlan()
    list_ok = not _shadowed(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for item in _find_in_function(node, source_lines, list_ok):
            if isinstance(item, ListCastCandidate):
                plan.safe.append(item)
            else:
                plan.skipped.append(item)
    plan.safe = span_dedupe.dedupe_nested_spans(tree, plan.safe)
    plan.safe, shadow_skipped = span_dedupe.filter_shadowed(
        tree, plan.safe, {"list_cast": {"list"}})
    plan.skipped.extend(shadow_skipped)
    plan.safe.sort(key=lambda c: (c.lineno, c.kind))
    return plan


def analyze_file(file_path: str) -> ListCastPlan:
    source = Path(file_path).read_text(encoding="utf-8")
    return analyze_source(source, filename=file_path)
