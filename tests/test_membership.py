"""Tests for membership_analysis.py (detection) and apply_membership.py.

Loaded via importlib.resources like the other bundled scripts — their parent
directory, ``perf-gate``, contains a hyphen so it can't be an importable
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
    pkg_skills = importlib.resources.files("perf_gate") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


analysis = _load_module(
    "_test_membership_analysis",
    "perf-gate/scripts/detectors/membership_analysis.py",
)
apply_mod = _load_module(
    "_test_apply_membership",
    "perf-gate/scripts/resolvers/apply_membership.py",
)

analyze_source = analysis.analyze_source
apply_memberships = apply_mod.apply_memberships


def _exec_and_get(source: str, func_name: str):
    ns: dict = {}
    exec(compile(source, "<test>", "exec"), ns)
    return ns[func_name]


class TestDetection:
    def test_list_literal_becomes_set(self):
        plan = analyze_source(textwrap.dedent("""\
            def check(mode):
                return mode in ["fast", "slow", "auto"]
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("membership", "check")]
        replacement = plan.safe[0].edits[0][4]
        assert ast.literal_eval(replacement) == {"fast", "slow", "auto"}

    def test_tuple_literal_becomes_set(self):
        plan = analyze_source(textwrap.dedent("""\
            def check(code):
                if code not in (200, 201, 204):
                    raise ValueError(code)
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("membership", "check")]
        assert plan.safe[0].edits[0][4] == "{200, 201, 204}"

    def test_mixed_constants_fire(self):
        plan = analyze_source(textwrap.dedent("""\
            def check(v):
                return v in [1, "two", None, True]
            """))
        assert len(plan.safe) == 1


class TestNegatives:
    def test_two_elements_skipped_silently(self):
        plan = analyze_source(textwrap.dedent("""\
            def check(mode):
                return mode in ["fast", "slow"]
            """))
        assert plan.safe == [] and plan.skipped == []

    def test_non_constant_elements_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def check(x, extra):
                return x in [1, 2, extra]
            """))
        assert plan.safe == []
        assert any("non-constant" in reason for _, reason in plan.skipped)

    def test_call_elements_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def check(x):
                return x in [1, 2, int("3")]
            """))
        assert plan.safe == []
        assert any("non-constant" in reason for _, reason in plan.skipped)

    def test_chained_comparison_untouched(self):
        plan = analyze_source(textwrap.dedent("""\
            def check(x, lo, hi):
                return lo in [1, 2, 3] in hi
            """))
        assert plan.safe == []

    def test_comment_in_span_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def check(x):
                return x in [1,  # one
                             2, 3]
            """))
        assert plan.safe == []
        assert any("comment" in reason for _, reason in plan.skipped)


class TestApply:
    SOURCE = textwrap.dedent("""\
        def check(mode):
            if mode in ["fast", "slow", "auto"]:
                return 1
            return 0
        """)

    def test_apply_rewrites(self):
        plan = analyze_source(self.SOURCE)
        out = apply_memberships(self.SOURCE, plan)
        assert "mode in {" in out and "mode in [" not in out
        ast.parse(out)

    def test_results_identical(self):
        plan = analyze_source(self.SOURCE)
        out = apply_memberships(self.SOURCE, plan)
        for mode in ("fast", "slow", "auto", "other"):
            assert _exec_and_get(self.SOURCE, "check")(mode) == \
                _exec_and_get(out, "check")(mode)

    def test_idempotent(self):
        once = apply_memberships(self.SOURCE, analyze_source(self.SOURCE))
        plan2 = analyze_source(once)
        assert plan2.safe == []
        assert apply_memberships(once, plan2) == once

    def test_collateral_diff_only_intended_lines(self):
        plan = analyze_source(self.SOURCE)
        out = apply_memberships(self.SOURCE, plan)
        diff = list(difflib.unified_diff(
            self.SOURCE.splitlines(), out.splitlines(), lineterm=""))
        changed = [line for line in diff
                   if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
        assert len(changed) == 2
        assert changed[0].startswith("-") and changed[1].startswith("+")

    def test_stale_plan_raises(self):
        plan = analyze_source(self.SOURCE)
        mutated = self.SOURCE.replace('"slow"', '"leisurely"')
        with pytest.raises(apply_mod.PlanMismatchError):
            apply_memberships(mutated, plan)

    def test_empty_plan_returns_source(self):
        assert apply_memberships("x = 1\n", analysis.MembershipPlan()) == "x = 1\n"
