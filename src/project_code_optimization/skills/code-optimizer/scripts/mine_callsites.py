"""Mine real call sites of a target function and sketch a harness module.

Big-O on synthetic micro-input can only prove the call ran, never the
complexity. This script finds how the target is REALLY called across a repo
(arg sources included) and emits a harness sketch: a starting-point module
the human reviews, corrects for realism, and then profiles with the normal
pipeline. Approved harnesses keep the ``__big_o_provenance__`` marker, which
``run_big_o.profile_big_o`` reads -- that is the only thing that upgrades a
row from "synthetic" to "harness" provenance.

The sketch follows the adapter's single-list calling convention: the entry
point takes one growing ``data`` list and derives ``n = len(data)`` from it,
so the existing adapter scales it with zero profiler changes.

Read-only toward the target (AST only, never imports or calls anything);
writes only the sketch file (or stdout). A sketch with zero mined sites is
still emitted -- defaults plus EDIT markers -- with exit 0.
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
from dataclasses import dataclass, field

# Hygiene dirs only -- tests/examples/docs are INCLUDED deliberately: calls
# found there are realistic inputs.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", ".tox",
    "node_modules", "build", "dist", ".eggs",
})


@dataclass
class CallSite:
    """One syntactic call of the target: file, line, arg sources as written."""

    file: str
    lineno: int
    args: list[str] = field(default_factory=list)


def _iter_repo_files(repo: str) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")
                       and not d.endswith(".egg-info")]
        found.extend(
            os.path.join(dirpath, name)
            for name in filenames
            if name.endswith(".py")
        )
    return sorted(found)


def _target_signature(file_path: str, func_name: str) -> list[tuple[str, str, str]]:
    """(param, annotation_src, default_src) for the target's positional params."""
    with open(file_path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=file_path)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == func_name:
            params = list(node.args.args)
            if params and params[0].arg in ("self", "cls"):
                params = params[1:]
            defaults = [None] * (len(params) - len(node.args.defaults)) \
                + list(node.args.defaults)
            return [(p.arg,
                     ast.unparse(p.annotation) if p.annotation else "",
                     ast.unparse(d) if d is not None else "")
                    for p, d in zip(params, defaults)]
    raise ValueError(f"no top-level 'def {func_name}' in '{file_path}'")


def _default_for(annotation: str) -> str:
    ann = annotation.strip().strip("'\"")
    if ann.startswith("str"):
        return '""'
    if ann.startswith("bool"):
        return "False"
    if ann.startswith("float"):
        return "0.0"
    if ann.startswith("int"):
        return "0"
    if ann.startswith("list") or ann.startswith("tuple"):
        return "[]"
    if ann.startswith("dict"):
        return "{}"
    return "None"


def _calls_in_file(path: str, target: str, stem: str, target_abs: str) -> list[CallSite]:
    """Name-based call search with module-basename correlation (conservative)."""
    try:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return []
    direct: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mod_base = node.module.split(".")[-1]
            for alias in node.names:
                if alias.name == target and mod_base in (stem, target):
                    direct.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[-1] == stem:
                    direct.add(alias.asname or alias.name.split(".")[-1])
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        hit = False
        if isinstance(func, ast.Name) and func.id in direct:
            hit = True
        elif (isinstance(func, ast.Attribute) and func.attr == target
                and isinstance(func.value, ast.Name)
                and func.value.id in direct):
            hit = True
        elif os.path.abspath(path) == target_abs \
                and isinstance(func, ast.Name) and func.id == target:
            hit = True  # same-file call needs no import
        if not hit:
            continue
        args = [ast.unparse(a) for a in node.args]
        args += [f"{k.arg}={ast.unparse(k.value)}" for k in node.keywords
                 if k.arg is not None]
        sites.append(CallSite(file=path, lineno=node.lineno or 0, args=args))
    return sites


def mine(repo: str, file_path: str, func_name: str) -> tuple[list[tuple[str, str, str]],
                                                            list[CallSite]]:
    """Return (signature, call sites across *repo*, sorted by file then line)."""
    target_abs = os.path.abspath(file_path)
    stem = os.path.splitext(os.path.basename(file_path))[0]
    signature = _target_signature(file_path, func_name)
    sites: list[CallSite] = []
    for path in _iter_repo_files(repo):
        sites.extend(_calls_in_file(path, func_name, stem, target_abs))
    sites.sort(key=lambda s: (s.file, s.lineno))
    return signature, sites


def render_sketch(file_path: str, func_name: str,
                  signature: list[tuple[str, str, str]],
                  sites: list[CallSite], max_sites: int = 10) -> str:
    """Build the harness module text (review before use -- it is a sketch)."""
    stem = os.path.splitext(os.path.basename(file_path))[0]
    target_dir = os.path.dirname(os.path.abspath(file_path))
    shown = sites[:max_sites]
    lines = [
        f'"""HARNESS SKETCH for {file_path} :: {func_name} (generated, UNAPPROVED).',
        "",
        "Review the built inputs for realism, adjust for your domain, then profile:",
        f"  generate_baseline_csv.py --target-type function --file <this> "
        f"--func harness_for_{func_name}",
        "Approved harnesses keep the __big_o_provenance__ marker below -- that",
        "marker is what upgrades the row from synthetic to harness provenance.",
        "",
        "Mined call sites:",
    ]
    if not shown:
        lines.append("  (none found -- defaults below are pure guesses)")
    for site in shown:
        call = f"{func_name}({', '.join(site.args)})"
        lines.append(f"  {site.file}:{site.lineno}: {call}")
    if len(sites) > max_sites:
        lines.append(f"  ...and {len(sites) - max_sites} more")
    lines += [
        '"""',
        "import os",
        "import sys",
        "",
        f"sys.path.insert(0, {target_dir!r})  # adjust if the target moves",
        f"from {stem} import {func_name}",
        "",
        "",
        f"def harness_for_{func_name}(data):",
        '    """Build a realistic size-n input and call straight through."""',
        "    n = len(data)  # the adapter scales this list; EDIT the rest",
    ]
    call_args = []
    for i, (param, annotation, default) in enumerate(signature):
        if i == 0:
            lines.append(f"    {param} = list(range(n))  # EDIT: realistic size-n input")
        elif default:
            lines.append(f"    {param} = {default}")
        else:
            lines.append(f"    {param} = {_default_for(annotation)}  # EDIT for realism")
        call_args.append(param)
    lines += [
        f"    return {func_name}({', '.join(call_args)})",
        "",
        "",
        f"harness_for_{func_name}.__big_o_provenance__ = \"harness\"",
        "",
        "",
        'if __name__ == "__main__":  # smoke: python3 <this> (no profiling)',
        f"    print(harness_for_{func_name}(list(range(10))))",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Mine real call sites and sketch a Big-O harness module.",
    )
    parser.add_argument("--file", required=True, help="Target Python file.")
    parser.add_argument("--func", required=True, help="Target function name.")
    parser.add_argument("--repo", required=True, help="Repo root to mine.")
    parser.add_argument("--output", default=None,
                        help="Write sketch here (default: stdout).")
    parser.add_argument("--max-sites", type=int, default=10,
                        help="Call sites quoted in the sketch (default: 10).")
    args = parser.parse_args(argv)

    try:
        signature, sites = mine(args.repo, args.file, args.func)
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
    sketch = render_sketch(args.file, args.func, signature, sites, args.max_sites)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(sketch)
        print(f"Wrote harness sketch to {args.output} "
              f"({len(sites)} call site(s) mined -- REVIEW before use).")
    else:
        print(sketch)


if __name__ == "__main__":
    main()
