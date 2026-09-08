# Project Specification Document: `project-code-optimization`

Use this document as direct input for **Claude Code**, **Antigravity**, or **Gemini CLI** to scaffold and develop the **`project-code-optimization`** package.

---

## 1. Executive Summary & Objective

**`project-code-optimization`** is an enterprise-grade Python package that packages and distributes **Agent Skills** following the **Agent Skills Open Standard (`SKILL.md`)**.

It provides AI agents (Claude Code, Cursor, Gemini CLI, Codex CLI) with an autonomous, parallelized profiling and corrective refactoring workflow. It combines **empirical Big-O curve fitting** and **line-by-line operation hit profiling** with a **quality-gated test loop**.

The package is designed for direct installation via local directory wheel files (`.whl`), bypassing Git remote dependencies while providing a one-command CLI installer (`install-agent-skills`) to deploy skills into local repository `.agents/skills/` directories.

---

## 2. Target Directory & File Tree

```text
project-code-optimization/
├── pyproject.toml
├── README.md
├── src/
│   └── project_code_optimization/
│       ├── __init__.py
│       ├── cli.py
│       └── skills/
│           └── code_optimizer/
│               ├── SKILL.md
│               ├── scripts/
│               │   ├── run_big_o.py
│               │   └── run_line_profile.py
│               └── templates/
│                   └── report_template.md
└── tests/
    ├── test_cli.py
    └── test_profiling_scripts.py

```

---

## 3. Detailed Component Specifications

### 3.1. `pyproject.toml`

Build configuration using `setuptools` that bundles non-Python assets (`SKILL.md`, templates) and registers the CLI entry point.

```toml
[build-system]
requires = ["setuptools>=61.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "project-code-optimization"
version = "0.1.0"
description = "Agent Skills for automated Python Big-O profiling, line profiling, and corrective test loops."
readme = "README.md"
requires-python = ">=3.9"
dependencies = [
    "big-O>=0.10.0",
    "line-profiler>=4.1.0",
    "pytest>=7.0.0"
]

[project.scripts]
install-agent-skills = "project_code_optimization.cli:install_skills"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
"project_code_optimization" = ["skills/**/*"]

```

---

### 3.2. `src/project_code_optimization/skills/code_optimizer/SKILL.md`

The standard-compliant Agent Skill definition.

```markdown
---
name: code-optimizer
description: Profiles Python code using Big-O scaling and line hit counts with an automated corrective unit-test verification loop.
version: 1.0.0
compatible_agents: ["claude-code", "gemini-cli", "cursor", "codex-cli"]
allowed_tools: ["bash", "read_file", "write_file"]
---

# Code Optimization & Corrective Refactoring Skill

Follow this multi-step procedure to profile, optimize, and verify Python code performance.

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

```

---

### 3.3. `src/project_code_optimization/skills/code_optimizer/scripts/run_big_o.py`

```python
import sys
import argparse
import importlib.util
import big_O

def main():
    parser = argparse.ArgumentParser(description="Run Big-O empirical complexity estimation.")
    parser.add_argument("--file", required=True, help="Path to Python file")
    parser.add_argument("--func", required=True, help="Target function name")
    args = parser.parse_args()

    spec = importlib.util.spec_from_file_location("target_module", args.file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target_func = getattr(module, args.func)

    best_fit, fitted_complexities = big_O.big_O(
        target_func, 
        big_O.datagen.integers, 
        min_n=100, 
        max_n=10000, 
        n_measures=8
    )

    print("=== BIG-O PROFILER OUTPUT ===")
    print(f"Estimated Complexity: {best_fit}")
    print("Fitted Models:")
    for model, res in fitted_complexities.items():
        print(f"  - {model}: {res}")

if __name__ == "__main__":
    main()

```

---

### 3.4. `src/project_code_optimization/skills/code_optimizer/scripts/run_line_profile.py`

```python
import sys
import io
import argparse
import importlib.util
from line_profiler import LineProfiler

def main():
    parser = argparse.ArgumentParser(description="Run line-by-line operation hit profiling.")
    parser.add_argument("--file", required=True, help="Path to Python file")
    parser.add_argument("--func", required=True, help="Target function name")
    args = parser.parse_args()

    spec = importlib.util.spec_from_file_location("target_module", args.file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target_func = getattr(module, args.func)

    lp = LineProfiler()
    profiled = lp(target_func)

    # Attempt standard execution run with synthetic input
    try:
        profiled([i for i in range(1000)])
    except Exception:
        try:
            profiled("nitin" * 200)
        except Exception as e:
            print(f"Warning: Synthetic test run failed: {e}")

    stream = io.StringIO()
    lp.print_stats(stream=stream)
    print("=== LINE PROFILER OUTPUT ===")
    print(stream.getvalue())

if __name__ == "__main__":
    main()

```

---

### 3.5. `src/project_code_optimization/cli.py`

Deployer script that copies bundled skills into the current repository's `.agents/skills/` directory.

```python
import sys
import shutil
from pathlib import Path
import importlib.resources

def install_skills():
    """Extracts packaged skills into current project's .agents/skills/ directory."""
    target_dir = Path.cwd() / ".agents" / "skills"
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        package_skills = importlib.resources.files("project_code_optimization") / "skills"
        installed_count = 0

        for skill_folder in package_skills.iterdir():
            if skill_folder.is_dir():
                dest_path = target_dir / skill_folder.name
                if dest_path.exists():
                    shutil.rmtree(dest_path)
                shutil.copytree(skill_folder, dest_path)
                print(f" Successfully deployed skill '{skill_folder.name}' -> {dest_path}")
                installed_count += 1

        print(f"\nDeployment complete. Total skills deployed: {installed_count}")
    except Exception as e:
        print(f"Error deploying skills: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    install_skills()

```

---

## 4. Build, Local Distribution & Execution Workflow

Provide these exact instructions to Claude Code to verify setup:

1. **Build Wheel Locally**:
```bash
pip install build
python3 -m build

```


*Generates:* `dist/project_code_optimization-0.1.0-py3-none-any.whl`
2. **Install from Local Wheel**:
```bash
pip install dist/project_code_optimization-0.1.0-py3-none-any.whl

```


3. **Deploy Skills to Workspace**:
```bash
install-agent-skills

```


*Deploys to:* `.agents/skills/code-optimizer/`

---

## 5. Development Prompt for Claude Code

Copy and paste the prompt below into Claude Code to generate the codebase:

> **Claude Code Prompt:**
> "Please implement the `project-code-optimization` package based on the project specification document above. Create all directory paths, `pyproject.toml`, Python scripts, `SKILL.md`, and CLI deployment logic. Once created, run `python3 -m build` to compile the wheel, install it locally, and test running `install-agent-skills` to verify that `.agents/skills/code-optimizer/` is populated correctly."