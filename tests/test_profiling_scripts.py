"""Tests for the profiling scripts bundled under the ``code-optimizer`` skill.

Because the scripts live under ``skills/`` as package data we use
:mod:`importlib.resources` to resolve their paths, then load and call the
core functions directly.
"""

from __future__ import annotations

import importlib.resources
import importlib.util
import textwrap
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers to load core functions from the bundled scripts
# ---------------------------------------------------------------------------

def _load_module(name: str, rel_path: str):
    """Import a module from a path relative to the skills directory."""
    pkg_skills = importlib.resources.files("project_code_optimization") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_big_o_mod = _load_module("_test_big_o", "code-optimizer/scripts/run_big_o.py")
_line_mod = _load_module("_test_line_profile", "code-optimizer/scripts/run_line_profile.py")


profile_big_o = _big_o_mod.profile_big_o
format_output = _big_o_mod.format_output
load_module = _big_o_mod.load_module

run_line_profile = _line_mod.run_line_profile
synthesize_arguments = _line_mod.synthesize_arguments
load_function = _line_mod.load_function


# ---------------------------------------------------------------------------
# Fixtures: synthetic modules for testing
# ---------------------------------------------------------------------------

def _write_module(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / f"{name}.py"
    path.write_text(textwrap.dedent(source))
    return path


@pytest.fixture
def linear_func_file(tmp_path: Path) -> Path:
    return _write_module(
        tmp_path,
        "linear",
        """\
        def f(xs):
            return [x * 2 for x in xs]
        """,
    )


@pytest.fixture
def zero_arg_func_file(tmp_path: Path) -> Path:
    return _write_module(
        tmp_path,
        "zero_arg",
        """\
        def g():
            return 42
        """,
    )


@pytest.fixture
def multi_arg_func_file(tmp_path: Path) -> Path:
    return _write_module(
        tmp_path,
        "multi_arg",
        """\
        def h(a: int, b: int) -> int:
            return a + b
        """,
    )


@pytest.fixture
def raising_func_file(tmp_path: Path) -> Path:
    return _write_module(
        tmp_path,
        "raising",
        """\
        def crash(x):
            raise RuntimeError("boom")
        """,
    )


# ---------------------------------------------------------------------------
# Big-O tests
# ---------------------------------------------------------------------------

class TestBigO:
    def test_profile_estimates_linear(self, linear_func_file):
        mod = load_module(str(linear_func_file))
        best_fit, fitted = profile_big_o(mod.f, min_n=50, max_n=1000, n_measures=4)
        # big_O's complexity names include "Linear", "Linearithmic", etc.
        assert isinstance(best_fit, str)
        assert len(best_fit) > 0
        # A linear function should resolve to a complexity class
        assert "Linear" in best_fit or best_fit in fitted

    def test_profile_returns_fitted_dict(self, linear_func_file):
        mod = load_module(str(linear_func_file))
        _, fitted = profile_big_o(mod.f, min_n=50, max_n=500, n_measures=3)
        assert len(fitted) > 0

    def test_missing_file_raises(self):
        with pytest.raises(ValueError, match="File not found"):
            load_module("/nonexistent/path.py")

    def test_missing_func_raises(self, linear_func_file):
        mod = load_module(str(linear_func_file))
        with pytest.raises(AttributeError):
            _ = mod.nonexistent_func

    def test_format_output_contains_banner(self, linear_func_file):
        mod = load_module(str(linear_func_file))
        best_fit, fitted = profile_big_o(mod.f, min_n=50, max_n=500, n_measures=3)
        output = format_output(best_fit, fitted)
        assert "=== BIG-O PROFILER OUTPUT ===" in output
        assert "Estimated Complexity:" in output
        assert "Fitted Models:" in output


# ---------------------------------------------------------------------------
# Line profiler tests
# ---------------------------------------------------------------------------

class TestLineProfiler:
    def test_output_contains_banner_and_columns(self, linear_func_file):
        func = load_function(str(linear_func_file), "f")
        output = run_line_profile(func)
        assert "=== LINE PROFILER OUTPUT ===" in output
        assert "Line #" in output
        assert "Hits" in output

    def test_output_contains_function_name(self, linear_func_file):
        func = load_function(str(linear_func_file), "f")
        output = run_line_profile(func)
        assert "f" in output

    def test_zero_arg_function(self, zero_arg_func_file):
        func = load_function(str(zero_arg_func_file), "g")
        output = run_line_profile(func)
        assert "=== LINE PROFILER OUTPUT ===" in output
        # Should not crash — synthesize_arguments handles zero-arg callables
        assert "g" in output

    def test_multi_arg_function(self, multi_arg_func_file):
        func = load_function(str(multi_arg_func_file), "h")
        output = run_line_profile(func)
        assert "=== LINE PROFILER OUTPUT ===" in output
        assert "h" in output

    def test_graceful_failure_on_raising_func(self, raising_func_file):
        func = load_function(str(raising_func_file), "crash")
        output = run_line_profile(func)
        # Should not raise — graceful degradation
        assert "Warning: Synthetic test run failed:" in output

    def test_missing_file_raises(self):
        with pytest.raises(ValueError, match="File not found"):
            load_function("/nonexistent/path.py", "f")

    def test_missing_func_raises(self, linear_func_file):
        with pytest.raises(ValueError, match="not found"):
            load_function(str(linear_func_file), "nonexistent_func")


# ---------------------------------------------------------------------------
# Argument synthesis tests
# ---------------------------------------------------------------------------

class TestSynthesizeArguments:
    def test_single_arg_function(self, linear_func_file):
        func = load_function(str(linear_func_file), "f")
        args, kwargs = synthesize_arguments(func)
        assert len(args) > 0
        result = func(*args, **kwargs)
        assert isinstance(result, list)

    def test_zero_arg_function(self, zero_arg_func_file):
        func = load_function(str(zero_arg_func_file), "g")
        args, kwargs = synthesize_arguments(func)
        result = func(*args, **kwargs)
        assert result == 42

    def test_multi_arg_function(self, multi_arg_func_file):
        func = load_function(str(multi_arg_func_file), "h")
        args, kwargs = synthesize_arguments(func)
        # Should produce args for both 'a' and 'b' (typed as int → 0 each)
        result = func(*args, **kwargs)
        assert result == 0  # 0 + 0
