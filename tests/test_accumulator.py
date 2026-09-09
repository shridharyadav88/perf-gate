"""Tests for accumulator_analysis.py (detection) and apply_accumulator.py.

Loaded via importlib.resources like the other bundled scripts — their parent
directory, ``code-optimizer``, contains a hyphen so it can't be an importable
package.
"""

from __future__ import annotations

import ast
import importlib.resources
import importlib.util
import sys
import textwrap


def _load_module(name: str, rel_path: str):
    pkg_skills = importlib.resources.files("project_code_optimization") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


analysis = _load_module(
    "_test_accumulator_analysis",
    "code-optimizer/scripts/detectors/accumulator_analysis.py",
)
apply_mod = _load_module(
    "_test_apply_accumulator",
    "code-optimizer/scripts/resolvers/apply_accumulator.py",
)

analyze_source = analysis.analyze_source
apply_accumulates = apply_mod.apply_accumulates


def _exec_and_get(source: str, func_name: str):
    ns: dict = {}
    exec(compile(source, "<test>", "exec"), ns)
    return ns[func_name]


class TestDetection:
    def test_simple_join_loop_is_str_join(self):
        plan = analyze_source(textwrap.dedent("""\
            def join(xs):
                s = ""
                for x in xs:
                    s += str(x)
                return s
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("str_join", "join")]

    def test_guarded_sum_loop_is_sum_reduce(self):
        plan = analyze_source(textwrap.dedent("""\
            def total(xs):
                n = 0
                for x in xs:
                    if x:
                        n += x
                return n
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("sum_reduce", "total")]

    def test_simple_add_loop_is_set_build(self):
        plan = analyze_source(textwrap.dedent("""\
            def uniq(xs):
                seen = set()
                for x in xs:
                    seen.add(x)
                return seen
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("set_build", "uniq")]
        assert "set(" in plan.safe[0].replacement

    def test_guarded_add_loop_is_set_build(self):
        plan = analyze_source(textwrap.dedent("""\
            def uniq(xs):
                seen = set()
                for x in xs:
                    if x:
                        seen.add(str(x))
                return seen
            """))
        (cand,) = plan.safe
        assert cand.kind == "set_build"
        assert "if x" in cand.replacement

    def test_shadowed_set_name_is_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def set(xs):
                return list(xs)

            def uniq(xs):
                seen = set()
                for x in xs:
                    seen.add(x)
                return seen
            """))
        assert plan.safe == []
        assert any("rebound" in reason for _, reason in plan.skipped)

    def test_add_with_kwargs_is_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def uniq(xs):
                seen = set()
                for x in xs:
                    seen.add(x, y=1)
                return seen
            """))
        assert plan.safe == []

    def test_non_set_init_add_is_ignored(self):
        plan = analyze_source(textwrap.dedent("""\
            def uniq(xs):
                seen = []
                for x in xs:
                    seen.add(x)
                return seen
            """))
        assert plan.safe == []

    def test_float_zero_init_is_skipped(self):
        # sum([]) == 0 (int) would change the empty-iterable result's type.
        plan = analyze_source(textwrap.dedent("""\
            def total(xs):
                n = 0.0
                for x in xs:
                    n += x
                return n
            """))
        assert plan.safe == []

    def test_nonzero_init_is_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def total(xs):
                n = 10
                for x in xs:
                    n += x
                return n
            """))
        assert plan.safe == []

    def test_sub_assign_is_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def total(xs):
                n = 0
                for x in xs:
                    n -= x
                return n
            """))
        assert plan.safe == []

    def test_self_referencing_body_is_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def join(xs):
                s = ""
                for x in xs:
                    s += s + x
                return s
            """))
        assert plan.safe == []
        assert plan.skipped

    def test_loop_variable_read_after_loop_is_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def join(xs):
                s = ""
                for x in xs:
                    s += x
                return s + x
            """))
        assert plan.safe == []
        assert plan.skipped

    def test_async_loop_is_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            async def join(xs):
                s = ""
                async for x in xs:
                    s += x
                return s
            """))
        assert plan.safe == []

    def test_for_else_is_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def join(xs):
                s = ""
                for x in xs:
                    s += x
                else:
                    s += "!"
                return s
            """))
        assert plan.safe == []

    def test_non_adjacent_init_is_ignored(self):
        plan = analyze_source(textwrap.dedent("""\
            def join(xs):
                s = ""
                print("between")
                for x in xs:
                    s += x
                return s
            """))
        assert plan.safe == []


class TestApplyCorrectness:
    def test_rewritten_join_behaves_identically(self):
        source = textwrap.dedent("""\
            def join(xs):
                s = ""
                for x in xs:
                    if x:
                        s += str(x)
                return s
            """)
        plan = analyze_source(source)
        assert len(plan.safe) == 1
        new_source = apply_accumulates(source, plan)
        old, new = _exec_and_get(source, "join"), _exec_and_get(new_source, "join")
        for data in ([], ["a"], ["a", "", "bb"], [1, 2, 3]):
            assert new(data) == old(data)

    def test_rewritten_sum_behaves_identically(self):
        source = textwrap.dedent("""\
            def total(xs):
                n = 0
                for x in xs:
                    n += 2 * x
                return n
            """)
        plan = analyze_source(source)
        assert len(plan.safe) == 1
        new_source = apply_accumulates(source, plan)
        assert "sum(" in new_source
        old, new = _exec_and_get(source, "total"), _exec_and_get(new_source, "total")
        for data in ([], [1], [1, 2, 3], [-5, 5]):
            assert new(data) == old(data)
            assert type(new(data)) is type(old(data))

    def test_rewritten_set_behaves_identically(self):
        source = textwrap.dedent("""\
            def uniq(xs):
                seen = set()
                for x in xs:
                    if x is not None:
                        seen.add(2 * x)
                return seen
            """)
        plan = analyze_source(source)
        assert len(plan.safe) == 1
        new_source = apply_accumulates(source, plan)
        assert "set(" in new_source
        old, new = _exec_and_get(source, "uniq"), _exec_and_get(new_source, "uniq")
        for data in ([], [1], [1, 2, 2, 3], [-5, 5, None]):
            assert new(data) == old(data)
            assert type(new(data)) is type(old(data))
        assert analyze_source(new_source).safe == []

    def test_no_candidates_returns_source_unchanged(self):
        source = "def f(xs):\n    return len(xs)\n"
        assert apply_accumulates(source, analyze_source(source)) == source

    def test_output_is_always_valid_python(self):
        source = textwrap.dedent("""\
            def join(xs):
                s: str = ""
                for x in xs:
                    s += x
                return s
            """)
        new_source = apply_accumulates(source, analyze_source(source))
        ast.parse(new_source)
        assert "s: str =" in new_source

    def test_second_pass_finds_nothing_left_to_rewrite(self):
        source = textwrap.dedent("""\
            def join(xs):
                s = ""
                for x in xs:
                    s += x
                return s
            """)
        once = apply_accumulates(source, analyze_source(source))
        assert analyze_source(once).safe == []

    def test_stale_plan_fails_closed_file_untouched(self, tmp_path):
        mod = tmp_path / "mod.py"
        mod.write_text(textwrap.dedent("""\
            def join(xs):
                s = ""
                for x in xs:
                    s += x
                return s
            """))
        plan = analysis.analyze_file(str(mod))
        assert len(plan.safe) == 1
        mod.write_text(textwrap.dedent("""\
            def join(xs):
                s = ""
                for x in xs:
                    s += x + "!"
                return s
            """))
        import pytest

        with pytest.raises(apply_mod.PlanMismatchError):
            apply_accumulates(mod.read_text(), plan)
        assert 's += x + "!"' in mod.read_text()  # nothing written


class TestClassifyTiers:
    def test_accumulator_rows_route_to_tier0(self, tmp_path):
        cf = _load_module(
            "_test_classify_for_acc", "code-optimizer/scripts/classify_findings.py",
        )
        mod = tmp_path / "mod.py"
        mod.write_text(textwrap.dedent("""\
            def join(xs):
                s = ""
                for x in xs:
                    s += x
                return s

            def total(xs):
                n = 0
                for x in xs:
                    n += x
                return n

            def uniq(xs):
                seen = set()
                for x in xs:
                    seen.add(x)
                return seen
            """))
        rows = [
            {"file": str(mod), "function": "join", "empirical_big_o": "Linear",
             "complexity_rank": "2", "big_o_error": "", "line_profile_error": ""},
            {"file": str(mod), "function": "total", "empirical_big_o": "Linear",
             "complexity_rank": "2", "big_o_error": "", "line_profile_error": ""},
            {"file": str(mod), "function": "uniq", "empirical_big_o": "Linear",
             "complexity_rank": "2", "big_o_error": "", "line_profile_error": ""},
        ]
        cf.classify_rows(rows)
        assert rows[0]["tier"] == "tier0_str_join"
        assert rows[1]["tier"] == "tier0_sum_reduce"
        assert rows[2]["tier"] == "tier0_set_build"
        assert "set(...)" in rows[2]["tier_detail"]
