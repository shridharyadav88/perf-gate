"""Deterministically triage a baseline CSV into fix tiers — no LLM involved.

Consumes the CSV produced by `generate_baseline_csv.py` and adds `tier` /
`tier_detail` columns so the orchestrating skill knows, per finding, whether
it can be fixed automatically or which kind of model to spend tokens on:

  tier0_regex_hoist   -- a function-local re.compile() is safe to hoist to
                         module scope (see regex_hoist_analysis.py). Detected
                         by static analysis alone, so this applies even to
                         rows whose profiling failed (big_o_error/
                         line_profile_error set) — the fix needs no execution
                         evidence, just the source.
  tier2_algorithmic   -- empirical complexity_rank >= 4 (Quadratic or worse).
                         Needs an actual algorithm redesign; route to a
                         capable model, not a small one.
  tier1_review        -- profiling succeeded, no complexity problem, no known
                         mechanical pattern matched. A small model can look at
                         the hotspot line(s) and propose a narrow fix.
  not_actionable      -- neither profiling nor static analysis produced any
                         evidence to act on (e.g. couldn't even be parsed).

One file is analyzed once and its tier0 plan is shared across every row for
that file, so a file with two functions sharing a hoistable pattern (as seen
in crawl4ai's clean_pdf_text / clean_pdf_text_to_html) doesn't get evaluated
twice or produce inconsistent answers per-row.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import regex_hoist_analysis  # noqa: E402

OUTPUT_FIELDNAMES_SUFFIX = ["tier", "tier_detail"]


def _tier0_functions(file_path: str) -> dict[str, str]:
    """Return {function_name: detail} for every function tier0 can fix in *file_path*.

    Returns an empty dict (not an error) if the file can't be parsed —
    callers fall through to the other tier checks.
    """
    try:
        plan = regex_hoist_analysis.analyze_file(file_path)
    except (OSError, SyntaxError):
        return {}
    result: dict[str, str] = {}
    for cand in plan.safe:
        detail = f"hoist '{cand.var_name}' (re.compile) to module scope"
        for func_name in cand.functions:
            result[func_name] = (
                result[func_name] + "; " + detail if func_name in result else detail
            )
    return result


def classify_rows(rows: list[dict]) -> list[dict]:
    """Add 'tier' and 'tier_detail' to each row, mutating and returning the list."""
    tier0_cache: dict[str, dict[str, str]] = {}

    for row in rows:
        file_path = row["file"]
        func_name = row["function"]

        if file_path not in tier0_cache:
            tier0_cache[file_path] = _tier0_functions(file_path)
        tier0_hit = tier0_cache[file_path].get(func_name)

        if tier0_hit:
            row["tier"] = "tier0_regex_hoist"
            row["tier_detail"] = tier0_hit
            continue

        complexity_rank_raw = row.get("complexity_rank", "")
        try:
            complexity_rank = int(complexity_rank_raw)
        except (TypeError, ValueError):
            complexity_rank = None

        if complexity_rank is not None and complexity_rank >= 4:
            row["tier"] = "tier2_algorithmic"
            row["tier_detail"] = (
                f"empirical complexity '{row.get('empirical_big_o', '')}' "
                f"(rank {complexity_rank}) — needs an algorithm change, not a mechanical fix"
            )
            continue

        big_o_error = row.get("big_o_error", "")
        line_profile_error = row.get("line_profile_error", "")
        if big_o_error and line_profile_error:
            row["tier"] = "not_actionable"
            row["tier_detail"] = "no profiling evidence and no static pattern matched"
            continue

        row["tier"] = "tier1_review"
        row["tier_detail"] = "no complexity problem or known mechanical pattern — needs a look"

    return rows


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Classify a generate_baseline_csv.py report into deterministic fix tiers.",
    )
    parser.add_argument(
        "--input", required=True, help="Path to a CSV from generate_baseline_csv.py.",
    )
    parser.add_argument("--output", default=None, help="Write CSV to this path instead of stdout.")
    args = parser.parse_args(argv)

    with open(args.input, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    classify_rows(rows)
    out_fieldnames = fieldnames + [f for f in OUTPUT_FIELDNAMES_SUFFIX if f not in fieldnames]

    out = open(args.output, "w", newline="", encoding="utf-8") if args.output else sys.stdout
    try:
        writer = csv.DictWriter(out, fieldnames=out_fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if args.output:
            out.close()


if __name__ == "__main__":
    main()
