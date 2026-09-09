---
type: detectors component
title: Static detectors and bytecode audit
description: Documents the detectors/ package — static_audit AST checks, bytecode_audit load counts, per-pattern tier0 detectors, reachability call graph, and the shared per-file AST context.
tags: [detectors, static-analysis, ast, bytecode, reachability, tier0]
---

# Static detectors and bytecode audit

`skills/code-optimizer/scripts/detectors/` contains the static analysis modules that feed [classify_findings](pipeline.md) and the [resolvers](resolvers.md). All detectors are pure/read-only — they never write a file, never import or call target code — and produce plan dataclasses (not flat `Finding` lists) because resolvers need structured fields.

## static_audit.py

`detectors/static_audit.py` exposes `analyze_source(source, filename)` / `analyze_file(file_path)` returning an `AuditPlan` of warn-level hints. Two checks, both version-agnostic: `nested_loop` (a `for`/`async for`/`while` loop nested inside another loop — O(N*M) scaling risk) and `attr_cache` (a read of `<root>.<attr>` inside a loop where `root` is a known module/global alias; default `math`/`np`/`sys`, configurable). Deliberately not flagged (negative tests): attribute access on locals/arguments/loop variables, stores/deletes (recorded in `skipped`, never a hint), and loops inside a nested `def` (attributed to the inner function). Static hints are advisory only — they never escalate a finding's tier by themselves.

## bytecode_audit.py

`detectors/bytecode_audit.py` operates on source via `compile()` (pure static, no import side effects) and counts four load classes per function from `dis` instructions: `borrowed` (3.14+ `LOAD_FAST_BORROW*`), `plain` (`LOAD_FAST`), `global` (`LOAD_GLOBAL`), and `attr` (`LOAD_ATTR`/`LOAD_METHOD`). The opcode set is observed from the running interpreter's `dis.opmap` at call time (empty before 3.14 by construction); nothing asserts a hardcoded opcode name. Counts are advisory only (warn-level, never pass/fail); there is no classifier wiring and no tier escalation. CLI mirrors the static-audit CLI (`--files`, `--strict` exits 1 on hints, `--json` emits fingerprints for diffing).

## Tier0 per-pattern detectors

Each tier0 tier has a paired detector returning a plan dataclass with a `skipped` list (the FP budget is visible, not hidden):

- `regex_hoist_analysis.py` — `HoistCandidate`/`HoistPlan`: a function-local `re.compile(...)` that does not reference the function's parameters or locals is safe to hoist to module scope.
- `re_call_analysis.py` — `ReCallPlan`: a module-level `re.<method>(...)` call with a constant pattern is safe to rewrite to a hoisted compiled call.
- `perf_comprehension_analysis.py` — `RewritePlan`: manual list-copy (`out = []` + append loop → `list(items)`), list-build (→ comprehension), and dict-build (`d = {}` + assign loop → `dict(...)`/comprehension) loops.
- `accumulator_analysis.py` — `AccumulatePlan`: string-concat (`s += ...`), sum-accumulate (`total += ...`), and set-build (`seen.add(...)`) loops.
- `invariant_hoist_analysis.py` — `HoistPlan`: a loop-invariant module-global load is safe to hoist to a pre-loop local (names only; attribute loads stay report-only).

## reachability.py

`detectors/reachability.py` builds a conservative static call graph (stdlib AST only — names plus module-basename correlation) and reports per function: how many in-repo callers it has and whether any entry point can reach it. Entry points: `if __name__ == "__main__"` guards, functions named `main`, and click commands/groups (`@*.command()`, `@*.group()`). A function with zero in-repo callers is a neutral fact ("no in-repo callers") — libraries legitimately expose uncalled public APIs — with a stronger "likely dead" tag reserved for unreached private names. The graph cannot see dynamic dispatch or framework wiring, so treat reachability as a triage hint, never a deletion license.

## Shared per-file AST context (P8)

`classify_findings.py` analyzes one file once and shares its tier0 plan across every row for that file, so a file with two functions sharing a hoistable pattern does not get evaluated twice or produce inconsistent answers per-row. New detectors share one parsed AST per file (analysis context passed down, not re-parsed per detector) — repo-wide scans must not be O(detectors × files).

## Focused tests

- `tests/test_static_audit.py`, `tests/test_bytecode_audit.py` — detection, negative/near-miss, graceful degradation on baseline, nested-function attribution.
- `tests/test_regex_hoist.py`, `tests/test_re_call.py`, `tests/test_perf_comprehension.py`, `tests/test_accumulator.py`, `tests/test_invariant_hoist.py` — the four P4 classes per detector: negatives, idempotency, collateral-diff, exercised revert.
- `tests/test_reachability.py` — caller counts, entry-point reachability, likely-dead tagging.
- `tests/test_target_resolution.py`, `tests/test_max_targets.py` — target discovery, excluded dirs, `--max-targets` cap.

Run `pytest -q tests/test_static_audit.py tests/test_bytecode_audit.py tests/test_regex_hoist.py tests/test_re_call.py tests/test_perf_comprehension.py tests/test_accumulator.py tests/test_invariant_hoist.py tests/test_reachability.py tests/test_target_resolution.py tests/test_max_targets.py`.

## Change navigation

Adding a tier0 tier requires a detector here, a resolver in [resolvers](resolvers.md), and wiring in `classify_findings._TIER0_PRIORITY` and `render_action_list.TIER0_PRIORITY`/`TIER0_RESOLVER` (keep the two priority tuples identical; `test_pipeline.py` pins this). Each detector must carry the four P4 tests. Keep the "never executes" contract: detectors read source via AST/`compile()` only and never import or call target code.
