"""Tests for detectors/static_audit.py and its classify_findings.py wiring (W3).

Loaded via importlib by file path like the other bundled scripts -- the
parent directory, ``code-optimizer``, contains a hyphen so it can't be an
importable package.
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


audit = _load_module(
    "_test_static_audit", "code-optimizer/scripts/detectors/static_audit.py"
)
classify_mod = _load_module(
    "_test_sa_classify", "code-optimizer/scripts/classify_findings.py"
)
gen_mod = _load_module(
    "_test_sa_gen", "code-optimizer/scripts/generate_baseline_csv.py"
)

analyze_source = audit.analyze_source


def _hints(source, **kwargs):
    return analyze_source(textwrap.dedent(source), **kwargs).hints


def _kinds(source, **kwargs):
    return sorted(h.kind for h in _hints(source, **kwargs))


class TestNestedLoops:
    def test_nested_for_is_flagged_once(self):
        plan = analyze_source(
            "def f(xs, ys):\n"
            "    for x in xs:\n"
            "        for y in ys:\n"
            "            print(x, y)\n"
        )
        nested = [h for h in plan.hints if h.kind == "nested_loop"]
        assert len(nested) == 1
        assert nested[0].func_name == "f" and nested[0].lineno == 3

    def test_triple_nesting_reports_each_inner_loop_once(self):
        plan = analyze_source(
            "def f(a, b, c):\n"
            "    for x in a:\n"
            "        for y in b:\n"
            "            for z in c:\n"
            "                print(x, y, z)\n"
        )
        nested = [h for h in plan.hints if h.kind == "nested_loop"]
        assert sorted(h.lineno for h in nested) == [3, 4]

    def test_single_loop_is_clean(self):
        assert _kinds("def f(xs):\n    for x in xs:\n        print(x)\n") == []

    def test_while_nesting_counts(self):
        plan = analyze_source(
            "def f(xs, ys):\n"
            "    i = 0\n"
            "    while i < len(xs):\n"
            "        for y in ys:\n"
            "            print(i, y)\n"
            "        i += 1\n"
        )
        assert any(h.kind == "nested_loop" for h in plan.hints)

    def test_async_for_nesting_counts(self):
        plan = analyze_source(
            "async def f(xs, ys):\n"
            "    async for x in xs:\n"
            "        for y in ys:\n"
            "            print(x, y)\n"
        )
        assert any(h.kind == "nested_loop" for h in plan.hints)

    def test_loop_in_nested_def_is_not_outer_nesting(self):
        plan = analyze_source(
            "def f(xs):\n"
            "    for x in xs:\n"
            "        def g(ys):\n"
            "            for y in ys:\n"
            "                print(y)\n"
            "        g([x])\n"
        )
        nested = [h for h in plan.hints if h.kind == "nested_loop"]
        assert nested == []
        # ... but the inner function is still audited under its own name
        inner = analyze_source(
            "def g(ys, zs):\n"
            "    for y in ys:\n"
            "        for z in zs:\n"
            "            print(y, z)\n"
        )
        assert [h.func_name for h in inner.hints] == ["g"]


class TestAttrCache:
    def test_global_attr_read_in_loop_is_flagged(self):
        plan = analyze_source(
            "import math\n"
            "def f(xs):\n"
            "    out = []\n"
            "    for x in xs:\n"
            "        out.append(math.pi * x)\n"
            "    return out\n"
        )
        cached = [h for h in plan.hints if h.kind == "attr_cache"]
        assert len(cached) == 1
        assert cached[0].symbol == "math.pi"
        assert cached[0].lineno == 5

    def test_attr_outside_loop_is_clean(self):
        assert _kinds("import math\ndef f():\n    return math.pi\n") == []

    def test_local_attr_access_is_not_a_candidate(self):
        # Loop-variable / local attribute reads must never flag.
        assert _kinds(
            "def f(items):\n"
            "    for item in items:\n"
            "        print(item.name, item.value)\n"
        ) == []

    def test_store_ctx_goes_to_skipped_not_hints(self):
        plan = analyze_source(
            "import math\n"
            "def f(xs):\n"
            "    for x in xs:\n"
            "        math.pi = x\n"
        )
        assert [h.kind for h in plan.hints] == []
        assert len(plan.skipped) == 1
        assert "not read" in plan.skipped[0].reason

    def test_roots_are_configurable(self):
        source = (
            "def f(xs):\n"
            "    for x in xs:\n"
            "        print(pd.DataFrame(x))\n"
        )
        assert _kinds(source) == []
        assert _kinds(source, cached_attr_roots={"pd"}) == ["attr_cache"]

    def test_while_loop_attr_counts(self):
        plan = analyze_source(
            "import sys\n"
            "def f(xs):\n"
            "    i = 0\n"
            "    while i < len(xs):\n"
            "        print(sys.argv)\n"
            "        i += 1\n"
        )
        assert any(h.kind == "attr_cache" for h in plan.hints)


class TestEntryPoints:
    def test_analyze_tree_shares_one_parse(self):
        source = "def f(xs):\n    for x in xs:\n        print(x)\n"
        tree = ast.parse(source)
        plan = audit.analyze_tree(tree)
        assert plan.hints == []
        nested = audit.analyze_tree(ast.parse(
            "def f(a, b):\n    for x in a:\n        for y in b:\n            print(x, y)\n"
        ))
        assert [h.kind for h in nested.hints] == ["nested_loop"]

    def test_analyze_file_reads_from_disk(self, tmp_path):
        mod = tmp_path / "mod.py"
        mod.write_text(
            "def f(a, b):\n    for x in a:\n        for y in b:\n            print(x, y)\n"
        )
        plan = audit.analyze_file(str(mod))
        assert [h.kind for h in plan.hints] == ["nested_loop"]

    def test_hints_sorted_deterministically(self):
        plan = analyze_source(
            "def b(a, c):\n"
            "    for x in a:\n"
            "        for y in c:\n"
            "            print(x, y)\n"
            "def a(m, n):\n"
            "    for x in m:\n"
            "        for y in n:\n"
            "            print(x, y)\n"
        )
        assert [(h.func_name, h.lineno) for h in plan.hints] == [("a", 7), ("b", 3)]


def _row(**overrides):
    base = {
        "rank": "", "severity": "", "file": "", "function": "f",
        "empirical_big_o": "Linear", "complexity_rank": "2",
        "big_o_error": "", "total_hits": "", "total_hotspot_time_us": "",
        "top_hotspot_pct_time": "", "top_hotspot_line": "",
        "top_hotspot_source": "", "line_profile_error": "",
        "tier": "", "tier_detail": "",
    }
    base.update(overrides)
    return base


class TestClassifierWiring:
    def test_tier1_row_gains_static_summary(self, tmp_path):
        # NOTE: a nested *append* loop now routes to tier0_perf401 by design
        # (nested-comprehension support), so this fixture uses a shape no
        # tier0 matches: function-local import (no hoist) + subscript store
        # on a list (no comprehension tier).
        mod = tmp_path / "mod.py"
        mod.write_text(
            "def f(a, b):\n"
            "    import math\n"
            "    out = []\n"
            "    for x in a:\n"
            "        for y in b:\n"
            "            out.append(y)\n"
            "            out[x] = math.pi\n"
            "    return out\n"
        )
        row = _row(file=str(mod), function="f")
        classify_mod.classify_rows([row])
        assert row["tier"] == "tier1_review"
        assert "static audit" in row["tier_detail"]
        assert "nested loop" in row["tier_detail"]
        assert "math.pi" in row["tier_detail"]

    def test_tier1_row_without_hints_unchanged(self, tmp_path):
        mod = tmp_path / "mod.py"
        mod.write_text("def f(xs):\n    return sorted(xs)\n")
        row = _row(file=str(mod), function="f")
        classify_mod.classify_rows([row])
        assert row["tier"] == "tier1_review"
        assert "static audit" not in row["tier_detail"]

    def test_tier2_rows_untouched_by_static_hints(self, tmp_path):
        mod = tmp_path / "mod.py"
        mod.write_text(
            "def f(a, b):\n    for x in a:\n        for y in b:\n            print(x, y)\n"
        )
        row = _row(
            file=str(mod), function="f",
            empirical_big_o="Quadratic", complexity_rank="4",
            confirmations="2",
        )
        classify_mod.classify_rows([row])
        assert row["tier"] == "tier2_algorithmic"
        assert "static audit" not in row["tier_detail"]

    def test_static_hints_never_escalate_alone(self, tmp_path):
        # Nested loops with failed profiling must NOT become tier2 (no
        # execution evidence); both-errors + static pattern is tier1.
        mod = tmp_path / "mod.py"
        mod.write_text(
            "def f(a, b):\n    for x in a:\n        for y in b:\n            print(x, y)\n"
        )
        row = _row(
            file=str(mod), function="f", empirical_big_o="", complexity_rank="",
            big_o_error="boom", line_profile_error="",
        )
        classify_mod.classify_rows([row])
        assert row["tier"] == "tier1_review"

    def test_csv_fieldnames_frozen(self):
        assert gen_mod.FIELDNAMES == [
            "rank", "severity", "file", "function", "empirical_big_o",
            "complexity_rank", "big_o_error", "total_hits",
            "total_hotspot_time_us", "top_hotspot_pct_time",
            "top_hotspot_line", "top_hotspot_source", "line_profile_error",
        ]
        assert classify_mod.OUTPUT_FIELDNAMES_SUFFIX == ["tier", "tier_detail"]
