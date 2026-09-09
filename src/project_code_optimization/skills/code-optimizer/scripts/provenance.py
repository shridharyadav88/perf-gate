"""Artifact provenance + schema versioning (W7).

Every emitted CSV carries a ``# key=value`` preamble above the header with
``tool_version``, ``schema_version``, ``python``, ``tier``, ``commit``
(best-effort), and ``created_utc``. Consumers assert the schema before
parsing anything: unknown or mismatched versions fail CLOSED (loud error,
never silent misread). :func:`check_compatible` refuses cross-schema and
cross-tier baseline comparisons for before/after diffing (wired into CI in
the pipeline pass; tested here).

The preamble uses ``#`` comment lines rather than extra CSV columns so the
frozen ``FIELDNAMES`` contract is untouched -- plain ``csv.DictReader``
users must strip leading ``#`` lines first (see :func:`split_preamble`).
"""

from __future__ import annotations

import datetime
import os
import platform
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tier_gate  # noqa: E402

# Bump on ANY column or semantic change to an emitted artifact. Consumers
# compare with == and refuse anything else.
SCHEMA_VERSION = 1

try:
    from project_code_optimization import __version__ as TOOL_VERSION
except Exception:  # standalone skill deployment: package not importable
    TOOL_VERSION = "unknown"


class ProvenanceError(ValueError):
    """A consumed artifact is missing, unreadable, or incompatible provenance."""


def _current_commit(path: str | None = None) -> str:
    """Best-effort short SHA of the tree containing *path*; ``"unknown"`` when unsure.

    ``git -C <dir>`` walks up to the enclosing repo itself, so the SHA
    describes the MEASURED code -- never whatever directory the tool
    happened to be invoked from. ``path`` may be a file or directory;
    ``None`` falls back to the current working directory.
    """
    if path is not None and not os.path.isdir(path):
        path = os.path.dirname(os.path.abspath(path)) or None
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
            cwd=path,
        )
    except Exception:
        return "unknown"
    sha = (out.stdout or "").strip()
    return sha if out.returncode == 0 and sha else "unknown"


def current_provenance(path: str | None = None) -> dict[str, str]:
    """Build the provenance record for an artifact emitted right now.

    *path* scopes the ``commit`` field to the measured tree (a target file,
    or the scanned repo root). Omit it only when no target path exists.
    """
    return {
        "tool_version": TOOL_VERSION,
        "schema_version": str(SCHEMA_VERSION),
        "python": platform.python_version(),
        "tier": tier_gate.detect_python_tier(),
        "commit": _current_commit(path),
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        ),
    }


def format_preamble(provenance: dict[str, str]) -> list[str]:
    """Render provenance as ``# key=value`` lines (no newlines in values)."""
    lines = []
    for key in ("tool_version", "schema_version", "python", "tier", "commit", "created_utc"):
        value = str(provenance.get(key, "unknown")).replace("\n", " ")
        lines.append(f"# {key}={value}")
    return lines


def split_preamble(text: str) -> tuple[dict[str, str], str]:
    """Split leading ``#`` lines off *text*; return (provenance, remainder)."""
    provenance: dict[str, str] = {}
    rest_start = 0
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped[1:].partition("=")
            provenance[key.strip()] = value.strip()
            rest_start = i + 1
        else:
            break
    return provenance, "".join(lines[rest_start:])


def assert_schema(provenance: dict[str, str], source: str = "input") -> None:
    """Fail closed unless *provenance* declares the current schema version."""
    raw = provenance.get("schema_version")
    try:
        declared = int(str(raw)) if raw is not None else None
    except (TypeError, ValueError):
        declared = None
    if declared != SCHEMA_VERSION:
        raise ProvenanceError(
            f"{source} declares schema_version={raw!r}; this tool requires "
            f"schema_version={SCHEMA_VERSION}. Regenerate the artifact with "
            "the current tool instead of consuming a stale/incompatible one."
        )


def check_compatible(first: dict[str, str], second: dict[str, str]) -> None:
    """Refuse before/after comparisons across schemas or tiers (P6).

    Raises :exc:`ProvenanceError` on any mismatch; returns ``None`` when the
    two records are comparable. Compares declared schemas first (a tier
    comparison is meaningless across different schemas).
    """
    assert_schema(first, source="first artifact")
    assert_schema(second, source="second artifact")
    if first.get("tier") != second.get("tier"):
        raise ProvenanceError(
            f"tier mismatch: {first.get('tier')!r} vs {second.get('tier')!r}. "
            "Baselines measured on different tiers (GIL vs free-threaded) "
            "are not comparable -- re-measure on one tier."
        )
