"""W4 resolver hardening: stale-plan fail-closed, idempotency, collateral, revert.

Loaded via importlib by file path like the other bundled scripts -- the
parent directory, ``perf-gate``, contains a hyphen so it can't be an
importable package.
"""

from __future__ import annotations

import ast
import importlib.resources
import importlib.util
import sys
import textwrap

import pytest


def _load_module(name: str, rel_path: str):
    pkg_skills = importlib.resources.files("perf_gate") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


hoist_analysis = _load_module(
    "_test_hard_hoist_analysis", "perf-gate/scripts/detectors/regex_hoist_analysis.py"
)
hoist_apply = _load_module(
    "_test_hard_hoist_apply", "perf-gate/scripts/resolvers/apply_regex_hoist.py"
)
perf_analysis = _load_module(
    "_test_hard_perf_analysis",
    "perf-gate/scripts/detectors/perf_comprehension_analysis.py",
)
perf_apply = _load_module(
    "_test_hard_perf_apply",
    "perf-gate/scripts/resolvers/apply_perf_comprehension.py",
)

HOIST_CLEAN = textwrap.dedent(
    """\
    import re

    def f(line):
        pattern = re.compile(r'^\\d+$')
        return bool(pattern.match(line))
    """
)
# Same file, but the pattern now depends on a parameter: no longer hoistable.
HOIST_MUTATED = textwrap.dedent(
    """\
    import re

    def f(line, prefix):
        pattern = re.compile(prefix + r'^\\d+$')
        return bool(pattern.match(line))
    """
)
PERF_CLEAN = textwrap.dedent(
    """\
    def f(items):
        out = []
        for x in items:
            out.append(x * 2)
        return out
    """
)
# Same shape, but the body gained a second statement: no longer rewritable.
PERF_MUTATED = textwrap.dedent(
    """\
    def f(items):
        out = []
        for x in items:
            out.append(x * 2)
            print(x)
        return out
    """
)


class TestHoistStalePlanFailsClosed:
    def test_verify_plan_accepts_fresh_plan(self):
        plan = hoist_analysis.analyze_source(HOIST_CLEAN)
        assert hoist_apply.verify_plan(HOIST_CLEAN, plan) == []

    def test_stale_plan_raises_and_edits_nothing(self):
        stale = hoist_analysis.analyze_source(HOIST_CLEAN)
        with pytest.raises(hoist_apply.PlanMismatchError, match="Stale plan"):
            hoist_apply.apply_hoist(HOIST_MUTATED, stale)

    def test_shifted_lines_also_raise(self):
        stale = hoist_analysis.analyze_source(HOIST_CLEAN)
        shifted = "\n\n" + HOIST_CLEAN  # same code, moved lines
        with pytest.raises(hoist_apply.PlanMismatchError):
            hoist_apply.apply_hoist(shifted, stale)

    def test_main_wiring_abort_is_clean_and_safe(self, tmp_path, monkeypatch, capsys):
        target = tmp_path / "mod.py"
        target.write_text(HOIST_CLEAN)  # safe pattern: main reaches apply...

        def _raise(source, plan):
            raise hoist_apply.PlanMismatchError("simulated stale plan")

        monkeypatch.setattr(hoist_apply, "apply_hoist", _raise)  # ...which refuses
        with pytest.raises(SystemExit) as exc_info:
            hoist_apply.main(["--file", str(target)])
        assert exc_info.value.code == 1
        assert target.read_text() == HOIST_CLEAN  # untouched
        assert "Error" in capsys.readouterr().err


class TestPerfStalePlanFailsClosed:
    def test_verify_plan_accepts_fresh_plan(self):
        plan = perf_analysis.analyze_source(PERF_CLEAN)
        assert perf_apply.verify_plan(PERF_CLEAN, plan) == []

    def test_stale_plan_raises_and_edits_nothing(self):
        stale = perf_analysis.analyze_source(PERF_CLEAN)
        with pytest.raises(perf_apply.PlanMismatchError, match="Stale plan"):
            perf_apply.apply_rewrites(PERF_MUTATED, stale)

    def test_main_wiring_abort_is_clean_and_safe(self, tmp_path, monkeypatch, capsys):
        target = tmp_path / "mod.py"
        target.write_text(PERF_CLEAN)  # safe pattern: main reaches apply...

        def _raise(source, plan):
            raise perf_apply.PlanMismatchError("simulated stale plan")

        monkeypatch.setattr(perf_apply, "apply_rewrites", _raise)  # ...which refuses
        with pytest.raises(SystemExit) as exc_info:
            perf_apply.main(["--file", str(target)])
        assert exc_info.value.code == 1
        assert target.read_text() == PERF_CLEAN  # untouched
        assert "Error" in capsys.readouterr().err


class TestIdempotentText:
    def test_hoist_apply_twice_is_stable(self):
        once = hoist_apply.apply_hoist(
            HOIST_CLEAN, hoist_analysis.analyze_source(HOIST_CLEAN)
        )
        plan2 = hoist_analysis.analyze_source(once)
        assert plan2.safe == []
        assert hoist_apply.apply_hoist(once, plan2) == once

    def test_rewrite_apply_twice_is_stable(self):
        once = perf_apply.apply_rewrites(
            PERF_CLEAN, perf_analysis.analyze_source(PERF_CLEAN)
        )
        plan2 = perf_analysis.analyze_source(once)
        assert plan2.safe == []
        assert perf_apply.apply_rewrites(once, plan2) == once


class TestCollateralDiff:
    def test_hoist_preserves_comments_and_formatting(self):
        source = textwrap.dedent(
            """\
            # header comment
            \"\"\"Module docstring.\"\"\"

            import re   # trailing comment


            def untouched(items):  # keep me
                out = [x    for   x in items]  # odd spacing kept
                return out


            def f(line):
                pattern = re.compile(r"x")
                return pattern.match(line)
            """
        )
        result = hoist_apply.apply_hoist(source, hoist_analysis.analyze_source(source))
        ast.parse(result)
        assert result.splitlines()[0] == "# header comment"
        assert "    out = [x    for   x in items]  # odd spacing kept" in result
        assert "def untouched(items):  # keep me" in result
        assert "import re   # trailing comment" in result
        assert 'pattern = re.compile(r"x")\n' in result  # hoisted block present
        assert "return pattern.match(line)" in result

    def test_rewrite_preserves_surrounding_code(self):
        source = textwrap.dedent(
            """\
            # leading comment


            def untouched(a, b):   # keep me
                total = a  +  b  # odd spacing
                return total


            def f(items):
                out = []
                for x in items:
                    out.append(x * 2)
                return out
            """
        )
        result = perf_apply.apply_rewrites(source, perf_analysis.analyze_source(source))
        ast.parse(result)
        assert result.splitlines()[0] == "# leading comment"
        assert "    total = a  +  b  # odd spacing" in result
        assert "out = [x * 2 for x in items]" in result


class TestSkippedNeverTouched:
    def test_hoist_main_leaves_skipped_only_file_alone(self, tmp_path, capsys):
        target = tmp_path / "mod.py"
        target.write_text(HOIST_MUTATED)  # param-dependent: skipped, never safe
        hoist_apply.main(["--file", str(target)])
        assert target.read_text() == HOIST_MUTATED
        assert "No safe" in capsys.readouterr().out

    def test_rewrite_main_leaves_skipped_only_file_alone(self, tmp_path, capsys):
        target = tmp_path / "mod.py"
        target.write_text(PERF_MUTATED)  # multi-statement body: skipped
        perf_apply.main(["--file", str(target)])
        assert target.read_text() == PERF_MUTATED
        assert "No safe" in capsys.readouterr().out


class TestRevertRoundTrip:
    def test_hoist_revert_restores_original_bytes(self, tmp_path):
        target = tmp_path / "mod.py"
        target.write_text(HOIST_CLEAN)
        hoist_apply.main(["--file", str(target)])
        assert target.read_text() != HOIST_CLEAN
        ast.parse(target.read_text())
        # Documented recovery (SKILL.md: `git checkout -- <path>` when the
        # file had no other pending changes) restores the exact bytes:
        target.write_text(HOIST_CLEAN)
        assert target.read_text() == HOIST_CLEAN

    def test_rewrite_revert_restores_original_bytes(self, tmp_path):
        target = tmp_path / "mod.py"
        target.write_text(PERF_CLEAN)
        perf_apply.main(["--file", str(target)])
        assert target.read_text() != PERF_CLEAN
        ast.parse(target.read_text())
        target.write_text(PERF_CLEAN)
        assert target.read_text() == PERF_CLEAN
