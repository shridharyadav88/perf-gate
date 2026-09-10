"""Apply the regex-hoist fix detected by detectors/regex_hoist_analysis.py.

Deterministic, no LLM: deletes every occurrence of a safe-to-hoist
`name = re.compile(...)` statement from inside function bodies and inserts
one deduplicated copy at module scope (after the docstring/imports). Nothing
in `regex_hoist_analysis.HoistPlan.skipped` is ever touched.

Usage:
    python3 resolvers/apply_regex_hoist.py --file <path> [--dry-run]

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
from detectors import regex_hoist_analysis as analysis  # noqa: E402


class PlanMismatchError(ValueError):
    """A caller-supplied plan no longer verifies against the target bytes.

    Raised when the file changed between planning (e.g. classification) and
    application: applying would edit locations whose safety proof no longer
    holds. Fail closed -- re-plan from the current bytes instead.
    """


def verify_plan(source: str, plan: analysis.HoistPlan) -> list[str]:
    """Re-derive the safety proof from *source*; return mismatch descriptions.

    Empty list means every candidate in ``plan.safe`` is still provably safe
    at exactly the planned location. Anything else must abort, not apply.
    """
    fresh = analysis.analyze_source(source)
    fresh_keys = {(c.var_name, tuple(c.occurrences)) for c in fresh.safe}
    errors = []
    for cand in plan.safe:
        if (cand.var_name, tuple(cand.occurrences)) not in fresh_keys:
            errors.append(
                f"'{cand.var_name}' (planned in: {', '.join(cand.functions)}): "
                "no longer provably safe at the planned location -- "
                "re-plan from the current file contents"
            )
    return errors


def apply_hoist(source: str, plan: analysis.HoistPlan) -> str:
    """Return *source* with every candidate in `plan.safe` hoisted to module scope.

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

    delete_line_numbers: set[int] = set()
    for cand in plan.safe:
        for _func, lineno, end_lineno in cand.occurrences:
            delete_line_numbers.update(range(lineno, end_lineno + 1))

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

    # Imports/docstring are never candidates for deletion, so this index is
    # stable: count of kept lines up to (and including) `insertion_line`.
    insert_idx = sum(1 for i in range(1, insertion_line + 1) if i not in delete_line_numbers)

    kept_lines = [line for i, line in enumerate(lines, start=1) if i not in delete_line_numbers]

    hoisted_block = "\n".join(cand.source_text for cand in plan.safe)
    insertion_text = "\n\n" + hoisted_block + "\n"

    new_lines = kept_lines[:insert_idx] + [insertion_text] + kept_lines[insert_idx:]
    new_source = "".join(new_lines)

    ast.parse(new_source)  # raise loudly here rather than write broken source
    return new_source


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Hoist safe function-local re.compile(...) calls to module scope.",
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
        print("No safe regex-hoist candidates found.")
        for var_name, reason in plan.skipped:
            print(f"  skipped '{var_name}': {reason}")
        return

    print(f"Found {len(plan.safe)} safe candidate(s):")
    for cand in plan.safe:
        funcs = ", ".join(cand.functions)
        print(f"  '{cand.var_name}' — currently local to: {funcs}")
    for var_name, reason in plan.skipped:
        print(f"  skipped '{var_name}': {reason}")

    try:
        new_source = apply_hoist(source, plan)
    except PlanMismatchError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("\n--dry-run: no file written. New module-level block would be:\n")
        print("\n\n".join(cand.source_text for cand in plan.safe))
        return

    with open(args.file, "w", encoding="utf-8") as f:
        f.write(new_source)
    print(f"\nApplied. Rewrote {args.file}.")


if __name__ == "__main__":
    main()
