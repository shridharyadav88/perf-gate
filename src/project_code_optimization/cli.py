"""Deployer CLI that copies bundled skills into a local ``.agents/skills/`` directory."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from project_code_optimization import BUNDLED_SKILLS, __version__

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger("install-agent-skills")


# ---------------------------------------------------------------------------
# Core logic (callable for tests and programmatic use)
# ---------------------------------------------------------------------------

def install_skills(
    target_dir: str | Path | None = None,
    skills: Sequence[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    backup: bool = True,
    verbose: bool = False,
) -> list[str]:
    """Extract bundled skills into a local ``.agents/skills/`` directory.

    Parameters
    ----------
    target_dir:
        Destination directory.  Defaults to ``<cwd>/.agents/skills``.
    skills:
        Names of skills to install (from :data:`BUNDLED_SKILLS`).
        ``None`` means *all* registered skills.
    force:
        If ``True``, silently overwrite existing installs without creating
        a backup (overrides *backup*).
    dry_run:
        Print what would be installed without touching the filesystem.
    backup:
        If ``True`` (the default) and a destination already exists, rename
        the old copy to ``<name>.backup-<timestamp>`` before installing.
        Ignored when *force* is ``True``.
    verbose:
        Enable debug-level logging.

    Returns
    -------
    list[str]
        Names of the skills that were successfully installed.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(format="%(message)s", level=level)

    target = Path(target_dir) if target_dir else Path.cwd() / ".agents" / "skills"

    import importlib.resources

    try:
        pkg_skills = importlib.resources.files("project_code_optimization") / "skills"
    except Exception as exc:
        logger.error("Cannot locate bundled skills: %s", exc)
        if not dry_run:
            sys.exit(1)
        return []

    skill_names = list(skills) if skills is not None else list(BUNDLED_SKILLS)
    if not skill_names:
        logger.error("No skills selected for installation.")
        sys.exit(1)

    installed: list[str] = []
    for name in skill_names:
        relative = BUNDLED_SKILLS.get(name)
        if relative is None:
            logger.error("Unknown skill '%s'. Registered skills: %s", name, list(BUNDLED_SKILLS))
            continue

        source = pkg_skills / name
        if isinstance(source, Path):
            source_path = source
        else:
            # importlib.resources < 3.9 compat — traversable
            import tempfile
            import os as _os
            source_path = Path(str(source))

        # Validate the skill has a SKILL.md before deploying
        skill_md = source_path / "SKILL.md"
        if not skill_md.is_file():
            logger.warning(
                "Skill '%s' is missing SKILL.md (expected at %s). Skipping.",
                name,
                skill_md,
            )
            continue

        dest = target / name
        if dry_run:
            action = "force-overwrite" if force else ("replace (with backup)" if dest.exists() and backup else "create")
            logger.info("[DRY-RUN] Would install '%s' → %s (%s)", name, dest, action)
            installed.append(name)
            continue

        if dest.exists():
            if not force and backup:
                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                backup_path = target / f"{name}.backup-{ts}"
                logger.info("Backing up existing '%s' → %s", name, backup_path.name)
                shutil.move(str(dest), str(backup_path))
            else:
                logger.info("Removing existing '%s' (force=%s)", name, force)
                shutil.rmtree(str(dest))

        try:
            shutil.copytree(str(source_path), str(dest))
            logger.info("Successfully deployed skill '%s' → %s", name, dest)
            installed.append(name)
        except OSError as exc:
            logger.error("Failed to copy skill '%s': %s", name, exc)
            if not dry_run:
                sys.exit(1)

    if dry_run:
        logger.info("\n[Dry run complete — no files were modified.]")
    else:
        logger.info("\nDeployment complete. Total skills deployed: %d", len(installed))

    return installed


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="install-agent-skills",
        description="Deploy bundled Agent Skills into a local `.agents/skills/` directory.",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=None,
        help="Destination directory (default: <cwd>/.agents/skills).",
    )
    parser.add_argument(
        "--skill",
        action="append",
        dest="skills",
        metavar="NAME",
        help="Install only this skill (repeatable). Omit to install all.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing installs without creating a backup.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be installed without touching the filesystem.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug-level output.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    args = parser.parse_args(argv)

    install_skills(
        target_dir=args.target,
        skills=args.skills,
        force=args.force,
        dry_run=args.dry_run,
        backup=not args.force,  # force disables backup
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
