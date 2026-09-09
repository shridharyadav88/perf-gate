"""Ruff PERF ingest -- detection-only lane for rules we don't reimplement.

Our AST detectors cover what Ruff can't prove safe to fix (re.compile
hoisting); Ruff's Perflint rules cover single-expression rewrites with
upstream auto-fixes. This script runs ``ruff check --select PERF`` on target
files and normalizes the JSON hits into plan dataclasses mirroring
``static_audit.py``. Overlap policy: PERF401/PERF402 hits at locations our
own detectors already cover are deduped in OUR favor (we own the applier
plus the measurement); ruff-unique rules (101/102/203) are reported as
their own lane -- apply with ``ruff --fix``, then re-measure with our
profilers. Detection only: this script never writes target files.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from detectors import (  # noqa: E402
    perf_comprehension_analysis as perf_analysis,
)
from detectors import (
    regex_hoist_analysis as hoist_analysis,
)

# Ruff codes whose ground our own detectors already cover (dedup candidates).
OVERLAPPING_CODES = ("PERF401", "PERF402", "PERF403")


class RuffNotAvailableError(RuntimeError):
    """The ruff binary is missing or failed -- detection cannot run."""


@dataclass
class RuffFinding:
    """One normalized ``ruff check --select PERF`` hit (unfixed, unapplied)."""

    file: str
    line: int
    col: int
    end_line: int
    code: str
    message: str
    fix_available: bool = False


def resolve_command(ruff_bin: str = "ruff") -> list[str]:
    """Resolve how to invoke ruff: binary on PATH, else the current env's module."""
    if ruff_bin != "ruff" or shutil.which(ruff_bin):
        return [ruff_bin]
    try:
        if importlib.util.find_spec("ruff") is not None:
            return [sys.executable, "-m", "ruff"]
    except (ImportError, AttributeError, ValueError):
        pass
    return [ruff_bin]  # let the run fail with the actionable not-found error


def run_ruff(files: list[str], ruff_bin: str = "ruff") -> list[dict]:
    """Run ruff on *files*; return raw JSON records (fail-open on findings).

    Exit 0 (clean) and 1 (findings) both yield records; anything else --
    missing binary, crashed linter -- raises :exc:`RuffNotAvailableError`
    with an actionable message. ``--no-cache`` keeps residue out of the
    target tree.
    """
    cmd = [*resolve_command(ruff_bin), "check", "--select", "PERF",
           "--output-format", "json", "--no-cache", "--", *files]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        raise RuffNotAvailableError(
            f"ruff binary '{ruff_bin}' not found -- install ruff "
            "(pip install ruff) or pass --ruff-bin"
        ) from None
    except subprocess.TimeoutExpired:
        raise RuffNotAvailableError("ruff timed out after 120s") from None
    if proc.returncode not in (0, 1):
        raise RuffNotAvailableError(
            f"ruff failed (exit {proc.returncode}): {proc.stderr.strip()[:200]}"
        )
    try:
        records = json.loads(proc.stdout or "[]")
    except ValueError:
        raise RuffNotAvailableError("ruff emitted unparseable JSON") from None
    return records if isinstance(records, list) else []


def normalize(records: list[dict]) -> list[RuffFinding]:
    """Normalize raw ruff JSON records; unknown shapes are skipped, not fatal."""
    findings = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        loc = rec.get("location") or {}
        end = rec.get("end_location") or {}
        try:
            line = int(loc.get("row", 0))
        except (TypeError, ValueError):
            continue
        if line <= 0:
            continue
        findings.append(RuffFinding(
            file=str(rec.get("filename", "")),
            line=line,
            col=int(loc.get("column", 0) or 0),
            end_line=int(end.get("row", 0) or 0) or line,
            code=str(rec.get("code", "")),
            message=str(rec.get("message", "")),
            fix_available=bool((rec.get("fix") or {}).get("applicability")),
        ))
    return findings


def our_ranges_for_file(path: str) -> list[tuple[int, int]]:
    """Line ranges our own detectors already cover in *path* (fail-open)."""
    try:
        with open(path, encoding="utf-8") as f:
            source = f.read()
    except (OSError, UnicodeDecodeError):
        return []
    try:
        hoist_plan = hoist_analysis.analyze_source(source, filename=path)
        perf_plan = perf_analysis.analyze_source(source, filename=path)
    except (SyntaxError, ValueError):
        return []
    ranges = []
    for cand in hoist_plan.safe:
        ranges.extend((start, end) for _, start, end in cand.occurrences)
    for cand in perf_plan.safe:
        ranges.append((cand.lineno, cand.end_lineno))
    return ranges


def dedup(findings: list[RuffFinding],
          ranges_by_file: dict[str, list[tuple[int, int]]]
          ) -> tuple[list[RuffFinding], int]:
    """Drop overlapping 401/402/403 hits our detectors cover; return (kept, n_dropped)."""
    kept = []
    dropped = 0
    for finding in findings:
        if finding.code in OVERLAPPING_CODES:
            ranges = ranges_by_file.get(finding.file, [])
            if any(start <= finding.line <= end for start, end in ranges):
                dropped += 1
                continue
        kept.append(finding)
    return kept, dropped


def collect(files: list[str], ruff_bin: str = "ruff"
            ) -> tuple[list[RuffFinding], int, list[str]]:
    """Full lane: ruff -> normalize -> dedup. Returns (kept, n_dropped, files)."""
    findings = normalize(run_ruff(files, ruff_bin))
    ranges = {path: our_ranges_for_file(path) for path in files}
    kept, dropped = dedup(findings, ranges)
    return kept, dropped, files


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Ingest Ruff PERF findings (detection only, never writes).",
    )
    parser.add_argument(
        "--files", nargs="+", required=True, help="Python files to check.",
    )
    parser.add_argument(
        "--ruff-bin", default="ruff", help="Ruff binary (default: ruff).",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="Exit 1 when any kept finding exists (default: advisory, exit 0).",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit kept findings as raw-shaped JSON.",
    )
    args = parser.parse_args(argv)

    try:
        kept, dropped, _ = collect(args.files, args.ruff_bin)
    except RuffNotAvailableError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    if args.json:
        print(json.dumps([
            {"cell": None, "code": f.code, "filename": f.file,
             "location": {"column": f.col, "row": f.line},
             "end_location": {"column": 0, "row": f.end_line},
             "message": f.message,
             "fix": {"applicability": "safe"} if f.fix_available else None}
            for f in kept
        ], indent=2))
    else:
        if not kept and not dropped:
            print("No Ruff PERF findings.")
        for finding in kept:
            fix = " [auto-fix available]" if finding.fix_available else ""
            print(f"{finding.file}:{finding.line}:{finding.col} "
                  f"[{finding.code}] {finding.message}{fix}")
        if dropped:
            print(f"({dropped} overlapping PERF401/402/403 hit(s) already "
                  "covered by our own detectors -- see tier0 rows)")
    if args.strict and kept:
        sys.exit(1)


if __name__ == "__main__":
    main()
