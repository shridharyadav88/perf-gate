# Files

- [Big-O profiling](big-o.md) - Documents the importable and CLI Big-O estimator — multi-target resolution, single-list adapter binding, n-timings noise damping, per-target timeout budget, output format, and failure behavior.
- [Static detectors and bytecode audit](detectors.md) - Documents the detectors/ package — static_audit AST checks, bytecode_audit load counts, per-pattern tier0 detectors, reachability call graph, and the shared per-file AST context.
- [Concurrency and memory gates](gates.md) - Documents the tier-aware concurrency gate (GIL-resurrection on enhanced, informational on baseline), the universal memory ceiling gate, and tracemalloc snapshot helpers.
- [Line profiling](line-profile.md) - Documents signature-aware synthetic invocation and line_profiler output for the bundled line profiling helper.
- [Code optimizer skill](overview.md) - Describes the packaged code-optimizer agent contract — the five-phase profiling-to-fix workflow, tier dispatch, quality gate, report handoff, and the tier0/tier1/tier2 fix lanes.
- [Baseline, classify, render, and regression pipeline](pipeline.md) - Documents the deterministic baseline-CSV → classify → render-action-list → regression-gate pipeline, provenance headers, confirmation hysteresis, heat lane, ruff-perf dedup, and CI templates.
- [Optimization report contract](reporting.md) - Defines the Markdown report template used to preserve profiling baselines, refactor evidence, quality gates, and the final keep-or-rollback decision.
- [Tier0 resolvers and apply-and-verify](resolvers.md) - Documents the resolvers/ package — each applier's re-parse-and-verify safety proof, the apply_and_verify measure-apply-remeasure-keep loop, and the tier→resolver routing table.
- [Tier detection and shared Finding](tier-detection.md) - Documents tier_gate.py — detect_python_tier build-flag keying, requires_tier bidirectional skip, the Finding envelope for process gates, fingerprint stable identity, and normalize_detail.
