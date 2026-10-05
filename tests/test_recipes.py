"""Tests for the recipe loader (enable / add_recipe_path / recipe_search_path)."""

from __future__ import annotations

import importlib
import re
import textwrap
from pathlib import Path

import pytest

import eosh.recipes as recipes_pkg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_search_path(monkeypatch, paths: list[Path]) -> None:
    """Replace recipe_search_path for the duration of a test."""
    monkeypatch.setattr(recipes_pkg, "recipe_search_path", list(paths))


def _write_recipe(directory: Path, name: str, side_effect_list: list) -> Path:
    """Write a minimal recipe that appends *name* to *side_effect_list* on register()."""
    path = directory / f"{name}.py"
    path.write_text(
        textwrap.dedent(f"""\
            _results = {side_effect_list!r}  # reference kept by caller

            def register():
                import eosh.recipes._test_results as _mod
                _mod.results.append({name!r})
        """)
    )
    return path


def _make_recipe(tmp_path: Path, name: str) -> Path:
    """Write a recipe that records its name in a shared results list."""
    path = tmp_path / f"{name}.py"
    path.write_text(
        textwrap.dedent(f"""\
            def register():
                import eosh.recipes._test_results as _mod
                _mod.results.append({name!r})
        """)
    )
    return path


# We use a tiny helper sub-module to collect side-effects across dynamically
# loaded recipe files (they can't share a list via closure easily).
@pytest.fixture(autouse=True)
def _result_module(monkeypatch):
    """Inject a fresh eosh.recipes._test_results module for each test."""
    import types
    mod = types.ModuleType("eosh.recipes._test_results")
    mod.results = []
    monkeypatch.setitem(
        importlib.import_module("eosh.recipes").__dict__,
        "_test_results_mod",
        mod,
    )
    import sys
    monkeypatch.setitem(sys.modules, "eosh.recipes._test_results", mod)
    return mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBuiltinRecipes:
    def test_builtin_recipe_loads(self):
        """enable() can load a built-in recipe without error."""
        # 'ls' is a lightweight built-in recipe (no external dependencies).
        # We just want to confirm it loads; we don't care about completers here.
        recipes_pkg.enable("ls")   # should not raise


class TestUserRecipes:
    def test_user_recipe_in_default_path(self, tmp_path, monkeypatch, _result_module):
        _reset_search_path(monkeypatch, [tmp_path])
        _make_recipe(tmp_path, "my_tool")

        recipes_pkg.enable("my_tool")

        assert _result_module.results == ["my_tool"]

    def test_not_found_raises_import_error(self, tmp_path, monkeypatch):
        _reset_search_path(monkeypatch, [tmp_path])

        with pytest.raises(ImportError, match="nonexistent"):
            recipes_pkg.enable("nonexistent")

    def test_error_message_lists_searched_dirs(self, tmp_path, monkeypatch):
        _reset_search_path(monkeypatch, [tmp_path])

        with pytest.raises(ImportError, match=re.escape(str(tmp_path))):
            recipes_pkg.enable("ghost")


class TestAddRecipePath:
    def test_add_recipe_path_appends(self, monkeypatch):
        _reset_search_path(monkeypatch, [])
        p = Path("/some/dir")

        recipes_pkg.add_recipe_path(p)

        assert recipes_pkg.recipe_search_path == [p]

    def test_add_recipe_path_accepts_string(self, monkeypatch):
        _reset_search_path(monkeypatch, [])

        recipes_pkg.add_recipe_path("/some/dir")

        assert recipes_pkg.recipe_search_path == [Path("/some/dir")]

    def test_first_match_wins(self, tmp_path, monkeypatch, _result_module):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()

        _make_recipe(dir_a, "shared")
        _make_recipe(dir_b, "shared")

        _reset_search_path(monkeypatch, [dir_a, dir_b])
        recipes_pkg.enable("shared")

        # Only the recipe from dir_a (first in path) should have fired.
        assert _result_module.results == ["shared"]

    def test_falls_through_to_second_dir(self, tmp_path, monkeypatch, _result_module):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()

        # Recipe only in dir_b.
        _make_recipe(dir_b, "only_in_b")

        _reset_search_path(monkeypatch, [dir_a, dir_b])
        recipes_pkg.enable("only_in_b")

        assert _result_module.results == ["only_in_b"]

    def test_builtin_takes_priority_over_search_path(self, tmp_path, monkeypatch, _result_module):
        """A user file named 'ls' must not shadow the built-in ls recipe."""
        _make_recipe(tmp_path, "ls")
        _reset_search_path(monkeypatch, [tmp_path])

        # Should load built-in ls, not our fake one — so results stay empty.
        recipes_pkg.enable("ls")

        assert _result_module.results == []


class TestEnableAll:
    """``enable("*")`` — discovery must only ever offer real recipes.

    ``enable()`` calls ``register()`` on whatever discovery returns, so a
    module without one turns a user's ``enable("*")`` into an
    ``AttributeError`` at config-load time.  That is not hypothetical: adding
    the support module ``_awsut_common.py`` next to the recipes broke exactly
    this, because the glob excluded only ``__init__``.
    """

    def test_every_discovered_builtin_has_a_register(self, monkeypatch):
        """The invariant enable("*") depends on, checked against the real package."""
        _reset_search_path(monkeypatch, [])

        names = recipes_pkg._discover_all_recipes()

        assert "ls" in names           # discovery still finds actual recipes
        for name in names:
            module = recipes_pkg._load_recipe(name)
            assert callable(getattr(module, "register", None)), (
                f"recipe {name!r} was discovered but has no register()"
            )

    def test_missing_dependency_skips_only_that_recipe(
            self, tmp_path, monkeypatch, _result_module):
        """``awsut`` without boto3 must not take the recipes after it down too.

        The failing recipe sorts first on purpose: a loop that aborted on it
        would never reach ``zzz_ok``.
        """
        (tmp_path / "aaa_needs_dep.py").write_text(
            "import eosh_no_such_dependency\n"
            "def register(): pass\n"
        )
        _make_recipe(tmp_path, "zzz_ok")
        _reset_search_path(monkeypatch, [tmp_path])
        monkeypatch.setattr(recipes_pkg, "_discover_all_recipes",
                            lambda: ["aaa_needs_dep", "zzz_ok"])
        monkeypatch.setattr(recipes_pkg, "skipped_recipes", {})

        recipes_pkg.enable("*")

        assert _result_module.results == ["zzz_ok"]
        assert recipes_pkg.skipped_recipes == {
            "aaa_needs_dep": "eosh_no_such_dependency"}

    def test_missing_dependency_still_raises_when_named(
            self, tmp_path, monkeypatch):
        (tmp_path / "needs_dep.py").write_text(
            "import eosh_no_such_dependency\n"
            "def register(): pass\n"
        )
        _reset_search_path(monkeypatch, [tmp_path])

        with pytest.raises(ModuleNotFoundError):
            recipes_pkg.enable("needs_dep")

    def test_awsut_without_boto3_names_the_extra(self, monkeypatch):
        """The real recipe: the error says how to fix it, and keeps ``e.name``."""
        import sys
        monkeypatch.setitem(sys.modules, "boto3", None)
        monkeypatch.delitem(sys.modules, "eosh.recipes.awsut", raising=False)

        with pytest.raises(ModuleNotFoundError, match=r"\[aws\]") as excinfo:
            recipes_pkg._load_recipe("awsut")
        assert excinfo.value.name == "boto3"

    def test_support_modules_are_not_discovered(self, monkeypatch):
        """A leading underscore means "imported by a recipe", not "is a recipe"."""
        _reset_search_path(monkeypatch, [])

        names = recipes_pkg._discover_all_recipes()

        assert "_awsut_common" not in names
        assert [n for n in names if n.startswith("_")] == []

    def test_user_helper_beside_a_user_recipe_is_skipped(
            self, tmp_path, monkeypatch, _result_module):
        """The same rule applies to a user's own shared helper module."""
        _make_recipe(tmp_path, "my_tool")
        (tmp_path / "_shared.py").write_text("# helper imported by my_tool\n")
        _reset_search_path(monkeypatch, [tmp_path])

        names = recipes_pkg._discover_all_recipes()

        assert "my_tool" in names
        assert "_shared" not in names
