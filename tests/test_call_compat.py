"""Tests for call compatibility: package-aware loading, multi-arg binding,
async execution, and shape cascades (probe follow-ups)."""

from __future__ import annotations

import importlib.resources
import importlib.util
import sys

import pytest


def _load_module(name: str, rel_path: str):
    pkg_skills = importlib.resources.files("project_code_optimization") / "skills"
    script_path = pkg_skills / rel_path
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tres = _load_module(
    "_test_cc_resolution", "code-optimizer/scripts/profilers/target_resolution.py"
)
big_o_mod = _load_module(
    "_test_cc_big_o", "code-optimizer/scripts/profilers/run_big_o.py"
)
line_mod = _load_module(
    "_test_cc_line", "code-optimizer/scripts/profilers/run_line_profile.py"
)


def _pkg_tree(tmp_path, pkg_name="ccpkg"):
    """A package whose __init__ must never execute, with relative imports."""
    pkg = tmp_path / pkg_name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("raise RuntimeError('heavy init ran')\n")
    (pkg / "sibling.py").write_text("VALUE = 41\n")
    (pkg / "mod.py").write_text(
        "from . import sibling\n"
        "from ccpkg import sibling as sib2\n"
        "\n"
        "def f(n):\n"
        "    return sibling.VALUE + sib2.VALUE + len(n)\n"
    )
    return pkg / "mod.py"


class TestPackageContextLoading:
    def test_relative_and_absolute_intra_repo_imports_resolve(self, tmp_path):
        mod_path = _pkg_tree(tmp_path)
        module = tres.load_module_at_path(str(mod_path))
        assert module.f([1, 2, 3]) == 85

    def test_heavy_init_never_executes(self, tmp_path):
        _pkg_tree(tmp_path)
        # Would raise RuntimeError if the real __init__ ran.
        tres.load_module_at_path(str(tmp_path / "ccpkg" / "mod.py"))

    def test_stubs_do_not_clobber_real_packages(self, tmp_path):
        import json  # a real package present in sys.modules

        _pkg_tree(tmp_path)
        tres.load_module_at_path(str(tmp_path / "ccpkg" / "mod.py"))
        assert sys.modules["json"] is json

    def test_plain_files_unchanged(self, tmp_path):
        mod = tmp_path / "plain.py"
        mod.write_text("def f(xs):\n    return list(xs)\n")
        assert tres.load_module_at_path(str(mod)).f([1]) == [1]

    def test_missing_third_party_dep_still_names_it(self, tmp_path):
        mod = tmp_path / "needsdep.py"
        mod.write_text("import surely_missing_dep_xyz\ndef f():\n    return 1\n")
        with pytest.raises(ModuleNotFoundError, match="surely_missing_dep_xyz"):
            tres.load_module_at_path(str(mod))

    def test_same_dir_absolute_import_resolves(self, tmp_path):
        (tmp_path / "helper_cc.py").write_text("K = 7\n")
        mod = tmp_path / "main_cc.py"
        mod.write_text("from helper_cc import K\ndef f():\n    return K\n")
        assert tres.load_module_at_path(str(mod)).f() == 7

    def test_missing_file_raises_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="File not found"):
            tres.load_module_at_path(str(tmp_path / "nope.py"))

    def test_load_function_end_to_end(self, tmp_path):
        mod_path = _pkg_tree(tmp_path)
        func = line_mod.load_function(str(mod_path), "f")
        assert func([1, 2]) == 84

    def test_deferred_relative_import_resolves_at_call_time(self, tmp_path):
        pkg = tmp_path / "dcpkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "sibling.py").write_text("VALUE = 5\n")
        mod = pkg / "mod.py"
        mod.write_text(
            "def f(n):\n"
            "    from . import sibling\n"
            "    return sibling.VALUE + len(n)\n"
        )
        func = line_mod.load_function(str(mod), "f")
        assert func([1, 2, 3]) == 8

    def test_stub_bypass_note_on_dunder_init_names(self, tmp_path):
        # A trivially importable __init__ is executed for real now; the
        # stub-bypass note applies when the real __init__ chain is
        # unavailable (heavy third-party imports), so simulate that.
        pkg = tmp_path / "nipkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(
            "import definitely_missing_dep_xyz\nREAL_NAME = 1\n"
        )
        mod = pkg / "mod.py"
        mod.write_text("from nipkg import REAL_NAME\ndef f():\n    return REAL_NAME\n")
        with pytest.raises(ImportError, match="__init__ bypassed by stubs"):
            tres.load_module_at_path(str(mod))

    def test_retry_error_preferred_over_plain_symptom(self, tmp_path):
        pkg = tmp_path / "rpkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "b.py").write_text("raise ImportError('synthetic deep failure')\n")
        (pkg / "a.py").write_text("from . import b\ndef f():\n    return 1\n")
        # Plain load fails with the "no known parent package" symptom; the
        # package-context retry gets further and names the real problem.
        with pytest.raises(ImportError, match="synthetic deep failure"):
            tres.load_module_at_path(str(pkg / "a.py"))

    def test_real_installed_parent_used_over_stubs(self, tmp_path, monkeypatch):
        pkg = tmp_path / "realpkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("SHARED = 'from-real-init'\n")
        (pkg / "mod.py").write_text("from realpkg import SHARED\ndef f():\n    return SHARED\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        module = tres.load_module_at_path(str(pkg / "mod.py"))
        assert module.f() == "from-real-init"
        assert not getattr(sys.modules["realpkg"], "_code_optimizer_stub", False)

    def test_foreign_install_never_shadows_target_tree(self, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        target = proj / "fgnpkg"
        target.mkdir(parents=True)
        (target / "__init__.py").write_text("MARKER = 'target'\n")
        (target / "mod.py").write_text("def f():\n    return 1\n")
        other = tmp_path / "other" / "fgnpkg"
        other.mkdir(parents=True)
        (other / "__init__.py").write_text("MARKER = 'foreign'\n")
        # Simulate a same-named install imported earlier in the process
        # (e.g. site-packages): it must not win over the target tree.
        monkeypatch.syspath_prepend(str(tmp_path / "other"))
        import fgnpkg  # noqa: F401 -- foreign copy now cached in sys.modules

        fullname = tres._ensure_parent_stubs(str(target / "mod.py"))
        assert fullname == "fgnpkg.mod"
        assert sys.modules["fgnpkg"].MARKER == "target"


class TestDiscoveryHygiene:
    def test_repo_skips_demo_test_trees_and_entry_points(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "demo_x.py").write_text("def demo_run():\n    return 1\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "helper.py").write_text("def util():\n    return 1\n")
        (tmp_path / "core.py").write_text(
            "def real(xs):\n    return list(xs)\n"
            "def demo_main():\n    return 2\n"
            "def test_check():\n    return 3\n"
        )
        pairs = tres.resolve_targets("repo", repo=str(tmp_path))
        assert pairs == [(str(tmp_path / "core.py"), "real")]

    def test_explicit_file_keeps_entry_points(self, tmp_path):
        mod = tmp_path / "mod.py"
        mod.write_text("def demo_run():\n    return 1\ndef real():\n    return 2\n")
        pairs = tres.resolve_targets("file", file=str(mod))
        assert [fn for _, fn in pairs] == ["demo_run", "real"]


class TestEnsureSyncCallable:
    def test_plain_callable_passes_through(self):
        def f():
            return 1

        wrapped, note = tres.ensure_sync_callable(f)
        assert wrapped is f and note == ""

    def test_coroutine_runs_to_value(self):
        async def f(xs):
            return len(xs)

        wrapped, note = tres.ensure_sync_callable(f)
        assert note == "async"
        assert wrapped([1, 2, 3]) == 3

    def test_asyncgen_raises_clearly(self):
        async def g():
            yield 1

        with pytest.raises(ValueError, match="async generator"):
            tres.ensure_sync_callable(g)


class TestBigOBinder:
    def test_multi_arg_profiles_with_scaling(self):
        def f(page, text):
            return len(text) + page

        best_fit, _ = big_o_mod.profile_big_o(
            f, min_n=10, max_n=60, n_measures=3
        )
        assert isinstance(best_fit, str) and best_fit

    def test_unannotated_str_eater_picks_str_shape(self):
        def f(blob):
            return blob.split()

        best_fit, _ = big_o_mod.profile_big_o(
            f, min_n=10, max_n=60, n_measures=3
        )
        assert isinstance(best_fit, str) and best_fit

    def test_zero_arg_fails_fast_with_clear_message(self):
        def f():
            return 1

        with pytest.raises(ValueError, match="takes no arguments"):
            big_o_mod.profile_big_o(f, min_n=10, max_n=30, n_measures=2)

    def test_impossible_shape_fails_with_harness_pointer(self):
        def f(conn):
            return conn.resource_types

        with pytest.raises(ValueError, match="harness"):
            big_o_mod.profile_big_o(f, min_n=10, max_n=30, n_measures=2)

    def test_size_name_hint_prefers_text_over_page(self):
        def clean(page, text):
            return text.split()

        best_fit, _ = big_o_mod.profile_big_o(
            clean, min_n=10, max_n=200, n_measures=3
        )
        # Scaling `text` (not `page`) must win: split cost grows with input.
        assert best_fit != "Constant"

    def test_int_eating_counter_profiles(self):
        def paeth(a, b, c):
            p = a + b - c
            return p if p >= 0 else 0

        best_fit, _ = big_o_mod.profile_big_o(
            paeth, min_n=10, max_n=60, n_measures=3
        )
        assert isinstance(best_fit, str) and best_fit

    def test_async_profiles_body_not_creation(self):
        async def f(xs):
            total = 0
            for x in xs:
                total += x * x
            return total

        best_fit, _ = big_o_mod.profile_big_o(
            f, min_n=50, max_n=800, n_measures=4
        )
        # Awaiting the body must show scaling work -- coroutine creation
        # alone would fit a flat Constant.
        assert best_fit != "Constant"

    def test_asyncgen_fails_clearly(self):
        async def g(xs):
            for x in xs:
                yield x

        with pytest.raises(ValueError, match="async generator"):
            big_o_mod.profile_big_o(g, min_n=10, max_n=30, n_measures=2)


class TestLineCascade:
    def test_unannotated_str_eater_now_profiles(self):
        def f(blob):
            return blob.split()

        stats = line_mod.compute_line_stats(f)
        assert not stats.get("error")
        assert stats["total_hits"] > 0

    def test_multi_arg_text_eater_now_profiles(self):
        def f(page, text):
            return len(text.split()) + page

        stats = line_mod.compute_line_stats(f)
        assert not stats.get("error")
        assert stats["total_hits"] > 0

    def test_multi_arg_counter_now_profiles(self):
        def f(a, b, c):
            return a + b - c

        stats = line_mod.compute_line_stats(f)
        assert not stats.get("error")
        assert stats["total_hits"] > 0

    def test_async_profiles_body(self):
        async def f(xs):
            return [x for x in xs]

        stats = line_mod.compute_line_stats(f)
        assert not stats.get("error")
        assert stats["total_hits"] > 0

    def test_asyncgen_reports_error_row(self):
        async def g(xs):
            for x in xs:
                yield x

        stats = line_mod.compute_line_stats(g)
        assert "async generator" in stats.get("error", "")
