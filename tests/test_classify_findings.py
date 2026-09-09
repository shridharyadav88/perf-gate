"""Tests for classify_findings.py — deterministic CSV-to-fix-tier triage."""

from __future__ import annotations

import csv
import importlib.resources
import importlib.util
import io
import sys
import textwrap
from pathlib import Path


def _load_module(name: str, rel_path: str):
    pkg_skills = importlib.resources.files("project_code_optimization") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cf = _load_module("_test_classify_findings", "code-optimizer/scripts/classify_findings.py")

classify_rows = cf.classify_rows
main = cf.main


def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(source))
    return path


def _base_row(**overrides) -> dict:
    row = {
        "file": "mod.py", "function": "f",
        "empirical_big_o": "", "complexity_rank": "", "big_o_error": "",
        "total_hits": "0", "total_hotspot_time_us": "0", "top_hotspot_pct_time": "0",
        "top_hotspot_line": "", "top_hotspot_source": "", "line_profile_error": "",
    }
    row.update(overrides)
    return row


def _write_input_csv(path: Path, fieldnames: list, row: dict) -> None:
    """Write a classify input CSV including the W7 provenance preamble.

    Since schema versioning landed, the classifier fails closed on inputs
    without provenance -- fixtures must carry it like real producer output.
    """
    with open(path, "w", newline="", encoding="utf-8") as fh:
        fh.write("# schema_version=1\n")
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(row)


class TestTier0Detection:
    def test_hoistable_regex_wins_over_everything_else(self, tmp_path: Path):
        mod = _write(
            tmp_path, "mod.py",
            r"""
            import re

            def f(line):
                pattern = re.compile(r'^\d+$')
                return bool(pattern.match(line))
            """,
        )
        # Even a row claiming Quadratic complexity should still be routed to
        # tier0 if a mechanical fix is available — cheaper and more certain
        # than an algorithm rewrite.
        row = _base_row(
            file=str(mod), function="f", empirical_big_o="Quadratic", complexity_rank="4",
        )
        classify_rows([row])
        assert row["tier"] == "tier0_regex_hoist"
        assert "pattern" in row["tier_detail"]

    def test_static_detection_works_even_when_profiling_failed(self, tmp_path: Path):
        mod = _write(
            tmp_path, "mod.py",
            r"""
            import re

            def f(a, b, c):
                pattern = re.compile(r'^\d+$')
                return pattern.match(str(a))
            """,
        )
        row = _base_row(
            file=str(mod), function="f",
            big_o_error="f() missing 2 required positional arguments",
            line_profile_error="f() missing 2 required positional arguments",
        )
        classify_rows([row])
        assert row["tier"] == "tier0_regex_hoist"

    def test_two_rows_same_file_share_one_analysis(self, tmp_path: Path):
        mod = _write(
            tmp_path, "mod.py",
            r"""
            import re

            def f(line):
                pattern = re.compile(r'^\d+$')
                return bool(pattern.match(line))

            def g(line):
                pattern = re.compile(r'^\d+$')
                return bool(pattern.search(line))
            """,
        )
        rows = [_base_row(file=str(mod), function="f"), _base_row(file=str(mod), function="g")]
        classify_rows(rows)
        assert rows[0]["tier"] == "tier0_regex_hoist"
        assert rows[1]["tier"] == "tier0_regex_hoist"


class TestTier2Detection:
    def test_high_complexity_rank_routes_to_tier2(self, tmp_path: Path):
        mod = _write(tmp_path, "mod.py", "def f(xs):\n    return [x for x in xs]\n")
        # Rank 4 (quadratic) is the fit-flap zone: it escalates only with
        # independent confirming measurements (W9 hysteresis).
        row = _base_row(
            file=str(mod), function="f", empirical_big_o="Quadratic", complexity_rank="4",
            confirmations="2",
        )
        classify_rows([row])
        assert row["tier"] == "tier2_algorithmic"
        assert "Quadratic" in row["tier_detail"]

    def test_linear_complexity_does_not_route_to_tier2(self, tmp_path: Path):
        mod = _write(tmp_path, "mod.py", "def f(xs):\n    return [x for x in xs]\n")
        row = _base_row(file=str(mod), function="f", empirical_big_o="Linear", complexity_rank="2")
        classify_rows([row])
        assert row["tier"] != "tier2_algorithmic"


class TestTier1AndNotActionable:
    def test_no_pattern_no_complexity_problem_is_tier1(self, tmp_path: Path):
        mod = _write(tmp_path, "mod.py", "def f(xs):\n    return sorted(xs)\n")
        row = _base_row(file=str(mod), function="f", empirical_big_o="Linear", complexity_rank="2")
        classify_rows([row])
        assert row["tier"] == "tier1_review"

    def test_both_errors_and_no_static_pattern_is_not_actionable(self, tmp_path: Path):
        mod = _write(tmp_path, "mod.py", "def f(a, b):\n    return a + b\n")
        row = _base_row(
            file=str(mod), function="f",
            big_o_error="missing argument", line_profile_error="missing argument",
        )
        classify_rows([row])
        assert row["tier"] == "not_actionable"

    def test_unparseable_file_falls_through_gracefully(self, tmp_path: Path):
        mod = _write(tmp_path, "broken.py", "def f(:\n    pass\n")
        row = _base_row(
            file=str(mod), function="f",
            big_o_error="syntax error", line_profile_error="syntax error",
        )
        classify_rows([row])  # must not raise
        assert row["tier"] == "not_actionable"


class TestCLI:
    def test_end_to_end_csv_roundtrip(self, tmp_path: Path):
        mod = _write(
            tmp_path, "mod.py",
            r"""
            import re

            def f(line):
                pattern = re.compile(r'^\d+$')
                return bool(pattern.match(line))
            """,
        )
        in_csv = tmp_path / "in.csv"
        fieldnames = list(_base_row().keys())
        _write_input_csv(in_csv, fieldnames, _base_row(file=str(mod), function="f"))

        out_csv = tmp_path / "out.csv"
        main(["--input", str(in_csv), "--output", str(out_csv)])

        with open(out_csv, newline="", encoding="utf-8") as fh:
            body = "".join(
                line for line in fh if not line.lstrip().startswith("#")
            )
            rows = list(csv.DictReader(io.StringIO(body)))
        assert rows[0]["tier"] == "tier0_regex_hoist"

    def test_stdout_output(self, tmp_path: Path, capsys):
        mod = _write(tmp_path, "mod.py", "def f(xs):\n    return sorted(xs)\n")
        in_csv = tmp_path / "in.csv"
        fieldnames = list(_base_row().keys())
        row = _base_row(
            file=str(mod), function="f", empirical_big_o="Linear", complexity_rank="2",
        )
        _write_input_csv(in_csv, fieldnames, row)

        main(["--input", str(in_csv)])
        out = capsys.readouterr().out
        body = "\n".join(
            line for line in out.splitlines() if not line.lstrip().startswith("#")
        )
        reader = csv.DictReader(io.StringIO(body))
        rows = list(reader)
        assert rows[0]["tier"] == "tier1_review"
