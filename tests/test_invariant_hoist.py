"""Tests for invariant_hoist_analysis.py (detection) and
apply_invariant_hoist.py (the codemod).

Loaded via importlib.resources like the other bundled scripts — their parent
directory, ``perf-gate``, contains a hyphen so it can't be an importable
package.
"""

from __future__ import annotations

import ast
import importlib.resources
import importlib.util
import sys
import textwrap


def _load_module(name: str, rel_path: str):
    pkg_skills = importlib.resources.files("perf_gate") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


analysis = _load_module(
    "_test_invariant_hoist_analysis",
    "perf-gate/scripts/detectors/invariant_hoist_analysis.py",
)
apply_mod = _load_module(
    "_test_apply_invariant_hoist",
    "perf-gate/scripts/resolvers/apply_invariant_hoist.py",
)

analyze_source = analysis.analyze_source
apply_hoists = apply_mod.apply_hoists

PRELUDE = "SCALE = 10\n\n\n"


def _exec_and_get(source: str, func_name: str):
    ns: dict = {}
    exec(compile(source, "<test>", "exec"), ns)
    return ns[func_name]


class TestDetection:
    def test_plain_const_loop_hoists(self):
        plan = analyze_source(PRELUDE + textwrap.dedent("""\
            def k(xs):
                total = 0
                for x in xs:
                    total += SCALE * x
                return total
            """))
        assert [(c.name, c.alias) for c in plan.safe] == [
            ("SCALE", "_hoisted_SCALE")]
        assert len(plan.safe[0].loads) == 1

    def test_while_loop_hoists(self):
        plan = analyze_source(PRELUDE + textwrap.dedent("""\
            def k(xs):
                total = 0
                i = 0
                while i < len(xs):
                    total += SCALE * xs[i]
                    i += 1
                return total
            """))
        assert [c.name for c in plan.safe] == ["SCALE"]

    def test_unrelated_store_does_not_void(self):
        plan = analyze_source(PRELUDE + textwrap.dedent("""\
            def k(xs):
                total = 0
                for x in xs:
                    total += SCALE * x
                    total += 1
                return total
            """))
        assert [c.name for c in plan.safe] == ["SCALE"]

    def test_store_to_name_skips_silently(self):
        plan = analyze_source(PRELUDE + textwrap.dedent("""\
            def k(xs):
                total = 0
                for x in xs:
                    SCALE = x
                    total += x
                return total
            """))
        assert plan.safe == []

    def test_param_named_like_global_skips(self):
        plan = analyze_source(PRELUDE + textwrap.dedent("""\
            def k(xs, SCALE):
                total = 0
                for x in xs:
                    total += SCALE * x
                return total
            """))
        assert plan.safe == []

    def test_nested_scope_in_loop_vetoes(self):
        plan = analyze_source(PRELUDE + textwrap.dedent("""\
            def k(xs):
                total = 0
                for x in xs:
                    def h():
                        return SCALE
                    total += h()
                return total
            """))
        assert plan.safe == []
        assert any("nested scope" in reason for _, reason in plan.skipped)

    def test_yield_in_loop_vetoes(self):
        plan = analyze_source(PRELUDE + textwrap.dedent("""\
            def k(xs):
                total = 0
                for x in xs:
                    total += SCALE * x
                    yield total
            """))
        assert plan.safe == []

    def test_yield_after_loop_is_fine(self):
        plan = analyze_source(PRELUDE + textwrap.dedent("""\
            def k(xs):
                total = 0
                for x in xs:
                    total += SCALE * x
                yield total
            """))
        assert [c.name for c in plan.safe] == ["SCALE"]

    def test_await_in_loop_vetoes(self):
        plan = analyze_source(PRELUDE + textwrap.dedent("""\
            async def k(xs):
                total = 0
                for x in xs:
                    total += SCALE * (await x)
                return total
            """))
        assert plan.safe == []

    def test_async_for_never_fires(self):
        plan = analyze_source(PRELUDE + textwrap.dedent("""\
            async def k(xs):
                total = 0
                async for x in xs:
                    total += SCALE * x
                return total
            """))
        assert plan.safe == []

    def test_exec_call_vetoes(self):
        plan = analyze_source(PRELUDE + textwrap.dedent("""\
            def k(xs):
                total = 0
                for x in xs:
                    total += SCALE * x
                    exec("pass")
                return total
            """))
        assert plan.safe == []

    def test_global_declared_callee_with_call_skips(self):
        plan = analyze_source(textwrap.dedent("""\
            SCALE = 10

            def bump():
                global SCALE
                SCALE += 1

            def k(xs):
                total = 0
                for x in xs:
                    bump()
                    total += SCALE * x
                return total
            """))
        # The callee itself is still hoistable (module function ref), but
        # SCALE must not be: bump() may rebind it between iterations.
        assert "SCALE" not in [c.name for c in plan.safe]
        assert any("rebind" in reason for _, reason in plan.skipped)

    def test_global_declared_callee_without_call_is_fine(self):
        plan = analyze_source(textwrap.dedent("""\
            SCALE = 10

            def bump():
                global SCALE
                SCALE += 1

            def k(xs):
                total = 0
                for x in xs:
                    total += SCALE * x
                return total
            """))
        assert [c.name for c in plan.safe] == ["SCALE"]

    def test_conditional_binding_proves_nothing(self):
        plan = analyze_source(textwrap.dedent("""\
            import sys

            if sys.version_info >= (3, 9):
                SCALE = 10

            def k(xs):
                total = 0
                for x in xs:
                    total += SCALE * x
                return total
            """))
        assert plan.safe == []

    def test_module_del_voids_proof(self):
        plan = analyze_source(textwrap.dedent("""\
            SCALE = 10
            del SCALE

            def k(xs):
                total = 0
                for x in xs:
                    total += SCALE * x
                return total
            """))
        assert plan.safe == []

    def test_star_import_voids_module(self):
        plan = analyze_source(textwrap.dedent("""\
            from mod import *

            def k(xs):
                total = 0
                for x in xs:
                    total += SCALE * x
                return total
            """))
        assert plan.safe == [] and plan.skipped == []

    def test_attribute_loads_are_not_hoisted(self):
        plan = analyze_source(textwrap.dedent("""\
            class Svc:
                def k(self, xs):
                    total = 0
                    for x in xs:
                        total += self.factor * x
                    return total
            """))
        assert plan.safe == []

    def test_alias_collision_skips(self):
        plan = analyze_source(PRELUDE + textwrap.dedent("""\
            def k(xs):
                _hoisted_SCALE = -1
                total = 0
                for x in xs:
                    total += SCALE * x
                return total
            """))
        assert plan.safe == []
        assert any("alias" in reason for _, reason in plan.skipped)

    def test_plain_calls_do_not_veto(self):
        plan = analyze_source(PRELUDE + textwrap.dedent("""\
            def k(xs):
                out = []
                for x in xs:
                    out.append(SCALE * len(x))
                return out
            """))
        assert [c.name for c in plan.safe] == ["SCALE"]


class TestApplyCorrectness:
    def test_rewritten_loop_behaves_identically(self):
        source = PRELUDE + textwrap.dedent("""\
            def k(xs):
                total = 0
                for x in xs:
                    total += SCALE * x
                return total
            """)
        plan = analyze_source(source)
        assert len(plan.safe) == 1
        new_source = apply_hoists(source, plan)
        assert "_hoisted_SCALE = SCALE" in new_source
        assert "total += _hoisted_SCALE * x" in new_source
        old, new = _exec_and_get(source, "k"), _exec_and_get(new_source, "k")
        for data in ([], [1], [1, 2, 3], [-5, 5]):
            assert new(data) == old(data)
        assert analyze_source(new_source).safe == []

    def test_no_candidates_returns_source_unchanged(self):
        source = "def f(xs):\n    return len(xs)\n"
        assert apply_hoists(source, analyze_source(source)) == source

    def test_multi_candidate_same_loop_applies_cleanly(self):
        source = textwrap.dedent("""\
            A = 3
            B = 5

            def k(xs):
                total = 0
                for x in xs:
                    total += A * x + B * x
                return total
            """)
        plan = analyze_source(source)
        assert sorted(c.name for c in plan.safe) == ["A", "B"]
        new_source = apply_hoists(source, plan)
        ast.parse(new_source)
        assert "_hoisted_A = A" in new_source
        assert "_hoisted_B = B" in new_source
        assert "total += _hoisted_A * x + _hoisted_B * x" in new_source
        old, new = _exec_and_get(source, "k"), _exec_and_get(new_source, "k")
        assert new([1, 2]) == old([1, 2]) == 24
        assert analyze_source(new_source).safe == []

    def test_output_is_always_valid_python(self):
        source = PRELUDE + textwrap.dedent("""\
            def k(xs):
                total = 0
                for x in xs:
                    total += SCALE * x
                return total
            """)
        new_source = apply_hoists(source, analyze_source(source))
        ast.parse(new_source)

    def test_stale_plan_fails_closed_file_untouched(self, tmp_path):
        mod = tmp_path / "mod.py"
        mod.write_text(PRELUDE + textwrap.dedent("""\
            def k(xs):
                total = 0
                for x in xs:
                    total += SCALE * x
                return total
            """))
        plan = analysis.analyze_file(str(mod))
        assert len(plan.safe) == 1
        mod.write_text(PRELUDE + textwrap.dedent("""\
            def k(xs):
                total = 0
                for x in xs:
                    total += OTHER * x
                return total
            """))
        import pytest

        with pytest.raises(apply_mod.PlanMismatchError):
            apply_hoists(mod.read_text(), plan)
        assert "OTHER * x" in mod.read_text()  # nothing written


class TestClassifyTiers:
    def test_hoist_rows_route_to_tier0(self, tmp_path):
        cf = _load_module(
            "_test_classify_for_invariant", "perf-gate/scripts/classify_findings.py",
        )
        mod = tmp_path / "mod.py"
        mod.write_text(PRELUDE + textwrap.dedent("""\
            def k(xs):
                for x in xs:
                    print(SCALE, x)
            """))
        rows = [
            {"file": str(mod), "function": "k", "empirical_big_o": "Linear",
             "complexity_rank": "2", "big_o_error": "", "line_profile_error": ""},
        ]
        cf.classify_rows(rows)
        assert rows[0]["tier"] == "tier0_invariant_hoist"
        assert "_hoisted_SCALE" in rows[0]["tier_detail"]
