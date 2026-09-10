"""Tests for tier1_propose.py (self-hosted model proposer).

A stub HTTP server stands in for Ollama -- no model download, no network.
"""

from __future__ import annotations

import importlib.resources
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


def _load_module(name: str, rel_path: str):
    pkg_skills = importlib.resources.files("perf_gate") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


t1_mod = _load_module("_test_tier1", "perf-gate/scripts/tier1_propose.py")

GOOD_DIFF = """--- a/target.py
+++ b/target.py
@@ -1,5 +1,5 @@
 def slow(xs):
     out = []
     for x in xs:
-        out.append(x)
+        out.extend([x])
     return out
"""

FIXTURE = ("def slow(xs):\n    out = []\n    for x in xs:\n"
           "        out.append(x)\n    return out\n")


class _StubHandler(BaseHTTPRequestHandler):
    response_text = GOOD_DIFF

    def log_message(self, *args):
        pass

    def do_GET(self):
        assert self.path == "/api/tags"
        body = b"[]"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        assert payload.get("stream") is False
        assert payload.get("model")
        body = json.dumps({"response": _StubHandler.response_text}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def ollama_stub(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("OLLAMA_HOST", f"http://127.0.0.1:{server.server_port}")
    yield server
    server.shutdown()


def _classified_csv(tmp_path, tier="tier1_review"):
    cols = ["rank", "severity", "file", "function", "empirical_big_o",
            "complexity_rank", "big_o_error", "total_hits",
            "total_hotspot_time_us", "top_hotspot_pct_time",
            "top_hotspot_line", "top_hotspot_source",
            "line_profile_error", "tier", "tier_detail"]
    import csv
    import io

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols)
    writer.writeheader()
    writer.writerow({
        "rank": "1", "severity": "Low", "file": str(tmp_path / "target.py"),
        "function": "slow", "empirical_big_o": "Linear", "complexity_rank": "2",
        "big_o_error": "", "total_hits": "10", "total_hotspot_time_us": "5.0",
        "top_hotspot_pct_time": "90.0", "top_hotspot_line": "4",
        "top_hotspot_source": "out.append(x)", "line_profile_error": "",
        "tier": tier, "tier_detail": "",
    })
    path = tmp_path / "classified.csv"
    path.write_text(
        "# tool_version=0.1.0\n# schema_version=1\n# python=3.11.0\n"
        "# tier=baseline\n# commit=x\n# created_utc=t\n" + buf.getvalue()
    )
    (tmp_path / "target.py").write_text(FIXTURE)
    return path


class TestPromptAndDiff:
    def test_prompt_carries_evidence_and_contract(self):
        row = {"top_hotspot_line": "4", "top_hotspot_source": "out.append(x)",
               "empirical_big_o": "Linear"}
        prompt = t1_mod.build_prompt(row, "def slow(xs):\n    pass\n")
        assert "def slow" in prompt
        assert "line 4" in prompt
        assert "unified diff" in prompt
        assert "No new imports" in prompt

    def test_extract_diff_fenced_and_bare(self):
        assert t1_mod.extract_diff(f"```diff\n{GOOD_DIFF}\n```").startswith("---")
        assert t1_mod.extract_diff(GOOD_DIFF).startswith("---")
        with pytest.raises(t1_mod.ProposalError):
            t1_mod.extract_diff("just some prose, no hunks here")

    def test_apply_diff_happy_path(self):
        out = t1_mod.apply_unified_diff(FIXTURE, GOOD_DIFF)
        assert "out.extend([x])" in out
        assert "out.append(x)" not in out
        assert out.endswith("\n")

    def test_apply_diff_mismatch_fails_closed(self):
        bad = GOOD_DIFF.replace("out.append(x)", "out.append(y)")
        with pytest.raises(t1_mod.ProposalError, match="mismatch"):
            t1_mod.apply_unified_diff(FIXTURE, bad)

    def test_apply_diff_stray_line_rejected(self):
        with pytest.raises(t1_mod.ProposalError):
            t1_mod.apply_unified_diff(FIXTURE, GOOD_DIFF + "oops\n")

    def test_extract_source_ok_missing_ambiguous(self, tmp_path):
        mod = tmp_path / "m.py"
        mod.write_text("def f():\n    return 1\n")
        assert "def f" in t1_mod.extract_function_source(str(mod), "f")
        with pytest.raises(t1_mod.ProposalError, match="no top-level"):
            t1_mod.extract_function_source(str(mod), "g")
        mod.write_text("def f():\n    return 1\ndef f():\n    return 2\n")
        with pytest.raises(t1_mod.ProposalError, match="ambiguous"):
            t1_mod.extract_function_source(str(mod), "f")


class TestEndToEnd:
    def test_stub_proposal_reports_verdict_and_cleans_up(
            self, tmp_path, capsys, ollama_stub):
        csv_path = _classified_csv(tmp_path)
        t1_mod.main(["--input", str(csv_path), "--max-proposals", "1"])
        out = capsys.readouterr().out
        assert "VERIFIED" in out or "REJECTED" in out
        assert (tmp_path / "target.py").read_text() == FIXTURE  # untouched
        assert list(tmp_path.glob("*__tier1prop*")) == []  # scratch cleaned

    def test_impossible_margin_rejects_identically(
            self, tmp_path, capsys, ollama_stub):
        csv_path = _classified_csv(tmp_path)
        t1_mod.main(["--input", str(csv_path), "--max-proposals", "1",
                     "--min-improvement", "999"])
        assert "REJECTED" in capsys.readouterr().out
        assert (tmp_path / "target.py").read_text() == FIXTURE

    def test_apply_writes_verified_proposal(
            self, tmp_path, capsys, ollama_stub, monkeypatch):
        monkeypatch.setattr(t1_mod.confirmation_mod, "decide_keep",
                            lambda *args: True)
        csv_path = _classified_csv(tmp_path)
        t1_mod.main(["--input", str(csv_path), "--max-proposals", "1",
                     "--apply"])
        out = capsys.readouterr().out
        assert "APPLIED" in out
        assert "out.extend([x])" in (tmp_path / "target.py").read_text()

    def test_garbage_proposal_skips_row(self, tmp_path, capsys, ollama_stub):
        _StubHandler.response_text = "no diff here, just prose"
        try:
            csv_path = _classified_csv(tmp_path)
            t1_mod.main(["--input", str(csv_path), "--max-proposals", "1"])
            assert "SKIPPED" in capsys.readouterr().out
        finally:
            _StubHandler.response_text = GOOD_DIFF


class TestHarnessErrors:
    def test_server_down_exits_2(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:1")
        csv_path = _classified_csv(tmp_path)
        with pytest.raises(SystemExit) as excinfo:
            t1_mod.main(["--input", str(csv_path)])
        assert excinfo.value.code == 2
        assert "Ollama" in capsys.readouterr().err

    def test_no_tier1_rows_exits_0(self, tmp_path, capsys, ollama_stub):
        csv_path = _classified_csv(tmp_path, tier="not_actionable")
        t1_mod.main(["--input", str(csv_path)])
        assert "Nothing to propose" in capsys.readouterr().out

    def test_missing_tier_column_exits_2(self, tmp_path, capsys):
        path = tmp_path / "raw.csv"
        path.write_text(
            "# tool_version=0.1.0\n# schema_version=1\n# python=3.11.0\n"
            "# tier=baseline\n# commit=x\n# created_utc=t\nfile,function\nm.py,f\n")
        with pytest.raises(SystemExit) as excinfo:
            t1_mod.main(["--input", str(path)])
        assert excinfo.value.code == 2
