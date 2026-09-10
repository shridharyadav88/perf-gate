"""Apply the accumulator collapses detected by detectors/accumulator_analysis.py.

Deterministic, no LLM: replaces every safe init+loop pair
(`s = ""` + `for ...: s += ...` with `"".join`, `total = 0` +
`for ...: total += ...` with `sum`, `seen = set()` +
`for ...: seen.add(...)` with `set(...)`). Nothing in
`AccumulatePlan.skipped` is ever touched.

Usage:
    python3 resolvers/apply_accumulator.py --file <path> [--dry-run]

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
from detectors import accumulator_analysis as analysis  # noqa: E402


class PlanMismatchError(ValueError):
    """A caller-supplied plan no longer verifies against the target bytes.

    Raised when the file changed between planning (e.g. classification) and
    application: applying would edit locations whose safety proof no longer
    holds. Fail closed -- re-plan from the current bytes instead.
    """


def verify_plan(source: str, plan: analysis.AccumulatePlan) -> list[str]:
    """Re-derive the safety proof from *source*; return mismatch descriptions.

    Empty list means every candidate in ``plan.safe`` is still provably safe
    at exactly the planned location with the identical replacement. Anything
    else must abort, not apply.
    """
    fresh = analysis.analyze_source(source)
    fresh_keys = {
        (c.func_name, c.var_name, c.kind, c.lineno, c.end_lineno, c.replacement)
        for c in fresh.safe
    }
    errors = []
    for cand in plan.safe:
        key = (
            cand.func_name, cand.var_name, cand.kind,
            cand.lineno, cand.end_lineno, cand.replacement,
        )
        if key not in fresh_keys:
            errors.append(
                f"'{cand.var_name}' in {cand.func_name} "
                f"(planned lines {cand.lineno}-{cand.end_lineno}): "
                "no longer provably safe at the planned location -- "
                "re-plan from the current file contents"
            )
    return errors


def apply_accumulates(source: str, plan: analysis.AccumulatePlan) -> str:
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
    # Ranges are disjoint and each candidate's init is adjacent to its loop,
    # but apply bottom-up anyway so line numbers stay valid as we edit.
    new_lines = list(lines)
    for cand in sorted(plan.safe, key=lambda c: c.lineno, reverse=True):
        start = cand.lineno - 1  # 0-indexed, inclusive
        end = cand.end_lineno  # exclusive in 0-indexed slicing
        new_lines[start:end] = [cand.indent + cand.replacement]

    new_source = "".join(new_lines)
    ast.parse(new_source)  # raise loudly here rather than write broken source
    return new_source


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Collapse safe accumulator loops (str-join, sum-reduce).",
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
        print("No safe accumulator candidates found.")
        for var_name, reason in plan.skipped:
            print(f"  skipped '{var_name}': {reason}")
        return

    print(f"Found {len(plan.safe)} safe candidate(s):")
    for cand in plan.safe:
        print(f"  '{cand.var_name}' in {cand.func_name} ({cand.kind})")
    for var_name, reason in plan.skipped:
        print(f"  skipped '{var_name}': {reason}")

    try:
        new_source = apply_accumulates(source, plan)
    except PlanMismatchError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("\n--dry-run: no file written. Replacements would be:\n")
        for cand in plan.safe:
            print(f"  lines {cand.lineno}-{cand.end_lineno}: {cand.replacement.strip()}")
        return

    with open(args.file, "w", encoding="utf-8") as f:
        f.write(new_source)
    print(f"\nApplied. Rewrote {args.file}.")


if __name__ == "__main__":
    main()
