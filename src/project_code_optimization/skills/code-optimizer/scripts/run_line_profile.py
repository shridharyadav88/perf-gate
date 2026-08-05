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
import sys
from collections.abc import Callable, Iterable
from typing import Any


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
    except Exception:
        # Graceful degradation — try once more with a string argument as a
        # broad fallback, mirroring the spec's intent.
        try:
            profiled("x" * 200)
        except Exception as exc:
            return (
                "=== LINE PROFILER OUTPUT ===\n"
                f"Warning: Synthetic test run failed: {exc}\n"
                "No profile data collected.\n"
            )

    stream = io.StringIO()
    lp.print_stats(stream=stream)
    return "=== LINE PROFILER OUTPUT ===\n" + stream.getvalue()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run line-by-line operation hit profiling.",
    )
    parser.add_argument("--file", required=True, help="Path to Python file.")
    parser.add_argument("--func", required=True, help="Target function name.")
    parser.add_argument(
        "--call-args",
        type=str,
        default=None,
        help="JSON array of positional arguments to pass to the function.",
    )
    args = parser.parse_args(argv)

    # -- Load function -------------------------------------------------------
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


if __name__ == "__main__":
    main()
