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
  tier0_perf402       -- a manual list-copy loop is safe to collapse into
                         `out = list(items)` (see
                         perf_comprehension_analysis.py). Same static-only
                         guarantee as tier0_regex_hoist.
  tier0_perf401       -- a manual list-build loop is safe to collapse into a
                         list comprehension (see
                         perf_comprehension_analysis.py). Same static-only
                         guarantee as tier0_regex_hoist.
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
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import confirmation as confirmation_mod  # noqa: E402
import provenance as provenance_mod  # noqa: E402
from detectors import (
    accumulator_analysis,  # noqa: E402
    invariant_hoist_analysis,  # noqa: E402
    perf_comprehension_analysis,  # noqa: E402
    re_call_analysis,  # noqa: E402
    regex_hoist_analysis,  # noqa: E402
    static_audit,  # noqa: E402
)

OUTPUT_FIELDNAMES_SUFFIX = ["tier", "tier_detail"]

# Cheapest-and-most-certain first: a mechanical fix beats a model rewrite
# even for a row that also shows high complexity.
_TIER0_PRIORITY = (
    "tier0_regex_hoist", "tier0_re_call", "tier0_perf402", "tier0_perf401",
    "tier0_perf403", "tier0_str_join", "tier0_sum_reduce", "tier0_set_build",
    "tier0_invariant_hoist",
)


def _tier0_functions(file_path: str) -> dict[str, tuple[str, str]]:
    """Return {function_name: (tier, detail)} for every function tier0 can fix.

    Returns an empty dict (not an error) if the file can't be parsed —
    callers fall through to the other tier checks.
    """
    result: dict[str, tuple[str, str]] = {}
    try:
        hoist_plan = regex_hoist_analysis.analyze_file(file_path)
    except (OSError, SyntaxError):
        return {}
    for cand in hoist_plan.safe:
        detail = f"hoist '{cand.var_name}' (re.compile) to module scope"
        for func_name in cand.functions:
            _merge_tier0(result, func_name, "tier0_regex_hoist", detail)
    try:
        perf_plan = perf_comprehension_analysis.analyze_file(file_path)
    except (OSError, SyntaxError):
        return result
    for cand in perf_plan.safe:
        if cand.kind == "perf402":
            detail = f"collapse '{cand.var_name}' copy-loop into list(...)"
        elif cand.kind == "perf403":
            rhs = cand.replacement.split("=", 1)[1].strip()
            what = "dict(...)" if rhs.startswith("dict(") else "a dict comprehension"
            detail = f"collapse '{cand.var_name}' dict-loop into {what}"
        else:
            detail = f"collapse '{cand.var_name}' build-loop into a comprehension"
        _merge_tier0(result, cand.func_name, f"tier0_{cand.kind}", detail)
    try:
        acc_plan = accumulator_analysis.analyze_file(file_path)
    except (OSError, SyntaxError):
        return result
    for cand in acc_plan.safe:
        if cand.kind == "str_join":
            detail = f"collapse '{cand.var_name}' string-accumulation loop into ''.join"
        elif cand.kind == "set_build":
            detail = f"collapse '{cand.var_name}' set-accumulation loop into set(...)"
        else:
            detail = f"collapse '{cand.var_name}' summation loop into sum(...)"
        _merge_tier0(result, cand.func_name, f"tier0_{cand.kind}", detail)
    try:
        recall_plan = re_call_analysis.analyze_file(file_path)
    except (OSError, SyntaxError):
        return result
    for cand in recall_plan.safe:
        detail = (f"hoist {cand.method} call at line {cand.lineno} "
                  f"to compiled '{cand.module_name}'")
        _merge_tier0(result, cand.func_name, "tier0_re_call", detail)
    try:
        inv_plan = invariant_hoist_analysis.analyze_file(file_path)
    except (OSError, SyntaxError):
        return result
    for cand in inv_plan.safe:
        detail = (f"hoist loop-invariant '{cand.name}' to '{cand.alias}' "
                  f"({len(cand.loads)} load(s))")
        _merge_tier0(result, cand.func_name, "tier0_invariant_hoist", detail)
    return result


def _merge_tier0(
    result: dict[str, tuple[str, str]], func_name: str, tier: str, detail: str
) -> None:
    """Merge one tier0 hit, keeping the highest-priority tier as the label."""
    if func_name in result:
        old_tier, old_detail = result[func_name]
        tiers = {old_tier: old_detail, tier: detail}
        best = min(tiers, key=_TIER0_PRIORITY.index)
        combined = "; ".join(tiers[t] for t in _TIER0_PRIORITY if t in tiers)
        result[func_name] = (best, combined)
    else:
        result[func_name] = (tier, detail)


def _static_hints(file_path: str) -> dict[str, list[str]]:
    """Return {function_name: [short hint strings]} for one file's static audit.

    Returns an empty dict (not an error) if the file can't be read or parsed --
    callers fall through to the other tier checks. Static hints are advisory:
    they enrich tier1 rows but never escalate a tier on their own (no
    execution evidence; confirmation rules arrive with the statistics pass).
    """
    try:
        plan = static_audit.analyze_file(file_path)
    except (OSError, SyntaxError, UnicodeDecodeError):
        return {}
    result: dict[str, list[str]] = {}
    for hint in plan.hints:
        if hint.kind == "nested_loop":
            short = f"nested loop at line {hint.lineno} (O(N*M) risk)"
        else:
            short = f"cache {hint.symbol} to a local (line {hint.lineno})"
        result.setdefault(hint.func_name, []).append(short)
    return result


def _confirmations(row: dict) -> int:
    """Independent agreeing measurements behind this row's rank (default 1).

    Read from the optional ``confirmations`` column so re-measurement
    protocols can record agreement without any schema change.
    """
    try:
        return max(int(row.get("confirmations") or 1), 1)
    except (TypeError, ValueError):
        return 1


def classify_rows(rows: list[dict]) -> list[dict]:
    """Add 'tier' and 'tier_detail' to each row, mutating and returning the list."""
    tier0_cache: dict[str, dict[str, tuple[str, str]]] = {}
    static_cache: dict[str, dict[str, list[str]]] = {}

    for row in rows:
        file_path = row["file"]
        func_name = row["function"]

        if file_path not in tier0_cache:
            tier0_cache[file_path] = _tier0_functions(file_path)
        if file_path not in static_cache:
            static_cache[file_path] = _static_hints(file_path)
        tier0_hit = tier0_cache[file_path].get(func_name)

        if tier0_hit:
            row["tier"], row["tier_detail"] = tier0_hit
            continue

        complexity_rank_raw = row.get("complexity_rank", "")
        try:
            complexity_rank = int(complexity_rank_raw)
        except (TypeError, ValueError):
            complexity_rank = None

        if complexity_rank is not None and complexity_rank >= 4:
            confirmations = _confirmations(row)
            if confirmation_mod.tier2_confirmed(complexity_rank, confirmations):
                row["tier"] = "tier2_algorithmic"
                row["tier_detail"] = (
                    f"empirical complexity '{row.get('empirical_big_o', '')}' "
                    f"(rank {complexity_rank}) — needs an algorithm change, not a mechanical fix"
                )
                continue
            # Rank 4 on a single reading is as likely to be a fit flap as a
            # finding (P7): review lane with a re-measurement instruction,
            # never the algorithmic lane. Static hints still apply below.
            row["tier"] = "tier1_review"
            row["tier_detail"] = (
                f"empirical complexity '{row.get('empirical_big_o', '')}' "
                f"(rank {complexity_rank}, {confirmations} measurement) — "
                "unconfirmed: re-measure (repeat runs and/or wider --max-n) "
                "and escalate only if independent readings agree"
            )
            static_hints = static_cache[file_path].get(func_name, [])
            if static_hints:
                row["tier_detail"] += "; static audit: " + "; ".join(static_hints)
            continue

        big_o_error = row.get("big_o_error", "")
        line_profile_error = row.get("line_profile_error", "")
        if big_o_error and line_profile_error:
            row["tier"] = "not_actionable"
            row["tier_detail"] = "no profiling evidence and no static pattern matched"
            continue

        row["tier"] = "tier1_review"
        row["tier_detail"] = "no complexity problem or known mechanical pattern — needs a look"
        static_hints = static_cache[file_path].get(func_name, [])
        if static_hints:
            row["tier_detail"] += "; static audit: " + "; ".join(static_hints)

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
        raw = f.read()
    proven, body = provenance_mod.split_preamble(raw)
    try:
        provenance_mod.assert_schema(proven, source=args.input)
    except provenance_mod.ProvenanceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
    reader = csv.DictReader(io.StringIO(body))
    rows = list(reader)
    fieldnames = list(reader.fieldnames or [])

    classify_rows(rows)
    out_fieldnames = fieldnames + [f for f in OUTPUT_FIELDNAMES_SUFFIX if f not in fieldnames]

    out = open(args.output, "w", newline="", encoding="utf-8") if args.output else sys.stdout
    try:
        # Carry the input's measurement provenance through: the classified
        # CSV describes the same measurement, not a new one.
        for line in provenance_mod.format_preamble(proven):
            out.write(line + "\n")
        writer = csv.DictWriter(out, fieldnames=out_fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if args.output:
            out.close()


if __name__ == "__main__":
    main()
