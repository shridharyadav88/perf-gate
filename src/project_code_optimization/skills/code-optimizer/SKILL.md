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

## Phase 1: Parallel Baseline Profiling
Run baseline checks in parallel before altering any source code:

1. **Big-O Growth Analysis**:
   ```bash
   python3 .agents/skills/code-optimizer/scripts/run_big_o.py --file <target_file> --func <func_name>
   ```

2. **Line-by-Line Operation Profiling**:
   ```bash
   python3 .agents/skills/code-optimizer/scripts/run_line_profile.py --file <target_file> --func <func_name>
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
