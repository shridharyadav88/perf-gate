"""Tests for render_action_list.py and the Big-O input-provenance sidecar."""

from __future__ import annotations

import csv
import importlib.resources
import importlib.util
import io
import json
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


render_mod = _load_module(
    "_test_render", "code-optimizer/scripts/render_action_list.py"
)
gen_mod = _load_module(
    "_test_render_gen", "code-optimizer/scripts/generate_baseline_csv.py"
)
big_o_mod = _load_module(
    "_test_render_big_o", "code-optimizer/scripts/profilers/run_big_o.py"
)

BASE_COLUMNS = list(gen_mod.FIELDNAMES) + ["tier", "tier_detail"]

PREAMBLE = (
    "# tool_version=0.1.0\n# schema_version=1\n# python=3.11.0\n"
    "# tier=baseline\n# commit=abc123\n# created_utc=2026-01-01T00:00:00+00:00\n"
)


def _row(**overrides):
    row = {c: "" for c in BASE_COLUMNS}
    row.update({
        "rank": "1", "severity": "Unknown", "file": "a.py", "function": "f",
        "empirical_big_o": "", "complexity_rank": "0", "big_o_error": "",
        "total_hits": "0", "total_hotspot_time_us": "0.0",
        "top_hotspot_pct_time": "0.0", "top_hotspot_line": "",
        "top_hotspot_source": "", "line_profile_error": "",
        "tier": "not_actionable", "tier_detail": "",
    })
    row.update(overrides)
    return row


def _csv_text(rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=BASE_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return PREAMBLE + buf.getvalue()


class TestBigOLabel:
    def test_synthetic_constant_is_unmeasured(self):
        row = _row(empirical_big_o="Constant: time = 1e-06 (sec)")
        sidecar = {("a.py", "f"): {"provenance": "synthetic"}}
        label = render_mod.big_o_label(row, sidecar)
        assert "unmeasured" in label
        assert "synthetic micro-input" in label

    def test_synthetic_non_constant_is_weak(self):
        row = _row(empirical_big_o="Linear: time = 1e-06 * n (sec)")
        sidecar = {("a.py", "f"): {"provenance": "synthetic"}}
        assert "weak" in render_mod.big_o_label(row, sidecar)

    def test_harness_is_measured(self):
        row = _row(empirical_big_o="Linear: time = 1e-06 * n (sec)")
        sidecar = {("a.py", "f"): {"provenance": "harness"}}
        assert "measured" in render_mod.big_o_label(row, sidecar)

    def test_missing_sidecar_is_unknown_not_a_finding(self):
        row = _row(empirical_big_o="Constant: time = 1e-06 (sec)")
        label = render_mod.big_o_label(row, {})
        assert "unknown" in label
        assert "measured" not in label

    def test_failed_row_labels_failed(self):
        row = _row(big_o_error="No module named 'bs4'")
        assert render_mod.big_o_label(row, {}) == "failed"


class TestRender:
    def test_tier0_leads_with_resolver_and_honest_label(self):
        rows = [
            _row(file="m.py", function="scrub", tier="tier0_regex_hoist",
                 tier_detail="hoist re.compile at line 3",
                 empirical_big_o="Constant: time = 1e-06 (sec)",
                 top_hotspot_line="3", top_hotspot_source="pat = re.compile(x)"),
            _row(file="m.py", function="ok", tier="tier1_review",
                 empirical_big_o="Linear: time = 1e-06 * n (sec)"),
            _row(file="c.py", function="main",
                 big_o_error="'main' takes no arguments; Big-O needs a size parameter",
                 line_profile_error="boom"),
        ]
        sidecar = {
            ("m.py", "scrub"): {"provenance": "synthetic"},
            ("m.py", "ok"): {"provenance": "harness"},
        }
        report = render_mod.render(rows, sidecar, top=10, provenance={})
        fix_at = report.index("## Fix now")
        review_at = report.index("## Review")
        appendix_at = report.index("## By design")
        assert fix_at < review_at < appendix_at
        assert "apply_regex_hoist" in report
        assert "unmeasured" in report
        assert "measured, harness input" in report
        assert "1 rows: by design" in report
        assert "1 rows are dark on both profilers" in report

    def test_new_tier0_renders_with_its_resolver(self):
        rows = [
            _row(file="m.py", function="total", tier="tier0_consumer_list",
                 tier_detail="feed sum(...) a generator instead of a list (line 2)",
                 empirical_big_o="Linear: time = 1e-06 * n (sec)"),
        ]
        report = render_mod.render(rows, {}, top=10, provenance={})
        assert "## Fix now" in report
        assert "resolvers/apply_consumer.py" in report

    def test_tier1_cap_and_overflow_note(self):
        rows = [_row(file="m.py", function=f"f{i}", tier="tier1_review")
                for i in range(4)]
        report = render_mod.render(rows, {}, top=2, provenance={})
        assert "f0" in report and "f1" in report
        assert "f3" not in report
        assert "2 more tier1 rows" in report

    def test_empty_report_names_empty_sections(self):
        report = render_mod.render([], {}, top=10, provenance={})
        assert "(none" in report


class TestMain:
    def test_missing_tier_column_fails_closed(self, tmp_path, capsys):
        path = tmp_path / "raw.csv"
        path.write_text(PREAMBLE + "file,function\nm.py,f\n")
        with pytest.raises(SystemExit) as excinfo:
            render_mod.main(["--input", str(path)])
        assert excinfo.value.code == 2
        assert "classify_findings" in capsys.readouterr().err

    def test_broken_sidecar_never_fails_report(self, tmp_path, capsys):
        csv_path = tmp_path / "classified.csv"
        csv_path.write_text(_csv_text([_row(tier="tier1_review")]))
        bad = tmp_path / "classified.inputs.json"
        bad.write_text("{not json")
        render_mod.main(["--input", str(csv_path), "--inputs", str(bad)])
        out = capsys.readouterr().out
        assert "Performance action list" in out
        assert "provenance unknown" in out


class TestProbeLog:
    def test_success_records_synthetic_provenance(self):
        def f(xs):
            return [x for x in xs]

        log: dict = {}
        best, _ = big_o_mod.profile_big_o(
            f, min_n=50, max_n=500, n_measures=3, probe_log=log)
        assert best
        assert log["provenance"] == "synthetic"
        assert log["shape_desc"].startswith("scale ")
        assert (log["min_n"], log["max_n"], log["n_measures"]) == (50, 500, 3)

    def test_failure_leaves_log_untouched(self):
        def f():
            return 1

        log: dict = {}
        with pytest.raises(ValueError):
            big_o_mod.profile_big_o(f, min_n=10, max_n=30, n_measures=2,
                                    probe_log=log)
        assert log == {}

    def test_sidecar_round_trip_on_success_and_failure(self, tmp_path):
        mod = tmp_path / "mod.py"
        mod.write_text("def good(xs):\n    return [x for x in xs]\n")
        records: list = []

        class Args:
            min_n = 50
            max_n = 500
            n_measures = 3
            n_timings = 1
            n_repeats = 1
            timeout = 30

        cache: dict = {}
        row = gen_mod._build_one_row(str(mod), "good", cache, Args(), records)
        assert row["big_o_error"] == ""
        row = gen_mod._build_one_row(str(mod), "missing", cache, Args(), records)
        assert row["big_o_error"] != ""
        assert [r["provenance"] for r in records] == ["synthetic", None]

        sidecar = tmp_path / "r.inputs.json"
        gen_mod.write_sidecar(str(sidecar), records)
        payload = json.loads(sidecar.read_text())
        assert payload["generated_by"] == "generate_baseline_csv"
        assert len(payload["rows"]) == 2

    def test_sidecar_path_derivation(self):
        assert gen_mod.sidecar_path("r.csv") == "r.inputs.json"
        assert gen_mod.sidecar_path("r") == "r.inputs.json"


class TestReachabilityOrdering:
    def _reach(self):
        return {
            ("m.py", "hot"): {"callers": 5, "entry_reachable": False,
                              "value": "5 in-repo callers"},
            ("m.py", "entry"): {"callers": 1, "entry_reachable": True,
                                "value": "entry-reachable, no counted callers"},
            ("m.py", "quiet"): {"callers": 0, "entry_reachable": False,
                                "value": "no in-repo callers (public API?)"},
        }

    def test_entry_reachable_outranks_more_callers(self):
        rows = [_row(file="m.py", function=f, tier="tier1_review")
                for f in ("hot", "entry", "quiet")]
        report = render_mod.render(
            rows, {}, top=10, provenance={}, reach=self._reach())
        assert (report.index("### m.py :: entry")
                < report.index("### m.py :: hot")
                < report.index("### m.py :: quiet"))

    def test_reachability_lines_render_only_for_known_rows(self):
        rows = [_row(file="m.py", function=f, tier="tier1_review")
                for f in ("hot", "stranger")]
        report = render_mod.render(
            rows, {}, top=10, provenance={}, reach=self._reach())
        assert "- reachability: 5 in-repo callers" in report
        assert report.count("- reachability:") == 1

    def test_unknown_rows_sort_with_known_zero_not_first(self):
        # No reachability file at all: every row ties, input order stands.
        rows = [_row(file="m.py", function=f, tier="tier1_review")
                for f in ("aaa", "zzz")]
        report = render_mod.render(rows, {}, top=10, provenance={})
        assert (report.index("### m.py :: aaa")
                < report.index("### m.py :: zzz"))
        # Against a row with real evidence, the unknown sorts after it.
        rows = [_row(file="m.py", function=f, tier="tier1_review")
                for f in ("stranger", "hot")]
        report = render_mod.render(
            rows, {}, top=10, provenance={}, reach=self._reach())
        assert (report.index("### m.py :: hot")
                < report.index("### m.py :: stranger"))


class TestSweepFooter:
    def test_footer_names_targets_and_zero_token_cost(self):
        report = render_mod.render(
            [_row(tier="tier1_review")], {}, top=10, provenance={},
            meta={"targets": 12, "started_unix": 1000.0,
                  "finished_unix": 1045.0})
        assert ("_Sweep cost (wall-clock, this machine): 12 targets in 45s. "
                "Token cost: zero -- this report is deterministic._") in report

    def test_long_sweep_renders_minutes(self):
        report = render_mod.render(
            [], {}, top=10, provenance={},
            meta={"targets": 3, "started_unix": 0.0, "finished_unix": 600.0})
        assert "in 10m" in report

    def test_missing_or_inverted_meta_omits_footer(self):
        rows = [_row(tier="tier1_review")]
        assert "Sweep cost" not in render_mod.render(
            rows, {}, top=10, provenance={}, meta={})
        assert "Sweep cost" not in render_mod.render(
            rows, {}, top=10, provenance={},
            meta={"targets": 1, "started_unix": 50.0, "finished_unix": 40.0})

    def test_broken_reachability_file_never_fails_report(self, tmp_path):
        bad = tmp_path / "reach.json"
        bad.write_text("{not json")
        assert render_mod.load_reachability(str(bad)) == {}
        assert render_mod.load_reachability(None) == {}


class TestRuffLane:
    def _report(self, ruff_rows):
        return render_mod.render(
            [_row(tier="tier1_review")], {}, top=10, provenance={},
            ruff_rows=ruff_rows)

    def test_no_file_invites_flag(self):
        report = self._report(None)
        assert "(none -- pass --ruff <ruff-perf.json> to include this lane)" in report
        assert "zero findings" not in report

    def test_empty_file_reports_clean(self):
        report = self._report([])
        assert "(none -- ruff PERF checked, zero findings)" in report
        assert "pass --ruff" not in report

    def test_findings_render_with_fix_note(self):
        rows = [{"code": "PERF401", "file": "a.py", "line": 3,
                 "message": "use a list comprehension", "fix": True},
                {"code": "E501", "file": "a.py", "line": 9,
                 "message": "not a PERF rule"}]
        report = self._report(rows)
        assert "- `a.py:3` [PERF401] use a list comprehension" in report
        assert "[ruff auto-fix offered]" in report
        assert "E501" not in report

    def test_load_ruff_none_means_not_requested(self, tmp_path):
        assert render_mod.load_ruff(None) is None
        bad = tmp_path / "ruff.json"
        bad.write_text("{not json")
        assert render_mod.load_ruff(str(bad)) == []
        ok = tmp_path / "ok.json"
        ok.write_text("[]")
        assert render_mod.load_ruff(str(ok)) == []


class TestHeatLane:
    def _heat_file(self, tmp_path, rows, meta=None):
        payload = {"generated_by": "profile_heat", "python": "3.9.6",
                   "source_files": [], "rows": rows}
        if meta:
            payload.update(meta)
        path = tmp_path / "heat.json"
        path.write_text(json.dumps(payload))
        return str(path)

    def _mkrow(self, func, tier="tier1_review"):
        return _row(file="m.py", function=func, tier=tier)

    def test_hot_beats_entry_beats_unprofiled(self, tmp_path):
        path = self._heat_file(tmp_path, [
            {"file": "m.py", "function": "warm", "cumtime_s": 1.0,
             "tottime_s": 1.0, "share": 0.1, "calls": 5},
            {"file": "m.py", "function": "hot", "cumtime_s": 9.0,
             "tottime_s": 8.0, "share": 0.9, "calls": 50},
        ])
        heat, _ = render_mod.load_heat(path)
        reach = {("m.py", "warm"): {"callers": 0, "entry_reachable": True,
                                    "value": "entry"},
                 ("m.py", "hot"): {"callers": 0, "entry_reachable": False,
                                   "value": "cold"}}
        rows = [self._mkrow(f) for f in ("warm", "quiet", "hot")]
        report = render_mod.render(rows, {}, top=10, provenance={},
                                   reach=reach, heat=heat)
        assert (report.index("### m.py :: hot")
                < report.index("### m.py :: warm")
                < report.index("### m.py :: quiet"))

    def test_heat_lines_name_seconds_and_share(self, tmp_path):
        path = self._heat_file(tmp_path, [
            {"file": "m.py", "function": "hot", "cumtime_s": 12.345,
             "tottime_s": 2.0, "share": 0.456, "calls": 7},
        ])
        heat, _ = render_mod.load_heat(path)
        report = render_mod.render([self._mkrow("hot")], {}, top=10,
                                   provenance={}, heat=heat)
        assert "- heat: 12.345s cumulative (45.6% of profile)" in report

    def test_lane_off_leaves_no_heat_lines(self):
        report = render_mod.render([self._mkrow("hot")], {}, top=10,
                                   provenance={}, heat=None)
        assert "- heat:" not in report

    def test_broken_file_fails_open(self, tmp_path):
        bad = tmp_path / "heat.json"
        bad.write_text("{not json")
        heat, meta = render_mod.load_heat(str(bad))
        assert heat == {} and meta == {}
        assert render_mod.load_heat(None) == (None, {})

    def test_raw_list_shape_tolerated(self, tmp_path):
        path = tmp_path / "heat.json"
        path.write_text(json.dumps([
            {"file": "m.py", "function": "hot", "cumtime_s": 1.0,
             "tottime_s": 1.0, "share": 1.0, "calls": 1},
        ]))
        heat, _ = render_mod.load_heat(str(path))
        report = render_mod.render([self._mkrow("hot")], {}, top=10,
                                   provenance={}, heat=heat)
        assert "- heat: 1.000s cumulative (100.0% of profile)" in report

    def test_stale_python_warns(self, tmp_path):
        path = self._heat_file(tmp_path, [], meta={"python": "3.12.1"})
        heat, heat_meta = render_mod.load_heat(path)
        report = render_mod.render(
            [self._mkrow("hot")], {}, top=10,
            provenance={"tool_version": "0.1.0", "tier": "baseline",
                        "python": "3.9.6"},
            heat=heat, heat_meta=heat_meta)
        assert "Profile recorded under Python 3.12.1" in report
        same, _ = render_mod.load_heat(self._heat_file(tmp_path, []))
        clean = render_mod.render(
            [self._mkrow("hot")], {}, top=10,
            provenance={"tool_version": "0.1.0", "tier": "baseline",
                        "python": "3.9.6"},
            heat=same, heat_meta={"python": "3.9.6"})
        assert "Profile recorded under Python" not in clean

    def _dark_row(self, func):
        return _row(file="m.py", function=func, tier="not_actionable",
                    big_o_error="'x' takes no arguments; Big-O needs a size",
                    line_profile_error="boom")

    def test_dark_row_with_heat_is_surfaced(self, tmp_path):
        path = self._heat_file(tmp_path, [
            {"file": "m.py", "function": "dark_hot", "cumtime_s": 2.5,
             "tottime_s": 2.0, "share": 0.25, "calls": 3},
        ])
        heat, _ = render_mod.load_heat(path)
        report = render_mod.render(
            [self._dark_row("dark_hot"), self._dark_row("dark_cold")],
            {}, top=10, provenance={}, heat=heat)
        assert "2 rows are dark on both profilers" in report
        assert "heat-visible dark rows" in report
        assert "`m.py :: dark_hot` -- 2.500s cumulative (25.0% of profile)" \
            in report
        assert "dark_cold` --" not in report

    def test_dark_rows_without_heat_unchanged(self, tmp_path):
        path = self._heat_file(tmp_path, [])
        heat, _ = render_mod.load_heat(path)
        report = render_mod.render([self._dark_row("dark_cold")], {}, top=10,
                                   provenance={}, heat=heat)
        assert "1 rows are dark on both profilers" in report
        assert "heat-visible dark rows" not in report

    def test_lane_off_leaves_no_dark_heat_lines(self):
        report = render_mod.render([self._dark_row("dark_hot")], {}, top=10,
                                   provenance={}, heat=None)
        assert "heat-visible dark rows" not in report


class TestTierRegistry:
    """Render and classify must agree on the tier0 universe.

    A tier the classifier emits but the renderer doesn't list vanishes
    from the action section -- the worst failure mode this tool has.
    """

    def _classify(self):
        return _load_module(
            "_test_classify_for_registry",
            "code-optimizer/scripts/classify_findings.py",
        )

    def test_priority_tuples_match(self):
        cf = self._classify()
        assert (list(render_mod.TIER0_PRIORITY)
                == list(cf._TIER0_PRIORITY))

    def test_resolver_map_covers_every_tier(self):
        cf = self._classify()
        missing = [t for t in cf._TIER0_PRIORITY
                   if t not in render_mod.TIER0_RESOLVER]
        assert missing == []

    def test_every_tier_renders_in_fix_now(self):
        cf = self._classify()
        rows = [{"file": "m.py", "function": f"f{i}",
                 "empirical_big_o": "Linear", "complexity_rank": "2",
                 "big_o_error": "", "line_profile_error": "",
                 "tier": tier, "tier_detail": "d"}
                for i, tier in enumerate(cf._TIER0_PRIORITY)]
        report = render_mod.render(rows, {}, top=10, provenance={})
        for i, tier in enumerate(cf._TIER0_PRIORITY):
            assert f"### m.py :: f{i}" in report, tier
