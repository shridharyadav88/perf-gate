"""Line-by-line operation-hit profiling — core logic + CLI wrapper.

The :func:`run_line_profile` function is the importable core; :func:`main` is
the argparse-driven CLI that the agent skill invokes.
"""

from __future__ import annotations

import argparse
import inspect
import io
import json
import linecache
import os
import sys
import time
from collections.abc import Callable, Iterable
from typing import Any

# Sibling profiler modules live in this same package: add the package's parent
# (scripts/) to sys.path and import via the package, so this file works both
# by direct script execution and when imported as profilers.run_line_profile.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from profilers import (
    target_resolution,  # noqa: E402
    timeouts,  # noqa: E402
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_function(file_path: str, func_name: str) -> Callable:
    """Load *func_name* from *file_path* and return the callable.

    Package-aware: relative and intra-repo absolute imports resolve via stub
    parent packages (``__init__`` chains never execute). Raises
    :exc:`ValueError` if the file or function cannot be resolved.
    """
    try:
        module = target_resolution.load_module_at_path(
            file_path, module_name="_line_profile_target_module"
        )
    except FileNotFoundError:
        raise ValueError(f"File not found: '{file_path}'") from None
    func = getattr(module, func_name, None)
    if func is None:
        raise ValueError(f"Function '{func_name}' not found in '{file_path}'.")
    return func


def synthesize_arguments(func: Callable) -> tuple[tuple, dict]:
    """Build one plausible synthetic argument set for *func* using signature inspection.

    Returns ``(args, kwargs)`` suitable for calling ``func(*args, **kwargs)``.
    If the function signature includes ``*args`` or ``**kwargs`` a single
    iterable / empty dict is passed respectively.  The return values are best-
    effort — callers should still guard against :exc:`TypeError` or
    :exc:`ValueError` at the call site.
    """
    try:
        sig = inspect.signature(func)
    except (ValueError, TypeError):
        # Built-ins and some C extensions have no signature — fall back to
        # a single positional iterable.
        return (range(1000),), {}

    pos_args: list[Any] = []
    kw_args: dict[str, Any] = {}
    has_var_positional = False

    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_POSITIONAL:
            has_var_positional = True
            pos_args.append(range(1000))
            continue
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            kw_args["_synthetic"] = {}
            continue

        annotation = (param.annotation if param.annotation is not inspect.Parameter.empty
                      else None)

        if annotation is int:
            value: Any = 0
        elif annotation is float:
            value = 0.0
        elif annotation is bool:
            value = False
        elif annotation is str:
            value = "x" * 200
        elif annotation is list or annotation is Iterable:
            value = list(range(1000))
        elif annotation is dict:
            value = {"key": "value"}
        elif annotation is None or annotation is inspect.Parameter.empty:
            value = range(1000)
        else:
            value = range(1000)

        positional = (inspect.Parameter.POSITIONAL_ONLY,
                      inspect.Parameter.POSITIONAL_OR_KEYWORD)
        if param.kind in positional:
            pos_args.append(value)
        elif param.kind == inspect.Parameter.KEYWORD_ONLY:
            kw_args[param.name] = value

    # If no positional params at all (zero-arg function), don't pass anything
    # so the call does not fail with "takes 0 positional arguments".
    if not pos_args and not has_var_positional:
        return (), kw_args if kw_args else {}

    return tuple(pos_args), kw_args if kw_args else {}


# ---------------------------------------------------------------------------
# Core profiling (testable without CLI)
# ---------------------------------------------------------------------------

def _execute_profiled(
    func: Callable,
    args: tuple | None = None,
    kwargs: dict | None = None,
    timeout: float | None = None,
):
    """Instrument *func* with :class:`LineProfiler` and execute it once.

    Returns ``(profiler, error)``. *error* is ``None`` on success, or the
    exception raised by every call attempt. A *timeout* breach is also
    returned as *error* (a :exc:`TimeoutError`), so callers report it as a
    row, not a hang. Coroutine functions run via ``asyncio.run``; async
    generators come back as *error* (they cannot produce a value to time).
    """
    from line_profiler import LineProfiler  # local import

    try:
        func, _async_note = target_resolution.ensure_sync_callable(func)
    except Exception as exc:
        return None, exc

    lp = LineProfiler()
    profiled = lp(func)

    if args is None:
        try:
            args, kwargs = synthesize_arguments(func)
        except Exception:
            args, kwargs = (), {}

    candidates = _candidate_arg_sets(args, kwargs)

    last_exc: Exception | None = None
    for attempt_args, attempt_kwargs in candidates:
        try:
            timeouts.run_with_timeout(
                lambda: profiled(*attempt_args, **attempt_kwargs), timeout
            )
            return lp, None
        except timeouts.TimeoutBudgetExceeded as exc:
            return lp, exc  # no fallback: retrying a hung call cannot help
        except Exception as exc:
            last_exc = exc
            continue
    return lp, last_exc if last_exc is not None else RuntimeError("no attempts ran")


def _candidate_arg_sets(args: tuple, kwargs: dict | None) -> list:
    """Candidate argument shapes, first success wins. The synthesized set is
    tried first; then per-position int/str products over guessed
    containers (int first: counters fail fast on strings, and text-eaters
    still resolve second), capped so the search stays bounded; the bare
    string fallback from the original spec closes the list."""
    import itertools

    def _dedup_key(seq):
        # Trial tuples can embed synthesized containers (lists), which are
        # unhashable: dedup on the tuple itself, falling back to its repr
        # (exact, deterministic) so one exotic signature never kills a sweep.
        key = tuple(seq)
        try:
            hash(key)
        except TypeError:
            return repr(key)
        return key

    candidates = [(args, kwargs or {})]
    range_positions = [i for i, a in enumerate(args) if isinstance(a, range)]
    if range_positions and len(range_positions) > 3:
        # Wide signatures skip the product search; whole-tuple guesses still
        # beat the bare fallback's missing-argument noise.
        base = list(args)
        candidates.append((
            tuple(0 if isinstance(a, range) else a for a in base), kwargs or {},
        ))
        candidates.append((
            tuple("x" * 500 if isinstance(a, range) else a for a in base), kwargs or {},
        ))
    if range_positions and len(range_positions) <= 3:
        base = list(args)
        seen = {_dedup_key(base)}
        for combo in itertools.product(["int", "str"], repeat=len(range_positions)):
            trial = list(base)
            for pos, choice in zip(range_positions, combo):
                trial[pos] = 0 if choice == "int" else "x" * 500
            fingerprint = _dedup_key(trial)
            if fingerprint not in seen:
                seen.add(fingerprint)
                candidates.append((tuple(trial), kwargs or {}))
            if len(candidates) >= 12:
                break
    if ("x" * 200,) not in [c[0] for c in candidates]:
        candidates.append((("x" * 200,), {}))
    return candidates


VERDICT_REPEATS = 5


def measure_verdict_us(
    func: Callable,
    args: tuple | None = None,
    kwargs: dict | None = None,
    timeout: float | None = None,
    repeats: int = VERDICT_REPEATS,
) -> float:
    """Min-of-*repeats* wall microseconds for one synthetic call.

    Runs the same winning argument shape the line profiler would use, but
    times the plain call with :func:`time.perf_counter` -- no profiler
    overhead in the number, and the minimum across repeats rejects OS
    scheduling noise (standard ``timeit`` practice). Raises
    :exc:`RuntimeError` when no call shape runs or a verdict re-run fails.
    """
    try:
        func, _async_note = target_resolution.ensure_sync_callable(func)
    except Exception as exc:
        raise RuntimeError(f"cannot prepare callable: {exc}") from exc
    if args is None:
        try:
            args, kwargs = synthesize_arguments(func)
        except Exception:
            args, kwargs = (), {}
    call: Callable | None = None
    call_args: tuple = ()
    call_kwargs: dict = {}
    last_exc: Exception | None = None
    for attempt_args, attempt_kwargs in _candidate_arg_sets(args, kwargs):
        try:
            timeouts.run_with_timeout(
                lambda: func(*attempt_args, **attempt_kwargs), timeout
            )
            call, call_args, call_kwargs = func, attempt_args, attempt_kwargs
            break
        except timeouts.TimeoutBudgetExceeded as exc:
            raise RuntimeError(f"call timed out: {exc}") from exc
        except Exception as exc:
            last_exc = exc
    if call is None:
        detail = last_exc if last_exc is not None else "no attempts ran"
        raise RuntimeError(f"no call shape ran: {detail}")
    best: float | None = None
    for _ in range(max(1, repeats)):
        start = time.perf_counter()
        try:
            timeouts.run_with_timeout(
                lambda: call(*call_args, **call_kwargs), timeout
            )
        except Exception as exc:
            raise RuntimeError(f"verdict re-run failed: {exc}") from exc
        elapsed_us = (time.perf_counter() - start) * 1e6
        best = elapsed_us if best is None else min(best, elapsed_us)
    return float(best)


def run_line_profile(
    func: Callable,
    args: tuple | None = None,
    kwargs: dict | None = None,
    timeout: float | None = None,
) -> str:
    """Line-profile *func* and return the formatted stats string.

    If *args* / *kwargs* are not provided, :func:`synthesize_arguments` is
    used to build a plausible synthetic input.  If every call attempt raises
    an exception the output will contain a warning instead of a traceback.
    """
    lp, error = _execute_profiled(func, args, kwargs, timeout)
    if error is not None:
        return (
            "=== LINE PROFILER OUTPUT ===\n"
            f"Warning: Synthetic test run failed: {error}\n"
            "No profile data collected.\n"
        )

    stream = io.StringIO()
    lp.print_stats(stream=stream)
    return "=== LINE PROFILER OUTPUT ===\n" + stream.getvalue()


def compute_line_stats(
    func: Callable,
    args: tuple | None = None,
    kwargs: dict | None = None,
    timeout: float | None = None,
) -> dict:
    """Line-profile *func* and return structured hotspot data plus formatted text.

    Returns a dict with keys ``"formatted"`` (the same text :func:`run_line_profile`
    would produce), ``"total_hits"``, and ``"hotspots"`` — a list of
    ``{"line", "hits", "time_us", "pct_time", "source"}`` dicts sorted by
    descending time. On failure, ``"error"`` is set and ``"hotspots"`` is empty.
    """
    lp, error = _execute_profiled(func, args, kwargs, timeout)
    if error is not None:
        return {
            "formatted": (
                "=== LINE PROFILER OUTPUT ===\n"
                f"Warning: Synthetic test run failed: {error}\n"
                "No profile data collected.\n"
            ),
            "total_hits": 0,
            "hotspots": [],
            "error": str(error),
        }

    stream = io.StringIO()
    lp.print_stats(stream=stream)
    formatted = "=== LINE PROFILER OUTPUT ===\n" + stream.getvalue()

    stats = lp.get_stats()
    unit = stats.unit
    all_timings = [
        (lineno, nhits, total_time)
        for timings in stats.timings.values()
        for (lineno, nhits, total_time) in timings
    ]
    total_hits = sum(nhits for _, nhits, _ in all_timings)
    grand_total_time = sum(total_time for _, _, total_time in all_timings)

    hotspots = []
    for (filename, _start_lineno, _func_name), timings in stats.timings.items():
        for lineno, nhits, total_time in timings:
            time_us = total_time * unit * 1e6
            pct_time = (total_time / grand_total_time * 100) if grand_total_time else 0.0
            source = linecache.getline(filename, lineno).rstrip("\n")
            hotspots.append({
                "line": lineno,
                "hits": nhits,
                "time_us": round(time_us, 2),
                "pct_time": round(pct_time, 2),
                "source": source,
            })
    hotspots.sort(key=lambda h: h["time_us"], reverse=True)

    return {"formatted": formatted, "total_hits": total_hits, "hotspots": hotspots}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _profile_one(file_path: str, func_name: str, timeout: float | None = None) -> dict:
    """Profile one (file, func) pair, returning a result dict rather than exiting.

    Used by the multi-target loop so one broken target doesn't abort a
    file/files/commit/repo-wide run. Multi-target runs always use synthesized
    arguments — ``--call-args`` only applies to target-type 'function'.
    """
    try:
        func = load_function(file_path, func_name)
    except Exception as exc:  # a target's own import errors must not abort the scan
        return {"file": file_path, "func": func_name, "total_time_us": -1.0, "error": str(exc)}

    stats = compute_line_stats(func, timeout=timeout)
    if stats.get("error"):
        return {
            "file": file_path, "func": func_name, "total_time_us": -1.0, "error": stats["error"],
        }

    # Sort by absolute time spent, not concentration: a single-line function
    # is always "100% in one line" regardless of how expensive it actually is.
    total_time_us = sum(h["time_us"] for h in stats["hotspots"])
    top_pct = stats["hotspots"][0]["pct_time"] if stats["hotspots"] else 0.0
    return {
        "file": file_path, "func": func_name, "total_time_us": total_time_us,
        "top_pct": top_pct, "formatted": stats["formatted"],
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run line-by-line operation hit profiling.",
    )
    parser.add_argument(
        "--target-type",
        choices=["function", "file", "files", "commit", "repo"],
        default="function",
        help=(
            "Scope to profile: a single 'function' (default, requires --file/--func), "
            "every module-level function in one 'file', in multiple '--files', in the "
            "files changed by a 'commit' (requires --commit), or across a whole 'repo'."
        ),
    )
    parser.add_argument("--file", help="Path to Python file (target-type: function, file).")
    parser.add_argument("--func", help="Target function name (target-type: function).")
    parser.add_argument(
        "--files", action="append", help="Python file path (target-type: files; repeatable).",
    )
    parser.add_argument("--commit", help="Git commit-ish to scope to (target-type: commit).")
    parser.add_argument(
        "--repo", default=".", help="Repository root (target-type: commit, repo; default: '.').",
    )
    parser.add_argument(
        "--max-targets",
        type=int,
        default=30,
        help="Safety cap on resolved (file, func) pairs for multi-target runs "
        "(default: 30; 0 disables the cap).",
    )
    parser.add_argument(
        "--call-args",
        type=str,
        default=None,
        help="JSON array of positional arguments to pass to the function (target-type: function).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help=(
            "Per-target execution budget in seconds (default: 120). A target that "
            "exceeds it is reported as an error row instead of hanging the run."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List resolved targets and exit before importing or calling anything.",
    )
    args = parser.parse_args(argv)

    if args.target_type == "function":
        if not args.file or not args.func:
            print("Error: target-type 'function' requires --file and --func", file=sys.stderr)
            sys.exit(2)

        if args.dry_run:
            print(target_resolution.format_target_listing(
                "function", [(args.file, args.func)], args.max_targets, False))
            return

        # -- Load function -----------------------------------------------------
        try:
            func = load_function(args.file, args.func)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

        # -- Resolve arguments ---------------------------------------------------
        call_args: tuple = ()
        call_kwargs: dict = {}
        if args.call_args:
            try:
                parsed = json.loads(args.call_args)
                if isinstance(parsed, list):
                    call_args = tuple(parsed)
                elif isinstance(parsed, dict):
                    call_kwargs = parsed
            except json.JSONDecodeError as exc:
                print(f"Error: Invalid JSON for --call-args: {exc}", file=sys.stderr)
                sys.exit(1)

        # -- Profile -------------------------------------------------------------
        output = run_line_profile(func, args=call_args, kwargs=call_kwargs, timeout=args.timeout)
        print(output)
        return

    # -- Multi-target modes: file, files, commit, repo --------------------------
    try:
        pairs = target_resolution.resolve_targets(
            args.target_type,
            file=args.file,
            files=args.files,
            commit=args.commit,
            repo=args.repo,
        )
    except target_resolution.TargetResolutionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.max_targets <= 0:
        truncated = False  # 0 (or negative) disables the count cap entirely;
        # the per-target --timeout budget remains the guard rail.
    else:
        truncated = len(pairs) > args.max_targets
        pairs = pairs[: args.max_targets]

    if args.dry_run:
        print(target_resolution.format_target_listing(
            args.target_type, pairs, args.max_targets, truncated))
        return

    results = [_profile_one(f, fn, args.timeout) for f, fn in pairs]
    results.sort(key=lambda r: r["total_time_us"], reverse=True)

    print(f"=== LINE PROFILE BASELINE SCAN ({args.target_type}) - {len(results)} target(s) ===")
    if truncated:
        print(f"(truncated to --max-targets={args.max_targets}; more targets were resolved)")
    print()
    for result in results:
        header = f"--- TARGET: {result['file']} :: {result['func']} ---"
        print(header)
        if "error" in result:
            print(f"Error: {result['error']}")
        else:
            print(f"Top hotspot: {result['top_pct']}% of measured time")
            print(result["formatted"])
        print()


if __name__ == "__main__":
    main()
