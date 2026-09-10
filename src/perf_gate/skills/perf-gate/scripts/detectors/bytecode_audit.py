"""Bytecode load audit: borrowed/global/attribute load counts per function.

Operates on SOURCE via :func:`compile` -- pure static analysis, no import
side effects (honors the detectors "never executes" contract). For every
named function, counts four load classes from ``dis`` instructions:

* ``borrowed`` -- 3.14+ ``LOAD_FAST_BORROW*`` elided-refcount loads. The
  opcode set is observed from the running interpreter's ``dis.opmap`` at
  call time (empty before 3.14 by construction); nothing here asserts a
  hardcoded opcode name.
* ``plain`` -- regular ``LOAD_FAST`` local loads (refcount traffic).
* ``global`` -- ``LOAD_GLOBAL`` module-dict lookups (multi-cycle).
* ``attr`` -- ``LOAD_ATTR`` / ``LOAD_METHOD`` (MRO + descriptor cost).

Counts are ADVISORY ONLY (warn-level hints, never pass/fail): a function
with only local loads produces no hint at all. There is intentionally no
classifier wiring and no tier escalation -- numbers without execution
evidence do not route fixes.
"""

from __future__ import annotations

import argparse
import dis
import json
import os
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tier_gate import fingerprint as _fingerprint  # noqa: E402

_ATTR_OPS = frozenset({"LOAD_ATTR", "LOAD_METHOD"})


@dataclass
class BytecodeHint:
    """Per-function load counts for one function with dynamic lookups."""

    func_name: str
    lineno: int
    borrowed: int = 0
    plain: int = 0
    global_loads: int = 0
    attr_loads: int = 0
    detail: str = ""

    @property
    def fingerprint(self) -> str:
        """Stable identity (P5): same counts after a line shift, same print."""
        return _fingerprint("bytecode_loads", self.func_name, self.detail)


@dataclass
class BytecodePlan:
    hints: list[BytecodeHint] = field(default_factory=list)


def borrow_opnames(opmap: dict | None = None) -> frozenset:
    """Borrowed-load opcode names observed from *opmap* (default: live one).

    Taking the map as a parameter keeps the derivation testable without a
    3.14 interpreter: pass a synthetic map, get synthetic names back.
    """
    table = dis.opmap if opmap is None else opmap
    return frozenset(name for name in table if name.startswith("LOAD_FAST_BORROW"))


def count_instructions(instructions, borrow_ops: frozenset) -> dict[str, int]:
    """Count load classes over instruction-like objects (duck-typed ``opname``).

    Pure function over any iterable -- feed ``dis.get_instructions`` output
    in production, synthetic ``SimpleNamespace(opname=...)`` lists in tests.
    """
    counts = {"borrowed": 0, "plain": 0, "global": 0, "attr": 0}
    for ins in instructions:
        op = ins.opname
        if op in borrow_ops:
            counts["borrowed"] += 1
        elif op == "LOAD_FAST":
            counts["plain"] += 1
        elif op == "LOAD_GLOBAL":
            counts["global"] += 1
        elif op in _ATTR_OPS:
            counts["attr"] += 1
    return counts


def _iter_func_codes(code: types.CodeType):
    """Yield every named-function code object under *code* (including nested)."""
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            if not const.co_name.startswith("<"):
                yield const
            yield from _iter_func_codes(const)


def analyze_source(source: str, filename: str = "<string>") -> BytecodePlan:
    """Compile *source* (never execute it) and count loads per function."""
    borrow_ops = borrow_opnames()
    plan = BytecodePlan()
    top = compile(source, filename, "exec")
    for code in _iter_func_codes(top):
        counts = count_instructions(dis.get_instructions(code), borrow_ops)
        dynamic = counts["global"] + counts["attr"]
        if dynamic == 0:
            continue  # pure-local function: nothing actionable to say
        plan.hints.append(BytecodeHint(
            func_name=code.co_name,
            lineno=code.co_firstlineno,
            borrowed=counts["borrowed"],
            plain=counts["plain"],
            global_loads=counts["global"],
            attr_loads=counts["attr"],
            detail=(
                f"loads in '{code.co_name}': borrowed={counts['borrowed']} "
                f"plain={counts['plain']} global={counts['global']} "
                f"attr={counts['attr']} ({dynamic} dynamic lookups)"
            ),
        ))
    plan.hints.sort(key=lambda h: (h.func_name, h.lineno))
    return plan


def analyze_file(file_path: str) -> BytecodePlan:
    """Analyze the file at *file_path*. Raises OSError/SyntaxError like siblings."""
    source = Path(file_path).read_text(encoding="utf-8")
    return analyze_source(source, filename=file_path)


def main(argv: list[str] | None = None) -> None:
    """CLI mirroring the static-audit CLI: advisory default, 0/1/2 exits."""
    parser = argparse.ArgumentParser(
        description="Bytecode load audit (borrowed/global/attribute loads per function).",
    )
    parser.add_argument("--files", nargs="+", required=True, help="Python files to audit.")
    parser.add_argument(
        "--strict", action="store_true",
        help="Exit 1 when any hint is found (default: advisory, exit 0).",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit hints as JSON instead of text.",
    )
    args = parser.parse_args(argv)

    plans: dict[str, BytecodePlan] = {}
    harness_errors: list[str] = []
    for path in args.files:
        try:
            plans[path] = analyze_file(path)
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            harness_errors.append(f"{path}: cannot audit ({exc})")

    if args.json:
        print(json.dumps([
            {
                "file": path, "function": h.func_name, "line": h.lineno,
                "check": "bytecode_loads", "detail": h.detail,
                "fingerprint": h.fingerprint,
            }
            for path, plan in plans.items()
            for h in plan.hints
        ], indent=2))
    else:
        for path, plan in plans.items():
            for h in plan.hints:
                print(f"[WARN] {path}:{h.lineno}: {h.detail}")

    for err in harness_errors:
        print(f"Error: {err}", file=sys.stderr)

    if harness_errors:
        sys.exit(2)
    if args.strict and any(plan.hints for plan in plans.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
