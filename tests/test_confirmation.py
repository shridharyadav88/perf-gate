"""Tests for scripts/confirmation.py and classifier hysteresis (W9, P7)."""

from __future__ import annotations

import importlib.resources
import importlib.util
import sys


def _load_module(name: str, rel_path: str):
    pkg_skills = importlib.resources.files("project_code_optimization") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


conf = _load_module("_test_confirmation", "code-optimizer/scripts/confirmation.py")
clf = _load_module("_test_conf_classify", "code-optimizer/scripts/classify_findings.py")


class TestTier2Confirmed:
    def test_low_ranks_never_escalate(self):
        for rank in (-1, 0, 1, 2, 3):
            assert conf.tier2_confirmed(rank, 1) is False
            assert conf.tier2_confirmed(rank, 5) is False

    def test_rank4_needs_two_confirmations(self):
        assert conf.tier2_confirmed(4, 1) is False
        assert conf.tier2_confirmed(4, 2) is True
        assert conf.tier2_confirmed(4, 3) is True

    def test_rank5_plus_escalates_immediately(self):
        for rank in (5, 6):
            assert conf.tier2_confirmed(rank, 1) is True


class TestDecideKeep:
    def test_clear_improvement_keeps(self):
        assert conf.decide_keep(1000.0, 800.0) is True

    def test_noise_level_improvement_rejects(self):
        assert conf.decide_keep(1000.0, 980.0) is False  # 2% is noise

    def test_exactly_at_margin_rejects(self):
        assert conf.decide_keep(1000.0, 900.0) is False  # burden of proof on rewrite

    def test_regression_rejects(self):
        assert conf.decide_keep(1000.0, 1200.0) is False

    def test_no_change_rejects(self):
        assert conf.decide_keep(1000.0, 1000.0) is False

    def test_zero_baseline_rejects(self):
        assert conf.decide_keep(0.0, 0.0) is False
        assert conf.decide_keep(-1.0, -2.0) is False

    def test_margin_is_configurable(self):
        assert conf.decide_keep(1000.0, 980.0, min_improvement=0.01) is True


def _row(tmp_path, **overrides):
    mod = tmp_path / "mod.py"
    mod.write_text("def f(xs):\n    return [x for x in xs]\n")
    row = {
        "file": str(mod), "function": "f", "empirical_big_o": "Quadratic",
        "complexity_rank": "4", "big_o_error": "", "total_hits": "",
        "total_hotspot_time_us": "", "top_hotspot_pct_time": "",
        "top_hotspot_line": "", "top_hotspot_source": "",
        "line_profile_error": "", "tier": "", "tier_detail": "",
    }
    row.update(overrides)
    return row


class TestClassifierHysteresis:
    def test_single_reading_rank4_goes_to_review_not_tier2(self, tmp_path):
        row = _row(tmp_path)
        clf.classify_rows([row])
        assert row["tier"] == "tier1_review"
        assert "unconfirmed" in row["tier_detail"]

    def test_flapping_fit_does_not_escalate(self, tmp_path):
        # Alternating Linear/Quadratic fits across runs: each single reading
        # stays out of the algorithmic lane.
        rows = [
            _row(tmp_path, empirical_big_o="Linear", complexity_rank="2"),
            _row(tmp_path, empirical_big_o="Quadratic", complexity_rank="4"),
        ]
        clf.classify_rows(rows)
        assert all(r["tier"] != "tier2_algorithmic" for r in rows)

    def test_confirmed_rank4_escalates(self, tmp_path):
        row = _row(tmp_path, confirmations="2")
        clf.classify_rows([row])
        assert row["tier"] == "tier2_algorithmic"

    def test_high_rank_escalates_without_confirmations(self, tmp_path):
        row = _row(tmp_path, empirical_big_o="Cubic", complexity_rank="5")
        clf.classify_rows([row])
        assert row["tier"] == "tier2_algorithmic"

    def test_garbage_confirmations_default_to_one(self, tmp_path):
        row = _row(tmp_path, confirmations="junk")
        clf.classify_rows([row])
        assert row["tier"] == "tier1_review"
