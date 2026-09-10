"""Detect ``for`` loops (and comprehensions) iterating ``d.keys()``.

``for k in d.keys()`` builds a view object on every loop setup for no
reason; ``for k in d`` iterates the same keys. The ``list(...)`` /
``tuple(...)``-wrapped variants are doubly wasteful::

    for k in d.keys():        ->   for k in d:
    for k in list(d):         ->   for k in d:
    [f(k) for k in d.keys()]  ->   [f(k) for k in d]

Detection is pure/read-only (this module never writes a file) so it can
be shared by `classify_findings.py` (read-only triage) and
`resolvers/apply_dict_keys.py` (the actual codemod) without duplicating
logic.

Safety rules (anything ambiguous is skipped, never guessed at):

* The iterated object is a bare ``d.keys()`` call (no arguments), or a
  ``list(...)`` / ``tuple(...)`` call wrapping a bare name or such a
  ``.keys()`` call. Anything else is not this pattern.
* The loop body (or comprehension element/conditions) must not observe
  or disturb the live dict:
  - no ``del`` statement anywhere inside;
  - no subscript store (``d[k] = v``) — iterating the live dict while
    writing it raises where the snapshot form would not;
  - no ``d.pop/popitem/clear/update/setdefault`` call;
  - no in-place ``d |= ...``;
  - the name ``d`` is never loaded except as a subscript base
    (``d[k]``) or a non-mutating attribute base (``d.get(...)``) — a
    bare ``d`` passed to an arbitrary call could be mutated by it.
  Reads (``d[k]``, ``d.get(k)``, comparisons) are all fine against the
  live dict.
* ``async for`` never fires (rewriting an async iterable is out of
  scope for this tier).
* Loop targets are plain names or tuples of names; exotic targets are
  skipped.
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

MUTATING_METHODS = frozenset({"pop", "popitem", "clear", "update", "setdefault"})
WRAPPERS = frozenset({"list", "tuple"})


@dataclass
class DictKeysCandidate:
    """One ``.keys()`` iteration safe to simplify."""

    func_name: str
    kind: str  # always "dict_keys"
    lineno: int
    end_lineno: int
    edits: list = field(default_factory=list)
    # each edit: (start_line, start_col, end_line, end_col, text)
    detail: str = ""


@dataclass
class DictKeysPlan:
    safe: list[DictKeysCandidate] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (func, reason)


def _dict_name(iter_node: ast.expr) -> str | None:
    """Dict name for ``d.keys()`` / ``list(d)`` / ``tuple(d.keys())`` shapes."""
    node = iter_node
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in WRAPPERS and len(node.args) == 1
            and not node.keywords and not isinstance(node.args[0], ast.Starred)):
        node = node.args[0]
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "keys"
            and isinstance(node.func.value, ast.Name)
            and not node.args and not node.keywords):
        return node.func.value.id
    if isinstance(node, ast.Name) and node is not iter_node:
        # list(d) / tuple(d) only — a bare `for k in d` needs no fix.
        return node.id
    return None


def _body_safe(body: list[ast.stmt] | list[ast.expr], name: str) -> str | None:
    """None when the body is safe, else the skip reason."""
    parents: dict[int, ast.AST] = {}
    for root in body:
        for node in ast.walk(root):
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node
    for root in body:
        for node in ast.walk(root):
            if isinstance(node, ast.Delete):
                return "body deletes — live-dict iteration would differ"
            if isinstance(node, ast.Subscript) and not isinstance(node.ctx, ast.Load):
                return "body writes through a subscript — live-dict iteration would differ"
            if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) \
                    and node.target.id == name:
                return "body mutates the dict in place — not touched"
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == name
                    and node.func.attr in MUTATING_METHODS):
                return f"body calls d.{node.func.attr}(...) — not touched"
            if isinstance(node, ast.Name) and node.id == name \
                    and isinstance(node.ctx, ast.Load):
                parent = parents.get(id(node))
                if isinstance(parent, ast.Subscript) and parent.value is node:
                    continue  # d[k] reads are fine
                if isinstance(parent, ast.Attribute) and parent.value is node \
                        and parent.attr not in MUTATING_METHODS:
                    continue  # d.get(...) reads are fine
                return "dict name escapes to an arbitrary use — not touched"
    return None


def _loop_targets(target: ast.expr) -> bool:
    """True for plain name / tuple-of-names targets."""
    if isinstance(target, ast.Name):
        return True
    return isinstance(target, ast.Tuple) and all(
        isinstance(e, ast.Name) for e in target.elts)


def _span_has_hash(source_lines: list[str], node: ast.expr) -> bool:
    end = node.end_lineno or node.lineno
    return any("#" in source_lines[n - 1] for n in range(node.lineno, end + 1))


def _find_in_function(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[str],
) -> list[DictKeysCandidate | tuple[str, str]]:
    out: list[DictKeysCandidate | tuple[str, str]] = []

    def consider(iter_node: ast.expr, body_nodes: list, where: str) -> None:
        name = _dict_name(iter_node)
        if name is None:
            return
        reason = _body_safe(body_nodes, name)
        if reason is not None:
            out.append((func_node.name, f"{where}: {reason}"))
            return
        if _span_has_hash(source_lines, iter_node):
            out.append((func_node.name, "comment inside the replaced span — not touched"))
            return
        end = iter_node.end_lineno or iter_node.lineno
        out.append(DictKeysCandidate(
            func_name=func_node.name,
            kind="dict_keys",
            lineno=iter_node.lineno,
            end_lineno=end,
            edits=[(iter_node.lineno, iter_node.col_offset, end,
                    iter_node.end_col_offset or iter_node.col_offset, name)],
            detail=(f"iterate '{name}' directly instead of "
                    f"'{ast.unparse(iter_node)}' (line {iter_node.lineno})"),
        ))

    for node in ast.walk(func_node):
        if isinstance(node, ast.AsyncFor):
            continue  # async iterables are out of scope for this tier
        if isinstance(node, ast.For):
            if not _loop_targets(node.target):
                continue
            consider(node.iter, list(node.body), "loop body")
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for gen in node.generators:
                if gen.is_async:
                    continue
                if not _loop_targets(gen.target):
                    continue
                if isinstance(node, ast.DictComp):
                    body_nodes: list = [node.key, node.value]
                else:
                    body_nodes = [node.elt]
                body_nodes += [c for g in node.generators for c in g.ifs]
                # The other generators' iters also run per element: a bare
                # dict-name load there escapes the same way a call arg would.
                body_nodes += [g.iter for g in node.generators if g is not gen]
                consider(gen.iter, body_nodes, "comprehension")
    return out


def analyze_source(source: str, filename: str = "<string>") -> DictKeysPlan:
    """Analyze *source* text directly (no filesystem access) — testable core."""
    source_lines = source.splitlines()
    tree = ast.parse(source, filename=filename)
    plan = DictKeysPlan()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for item in _find_in_function(node, source_lines):
            if isinstance(item, DictKeysCandidate):
                plan.safe.append(item)
            else:
                plan.skipped.append(item)
    plan.safe = span_dedupe.dedupe_nested_spans(tree, plan.safe)
    plan.safe, shadow_skipped = span_dedupe.filter_shadowed(
        tree, plan.safe, {"dict_keys": {"list", "tuple"}}, check_module=True)
    plan.skipped.extend(shadow_skipped)
    plan.safe.sort(key=lambda c: (c.lineno, c.kind))
    return plan


def analyze_file(file_path: str) -> DictKeysPlan:
    source = Path(file_path).read_text(encoding="utf-8")
    return analyze_source(source, filename=file_path)
