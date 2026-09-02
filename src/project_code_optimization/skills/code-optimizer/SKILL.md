---
name: code-optimizer
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
prints one block per function **sorted most-severe-first**:

* `run_big_o.py` sorts by empirical complexity rank (worse growth first).
* `run_line_profile.py` sorts by total measured time (not just hotspot
  concentration — a trivial one-line function is always "100% in one line").

A target that fails to load or profile is still reported (with the error),
not silently dropped, so broad scans surface everything they touched.

---

## Phase 1: Parallel Baseline Profiling
Run baseline checks in parallel before altering any source code, using
whichever `--target-type` fits the scope chosen in Phase 0:

1. **Big-O Growth Analysis**:
   ```bash
   python3 .agents/skills/code-optimizer/scripts/run_big_o.py --file <target_file> --func <func_name>
   # or, e.g.: --target-type repo --repo . | --target-type commit --commit <sha> | --target-type files --files a.py --files b.py
   ```

2. **Line-by-Line Operation Profiling**:
   ```bash
   python3 .agents/skills/code-optimizer/scripts/run_line_profile.py --file <target_file> --func <func_name>
   # same --target-type options as above
   ```

3. **Correctness Baseline**:
   ```bash
   pytest
   ```

---

## Phase 2: Bottleneck Identification

Analyze combined diagnostics:

* Locate lines with highest execution counts (**Hits** column).
* Identify operations contributing to high space allocations.
* Compare empirical Big-O against expected theoretical bounds.

---

## Phase 3: Corrective Optimization Loop (Max 3 Attempts)

Execute this loop up to 3 times:

1. **Refactor Code**: Modify target file to reduce hotspot execution counts or drop time/space complexity orders.
2. **Quality Gate (Unit Tests)**:
Run `pytest`. If tests fail:
* Capture failure tracebacks.
* Do NOT run performance tools yet.
* Fix regressions and rerun `pytest` until passing.

3. **Performance Verification**:
Rerun `run_big_o.py` and `run_line_profile.py`.
* If Big-O complexity improves or line hits decrease significantly AND unit tests pass: **EXIT LOOP**.
* If performance does not improve: Revert changes and try an alternative approach.

---

## Phase 4: Final Reporting

Format output using template at `.agents/skills/code-optimizer/templates/report_template.md`.

### Baseline-only assessments (no refactor)

If the request is to *find and report* issues rather than fix them — or the
target is broad enough (a file, several files, a commit, or the whole repo)
that fixing everything in one pass isn't the goal — skip Phase 3 entirely.
Run Phase 1 profiling with the appropriate `--target-type`, then report using
`.agents/skills/code-optimizer/templates/baseline_report_template.md` instead.
That template has no "after"/"optimizations applied" sections and requires
every finding to be listed **most severe to least severe**, using the same
complexity-rank-then-hotspot-magnitude criteria the scripts themselves use to
order their multi-target output.

### CSV baseline report (no LLM narration)

When the goal is the ranked findings as data — to save tokens, to feed
another tool, or because the target has too many functions for prose to be
useful — skip the template entirely and run:

```bash
python3 .agents/skills/code-optimizer/scripts/generate_baseline_csv.py \
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
  `run_big_o.py`'s "multi-argument functions need a wrapper" limitation).

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
python3 .agents/skills/code-optimizer/scripts/classify_findings.py \
  --input baseline.csv --output classified.csv
```

Adds `tier` and `tier_detail` columns by pure static/numeric analysis — no
execution, no LLM:

| `tier`               | Meaning                                                                 |
| --------------------- | ------------------------------------------------------------------------ |
| `tier0_regex_hoist`   | A function-local `re.compile(...)` is provably safe to hoist to module scope (see 5.2). Detected by AST alone, so it fires even on rows where profiling itself failed (`big_o_error`/`line_profile_error` set) — the fix needs no execution evidence. |
| `tier2_algorithmic`   | `complexity_rank >= 4` (Quadratic or worse). Needs a real algorithm change. |
| `tier1_review`        | Profiling succeeded, no complexity problem, no known mechanical pattern. |
| `not_actionable`      | Neither profiling nor static analysis produced anything to act on.       |

`tier0` is checked before `tier2` — a mechanical fix is cheaper and more
certain than a model-driven rewrite even for a row that also happens to show
high complexity, so take the free win first and re-profile afterward; if the
complexity problem is still there, it'll show up as `tier2` on the next pass.

### 5.2 Tier 0 — apply directly, then verify (no LLM)

For every `tier0_regex_hoist` row, group by `file` (one file may have the
same pattern duplicated across several functions — the tool merges those into
one hoist automatically) and run:

```bash
python3 .agents/skills/code-optimizer/scripts/apply_regex_hoist.py --file <path>
# add --dry-run first if you want to see the plan before writing
```

This only ever touches assignments where the `re.compile(...)` call doesn't
reference the function's own parameters or locals (so it's the same object on
every call) and only when the target module-level name doesn't already exist
— anything ambiguous is left alone and reported, never guessed at.

After applying, verify like any other change in this skill:
1. `pytest` — if it fails, revert (`git checkout -- <path>`, only if the file
   had no other pending changes before you started — check `git status`
   first) and fall back to `tier1_review` for that finding instead of
   retrying blindly.
2. Re-run `generate_baseline_csv.py` (or `run_line_profile.py`) on the same
   target and confirm the hotspot's cost actually dropped.
3. Keep the change only if both checks pass.

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

### 5.5 Reporting

Prefer another CSV over prose here too: run `generate_baseline_csv.py` again
after all tiers have run and hand back both CSVs (or their diff) rather than
narrating what changed — the numbers already say it. Fall back to
`report_template.md` only when a human specifically wants a written
before/after narrative for one finding.
