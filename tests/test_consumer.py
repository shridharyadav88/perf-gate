"""Tests for consumer_analysis.py (detection) and apply_consumer.py.

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
    "_test_consumer_analysis",
    "code-optimizer/scripts/detectors/consumer_analysis.py",
)
apply_mod = _load_module(
    "_test_apply_consumer",
    "code-optimizer/scripts/resolvers/apply_consumer.py",
)

analyze_source = analysis.analyze_source
apply_consumers = apply_mod.apply_consumers


def _exec_and_get(source: str, func_name: str):
    ns: dict = {}
    exec(compile(source, "<test>", "exec"), ns)
    return ns[func_name]


class TestDetection:
    def test_sum_listcomp(self):
        plan = analyze_source(textwrap.dedent("""\
            def total(xs):
                return sum([x * 2 for x in xs])
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("sum", "total")]
        assert plan.safe[0].edits[0][4] == "(x * 2 for x in xs)"

    def test_any_pure_listcomp(self):
        plan = analyze_source(textwrap.dedent("""\
            def has_pos(xs):
                return any([x > 0 for x in xs])
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("any", "has_pos")]

    def test_len_listcomp_becomes_sum(self):
        plan = analyze_source(textwrap.dedent("""\
            def count(xs):
                return len([x for x in xs if x])
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("len", "count")]
        assert plan.safe[0].edits[0][4] == "sum(1 for x in xs if x)"

    def test_join_listcomp(self):
        plan = analyze_source(textwrap.dedent("""\
            def words(parts):
                return ",".join([str(p) for p in parts])
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("join", "words")]

    def test_list_call_stripped(self):
        plan = analyze_source(textwrap.dedent("""\
            def total(items):
                return sum(list(items))
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("sum", "total")]
        assert plan.safe[0].edits[0][4] == "items"

    def test_nested_folds_into_outer(self):
        plan = analyze_source(textwrap.dedent("""\
            def total(items):
                return sum([x for x in sorted([y for y in items])])
            """))
        assert len(plan.safe) == 1
        assert plan.safe[0].kind == "sum"
        assert plan.safe[0].edits[0][4] == "(x for x in sorted((y for y in items)))"


class TestNegatives:
    def test_any_impure_element_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def check(xs):
                return any([g(x) for x in xs])
            """))
        assert plan.safe == []
        assert any("fewer times" in reason for _, reason in plan.skipped)

    def test_any_over_live_iterable_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def check(it):
                return any(list(it))
            """))
        assert plan.safe == []
        assert any("snapshot" in reason for _, reason in plan.skipped)

    def test_consumer_with_keywords_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def smallest(xs):
                return min([x for x in xs], default=0)
            """))
        assert plan.safe == []

    def test_already_generator_no_candidate(self):
        plan = analyze_source(textwrap.dedent("""\
            def total(xs):
                return sum(x * 2 for x in xs)
            """))
        assert plan.safe == [] and plan.skipped == []

    def test_shadowed_builtin_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def total(xs):
                return sum([x for x in xs])

            sum = lambda *a: 0
            """))
        assert plan.safe == []
        assert any("rebound" in reason for _, reason in plan.skipped)

    def test_async_comprehension_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            async def total(aiter):
                return sum([x async for x in aiter])
            """))
        assert plan.safe == []
        assert any("async" in reason for _, reason in plan.skipped)

    def test_comment_in_span_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def total(xs):
                return sum([  # count them
                    x for x in xs])
            """))
        assert plan.safe == []
        assert any("comment" in reason for _, reason in plan.skipped)


class TestApply:
    SOURCE = textwrap.dedent("""\
        def total(xs):
            return sum([x * 2 for x in xs])

        def count(xs):
            return len([x for x in xs if x])
        """)

    def test_apply_rewrites_both(self):
        plan = analyze_source(self.SOURCE)
        out = apply_consumers(self.SOURCE, plan)
        assert "sum((x * 2 for x in xs))" in out
        assert "sum(1 for x in xs if x)" in out
        ast.parse(out)

    def test_results_identical(self):
        plan = analyze_source(self.SOURCE)
        out = apply_consumers(self.SOURCE, plan)
        data = [3, -1, 0, 7]
        assert _exec_and_get(self.SOURCE, "total")(data) == \
            _exec_and_get(out, "total")(data) == 18
        assert _exec_and_get(self.SOURCE, "count")(data) == \
            _exec_and_get(out, "count")(data) == 3

    def test_idempotent(self):
        once = apply_consumers(self.SOURCE, analyze_source(self.SOURCE))
        plan2 = analyze_source(once)
        assert plan2.safe == []
        assert apply_consumers(once, plan2) == once

    def test_collateral_diff_only_intended_lines(self):
        plan = analyze_source(self.SOURCE)
        out = apply_consumers(self.SOURCE, plan)
        diff = list(difflib.unified_diff(
            self.SOURCE.splitlines(), out.splitlines(), lineterm=""))
        changed = [line for line in diff
                   if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
        assert len(changed) == 4  # two removed lines, two added lines
        added = [line for line in changed if line.startswith("+")]
        assert len(added) == 2 and all("sum(" in line for line in added)

    def test_stale_plan_raises(self):
        plan = analyze_source(self.SOURCE)
        mutated = self.SOURCE.replace("x * 2", "x * 3")
        with pytest.raises(apply_mod.PlanMismatchError):
            apply_consumers(mutated, plan)

    def test_empty_plan_returns_source(self):
        assert apply_consumers("x = 1\n", analysis.ConsumerPlan()) == "x = 1\n"


class TestScopeShadowing:
    def test_len_with_param_sum_skipped(self):
        # The len(...) rewrite introduces a sum() call: a shadowing
        # parameter would change which object is called.
        plan = analyze_source(textwrap.dedent("""\
            def f(sum, xs):
                return len([x for x in xs if x])
            """))
        assert plan.safe == []
        assert any("function scope" in reason for _, reason in plan.skipped)

    def test_len_with_closure_sum_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def outer(xs):
                def inner(ys):
                    return len([y for y in ys])
                def sum(it):
                    return 0
                return inner(xs)
            """))
        assert plan.safe == []

    def test_sum_call_with_param_sum_still_fires(self):
        # No name is introduced: only the argument changes, and the
        # callee resolves identically before and after.
        plan = analyze_source(textwrap.dedent("""\
            def f(sum, xs):
                return sum([x * 2 for x in xs])
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("sum", "f")]
