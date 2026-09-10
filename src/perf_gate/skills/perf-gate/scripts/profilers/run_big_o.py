"""Big-O empirical complexity estimation — core logic + CLI wrapper.

The :func:`profile_big_o` function is the importable core; :func:`main` is the
argparse-driven CLI that the agent skill invokes.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from typing import Any

# Sibling profiler modules live in this same package: add the package's parent
# (scripts/) to sys.path and import via the package, so this file works both
# by direct script execution and when imported as profilers.run_big_o.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from profilers import (
    severity,  # noqa: E402
    target_resolution,  # noqa: E402
    timeouts,  # noqa: E402
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_module(file_path: str) -> Any:
    """Load a Python module from *file_path* and return the module object.

    Package-aware: relative and intra-repo absolute imports resolve via stub
    parent packages (``__init__`` chains never execute). Raises
    :exc:`ValueError` if the file does not exist.
    """
    try:
        return target_resolution.load_module_at_path(
            file_path, module_name="_big_o_target_module"
        )
    except FileNotFoundError:
        raise ValueError(f"File not found: '{file_path}'") from None


# ---------------------------------------------------------------------------
# Core profiling (testable without CLI)
# ---------------------------------------------------------------------------

def _fixed_value(param, unknown_fill) -> Any:
    """One plausible fixed value: default first, else annotation, else fill.

    *unknown_fill* (``0`` or ``"range"``) covers unannotated params without
    defaults, where no single guess is right -- callers try both variants.
    """
    import inspect

    if param.default is not inspect.Parameter.empty:
        return param.default
    ann = param.annotation if param.annotation is not inspect.Parameter.empty else None
    if ann is int:
        return 0
    if ann is float:
        return 0.0
    if ann is bool:
        return False
    if ann is str:
        return "x" * 200
    if ann is bytes:
        return b"x" * 200
    if ann in (list, tuple, set):
        return []
    if ann is dict:
        return {}
    if ann is None:
        return 0 if unknown_fill == 0 else range(100)
    return 0 if unknown_fill == 0 else range(100)


def _adapt_data(data: list, annotation) -> Any:
    """Scale generated *data* (a list of length N) to *annotation*'s shape."""
    n = len(data)
    if annotation is str:
        return "x" * n
    if annotation is bytes:
        return b"x" * n
    if annotation is int:
        return n
    if annotation is float:
        return float(n)
    if annotation is tuple:
        return tuple(data)
    if annotation is set:
        return set(data)
    if annotation is dict:
        return {i: None for i in range(n)}
    if annotation is range:
        return range(n)
    return list(data)  # list, missing, or anything else list-shaped


_SCALABLE = (list, str, bytes, tuple, set, dict, range, None)

# Parameter names that usually denote the size/input argument. Used ONLY to
# rank otherwise-identical unannotated candidates (e.g. prefer scaling
# `text` over `page`); probes still decide, so a wrong hint costs one
# failed probe call, never a wrong fit.
_SIZE_NAME_HINTS = (
    "n", "size", "length", "data", "text", "items", "xs", "lst", "arr",
    "buf", "buffer", "input", "sequence", "seq", "payload",
)


def _build_adapter(func):
    """Bind *func* to the single-list calling convention ``big_o`` needs.

    Returns a list of ``(adapter, shape_desc)`` candidates where
    ``adapter(data)`` calls *func* with one parameter scaled to
    ``len(data)`` and the rest fixed (defaults honored). The search covers
    scale positions (annotated-scalable first, then unannotated, then
    defaulted) x shapes (annotation-driven, else list- then str-shaped) x
    fixed-guess variants for unknown params (``0``, then ``range``) -- at
    most a dozen tiny probe calls, first success wins. Callers probe each
    candidate on tiny input and keep the first that runs.

    Raises :exc:`ValueError` with an actionable message when no binding is
    possible (zero arguments -- probe loop reports the rest).
    """
    import inspect

    if hasattr(func, "callback") and hasattr(func, "params"):
        # A click Command/Group (discovered by its module-level `def` name
        # but no longer a plain function): argv-driven entry point, not a
        # Big-O-scalable unit. Same by-design family as zero-argument
        # functions -- invoking it would measure the CLI runner, never the
        # algorithm, so report instead of probing junk argv shapes.
        name = getattr(func, "name", getattr(func, "__name__", func))
        raise ValueError(
            f"'{name}' is a CLI command, not a scalable function; Big-O "
            "needs a size parameter that grows with N -- line profiling "
            "still applies"
        )

    try:
        sig = inspect.signature(func)
    except (ValueError, TypeError):
        return [(lambda data: func(list(data)), "single untyped argument")]

    name = getattr(func, "__name__", func)
    positional = [
        p for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    var_positional = next(
        (p for p in sig.parameters.values()
         if p.kind == inspect.Parameter.VAR_POSITIONAL), None,
    )
    if not positional and var_positional is None:
        raise ValueError(
            f"'{name}' takes no arguments; Big-O needs a size parameter "
            "that grows with N -- line profiling still applies"
        )

    if not positional:  # def f(*args): scale the var-positional bundle itself
        return [(lambda data: func(*data), "*args bundle")]

    def _ann(p):
        return p.annotation if p.annotation is not inspect.Parameter.empty else None

    def _hint_rank(i):
        try:
            return _SIZE_NAME_HINTS.index(positional[i].name)
        except ValueError:
            return len(_SIZE_NAME_HINTS)

    undefaulted = [i for i, p in enumerate(positional)
                   if p.default is inspect.Parameter.empty]
    annotated = [i for i in undefaulted if _ann(positional[i]) is not None]
    scalable_first = [i for i in annotated if _ann(positional[i]) in _SCALABLE]
    nonscalable_annotated = [i for i in annotated if _ann(positional[i]) not in _SCALABLE]
    plain_first = sorted(
        (i for i in undefaulted if _ann(positional[i]) is None), key=_hint_rank
    )
    defaulted_first = [i for i, p in enumerate(positional)
                       if p.default is not inspect.Parameter.empty]
    scale_positions = (
        scalable_first + plain_first + nonscalable_annotated + defaulted_first
    )[:3]
    if not scale_positions and positional:
        scale_positions = [0]  # pragma: no cover - defensive; unreachable above

    kwonly = {
        p.name: p for p in sig.parameters.values()
        if p.kind == inspect.Parameter.KEYWORD_ONLY
    }

    def _make(idx, annotation, fill):
        fixed = [_fixed_value(p, fill) for p in positional]
        fixed_kw = {k: _fixed_value(p, fill) for k, p in kwonly.items()}

        def adapter(data):
            call = list(fixed)
            call[idx] = _adapt_data(data, annotation)
            return func(*call, **fixed_kw)

        ann_name = getattr(annotation, "__name__", annotation)
        return adapter, f"scale '{positional[idx].name}' as {ann_name}"

    candidates = []
    for idx in scale_positions:
        ann = _ann(positional[idx])
        shapes = [ann] if ann is not None else [list, str, int]
        for shape in shapes:
            for fill in (0, "range"):
                candidates.append(_make(idx, shape, fill))
    return candidates


def profile_big_o(
    func,
    min_n: int = 100,
    max_n: int = 10000,
    n_measures: int = 8,
    n_timings: int = 5,
    n_repeats: int = 1,
    timeout: float | None = None,
    probe_log: dict | None = None,
    adapter=None,
) -> tuple[str, dict]:
    """Run empirical Big-O estimation on *func*.

    *func* may take several parameters: the size parameter (first undefaulted
    one, preferably list/str-shaped) is scaled with N and the rest are fixed
    (defaults honored, else annotation-plausible values). Unannotated size
    parameters are probed list-shaped then str-shaped; zero-argument
    functions fail fast with a clear message. Coroutine functions run via
    ``asyncio.run`` instead of timing coroutine creation.

    *n_timings* and *n_repeats* are forwarded to ``big_o.big_o`` to reduce
    measurement noise: ``big_o``'s own defaults for both are 1, which makes
    fast, low-variance functions prone to flipping between adjacent
    complexity classes (e.g. Linear vs. Linearithmic) across otherwise
    identical runs, since a single timer sample is kept per data point.
    *n_timings* repeats each measurement and keeps the minimum (filtering out
    GC/scheduler jitter); *n_repeats* calls *func* multiple times per
    measurement and sums the time (helps when a single call is close to the
    timer's resolution). Raise both for a report that will make a specific
    complexity claim; the cost is roughly linear in each.

    *adapter*, when given, is used as-is (``adapter(data)`` calls *func*
    with an input of size ``len(data)``) instead of the deterministic
    probe search — the ``llm_harness.py`` path for model-proposed,
    mechanically validated input builders. Provenance is then recorded as
    ``"llm-assisted"`` (never ``"harness"``: that label stays reserved for
    human-approved harnesses).

    Returns
    -------
    best_fit : str
        Name of the best-fitting complexity class (e.g. ``"Linear"``,
        ``"Quadratic"``).
    fitted : dict
        Mapping of complexity class name → residuals for every model fit.

    The optional *probe_log* dict, when given, is filled with how the input
    was produced so callers can label the result honestly without changing
    the return shape: ``provenance`` (``"harness"`` only when the measured
    function carries ``__big_o_provenance__ == "harness"`` -- set by
    human-approved harness sketches from ``mine_callsites.py`` -- else
    ``"synthetic"``), the winning ``shape_desc``, the measurement window
    (``min_n``/``max_n``/``n_measures``), and the per-class ``residuals``
    (non-finite values stored as None) for fit-confidence scoring. Left
    untouched on failure.
    """
    import functools

    import big_o  # local import keeps the package lightweight on import

    # big_o 0.11+ datagen.integers requires (n, min_, max_); wrap with
    # partial so it matches the single-argument calling convention.
    data_gen = functools.partial(big_o.datagen.integers, min_=0, max_=1_000_000)

    # Coroutine functions must run to a value -- timing coroutine creation
    # would print a convincing, completely bogus fit. Async generators have
    # no value to time and fail fast with a clear message.
    func, _async_note = target_resolution.ensure_sync_callable(func)

    # Bind multi-parameter functions to big_o's single-list convention, then
    # fast-fail probe each candidate shape on tiny input: a doomed fit must
    # become a clear error row, not a wasted budget burn. A caller-supplied
    # adapter (llm_harness.py, already validated) skips the probe search.
    probe_errors: list[str] = []
    winning_shape = ""
    adapter_supplied = adapter is not None
    if adapter is None:
        candidates = _build_adapter(func)
        for candidate, shape_desc in candidates:
            try:
                timeouts.run_with_timeout(lambda: candidate([0, 1, 2, 3, 4]), timeout)
                adapter = candidate
                winning_shape = shape_desc
                break
            except timeouts.TimeoutBudgetExceeded:
                raise  # a hung probe is a hung target: keep TimeoutError semantics
            except Exception as exc:
                probe_errors.append(f"{shape_desc}: {exc}")
    else:
        winning_shape = "llm-assisted build_input"
    if adapter is None:
        name = getattr(func, "__name__", func)
        raise ValueError(
            f"synthetic call failed for '{name}' "
            f"({'; '.join(probe_errors)}); Big-O needs callable input -- "
            "point --target-type function at a harness module that builds "
            "valid input instead"
        )

    # big_o.big_o raises its own exceptions on contract violations; let them
    # propagate so callers can choose how to present them. The whole fit runs
    # under the per-target budget: a hanging target raises
    # TimeoutBudgetExceeded (a TimeoutError subclass), which callers already
    # translate into an error row rather than a hung run.
    def _fit():
        return big_o.big_o(
            adapter,
            data_gen,
            min_n=min_n,
            max_n=max_n,
            n_measures=n_measures,
            n_timings=n_timings,
            n_repeats=n_repeats,
        )

    best_fit, fitted_complexities = timeouts.run_with_timeout(_fit, timeout)
    if probe_log is not None:
        residuals = {}
        for key, value in fitted_complexities.items():
            try:
                number = float(value)
            except (TypeError, ValueError):
                number = None
            residuals[str(key)] = number if number is not None and math.isfinite(number) \
                else None
        marker = getattr(func, "__big_o_provenance__", "synthetic")
        if marker == "harness":
            provenance = "harness"
        elif adapter_supplied:
            provenance = "llm-assisted"
        else:
            provenance = "synthetic"
        probe_log.update({
            "provenance": provenance,
            "shape_desc": winning_shape,
            "min_n": min_n,
            "max_n": max_n,
            "n_measures": n_measures,
            "residuals": residuals,
        })
    return str(best_fit), {str(k): v for k, v in fitted_complexities.items()}


def format_output(best_fit: str, fitted: dict) -> str:
    """Render the Big-O result as the canonical output block."""
    lines = [
        "=== BIG-O PROFILER OUTPUT ===",
        f"Estimated Complexity: {best_fit}",
        "Fitted Models:",
    ]
    for model, residuals in fitted.items():
        lines.append(f"  - {model}: {residuals}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _profile_one(file_path: str, func_name: str, module_cache: dict, args) -> dict:
    """Profile one (file, func) pair, returning a result dict rather than exiting.

    Used by the multi-target loop so one broken target doesn't abort a
    file/files/commit/repo-wide run.
    """
    if file_path not in module_cache:
        try:
            module_cache[file_path] = load_module(file_path)
        except Exception as exc:  # noqa: BLE001 - a target's own import errors must not abort the scan
            module_cache[file_path] = exc
    module = module_cache[file_path]

    if isinstance(module, Exception):
        return {
            "file": file_path, "func": func_name, "rank": severity.UNKNOWN_RANK,
            "error": str(module),
        }

    target_func = getattr(module, func_name, None)
    if target_func is None:
        return {
            "file": file_path, "func": func_name, "rank": severity.UNKNOWN_RANK,
            "error": f"Function '{func_name}' not found in '{file_path}'.",
        }

    try:
        best_fit, fitted = profile_big_o(
            target_func, min_n=args.min_n, max_n=args.max_n, n_measures=args.n_measures,
            n_timings=args.n_timings, n_repeats=args.n_repeats,
            timeout=getattr(args, "timeout", None),
        )
    except Exception as exc:
        return {
            "file": file_path, "func": func_name, "rank": severity.UNKNOWN_RANK,
            "error": (
                f"Big-O profiling failed: {exc}\n"
                f"Hint: for domain-specific input, point --target-type "
                f"function at a harness module that builds valid input."
            ),
        }

    rank = severity.complexity_rank(best_fit)
    return {
        "file": file_path, "func": func_name, "rank": rank,
        "label": severity.severity_label(rank),
        "output": format_output(best_fit, fitted),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run Big-O empirical complexity estimation.",
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
        "--min-n",
        type=int,
        default=100,
        help="Minimum input size (default: 100).",
    )
    parser.add_argument(
        "--max-n",
        type=int,
        default=10000,
        help="Maximum input size (default: 10000).",
    )
    parser.add_argument(
        "--n-measures",
        type=int,
        default=8,
        help="Number of measurements (default: 8).",
    )
    parser.add_argument(
        "--n-timings",
        type=int,
        default=5,
        help=(
            "Timer samples kept (minimum) per measurement (default: 5). Raise this to "
            "reduce classification noise on fast, low-variance functions."
        ),
    )
    parser.add_argument(
        "--n-repeats",
        type=int,
        default=1,
        help="Calls to func summed per measurement (default: 1); raise for very fast functions.",
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

        # -- Load module -------------------------------------------------------
        try:
            module = load_module(args.file)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

        # -- Resolve target function --------------------------------------------
        target_func = getattr(module, args.func, None)
        if target_func is None:
            print(
                f"Error: Function '{args.func}' not found in '{args.file}'.",
                file=sys.stderr,
            )
            sys.exit(1)

        # -- Profile -------------------------------------------------------------
        try:
            best_fit, fitted = profile_big_o(
                target_func,
                min_n=args.min_n,
                max_n=args.max_n,
                n_measures=args.n_measures,
                n_timings=args.n_timings,
                n_repeats=args.n_repeats,
                timeout=args.timeout,
            )
        except Exception as exc:
            print(
                f"Error: Big-O profiling failed: {exc}\n"
                f"Hint: for domain-specific input, point --target-type "
                f"function at a harness module that builds valid input.",
                file=sys.stderr,
            )
            sys.exit(1)

        print(format_output(best_fit, fitted))
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

    module_cache: dict = {}
    results = [_profile_one(f, fn, module_cache, args) for f, fn in pairs]
    results.sort(key=lambda r: r["rank"], reverse=True)

    print(f"=== BIG-O BASELINE SCAN ({args.target_type}) - {len(results)} target(s) ===")
    if truncated:
        print(f"(truncated to --max-targets={args.max_targets}; more targets were resolved)")
    print()
    for result in results:
        header = f"--- TARGET: {result['file']} :: {result['func']} ---"
        print(header)
        if "error" in result:
            print(f"Severity: {severity.severity_label(result['rank'])}")
            print(f"Error: {result['error']}")
        else:
            print(f"Severity: {result['label']}")
            print(result["output"])
        print()


if __name__ == "__main__":
    main()
