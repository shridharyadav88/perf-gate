"""Render a classified baseline CSV as a short Markdown action list.

Consumes the CSV produced by `classify_findings.py` (plus the optional
`.inputs.json` sidecar from `generate_baseline_csv.py`) and emits the
outsider-readable summary: what to fix now, what to review, and what is
by design -- instead of a 228-row CSV that is mostly error rows. Fully
deterministic, zero LLM tokens.

Big-O honesty rule (the reason the sidecar exists): a "Constant" fit on
synthetic micro-input proves the call ran, not the complexity. Such rows
render as *unmeasured*, never as findings.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import provenance as provenance_mod  # noqa: E402

# Cheapest-and-most-certain first (mirrors classify_findings._TIER0_PRIORITY --
# keep the two tuples identical; TestTierRegistry pins this).
TIER0_PRIORITY = (
    "tier0_regex_hoist", "tier0_re_call", "tier0_perf402", "tier0_perf401",
    "tier0_perf403", "tier0_str_join", "tier0_sum_reduce", "tier0_set_build",
    "tier0_invariant_hoist",
)

# Resolver owning each mechanical tier (points the fixer at the applier).
TIER0_RESOLVER = {
    "tier0_regex_hoist": "resolvers/apply_regex_hoist.py",
    "tier0_re_call": "resolvers/apply_re_call.py",
    "tier0_perf402": "resolvers/apply_perf_comprehension.py",
    "tier0_perf401": "resolvers/apply_perf_comprehension.py",
    "tier0_perf403": "resolvers/apply_perf_comprehension.py",
    "tier0_str_join": "resolvers/apply_accumulator.py",
    "tier0_sum_reduce": "resolvers/apply_accumulator.py",
    "tier0_set_build": "resolvers/apply_accumulator.py",
    "tier0_invariant_hoist": "resolvers/apply_invariant_hoist.py",
}


def load_sidecar(path: str | None) -> tuple[dict, dict]:
    """Return ((file, function) -> provenance record, sweep meta).

    Empty on any problem -- never fail the report over a missing sidecar.
    Meta carries wall-clock evidence (targets, started/finished timestamps)
    when the generating sweep recorded it.
    """
    if not path:
        return {}, {}
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return {}, {}
    if not isinstance(payload, dict):
        return {}, {}
    records = payload.get("rows", [])
    meta = {k: payload.get(k) for k in ("targets", "started_unix", "finished_unix")
            if payload.get(k) is not None}
    return ({(r.get("file"), r.get("function")): r for r in records
             if isinstance(r, dict)}, meta)


def load_reachability(path: str | None) -> dict:
    """Map (file, function) -> value record; empty on any problem."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return {}
    rows = payload if isinstance(payload, list) else []
    return {(r.get("file"), r.get("function")): r for r in rows
            if isinstance(r, dict)}


def _reach_key(row: dict, reach: dict) -> tuple:
    """Order key: entry-reachable first, then most callers (unknowns last)."""
    rec = reach.get((row.get("file"), row.get("function")), {}) or {}
    if "callers" not in rec:
        return (1, 0)
    return (0 if rec.get("entry_reachable") else 1, -(rec.get("callers") or 0))


def _heat_file_key(path: str | None) -> str | None:
    """Join-safe file key; None for empty so it can never match a row."""
    if not (path or "").strip():
        return None
    return os.path.normcase(os.path.abspath(path))


def load_heat(path: str | None) -> tuple[dict | None, dict]:
    """Map (file, function) -> heat record plus heat meta.

    (None, {}) when no file was passed (lane off); empty map on any
    problem -- never fail the report over a missing/broken heat file.
    Accepts the `profile_heat.py` dict shape and tolerates a raw list.
    """
    if not path:
        return None, {}
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return {}, {}
    meta: dict = {}
    rows = payload
    if isinstance(payload, dict):
        meta = {k: payload.get(k) for k in
                ("generated_by", "python", "source_files")
                if payload.get(k) is not None}
        rows = payload.get("rows", [])
    if not isinstance(rows, list):
        return {}, meta
    heat = {}
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        key = _heat_file_key(rec.get("file"))
        if key is None:
            continue
        heat[(key, rec.get("function"))] = rec
    return heat, meta


def _heat_key(row: dict, heat: dict | None) -> tuple:
    """Order key: hottest cumulative share first (unprofiled last)."""
    if heat is None:
        return (1, 0.0)
    rec: dict = {}
    key = _heat_file_key(row.get("file"))
    if key is not None:
        rec = heat.get((key, row.get("function")), {}) or {}
    if "cumtime_s" not in rec:
        return (1, 0.0)
    return (0, -(rec.get("share", 0.0) or 0.0))


def _sweep_footer(meta: dict) -> str:
    started, finished = meta.get("started_unix"), meta.get("finished_unix")
    if not isinstance(started, (int, float)) or \
            not isinstance(finished, (int, float)) or finished < started:
        return ""
    seconds = finished - started
    duration = f"{seconds:.0f}s" if seconds < 90 else f"{seconds / 60:.0f}m"
    targets = meta.get("targets", "?")
    return (f"_Sweep cost (wall-clock, this machine): {targets} targets "
            f"in {duration}. Token cost: zero -- this report is deterministic._")


def fit_confidence(row: dict, sidecar: dict) -> str:
    """Fit-quality qualifier from sidecar residuals (empty when N/A).

    Compares the reported winner against the best residual: disagreement
    means the fitter flap-picked, and a narrow margin means the window was
    too small to separate the models. Either way the remedy is concrete
    (wider N), never a shrug.
    """
    rec = sidecar.get((row.get("file"), row.get("function")), {}) or {}
    residuals = {k: v for k, v in (rec.get("residuals") or {}).items()
                 if isinstance(v, (int, float))}
    if len(residuals) < 2:
        return ""
    winner = (row.get("empirical_big_o") or "").strip()
    if winner not in residuals:
        return ""  # sidecar from a different run -- don't mix evidence
    ordered = sorted(residuals, key=lambda k: residuals[k])
    best, runner_up = residuals[ordered[0]], residuals[ordered[1]]
    max_n = rec.get("max_n") or 0
    wider = f" -- re-run with wider N (--max-n {2 * max_n})" if max_n else \
        " -- re-run with wider N"
    if winner != ordered[0]:
        return f"flap risk: winner differs from best residual{wider}"
    if runner_up <= 0:
        return ""
    separation = 1.0 - best / runner_up
    if separation >= 0.5:
        return "clear winner"
    return f"close call (separation {separation:.0%}){wider}"


def big_o_label(row: dict, sidecar: dict) -> str:
    """Honest one-line label for a row's Big-O cell."""
    if (row.get("big_o_error") or "").strip():
        return "failed"
    fit = (row.get("empirical_big_o") or "").strip() or "unknown"
    rec = sidecar.get((row.get("file"), row.get("function")), {})
    provenance = (rec or {}).get("provenance")
    confidence = fit_confidence(row, sidecar)
    if provenance == "harness":
        label = f"{fit} (measured, harness input)"
    elif provenance == "synthetic":
        if fit.startswith("Constant"):
            # A Constant fit on micro-input is uninformative even when the
            # fitter is sure of itself -- confidence adds nothing here.
            return (f"{fit} (unmeasured -- synthetic micro-input proves "
                    "the call ran, not the complexity)")
        label = f"{fit} (weak -- synthetic input, confirm with a harness)"
    else:
        return f"{fit} (provenance unknown -- no sidecar)"
    return f"{label}; {confidence}" if confidence else label


def error_bucket(message: str) -> str:
    """Coarse bucket for unactionable Big-O errors (counts, not findings)."""
    message = message or ""
    if "CLI command" in message or "takes no arguments" in message:
        return "by design (not Big-O-scalable; line profiling may still apply)"
    if "No module named" in message:
        return "missing dependency (install the target's requirements)"
    if "(unknown location)" in message or "partially initialized" in message:
        return "needs installed package (stub-blocked __init__ name/order)"
    if "unsupported operand" in message:
        return "needs newer interpreter (annotation syntax)"
    return "target-side rejection (fuzzed input correctly refused)"


def _row_title(row: dict) -> str:
    return f"{row.get('file', '?')} :: {row.get('function', '?')}"


def _hotspot(row: dict) -> str:
    line = (row.get("top_hotspot_line") or "").strip()
    source = (row.get("top_hotspot_source") or "").strip()
    if not line and not source:
        return "no hotspot recorded"
    return f"line {line}: `{source}`" if line else f"`{source}`"


def render(rows: list[dict], sidecar: dict, top: int,
           provenance: dict, ruff_rows: list[dict] | None = None,
           reach: dict | None = None, meta: dict | None = None,
           heat: dict | None = None, heat_meta: dict | None = None) -> str:
    """Build the full Markdown report."""
    out = ["# Performance action list", ""]
    if provenance:
        stamp = ", ".join(f"{k}={provenance.get(k, '?')}"
                          for k in ("tool_version", "tier", "python"))
        out += [f"_Measured with {stamp}; {len(rows)} targets._", ""]

    tier0 = sorted(
        (r for r in rows if (r.get("tier") or "") in TIER0_PRIORITY),
        key=lambda r: TIER0_PRIORITY.index(r.get("tier", "")),
    )
    out += ["## Fix now (mechanical, verify with a re-measure)", ""]
    if not tier0:
        out += ["(none)", ""]
    for row in tier0:
        tier = row.get("tier", "")
        out += [f"### {_row_title(row)}",
                f"- tier: `{tier}` (apply `{TIER0_RESOLVER.get(tier, '?')}`)",
                f"- detail: {row.get('tier_detail', '').strip()}",
                f"- Big-O: {big_o_label(row, sidecar)}",
                f"- hotspot: {_hotspot(row)}", ""]

    def _review_key(r: dict) -> tuple:
        # Measured heat outranks static reachability; either lane off
        # ties so the other decides (unknowns last in both).
        return (_heat_key(r, heat), _reach_key(r, reach or {}))

    tier2 = sorted(
        (r for r in rows if r.get("tier") == "tier2_algorithmic"),
        key=_review_key,
    )
    tier1_all = sorted(
        (r for r in rows if r.get("tier") == "tier1_review"),
        key=_review_key,
    )
    out += ["## Review (needs a human or capable model)", ""]
    review = tier2 + tier1_all[:top]
    if not review:
        out += ["(none)", ""]
    for row in review:
        out += [f"### {_row_title(row)}",
                f"- tier: `{row.get('tier', '')}`",
                f"- Big-O: {big_o_label(row, sidecar)}",
                f"- hotspot: {_hotspot(row)}"]
        rec = (reach or {}).get((row.get("file"), row.get("function")), {}) or {}
        if "callers" in rec:
            out += [f"- reachability: {rec.get('value', 'unknown')}"]
        if heat is not None:
            hrec: dict = {}
            hkey = _heat_file_key(row.get("file"))
            if hkey is not None:
                hrec = heat.get((hkey, row.get("function")), {}) or {}
            if "cumtime_s" in hrec:
                share = 100 * (hrec.get("share", 0.0) or 0.0)
                out += [f"- heat: {hrec['cumtime_s']:.3f}s cumulative "
                        f"({share:.1f}% of profile)"]
        out += [""]
    if len(tier1_all) > top:
        out += [f"_...and {len(tier1_all) - top} more tier1 rows (use --top N)._", ""]
    if heat is not None:
        heat_py = (heat_meta or {}).get("python")
        report_py = (provenance or {}).get("python")
        if heat_py and report_py and heat_py.split(".")[:2] != \
                report_py.split(".")[:2]:
            out += [f"_Profile recorded under Python {heat_py}, report measured "
                    f"under {report_py} -- heat shares still rank, absolute "
                    f"seconds may differ._", ""]

    buckets: dict[str, int] = {}
    dark = 0
    dark_rows: list[dict] = []
    for row in rows:
        if not (row.get("big_o_error") or "").strip():
            continue
        buckets[error_bucket(row["big_o_error"])] = \
            buckets.get(error_bucket(row["big_o_error"]), 0) + 1
        if (row.get("line_profile_error") or "").strip():
            dark += 1
            dark_rows.append(row)
    out += ["## By design / errors (not findings)", ""]
    if not buckets:
        out += ["(none -- every row has Big-O signal)", ""]
    for bucket in sorted(buckets, key=lambda b: -buckets[b]):
        out += [f"- {buckets[bucket]} rows: {bucket}"]
    out += ["", f"{dark} rows are dark on both profilers.", ""]
    if heat is not None and dark_rows:
        # A dark row with measured cProfile heat is not provably a finding
        # (no scaling proof), but heat says exactly where to spend the one
        # harness that would promote it -- surface the top of that queue.
        heated = []
        for row in dark_rows:
            hkey = _heat_file_key(row.get("file"))
            hrec = (heat.get((hkey, row.get("function")), {})
                    if hkey is not None else {}) or {}
            if "cumtime_s" in hrec:
                share = 100 * (hrec.get("share", 0.0) or 0.0)
                heated.append((share, hrec["cumtime_s"], row))
        heated.sort(key=lambda t: -t[0])
        if heated:
            out += ["- heat-visible dark rows (no scaling proof, but measured "
                    "profile time -- write one harness here first):", ""]
            for share, cumtime, row in heated[:top]:
                out += [f"- `{_row_title(row)}` -- {cumtime:.3f}s cumulative "
                        f"({share:.1f}% of profile)"]
            if len(heated) > top:
                out += [f"- ...and {len(heated) - top} more (same --top cap)"]
            out += [""]

    out += ["## Ruff PERF (upstream lints, not our tiers)", ""]
    if ruff_rows is None:
        out += ["(none -- pass --ruff <ruff-perf.json> to include this lane)", ""]
    else:
        ruff_rows = [r for r in ruff_rows if r.get("code", "").startswith("PERF")]
        if not ruff_rows:
            out += ["(none -- ruff PERF checked, zero findings)", ""]
        else:
            out += ["Apply with `ruff check --select PERF --fix` where a fix is "
                    "offered, otherwise by hand, then re-measure with our "
                    "profilers. PERF401/402/403 hits already covered by tier0 rows "
                    "above are deduped by `ruff_perf.py` -- what remains here "
                    "is ruff-unique.", ""]
        for row in ruff_rows[:top]:
            fix = " [ruff auto-fix offered]" if row.get("fix") else ""
            out += [f"- `{row.get('file', '?')}:{row.get('line', '?')}` "
                    f"[{row.get('code', '?')}] {row.get('message', '')}{fix}"]
        if len(ruff_rows) > top:
            out += [f"- ...and {len(ruff_rows) - top} more (same --top cap)"]
        out += [""]
    footer = _sweep_footer(meta or {})
    if footer:
        out += [footer, ""]
    return "\n".join(out)


def load_ruff(path: str | None) -> list[dict] | None:
    """Parse a ruff PERF JSON file (raw list shape).

    None when no file was passed (lane not requested); empty on any
    problem -- never fail the report over a missing/broken ruff file.
    """
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return []  # never fail the report over a missing/broken ruff file
    if isinstance(payload, dict):
        # Tolerate a wrapped shape; the raw ruff list is the contract.
        payload = payload.get("findings", [])
    if not isinstance(payload, list):
        return []
    rows = []
    for rec in payload:
        if not isinstance(rec, dict):
            continue
        loc = rec.get("location") or {}
        try:
            line = int(loc.get("row", 0))
        except (TypeError, ValueError):
            continue
        if line <= 0:
            continue
        rows.append({
            "file": str(rec.get("filename", "")),
            "line": line,
            "code": str(rec.get("code", "")),
            "message": str(rec.get("message", "")),
            "fix": bool((rec.get("fix") or {}).get("applicability")),
        })
    return rows


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Render a classified CSV as a Markdown action list.",
    )
    parser.add_argument("--input", required=True, help="Classified CSV path.")
    parser.add_argument("--inputs", default=None,
                        help="Optional .inputs.json sidecar for Big-O honesty labels.")
    parser.add_argument("--ruff", default=None,
                        help="Optional ruff PERF JSON file (raw list shape).")
    parser.add_argument("--reachability", default=None,
                        help="Optional reachability JSON (detectors/reachability.py "
                        "--json): orders Review by value, tags dead code.")
    parser.add_argument("--heat", default=None,
                        help="Optional heat JSON (profile_heat.py --json): "
                        "orders Review by measured cumulative share first.")
    parser.add_argument("--top", type=int, default=10,
                        help="Max tier1 rows listed (default: 10; tier2 always fully listed).")
    parser.add_argument("--output", default=None, help="Write to path instead of stdout.")
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
    sidecar, meta = load_sidecar(args.inputs)
    heat, heat_meta = load_heat(args.heat)
    report = render(list(reader), sidecar, args.top, proven,
                    load_ruff(args.ruff), load_reachability(args.reachability), meta,
                    heat, heat_meta)

    out = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout
    try:
        out.write(report)
    finally:
        if args.output:
            out.close()


if __name__ == "__main__":
    main()
