"""Detect module-level `re.*` calls safe to hoist to compiled calls.

Extends the `tier0_regex_hoist` family from hoisting `re.compile(...)`
statements to rewriting repeated `re.<method>(<constant pattern>, ...)`
call sites (no Ruff PERF overlap -- checked against PERF101/102/203/401/
402/403, none covers this)::

    m = re.match(r"(\\d+)-(\\d+)", s)   # -> _re_match = re.compile(...)
                                        #    m = _re_match.match(s)

Detection is pure/read-only (this module never writes a file) so it can be
shared by `classify_findings.py` (read-only triage) and
`resolvers/apply_re_call.py` (the actual codemod) without duplicating logic.

Safety rules (anything ambiguous is skipped, never guessed at):

* Only the attribute form (`re.<method>` / an `import re as <alias>` alias).
  `from re import sub` style would need an import edit to reference
  `re.compile` -- skipped with a reason.
* The pattern argument is a constant string (inline flags like `(?i)` are
  fine -- they travel with the pattern).
* No `*args` / `**kwargs` in the call; every other argument is preserved
  verbatim and in order (`flags=` moves into the `compile()` call, since a
  compiled pattern already carries it; positional flag slots are mapped per
  method, anything unrecognized is skipped).
* Single-line calls only (column surgery on multi-line calls is skipped).
* The call must be the entire value of an assignment (`x = ...`, including
  annotated), a `return`, or a bare expression statement -- never nested
  inside a larger expression (replacing a subexpression span is still line
  surgery, but scoping review to statement values keeps the proof small).
* The hoisted module-level name (`_re_<method>`, suffixed on collision)
  must not already exist at module scope.
"""

from __future__ import annotations

import ast
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from detectors import span_dedupe  # noqa: E402

# method -> positional parameter names AFTER the pattern (for flag slots).
METHOD_PARAMS: dict[str, list[str]] = {
    "match": ["string", "flags"],
    "fullmatch": ["string", "flags"],
    "search": ["string", "flags"],
    "findall": ["string", "flags"],
    "finditer": ["string", "flags"],
    "sub": ["repl", "string", "count", "flags"],
    "subn": ["repl", "string", "count", "flags"],
    "split": ["string", "maxsplit", "flags"],
}


@dataclass
class ReCallCandidate:
    """One module-level `re.*` call site safe to rewrite to a hoisted call."""

    func_name: str
    lineno: int  # 1-indexed line of the call
    col: int  # UTF-8 byte offset of the call start within its line
    end_col: int  # UTF-8 byte offset of the call end
    method: str
    module_name: str  # hoisted `_re_<method>[_N]` name
    hoist_src: str  # `_re_x = re.compile(<pattern>[, <flags>])` statement
    new_call_src: str  # replacement expression source
    call_src: str  # original call source (verify key + mismatch messages)


@dataclass
class ReCallPlan:
    safe: list[ReCallCandidate] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (where, reason)


def _module_names(tree: ast.Module) -> set[str]:
    """Names bound at module scope (collision set for hoisted names)."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            names.update(n.id for n in ast.walk(node.target) if isinstance(n, ast.Name))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.For):
            targets = node.target
            nodes = targets.elts if isinstance(targets, ast.Tuple) else [targets]
            for target in nodes:
                names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
    return names


def _re_aliases(tree: ast.Module) -> dict[str, str]:
    """Map local name -> canonical `re` module reference for `import re [as x]`."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "re":
                    aliases[alias.asname or "re"] = alias.asname or "re"
    return aliases


def _split_flags(method: str, args: list[ast.expr], keywords: list[ast.keyword]
                 ) -> tuple[list[ast.expr], str | None, list[ast.keyword]] | None:
    """Split call args into (kept_positionals, flags_src, kept_keywords)."""
    params = METHOD_PARAMS[method]
    kept_positionals: list[ast.expr] = []
    flags_src: str | None = None
    for i, arg in enumerate(args):
        if i >= len(params):
            return None  # unknown signature shape -- not touched
        if params[i] == "flags":
            flags_src = ast.unparse(arg)
        else:
            kept_positionals.append(arg)
    kept_keywords: list[ast.keyword] = []
    for keyword in keywords:
        if keyword.arg is None:
            return None  # **kwargs -- not touched
        if keyword.arg == "flags":
            flags_src = ast.unparse(keyword.value)
        else:
            kept_keywords.append(keyword)
    return kept_positionals, flags_src, kept_keywords


def _call_at(stmt: ast.stmt) -> ast.Call | None:
    """The re.* call when it IS the whole statement value, else None."""
    value: ast.expr | None = None
    if isinstance(stmt, ast.Expr):
        value = stmt.value
    elif isinstance(stmt, ast.Return):
        value = stmt.value
    elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        value = stmt.value
    elif (isinstance(stmt, ast.AnnAssign) and stmt.value is not None and stmt.simple):
        value = stmt.value
    if isinstance(value, ast.Call):
        return value
    return None


def _innermost_scope(node: ast.AST, parent_of: dict[ast.AST, ast.AST],
                     stop: ast.AST) -> ast.AST | None:
    """Nearest enclosing scope-ish ancestor of *node* below *stop* (or None)."""
    current = parent_of.get(node)
    while current is not None and current is not stop:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return current
        current = parent_of.get(current)
    return None


def _find_in_function(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    aliases: dict[str, str],
    taken: set[str],
    tree: ast.Module,
) -> list[ReCallCandidate | tuple[str, str]]:
    out: list[ReCallCandidate | tuple[str, str]] = []
    parent_of: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(func_node):
        for child in ast.iter_child_nodes(parent):
            parent_of.setdefault(child, parent)
    for stmt in ast.walk(func_node):
        if not isinstance(stmt, (ast.Expr, ast.Return, ast.Assign, ast.AnnAssign)):
            continue
        if _innermost_scope(stmt, parent_of, func_node) is not None:
            continue  # belongs to a nested def/class -- that scope owns it
        call = _call_at(stmt)
        if call is None:
            continue
        if not (isinstance(call.func, ast.Attribute) and call.func.attr in METHOD_PARAMS):
            continue
        method = call.func.attr
        receiver = call.func.value
        if not isinstance(receiver, ast.Name) or receiver.id not in aliases:
            if isinstance(receiver, ast.Name):
                out.append((func_node.name, f"'{receiver.id}.{method}' is not the re module"))
            continue
        if receiver.id in span_dedupe.chain_blocked(tree, call.lineno or 0):
            out.append((func_node.name,
                        f"'{receiver.id}' is rebound in a function scope — not touched"))
            continue
        if not call.args:
            continue
        pattern = call.args[0]
        if not (isinstance(pattern, ast.Constant) and type(pattern.value) is str):
            out.append((func_node.name, f"{method} with non-constant pattern — not touched"))
            continue
        if any(isinstance(a, ast.Starred) for a in call.args):
            out.append((func_node.name, f"{method} with *args — not touched"))
            continue
        split = _split_flags(method, list(call.args[1:]), list(call.keywords))
        if split is None:
            # NOTE: args[1:] shifts positional indexes, but _split_flags only
            # reads parameter NAMES for the "flags" slot -- the pattern slot
            # itself is never positional-indexed, so slicing is safe.
            out.append((func_node.name, f"{method} with unrecognized shape — not touched"))
            continue
        kept_positionals, flags_src, kept_keywords = split
        if call.lineno != call.end_lineno:
            out.append((func_node.name, f"{method} spans lines — not touched"))
            continue
        base = f"_re_{method}"
        module_name = base
        counter = 2
        scope_blocked = span_dedupe.chain_blocked(tree, call.lineno or 0)
        while module_name in taken or module_name in scope_blocked:
            module_name = f"{base}_{counter}"
            counter += 1
        taken.add(module_name)
        re_alias = aliases[receiver.id]
        pattern_src = ast.unparse(pattern)
        hoist_src = f"{module_name} = {re_alias}.compile({pattern_src}"
        if flags_src is not None:
            hoist_src += f", {flags_src}"
        hoist_src += ")"
        arg_srcs = [ast.unparse(a) for a in kept_positionals]
        arg_srcs += [f"{k.arg}={ast.unparse(k.value)}" for k in kept_keywords]
        new_call_src = f"{module_name}.{method}({', '.join(arg_srcs)})"
        call_src = ast.unparse(call)
        out.append(ReCallCandidate(
            func_name=func_node.name,
            lineno=call.lineno or 0,
            col=call.col_offset,
            end_col=call.end_col_offset or 0,
            method=method,
            module_name=module_name,
            hoist_src=hoist_src,
            new_call_src=new_call_src,
            call_src=call_src,
        ))
    return out


def analyze_source(source: str, filename: str = "<string>") -> ReCallPlan:
    """Analyze *source* text directly (no filesystem access) — testable core."""
    tree = ast.parse(source, filename=filename)
    aliases = _re_aliases(tree)
    taken = _module_names(tree)
    plan = ReCallPlan()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for item in _find_in_function(node, aliases, taken, tree):
            if isinstance(item, ReCallCandidate):
                plan.safe.append(item)
            else:
                plan.skipped.append(item)
    plan.safe.sort(key=lambda c: (c.lineno, c.col))
    return plan


def analyze_file(file_path: str) -> ReCallPlan:
    source = Path(file_path).read_text(encoding="utf-8")
    return analyze_source(source, filename=file_path)
