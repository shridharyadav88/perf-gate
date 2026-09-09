"""Tests for profilers/concurrency.py (W5: tier-aware concurrency gate)."""

from __future__ import annotations

import importlib.resources
import importlib.util
import json
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


conc = _load_module(
    "_test_concurrency", "code-optimizer/scripts/profilers/concurrency.py"
)


class TestGilResurrection:
    def test_missing_gil_probe_fails_closed(self):
        # This interpreter (pre-3.13) has no sys._is_gil_enabled: on an FT
        # build that would mean something is wrong, so fail, don't pass.
        assert not hasattr(sys, "_is_gil_enabled")
        finding = conc.audit_gil_resurrection("enhanced")
        assert finding.status == "fail"
        assert "SCALING_LOCKED" in finding.detail
        assert finding.check == "gil_resurrection"

    def test_gil_enabled_means_resurrection(self, monkeypatch):
        monkeypatch.setattr(sys, "_is_gil_enabled", lambda: True, raising=False)
        finding = conc.audit_gil_resurrection("enhanced")
        assert finding.status == "fail"

    def test_gil_disabled_means_true_parallel(self, monkeypatch):
        monkeypatch.setattr(sys, "_is_gil_enabled", lambda: False, raising=False)
        finding = conc.audit_gil_resurrection("enhanced")
        assert finding.status == "pass"
        assert "TRUE_PARALLEL" in finding.detail

    def test_result_is_finding_envelope(self, monkeypatch):
        monkeypatch.setattr(sys, "_is_gil_enabled", lambda: False, raising=False)
        finding = conc.audit_gil_resurrection("enhanced")
        assert finding.skipped is False and finding.tier == "enhanced"


class TestBaselineBranch:
    def test_baseline_is_informational_pass(self):
        finding = conc.audit_baseline_concurrency("baseline")
        assert finding.status == "pass"
        assert finding.check == "concurrency_baseline"
        assert "multiprocessing" in finding.detail


class TestDispatch:
    def test_explicit_tier_routes_without_detection(self):
        assert conc.audit_concurrency("baseline").check == "concurrency_baseline"

    def test_live_detection_routes(self):
        # This machine is a GIL build -> baseline branch, no warning expected.
        finding = conc.audit_concurrency()
        assert finding.tier == "baseline"
        assert finding.check == "concurrency_baseline"


class TestExitCodes:
    def test_pass_exits_zero(self, capsys):
        conc.main([])
        assert capsys.readouterr().out.startswith("[PASS]")

    def test_fail_exits_one(self, monkeypatch, capsys):
        monkeypatch.setattr(
            conc, "audit_concurrency",
            lambda: conc.Finding(
                tier="enhanced", check="gil_resurrection", status="fail", detail="x"
            ),
        )
        with pytest.raises(SystemExit) as exc_info:
            conc.main([])
        assert exc_info.value.code == 1

    def test_harness_error_exits_two(self, monkeypatch, capsys):
        def _boom():
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(conc, "audit_concurrency", _boom)
        with pytest.raises(SystemExit) as exc_info:
            conc.main([])
        assert exc_info.value.code == 2
        assert "Error" in capsys.readouterr().err

    def test_json_output_parses(self, capsys):
        conc.main(["--json"])
        row = json.loads(capsys.readouterr().out)
        assert row["check"] == "concurrency_baseline"
        assert set(row) == {
            "tier", "check", "status", "skipped", "detail", "file", "line",
            "fingerprint",
        }
