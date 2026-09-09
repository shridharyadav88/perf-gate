"""Tests for project_code_optimization.auditing (W11: total decorator)."""

from __future__ import annotations

import importlib.resources
import time
import tracemalloc
import warnings

import pytest

from project_code_optimization import auditing


def test_bare_decorator_preserves_behavior_and_metadata():
    @auditing.audit_performance
    def add(a, b):
        """Add."""
        return a + b

    assert add(2, 3) == 5
    assert add.__name__ == "add"


def test_time_violation_warns_and_returns_result():
    @auditing.audit_performance(max_seconds=0.0)
    def slow():
        time.sleep(0.01)
        return "done"

    with pytest.warns(RuntimeWarning, match="over budget"):
        assert slow() == "done"


def test_strict_time_violation_raises():
    @auditing.audit_performance(max_seconds=0.0, strict=True)
    def slow():
        time.sleep(0.01)
        return "done"

    with pytest.raises(auditing.PerformanceViolation, match="over budget"):
        slow()


def test_memory_violation_strict_raises():
    tracemalloc.stop()

    @auditing.audit_performance(max_bytes=1000, strict=True)
    def hog():
        return bytearray(2_000_000)

    with pytest.raises(auditing.PerformanceViolation, match="over budget"):
        hog()


def test_raising_callback_is_contained():
    def _boom(message):
        raise RuntimeError("listener exploded")

    @auditing.audit_performance(max_seconds=0.0, on_violation=_boom)
    def slow():
        time.sleep(0.01)
        return "done"

    assert slow() == "done"  # totality: listener failure never propagates


def test_broken_timer_still_returns_result(monkeypatch):
    def _boom():
        raise RuntimeError("no clock")

    monkeypatch.setattr(auditing.time, "perf_counter", _boom)

    @auditing.audit_performance(max_seconds=1.0)
    def fine():
        return 42

    assert fine() == 42


def test_wrapped_errors_always_propagate():
    @auditing.audit_performance(max_seconds=100.0, strict=True)
    def fails():
        raise ValueError("user code broke")

    with pytest.raises(ValueError, match="user code broke"):
        fails()


def test_disabled_is_exact_passthrough():
    tracemalloc.stop()

    @auditing.audit_performance(max_seconds=0.0, strict=True, enabled=False)
    def slow():
        time.sleep(0.01)
        return "done"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert slow() == "done"
    assert caught == []
    assert not tracemalloc.is_tracing()


def test_concurrency_fail_counts_as_violation(monkeypatch):
    monkeypatch.setattr(
        auditing, "_concurrency_status",
        lambda: ("enhanced", "fail", "SCALING_LOCKED"),
    )

    @auditing.audit_performance(strict=True)
    def fine():
        return 1

    with pytest.raises(auditing.PerformanceViolation, match="SCALING_LOCKED"):
        fine()


def test_concurrency_pass_is_silent_on_baseline():
    @auditing.audit_performance()
    def fine():
        return 1

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert fine() == 1
    assert [w for w in caught if issubclass(w.category, RuntimeWarning)] == []


class TestTrackMemory:
    def test_growth_recorded(self):
        with auditing.track_memory() as mem:
            sink = [bytearray(500_000)]  # noqa: F841 -- held for measurement
        assert mem["before"] is not None
        assert mem["growth"] is not None and mem["growth"] > 0
        assert mem["peak"] is not None and mem["peak"] >= mem["after"]

    def test_disabled_yields_empty_record_without_tracing(self):
        tracemalloc.stop()
        with auditing.track_memory(enabled=False) as mem:
            _ = bytearray(10)
        assert mem == {"before": None, "after": None, "growth": None, "peak": None}
        assert not tracemalloc.is_tracing()

    def test_body_exception_propagates_with_record_filled(self):
        with pytest.raises(ValueError, match="boom"):
            with auditing.track_memory() as mem:
                _ = bytearray(100)
                raise ValueError("boom")
        assert mem["after"] is not None


def test_skill_documents_decorator_contract():
    text = (
        importlib.resources.files("project_code_optimization")
        / "skills"
        / "code-optimizer"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    for token in ("audit_performance", "PerformanceViolation", "enabled=False", "strict=True"):
        assert token in text


def test_skill_documents_probe_learnings():
    text = (
        importlib.resources.files("project_code_optimization")
        / "skills"
        / "code-optimizer"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    # Gap 2: direct-file-load harness recipe for heavy package inits.
    assert "spec_from_file_location" in text
    # Gap 3: line-profiler instruments only the target function itself.
    assert "Only the target function itself is instrumented" in text
