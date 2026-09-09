---
type: resolvers component
title: Tier0 resolvers and apply-and-verify
description: Documents the resolvers/ package — each applier's re-parse-and-verify safety proof, the apply_and_verify measure-apply-remeasure-keep loop, and the tier→resolver routing table.
tags: [resolvers, codemod, apply, verify, tier0, keep-or-revert]
---

# Tier0 resolvers and apply-and-verify

`skills/code-optimizer/scripts/resolvers/` contains the deterministic codemods that apply tier0 mechanical fixes. Each applier is paired with its detector in [detectors](detectors.md) and routed by `render_action_list.TIER0_RESOLVER`.

## Resolver-to-tier routing

| Tier | Resolver |
|---|---|
| `tier0_regex_hoist` | `resolvers/apply_regex_hoist.py` |
| `tier0_re_call` | `resolvers/apply_re_call.py` |
| `tier0_perf402` / `tier0_perf401` / `tier0_perf403` | `resolvers/apply_perf_comprehension.py` |
| `tier0_str_join` / `tier0_sum_reduce` / `tier0_set_build` | `resolvers/apply_accumulator.py` |
| `tier0_invariant_hoist` | `resolvers/apply_invariant_hoist.py` |

## Re-verify at apply time (P3)

Classification and application are separate process invocations; the file may change between them. Every applier re-parses the target bytes, re-derives its own safety proof via `verify_plan(source, plan) -> list[str]`, and fails closed (`PlanMismatchError`, nothing written) on any mismatch. The classifier's output is a hint, never a proof — never apply a plan computed from different bytes.

`apply_regex_hoist.py` deletes every occurrence of a safe-to-hoist `name = re.compile(...)` statement from inside function bodies and inserts one deduplicated copy at module scope (after the docstring/imports). `apply_perf_comprehension.py` only rewrites an `out = []` init directly followed by a single-`append` loop whose iterable, expression, and post-loop reads cannot observe the difference — anything ambiguous is left alone and reported, never guessed at. Nothing in any plan's `skipped` list is ever touched.

## apply_and_verify.py — the measure-apply-remeasure-keep loop

`scripts/apply_and_verify.py` closes the loop for the mechanical lane. For one function it: measures (`measure_verdict_us` via `time.perf_counter` min-of-5 over the winning synthetic call shape), applies the matching resolver, re-measures, and keeps the rewrite only if `confirmation.decide_keep` says the gain clears `--min-improvement` (default 0.10). Anything else restores the original bytes (verified identical) and reports `REVERTED`. Exit codes: 0 = finished with outcome printed (`KEPT`, `REVERTED`, `NO-FIX`, or the `--dry-run` plan); 1 = error (unmeasurable, plan mismatch, apply failure — the file is always left untouched in these cases). `--diff` prints a paste-ready unified diff (stdlib `difflib`) of the kept-or-rejected rewrite.

## Focused tests

- `tests/test_regex_hoist.py`, `tests/test_re_call.py`, `tests/test_perf_comprehension.py`, `tests/test_accumulator.py`, `tests/test_invariant_hoist.py` — the four P4 classes per resolver: negative/near-miss, idempotency (apply-twice is a no-op), collateral-diff (output parses; only intended lines differ), exercised revert.
- `tests/test_resolver_hardening.py` — P3 fail-closed test (mutate file between classify and apply → clean abort, file untouched).
- `tests/test_apply_and_verify.py` — the keep/revert/no-fix loop, `--dry-run`, `--diff`, and the untouched-on-error invariant.

Run `pytest -q tests/test_regex_hoist.py tests/test_re_call.py tests/test_perf_comprehension.py tests/test_accumulator.py tests/test_invariant_hoist.py tests/test_resolver_hardening.py tests/test_apply_and_verify.py`.

## Change navigation

Adding a tier0 tier requires a resolver here, a detector in [detectors](detectors.md), and identical `classify_findings._TIER0_PRIORITY` and `render_action_list.TIER0_PRIORITY`/`TIER0_RESOLVER` entries (`test_pipeline.py` pins the two priority tuples). Each resolver must carry the four P4 tests plus the P3 re-verify test. Keep `apply_and_verify`'s `_filter_plan`, `_analyze`, `_verify`, and `_apply` dispatch tables in sync with the resolver set. The verdict number is `time.perf_counter` min-of-repeats, never the line-profiler hotspot sum (profiler overhead variance flips micro-scale comparisons).
