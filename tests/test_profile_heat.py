"""Tests for profile_heat.py (cProfile/pstats heat ingest)."""

from __future__ import annotations

import importlib.resources
import importlib.util
import json
import pstats
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


heat_mod = _load_module("_test_heat", "perf-gate/scripts/profile_heat.py")

TARGET = (
    "def hot(n):\n"
    "    total = 0\n"
    "    for i in range(n):\n"
    "        total += i\n"
    "    return total\n"
    "\n"
    "\n"
    "class Svc:\n"
    "    def method(self, n):\n"
    "        return n\n"
    "\n"
    "\n"
    "def cold(n):\n"
    "    return n\n"
)


def _stats(entries):
    st = pstats.Stats()
    st.stats = dict(entries)
    st.total_tt = sum(v[2] for v in entries.values())
    return st


def _entry(path, lineno, name, ct, tt=None, nc=1):
    return (path, lineno, name), (1, nc, tt if tt is not None else ct, ct, {})


class TestDefLines:
    def test_module_level_only(self, tmp_path):
        target = tmp_path / "target.py"
        target.write_text(TARGET)
        assert heat_mod.def_lines(str(target)) == {1: "hot", 13: "cold"}

    def test_broken_source_is_empty(self, tmp_path):
        target = tmp_path / "target.py"
        target.write_text("def broken(:\n")
        assert heat_mod.def_lines(str(target)) == {}
        assert heat_mod.def_lines(str(tmp_path / "missing.py")) == {}


class TestHeatIndex:
    def test_rank_orders_by_cumtime_with_shares(self, tmp_path):
        target = tmp_path / "target.py"
        target.write_text(TARGET)
        path = str(target)
        stats = _stats(dict([
            _entry(path, 1, "hot", 9.0, tt=8.0, nc=10),
            _entry(path, 13, "cold", 1.0, tt=1.0, nc=2),
        ]))
        rows, skipped = heat_mod.heat_index(stats)
        assert [r["function"] for r in rows] == ["hot", "cold"]
        assert rows[0]["share"] == pytest.approx(0.9)
        assert rows[0]["calls"] == 10
        assert rows[0]["tottime_s"] == pytest.approx(8.0)
        assert skipped == {"non_python": 0, "unparseable": 0,
                           "non_module_level": 0}

    def test_methods_and_builtins_skipped(self, tmp_path):
        target = tmp_path / "target.py"
        target.write_text(TARGET)
        path = str(target)
        stats = _stats(dict([
            _entry(path, 1, "hot", 4.0),
            _entry(path, 10, "method", 4.0),  # def line is class-level
            _entry("~", 0, "method 'append' of 'list' objects", 2.0),
            _entry(path, 2, "<listcomp>", 1.0),
        ]))
        rows, skipped = heat_mod.heat_index(stats)
        assert [r["function"] for r in rows] == ["hot"]
        assert skipped["non_python"] == 1
        assert skipped["non_module_level"] == 2

    def test_missing_file_is_unparseable(self, tmp_path):
        stats = _stats(dict([
            _entry(str(tmp_path / "gone.py"), 1, "hot", 3.0),
            _entry(str(tmp_path / "gone.py"), 5, "other", 2.0),
        ]))
        rows, skipped = heat_mod.heat_index(stats)
        assert rows == []
        assert skipped["unparseable"] == 2

    def test_zero_total_gives_zero_shares(self, tmp_path):
        target = tmp_path / "target.py"
        target.write_text(TARGET)
        stats = _stats(dict([_entry(str(target), 1, "hot", 0.0)]))
        rows, _ = heat_mod.heat_index(stats)
        assert rows[0]["share"] == 0.0


class TestCli:
    def test_json_payload_carries_meta(self, tmp_path, capsys):
        target = tmp_path / "target.py"
        target.write_text(TARGET)
        dump = tmp_path / "run.pstats"
        import subprocess
        driver = tmp_path / "driver.py"
        driver.write_text(
            "import cProfile, sys\n"
            f"sys.path.insert(0, {str(tmp_path)!r})\n"
            "import target\n"
            f"cProfile.run('target.hot(500); target.cold(1)', "
            f"{str(dump)!r})\n")
        subprocess.run([sys.executable, str(driver)], check=True,
                       capture_output=True)
        heat_mod.main(["--profile", str(dump), "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["generated_by"] == "profile_heat"
        assert payload["python"]
        assert payload["source_files"] == [str(dump)]
        assert payload["rows"]

    def test_missing_dump_exits_two(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc:
            heat_mod.main(["--profile", str(tmp_path / "nope.pstats")])
        assert exc.value.code == 2
