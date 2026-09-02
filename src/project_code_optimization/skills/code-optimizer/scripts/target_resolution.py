"""Target resolution — expands a CLI target spec into concrete (file, func) pairs.

Both ``run_big_o.py`` and ``run_line_profile.py`` accept a ``--target-type`` of
``function`` (the original single-function behavior), ``file``, ``files``,
``commit``, or ``repo``. This module contains the shared logic to turn any of
those specs into an ordered list of ``(file_path, func_name)`` pairs that the
profilers can loop over.

Function discovery is limited to module-level ``def``/``async def`` statements
(no nested functions, no methods) because the profilers load a target by
``getattr(module, func_name)`` and call it directly — a bound method would
need a ``self``, which neither profiler synthesizes.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

# Directories skipped during repo-wide and commit-scoped discovery.
_EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", "build", "dist", "site-packages", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "*.egg-info",
}


class TargetResolutionError(ValueError):
    """Raised when a target spec cannot be resolved to any (file, func) pairs."""


def _is_excluded(path: Path) -> bool:
    parts = set(path.parts)
    if parts & _EXCLUDED_DIRS:
        return True
    return any(part.endswith(".egg-info") for part in path.parts)


def _is_test_file(path: Path) -> bool:
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def discover_functions(file_path: str) -> list[str]:
    """Return the names of module-level functions defined in *file_path*.

    Raises :exc:`TargetResolutionError` if the file does not exist or cannot
    be parsed as Python source.
    """
    path = Path(file_path)
    if not path.is_file():
        raise TargetResolutionError(f"File not found: '{file_path}'")
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise TargetResolutionError(f"Cannot parse '{file_path}': {exc}") from exc

    names = [
        node.name
        for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return names


def _pairs_for_file(file_path: str) -> list[tuple[str, str]]:
    return [(file_path, name) for name in discover_functions(file_path)]


def _discover_repo_py_files(repo_root: str) -> list[str]:
    root = Path(repo_root)
    if not root.is_dir():
        raise TargetResolutionError(f"Repository path not found: '{repo_root}'")
    found = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if _is_excluded(rel) or _is_test_file(path):
            continue
        found.append(str(path))
    return found


def _changed_py_files_for_commit(commit: str, repo_root: str) -> list[str]:
    """Return .py files changed by *commit*, as paths relative to cwd.

    Profiles the current working-tree contents of the changed files (not the
    historical blob) so that relative imports and package context still
    resolve the same way they would for any other target type.
    """
    try:
        result = subprocess.run(
            # --root makes a parentless (root) commit diff against the empty
            # tree, so its files show up instead of an empty result.
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", commit],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise TargetResolutionError("git executable not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise TargetResolutionError(
            f"Cannot resolve commit '{commit}': {stderr or exc}"
        ) from exc

    root = Path(repo_root)
    changed = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or not line.endswith(".py"):
            continue
        full = root / line
        if not full.is_file():
            # Deleted in a later state than what's checked out — skip.
            continue
        rel = Path(line)
        if _is_excluded(rel) or _is_test_file(full):
            continue
        changed.append(str(full))
    return changed


def resolve_targets(
    target_type: str,
    *,
    file: str | None = None,
    files: list[str] | None = None,
    func: str | None = None,
    commit: str | None = None,
    repo: str | None = None,
) -> list[tuple[str, str]]:
    """Resolve a target spec into an ordered list of ``(file_path, func_name)``.

    Parameters
    ----------
    target_type:
        One of ``"function"``, ``"file"``, ``"files"``, ``"commit"``, ``"repo"``.
    file:
        Required for ``target_type="function"`` or ``"file"``.
    files:
        Required for ``target_type="files"`` — a list of file paths.
    func:
        Required for ``target_type="function"``.
    commit:
        Required for ``target_type="commit"`` — a git commit-ish (sha, tag, ref).
    repo:
        Repository root to search/run git in. Defaults to the current
        directory for ``"repo"`` and ``"commit"`` target types.
    """
    if target_type == "function":
        if not file or not func:
            raise TargetResolutionError("target-type 'function' requires --file and --func")
        return [(file, func)]

    if target_type == "file":
        if not file:
            raise TargetResolutionError("target-type 'file' requires --file")
        pairs = _pairs_for_file(file)
        if not pairs:
            raise TargetResolutionError(f"No module-level functions found in '{file}'")
        return pairs

    if target_type == "files":
        if not files:
            raise TargetResolutionError("target-type 'files' requires --files")
        pairs: list[tuple[str, str]] = []
        for f in files:
            pairs.extend(_pairs_for_file(f))
        if not pairs:
            raise TargetResolutionError("No module-level functions found in any of the given files")
        return pairs

    if target_type == "commit":
        if not commit:
            raise TargetResolutionError("target-type 'commit' requires --commit")
        repo_root = repo or "."
        changed = _changed_py_files_for_commit(commit, repo_root)
        pairs = []
        for f in changed:
            try:
                pairs.extend(_pairs_for_file(f))
            except TargetResolutionError:
                continue
        if not pairs:
            raise TargetResolutionError(
                f"No profilable module-level functions found among files changed by "
                f"commit '{commit}'"
            )
        return pairs

    if target_type == "repo":
        repo_root = repo or "."
        py_files = _discover_repo_py_files(repo_root)
        pairs = []
        for f in py_files:
            try:
                pairs.extend(_pairs_for_file(f))
            except TargetResolutionError:
                continue
        if not pairs:
            raise TargetResolutionError(
                f"No profilable module-level functions found under '{repo_root}'"
            )
        return pairs

    raise TargetResolutionError(
        f"Unknown target-type '{target_type}'. Expected one of: "
        f"function, file, files, commit, repo."
    )
