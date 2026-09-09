---
type: testing guide
title: Testing and validation
description: Maps behavioral tests to package contracts and gives narrow checks for source changes, packaging artifacts, and operational documentation.
tags: [testing, validation, packaging, quality]
---

# Testing and validation

## Test ownership

The suite (`pytest`, 546+ tests) is organized by component rather than by file:

- **Installer** — `tests/test_cli.py` owns target creation, complete skill layout, default destination, timestamped backup, force replacement, dry-run non-mutation, selection and unknown-name behavior, manifest fields, and `main` flags.
- **Profiling scripts** — `tests/test_profiling_scripts.py` loads the packaged scripts through `importlib.resources` and owns Big-O fitting/formatting, dynamic loading errors, line-profiler output, signature synthesis, explicit invocation, and graceful failure. `tests/test_call_compat.py` covers the multi-parameter adapter binding and call-shape probing.
- **Tier detection and provenance** — `tests/test_tier_gate.py` (build-flag detection, bidirectional `requires_tier`, `Finding` envelope, fingerprint stability) and `tests/test_provenance.py` (schema-version assertions, cross-tier/cross-schema refusal).
- **Gates** — `tests/test_concurrency_gate.py` (mocked-tier both branches, skip envelope) and `tests/test_memory_gate.py` (threshold boundaries incl. `baseline <= 0`, universal default).
- **Detectors** — `tests/test_static_audit.py`, `tests/test_bytecode_audit.py`, `tests/test_regex_hoist.py`, `tests/test_re_call.py`, `tests/test_invariant_hoist.py`, `tests/test_perf_comprehension.py`, `tests/test_accumulator.py` (detection + near-miss negatives per detector).
- **Resolvers** — `tests/test_resolver_hardening.py` (P3 fail-closed, P4 four tests per resolver) and `tests/test_apply_and_verify.py` (keep-or-revert loop, reverted byte-identity).
- **Pipeline** — `tests/test_pipeline.py`, `tests/test_generate_baseline_csv.py`, `tests/test_classify_findings.py`, `tests/test_render_action_list.py`, `tests/test_regression_gate.py`, `tests/test_profile_heat.py`, `tests/test_reachability.py`, `tests/test_ruff_perf.py`.
- **Confirmation and timeouts** — `tests/test_confirmation.py` (margin + tier2 hysteresis) and `tests/test_timeouts.py` (budget breach → error row, not a hang).
- **Tier1 proposals and callsite mining** — `tests/test_tier1_propose.py` (self-hosted model verification) and `tests/test_big_o_evidence.py` (callsite mining + probe-log provenance/residuals).
- **Auditing** — `tests/test_auditing.py` owns the `@audit_performance` / `track_memory` totality contract (inner failures contained, wrapped result unaffected, strict-mode raise, disabled-overhead). `tests/test_concurrency_gate.py` and `tests/test_memory_gate.py` cover the mirrored gate semantics.

The profiling and pipeline tests use temporary synthetic Python modules, so they exercise behavior without depending on repository application code. Their assertions establish stable output banners, error semantics, fingerprint stability, and provenance contracts rather than exact timing values.

## Narrow validation

- Installer changes: `pytest -q tests/test_cli.py`
- Profiling changes: `pytest -q tests/test_profiling_scripts.py`
- Tier detection / provenance: `pytest -q tests/test_tier_gate.py tests/test_provenance.py`
- Gates: `pytest -q tests/test_concurrency_gate.py tests/test_memory_gate.py`
- Detectors: `pytest -q tests/test_static_audit.py tests/test_bytecode_audit.py tests/test_regex_hoist.py`
- Resolvers: `pytest -q tests/test_apply_and_verify.py tests/test_resolver_hardening.py`
- Pipeline: `pytest -q tests/test_pipeline.py`
- Full unit suite: `pytest -q`
- Lint: `ruff check .`
- Build: `python3 -m build`

For a packaging change, install the newly built wheel into a clean temporary environment and verify the console script plus the deployed tree: `SKILL.md`, `scripts/` (`profilers/`, `detectors/`, `resolvers/`, tier gate, classify, render, etc.), and `templates/` (`report_template.md`, `baseline_report_template.md`, CI examples). Also inspect `dist/` and the generated `src/project_code_optimization.egg-info/SOURCES.txt` and `entry_points.txt`; these are generated contracts that reveal omitted package data or a stale console entry point. Source-tree tests alone can pass even if a wheel omits resources.

## Validation boundaries

`big-O` and `line-profiler` are runtime dependencies with upper bounds; profiling tests require them installed. `pytest` is a development extra and target-repository quality gate, not an installer runtime dependency. `ruff` is a development extra used by the PERF detection lane (`ruff_perf.py`) and the repo lint gate. Performance measurements are inherently variable, so preserve output and compare equivalent measurement parameters. Do not commit generated `.agents/` deployments in a consuming repository. The `tier1_propose.py` self-hosted lane talks to a local Ollama server (`$OLLAMA_HOST`); it is not run in CI and requires no cloud tokens.
