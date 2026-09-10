"""Tests for detectors/span_dedupe.py (nested-function duplicate removal).

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
from types import SimpleNamespace


def _load_module(name: str, rel_path: str):
    pkg_skills = importlib.resources.files("perf_gate") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dedupe_mod = _load_module(
    "_test_span_dedupe",
    "perf-gate/scripts/detectors/span_dedupe.py",
)

dedupe_nested_spans = dedupe_mod.dedupe_nested_spans


def _cand(func_name, kind="membership", lineno=4, edits=((4, 11, 4, 30, "{1, 2}"),)):
    return SimpleNamespace(func_name=func_name, kind=kind, lineno=lineno,
                           end_lineno=lineno, edits=list(edits))


NESTED = ast.parse(textwrap.dedent("""\
    def outer(x):
        def middle(x):
            def inner(x):
                return x
            return inner(x)
        return middle(x)
    """))


class TestDedupeNestedSpans:
    def test_exact_duplicates_keep_innermost(self):
        inner_edits = ((4, 11, 4, 30, "{1, 2}"),)
        cands = [_cand("outer", edits=inner_edits),
                 _cand("middle", edits=inner_edits),
                 _cand("inner", edits=inner_edits)]
        kept = dedupe_nested_spans(NESTED, cands)
        assert [(c.func_name) for c in kept] == ["inner"]

    def test_distinct_same_line_spans_both_kept(self):
        cands = [_cand("inner", edits=((4, 11, 4, 20, "{1}"),)),
                 _cand("inner", edits=((4, 30, 4, 39, "{2}"),))]
        assert dedupe_nested_spans(NESTED, cands) == cands

    def test_different_kinds_same_span_both_kept(self):
        cands = [_cand("inner", kind="membership"),
                 _cand("inner", kind="other")]
        assert dedupe_nested_spans(NESTED, cands) == cands

    def test_empty_edits_candidates_always_kept(self):
        cands = [_cand("outer", edits=[]), _cand("inner", edits=[])]
        assert dedupe_nested_spans(NESTED, cands) == cands

    def test_malformed_candidates_always_kept(self):
        cands = [SimpleNamespace(func_name="outer"),
                 SimpleNamespace(func_name="inner")]
        assert dedupe_nested_spans(NESTED, cands) == cands

    def test_no_duplicates_passthrough_preserves_order(self):
        cands = [_cand("outer", lineno=2, edits=((2, 4, 2, 9, "x"),)),
                 _cand("inner", lineno=4)]
        assert dedupe_nested_spans(NESTED, cands) == cands

    def test_unmatched_scope_keeps_first_never_drops(self):
        edits = ((4, 11, 4, 30, "{1, 2}"),)
        cands = [_cand("ghost_outer", edits=edits),
                 _cand("ghost_inner", edits=edits)]
        kept = dedupe_nested_spans(NESTED, cands)
        assert [c.func_name for c in kept] == ["ghost_outer"]

    def test_method_nesting_keeps_method(self):
        tree = ast.parse(textwrap.dedent("""\
            class Worker:
                def run(self, x):
                    def helper(y):
                        return y in [1, 2, 3]
                    return helper(x)
            """))
        edits = ((5, 15, 5, 34, "{1, 2, 3}"),)
        cands = [_cand("run", edits=edits), _cand("helper", edits=edits)]
        kept = dedupe_nested_spans(tree, cands)
        assert [c.func_name for c in kept] == ["helper"]

    def test_mixed_unique_and_duplicate(self):
        dup = ((4, 11, 4, 30, "{1, 2}"),)
        unique = _cand("outer", lineno=2, edits=((2, 4, 2, 9, "x"),))
        cands = [unique, _cand("outer", edits=dup), _cand("inner", edits=dup)]
        kept = dedupe_nested_spans(NESTED, cands)
        assert kept == [unique, cands[2]]


class TestScopeBinds:
    def _func(self, source: str, name: str = "f"):
        tree = ast.parse(textwrap.dedent(source))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return tree, node
        raise AssertionError(f"no function {name}")

    def test_params_assigns_imports_except_with_for_walrus(self):
        _tree, node = self._func("""\
            def f(a, *args, k=None, **kw):
                import os as operating
                from sys import path as spath
                b = 1
                try:
                    pass
                except ValueError as e:
                    pass
                with open("x") as handle:
                    pass
                for i, (j, *rest) in []:
                    pass
                if (w := 1):
                    pass
                del b
            """)
        bound = dedupe_mod.scope_binds(node)
        for name in ("a", "args", "k", "kw", "operating", "spath", "b", "e",
                     "handle", "i", "j", "rest", "w"):
            assert name in bound, name

    def test_nested_def_name_binds_but_body_assigns_do_not(self):
        _tree, node = self._func("""\
            def f(xs):
                def helper():
                    shadowed = 1
                return len(xs)
            """)
        bound = dedupe_mod.scope_binds(node)
        assert "helper" in bound
        assert "shadowed" not in bound
        assert "xs" in bound

    def test_lambda_params_and_nonlocal_count(self):
        _tree, node = self._func("""\
            def f(xs):
                g = lambda total: total + 1
                def inner():
                    nonlocal total
                    total = 2
                return g
            """)
        bound = dedupe_mod.scope_binds(node)
        assert "total" in bound
        assert "g" in bound


class TestChainBlocked:
    def test_closure_param_visible_in_nested_span(self):
        tree = ast.parse(textwrap.dedent("""\
            import re


            def outer(re):
                def inner(s):
                    return s
                return inner
            """))
        assert "re" in dedupe_mod.chain_blocked(tree, 6)

    def test_class_scope_contributes_nothing_but_outer_func_does(self):
        tree = ast.parse(textwrap.dedent("""\
            def outer(helper):
                class Worker:
                    kind = "x"

                    def run(self):
                        return 1
                return Worker
            """))
        blocked = dedupe_mod.chain_blocked(tree, 6)
        assert "helper" in blocked
        assert "kind" not in blocked
        assert "self" in blocked

    def test_top_level_line_blocks_nothing(self):
        tree = ast.parse(textwrap.dedent("""\
            x = 1


            def f():
                return x
            """))
        assert dedupe_mod.chain_blocked(tree, 1) == set()


class TestFilterShadowed:
    def test_drops_only_needed_kinds(self):
        tree = ast.parse(textwrap.dedent("""\
            def f(sum, xs):
                a = len([x for x in xs])
                b = sum([x for x in xs])
                return a, b
            """))
        len_cand = _cand("f", kind="len", lineno=2)
        sum_cand = _cand("f", kind="sum", lineno=3)
        kept, skipped = dedupe_mod.filter_shadowed(
            tree, [len_cand, sum_cand], {"len": {"sum"}})
        assert kept == [sum_cand]
        assert [n for n, _r in skipped] == ["f"]

    def test_module_check_opt_in(self):
        tree = ast.parse(textwrap.dedent("""\
            sum = 5


            def total(xs):
                return len(xs)
            """))
        cand = _cand("total", kind="len", lineno=5)
        kept, _skipped = dedupe_mod.filter_shadowed(
            tree, [cand], {"len": {"sum"}})
        assert kept == [cand]
        kept, skipped = dedupe_mod.filter_shadowed(
            tree, [cand], {"len": {"sum"}}, check_module=True)
        assert kept == []
        assert "this module" in skipped[0][1]

    def test_legitimate_import_is_not_shadowing(self):
        tree = ast.parse(textwrap.dedent("""\
            import asyncio
            import time


            async def poll():
                time.sleep(1)
            """))
        cand = _cand("poll", kind="async_sleep", lineno=6)
        kept, _skipped = dedupe_mod.filter_shadowed(
            tree, [cand], {"async_sleep": {"asyncio", "time"}})
        assert kept == [cand]
