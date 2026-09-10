"""Tests for tier_gate.py (W1: tier detection, requires_tier, Finding).

Loaded via importlib by file path like the other bundled scripts -- the
parent directory, ``perf-gate``, contains a hyphen so it can't be an
importable package.
"""

from __future__ import annotations

import importlib.resources
import importlib.util
import sys
import sysconfig
import warnings

import pytest


def _load_module(name: str, rel_path: str):
    pkg_skills = importlib.resources.files("perf_gate") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module("_test_tier_gate", "perf-gate/scripts/tier_gate.py")


def _mock_runtime(monkeypatch, version, gil_disabled):
    """Fake sys.version_info and the Py_GIL_DISABLED build flag."""
    monkeypatch.setattr(sys, "version_info", (version[0], version[1], 0, "final", 0))

    def fake_get_config_var(name, *args, **kwargs):
        if name == "Py_GIL_DISABLED":
            return gil_disabled
        return orig_get_config_var(name, *args, **kwargs)

    orig_get_config_var = sysconfig.get_config_var
    monkeypatch.setattr(sysconfig, "get_config_var", fake_get_config_var)


@pytest.mark.parametrize(
    "version,gil_disabled,expected",
    [
        ((3, 9), 0, "baseline"),  # floor: supported, version-agnostic form
        ((3, 8), 0, "baseline"),  # below floor: degrades, never raises (P1)
        ((3, 11), 0, "baseline"),
        ((3, 12), 0, "baseline"),
        ((3, 14), 0, "baseline"),  # GIL-enabled 3.14 stays baseline
        ((3, 14), None, "baseline"),  # flag absent on non-FT builds
        ((3, 13), 1, "enhanced"),  # experimental free-threaded 3.13t
        ((3, 14), 1, "enhanced"),
        ((3, 15), 1, "enhanced"),
        # A FT flag on a version predating free-threaded builds cannot occur
        # in practice; the floor guard keeps it baseline:
        ((3, 12), 1, "baseline"),
    ],
)
def test_detect_python_tier_matrix(monkeypatch, version, gil_disabled, expected):
    _mock_runtime(monkeypatch, version, gil_disabled)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert gate.detect_python_tier() == expected


def test_enhanced_emits_runtime_warning_once_per_call_site(monkeypatch):
    _mock_runtime(monkeypatch, (3, 14), 1)
    with pytest.warns(RuntimeWarning, match="free-threaded build"):
        assert gate.detect_python_tier() == "enhanced"


def test_baseline_emits_no_warning(monkeypatch):
    _mock_runtime(monkeypatch, (3, 12), 0)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert gate.detect_python_tier() == "baseline"
    assert [w for w in caught if issubclass(w.category, RuntimeWarning)] == []


def test_detect_survives_warnings_as_errors(monkeypatch):
    # P1: `python -W error` / pytest `filterwarnings = error` must not turn
    # tier detection into a crash.
    _mock_runtime(monkeypatch, (3, 14), 1)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert gate.detect_python_tier() == "enhanced"


def test_requires_tier_skips_off_tier_both_directions():
    @gate.requires_tier("enhanced")
    def enhanced_only(active_tier=None):
        return ("ran", active_tier)

    @gate.requires_tier("baseline")
    def baseline_only(active_tier=None):
        return ("ran", active_tier)

    skipped = enhanced_only(active_tier="baseline")
    assert isinstance(skipped, gate.Finding)
    assert skipped.skipped is True and skipped.status == "skipped"
    assert skipped.tier == "baseline"
    assert skipped.check == "enhanced_only"

    skipped = baseline_only(active_tier="enhanced")
    assert skipped.skipped is True and skipped.tier == "enhanced"

    assert enhanced_only(active_tier="enhanced") == ("ran", "enhanced")
    assert baseline_only(active_tier="baseline") == ("ran", "baseline")


def test_requires_tier_detects_live_tier(monkeypatch):
    _mock_runtime(monkeypatch, (3, 12), 0)

    @gate.requires_tier("baseline")
    def probe(active_tier=None):
        return active_tier

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert probe() == "baseline"


def test_finding_to_csv_row_shape():
    finding = gate.Finding(tier="enhanced", check="gil_resurrection", status="fail", detail="x")
    row = finding.to_csv_row()
    assert row == {
        "tier": "enhanced",
        "check": "gil_resurrection",
        "status": "fail",
        "skipped": False,
        "detail": "x",
        "file": None,
        "line": None,
        "fingerprint": finding.fingerprint,
    }
