"""Detect blocking ``time.sleep(...)`` calls inside async functions.

``time.sleep`` blocks the event loop's only thread; ``await
asyncio.sleep`` yields it::

    async def poll():
        time.sleep(1)      ->   await asyncio.sleep(1)

Detection is pure/read-only (this module never writes a file) so it can
be shared by `classify_findings.py` (read-only triage) and
`resolvers/apply_async_sleep.py` (the actual codemod) without
duplicating logic.

Safety rules (anything ambiguous is skipped, never guessed at):

* The call is ``time.sleep(...)`` with ``time`` a plain name (any
  arguments, positional or keyword, are preserved verbatim).
* The innermost enclosing function definition is ``async``: a nested
  synchronous ``def`` inside an async function cannot ``await``.
* The call is not in a decorator, default value, or annotation: those
  evaluate at definition time, outside any event loop turn.
* The module already imports ``asyncio`` (plain or aliased — the alias
  is reused in the rewrite). Adding imports is out of scope for this
  tier; missing imports are skipped with a reason.
* The module must not rebind the names ``time`` or ``asyncio`` (or the
  asyncio alias when aliased).
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
class AsyncSleepCandidate:
    """One blocking sleep safe to convert to an awaited sleep."""

    func_name: str
    kind: str  # always "async_sleep"
    lineno: int
    end_lineno: int
    edits: list = field(default_factory=list)
    # each edit: (start_line, start_col, end_line, end_col, text)
    detail: str = ""


@dataclass
class AsyncSleepPlan:
    safe: list[AsyncSleepCandidate] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (func, reason)


def _module_facts(tree: ast.Module) -> tuple[str | None, set[str]]:
    """(asyncio alias or None, rebound names among time/asyncio)."""
    alias: str | None = None
    rebound: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in ("time", "asyncio"):
                rebound.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ("time", "asyncio"):
                    rebound.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id in ("time", "asyncio"):
            rebound.add(node.target.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "asyncio" and a.asname is None:
                    alias = "asyncio"
                elif a.name == "asyncio" and a.asname:
                    alias = a.asname
                # Plain `import time` / `import asyncio` bind the real
                # modules — only an `as` alias shadowing the name counts.
                if a.asname in ("time", "asyncio"):
                    rebound.add(a.asname)
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if (a.asname or a.name) in ("time", "asyncio"):
                    rebound.add(a.asname or a.name)
    if alias in rebound:
        alias = None
    return alias, rebound


def _header_nodes(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[int]:
    """Ids of every node in decorators, defaults, and annotations."""
    ids: set[int] = set()

    def collect(node: ast.AST) -> None:
        for child in ast.walk(node):
            ids.add(id(child))

    for dec in func.decorator_list:
        collect(dec)
    for default in list(func.args.defaults) + [
        d for d in func.args.kw_defaults if d is not None
    ]:
        collect(default)
    if func.returns is not None:
        collect(func.returns)
    for arg in list(func.args.posonlyargs) + list(func.args.args) \
            + list(func.args.kwonlyargs):
        if arg.annotation is not None:
            collect(arg.annotation)
    return ids


def _locally_bound(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names bound in the function scope itself (params count).

    A ``time`` parameter shadows the module even though no module-level
    rebinding exists — rewriting its ``.sleep`` attribute would turn
    raising code into working code. Nested scopes contribute only their
    defined names: a ``def time`` two levels down does not shadow this
    scope's read, so its body is never descended into.
    """
    bound: set[str] = set()
    args = func_node.args
    for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
        bound.add(arg.arg)
    if args.vararg is not None:
        bound.add(args.vararg.arg)
    if args.kwarg is not None:
        bound.add(args.kwarg.arg)
    nested_ids: set[int] = set()
    for node in ast.walk(func_node):
        if node is func_node:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            bound.add(getattr(node, "name", ""))
            for child in ast.walk(node):
                nested_ids.add(id(child))
    for node in ast.walk(func_node):
        if id(node) in nested_ids or node is func_node:
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                bound.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                bound.add(a.asname or a.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
    bound.discard("")
    return bound


def _span_has_hash(source_lines: list[str], node: ast.expr) -> bool:
    end = node.end_lineno or node.lineno
    return any("#" in source_lines[n - 1] for n in range(node.lineno, end + 1))


def _find_in_function(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[str],
    asyncio_alias: str | None,
    rebound: set[str],
) -> list[AsyncSleepCandidate | tuple[str, str]]:
    out: list[AsyncSleepCandidate | tuple[str, str]] = []
    header_ids = _header_nodes(func_node)
    is_async = isinstance(func_node, ast.AsyncFunctionDef)
    where = func_node.name
    local_bound = _locally_bound(func_node)
    # ast.walk does not prune: exclude every node under a nested scope
    # explicitly (a nested sync def inside an async function cannot await).
    nested_ids: set[int] = set()
    for node in ast.walk(func_node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node is not func_node:
            for child in ast.walk(node):
                nested_ids.add(id(child))
    for node in ast.walk(func_node):
        if id(node) in nested_ids:
            continue  # nested scope: analyzed under its own function node
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "sleep"
                and isinstance(func.value, ast.Name)
                and func.value.id == "time"):
            continue
        if id(node) in header_ids:
            out.append((where, "sleep call in a decorator/default/annotation — not touched"))
            continue
        if not is_async:
            out.append((where, "enclosing function is synchronous — cannot await"))
            continue
        if "time" in rebound or "time" in local_bound:
            out.append((where, "name 'time' is rebound — not touched"))
            continue
        if asyncio_alias is None or asyncio_alias in local_bound:
            out.append((where, "asyncio is not imported — not touched"))
            continue
        if _span_has_hash(source_lines, node):
            out.append((where, "comment inside the replaced span — not touched"))
            continue
        arg_src = ", ".join(
            [ast.unparse(a) for a in node.args]
            + [f"{k.arg}={ast.unparse(k.value)}" if k.arg else ast.unparse(k.value)
               for k in node.keywords]
        )
        replacement = f"await {asyncio_alias}.sleep({arg_src})"
        end = node.end_lineno or node.lineno
        out.append(AsyncSleepCandidate(
            func_name=func_node.name,
            kind="async_sleep",
            lineno=node.lineno,
            end_lineno=end,
            edits=[(node.lineno, node.col_offset, end,
                    node.end_col_offset or node.col_offset, replacement)],
            detail=(f"await {asyncio_alias}.sleep(...) instead of blocking "
                    f"time.sleep(...) (line {node.lineno})"),
        ))
    return out


def analyze_source(source: str, filename: str = "<string>") -> AsyncSleepPlan:
    """Analyze *source* text directly (no filesystem access) — testable core."""
    source_lines = source.splitlines()
    tree = ast.parse(source, filename=filename)
    plan = AsyncSleepPlan()
    asyncio_alias, rebound = _module_facts(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for item in _find_in_function(node, source_lines, asyncio_alias, rebound):
            if isinstance(item, AsyncSleepCandidate):
                plan.safe.append(item)
            else:
                plan.skipped.append(item)
    plan.safe = span_dedupe.dedupe_nested_spans(tree, plan.safe)
    shadow_needs = {"async_sleep": {"time"} | ({asyncio_alias} if asyncio_alias else set())}
    plan.safe, shadow_skipped = span_dedupe.filter_shadowed(
        tree, plan.safe, shadow_needs)
    plan.skipped.extend(shadow_skipped)
    plan.safe.sort(key=lambda c: (c.lineno, c.kind))
    return plan


def analyze_file(file_path: str) -> AsyncSleepPlan:
    source = Path(file_path).read_text(encoding="utf-8")
    return analyze_source(source, filename=file_path)
