"""Tests for regression_gate.py (classified-CSV regression gate)."""

from __future__ import annotations

import importlib.resources
import importlib.util
import sys

import pytest


def _load_module(name: str, rel_path: str):
    pkg_skills = importlib.resources.files("project_code_optimization") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gate_mod = _load_module("_test_gate", "code-optimizer/scripts/regression_gate.py")

COLS = ["file", "function", "empirical_big_o", "complexity_rank",
        "total_hotspot_time_us", "tier", "tier_detail"]


def _row(**overrides):
    row = {"file": "a.py", "function": "f", "empirical_big_o": "Linear",
           "complexity_rank": "2", "total_hotspot_time_us": "100.0",
           "tier": "tier1_review", "tier_detail": ""}
    row.update(overrides)
    return row


def _write(tmp_path, name, rows):
    import csv
    path = tmp_path / name
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write("# tool_version=0.1.0\n# schema_version=1\n")
        writer = csv.DictWriter(f, fieldnames=COLS)
        writer.writeheader()
        writer.writerows(rows)
    return str(path)


class TestCompare:
    def test_identical_passes(self):
        result = gate_mod.compare([_row()], [_row()])
        assert result == {"failures": [], "advisories": [],
                          "improvements": []}

    def test_rank_increase_fails(self):
        result = gate_mod.compare([_row()], [_row(complexity_rank="5")])
        assert len(result["failures"]) == 1
        assert "2 -> 5" in result["failures"][0]

    def test_rank_decrease_is_improvement(self):
        result = gate_mod.compare([_row(complexity_rank="5")], [_row()])
        assert result["failures"] == []
        assert len(result["improvements"]) == 1

    def test_unknown_baseline_never_fails(self):
        result = gate_mod.compare([_row(complexity_rank="-1")],
                                  [_row(complexity_rank="5")])
        assert result["failures"] == []

    def test_new_actionable_row_fails(self):
        rows = [_row(), _row(function="g", tier="tier1_review")]
        new = [_row(), _row(function="g", tier="tier0_str_join")]
        result = gate_mod.compare(rows, new)
        assert any("newly tier0_str_join" in f or
                   "entered tier0_str_join" in f for f in result["failures"])

    def test_resolved_row_is_improvement(self):
        result = gate_mod.compare([_row(), _row(function="g")], [_row()])
        assert result["improvements"] == ["a.py :: g no longer reported"]

    def test_time_drift_is_advisory_by_default(self):
        result = gate_mod.compare(
            [_row()], [_row(total_hotspot_time_us="150.0")], margin=0.10)
        assert result["failures"] == []
        assert len(result["advisories"]) == 1
        assert "+50%" in result["advisories"][0]

    def test_small_drift_ignored(self):
        result = gate_mod.compare(
            [_row()], [_row(total_hotspot_time_us="105.0")], margin=0.10)
        assert result["advisories"] == []


class TestCli:
    def test_exit_codes(self, tmp_path, capsys):
        base = _write(tmp_path, "base.csv", [_row()])
        same = _write(tmp_path, "same.csv", [_row()])
        gate_mod.main(["--baseline", base, "--current", same])
        assert "No blocking regressions." in capsys.readouterr().out
        worse = _write(tmp_path, "worse.csv", [_row(complexity_rank="6")])
        with pytest.raises(SystemExit) as exc:
            gate_mod.main(["--baseline", base, "--current", worse])
        assert exc.value.code == 1

    def test_strict_time_promotes_advisory(self, tmp_path, capsys):
        base = _write(tmp_path, "base.csv", [_row()])
        slow = _write(tmp_path, "slow.csv",
                       [_row(total_hotspot_time_us="300.0")])
        gate_mod.main(["--baseline", base, "--current", slow])
        assert "ADVISORY" in capsys.readouterr().out
        with pytest.raises(SystemExit) as exc:
            gate_mod.main(["--baseline", base, "--current", slow,
                           "--strict-time"])
        assert exc.value.code == 1
        assert "FAIL" in capsys.readouterr().out

    def test_missing_tier_column_exits_two(self, tmp_path):
        bad = tmp_path / "bad.csv"
        bad.write_text("file,function\n")
        with pytest.raises(SystemExit) as exc:
            gate_mod.main(["--baseline", str(bad),
                           "--current", str(bad)])
        assert exc.value.code == 2

    def test_missing_file_exits_two(self, tmp_path):
        with pytest.raises(SystemExit) as exc:
            gate_mod.main(["--baseline", str(tmp_path / "no.csv"),
                           "--current", str(tmp_path / "no.csv")])
        assert exc.value.code == 2
