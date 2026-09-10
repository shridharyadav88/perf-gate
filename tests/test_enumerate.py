"""Tests for enumerate_analysis.py (detection) and apply_enumerate.py.

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
    "_test_enumerate_analysis",
    "code-optimizer/scripts/detectors/enumerate_analysis.py",
)
apply_mod = _load_module(
    "_test_apply_enumerate",
    "code-optimizer/scripts/resolvers/apply_enumerate.py",
)

analyze_source = analysis.analyze_source
apply_enumerates = apply_mod.apply_enumerates


def _exec_and_get(source: str, func_name: str):
    ns: dict = {}
    exec(compile(source, "<test>", "exec"), ns)
    return ns[func_name]


class TestDetection:
    def test_basic_loop_fires(self):
        plan = analyze_source(textwrap.dedent("""\
            def total(xs):
                s = 0
                for i in range(len(xs)):
                    s += xs[i]
                return s
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("enumerate", "total")]
        cand = plan.safe[0]
        texts = [e[4] for e in cand.edits]
        assert "i, xs_item" in texts[0]
        assert texts[1] == "enumerate(xs)"
        assert texts[2] == "xs_item"

    def test_explicit_zero_start_fires(self):
        plan = analyze_source(textwrap.dedent("""\
            def total(xs):
                s = 0
                for i in range(0, len(xs)):
                    s += xs[i]
                return s
            """))
        assert len(plan.safe) == 1

    def test_comprehension_fires(self):
        plan = analyze_source(textwrap.dedent("""\
            def doubled(xs):
                return [xs[i] * 2 for i in range(len(xs))]
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("enumerate", "doubled")]

    def test_value_name_avoids_collision(self):
        plan = analyze_source(textwrap.dedent("""\
            def total(xs):
                xs_item = "taken"
                s = 0
                for i in range(len(xs)):
                    s += xs[i]
                return s
            """))
        assert len(plan.safe) == 1
        assert plan.safe[0].edits[0][4] == "i, xs_value"


class TestNegatives:
    def test_append_in_body_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def grow(xs):
                for i in range(len(xs)):
                    xs.append(i)
            """))
        assert plan.safe == []
        assert any("length" in reason for _, reason in plan.skipped)

    def test_index_rebound_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def total(xs):
                s = 0
                for i in range(len(xs)):
                    i = 5
                    s += i
                return s
            """))
        assert plan.safe == []
        assert any("rebound" in reason for _, reason in plan.skipped)

    def test_unused_index_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def show(xs):
                for i in range(len(xs)):
                    print("hi")
            """))
        assert plan.safe == []
        assert any("never read" in reason for _, reason in plan.skipped)

    def test_step_range_skipped_silently(self):
        plan = analyze_source(textwrap.dedent("""\
            def show(xs):
                for i in range(0, len(xs), 2):
                    print(xs[i])
            """))
        assert plan.safe == []

    def test_bare_seq_arg_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def show(xs):
                for i in range(len(xs)):
                    print(i, xs)
            """))
        assert plan.safe == []

    def test_rebound_len_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def total(xs):
                s = 0
                for i in range(len(xs)):
                    s += xs[i]
                return s

            len = lambda x: 0
            """))
        assert plan.safe == []
        assert any("rebound" in reason for _, reason in plan.skipped)


class TestApply:
    SOURCE = textwrap.dedent("""\
        def total(xs):
            s = 0
            for i in range(len(xs)):
                s += xs[i]
            return s
        """)

    def test_apply_rewrites(self):
        plan = analyze_source(self.SOURCE)
        out = apply_enumerates(self.SOURCE, plan)
        assert "for i, xs_item in enumerate(xs):" in out
        assert "s += xs_item" in out
        ast.parse(out)

    def test_results_identical(self):
        plan = analyze_source(self.SOURCE)
        out = apply_enumerates(self.SOURCE, plan)
        assert _exec_and_get(self.SOURCE, "total")([1, 2, 3]) == \
            _exec_and_get(out, "total")([1, 2, 3]) == 6

    def test_idempotent(self):
        once = apply_enumerates(self.SOURCE, analyze_source(self.SOURCE))
        plan2 = analyze_source(once)
        assert plan2.safe == []
        assert apply_enumerates(once, plan2) == once

    def test_collateral_diff_only_intended_lines(self):
        plan = analyze_source(self.SOURCE)
        out = apply_enumerates(self.SOURCE, plan)
        diff = list(difflib.unified_diff(
            self.SOURCE.splitlines(), out.splitlines(), lineterm=""))
        changed = [line for line in diff
                   if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
        removed = sorted(line[1:] for line in changed if line.startswith("-"))
        added = sorted(line[1:] for line in changed if line.startswith("+"))
        assert removed == ["        s += xs[i]", "    for i in range(len(xs)):"]
        assert added == ["        s += xs_item", "    for i, xs_item in enumerate(xs):"]

    def test_stale_plan_raises(self):
        plan = analyze_source(self.SOURCE)
        mutated = self.SOURCE.replace("xs[i]", "xs[i + 1]")
        with pytest.raises(apply_mod.PlanMismatchError):
            apply_enumerates(mutated, plan)

    def test_empty_plan_returns_source(self):
        assert apply_enumerates("x = 1\n", analysis.EnumeratePlan()) == "x = 1\n"

    def test_index_only_loop_skipped(self):
        # crawl4ai field case: the index is read but the sequence is
        # never subscripted, so enumerate would only add an unused name.
        plan = analyze_source(textwrap.dedent("""\
            def mark(args, param_names):
                explicit = set()
                for i in range(len(args)):
                    if i < len(param_names):
                        explicit.add(param_names[i])
                return explicit
            """))
        assert plan.safe == []
        assert any("never subscripts" in reason for _, reason in plan.skipped)

    def test_nested_loop_single_attribution(self):
        plan = analyze_source(textwrap.dedent("""\
            def outer(xs):
                def inner(ys):
                    s = 0
                    for i in range(len(ys)):
                        s += ys[i]
                    return s
                return inner(xs)
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("enumerate", "inner")]

    def test_nested_apply_reaches_fixpoint(self):
        source = textwrap.dedent("""\
            def outer(xs):
                def inner(ys):
                    s = 0
                    for i in range(len(ys)):
                        s += ys[i]
                    return s
                return inner(xs)
            """)
        plan = analyze_source(source)
        assert len(plan.safe) == 1
        out = apply_enumerates(source, plan)
        ast.parse(out)
        assert "for i, ys_item in enumerate(ys):" in out
        assert analyze_source(out).safe == []


class TestScopeShadowing:
    def test_nested_len_rebind_skipped(self):
        # The inner rebind is invisible to the outer scope's own check,
        # but the span-scope chain sees it: a single outer candidate must
        # not survive either.
        plan = analyze_source(textwrap.dedent("""\
            def outer(xs):
                def inner(ys):
                    len = lambda x: 0
                    s = 0
                    for i in range(len(ys)):
                        s += ys[i]
                    return s
                return inner(xs)
            """))
        assert plan.safe == []
        assert any("function scope" in reason for _, reason in plan.skipped)

    def test_closure_len_param_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def outer(xs, len):
                def inner(ys):
                    s = 0
                    for i in range(len(ys)):
                        s += ys[i]
                    return s
                return inner(xs)
            """))
        assert plan.safe == []
