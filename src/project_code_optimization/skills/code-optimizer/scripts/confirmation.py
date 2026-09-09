"""Statistical treatment for noisy empirical signals (W9, P7).

Big-O curve fits flap between adjacent classes run-to-run (documented in
SKILL.md: the same function can fit Quadratic at one range/run and Linear at
another). Two rules keep noise from driving decisions:

* Tier-2 escalation needs confirmation in the flap zone. Ranks far above the
  noise (cubic and worse) escalate on a single reading; rank 4 (quadratic --
  the classic Linear<->Quadratic flap zone) escalates only with
  ``REQUIRED_CONFIRMATIONS`` independent agreeing measurements. The
  re-measurement protocol (repeat runs / wider N) lives in SKILL.md; the
  ``confirmations`` count travels on the row as an OPTIONAL column
  (absent means 1 -- fully backward compatible, no FIELDNAMES change).
* Keep-or-revert needs a margin. :func:`decide_keep` requires relative
  improvement strictly greater than ``KEEP_MARGIN`` (default 10%) so a 2%
  "improvement" -- noise wearing a costume -- never keeps a rewrite.
"""

from __future__ import annotations

# Independent agreeing measurements required to escalate rank 4 (quadratic).
REQUIRED_CONFIRMATIONS = 2

# Minimum relative improvement (0.10 = 10%) to keep a fix after re-profiling.
KEEP_MARGIN = 0.10


def tier2_confirmed(rank: int, confirmations: int = 1) -> bool:
    """Whether *rank* may route to the algorithmic (tier2) lane.

    Ranks 5+ (cubic/polynomial-high and worse) sit far above measurement
    noise and escalate immediately. Rank 4 needs ``REQUIRED_CONFIRMATIONS``
    agreeing measurements -- a single Quadratic reading is as likely to be
    a flap as a finding. Anything lower never escalates.
    """
    if rank >= 5:
        return True
    if rank == 4:
        return confirmations >= REQUIRED_CONFIRMATIONS
    return False


def decide_keep(before: float, after: float, min_improvement: float = KEEP_MARGIN) -> bool:
    """Keep the rewrite iff *after* beats *before* by more than the margin.

    Both values are the same cost metric (e.g. total hotspot microseconds)
    measured the same way. ``before <= 0`` cannot form a ratio -- returns
    ``False`` (no evidence of improvement). Exactly-at-margin returns
    ``False``: the burden of proof is on the rewrite.
    """
    if before <= 0:
        return False
    if after >= before:
        return False
    return (before - after) / before > min_improvement
