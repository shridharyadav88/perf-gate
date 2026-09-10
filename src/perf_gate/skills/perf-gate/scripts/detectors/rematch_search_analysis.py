"""Detect ``re.match(".*pat", s)`` tests safe to rewrite as ``re.search``.

``re.match`` anchors at position 0, but a leading greedy ``.*`` (under
``DOTALL``) can stretch to any position — so the call only tests
whether ``pat`` occurs *anywhere*, exactly what ``re.search`` asks::

    if re.match(".*error:(.*)", log, re.DOTALL):   ->   if re.search("error:(.*)", log, re.DOTALL):

Detection is pure/read-only (this module never writes a file) so it can
be shared by `classify_findings.py` (read-only triage) and
`resolvers/apply_rematch_search.py` (the actual codemod) without
duplicating logic.

Safety rules (anything ambiguous is skipped, never guessed at):

* The receiver is the plain name ``re`` (``import re`` present, ``re``
  rebound nowhere — module level or function scope). Aliased imports
  are skipped: the rewrite must spell the same receiver.
* The pattern is a constant ``str`` starting with exactly one greedy
  ``.*`` (``.*?`` also accepted), with a non-empty remainder. ``.+``
  is NOT accepted (it requires a character before the match, so
  position-0 occurrences would be lost).
* ``DOTALL`` is proven: a ``flags`` argument (positional or keyword)
  containing ``re.DOTALL`` / ``re.S`` (bare, or inside a ``|``
  combination), an integer constant with the DOTALL bit set, or a
  leading inline ``(?s)`` flag. Without it, ``.*`` cannot cross
  newlines and the two calls test different things.
* Only ``pattern`` / ``string`` / ``flags`` arguments are present
  (``count`` / ``pos`` / ``endpos`` change the question — skipped).
* The call sits in a *boolean context*: an ``if`` / ``while`` /
  ``assert`` test, a ``not`` operand, an ``if``-expression test, a
  proven-``bool(...)`` argument, or an ``and`` / ``or`` operand that
  is itself in a boolean context. The match object's span/groups are
  then never observed (``.*``-anchored spans start at 0; ``search``
  spans do not), so only the identical truth value matters. Plain
  assignments, returns of the object itself, and bare expression
  statements keep the object — skipped.
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

DOTALL_BIT = 16  # re.DOTALL == re.S == 16


@dataclass
class RematchSearchCandidate:
    """One ``re.match(".*..")`` safe to rewrite as ``re.search``."""

    func_name: str
    kind: str  # always "rematch_search"
    lineno: int
    end_lineno: int
    edits: list = field(default_factory=list)
    # each edit: (start_line, start_col, end_line, end_col, text)
    detail: str = ""


@dataclass
class RematchSearchPlan:
    safe: list[RematchSearchCandidate] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (func, reason)


def _module_re_ok(tree: ast.Module) -> bool:
    """True for a plain ``import re`` with no rebinding of ``re``.

    Only the unaliased import is supported: the rewrite must spell the
    same ``re.search`` receiver, and anything else is skipped.
    """
    imported = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.name == "re":
            return False
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "re" for t in node.targets):
            return False
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "re":
            return False
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "re" and a.asname is None:
                    imported = True
                elif a.asname == "re" or (a.name == "re" and a.asname):
                    return False
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if (a.asname or a.name) == "re":
                    return False
    return imported


def _module_bool_ok(tree: ast.Module) -> bool:
    """True when the module never rebinds the name ``bool``."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.name == "bool":
            return False
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "bool" for t in node.targets):
            return False
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "bool":
            return False
        if isinstance(node, ast.Import):
            for a in node.names:
                if (a.asname or a.name.split(".")[0]) == "bool":
                    return False
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if (a.asname or a.name) == "bool":
                    return False
    return True


def _locally_bound(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names bound in the function scope itself (params count).

    Nested scopes contribute only their defined names: a ``def re`` two
    levels down does not shadow this scope's read, so its body is never
    descended into.
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


def _dotall_present(flags: ast.expr | None) -> bool:
    """True if a flags expression provably includes re.DOTALL."""
    if flags is None:
        return False
    if isinstance(flags, ast.Constant) and type(flags.value) is int:
        return bool(flags.value & DOTALL_BIT)
    if isinstance(flags, ast.Attribute) and flags.attr in ("DOTALL", "S"):
        return isinstance(flags.value, ast.Name) and flags.value.id == "re"
    if isinstance(flags, ast.BinOp) and isinstance(flags.op, ast.BitOr):
        return _dotall_present(flags.left) or _dotall_present(flags.right)
    return False


def _split_pattern(pattern: str) -> str | None:
    """Remainder after a leading ``(?s)`` + exactly one ``.*`` / ``.*?``.

    Returns None unless the remainder is non-empty. ``.+`` is rejected
    (it requires a character before the match); ``.**`` is rejected
    (possessive-ish quantifier stacking with different backtracking).
    """
    rest = pattern
    if rest.startswith("(?s)"):
        rest = rest[len("(?s)"):]
    if rest.startswith(".*?"):
        rest = rest[len(".*?"):]
    elif rest.startswith(".*") and rest[2:3] != "*":
        rest = rest[2:]
    else:
        return None
    return rest or None


def _bool_context(node: ast.expr, parents: dict[int, ast.AST],
                  bool_ok: bool) -> bool:
    """True if *node*'s value is only ever used for its truthiness.

    Tests (``if``/``while``/``assert``/``if``-expression), ``not``, and
    a proven-``bool(...)`` call all discard the object at that point —
    downstream only ever sees a genuine boolean, so they terminate the
    walk. ``and``/``or`` operands may *become* the result (``m or
    default`` keeps ``m``), so the walk recurses through them.
    """
    current: ast.AST = node
    while True:
        parent = parents.get(id(current))
        if parent is None:
            return False
        if isinstance(parent, (ast.If, ast.While, ast.Assert)) \
                and getattr(parent, "test", None) is current:
            return True
        if isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not):
            return True
        if isinstance(parent, ast.IfExp) and parent.test is current:
            return True
        if bool_ok and isinstance(parent, ast.Call) \
                and isinstance(parent.func, ast.Name) \
                and parent.func.id == "bool" and len(parent.args) == 1 \
                and not parent.keywords:
            return True
        if isinstance(parent, ast.BoolOp):
            current = parent
            continue
        return False


def _call_parts(node: ast.Call) -> tuple[ast.expr, ast.expr, ast.expr | None] | None:
    """(pattern, string, flags) for a plain 2-3 arg re.match call."""
    args = list(node.args)
    keywords = {k.arg: k.value for k in node.keywords if k.arg}
    if any(k.arg is None for k in node.keywords):
        return None
    if set(keywords) - {"pattern", "string", "flags"}:
        return None
    if len(args) > 3:
        return None
    pattern = keywords.get("pattern")
    string = keywords.get("string")
    flags = keywords.get("flags")
    positional = ["pattern", "string", "flags"]
    for value, name in zip(args, positional):
        if name in keywords:
            return None  # duplicate — invalid call, leave alone
        if name == "pattern":
            pattern = value
        elif name == "string":
            string = value
        else:
            flags = value
    if pattern is None or string is None:
        return None
    return pattern, string, flags


def _span_has_hash(source_lines: list[str], node: ast.expr) -> bool:
    end = node.end_lineno or node.lineno
    return any("#" in source_lines[n - 1] for n in range(node.lineno, end + 1))


def _find_in_function(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[str],
    re_ok: bool,
    bool_ok: bool,
) -> list[RematchSearchCandidate | tuple[str, str]]:
    out: list[RematchSearchCandidate | tuple[str, str]] = []
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(func_node):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    local_bound = _locally_bound(func_node)
    bool_ok = bool_ok and "bool" not in local_bound
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "match"
                and isinstance(func.value, ast.Name)
                and func.value.id == "re"):
            continue
        if not re_ok or "re" in local_bound:
            out.append((func_node.name, "name 're' is rebound — not touched"))
            continue
        parts = _call_parts(node)
        if parts is None:
            out.append((func_node.name,
                        "re.match takes args beyond pattern/string/flags — not touched"))
            continue
        pattern_node, string_node, flags_node = parts
        if not (isinstance(pattern_node, ast.Constant)
                and type(pattern_node.value) is str):
            continue  # dynamic pattern — silently not this tier
        rest = _split_pattern(pattern_node.value)
        if rest is None:
            continue  # no leading .* — silently not this tier
        inline_dotall = pattern_node.value.startswith("(?s)")
        if not (inline_dotall or _dotall_present(flags_node)):
            out.append((func_node.name,
                        "no DOTALL: .* cannot cross newlines — not touched"))
            continue
        if not _bool_context(node, parents, bool_ok):
            out.append((func_node.name,
                        "match object escapes boolean context — not touched"))
            continue
        if _span_has_hash(source_lines, node):
            out.append((func_node.name, "comment inside the replaced span — not touched"))
            continue
        flags_src = f", {ast.unparse(flags_node)}" if flags_node is not None else ""
        replacement = f"re.search({rest!r}, {ast.unparse(string_node)}{flags_src})"
        end = node.end_lineno or node.lineno
        out.append(RematchSearchCandidate(
            func_name=func_node.name,
            kind="rematch_search",
            lineno=node.lineno,
            end_lineno=end,
            edits=[(node.lineno, node.col_offset, end,
                    node.end_col_offset or node.col_offset, replacement)],
            detail=(f"search for {rest!r} instead of matching '.*' first "
                    f"(line {node.lineno})"),
        ))
    return out


def analyze_source(source: str, filename: str = "<string>") -> RematchSearchPlan:
    """Analyze *source* text directly (no filesystem access) — testable core."""
    source_lines = source.splitlines()
    tree = ast.parse(source, filename=filename)
    plan = RematchSearchPlan()
    re_ok = _module_re_ok(tree)
    bool_ok = _module_bool_ok(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for item in _find_in_function(node, source_lines, re_ok, bool_ok):
            if isinstance(item, RematchSearchCandidate):
                plan.safe.append(item)
            else:
                plan.skipped.append(item)
    plan.safe = span_dedupe.dedupe_nested_spans(tree, plan.safe)
    plan.safe, shadow_skipped = span_dedupe.filter_shadowed(
        tree, plan.safe, {"rematch_search": {"re"}})
    plan.skipped.extend(shadow_skipped)
    plan.safe.sort(key=lambda c: (c.lineno, c.kind))
    return plan


def analyze_file(file_path: str) -> RematchSearchPlan:
    source = Path(file_path).read_text(encoding="utf-8")
    return analyze_source(source, filename=file_path)
