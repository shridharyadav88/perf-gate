"""Detect function-local `re.compile(...)` assignments safe to hoist to module scope.

If a `pattern = re.compile(...)` assignment inside a function's body doesn't
reference any of that function's parameters or other local names, it
evaluates to the exact same object on every call — recompiling it per call is
pure waste with zero behavior change. This is a real, measured cost we found
in crawl4ai's `clean_pdf_text`/`clean_pdf_text_to_html` (see
reports/2026-09-02-pdf-utils-baseline.md) — moving it to module scope is
always correct, so it's safe to detect and apply without an LLM in the loop.

Detection is pure/read-only (this module never writes a file) so it can be
shared by `classify_findings.py` (read-only triage) and
`resolvers/apply_regex_hoist.py` (the actual codemod) without duplicating the
analysis logic.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path


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


def _function_dependent_names(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names that vary call-to-call: parameters, and anything the function assigns."""
    names: set[str] = set()
    args = func_node.args
    for group in (args.posonlyargs, args.args, args.kwonlyargs):
        names.update(a.arg for a in group)
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    for node in ast.walk(func_node):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.withitem) and isinstance(node.optional_vars, ast.Name):
            names.add(node.optional_vars.id)
    return names


def _dedent_statement(segment: str, indent: int) -> str:
    """Strip *indent* leading columns from each line of a (possibly multi-line) statement."""
    out = []
    for line in segment.split("\n"):
        out.append(line[indent:] if line[:indent].strip() == "" else line.lstrip())
    return "\n".join(out)


def find_hoist_candidates_in_function(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef, source_lines: list[str],
) -> list[HoistCandidate]:
    """Find hoistable `re.compile(...)` assignments directly in *func_node*'s body.

    Nested function defs are walked too (their own params count as dependent
    only for themselves), matching how Python scoping actually works.
    """
    dependent = _function_dependent_names(func_node)
    found = []
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
        if referenced & dependent:
            continue  # depends on a parameter/local — not safe to hoist
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
            for cand in find_hoist_candidates_in_function(node, source_lines):
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
    return HoistPlan(safe=safe, skipped=skipped)


def analyze_file(file_path: str) -> HoistPlan:
    source = Path(file_path).read_text(encoding="utf-8")
    return analyze_source(source, filename=file_path)
