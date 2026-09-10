"""Tests for regex_hoist_analysis.py (detection) and apply_regex_hoist.py (the codemod).

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

import pytest


def _load_module(name: str, rel_path: str):
    pkg_skills = importlib.resources.files("perf_gate") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    # dataclasses (used in regex_hoist_analysis.py) needs the module registered
    # in sys.modules to resolve `from __future__ import annotations` string
    # annotations at class-definition time.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


analysis = _load_module(
    "_test_regex_hoist_analysis", "perf-gate/scripts/detectors/regex_hoist_analysis.py",
)
apply_mod = _load_module(
    "_test_apply_regex_hoist", "perf-gate/scripts/resolvers/apply_regex_hoist.py",
)

analyze_source = analysis.analyze_source
apply_hoist = apply_mod.apply_hoist


def _exec_and_get(source: str, func_name: str):
    ns: dict = {}
    exec(compile(source, "<test>", "exec"), ns)
    return ns[func_name]


class TestDetection:
    def test_simple_independent_pattern_is_safe(self):
        source = textwrap.dedent(
            r"""
            import re

            def f(line):
                pattern = re.compile(r'^\d+$')
                return bool(pattern.match(line))
            """
        )
        plan = analyze_source(source)
        assert len(plan.safe) == 1
        assert plan.safe[0].var_name == "pattern"
        assert plan.safe[0].functions == ["f"]
        assert plan.skipped == []

    def test_pattern_depending_on_parameter_is_not_safe(self):
        source = textwrap.dedent(
            r"""
            import re

            def f(prefix, line):
                pattern = re.compile(prefix + r'\d+$')
                return bool(pattern.match(line))
            """
        )
        plan = analyze_source(source)
        assert plan.safe == []

    def test_pattern_depending_on_local_variable_is_not_safe(self):
        source = textwrap.dedent(
            r"""
            import re

            def compute_suffix(line):
                return line[-1]

            def f(line):
                suffix = compute_suffix(line)
                pattern = re.compile(suffix)
                return bool(pattern.match(line))
            """
        )
        plan = analyze_source(source)
        assert plan.safe == []

    def test_duplicate_identical_pattern_across_functions_is_merged(self):
        source = textwrap.dedent(
            r"""
            import re

            def f(line):
                pattern = re.compile(r'^\d+$')
                return bool(pattern.match(line))

            def g(line):
                pattern = re.compile(r'^\d+$')
                return bool(pattern.search(line))
            """
        )
        plan = analyze_source(source)
        assert len(plan.safe) == 1
        assert set(plan.safe[0].functions) == {"f", "g"}
        assert len(plan.safe[0].occurrences) == 2

    def test_same_name_different_pattern_is_skipped(self):
        source = textwrap.dedent(
            r"""
            import re

            def f(line):
                pattern = re.compile(r'^\d+$')
                return bool(pattern.match(line))

            def g(line):
                pattern = re.compile(r'^[a-z]+$')
                return bool(pattern.match(line))
            """
        )
        plan = analyze_source(source)
        assert plan.safe == []
        assert any(name == "pattern" for name, _reason in plan.skipped)

    def test_collision_with_existing_module_level_name_is_skipped(self):
        source = textwrap.dedent(
            r"""
            import re

            pattern = "not a regex, a different module-level constant"

            def f(line):
                pattern = re.compile(r'^\d+$')
                return bool(pattern.match(line))
            """
        )
        plan = analyze_source(source)
        assert plan.safe == []
        assert any(name == "pattern" for name, _reason in plan.skipped)

    def test_multiline_concatenated_pattern_is_safe(self):
        source = textwrap.dedent(
            r"""
            import re

            def f(line):
                pattern = re.compile(
                    r'^\s*[A-Z]'
                    r'[a-z]+\s*$'
                )
                return bool(pattern.match(line))
            """
        )
        plan = analyze_source(source)
        assert len(plan.safe) == 1
        assert "pattern" in plan.safe[0].source_text


class TestApplyCorrectness:
    def test_hoisted_module_still_behaves_identically(self):
        source = textwrap.dedent(
            r"""
            import re

            def f(line):
                pattern = re.compile(r'^\d+$')
                return bool(pattern.match(line))
            """
        )
        plan = analyze_source(source)
        new_source = apply_hoist(source, plan)

        before = _exec_and_get(source, "f")
        after = _exec_and_get(new_source, "f")
        for sample in ["123", "abc", "", "42x", "0"]:
            assert before(sample) == after(sample)

        # And the compile call must actually be gone from inside f's body.
        assert "def f(line):\n    pattern = re.compile" not in new_source

    def test_hoisted_pattern_is_shared_object_across_calls(self):
        source = textwrap.dedent(
            r"""
            import re

            def f(line):
                pattern = re.compile(r'^\d+$')
                return pattern

            def g(line):
                pattern = re.compile(r'^\d+$')
                return pattern
            """
        )
        plan = analyze_source(source)
        new_source = apply_hoist(source, plan)
        ns: dict = {}
        exec(compile(new_source, "<test>", "exec"), ns)
        assert ns["f"]("x") is ns["g"]("x")  # same module-level object now

    def test_no_candidates_returns_source_unchanged(self):
        source = "import re\n\ndef f(x):\n    return re.match(x, x)\n"
        plan = analyze_source(source)
        assert apply_hoist(source, plan) == source

    def test_output_is_always_valid_python(self):
        source = textwrap.dedent(
            r"""
            '''Module docstring.'''
            import re
            import os

            def f(line):
                pattern = re.compile(r'^\d+$')
                return bool(pattern.match(line))

            def g(line):
                other = re.compile(r'^[a-z]+$')
                return bool(other.match(line))
            """
        )
        plan = analyze_source(source)
        new_source = apply_hoist(source, plan)
        ast.parse(new_source)  # must not raise

    def test_second_pass_finds_nothing_left_to_hoist(self):
        source = textwrap.dedent(
            r"""
            import re

            def f(line):
                pattern = re.compile(r'^\d+$')
                return bool(pattern.match(line))
            """
        )
        plan = analyze_source(source)
        new_source = apply_hoist(source, plan)
        second_plan = analyze_source(new_source)
        assert second_plan.safe == []

    def test_file_with_no_docstring_or_imports_inserts_at_top(self):
        source = textwrap.dedent(
            r"""
            import re

            def f(line):
                pattern = re.compile(r'^\d+$')
                return bool(pattern.match(line))
            """
        )
        plan = analyze_source(source)
        new_source = apply_hoist(source, plan)
        # The hoisted assignment must land after the `import re` line, before `def f`.
        lines = [ln for ln in new_source.splitlines() if ln.strip()]
        assert lines[0] == "import re"
        assert lines[1].startswith("pattern = re.compile")
        assert any(ln.startswith("def f(") for ln in lines)


@pytest.fixture
def real_world_style_source() -> str:
    # Mirrors the actual duplicated-pattern shape found in
    # crawl4ai/processors/pdf/utils.py's clean_pdf_text / clean_pdf_text_to_html.
    return textwrap.dedent(
        r"""
        import re
        import html

        def clean_a(page_number, text):
            email_pattern = re.compile(r'\{.*?\}')
            lines = text.split('\n')
            return [l for l in lines if email_pattern.match(l)]

        def clean_b(page_number, text):
            email_pattern = re.compile(r'\{.*?\}')
            lines = text.split('\n')
            return [html.escape(l) for l in lines if email_pattern.match(l)]
        """
    )


class TestRealWorldShape:
    def test_duplicated_pattern_across_two_functions_hoists_once(self, real_world_style_source):
        plan = analyze_source(real_world_style_source)
        assert len(plan.safe) == 1
        assert set(plan.safe[0].functions) == {"clean_a", "clean_b"}

        new_source = apply_hoist(real_world_style_source, plan)
        ns: dict = {}
        exec(compile(new_source, "<test>", "exec"), ns)
        text = "{a}\nno match\n{b}"
        assert ns["clean_a"](1, text) == ["{a}", "{b}"]
        assert ns["clean_b"](1, text) == ["{a}", "{b}"]


class TestScopeShadowing:
    def test_closure_pattern_is_not_safe(self):
        # `suffix` is bound in the enclosing function: at import time the
        # hoisted statement would raise NameError.
        source = textwrap.dedent(
            r"""
            import re

            def outer():
                suffix = "+"

                def inner(s):
                    pat = re.compile("a" + suffix)
                    return pat.search(s)

                return inner
            """
        )
        plan = analyze_source(source)
        assert plan.safe == []

    def test_param_receiver_is_not_safe(self):
        source = textwrap.dedent(
            r"""
            import re

            def f(re, s):
                pat = re.compile("a+")
                return pat.search(s)
            """
        )
        plan = analyze_source(source)
        assert plan.safe == []

    def test_param_shadowing_hoisted_name_is_not_safe(self):
        # Deleting the local assignment would leave the call sites reading
        # the parameter instead of the module global.
        source = textwrap.dedent(
            r"""
            import re

            def f(pat, s):
                pat = re.compile("a+")
                return pat.search(s)
            """
        )
        plan = analyze_source(source)
        assert plan.safe == []

    def test_module_level_pattern_constant_still_fires(self):
        # A module-bound constant stays hoistable: import-time and
        # call-time reads resolve to the same object.
        source = textwrap.dedent(
            r"""
            import re

            PREFIX = "a"

            def f(s):
                pat = re.compile(PREFIX + "+")
                return pat.search(s)
            """
        )
        plan = analyze_source(source)
        assert [(c.var_name, c.functions) for c in plan.safe] == [("pat", ["f"])]
