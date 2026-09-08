"""Tests for the ``install-agent-skills`` CLI and ``install_skills`` function."""

from __future__ import annotations

import importlib.resources
import textwrap
from pathlib import Path

import pytest

from project_code_optimization import BUNDLED_SKILLS
from project_code_optimization.cli import install_skills


class TestInstallSkills:
    """Unit tests for :func:`install_skills` using a temporary directory."""

    def test_install_creates_skill_layout(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        installed = install_skills(target_dir=tmp_path / "out")

        assert "code-optimizer" in installed
        dest = tmp_path / "out" / "code-optimizer"
        assert dest.is_dir()
        assert (dest / "SKILL.md").is_file()
        assert (dest / "scripts" / "run_big_o.py").is_file()
        assert (dest / "scripts" / "run_line_profile.py").is_file()
        assert (dest / "templates" / "report_template.md").is_file()

    def test_install_creates_default_target(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        install_skills()
        default = tmp_path / ".agents" / "skills" / "code-optimizer"
        assert default.is_dir()

    def test_install_overwrite_creates_backup(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        dest = tmp_path / "out" / "code-optimizer"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text("modified")

        install_skills(target_dir=tmp_path / "out")

        # Original should be backed up
        backups = list((tmp_path / "out").glob("code-optimizer.backup-*"))
        assert len(backups) == 1
        assert (backups[0] / "SKILL.md").read_text() == "modified"
        # New copy should be the bundled one
        assert "name: code-optimizer" in (dest / "SKILL.md").read_text()

    def test_install_force_no_backup(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        dest = tmp_path / "out" / "code-optimizer"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text("modified")

        install_skills(target_dir=tmp_path / "out", force=True)

        backups = list((tmp_path / "out").glob("code-optimizer.backup-*"))
        assert len(backups) == 0
        assert "name: code-optimizer" in (dest / "SKILL.md").read_text()

    def test_dry_run_writes_nothing(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        install_skills(target_dir=tmp_path / "out", dry_run=True)
        assert not (tmp_path / "out").exists()

    def test_dry_run_reports_installed(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        installed = install_skills(target_dir=tmp_path / "out", dry_run=True)
        assert "code-optimizer" in installed

    def test_install_subset_skill(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        installed = install_skills(
            target_dir=tmp_path / "out",
            skills=["code-optimizer"],
        )
        assert installed == ["code-optimizer"]

    def test_install_unknown_skill_logs_error(self, tmp_path: Path, monkeypatch, caplog):
        monkeypatch.chdir(tmp_path)
        installed = install_skills(
            target_dir=tmp_path / "out",
            skills=["nonexistent-skill"],
        )
        assert installed == []
        assert "Unknown skill" in caplog.text

    def test_skill_md_frontmatter_compliant(self):
        """Every bundled skill must ship a valid SKILL.md with name + description."""
        pkg_skills = importlib.resources.files("project_code_optimization") / "skills"
        for name in BUNDLED_SKILLS:
            skill_md = pkg_skills / name / "SKILL.md"
            assert skill_md.is_file(), f"SKILL.md missing for '{name}'"
            content = skill_md.read_text()
            assert "name:" in content, f"SKILL.md for '{name}' missing 'name:'"
            assert "description:" in content, f"SKILL.md for '{name}' missing 'description:'"


class TestCLIEntryPoint:
    """Smoke-tests for the argparse entry point."""

    def test_main_runs_with_defaults(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from project_code_optimization.cli import main

        main(["--target", str(tmp_path / "out")])
        assert (tmp_path / "out" / "code-optimizer" / "SKILL.md").exists()

    def test_main_dry_run(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from project_code_optimization.cli import main

        main(["--target", str(tmp_path / "out"), "--dry-run"])
        assert not (tmp_path / "out").exists()

    def test_main_version(self, capsys):
        from project_code_optimization.cli import main

        with pytest.raises(SystemExit) as excinfo:
            main(["--version"])
        assert excinfo.value.code == 0

    def test_main_verbose(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from project_code_optimization.cli import main

        main(["--target", str(tmp_path / "out"), "--verbose"])
        assert (tmp_path / "out" / "code-optimizer" / "SKILL.md").exists()
