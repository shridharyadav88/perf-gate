"""Concurrency gate (Skill 2): tier-aware parallelism audit + CLI gate.

On enhanced-tier (free-threaded) builds this is the GIL-resurrection gate:
a legacy C-extension without ``Py_mod_gil = Py_MOD_GIL_NOT_USED`` silently
re-enables the GIL process-wide, killing multi-core scaling. On baseline
(GIL) builds there is no GIL to resurrect, so the gate instead reports the
useful signal for that tier: CPU-bound work stays behind the GIL and wants
multiprocessing or GIL-releasing C-extension sections.

Single call site: :func:`audit_concurrency`. Exit codes follow the gate
contract (Section 10): 0 unless the finding is ``fail`` (1); harness errors
are 2, never a silent pass.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tier_gate  # noqa: E402
from tier_gate import Finding  # noqa: E402


def audit_gil_resurrection(active_tier: str) -> Finding:
    """Enhanced tier only: ``fail`` iff the GIL was resurrected on an FT build.

    Must only be called with ``active_tier == "enhanced"`` (a confirmed
    free-threaded build per :func:`tier_gate.detect_python_tier`), so
    ``gil_active=True`` here reliably means resurrection happened -- not a
    misclassified GIL build. Missing ``sys._is_gil_enabled`` fails closed.
    """
    gil_active = sys._is_gil_enabled() if hasattr(sys, "_is_gil_enabled") else True
    if gil_active:
        return Finding(
            tier=active_tier, check="gil_resurrection", status="fail",
            detail="SCALING_LOCKED: GIL active on a free-threaded build -- "
            "a legacy C-extension likely resurrected it",
        )
    return Finding(
        tier=active_tier, check="gil_resurrection", status="pass",
        detail="TRUE_PARALLEL: GIL disabled on a free-threaded build",
    )


def audit_baseline_concurrency(active_tier: str) -> Finding:
    """Baseline tier: informational -- the GIL is permanently active here."""
    return Finding(
        tier=active_tier, check="concurrency_baseline", status="pass",
        detail="GIL always active on this build; not applicable -- consider "
        "multiprocessing or GIL-release C-extension sections for CPU parallelism.",
    )


def audit_concurrency(active_tier: str | None = None) -> Finding:
    """Audit parallelism for this process with exactly one tier lookup.

    *active_tier* overrides detection (tests and callers that already hold
    the tier); ``None`` detects live.
    """
    tier = active_tier or tier_gate.detect_python_tier()
    if tier == "enhanced":
        return audit_gil_resurrection(tier)
    return audit_baseline_concurrency(tier)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Audit process parallelism (GIL-resurrection gate on FT builds).",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit the finding as JSON instead of text.",
    )
    args = parser.parse_args(argv)

    try:
        finding = audit_concurrency()
    except Exception as exc:  # harness failure: loud, never a silent pass
        print(f"Error: concurrency audit failed: {exc}", file=sys.stderr)
        sys.exit(2)

    if args.json:
        print(json.dumps(finding.to_csv_row(), indent=2))
    else:
        print(f"[{finding.status.upper()}] {finding.check}: {finding.detail}")

    if finding.status == "fail":
        sys.exit(1)


if __name__ == "__main__":
    main()
