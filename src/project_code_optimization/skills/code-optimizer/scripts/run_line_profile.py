"""Line-by-line operation-hit profiling — core logic + CLI wrapper.

The :func:`run_line_profile` function is the importable core; :func:`main` is
the argparse-driven CLI that the agent skill invokes.
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import io
import json
import linecache
import os
import sys
from collections.abc import Callable, Iterable
from typing import Any

# This module is always loaded either by direct script execution or via
# importlib.util.spec_from_file_location (never as part of an importable
# package, since "code-optimizer" is not a valid Python identifier) — so
# sibling modules are resolved by adding this file's directory to sys.path
# rather than via package-relative imports.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import target_resolution  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_function(file_path: str, func_name: str) -> Callable:
    """Load *func_name* from *file_path* and return the callable.

    Raises :exc:`ValueError` if the file or function cannot be resolved.
    """
    spec = importlib.util.spec_from_file_location("_line_profile_target_module", file_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load module from '{file_path}'")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
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
    has_var_keyword = False

    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_POSITIONAL:
            has_var_positional = True
            pos_args.append(range(1000))
            continue
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            has_var_keyword = True
            kw_args["_synthetic"] = {}
            continue

        annotation = param.annotation if param.annotation is not inspect.Parameter.empty else None

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

        if param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
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
):
    """Instrument *func* with :class:`LineProfiler` and execute it once.

    Returns ``(profiler, error)``. *error* is ``None`` on success, or the
    exception raised by both the resolved-argument attempt and the string
    fallback attempt.
    """
    from line_profiler import LineProfiler  # local import

    lp = LineProfiler()
    profiled = lp(func)

    if args is None:
        try:
            args, kwargs = synthesize_arguments(func)
        except Exception:
            args, kwargs = (), {}

    # Attempt execution with the resolved (or provided) arguments.
    try:
        profiled(*args, **(kwargs or {}))
        return lp, None
    except Exception:
        # Graceful degradation — try once more with a string argument as a
        # broad fallback, mirroring the spec's intent.
        try:
            profiled("x" * 200)
            return lp, None
        except Exception as exc:
            return lp, exc


def run_line_profile(
    func: Callable,
    args: tuple | None = None,
    kwargs: dict | None = None,
) -> str:
    """Line-profile *func* and return the formatted stats string.

    If *args* / *kwargs* are not provided, :func:`synthesize_arguments` is
    used to build a plausible synthetic input.  If every call attempt raises
    an exception the output will contain a warning instead of a traceback.
    """
    lp, error = _execute_profiled(func, args, kwargs)
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
) -> dict:
    """Line-profile *func* and return structured hotspot data plus formatted text.

    Returns a dict with keys ``"formatted"`` (the same text :func:`run_line_profile`
    would produce), ``"total_hits"``, and ``"hotspots"`` — a list of
    ``{"line", "hits", "time_us", "pct_time", "source"}`` dicts sorted by
    descending time. On failure, ``"error"`` is set and ``"hotspots"`` is empty.
    """
    lp, error = _execute_profiled(func, args, kwargs)
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

def _profile_one(file_path: str, func_name: str) -> dict:
    """Profile one (file, func) pair, returning a result dict rather than exiting.

    Used by the multi-target loop so one broken target doesn't abort a
    file/files/commit/repo-wide run. Multi-target runs always use synthesized
    arguments — ``--call-args`` only applies to target-type 'function'.
    """
    try:
        func = load_function(file_path, func_name)
    except Exception as exc:  # a target's own import errors must not abort the scan
        return {"file": file_path, "func": func_name, "total_time_us": -1.0, "error": str(exc)}

    stats = compute_line_stats(func)
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
        help="Safety cap on resolved (file, func) pairs for multi-target runs (default: 30).",
    )
    parser.add_argument(
        "--call-args",
        type=str,
        default=None,
        help="JSON array of positional arguments to pass to the function (target-type: function).",
    )
    args = parser.parse_args(argv)

    if args.target_type == "function":
        if not args.file or not args.func:
            print("Error: target-type 'function' requires --file and --func", file=sys.stderr)
            sys.exit(2)

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
        output = run_line_profile(func, args=call_args, kwargs=call_kwargs)
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

    truncated = len(pairs) > args.max_targets
    pairs = pairs[: args.max_targets]

    results = [_profile_one(f, fn) for f, fn in pairs]
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
