"""Apply a mechanical tier0 fix to one function and verify it got faster.

The pipeline is a scout by default: it reports where to look. This script
closes the loop for the mechanical lane -- measure, apply the matching
resolver, re-measure, and keep the rewrite only if
``confirmation.decide_keep`` says the gain clears the margin. Anything else
restores the original bytes (verified identical) and reports REVERTED.

Exit codes: 0 = finished with the outcome printed (KEPT, REVERTED,
NO-FIX, or the --dry-run plan); 1 = error (unmeasurable, plan mismatch,
apply failure -- the file is always left untouched in these cases).
"""

from __future__ import annotations

import argparse
import difflib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import confirmation as confirmation_mod  # noqa: E402
from detectors import (  # noqa: E402
    accumulator_analysis as acc_analysis,
)
from detectors import (
    async_sleep_analysis,
    consumer_analysis,
    dict_keys_analysis,
    enumerate_analysis,
    list_cast_analysis,
    logging_lazy_analysis,
    membership_analysis,
    rematch_search_analysis,
    sorted_minmax_analysis,
    try_hoist_analysis,
)
from detectors import (
    invariant_hoist_analysis as inv_analysis,
)
from detectors import (
    perf_comprehension_analysis as perf_analysis,
)
from detectors import (
    re_call_analysis as recall_analysis,
)
from detectors import (
    regex_hoist_analysis as hoist_analysis,
)
from profilers import run_line_profile  # noqa: E402
from resolvers import (  # noqa: E402
    apply_accumulator as apply_acc,
)
from resolvers import (
    apply_async_sleep,
    apply_consumer,
    apply_dict_keys,
    apply_enumerate,
    apply_list_cast,
    apply_logging_lazy,
    apply_membership,
    apply_rematch_search,
    apply_sorted_minmax,
    apply_try_hoist,
)
from resolvers import (
    apply_invariant_hoist as apply_inv,
)
from resolvers import (
    apply_perf_comprehension as apply_perf,
)
from resolvers import (
    apply_re_call as apply_recall,
)
from resolvers import (
    apply_regex_hoist as apply_hoist,
)

TIERS = ("regex_hoist", "re_call", "perf401", "perf402", "perf403", "str_join",
         "sum_reduce", "set_build", "invariant_hoist", "consumer_list",
         "sorted_minmax", "literal_membership", "list_cast", "logging_lazy",
         "dict_keys", "async_sleep", "enumerate", "rematch_search",
         "except_hoist")

# Span-edit tiers: short tier -> (analysis module, plan class,
# apply module, apply function name). One table drives _analyze/_verify/
# _apply/_filter_plan so ten tiers don't need fifty branches.
_NEW_TIER_MODULES = {
    "consumer_list": (consumer_analysis, consumer_analysis.ConsumerPlan,
                      apply_consumer, "apply_consumers"),
    "sorted_minmax": (sorted_minmax_analysis, sorted_minmax_analysis.SortedMinmaxPlan,
                      apply_sorted_minmax, "apply_sorted_minmax"),
    "literal_membership": (membership_analysis, membership_analysis.MembershipPlan,
                           apply_membership, "apply_memberships"),
    "list_cast": (list_cast_analysis, list_cast_analysis.ListCastPlan,
                  apply_list_cast, "apply_list_cast"),
    "logging_lazy": (logging_lazy_analysis, logging_lazy_analysis.LoggingLazyPlan,
                     apply_logging_lazy, "apply_logging_lazy"),
    "dict_keys": (dict_keys_analysis, dict_keys_analysis.DictKeysPlan,
                  apply_dict_keys, "apply_dict_keys"),
    "async_sleep": (async_sleep_analysis, async_sleep_analysis.AsyncSleepPlan,
                    apply_async_sleep, "apply_async_sleep"),
    "enumerate": (enumerate_analysis, enumerate_analysis.EnumeratePlan,
                  apply_enumerate, "apply_enumerates"),
    "rematch_search": (rematch_search_analysis, rematch_search_analysis.RematchSearchPlan,
                       apply_rematch_search, "apply_rematch_search"),
    "except_hoist": (try_hoist_analysis, try_hoist_analysis.TryHoistPlan,
                     apply_try_hoist, "apply_try_hoists"),
}


ACC_TIERS = ("str_join", "sum_reduce", "set_build")


def _filter_plan(tier: str, plan, func_name: str):
    """Restrict a file-wide plan to candidates touching *func_name* only."""
    if tier in ACC_TIERS:
        kept = [c for c in plan.safe
                if c.func_name == func_name and c.kind == tier]
        return acc_analysis.AccumulatePlan(safe=kept, skipped=[])
    if tier == "re_call":
        kept = [c for c in plan.safe if c.func_name == func_name]
        return recall_analysis.ReCallPlan(safe=kept, skipped=[])
    if tier == "regex_hoist":
        kept = []
        for cand in plan.safe:
            occurrences = [o for o in cand.occurrences if o[0] == func_name]
            if not occurrences:
                continue
            kept.append(hoist_analysis.HoistCandidate(
                var_name=cand.var_name, source_text=cand.source_text,
                functions=[func_name], occurrences=occurrences,
            ))
        return hoist_analysis.HoistPlan(safe=kept, skipped=[])
    if tier == "invariant_hoist":
        kept = [c for c in plan.safe if c.func_name == func_name]
        return inv_analysis.HoistPlan(safe=kept, skipped=[])
    if tier in _NEW_TIER_MODULES:
        _, plan_cls, _, _ = _NEW_TIER_MODULES[tier]
        kept = [c for c in plan.safe if c.func_name == func_name]
        return plan_cls(safe=kept, skipped=[])
    kept = [c for c in plan.safe if c.func_name == func_name and c.kind == tier]
    return perf_analysis.RewritePlan(safe=kept, skipped=[])


def _describe(tier: str, subplan) -> str:
    if tier == "regex_hoist":
        return ", ".join(f"hoist '{c.var_name}'" for c in subplan.safe)
    if tier == "re_call":
        return ", ".join(f"hoist {c.method} call at line {c.lineno}" for c in subplan.safe)
    if tier == "invariant_hoist":
        return ", ".join(f"hoist '{c.name}' to '{c.alias}'" for c in subplan.safe)
    if tier in _NEW_TIER_MODULES:
        return "; ".join(c.detail for c in subplan.safe)
    return ", ".join(f"collapse '{c.var_name}' ({c.kind})" for c in subplan.safe)


_VERDICT_REPEATS = 5


def _measure_total_us(file_path: str, func_name: str, timeout: float | None) -> float:
    """Verdict microseconds for one function; raises on any failure.

    ``time.perf_counter`` min-of-repeats over the winning synthetic call
    shape. The line-profiler hotspot sum stays the triage signal, but it is
    not the keep/revert number -- profiler overhead variance flips verdicts
    at micro scale.
    """
    try:
        func = run_line_profile.load_function(file_path, func_name)
    except Exception as exc:
        raise RuntimeError(f"cannot load '{func_name}': {exc}") from exc
    try:
        return run_line_profile.measure_verdict_us(
            func, timeout=timeout, repeats=_VERDICT_REPEATS)
    except Exception as exc:
        raise RuntimeError(f"cannot measure '{func_name}': {exc}") from exc


def _analyze(tier: str, source: str, filename: str):
    if tier == "regex_hoist":
        return hoist_analysis.analyze_source(source, filename=filename)
    if tier == "re_call":
        return recall_analysis.analyze_source(source, filename=filename)
    if tier == "invariant_hoist":
        return inv_analysis.analyze_source(source, filename=filename)
    if tier in ACC_TIERS:
        return acc_analysis.analyze_source(source, filename=filename)
    if tier in _NEW_TIER_MODULES:
        analysis_mod, _, _, _ = _NEW_TIER_MODULES[tier]
        return analysis_mod.analyze_source(source, filename=filename)
    return perf_analysis.analyze_source(source, filename=filename)


def _verify(tier: str, source: str, subplan) -> list[str]:
    if tier == "regex_hoist":
        return apply_hoist.verify_plan(source, subplan)
    if tier == "re_call":
        return apply_recall.verify_plan(source, subplan)
    if tier == "invariant_hoist":
        return apply_inv.verify_plan(source, subplan)
    if tier in ACC_TIERS:
        return apply_acc.verify_plan(source, subplan)
    if tier in _NEW_TIER_MODULES:
        _, _, apply_mod, _ = _NEW_TIER_MODULES[tier]
        return apply_mod.verify_plan(source, subplan)
    return apply_perf.verify_plan(source, subplan)


def _apply(tier: str, source: str, subplan) -> str:
    if tier == "regex_hoist":
        return apply_hoist.apply_hoist(source, subplan)
    if tier == "re_call":
        return apply_recall.apply_re_calls(source, subplan)
    if tier == "invariant_hoist":
        return apply_inv.apply_hoists(source, subplan)
    if tier in ACC_TIERS:
        return apply_acc.apply_accumulates(source, subplan)
    if tier in _NEW_TIER_MODULES:
        _, _, apply_mod, func_name = _NEW_TIER_MODULES[tier]
        return getattr(apply_mod, func_name)(source, subplan)
    return apply_perf.apply_rewrites(source, subplan)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write(path: str, source: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(source)


def _unified_diff(path: str, before: str, after: str) -> list[str]:
    """Paste-ready unified diff (stdlib difflib, no external tool)."""
    return list(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
    ))


def apply_and_verify(
    file_path: str, func_name: str, tiers: list[str],
    min_improvement: float, timeout: float | None, dry_run: bool = False,
    show_diff: bool = False,
) -> list[str]:
    """Run the mechanic loop; return human-readable outcome lines."""
    lines = [f"{file_path} :: {func_name}"]
    any_fix = False
    for tier in tiers:
        plan = _analyze(tier, _read(file_path), file_path)
        subplan = _filter_plan(tier, plan, func_name)
        if not subplan.safe:
            continue
        any_fix = True
        desc = _describe(tier, subplan)
        if dry_run:
            lines.append(f"[{tier}] would apply: {desc} (no file written)")
            if show_diff:
                current = _read(file_path)
                proposed = _apply(tier, current, subplan)
                lines += ["", "```diff"] + \
                    _unified_diff(file_path, current, proposed) + ["```"]
            continue
        try:
            before = _measure_total_us(file_path, func_name, timeout)
        except RuntimeError as exc:
            lines.append(f"[{tier}] ERROR: {exc} -- file untouched")
            return lines
        pre_apply = _read(file_path)
        mismatches = _verify(tier, pre_apply, subplan)
        if mismatches:
            lines.append(f"[{tier}] ERROR: plan mismatch -- file untouched")
            lines.extend(f"  - {m}" for m in mismatches)
            return lines
        proposed = _apply(tier, pre_apply, subplan)
        _write(file_path, proposed)
        if show_diff:
            lines += ["", "```diff"] + \
                _unified_diff(file_path, pre_apply, proposed) + ["```"]
        try:
            after = _measure_total_us(file_path, func_name, timeout)
        except RuntimeError as exc:
            _write(file_path, pre_apply)
            assert _read(file_path) == pre_apply
            lines.append(f"[{tier}] ERROR: {exc} -- restored pre-apply bytes")
            return lines
        if confirmation_mod.decide_keep(before, after, min_improvement):
            pct = (before - after) / before * 100 if before else 0.0
            lines.append(f"[{tier}] KEPT: {desc} "
                         f"({before:.1f} -> {after:.1f} us, -{pct:.0f}%)")
        else:
            _write(file_path, pre_apply)
            assert _read(file_path) == pre_apply
            lines.append(f"[{tier}] REVERTED: {desc} "
                         f"({before:.1f} -> {after:.1f} us, "
                         "below margin; pre-apply bytes restored)")
    if not any_fix:
        lines.append("NO-FIX: no mechanical tier0 candidate touches this function")
    return lines


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Apply a mechanical tier0 fix to one function and verify "
        "the gain; revert unless it clears the margin.",
    )
    parser.add_argument("--file", required=True, help="Path to the Python file.")
    parser.add_argument("--func", required=True, help="Function to fix.")
    parser.add_argument(
        "--tier", choices=["auto"] + list(TIERS), default="auto",
        help="Mechanical tier to apply (default: auto tries each in order).",
    )
    parser.add_argument(
        "--min-improvement", type=float, default=confirmation_mod.KEEP_MARGIN,
        help="Required relative gain to keep the rewrite (default: 0.10).",
    )
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="Per-measurement budget in seconds (default: 120).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show the plan and baseline without writing the file.")
    parser.add_argument("--diff", action="store_true",
                        help="Print the paste-ready unified diff (proposed in "
                        "dry-run, applied-or-rejected otherwise).")
    args = parser.parse_args(argv)

    try:
        tiers = list(TIERS) if args.tier == "auto" else [args.tier]
        lines = apply_and_verify(
            args.file, args.func, tiers, args.min_improvement,
            args.timeout, args.dry_run, args.diff,
        )
    except OSError as exc:
        print(f"Error: cannot read file: {exc}", file=sys.stderr)
        sys.exit(1)
    except (apply_hoist.PlanMismatchError, apply_perf.PlanMismatchError,
            apply_acc.PlanMismatchError, apply_inv.PlanMismatchError,
            apply_recall.PlanMismatchError, apply_consumer.PlanMismatchError,
            apply_sorted_minmax.PlanMismatchError,
            apply_membership.PlanMismatchError,
            apply_list_cast.PlanMismatchError,
            apply_logging_lazy.PlanMismatchError,
            apply_dict_keys.PlanMismatchError,
            apply_async_sleep.PlanMismatchError,
            apply_enumerate.PlanMismatchError,
            apply_rematch_search.PlanMismatchError,
            apply_try_hoist.PlanMismatchError) as exc:
        print(f"Error: {exc} -- file untouched", file=sys.stderr)
        sys.exit(1)
    print("\n".join(lines))
    if any(line.startswith("[") and "ERROR" in line for line in lines):
        sys.exit(1)


if __name__ == "__main__":
    main()
