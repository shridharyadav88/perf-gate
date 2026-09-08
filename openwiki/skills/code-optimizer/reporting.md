---
type: reporting template
title: Optimization report contract
description: Defines the Markdown report template used to preserve profiling baselines, refactor evidence, quality gates, and the final keep-or-rollback decision.
tags: [reporting, template, optimization, quality-gate]
---

# Optimization report contract

`skills/code-optimizer/templates/report_template.md` is a fill-in template, not executable code. The agent must retain every section and replace placeholders with the current run's evidence; absent data is `N/A`.

## Required evidence

The header records date, repository, target file/function, commit, and tool versions. The summary compares empirical Big-O, total line hits, and unit-test status before and after. Baseline diagnostics preserve raw Big-O output and the top ten line-profile hotspots. The optimization table records before/after snippets and expected complexity effect. The Quality Gate preserves raw pytest output and explicitly checks that tests passed before the performance rerun and that no new failures were introduced.

The before/after section repeats complexity class and hotspot metrics for retrieval. The final recommendation is `KEEP` when performance improves and tests pass, `ROLLBACK` when there is no measurable improvement or tests break, or `NEXT STEP` when another iteration is justified. This output is produced by the agent following [the skill workflow](overview.md); the Python package does not generate or validate reports.

## Safe use

Keep failing-test tracebacks verbatim. Do not claim an empirical improvement without comparable inputs and tool settings. A report with a missing measurement should say `N/A`, preserving the schema for downstream readers.
