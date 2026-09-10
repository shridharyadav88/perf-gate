"""Fail CI when a change regresses measured performance or adds findings.

Compares two classified CSVs (baseline vs current -- same frozen schema,
no new artifact format):

* hard failure (exit 1): a row's complexity rank got worse, or a
  (file, function) row is newly tier0/tier2;
* advisory: wall-clock hotspot movement beyond --margin (timers are noisy
  and CI runners vary -- harden with --strict-time only on a pinned
  runner);
* harness error (exit 2): unreadable input or a missing tier column.

Rank improvements and resolved findings are reported, never failures.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys

DEFAULT_MARGIN = 0.10  # same 10% as confirmation.KEEP_MARGIN


def _is_actionable(tier: str) -> bool:
    """Any mechanical tier0 or tier2 needs a human/merge decision.

    Prefix-based so future tier0_* tiers are covered without edits here.
    """
    return tier.startswith("tier0_") or tier == "tier2_algorithmic"


def read_rows(path: str) -> list[dict]:
    """Read a classified CSV, ignoring `#` preamble comment lines."""
    try:
        with open(path, newline="", encoding="utf-8") as f:
            raw = f.read()
    except OSError as exc:
        print(f"Error: cannot read '{path}': {exc}", file=sys.stderr)
        sys.exit(2)
    body = "\n".join(
        line for line in raw.splitlines() if not line.startswith("#"))
    reader = csv.DictReader(io.StringIO(body))
    if not reader.fieldnames or "tier" not in reader.fieldnames:
        print(f"Error: '{path}' has no 'tier' column -- not a classified "
              f"CSV (run classify_findings.py first)", file=sys.stderr)
        sys.exit(2)
    return list(reader)


def _rank(row: dict) -> int | None:
    try:
        return int((row.get("complexity_rank") or "").strip())
    except (ValueError, AttributeError):
        return None


def _time_us(row: dict) -> float | None:
    try:
        return float((row.get("total_hotspot_time_us") or "").strip())
    except (ValueError, AttributeError):
        return None


def compare(baseline: list[dict], current: list[dict],
            margin: float = DEFAULT_MARGIN) -> dict:
    """Diff two classified row sets; pure function for tests and CLI."""
    base = {(r.get("file"), r.get("function")): r for r in baseline}
    cur = {(r.get("file"), r.get("function")): r for r in current}
    failures: list[str] = []
    advisories: list[str] = []
    improvements: list[str] = []
    for key in sorted(set(base) | set(cur)):
        old, new = base.get(key), cur.get(key)
        label = f"{key[0]} :: {key[1]}"
        if old is None:
            if new is not None and _is_actionable(new.get("tier") or ""):
                failures.append(f"{label} is newly {new['tier']}")
            continue
        if new is None:
            improvements.append(f"{label} no longer reported")
            continue
        old_rank, new_rank = _rank(old), _rank(new)
        if old_rank is not None and new_rank is not None:
            if old_rank >= 0 and new_rank > old_rank:
                failures.append(
                    f"{label} complexity rank {old_rank} -> {new_rank} "
                    f"({old.get('empirical_big_o', '?')} -> "
                    f"{new.get('empirical_big_o', '?')})")
            elif new_rank < old_rank:
                improvements.append(
                    f"{label} complexity rank {old_rank} -> {new_rank}")
        old_tier = old.get("tier") or ""
        new_tier = new.get("tier") or ""
        if _is_actionable(new_tier) and old_tier != new_tier:
            failures.append(f"{label} entered {new_tier} (was {old_tier})")
        old_t, new_t = _time_us(old), _time_us(new)
        if old_t and new_t and old_t > 0:
            drift = (new_t - old_t) / old_t
            if drift > margin:
                advisories.append(
                    f"{label} hotspot {old_t:.1f}us -> {new_t:.1f}us "
                    f"(+{100 * drift:.0f}%, margin {100 * margin:.0f}%)")
    return {"failures": failures, "advisories": advisories,
            "improvements": improvements}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Fail when classified findings regress vs a baseline.",
    )
    parser.add_argument("--baseline", required=True,
                        help="Baseline classified CSV.")
    parser.add_argument("--current", required=True,
                        help="Current classified CSV.")
    parser.add_argument("--margin", type=float, default=DEFAULT_MARGIN,
                        help="Relative hotspot drift flagging threshold "
                        f"(default: {DEFAULT_MARGIN}).")
    parser.add_argument("--strict-time", action="store_true",
                        help="Promote wall-time advisories to failures "
                        "(same-runner only; timers are noisy).")
    args = parser.parse_args(argv)

    result = compare(read_rows(args.baseline), read_rows(args.current),
                     margin=args.margin)
    for line in result["improvements"]:
        print(f"IMPROVED: {line}")
    for line in result["advisories"]:
        print(f"{'FAIL' if args.strict_time else 'ADVISORY'}: {line}")
    for line in result["failures"]:
        print(f"FAIL: {line}")
    if result["failures"] or (args.strict_time and result["advisories"]):
        print(f"{len(result['failures'])} blocking regression(s).")
        sys.exit(1)
    print("No blocking regressions.")


if __name__ == "__main__":
    main()
