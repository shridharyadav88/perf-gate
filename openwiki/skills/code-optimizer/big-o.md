---
type: profiling component
title: Big-O profiling
description: Documents the importable and CLI Big-O estimator, its generated-input contract, output format, and failure behavior.
tags: [profiling, big-o, cli, diagnostics]
---

# Big-O profiling

`skills/code-optimizer/scripts/profilers/run_big_o.py` exposes `load_module(file_path)`, `profile_big_o(func, min_n=100, max_n=10000, n_measures=8)`, `format_output(best_fit, fitted)`, and `main(argv)`. The script dynamically loads a target Python file under `_big_o_target_module`, resolves the requested function, and delegates empirical fitting to `big_o.big_o`.

## Data and fitting contract

`profile_big_o` expects a callable accepting one positional generated input. It wraps `big_o.datagen.integers` with `functools.partial(..., min_=0, max_=1_000_000)` because current `big-O` versions require those generator parameters. It passes the configured measurement bounds to `big_o.big_o` and returns a stringified best-fit class plus a string-keyed fitted-model dictionary. Library exceptions propagate from the core function so callers can decide presentation.

The formatter emits `=== BIG-O PROFILER OUTPUT ===`, `Estimated Complexity:`, and one `Fitted Models:` line per model. `main` parses `--file`, `--func`, `--min-n`, `--max-n`, and `--n-measures`, calls `load_module`, then `getattr(module, args.func, None)`, then `profile_big_o`, and finally prints `format_output` to stdout. A missing file is reported as `Error: File not found: '<path>'`; an unresolved function is `Error: Function '<name>' not found in '<path>'.`; a profiling exception is `Error: Big-O profiling failed: <exception>` followed by the single-positional-argument hint. Each path writes to stderr and calls `sys.exit(1)`; successful output is printed to stdout. `--min-n`, `--max-n`, and `--n-measures` tune measurement cost.

## Usage and limitations

```bash
python3 .agents/skills/code-optimizer/scripts/profilers/run_big_o.py \
  --file src/my_module.py --func process_records \
  --min-n 100 --max-n 10000 --n-measures 8
```

The target must accept one generated list-like argument. Multi-argument functions need a wrapper or the line profiler's explicit argument support. Empirical class names are model fits, not proof of a theoretical bound.

## Focused tests

`tests/test_profiling_scripts.py::TestBigO` checks a synthetic linear function produces a non-empty complexity result and fitted dictionary, missing files raise `ValueError`, missing attributes remain observable, and formatted output contains the canonical banner and sections. Focused CLI validation for `main` is one case each for a nonexistent `--file` (stderr `File not found`, status 1), an existing file with nonexistent `--func` (stderr `Function ... not found`, status 1), and a callable that raises or violates the one-argument contract (stderr `Big-O profiling failed` plus hint, status 1). The checked-in `TestBigO` suite covers the importable core and does not assert subprocess exit streams; run `pytest -q tests/test_profiling_scripts.py -k BigO`, then execute these three CLI cases when validating wrapper changes.
