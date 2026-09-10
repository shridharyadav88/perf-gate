<!--
  Instructions for the agent:
  Fill every `<PLACEHOLDER>` in this template with data from the current
  profiling run.  Keep failing-test tracebacks verbatim in the Quality Gate
  section.  Do not remove any section — a section with no data should read
  "N/A".
-->

# Code Optimization Report

| Field             | Value                       |
| ----------------- | --------------------------- |
| Date              | `<YYYY-MM-DD>`              |
| Repository        | `<repo-path>`               |
| Target file       | `<relative-path>`           |
| Target function   | `<func-name>`               |
| Git commit        | `<sha>`                     |
| Big-O tool        | `big-O <version>`           |
| Line-profiler tool| `line_profiler <version>`   |

---

## 1. Summary / Verdict

| Metric               | Before              | After               | Change            |
| -------------------- | ------------------- | ------------------- | ------------------ |
| Empirical Big-O      | `<before-class>`    | `<after-class>`     | `<verdict>`        |
| Total line hits      | `<before-hits>`     | `<after-hits>`      | `<delta>`          |
| Unit tests           | `<before-pass/fail>`| `<after-pass/fail>` | `<verdict>`        |
| **Decision**         |                     |                     | **KEEP / ROLLBACK**|

---

## 2. Baseline Diagnostics

### 2.1 Big-O Empirical Estimation

```
<Insert raw Big-O profiler output here>
```

### 2.2 Line-Profile Hotspots (Top 10)

| Line # | Hits     | Time (μs) | % Time | Line Contents           |
| ------ | -------- | --------- | ------ | ----------------------- |
| `<n>`  | `<hits>` | `<time>`  | `<pct>`| `<source>`              |

---

## 3. Optimizations Applied

| # | Before                          | After                           | Expected Effect         |
| - | ------------------------------- | ------------------------------- | ----------------------- |
| 1 | `<snippet>`                     | `<snippet>`                     | `<e.g. O(n²)→O(n)>`    |

---

## 4. Quality Gate — Unit Tests

```text
<Insert raw pytest output here>
```

- [ ] All tests passed before performance re-run
- [ ] No new failures introduced by refactor

---

## 5. Before / After Comparison

| Metric                    | Before          | After           |
| ------------------------- | --------------- | --------------- |
| Complexity class          | `<class>`       | `<class>`       |
| Top hotspot line hits     | `<hits>`        | `<hits>`        |
| Top hotspot % time        | `<pct>`         | `<pct>`         |

---

## 6. Final Recommendation

- **KEEP** — performance improved, tests pass.
- **ROLLBACK** — no measurable improvement or tests broken.
- **NEXT STEP** — `<guidance for the next iteration if applicable>`
