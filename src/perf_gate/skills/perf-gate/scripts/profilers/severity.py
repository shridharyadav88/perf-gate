"""Severity ranking shared by the profiling scripts and the baseline report.

Empirical Big-O complexity classes are mapped to a numeric rank so that
multi-target profiling runs (``--target-type file/files/commit/repo``) can be
sorted and labeled most-severe-first, matching the ordering the
``baseline_report_template.md`` asks the agent to produce.
"""

from __future__ import annotations

import re

# Ordered worst-to-best is implied by the numeric value, not dict order.
_COMPLEXITY_RANK = {
    "exponential": 6,
    "cubic": 5,
    "polynomial": 5,
    "quadratic": 4,
    "linearithmic": 3,
    "linear": 2,
    "logarithmic": 1,
    "constant": 0,
}

UNKNOWN_RANK = -1

# big_o's "Polynomial" class fits x^k for whatever k minimizes residuals, so it
# overlaps Linear (k=1) and Quadratic (k=2) rather than meaning "worse than
# both" — a flat "polynomial => rank 5" would mislabel e.g. "Polynomial: time
# = 1.3E-05 * x^1 (sec)" (a real result seen in practice) as worse than
# Quadratic when it is actually equivalent to Linear.
_POLYNOMIAL_EXPONENT_RE = re.compile(r"x\^(-?[\d.]+)")


def complexity_rank(complexity_name: str | None) -> int:
    """Map an empirical Big-O class name (e.g. ``"Quadratic"``) to a severity rank.

    Higher is worse. Returns :data:`UNKNOWN_RANK` if *complexity_name* is
    empty or does not match a known class (e.g. profiling failed).
    """
    key = (complexity_name or "").strip().lower()
    if not key:
        return UNKNOWN_RANK
    if "polynomial" in key:
        return _polynomial_rank(key)
    for known, rank in _COMPLEXITY_RANK.items():
        if known in key:
            return rank
    return UNKNOWN_RANK


def _polynomial_rank(key: str) -> int:
    """Rank a "Polynomial: ... x^<exponent> ..." fit by its actual exponent.

    Falls back to the flat Cubic-tier rank if the exponent can't be parsed
    out of the descriptive string (e.g. only the bare word "polynomial").
    """
    match = _POLYNOMIAL_EXPONENT_RE.search(key)
    if not match:
        return _COMPLEXITY_RANK["polynomial"]
    exponent = float(match.group(1))
    if exponent <= 1.15:
        return _COMPLEXITY_RANK["linear"]
    if exponent <= 2.15:
        return _COMPLEXITY_RANK["quadratic"]
    return _COMPLEXITY_RANK["polynomial"]


def severity_label(rank: int) -> str:
    """Human-readable severity band for a rank produced by :func:`complexity_rank`."""
    if rank >= 6:
        return "Critical"
    if rank >= 4:
        return "High"
    if rank >= 2:
        return "Medium"
    if rank >= 0:
        return "Low"
    return "Unknown"


def hotspot_sort_key(rank: int, magnitude: float) -> tuple[int, float]:
    """Sort key for ranking issues most-severe-first.

    Primary: empirical complexity rank (worse growth first).
    Secondary: a magnitude measure for tie-breaking within the same
    complexity class — e.g. total measured hotspot time (preferred, since a
    single-line function is always "100% in one line" regardless of cost) or,
    failing that, top-hotspot concentration percentage.
    """
    return (rank, magnitude)
