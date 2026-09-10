"""Tests for sorted_minmax_analysis.py (detection) and apply_sorted_minmax.py.

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
    "_test_sorted_minmax_analysis",
    "code-optimizer/scripts/detectors/sorted_minmax_analysis.py",
)
apply_mod = _load_module(
    "_test_apply_sorted_minmax",
    "code-optimizer/scripts/resolvers/apply_sorted_minmax.py",
)

analyze_source = analysis.analyze_source
apply_sorted_minmax = apply_mod.apply_sorted_minmax


def _exec_and_get(source: str, func_name: str):
    ns: dict = {}
    exec(compile(source, "<test>", "exec"), ns)
    return ns[func_name]


class TestDetection:
    def test_sorted_first_becomes_min(self):
        plan = analyze_source(textwrap.dedent("""\
            def first(xs):
                return sorted(xs)[0]
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("sorted_minmax", "first")]
        assert plan.safe[0].edits[0][4] == "min(xs)"

    def test_sorted_last_becomes_max(self):
        plan = analyze_source(textwrap.dedent("""\
            def last(xs):
                return sorted(xs)[-1]
            """))
        assert plan.safe[0].edits[0][4] == "max(xs)"

    def test_reversed_first_becomes_max(self):
        plan = analyze_source(textwrap.dedent("""\
            def biggest(xs):
                return sorted(xs, reverse=True)[0]
            """))
        assert plan.safe[0].edits[0][4] == "max(xs)"

    def test_reversed_last_becomes_min(self):
        plan = analyze_source(textwrap.dedent("""\
            def smallest(xs):
                return sorted(xs, reverse=True)[-1]
            """))
        assert plan.safe[0].edits[0][4] == "min(xs)"


class TestNegatives:
    def test_middle_index_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def median(xs):
                return sorted(xs)[1]
            """))
        assert plan.safe == []
        assert any("not [0] or [-1]" in reason for _, reason in plan.skipped)

    def test_key_function_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def longest(words):
                return sorted(words, key=len)[-1]
            """))
        assert plan.safe == []
        assert any("key=" in reason for _, reason in plan.skipped)

    def test_slice_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def top3(xs):
                return sorted(xs)[:3]
            """))
        assert plan.safe == []

    def test_shadowed_min_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def first(xs):
                return sorted(xs)[0]

            min = lambda *a: None
            """))
        assert plan.safe == []
        assert any("rebound" in reason for _, reason in plan.skipped)


class TestApply:
    SOURCE = textwrap.dedent("""\
        def first(xs):
            return sorted(xs)[0]

        def last(xs):
            return sorted(xs)[-1]
        """)

    def test_apply_rewrites(self):
        plan = analyze_source(self.SOURCE)
        out = apply_sorted_minmax(self.SOURCE, plan)
        assert "return min(xs)" in out and "return max(xs)" in out
        ast.parse(out)

    def test_results_identical(self):
        plan = analyze_source(self.SOURCE)
        out = apply_sorted_minmax(self.SOURCE, plan)
        data = [5, 1, 9, 3]
        assert _exec_and_get(self.SOURCE, "first")(list(data)) == \
            _exec_and_get(out, "first")(list(data)) == 1
        assert _exec_and_get(self.SOURCE, "last")(list(data)) == \
            _exec_and_get(out, "last")(list(data)) == 9

    def test_idempotent(self):
        once = apply_sorted_minmax(self.SOURCE, analyze_source(self.SOURCE))
        plan2 = analyze_source(once)
        assert plan2.safe == []
        assert apply_sorted_minmax(once, plan2) == once

    def test_collateral_diff_only_intended_lines(self):
        plan = analyze_source(self.SOURCE)
        out = apply_sorted_minmax(self.SOURCE, plan)
        diff = list(difflib.unified_diff(
            self.SOURCE.splitlines(), out.splitlines(), lineterm=""))
        changed = [line for line in diff
                   if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
        assert len(changed) == 4
        assert changed[0].startswith("-") and changed[1].startswith("+")

    def test_stale_plan_raises(self):
        plan = analyze_source(self.SOURCE)
        mutated = self.SOURCE.replace("sorted(xs)[0]", "sorted(xs)[1]")
        with pytest.raises(apply_mod.PlanMismatchError):
            apply_sorted_minmax(mutated, plan)

    def test_empty_plan_returns_source(self):
        assert apply_sorted_minmax("x = 1\n", analysis.SortedMinmaxPlan()) == "x = 1\n"


class TestScopeShadowing:
    def test_param_min_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def f(min, xs):
                return sorted(xs)[0]
            """))
        assert plan.safe == []
        assert any("function scope" in reason for _, reason in plan.skipped)

    def test_closure_min_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def outer(xs):
                def inner(ys):
                    return sorted(ys)[0]
                min = max
                return inner(xs)
            """))
        assert plan.safe == []
