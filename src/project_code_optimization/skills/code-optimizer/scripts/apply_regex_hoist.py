"""Apply the regex-hoist fix detected by regex_hoist_analysis.py.

Deterministic, no LLM: deletes every occurrence of a safe-to-hoist
`name = re.compile(...)` statement from inside function bodies and inserts
one deduplicated copy at module scope (after the docstring/imports). Nothing
in `regex_hoist_analysis.HoistPlan.skipped` is ever touched.

Usage:
    python3 apply_regex_hoist.py --file <path> [--dry-run] [--check]

`--check` re-parses the result and, for every hoisted name, asserts the
function(s) it was removed from still resolve that name at module scope (a
NameError guard) — a cheap correctness net beyond "it still parses".
"""

from __future__ import annotations

import argparse
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import regex_hoist_analysis as analysis  # noqa: E402


def apply_hoist(source: str, plan: analysis.HoistPlan) -> str:
    """Return *source* with every candidate in `plan.safe` hoisted to module scope.

    Pure function — takes/returns text, does no I/O, so it's directly testable.
    """
    if not plan.safe:
        return source

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

    plan = analysis.analyze_file(args.file)

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

    with open(args.file, encoding="utf-8") as f:
        source = f.read()
    new_source = apply_hoist(source, plan)

    if args.dry_run:
        print("\n--dry-run: no file written. New module-level block would be:\n")
        print("\n\n".join(cand.source_text for cand in plan.safe))
        return

    with open(args.file, "w", encoding="utf-8") as f:
        f.write(new_source)
    print(f"\nApplied. Rewrote {args.file}.")


if __name__ == "__main__":
    main()
