---
type: reporting template
title: Optimization report contract
description: Defines the Markdown report template used to preserve profiling baselines, refactor evidence, quality gates, and the final keep-or-rollback decision.
tags: [reporting, template, optimization, quality-gate]
---

# Optimization report contract

The skill ships three report surfaces; the agent picks the one that fits the request:

- `skills/code-optimizer/templates/report_template.md` — the original fill-in before/after template for a single targeted optimization run.
- `skills/code-optimizer/templates/baseline_report_template.md` — for baseline-only assessments (no refactor), where every finding is listed most-severe to least-severe using the same complexity-rank-then-hotspot-magnitude criteria the scripts use to order multi-target output.
- `generate_baseline_csv.py` — a CSV (one row per resolved function, pre-sorted most-to-least severe) when the goal is ranked findings as data rather than prose. Columns: `rank, severity, file, function, empirical_big_o, complexity_rank, big_o_error, total_hits, total_hotspot_time_us, top_hotspot_pct_time, top_hotspot_line, top_hotspot_source, line_profile_error`. See [pipeline](pipeline.md).

Each is a fill-in template or deterministic emitter, not executable code. The agent must retain every section and replace placeholders with the current run's evidence; absent data is `N/A`.

## Required evidence

The header records date, repository, target file/function, commit, and tool versions. The summary compares empirical Big-O, total line hits, and unit-test status before and after. Baseline diagnostics preserve raw Big-O output and the top ten line-profile hotspots. The optimization table records before/after snippets and expected complexity effect. The Quality Gate preserves raw pytest output and explicitly checks that tests passed before the performance rerun and that no new failures were introduced.

The before/after section repeats complexity class and hotspot metrics for retrieval. The final recommendation is `KEEP` when performance improves and tests pass, `ROLLBACK` when there is no measurable improvement or tests break, or `NEXT STEP` when another iteration is justified. This output is produced by the agent following [the skill workflow](overview.md); the Python package does not generate or validate reports.

## Safe use

Keep failing-test tracebacks verbatim. Do not claim an empirical improvement without comparable inputs and tool settings. A report with a missing measurement should say `N/A`, preserving the schema for downstream readers.
