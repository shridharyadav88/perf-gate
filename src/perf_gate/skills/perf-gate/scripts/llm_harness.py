"""LLM-assisted Big-O input synthesis — proposes, never decides.

Deterministic synthesis (``profilers/run_big_o.py`` adapters) stays the
default and the only path that runs unprompted. When every deterministic
probe fails, this module may ask a small model for a ``build_input(n)``
snippet — then validates it through mechanical gates (parse, AST safety
screen, execution in a restricted namespace, monotonic-size check). Any
gate failure discards the proposal: model output can only supply measured
inputs, never a verdict. Approved harnesses (``mine_callsites.py`` +
human review, ``__big_o_provenance__ == "harness"``) stay the only
top-trust label; a validated model snippet is labeled ``llm-assisted``.

Backends (stdlib ``urllib`` only, mirroring ``tier1_propose.py``):

* ``ollama`` (default): local server, private and free. Honors
  ``OLLAMA_HOST`` (default ``http://localhost:11434``).
* ``openrouter``: needs ``OPENROUTER_API_KEY`` in the environment. Opt-in
  only — repo source must not leave the machine by default.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import hashlib
import json
import os
import sys
from collections.abc import Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from profilers import timeouts  # noqa: E402

DEFAULT_OLLAMA_MODEL = "qwen2.5-coder:7b"
DEFAULT_OPENROUTER_MODEL = "meta-llama/llama-3.1-8b-instruct"
DEFAULT_HOST = "http://localhost:11434"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_TOKENS = 600
DEFAULT_CACHE = os.path.join(
    os.path.expanduser("~"), ".cache", "perf-gate",
    "llm_harness.json",
)

# Sizes at which a proposal must run and grow. Small on purpose: validation
# proves the recipe works, the real fit (run separately) proves the curve.
_VALIDATE_SIZES = (8, 64)
_MAX_AST_NODES = 800


class LlmProposalError(RuntimeError):
    """A row-level failure: empty reply, unparseable code, rejected gates."""


class LlmBackendUnreachable(RuntimeError):
    """The model backend cannot be reached at all (harness error)."""


class LlmNeedsResource(LlmProposalError):
    """The model reports the target needs a real resource (DB/net/files).

    Not a failure of the model — a triage verdict. Carries the reason so
    callers can annotate the error row instead of retrying blindly.
    """


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

def ollama_host() -> str:
    """Ollama base URL, honoring OLLAMA_HOST like tier1_propose.py."""
    return os.environ.get("OLLAMA_HOST", DEFAULT_HOST).rstrip("/")


def openrouter_key() -> str | None:
    """OPENROUTER_API_KEY or None (remote backend stays opt-in)."""
    return os.environ.get("OPENROUTER_API_KEY") or None


def _post_json(url: str, payload: dict, timeout: float,
               headers: dict | None = None) -> dict:
    import urllib.error
    import urllib.request

    body = json.dumps(payload).encode("utf-8")
    heads = {"Content-Type": "application/json"}
    heads.update(headers or {})
    request = urllib.request.Request(url, data=body, headers=heads, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise LlmBackendUnreachable(
            f"model backend rejected the request (HTTP {exc.code}); "
            "for openrouter, check OPENROUTER_API_KEY"
        ) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LlmBackendUnreachable(f"cannot reach model backend at {url}: {exc}") from exc


def check_backend(backend: str, timeout: float = 10.0) -> None:
    """Preflight: fail fast when the backend is unusable (harness error)."""
    import urllib.error
    import urllib.request

    if backend == "openrouter":
        if not openrouter_key():
            raise LlmBackendUnreachable(
                "openrouter backend needs OPENROUTER_API_KEY in the environment"
            )
        url = "https://openrouter.ai/api/v1/models"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {openrouter_key()}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                response.read(1)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise LlmBackendUnreachable(f"cannot reach openrouter: {exc}") from exc
        return
    if backend != "ollama":
        raise LlmProposalError(f"unknown backend '{backend}' (ollama|openrouter)")
    url = f"{ollama_host()}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            response.read(1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LlmBackendUnreachable(
            f"no Ollama server at {ollama_host()} -- start it (ollama serve) and "
            f"pull a code model (ollama pull {DEFAULT_OLLAMA_MODEL}): {exc}"
        ) from exc


def generate_text(backend: str, model: str, prompt: str, timeout: float,
                  max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
    """Ask the model for text; return the raw response string."""
    if backend == "openrouter":
        key = openrouter_key()
        if not key:
            raise LlmBackendUnreachable(
                "openrouter backend needs OPENROUTER_API_KEY in the environment"
            )
        payload = {"model": model,
                   "messages": [{"role": "user", "content": prompt}],
                   "temperature": 0.0, "max_tokens": max_tokens}
        body = _post_json(OPENROUTER_URL, payload, timeout,
                          headers={"Authorization": f"Bearer {key}"})
        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmProposalError(f"openrouter returned an unexpected shape: {exc}") from exc
    elif backend == "ollama":
        payload = {"model": model or DEFAULT_OLLAMA_MODEL, "prompt": prompt,
                   "stream": False,
                   "options": {"temperature": 0.0, "num_predict": max_tokens}}
        body = _post_json(f"{ollama_host()}/api/generate", payload, timeout)
        text = body.get("response", "")
    else:
        raise LlmProposalError(f"unknown backend '{backend}' (ollama|openrouter)")
    if not isinstance(text, str) or not text.strip():
        raise LlmProposalError("model returned an empty response")
    return text


# ---------------------------------------------------------------------------
# Prompting and parsing
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """\
You write a test-input builder for empirical Big-O profiling of this Python
function. Reply with EXACTLY ONE of the two shapes below — nothing else.

Function source:
```python
{func_source}
```

Signature: {signature_desc}

What the deterministic synthesizer already tried and why it failed:
{probe_errors}

Shape 1 — a builder. Define exactly one function:

```python
def build_input(n):
    ...
    return (args, kwargs)
```

Rules (all mandatory):
- `build_input(n)` takes ONE int `n` and returns a 2-tuple `(args, kwargs)`
  where `args` is a list/tuple of positional arguments and `kwargs` a dict.
  The profiler calls `target(*args, **kwargs)`. Example: for
  `def f(rows, k=1)` return `([[1] * n], {{"k": 1}})` — note the outer list
  holds the ONE positional argument, which itself has length `n`.
- Use ONLY Python builtins (range, len, list, dict, str, comprehensions).
  No imports, no I/O, no threads, no randomness, no exec/eval/open,
  no dunder-attribute access, no global/nonlocal statements.
- Every sized input MUST grow with `n` (e.g. a list of length `n`, a string
  of length `n`). Fixed decoys for the other parameters are fine.
- Keep it under 30 lines. No explanation outside the fenced block.

Shape 2 — triage (only when NO pure-builtin input can work, e.g. the
function needs a live database, network, secret, GPU, or a complex
on-disk fixture). Reply with exactly one line:

NEEDS-RESOURCE: <one-line reason>
"""


def build_prompt(func_source: str, signature_desc: str, probe_errors: str) -> str:
    """Render the synthesis prompt (pure function, easy to assert)."""
    return PROMPT_TEMPLATE.format(
        func_source=func_source.strip(),
        signature_desc=signature_desc.strip(),
        probe_errors=probe_errors.strip(),
    )


def prompt_fingerprint(func_source: str, signature_desc: str, probe_errors: str) -> str:
    """Cache key: what the proposal depends on (never the model name)."""
    blob = "\0".join((func_source.strip(), signature_desc.strip(), probe_errors.strip()))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def extract_proposal(text: str) -> tuple[str, str]:
    """Split a reply into ("code", python) or ("needs-resource", reason)."""
    import re

    match = re.search(r"```python\s*\n(.*?)```", text, re.DOTALL)
    if match and "def build_input" in match.group(1):
        return "code", match.group(1)
    for line in text.splitlines():
        if line.strip().upper().startswith("NEEDS-RESOURCE:"):
            reason = line.split(":", 1)[1].strip() or "unspecified resource"
            return "needs-resource", reason
    raise LlmProposalError("no build_input block or NEEDS-RESOURCE verdict in reply")


# ---------------------------------------------------------------------------
# Mechanical gates (the model proposes; these dispose)
# ---------------------------------------------------------------------------

# Calls that never belong in synthesized input builders.
_FORBIDDEN_CALLS = frozenset({
    "exec", "eval", "open", "compile", "__import__", "input", "exit",
    "quit", "help", "globals", "locals", "vars", "dir", "breakpoint",
    "getattr", "setattr", "delattr", "hasattr", "memoryview",
})

# Builtins available when executing a proposal. Anything else (random,
# sys, pathlib, ...) raises NameError at validation time and rejects the
# proposal — determinism by construction, since no import form can pass
# the screen below.
_SAFE_BUILTINS = frozenset({
    "range", "len", "list", "dict", "tuple", "set", "frozenset", "str",
    "int", "float", "bool", "bytes", "chr", "ord", "abs", "min", "max",
    "sum", "all", "any", "enumerate", "zip", "reversed", "sorted",
    "round", "pow", "divmod", "isinstance",
})


def screen_tree(tree: ast.Module) -> str | None:
    """Return a rejection reason, or None when the snippet may execute.

    Exactly one top-level ``def build_input`` (plus an optional module
    docstring) is allowed; bodies must avoid imports, dunder access,
    suspension points, scope escapes, and the forbidden calls above.
    """
    if sum(1 for _ in ast.walk(tree)) > _MAX_AST_NODES:
        return "snippet too large"
    body = [n for n in tree.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    if len(body) != 1 or not isinstance(body[0], ast.FunctionDef) \
            or body[0].name != "build_input":
        return "must define exactly one top-level 'def build_input'"
    func = body[0]
    if len(func.args.posonlyargs) + len(func.args.args) != 1 \
            or func.args.vararg is not None or func.args.kwarg is not None \
            or func.args.kwonlyargs:
        return "'build_input' must take exactly one argument"
    for node in ast.walk(func):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return "imports are not allowed"
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            return "global/nonlocal statements are not allowed"
        if isinstance(node, (ast.AsyncFunctionDef, ast.Await,
                             ast.Yield, ast.YieldFrom)):
            return "async/yield constructs are not allowed"
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            return "dunder-attribute access is not allowed"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in _FORBIDDEN_CALLS:
            return f"call to '{node.func.id}' is not allowed"
    return None


def _sized_total(args, kwargs) -> int:
    """Combined len() of sized call arguments (0 when nothing is sized)."""
    total = 0
    for value in list(args) + list(kwargs.values()):
        try:
            total += len(value)
        except TypeError:
            continue
    return total


def validate_build_input(code: str, timeout: float | None) -> Callable:
    """Parse, screen, execute, and size-check one proposal.

    Returns the ``build_input`` callable on success; raises
    :exc:`LlmProposalError` with the failing gate otherwise. Execution
    happens in a namespace whose ``__builtins__`` is the safe subset, so
    anything the screen missed still fails closed with NameError.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise LlmProposalError(f"proposal does not parse: {exc}") from exc
    reason = screen_tree(tree)
    if reason is not None:
        raise LlmProposalError(f"proposal rejected: {reason}")
    safe_ns = {"__builtins__": {name: getattr(builtins, name)
                                for name in _SAFE_BUILTINS}}
    try:
        exec(compile(tree, "<llm-harness>", "exec"), safe_ns)
    except Exception as exc:
        raise LlmProposalError(f"proposal raised at definition: {exc!r}") from exc
    build_fn = safe_ns.get("build_input")
    if not callable(build_fn):
        raise LlmProposalError("proposal defines no callable 'build_input'")
    sizes = []
    for n in _VALIDATE_SIZES:
        try:
            out = timeouts.run_with_timeout(lambda: build_fn(n), timeout)
        except timeouts.TimeoutBudgetExceeded as exc:
            raise LlmProposalError(f"proposal timed out at n={n}: {exc}") from exc
        except Exception as exc:
            raise LlmProposalError(f"proposal raised at n={n}: {exc!r}") from exc
        if not (isinstance(out, (tuple, list)) and len(out) == 2
                and isinstance(out[0], (tuple, list))
                and isinstance(out[1], dict)):
            raise LlmProposalError(
                "proposal must return (args, kwargs): a 2-tuple of "
                "list/tuple + dict")
        sizes.append(_sized_total(out[0], out[1]))
    if sizes[-1] <= 0 or any(b <= a for a, b in zip(sizes, sizes[1:])):
        raise LlmProposalError(
            f"built input does not grow with n (sized totals {sizes})")
    return build_fn


def bind_adapter(build_fn, target_func):
    """Adapt a validated builder to big_o's single-list convention."""
    def adapter(data):
        args, kwargs = build_fn(len(data))
        return target_func(*args, **kwargs)
    return adapter


# ---------------------------------------------------------------------------
# Target source extraction
# ---------------------------------------------------------------------------

def extract_function_source(file_path: str, func_name: str) -> tuple[str, str]:
    """Return (func_source, signature_desc) for a module-level function.

    Only module-level ``def`` targets are supported: a method would need a
    constructed instance, which no synthesizer can guess soundly — approve
    a harness via ``mine_callsites.py`` for those instead.
    """
    try:
        with open(file_path, encoding="utf-8") as handle:
            source = handle.read()
    except OSError as exc:
        raise LlmProposalError(f"cannot read '{file_path}': {exc}") from exc
    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError as exc:
        raise LlmProposalError(f"cannot parse '{file_path}': {exc}") from exc
    matches = [n for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == func_name]
    if not matches:
        nested = [n.name for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == func_name]
        if nested:
            raise LlmProposalError(
                f"'{func_name}' is a nested function or method, not a "
                "module-level def; approve a harness via mine_callsites.py")
        raise LlmProposalError(f"no top-level 'def {func_name}' in '{file_path}'")
    if len(matches) > 1:
        raise LlmProposalError(
            f"{len(matches)} top-level 'def {func_name}' -- ambiguous")
    node = matches[0]
    lines = source.splitlines()
    func_source = "\n".join(lines[node.lineno - 1: node.end_lineno])
    params = []
    for arg in list(node.args.posonlyargs) + list(node.args.args):
        params.append(arg.arg + (": ..." if arg.annotation is not None else ""))
    if node.args.vararg is not None:
        params.append("*" + node.args.vararg.arg)
    for arg in node.args.kwonlyargs:
        params.append(arg.arg + "=...")
    if node.args.kwarg is not None:
        params.append("**" + node.args.kwarg.arg)
    kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    signature_desc = f"{kind} {func_name}({', '.join(params)})"
    return func_source, signature_desc


# ---------------------------------------------------------------------------
# Response cache (reruns must not re-spend tokens or flap)
# ---------------------------------------------------------------------------

def load_cache(path: str | None) -> dict:
    """Read the JSON response cache (missing/corrupt file reads as empty)."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cache(path: str | None, cache: dict) -> None:
    """Write the JSON response cache (best effort; failures are silent)."""
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, indent=1, sort_keys=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def default_model(backend: str) -> str:
    """Small instruct model per backend (overridable via --model)."""
    if backend == "openrouter":
        return DEFAULT_OPENROUTER_MODEL
    return DEFAULT_OLLAMA_MODEL


def propose_and_validate(file_path: str, func_name: str, probe_errors: str,
                         backend: str = "ollama", model: str | None = None,
                         timeout: float = DEFAULT_TIMEOUT,
                         max_tokens: int = DEFAULT_MAX_TOKENS,
                         cache: dict | None = None,
                         cache_path: str | None = None) -> dict:
    """Ask the model for input code, validate it, return the outcome.

    Returns ``{"status": "code", "code", "build_fn", "model", "prompt_sha"}``
    with a validated builder, or raises :exc:`LlmNeedsResource` (triage
    verdict, not a failure) or :exc:`LlmProposalError` (any gate failed).
    Backend errors raise :exc:`LlmBackendUnreachable`. *cache* (plus the
    optional *cache_path* file) skips repeat HTTP calls for identical
    prompts — cached code is still re-validated, so a stale entry can only
    cost time, never correctness.
    """
    if cache is None:
        cache = {}
    model = model or default_model(backend)
    func_source, signature_desc = extract_function_source(file_path, func_name)
    prompt = build_prompt(func_source, signature_desc, probe_errors)
    fingerprint = prompt_fingerprint(func_source, signature_desc, probe_errors)
    key = f"{backend}\0{model}\0{fingerprint}"
    entry = cache.get(key)
    if isinstance(entry, dict) and isinstance(entry.get("code"), str):
        build_fn = validate_build_input(entry["code"], timeout)
        return {"status": "code", "code": entry["code"], "build_fn": build_fn,
                "model": entry.get("model", model),
                "prompt_sha": fingerprint, "cached": True}
    text = generate_text(backend, model, prompt, timeout, max_tokens)
    kind, payload = extract_proposal(text)
    if kind == "needs-resource":
        raise LlmNeedsResource(payload)
    build_fn = validate_build_input(payload, timeout)
    cache[key] = {"code": payload, "model": model, "prompt_sha": fingerprint}
    save_cache(cache_path, cache)
    return {"status": "code", "code": payload, "build_fn": build_fn,
            "model": model, "prompt_sha": fingerprint, "cached": False}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    """Propose + validate Big-O input for one function; print the outcome."""
    parser = argparse.ArgumentParser(
        description="Ask a small model for Big-O input code, then validate "
                    "it through mechanical gates (never trusted blindly).",
    )
    parser.add_argument("--file", required=True, help="Path to the Python file.")
    parser.add_argument("--func", required=True, help="Module-level function name.")
    parser.add_argument("--probe-errors", default="",
                        help="Deterministic probe failures, for prompt context.")
    parser.add_argument("--backend", choices=["ollama", "openrouter"], default="ollama",
                        help="Model backend (default: ollama; openrouter needs "
                             "OPENROUTER_API_KEY and is opt-in).")
    parser.add_argument("--model", default=None,
                        help="Model id (defaults per backend; small instruct "
                             "models are sufficient and cheap).")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="Seconds per model call and validation run.")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--cache-file", default=None,
                        help="JSON response cache (default: none; pass a path "
                             "to reuse identical prompts across runs).")
    parser.add_argument("--json", action="store_true",
                        help="Print the machine-readable outcome envelope.")
    args = parser.parse_args(argv)

    # Exit codes: 0 = finished with an outcome (validated code OR a
    # triage/rejection verdict, always printed); 1 = usage/target errors;
    # 2 = the backend itself failed. Mirrors apply_and_verify.py.
    try:
        check_backend(args.backend, timeout=min(10.0, args.timeout))
    except LlmBackendUnreachable as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(2) from exc
    cache = load_cache(args.cache_file)
    try:
        outcome = propose_and_validate(
            args.file, args.func, args.probe_errors, backend=args.backend,
            model=args.model, timeout=args.timeout, max_tokens=args.max_tokens,
            cache=cache, cache_path=args.cache_file,
        )
    except LlmNeedsResource as exc:
        print(f"NEEDS-RESOURCE: {exc}")
        return
    except LlmProposalError as exc:
        print(f"REJECTED: {exc}")
        return
    except LlmBackendUnreachable as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(2) from exc
    if args.json:
        print(json.dumps({"status": "code", "model": outcome["model"],
                          "prompt_sha": outcome["prompt_sha"],
                          "cached": outcome["cached"], "code": outcome["code"]},
                         indent=1))
    else:
        where = "cache" if outcome["cached"] else outcome["model"]
        print(f"VALIDATED (via {where}):\n\n{outcome['code']}")


if __name__ == "__main__":
    main()
