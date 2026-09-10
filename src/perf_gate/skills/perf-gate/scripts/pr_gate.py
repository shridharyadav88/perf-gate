"""Diff-scoped pre-merge performance gate — one command for PR CI.

Profiles only the functions a change touches, on both sides of the change,
and fails only on *measured* regressions the diff introduces:

* a modified function's complexity rank got worse (base worktree vs PR tree);
* a touched function is newly in a mechanical tier0 tier (cheap, certain fix);
* (advisory only) wall-clock hotspot drift beyond ``--margin`` — timers are
  noisy and CI runners vary, so drift never blocks unless ``--strict-time``.

Everything else abstains silently: unresolvable targets, error rows on
either side, new functions without a mechanical pattern, deleted functions
(reported as improvements). Infrastructure hiccups (shallow checkout with
no merge-base, worktree failure, profiler crash) SKIP the gate with exit 0
and a printed reason — a gate that cries wolf gets deleted; a quiet one
that catches one real regression a month becomes permanent.

Isolation: each side runs in its own ``generate_baseline_csv`` /
``classify_findings`` subprocess with cwd set to that side's tree, so base
and PR modules never share ``sys.modules`` and row paths stay tree-relative
(and therefore comparable). No LLM, no network, stdlib only.

Exit codes: 0 clean-or-skipped, 1 blocking regression, 2 harness error
(bad arguments, missing git, not a repository).
"""

from __future__ import annotations

import argparse
import ast
import csv
import io
import os
import shutil
import subprocess
import sys
import tempfile

DEFAULT_MARGIN = 0.20  # looser than regression_gate: CI runners are noisy
DEFAULT_MAX_TARGETS = 20
DEFAULT_TARGET_TIMEOUT = 30.0
DEFAULT_STAGE_TIMEOUT = 600.0


class GateHarnessError(Exception):
    """Bad invocation or environment: exit 2."""


class GateSkip(Exception):
    """Fail-open abstention: exit 0 with the reason printed."""


def _run_cmd(argv: list[str], cwd: str, timeout: float) -> subprocess.CompletedProcess:
    """Single subprocess choke point (monkeypatched in tests)."""
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise GateHarnessError(f"executable not found: {argv[0]} ({exc})") from exc
    except subprocess.TimeoutExpired as exc:
        raise GateSkip(f"command timed out after {timeout:.0f}s: {argv[0]}") from exc


def _git(repo: str, *args: str, timeout: float = 60.0) -> str:
    """Run git in *repo*; transport/git-data failures skip, bad refs are harness errors."""
    try:
        proc = _run_cmd(["git", *args], cwd=repo, timeout=timeout)
    except GateSkip as exc:
        raise GateSkip(f"git {' '.join(args)} failed: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        raise GateHarnessError(f"git {' '.join(args)} failed: {err or proc.returncode}")
    return proc.stdout or ""


def parse_unified0_diff(text: str) -> tuple[dict[str, list[tuple[int, int]]], list[str]]:
    """Parse ``git diff -U0`` into ({path: [(new_start, new_count)]}, deleted).

    Only the ``+`` side ranges matter: they are line numbers in the current
    (PR) tree, which is what gets profiled. ``+++ /dev/null`` marks a
    deleted file.
    """
    changed: dict[str, list[tuple[int, int]]] = {}
    deleted: list[str] = []
    old: str | None = None  # `---` side; `+++` decides changed vs deleted
    path: str | None = None
    for line in text.splitlines():
        if line.startswith("--- "):
            name = line[4:].strip()
            old = name[2:] if name.startswith("a/") else name
        elif line.startswith("+++ "):
            name = line[4:].strip()
            name = name[2:] if name.startswith("b/") else name
            if name == "/dev/null":
                if old and old != "/dev/null":
                    deleted.append(old)
                path, old = None, None
            else:
                path, old = name, None
                changed.setdefault(path, [])
        elif line.startswith("@@ ") and path is not None:
            try:
                new_part = line.split("+", 1)[1].split("@@", 1)[0].strip()
                start_s, _, count_s = new_part.partition(",")
                start, count = int(start_s), int(count_s or "1")
            except (ValueError, IndexError):
                continue
            if count > 0:
                changed[path].append((start, count))
    return changed, deleted


def touched_functions(source: str, ranges: list[tuple[int, int]]) -> list[str]:
    """Module-level function names whose span intersects any changed range.

    Unparseable source yields [] (fail-open: the pipeline's error rows then
    abstain instead of guessing scope).
    """
    if not ranges:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    spans = [
        (node.name, node.lineno, getattr(node, "end_lineno", None) or node.lineno)
        for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    names = [
        name for name, first, last in spans
        if any(start <= last and first < start + count for start, count in ranges)
    ]
    return names


def _module_functions(source: str) -> list[str]:
    """All module-level function names (for new/untracked files)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [
        node.name for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _is_excluded_rel(rel: str) -> bool:
    parts = set(rel.split("/"))
    return bool(parts & {"tests", "testing", "docs", "examples", "demo", "demos"})


def collect_touched(repo: str, base: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return ((current pairs), (base-only pairs)) for the base...worktree diff.

    Current pairs are (repo-relative path, func) in the working tree;
    base-only pairs cover functions deleted by the change (resolved from the
    base blob so the comparison can report them as improvements).
    """
    try:
        merge_base = _git(repo, "merge-base", base, "HEAD").strip()
    except GateHarnessError as exc:
        raise GateSkip(f"no merge-base with '{base}' (shallow checkout?): {exc}") from exc
    try:
        diff = _git(repo, "diff", "-U0", merge_base, "--", "*.py")
    except GateHarnessError as exc:
        raise GateSkip(f"cannot diff against '{base}': {exc}") from exc
    changed, deleted = parse_unified0_diff(diff)

    try:
        status = _git(repo, "status", "--porcelain", "--", "*.py")
    except GateHarnessError as exc:
        raise GateSkip(f"cannot list untracked files: {exc}") from exc
    whole: set[str] = set()  # untracked files: every function is new
    for line in status.splitlines():
        if line.startswith("??"):
            name = line[2:].strip().strip('"')
            if name.endswith(".py") and not _is_excluded_rel(name):
                changed.setdefault(name, [])
                whole.add(name)

    current: list[tuple[str, str]] = []
    for rel in sorted(changed):
        if _is_excluded_rel(rel):
            continue
        full = os.path.join(repo, rel)
        if not os.path.isfile(full):
            continue  # deleted from worktree after the diff; base side covers it
        try:
            with open(full, encoding="utf-8") as f:
                source = f.read()
        except OSError:
            continue
        names = _module_functions(source) if rel in whole else touched_functions(
            source, changed[rel])
        current.extend((rel, name) for name in names)

    base_only: list[tuple[str, str]] = []
    missing = {r for r in changed if not os.path.isfile(os.path.join(repo, r))}
    for rel in sorted(set(deleted) | missing):
        if _is_excluded_rel(rel) or not rel.endswith(".py"):
            continue
        try:
            blob = _git(repo, "show", f"{merge_base}:{rel}")
        except GateHarnessError:
            continue
        base_only.extend((rel, name) for name in _module_functions(blob))
    # Intra-file deletions simply vanish from scope (nothing left to gate);
    # whole-file deletions compare as "no longer reported" improvements.
    return current, base_only


def _is_tier0(tier: str) -> bool:
    """Mechanical, certain, cheap-to-fix tiers — the only new-code failures."""
    return tier.startswith("tier0_")


def _rank(row: dict) -> int | None:
    try:
        return int((row.get("complexity_rank") or "").strip())
    except (ValueError, AttributeError):
        return None


def _time_us(row: dict) -> float | None:
    try:
        return float((row.get("total_hotspot_time_us") or "").strip())
    except (ValueError, AttributeError):
        return None


def _row_ok(row: dict) -> bool:
    """A row with usable evidence (profiling actually ran on both sides)."""
    return not (row.get("big_o_error") or row.get("line_profile_error"))


def verdict(
    touched: list[tuple[str, str]],
    base_rows: list[dict],
    current_rows: list[dict],
    margin: float = DEFAULT_MARGIN,
) -> dict:
    """Compare two classified row sets over touched functions; pure function.

    * modified (evidence both sides): rank worsening fails; newly-entered
      tier0 fails; hotspot drift beyond *margin* is advisory.
    * new (no usable base row): current tier0 fails (fresh mechanical debt
      is always cheap to fix now, expensive later); tier2 is advisory.
    * deleted (no current row): improvement note, never a failure.
    * error rows on either side of a modified function: abstain silently.
    """
    base = {(r.get("file"), r.get("function")): r for r in base_rows}
    cur = {(r.get("file"), r.get("function")): r for r in current_rows}
    failures: list[str] = []
    advisories: list[str] = []
    improvements: list[str] = []
    for key in sorted(set(touched)):
        old, new = base.get(key), cur.get(key)
        label = f"{key[0]} :: {key[1]}"
        if new is None:
            improvements.append(f"{label} no longer reported")
            continue
        new_tier = new.get("tier") or ""
        if old is None or not _row_ok(old):
            if _is_tier0(new_tier):
                failures.append(f"{label} is newly {new_tier}")
            elif (new.get("tier") or "") == "tier2_algorithmic":
                advisories.append(f"{label} is newly tier2_algorithmic (review)")
            continue
        if not _row_ok(new):
            continue  # unmeasurable after the change: abstain, never guess
        old_rank, new_rank = _rank(old), _rank(new)
        if old_rank is not None and new_rank is not None and old_rank >= 0:
            if new_rank > old_rank:
                failures.append(
                    f"{label} complexity rank {old_rank} -> {new_rank} "
                    f"({old.get('empirical_big_o', '?')} -> "
                    f"{new.get('empirical_big_o', '?')})")
            elif new_rank < old_rank:
                improvements.append(f"{label} complexity rank {old_rank} -> {new_rank}")
        old_tier = old.get("tier") or ""
        if _is_tier0(new_tier) and new_tier != old_tier:
            failures.append(f"{label} entered {new_tier} (was {old_tier or 'none'})")
        old_t, new_t = _time_us(old), _time_us(new)
        if old_t and new_t and old_t > 0 and (new_t - old_t) / old_t > margin:
            advisories.append(
                f"{label} hotspot {old_t:.1f}us -> {new_t:.1f}us "
                f"(+{100 * (new_t - old_t) / old_t:.0f}%, margin {100 * margin:.0f}%)")
    return {"failures": failures, "advisories": advisories, "improvements": improvements}


def read_classified(path: str) -> list[dict]:
    """Read a classified CSV, ignoring `#` preamble comment lines."""
    try:
        with open(path, newline="", encoding="utf-8") as f:
            raw = f.read()
    except OSError as exc:
        raise GateSkip(f"cannot read '{path}': {exc}") from exc
    body = "\n".join(line for line in raw.splitlines() if not line.startswith("#"))
    reader = csv.DictReader(io.StringIO(body))
    if not reader.fieldnames or "tier" not in reader.fieldnames:
        raise GateSkip(f"'{path}' has no 'tier' column — classification failed")
    return list(reader)


def _merge_raw_csvs(parts: list[str], merged: str) -> None:
    """Combine per-pair raw CSVs: first preamble wins, bodies concatenated."""
    preamble: list[str] | None = None
    fieldnames: list[str] | None = None
    bodies: list[list[dict]] = []
    for part in parts:
        with open(part, newline="", encoding="utf-8") as f:
            raw = f.read()
        head = [line for line in raw.splitlines() if line.startswith("#")]
        if preamble is None:
            preamble = head
        body = "\n".join(line for line in raw.splitlines() if not line.startswith("#"))
        reader = csv.DictReader(io.StringIO(body))
        if fieldnames is None:
            fieldnames = list(reader.fieldnames or [])
        bodies.append(list(reader))
    with open(merged, "w", newline="", encoding="utf-8") as f:
        for line in preamble or []:
            f.write(line + "\n")
        writer = csv.DictWriter(f, fieldnames=fieldnames or [])
        writer.writeheader()
        for rows in bodies:
            writer.writerows(rows)


def run_side(
    scripts_dir: str, tree_root: str, pairs: list[tuple[str, str]], work_prefix: str,
    target_timeout: float, stage_timeout: float,
) -> list[dict]:
    """Profile + classify exactly *pairs* (tree-relative) with cwd=*tree_root*.

    One ``--target-type function`` invocation per pair keeps the cost
    proportional to the diff (never whole files); per-target errors become
    error rows that the verdict abstains on. Returns the classified rows.
    Raises GateSkip when a stage fails — an unmeasurable side abstains,
    it never blocks the merge.
    """
    if not pairs:
        return []
    gen_script = os.path.join(scripts_dir, "generate_baseline_csv.py")
    parts = []
    for i, (rel, func) in enumerate(pairs):
        part = f"{work_prefix}_{i}_raw.csv"
        proc = _run_cmd(
            [sys.executable, gen_script, "--target-type", "function",
             "--file", rel, "--func", func, "--output", part,
             "--timeout", str(target_timeout)],
            cwd=tree_root, timeout=stage_timeout)
        if proc.returncode != 0:
            raise GateSkip(f"profiling {rel} :: {func} failed under "
                           f"'{tree_root}': {(proc.stderr or '').strip()[-300:]}")
        parts.append(part)
    raw_csv = f"{work_prefix}_raw.csv"
    classified_csv = f"{work_prefix}.csv"
    try:
        _merge_raw_csvs(parts, raw_csv)
    except OSError as exc:
        raise GateSkip(f"cannot merge profiling output: {exc}") from exc
    proc = _run_cmd(
        [sys.executable, os.path.join(scripts_dir, "classify_findings.py"),
         "--input", raw_csv, "--output", classified_csv],
        cwd=tree_root, timeout=stage_timeout)
    if proc.returncode != 0:
        raise GateSkip(f"classification failed under '{tree_root}': "
                       f"{(proc.stderr or '').strip()[-300:]}")
    return read_classified(classified_csv)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Fail only on measured performance regressions the diff introduces.")
    parser.add_argument("--base", default="origin/main",
                        help="Base ref to compare against (default: origin/main).")
    parser.add_argument("--repo", default=".",
                        help="Repository root / PR tree (default: '.').")
    parser.add_argument("--margin", type=float, default=DEFAULT_MARGIN,
                        help="Relative hotspot drift advisory threshold "
                        f"(default: {DEFAULT_MARGIN}).")
    parser.add_argument("--max-targets", type=int, default=DEFAULT_MAX_TARGETS,
                        help="Max touched functions to profile per side "
                        f"(default: {DEFAULT_MAX_TARGETS}; 0 disables).")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TARGET_TIMEOUT,
                        help="Per-target execution budget in seconds "
                        f"(default: {DEFAULT_TARGET_TIMEOUT}).")
    parser.add_argument("--stage-timeout", type=float, default=DEFAULT_STAGE_TIMEOUT,
                        help="Seconds per pipeline stage before skipping "
                        f"(default: {DEFAULT_STAGE_TIMEOUT}).")
    parser.add_argument("--workdir", default=None,
                        help="Artifact/worktree parent (default: a fresh temp dir).")
    parser.add_argument("--keep-artifacts", action="store_true",
                        help="Keep workdir CSVs + base worktree for inspection.")
    parser.add_argument("--strict-time", action="store_true",
                        help="Promote hotspot advisories to failures (same-runner only).")
    args = parser.parse_args(argv)

    try:
        repo = os.path.abspath(args.repo)
        scripts_dir = os.path.dirname(os.path.abspath(__file__))
        _git(repo, "rev-parse", "--show-toplevel")  # harness error if not a repo
        try:
            base_sha = _git(repo, "rev-parse", "--verify", args.base).strip()
        except GateHarnessError as exc:
            raise GateSkip(f"base ref '{args.base}' not present "
                           f"(shallow checkout?): {exc}") from exc

        current, base_only = collect_touched(repo, base_sha)
        touched = sorted(set(current) | set(base_only))
        if not touched:
            print("SKIP: no touched functions vs base — nothing to gate.")
            return
        if args.max_targets > 0 and len(touched) > args.max_targets:
            print(f"Note: {len(touched)} touched functions; profiling first "
                  f"{args.max_targets} (sorted).")
            touched = touched[: args.max_targets]
        current_in_scope = [p for p in current if p in touched]
        base_in_scope = [p for p in touched]  # base profiles everything in scope
        print(f"Gating {len(touched)} touched function(s) "
              f"({len(base_only)} base-only) vs {args.base}.")

        tmp = args.workdir or tempfile.mkdtemp(prefix="pr_gate_")
        os.makedirs(tmp, exist_ok=True)
        base_tree = os.path.join(tmp, "base_tree")
        if os.path.exists(base_tree):
            shutil.rmtree(base_tree)
        try:
            _git(repo, "worktree", "add", "--detach", base_tree, base_sha)
        except GateHarnessError as exc:
            raise GateSkip(f"cannot materialize base worktree: {exc}") from exc
        try:
            head_rows = run_side(scripts_dir, repo, current_in_scope,
                                 os.path.join(tmp, "head"), args.timeout,
                                 args.stage_timeout)
            base_rows = run_side(scripts_dir, base_tree, base_in_scope,
                                 os.path.join(tmp, "base"), args.timeout,
                                 args.stage_timeout)
            result = verdict(touched, base_rows, head_rows, margin=args.margin)
        finally:
            try:
                _run_cmd(["git", "worktree", "remove", "--force", base_tree],
                         cwd=repo, timeout=60.0)
            except (GateHarnessError, GateSkip) as exc:
                print(f"Note: base worktree cleanup failed ({exc}); "
                      f"remove '{base_tree}' manually.")
            if not args.keep_artifacts and not args.workdir:
                shutil.rmtree(tmp, ignore_errors=True)

        for line in result["improvements"]:
            print(f"IMPROVED: {line}")
        for line in result["advisories"]:
            print(f"{'FAIL' if args.strict_time else 'ADVISORY'}: {line}")
        for line in result["failures"]:
            print(f"FAIL: {line}")
        if result["failures"] or (args.strict_time and result["advisories"]):
            print(f"{len(result['failures'])} blocking regression(s) introduced by this change.")
            sys.exit(1)
        print("No blocking regressions introduced by this change.")
    except GateSkip as exc:
        print(f"SKIP: {exc}")
    except GateHarnessError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
