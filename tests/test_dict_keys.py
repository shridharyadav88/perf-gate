"""Tests for dict_keys_analysis.py (detection) and apply_dict_keys.py.

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
    "_test_dict_keys_analysis",
    "perf-gate/scripts/detectors/dict_keys_analysis.py",
)
apply_mod = _load_module(
    "_test_apply_dict_keys",
    "perf-gate/scripts/resolvers/apply_dict_keys.py",
)

analyze_source = analysis.analyze_source
apply_dict_keys = apply_mod.apply_dict_keys


def _exec_and_get(source: str, func_name: str):
    ns: dict = {}
    exec(compile(source, "<test>", "exec"), ns)
    return ns[func_name]


class TestDetection:
    def test_keys_call_simplified(self):
        plan = analyze_source(textwrap.dedent("""\
            def names(d):
                out = []
                for k in d.keys():
                    out.append(k)
                return out
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("dict_keys", "names")]
        assert plan.safe[0].edits[0][4] == "d"

    def test_list_wrapped_dict_simplified(self):
        plan = analyze_source(textwrap.dedent("""\
            def names(d):
                for k in list(d):
                    print(k)
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("dict_keys", "names")]

    def test_list_wrapped_keys_simplified(self):
        plan = analyze_source(textwrap.dedent("""\
            def names(d):
                for k in list(d.keys()):
                    print(k)
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("dict_keys", "names")]

    def test_comprehension_keys_simplified(self):
        plan = analyze_source(textwrap.dedent("""\
            def names(d):
                return [k.upper() for k in d.keys()]
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("dict_keys", "names")]

    def test_subscript_reads_are_safe(self):
        plan = analyze_source(textwrap.dedent("""\
            def total(d):
                s = 0
                for k in d.keys():
                    s += d[k]
                return s
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("dict_keys", "total")]


class TestNegatives:
    def test_subscript_write_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def fill(d, ks):
                for k in d.keys():
                    d[k] = 1
            """))
        assert plan.safe == []
        assert any("subscript" in reason for _, reason in plan.skipped)

    def test_delete_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def clean(d):
                for k in d.keys():
                    del d[k]
            """))
        assert plan.safe == []

    def test_mutating_method_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def drain(d):
                for k in d.keys():
                    d.pop(k)
            """))
        assert plan.safe == []
        assert any("pop" in reason for _, reason in plan.skipped)

    def test_bare_dict_escape_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def show(d):
                for k in d.keys():
                    print(k, d)
            """))
        assert plan.safe == []
        assert any("escapes" in reason for _, reason in plan.skipped)

    def test_inplace_or_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def merge(d, other):
                for k in d.keys():
                    d |= other
            """))
        assert plan.safe == []

    def test_async_for_untouched(self):
        plan = analyze_source(textwrap.dedent("""\
            async def names(aiter):
                async for k in aiter:
                    print(k)
            """))
        assert plan.safe == []

    def test_bare_dict_needs_no_fix(self):
        plan = analyze_source(textwrap.dedent("""\
            def names(d):
                for k in d:
                    print(k)
            """))
        assert plan.safe == [] and plan.skipped == []


class TestApply:
    SOURCE = textwrap.dedent("""\
        def total(d):
            s = 0
            for k in d.keys():
                s += d[k]
            return s
        """)

    def test_apply_rewrites(self):
        plan = analyze_source(self.SOURCE)
        out = apply_dict_keys(self.SOURCE, plan)
        assert "for k in d:" in out and ".keys()" not in out
        ast.parse(out)

    def test_results_identical(self):
        plan = analyze_source(self.SOURCE)
        out = apply_dict_keys(self.SOURCE, plan)
        data = {"a": 1, "b": 2}
        assert _exec_and_get(self.SOURCE, "total")(dict(data)) == \
            _exec_and_get(out, "total")(dict(data)) == 3

    def test_idempotent(self):
        once = apply_dict_keys(self.SOURCE, analyze_source(self.SOURCE))
        plan2 = analyze_source(once)
        assert plan2.safe == []
        assert apply_dict_keys(once, plan2) == once

    def test_collateral_diff_only_intended_lines(self):
        plan = analyze_source(self.SOURCE)
        out = apply_dict_keys(self.SOURCE, plan)
        diff = list(difflib.unified_diff(
            self.SOURCE.splitlines(), out.splitlines(), lineterm=""))
        changed = [line for line in diff
                   if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
        assert len(changed) == 2
        assert changed[0].startswith("-") and changed[1].startswith("+")

    def test_stale_plan_raises(self):
        plan = analyze_source(self.SOURCE)
        mutated = self.SOURCE.replace("d.keys()", "d.values()")
        with pytest.raises(apply_mod.PlanMismatchError):
            apply_dict_keys(mutated, plan)

    def test_empty_plan_returns_source(self):
        assert apply_dict_keys("x = 1\n", analysis.DictKeysPlan()) == "x = 1\n"


class TestScopeShadowing:
    def test_param_list_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def f(d, list):
                for k in list(d.keys()):
                    print(k)
            """))
        assert plan.safe == []
        assert any("function scope" in reason for _, reason in plan.skipped)
