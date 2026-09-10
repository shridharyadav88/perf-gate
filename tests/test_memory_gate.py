"""Tests for profilers/memory.py (W6: tier-aware memory ceiling gate)."""

from __future__ import annotations

import importlib.resources
import importlib.util
import json
import sys

import pytest


def _load_module(name: str, rel_path: str):
    pkg_skills = importlib.resources.files("perf_gate") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mem = _load_module("_test_memory", "perf-gate/scripts/profilers/memory.py")


class TestThresholds:
    def test_universal_default_is_two_percent_on_both_tiers(self):
        for tier in ("baseline", "enhanced"):
            assert mem.memory_ceiling_gate(1000.0, 1015.0, tier).status == "pass"
            assert mem.memory_ceiling_gate(1000.0, 1025.0, tier).status == "fail"

    def test_shrinkage_passes(self):
        assert mem.memory_ceiling_gate(1000.0, 500.0, "baseline").status == "pass"

    def test_zero_baseline_skips(self):
        finding = mem.memory_ceiling_gate(0.0, 100.0, "baseline")
        assert finding.status == "skipped" and finding.skipped is True

    def test_negative_baseline_skips(self):
        finding = mem.memory_ceiling_gate(-5.0, 100.0, "enhanced")
        assert finding.status == "skipped" and finding.skipped is True

    def test_threshold_is_overridable(self):
        assert mem.memory_ceiling_gate(1000.0, 1100.0, "baseline", max_growth=0.2).status == "pass"
        gate = mem.memory_ceiling_gate(1000.0, 1010.0, "enhanced", max_growth=0.005)
        assert gate.status == "fail"

    def test_detail_carries_numbers(self):
        finding = mem.memory_ceiling_gate(1000.0, 1025.0, "baseline")
        assert "growth=0.0250" in finding.detail and "threshold=0.02" in finding.detail


class TestSnapshotHelpers:
    def test_ensure_is_idempotent(self):
        mem.ensure_tracemalloc()
        mem.ensure_tracemalloc()  # must not raise or reset state

    def test_round_trip_measures_real_growth(self):
        before = mem.current_traced_bytes()
        sink = [bytearray(1_000_000)]  # noqa: F841 -- held to defeat the GC
        after = mem.current_traced_bytes()
        assert after > before
        finding = mem.memory_ceiling_gate(float(before), float(after), "baseline")
        assert finding.status == "fail"  # ~1MB over a ~KB baseline


class TestExitCodes:
    def test_pass_exits_zero(self, capsys):
        mem.main(["--baseline", "1000", "--current", "1001"])
        assert capsys.readouterr().out.startswith("[PASS]")

    def test_fail_exits_one(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            mem.main(["--baseline", "1000", "--current", "2000"])
        assert exc_info.value.code == 1

    def test_skipped_exits_zero(self, capsys):
        mem.main(["--baseline", "0", "--current", "2000"])
        assert capsys.readouterr().out.startswith("[SKIPPED]")

    def test_harness_error_exits_two(self, monkeypatch, capsys):
        def _boom(*args, **kwargs):
            raise RuntimeError("gate exploded")

        monkeypatch.setattr(mem, "memory_ceiling_gate", _boom)
        with pytest.raises(SystemExit) as exc_info:
            mem.main(["--baseline", "1000", "--current", "2000"])
        assert exc_info.value.code == 2
        assert "Error" in capsys.readouterr().err

    def test_json_output_parses(self, capsys):
        mem.main(["--baseline", "1000", "--current", "1001", "--json"])
        row = json.loads(capsys.readouterr().out)
        assert row["check"] == "memory_ceiling" and row["status"] == "pass"
        assert "fingerprint" in row
