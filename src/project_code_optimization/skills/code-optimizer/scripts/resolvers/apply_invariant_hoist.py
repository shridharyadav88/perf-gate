"""Apply the loop-invariant hoists detected by detectors/invariant_hoist_analysis.py.

Deterministic, no LLM: inserts one `{alias} = {name}` line above each
hoisted loop and rewrites every recorded load span to the alias. Nothing
in `HoistPlan.skipped` is ever touched.

Usage:
    python3 resolvers/apply_invariant_hoist.py --file <path> [--dry-run]

Safety (P3): the plan is always re-derived from the exact bytes being
rewritten -- a caller-supplied plan whose candidates no longer verify
against the current source raises :exc:`PlanMismatchError` and nothing is
written. Every load span is additionally checked to still spell the
hoisted name at exactly the planned columns. Never apply a plan computed
from different bytes.
"""

from __future__ import annotations

import argparse
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from detectors import invariant_hoist_analysis as analysis  # noqa: E402


class PlanMismatchError(ValueError):
    """A caller-supplied plan no longer verifies against the target bytes.

    Raised when the file changed between planning (e.g. classification) and
    application: applying would edit locations whose safety proof no longer
    holds. Fail closed -- re-plan from the current file contents.
    """


def verify_plan(source: str, plan: analysis.HoistPlan) -> list[str]:
    """Re-derive the safety proof from *source*; return mismatch descriptions.

    Empty list means every candidate in ``plan.safe`` is still provably safe
    at exactly the planned location with identical load spans. Anything
    else must abort, not apply.
    """
    lines = source.splitlines()
    fresh = analysis.analyze_source(source)
    fresh_keys = {
        (c.func_name, c.name, c.alias, c.loop_lineno, tuple(c.loads))
        for c in fresh.safe
    }
    errors = []
    for cand in plan.safe:
        key = (cand.func_name, cand.name, cand.alias, cand.loop_lineno,
               tuple(cand.loads))
        if key not in fresh_keys:
            errors.append(
                f"'{cand.name}' in {cand.func_name} "
                f"(planned loop line {cand.loop_lineno}): "
                "no longer provably safe at the planned location -- "
                "re-plan from the current file contents"
            )
            continue
        for lineno, col, end in cand.loads:
            if lineno - 1 >= len(lines) or lines[lineno - 1][col:end] != cand.name:
                errors.append(
                    f"'{cand.name}' in {cand.func_name}: load span "
                    f"{lineno}:{col}-{end} no longer spells the name -- "
                    "re-plan from the current file contents"
                )
    return errors


def apply_hoists(source: str, plan: analysis.HoistPlan) -> str:
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
    # Phase 1: load-span swaps. These never change the line count, so every
    # recorded line number stays valid across all candidates. Lines go
    # bottom-up; spans within one line go right-to-left.
    swaps: dict[int, list[tuple[int, int, str]]] = {}
    for cand in plan.safe:
        for lineno, col, end in cand.loads:
            swaps.setdefault(lineno, []).append((col, end, cand.alias))
    for lineno in sorted(swaps, reverse=True):
        idx = lineno - 1
        text = lines[idx]
        eol = "\n" if text.endswith("\n") else ""
        body = text[:-1] if eol else text
        for col, end, alias in sorted(swaps[lineno], reverse=True):
            body = body[:col] + alias + body[end:]
        lines[idx] = body + eol
    # Phase 2: alias inserts. These shift lines, so they run only after all
    # span edits are baked in -- deepest loop first, and for one loop line
    # in reverse name order so the final order reads ascending.
    for cand in sorted(plan.safe,
                       key=lambda c: (c.loop_lineno, c.name), reverse=True):
        insert_at = cand.loop_lineno - 1
        lines[insert_at:insert_at] = [f"{cand.indent}{cand.alias} = {cand.name}\n"]

    new_source = "".join(lines)
    ast.parse(new_source)  # raise loudly here rather than write broken source
    return new_source


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Hoist safe loop-invariant loads to pre-loop locals.",
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
        print("No safe hoist candidates found.")
        for var_name, reason in plan.skipped:
            print(f"  skipped '{var_name}': {reason}")
        return

    print(f"Found {len(plan.safe)} safe candidate(s):")
    for cand in plan.safe:
        print(f"  '{cand.name}' in {cand.func_name} "
              f"(loop line {cand.loop_lineno}, {len(cand.loads)} load(s))")
    for var_name, reason in plan.skipped:
        print(f"  skipped '{var_name}': {reason}")

    try:
        new_source = apply_hoists(source, plan)
    except PlanMismatchError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("\n--dry-run: no file written. Hoists would be:\n")
        for cand in plan.safe:
            print(f"  line {cand.loop_lineno}: {cand.alias} = {cand.name} "
                  f"({len(cand.loads)} load(s) rewritten)")
        return

    with open(args.file, "w", encoding="utf-8") as f:
        f.write(new_source)
    print(f"\nApplied. Rewrote {args.file}.")


if __name__ == "__main__":
    main()
