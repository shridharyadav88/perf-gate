"""In-repo call graph: who calls whom, and what is reachable from entries.

Rankings by fixability alone promote dead code over hot code. This module
answers the value question with a conservative static call graph (stdlib
AST only -- names plus module-basename correlation, same matching rules as
`mine_callsites.py`): per function, how many in-repo callers it has and
whether any entry point can reach it.

Entry points: calls inside `if __name__ == "__main__"` guards, functions
named exactly `main`, and click commands/groups (`@*.command()`,
`@*.group()`). Anything else with zero in-repo callers is reported as a
neutral fact ("no in-repo callers") -- libraries legitimately expose
uncalled-public APIs -- with a stronger "likely dead" tag reserved for
_unreached private_ names.

Detection is pure/read-only (this module never writes a file, never imports
or calls target code).
"""

from __future__ import annotations

import argparse
import ast
import builtins
import json
import os
import sys
from dataclasses import dataclass, field

# Hygiene dirs only -- tests/examples/docs count as callers: a function only
# exercised by tests is still reached, just not by shipped code.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", ".tox",
    "node_modules", "build", "dist", ".eggs",
})

ENTRY_DECORATORS = ("command", "group")

# Same-file calls to these names are builtins, not repo edges (a same-file
# `def` shadowing a builtin is pathological enough to ignore explicitly).
_BUILTINS = frozenset(dir(builtins))


@dataclass
class ReachabilityIndex:
    """Conservative in-repo call graph for one repo snapshot."""

    # (file, qualname) -> sorted list of (file, qualname) callers.
    callers: dict[tuple[str, str], list[tuple[str, str]]] = field(default_factory=dict)
    # (file, qualname) entry points.
    entries: set[tuple[str, str]] = field(default_factory=set)
    # All defined (file, qualname) functions (so zero-caller ones exist).
    defined: set[tuple[str, str]] = field(default_factory=set)


def _iter_repo_files(repo: str) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")
                       and not d.endswith(".egg-info")]
        found.extend(
            os.path.join(dirpath, name)
            for name in filenames
            if name.endswith(".py")
        )
    return sorted(found)


def _parse(path: str) -> ast.Module | None:
    try:
        with open(path, encoding="utf-8") as f:
            return ast.parse(f.read(), filename=path)
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return None


def _qualname(stack: list[str], name: str) -> str:
    return ".".join([*stack, name]) if stack else name


def _is_entry_decorator(decorator: ast.expr) -> bool:
    func = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(func, ast.Attribute):
        return func.attr in ENTRY_DECORATORS
    return isinstance(func, ast.Name) and func.id in ENTRY_DECORATORS


def _index_file(path: str, stem_to_path: dict[str, str],
                index: ReachabilityIndex) -> None:
    tree = _parse(path)
    if tree is None:
        return

    imports: dict[str, str] = {}  # local name -> defining file (same-repo only)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            base = node.module.split(".")[-1]
            target = stem_to_path.get(base)
            if target is None:
                continue
            for alias in node.names:
                imports[alias.asname or alias.name] = target
        elif isinstance(node, ast.Import):
            for alias in node.names:
                base = alias.name.split(".")[-1]
                if base in stem_to_path:
                    imports[alias.asname or base] = stem_to_path[base]

    def visit(node: ast.AST, stack: list[str], in_main_guard: bool) -> None:
        # Record here (not only in the child dispatch below): conditions
        # are visited directly (`visit(child.test)`), so a bare `if f():`
        # would otherwise never pass through the Call branch.
        if isinstance(node, ast.Call):
            record_call(node, stack, in_main_guard)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = _qualname(stack, child.name)
                index.defined.add((path, qual))
                if child.name == "main" or \
                        any(_is_entry_decorator(d) for d in child.decorator_list):
                    index.entries.add((path, qual))
                visit(child, [*stack, child.name], False)
            elif isinstance(child, ast.ClassDef):
                visit(child, [*stack, child.name], False)
            elif isinstance(child, ast.If):
                guard = child.test
                visit(guard, stack, in_main_guard)  # calls in the condition count
                main_guard = (
                    isinstance(guard, ast.Compare)
                    and isinstance(guard.left, ast.Name)
                    and guard.left.id == "__name__"
                )
                for sub in (child.body, child.orelse):
                    for stmt in sub:
                        visit(stmt, stack, in_main_guard or main_guard)
            else:
                visit(child, stack, in_main_guard)

    def record_call(call: ast.Call, stack: list[str], in_main_guard: bool) -> None:
        caller = (path, ".".join(stack) if stack else "<module>")
        func = call.func
        target: tuple[str, str] | None = None
        if isinstance(func, ast.Name):
            if func.id in imports:
                target = (imports[func.id], func.id)
            elif func.id not in _BUILTINS:
                target = (path, func.id)  # same-file call
        # NOTE: `self.method()` calls are out of scope -- without receiver
        # types they cannot resolve; methods are still reached via their
        # class's constructors and external callers when those are visible.
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            resolved = imports.get(func.value.id)
            if resolved is not None:
                target = (resolved, func.attr)
        if target is None:
            return
        index.defined.add(target)
        if in_main_guard:
            index.entries.add(target)
        index.callers.setdefault(target, [])
        if caller not in index.callers[target]:
            index.callers[target].append(caller)

    visit(tree, [], False)


def analyze_repo(repo: str) -> ReachabilityIndex:
    """Build the call graph for every Python file under *repo*."""
    files = _iter_repo_files(repo)
    stem_to_path = {os.path.splitext(os.path.basename(p))[0]: p for p in files}
    index = ReachabilityIndex()
    for path in files:
        _index_file(path, stem_to_path, index)
    for callers in index.callers.values():
        callers.sort()
    return index


def caller_count(index: ReachabilityIndex, file: str, func: str) -> int:
    """In-repo caller count (methods match by qualified or bare name)."""
    total = 0
    seen: set[tuple[str, str]] = set()
    for (cfile, cfunc), callers in index.callers.items():
        if cfile == file and cfunc in (func, func.split(".")[-1]):
            for caller in callers:
                if caller not in seen:
                    seen.add(caller)
                    total += 1
    return total


def is_entry_reachable(index: ReachabilityIndex, file: str, func: str) -> bool:
    """True when some entry point can reach (file, func) through calls."""
    names = {func, func.split(".")[-1]}
    # Callee edges: invert the caller map once per query (repos are small).
    callees: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for callee, callers in index.callers.items():
        for caller in callers:
            callees.setdefault(caller, []).append(callee)
    seen: set[tuple[str, str]] = set(index.entries)
    frontier = list(index.entries)
    while frontier:
        current = frontier.pop()
        if current[0] == file and current[1] in names:
            return True
        for nxt in callees.get(current, []):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return False


def value_tag(index: ReachabilityIndex, file: str, func: str) -> str:
    """Short value label for a finding row (neutral facts, one strong claim)."""
    callers = caller_count(index, file, func)
    if callers > 0:
        word = "caller" if callers == 1 else "callers"
        return f"{callers} in-repo {word}"
    if is_entry_reachable(index, file, func):
        return "entry-reachable, no counted callers"
    if func.split(".")[-1].startswith("_"):
        return "likely dead (private, unreached)"
    return "no in-repo callers (public API?)"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="In-repo call graph: callers and entry reachability.",
    )
    parser.add_argument("--repo", required=True, help="Repo root to analyze.")
    parser.add_argument(
        "--json", action="store_true", help="Emit the index as JSON.",
    )
    args = parser.parse_args(argv)

    if not os.path.isdir(args.repo):
        print(f"Error: not a directory: '{args.repo}'", file=sys.stderr)
        sys.exit(2)
    index = analyze_repo(args.repo)
    rows = [
        {"file": f, "function": func,
         "callers": caller_count(index, f, func),
         "entry_reachable": is_entry_reachable(index, f, func),
         "value": value_tag(index, f, func)}
        for f, func in sorted(index.defined)
    ]
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("No functions found.")
        return
    for row in rows:
        print(f"{row['file']} :: {row['function']} -- {row['value']}")


if __name__ == "__main__":
    main()
