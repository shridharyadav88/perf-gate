---
type: gate component
title: Concurrency and memory gates
description: Documents the tier-aware concurrency gate (GIL-resurrection on enhanced, informational on baseline), the universal memory ceiling gate, and tracemalloc snapshot helpers.
tags: [gate, concurrency, memory, gil, tracemalloc, tier-aware]
---

# Concurrency and memory gates

Two tier-aware process gates run after [tier detection](tier-detection.md) and before [baseline profiling](big-o.md): a concurrency gate and a memory ceiling gate. Both return a [Finding](tier-detection.md) envelope and follow the gate exit-code contract (0 pass/warn/skipped, 1 finding-fail, 2 harness error).

## Concurrency gate

`skills/code-optimizer/scripts/profilers/concurrency.py` exposes `audit_concurrency(active_tier=None) -> Finding`. On the enhanced tier (a confirmed free-threaded build) this is the GIL-resurrection gate: `audit_gil_resurrection` checks `sys._is_gil_enabled()` (missing API fails closed — `True`, so `fail`) and returns `fail` with `SCALING_LOCKED` when a legacy C-extension without `Py_mod_gil = Py_MOD_GIL_NOT_USED` has silently re-enabled the GIL process-wide, or `pass` with `TRUE_PARALLEL` when the GIL is disabled. On the baseline tier `audit_baseline_concurrency` returns an informational `pass` with `concurrency_baseline` (the GIL is permanently active; consider multiprocessing or GIL-release C-extension sections). The check names are distinct (`gil_resurrection` vs `concurrency_baseline`) — a GIL build can never resurrect, and the name must not imply otherwise. `audit_concurrency` does exactly one `detect_python_tier()` call and passes the tier down explicitly.

## Memory ceiling gate

`skills/code-optimizer/scripts/profilers/memory.py` exposes `memory_ceiling_gate(baseline_rss, current_rss, max_growth=0.02) -> Finding` (pure over numbers — trivially testable with synthetic values). The ceiling is a single universal threshold (`DEFAULT_MAX_GROWTH = 0.02`, 2%): 0.5% would be unsound on the enhanced tier (QSBR-deferred reclamation and mimalloc's reluctance to return freed pages read as growth), and the gate accepts arbitrary numbers (RSS or `tracemalloc`), so the default must be safe for either signal on every tier. `baseline_rss <= 0` returns `skipped` (cannot compute a growth ratio). `growth = (current - baseline) / baseline`; `pass` when `growth <= max_growth`, else `fail`. `--max-growth` tightens per-pipeline once p95 data is measured (uncalibrated starting point; the p95 procedure is in `SKILL.md`).

`ensure_tracemalloc(nframe=1)` starts `tracemalloc` idempotently if it is not already tracing. `current_traced_bytes(collect_first=False)` returns currently-traced Python heap bytes; pass `collect_first=True` on the enhanced tier so `gc.collect()` frees deferred cyclic garbage before measuring (otherwise deferred frees read as growth — collection happens in the measurement helper, not the gate, because collecting after the numbers were measured cannot change the ratio).

## Focused tests

`tests/test_concurrency_gate.py` owns mocked-tier tests for both branches (`enhanced` → `gil_resurrection` `fail`/`pass`; `baseline` → `concurrency_baseline` `pass`), the missing-`_is_gil_enabled` fail-closed, and the skip-envelope on mismatch. `tests/test_memory_gate.py` owns the threshold-boundary tests (`baseline <= 0` skip, universal default, `pass`/`fail` boundary), the snapshot-helper round-trip, and the enhanced-tier `collect_first` path. Run `pytest -q tests/test_concurrency_gate.py tests/test_memory_gate.py`.

## Change navigation

<!-- openwiki: broken internal link [../package/auditing.md] file "../package/auditing.md" does not exist. Fix the href or restore the target, then delete this comment. -->
The concurrency probe is mirrored in [auditing](../package/auditing.md) — change both together if the tier-aware semantics change. Keep `DEFAULT_MAX_GROWTH` in sync with `SKILL.md`'s documented threshold and the `--max-growth` flag default. The gate stays pure over numbers; `gc.collect()` lives in the measurement helper, not the gate.
