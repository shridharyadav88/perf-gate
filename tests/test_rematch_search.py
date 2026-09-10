"""Tests for rematch_search_analysis.py (detection) and apply_rematch_search.py.

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
    "_test_rematch_search_analysis",
    "perf-gate/scripts/detectors/rematch_search_analysis.py",
)
apply_mod = _load_module(
    "_test_apply_rematch_search",
    "perf-gate/scripts/resolvers/apply_rematch_search.py",
)

analyze_source = analysis.analyze_source
apply_rematch_search = apply_mod.apply_rematch_search


def _exec_and_get(source: str, func_name: str):
    ns: dict = {}
    exec(compile(source, "<test>", "exec"), ns)
    return ns[func_name]


class TestDetection:
    HEADER = "import re\n"

    def test_dotall_flag_fires(self):
        plan = analyze_source(self.HEADER + textwrap.dedent("""\
            def has_error(log):
                if re.match(".*error:(.*)", log, re.DOTALL):
                    return True
                return False
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("rematch_search", "has_error")]
        edit = plan.safe[0].edits[0][4]
        assert edit.startswith("re.search(") and "re.DOTALL" in edit
        assert ast.literal_eval(edit.split(",", 1)[0].split("(", 1)[1]) == "error:(.*)"

    def test_combined_flags_fire(self):
        plan = analyze_source(self.HEADER + textwrap.dedent("""\
            def has_error(log):
                return bool(re.match(".*error", log, re.IGNORECASE | re.DOTALL))
            """))
        assert len(plan.safe) == 1

    def test_inline_dotall_fires(self):
        plan = analyze_source(self.HEADER + textwrap.dedent("""\
            def has_error(log):
                return bool(re.match("(?s).*error", log))
            """))
        assert len(plan.safe) == 1
        assert plan.safe[0].edits[0][4].startswith('re.search(\'error\'')

    def test_lazy_star_accepted(self):
        plan = analyze_source(self.HEADER + textwrap.dedent("""\
            def has_error(log):
                if re.match(".*?error", log, flags=re.DOTALL):
                    return True
                return False
            """))
        assert len(plan.safe) == 1


class TestNegatives:
    HEADER = "import re\n"

    def test_no_dotall_skipped(self):
        plan = analyze_source(self.HEADER + textwrap.dedent("""\
            def has_error(log):
                if re.match(".*error", log):
                    return True
                return False
            """))
        assert plan.safe == []
        assert any("DOTALL" in reason for _, reason in plan.skipped)

    def test_plus_prefix_skipped_silently(self):
        plan = analyze_source(self.HEADER + textwrap.dedent("""\
            def has_error(log):
                if re.match(".+error", log, re.DOTALL):
                    return True
                return False
            """))
        assert plan.safe == []

    def test_match_object_escapes_via_return(self):
        plan = analyze_source(self.HEADER + textwrap.dedent("""\
            def find_error(log):
                return re.match(".*error", log, re.DOTALL)
            """))
        assert plan.safe == []
        assert any("boolean context" in reason for _, reason in plan.skipped)

    def test_match_object_escapes_via_assign(self):
        plan = analyze_source(self.HEADER + textwrap.dedent("""\
            def find_error(log):
                m = re.match(".*error", log, re.DOTALL)
                return m
            """))
        assert plan.safe == []

    def test_key_argument_skipped(self):
        plan = analyze_source(self.HEADER + textwrap.dedent("""\
            def has_error(log):
                if re.match(".*error", log, re.DOTALL, count=1):
                    return True
                return False
            """))
        # count= is not in {pattern, string, flags}
        assert plan.safe == []

    def test_aliased_module_untouched(self):
        plan = analyze_source("import re as regex\n" + textwrap.dedent("""\
            def has_error(log):
                if regex.match(".*error", log):
                    return True
                return False
            """))
        assert plan.safe == [] and plan.skipped == []

    def test_rebound_re_skipped(self):
        plan = analyze_source(self.HEADER + textwrap.dedent("""\
            def has_error(log):
                if re.match(".*error", log, re.DOTALL):
                    return True
                return False

            re = None
            """))
        assert plan.safe == []
        assert any("rebound" in reason for _, reason in plan.skipped)

    def test_dynamic_pattern_untouched(self):
        plan = analyze_source(self.HEADER + textwrap.dedent("""\
            def has_error(log, pat):
                if re.match(".*" + pat, log, re.DOTALL):
                    return True
                return False
            """))
        assert plan.safe == [] and plan.skipped == []


class TestApply:
    SOURCE = ("import re\n" + textwrap.dedent("""\
        def has_error(log):
            if re.match(".*error:(.*)", log, re.DOTALL):
                return True
            return False
        """))

    def test_apply_rewrites(self):
        plan = analyze_source(self.SOURCE)
        out = apply_rematch_search(self.SOURCE, plan)
        assert "re.search(" in out and "re.match(" not in out
        ast.parse(out)

    def test_truth_values_identical(self):
        plan = analyze_source(self.SOURCE)
        out = apply_rematch_search(self.SOURCE, plan)
        cases = ["all fine", "an error: boom", "line1\nerror: x\nline3", ""]
        for case in cases:
            assert _exec_and_get(self.SOURCE, "has_error")(case) == \
                _exec_and_get(out, "has_error")(case)
        # Cross-check against the real re module: both must agree with
        # search on multi-line input (the DOTALL case this tier exists for).
        assert _exec_and_get(out, "has_error")("line1\nerror: x") is True

    def test_idempotent(self):
        once = apply_rematch_search(self.SOURCE, analyze_source(self.SOURCE))
        plan2 = analyze_source(once)
        assert plan2.safe == []
        assert apply_rematch_search(once, plan2) == once

    def test_collateral_diff_only_intended_lines(self):
        plan = analyze_source(self.SOURCE)
        out = apply_rematch_search(self.SOURCE, plan)
        diff = list(difflib.unified_diff(
            self.SOURCE.splitlines(), out.splitlines(), lineterm=""))
        changed = [line for line in diff
                   if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
        assert len(changed) == 2
        assert changed[0].startswith("-") and changed[1].startswith("+")

    def test_stale_plan_raises(self):
        plan = analyze_source(self.SOURCE)
        mutated = self.SOURCE.replace(".*error:(.*)", ".*warning:(.*)")
        with pytest.raises(apply_mod.PlanMismatchError):
            apply_rematch_search(mutated, plan)

    def test_empty_plan_returns_source(self):
        assert apply_rematch_search("x = 1\n", analysis.RematchSearchPlan()) == "x = 1\n"


class TestScopeShadowing:
    HEADER = "import re\n"

    def test_closure_re_param_skipped(self):
        plan = analyze_source(self.HEADER + textwrap.dedent("""\
            def outer(re):
                def has_error(log):
                    if re.match(".*error:(.*)", log, re.DOTALL):
                        return True
                    return False
                return has_error
            """))
        assert plan.safe == []
        assert any("function scope" in reason for _, reason in plan.skipped)
