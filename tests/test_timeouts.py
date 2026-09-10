"""Tests for W2 execution containment (timeouts turn hangs into error rows).

Loaded via importlib by file path like the other bundled scripts -- the
parent directory, ``perf-gate``, contains a hyphen so it can't be an
importable package.
"""

from __future__ import annotations

import importlib.resources
import importlib.util
import sys
import textwrap
import time

import pytest


def _load_module(name: str, rel_path: str):
    pkg_skills = importlib.resources.files("perf_gate") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


timeouts_mod = _load_module(
    "_test_to_timeouts", "perf-gate/scripts/profilers/timeouts.py"
)
big_o_mod = _load_module(
    "_test_to_big_o", "perf-gate/scripts/profilers/run_big_o.py"
)
line_mod = _load_module(
    "_test_to_line", "perf-gate/scripts/profilers/run_line_profile.py"
)
gen_mod = _load_module("_test_to_gen", "perf-gate/scripts/generate_baseline_csv.py")


def _hang_forever():
    time.sleep(60)


class TestRunWithTimeout:
    def test_none_timeout_runs_inline(self):
        assert timeouts_mod.run_with_timeout(lambda: 42, None) == 42

    def test_nonpositive_timeout_runs_inline(self):
        assert timeouts_mod.run_with_timeout(lambda: 42, 0) == 42

    def test_fast_call_returns_result(self):
        assert timeouts_mod.run_with_timeout(lambda: "ok", 5.0) == "ok"

    def test_hang_raises_timeout_budget_exceeded(self):
        with pytest.raises(timeouts_mod.TimeoutBudgetExceeded, match="Timed out after"):
            timeouts_mod.run_with_timeout(_hang_forever, 0.2)

    def test_timeout_is_timeout_error_subclass(self):
        assert issubclass(timeouts_mod.TimeoutBudgetExceeded, TimeoutError)

    def test_worker_exception_propagates(self):
        def _boom():
            raise ValueError("target blew up")

        with pytest.raises(ValueError, match="target blew up"):
            timeouts_mod.run_with_timeout(_boom, 5.0)


class TestBigOTimeout:
    def test_hanging_target_raises_timeout(self):
        def slow(data):
            time.sleep(30)
            return len(data)

        with pytest.raises(TimeoutError):
            big_o_mod.profile_big_o(
                slow, min_n=10, max_n=20, n_measures=2, n_timings=1, timeout=0.3
            )

    def test_fast_target_unaffected_by_timeout(self):
        best_fit, _ = big_o_mod.profile_big_o(
            lambda data: sum(data), min_n=10, max_n=40, n_measures=3, timeout=60.0
        )
        assert isinstance(best_fit, str) and best_fit


class TestLineProfileTimeout:
    def test_hanging_target_reports_error_not_hang(self):
        def slow(n):
            time.sleep(30)
            return n

        started = time.monotonic()
        stats = line_mod.compute_line_stats(slow, timeout=0.5)
        elapsed = time.monotonic() - started
        assert elapsed < 20
        assert stats.get("error") and "Timed out" in stats["error"]
        assert stats["hotspots"] == []

    def test_fast_target_unaffected_by_timeout(self):
        def quick(n):
            return sum(n)

        stats = line_mod.compute_line_stats(quick, timeout=60.0)
        assert not stats.get("error")
        assert stats["total_hits"] > 0


class TestScanCompletesDespiteHang:
    def test_csv_scan_reports_hang_as_row_and_finishes(self, tmp_path, capsys):
        target = tmp_path / "mixed_targets.py"
        target.write_text(
            textwrap.dedent(
                """\
                import time

                def quick(data):
                    return sum(data)

                def hanging(data):
                    time.sleep(30)
                    return len(data)
                """
            )
        )
        started = time.monotonic()
        gen_mod.main([
            "--target-type", "file", "--file", str(target),
            "--min-n", "10", "--max-n", "20", "--n-measures", "2",
            "--n-timings", "1", "--timeout", "1",
        ])
        elapsed = time.monotonic() - started
        out = capsys.readouterr().out
        assert "quick" in out and "hanging" in out
        assert "Timed out" in out
        # Two 1s budgets for the hanging row; the run must not hang.
        assert elapsed < 60
