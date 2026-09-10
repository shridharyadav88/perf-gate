"""Detect function-local `re.compile(...)` assignments safe to hoist to module scope.

The hoisted statement runs at import time, so every name it reads must
resolve identically there and at each call: pattern names must be bound at
module level (or be builtins), and neither the `re` receiver nor the hoisted
variable may be rebound in any function scope enclosing an occurrence.
This is a real, measured cost we found in crawl4ai's
`clean_pdf_text`/`clean_pdf_text_to_html` (see
reports/2026-09-02-pdf-utils-baseline.md) — moving it to module scope is
correct when the scope rules hold, so it's safe to detect and apply without
an LLM in the loop.

Detection is pure/read-only (this module never writes a file) so it can be
shared by `classify_findings.py` (read-only triage) and
`resolvers/apply_regex_hoist.py` (the actual codemod) without duplicating the
analysis logic.
"""

from __future__ import annotations

import ast
import builtins
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from detectors import span_dedupe  # noqa: E402

_SAFE_BUILTINS = frozenset(dir(builtins))


@dataclass
class HoistCandidate:
    """One variable name safe to hoist, merged across every function it appears in."""

    var_name: str
    source_text: str  # exact `name = re.compile(...)` statement, dedented to column 0
    functions: list[str] = field(default_factory=list)
    # (function_name, lineno, end_lineno) for every occurrence to delete, 1-indexed inclusive
    occurrences: list[tuple[str, int, int]] = field(default_factory=list)


@dataclass
class HoistPlan:
    safe: list[HoistCandidate]
    skipped: list[tuple[str, str]]  # (var_name, reason) — never touched, always reported


def _is_re_compile_call(call: ast.Call) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "compile"
        and isinstance(func.value, ast.Name)
        and func.value.id == "re"
    )


def _module_re_ok(tree: ast.Module) -> bool:
    """True when a top-level ``import re`` binds ``re`` and nothing else does.

    The hoisted statement evaluates ``re.compile`` at import time, so the
    module-global ``re`` must be the real module — any other top-level
    binding (assign/def/from-import) voids the proof.
    """
    imported = False
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "re" and alias.asname in (None, "re"):
                    imported = True
                elif (alias.asname or alias.name.split(".")[0]) == "re":
                    return False
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == "re":
                return False
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "re" for t in node.targets):
                return False
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "re":
                return False
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name) == "re":
                    return False
    return imported


def _dedent_statement(segment: str, indent: int) -> str:
    """Strip *indent* leading columns from each line of a (possibly multi-line) statement."""
    out = []
    for line in segment.split("\n"):
        out.append(line[indent:] if line[:indent].strip() == "" else line.lstrip())
    return "\n".join(out)


def find_hoist_candidates_in_function(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef, source_lines: list[str],
    module_names: set[str], tree: ast.Module,
) -> list[HoistCandidate]:
    """Find hoistable `re.compile(...)` assignments in *func_node*'s subtree.

    The hoisted statement runs at import time: pattern names must already
    be bound there (module level or builtins), and neither the ``re``
    receiver nor the hoisted variable may be rebound in any function scope
    enclosing the occurrence — a shadowing binding would change which
    object the hoisted code, or the call sites left behind, reads.
    """
    raw: list = []
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        call = node.value
        if not (isinstance(call, ast.Call) and _is_re_compile_call(call)):
            continue
        referenced = {n.id for n in ast.walk(call) if isinstance(n, ast.Name)}
        referenced.discard("re")
        if referenced - module_names - _SAFE_BUILTINS:
            continue  # import time cannot see these names — not safe to hoist
        raw.append((node, target, referenced))
    same_var_assigns: dict[str, list] = {}
    for node, target, _referenced in raw:
        same_var_assigns.setdefault(target.id, []).append(node)
    found = []
    for node, target, referenced in raw:
        # The hoisted assignments themselves are deleted by the rewrite,
        # so their own bindings must not count as shadowing the name.
        skip = set()
        for assign_node in same_var_assigns[target.id]:
            for child in ast.walk(assign_node):
                skip.add(id(child))
        chain = span_dedupe.chain_blocked(tree, node.lineno, frozenset(skip))
        if "re" in chain or (referenced & chain) or target.id in chain:
            continue  # a function scope rebinds a hoisted name — not safe
        segment = "\n".join(source_lines[node.lineno - 1 : node.end_lineno])
        found.append(
            HoistCandidate(
                var_name=target.id,
                source_text=_dedent_statement(segment, target.col_offset),
                functions=[func_node.name],
                occurrences=[(func_node.name, node.lineno, node.end_lineno)],
            )
        )
    return found


def _module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def analyze_source(source: str, filename: str = "<string>") -> HoistPlan:
    """Analyze *source* text directly (no filesystem access) — the core, testable logic."""
    source_lines = source.splitlines()
    tree = ast.parse(source, filename=filename)
    module_names = _module_level_names(tree)

    by_name: dict[str, list[HoistCandidate]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for cand in find_hoist_candidates_in_function(
                    node, source_lines, module_names, tree):
                by_name.setdefault(cand.var_name, []).append(cand)

    safe: list[HoistCandidate] = []
    skipped: list[tuple[str, str]] = []
    for var_name, cands in sorted(by_name.items(), key=lambda kv: kv[1][0].occurrences[0][1]):
        rhs_texts = {c.source_text.split("=", 1)[1].strip() for c in cands}
        if len(rhs_texts) > 1:
            skipped.append((var_name, "same name, different regex across functions — not touched"))
            continue
        if var_name in module_names:
            skipped.append((var_name, "name already used at module level — not touched"))
            continue
        safe.append(
            HoistCandidate(
                var_name=var_name,
                source_text=cands[0].source_text,
                functions=[c.functions[0] for c in cands],
                occurrences=[c.occurrences[0] for c in cands],
            )
        )
    if not _module_re_ok(tree):
        for cand in safe:
            skipped.append((cand.var_name,
                            "'re' is not the re module at import time — not touched"))
        safe = []
    return HoistPlan(safe=safe, skipped=skipped)


def analyze_file(file_path: str) -> HoistPlan:
    source = Path(file_path).read_text(encoding="utf-8")
    return analyze_source(source, filename=file_path)
