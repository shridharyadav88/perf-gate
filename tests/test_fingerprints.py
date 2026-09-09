"""Tests for tier_gate fingerprints (W8: stable finding identity)."""

from __future__ import annotations

import importlib.resources
import importlib.util
import sys


def _load_module(name: str, rel_path: str):
    pkg_skills = importlib.resources.files("project_code_optimization") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module("_test_fp_gate", "code-optimizer/scripts/tier_gate.py")


class TestFingerprint:
    def test_is_sha1_hex(self):
        fp = gate.fingerprint("nested_loop", "f", "nested loop at line 12")
        assert len(fp) == 40 and all(c in "0123456789abcdef" for c in fp)

    def test_line_shifted_duplicates_match(self):
        a = gate.fingerprint("nested_loop", "f", "nested loop at line 12 (O(N*M) risk)")
        b = gate.fingerprint("nested_loop", "f", "nested loop at line 45 (O(N*M) risk)")
        assert a == b

    def test_different_check_differs(self):
        a = gate.fingerprint("nested_loop", "f", "nested loop at line 12")
        b = gate.fingerprint("attr_cache", "f", "nested loop at line 12")
        assert a != b

    def test_different_function_differs(self):
        a = gate.fingerprint("nested_loop", "f", "nested loop at line 12")
        b = gate.fingerprint("nested_loop", "g", "nested loop at line 12")
        assert a != b

    def test_genuinely_different_detail_differs(self):
        a = gate.fingerprint("attr_cache", "f", "cache math.pi to a local (line 5)")
        b = gate.fingerprint("attr_cache", "f", "cache sys.argv to a local (line 5)")
        assert a != b

    def test_whitespace_and_addresses_normalized(self):
        a = gate.fingerprint("c", "f", "object  at  0x7f8a1c00  failed")
        b = gate.fingerprint("c", "f", "object at 0xdeadBEEF failed")
        assert a == b


class TestFindingFingerprint:
    def test_property_matches_helper(self):
        finding = gate.Finding(
            tier="baseline", check="nested_loop", status="warn",
            file="mod.py", line=12, detail="nested loop at line 12",
        )
        assert finding.fingerprint == gate.fingerprint(
            "nested_loop", "mod.py", "nested loop at line 12"
        )

    def test_line_shift_keeps_identity(self):
        before = gate.Finding(
            tier="baseline", check="nested_loop", status="warn",
            file="mod.py", line=12, detail="nested loop at line 12",
        )
        after = gate.Finding(
            tier="baseline", check="nested_loop", status="warn",
            file="mod.py", line=30, detail="nested loop at line 30",
        )
        assert before.fingerprint == after.fingerprint

    def test_row_carries_fingerprint_alongside(self):
        finding = gate.Finding(
            tier="baseline", check="c", status="pass", detail="d",
        )
        row = finding.to_csv_row()
        assert row["fingerprint"] == finding.fingerprint
        assert row["detail"] == "d"  # human fields untouched
