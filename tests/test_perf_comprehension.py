"""Tests for perf_comprehension_analysis.py (detection) and
apply_perf_comprehension.py (the codemod).

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
    "_test_perf_comprehension_analysis",
    "code-optimizer/scripts/detectors/perf_comprehension_analysis.py",
)
apply_mod = _load_module(
    "_test_apply_perf_comprehension",
    "code-optimizer/scripts/resolvers/apply_perf_comprehension.py",
)

analyze_source = analysis.analyze_source
apply_rewrites = apply_mod.apply_rewrites


def _exec_and_get(source: str, func_name: str):
    ns: dict = {}
    exec(compile(source, "<test>", "exec"), ns)
    return ns[func_name]


class TestDetection:
    def test_simple_build_loop_is_perf401(self):
        source = textwrap.dedent(
            """\
            def f(items):
                out = []
                for x in items:
                    out.append(x * 2)
                return out
            """
        )
        plan = analyze_source(source)
        assert len(plan.safe) == 1
        assert plan.safe[0].kind == "perf401"
        assert plan.safe[0].var_name == "out"

    def test_guarded_append_is_perf401(self):
        source = textwrap.dedent(
            """\
            def f(items):
                out = []
                for x in items:
                    if x:
                        out.append(x)
                return out
            """
        )
        plan = analyze_source(source)
        assert len(plan.safe) == 1
        assert plan.safe[0].kind == "perf401"

    def test_identity_copy_loop_is_perf402(self):
        source = textwrap.dedent(
            """\
            def f(items):
                out = []
                for x in items:
                    out.append(x)
                return out
            """
        )
        plan = analyze_source(source)
        assert len(plan.safe) == 1
        assert plan.safe[0].kind == "perf402"

    def test_loop_variable_read_after_loop_is_skipped(self):
        source = textwrap.dedent(
            """\
            def f(items):
                out = []
                for x in items:
                    out.append(x)
                return x
            """
        )
        plan = analyze_source(source)
        assert plan.safe == []
        assert any("read after the loop" in reason for _, reason in plan.skipped)

    def test_for_else_is_skipped(self):
        source = textwrap.dedent(
            """\
            def f(items):
                out = []
                for x in items:
                    out.append(x)
                else:
                    out.append(-1)
                return out
            """
        )
        plan = analyze_source(source)
        assert plan.safe == []

    def test_multi_statement_body_is_skipped(self):
        source = textwrap.dedent(
            """\
            def f(items):
                out = []
                for x in items:
                    out.append(x)
                    print(x)
                return out
            """
        )
        plan = analyze_source(source)
        assert plan.safe == []

    def test_simple_dict_loop_is_perf403(self):
        source = textwrap.dedent(
            """\
            def f(items):
                d = {}
                for x in items:
                    d[x.id] = x
                return d
            """
        )
        plan = analyze_source(source)
        assert len(plan.safe) == 1
        assert plan.safe[0].kind == "perf403"
        assert "{x.id: x for x in items}" in plan.safe[0].replacement

    def test_guarded_dict_loop_is_perf403(self):
        source = textwrap.dedent(
            """\
            def f(items):
                d = {}
                for x in items:
                    if x:
                        d[x.id] = str(x)
                return d
            """
        )
        plan = analyze_source(source)
        (cand,) = plan.safe
        assert cand.kind == "perf403"
        assert "if x" in cand.replacement

    def test_identity_dict_loop_becomes_dict_call(self):
        source = textwrap.dedent(
            """\
            def f(items):
                d = {}
                for k, v in items:
                    d[k] = v
                return d
            """
        )
        plan = analyze_source(source)
        (cand,) = plan.safe
        assert cand.kind == "perf403"
        assert cand.replacement.strip().endswith("d = dict(items)")

    def test_shadowed_dict_name_skips_copy(self):
        source = textwrap.dedent(
            """\
            def dict(items):
                return list(items)

            def f(items):
                d = {}
                for k, v in items:
                    d[k] = v
                return d
            """
        )
        plan = analyze_source(source)
        assert plan.safe == []
        assert any("rebound" in reason for _, reason in plan.skipped)

    def test_dict_self_reference_is_skipped(self):
        source = textwrap.dedent(
            """\
            def f(items):
                d = {}
                for x in items:
                    d[x.id] = d
                return d
            """
        )
        plan = analyze_source(source)
        assert plan.safe == []

    def test_list_init_still_ignores_subscript_store(self):
        source = textwrap.dedent(
            """\
            def f(items):
                out = []
                for x in items:
                    out[x.id] = x
                return out
            """
        )
        plan = analyze_source(source)
        assert plan.safe == []

    def test_nested_loop_build_is_perf401(self):
        source = textwrap.dedent(
            """\
            def f(matrix):
                out = []
                for row in matrix:
                    for x in row:
                        out.append(x * 2)
                return out
            """
        )
        plan = analyze_source(source)
        assert len(plan.safe) == 1
        assert plan.safe[0].kind == "perf401"
        assert (plan.safe[0].replacement.strip()
                == "out = [x * 2 for row in matrix for x in row]")

    def test_nested_guarded_loop_carries_both_conds(self):
        source = textwrap.dedent(
            """\
            def f(matrix):
                out = []
                for row in matrix:
                    for x in row:
                        if x:
                            out.append(x)
                return out
            """
        )
        plan = analyze_source(source)
        (cand,) = plan.safe
        assert cand.kind == "perf401"
        assert cand.replacement.strip() == \
            "out = [x for row in matrix for x in row if x]"

    def test_triple_nest_is_skipped(self):
        source = textwrap.dedent(
            """\
            def f(cube):
                out = []
                for m in cube:
                    for row in m:
                        for x in row:
                            out.append(x)
                return out
            """
        )
        plan = analyze_source(source)
        assert plan.safe == []

    def test_mixed_sync_async_nest_is_skipped(self):
        source = textwrap.dedent(
            """\
            async def f(matrix):
                out = []
                for row in matrix:
                    async for x in row:
                        out.append(x)
                return out
            """
        )
        plan = analyze_source(source)
        assert plan.safe == []

    def test_nested_loop_var_read_after_is_skipped(self):
        source = textwrap.dedent(
            """\
            def f(matrix):
                out = []
                for row in matrix:
                    for x in row:
                        out.append(x)
                return row
            """
        )
        plan = analyze_source(source)
        assert plan.safe == []


class TestApplyCorrectness:
    def test_rewritten_build_loop_behaves_identically(self):
        source = textwrap.dedent(
            """\
            def f(items):
                out = []
                for x in items:
                    if x % 2 == 0:
                        out.append(x * 10)
                return out
            """
        )
        plan = analyze_source(source)
        new_source = apply_rewrites(source, plan)
        assert "out = [x * 10 for x in items if x % 2 == 0]" in new_source

        before = _exec_and_get(source, "f")
        after = _exec_and_get(new_source, "f")
        assert before([1, 2, 3, 4]) == after([1, 2, 3, 4]) == [20, 40]

    def test_rewritten_copy_loop_behaves_identically(self):
        source = textwrap.dedent(
            """\
            def f(items):
                out = []
                for x in items:
                    out.append(x)
                return out
            """
        )
        plan = analyze_source(source)
        new_source = apply_rewrites(source, plan)
        assert "out = list(items)" in new_source

        before = _exec_and_get(source, "f")
        after = _exec_and_get(new_source, "f")
        assert before([1, 2]) == after([1, 2]) == [1, 2]

    def test_rewritten_dict_loop_behaves_identically(self):
        source = textwrap.dedent(
            """\
            def f(items):
                d = {}
                for x in items:
                    if x[0]:
                        d[x[0]] = x[1] * 2
                return d
            """
        )
        plan = analyze_source(source)
        new_source = apply_rewrites(source, plan)
        assert "d = {x[0]: x[1] * 2 for x in items if x[0]}" in new_source

        before = _exec_and_get(source, "f")
        after = _exec_and_get(new_source, "f")
        data = [("a", 1), ("", 2), ("b", 3)]
        assert before(data) == after(data) == {"a": 2, "b": 6}
        assert analyze_source(new_source).safe == []

    def test_rewritten_dict_copy_behaves_identically(self):
        source = textwrap.dedent(
            """\
            def f(items):
                d = {}
                for k, v in items:
                    d[k] = v
                return d
            """
        )
        plan = analyze_source(source)
        new_source = apply_rewrites(source, plan)
        assert "d = dict(items)" in new_source

        before = _exec_and_get(source, "f")
        after = _exec_and_get(new_source, "f")
        assert before([("a", 1)]) == after([("a", 1)]) == {"a": 1}
        assert before([]) == after([]) == {}

    def test_rewritten_nested_loop_behaves_identically(self):
        source = textwrap.dedent(
            """\
            def f(matrix):
                out = []
                for row in matrix:
                    for x in row:
                        out.append(x * 3)
                return out
            """
        )
        plan = analyze_source(source)
        new_source = apply_rewrites(source, plan)
        assert "out = [x * 3 for row in matrix for x in row]" in new_source

        before = _exec_and_get(source, "f")
        after = _exec_and_get(new_source, "f")
        data = [[1, 2], [], [3]]
        assert before(data) == after(data) == [3, 6, 9]
        assert analyze_source(new_source).safe == []

    def test_no_candidates_returns_source_unchanged(self):
        source = "def f(xs):\n    return sorted(xs)\n"
        plan = analyze_source(source)
        assert apply_rewrites(source, plan) == source

    def test_output_is_always_valid_python(self):
        source = textwrap.dedent(
            """\
            def f(items):
                out = []
                for x in items:
                    out.append(str(x))
                return out
            """
        )
        plan = analyze_source(source)
        new_source = apply_rewrites(source, plan)
        ast.parse(new_source)  # must not raise

    def test_second_pass_finds_nothing_left_to_rewrite(self):
        source = textwrap.dedent(
            """\
            def f(items):
                out = []
                for x in items:
                    out.append(x)
                return out
            """
        )
        plan = analyze_source(source)
        new_source = apply_rewrites(source, plan)
        assert analyze_source(new_source).safe == []


class TestClassifyTiers:
    def test_perf_rows_route_to_tier0(self, tmp_path):
        cf = _load_module(
            "_test_classify_for_perf", "code-optimizer/scripts/classify_findings.py",
        )
        mod = tmp_path / "mod.py"
        mod.write_text(
            textwrap.dedent(
                """\
                def build(items):
                    out = []
                    for x in items:
                        out.append(x * 2)
                    return out

                def copy(items):
                    out = []
                    for x in items:
                        out.append(x)
                    return out

                def to_dict(items):
                    d = {}
                    for k, v in items:
                        d[k] = v
                    return d

                def build_dict(items):
                    d = {}
                    for x in items:
                        d[x[0]] = x[1]
                    return d
                """
            )
        )
        rows = [
            {"file": str(mod), "function": "build", "empirical_big_o": "Linear",
             "complexity_rank": "2", "big_o_error": "", "line_profile_error": ""},
            {"file": str(mod), "function": "copy", "empirical_big_o": "Linear",
             "complexity_rank": "2", "big_o_error": "", "line_profile_error": ""},
            {"file": str(mod), "function": "to_dict", "empirical_big_o": "Linear",
             "complexity_rank": "2", "big_o_error": "", "line_profile_error": ""},
            {"file": str(mod), "function": "build_dict", "empirical_big_o": "Linear",
             "complexity_rank": "2", "big_o_error": "", "line_profile_error": ""},
        ]
        cf.classify_rows(rows)
        assert rows[0]["tier"] == "tier0_perf401"
        assert rows[1]["tier"] == "tier0_perf402"
        assert rows[2]["tier"] == "tier0_perf403"
        assert "dict(...)" in rows[2]["tier_detail"]
        assert rows[3]["tier"] == "tier0_perf403"
        assert "dict comprehension" in rows[3]["tier_detail"]

    def test_nested_loop_routes_to_tier0(self, tmp_path):
        cf = _load_module(
            "_test_classify_for_nested", "code-optimizer/scripts/classify_findings.py",
        )
        mod = tmp_path / "mod.py"
        mod.write_text(
            textwrap.dedent(
                """\
                def flat(matrix):
                    out = []
                    for row in matrix:
                        for x in row:
                            out.append(x * 2)
                    return out
                """
            )
        )
        rows = [
            {"file": str(mod), "function": "flat", "empirical_big_o": "Linear",
             "complexity_rank": "2", "big_o_error": "", "line_profile_error": ""},
        ]
        cf.classify_rows(rows)
        assert rows[0]["tier"] == "tier0_perf401"
        assert "for row in matrix for x in row" in rows[0]["tier_detail"] or \
            "comprehension" in rows[0]["tier_detail"]


class TestScopeShadowing:
    def test_param_list_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def f(items, list):
                out = []
                for it in items:
                    out.append(it)
                return out
            """))
        assert plan.safe == []
        assert any("function scope" in reason for _, reason in plan.skipped)

    def test_param_dict_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def f(pairs, dict):
                out = {}
                for k, v in pairs:
                    out[k] = v
                return out
            """))
        assert plan.safe == []
