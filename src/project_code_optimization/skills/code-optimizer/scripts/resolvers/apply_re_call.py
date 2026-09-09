"""Apply the compiled-call rewrites detected by detectors/re_call_analysis.py.

Deterministic, no LLM: replaces every safe module-level `re.<method>(...)`
call site with a call on a hoisted `re.compile(...)` and inserts the hoist
statements after the module docstring/imports. Nothing in
`ReCallPlan.skipped` is ever touched.

Usage:
    python3 resolvers/apply_re_call.py --file <path> [--dry-run]

Safety (P3): the plan is always re-derived from the exact bytes being
rewritten -- a caller-supplied plan whose candidates no longer verify
against the current source raises :exc:`PlanMismatchError` and nothing is
written. Never apply a plan computed from different bytes.
"""

from __future__ import annotations

import argparse
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from detectors import re_call_analysis as analysis  # noqa: E402


class PlanMismatchError(ValueError):
    """A caller-supplied plan no longer verifies against the target bytes.

    Raised when the file changed between planning (e.g. classification) and
    application: applying would edit locations whose safety proof no longer
    holds. Fail closed -- re-plan from the current bytes instead.
    """


def verify_plan(source: str, plan: analysis.ReCallPlan) -> list[str]:
    """Re-derive the safety proof from *source*; return mismatch descriptions.

    Empty list means every candidate in ``plan.safe`` is still provably safe
    at exactly the planned location with the identical replacement. Anything
    else must abort, not apply.
    """
    fresh = analysis.analyze_source(source)
    fresh_keys = {
        (c.func_name, c.lineno, c.col, c.method, c.module_name,
         c.hoist_src, c.new_call_src, c.call_src)
        for c in fresh.safe
    }
    errors = []
    for cand in plan.safe:
        key = (
            cand.func_name, cand.lineno, cand.col, cand.method, cand.module_name,
            cand.hoist_src, cand.new_call_src, cand.call_src,
        )
        if key not in fresh_keys:
            errors.append(
                f"'{cand.method}' call in {cand.func_name} "
                f"(planned line {cand.lineno}, col {cand.col}): "
                "no longer provably safe at the planned location -- "
                "re-plan from the current file contents"
            )
    return errors


def _splice_line(line: str, start_col: int, end_col: int, replacement: str) -> str:
    """Replace the UTF-8 byte span [start_col, end_col) in *line*.

    AST column offsets count UTF-8 bytes, not characters -- slicing the
    str directly corrupts any line with non-ASCII text before the span.
    """
    raw = line.encode("utf-8")
    return (raw[:start_col] + replacement.encode("utf-8") + raw[end_col:]).decode("utf-8")


def apply_re_calls(source: str, plan: analysis.ReCallPlan) -> str:
    """Return *source* with every candidate in `plan.safe` rewritten.

    Pure function — takes/returns text, does no I/O, so it's directly testable.
    Re-verifies *plan* against *source* first (P3); raises
    :exc:`PlanMismatchError` on any mismatch instead of editing blindly.
    """
    if not plan.safe:
        return source

    errors = verify_plan(source, plan)
    if errors:
        raise PlanMismatchError(
            "Stale plan: target changed since analysis; refusing to apply:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

    lines = source.splitlines(keepends=True)
    # Bottom-up (line, then column) so earlier spans stay valid as we edit.
    for cand in sorted(plan.safe, key=lambda c: (c.lineno, c.col), reverse=True):
        idx = cand.lineno - 1
        eol = "\n" if lines[idx].endswith("\n") else ""
        body = lines[idx][:-1] if eol else lines[idx]
        lines[idx] = _splice_line(body, cand.col, cand.end_col, cand.new_call_src) + eol

    tree = ast.parse(source)
    insertion_line = 0
    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        insertion_line = tree.body[0].end_lineno
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            insertion_line = max(insertion_line, node.end_lineno)

    hoisted_block = "\n".join(cand.hoist_src for cand in plan.safe)
    insertion_text = "\n\n" + hoisted_block + "\n"
    new_lines = lines[:insertion_line] + [insertion_text] + lines[insertion_line:]

    new_source = "".join(new_lines)
    ast.parse(new_source)  # raise loudly here rather than write broken source
    return new_source


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Hoist repeated module-level re.* calls to compiled calls.",
    )
    parser.add_argument("--file", required=True, help="Path to the Python file to fix.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the plan without writing the file.",
    )
    args = parser.parse_args(argv)

    # Single read: the plan is derived from the exact bytes below, so there
    # is no plan-vs-write race inside this invocation.
    with open(args.file, encoding="utf-8") as f:
        source = f.read()
    plan = analysis.analyze_source(source, filename=args.file)

    if not plan.safe:
        print("No safe compiled-call candidates found.")
        for func_name, reason in plan.skipped:
            print(f"  skipped in '{func_name}': {reason}")
        return

    print(f"Found {len(plan.safe)} safe candidate(s):")
    for cand in plan.safe:
        print(f"  {cand.func_name} line {cand.lineno}: "
              f"{cand.call_src} -> {cand.new_call_src}")
    for func_name, reason in plan.skipped:
        print(f"  skipped in '{func_name}': {reason}")

    try:
        new_source = apply_re_calls(source, plan)
    except PlanMismatchError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("\n--dry-run: no file written. Hoist block would be:\n")
        print("\n".join(cand.hoist_src for cand in plan.safe))
        return

    with open(args.file, "w", encoding="utf-8") as f:
        f.write(new_source)
    print(f"\nApplied. Rewrote {args.file}.")


if __name__ == "__main__":
    main()
