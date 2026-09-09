---
type: profiling component
title: Big-O profiling
description: Documents the importable and CLI Big-O estimator — multi-target resolution, single-list adapter binding, n-timings noise damping, per-target timeout budget, output format, and failure behavior.
tags: [profiling, big-o, cli, diagnostics, target-resolution]
---

# Big-O profiling

`skills/code-optimizer/scripts/profilers/run_big_o.py` exposes `load_module(file_path)`, `profile_big_o(func, min_n=100, max_n=10000, n_measures=8, n_timings=5, n_repeats=1, timeout=None, probe_log=None)`, `format_output(best_fit, fitted)`, `measure_verdict_us(func, ...)`, and `main(argv)`. The script dynamically loads a target Python file under `_big_o_target_module` (package-aware: relative and intra-repo absolute imports resolve via stub parent packages; `__init__` chains never execute), resolves the requested function, and delegates empirical fitting to `big_o.big_o`.

## Multi-target resolution

`--target-type` controls scope: `function` (default, `--file`/`--func`), `file` (every module-level function in one file), `files` (`--files`, repeatable), `commit` (`--commit`, optional `--repo`; functions in the `.py` files a commit touched, profiled as they exist in the working tree now), or `repo` (optional `--repo`, default `.`). `--max-targets` (default 30; 0 disables the count cap) bounds the resolved pairs; the per-target `--timeout` (default 120s) remains the guard. Multi-target output is sorted most-severe-first by empirical complexity rank (worse growth first). `--dry-run` lists resolved targets and exits before importing or calling anything. Only module-level `def`/`async def` functions are discoverable; methods and nested functions are out of scope. Target resolution lives in `profilers/target_resolution.py`; excluded dirs and entry-point prefixes are documented there.

## Adapter binding and data contract

`profile_big_o` no longer requires a single-positional-argument callable. `_build_adapter(func)` binds multi-parameter functions to `big_o`'s single-list convention: it inspects the signature, identifies the size parameter (first undefaulted, preferably list/str-shaped/annotated-scalable), scales it with `len(data)` via `_adapt_data`, and fixes the rest (defaults honored, else annotation-plausible values). It returns a list of `(adapter, shape_desc)` candidates — at most a dozen tiny probe calls, first success wins. Click commands and zero-argument functions fail fast with a clear message (`'...' is a CLI command, not a scalable function` / `'...' takes no arguments`). Coroutine functions run via `asyncio.run` instead of timing coroutine creation. The optional `probe_log` dict is filled with `provenance` (`"harness"` only when the measured function carries `__big_o_provenance__ == "harness"` — set by approved harness sketches from `mine_callsites.py` — else `"synthetic"`), the winning `shape_desc`, the measurement window, and per-class `residuals` (non-finite stored as `None`).

`data_gen` wraps `big_o.datagen.integers` with `functools.partial(..., min_=0, max_=1_000_000)` because current `big-O` versions require those generator parameters. `n_timings` (default 5) repeats each measurement and keeps the minimum (filtering GC/scheduler jitter); `n_repeats` (default 1) calls `func` multiple times per measurement and sums the time. Raise both for a report that will make a specific complexity claim.

## Per-target timeout budget (P2)

The whole fit runs under `timeouts.run_with_timeout(_fit, timeout)`. A hanging or pathologically slow target raises `TimeoutBudgetExceeded` (a `TimeoutError` subclass), which callers translate into an error row rather than a hung run. The worker thread is abandoned on timeout, not killed (Python cannot kill threads); isolate genuinely untrusted code at the process level.

## Output and failure behavior

`format_output` emits `=== BIG-O PROFILER OUTPUT ===`, `Estimated Complexity:`, and one `Fitted Models:` line per model. In multi-target mode, `_profile_one` returns a result dict per target so one broken target does not abort the scan; a target that fails to load or profile is still reported with the error. `main` parses `--file`, `--func`, `--target-type`, `--files`, `--commit`, `--repo`, `--max-targets`, `--min-n`, `--max-n`, `--n-measures`, `--n-timings`, `--n-repeats`, `--timeout`, and `--dry-run`. A missing file is `Error: File not found: '<path>'`; an unresolved function is `Error: Function '<name>' not found in '<path>'.`; a profiling exception is `Error: Big-O profiling failed: <exception>` with a harness hint. Each error path writes to stderr and exits status 1 (single-target) or becomes an error row (multi-target); successful output is printed to stdout.

## Usage

```bash
python3 .agents/skills/code-optimizer/scripts/profilers/run_big_o.py \
  --file src/my_module.py --func process_records \
  --min-n 100 --max-n 10000 --n-measures 8 --n-timings 5
# or: --target-type file --file src/my_module.py
# or: --target-type repo --repo . --max-targets 0
```

The target must accept at least one generatable argument; multi-argument functions are handled by the adapter (or use a harness from `mine_callsites.py` for domain-specific input). Empirical class names are model fits, not proof of a theoretical bound.

## Focused tests

`tests/test_profiling_scripts.py::TestBigO` checks a synthetic linear function produces a non-empty complexity result and fitted dictionary, missing files raise `ValueError`, missing attributes remain observable, and formatted output contains the canonical banner and sections. `tests/test_big_o_evidence.py` covers probe-log provenance and residuals. Focused CLI validation for `main` is one case each for a nonexistent `--file` (stderr `File not found`, status 1), an existing file with nonexistent `--func` (stderr `Function ... not found`, status 1), and a callable that raises or violates the argument contract (stderr `Big-O profiling failed` plus hint, status 1). Run `pytest -q tests/test_profiling_scripts.py -k BigO` then `pytest -q tests/test_big_o_evidence.py`; execute the three CLI cases when validating wrapper changes.
