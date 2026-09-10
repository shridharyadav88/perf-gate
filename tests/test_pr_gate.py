"""Tests for pr_gate.py (diff-scoped pre-merge performance gate)."""

from __future__ import annotations

import importlib.resources
import importlib.util
import os
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


gate_mod = _load_module("_test_pr_gate", "perf-gate/scripts/pr_gate.py")


def _ok_row(**overrides):
    row = {"file": "a.py", "function": "f", "empirical_big_o": "Linear",
           "complexity_rank": "2", "total_hotspot_time_us": "100.0",
           "tier": "tier1_review", "tier_detail": "",
           "big_o_error": "", "line_profile_error": ""}
    row.update(overrides)
    return row


# --- parse_unified0_diff ----------------------------------------------------

def test_parse_modify_and_new_ranges():
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -10,3 +10,5 @@ def f():\n"
        " context\n"
    )
    changed, deleted = gate_mod.parse_unified0_diff(diff)
    assert changed == {"a.py": [(10, 5)]}
    assert deleted == []


def test_parse_deleted_file():
    diff = (
        "diff --git a/old.py b/old.py\n"
        "--- a/old.py\n"
        "+++ /dev/null\n"
        "@@ -1,4 +0,0 @@\n"
    )
    changed, deleted = gate_mod.parse_unified0_diff(diff)
    assert changed == {}
    assert deleted == ["old.py"]


def test_parse_new_file_and_pure_deletion_hunk():
    diff = (
        "diff --git a/n.py b/n.py\n"
        "--- /dev/null\n"
        "+++ b/n.py\n"
        "@@ -0,0 +1,9 @@\n"
        "diff --git a/d.py b/d.py\n"
        "--- a/d.py\n"
        "+++ b/d.py\n"
        "@@ -5,3 +5,0 @@\n"  # pure deletion: no + lines, contributes no range
    )
    changed, deleted = gate_mod.parse_unified0_diff(diff)
    assert changed == {"n.py": [(1, 9)], "d.py": []}
    assert deleted == []


def test_parse_malformed_hunk_skipped():
    diff = "--- a/a.py\n+++ b/a.py\n@@ -x +y @@\n@@ -3,2 +30,4 @@\n"
    changed, _ = gate_mod.parse_unified0_diff(diff)
    assert changed == {"a.py": [(30, 4)]}


# --- touched_functions ------------------------------------------------------

SRC = (
    "def alpha():\n"
    "    return 1\n"
    "\n"
    "async def beta():\n"
    "    return 2\n"
    "\n"
    "def gamma():\n"
    "    return 3\n"
)


def test_touched_functions_intersection():
    assert gate_mod.touched_functions(SRC, [(1, 2)]) == ["alpha"]
    assert gate_mod.touched_functions(SRC, [(4, 2)]) == ["beta"]
    assert gate_mod.touched_functions(SRC, [(2, 6)]) == ["alpha", "beta", "gamma"]


def test_touched_functions_no_overlap_and_bad_source():
    assert gate_mod.touched_functions(SRC, [(100, 5)]) == []
    assert gate_mod.touched_functions(SRC, []) == []
    assert gate_mod.touched_functions("def broken(:\n", [(1, 3)]) == []
    assert gate_mod._module_functions("def broken(:\n") == []


# --- verdict ----------------------------------------------------------------

def test_verdict_rank_worsening_fails_and_improvement_noted():
    touched = [("a.py", "f")]
    base = [_ok_row(complexity_rank="2", empirical_big_o="Linear")]
    cur = [_ok_row(complexity_rank="4", empirical_big_o="Quadratic")]
    result = gate_mod.verdict(touched, base, cur)
    assert len(result["failures"]) == 1 and "2 -> 4" in result["failures"][0]
    assert result["advisories"] == []
    result = gate_mod.verdict(touched, cur, base)
    assert result["failures"] == [] and len(result["improvements"]) == 1


def test_verdict_new_tier0_fails_new_tier2_advises_new_other_silent():
    touched = [("a.py", "f")]
    result = gate_mod.verdict(touched, [], [_ok_row(tier="tier0_dict_keys")])
    assert len(result["failures"]) == 1 and "newly tier0_dict_keys" in result["failures"][0]
    result = gate_mod.verdict(touched, [], [_ok_row(tier="tier2_algorithmic")])
    assert result["failures"] == [] and len(result["advisories"]) == 1
    result = gate_mod.verdict(touched, [], [_ok_row(tier="tier1_review")])
    assert result == {"failures": [], "advisories": [], "improvements": []}


def test_verdict_deleted_is_improvement_error_rows_abstain():
    touched = [("a.py", "f")]
    result = gate_mod.verdict(touched, [_ok_row()], [])
    assert result["failures"] == [] and len(result["improvements"]) == 1
    # error on either side of a modified function: silent abstention
    bad = _ok_row(big_o_error="boom")
    assert gate_mod.verdict(touched, [bad], [_ok_row()])["failures"] == []
    assert gate_mod.verdict(touched, [_ok_row()], [bad]) == {
        "failures": [], "advisories": [], "improvements": []}


def test_verdict_entered_tier0_fails_same_tier_silent_drift_advises():
    touched = [("a.py", "f")]
    old = _ok_row(tier="tier1_review")
    new = _ok_row(tier="tier0_enumerate")
    result = gate_mod.verdict(touched, [old], [new])
    assert any("entered tier0_enumerate" in f for f in result["failures"])
    assert gate_mod.verdict(touched, [new], [_ok_row(tier="tier0_enumerate")]) == {
        "failures": [], "advisories": [], "improvements": []}
    hot = _ok_row(total_hotspot_time_us="100.0")
    hotter = _ok_row(total_hotspot_time_us="130.0")  # +30% over 20% margin
    result = gate_mod.verdict(touched, [hot], [hotter])
    assert result["failures"] == [] and len(result["advisories"]) == 1
    borderline = _ok_row(total_hotspot_time_us="119.0")  # within margin
    assert gate_mod.verdict(touched, [hot], [borderline])["advisories"] == []


# --- read_classified / merge -------------------------------------------------

def test_read_classified_skips_preamble_and_rejects(tmp_path):
    good = tmp_path / "c.csv"
    good.write_text("# commit=abc\nfile,function,tier\n", encoding="utf-8")
    rows = gate_mod.read_classified(str(good))
    assert rows == []
    bad = tmp_path / "b.csv"
    bad.write_text("file,function\n", encoding="utf-8")
    with pytest.raises(gate_mod.GateSkip):
        gate_mod.read_classified(str(bad))
    with pytest.raises(gate_mod.GateSkip):
        gate_mod.read_classified(str(tmp_path / "missing.csv"))


def test_merge_raw_csvs_first_preamble_wins(tmp_path):
    p1 = tmp_path / "p1.csv"
    p1.write_text("# base=1\nfile,function\nx.py,f\n", encoding="utf-8")
    p2 = tmp_path / "p2.csv"
    p2.write_text("# base=2\nfile,function\ny.py,g\n", encoding="utf-8")
    out = str(tmp_path / "m.csv")
    gate_mod._merge_raw_csvs([str(p1), str(p2)], out)
    import csv as _csv
    with open(out, encoding="utf-8") as f:
        raw = f.read()
    assert raw.splitlines()[0] == "# base=1"
    body_lines = [ln for ln in raw.splitlines() if not ln.startswith("#")]
    assert len(list(_csv.DictReader(body_lines))) == 2


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


RAW_ONE = ("file,function,empirical_big_o,complexity_rank,total_hotspot_time_us,"
           "tier,tier_detail,big_o_error,line_profile_error\n"
           "m.py,f,Linear,2,10.0,tier1_review,,,\n")


def _fake_run_factory(calls, fail_on=()):
    def _fake(argv, cwd, timeout):
        calls.append((argv, cwd))
        if any(tag in " ".join(argv) for tag in fail_on):
            return _FakeProc(returncode=1, stderr="stage exploded")
        if "generate_baseline_csv.py" in argv[1]:
            out = argv[argv.index("--output") + 1]
            func = argv[argv.index("--func") + 1]
            with open(out, "w", encoding="utf-8") as f:
                f.write(RAW_ONE.replace("m.py", argv[argv.index("--file") + 1])
                        .replace(",f,", f",{func},"))
        if "classify_findings.py" in argv[1]:
            src = argv[argv.index("--input") + 1]
            out = argv[argv.index("--output") + 1]
            with open(src, encoding="utf-8") as f:
                raw = f.read()
            body = "\n".join(ln for ln in raw.splitlines() if not ln.startswith("#"))
            with open(out, "w", encoding="utf-8") as f:
                f.write(body)
        return _FakeProc()
    return _fake


def test_run_side_function_scope_and_empty(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(gate_mod, "_run_cmd", _fake_run_factory(calls))
    rows = gate_mod.run_side("scripts", "tree", [("m.py", "f"), ("m.py", "g")],
                             str(tmp_path / "side"), 5.0, 60.0)
    assert [(r["file"], r["function"]) for r in rows] == [("m.py", "f"), ("m.py", "g")]
    gen_argv = [argv for argv, _ in calls if "generate_baseline_csv.py" in argv[1]]
    assert len(gen_argv) == 2  # one invocation per pair, never whole files
    assert all(a[a.index("--target-type") + 1] == "function" for a in gen_argv)
    assert all(cwd == "tree" for _, cwd in calls)
    assert gate_mod.run_side("scripts", "tree", [], str(tmp_path / "e"), 5.0, 60.0) == []


def test_run_side_stage_failure_skips(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(gate_mod, "_run_cmd", _fake_run_factory(calls, fail_on=("classify",)))
    with pytest.raises(gate_mod.GateSkip):
        gate_mod.run_side("scripts", "tree", [("m.py", "f")],
                          str(tmp_path / "s"), 5.0, 60.0)


def _git_repo(path):
    env = {"GIT_CONFIG_NOSYSTEM": "1", "HOME": str(path)}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True,
                     env={**os.environ, **env})
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    return path


def test_collect_touched_modify_untracked_delete(tmp_path):
    repo = str(_git_repo(tmp_path))
    (tmp_path / "keep.py").write_text("def a():\n    return 1\n\ndef b():\n    return 2\n",
                                      encoding="utf-8")
    (tmp_path / "gone.py").write_text("def old():\n    return 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    (tmp_path / "keep.py").write_text("def a():\n    return 1\n\ndef b():\n    return 3\n",
                                      encoding="utf-8")
    (tmp_path / "gone.py").unlink()
    (tmp_path / "brand_new.py").write_text("def fresh():\n    return 9\n", encoding="utf-8")
    current, base_only = gate_mod.collect_touched(repo, "main")
    assert current == [("brand_new.py", "fresh"), ("keep.py", "b")]
    assert base_only == [("gone.py", "old")]


def test_main_skip_and_harness_error(tmp_path, capsys):
    repo = str(_git_repo(tmp_path))  # clean tree: nothing touched vs base
    (tmp_path / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    gate_mod.main(["--base", "main", "--repo", repo])
    assert "SKIP: no touched functions" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        gate_mod.main(["--base", "main", "--repo", str(tmp_path / "not-a-repo")])
    assert exc.value.code == 2
