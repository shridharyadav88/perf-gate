"""Tests for ruff_perf.py (Ruff PERF ingest lane)."""

from __future__ import annotations

import csv
import importlib.resources
import importlib.util
import io
import json
import shutil
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


ruff_mod = _load_module("_test_ruff", "perf-gate/scripts/ruff_perf.py")
render_mod = _load_module(
    "_test_ruff_render", "perf-gate/scripts/render_action_list.py"
)

def _ruff_usable():
    import importlib.util

    return shutil.which("ruff") is not None or \
        importlib.util.find_spec("ruff") is not None


needs_ruff = pytest.mark.skipif(
    not _ruff_usable(), reason="neither ruff binary nor module available")


def _finding(code, file="m.py", line=4):
    return ruff_mod.RuffFinding(
        file=file, line=line, col=9, end_line=line, code=code,
        message=f"{code} message", fix_available=False,
    )


class TestNormalize:
    def test_full_record(self):
        recs = [{
            "cell": None, "code": "PERF401", "filename": "m.py",
            "location": {"column": 9, "row": 4},
            "end_location": {"column": 26, "row": 4},
            "message": "Use a list comprehension",
            "fix": {"applicability": "safe", "edits": []},
        }]
        (finding,) = ruff_mod.normalize(recs)
        assert (finding.file, finding.line, finding.col) == ("m.py", 4, 9)
        assert finding.code == "PERF401"
        assert finding.fix_available is True

    def test_missing_keys_and_bad_shapes_skipped(self):
        recs = [
            {"code": "PERF401", "filename": "m.py",
             "location": {"column": 9, "row": 4}, "message": "m"},  # minimal ok
            {"code": "PERF402"},  # no location: skipped
            {"code": "PERF403", "filename": "m.py",
             "location": {"column": 1, "row": "x"}, "message": "m"},  # bad row
            "not-a-dict",
            {"code": "PERF101", "filename": "m.py",
             "location": {"column": 1, "row": 0}, "message": "m"},  # row 0
        ]
        findings = ruff_mod.normalize(recs)
        assert [(f.code, f.line) for f in findings] == [("PERF401", 4)]
        assert findings[0].fix_available is False

    def test_non_list_payload_is_empty(self):
        assert ruff_mod.normalize([]) == []


class TestDedup:
    def test_overlapping_401_dropped_others_kept(self):
        findings = [
            _finding("PERF401", line=4),   # inside our range
            _finding("PERF401", line=40),  # outside: kept
            _finding("PERF102", line=4),   # ruff-unique: never deduped
        ]
        kept, dropped = ruff_mod.dedup(findings, {"m.py": [(1, 10)]})
        assert [(f.code, f.line) for f in kept] == [
            ("PERF401", 40), ("PERF102", 4)]
        assert dropped == 1

    def test_overlapping_403_dropped_in_range(self):
        findings = [
            _finding("PERF403", line=4),   # inside our range: deduped
            _finding("PERF403", line=40),  # outside: kept
        ]
        kept, dropped = ruff_mod.dedup(findings, {"m.py": [(1, 10)]})
        assert [(f.code, f.line) for f in kept] == [("PERF403", 40)]
        assert dropped == 1

    def test_no_ranges_keeps_everything(self):
        findings = [_finding("PERF402")]
        kept, dropped = ruff_mod.dedup(findings, {})
        assert len(kept) == 1 and dropped == 0


class TestCollect:
    def test_our_ranges_cover_detector_output(self, tmp_path):
        mod = tmp_path / "m.py"
        mod.write_text("def f(xs):\n    out = []\n    for x in xs:\n"
                       "        out.append(x)\n    return out\n")
        ranges = ruff_mod.our_ranges_for_file(str(mod))
        assert ranges  # the manual list-copy loop is a known range
        assert any(start <= 4 <= end for start, end in ranges)

    def test_unparseable_file_yields_no_ranges(self, tmp_path):
        mod = tmp_path / "m.py"
        mod.write_text("def broken(:\n")
        assert ruff_mod.our_ranges_for_file(str(mod)) == []
        assert ruff_mod.our_ranges_for_file(str(tmp_path / "nope.py")) == []


@needs_ruff
class TestCli:
    def test_text_lists_and_dedups(self, tmp_path, capsys):
        mod = tmp_path / "m.py"
        mod.write_text("def f(xs):\n    out = []\n    for x in xs:\n"
                       "        out.append(x * 2)\n    return out\n")
        ruff_mod.main(["--files", str(mod)])
        out = capsys.readouterr().out
        # Our own detector covers this exact loop: ruff hit deduped, noted.
        assert "already covered by our own detectors" in out

    def test_json_emits_raw_shaped_records(self, tmp_path, capsys):
        mod = tmp_path / "m.py"
        mod.write_text("def f(xs):\n    for x in xs:\n        try:\n"
                       "            print(x)\n        except ValueError:\n"
                       "            pass\n")
        ruff_mod.main(["--files", str(mod), "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert isinstance(payload, list)
        assert payload and payload[0]["code"] == "PERF203"

    def test_strict_exits_1_on_findings(self, tmp_path):
        mod = tmp_path / "m.py"
        mod.write_text("def f(xs):\n    for x in xs:\n        try:\n"
                       "            print(x)\n        except ValueError:\n"
                       "            pass\n")
        with pytest.raises(SystemExit) as excinfo:
            ruff_mod.main(["--files", str(mod), "--strict"])
        assert excinfo.value.code == 1

    def test_missing_binary_fails_closed(self, tmp_path, capsys):
        mod = tmp_path / "m.py"
        mod.write_text("x = 1\n")
        with pytest.raises(SystemExit) as excinfo:
            ruff_mod.main(["--files", str(mod), "--ruff-bin",
                           "/nonexistent/ruff-binary"])
        assert excinfo.value.code == 2
        assert "not found" in capsys.readouterr().err

    def test_fully_deduped_reports_dedup_not_empty(self, tmp_path, capsys):
        mod = tmp_path / "m.py"
        mod.write_text("def f(xs):\n    out = []\n    for x in xs:\n"
                       "        out.append(x * 2)\n    return out\n")
        ruff_mod.main(["--files", str(mod)])
        out = capsys.readouterr().out
        assert "already covered by our own detectors" in out
        assert "No Ruff PERF findings." not in out

    def test_clean_file_reports_empty(self, tmp_path, capsys):
        mod = tmp_path / "m.py"
        mod.write_text("def f(xs):\n    return list(xs)\n")
        ruff_mod.main(["--files", str(mod)])
        assert "No Ruff PERF findings." in capsys.readouterr().out


class TestReportSection:
    def _classified(self):
        cols = ["rank", "severity", "file", "function", "empirical_big_o",
                "complexity_rank", "big_o_error", "total_hits",
                "total_hotspot_time_us", "top_hotspot_pct_time",
                "top_hotspot_line", "top_hotspot_source",
                "line_profile_error", "tier", "tier_detail"]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=cols)
        writer.writeheader()
        return ("# tool_version=0.1.0\n# schema_version=1\n# python=3.11.0\n"
                "# tier=baseline\n# commit=x\n# created_utc=t\n" + buf.getvalue())

    def test_ruff_section_lists_and_caps(self, tmp_path, capsys):
        csv_path = tmp_path / "c.csv"
        csv_path.write_text(self._classified())
        ruff_path = tmp_path / "r.json"
        ruff_path.write_text(json.dumps([
            {"code": "PERF101", "filename": "m.py",
             "location": {"column": 5, "row": i}, "message": f"hit {i}",
             "fix": {"applicability": "safe"}}
            for i in range(1, 5)
        ]))
        render_mod.main(["--input", str(csv_path), "--ruff", str(ruff_path),
                         "--top", "2"])
        out = capsys.readouterr().out
        assert "## Ruff PERF" in out
        assert "hit 1" in out and "hit 2" in out
        assert "2 more" in out
        assert "auto-fix offered" in out

    def test_no_ruff_flag_names_the_lane(self, tmp_path, capsys):
        csv_path = tmp_path / "c.csv"
        csv_path.write_text(self._classified())
        render_mod.main(["--input", str(csv_path)])
        out = capsys.readouterr().out
        assert "## Ruff PERF" in out
        assert "--ruff" in out

    def test_broken_ruff_file_degrades_quietly(self, tmp_path, capsys):
        csv_path = tmp_path / "c.csv"
        csv_path.write_text(self._classified())
        bad = tmp_path / "r.json"
        bad.write_text("nope")
        render_mod.main(["--input", str(csv_path), "--ruff", str(bad)])
        assert "## Ruff PERF" in capsys.readouterr().out
