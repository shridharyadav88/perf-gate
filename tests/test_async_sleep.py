"""Tests for async_sleep_analysis.py (detection) and apply_async_sleep.py.

Loaded via importlib.resources like the other bundled scripts — their parent
directory, ``perf-gate``, contains a hyphen so it can't be an importable
package.
"""

from __future__ import annotations

import ast
import asyncio
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
    "_test_async_sleep_analysis",
    "perf-gate/scripts/detectors/async_sleep_analysis.py",
)
apply_mod = _load_module(
    "_test_apply_async_sleep",
    "perf-gate/scripts/resolvers/apply_async_sleep.py",
)

analyze_source = analysis.analyze_source
apply_async_sleep = apply_mod.apply_async_sleep


class TestDetection:
    HEADER = "import asyncio\nimport time\n"

    def test_sleep_in_async_fires(self):
        plan = analyze_source(self.HEADER + textwrap.dedent("""\
            async def poll():
                time.sleep(1)
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("async_sleep", "poll")]
        assert plan.safe[0].edits[0][4] == "await asyncio.sleep(1)"

    def test_aliased_asyncio_reused(self):
        plan = analyze_source("import asyncio as aio\nimport time\n" + textwrap.dedent("""\
            async def poll():
                time.sleep(1)
            """))
        assert len(plan.safe) == 1
        assert plan.safe[0].edits[0][4] == "await aio.sleep(1)"

    def test_arguments_preserved(self):
        plan = analyze_source(self.HEADER + textwrap.dedent("""\
            async def poll(delay):
                time.sleep(delay + 1)
            """))
        assert plan.safe[0].edits[0][4] == "await asyncio.sleep(delay + 1)"


class TestNegatives:
    HEADER = "import asyncio\nimport time\n"

    def test_sync_function_skipped(self):
        plan = analyze_source(self.HEADER + textwrap.dedent("""\
            def poll():
                time.sleep(1)
            """))
        assert plan.safe == []
        assert any("synchronous" in reason for _, reason in plan.skipped)

    def test_nested_sync_def_skipped(self):
        plan = analyze_source(self.HEADER + textwrap.dedent("""\
            async def outer():
                def inner():
                    time.sleep(1)
                inner()
            """))
        assert plan.safe == []
        assert any("synchronous" in reason for _, reason in plan.skipped)

    def test_missing_asyncio_import_skipped(self):
        plan = analyze_source("import time\n" + textwrap.dedent("""\
            async def poll():
                time.sleep(1)
            """))
        assert plan.safe == []
        assert any("not imported" in reason for _, reason in plan.skipped)

    def test_parameter_shadowing_skipped(self):
        plan = analyze_source(self.HEADER + textwrap.dedent("""\
            async def poll(time):
                time.sleep(1)
            """))
        assert plan.safe == []
        assert any("rebound" in reason for _, reason in plan.skipped)

    def test_default_value_skipped(self):
        plan = analyze_source(self.HEADER + textwrap.dedent("""\
            async def poll(delay=time.sleep(0)):
                pass
            """))
        assert plan.safe == []
        assert any("decorator/default" in reason for _, reason in plan.skipped)


class TestApply:
    SOURCE = ("import asyncio\nimport time\n" + textwrap.dedent("""\
        async def poll():
            time.sleep(0)
            return "done"
        """))

    def test_apply_rewrites(self):
        plan = analyze_source(self.SOURCE)
        out = apply_async_sleep(self.SOURCE, plan)
        assert "await asyncio.sleep(0)" in out and "time.sleep" not in out
        ast.parse(out)

    def test_rewritten_coroutine_runs(self):
        plan = analyze_source(self.SOURCE)
        out = apply_async_sleep(self.SOURCE, plan)
        ns: dict = {}
        exec(compile(out, "<test>", "exec"), ns)
        assert asyncio.run(ns["poll"]()) == "done"

    def test_idempotent(self):
        once = apply_async_sleep(self.SOURCE, analyze_source(self.SOURCE))
        plan2 = analyze_source(once)
        assert plan2.safe == []
        assert apply_async_sleep(once, plan2) == once

    def test_collateral_diff_only_intended_lines(self):
        plan = analyze_source(self.SOURCE)
        out = apply_async_sleep(self.SOURCE, plan)
        diff = list(difflib.unified_diff(
            self.SOURCE.splitlines(), out.splitlines(), lineterm=""))
        changed = [line for line in diff
                   if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
        assert len(changed) == 2
        assert changed[0].startswith("-") and changed[1].startswith("+")

    def test_stale_plan_raises(self):
        plan = analyze_source(self.SOURCE)
        mutated = self.SOURCE.replace("time.sleep(0)", "time.sleep(5)")
        with pytest.raises(apply_mod.PlanMismatchError):
            apply_async_sleep(mutated, plan)

    def test_empty_plan_returns_source(self):
        assert apply_async_sleep("x = 1\n", analysis.AsyncSleepPlan()) == "x = 1\n"


class TestScopeShadowing:
    HEADER = "import asyncio\nimport time\n"

    def test_closure_asyncio_param_skipped(self):
        plan = analyze_source(self.HEADER + textwrap.dedent("""\
            def outer(asyncio):
                async def inner():
                    time.sleep(1)
                return inner()
            """))
        assert plan.safe == []
        assert any("function scope" in reason for _, reason in plan.skipped)
