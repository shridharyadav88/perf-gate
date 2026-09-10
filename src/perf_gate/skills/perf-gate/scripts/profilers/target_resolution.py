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
import functools
import importlib.util
import os
import subprocess
import sys
import types
from pathlib import Path

# Directories skipped during repo-wide and commit-scoped discovery.
# Beyond tooling/artifact dirs, demo and test trees are excluded on purpose:
# they contain programs (dashboards, server polls, model downloads), not
# units -- executing them with synthetic arguments produces noise (or side
# effects), never performance signal.
_EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", "build", "dist", "site-packages", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "*.egg-info",
    "docs", "examples", "deploy", "demo", "demos", "tests", "testing",
}

# Module-level function names skipped during repo/commit discovery (broad
# sweeps): entry points, not units. Explicit file/files targets still
# include them -- pointing at a file is a deliberate choice.
_ENTRY_POINT_PREFIXES = ("test_", "demo_")


def _pairs_for_file(
    file_path: str, *, skip_entry_points: bool = False
) -> list[tuple[str, str]]:
    names = discover_functions(file_path)
    if skip_entry_points:
        names = [n for n in names if not n.startswith(_ENTRY_POINT_PREFIXES)]
    return [(file_path, name) for name in names]


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


def _package_chain(file_path: str) -> tuple[str, list[str], str] | None:
    """Split *file_path* into (dotted_name, parent_dotted_names, repo_root).

    Returns ``None`` when the file sits outside any package (no enclosing
    ``__init__.py``). ``repo_root`` is the first directory above the package
    chain -- the entry that belongs on ``sys.path``.
    """
    path = Path(file_path).absolute()
    parts = [path.stem] if path.stem != "__init__" else []
    directory = path.parent
    chain: list[str] = []
    while (directory / "__init__.py").is_file():
        chain.append(directory.name)
        directory = directory.parent
    if not chain:
        return None
    dotted = list(reversed(chain)) + parts
    parents = [".".join(dotted[:i]) for i in range(1, len(dotted))]
    return ".".join(dotted), parents, str(directory)


def _module_roots(module) -> list[str]:
    """Resolved ``__path__`` entries of *module* (empty for non-packages)."""
    roots = []
    for path in getattr(module, "__path__", []) or []:
        try:
            roots.append(str(Path(path).resolve()))
        except OSError:
            continue
    return roots


def _inside_root(roots: list[str], repo_root: str) -> bool:
    """True when every root lives inside *repo_root* (the target tree)."""
    want = str(Path(repo_root).resolve())
    return bool(roots) and all(r == want or r.startswith(want + os.sep) for r in roots)


def _real_parent_importable(parent: str, repo_root: str):
    """Import the real *parent* package, or return None to signal stubs.

    The real package wins only when it resolves INSIDE *repo_root* (never a
    stale same-named install from site-packages shadowing the target
    snapshot) -- executing the genuine ``__init__`` chain then provides the
    re-exported names and import order stubs deliberately lack. Any import
    failure (missing third-party deps included) falls back to stubs.
    """
    try:
        module = importlib.import_module(parent)
    except Exception:
        return None
    if _inside_root(_module_roots(module), repo_root):
        return module
    return None


def _evict_subtree(parent: str) -> None:
    """Drop *parent* and its submodules from ``sys.modules`` (re-resolvable)."""
    for key in [k for k in sys.modules if k == parent or k.startswith(parent + ".")]:
        sys.modules.pop(key, None)


def format_target_listing(
    target_type: str, pairs: list[tuple[str, str]], max_targets: int, truncated: bool,
) -> str:
    """Render a ``--dry-run`` listing: what WOULD be profiled, nothing executed.

    Resolution itself is AST + git only (never imports or calls target
    code), so printing this proves the scope without any side effects.
    """
    lines = [f"=== DRY RUN ({target_type}) - {len(pairs)} target(s) ==="]
    if truncated:
        lines.append(
            f"(truncated to --max-targets={max_targets}; more targets were resolved)"
        )
    lines.append("No imports, no calls -- discovery only.")
    lines.extend(f"{file_path} :: {func_name}" for file_path, func_name in pairs)
    return "\n".join(lines) + "\n"


def _ensure_parent_stubs(file_path: str) -> str | None:
    """Register stub parent packages for *file_path*; return dotted name or None.

    Shared by both load paths: when the target package is properly installed
    (importable with its ``__init__`` chain resolving inside the target
    tree), the real package is used; otherwise lightweight namespace stubs
    stand in so heavy ``__init__`` chains never execute. Real packages in
    ``sys.modules`` are never overwritten (stale stubs from elsewhere get
    replaced), and the package root joins ``sys.path`` for the process
    lifetime. Returns the file's dotted name, or ``None`` when the file sits
    outside any package.
    """
    info = _package_chain(file_path)
    if info is None:
        return None
    fullname, parents, repo_root = info

    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    for parent in parents:
        existing = sys.modules.get(parent)
        if existing is not None and not getattr(existing, "_perf_gate_stub", False):
            if _inside_root(_module_roots(existing), repo_root):
                continue  # the target tree itself; never overwrite it
            # A real but FOREIGN package (same name imported earlier from
            # elsewhere): evict the subtree so the target tree resolves
            # instead of silently profiling the wrong code.
            _evict_subtree(parent)
        if _real_parent_importable(parent, repo_root) is not None:
            continue  # genuine __init__ chain in place; stubs unneeded
        parent_dir = repo_root
        for bit in parent.split("."):
            parent_dir = str(Path(parent_dir) / bit)
        stub = types.ModuleType(parent)
        stub.__path__ = [parent_dir]  # type: ignore[attr-defined]
        stub._perf_gate_stub = True  # type: ignore[attr-defined]
        sys.modules[parent] = stub

    return fullname


def _exec_with_package_context(file_path: str):
    """Exec *file_path* under its dotted name with stub parents (see above)."""
    fullname = _ensure_parent_stubs(file_path)
    assert fullname is not None
    abs_path = str(Path(file_path).absolute())

    spec = importlib.util.spec_from_file_location(fullname, abs_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from '{file_path}'")
    module = importlib.util.module_from_spec(spec)
    sys.modules[fullname] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(fullname, None)
        raise
    return module


def load_module_at_path(file_path: str, *, module_name: str = "_target_module"):
    """Load the file at *file_path* as a module object (shared loader).

    Strategy, first success wins:

    1. Plain file-location load (historical behavior; zero side effects
       beyond executing the file itself).
    2. On :exc:`ImportError`, if the file lives in a package: package-context
       load with stub parents (``__init__`` chains never execute) so
       relative and intra-repo absolute imports resolve.
    3. On :exc:`ModuleNotFoundError` without a package: retry with the
       file's own directory transiently on ``sys.path`` (same-directory
       absolute imports like ``from database import X``).

    Raises :exc:`FileNotFoundError` for a missing file. Otherwise raises the
    most informative failure: when a retry was attempted, the retry's error
    wins -- the retry executes strictly more of the module (relatives
    resolved), so its failure names the real problem (a missing third-party
    dependency, an import cycle only the real ``__init__`` order survives)
    instead of the plain load's "no known parent package" symptom. With a
    single attempt that error is raised unchanged.
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: '{file_path}'")

    errors: list[Exception] = []
    try:
        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load module from '{file_path}'")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Plain load succeeded: still register the package context so
        # DEFERRED (in-function) relative/sibling imports resolve at call
        # time instead of failing with "no known parent package".
        fullname = _ensure_parent_stubs(file_path)
        if fullname is not None:
            module.__package__ = fullname.rpartition(".")[0]
            sys.modules.setdefault(fullname, module)
        return module
    except ImportError as exc:
        errors.append(exc)

    info = _package_chain(file_path)
    if info is not None:
        try:
            return _exec_with_package_context(file_path)
        except ImportError as exc:
            errors.append(exc)
    else:
        directory = str(path.parent.absolute())
        if directory not in sys.path:
            sys.path.insert(0, directory)
            try:
                spec = importlib.util.spec_from_file_location(module_name, str(path))
                if spec is None or spec.loader is None:
                    raise ImportError(f"Cannot load module from '{file_path}'")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module
            except ImportError as exc:
                errors.append(exc)
            finally:
                try:
                    sys.path.remove(directory)
                except ValueError:
                    pass

    unknown_loc = next(
        (exc for exc in errors if "(unknown location)" in str(exc)), None
    )
    if unknown_loc is not None and _package_chain(file_path) is not None:
        # The name lives in the real package __init__, which stubs
        # deliberately never execute: only the installed package (with its
        # own dependencies) can provide it.
        raise ImportError(
            f"{unknown_loc} [package __init__ bypassed by stubs; "
            "names defined there need the installed package]"
        ) from unknown_loc
    raise errors[-1]


def ensure_sync_callable(func):
    """Wrap coroutine functions so profilers can call them synchronously.

    Returns ``(callable, note)`` where *note* is ``""`` for plain callables.
    Async generator functions cannot run to a value -- they raise a clear
    :exc:`ValueError` instead of producing a silently meaningless reading.
    """
    import asyncio
    import inspect

    if inspect.isasyncgenfunction(func):
        raise ValueError(
            f"'{getattr(func, '__name__', func)}' is an async generator; "
            "profilers need a callable returning a value"
        )
    if inspect.iscoroutinefunction(func):
        @functools.wraps(func)
        def _runner(*args, **kwargs):
            return asyncio.run(func(*args, **kwargs))

        return _runner, "async"
    return func, ""


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
                pairs.extend(_pairs_for_file(f, skip_entry_points=True))
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
                pairs.extend(_pairs_for_file(f, skip_entry_points=True))
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
