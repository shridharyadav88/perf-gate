"""Detect eager logging strings safe to make lazy.

``log.debug(f"loaded {n} rows from {path}")`` formats the message on
every call — even when debug logging is disabled and the string is
thrown away. Passing the values as arguments defers formatting until
(and unless) the record is actually emitted::

    log.debug(f"loaded {n} rows")          ->   log.debug("loaded %s rows", n)
    log.info("loaded {} rows".format(n))   ->   log.info("loaded %s rows", n)

Detection is pure/read-only (this module never writes a file) so it can
be shared by `classify_findings.py` (read-only triage) and
`resolvers/apply_logging_lazy.py` (the actual codemod) without
duplicating logic.

Safety rules (anything ambiguous is skipped, never guessed at):

* The receiver call is a plain ``<logger>.<debug|info|warning|warn|
  error|exception|critical>(...)`` attribute call with no other
  positional or keyword arguments (an existing ``*args``/``exc_info=``
  would collide with the new lazy arguments).
* f-string fields convert exactly: no conversion or ``!s`` becomes
  ``%s``; ``!r`` becomes ``%r``. Any ``!a`` conversion or any
  format spec (``{x:>10}``, ``{x:.2f}``, ...) skips the candidate —
  ``%``-formatting has no exact equivalent for most specs, and a
  changed rendering is a behavior change, not an optimization.
* Literal text is copied verbatim with ``%`` escaped to ``%%``. The
  AST already decoded ``{{``/``}}`` to literal braces, so nothing
  further is needed there.
* ``"...".format(...)`` only fires for purely automatic ``{}`` fields
  (no ``{0}``/``{name}``, no nested replacement fields, no ``!`` /
  ``:`` inside): those map one-to-one onto ``%s`` positions in order.
  Anything else is skipped.
* ``%``-style calls are already lazy — never touched.
* Only code inside functions is analyzed (module-level statements are
  left alone, matching the other detectors).
* A ``#`` character anywhere in the replaced span skips the candidate.
"""

from __future__ import annotations

import ast
import os
import string
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from detectors import span_dedupe  # noqa: E402

LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical"}
)


@dataclass
class LoggingLazyCandidate:
    """One eager logging call safe to make lazy."""

    func_name: str
    kind: str  # always "logging_lazy"
    lineno: int
    end_lineno: int
    edits: list = field(default_factory=list)
    # each edit: (start_line, start_col, end_line, end_col, text)
    detail: str = ""


@dataclass
class LoggingLazyPlan:
    safe: list[LoggingLazyCandidate] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (func, reason)


def _joined_parts(node: ast.JoinedStr) -> tuple[str, list[ast.expr]] | None:
    """(printf_template, arg_exprs) for an exactly-translatable f-string."""
    chunks: list[str] = []
    args: list[ast.expr] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            chunks.append(value.value.replace("%", "%%"))
        elif isinstance(value, ast.FormattedValue):
            if value.format_spec is not None:
                return None
            if value.conversion == -1 or value.conversion == ord("s"):
                chunks.append("%s")
            elif value.conversion == ord("r"):
                chunks.append("%r")
            else:
                return None
            args.append(value.value)
        else:
            return None
    return "".join(chunks), args


def _format_parts(node: ast.Call) -> tuple[str, list[ast.expr]] | None:
    """(printf_template, arg_exprs) for a plain ``"{}".format(...)`` call."""
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "format"
            and isinstance(func.value, ast.Constant)
            and isinstance(func.value.value, str)):
        return None
    if node.keywords or any(isinstance(a, ast.Starred) for a in node.args):
        return None
    template = func.value.value
    try:
        fields = list(string.Formatter().parse(template))
    except ValueError:
        return None
    # Strictly automatic `{}` fields, one per positional arg, in order.
    parts: list[str] = []
    consumed = 0
    for literal, field_name, format_spec, conversion in fields:
        parts.append(literal.replace("%", "%%"))
        if field_name is None:
            continue
        if field_name != "" or format_spec or conversion:
            return None
        consumed += 1
        parts.append("%s")
    if consumed != len(node.args):
        return None
    return "".join(parts), list(node.args)


def _match(node: ast.Call) -> tuple[str, list[ast.expr]] | tuple[None, str] | None:
    """Match one eager logging call; return (template, lazy_args).

    Returns ``(None, reason)`` for a recognized-but-unsafe shape and
    ``None`` for a non-match.
    """
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr in LOG_METHODS):
        return None
    if len(node.args) != 1 or node.keywords:
        return None
    (message,) = node.args
    if isinstance(message, ast.JoinedStr):
        if not any(isinstance(v, ast.FormattedValue) for v in message.values):
            return None  # plain string — already lazy
        parts = _joined_parts(message)
        if parts is None:
            return None, "f-string uses conversions/specs with no % equivalent"
        return parts
    if isinstance(message, ast.Call):
        parts = _format_parts(message)
        if parts is None:
            # Distinguish "not a .format call" (silent) from "a .format
            # call we cannot translate" (skipped with reason).
            inner = message.func
            if (isinstance(inner, ast.Attribute) and inner.attr == "format"
                    and isinstance(inner.value, ast.Constant)
                    and isinstance(inner.value.value, str)):
                return None, ".format() uses indexed/named/spec fields — not touched"
            return None
        return parts
    return None


def _span_has_hash(source_lines: list[str], node: ast.expr) -> bool:
    end = node.end_lineno or node.lineno
    return any("#" in source_lines[n - 1] for n in range(node.lineno, end + 1))


def _find_in_function(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[str],
) -> list[LoggingLazyCandidate | tuple[str, str]]:
    out: list[LoggingLazyCandidate | tuple[str, str]] = []
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        matched = _match(node)
        if matched is None:
            continue
        template, lazy_args = matched
        if template is None:
            out.append((func_node.name, lazy_args))
            continue
        assert isinstance(node.func, ast.Attribute)
        receiver = ast.unparse(node.func.value)
        method = node.func.attr
        arg_src = ", ".join([repr(template)] + [ast.unparse(a) for a in lazy_args])
        replacement = f"{receiver}.{method}({arg_src})"
        if _span_has_hash(source_lines, node):
            out.append((func_node.name, "comment inside the replaced span — not touched"))
            continue
        end = node.end_lineno or node.lineno
        out.append(LoggingLazyCandidate(
            func_name=func_node.name,
            kind="logging_lazy",
            lineno=node.lineno,
            end_lineno=end,
            edits=[(node.lineno, node.col_offset, end,
                    node.end_col_offset or node.col_offset, replacement)],
            detail=(f"pass logging args lazily instead of formatting eagerly "
                    f"(line {node.lineno})"),
        ))
    return out


def analyze_source(source: str, filename: str = "<string>") -> LoggingLazyPlan:
    """Analyze *source* text directly (no filesystem access) — testable core."""
    source_lines = source.splitlines()
    tree = ast.parse(source, filename=filename)
    plan = LoggingLazyPlan()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for item in _find_in_function(node, source_lines):
            if isinstance(item, LoggingLazyCandidate):
                plan.safe.append(item)
            else:
                plan.skipped.append(item)
    plan.safe = span_dedupe.dedupe_nested_spans(tree, plan.safe)
    plan.safe.sort(key=lambda c: (c.lineno, c.kind))
    return plan


def analyze_file(file_path: str) -> LoggingLazyPlan:
    source = Path(file_path).read_text(encoding="utf-8")
    return analyze_source(source, filename=file_path)
