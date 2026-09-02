"""Tests for severity.py — the Big-O-to-severity ranking shared across the skill."""

from __future__ import annotations

import importlib.resources
import importlib.util

import pytest


def _load_module(name: str, rel_path: str):
    pkg_skills = importlib.resources.files("project_code_optimization") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sev = _load_module("_test_severity", "code-optimizer/scripts/severity.py")


class TestComplexityRank:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Exponential: time = 1.2E-05 * 1^n (sec)", 6),
            ("Cubic: time = 8.1E-05 + 1.8E-10*n^3 (sec)", 5),
            ("Quadratic: time = -2.7E-05 + 3.9E-08*n^2 (sec)", 4),
            ("Linearithmic: time = -0.00019 + 1.5E-06*n*log(n) (sec)", 3),
            ("Linear: time = -0.00028 + 8.5E-06*n (sec)", 2),
            ("Logarithmic: time = -0.0018 + 0.00057*log(n) (sec)", 1),
            ("Constant: time = 7.2E-06 (sec)", 0),
        ],
    )
    def test_known_classes(self, name, expected):
        assert sev.complexity_rank(name) == expected

    def test_empty_or_none_is_unknown(self):
        assert sev.complexity_rank("") == sev.UNKNOWN_RANK
        assert sev.complexity_rank(None) == sev.UNKNOWN_RANK

    def test_unrecognized_text_is_unknown(self):
        assert sev.complexity_rank("garbage output") == sev.UNKNOWN_RANK

    def test_case_insensitive(self):
        assert sev.complexity_rank("QUADRATIC") == 4


class TestPolynomialExponent:
    """big_o's Polynomial class fits x^k for any k — it must not be treated
    as flatly worse than Quadratic when k happens to be <= 2."""

    def test_exponent_one_is_linear_equivalent(self):
        # A real result observed in practice: a genuinely linear function
        # fit as "Polynomial x^1" rather than "Linear" by big_o's own model
        # selection. Severity must match Linear's rank, not Cubic's.
        assert sev.complexity_rank("Polynomial: time = 1.3E-05 * x^1 (sec)") == 2

    def test_exponent_near_one_is_linear_equivalent(self):
        assert sev.complexity_rank("Polynomial: time = 9.7E-06 * x^-0.4 (sec)") == 2
        assert sev.complexity_rank("Polynomial: time = 0.00043 * x^0.61 (sec)") == 2

    def test_exponent_two_is_quadratic_equivalent(self):
        assert sev.complexity_rank("Polynomial: time = 7.7E-08 * x^1.7 (sec)") == 4
        assert sev.complexity_rank("Polynomial: time = 0.0013 * x^2.1 (sec)") == 4

    def test_exponent_above_two_is_polynomial_tier(self):
        assert sev.complexity_rank("Polynomial: time = 1E-09 * x^3.5 (sec)") == 5

    def test_unparseable_exponent_falls_back_to_flat_rank(self):
        assert sev.complexity_rank("polynomial") == 5


class TestSeverityLabel:
    @pytest.mark.parametrize(
        "rank,label",
        [
            (6, "Critical"),
            (5, "High"),
            (4, "High"),
            (3, "Medium"),
            (2, "Medium"),
            (1, "Low"),
            (0, "Low"),
            (sev.UNKNOWN_RANK, "Unknown"),
        ],
    )
    def test_label_bands(self, rank, label):
        assert sev.severity_label(rank) == label


class TestHotspotSortKey:
    def test_sorts_rank_first_then_magnitude(self):
        keys = [
            sev.hotspot_sort_key(2, 50.0),
            sev.hotspot_sort_key(4, 10.0),
            sev.hotspot_sort_key(2, 99.0),
        ]
        assert sorted(keys, reverse=True) == [
            sev.hotspot_sort_key(4, 10.0),
            sev.hotspot_sort_key(2, 99.0),
            sev.hotspot_sort_key(2, 50.0),
        ]
