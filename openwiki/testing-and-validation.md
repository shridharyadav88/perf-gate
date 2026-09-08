---
type: testing guide
title: Testing and validation
description: Maps behavioral tests to package contracts and gives narrow checks for source changes, packaging artifacts, and operational documentation.
tags: [testing, validation, packaging, quality]
---

# Testing and validation

## Test ownership

`tests/test_cli.py` owns the installer contract: target creation, complete skill layout, default destination, timestamped backup, force replacement, dry-run non-mutation, selection and unknown-name behavior, manifest fields, and `main` flags. `tests/test_profiling_scripts.py` loads the packaged scripts through `importlib.resources` and owns Big-O fitting/formatting, dynamic loading errors, line-profiler output, signature synthesis, explicit invocation, and graceful failure.

The profiling tests use temporary synthetic Python modules, so they exercise behavior without depending on repository application code. Their assertions establish stable output banners and error semantics rather than exact timing values.

## Narrow validation

- Installer changes: `pytest -q tests/test_cli.py`
- Profiling changes: `pytest -q tests/test_profiling_scripts.py`
- Full unit suite: `pytest -q`
- Lint: `ruff check .`
- Build: `python3 -m build`

For a packaging change, install the newly built wheel into a clean temporary environment and verify the console script plus all four deployed assets: `SKILL.md`, `scripts/run_big_o.py`, `scripts/run_line_profile.py`, and `templates/report_template.md`. Also inspect `dist/` and the generated `src/project_code_optimization.egg-info/SOURCES.txt` and `entry_points.txt`; these are generated contracts that reveal omitted package data or a stale console entry point. Source-tree tests alone can pass even if a wheel omits resources.

## Validation boundaries

`big-O` and `line-profiler` are runtime dependencies with upper bounds; profiling tests require them installed. `pytest` is a development extra and target-repository quality gate, not an installer runtime dependency. Performance measurements are inherently variable, so preserve output and compare equivalent measurement parameters. Do not commit generated `.agents/` deployments in a consuming repository.
