"""Single entry point for interpreter-tier detection and shared finding types.

Every diagnostic in this skill calls :func:`detect_python_tier` (directly or
via :func:`requires_tier`) instead of scattering ``sys.version_info`` checks.
Tiers (see v4.md Sections 1-2, 7):

* ``"baseline"``  -- any standard GIL build, 3.9+. Full diagnostics in their
  version-agnostic form. The floor is (3, 9) to match ``pyproject.toml``
  ``requires-python``; nothing in this module needs newer APIs.
* ``"enhanced"``  -- a build actually compiled with ``Py_GIL_DISABLED``
  (3.13t experimental, 3.14t+). Baseline diagnostics still run, plus
  free-threading-specific checks. Activation emits one ``RuntimeWarning``.

The ``Finding`` envelope here is for PROCESS-level gates only (concurrency,
memory, bytecode summaries). Function-level static checks use per-detector
plan dataclasses (cf. ``detectors/regex_hoist_analysis.py``) because
resolvers need structured fields a flat envelope cannot carry.

Reliability contract (P1): detection is infallible from the caller's
perspective. :func:`detect_python_tier` never raises -- there is no
unsupported-version error, and the warning emit is guarded so
``python -W error`` / pytest ``filterwarnings = error`` cannot turn tier
detection into a crash.
"""

from __future__ import annotations

import functools
import hashlib
import re
import sys
import sysconfig
import warnings
from dataclasses import asdict, dataclass

SUPPORTED_MIN = (3, 9)
# Free-threaded builds exist starting 3.13 (experimental).
FT_VERSION_FLOOR = (3, 13)


@dataclass
class Finding:
    """Stable schema every process-level diagnostic returns, either tier."""

    tier: str  # "baseline" | "enhanced"
    check: str  # e.g. "gil_resurrection", "memory_ceiling"
    status: str  # "pass" | "fail" | "warn" | "skipped"
    skipped: bool = False
    detail: str = ""
    file: str | None = None
    line: int | None = None

    def to_csv_row(self) -> dict:
        """Return this finding as a plain dict (CSV-writer ready).

        Includes the stable ``fingerprint`` alongside -- never replacing --
        the human-readable fields, so CI policy can diff findings across
        commits without relying on shifting line numbers.
        """
        row = asdict(self)
        row["fingerprint"] = self.fingerprint
        return row

    @property
    def fingerprint(self) -> str:
        """Stable identity for this finding (P5)."""
        return fingerprint(self.check, self.file or "", self.detail)


_LINE_NUMBER_RE = re.compile(r"line\s*\d+", re.IGNORECASE)
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]+")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_detail(detail: str) -> str:
    """Normalize free-text detail so line shifts don't change identity.

    Strips line numbers (``line 12`` -> ``line N``) and hex addresses,
    collapses whitespace. The normalized form is for IDENTITY only -- always
    display the original ``detail`` to humans.
    """
    text = _LINE_NUMBER_RE.sub("line N", detail)
    text = _ADDRESS_RE.sub("0xADDR", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def fingerprint(check: str, function: str, detail: str) -> str:
    """Stable sha1 identity: ``check + function + normalized detail``.

    Line-shifted duplicates of the same finding produce identical
    fingerprints; genuinely different findings do not.
    """
    payload = "\0".join([check, function, normalize_detail(detail)])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def detect_python_tier() -> str:
    """Return ``"baseline"`` or ``"enhanced"`` for this interpreter.

    Keys off the actual build flag (``Py_GIL_DISABLED``), not the version
    number, so a GIL-enabled 3.14 stays baseline and an experimental 3.13t
    build correctly gets enhanced diagnostics. Never raises.
    """
    version = sys.version_info[:2]

    if version < SUPPORTED_MIN:
        # Below the tested floor, but still fully usable: every diagnostic
        # in this module degrades to its version-agnostic form. Failing here
        # would break the package on interpreters it otherwise supports.
        return "baseline"

    is_free_threaded = sysconfig.get_config_var("Py_GIL_DISABLED") == 1

    if is_free_threaded and version >= FT_VERSION_FLOOR:
        label = "3.13t (experimental)" if version == (3, 13) else f"{version[0]}.{version[1]}t"
        try:
            warnings.warn(
                f"Detected a free-threaded build ({label}). Primary support "
                "tier is the standard GIL build on 3.11/3.12; free-threading "
                "diagnostics (biased-refcounting audit, GIL-resurrection "
                "gate, QSBR-aware memory ceiling) are now active for this "
                "session. Treat findings as experimental, especially on "
                "3.13t, since free-threading internals have moved between "
                "point releases.",
                category=RuntimeWarning,
                stacklevel=2,
            )
        except Warning:
            # Warnings-as-errors configurations (python -W error, pytest
            # filterwarnings = error) turn warn() into a raise. Tier
            # detection must survive that: proceed silently.
            pass
        return "enhanced"

    return "baseline"


def requires_tier(tier: str):
    """Decorate a diagnostic so it skips cleanly off-tier, both directions.

    On a tier mismatch the wrapper returns a ``skipped`` :class:`Finding`
    instead of running the function (never raises). The active tier is
    passed into the wrapped function as the ``active_tier`` keyword --
    every gated function MUST accept that kwarg.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, active_tier: str | None = None, **kwargs):
            active = active_tier or detect_python_tier()
            if active != tier:
                return Finding(
                    tier=active,
                    check=func.__name__,
                    status="skipped",
                    skipped=True,
                    detail=f"{func.__name__} requires tier '{tier}'; active tier is '{active}'.",
                )
            return func(*args, active_tier=active, **kwargs)

        return wrapper

    return decorator
