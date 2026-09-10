"""Tests for apply_and_verify.py (tier0 mechanic loop)."""

from __future__ import annotations

import importlib.resources
import importlib.util
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


mech_mod = _load_module("_test_mech", "perf-gate/scripts/apply_and_verify.py")

HOISTABLE = (
    "import re\n"
    "\n"
    "def clean(text):\n"
    '    pat = re.compile(r"\\s+")\n'
    '    return pat.sub(" ", text)\n'
    "\n"
    "def plain(xs):\n"
    "    return list(xs)\n"
)


def _write(tmp_path, source=HOISTABLE):
    mod = tmp_path / "target.py"
    mod.write_text(source)
    return mod


class TestDryRun:
    def test_plan_shown_nothing_written(self, tmp_path, capsys):
        mod = _write(tmp_path)
        before = mod.read_bytes()
        mech_mod.main(["--file", str(mod), "--func", "clean", "--dry-run"])
        assert mod.read_bytes() == before
        out = capsys.readouterr().out
        assert "would apply" in out
        assert "hoist 'pat'" in out

    def test_no_fix_reported(self, tmp_path, capsys):
        mod = _write(tmp_path)
        mech_mod.main(["--file", str(mod), "--func", "plain", "--dry-run"])
        assert "NO-FIX" in capsys.readouterr().out


class TestDiff:
    def test_dry_run_diff_prints_patch_without_writing(
            self, tmp_path, capsys):
        mod = _write(tmp_path)
        before = mod.read_bytes()
        mech_mod.main(["--file", str(mod), "--func", "clean",
                       "--dry-run", "--diff"])
        assert mod.read_bytes() == before
        out = capsys.readouterr().out
        assert "```diff" in out
        assert "-    pat = re.compile" in out
        assert "+pat = re.compile" in out

    def test_dry_run_without_diff_has_no_fence(self, tmp_path, capsys):
        mod = _write(tmp_path)
        mech_mod.main(["--file", str(mod), "--func", "clean", "--dry-run"])
        assert "```diff" not in capsys.readouterr().out

    def test_live_diff_shown_even_when_reverted(self, tmp_path, capsys):
        mod = _write(tmp_path)
        before = mod.read_bytes()
        mech_mod.main(["--file", str(mod), "--func", "clean",
                       "--min-improvement", "999", "--diff"])
        out = capsys.readouterr().out
        assert "REVERTED" in out
        assert "```diff" in out
        assert mod.read_bytes() == before


class TestLiveLoop:
    def test_keep_or_revert_preserves_invariants(self, tmp_path, capsys):
        mod = _write(tmp_path)
        before = mod.read_bytes()
        mech_mod.main(["--file", str(mod), "--func", "clean"])
        out = capsys.readouterr().out
        after = mod.read_bytes()
        assert "KEPT" in out or "REVERTED" in out
        if "KEPT" in out:
            assert after != before
            assert b"pat = re.compile" in after
            compile_pos = after.index(b"pat = re.compile")
            assert after.index(b"def clean") > compile_pos  # hoisted above use
        else:
            assert after == before

    def test_impossible_margin_always_reverts_identically(self, tmp_path, capsys):
        mod = _write(tmp_path)
        before = mod.read_bytes()
        mech_mod.main([
            "--file", str(mod), "--func", "clean", "--min-improvement", "999",
        ])
        out = capsys.readouterr().out
        assert "REVERTED" in out
        assert mod.read_bytes() == before

    def test_str_join_tier_runs_end_to_end(self, tmp_path, capsys):
        mod = tmp_path / "target.py"
        mod.write_text(
            "def join(xs):\n"
            '    s = ""\n'
            "    for x in xs:\n"
            "        s += str(x)\n"
            "    return s\n"
        )
        before = mod.read_bytes()
        mech_mod.main(["--file", str(mod), "--func", "join", "--tier", "str_join"])
        out = capsys.readouterr().out
        assert "KEPT" in out or "REVERTED" in out
        assert "str_join" in out
        if "REVERTED" in out:
            assert mod.read_bytes() == before

    def test_re_call_tier_runs_end_to_end(self, tmp_path, capsys):
        mod = tmp_path / "target.py"
        mod.write_text(
            "import re\n"
            "\n"
            "def find(s):\n"
            '    return re.search(r"\\d+", s)\n'
        )
        before = mod.read_bytes()
        mech_mod.main(["--file", str(mod), "--func", "find", "--tier", "re_call"])
        out = capsys.readouterr().out
        assert "KEPT" in out or "REVERTED" in out
        assert "re_call" in out
        if "REVERTED" in out:
            assert mod.read_bytes() == before

    def test_unmeasurable_baseline_aborts_untouched(self, tmp_path, capsys):
        mod = tmp_path / "broken.py"
        mod.write_text(
            "import definitely_missing_dep_xyz\n"
            "import re\n"
            "\n"
            "def clean(text):\n"
            '    pat = re.compile(r"\\s+")\n'
            '    return pat.sub(" ", text)\n'
        )
        before = mod.read_bytes()
        with pytest.raises(SystemExit) as excinfo:
            mech_mod.main(["--file", str(mod), "--func", "clean"])
        assert excinfo.value.code == 1
        assert mod.read_bytes() == before
        assert "ERROR" in capsys.readouterr().out


NEW_TIER_FIXTURES = {
    "consumer_list": "def f(xs):\n    return sum([x * 2 for x in xs])\n",
    "sorted_minmax": "def f(xs):\n    return sorted(xs)[0]\n",
    "literal_membership": "def f(m):\n    return m in ['a', 'b', 'c']\n",
    "list_cast": "def f():\n    for x in list((1, 2)):\n        print(x)\n",
    "logging_lazy": "import logging\nlog = logging.getLogger('t')\n"
                    "def f(n):\n    log.debug(f'n={n}')\n",
    "dict_keys": "def f(d):\n    return [k for k in d.keys()]\n",
    "async_sleep": "import asyncio\nimport time\n"
                   "async def f():\n    time.sleep(1)\n",
    "enumerate": "def f(xs):\n    for i in range(len(xs)):\n        print(i, xs[i])\n",
    "rematch_search": "import re\ndef f(log):\n"
                      "    if re.match('.*error', log, re.DOTALL):\n"
                      "        return True\n    return False\n",
    "except_hoist": "def f(chunks):\n    for c in chunks:\n"
                    "        try:\n            print(int(c))\n"
                    "        except ValueError:\n            raise\n",
}


class TestNewTierDispatch:
    def test_dry_run_shows_plan_per_tier(self, tmp_path, capsys):
        for tier, source in NEW_TIER_FIXTURES.items():
            mod = tmp_path / f"{tier}.py"
            mod.write_text(source)
            before = mod.read_bytes()
            mech_mod.main(["--file", str(mod), "--func", "f",
                           "--tier", tier, "--dry-run"])
            out = capsys.readouterr().out
            assert "would apply" in out, tier
            assert f"[{tier}]" in out, tier
            assert mod.read_bytes() == before

    def test_live_consumer_loop_keeps_invariants(self, tmp_path, capsys):
        mod = tmp_path / "consumer.py"
        mod.write_text(NEW_TIER_FIXTURES["consumer_list"])
        before = mod.read_bytes()
        mech_mod.main(["--file", str(mod), "--func", "f",
                       "--tier", "consumer_list"])
        out = capsys.readouterr().out
        assert "KEPT" in out or "REVERTED" in out
        if "KEPT" in out:
            assert b"sum((x * 2 for x in xs))" in mod.read_bytes()
        else:
            assert mod.read_bytes() == before
