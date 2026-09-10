"""Tests for detectors/bytecode_audit.py (W12: borrowed-load audit)."""

from __future__ import annotations

import dis
import importlib.resources
import importlib.util
import json
import sys
from types import SimpleNamespace

import pytest


def _load_module(name: str, rel_path: str):
    pkg_skills = importlib.resources.files("perf_gate") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bc = _load_module(
    "_test_bytecode_audit", "perf-gate/scripts/detectors/bytecode_audit.py"
)


def _ins(*opnames):
    return [SimpleNamespace(opname=op) for op in opnames]


class TestCounting:
    def test_counts_each_class(self):
        counts = bc.count_instructions(
            _ins("LOAD_FAST", "LOAD_FAST_BORROW", "LOAD_GLOBAL", "LOAD_ATTR",
                 "LOAD_METHOD", "LOAD_CONST", "RETURN_VALUE"),
            frozenset({"LOAD_FAST_BORROW"}),
        )
        assert counts == {"borrowed": 1, "plain": 1, "global": 1, "attr": 2}

    def test_borrow_set_derived_from_map_not_hardcoded(self):
        fake_map = {"LOAD_FAST": 1, "LOAD_FAST_BORROW": 2, "LOAD_FAST_BORROW_X": 3}
        assert bc.borrow_opnames(fake_map) == {"LOAD_FAST_BORROW", "LOAD_FAST_BORROW_X"}
        assert bc.borrow_opnames({}) == frozenset()

    def test_live_map_has_no_borrow_before_314(self):
        if sys.version_info[:2] < (3, 14):
            assert bc.borrow_opnames() == frozenset()


class TestSourceAnalysis:
    def test_pure_local_function_produces_no_hint(self):
        plan = bc.analyze_source("def f(a, b):\n    return a + b\n")
        assert plan.hints == []

    def test_dynamic_lookups_produce_hint_with_counts(self):
        plan = bc.analyze_source(
            "import math\n"
            "def f(xs):\n"
            "    out = []\n"
            "    for x in xs:\n"
            "        out.append(math.pi * x)\n"
            "    return out\n"
        )
        assert len(plan.hints) == 1
        hint = plan.hints[0]
        assert hint.func_name == "f"
        assert hint.global_loads >= 0 and hint.attr_loads >= 1
        assert hint.borrowed == 0  # graceful degradation pre-3.14
        assert hint.plain >= 1

    def test_nested_functions_attributed_separately(self):
        plan = bc.analyze_source(
            "import sys\n"
            "def outer(xs):\n"
            "    print(len(xs))\n"
            "    def inner(ys):\n"
            "        print(sys.argv)\n"
            "        return ys\n"
            "    return inner(xs)\n"
        )
        by_func = {h.func_name: h for h in plan.hints}
        assert set(by_func) == {"outer", "inner"}
        assert by_func["inner"].attr_loads >= 1

    def test_line_shift_keeps_fingerprint(self):
        shifted = "\n\n" + "import math\ndef f(xs):\n    return math.pi * len(xs)\n"
        base = "import math\ndef f(xs):\n    return math.pi * len(xs)\n"
        fp1 = [h.fingerprint for h in bc.analyze_source(base).hints]
        fp2 = [h.fingerprint for h in bc.analyze_source(shifted).hints]
        assert fp1 == fp2 and len(fp1) == 1

    def test_analyze_file_reads_from_disk(self, tmp_path):
        mod = tmp_path / "mod.py"
        mod.write_text("def f(a, b):\n    return a + b\n")
        assert bc.analyze_file(str(mod)).hints == []


class TestCLI:
    def test_clean_file_exits_zero(self, tmp_path, capsys):
        mod = tmp_path / "mod.py"
        mod.write_text("def f(a, b):\n    return a + b\n")
        assert bc.main(["--files", str(mod)]) is None
        assert capsys.readouterr().out == ""

    def test_hints_advisory_by_default_strict_exits_one(self, tmp_path, capsys):
        mod = tmp_path / "mod.py"
        mod.write_text("import math\ndef f(xs):\n    return math.pi * len(xs)\n")
        assert bc.main(["--files", str(mod)]) is None
        assert "[WARN]" in capsys.readouterr().out
        with pytest.raises(SystemExit) as exc_info:
            bc.main(["--files", str(mod), "--strict"])
        assert exc_info.value.code == 1

    def test_unparseable_file_exits_two(self, tmp_path, capsys):
        mod = tmp_path / "mod.py"
        mod.write_text("def f(:\n    pass\n")
        with pytest.raises(SystemExit) as exc_info:
            bc.main(["--files", str(mod)])
        assert exc_info.value.code == 2
        assert "Error" in capsys.readouterr().err

    def test_json_carries_fingerprints(self, tmp_path, capsys):
        mod = tmp_path / "mod.py"
        mod.write_text("import math\ndef f(xs):\n    return math.pi * len(xs)\n")
        bc.main(["--files", str(mod), "--json"])
        rows = json.loads(capsys.readouterr().out)
        assert rows and rows[0]["check"] == "bytecode_loads"
        assert len(rows[0]["fingerprint"]) == 40

    def test_dis_opmap_sanity(self):
        # The live interpreter really does disassemble what we claim to count.
        assert any(i.opname == "LOAD_FAST" for i in dis.get_instructions(lambda x: x))
