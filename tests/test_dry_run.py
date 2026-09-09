"""Tests for --dry-run across the three scanning CLIs."""

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


big_o_mod = _load_module(
    "_test_dr_big_o", "code-optimizer/scripts/profilers/run_big_o.py"
)
line_mod = _load_module(
    "_test_dr_line", "code-optimizer/scripts/profilers/run_line_profile.py"
)
gen_mod = _load_module(
    "_test_dr_gen", "code-optimizer/scripts/generate_baseline_csv.py"
)

CLIS = {
    "big_o": big_o_mod.main,
    "line": line_mod.main,
    "csv": gen_mod.main,
}


def _evil_module(tmp_path):
    mod = tmp_path / "evil.py"
    mod.write_text(
        "raise RuntimeError('IMPORTED -- dry run executed target code')\n"
        "def f(xs):\n"
        "    return xs\n"
    )
    return mod


@pytest.mark.parametrize("cli", sorted(CLIS))
def test_dry_run_lists_without_executing(tmp_path, capsys, cli):
    mod = _evil_module(tmp_path)
    CLIS[cli](["--target-type", "file", "--file", str(mod), "--dry-run"])
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert f"{mod} :: f" in out
    assert "No imports, no calls" in out


@pytest.mark.parametrize("cli", sorted(CLIS))
def test_dry_run_truncation_note(tmp_path, capsys, cli):
    mod = tmp_path / "two.py"
    mod.write_text("def fa(xs):\n    return xs\ndef fb(xs):\n    return xs\n")
    CLIS[cli]([
        "--target-type", "file", "--file", str(mod),
        "--max-targets", "1", "--dry-run",
    ])
    out = capsys.readouterr().out
    assert "1 target(s)" in out
    assert "truncated to --max-targets=1" in out


@pytest.mark.parametrize("cli", sorted(CLIS))
def test_dry_run_function_mode_validates_without_importing(tmp_path, capsys, cli):
    mod = _evil_module(tmp_path)
    CLIS[cli]([
        "--target-type", "function", "--file", str(mod),
        "--func", "f", "--dry-run",
    ])
    assert f"{mod} :: f" in capsys.readouterr().out


@pytest.mark.parametrize("cli", sorted(CLIS))
def test_dry_run_function_mode_missing_args_exits_2(capsys, cli):
    with pytest.raises(SystemExit) as excinfo:
        CLIS[cli](["--target-type", "function", "--dry-run"])
    assert excinfo.value.code == 2


def test_csv_dry_run_writes_no_files(tmp_path, capsys):
    mod = tmp_path / "m.py"
    mod.write_text("def f(xs):\n    return xs\n")
    out_csv = tmp_path / "out.csv"
    gen_mod.main([
        "--target-type", "file", "--file", str(mod),
        "--output", str(out_csv), "--dry-run",
    ])
    assert "DRY RUN" in capsys.readouterr().out
    assert not out_csv.exists()
    assert not tmp_path.joinpath("out.inputs.json").exists()
