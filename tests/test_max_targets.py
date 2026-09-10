"""Tests for --max-targets 0 (no count cap) across the three scanning CLIs."""

from __future__ import annotations

import csv
import importlib.resources
import importlib.util
import io
import sys


def _load_module(name: str, rel_path: str):
    pkg_skills = importlib.resources.files("perf_gate") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


big_o_mod = _load_module(
    "_test_mt_big_o", "perf-gate/scripts/profilers/run_big_o.py"
)
line_mod = _load_module(
    "_test_mt_line", "perf-gate/scripts/profilers/run_line_profile.py"
)
gen_mod = _load_module("_test_mt_gen", "perf-gate/scripts/generate_baseline_csv.py")


def _three_func_module(tmp_path):
    mod = tmp_path / "mod.py"
    mod.write_text(
        "def fa(xs):\n    return [x for x in xs]\n"
        "def fb(xs):\n    return [x for x in xs]\n"
        "def fc(xs):\n    return [x for x in xs]\n"
    )
    return mod


class TestZeroDisablesCap:
    def test_big_o_cap_one_truncates(self, tmp_path, capsys):
        mod = _three_func_module(tmp_path)
        big_o_mod.main([
            "--target-type", "file", "--file", str(mod), "--max-targets", "1",
            "--min-n", "10", "--max-n", "30", "--n-measures", "2",
        ])
        out = capsys.readouterr().out
        assert out.count("--- TARGET:") == 1
        assert "truncated to --max-targets=1" in out

    def test_big_o_zero_covers_all(self, tmp_path, capsys):
        mod = _three_func_module(tmp_path)
        big_o_mod.main([
            "--target-type", "file", "--file", str(mod), "--max-targets", "0",
            "--min-n", "10", "--max-n", "30", "--n-measures", "2",
        ])
        out = capsys.readouterr().out
        assert out.count("--- TARGET:") == 3
        assert "truncated" not in out

    def test_line_profile_zero_covers_all(self, tmp_path, capsys):
        mod = _three_func_module(tmp_path)
        line_mod.main([
            "--target-type", "file", "--file", str(mod), "--max-targets", "0",
        ])
        out = capsys.readouterr().out
        assert out.count("--- TARGET:") == 3
        assert "truncated" not in out

    def test_csv_zero_covers_all_rows(self, tmp_path, capsys):
        mod = _three_func_module(tmp_path)
        gen_mod.main([
            "--target-type", "file", "--file", str(mod), "--max-targets", "0",
            "--min-n", "10", "--max-n", "30", "--n-measures", "2",
        ])
        captured = capsys.readouterr()
        body = "\n".join(
            line for line in captured.out.splitlines() if not line.startswith("#")
        )
        rows = list(csv.DictReader(io.StringIO(body)))
        assert len(rows) == 3
        assert "truncated" not in captured.err
