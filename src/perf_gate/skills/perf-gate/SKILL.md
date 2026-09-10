---
name: perf-gate
description: Profiles Python code using Big-O scaling and line hit counts with an automated corrective unit-test verification loop.
version: 1.0.0
compatible_agents: ["claude-code", "gemini-cli", "cursor", "codex-cli"]
allowed_tools: ["bash", "read_file", "write_file"]
---

# Code Optimization & Corrective Refactoring Skill

Follow this multi-step procedure to profile, optimize, and verify Python code performance.

## Environment Requirements

The profiling scripts require the following packages to be installed in the
environment where they run:

```bash
pip install big-O line-profiler
```

The quality gate (Phase 3) expects `pytest` to be available in the target
repository's own environment — it is **not** a dependency of this skill.

---

## Phase 0: Choosing a Target

Both profiling scripts accept a `--target-type` flag that controls how much
code a single invocation covers. Pick the narrowest one that fits the ask:

| `--target-type` | Required flags                      | Scope                                              |
| ---------------- | ------------------------------------ | --------------------------------------------------- |
| `function` (default) | `--file`, `--func`               | One function.                                       |
| `file`            | `--file`                             | Every module-level function in one file.            |
| `files`           | `--files` (repeatable)               | Every module-level function across several files.   |
| `commit`          | `--commit`, optional `--repo`        | Every module-level function in the `.py` files a commit touched (profiled as they exist in the working tree now). |
| `repo`            | optional `--repo` (default `.`)      | Every module-level function across the whole repository (test files and common non-source dirs excluded). |

Only module-level `def`/`async def` functions are discoverable this way —
methods and nested functions are out of scope, same as the single-function
mode's `getattr(module, func_name)` contract.

For `file`/`files`/`commit`/`repo`, each script resolves every candidate
function, profiles all of them (capped by `--max-targets`, default 30), and
prints one block per function **sorted most-severe-first**. For full sweeps where coverage matters more than speed, pass `--max-targets 0` to disable the count cap -- the per-target `--timeout` budget remains the guard, and the static stages (AST audit, tier0 dispatch) are uncapped by construction:

* `run_big_o.py` sorts by empirical complexity rank (worse growth first).
* `run_line_profile.py` sorts by total measured time (not just hotspot
  concentration — a trivial one-line function is always "100% in one line").

A target that fails to load or profile is still reported (with the error),
not silently dropped, so broad scans surface everything they touched.

### Execution policy (profiling runs target code)

Profiling **imports and calls** the target functions -- repeatedly for Big-O
(at growing input sizes up to `--max-n`), once with synthesized arguments
for line profiling. Three consequences:

* A hanging or pathologically slow target does not hang the run: every
  target runs under a `--timeout` budget (default 120s per target on all
  three scripts) and a breach is reported as an error row instead.
* Side-effecting targets (network, disk, DB, ...) will fire for real. Point
  `--target-type function` at a small harness module that builds valid input
  and calls straight through, rather than profiling the side effect
  directly -- same advice as for multi-argument functions below. To draft one,
  mine real call sites first (`scripts/mine_callsites.py --file <t> --func <f>
  --repo <root>`); review the sketch for realism before profiling it, and keep
  its `__big_o_provenance__` marker so the row reports harness provenance.
* The timeout abandons the worker thread but cannot kill it; for genuinely
  untrusted code, isolate at the process level instead of relying on the
  in-process budget.
  untrusted code, isolate at the process level instead of relying on the
  in-process budget.
* For the best Big-O hit rate, profile inside the target's own environment:
  a venv with the target `pip install -e`'d (plus a modern Python) lets the
  loader use the real `__init__` chain (re-exports, import order) instead of
  stubs; without it, stub-blocked names fail with an actionable error row.
  Click commands and zero-argument entry points are reported by design --
  line profiling still applies.
* Scope first, execute later: every scanning CLI accepts `--dry-run`, which
  lists the resolved targets and exits before importing or calling anything.
  Use it before the first real run on unfamiliar code.

---

## Phase 1: Parallel Baseline Profiling
Run tier detection FIRST -- every gate below branches on it:

```bash
python3 -c "import sys; sys.path.insert(0, '.agents/skills/perf-gate/scripts'); import tier_gate; print(tier_gate.detect_python_tier())"
```

On a free-threaded build this prints a `RuntimeWarning` and returns `enhanced` (extra checks active, treat as experimental); otherwise `baseline`. Then run baseline checks in parallel before altering any source code, using
whichever `--target-type` fits the scope chosen in Phase 0:

1. **Big-O Growth Analysis**:
   ```bash
   python3 .agents/skills/perf-gate/scripts/profilers/run_big_o.py --file <target_file> --func <func_name>
   # or, e.g.: --target-type repo --repo . | --target-type commit --commit <sha> | --target-type files --files a.py --files b.py
   ```

2. **Line-by-Line Operation Profiling**:
   ```bash
   python3 .agents/skills/perf-gate/scripts/profilers/run_line_profile.py --file <target_file> --func <func_name>
   # same --target-type options as above
   ```
   Only the target function itself is instrumented -- time spent inside functions it CALLS appears as a single line. To see hotspots inside a callee, profile that callee directly with explicit `--call-args`.

3. **Static AST & Bytecode Audit** (no execution, all tiers):
   ```bash
   python3 .agents/skills/perf-gate/scripts/detectors/static_audit.py --files <f1.py> [<f2.py> ...]
   python3 .agents/skills/perf-gate/scripts/detectors/bytecode_audit.py --files <f1.py> [<f2.py> ...]
   # advisory by default (exit 0 even with hints); --strict exits 1 on hints; --json emits fingerprints for diffing
   ```

4. **Concurrency Gate** (tier-aware):
   ```bash
   python3 .agents/skills/perf-gate/scripts/profilers/concurrency.py [--json]
   # enhanced tier: fails (exit 1) on GIL resurrection; baseline: informational pass
   ```

5. **Memory Ceiling** (tier-aware thresholds):
   ```bash
   python3 .agents/skills/perf-gate/scripts/profilers/memory.py --baseline <bytes> --current <bytes>
   # single universal ceiling: 2% on every tier (0.5% false-positives on free-threaded builds); tighten with --max-growth once measured
   ```

6. **Correctness Baseline**:
   ```bash
   pytest
   ```

`py-spy` and `memray` are OPTIONAL integrations (sampling / native-allocation views) -- documented here for completeness, never required: the built-in path is `line_profiler` + `tracemalloc`, both already dependencies.

---

## Phase 2: Bottleneck Identification

Analyze combined diagnostics:

* Locate lines with highest execution counts (**Hits** column).
* Identify operations contributing to high space allocations.
* Compare empirical Big-O against expected theoretical bounds.

---

## Phase 3: Corrective Optimization Loop (Max 3 Attempts)

### Integration decorator (`@audit_performance`)

For local test runs, the importable helper `perf_gate.auditing` enforces time/memory budgets in-process (it calls the tier-aware concurrency probe, so it degrades correctly on baseline builds):

```python
from perf_gate import auditing

@auditing.audit_performance(max_seconds=2.0, max_bytes=1_000_000)
def my_function(...): ...

with auditing.track_memory() as mem:
    my_function(...)
print(mem["growth"])  # traced-heap delta in bytes
```

Signature & failure semantics (normative): bare `@audit_performance` measures and warns; `max_seconds` / `max_bytes` set the budgets (`None` = unenforced; memory unset means tracemalloc is never touched); `enabled=False` restores the exact undecorated function (one flag check, near-zero overhead); `strict=True` raises `auditing.PerformanceViolation` on breach instead of warning; `on_violation=callable` overrides warning delivery. The wrapped function's return value and OWN exceptions are never altered -- audit-machinery failures (including a raising callback or warnings-as-errors) are contained and the result stands. `track_memory(enabled=False)` yields an all-`None` record without touching tracemalloc.

Execute this loop up to 3 times:

1. **Refactor Code**: Modify target file to reduce hotspot execution counts or drop time/space complexity orders.
2. **Quality Gate (Unit Tests)**:
Run `pytest`. If tests fail:
* Capture failure tracebacks.
* Do NOT run performance tools yet.
* Fix regressions and rerun `pytest` until passing.

3. **Performance Verification** (margin-gated, not any-delta):
Rerun `run_big_o.py` and `run_line_profile.py`.
* Compare hotspot cost before/after with `confirmation.decide_keep(before_us, after_us)`: keep the change only on a **>10% improvement over repeated runs** (a 2% "improvement" is measurement noise, not a win) AND unit tests pass: **EXIT LOOP**.
* If the margin is not met: Revert changes and try an alternative approach.

---

## Phase 4: Final Reporting

Format output using template at `.agents/skills/perf-gate/templates/report_template.md`.

### Baseline-only assessments (no refactor)

If the request is to *find and report* issues rather than fix them — or the
target is broad enough (a file, several files, a commit, or the whole repo)
that fixing everything in one pass isn't the goal — skip Phase 3 entirely.
Run Phase 1 profiling with the appropriate `--target-type`, then report using
`.agents/skills/perf-gate/templates/baseline_report_template.md` instead.
That template has no "after"/"optimizations applied" sections and requires
every finding to be listed **most severe to least severe**, using the same
complexity-rank-then-hotspot-magnitude criteria the scripts themselves use to
order their multi-target output.

### CSV baseline report (no LLM narration)

When the goal is the ranked findings as data — to save tokens, to feed
another tool, or because the target has too many functions for prose to be
useful — skip the template entirely and run:

```bash
python3 .agents/skills/perf-gate/scripts/generate_baseline_csv.py \
  --target-type file --file <target_file> \
  [--output report.csv]
```

It accepts the same `--target-type`/`--file`/`--files`/`--commit`/`--repo`/
`--max-targets`/`--min-n`/`--max-n`/`--n-measures` flags as the two profiling
scripts, calls their underlying functions directly (no subprocess, no text
parsing), and writes one CSV row per resolved function — already sorted
most-to-least severe — with columns `rank, severity, file, function,
empirical_big_o, complexity_rank, big_o_error, total_hits,
total_hotspot_time_us, top_hotspot_pct_time, top_hotspot_line,
top_hotspot_source, line_profile_error`. Nothing in that file is written by
an agent; report it back verbatim (or hand over the `--output` path) rather
than re-narrating it into prose.

Two things worth knowing before trusting the `severity` column at face value:

* Severity is derived from `empirical_big_o` alone (via `severity.py`), so it
  can miss a large **constant-factor** problem in an algorithmically-linear
  function (e.g. a pure-Python per-byte loop) — the `total_hotspot_time_us`
  column carries that signal instead; sort or re-rank by it if raw cost
  matters more than complexity class for the target at hand.
* `big_o`'s empirical curve-fitting is noisy on fast, low-variance functions
  — the same function can fit as "Quadratic" at one range/run and "Linear" at
  another, even with identical parameters, because each measurement kept only
  one timer sample by default. `profile_big_o`/`run_big_o.py`/
  `generate_baseline_csv.py` all default `--n-timings` to 5 (keep the minimum
  of 5 timer samples per point) to damp this; raise `--n-timings` and
  `--n-repeats` further, and prefer a wide `--max-n` and `--n-measures >= 8`,
  before trusting a specific complexity claim. Also note `severity.py` treats
  a "Polynomial: ... x^k ..." fit as Linear-equivalent for k<=1.15 and
  Quadratic-equivalent for k<=2.15 rather than flatly worse than both — the
  raw exponent, not just the class name, decides the rank.
* A function whose signature isn't a single generatable argument (most
  multi-arg, domain-typed functions) will show a `big_o_error`/
  `line_profile_error` explaining the failed call rather than a silently
  wrong number — that's expected, not a bug in this script. Point
  `--target-type function --file <harness>` at a small wrapper module that
  builds valid input scaled by size and calls straight through to the real
  function to get a real reading (see Phase 0's target-type guidance and
  argument auto-binding: Big-O scales the size parameter and fixes the rest
  from defaults/annotations, line profiling synthesizes every parameter and
  cascades shapes on failure. Reach for a harness only when valid input is
  domain-specific (DB handles, live connections, structured configs)).
  If importing the target's package drags in heavy dependencies (browsers, ML stacks), load the module FILE directly in the harness instead of importing the package, so the package `__init__` never executes:
  `spec = importlib.util.spec_from_file_location("target_mod", "<path/to/module.py>"); mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)` -- then call `mod.real_function(...)`. For Big-O the wrapper takes the generated data LIST and scales by `len(data)`.

---

## Phase 5: Tiered Fix Dispatch (from a baseline CSV)

Once a baseline CSV exists (Phase 4), this phase turns findings into fixes
without spending tokens where a deterministic tool already gives a certain
answer, and without asking a small model to do something it's unreliable at.
This is a dispatcher the orchestrating agent runs, not a single script — each
finding gets routed to exactly one of three lanes, cheapest-and-most-certain
first.

### 5.1 Classify (deterministic, zero tokens)

```bash
python3 .agents/skills/perf-gate/scripts/classify_findings.py \
  --input baseline.csv --output classified.csv
```

Adds `tier` and `tier_detail` columns by pure static/numeric analysis — no
execution, no LLM:

| `tier`               | Meaning                                                                 |
| --------------------- | ------------------------------------------------------------------------ |
| `tier0_regex_hoist`   | A function-local `re.compile(...)` is provably safe to hoist to module scope (see 5.2). Detected by AST alone, so it fires even on rows where profiling itself failed (`big_o_error`/`line_profile_error` set) — the fix needs no execution evidence. |
| `tier0_perf402`       | A manual list-copy loop is provably safe to collapse into `out = list(items)` (see 5.2). Same static-only guarantee as `tier0_regex_hoist`. |
| `tier0_perf401`       | A manual list-build loop is provably safe to collapse into a list comprehension (see 5.2). Same static-only guarantee as `tier0_regex_hoist`. |
| `tier0_re_call`       | A module-level `re.<method>(...)` call with a constant pattern is provably safe to rewrite to a hoisted compiled call (see 5.2). Same static-only guarantee as `tier0_regex_hoist`. |
| `tier0_str_join`      | A `s = ''` + `for ...: s += ...` loop is provably safe to collapse into `''.join(...)` (see 5.2). Same static-only guarantee as `tier0_regex_hoist`. |
| `tier0_sum_reduce`    | A `total = 0` + `for ...: total += ...` loop is provably safe to collapse into `sum(...)` (see 5.2). Same static-only guarantee as `tier0_regex_hoist`. |
| `tier0_invariant_hoist` | A loop-invariant module-global load is provably safe to hoist to a pre-loop local (see 5.2). Names only -- attribute loads stay report-only. |
| `tier0_perf403`       | A `d = {}` + `for ...: d[k] = v` loop is provably safe to collapse into a dict comprehension or `dict(...)` (see 5.2). Same static-only guarantee as `tier0_regex_hoist`; PERF403 hits ours cover are deduped in our favor. |
| `tier0_set_build`     | A `seen = set()` + `for ...: seen.add(...)` loop is provably safe to collapse into `set(...)` (see 5.2). Same static-only guarantee as `tier0_regex_hoist`, plus a no-rebinding check on the name `set`. |
| `tier0_consumer_list` | An eager list fed to a single-pass consumer (`sum`/`min`/`len`/`join`/...) is provably safe to stream as a generator (see 5.2). `any`/`all` additionally require pure element expressions. |
| `tier0_sorted_minmax` | `sorted(...)[0]` / `[-1]` with no `key=` is provably safe to collapse to `min(...)` / `max(...)` (see 5.2). |
| `tier0_literal_membership` | `x in [...]` over 3+ constant elements is provably safe to test against a set literal (see 5.2). |
| `tier0_list_cast`     | `list(...)` around a provably-fresh temporary in loop position is safe to drop (see 5.2). Bare names keep their snapshot. |
| `tier0_logging_lazy`  | An eager logging f-string / `.format` with exactly-translatable fields is safe to pass as lazy `%`-args (see 5.2). |
| `tier0_dict_keys`     | `for k in d.keys()` is safe to iterate as `for k in d` when the body never mutates `d` (see 5.2). |
| `tier0_async_sleep`   | `time.sleep(...)` inside an async function (with `asyncio` imported) is safe to `await asyncio.sleep(...)` (see 5.2). |
| `tier0_enumerate`     | `for i in range(len(x))` with `x[i]` reads and no length mutation is safe to iterate as `enumerate` (see 5.2). |
| `tier0_rematch_search` | `re.match(".*pat", ...)` with proven DOTALL in a boolean test is safe to `re.search("pat", ...)` (see 5.2). |
| `tier0_except_hoist`  | A per-iteration `try/except` whose handlers all `raise`/`return` is safe to hoist around the loop (see 5.2). |
| `tier2_algorithmic`   | `complexity_rank >= 4` (Quadratic or worse). Needs a real algorithm change. |
| `tier1_review`        | Profiling succeeded, no complexity problem, no known mechanical pattern. |
| `not_actionable`      | Neither profiling nor static analysis produced anything to act on.       |

`tier0` is checked before `tier2` — a mechanical fix is cheaper and more
certain than a model-driven rewrite even for a row that also happens to show
high complexity, so take the free win first and re-profile afterward; if the
complexity problem is still there, it'll show up as `tier2` on the next pass.
When one function hits several tier0 patterns, the row keeps the
highest-priority tier (`regex_hoist` > `re_call` > `perf402` > `perf401` > `perf403` > `str_join` > `sum_reduce` > `set_build` > `invariant_hoist` > `consumer_list` > `sorted_minmax` > `literal_membership` > `list_cast` > `logging_lazy` > `dict_keys` > `async_sleep` > `enumerate` > `rematch_search` > `except_hoist`) with all details
joined in `tier_detail` — run every matching applier below, then re-profile.

**Ruff PERF lane (detection only, no new tiers).** Our AST detectors cover
what Ruff cannot prove safe (notably `re.compile` hoisting -- no Ruff rule
exists for it). For the six upstream Perflint rules, do not reimplement:
run `scripts/ruff_perf.py --files <f.py> ...` (or `ruff check --select PERF
--output-format json` directly). PERF401/402/403 hits at locations our tier0
rows already cover are deduped in our favor -- we own the applier plus the
measurement. The ruff-unique rules (PERF101/102/203) render as their own
section in `render_action_list.py --ruff <file.json>`; apply with
`ruff check --select PERF --fix` where a fix is offered, otherwise by hand,
then re-measure with our profilers before keeping.

### 5.2 Tier 0 — apply directly, then verify (no LLM)

Group tier0 rows by `file` and run the matching applier per tier (one file may
hold the same pattern across several functions — each tool fixes all of
its matches in one pass):

```bash
# tier0_regex_hoist rows:
python3 .agents/skills/perf-gate/scripts/resolvers/apply_regex_hoist.py --file <path>
# tier0_perf401 / tier0_perf402 rows:
python3 .agents/skills/perf-gate/scripts/resolvers/apply_perf_comprehension.py --file <path>
# tier0_str_join / tier0_sum_reduce rows:
python3 .agents/skills/perf-gate/scripts/resolvers/apply_accumulator.py --file <path>
# tier0_re_call rows:
python3 .agents/skills/perf-gate/scripts/resolvers/apply_re_call.py --file <path>
# tier0_consumer_list / tier0_sorted_minmax / tier0_literal_membership /
#   tier0_list_cast / tier0_logging_lazy / tier0_dict_keys /
#   tier0_async_sleep / tier0_enumerate / tier0_rematch_search /
#   tier0_except_hoist rows (one applier per tier):
python3 .agents/skills/perf-gate/scripts/resolvers/apply_consumer.py --file <path>
python3 .agents/skills/perf-gate/scripts/resolvers/apply_sorted_minmax.py --file <path>
python3 .agents/skills/perf-gate/scripts/resolvers/apply_membership.py --file <path>
python3 .agents/skills/perf-gate/scripts/resolvers/apply_list_cast.py --file <path>
python3 .agents/skills/perf-gate/scripts/resolvers/apply_logging_lazy.py --file <path>
python3 .agents/skills/perf-gate/scripts/resolvers/apply_dict_keys.py --file <path>
python3 .agents/skills/perf-gate/scripts/resolvers/apply_async_sleep.py --file <path>
python3 .agents/skills/perf-gate/scripts/resolvers/apply_enumerate.py --file <path>
python3 .agents/skills/perf-gate/scripts/resolvers/apply_rematch_search.py --file <path>
python3 .agents/skills/perf-gate/scripts/resolvers/apply_try_hoist.py --file <path>
# add --dry-run first if you want to see the plan before writing
```

Or run the wrapped mechanic for one function -- it measures, applies,
re-measures, and keeps the rewrite only if the gain clears the margin
(reverting byte-identical otherwise):

```bash
python3 .agents/skills/perf-gate/scripts/apply_and_verify.py --file <path> --func <name>
# --tier <any tier short name> (default: auto tries each in priority order),
#   --min-improvement (default: 0.10)
```

This only ever touches assignments where the `re.compile(...)` call doesn't
reference the function's own parameters or locals (so it's the same object on
every call) and only when the target module-level name doesn't already exist.
`apply_perf_comprehension.py` only rewrites an `out = []` init directly
followed by a single-`append` loop whose iterable, expression, and post-loop
reads can't observe the difference
— anything ambiguous is left alone and reported, never guessed at.

After applying, verify like any other change in this skill:
1. `pytest` — if it fails, revert (`git checkout -- <path>`, only if the file
   had no other pending changes before you started — check `git status`
   first) and fall back to `tier1_review` for that finding instead of
   retrying blindly.
2. Re-run `generate_baseline_csv.py` (or `run_line_profile.py`) on the same
   target and confirm the hotspot's cost dropped by **more than 10% across repeated runs** (`confirmation.decide_keep`) -- any-delta is noise, not evidence.
3. Keep the change only if both checks pass.

Re-measurement protocol for `tier1_review` rows carrying an "unconfirmed" complexity note (rank 4 on a single reading): re-run the profiler with repeat runs and/or a wider `--max-n`, record agreement in the row's `confirmations` column, and escalate to the algorithmic lane only if independent readings agree (`confirmation.tier2_confirmed`). A lone Quadratic fit is as likely to be a curve-fit flap as a finding.

### 5.3 Tier 2 — algorithmic fixes (capable model)

For `tier2_algorithmic` rows, spawn an agent scoped to just that function —
its source, the CSV row's `empirical_big_o`/hotspot columns, and the
constraint that behavior must not change — using `model: "opus"` for
`Critical` severity and `model: "sonnet"` for `High`. Don't hand it the whole
file or repo; the finding already says exactly which function and why. After
it proposes a change, verify exactly as in 5.2 (tests, then re-profile,
keep-or-revert).

### 5.4 Tier 1 — small-model review (narrow, templated fixes)

For `tier1_review` rows, spawn an agent with `model: "haiku"` scoped to just
that function's source plus its hotspot line(s) from the CSV, asking for a
minimal, behavior-preserving fix (e.g. combining redundant passes over the
same string, inlining a trivially small helper called in a hot loop). If it
can't produce a confident fix, escalate that row to Tier 2 rather than
forcing a small model to guess at something structural. Verify exactly as in
5.2.

Self-hosted runner (no cloud tokens): `scripts/tier1_propose.py --input classified.csv`
sends each `tier1_review` row (function source + hotspot + Big-O label) to a local
Ollama server (`$OLLAMA_HOST`, default `http://localhost:11434`) and verifies every
returned diff on a scratch copy -- parse, apply, byte-compile, measure past the
margin -- reporting VERIFIED or REJECTED per row without ever writing the original
unless `--apply` is passed. Start the server with `ollama serve` and pull a code
model first (`ollama pull qwen2.5-coder:14b`, ~9 GB; override with `--model`).
Model output carries no equivalence proof, so a measured gain on synthetic inputs
is evidence, not certainty -- review VERIFIED diffs before merging.

### 5.5 Reporting

Prefer another CSV over prose here too: run `generate_baseline_csv.py` again
after all tiers have run and hand back both CSVs (or their diff) rather than
narrating what changed — the numbers already say it. Fall back to
`report_template.md` only when a human specifically wants a written
before/after narrative for one finding. For the outsider-readable summary --
what to fix now, what to review, what is by design -- render the classified
CSV (plus its `.inputs.json` sidecar when present) to Markdown instead of
handing back raw rows:

```bash
python3 .agents/skills/perf-gate/scripts/render_action_list.py --input classified.csv --top 10
# add --inputs baseline.inputs.json for honest Big-O labels (measured vs unmeasured)
```

Prioritize with reachability, not size: a slow function nobody calls is
less urgent than a medium one on every request path. `render_action_list.py
--reachability` (built by `scripts/detectors/reachability.py --repo <root>`)
orders `tier1_review` rows by caller count from a conservative static call
graph -- a Review row with zero known callers may be dead code, but the
graph cannot see dynamic dispatch or framework wiring, so treat that as a
triage hint to confirm, never as a deletion license.

When a measured profile exists, heat outranks reachability: build it with `scripts/profile_heat.py --profile run.pstats --json > heat.json` (repeat `--profile` to coalesce several dumps; record without `strip_dirs`, resolved from the repo root, so file keys join) and pass `--heat heat.json` to the renderer. Review rows then sort by cumulative share first, each carrying a `- heat:` line; unprofiled rows sort last, and a render without `--heat` behaves exactly as before. Prefer heat over caller counts whenever the profile covers the workload -- measured burn beats static edges; keep reachability for everything the profile never executed.

Start in CI with the static-only workflow
(`templates/ci_static_example.yml`): ruff PERF as the blocking gate plus
the two AST audits as advisory log output -- seconds per PR, no target-code
execution, no virtualenv. Promote the execution profilers (Big-O, line
profile, memory/concurrency gates in `templates/ci_gates_example.yml`) to a
scheduled deep-audit workflow once the static lane is green.

For reviewable fixes, `apply_and_verify.py --diff` prints the
paste-ready unified diff of a kept-or-rejected rewrite -- useful when the
applier reverts (below margin) but the reviewer still wants to see exactly
what was measured and rejected.

Gate exit codes & failure policy (normative): every gate script exits 0 on pass/warn/skipped, 1 when a finding fails, 2 on harness error. Desktop hooks (`templates/pre_commit_example.sh` -- copy to `.git/hooks/` manually, the installer never writes hooks) FAIL OPEN: tool errors warn and exit 0. CI (`templates/ci_gates_example.yml`) FAILS CLOSED: harness errors fail the build. `skipped` is never a failure. For rollout on legacy codebases, diff finding `fingerprint` sets (`--json` outputs carry them) and fail only on INTRODUCED fingerprints -- see the CI template. Hold the line across PRs with `scripts/pr_gate.py --base <base-sha>` (diff-scoped: only functions the change touches are profiled, base in an isolated worktree; rank worsening and newly-introduced tier0 fail the build, wall-time drift only advises unless `--strict-time`, unmeasurable targets skip) -- see `templates/pr_gate_example.yml`. The older two-CSV flow (`scripts/regression_gate.py --baseline base_classified.csv --current head_classified.csv`, `templates/ci_regression_example.yml`) still works for fixed tracked scopes.

When to reach for something else instead of a full skill pass: if the question is only 'does this file violate upstream style/perf lints', run `ruff check --select PERF` and stop -- the skill adds nothing on top. If the question needs production sampling or native-allocation views (multi-process services, C extensions), use `py-spy` / `memray` directly instead of the built-in profilers. Bring the full skill pass when a candidate fix must be measured, verified, and kept-or-reverted behind the gates above.
