"""Tests for the profiling scripts bundled under the ``perf-gate`` skill.

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
    pkg_skills = importlib.resources.files("perf_gate") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_big_o_mod = _load_module(
    "_test_big_o", "perf-gate/scripts/profilers/run_big_o.py"
)
_line_mod = _load_module(
    "_test_line_profile", "perf-gate/scripts/profilers/run_line_profile.py"
)


profile_big_o = _big_o_mod.profile_big_o
format_output = _big_o_mod.format_output
load_module = _big_o_mod.load_module

run_line_profile = _line_mod.run_line_profile
synthesize_arguments = _line_mod.synthesize_arguments
load_function = _line_mod.load_function
compute_line_stats = _line_mod.compute_line_stats
measure_verdict_us = _line_mod.measure_verdict_us

big_o_main = _big_o_mod.main
line_profile_main = _line_mod.main


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


# ---------------------------------------------------------------------------
# compute_line_stats (structured hotspot data used by multi-target scans)
# ---------------------------------------------------------------------------

class TestComputeLineStats:
    def test_returns_formatted_and_hotspots(self, linear_func_file):
        func = load_function(str(linear_func_file), "f")
        stats = compute_line_stats(func)
        assert "=== LINE PROFILER OUTPUT ===" in stats["formatted"]
        assert stats["total_hits"] > 0
        assert len(stats["hotspots"]) > 0
        top = stats["hotspots"][0]
        assert {"line", "hits", "time_us", "pct_time", "source"} <= top.keys()

    def test_hotspots_sorted_by_time_descending(self, linear_func_file):
        func = load_function(str(linear_func_file), "f")
        stats = compute_line_stats(func)
        times = [h["time_us"] for h in stats["hotspots"]]
        assert times == sorted(times, reverse=True)

    def test_failure_reports_error_and_no_hotspots(self, raising_func_file):
        func = load_function(str(raising_func_file), "crash")
        stats = compute_line_stats(func)
        assert stats["hotspots"] == []
        assert stats["total_hits"] == 0
        assert "error" in stats
        assert "=== LINE PROFILER OUTPUT ===" in stats["formatted"]


class TestMeasureVerdict:
    def test_returns_plain_call_microseconds(self, linear_func_file):
        func = load_function(str(linear_func_file), "f")
        value = measure_verdict_us(func, timeout=60.0)
        assert isinstance(value, float)
        assert 0.0 <= value < 60.0 * 1e6

    def test_takes_minimum_across_repeats(self, linear_func_file, monkeypatch):
        # Scripted clock: three repeats of 50ms, 20ms, 90ms -- the
        # verdict must be the 20ms minimum, proving noise rejection.
        ticks = iter([100.0, 100.05, 100.05, 100.07, 100.07, 100.16])
        monkeypatch.setattr(
            _line_mod.time, "perf_counter", lambda: next(ticks))
        func = load_function(str(linear_func_file), "f")
        assert measure_verdict_us(func, timeout=60.0,
                                  repeats=3) == pytest.approx(20000.0)

    def test_unrunnable_callable_raises(self, raising_func_file):
        func = load_function(str(raising_func_file), "crash")
        with pytest.raises(RuntimeError):
            measure_verdict_us(func, timeout=60.0, repeats=1)


# ---------------------------------------------------------------------------
# Multi-target CLI (--target-type file/files/commit/repo)
# ---------------------------------------------------------------------------

class TestBigOMultiTarget:
    def test_target_type_file_profiles_every_function(self, tmp_path: Path, capsys):
        mod = tmp_path / "mod.py"
        mod.write_text(
            "def linear(xs):\n"
            "    return [x for x in xs]\n"
            "\n"
            "def quad(xs):\n"
            "    return [x for x in xs for y in xs]\n"
        )
        big_o_main([
            "--target-type", "file", "--file", str(mod),
            "--min-n", "50", "--max-n", "5000", "--n-measures", "6",
        ])
        out = capsys.readouterr().out
        assert "BIG-O BASELINE SCAN (file)" in out
        assert "linear" in out
        assert "quad" in out
        assert "Severity:" in out
        # The quadratic function must be reported ahead of the linear one.
        assert out.index("quad") < out.index("linear")

    def test_target_type_file_missing_raises_exit_1(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            big_o_main(["--target-type", "file", "--file", "/nonexistent/mod.py"])
        assert excinfo.value.code == 1
        assert "Error:" in capsys.readouterr().err

    def test_function_target_missing_args_exit_2(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            big_o_main(["--target-type", "function"])
        assert excinfo.value.code == 2
        assert "requires --file and --func" in capsys.readouterr().err

    def test_target_with_broken_function_reports_error_not_abort(self, tmp_path: Path, capsys):
        mod = tmp_path / "mod.py"
        mod.write_text(
            "def ok_func(xs):\n"
            "    return len(xs)\n"
            "\n"
            "def crash_func(xs):\n"
            "    raise RuntimeError('boom')\n"
        )
        big_o_main([
            "--target-type", "file", "--file", str(mod),
            "--min-n", "20", "--max-n", "200", "--n-measures", "4",
        ])
        out = capsys.readouterr().out
        assert "crash_func" in out
        assert "ok_func" in out
        assert "Error: Big-O profiling failed" in out

    def test_target_with_broken_import_reports_error_not_abort(self, tmp_path: Path, capsys):
        # A relative import with no package context (a real failure mode for
        # files loaded standalone from a larger package) must not crash the
        # whole scan — it must be reported and the run must continue.
        mod = tmp_path / "mod.py"
        mod.write_text(
            "from . import nonexistent_sibling\n"
            "\n"
            "def unreachable(xs):\n"
            "    return xs\n"
        )
        big_o_main(["--target-type", "file", "--file", str(mod)])
        out = capsys.readouterr().out
        assert "unreachable" in out
        assert "Error:" in out
        assert "Severity: Unknown" in out


class TestLineProfileMultiTarget:
    def test_target_type_file_profiles_every_function(self, tmp_path: Path, capsys):
        mod = tmp_path / "mod.py"
        mod.write_text(
            "def f(xs):\n"
            "    return [x for x in xs]\n"
            "\n"
            "def g(xs):\n"
            "    return [x for x in xs]\n"
        )
        line_profile_main(["--target-type", "file", "--file", str(mod)])
        out = capsys.readouterr().out
        assert "LINE PROFILE BASELINE SCAN (file)" in out
        assert "f" in out and "g" in out
        assert "Top hotspot:" in out

    def test_target_type_file_missing_raises_exit_1(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            line_profile_main(["--target-type", "file", "--file", "/nonexistent/mod.py"])
        assert excinfo.value.code == 1
        assert "Error:" in capsys.readouterr().err

    def test_function_target_missing_args_exit_2(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            line_profile_main(["--target-type", "function"])
        assert excinfo.value.code == 2
        assert "requires --file and --func" in capsys.readouterr().err

    def test_target_with_broken_import_reports_error_not_abort(self, tmp_path: Path, capsys):
        mod = tmp_path / "mod.py"
        mod.write_text(
            "from . import nonexistent_sibling\n"
            "\n"
            "def unreachable(xs):\n"
            "    return xs\n"
        )
        line_profile_main(["--target-type", "file", "--file", str(mod)])
        out = capsys.readouterr().out
        assert "unreachable" in out
        assert "Error:" in out


class TestExecuteProfiledRobustness:
    def test_unhashable_args_with_range_do_not_crash(self):
        # A synthesized container arg (list) next to a range arg used to
        # abort the candidate-shape search with TypeError: unhashable
        # type -- killing the whole sweep instead of one row (crawl4ai v5).
        def f(items, n):
            return len(items) + n

        lp, error = _line_mod._execute_profiled(f, ([1, 2], range(4)), {}, 30)
        assert error is None
        assert lp is not None

    def test_click_command_reports_by_design_not_junk_probe(self):
        # Click Command/Group objects (found by their module-level def
        # name) are argv-driven entry points: report the by-design lane
        # instead of probing junk argv shapes (real noise on crawl4ai
        # cli.py: "<Command list> (*args bundle: 'int' object is not
        # iterable)"). Duck-typed so the test needs no click install.
        class FakeCommand:
            name = 'serve'
            callback = staticmethod(lambda: None)
            params = []

        with pytest.raises(ValueError, match='CLI command'):
            _big_o_mod._build_adapter(FakeCommand())
