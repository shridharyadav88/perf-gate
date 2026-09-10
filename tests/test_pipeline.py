"""Tests for W10 pipeline wiring: audit CLI codes, hook script, CI template."""

from __future__ import annotations

import importlib.resources
import importlib.util
import json
import os
import shutil
import subprocess
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


audit = _load_module(
    "_test_pipe_audit", "perf-gate/scripts/detectors/static_audit.py"
)

CLEAN = "def f(xs):\n    return sorted(xs)\n"
HINTED = "def f(a, b):\n    for x in a:\n        for y in b:\n            print(x, y)\n"
BROKEN = "def f(:\n    pass\n"


class TestAuditCLI:
    def test_clean_file_exits_zero_silently(self, tmp_path, capsys):
        mod = tmp_path / "mod.py"
        mod.write_text(CLEAN)
        assert audit.main(["--files", str(mod)]) is None
        assert capsys.readouterr().out == ""

    def test_hints_printed_but_advisory_by_default(self, tmp_path, capsys):
        mod = tmp_path / "mod.py"
        mod.write_text(HINTED)
        assert audit.main(["--files", str(mod)]) is None
        assert "[WARN]" in capsys.readouterr().out

    def test_strict_exits_one_on_hints(self, tmp_path):
        mod = tmp_path / "mod.py"
        mod.write_text(HINTED)
        with pytest.raises(SystemExit) as exc_info:
            audit.main(["--files", str(mod), "--strict"])
        assert exc_info.value.code == 1

    def test_strict_clean_stays_zero(self, tmp_path):
        mod = tmp_path / "mod.py"
        mod.write_text(CLEAN)
        assert audit.main(["--files", str(mod), "--strict"]) is None

    def test_unparseable_file_exits_two(self, tmp_path, capsys):
        mod = tmp_path / "mod.py"
        mod.write_text(BROKEN)
        with pytest.raises(SystemExit) as exc_info:
            audit.main(["--files", str(mod)])
        assert exc_info.value.code == 2
        assert "Error" in capsys.readouterr().err

    def test_json_carries_fingerprints(self, tmp_path, capsys):
        mod = tmp_path / "mod.py"
        mod.write_text(HINTED)
        audit.main(["--files", str(mod), "--json"])
        rows = json.loads(capsys.readouterr().out)
        assert len(rows) == 1
        assert rows[0]["check"] == "nested_loop"
        assert len(rows[0]["fingerprint"]) == 40

    def test_hint_fingerprint_survives_line_shift(self):
        shifted = "\n\n" + HINTED
        fp1 = [h.fingerprint for h in audit.analyze_source(HINTED).hints]
        fp2 = [h.fingerprint for h in audit.analyze_source(shifted).hints]
        assert fp1 == fp2 and len(fp1) == 1


def _skill_dir():
    return (
        importlib.resources.files("perf_gate")
        / "skills"
        / "perf-gate"
    )


def _make_repo(tmp_path, files: dict):
    """Init a git repo with the skill 'deployed' and *files* staged."""
    repo = tmp_path / "repo"
    target = repo / ".agents" / "skills" / "perf-gate"
    target.parent.mkdir(parents=True)
    os.symlink(_skill_dir(), target, target_is_directory=True)
    for name, content in files.items():
        dest = repo / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    return repo


def _run_hook(repo, strict=False):
    hook = (
        repo / ".agents" / "skills" / "perf-gate"
        / "templates" / "pre_commit_example.sh"
    )
    env = dict(os.environ)
    if strict:
        env["STRICT"] = "1"
    return subprocess.run(
        ["sh", str(hook)], cwd=repo, env=env,
        capture_output=True, text=True, timeout=120,
    )


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


@needs_git
class TestPreCommitHook:
    def test_clean_tree_passes(self, tmp_path):
        repo = _make_repo(tmp_path, {"mod.py": CLEAN})
        proc = _run_hook(repo)
        assert proc.returncode == 0

    def test_hints_printed_but_fail_open_by_default(self, tmp_path):
        repo = _make_repo(tmp_path, {"mod.py": HINTED})
        proc = _run_hook(repo)
        assert proc.returncode == 0
        assert "[WARN]" in proc.stdout

    def test_strict_blocks_on_hints(self, tmp_path):
        repo = _make_repo(tmp_path, {"mod.py": HINTED})
        proc = _run_hook(repo, strict=True)
        assert proc.returncode == 1

    def test_harness_error_fails_open(self, tmp_path):
        repo = _make_repo(tmp_path, {"mod.py": BROKEN})
        proc = _run_hook(repo)
        assert proc.returncode == 0
        assert "fail-open" in proc.stdout

    def test_no_python_staged_passes(self, tmp_path):
        repo = _make_repo(tmp_path, {"notes.txt": "hello\n"})
        proc = _run_hook(repo)
        assert proc.returncode == 0


class TestCITemplate:
    def test_template_exists_with_all_gates(self):
        text = (_skill_dir() / "templates" / "ci_gates_example.yml").read_text()
        for job in ("static-audit", "concurrency-gate", "memory-gate", "baseline-classify"):
            assert job in text
        assert "fingerprint" in text
        assert "memray" in text  # optional-only status documented

    def test_hook_template_exists(self):
        hook = _skill_dir() / "templates" / "pre_commit_example.sh"
        text = hook.read_text().lower()
        assert "strict" in text and "fail-open" in text
        assert "never writes hooks" in text
