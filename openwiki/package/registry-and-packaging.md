---
type: packaging contract
title: Registry and packaging
description: Documents the public skill registry, package metadata, generated distribution contracts, and the source-to-installed-resource boundary.
tags: [packaging, registry, distribution, extension]
---

# Registry and packaging

## Public package surface

`project_code_optimization.__version__` reads the installed distribution metadata for `project-code-optimization` and falls back to `0.1.0`. `BUNDLED_SKILLS` is the registry consumed by the installer and tests:

```python
{"code-optimizer": "skills/code-optimizer"}
```

The key is the user-facing skill name and the value is the package-relative resource path. Adding a skill requires a directory with `SKILL.md`, a registry entry, and a version/build update; the installer will then expose it to `--skill NAME` and default installation.

## Build contract

`pyproject.toml` uses `setuptools.build_meta`, discovers packages under `src`, and includes `skills/**/*` as package data. Runtime dependencies are bounded to `big-O>=0.10.0,<0.12` and `line-profiler>=4.1.0,<6`; development tools are optional (`pytest`, `pytest-cov`, `ruff`). The `[project.scripts]` entry maps `install-agent-skills` to `project_code_optimization.cli:main`.

The generated `src/project_code_optimization.egg-info/entry_points.txt` records the same console contract, while `SOURCES.txt` records the Python modules, `SKILL.md`, both scripts, template, tests, and metadata included in the source distribution manifest. These generated files are evidence of the build result, not the primary edit surface.

## Artifact verification

`python3 -m build` should produce the wheel and sdist under `dist/`. Validate the built wheel in a clean environment rather than relying only on source-tree tests: install the wheel, import `project_code_optimization`, resolve `importlib.resources.files("project_code_optimization") / "skills"`, and assert that `SKILL.md`, `scripts/profilers/run_big_o.py`, `scripts/profilers/run_line_profile.py`, and `templates/report_template.md` exist. Run `install-agent-skills --target <temporary-directory>` and verify the copied tree and console script. This catches missing package-data declarations that `tests/test_cli.py` can miss when importing the checkout.

See [testing and validation](../testing-and-validation.md) for the narrow commands and [installer](installer.md) for resource consumption.
