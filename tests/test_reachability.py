"""Tests for detectors/reachability.py (U5: review prioritization)."""

from __future__ import annotations

import importlib.resources
import importlib.util
import json
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


reach_mod = _load_module(
    "_test_reach", "perf-gate/scripts/detectors/reachability.py"
)


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "a.py").write_text(
        "def helper(x):\n"
        "    return x + 1\n"
        "\n"
        "\n"
        "def _private():\n"
        "    return 0\n"
        "\n"
        "\n"
        "def main():\n"
        "    return helper(1)\n"
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    main()\n",
        encoding="utf-8",
    )
    (tmp_path / "b.py").write_text(
        "import a\n"
        "\n"
        "\n"
        "def use_it():\n"
        "    return a.helper(2) + a.helper(3)\n",
        encoding="utf-8",
    )
    return str(tmp_path)


def test_caller_count_dedups_repeat_calls_from_one_caller(repo):
    index = reach_mod.analyze_repo(repo)
    a_file = next(f for f, func in index.defined if func == "helper")
    # use_it calls helper twice but counts once; main calls it once.
    assert reach_mod.caller_count(index, a_file, "helper") == 2


def test_entry_reachable_through_main_guard(repo):
    index = reach_mod.analyze_repo(repo)
    files = {func: f for f, func in index.defined}
    assert reach_mod.is_entry_reachable(index, files["main"], "main") is True
    assert reach_mod.is_entry_reachable(index, files["helper"], "helper") is True
    assert reach_mod.is_entry_reachable(
        index, files["_private"], "_private") is False


def test_value_tags_are_neutral(repo):
    index = reach_mod.analyze_repo(repo)
    files = {func: f for f, func in index.defined}
    assert reach_mod.value_tag(
        index, files["helper"], "helper") == "2 in-repo callers"
    assert "dead" in reach_mod.value_tag(
        index, files["_private"], "_private").lower()
    assert "delete" not in reach_mod.value_tag(
        index, files["_private"], "_private").lower()


def test_json_output_shape_and_text_mode(repo, capsys):
    reach_mod.main(["--repo", repo, "--json"])
    rows = json.loads(capsys.readouterr().out)
    by_func = {r["function"]: r for r in rows}
    assert set(by_func) >= {"helper", "main", "_private", "use_it"}
    for row in rows:
        assert set(row) == {
            "file", "function", "callers", "entry_reachable", "value"}
    reach_mod.main(["--repo", repo])
    text = capsys.readouterr().out
    assert "helper -- 2 in-repo callers" in text


def test_broken_file_is_skipped_not_fatal(tmp_path, capsys):
    (tmp_path / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text(
        "def fine():\n    return 1\n", encoding="utf-8")
    reach_mod.main(["--repo", str(tmp_path), "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert [r["function"] for r in rows] == ["fine"]


def test_missing_repo_exits_2(capsys):
    with pytest.raises(SystemExit) as exc:
        reach_mod.main(["--repo", "/nonexistent-dir-xyz"])
    assert exc.value.code == 2


def test_call_in_if_condition_counts_as_caller(tmp_path):
    (tmp_path / "c.py").write_text(
        "def helper(x):\n"
        "    return x\n"
        "\n"
        "\n"
        "def check(x):\n"
        "    if helper(x):\n"
        "        return 1\n"
        "    return 0\n",
        encoding="utf-8",
    )
    index = reach_mod.analyze_repo(str(tmp_path))
    helper = next(f for f, func in index.defined if func == "helper")
    assert reach_mod.caller_count(index, helper, "helper") == 1
