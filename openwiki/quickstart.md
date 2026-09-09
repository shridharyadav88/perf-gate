---
type: wiki guide
title: Project code optimization wiki
description: Entry point for understanding, using, and safely changing the project-code-optimization package and its deployed code-optimizer skill.
tags: [quickstart, navigation, project-code-optimization]
---

# Project code optimization wiki

`project-code-optimization` packages one Agent Skill for Python performance work. The package builds a wheel, exposes `install-agent-skills` and the importable `project_code_optimization.auditing` surface, and copies the bundled `code-optimizer` resource tree into a consuming repository's `.agents/skills/` directory. The deployed skill is a five-phase, tier-aware, deterministic pipeline: tier detection, parallel baseline profiling, bottleneck identification, a corrective optimization loop, and tiered fix dispatch from a baseline CSV. Start with [architecture](architecture/overview.md), then follow the owning page for the change you intend to make.

## Map of the wiki

- [Architecture and runtime flows](architecture/overview.md) — package boundaries and deployment-to-agent sequence.
- [Registry and packaging](package/registry-and-packaging.md) — `BUNDLED_SKILLS`, `pyproject.toml`, package data, generated distribution metadata, and adding a skill.
- [Installer CLI](package/installer.md) — `install_skills` and `main`, selection, dry-run, backups, force replacement, and failure semantics.
- [Auditing surface](package/auditing.md) — `@audit_performance` and `track_memory` local-integration decorators with totality-by-construction reliability.
- [Code optimizer skill](skills/code-optimizer/overview.md) — Agent Skills manifest and five-phase workflow (target → baseline → diagnose → refactor → tiered dispatch → report).
- [Tier detection](skills/code-optimizer/tier-detection.md) — `detect_python_tier`, `requires_tier`, `Finding`, fingerprints, and provenance.
- [Big-O profiler](skills/code-optimizer/big-o.md) — `profile_big_o`, multi-target resolution, adapter binding, `n-timings`, timeout budget, output, and CLI errors.
- [Line profiler](skills/code-optimizer/line-profile.md) — signature synthesis, `LineProfiler`, explicit JSON arguments, fallback, and graceful failure.
- [Gates](skills/code-optimizer/gates.md) — concurrency (GIL-resurrection) and memory-ceiling gates with tier-aware exit codes.
- [Detectors](skills/code-optimizer/detectors.md) — static AST and bytecode audit plus the nine tier0 pattern detectors.
- [Resolvers](skills/code-optimizer/resolvers.md) — mechanical tier0 appliers with check-then-act re-verification, and `apply_and_verify` keep-or-revert.
- [Pipeline](skills/code-optimizer/pipeline.md) — `generate_baseline_csv` → `classify_findings` → `render_action_list` → `regression_gate`, plus heat, reachability, ruff PERF, callsite mining, and tier1 self-hosted proposal lanes.
- [Optimization reporting](skills/code-optimizer/reporting.md) — report templates, CSV baseline, diagnostics, quality gate, comparison, and verdict schema.
- [Testing and validation](testing-and-validation.md) — focused tests, build checks, artifact validation, and dependency boundaries.

## First-use path

```bash
pip install build
python3 -m build
pip install dist/project_code_optimization-0.1.0-py3-none-any.whl
install-agent-skills
ls .agents/skills/code-optimizer/
```

The default target is `<cwd>/.agents/skills`; use `--target PATH` for another destination. Use `--skill code-optimizer` for an explicit subset, `--dry-run` to preview, `--force` to replace without a backup, and `--version` to inspect the installed package version. A normal replacement moves the old skill to a UTC `code-optimizer.backup-<timestamp>` directory.

To profile a target after deployment:

```bash
python3 .agents/skills/code-optimizer/scripts/profilers/run_big_o.py \
  --file src/my_module.py --func process_records
python3 .agents/skills/code-optimizer/scripts/profilers/run_line_profile.py \
  --file src/my_module.py --func process_records
pytest
```

Both profilers accept `--target-type` (`function`, `file`, `files`, `commit`, `repo`) for multi-target sweeps, `--max-targets` (default 30; `0` disables the cap), `--timeout` (default 120s per target), and `--dry-run`. Use `--call-args '[1000, {"mode": "strict"}]'` for explicit line-profiler positional arguments. Big-O auto-binds multi-parameter functions via an adapter; use a harness module for domain-specific input.

The full deterministic pipeline:

```bash
SK=.agents/skills/code-optimizer/scripts
python3 $SK/generate_baseline_csv.py --target-type file --file src/my_module.py --output baseline.csv
python3 $SK/classify_findings.py --input baseline.csv --output classified.csv
python3 $SK/render_action_list.py --input classified.csv --top 10
python3 $SK/regression_gate.py --baseline base_classified.csv --current head_classified.csv
```

## Task routing

| Intent | Canonical page | Owning source and symbols | Focused tests | Minimal validation |
|---|---|---|---|---|
| Add or rename a bundled skill | [Registry and packaging](package/registry-and-packaging.md) | `src/project_code_optimization/__init__.py:BUNDLED_SKILLS`; `pyproject.toml` package-data | `tests/test_cli.py::TestInstallSkills` | `pytest -q tests/test_cli.py`; build and inspect wheel assets |
| Change install destination or replacement behavior | [Installer CLI](package/installer.md) | `src/project_code_optimization/cli.py:install_skills`, `main` | `tests/test_cli.py::TestInstallSkills`, `TestCLIEntryPoint` | `pytest -q tests/test_cli.py` |
| Change `@audit_performance` / `track_memory` | [Auditing](package/auditing.md) | `src/project_code_optimization/auditing.py` | `tests/test_concurrency_gate.py`, `tests/test_memory_gate.py` | `pytest -q tests/test_concurrency_gate.py tests/test_memory_gate.py` |
| Change tier detection, `Finding`, or provenance | [Tier detection](skills/code-optimizer/tier-detection.md) | `scripts/tier_gate.py`, `scripts/provenance.py` | `tests/test_tier_gate.py`, `tests/test_provenance.py` | `pytest -q tests/test_tier_gate.py tests/test_provenance.py` |
| Change Big-O measurement or output | [Big-O profiler](skills/code-optimizer/big-o.md) | `run_big_o.py:profile_big_o`, `format_output`, `main` | `tests/test_profiling_scripts.py::TestBigO` | `pytest -q tests/test_profiling_scripts.py -k BigO` |
| Change synthetic line-profiler calls | [Line profiler](skills/code-optimizer/line-profile.md) | `run_line_profile.py:synthesize_arguments`, `run_line_profile` | `TestLineProfiler`, `TestSynthesizeArguments` | `pytest -q tests/test_profiling_scripts.py -k 'LineProfiler or SynthesizeArguments'` |
| Change concurrency or memory gates | [Gates](skills/code-optimizer/gates.md) | `profilers/concurrency.py`, `profilers/memory.py` | `tests/test_concurrency_gate.py`, `tests/test_memory_gate.py` | `pytest -q tests/test_concurrency_gate.py tests/test_memory_gate.py` |
| Change static analysis or tier0 pattern detection | [Detectors](skills/code-optimizer/detectors.md) | `detectors/static_audit.py`, `detectors/bytecode_audit.py`, `detectors/regex_hoist_analysis.py`, … | `tests/test_static_audit.py`, `tests/test_bytecode_audit.py`, `tests/test_regex_hoist.py`, … | `pytest -q tests/test_static_audit.py tests/test_bytecode_audit.py` |
| Change mechanical appliers or keep-or-revert | [Resolvers](skills/code-optimizer/resolvers.md) | `resolvers/apply_*.py`, `apply_and_verify.py` | `tests/test_apply_and_verify.py`, `tests/test_resolver_hardening.py` | `pytest -q tests/test_apply_and_verify.py tests/test_resolver_hardening.py` |
| Change baseline CSV, classifier, renderer, or regression gate | [Pipeline](skills/code-optimizer/pipeline.md) | `generate_baseline_csv.py`, `classify_findings.py`, `render_action_list.py`, `regression_gate.py` | `tests/test_generate_baseline_csv.py`, `tests/test_classify_findings.py`, `tests/test_render_action_list.py`, `tests/test_regression_gate.py` | `pytest -q tests/test_pipeline.py` |
| Change agent phases or report evidence | [Skill overview](skills/code-optimizer/overview.md) and [reporting](skills/code-optimizer/reporting.md) | `SKILL.md`, `templates/report_template.md` | Skill behavior is agent-facing; preserve package tests | `pytest -q` plus a representative agent run |
| Change distribution/build behavior | [Registry and packaging](package/registry-and-packaging.md) | `pyproject.toml`, `src/project_code_optimization.egg-info/` generated contracts | CLI resource-layout test | `python3 -m build`; install wheel in a clean environment |

## Safe-change checklist

1. Identify the owning symbol and read its focused tests before editing.
2. Preserve the package-data path and deployed skill tree layout (`SKILL.md`, `scripts/`, `templates/`).
3. Keep installer replacement semantics explicit: dry-run never mutates; normal replacement backs up; force is destructive; copy failure is not rolled back.
4. Keep profiling output banners and failure messages stable because agents and reports use them as evidence boundaries.
5. Tier detection must stay infallible (P1): `detect_python_tier` never raises and the warning emit is guarded so `python -W error` cannot turn it into a crash.
6. Every applier must re-verify its plan against the current bytes at apply time (P3); a `PlanMismatchError` aborts without writing.
7. Each resolver carries four P4 tests: negatives, idempotency, collateral-diff, and exercised revert.
8. Run the narrow test first, then `pytest -q`, `ruff check .`, and `python3 -m build` when the change crosses packaging or distribution boundaries.

## Scope and backlog

No repository component is intentionally deferred. The built-artifact resource check is a validation recipe rather than a checked-in test; add it to CI if distribution regressions become a recurring risk.
