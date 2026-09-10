"""Propose tier1 fixes with a self-hosted model, verify or reject each one.

Reads a classified CSV, takes up to ``--max-proposals`` ``tier1_review``
rows, and for each asks a local Ollama server for a unified diff constrained
to the target function. Every proposal must survive three gates before it is
reported VERIFIED: it parses as a diff, it applies to a scratch copy placed
beside the original (same package context), the copy byte-compiles, and the
copy measures faster than the original past the keep margin. Anything else
is REJECTED with a reason. The original file is never written unless
``--apply`` is passed explicitly -- model output carries no equivalence
proof, so a measured gain on synthetic inputs is evidence, not certainty.

No cloud, no tokens: stdlib ``urllib`` against ``$OLLAMA_HOST``
(default ``http://localhost:11434``).

Exit codes: 0 = every row processed with its verdict printed (row-level
failures are data); 1 = nothing proposed and nothing wrong is impossible --
reserved, unused; 2 = harness error (unreadable input, bad schema,
unreachable model server when proposals are needed).
"""

from __future__ import annotations

import argparse
import ast
import csv
import io
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import confirmation as confirmation_mod  # noqa: E402
import provenance as provenance_mod  # noqa: E402
from profilers import run_line_profile  # noqa: E402

DEFAULT_MODEL = "qwen2.5-coder:14b"
DEFAULT_HOST = "http://localhost:11434"


class ProposalError(RuntimeError):
    """A row-level failure: ambiguous source, bad diff, unmeasurable, ..."""


class OllamaUnreachable(RuntimeError):
    """The model server cannot be reached at all (harness error)."""


def ollama_host() -> str:
    return os.environ.get("OLLAMA_HOST", DEFAULT_HOST).rstrip("/")


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OllamaUnreachable(f"cannot reach Ollama at {url}: {exc}") from exc


def check_server(host: str, timeout: float = 10.0) -> None:
    """Preflight: fail fast when no server is there (exit 2, not N row errors)."""
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=timeout) as response:
            response.read(1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OllamaUnreachable(
            f"no Ollama server at {host} -- start it (ollama serve) and pull "
            f"a code model (ollama pull {DEFAULT_MODEL}): {exc}"
        ) from exc


def generate_diff(host: str, model: str, prompt: str, timeout: float,
                  temperature: float = 0.0) -> str:
    """Ask the model for a diff; return the raw response text."""
    payload = {"model": model, "prompt": prompt, "stream": False,
               "options": {"temperature": temperature}}
    body = _post_json(f"{host}/api/generate", payload, timeout)
    text = body.get("response", "")
    if not isinstance(text, str) or not text.strip():
        raise ProposalError("model returned an empty response")
    return text


def extract_function_source(file_path: str, func_name: str) -> str:
    """Exact source of the single top-level ``def`` (ambiguity fails)."""
    with open(file_path, encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=file_path)
    matches = [n for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == func_name]
    if not matches:
        raise ProposalError(f"no top-level 'def {func_name}' in '{file_path}'")
    if len(matches) > 1:
        raise ProposalError(f"{len(matches)} top-level 'def {func_name}' -- ambiguous")
    segment = ast.get_source_segment(source, matches[0])
    if segment is None:  # pragma: no cover - defensive, ast always segments here
        raise ProposalError(f"cannot extract source of '{func_name}'")
    return segment


def build_prompt(row: dict, func_source: str) -> str:
    """Narrow repair prompt: one function, its evidence, diff-only contract."""
    hotspot_line = (row.get("top_hotspot_line") or "").strip() or "?"
    hotspot_src = (row.get("top_hotspot_source") or "").strip() or "n/a"
    big_o = (row.get("empirical_big_o") or "").strip() or "unmeasured"
    return (
        "You are a Python performance fixer. Rewrite the function below to be "
        "faster WITHOUT changing its behavior on any input. Rules:\n"
        "- Keep the function name, signature, and return contract identical.\n"
        "- Small, local change only: same algorithm family unless the hotspot "
        "proves a better one.\n"
        "- No new imports, no new dependencies, no I/O, no threads.\n"
        f"- Hotspot: line {hotspot_line}: `{hotspot_src}`.\n"
        f"- Measured complexity: {big_o}.\n"
        "Respond with ONLY a unified diff (```diff fence optional) that applies "
        "to the file containing this function. No prose.\n\n"
        f"```python\n{func_source}\n```\n"
    )


def extract_diff(text: str) -> str:
    """Pull the unified diff out of model output (fenced or bare)."""
    if "```diff" in text:
        chunk = text.split("```diff", 1)[1].split("```", 1)[0]
    elif "```" in text:
        chunk = text.split("```", 1)[1].split("```", 1)[0]
    else:
        chunk = text
    diff = chunk.strip("\n")
    if not any(line.startswith("@@") for line in diff.splitlines()):
        raise ProposalError("no unified-diff hunks in model response")
    return diff


def apply_unified_diff(original: str, diff: str) -> str:
    """Apply a unified diff to *original*; strict matching, fail closed."""
    lines = original.split("\n")
    had_trailing_nl = original.endswith("\n")
    if had_trailing_nl:
        lines = lines[:-1]
    new_lines = list(lines)
    old_pos = 0  # set per hunk from its header
    in_hunk = False
    for raw in diff.splitlines():
        if raw.startswith("--- ") or raw.startswith("+++ "):
            continue
        if raw.startswith("@@"):
            parts = raw.split()
            if len(parts) < 3 or not parts[1].startswith("-"):
                raise ProposalError(f"malformed hunk header: {raw!r}")
            try:
                old_pos = int(parts[1][1:].split(",")[0]) - 1
            except ValueError:
                raise ProposalError(f"malformed hunk header: {raw!r}") from None
            if old_pos < 0:
                raise ProposalError(f"malformed hunk header: {raw!r}")
            in_hunk = True
            continue
        if not in_hunk or not raw:
            if not raw:
                continue
            raise ProposalError(f"stray line outside hunk: {raw!r}")
        kind, content = raw[0], raw[1:]
        if kind == " ":
            if old_pos >= len(new_lines) or new_lines[old_pos] != content:
                raise ProposalError("context mismatch -- proposal doesn't apply")
            old_pos += 1
        elif kind == "-":
            if old_pos >= len(new_lines) or new_lines[old_pos] != content:
                raise ProposalError("removal mismatch -- proposal doesn't apply")
            del new_lines[old_pos]
        elif kind == "+":
            new_lines.insert(old_pos, content)
            old_pos += 1
        elif raw.startswith("\\ "):
            raise ProposalError("unsupported '\\ No newline' marker -- rejecting")
        else:
            raise ProposalError(f"unsupported diff line: {raw!r}")
    if not in_hunk:
        raise ProposalError("no hunks applied")
    result = "\n".join(new_lines)
    return result + "\n" if had_trailing_nl else result


def _measure(file_path: str, func_name: str, timeout: float | None) -> float:
    try:
        func = run_line_profile.load_function(file_path, func_name)
    except Exception as exc:
        raise ProposalError(f"cannot load '{func_name}': {exc}") from exc
    stats = run_line_profile.compute_line_stats(func, timeout=timeout)
    if stats.get("error"):
        raise ProposalError(f"cannot measure '{func_name}': {stats['error']}")
    return float(sum(h["time_us"] for h in stats["hotspots"]))


def propose_for_row(row: dict, host: str, model: str, timeout: float | None,
                    http_timeout: float, temperature: float,
                    min_improvement: float, apply: bool, index: int) -> list[str]:
    """Full per-row loop; never raises ProposalError (reports it)."""
    title = f"{row.get('file', '?')} :: {row.get('function', '?')}"
    lines = [f"### {title}"]
    file_path, func_name = row.get("file", ""), row.get("function", "")
    stem, ext = os.path.splitext(os.path.basename(file_path))
    scratch = os.path.join(os.path.dirname(file_path) or ".",
                           f"{stem}__tier1prop{index}{ext or '.py'}")
    try:
        try:
            func_source = extract_function_source(file_path, func_name)
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            raise ProposalError(f"cannot read source: {exc}") from exc
        prompt = build_prompt(row, func_source)
        try:
            raw = generate_diff(host, model, prompt, http_timeout, temperature)
        except OllamaUnreachable as exc:
            raise ProposalError(str(exc)) from exc
        diff = extract_diff(raw)
        try:
            with open(file_path, encoding="utf-8") as f:
                original = f.read()
            patched = apply_unified_diff(original, diff)
        except (OSError, UnicodeDecodeError) as exc:
            raise ProposalError(f"cannot stage proposal: {exc}") from exc
        try:
            compile(patched, scratch, "exec")
        except SyntaxError as exc:
            raise ProposalError(f"proposal doesn't compile: {exc}") from exc
        with open(scratch, "w", encoding="utf-8") as f:
            f.write(patched)
        before = _measure(file_path, func_name, timeout)
        after = _measure(scratch, func_name, timeout)
        if confirmation_mod.decide_keep(before, after, min_improvement):
            pct = (before - after) / before * 100 if before else 0.0
            if apply:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(patched)
                lines.append(f"- APPLIED ({before:.1f} -> {after:.1f} us, -{pct:.0f}%)")
            else:
                lines.append(f"- VERIFIED ({before:.1f} -> {after:.1f} us, -{pct:.0f}%) "
                             "-- original untouched, apply manually:")
            lines += ["", "```diff", diff, "```"]
        else:
            lines.append(f"- REJECTED ({before:.1f} -> {after:.1f} us, "
                         "below margin; original untouched)")
    except ProposalError as exc:
        lines.append(f"- SKIPPED: {exc}")
    finally:
        try:
            os.remove(scratch)
        except OSError:
            pass
    lines.append("")
    return lines


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Propose tier1 fixes with a self-hosted model; verify or reject.",
    )
    parser.add_argument("--input", required=True, help="Classified CSV path.")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Ollama model (default: {DEFAULT_MODEL}).")
    parser.add_argument("--max-proposals", type=int, default=3,
                        help="Max tier1 rows to process (default: 3).")
    parser.add_argument("--min-improvement", type=float,
                        default=confirmation_mod.KEEP_MARGIN,
                        help="Required relative gain (default: 0.10).")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="Per-measurement budget in seconds (default: 120).")
    parser.add_argument("--http-timeout", type=float, default=300.0,
                        help="Model HTTP budget in seconds (default: 300).")
    parser.add_argument("--temperature", type=float, default=0.0,
                        help="Model temperature (default: 0.0).")
    parser.add_argument("--apply", action="store_true",
                        help="Write VERIFIED proposals to the original file "
                        "(default: report only -- model output carries no "
                        "equivalence proof).")
    args = parser.parse_args(argv)

    try:
        with open(args.input, newline="", encoding="utf-8") as f:
            raw = f.read()
    except OSError as exc:
        print(f"Error: cannot read input: {exc}", file=sys.stderr)
        sys.exit(2)
    proven, body = provenance_mod.split_preamble(raw)
    try:
        provenance_mod.assert_schema(proven, source=args.input)
    except provenance_mod.ProvenanceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
    reader = csv.DictReader(io.StringIO(body))
    if not reader.fieldnames or "tier" not in reader.fieldnames:
        print("Error: input has no 'tier' column -- not a classified CSV "
              "(run classify_findings.py first)", file=sys.stderr)
        sys.exit(2)
    rows = [r for r in reader if r.get("tier") == "tier1_review"][:args.max_proposals]
    if not rows:
        print("Nothing to propose: no tier1_review rows.")
        return

    host = ollama_host()
    try:
        check_server(host)
    except OllamaUnreachable as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    out = [f"# Tier1 proposals ({len(rows)} row(s), model {args.model})", ""]
    for i, row in enumerate(rows):
        out.extend(propose_for_row(
            row, host, args.model, args.timeout, args.http_timeout,
            args.temperature, args.min_improvement, args.apply, i,
        ))
    print("\n".join(out))


if __name__ == "__main__":
    main()
