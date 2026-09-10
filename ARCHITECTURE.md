Agentic Performance Optimization Framework (v4): Corrected Tier Detection & Structured Findings

> **As-built status (2026-09-09): this spec is implemented — see Section 12
> for the file-by-file build record and the post-spec additions (heat lane,
> action-list renderer, regression gate, new tier0s, perf_counter verdicts).**

> **Document status: PRODUCT DOCUMENT (build spec).** Sections 1–6 are the
> normative spec. Section 8 lists non-negotiable architectural requirements —
> every work item in Section 9 must satisfy the principles marked as applying
> to it. A work item is done only when all its acceptance criteria pass,
> including the shared gates in Section 9.0. Decisions that needed repo
> access have been resolved by a reviewer with repo access (see Section 7);
> nothing below requires further input before implementation.

Changelog from v3, based on external review

* Tier detection now keys off the actual build (`Py_GIL_DISABLED`), not the version number — fixes misclassification of GIL-enabled 3.14 and free-threaded 3.13t.
* All diagnostics return a stable `Finding` structure instead of `print()`, so they can feed a CSV/classifier pipeline.
* `requires_tier` mismatch check generalized to work in both directions.
* `audit_concurrency()` no longer calls tier detection twice.
* `memory_ceiling_gate` has a zero-guard and a corrected comment about what `gc.collect()` actually does.
* `visit_While` added so attribute-hoisting hints aren't for-loop-only.
* Bytecode opcode matching now derived from `dis.opmap` instead of a `startswith` guess, so it correctly counts the `LOAD_FAST_BORROW_LOAD_FAST_BORROW` super-instruction instead of either missing it or over-matching unrelated opcodes.
* Loop-warning dedup keyed on `(lineno, col_offset)` instead of `id(node)`.
* Pipeline stages renamed from "Phase 1–4" to named stages to avoid colliding with an existing `SKILL.md` numbering — slot these wherever your real phases live.
* Added an explicit "Open Items" section instead of leaving unresolved questions implicit.

Revision 2 (product-document pass, architect review): all open items from the
previous paragraph are now RESOLVED in Section 7 — no input needed before
implementation. Added Section 8 (non-negotiable architectural requirements
P1–P8) and Section 9 (work items W1–W11 with acceptance criteria). `Finding`
envelope scoped to process-level gates only (Section 2); function-level
static checks use per-detector plan dataclasses per existing repo convention.
Added Section 10 (per-stage failure policy) and Section 11 (finding
fingerprints + artifact provenance).

Revision 3 (post-implementation decisions, binding):

* Memory ceiling is a SINGLE universal threshold of 2% (`--max-growth`,
  default 0.02) -- the tier-branched 0.5%/2% pair is removed. Rationale:
  0.5% is unsound on the enhanced tier (QSBR/mimalloc reclaim lag reads as
  growth), and the gate accepts arbitrary numbers (RSS or tracemalloc), so
  the default must be safe for either signal on every supported tier. A
  real leak compounds past 2% quickly (delayed, never missed); a false
  positive disables the gate (total loss). Tighten per-pipeline with
  `--max-growth` once p95 data is measured. Section 5 / W6 updated.
* New work item W12 (Section 9): the v4 Section 3 bytecode half
  (`audit_bytecode_hotpath`), which had acceptance criteria missing. The
  W10 "verify opcode names on real 3.14" note is superseded by W12's
  version-agnostic test strategy (synthetic-instruction counting tests +
  graceful-degradation assertions, no 3.14 interpreter required).

1. Executive Summary

Primary support tier: 3.11/3.12 (and any standard GIL build up to and including 3.14 — same diagnostics, no free-threading checks). Enhanced tier: auto-detected by build flag, not version — any interpreter actually compiled with `Py_GIL_DISABLED` (3.13t experimental, 3.14t+). A `RuntimeWarning` fires the moment the enhanced tier activates.

2. Version/Build Detection & Structured Findings

    import sys
    import sysconfig
    import warnings
    import functools
    from dataclasses import dataclass, asdict
    from typing import Optional

    # RESOLVED (repo decision): floor stays (3, 9) to match pyproject.toml
    # requires-python >= 3.9. No version uses 3.11+-only APIs, and the
    # free-threading flag simply reads 0/None on older builds, so no hard
    # error exists at any version — detect_python_tier() is infallible
    # (see P1). 3.11/3.12 GIL builds are the primary-*tested* tier.
    SUPPORTED_MIN = (3, 9)

    FT_VERSION_FLOOR = (3, 13)  # free-threaded builds exist starting 3.13 (experimental)

    @dataclass
    class Finding:
        """Stable schema every diagnostic returns, regardless of tier."""
        tier: str            # "baseline" | "enhanced"
        check: str           # e.g. "gil_resurrection", "nested_loop", "memory_ceiling"
        status: str          # "pass" | "fail" | "warn" | "skipped"
        skipped: bool = False
        detail: str = ""
        file: Optional[str] = None
        line: Optional[int] = None

        def to_csv_row(self):
            return asdict(self)

    def detect_python_tier():
        """
        Returns 'baseline' or 'enhanced'. Keys off the actual build
        (Py_GIL_DISABLED), not the version number, so a GIL-enabled 3.14
        build stays baseline and an experimental 3.13t build correctly
        gets the enhanced diagnostics.
        """
        version = sys.version_info[:2]

        if version < SUPPORTED_MIN:
            raise RuntimeError(
                f"Python {version[0]}.{version[1]} is below the minimum "
                f"supported version {SUPPORTED_MIN[0]}.{SUPPORTED_MIN[1]}."
            )

        is_free_threaded = sysconfig.get_config_var("Py_GIL_DISABLED") == 1

        if is_free_threaded and version >= FT_VERSION_FLOOR:
            label = "3.13t (experimental)" if version == (3, 13) else f"{version[0]}.{version[1]}t"
            warnings.warn(
                f"Detected a free-threaded build ({label}). Primary support "
                "tier is the standard GIL build on 3.11/3.12; free-threading "
                "diagnostics (biased-refcounting audit, GIL-resurrection "
                "gate, QSBR-aware memory ceiling) are now active for this "
                "session. Treat findings as experimental, especially on "
                "3.13t, since free-threading internals have moved between "
                "point releases.",
                category=RuntimeWarning,
                stacklevel=2,
            )
            return "enhanced"

        return "baseline"

    def requires_tier(tier):
        """Decorator: skip (don't run) a diagnostic whose required tier
        doesn't match the active one — works both directions, unlike v3."""
        def decorator(func):
            @functools.wraps(func)
            def wrapper(*args, active_tier=None, **kwargs):
                active = active_tier or detect_python_tier()
                if active != tier:
                    return Finding(
                        tier=active, check=func.__name__, status="skipped",
                        skipped=True,
                        detail=f"{func.__name__} requires tier '{tier}'; active tier is '{active}'.",
                    )
                return func(*args, active_tier=active, **kwargs)
            return wrapper
        return decorator

3. Skill 1: AST/Bytecode Auditor — structured findings, `while` fixed, dedup fixed

    import ast

    class PerformanceAuditor(ast.NodeVisitor):
        def __init__(self, cached_attr_roots=None):
            self.in_loop = 0
            self.cached_attr_roots = cached_attr_roots or {"math", "np", "sys"}
            self._warned_loops = set()   # (lineno, col_offset) pairs, not id(node)
            self.findings = []

        def visit_For(self, node):
            self._enter_loop(node)

        def visit_While(self, node):
            self._enter_loop(node)  # was missing in v2/v3 — while loops got no hints

        def _enter_loop(self, node):
            self.in_loop += 1
            self.check_complexity(node)
            self.generic_visit(node)
            self.in_loop -= 1

        def visit_Attribute(self, node):
            if self.in_loop > 0 and isinstance(node.ctx, ast.Load):
                if isinstance(node.value, ast.Name) and node.value.id in self.cached_attr_roots:
                    self.findings.append(Finding(
                        tier="baseline", check="attr_cache_hint", status="warn",
                        line=node.lineno,
                        detail=f"Cache {node.value.id}.{node.attr} to a local before the loop.",
                    ))
            self.generic_visit(node)

        def check_complexity(self, node):
            for child in ast.walk(node):
                if isinstance(child, (ast.For, ast.While)) and child != node:
                    key = (child.lineno, child.col_offset)
                    if key in self._warned_loops:
                        continue
                    self._warned_loops.add(key)
                    self.findings.append(Finding(
                        tier="baseline", check="nested_loop", status="warn",
                        line=child.lineno,
                        detail="O(N*M) trap: nested loop.",
                    ))

A CLI wrapper does the printing; the auditor itself only produces `Finding` objects, so it can also feed a CSV writer or classifier directly:

    def run_auditor_cli(tree):
        auditor = PerformanceAuditor()
        auditor.visit(tree)
        for f in auditor.findings:
            print(f"[{f.status.upper()}] line {f.line}: {f.detail}")
        return auditor.findings

Bytecode check — opcode set derived dynamically instead of guessed via `startswith`, so it correctly includes the `LOAD_FAST_BORROW_LOAD_FAST_BORROW` super-instruction without also catching unrelated opcodes that happen to share a prefix:

    import dis

    _BORROW_OPS = {name for name in dis.opmap if name.startswith("LOAD_FAST_BORROW")}

    def audit_bytecode_hotpath(func, active_tier=None):
        tier = active_tier or detect_python_tier()
        instructions = list(dis.get_instructions(func))
        borrowed = sum(1 for i in instructions if i.opname in _BORROW_OPS)
        plain = sum(1 for i in instructions if i.opname == "LOAD_FAST")

        if tier == "enhanced":
            return Finding(tier=tier, check="bytecode_hotpath", status="pass",
                            detail=f"borrowed_loads={borrowed} plain_loads={plain}")
        return Finding(tier=tier, check="bytecode_hotpath", status="pass",
                        detail=f"plain_loads={plain} (LOAD_FAST_BORROW doesn't exist before 3.14)")

4. Skill 2: Concurrency Gate — single tier lookup, correct meaning under fixed detection

    import sys

    def audit_gil_resurrection(active_tier):
        """
        Only called when active_tier == 'enhanced', which now strictly means
        'confirmed free-threaded build' (Section 2 fix) — so gil_active=True
        here reliably means resurrection actually happened, not a
        misclassified GIL-enabled build.
        """
        gil_active = sys._is_gil_enabled() if hasattr(sys, "_is_gil_enabled") else True
        status = "fail" if gil_active else "pass"
        return Finding(tier=active_tier, check="gil_resurrection", status=status,
                        detail="SCALING_LOCKED" if gil_active else "TRUE_PARALLEL")

    def audit_baseline_concurrency(active_tier):
        # NOTE: distinct check name (not "gil_resurrection") — the GIL can
        # never resurrect on this build, and the name must not imply otherwise.
        return Finding(tier=active_tier, check="concurrency_baseline", status="pass",
                        detail="GIL always active on this build; not applicable — "
                                "consider multiprocessing or GIL-release C-extension "
                                "sections for CPU parallelism.")

    def audit_concurrency():
        """Single detect_python_tier() call, tier passed down explicitly."""
        tier = detect_python_tier()
        if tier == "enhanced":
            return audit_gil_resurrection(tier)
        return audit_baseline_concurrency(tier)

5. Skill 3: Memory Ceiling — zero-guard, corrected `gc.collect()` claim

    import gc

    def memory_ceiling_gate(baseline_rss, current_rss):
        tier = detect_python_tier()

        # RESOLVED (Revision 3): single universal threshold, no tier branch.
        # 0.5% false-positives on the enhanced tier (QSBR/mimalloc lag), and
        # the gate accepts RSS or tracemalloc numbers, so the default must
        # be safe for either signal everywhere. See W6.
        threshold = 0.02

        if baseline_rss <= 0:
            return Finding(tier=tier, check="memory_ceiling", status="skipped",
                            skipped=True, detail="baseline_rss <= 0; cannot compute a growth ratio.")

        growth = (current_rss - baseline_rss) / baseline_rss
        status = "pass" if growth <= threshold else "fail"
        return Finding(tier=tier, check="memory_ceiling", status=status,
                        detail=f"growth={growth:.4f} threshold={threshold}")

6. Pipeline (stages renamed to avoid colliding with existing SKILL.md phase numbers — slot into your real numbering)

    [ DESKTOP: TIER DETECTION + AST/BYTECODE ] -> [ SURGICAL PROFILING ] -> [ LOCAL INTEGRATION ] -> [ CI/CD GATES ]

* **Desktop stage:** `detect_python_tier()` runs first; on an enhanced-tier build the warning surfaces in the dev's terminal immediately, before any other check.
* **Surgical stage:** `py-spy` / `line_profiler` — tier-agnostic, unchanged.
* **Local integration stage:** `@audit_performance` calls `audit_concurrency()`, which now only calls `detect_python_tier()` once.
* **CI/CD stage:** build fails if any `Finding.status == "fail"`; a `"skipped"` status (e.g. enhanced-only checks running on baseline) is not a failure.

7. Open Items — ALL RESOLVED (repo reviewer decisions, binding)

* `SUPPORTED_MIN` → **(3, 9)**, matching `pyproject.toml` `requires-python`.
  No hard error at any version; 3.11/3.12 GIL builds are primary-*tested*.
  Rationale: maintainer toolchain runs 3.9; sketches use no 3.11+-only APIs.
* CSV/classifier columns → **unchanged**. `generate_baseline_csv.py`
  FIELDNAMES plus appended `tier, tier_detail` stay exactly as-is. The
  `Finding` envelope (`tier/check/status/skipped/detail/file/line`) is for
  process-level gates only (GIL, memory, bytecode summary) and NEVER becomes
  CSV rows. Function-level static checks use per-detector plan dataclasses
  with `analyze_source()`/`analyze_file()` (same pattern as `HoistPlan` /
  `RewritePlan`, including a `skipped` list that makes the FP budget visible).
* SKILL.md mapping → real headers are Phase 0 (target) through Phase 5
  (tiered dispatch, §§5.1–5.5). Tier-detection preamble → top of Phase 1;
  AST/bytecode audit → Phase 1 baseline list; concurrency/memory gates →
  Phase 1 + Phase 5 reporting. No renumbering; generic stage names dropped.
* Threshold calibration → ship `--max-growth` flags defaulting to 0.005
  baseline / 0.02 enhanced, documented as uncalibrated starting points, with
  the p95-of-clean-builds procedure in SKILL.md. No 3.14t interpreter exists
  in this environment to measure on — knobs now, data later (P7).
* `requires_tier` failure mode → **return skipped `Finding`, never raise**.
  Matches the repo contract "errors are data, not a crash". Contract note:
  the wrapper injects `active_tier=` into the wrapped function, so every
  gated function MUST accept that kwarg.
* Pre-commit → **ship an example hook + docs; the installer never touches
  `.git/hooks`** of a target repo. Gate exit codes: 0 pass, 1 finding-fails,
  2 harness error (see Section 10).
* `memray` / `py-spy` → **optional, documented-only integrations** (zero
  references in the repo today). Built-in path is `tracemalloc` +
  `line_profiler`, both already dependencies. No `pyproject.toml` changes.
* Module placement → new shared gate module lives at
  `scripts/tier_gate.py` (precedent: `profilers/severity.py`,
  `profilers/target_resolution.py`), imported via the existing
  `sys.path.insert(0, dirname)` + absolute-import pattern — no relative
  imports (tests load scripts by file path via importlib; hyphenated dirs
  are not importable packages). New code follows `from __future__ import
  annotations` + `X | None` style per ruff `target-version = py39`.

8. Non-Negotiable Architectural Requirements (P1–P8)

These encode past incidents as design law. Each work item in Section 9 cites
the principles that apply to it; violating a cited principle fails review
even if all other criteria pass.

* **P1 — Observability never breaks the observed.** Diagnostic code paths
  must be infallible from the caller's perspective. `detect_python_tier()`
  never raises (no hard error at any version; the `RuntimeWarning` emit is
  wrapped so `python -W error` / pytest `filterwarnings = error` cannot turn
  tier detection into a crash). `@audit_performance` / `track_memory()`
  catch their own internal failures and record them; they raise only under
  explicit `strict=True`. A diagnostic that can fail the run is a defect.
* **P2 — Execution containment.** Profiling imports and CALLS target code,
  repeatedly, at growing N. Every execution path gets a per-target timeout
  and a resource budget (`--max-targets` exists; add per-row time and `max_n`
  caps). A timeout or budget breach becomes an error ROW, never a hung or
  dead run. SKILL.md documents the execution policy: side-effecting targets
  must be profiled behind harness wrappers.
* **P3 — Resolvers re-verify at apply time (no check-then-act race).**
  Classification and application are separate process invocations; the file
  may change between them. Every applier re-parses the target bytes,
  re-derives its own safety proof, and fails closed on mismatch. The
  classifier's output is a hint, never a proof.
* **P4 — Four tests per resolver, no exceptions:** (a) negative tests for
  near-miss patterns that must NOT fire; (b) idempotency (second apply is a
  no-op); (c) collateral-diff (output parses; diff touches only intended
  lines, byte-for-byte elsewhere); (d) documented revert path actually
  exercised. Tier0 auto-apply additionally requires a measured near-zero
  false-positive rate against a corpus — alert fatigue kills tools slowly.
* **P5 — Stable finding identity.** Every finding carries a fingerprint,
  `check + function + hash(normalized detail)` — never raw line numbers
  (they shift with every edit). Without this, CI can only do absolute
  thresholds and blocks legacy codebases on day one; with it, policy is
  "fail on findings introduced by this change".
* **P6 — Artifact provenance + schema versioning.** Every emitted
  CSV/report carries a header: `tool_version`, commit SHA, interpreter
  version + tier, timestamp. Consumers fail CLOSED on version/provenance
  mismatch (never silently misread columns). The gate refuses to compare
  baselines across tiers or across schema versions.
* **P7 — Noisy signals get statistics, not thresholds.** Single-reading
  rank crossings never escalate tiers (hysteresis: borderline ranks require
  confirmation via repeat measurement / wider N). Keep-or-revert after a fix
  requires improvement beyond a margin (default: >10% over repeated runs),
  not any-delta. Rationale: Big-O fits are known to flap run-to-run
  (documented in SKILL.md Phase 4 notes).
* **P8 — The tool's own cost is budgeted.** New detectors share one parsed
  AST per file (analysis context passed down, not re-parsed per detector —
  repo-wide scans must not be O(detectors × files)). Total-run budgets bound
  the whole pipeline. Per-stage failure policy is explicit (Section 10):
  fail-open on desktop, fail-closed in CI.

9. Work Breakdown (build order is normative — later items assume earlier ones)

9.0 Shared gates — EVERY work item must pass these, no exceptions.

* (a) Full existing suite green (`pytest tests/`) plus ruff (`line-length
  100`, `target-version = py39`) on every touched file.
* (b) New tests load new scripts via the `test_regex_hoist.py` pattern
  (importlib by file path — hyphenated dirs are not importable).
* (c) No `pyproject.toml` dependency or `requires-python` changes without an
  explicit, separately-approved decision (default: stdlib only for new
  modules; `memray`/`py-spy` stay documented-only).

* **W1 — `scripts/tier_gate.py` + `tests/test_tier_gate.py`.** [P1, P6]
  Implement `detect_python_tier()` (build-flag keying per Section 2,
  floor (3, 9), warning wrapped so `-W error` cannot raise),
  `requires_tier()` (bidirectional skip, `active_tier=` passthrough
  contract), `Finding` envelope (process-level gates ONLY — GIL, memory,
  bytecode summary). Acceptance: parametrized tests over mocked
  `{version, Py_GIL_DISABLED}` combos incl. 3.14-GIL-build-stays-baseline
  and 3.13t-goes-enhanced; test asserting `detect_python_tier()` does not
  raise with warnings-as-errors; skip-envelope shape test both directions.
* **W2 — Profiler execution containment.** [P2, P8]
  Add per-target timeout + per-row time/`max_n` budget flags to the
  profiling path (`run_big_o.py`, `run_line_profile.py`,
  `generate_baseline_csv.py`); breach becomes an error row (`big_o_error` /
  `line_profile_error`), never a hang. Document the execution policy in
  SKILL.md Phase 0 (profiling imports and CALLS target code; side-effecting
  or multi-arg targets need harness wrappers). Acceptance: a deliberately
  slow/hanging target function yields an error row within the budget while
  the rest of the run completes; SKILL.md policy paragraph present.
* **W3 — Skill 1 vertical slice: static auditor → classifier → reporting.**
  [P4, P5, P7, P8] New `detectors/static_audit.py` exposing
  `analyze_source()`/`analyze_file()` returning plan dataclasses (NOT raw
  `Finding` lists — resolvers need structured fields; follow
  `HoistPlan`/`RewritePlan` incl. a `skipped` list). Shared per-file parsed
  AST context (P8 — no re-parse per detector). Wire new patterns into
  `classify_findings.py` tier0/tier1 classes ONLY where a mechanical fix or
  review lane exists; CSV FIELDNAMES unchanged. Nested-loop signals feed
  tier-escalation through P7 hysteresis, never a single reading.
  Acceptance: detection tests + near-miss negative tests; classifier tests
  for new tiers; no existing CSV column order changed (golden-header test);
  FP-oriented corpus check recorded.
* **W4 — Resolver hardening (existing + any new appliers).** [P3, P4]
  Every applier re-parses target bytes at apply time and re-derives its
  safety proof, failing closed with a clear error on mismatch (P3). Each
  resolver carries the four P4 tests: negatives, idempotency
  (apply-twice-no-op), collateral-diff (output parses; only intended lines
  differ), exercised revert. Acceptance: P3 fail-closed test (mutate file
  between classify and apply → clean abort, file untouched); all four test
  classes present per resolver.
* **W5 — Skill 2 concurrency gate.** [P1, P5]
  Implement `audit_concurrency()` per Section 4 (single tier lookup, passed
  down), `Finding` envelope, distinct check names per branch
  (`gil_resurrection` vs `concurrency_baseline`). Wire exit codes
  (0/1/2 per Section 10). Acceptance: mocked-tier tests for both branches;
  skip-envelope on mismatch; CLI exit-code tests.
* **W6 — Skill 3 memory gate.** [P1, P5] Pure `memory_ceiling_gate(baseline,
  current)` over numbers (trivially testable with synthetic values) +
  separate `tracemalloc` snapshot helpers that supply the numbers; stdlib
  only. ONE `--max-growth` flag (default 0.02 universal, Revision 3
  decision) documented as an uncalibrated starting point with the p95
  procedure in SKILL.md.
  Acceptance: threshold-boundary tests incl. `baseline <= 0` skip and the
  universal default; snapshot-helper round-trip test.
* **W7 — Provenance + schema versioning on all emitted artifacts.** [P6]
  Every CSV/report carries `tool_version`, commit SHA, interpreter +
  tier, timestamp; `classify_findings.py` fails closed on version/provenance
  mismatch (loud error, never silent misread); gate refuses cross-tier and
  cross-schema baseline comparisons. Acceptance: mismatch tests (old CSV →
  new classifier errors clearly); cross-tier comparison refusal test.
* **W8 — Finding fingerprints.** [P5] `fingerprint(check, function,
  normalized_detail)` helper in `tier_gate.py`; fingerprints surfaced
  wherever findings are listed so CI policy can be "fail on findings
  introduced by this change". Acceptance: fingerprint stability test
  (line-shifted duplicate source → identical fingerprint; genuinely
  different detail → different fingerprint).
* **W9 — Statistical treatment of noisy signals.** [P7] Hysteresis on
  tier-2 escalation (borderline `complexity_rank` requires confirmation:
  repeat measurement / wider N before routing to the algorithmic lane);
  keep-or-revert margin (>10% over repeated runs, configurable) encoded in
  SKILL.md Phase 3/5.2 verification steps. Acceptance: flapping-fit test
  (alternating ranks do NOT escalate on a single reading); margin test on
  the keep-or-revert decision helper.
* **W10 — Pipeline wiring: SKILL.md + example hook + CI stage.** [P8]
  Slot Sections 2/4/5/6 into real Phase 0–5 headers (Section 7 mapping);
  ship `templates/pre_commit_example.sh` (or equivalent) + docs — installer
  never writes hooks; CI example gates on exit codes + fingerprints (W8).
  `memray`/`py-spy` documented as optional only. Bytecode opcode names
  (incl. any `LOAD_FAST_BORROW*` super-instruction) MUST be verified on a
  real 3.14 interpreter before merge — the `dis.opmap` derivation is correct
  by construction, but the names in any test/doc must be observed, not
  guessed; tests inject a fake `opmap`. Acceptance: SKILL.md diff reviewed
  against the mapping; hook example tested for exit codes 0/1/2.
* **W11 — `@audit_performance` / `track_memory()` helper.** [P1] New
  importable surface (first non-CLI surface — lives in the
  `perf_gate` package, NOT the skill scripts dir). Total
  by construction (internal try/except; raises only with `strict=True`);
  near-zero overhead when disabled; calls `audit_concurrency()` (never the
  raw GIL check) so it degrades on baseline. Signature, defaults, and
  failure semantics documented in SKILL.md Phase 3-equivalent.
  Acceptance: totality test (inner failures → recorded, wrapped
  function result unaffected); disabled-overhead smoke test; strict-mode
  raise test.
* **W12 — Skill 1 bytecode half: borrowed-load audit.** [P1, P5, P8]
  Implements the v4 Section 3 `audit_bytecode_hotpath` sketch, which shipped
  without acceptance criteria. Design decisions (binding): lives at
  `detectors/bytecode_audit.py` and operates on SOURCE via `compile()`
  (pure, no import side effects -- honors the detectors "never executes"
  contract); per-function plan dataclasses mirroring `static_audit.py`
  (`BytecodeHint`: func, borrowed/plain/global/attr load counts); opcode
  set derived from `dis.opmap` at call time (empty pre-3.14 by
  construction); counts are ADVISORY ONLY (warn-level, never pass/fail --
  no classifier wiring, no tier escalation); CLI with `--files`/`--strict`/
  `--json` mirroring the static-audit CLI (hints carry fingerprints for CI
  diffing); SKILL.md Phase 1 lists it alongside the AST audit.
  Acceptance: counting test against synthetic instruction lists (no 3.14
  interpreter needed); graceful-degradation test (borrowed == 0 with plain
  loads counted on baseline); nested-function attribution test; CLI exit
  0/1/2 tests; hint-fingerprint line-shift stability test. The old "verify
  opcode names on real 3.14" caution is retired: nothing in this item
  asserts a hardcoded opcode name -- names are observed from the running
  interpreter's `dis.opmap`, and tests inject synthetic opcodes instead.

10. Per-Stage Failure Policy (normative)

| Stage | Tool itself errors | Genuine finding |
|---|---|---|
| Pre-commit / desktop hook | Warn, exit 0 (fail OPEN — a blocking hook that misfires gets uninstalled) | Report; block only on `fail` findings, never on `skipped` |
| CI/CD gate | Fail CLOSED (exit 2 — a silently-passing broken gate is worse than none) | Fail on `fail` (exit 1); `skipped` is not failure; fingerprint-diff policy per W8 |

11. Fingerprints & Provenance (normative field specs)

* Fingerprint: `sha1(check + "\0" + function + "\0" + normalized_detail)`,
  where normalization = strip line numbers/addresses, collapse whitespace.
  Stored alongside — never replacing — human-readable fields.
* Artifact header (CSV comment lines / report frontmatter): `tool_version`
  (== package version), `schema_version` (integer, bumped on any column or
  semantic change), `commit`, `python` (full version string), `tier`,
  `created_utc`. Consumers assert `schema_version` before parsing anything.

12. As-Built Record (2026-09-09 — what actually shipped)

All of W1–W12 is implemented in `src/perf_gate/skills/
perf-gate/scripts/` (flat scripts plus `detectors/`, `profilers/`,
`resolvers/` packages; tests load them by file path via importlib):

* W1 `tier_gate.py` + `tests/test_tier_gate.py` (build-flag detection,
  bidirectional `requires_tier`, `Finding` envelope for process gates).
* W2 `profilers/timeouts.py` (`run_with_timeout`; breach → error row,
  never a hang) wired through `run_big_o.py`, `run_line_profile.py`,
  `generate_baseline_csv.py` (`--timeout`, `--max-targets`).
* W3 `detectors/static_audit.py` (plan dataclasses with `skipped` lists,
  shared per-file AST) feeding `classify_findings.py`; CSV FIELDNAMES
  frozen (`... , tier, tier_detail` appended, order unchanged).
* W4 resolver hardening: every applier in `resolvers/` re-parses target
  bytes at apply time and fails closed on mismatch (`verify_plan`),
  with negative / idempotency / collateral-diff / exercised-revert
  tests per resolver.
* W5 `profilers/concurrency.py`, W6 `profilers/memory.py` (universal
  `--max-growth` default 0.02), W7 `provenance.py` (headers asserted
  before parse; cross-tier/cross-schema comparison refused),
  W8 fingerprints (`sha1(check + "\0" + function + "\0" +
  normalized_detail)`), W9 `confirmation.py` (`KEEP_MARGIN = 0.10`,
  `decide_keep`), W10 `templates/` (`pre_commit_example.sh`,
  `ci_static_example.yml`, `ci_gates_example.yml`,
  `ci_regression_example.yml`) + SKILL.md Phase 0–5 wiring,
  W11 `src/perf_gate/auditing.py`
  (`@audit_performance`, total by construction),
  W12 `detectors/bytecode_audit.py` (source-side `compile()`,
  advisory-only hints with fingerprints).

Post-spec additions (same stdlib-only, deterministic contract):

* Execution pipeline: `generate_baseline_csv.py` (Big-O + line
  hotspots per function) → `classify_findings.py` (deterministic
  tiers) → `render_action_list.py` (Markdown action list: Fix-now,
  Review, By-design/errors, Ruff PERF).
* Tier0 universe (19, in priority order): `tier0_regex_hoist`,
  `tier0_re_call`, `tier0_perf402`, `tier0_perf401`, `tier0_perf403`,
  `tier0_str_join`, `tier0_sum_reduce`, `tier0_set_build`,
  `tier0_invariant_hoist`, `tier0_consumer_list`, `tier0_sorted_minmax`,
  `tier0_literal_membership`, `tier0_list_cast`, `tier0_logging_lazy`,
  `tier0_dict_keys`, `tier0_async_sleep`, `tier0_enumerate`,
  `tier0_rematch_search`, `tier0_except_hoist` — each routable to a
  mechanical applier in `resolvers/` (the accumulator trio shares one
  detector/applier pair; every span-edit tier carries its own
  detector/applier pair plus a shared `span_edit.splice` primitive),
  all driven by an `apply_and_verify.py` loop that keeps the rewrite
  only if a re-measure clears the margin, else restores pre-apply
  bytes.
* Span-tier hardening (crawl4ai field findings): `detectors/span_dedupe.py`
  collapses nested-function duplicate candidates (identical edit spans
  reported under both outer and inner scopes) onto the innermost scope —
  previously the pair made `span_edit.splice` refuse the whole file and
  inflated per-function counts; `enumerate` additionally skips index-only
  loops (index read but never subscripted — the rewrite would only add an
  unused name). Covered by `tests/test_span_dedupe.py`,
  `tests/test_nested_attribution.py` (all ten span tiers), and new
  `test_enumerate.py` cases.
* Scope-chain audit (all 15 tier0 detectors): every rewrite-introduced or
  rewrite-dropped builtin must resolve identically at the span — checked
  via `span_dedupe.scope_binds` (own scope: params, assigns, imports,
  except/with/for/walrus targets, nested def/class names, lambda params,
  global/nonlocal declarations) plus `chain_blocked` (innermost scope
  holding the line through every enclosing function; classes contribute
  nothing) plus opt-in `module_binds`. Closed 12 gaps, e.g. param/closure
  `sum` with the `len(...)`→`sum(1 for ...)` rewrite, param/closure
  `min`/`max`/`list`/`dict`/`set`/`re`/`asyncio`, missing module checks
  for `sum_reduce`/`perf402`, closure-var patterns and param receivers
  for `regex_hoist` (now requires import-time-bound patterns and a plain
  top-level `import re`), and `re_call` receiver/hoisted-name collisions
  (the latter now renames to `_re_sub_2` instead of firing). `membership`,
  `logging_lazy`, `try_hoist`, `perf401`, `str_join`, and
  `invariant_hoist` are clean by construction (no name dependencies, or
  nested scopes never descended). Field effect on crawl4ai: 9 findings
  dropped, each verified a true skip (zero-benefit loops, collapsed
  duplicates, function-local `import re` that a module hoist would have
  broken) — no valid fix lost. Residuals (documented, not fixed):
  conditional module-level rebinds (`if c: sum = 5`), runtime rebinding
  via `global` from another function, and lambda/comprehension-var
  shadowing inside the span expression itself.
* Provenance loop: `mine_callsites.py` sketches harness wrappers from
  real call sites (approved harnesses carry `__big_o_provenance__`,
  the only thing promoting a row past "synthetic"); `tier1_propose.py`
  proposes tier1 fixes via a self-hosted model, each verified-or-
  rejected by gates, never applied without `--apply`.
* Measured heat lane: `profile_heat.py` ingests cProfile/pstats dumps
  (stdlib only, zero tokens) and ranks by cumulative-time share;
  Review rows sort heat-first, reachability (`detectors/
  reachability.py`, in-repo callers + entry reachability) second.
* Dark-row surfacing: rows dark on both profilers that still carry
  heat are listed with their share — the harness-priority queue.
* Ruff PERF lane: `ruff_perf.py` (detection only; PERF401/402/403
  already covered by tier0 rows are deduped in our favor).
* Two-level regression gate: `regression_gate.py`
  (`templates/ci_regression_example.yml`).
* Diff-scoped PR gate (recommended pre-merge lane): `pr_gate.py`
  resolves only functions the diff touches (`git diff -U0` ranges
  intersected with AST spans; untracked files fully in scope),
  profiles base in an isolated `git worktree` and the PR tree each in
  its own `generate_baseline_csv`/`classify_findings` subprocess
  (per-pair `--target-type function`, never whole files, so base and
  PR modules never share `sys.modules`), and fails only on measured,
  introduced regressions: rank worsening, newly-entered tier0
  (new functions fail on tier0, advise on tier2). Drift beyond the
  20% default margin advises; unmeasurable targets and infra hiccups
  SKIP exit 0; bad invocation exits 2. CI template:
  `templates/pr_gate_example.yml`; this repo dogfoods it
  (`.github/workflows/perf-gate.yml`).
* LLM-assisted input synthesis (optional, opt-in fallback):
  `llm_harness.py` asks a small model (Ollama default, OpenRouter
  opt-in) for an N-growing builder plus fixed calls when a function
  has no inferable shape; the proposal passes AST-parse, safe-builtins
  + stdlib import-allowlists, 5s exec-timeout, memoized-size, monotonic
  growth, and one fixed-call smoke gate before a budget-capped bounded
  fit. Deterministic lanes stay the default; only an explicit
  `--llm-harness` flag invokes the model for unresolved targets.
* Verdict timing: keep/revert numbers come from
  `measure_verdict_us()` — `time.perf_counter()` min-of-5
  (`_VERDICT_REPEATS = 5`) over the winning synthetic call shape.
  The line-profiler hotspot sum is triage signal, not the verdict
  (profiler overhead variance flips micro-scale comparisons).

Validation: full suite green (`827 passed`, 2026-09-10) plus `ruff
check` clean on skill and tests. Live recall on the crawl4ai
`processors/pdf/utils.py` scope the same day: 3 tier0 rows
(regex hoists + the new invariant hoist), heat-visible dark row at
34.6% profile share surfaced, 105 repo-wide ruff PERF findings over
506 files, 7018-entry reachability index — rendered report saved
alongside the target repo's baselines.