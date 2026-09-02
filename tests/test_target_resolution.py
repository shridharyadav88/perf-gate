"""Tests for the ``target_resolution`` module shared by the profiling scripts.

Loaded the same way ``tests/test_profiling_scripts.py`` loads the profiler
scripts: via :mod:`importlib.resources` since the module lives under the
``skills/`` package-data tree rather than an importable package (its parent
directory, ``code-optimizer``, contains a hyphen).
"""

from __future__ import annotations

import importlib.resources
import importlib.util
import subprocess
import textwrap
from pathlib import Path

import pytest


def _load_module(name: str, rel_path: str):
    pkg_skills = importlib.resources.files("project_code_optimization") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_tr = _load_module("_test_target_resolution", "code-optimizer/scripts/target_resolution.py")

resolve_targets = _tr.resolve_targets
discover_functions = _tr.discover_functions
TargetResolutionError = _tr.TargetResolutionError


def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(source))
    return path


@pytest.fixture
def two_func_file(tmp_path: Path) -> Path:
    return _write(
        tmp_path,
        "mod_a.py",
        """\
        def alpha(x):
            return x

        async def beta(x):
            return x

        class NotAFunction:
            def method(self):
                return 1

        def _gamma(x):
            return x
        """,
    )


@pytest.fixture
def other_file(tmp_path: Path) -> Path:
    return _write(tmp_path, "mod_b.py", "def delta(x):\n    return x\n")


class TestDiscoverFunctions:
    def test_finds_module_level_functions_only(self, two_func_file):
        names = discover_functions(str(two_func_file))
        assert set(names) == {"alpha", "beta", "_gamma"}
        assert "method" not in names

    def test_missing_file_raises(self):
        with pytest.raises(TargetResolutionError, match="File not found"):
            discover_functions("/nonexistent/path.py")

    def test_syntax_error_raises(self, tmp_path: Path):
        bad = _write(tmp_path, "bad.py", "def broken(:\n    pass\n")
        with pytest.raises(TargetResolutionError, match="Cannot parse"):
            discover_functions(str(bad))


class TestResolveTargetsFunction:
    def test_function_target(self, two_func_file):
        pairs = resolve_targets("function", file=str(two_func_file), func="alpha")
        assert pairs == [(str(two_func_file), "alpha")]

    def test_function_target_missing_args_raises(self):
        with pytest.raises(TargetResolutionError, match="requires --file and --func"):
            resolve_targets("function", file="x.py")


class TestResolveTargetsFile:
    def test_file_target_returns_all_functions(self, two_func_file):
        pairs = resolve_targets("file", file=str(two_func_file))
        assert set(pairs) == {
            (str(two_func_file), "alpha"),
            (str(two_func_file), "beta"),
            (str(two_func_file), "_gamma"),
        }

    def test_file_target_missing_file_raises(self):
        with pytest.raises(TargetResolutionError):
            resolve_targets("file", file="/nonexistent/path.py")


class TestResolveTargetsFiles:
    def test_files_target_spans_multiple_files(self, two_func_file, other_file):
        pairs = resolve_targets("files", files=[str(two_func_file), str(other_file)])
        funcs = {name for _, name in pairs}
        assert funcs == {"alpha", "beta", "_gamma", "delta"}

    def test_files_target_requires_files(self):
        with pytest.raises(TargetResolutionError, match="requires --files"):
            resolve_targets("files")


class TestResolveTargetsRepo:
    def test_repo_target_walks_directory(self, tmp_path: Path, two_func_file, other_file):
        pairs = resolve_targets("repo", repo=str(tmp_path))
        funcs = {name for _, name in pairs}
        assert funcs == {"alpha", "beta", "_gamma", "delta"}

    def test_repo_target_excludes_test_files(self, tmp_path: Path):
        _write(tmp_path, "test_something.py", "def should_not_appear():\n    pass\n")
        _write(tmp_path, "real.py", "def keep_me():\n    pass\n")
        pairs = resolve_targets("repo", repo=str(tmp_path))
        funcs = {name for _, name in pairs}
        assert funcs == {"keep_me"}

    def test_repo_target_missing_dir_raises(self):
        with pytest.raises(TargetResolutionError, match="Repository path not found"):
            resolve_targets("repo", repo="/nonexistent/dir")


class TestResolveTargetsCommit:
    def _init_repo_with_commit(self, tmp_path: Path) -> Path:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
        _write(tmp_path, "committed.py", "def committed_func(x):\n    return x\n")
        subprocess.run(["git", "add", "committed.py"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
        return tmp_path

    def test_commit_target_resolves_root_commit(self, tmp_path: Path):
        repo = self._init_repo_with_commit(tmp_path)
        pairs = resolve_targets("commit", commit="HEAD", repo=str(repo))
        assert any(name == "committed_func" for _, name in pairs)

    def test_commit_target_requires_commit(self):
        with pytest.raises(TargetResolutionError, match="requires --commit"):
            resolve_targets("commit", repo=".")

    def test_commit_target_bad_ref_raises(self, tmp_path: Path):
        repo = self._init_repo_with_commit(tmp_path)
        with pytest.raises(TargetResolutionError, match="Cannot resolve commit"):
            resolve_targets("commit", commit="not-a-real-ref", repo=str(repo))


def test_unknown_target_type_raises():
    with pytest.raises(TargetResolutionError, match="Unknown target-type"):
        resolve_targets("bogus")
