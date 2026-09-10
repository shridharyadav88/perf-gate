"""Per-target execution budgets for the profiling path (W2).

Profiling imports and CALLS target code -- repeatedly, at growing input
sizes. A hanging (or pathologically slow) target must produce an error ROW,
never a hung or dead run. :func:`run_with_timeout` is the single mechanism
every execution path uses.

Implementation note: the callable runs on a daemon thread that is ABANDONED
on timeout, not killed -- Python cannot kill threads. The abandoned call
keeps running until process exit, so a timeout breach costs one lingering
thread, not a hang. For genuinely untrusted code, isolate at the process
level (subprocess) instead of relying on this; this helper is containment
for accidents, not a sandbox against adversaries.
"""

from __future__ import annotations

import threading


class TimeoutBudgetExceeded(TimeoutError):
    """A profiled target exceeded its per-target execution budget."""


def run_with_timeout(call, timeout: float | None):
    """Run zero-argument callable *call*, aborting after *timeout* seconds.

    *timeout* of ``None`` (or non-positive) runs *call* inline with no
    budget. Otherwise *call* runs on a daemon worker: if it is still alive
    after *timeout* seconds, raise :exc:`TimeoutBudgetExceeded` and abandon
    the worker. Exceptions raised by *call* itself propagate unchanged.
    """
    if timeout is None or timeout <= 0:
        return call()

    box: dict = {}
    done = threading.Event()

    def _target() -> None:
        try:
            box["result"] = call()
        except Exception as exc:  # worker errors are re-raised by the caller
            box["error"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=_target, daemon=True)
    worker.start()
    if not done.wait(timeout):
        raise TimeoutBudgetExceeded(
            f"Timed out after {timeout:g}s. Raise the budget with --timeout, "
            "or shrink the work with --max-n / --n-measures."
        )
    if "error" in box:
        raise box["error"]
    return box.get("result")
