---
type: wiki guide
title: Project code optimization wiki
description: Entry point for understanding, using, and safely changing the project-code-optimization package and its deployed code-optimizer skill.
tags: [quickstart, navigation, project-code-optimization]
---

# Project code optimization wiki

`project-code-optimization` packages one Agent Skill for Python performance work. The package builds a wheel, exposes `install-agent-skills`, and copies the bundled `code-optimizer` resource tree into a consuming repository's `.agents/skills/` directory. Start with [architecture](architecture/overview.md), then follow the owning page for the change you intend to make.

## Map of the wiki

- [Architecture and runtime flows](architecture/overview.md) — package boundaries and deployment-to-agent sequence.
- [Registry and packaging](package/registry-and-packaging.md) — `BUNDLED_SKILLS`, `pyproject.toml`, package data, generated distribution metadata, and adding a skill.
- [Installer CLI](package/installer.md) — `install_skills` and `main`, selection, dry-run, backups, force replacement, and failure semantics.
- [Code optimizer skill](skills/code-optimizer/overview.md) — Agent Skills manifest and baseline → diagnose → refactor → quality gate → verify workflow.
- [Big-O profiler](skills/code-optimizer/big-o.md) — `profile_big_o`, generated inputs, fitting, output, and CLI errors.
- [Line profiler](skills/code-optimizer/line-profile.md) — signature synthesis, `LineProfiler`, explicit JSON arguments, fallback, and graceful failure.
- [Optimization reporting](skills/code-optimizer/reporting.md) — report metadata, diagnostics, quality gate, comparison, and verdict schema.
- [Testing and validation](testing-and-validation.md) — focused tests, build checks, artifact validation, and dependency boundaries.
- [OpenWiki operations](workflow/operations.md) — automated documentation workflow, permissions, provider configuration, and PR generation.
- [Connector extension boundary](workflow/connector-boundary.md) — external OpenWiki connector contract, explicitly not implemented by this Python package.

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
python3 .agents/skills/code-optimizer/scripts/run_big_o.py \
  --file src/my_module.py --func process_records
python3 .agents/skills/code-optimizer/scripts/run_line_profile.py \
  --file src/my_module.py --func process_records
pytest
```

Use `--call-args '[1000, {"mode": "strict"}]'` for explicit line-profiler positional arguments. Big-O targets must accept one generated positional argument; use a wrapper for multi-argument functions.

## Task routing

| Intent | Canonical page | Owning source and symbols | Focused tests | Minimal validation |
|---|---|---|---|---|
| Add or rename a bundled skill | [Registry and packaging](package/registry-and-packaging.md) | `src/project_code_optimization/__init__.py:BUNDLED_SKILLS`; `pyproject.toml` package-data | `tests/test_cli.py::TestInstallSkills` | `pytest -q tests/test_cli.py`; build and inspect wheel assets |
| Change install destination or replacement behavior | [Installer CLI](package/installer.md) | `src/project_code_optimization/cli.py:install_skills`, `main` | `tests/test_cli.py::TestInstallSkills`, `TestCLIEntryPoint` | `pytest -q tests/test_cli.py` |
| Change Big-O measurement or output | [Big-O profiler](skills/code-optimizer/big-o.md) | `run_big_o.py:profile_big_o`, `format_output`, `main` | `tests/test_profiling_scripts.py::TestBigO` | `pytest -q tests/test_profiling_scripts.py -k BigO` |
| Change synthetic line-profiler calls | [Line profiler](skills/code-optimizer/line-profile.md) | `run_line_profile.py:synthesize_arguments`, `run_line_profile` | `TestLineProfiler`, `TestSynthesizeArguments` | `pytest -q tests/test_profiling_scripts.py -k 'LineProfiler or SynthesizeArguments'` |
| Change agent phases or report evidence | [Skill overview](skills/code-optimizer/overview.md) and [reporting](skills/code-optimizer/reporting.md) | `SKILL.md`, `report_template.md` | Skill behavior is agent-facing; preserve package tests | `pytest -q` plus a representative agent run |
| Change distribution/build behavior | [Registry and packaging](package/registry-and-packaging.md) | `pyproject.toml`, `src/project_code_optimization.egg-info/` generated contracts | CLI resource-layout test | `python3 -m build`; install wheel in a clean environment |
| Change documentation automation | [OpenWiki operations](workflow/operations.md) | `.github/workflows/openwiki-update.yml` | Workflow run / PR review | Validate YAML and run the workflow with configured secrets |

## Safe-change checklist

1. Identify the owning symbol and read its focused tests before editing.
2. Preserve the package-data path and deployed four-file skill layout.
3. Keep installer replacement semantics explicit: dry-run never mutates; normal replacement backs up; force is destructive; copy failure is not rolled back.
4. Keep profiling output banners and failure messages stable because agents and reports use them as evidence boundaries.
5. Run the narrow test first, then `pytest -q`, `ruff check .`, and `python3 -m build` when the change crosses packaging or distribution boundaries.

## Scope and backlog

No repository component is intentionally deferred. The connector page is a scope boundary only: connector implementation belongs to the external OpenWiki TypeScript repository and `/skills/write-connector/SKILL.md`, not this package. The built-artifact resource check is a validation recipe rather than a checked-in test; add it to CI if distribution regressions become a recurring risk.
