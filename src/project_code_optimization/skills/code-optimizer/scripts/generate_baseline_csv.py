"""Generate a baseline assessment report as CSV — no LLM step required.

`run_big_o.py` and `run_line_profile.py` print human-readable text that an
agent then has to read and transcribe into `baseline_report_template.md`.
This script instead calls the same underlying profiling functions directly
and writes one CSV row per (file, function) target, pre-sorted most-to-least
severe using the same `severity.py` ranking the scripts already use. Nothing
here is written by an LLM — the numbers are the report.

Usage mirrors run_big_o.py / run_line_profile.py's multi-target flags:

    python3 generate_baseline_csv.py --target-type file --file <path>
    python3 generate_baseline_csv.py --target-type repo --repo . --output report.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import provenance as provenance_mod  # noqa: E402
from profilers import (
    run_big_o,  # noqa: E402
    run_line_profile,  # noqa: E402
    severity,  # noqa: E402
    target_resolution,  # noqa: E402
)

FIELDNAMES = [
    "rank",
    "severity",
    "file",
    "function",
    "empirical_big_o",
    "complexity_rank",
    "big_o_error",
    "total_hits",
    "total_hotspot_time_us",
    "top_hotspot_pct_time",
    "top_hotspot_line",
    "top_hotspot_source",
    "line_profile_error",
]


def _input_record(file_path: str, func_name: str, probe_log: dict | None) -> dict:
    """One sidecar entry describing how a row's Big-O input was produced."""
    record = {"file": file_path, "function": func_name, "provenance": None,
              "shape_desc": "", "min_n": 0, "max_n": 0, "n_measures": 0,
              "residuals": None}
    if probe_log:
        record.update({k: probe_log.get(k, record[k]) for k in record if k in probe_log})
        record["provenance"] = probe_log.get("provenance")
        record["residuals"] = probe_log.get("residuals")
    return record


def _profile_big_o_field(
    file_path: str, func_name: str, module_cache: dict, args,
    input_records: list | None = None,
) -> dict:
    """Run Big-O on one target, returning the CSV-relevant fields only.

    When *input_records* is given, appends a sidecar entry describing how
    the input was produced (provenance None on failure) -- the CSV itself
    is unchanged (FIELDNAMES frozen).
    """
    def _record(probe_log):
        if input_records is not None:
            input_records.append(_input_record(file_path, func_name, probe_log))

    if file_path not in module_cache:
        try:
            module_cache[file_path] = run_big_o.load_module(file_path)
        except Exception as exc:  # a target's own import errors are data, not a crash
            module_cache[file_path] = exc
    module = module_cache[file_path]

    if isinstance(module, Exception):
        _record(None)
        return {
            "empirical_big_o": "", "complexity_rank": severity.UNKNOWN_RANK,
            "big_o_error": str(module),
        }

    target_func = getattr(module, func_name, None)
    if target_func is None:
        _record(None)
        return {
            "empirical_big_o": "", "complexity_rank": severity.UNKNOWN_RANK,
            "big_o_error": f"Function '{func_name}' not found in '{file_path}'.",
        }

    probe_log: dict = {}
    try:
        best_fit, _fitted = run_big_o.profile_big_o(
            target_func, min_n=args.min_n, max_n=args.max_n, n_measures=args.n_measures,
            n_timings=args.n_timings, n_repeats=args.n_repeats,
            timeout=getattr(args, "timeout", None), probe_log=probe_log,
        )
    except Exception as exc:
        _record(None)
        return {
            "empirical_big_o": "", "complexity_rank": severity.UNKNOWN_RANK,
            "big_o_error": str(exc),
        }

    _record(probe_log)
    rank = severity.complexity_rank(best_fit)
    return {"empirical_big_o": best_fit, "complexity_rank": rank, "big_o_error": ""}


def _profile_line_field(file_path: str, func_name: str, timeout: float | None = None) -> dict:
    """Run line-profiling on one target, returning the CSV-relevant fields only."""
    try:
        func = run_line_profile.load_function(file_path, func_name)
    except Exception as exc:  # a target's own import errors are data, not a crash
        return {
            "total_hits": 0, "total_hotspot_time_us": 0.0, "top_hotspot_pct_time": 0.0,
            "top_hotspot_line": "", "top_hotspot_source": "", "line_profile_error": str(exc),
        }

    stats = run_line_profile.compute_line_stats(func, timeout=timeout)
    if stats.get("error"):
        return {
            "total_hits": 0, "total_hotspot_time_us": 0.0, "top_hotspot_pct_time": 0.0,
            "top_hotspot_line": "", "top_hotspot_source": "", "line_profile_error": stats["error"],
        }

    hotspots = stats["hotspots"]
    total_time_us = sum(h["time_us"] for h in hotspots)
    top = hotspots[0] if hotspots else None
    return {
        "total_hits": stats["total_hits"],
        "total_hotspot_time_us": round(total_time_us, 2),
        "top_hotspot_pct_time": top["pct_time"] if top else 0.0,
        "top_hotspot_line": top["line"] if top else "",
        "top_hotspot_source": top["source"].strip()[:200] if top else "",
        "line_profile_error": "",
    }


def _build_one_row(
    file_path: str, func_name: str, module_cache: dict, args,
    input_records: list | None = None,
) -> dict:
    """Profile one pair into a CSV-ready row (no rank; caller assigns it)."""
    row = {"file": file_path, "function": func_name}
    row.update(_profile_big_o_field(file_path, func_name, module_cache, args, input_records))
    row.update(_profile_line_field(file_path, func_name, getattr(args, "timeout", None)))
    row["severity"] = severity.severity_label(row["complexity_rank"])
    return row


def build_rows(
    pairs: list[tuple[str, str]], args, input_records: list | None = None,
) -> list[dict]:
    """Profile every (file, func) pair and return CSV-ready rows, unsorted."""
    module_cache: dict = {}
    return [_build_one_row(f, fn, module_cache, args, input_records) for f, fn in pairs]


def sidecar_path(csv_path: str) -> str:
    """Path of the input-provenance sidecar accompanying *csv_path*."""
    if csv_path.endswith(".csv"):
        return csv_path[: -len(".csv")] + ".inputs.json"
    return csv_path + ".inputs.json"


def write_sidecar(path: str, records: list[dict], meta: dict | None = None) -> None:
    """Write input-provenance records beside a finished CSV (never mid-run)."""
    payload: dict = {"generated_by": "generate_baseline_csv", "rows": records}
    if meta:
        payload.update(meta)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def sort_rows(rows: list[dict]) -> list[dict]:
    """Sort most-to-least severe: complexity rank, then total hotspot time."""
    rows.sort(
        key=lambda r: severity.hotspot_sort_key(r["complexity_rank"], r["total_hotspot_time_us"]),
        reverse=True,
    )
    for i, row in enumerate(rows, start=1):
        row["rank"] = i
    return rows


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a baseline assessment CSV directly from profiling data. "
            "Rows stream in resolution order (rank = emission order); order "
            "by severity when reporting."
        ),
    )
    parser.add_argument(
        "--target-type",
        choices=["function", "file", "files", "commit", "repo"],
        default="function",
        help="Scope to profile — same semantics as run_big_o.py / run_line_profile.py.",
    )
    parser.add_argument("--file", help="Path to Python file (target-type: function, file).")
    parser.add_argument("--func", help="Target function name (target-type: function).")
    parser.add_argument(
        "--files", action="append", help="Python file path (target-type: files; repeatable).",
    )
    parser.add_argument("--commit", help="Git commit-ish to scope to (target-type: commit).")
    parser.add_argument(
        "--repo", default=".", help="Repository root (target-type: commit, repo; default: '.').",
    )
    parser.add_argument(
        "--max-targets", type=int, default=30,
        help="Safety cap on resolved (file, func) pairs (default: 30; 0 disables the cap).",
    )
    parser.add_argument(
        "--min-n", type=int, default=100, help="Minimum Big-O input size (default: 100).",
    )
    parser.add_argument(
        "--max-n", type=int, default=10000, help="Maximum Big-O input size (default: 10000).",
    )
    parser.add_argument(
        "--n-measures", type=int, default=8, help="Number of Big-O measurements (default: 8).",
    )
    parser.add_argument(
        "--n-timings", type=int, default=5,
        help=(
            "Timer samples kept (minimum) per measurement (default: 5). Raise this to "
            "reduce classification noise on fast, low-variance functions."
        ),
    )
    parser.add_argument(
        "--n-repeats", type=int, default=1,
        help="Calls to func summed per measurement (default: 1); raise for very fast functions.",
    )
    parser.add_argument(
        "--timeout", type=float, default=120.0,
        help=(
            "Per-target execution budget in seconds (default: 120). A target that "
            "exceeds it is reported as an error row instead of hanging the run."
        ),
    )
    parser.add_argument(
        "--output", default=None, help="Write CSV to this path instead of stdout.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List resolved targets and exit before importing or calling anything "
        "(no CSV or sidecar is written).",
    )
    args = parser.parse_args(argv)

    if args.target_type == "function":
        if not args.file or not args.func:
            print("Error: target-type 'function' requires --file and --func", file=sys.stderr)
            sys.exit(2)
        pairs = [(args.file, args.func)]
    else:
        try:
            pairs = target_resolution.resolve_targets(
                args.target_type, file=args.file, files=args.files,
                commit=args.commit, repo=args.repo,
            )
        except target_resolution.TargetResolutionError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

    if args.max_targets <= 0:
        truncated = False  # 0 (or negative) disables the count cap entirely;
        # the per-target --timeout budget remains the guard rail.
    else:
        truncated = len(pairs) > args.max_targets
        pairs = pairs[: args.max_targets]
    if truncated:
        print(
            f"Warning: truncated to --max-targets={args.max_targets}; more targets were resolved",
            file=sys.stderr,
        )
    if args.dry_run:
        print(target_resolution.format_target_listing(
            args.target_type, pairs, args.max_targets, truncated))
        return

    # NOTE: rows stream below (no sort_rows): every completed row is
    # written and flushed immediately, so an interrupted multi-hour scan
    # keeps everything measured so far instead of losing it all. `rank` is
    # therefore emission order, not severity order -- sort explicitly with
    # sort_rows() (still exported) or order agent-side when reporting.

    # Scope the provenance commit to the MEASURED tree, never the CWD:
    # repo/commit scans use the scanned root, file scans use the file.
    if args.target_type in ("repo", "commit"):
        provenance_path = args.repo
    elif args.target_type == "files" and args.files:
        try:
            provenance_path = os.path.commonpath(
                [os.path.abspath(f) for f in args.files]
            )
        except ValueError:
            provenance_path = args.files[0]  # e.g. different drives: use first file
    elif pairs:
        provenance_path = pairs[0][0]
    else:
        provenance_path = None

    out = open(args.output, "w", newline="", encoding="utf-8") if args.output else sys.stdout
    try:
        # Provenance preamble (W7): `# key=value` lines above the header so
        # consumers can refuse stale/incompatible artifacts. FIELDNAMES itself
        # is unchanged -- plain csv readers must skip leading `#` lines.
        provenance = provenance_mod.current_provenance(path=provenance_path)
        for line in provenance_mod.format_preamble(provenance):
            out.write(line + "\n")
        writer = csv.DictWriter(out, fieldnames=FIELDNAMES)
        writer.writeheader()
        out.flush()
        module_cache: dict = {}
        input_records: list[dict] = []
        started = time.time()
        for i, (file_path, func_name) in enumerate(pairs, start=1):
            row = _build_one_row(file_path, func_name, module_cache, args, input_records)
            row["rank"] = i
            writer.writerow(row)
            out.flush()
        if args.output:
            # Sidecar (never a column: FIELDNAMES stays frozen). Written
            # once at completion -- an interrupted run keeps its streamed
            # CSV rows; the report then labels provenance "unknown".
            write_sidecar(sidecar_path(args.output), input_records, meta={
                "targets": len(pairs),
                "started_unix": started,
                "finished_unix": time.time(),
            })
    finally:
        if args.output:
            out.close()


if __name__ == "__main__":
    main()
