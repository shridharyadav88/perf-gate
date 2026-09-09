"""Tests for generate_baseline_csv.py — the no-LLM CSV baseline report generator.

Loaded via importlib.resources, same as the other bundled scripts (its parent
directory, ``code-optimizer``, contains a hyphen so it can't be an importable
package).
"""

from __future__ import annotations

import csv
import importlib.resources
import importlib.util
import io
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_module(name: str, rel_path: str):
    pkg_skills = importlib.resources.files("project_code_optimization") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_gen = _load_module(
    "_test_generate_baseline_csv", "code-optimizer/scripts/generate_baseline_csv.py",
)

build_rows = _gen.build_rows
sort_rows = _gen.sort_rows
main = _gen.main
FIELDNAMES = _gen.FIELDNAMES


def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(source))
    return path


def _default_args(**overrides):
    base = dict(min_n=50, max_n=2000, n_measures=6, n_timings=2, n_repeats=1)
    base.update(overrides)
    return SimpleNamespace(**base)


def _body_without_preamble(text: str) -> str:
    """Strip the W7 `# key=value` provenance preamble preceding the CSV header."""
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines) and lines[i].lstrip().startswith("#"):
        i += 1
    return "".join(lines[i:])


@pytest.fixture
def linear_quadratic_file(tmp_path: Path) -> Path:
    return _write(
        tmp_path,
        "mod.py",
        """\
        def linear(xs):
            return [x for x in xs]

        def quad(xs):
            return [x for x in xs for y in xs]
        """,
    )


class TestBuildRows:
    def test_returns_one_row_per_pair_with_expected_fields(self, linear_quadratic_file):
        rows = build_rows([(str(linear_quadratic_file), "linear")], _default_args())
        assert len(rows) == 1
        row = rows[0]
        assert set(FIELDNAMES) - {"rank"} <= set(row.keys())
        assert row["function"] == "linear"
        assert row["big_o_error"] == ""
        assert row["complexity_rank"] >= 0

    def test_missing_function_reports_error_not_crash(self, linear_quadratic_file):
        rows = build_rows([(str(linear_quadratic_file), "nonexistent")], _default_args())
        assert len(rows) == 1
        assert rows[0]["big_o_error"] != ""
        assert rows[0]["severity"] == "Unknown"

    def test_missing_file_reports_error_not_crash(self):
        rows = build_rows([("/nonexistent/mod.py", "f")], _default_args())
        assert len(rows) == 1
        assert "File not found" in rows[0]["big_o_error"] or rows[0]["big_o_error"] != ""


class TestStreamingMain:
    def test_rows_stream_in_resolution_order(self, linear_quadratic_file, capsys):
        out_path = linear_quadratic_file.parent / "out.csv"
        main([
            "--target-type", "file", "--file", str(linear_quadratic_file),
            "--min-n", "10", "--max-n", "40", "--n-measures", "2",
            "--output", str(out_path),
        ])
        body = "\n".join(
            line for line in out_path.read_text().splitlines() if not line.startswith("#")
        )
        rows = list(csv.DictReader(io.StringIO(body)))
        # Emission order (linear first: file order), NOT severity order --
        # streaming keeps completed rows on interrupted runs.
        assert [r["function"] for r in rows] == ["linear", "quad"]
        assert [r["rank"] for r in rows] == ["1", "2"]


class TestSortRows:
    def test_sorts_most_severe_first_and_assigns_rank(self, linear_quadratic_file):
        rows = build_rows(
            [(str(linear_quadratic_file), "linear"), (str(linear_quadratic_file), "quad")],
            _default_args(min_n=50, max_n=5000, n_measures=8),
        )
        sorted_rows = sort_rows(rows)
        assert [r["rank"] for r in sorted_rows] == list(range(1, len(sorted_rows) + 1))
        # quad (quadratic) must outrank linear regardless of row order going in.
        by_func = {r["function"]: r["rank"] for r in sorted_rows}
        assert by_func["quad"] < by_func["linear"]

    def test_errors_sort_last(self, linear_quadratic_file):
        rows = build_rows(
            [(str(linear_quadratic_file), "linear"), (str(linear_quadratic_file), "nonexistent")],
            _default_args(),
        )
        sorted_rows = sort_rows(rows)
        by_func = {r["function"]: r["rank"] for r in sorted_rows}
        assert by_func["linear"] < by_func["nonexistent"]


class TestMainCLI:
    def test_writes_valid_csv_to_stdout(self, linear_quadratic_file, capsys):
        main(["--target-type", "file", "--file", str(linear_quadratic_file),
              "--min-n", "50", "--max-n", "2000", "--n-measures", "6"])
        out = capsys.readouterr().out
        reader = csv.DictReader(io.StringIO(_body_without_preamble(out)))
        rows = list(reader)
        assert reader.fieldnames == FIELDNAMES
        assert {row["function"] for row in rows} == {"linear", "quad"}

    def test_writes_csv_to_output_file(self, linear_quadratic_file, tmp_path: Path):
        out_path = tmp_path / "report.csv"
        main(["--target-type", "file", "--file", str(linear_quadratic_file),
              "--min-n", "50", "--max-n", "2000", "--n-measures", "6",
              "--output", str(out_path)])
        content = out_path.read_text()
        reader = csv.DictReader(io.StringIO(_body_without_preamble(content)))
        assert reader.fieldnames == FIELDNAMES
        assert len(list(reader)) == 2

    def test_function_target_missing_args_exit_2(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            main(["--target-type", "function"])
        assert excinfo.value.code == 2
        assert "requires --file and --func" in capsys.readouterr().err

    def test_unresolvable_target_exit_1(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            main(["--target-type", "file", "--file", "/nonexistent/mod.py"])
        assert excinfo.value.code == 1
        assert "Error:" in capsys.readouterr().err
