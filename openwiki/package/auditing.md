---
type: package component
title: Local-integration auditing surface
description: Documents the importable @audit_performance decorator and track_memory context manager — totality by construction, budget enforcement, and the zero-coupling concurrency probe.
tags: [auditing, decorator, track_memory, tracemalloc, integration, p1]
---

# Local-integration auditing surface

`src/project_code_optimization/auditing.py` is the first non-CLI importable surface of the package. It provides `@audit_performance(...)` and `track_memory(...)`, the local-integration budget enforcers described in `SKILL.md` Phase 3.

## Surface

```python
from project_code_optimization import auditing

@auditing.audit_performance(max_seconds=2.0, max_bytes=1_000_000)
def my_function(...): ...

with auditing.track_memory() as mem:
    my_function(...)
print(mem["growth"])  # traced-heap delta in bytes
```

`@audit_performance` measures and warns by default; `max_seconds` / `max_bytes` set the budgets (`None` = unenforced; memory unset means `tracemalloc` is never touched); `enabled=False` restores the exact undecorated function (one flag check, near-zero overhead); `strict=True` raises `auditing.PerformanceViolation` on breach instead of warning; `on_violation=callable` overrides warning delivery. `track_memory(enabled=False)` yields an all-`None` record without touching `tracemalloc`.

## Reliability contract (P1)

The wrapped function's return value and own exceptions are never altered by the audit machinery. Every audit-machinery failure (broken timer, `tracemalloc` error, a raising `on_violation` callback, warnings-as-errors) is contained: the result is returned as if auditing had been disabled. Only the function's own exceptions propagate, and in `strict=True` mode an intentional `PerformanceViolation` may raise. Setting `max_bytes` starts `tracemalloc` process-wide if it is not already tracing (required to measure anything); time-only audits never touch `tracemalloc`.

## Concurrency probe

The concurrency probe mirrors `skills/code-optimizer/scripts/profilers/concurrency.py` semantics (see [gates](../skills/code-optimizer/gates.md)) in stdlib-only form, so this module has zero coupling to the deployed skill tree: baseline builds report an informational pass; enhanced builds fail only when the GIL is actually active on a free-threaded interpreter.

## Focused tests

`tests/test_auditing.py` owns the totality test (inner failures → recorded, wrapped function result unaffected), the disabled-overhead smoke test, the `strict=True` raise test, and `track_memory` enabled/disabled behavior. Run `pytest -q tests/test_auditing.py`.

## Change navigation

The concurrency probe is a mirror of [the gate](../skills/code-optimizer/gates.md) — change both together if the tier-aware semantics change. Keep `enabled=False` as a single flag check plus a direct call (near-zero overhead). Never alter the wrapped function's return value or own exceptions; audit-machinery failures are contained.
