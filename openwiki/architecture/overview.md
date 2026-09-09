---
type: architecture overview
title: Package architecture and runtime flows
description: Explains how the Python package, packaged skill resources, installer, profiling scripts, and target repository fit together.
tags: [architecture, packaging, installer, profiling]
---

# Package architecture and runtime flows

`project-code-optimization` is a `src/`-layout Python distribution whose public runtime is the `install-agent-skills` console script plus the importable `project_code_optimization.auditing` local-integration surface. The package owns a registry of bundled skills; the registry and setuptools package-data rule make the `skills/` tree available after installation. The installer copies that resource tree into a consuming repository, where an agent reads `SKILL.md` and invokes the profiling, detection, and resolver scripts.

## Ownership and boundaries

- `src/project_code_optimization/__init__.py` owns version fallback and the `BUNDLED_SKILLS` registry.
- `src/project_code_optimization/cli.py` owns deployment policy, filesystem mutation, logging, and CLI parsing.
- `src/project_code_optimization/auditing.py` owns the importable `@audit_performance` and `track_memory` local-integration surface (totality by construction, zero coupling to the skill tree).
- `skills/code-optimizer/SKILL.md` owns the five-phase agent procedure; `scripts/` owns [tier detection](../skills/code-optimizer/tier-detection.md), [detectors](../skills/code-optimizer/detectors.md), [resolvers](../skills/code-optimizer/resolvers.md), the profiling and gate scripts, and the [baseline-classify-render-regression pipeline](../skills/code-optimizer/pipeline.md); templates own output structure.
- `pyproject.toml` owns the build metadata, dependency bounds, console entry point, and inclusion of non-Python skill files.
- `tests/` validates installer behavior, profiling behavior, every detector and resolver (four P4 tests each), the pipeline, gates, provenance, and regression gate; the target repository's tests are a quality gate described by the skill, not executed by this package automatically.

```mermaid
sequenceDiagram
    participant Build as Build backend
    participant Package as Installed package resources
    participant CLI as install-agent-skills
    participant Repo as Target repository
    participant Agent as Coding agent
    participant Profiler as Profiling scripts
    Build->>Package: Include skills tree and console metadata
    CLI->>Package: Resolve skills with importlib.resources
    CLI->>Repo: Copy selected skill to .agents/skills
    Agent->>Repo: Read deployed SKILL.md
    Agent->>Profiler: Run Big-O and line-profile commands
    Profiler-->>Agent: Diagnostics
    Agent->>Repo: Run pytest and apply bounded refactor loop
```
Caption: Installed resources flow through the deployer before the consuming agent runs diagnostics.

## Deployment lifecycle

The default destination is `<cwd>/.agents/skills`; `--target` changes it. A selected skill must have `SKILL.md` before it is copied. Existing destinations are moved to a UTC timestamped backup unless `--force` is supplied. `--dry-run` reports intended actions and returns selected names without creating the target. See [the installer contract](../package/installer.md) and [packaging ownership](../package/registry-and-packaging.md).

## Change navigation

Change the registry or build rules in [registry and packaging](../package/registry-and-packaging.md); change deployment semantics in [installer](../package/installer.md); change the importable audit surface in [auditing](../package/auditing.md); change agent behavior in [skill overview](../skills/code-optimizer/overview.md); change tier detection in [tier detection](../skills/code-optimizer/tier-detection.md); change static analysis in [detectors](../skills/code-optimizer/detectors.md); change mechanical fixes in [resolvers](../skills/code-optimizer/resolvers.md); change gate behavior in [gates](../skills/code-optimizer/gates.md); change the baseline/classify/render/regression flow in [pipeline](../skills/code-optimizer/pipeline.md); change diagnostics in [Big-O](../skills/code-optimizer/big-o.md) or [line profiling](../skills/code-optimizer/line-profile.md).
