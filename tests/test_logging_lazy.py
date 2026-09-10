"""Tests for logging_lazy_analysis.py (detection) and apply_logging_lazy.py.

Loaded via importlib.resources like the other bundled scripts — their parent
directory, ``code-optimizer``, contains a hyphen so it can't be an importable
package.
"""

from __future__ import annotations

import ast
import difflib
import importlib.resources
import importlib.util
import logging
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
    "_test_logging_lazy_analysis",
    "code-optimizer/scripts/detectors/logging_lazy_analysis.py",
)
apply_mod = _load_module(
    "_test_apply_logging_lazy",
    "code-optimizer/scripts/resolvers/apply_logging_lazy.py",
)

analyze_source = analysis.analyze_source
apply_logging_lazy = apply_mod.apply_logging_lazy


class TestDetection:
    def test_fstring_becomes_lazy(self):
        plan = analyze_source(textwrap.dedent("""\
            import logging
            log = logging.getLogger("x")
            def f(n, path):
                log.debug(f"loaded {n} rows from {path}")
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("logging_lazy", "f")]
        assert plan.safe[0].edits[0][4] == \
            "log.debug('loaded %s rows from %s', n, path)"

    def test_format_call_becomes_lazy(self):
        plan = analyze_source(textwrap.dedent("""\
            import logging
            def f(n):
                logging.info("done {}".format(n))
            """))
        assert [(c.kind, c.func_name) for c in plan.safe] == [("logging_lazy", "f")]
        assert plan.safe[0].edits[0][4] == "logging.info('done %s', n)"

    def test_repr_conversion_becomes_percent_r(self):
        plan = analyze_source(textwrap.dedent("""\
            def f(obj, log):
                log.error(f"bad {obj!r}")
            """))
        assert len(plan.safe) == 1
        assert plan.safe[0].edits[0][4] == "log.error('bad %r', obj)"

    def test_percent_in_literal_is_escaped(self):
        plan = analyze_source(textwrap.dedent("""\
            def f(n, log):
                log.info(f"{n}% complete")
            """))
        assert plan.safe[0].edits[0][4] == "log.info('%s%% complete', n)"


class TestNegatives:
    def test_format_spec_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def f(n, log):
                log.debug(f"bad {n:>10}")
            """))
        assert plan.safe == []
        assert any("no % equivalent" in reason for _, reason in plan.skipped)

    def test_ascii_conversion_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def f(s, log):
                log.debug(f"{s!a}")
            """))
        assert plan.safe == []

    def test_indexed_format_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def f(a, b, log):
                log.info("{0} {1}".format(a, b))
            """))
        assert plan.safe == []
        assert any("indexed/named" in reason for _, reason in plan.skipped)

    def test_named_format_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def f(n, log):
                log.info("{n} rows".format(n=n))
            """))
        assert plan.safe == []

    def test_plain_string_untouched(self):
        plan = analyze_source(textwrap.dedent("""\
            def f(log):
                log.debug("nothing to format")
            """))
        assert plan.safe == [] and plan.skipped == []

    def test_extra_logging_args_skipped(self):
        plan = analyze_source(textwrap.dedent("""\
            def f(n, log):
                log.debug(f"{n}", exc_info=True)
            """))
        assert plan.safe == []

    def test_non_log_method_untouched(self):
        plan = analyze_source(textwrap.dedent("""\
            def f(n, out):
                out.write(f"{n}")
            """))
        assert plan.safe == [] and plan.skipped == []


class TestApply:
    SOURCE = textwrap.dedent("""\
        import logging
        log = logging.getLogger("test_lazy")

        def emit(n):
            log.debug(f"loaded {n} rows")
            return n
        """)

    def test_apply_rewrites(self):
        plan = analyze_source(self.SOURCE)
        out = apply_logging_lazy(self.SOURCE, plan)
        assert "log.debug('loaded %s rows', n)" in out
        ast.parse(out)

    def test_same_output_when_emitted(self):
        plan = analyze_source(self.SOURCE)
        out = apply_logging_lazy(self.SOURCE, plan)
        records_before: list[str] = []
        records_after: list[str] = []

        class Handler(logging.Handler):
            def __init__(self, sink: list):
                super().__init__()
                self.sink = sink

            def emit(self, record: logging.LogRecord) -> None:
                self.sink.append(record.getMessage())

        for source, sink in ((self.SOURCE, records_before), (out, records_after)):
            ns: dict = {}
            exec(compile(source, "<test>", "exec"), ns)
            logger = logging.getLogger("test_lazy")
            logger.setLevel(logging.DEBUG)
            handler = Handler(sink)
            logger.addHandler(handler)
            try:
                assert ns["emit"](42) == 42
            finally:
                logger.removeHandler(handler)
        assert records_before == records_after == ["loaded 42 rows"]

    def test_formatting_deferred_when_disabled(self):
        # Lazy logging defers the *formatting*, not argument evaluation
        # (Python always evaluates call args): with the level disabled,
        # the original runs __format__ while the rewrite runs nothing.
        src = textwrap.dedent("""\
            def emit(t, log):
                log.debug(f"loaded {t} rows")
                return t
            """)
        out_src = apply_logging_lazy(src, analyze_source(src))
        calls = {"format": 0, "str": 0}

        class Tracked:
            def __init__(self, value: int):
                self.value = value

            def __format__(self, spec: str) -> str:
                calls["format"] += 1
                return format(self.value, spec)

            def __str__(self) -> str:
                calls["str"] += 1
                return str(self.value)

        logger = logging.getLogger("test_lazy_defer")
        logger.setLevel(logging.WARNING)  # debug disabled
        for source in (src, out_src):
            calls.update({"format": 0, "str": 0})
            ns: dict = {"log": logger}
            exec(compile(source, "<test>", "exec"), ns)
            assert ns["emit"](Tracked(7), logger) is not None
            if source is src:
                assert calls == {"format": 1, "str": 0}
            else:
                assert calls == {"format": 0, "str": 0}

    def test_idempotent(self):
        once = apply_logging_lazy(self.SOURCE, analyze_source(self.SOURCE))
        plan2 = analyze_source(once)
        assert plan2.safe == []
        assert apply_logging_lazy(once, plan2) == once

    def test_collateral_diff_only_intended_lines(self):
        plan = analyze_source(self.SOURCE)
        out = apply_logging_lazy(self.SOURCE, plan)
        diff = list(difflib.unified_diff(
            self.SOURCE.splitlines(), out.splitlines(), lineterm=""))
        changed = [line for line in diff
                   if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
        assert len(changed) == 2
        assert changed[0].startswith("-") and changed[1].startswith("+")

    def test_stale_plan_raises(self):
        plan = analyze_source(self.SOURCE)
        mutated = self.SOURCE.replace("loaded {n} rows", "loaded {n} cols")
        with pytest.raises(apply_mod.PlanMismatchError):
            apply_logging_lazy(mutated, plan)

    def test_empty_plan_returns_source(self):
        assert apply_logging_lazy("x = 1\n", analysis.LoggingLazyPlan()) == "x = 1\n"
