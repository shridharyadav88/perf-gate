"""Tests for scripts/provenance.py and its producer/consumer wiring (W7)."""

from __future__ import annotations

import csv
import importlib.resources
import importlib.util
import io
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


prov = _load_module("_test_provenance", "perf-gate/scripts/provenance.py")
gen = _load_module("_test_prov_gen", "perf-gate/scripts/generate_baseline_csv.py")
clf = _load_module("_test_prov_clf", "perf-gate/scripts/classify_findings.py")


def _quick_module(tmp_path):
    mod = tmp_path / "mod.py"
    mod.write_text("def f(xs):\n    return [x for x in xs]\n")
    return mod


class TestPreambleFormat:
    def test_current_provenance_shape(self):
        proven = prov.current_provenance()
        assert proven["schema_version"] == str(prov.SCHEMA_VERSION)
        assert proven["tier"] in ("baseline", "enhanced")
        assert proven["python"] and proven["created_utc"] and proven["commit"]

    def test_format_split_round_trip(self):
        proven = {
            "tool_version": "0.1.0", "schema_version": "1", "python": "3.12.1",
            "tier": "baseline", "commit": "abc1234", "created_utc": "2026-01-01T00:00:00+00:00",
        }
        text = "\n".join(prov.format_preamble(proven)) + "\nheader\n"
        back, body = prov.split_preamble(text)
        assert back == proven
        assert body == "header\n"

    def test_split_without_preamble_returns_empty(self):
        proven, body = prov.split_preamble("a,b\n1,2\n")
        assert proven == {}
        assert body == "a,b\n1,2\n"

    def test_values_with_newlines_are_flattened(self):
        lines = prov.format_preamble({"tool_version": "a\nb"})
        assert all("\n" not in line for line in lines)


class TestAssertSchema:
    def test_current_schema_passes(self):
        prov.assert_schema({"schema_version": str(prov.SCHEMA_VERSION)})

    def test_missing_schema_fails_closed(self):
        with pytest.raises(prov.ProvenanceError, match="schema_version"):
            prov.assert_schema({})

    def test_wrong_schema_fails_closed(self):
        with pytest.raises(prov.ProvenanceError, match="Regenerate"):
            prov.assert_schema({"schema_version": "999"})

    def test_non_numeric_schema_fails_closed(self):
        with pytest.raises(prov.ProvenanceError):
            prov.assert_schema({"schema_version": "one"})


class TestCheckCompatible:
    def _pair(self, tier_a="baseline", tier_b="baseline", schema="1"):
        def mk(tier):
            return {
                "tool_version": "x", "schema_version": schema, "python": "3.12",
                "tier": tier, "commit": "c", "created_utc": "t",
        }
        return mk(tier_a), mk(tier_b)

    def test_matching_records_are_compatible(self):
        a, b = self._pair()
        assert prov.check_compatible(a, b) is None

    def test_tier_mismatch_refuses(self):
        a, b = self._pair("baseline", "enhanced")
        with pytest.raises(prov.ProvenanceError, match="tier mismatch"):
            prov.check_compatible(a, b)

    def test_schema_mismatch_refuses_first(self):
        a, b = self._pair("baseline", "enhanced", schema="999")
        with pytest.raises(prov.ProvenanceError, match="schema_version"):
            prov.check_compatible(a, b)


def _gen_args(mod, out=None, **overrides):
    argv = [
        "--target-type", "file", "--file", str(mod),
        "--min-n", "10", "--max-n", "50", "--n-measures", "2",
    ]
    if out is not None:
        argv += ["--output", str(out)]
    return argv


class TestCommitScoping:
    def _git_repo_with_target(self, tmp_path):
        import shutil
        import subprocess

        if shutil.which("git") is None:
            pytest.skip("git not available")
        repo = tmp_path / "target_repo"
        repo.mkdir()
        mod = repo / "mod.py"
        mod.write_text("def f(xs):\n    return [x for x in xs]\n")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qm", "init"],
            cwd=repo, check=True,
        )
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo, check=True, capture_output=True, text=True,
        ).stdout.strip()
        return repo, mod, sha

    def test_commit_scoped_to_target_repo_not_cwd(self, tmp_path):
        repo, mod, sha = self._git_repo_with_target(tmp_path)
        out_path = tmp_path / "report.csv"
        gen.main(_gen_args(mod, out_path))
        proven, _ = prov.split_preamble(out_path.read_text())
        assert proven["commit"] == sha

    def test_non_git_target_reports_unknown(self, tmp_path):
        mod = tmp_path / "mod.py"
        mod.write_text("def f(xs):\n    return [x for x in xs]\n")
        proven = prov.current_provenance(path=str(mod))
        assert proven["commit"] == "unknown"

    def test_file_and_dir_paths_agree(self, tmp_path):
        repo, mod, sha = self._git_repo_with_target(tmp_path)
        assert prov.current_provenance(path=str(mod))["commit"] == sha
        assert prov.current_provenance(path=str(repo))["commit"] == sha


class TestProducerConsumerChain:
    def test_csv_file_carries_preamble_and_rows(self, tmp_path):
        mod = _quick_module(tmp_path)
        out_path = tmp_path / "report.csv"
        gen.main(_gen_args(mod, out_path))
        text = out_path.read_text()
        assert text.splitlines()[0].startswith("# ")
        proven, body = prov.split_preamble(text)
        assert proven["schema_version"] == str(prov.SCHEMA_VERSION)
        rows = list(csv.DictReader(io.StringIO(body)))
        assert [r["function"] for r in rows] == ["f"]

    def test_stdout_path_carries_preamble(self, tmp_path, capsys):
        gen.main(_gen_args(_quick_module(tmp_path)))
        out = capsys.readouterr().out
        assert out.splitlines()[0].startswith("# ")

    def test_classify_accepts_fresh_csv_and_preserves_provenance(self, tmp_path):
        mod = _quick_module(tmp_path)
        in_path = tmp_path / "in.csv"
        gen.main(_gen_args(mod, in_path))
        out_path = tmp_path / "classified.csv"
        clf.main(["--input", str(in_path), "--output", str(out_path)])
        proven, body = prov.split_preamble(out_path.read_text())
        assert proven["schema_version"] == str(prov.SCHEMA_VERSION)
        rows = list(csv.DictReader(io.StringIO(body)))
        assert "tier" in rows[0] and rows[0]["function"] == "f"

    def test_old_csv_without_preamble_fails_closed(self, tmp_path, capsys):
        in_path = tmp_path / "old.csv"
        in_path.write_text(
            "file,function,empirical_big_o,complexity_rank,big_o_error\n"
            "mod.py,f,Linear,2,\n"
        )
        with pytest.raises(SystemExit) as exc_info:
            clf.main(["--input", str(in_path)])
        assert exc_info.value.code == 2
        assert "schema_version" in capsys.readouterr().err

    def test_tampered_schema_fails_closed(self, tmp_path, capsys):
        in_path = tmp_path / "tampered.csv"
        in_path.write_text(
            "# schema_version=999\n"
            "file,function,empirical_big_o,complexity_rank,big_o_error\n"
            "mod.py,f,Linear,2,\n"
        )
        with pytest.raises(SystemExit) as exc_info:
            clf.main(["--input", str(in_path)])
        assert exc_info.value.code == 2
        assert "Regenerate" in capsys.readouterr().err
