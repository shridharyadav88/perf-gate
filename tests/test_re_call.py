"""Tests for re_call_analysis.py (detection) and apply_re_call.py."""

from __future__ import annotations

import ast
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
    "_test_recall_analysis",
    "code-optimizer/scripts/detectors/re_call_analysis.py",
)
apply_mod = _load_module(
    "_test_apply_recall",
    "code-optimizer/scripts/resolvers/apply_re_call.py",
)

analyze_source = analysis.analyze_source
apply_re_calls = apply_mod.apply_re_calls

WITH_RE = "import re\n\n"


def _exec_and_get(source: str, func_name: str):
    ns: dict = {}
    exec(compile(source, "<test>", "exec"), ns)
    return ns[func_name]


class TestDetection:
    def test_match_assign_is_safe(self):
        plan = analyze_source(WITH_RE + textwrap.dedent("""\
            def f(s):
                m = re.match(r"(\\d+)", s)
                return m
            """))
        assert [(c.method, c.func_name) for c in plan.safe] == [("match", "f")]
        cand = plan.safe[0]
        assert cand.module_name == "_re_match"
        assert cand.new_call_src == "_re_match.match(s)"

    def test_sub_flags_move_to_compile(self):
        plan = analyze_source(WITH_RE + textwrap.dedent("""\
            def g(s):
                return re.sub(r"x+", "-", s, flags=re.I)
            """))
        assert len(plan.safe) == 1
        cand = plan.safe[0]
        assert cand.hoist_src == "_re_sub = re.compile('x+', re.I)"
        assert cand.new_call_src == "_re_sub.sub('-', s)"

    def test_import_alias_is_honored(self):
        plan = analyze_source("import re as rx\n\ndef f(s):\n"
                              "    return rx.search(r'z', s)\n")
        assert len(plan.safe) == 1
        assert plan.safe[0].hoist_src.startswith("_re_search = rx.compile(")

    def test_non_constant_pattern_is_skipped(self):
        plan = analyze_source(WITH_RE + "def f(s, p):\n    return re.match(p, s)\n")
        assert plan.safe == []
        assert plan.skipped

    def test_star_args_is_skipped(self):
        plan = analyze_source(WITH_RE + "def f(s, a):\n    return re.match(*a)\n")
        assert plan.safe == []

    def test_nested_call_is_not_statement_value(self):
        plan = analyze_source(WITH_RE + "def f(s):\n    return [re.match(r'a', s)]\n")
        assert plan.safe == []

    def test_non_re_receiver_is_skipped(self):
        plan = analyze_source("def f(pat, s):\n    return pat.match(s)\n")
        assert plan.safe == []

    def test_nested_function_attributed_once(self):
        plan = analyze_source(WITH_RE + textwrap.dedent("""\
            def outer(s):
                def inner(t):
                    return re.match(r"a", t)
                return inner(s)
            """))
        assert [(c.func_name, c.lineno) for c in plan.safe] == [("inner", 5)]

    def test_name_collision_gets_suffix(self):
        plan = analyze_source(WITH_RE + "_re_match = 1\n\ndef f(s):\n"
                              "    return re.match(r'a', s)\n")
        assert len(plan.safe) == 1
        assert plan.safe[0].module_name == "_re_match_2"


class TestApplyCorrectness:
    def test_rewritten_calls_behave_identically(self):
        source = WITH_RE + textwrap.dedent("""\
            def f(s):
                m = re.match(r"(\\d+)", s)
                return m.group(1) if m else None

            def g(s):
                return re.sub(r"x+", "-", s, flags=re.I)
            """)
        plan = analyze_source(source)
        assert len(plan.safe) == 2
        new_source = apply_re_calls(source, plan)
        for name, args, _ in (("f", ("123abc",), None), ("f", ("abc",), None),
                              ("g", ("aXXb",), None)):
            old = _exec_and_get(source, name)(*args)
            new = _exec_and_get(new_source, name)(*args)
            assert new == old

    def test_non_ascii_line_survives_byte_surgery(self):
        source = WITH_RE + 'def h(s):\n    note = "caf\u00e9 \u00fcn\u00efcod\u00e9"\n' \
            '    return re.search(r"z", s)\n'
        plan = analyze_source(source)
        assert len(plan.safe) == 1
        new_source = apply_re_calls(source, plan)
        ast.parse(new_source)
        assert "caf\u00e9 \u00fcn\u00efcod\u00e9" in new_source
        assert _exec_and_get(new_source, "h")("zzz") is not None

    def test_hoist_block_lands_after_imports(self):
        source = '"""Doc."""\nimport os\nimport re\n\ndef f(s):\n    return re.match(r"a", s)\n'
        new_source = apply_re_calls(source, analyze_source(source))
        compile_pos = new_source.index("_re_match = re.compile")
        import_pos = new_source.index("import re\n")
        def_pos = new_source.index("def f")
        assert import_pos < compile_pos < def_pos

    def test_no_candidates_returns_source_unchanged(self):
        source = "def f(s):\n    return s\n"
        assert apply_re_calls(source, analyze_source(source)) == source

    def test_second_pass_finds_nothing_left_to_rewrite(self):
        source = WITH_RE + "def f(s):\n    return re.match(r'a', s)\n"
        once = apply_re_calls(source, analyze_source(source))
        assert analyze_source(once).safe == []

    def test_stale_plan_fails_closed_file_untouched(self, tmp_path):
        mod = tmp_path / "mod.py"
        mod.write_text(WITH_RE + "def f(s):\n    return re.match(r'a', s)\n")
        plan = analysis.analyze_file(str(mod))
        assert len(plan.safe) == 1
        mod.write_text(WITH_RE + "def f(s):\n    return re.match(r'b', s)\n")
        with pytest.raises(apply_mod.PlanMismatchError):
            apply_re_calls(mod.read_text(), plan)
        assert "r'b'" in mod.read_text()  # nothing written


class TestClassifyTiers:
    def test_re_call_rows_route_to_tier0(self, tmp_path):
        cf = _load_module(
            "_test_classify_for_recall", "code-optimizer/scripts/classify_findings.py",
        )
        mod = tmp_path / "mod.py"
        mod.write_text(WITH_RE + "def f(s):\n    return re.match(r'a', s)\n")
        rows = [
            {"file": str(mod), "function": "f", "empirical_big_o": "Constant",
             "complexity_rank": "0", "big_o_error": "", "line_profile_error": ""},
        ]
        cf.classify_rows(rows)
        assert rows[0]["tier"] == "tier0_re_call"


class TestScopeShadowing:
    def test_closure_re_param_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            import re


            def outer(re):
                def inner(s):
                    return re.sub("a", "b", s)
                return inner
            """))
        assert plan.safe == []
        assert any("function scope" in reason for _, reason in plan.skipped)

    def test_local_name_collision_renames_hoist(self):
        # A function-local _re_sub must not capture the call site: the
        # hoisted name is suffixed instead, and the fix is preserved.
        plan = analyze_source(textwrap.dedent("""\
            import re


            def f(s, _re_sub):
                return re.sub("a", "b", s)
            """))
        assert [(c.func_name, c.module_name) for c in plan.safe] == [("f", "_re_sub_2")]
