"""Agent Skills for automated Python Big-O profiling, line profiling, and corrective test loops.

Provides the ``code-optimizer`` skill and the ``install-agent-skills`` CLI for
deploying bundled skills into any local repository's ``.agents/skills/`` directory.
"""

from __future__ import annotations

from importlib.metadata import version as _metadata_version

try:
    __version__ = _metadata_version("project-code-optimization")
except Exception:
    __version__ = "0.1.0"

# Registry of bundled skills. Each key is the skill name and each value is
# the package-relative path to that skill's directory under ``skills/``.
# To add a new skill: drop a directory under ``skills/<name>/`` containing
# a ``SKILL.md``, then add one entry here.
BUNDLED_SKILLS: dict[str, str] = {
    "code-optimizer": "skills/code-optimizer",
}

__all__ = ["__version__", "BUNDLED_SKILLS"]
