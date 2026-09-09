---
type: tier detection component
title: Tier detection and shared Finding
description: Documents tier_gate.py — detect_python_tier build-flag keying, requires_tier bidirectional skip, the Finding envelope for process gates, fingerprint stable identity, and normalize_detail.
tags: [tier-detection, finding, fingerprint, provenance, gate]
---

# Tier detection and shared Finding

`skills/code-optimizer/scripts/tier_gate.py` is the single entry point for interpreter-tier detection and the shared finding types every process-level gate uses. Every diagnostic in the skill calls `detect_python_tier` (directly or via `requires_tier`) instead of scattering `sys.version_info` checks.

## Tiers

- `"baseline"` — any standard GIL build, 3.9+. Full diagnostics in their version-agnostic form. `SUPPORTED_MIN = (3, 9)` matches `pyproject.toml` `requires-python`; nothing in this module needs newer APIs.
- `"enhanced"` — a build actually compiled with `Py_GIL_DISABLED` (3.13t experimental, 3.14t+). Baseline diagnostics still run, plus free-threading-specific checks. Activation emits one `RuntimeWarning`.

`detect_python_tier` keys off the actual build flag (`sysconfig.get_config_var("Py_GIL_DISABLED")`), not the version number, so a GIL-enabled 3.14 build stays baseline and an experimental 3.13t build correctly gets the enhanced diagnostics. Detection is infallible (P1): there is no unsupported-version error, and the warning emit is guarded so `python -W error` / pytest `filterwarnings = error` cannot turn tier detection into a crash.

## requires_tier decorator

`requires_tier(tier)` wraps a diagnostic so it is skipped (returns a `skipped` `Finding`) when the active tier does not match — bidirectionally, unlike the v3 sketch. The wrapper injects `active_tier=` into the wrapped function, so every gated function must accept that kwarg. A `skipped` status is never a failure (the gate contract).

## Finding envelope and fingerprint

`Finding` is the stable schema every process-level diagnostic returns: `tier`, `check`, `status` (`pass`/`fail`/`warn`/`skipped`), `skipped`, `detail`, `file`, `line`. `to_csv_row` includes the stable `fingerprint` alongside the human-readable fields so CI policy can diff findings across commits without relying on shifting line numbers. The `Finding` envelope is for process-level gates only ([concurrency and memory](gates.md), bytecode summaries); function-level static checks use per-detector plan dataclasses (see [detectors](detectors.md)).

`fingerprint(check, function, detail)` returns `sha1(check + "\0" + function + "\0" + normalize_detail(detail))`. `normalize_detail` strips line numbers (`line 12` → `line N`) and hex addresses and collapses whitespace, so line-shifted duplicates of the same finding produce identical fingerprints while genuinely different findings do not (P5).

## Focused tests

`tests/test_tier_gate.py` owns parametrized tests over mocked `{version, Py_GIL_DISABLED}` combos (including 3.14-GIL-build-stays-baseline and 3.13t-goes-enhanced), the assertion that `detect_python_tier` does not raise with warnings-as-errors, and the skip-envelope shape test both directions. `tests/test_fingerprints.py` owns fingerprint stability (line-shifted duplicate → identical; genuinely different detail → different). Run `pytest -q tests/test_tier_gate.py tests/test_fingerprints.py`.

## Change navigation

Changing tier rules requires updating `detect_python_tier` here, not in the individual gates. The `Finding` envelope is for process-level gates only; do not route function-level static check results through it (use per-detector plan dataclasses). Bump `provenance.SCHEMA_VERSION` if any CSV column or the `Finding` shape changes semantically.
