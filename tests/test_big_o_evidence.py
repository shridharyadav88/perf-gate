"""Tests for mine_callsites.py and Big-O fit-confidence evidence (U4)."""

from __future__ import annotations

import importlib.resources
import importlib.util
import json
import sys


def _load_module(name: str, rel_path: str):
    pkg_skills = importlib.resources.files("perf_gate") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mine_mod = _load_module("_test_mine", "perf-gate/scripts/mine_callsites.py")
render_mod = _load_module(
    "_test_ev_render", "perf-gate/scripts/render_action_list.py"
)
gen_mod = _load_module(
    "_test_ev_gen", "perf-gate/scripts/generate_baseline_csv.py"
)
big_o_mod = _load_module(
    "_test_ev_big_o", "perf-gate/scripts/profilers/run_big_o.py"
)


def _repo(tmp_path):
    target = tmp_path / "target.py"
    target.write_text("def clean(text, extra=0):\n    return text.strip()\n")
    caller = tmp_path / "caller.py"
    caller.write_text("from target import clean\n\ndef run():\n"
                      "    return clean(open('a').read())\n")
    same = tmp_path / "target2.py"  # unrelated same-named function elsewhere
    same.write_text("def clean(x):\n    return x\n")
    (tmp_path / ".venv" / "x.py").parent.mkdir(exist_ok=True)
    (tmp_path / ".venv" / "x.py").write_text("from target import clean\nclean(1)\n")
    return target


class TestMiner:
    def test_signature_and_sites(self, tmp_path):
        target = _repo(tmp_path)
        sig, sites = mine_mod.mine(str(tmp_path), str(target), "clean")
        assert sig[0][0] == "text"
        assert sig[1] == ("extra", "", "0")
        hits = {(s.file, s.lineno) for s in sites}
        assert (str(tmp_path / "caller.py"), 4) in hits
        # .venv is hygiene-skipped; target2.clean is a different function.
        assert not any(".venv" in f for f, _ in hits)
        assert not any("target2" in f for f, _ in hits)

    def test_same_file_call_needs_no_import(self, tmp_path):
        target = tmp_path / "target.py"
        target.write_text("def clean(t):\n    return t\n\ndef go():\n"
                          "    return clean('x')\n")
        _, sites = mine_mod.mine(str(tmp_path), str(target), "clean")
        assert [(s.file, s.lineno) for s in sites] == [(str(target), 5)]

    def test_attribute_call_matches_module_basename(self, tmp_path):
        target = tmp_path / "target.py"
        target.write_text("def clean(t):\n    return t\n")
        user = tmp_path / "user.py"
        user.write_text("import target\n\ndef go():\n    return target.clean('x')\n")
        _, sites = mine_mod.mine(str(tmp_path), str(target), "clean")
        assert [(s.file, s.lineno) for s in sites] == [(str(user), 4)]

    def test_missing_function_errors(self, tmp_path):
        target = _repo(tmp_path)
        import pytest

        with pytest.raises(ValueError, match="no top-level"):
            mine_mod.mine(str(tmp_path), str(target), "nope")

    def test_sketch_shape(self, tmp_path):
        target = _repo(tmp_path)
        sig, sites = mine_mod.mine(str(tmp_path), str(target), "clean")
        sketch = mine_mod.render_sketch(str(target), "clean", sig, sites)
        assert "UNAPPROVED" in sketch
        assert "def harness_for_clean(data):" in sketch
        assert "n = len(data)" in sketch
        assert 'harness_for_clean.__big_o_provenance__ = "harness"' in sketch
        assert "caller.py:4" in sketch
        compile(sketch, "<sketch>", "exec")


class TestConfidence:
    def _sidecar(self, residuals, provenance="synthetic", max_n=500):
        return {("m.py", "f"): {"provenance": provenance, "max_n": max_n,
                                "residuals": residuals}}

    def test_clear_winner(self):
        row = {"file": "m.py", "function": "f",
               "empirical_big_o": "Linear: 1", "big_o_error": ""}
        rec = self._sidecar({"Linear: 1": 1e-14, "Quadratic: 2": 1e-10})
        assert render_mod.fit_confidence(row, rec) == "clear winner"

    def test_close_call_names_remedy(self):
        row = {"file": "m.py", "function": "f",
               "empirical_big_o": "Linear: 1", "big_o_error": ""}
        rec = self._sidecar({"Linear: 1": 1e-10, "Quadratic: 2": 1.2e-10})
        label = render_mod.fit_confidence(row, rec)
        assert "close call" in label and "--max-n 1000" in label

    def test_flap_when_winner_is_not_best(self):
        row = {"file": "m.py", "function": "f",
               "empirical_big_o": "Constant: 1", "big_o_error": ""}
        rec = self._sidecar({"Constant: 1": 1e-10, "Linear: 2": 1e-14})
        assert "flap risk" in render_mod.fit_confidence(row, rec)

    def test_unknown_without_sidecar_or_on_stale(self):
        row = {"file": "m.py", "function": "f",
               "empirical_big_o": "Linear: 1", "big_o_error": ""}
        assert render_mod.fit_confidence(row, {}) == ""
        stale = {("m.py", "f"): {"provenance": "synthetic",
                                 "residuals": {"Other: 9": 1.0}}}
        assert render_mod.fit_confidence(row, stale) == ""
        single = {("m.py", "f"): {"provenance": "synthetic",
                                  "residuals": {"Linear: 1": 1.0}}}
        assert render_mod.fit_confidence(row, single) == ""

    def test_harness_label_carries_confidence(self):
        row = {"file": "m.py", "function": "f",
               "empirical_big_o": "Linear: 1", "big_o_error": ""}
        rec = self._sidecar({"Linear: 1": 1e-14, "Quadratic: 2": 1e-10},
                            provenance="harness")
        label = render_mod.big_o_label(row, rec)
        assert "measured, harness input" in label and "clear winner" in label

    def test_probe_log_records_marker_and_residuals(self):
        def f(xs):
            return [x for x in xs]

        log: dict = {}
        big_o_mod.profile_big_o(f, min_n=50, max_n=500, n_measures=3,
                                probe_log=log)
        assert log["provenance"] == "synthetic"
        assert isinstance(log["residuals"], dict) and len(log["residuals"]) >= 2

        def h(xs):
            return [x for x in xs]

        h.__big_o_provenance__ = "harness"
        log2: dict = {}
        big_o_mod.profile_big_o(h, min_n=50, max_n=500, n_measures=3,
                                probe_log=log2)
        assert log2["provenance"] == "harness"

        def g(xs):
            return [x for x in xs]

        g.__big_o_provenance__ = "whatever"
        log3: dict = {}
        big_o_mod.profile_big_o(g, min_n=50, max_n=500, n_measures=3,
                                probe_log=log3)
        assert log3["provenance"] == "synthetic"


class TestHarnessEndToEnd:
    def test_approved_sketch_row_reports_measured(self, tmp_path, capsys):
        target = tmp_path / "target.py"
        target.write_text("def total(xs):\n    return sum(xs)\n")
        caller = tmp_path / "caller.py"
        caller.write_text("from target import total\n\ndef go(ns):\n"
                          "    return [total(range(n)) for n in ns]\n")
        sig, sites = mine_mod.mine(str(tmp_path), str(target), "total")
        assert sites  # the miner found the real call site
        sketch_path = tmp_path / "harness_total.py"
        sketch_path.write_text(
            mine_mod.render_sketch(str(target), "total", sig, sites))

        class Args:
            min_n = 50
            max_n = 500
            n_measures = 3
            n_timings = 1
            n_repeats = 1
            timeout = 60

        records: list = []
        row = gen_mod._build_one_row(
            str(sketch_path), "harness_for_total", {}, Args(), records)
        assert row["big_o_error"] == "", row["big_o_error"]
        assert records[0]["provenance"] == "harness"

        sidecar = {("harness_total.py", "harness_for_total"): records[0]}
        row = dict(row, file="harness_total.py", function="harness_for_total")
        label = render_mod.big_o_label(row, sidecar)
        assert "measured, harness input" in label
        assert json_sidecar_roundtrip(records)

    def test_sidecar_json_safe(self, tmp_path):
        assert json_sidecar_roundtrip(
            [{"file": "a", "function": "f", "provenance": "synthetic",
              "shape_desc": "x", "min_n": 1, "max_n": 2, "n_measures": 3,
              "residuals": {"A": 1.0, "B": None}}])


def json_sidecar_roundtrip(records):
    path = "/tmp/_sidecar_probe.json"
    gen_mod.write_sidecar(path, records)
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    return payload["rows"] == records
