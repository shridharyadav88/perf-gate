<!--
  Instructions for the agent:
  This template is for a BASELINE-ONLY assessment — profiling data captured
  before any refactor, with no "after" or "optimizations applied" sections.
  Use it when the user asks to *find and report* performance issues rather
  than fix them, or as the Phase 1/2 record for a target too broad to fix in
  one pass (a whole file, a set of files, a commit, or a repository).

  Fill every `<PLACEHOLDER>`. Do not remove a section — a section with no
  data should read "N/A". Keep raw profiler output verbatim.

  ORDERING — list every finding from MOST to LEAST severe. Determine severity
  per finding using, in order:
    1. Empirical Big-O complexity rank (worse growth = more severe):
       Exponential/Cubic/Polynomial > Quadratic > Linearithmic > Linear
       > Logarithmic/Constant.
    2. Within the same complexity class, the function with more concentrated
       or larger absolute hotspot cost (line-profiler "Time" / "% Time")
       ranks higher.
    3. A function whose profiling failed (crashed, no usable signature) is
       listed last, under "Not Assessed", rather than assigned a severity.
  Map complexity rank to a label: Exponential/Cubic/Polynomial → Critical,
  Quadratic → High, Linearithmic → Medium, Linear → Low, Logarithmic/Constant
  → Info. This mirrors the ranking the bundled `run_big_o.py` / `run_line_profile.py`
  `--target-type file|files|commit|repo` scans already apply when sorting
  their own output, so cross-check your ordering against theirs.
-->

# Code Optimization — Baseline Assessment Report

| Field              | Value                                                  |
| ------------------ | ------------------------------------------------------- |
| Date               | `<YYYY-MM-DD>`                                          |
| Repository         | `<repo-path>`                                           |
| Target type        | `<function \| file \| files \| commit \| repo>`         |
| Target spec        | `<func-name, relative-path(s), commit-sha, or "whole repository">` |
| Git commit (HEAD)  | `<sha>`                                                 |
| Big-O tool         | `big-O <version>`                                       |
| Line-profiler tool | `line_profiler <version>`                               |
| Functions assessed | `<n>`                                                   |

---

## 1. Summary of Findings (Most → Least Severe)

| # | Severity   | File            | Function      | Empirical Big-O   | Top Hotspot % Time | One-line Risk                  |
| - | ---------- | --------------- | -------------- | ------------------ | ------------------- | ------------------------------- |
| 1 | `<Critical/High/Medium/Low/Info>` | `<relative-path>` | `<func-name>` | `<class>` | `<pct>%` | `<why this matters>` |

Not Assessed (profiling failed or target unresolvable):

| File            | Function   | Reason                        |
| --------------- | ---------- | ------------------------------ |
| `<path>`        | `<func>`   | `<error message>`              |

---

## 2. Baseline Diagnostics (same order as Section 1)

### 2.<n> `<file>` :: `<func-name>` — Severity: `<label>`

**Big-O Empirical Estimation**

```
<Insert raw Big-O profiler output here>
```

**Line-Profile Hotspots (Top 10)**

| Line # | Hits     | Time (μs) | % Time | Line Contents |
| ------ | -------- | --------- | ------ | -------------- |
| `<n>`  | `<hits>` | `<time>`  | `<pct>`| `<source>`     |

**Why this ranks here:** `<one or two sentences tying the complexity class and hotspot data to the assigned severity>`

_Repeat this subsection for every assessed function, in severity order._

---

## 3. Correctness Baseline

```text
<Insert raw pytest output here, or "N/A — not run for a baseline-only assessment">
```

---

## 4. Recommended Next Steps (no code changed yet)

| Severity | File :: Function | Suggested direction                        |
| -------- | ------------------ | ------------------------------------------- |
| `<label>`| `<file>` :: `<func>` | `<e.g. "collapse nested loop, expect O(n²)→O(n)">` |

This report captures a baseline only. Re-run the `code-optimizer` skill's Phase 3 corrective loop against the highest-severity entries above, then record before/after results with `report_template.md`.
