"""Tests for llm_harness.py (proposal parsing, gates, backends, cache).

Loaded via importlib.resources like the other bundled scripts — their parent
directory, ``code-optimizer``, contains a hyphen so it can't be an importable
package. All model HTTP is faked: no test here touches the network.
"""

from __future__ import annotations

import importlib.resources
import importlib.util
import io
import json
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


harness = _load_module(
    "_test_llm_harness",
    "code-optimizer/scripts/llm_harness.py",
)

GOOD_CODE = textwrap.dedent("""\
    def build_input(n):
        rows = [{"name": "x"} for _ in range(n)]
        return ([rows], {})
    """)


class _FakeResp:
    """Minimal urlopen response: context manager yielding JSON bytes."""

    def __init__(self, payload):
        self._buf = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return self._buf.read(n)


def _fake_urlopen_factory(monkeypatch, handler):
    """Replace urlopen; handler(request) -> payload dict; records calls."""
    import urllib.request

    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        return _FakeResp(handler(request))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def _ollama_payload(code=GOOD_CODE):
    return {"response": f"Here you go:\n```python\n{code}\n```\n"}


class TestPromptAndParse:
    def test_prompt_carries_source_signature_and_errors(self):
        prompt = harness.build_prompt("def f(rows):\n    return 1", "def f(rows)",
                                      "scale rows as list: boom")
        assert "def f(rows):\n    return 1" in prompt
        assert "def f(rows)" in prompt
        assert "boom" in prompt
        assert "build_input" in prompt and "NEEDS-RESOURCE" in prompt

    def test_extract_fenced_code(self):
        kind, code = harness.extract_proposal(_ollama_payload()["response"])
        assert kind == "code"
        assert "def build_input" in code

    def test_extract_triage_verdict(self):
        kind, reason = harness.extract_proposal("NEEDS-RESOURCE: needs a live DB")
        assert (kind, reason) == ("needs-resource", "needs a live DB")

    def test_extract_garbage_raises(self):
        with pytest.raises(harness.LlmProposalError):
            harness.extract_proposal("no idea, good luck")

    def test_fingerprint_stable_and_sensitive(self):
        a = harness.prompt_fingerprint("s", "sig", "e")
        assert a == harness.prompt_fingerprint("s", "sig", "e")
        assert a != harness.prompt_fingerprint("s!", "sig", "e")


class TestGates:
    def test_good_code_validates_and_grows(self):
        fn = harness.validate_build_input(GOOD_CODE, timeout=10)
        args, kwargs = fn(8)
        assert len(args) == 1 and len(args[0]) == 8
        assert all(set(d) == {"name"} for d in args[0])
        assert kwargs == {}

    def test_bind_adapter_calls_target(self):
        fn = harness.validate_build_input(GOOD_CODE, timeout=10)
        seen = {}

        def target(rows):
            seen["n"] = len(rows)
            return seen["n"]

        adapter = harness.bind_adapter(fn, target)
        assert adapter([0] * 16) == 16
        assert seen["n"] == 16

    @pytest.mark.parametrize("code", [
        "import os\ndef build_input(n):\n    return ([], {})",
        "def build_input(n):\n    import sys\n    return ([], {})",
        "def build_input(n):\n    return eval('[]'), {}",
        "def build_input(n):\n    open('x').read()\n    return ([], {})",
        "def build_input(n):\n    return (x.__class__, {})",
        "def build_input(n):\n    global g\n    return ([], {})",
        "def build_input(n):\n    x = 1\n    def inner():\n        nonlocal x\n    return ([], {})",
        "async def build_input(n):\n    return ([], {})",
        "def build_input(n):\n    yield []",
        "def f(n):\n    return ([], {})",
        "def build_input(n, m):\n    return ([], {})",
        "def build_input(n):\n    return ([1, 2], {})",
        "def build_input(n):\n    return 'not-a-tuple'",
        "def build_input(n):\n    return ([], [])",
        "def build_input(n):\n    return (random.sample(range(n), n), {})",
        "def build_input(n):\n    return (os.listdir('.'), {})",
        "def build_input(n):\n    x = '1' +",
    ])
    def test_rejections(self, code):
        with pytest.raises(harness.LlmProposalError):
            harness.validate_build_input(code, timeout=10)

    def test_infinite_loop_times_out(self):
        with pytest.raises(harness.LlmProposalError, match="timed out"):
            harness.validate_build_input(
                "def build_input(n):\n    while True:\n        pass\n    return ([], {})",
                timeout=0.5)

    def test_oversized_snippet_rejected(self):
        big = "def build_input(n):\n" + "".join(
            f"    v{i} = {i}\n" for i in range(2000)) + "    return ([v0] * n, {})\n"
        with pytest.raises(harness.LlmProposalError, match="too large"):
            harness.validate_build_input(big, timeout=10)


class TestBackends:
    def test_ollama_parses_response(self, monkeypatch):
        calls = _fake_urlopen_factory(monkeypatch, lambda req: _ollama_payload())
        text = harness.generate_text("ollama", "tiny", "prompt", timeout=10)
        assert "def build_input" in text
        assert calls[0].full_url.endswith("/api/generate")
        sent = json.loads(calls[0].data.decode("utf-8"))
        assert sent["options"]["temperature"] == 0.0
        assert sent["model"] == "tiny"

    def test_openrouter_parses_choices_and_auths(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        calls = _fake_urlopen_factory(
            monkeypatch,
            lambda req: {"choices": [{"message": {"content": _ollama_payload()["response"]}}]})
        text = harness.generate_text("openrouter", "tiny-model", "prompt", timeout=10)
        assert "def build_input" in text
        assert calls[0].full_url.endswith("/api/v1/chat/completions")
        assert calls[0].headers["Authorization"] == "Bearer test-key"

    def test_openrouter_without_key_is_harness_error(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(harness.LlmBackendUnreachable, match="OPENROUTER_API_KEY"):
            harness.generate_text("openrouter", "m", "prompt", timeout=10)

    def test_unreachable_backend(self, monkeypatch):
        import urllib.error
        import urllib.request

        def boom(request, timeout=None):
            raise urllib.error.URLError("conn refused")

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        with pytest.raises(harness.LlmBackendUnreachable):
            harness.generate_text("ollama", "tiny", "prompt", timeout=10)

    def test_empty_reply_rejected(self, monkeypatch):
        _fake_urlopen_factory(monkeypatch, lambda req: {"response": "   "})
        with pytest.raises(harness.LlmProposalError, match="empty"):
            harness.generate_text("ollama", "tiny", "prompt", timeout=10)

    def test_unknown_backend(self):
        with pytest.raises(harness.LlmProposalError, match="unknown backend"):
            harness.generate_text("watson", "m", "prompt", timeout=10)


class TestOrchestration:
    def _target_file(self, tmp_path):
        mod = tmp_path / "mod.py"
        mod.write_text(textwrap.dedent("""\
            def first_lens(rows):
                return sum(len(r["name"]) for r in rows)
            """))
        return str(mod)

    def test_propose_validates_and_caches(self, monkeypatch, tmp_path):
        calls = _fake_urlopen_factory(monkeypatch, lambda req: _ollama_payload())
        cache = {}
        first = harness.propose_and_validate(
            self._target_file(tmp_path), "first_lens", "all probes failed",
            backend="ollama", model="tiny", timeout=10, cache=cache)
        assert first["status"] == "code" and first["cached"] is False
        assert len(first["build_fn"](8)[0][0]) == 8
        assert first["prompt_sha"] == harness.prompt_fingerprint(
            "def first_lens(rows):\n    return sum(len(r[\"name\"]) for r in rows)",
            "def first_lens(rows)", "all probes failed")
        second = harness.propose_and_validate(
            self._target_file(tmp_path), "first_lens", "all probes failed",
            backend="ollama", model="tiny", timeout=10, cache=cache)
        assert second["cached"] is True
        assert len(calls) == 1  # one HTTP call total across both proposes

    def test_triage_verdict_raises_needs_resource(self, monkeypatch, tmp_path):
        _fake_urlopen_factory(
            monkeypatch, lambda req: {"response": "NEEDS-RESOURCE: needs a live DB"})
        with pytest.raises(harness.LlmNeedsResource, match="live DB"):
            harness.propose_and_validate(
                self._target_file(tmp_path), "first_lens", "boom",
                backend="ollama", model="tiny", timeout=10, cache={})

    def test_rejected_code_raises(self, monkeypatch, tmp_path):
        _fake_urlopen_factory(
            monkeypatch,
            lambda req: {"response": "```python\ndef build_input(n):\n"
                                    "    import os\n    return ([], {})\n```"})
        with pytest.raises(harness.LlmProposalError, match="imports"):
            harness.propose_and_validate(
                self._target_file(tmp_path), "first_lens", "boom",
                backend="ollama", model="tiny", timeout=10, cache={})

    def test_method_target_refused(self, tmp_path):
        mod = tmp_path / "mod.py"
        mod.write_text("class A:\n    def m(self, xs):\n        return len(xs)\n")
        with pytest.raises(harness.LlmProposalError, match="mine_callsites"):
            harness.propose_and_validate(
                str(mod), "m", "boom", backend="ollama", model="tiny",
                timeout=10, cache={})

    def test_cache_file_roundtrip(self, tmp_path):
        path = str(tmp_path / "cache.json")
        assert harness.load_cache(path) == {}
        assert harness.load_cache(None) == {}
        cache = {"k": {"code": GOOD_CODE, "model": "m", "prompt_sha": "s"}}
        harness.save_cache(path, cache)
        assert harness.load_cache(path) == cache
        harness.save_cache(None, cache)  # no-op, must not raise


class TestAdapterPlumbing:
    def test_profile_big_o_accepts_adapter_with_llm_provenance(self):
        big_o_mod = _load_module(
            "_test_llm_big_o", "code-optimizer/scripts/profilers/run_big_o.py")

        def total(xs):
            return sum(xs)

        probe_log = {}
        best, _fitted = big_o_mod.profile_big_o(
            total, min_n=50, max_n=500, n_measures=3, n_timings=2,
            adapter=lambda data: total(list(data)), probe_log=probe_log)
        assert best.startswith(("Linear", "Constant"))
        assert probe_log["provenance"] == "llm-assisted"

    def test_default_path_stays_synthetic(self):
        big_o_mod = _load_module(
            "_test_llm_big_o2", "code-optimizer/scripts/profilers/run_big_o.py")

        def total(xs):
            return sum(xs)

        probe_log = {}
        big_o_mod.profile_big_o(
            total, min_n=50, max_n=500, n_measures=3, n_timings=2,
            probe_log=probe_log)
        assert probe_log["provenance"] == "synthetic"
