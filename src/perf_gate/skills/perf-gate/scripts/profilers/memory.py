"""Memory ceiling gate (Skill 3): tier-aware heap-growth check + CLI gate.

``tracemalloc`` behaves the same on every supported version, so no gating is
needed on the measurement side. The ceiling is a SINGLE universal threshold
(2%): 0.5% would be unsound on the enhanced tier (QSBR-deferred reclamation
and mimalloc's reluctance to return freed pages read as growth), and this
gate accepts arbitrary numbers (RSS or tracemalloc), so the default must be
safe for either signal on every supported tier. A real leak compounds past
2% quickly (delayed, never missed); a false positive disables the gate
(total loss). Tighten per-pipeline with ``--max-growth`` once p95 data is
measured.

Design note (corrects the v4 sketch): ``gc.collect()`` lives in the
MEASUREMENT helper, not in the gate. Collecting inside the gate -- after the
numbers were already measured -- cannot change the ratio; callers on the
enhanced tier must collect BEFORE measuring (see
:func:`current_traced_bytes` with ``collect_first=True``). The gate itself
stays pure over numbers, which is what makes it trivially testable.

Thresholds are uncalibrated starting points (see SKILL.md for the p95
procedure); both are flags, not constants.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tier_gate  # noqa: E402
from tier_gate import Finding  # noqa: E402

DEFAULT_MAX_GROWTH = 0.02


def ensure_tracemalloc(nframe: int = 1) -> None:
    """Start ``tracemalloc`` if it is not already tracing (idempotent)."""
    if not tracemalloc.is_tracing():
        tracemalloc.start(nframe)


def current_traced_bytes(collect_first: bool = False) -> int:
    """Return currently-traced Python heap bytes.

    Pass ``collect_first=True`` on the enhanced tier so QSBR-deferred cyclic
    garbage is actually freed at the allocator level BEFORE measuring --
    otherwise deferred frees read as growth. (What collection cannot do is
    force mimalloc to hand freed pages back to the OS; that lag is what the
    widened enhanced threshold absorbs.)
    """
    ensure_tracemalloc()
    if collect_first:
        gc.collect()
    current, _peak = tracemalloc.get_traced_memory()
    return current


def memory_ceiling_gate(
    baseline: float,
    current: float,
    active_tier: str | None = None,
    max_growth: float = DEFAULT_MAX_GROWTH,
) -> Finding:
    """Pure gate: ``fail`` iff relative growth exceeds the universal ceiling.

    The threshold is deliberately NOT tier-branched (see module docstring).
    ``baseline <= 0`` cannot form a ratio -- returns ``skipped``, never a
    divide-by-zero or a false fail. Shrinkage (negative growth) passes.
    """
    tier = active_tier or tier_gate.detect_python_tier()
    threshold = max_growth

    if baseline <= 0:
        return Finding(
            tier=tier, check="memory_ceiling", status="skipped", skipped=True,
            detail="baseline <= 0; cannot compute a growth ratio",
        )

    growth = (current - baseline) / baseline
    status = "pass" if growth <= threshold else "fail"
    return Finding(
        tier=tier, check="memory_ceiling", status=status,
        detail=f"growth={growth:.4f} threshold={threshold}",
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Fail iff heap growth since baseline exceeds the tier ceiling.",
    )
    parser.add_argument("--baseline", type=float, required=True, help="Baseline bytes.")
    parser.add_argument("--current", type=float, required=True, help="Current bytes.")
    parser.add_argument(
        "--max-growth", type=float, default=DEFAULT_MAX_GROWTH,
        help=f"Growth ceiling as a ratio (default: {DEFAULT_MAX_GROWTH}).",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit the finding as JSON instead of text.",
    )
    args = parser.parse_args(argv)

    try:
        finding = memory_ceiling_gate(
            args.baseline, args.current, max_growth=args.max_growth,
        )
    except Exception as exc:  # harness failure: loud, never a silent pass
        print(f"Error: memory gate failed: {exc}", file=sys.stderr)
        sys.exit(2)

    if args.json:
        print(json.dumps(finding.to_csv_row(), indent=2))
    else:
        print(f"[{finding.status.upper()}] {finding.check}: {finding.detail}")

    if finding.status == "fail":
        sys.exit(1)


if __name__ == "__main__":
    main()
