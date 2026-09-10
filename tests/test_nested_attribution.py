"""Nested-function attribution: one candidate, owned by the inner scope.

Every span-edit detector walks all function definitions including nested
ones, so a hit inside a nested function used to be reported twice (outer +
inner) with identical edit spans — and the pair made
``resolvers/span_edit.splice`` refuse the whole file. ``span_dedupe``
collapses each pair onto the innermost scope; these tests pin that wiring
for all ten span tiers.
"""

from __future__ import annotations

import importlib.resources
import importlib.util
import sys


def _load_module(name: str, rel_path: str):
    pkg_skills = importlib.resources.files("perf_gate") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _detector(name: str):
    mod = _load_module(
        f"_test_nested_{name}",
        f"perf-gate/scripts/detectors/{name}_analysis.py",
    )
    return mod.analyze_source


analyze_consumer = _detector("consumer")
analyze_sorted_minmax = _detector("sorted_minmax")
analyze_membership = _detector("membership")
analyze_list_cast = _detector("list_cast")
analyze_logging_lazy = _detector("logging_lazy")
analyze_dict_keys = _detector("dict_keys")
analyze_async_sleep = _detector("async_sleep")
analyze_enumerate = _detector("enumerate")
analyze_rematch_search = _detector("rematch_search")
analyze_try_hoist = _detector("try_hoist")


def _nested(body: str, header: str = "") -> str:
    # *body* lines already carry their 8-space inner-function indent;
    # the template must not add any or multi-line bodies break.
    return header + "def outer():\n    def inner():\n" + body + \
        "    return inner()\n"


def _check_single_inner(analyze, source, kind):
    plan = analyze(source)
    assert [(c.kind, c.func_name) for c in plan.safe] == [(kind, "inner")]


class TestNestedAttribution:
    def test_consumer(self):
        _check_single_inner(
            analyze_consumer,
            _nested('        return sum([x * 2 for x in items])\n'),
            "sum")

    def test_sorted_minmax(self):
        _check_single_inner(
            analyze_sorted_minmax,
            _nested('        return sorted(items)[0]\n'),
            "sorted_minmax")

    def test_membership(self):
        _check_single_inner(
            analyze_membership,
            _nested('        return mode in ["fast", "slow", "auto"]\n'),
            "membership")

    def test_list_cast(self):
        _check_single_inner(
            analyze_list_cast,
            _nested('        for x in list((1, 2, 3)):\n'
                    '            print(x)\n'),
            "list_cast")

    def test_logging_lazy(self):
        _check_single_inner(
            analyze_logging_lazy,
            _nested('        log.debug(f"loaded {n} rows")\n',
                    header="import logging\nlog = logging.getLogger('x')\n"),
            "logging_lazy")

    def test_dict_keys(self):
        _check_single_inner(
            analyze_dict_keys,
            _nested('        for k in d.keys():\n'
                    '            print(k)\n'),
            "dict_keys")

    def test_async_sleep(self):
        source = _nested('        time.sleep(1)\n',
                         header="import asyncio\nimport time\n")
        source = source.replace("    def inner():", "    async def inner():")
        _check_single_inner(analyze_async_sleep, source, "async_sleep")

    def test_enumerate(self):
        _check_single_inner(
            analyze_enumerate,
            _nested('        for i in range(len(xs)):\n'
                    '            total += xs[i]\n'),
            "enumerate")

    def test_rematch_search(self):
        _check_single_inner(
            analyze_rematch_search,
            _nested('        if re.match(".*error:(.*)", log, re.DOTALL):\n'
                    '            return True\n'
                    '        return False\n',
                    header="import re\n"),
            "rematch_search")

    def test_try_hoist(self):
        _check_single_inner(
            analyze_try_hoist,
            _nested('        for chunk in chunks:\n'
                    '            try:\n'
                    '                out.append(int(chunk))\n'
                    '            except ValueError:\n'
                    '                raise\n'),
            "except_hoist")
