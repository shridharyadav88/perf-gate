"""Tests for try_hoist_analysis.py (detection) and apply_try_hoist.py.

Loaded via importlib.resources like the other bundled scripts — their parent
directory, ``perf-gate``, contains a hyphen so it can't be an importable
package.
"""

from __future__ import annotations

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


analysis = _load_module(
    "_test_try_hoist_analysis",
    "perf-gate/scripts/detectors/try_hoist_analysis.py",
)
apply_mod = _load_module(
    "_test_apply_try_hoist",
    "perf-gate/scripts/resolvers/apply_try_hoist.py",
)

analyze_source = analysis.analyze_source
apply_try_hoists = apply_mod.apply_try_hoists


def _exec_and_get(source: str, func_name: str):
    ns: dict = {}
    exec(compile(source, "<test>", "exec"), ns)
    return ns[func_name]


class TestDetection:
    def test_raise_handler_fires(self):
        plan = analyze_source(textwrap.dedent("""\
            def parse_all(chunks):
                out = []
                for chunk in chunks:
                    try:
                        out.append(int(chunk))
                    except ValueError:
                        raise
                return out
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("except_hoist", "parse_all")]
        assert len(plan.safe[0].edits) == 1

    def test_return_handler_fires(self):
        plan = analyze_source(textwrap.dedent("""\
            def find_first(chunks):
                for chunk in chunks:
                    try:
                        return int(chunk)
                    except ValueError:
                        return -1
                return -2
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("except_hoist", "find_first")]


class TestNegatives:
    def test_fallthrough_handler_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def parse_all(chunks):
                out = []
                for chunk in chunks:
                    try:
                        out.append(int(chunk))
                    except ValueError:
                        out.append(0)
                return out
            """))
        assert plan.safe == []
        assert any("falls through" in reason for _, reason in plan.skipped)

    def test_continue_handler_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def parse_all(chunks):
                out = []
                for chunk in chunks:
                    try:
                        out.append(int(chunk))
                    except ValueError:
                        continue
                return out
            """))
        assert plan.safe == []
        assert any("breaks/continues" in reason or "falls through" in reason
                   for _, reason in plan.skipped)

    def test_finally_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def parse_all(chunks):
                out = []
                for chunk in chunks:
                    try:
                        out.append(int(chunk))
                    except ValueError:
                        raise
                    finally:
                        pass
                return out
            """))
        assert plan.safe == []
        assert any("finally" in reason for _, reason in plan.skipped)

    def test_for_else_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def parse_all(chunks):
                out = []
                for chunk in chunks:
                    try:
                        out.append(int(chunk))
                    except ValueError:
                        raise
                else:
                    out.append(-1)
                return out
            """))
        assert plan.safe == []
        assert any("for-else" in reason for _, reason in plan.skipped)

    def test_non_try_body_untouched(self):
        plan = analyze_source(textwrap.dedent("""\
            def total(xs):
                s = 0
                for x in xs:
                    s += x
                return s
            """))
        assert plan.safe == [] and plan.skipped == []


class TestApply:
    SOURCE = textwrap.dedent("""\
        def parse_all(chunks):
            out = []
            for chunk in chunks:
                try:
                    out.append(int(chunk))
                except ValueError:
                    raise
            return out
        """)

    EXPECTED = textwrap.dedent("""\
        def parse_all(chunks):
            out = []
            try:
                for chunk in chunks:
                    out.append(int(chunk))
            except ValueError:
                raise
            return out
        """)

    def test_apply_rewrites_exactly(self):
        plan = analyze_source(self.SOURCE)
        out = apply_try_hoists(self.SOURCE, plan)
        assert out == self.EXPECTED

    def test_results_identical(self):
        plan = analyze_source(self.SOURCE)
        out = apply_try_hoists(self.SOURCE, plan)
        assert _exec_and_get(self.SOURCE, "parse_all")(["1", "2"]) == \
            _exec_and_get(out, "parse_all")(["1", "2"]) == [1, 2]
        with pytest.raises(ValueError):
            _exec_and_get(self.SOURCE, "parse_all")(["1", "x"])
        with pytest.raises(ValueError):
            _exec_and_get(out, "parse_all")(["1", "x"])

    def test_idempotent(self):
        once = apply_try_hoists(self.SOURCE, analyze_source(self.SOURCE))
        plan2 = analyze_source(once)
        assert plan2.safe == []
        assert apply_try_hoists(once, plan2) == once

    def test_stale_plan_raises(self):
        plan = analyze_source(self.SOURCE)
        mutated = self.SOURCE.replace("raise", "return []")
        with pytest.raises(apply_mod.PlanMismatchError):
            apply_try_hoists(mutated, plan)

    def test_empty_plan_returns_source(self):
        assert apply_try_hoists("x = 1\n", analysis.TryHoistPlan()) == "x = 1\n"
