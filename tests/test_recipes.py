"""Tests for the recipe loader: enable() over built-in recipes and add-ons.

Your own recipes aren't looked up by the loader (discussion #40): they are
defined in config.py, or in a module it imports.
"""

from __future__ import annotations

import sys
import types

import pytest

import eosh.recipes as recipes_pkg
from eosh.commands import registry as command_registry


@pytest.fixture(autouse=True)
def _isolate_commands(monkeypatch):
    monkeypatch.setattr(command_registry, "_commands", dict(command_registry._commands))
    monkeypatch.setattr(recipes_pkg, "skipped_recipes", {})


def _fake_recipe(monkeypatch, name: str, register) -> None:
    """Install a built-in-looking recipe module ``eosh.recipes.<name>``."""
    mod = types.ModuleType(f"eosh.recipes.{name}")
    mod.register = register
    monkeypatch.setitem(sys.modules, f"eosh.recipes.{name}", mod)


def _without_boto3(monkeypatch) -> None:
    """Make the awsut add-on import as if boto3 were not installed."""
    monkeypatch.setitem(sys.modules, "boto3", None)
    for mod in [m for m in sys.modules if m.startswith("eosh_addons.awsut")]:
        monkeypatch.delitem(sys.modules, mod)


class TestLookup:
    def test_a_builtin_recipe_loads(self):
        recipes_pkg.enable("ls")
        assert command_registry.has("ls")

    def test_an_addon_loads(self):
        recipes_pkg.enable("awsut")
        assert command_registry.get("awsut").has_any_handler()

    def test_an_unknown_name_is_a_warning_and_the_rest_still_load(self, capsys):
        recipes_pkg.enable("no_such", "ls")
        err = capsys.readouterr().err
        assert "config warning: No recipe or add-on named 'no_such'" in err
        assert "Traceback" not in err          # a typo needs no traceback
        assert command_registry.has("ls")

    def test_there_are_no_user_search_paths(self):
        assert not hasattr(recipes_pkg, "recipe_search_path")
        assert not hasattr(recipes_pkg, "add_recipe_path")


class TestAbsentTools:
    """enable() drops a recipe's command when the tool isn't installed, so
    recipes don't each check PATH themselves."""

    def test_a_command_for_a_missing_tool_is_dropped(self, monkeypatch):
        def register():
            command_registry.command("eosh-no-such-tool", help="recipe")
        _fake_recipe(monkeypatch, "_t_absent", register)
        recipes_pkg.enable("_t_absent")
        assert not command_registry.has("eosh-no-such-tool")

    def test_each_name_a_recipe_registers_is_checked_on_its_own(self, monkeypatch):
        def register():
            command_registry.command("sh", help="present everywhere")
            command_registry.command("eosh-no-such-tool", help="absent")
        _fake_recipe(monkeypatch, "_t_mixed", register)
        recipes_pkg.enable("_t_mixed")
        assert command_registry.has("sh")
        assert not command_registry.has("eosh-no-such-tool")

    def test_a_python_command_is_never_dropped(self, monkeypatch):
        def register():
            @command_registry.command("eosh-python-cmd")
            def handler():
                pass
        _fake_recipe(monkeypatch, "_t_python", register)
        recipes_pkg.enable("_t_python")
        assert command_registry.has("eosh-python-cmd")

    def test_a_name_that_was_already_registered_is_left_alone(self, monkeypatch):
        command_registry.command("eosh-no-such-tool", help="the user's own")
        def register():
            command_registry.command("eosh-no-such-tool", help="recipe")
        _fake_recipe(monkeypatch, "_t_known", register)
        recipes_pkg.enable("_t_known")
        assert command_registry.has("eosh-no-such-tool")


class TestEnableAll:
    """``enable("*")`` — discovery must only ever offer real recipes, since
    ``enable()`` calls ``register()`` on whatever it returns."""

    def test_every_discovered_name_has_a_register(self):
        names = recipes_pkg._discover_all_recipes()
        assert "ls" in names and "awsut" in names
        for name in names:
            module = recipes_pkg._load_recipe(name)
            assert callable(getattr(module, "register", None)), name

    def test_support_modules_are_not_discovered(self):
        """A leading underscore means "imported by a recipe", not "is a recipe"."""
        assert [n for n in recipes_pkg._discover_all_recipes() if n.startswith("_")] == []

    def test_an_addon_missing_a_dependency_is_skipped_quietly(self, monkeypatch, capsys):
        """``awsut`` without the eosh[awsut] extra is a choice, not an error —
        and the recipes after it still load."""
        _without_boto3(monkeypatch)
        monkeypatch.setattr(recipes_pkg, "_discover_all_recipes", lambda: ["awsut", "ls"])
        monkeypatch.setattr(recipes_pkg, "_register_unavailable", lambda *a: None)

        recipes_pkg.enable("*")

        assert recipes_pkg.skipped_recipes == {"awsut": "boto3"}
        assert command_registry.has("ls")
        assert capsys.readouterr().err == ""

    def test_named_explicitly_it_warns_with_the_extra(self, monkeypatch, capsys):
        _without_boto3(monkeypatch)
        monkeypatch.setattr(recipes_pkg, "_register_unavailable", lambda *a: None)
        recipes_pkg.enable("awsut")
        assert recipes_pkg.skipped_recipes == {"awsut": "boto3"}
        assert capsys.readouterr().err == (
            "config warning: awsut: needs the Python module 'boto3' "
            "— install eosh[awsut]\n")

    def test_a_builtin_recipe_bug_is_reported_and_the_rest_still_load(
            self, monkeypatch, capsys):
        def register():
            raise ValueError("bug in a recipe")
        _fake_recipe(monkeypatch, "_t_buggy", register)
        monkeypatch.setattr(recipes_pkg, "_discover_all_recipes",
                            lambda: ["_t_buggy", "ls"])
        recipes_pkg.enable("*")
        err = capsys.readouterr().err
        assert "config warning: enable('_t_buggy') failed:" in err
        assert "ValueError: bug in a recipe" in err   # with its traceback
        assert command_registry.has("ls")
