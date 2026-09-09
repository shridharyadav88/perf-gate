"""Local-integration auditing: ``@audit_performance`` + ``track_memory()``.

The first importable (non-CLI) surface of this package::

    from project_code_optimization import auditing

    @auditing.audit_performance(max_seconds=2.0, max_bytes=1_000_000)
    def my_function(...):
        ...

    with auditing.track_memory() as mem:
        ...
    print(mem["growth"])

Reliability contract (P1 -- observability never breaks the observed):

* The wrapped function's return value and exceptions are NEVER altered by
  the audit machinery. Only the function's OWN exceptions propagate, and in
  ``strict=True`` mode an intentional :exc:`PerformanceViolation` may raise.
* Every audit-machinery failure (broken timer, tracemalloc error, a raising
  ``on_violation`` callback, warnings-as-errors) is contained: the result
  is returned as if auditing had been disabled.
* ``enabled=False`` is a single flag check plus a direct call -- near-zero
  overhead, and tracemalloc is never touched.
* Setting ``max_bytes`` starts ``tracemalloc`` process-wide if it is not
  already tracing (required to measure anything). Time-only audits never
  touch tracemalloc.

The concurrency probe mirrors ``scripts/profilers/concurrency.py`` semantics
(check ``audit_concurrency()`` there for the canonical gate) in stdlib-only
form so this module has zero coupling to the deployed skill tree: baseline
builds report an informational pass, enhanced builds fail only when the GIL
is actually active on a free-threaded interpreter.
"""

from __future__ import annotations

import contextlib
import functools
import sys
import sysconfig
import time
import tracemalloc
import warnings


class PerformanceViolation(Exception):
    """An audited budget was exceeded in ``strict=True`` mode."""


def _detect_tier() -> str:
    """Baseline/enhanced probe; infallible by construction (never raises)."""
    try:
        version = sys.version_info[:2]
        if version < (3, 9):
            return "baseline"
        free_threaded = sysconfig.get_config_var("Py_GIL_DISABLED") == 1
        if free_threaded and version >= (3, 13):
            return "enhanced"
    except Exception:
        pass
    return "baseline"


def _concurrency_status() -> tuple[str, str, str]:
    """(tier, status, detail); fail means GIL resurrected on an FT build."""
    tier = _detect_tier()
    if tier != "enhanced":
        return (
            tier, "pass",
            "GIL always active on this build; not applicable.",
        )
    try:
        active = sys._is_gil_enabled() if hasattr(sys, "_is_gil_enabled") else True
    except Exception:
        active = True
    if active:
        return (tier, "fail", "SCALING_LOCKED: GIL active on a free-threaded build")
    return (tier, "pass", "TRUE_PARALLEL: GIL disabled on a free-threaded build")


def _notify(message: str, on_violation) -> None:
    """Deliver a violation report; never raises, whatever happens."""
    try:
        if on_violation is not None:
            on_violation(message)
            return
        try:
            warnings.warn(message, category=RuntimeWarning, stacklevel=4)
        except Warning:
            pass  # warnings-as-errors must not break user code
    except Exception:
        pass  # a raising callback must not break user code either


def _measure_memory_before():
    try:
        if not tracemalloc.is_tracing():
            tracemalloc.start(1)
        return tracemalloc.get_traced_memory()[0]
    except Exception:
        return None


def _measure_memory_after():
    try:
        current, _peak = tracemalloc.get_traced_memory()
        return current
    except Exception:
        return None


def audit_performance(
    func=None,
    *,
    max_seconds: float | None = None,
    max_bytes: float | None = None,
    enabled: bool = True,
    strict: bool = False,
    on_violation=None,
    check_concurrency: bool = True,
):
    """Decorate *func* with time/memory/concurrency budgets.

    Usable bare (``@audit_performance`` -- measure only, violations warn) or
    parameterized (``@audit_performance(max_seconds=1.0, strict=True)``).

    Parameters
    ----------
    max_seconds:
        Wall-clock budget per call. ``None`` (default) measures without
        enforcing.
    max_bytes:
        Traced-heap growth budget per call in bytes. ``None`` (default)
        neither measures nor enforces (tracemalloc untouched). Setting it
        starts tracemalloc process-wide when needed.
    enabled:
        ``False`` restores the undecorated function exactly (one flag check).
    strict:
        ``True`` raises :exc:`PerformanceViolation` on any violation instead
        of reporting it. The wrapped function's own exceptions always
        propagate regardless of this flag.
    on_violation:
        Optional ``callable(message: str)`` receiving each violation report.
        Default reports via ``warnings.warn``. A raising callback is
        contained, never propagated.
    check_concurrency:
        Also evaluate the tier-aware concurrency probe per call (cheap flag
        reads; a ``fail`` finding -- GIL resurrected on an FT build -- counts
        as a violation like any budget breach).
    """

    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not enabled:
                return fn(*args, **kwargs)
            try:
                start = time.perf_counter()
            except Exception:
                start = None
            mem_before = _measure_memory_before() if max_bytes is not None else None
            result = fn(*args, **kwargs)
            try:
                _evaluate(
                    fn, result, start, mem_before,
                    max_seconds=max_seconds, max_bytes=max_bytes,
                    strict=strict, on_violation=on_violation,
                    check_concurrency=check_concurrency,
                )
            except PerformanceViolation:
                raise
            except Exception:
                pass  # audit machinery failed: result stands regardless
            return result

        return wrapper

    if func is None:
        return decorate
    return decorate(func)


def _evaluate(
    fn, result, start, mem_before, *, max_seconds, max_bytes,
    strict, on_violation, check_concurrency,
):
    """Compare measurements against budgets; warn or strict-raise. Returns result."""
    elapsed = None
    if start is not None:
        try:
            elapsed = time.perf_counter() - start
        except Exception:
            elapsed = None

    violations = []
    if max_seconds is not None and elapsed is not None and elapsed > max_seconds:
        violations.append(f"took {elapsed:.3f}s over budget {max_seconds:g}s")
    if max_bytes is not None and mem_before is not None:
        mem_after = _measure_memory_after()
        if mem_after is not None:
            growth = mem_after - mem_before
            if growth > max_bytes:
                violations.append(f"grew {growth} bytes over budget {max_bytes:g} bytes")
    if check_concurrency:
        tier, status, detail = _concurrency_status()
        if status == "fail":
            violations.append(f"concurrency gate failed ({detail})")

    if not violations:
        return result
    message = f"@audit_performance: {getattr(fn, '__qualname__', fn)}: " + "; ".join(violations)
    if strict:
        raise PerformanceViolation(message)
    _notify(message, on_violation)
    return result


@contextlib.contextmanager
def track_memory(enabled: bool = True):
    """Yield a record dict filled with traced-heap numbers on exit.

    Keys: ``before`` / ``after`` / ``growth`` / ``peak`` (bytes; ``None``
    when unmeasurable). With ``enabled=False`` yields an all-``None`` record
    without touching tracemalloc. Exceptions from the wrapped block always
    propagate; the record is still filled first. Never raises itself.
    """
    record = {"before": None, "after": None, "growth": None, "peak": None}
    if not enabled:
        yield record
        return
    try:
        if not tracemalloc.is_tracing():
            tracemalloc.start(1)
        record["before"] = tracemalloc.get_traced_memory()[0]
    except Exception:
        pass
    try:
        yield record
    finally:
        try:
            current, peak = tracemalloc.get_traced_memory()
            record["after"] = current
            record["peak"] = peak
            if record["before"] is not None:
                record["growth"] = current - record["before"]
        except Exception:
            pass
