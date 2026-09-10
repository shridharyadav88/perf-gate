"""Rank findings by measured production heat from cProfile/pstats dumps.

Static reachability guesses at value ("called" vs "hot"). A `cProfile`
dump (`python -m cProfile -o run.pstats …`) records what actually burned.
This module joins dump entries to module-level functions and emits a heat
index the report renderer sorts by -- measured heat outranks static
caller counts, unprofiled rows sort last.

Detection is pure/read-only (this module never writes a file, never imports
or calls target code); it only reads the dump plus target *source text*
for def-line resolution.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import pstats
import sys

GENERATED_BY = "profile_heat"


def _parse(path: str) -> ast.Module | None:
    try:
        with open(path, encoding="utf-8") as f:
            return ast.parse(f.read(), filename=path)
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return None


def def_lines(path: str) -> dict[int, str]:
    """Map 1-based def line -> name for module-level defs in *path*."""
    tree = _parse(path)
    if tree is None:
        return {}
    return {
        node.lineno: node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def norm(path: str) -> str:
    """Join-safe file key: absolute + case-normalized on both sides."""
    return os.path.normcase(os.path.abspath(path))


def heat_index(stats: pstats.Stats) -> tuple[list[dict], dict[str, int]]:
    """Map dump entries to module-level (file, function) heat rows.

    Returns (rows, skipped) where skipped counts entries dropped as
    non-Python (`~`), unparseable-file, or non-module-level (methods,
    comprehensions, nested defs) -- same module-level contract as the
    rest of the tool.
    """
    defs: dict[str, dict[int, str] | None] = {}
    acc: dict[tuple[str, str], dict] = {}
    skipped = {"non_python": 0, "unparseable": 0, "non_module_level": 0}
    for (raw_file, lineno, name), (cc, nc, tt, ct, _callers) in \
            stats.stats.items():
        if not raw_file.endswith(".py"):
            skipped["non_python"] += 1
            continue
        key = norm(raw_file)
        if key not in defs:
            # None = file not resolvable from here; cached so a dump
            # recorded under another cwd costs one probe, not one per
            # entry. Generate dumps without strip_dirs, resolved from
            # the repo root, for exact joins.
            defs[key] = def_lines(raw_file) if os.path.isfile(raw_file) \
                else None
        table = defs[key]
        if table is None:
            skipped["unparseable"] += 1
            continue
        func = table.get(lineno)
        if func is None or func != name:
            # Method, nested def, comprehension, lambda, or decorator
            # line -- not addressable by the tool's (file, function)
            # contract.
            skipped["non_module_level"] += 1
            continue
        row = acc.setdefault((key, func), {
            "file": key, "function": func,
            "cumtime_s": 0.0, "tottime_s": 0.0, "calls": 0,
        })
        row["cumtime_s"] = max(row["cumtime_s"], ct)
        row["tottime_s"] = max(row["tottime_s"], tt)
        row["calls"] += nc
    rows = sorted(acc.values(), key=lambda r: -r["cumtime_s"])
    total = sum(r["cumtime_s"] for r in rows)
    for row in rows:
        row["share"] = row["cumtime_s"] / total if total > 0 else 0.0
    return rows, skipped


def load_profiles(paths: list[str]) -> pstats.Stats:
    """Load and coalesce one or more pstats dumps; exit 2 on any problem."""
    stats: pstats.Stats | None = None
    for path in paths:
        try:
            one = pstats.Stats(path)
        except (OSError, ValueError, TypeError, EOFError) as exc:
            print(f"Error: cannot read profile dump '{path}': {exc}",
                  file=sys.stderr)
            sys.exit(2)
        stats = one if stats is None else stats.add(path)
    assert stats is not None  # argparse requires >= 1 --profile
    return stats


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Rank functions by measured cProfile/pstats heat.",
    )
    parser.add_argument("--profile", required=True, action="append",
                        help="pstats dump file (repeatable; coalesced).")
    parser.add_argument("--json", action="store_true",
                        help="Emit the heat index as JSON.")
    args = parser.parse_args(argv)

    stats = load_profiles(args.profile)
    rows, skipped = heat_index(stats)
    payload = {
        "generated_by": GENERATED_BY,
        "python": sys.version.split()[0],
        "source_files": sorted(args.profile),
        "rows": rows,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return
    if not rows:
        print("No module-level functions matched the dump "
              f"(skipped={skipped}).")
        return
    for row in rows:
        print(f"{row['file']} :: {row['function']} -- "
              f"{row['cumtime_s']:.3f}s cumulative "
              f"({100 * row['share']:.1f}% of profile)")


if __name__ == "__main__":
    main()
