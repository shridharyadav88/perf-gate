"""Tests for list_cast_analysis.py (detection) and apply_list_cast.py.

Loaded via importlib.resources like the other bundled scripts — their parent
directory, ``code-optimizer``, contains a hyphen so it can't be an importable
package.
"""

from __future__ import annotations

import ast
import difflib
import importlib.resources
import importlib.util
import sys
import textwrap

import pytest


def _load_module(name: str, rel_path: str):
    pkg_skills = importlib.resources.files("project_code_optimization") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


analysis = _load_module(
    "_test_list_cast_analysis",
    "code-optimizer/scripts/detectors/list_cast_analysis.py",
)
apply_mod = _load_module(
    "_test_apply_list_cast",
    "code-optimizer/scripts/resolvers/apply_list_cast.py",
)

analyze_source = analysis.analyze_source
apply_list_cast = apply_mod.apply_list_cast


def _exec_and_get(source: str, func_name: str):
    ns: dict = {}
    exec(compile(source, "<test>", "exec"), ns)
    return ns[func_name]


class TestDetection:
    def test_list_of_tuple_dropped(self):
        plan = analyze_source(textwrap.dedent("""\
            def total():
                s = 0
                for x in list((1, 2, 3)):
                    s += x
                return s
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("list_cast", "total")]
        assert plan.safe[0].edits[0][4] == "(1, 2, 3)"

    def test_list_of_comprehension_dropped(self):
        plan = analyze_source(textwrap.dedent("""\
            def total(xs):
                s = 0
                for x in list(x * 2 for x in xs):
                    s += x
                return s
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("list_cast", "total")]
        assert plan.safe[0].edits[0][4] == "(x * 2 for x in xs)"

    def test_double_list_unwraps_once(self):
        plan = analyze_source(textwrap.dedent("""\
            def total(xs):
                for x in list(list([1, 2])):
                    print(x)
            """))
        assert len(plan.safe) == 1
        assert plan.safe[0].edits[0][4] == "[1, 2]"


class TestNegatives:
    def test_bare_name_keeps_snapshot(self):
        plan = analyze_source(textwrap.dedent("""\
            def total(items):
                s = 0
                for x in list(items):
                    s += x
                return s
            """))
        assert plan.safe == []
        assert any("snapshot" in reason for _, reason in plan.skipped)

    def test_shadowed_list_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def total():
                for x in list((1, 2)):
                    print(x)

            list = tuple
            """))
        assert plan.safe == []
        assert any("rebound" in reason for _, reason in plan.skipped)

    def test_async_for_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            async def total(aiter):
                async for x in list([1, 2]):
                    print(x)
            """))
        assert plan.safe == []
        assert any("async" in reason for _, reason in plan.skipped)

    def test_assignment_position_untouched(self):
        plan = analyze_source(textwrap.dedent("""\
            def copy(pair):
                return list(pair)
            """))
        assert plan.safe == [] and plan.skipped == []


class TestApply:
    SOURCE = textwrap.dedent("""\
        def total():
            s = 0
            for x in list((1, 2, 3)):
                s += x
            return s
        """)

    def test_apply_rewrites(self):
        plan = analyze_source(self.SOURCE)
        out = apply_list_cast(self.SOURCE, plan)
        assert "for x in (1, 2, 3):" in out and "list(" not in out
        ast.parse(out)

    def test_results_identical(self):
        plan = analyze_source(self.SOURCE)
        out = apply_list_cast(self.SOURCE, plan)
        assert _exec_and_get(self.SOURCE, "total")() == \
            _exec_and_get(out, "total")() == 6

    def test_idempotent(self):
        once = apply_list_cast(self.SOURCE, analyze_source(self.SOURCE))
        plan2 = analyze_source(once)
        assert plan2.safe == []
        assert apply_list_cast(once, plan2) == once

    def test_collateral_diff_only_intended_lines(self):
        plan = analyze_source(self.SOURCE)
        out = apply_list_cast(self.SOURCE, plan)
        diff = list(difflib.unified_diff(
            self.SOURCE.splitlines(), out.splitlines(), lineterm=""))
        changed = [line for line in diff
                   if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
        assert len(changed) == 2
        assert changed[0].startswith("-") and changed[1].startswith("+")

    def test_stale_plan_raises(self):
        plan = analyze_source(self.SOURCE)
        mutated = self.SOURCE.replace("list((1, 2, 3))", "list((1, 2, 4))")
        with pytest.raises(apply_mod.PlanMismatchError):
            apply_list_cast(mutated, plan)

    def test_empty_plan_returns_source(self):
        assert apply_list_cast("x = 1\n", analysis.ListCastPlan()) == "x = 1\n"


class TestScopeShadowing:
    def test_param_list_skipped(self):
        # Dropping the list(...) call is only safe when list is the builtin.
        plan = analyze_source(textwrap.dedent("""\
            def f(list):
                s = 0
                for x in list((1, 2, 3)):
                    s += x
                return s
            """))
        assert plan.safe == []
        assert any("function scope" in reason for _, reason in plan.skipped)
