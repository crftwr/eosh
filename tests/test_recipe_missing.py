"""A recipe skipped for a missing dependency explains itself where it's used.

Covers the one-line hint (``eosh.recipes.missing_message``) and the
placeholder command ``enable("*")`` registers in the skipped recipe's place.
"""

from __future__ import annotations

import shutil

import pytest

import eosh.recipes as recipes_pkg
from eosh.commands import registry
from eosh.recipes import missing_message


# ---------------------------------------------------------------------------
# missing_message
# ---------------------------------------------------------------------------

def test_message_for_an_addon_names_its_extra():
    """Add-ons name their extra after themselves, so no lookup table."""
    assert missing_message("awsut", "boto3") == (
        "awsut: needs the Python module 'boto3' — install eosh[awsut]"
    )


def test_message_for_a_user_recipe_names_only_the_module():
    msg = missing_message("my_tool", "requests")
    assert "'requests'" in msg
    assert "eosh[" not in msg


# ---------------------------------------------------------------------------
# enable("*") → placeholder command
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """A search path holding one recipe whose dependency is missing."""
    (tmp_path / "needs_dep.py").write_text(
        "import eosh_no_such_dependency\n"
        "def register(): pass\n"
    )
    monkeypatch.setattr(recipes_pkg, "recipe_search_path", [tmp_path])
    monkeypatch.setattr(recipes_pkg, "_discover_all_recipes", lambda: ["needs_dep"])
    monkeypatch.setattr(recipes_pkg, "skipped_recipes", {})
    monkeypatch.setattr(registry, "_commands", dict(registry._commands))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    return tmp_path


def test_skipped_recipe_registers_a_placeholder(isolated, capsys):
    recipes_pkg.enable("*")

    cmd = registry.get("needs_dep")
    assert cmd is not None
    assert "unavailable" in cmd.help_text
    with pytest.raises(SystemExit) as excinfo:
        cmd.invoke(["any", "args"])
    assert excinfo.value.code == 127
    err = capsys.readouterr().err
    assert "needs_dep: needs the Python module 'eosh_no_such_dependency'" in err


def test_placeholder_never_shadows_an_executable(isolated, monkeypatch):
    """A completion-only recipe for ``git`` must leave ``git`` runnable."""
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    recipes_pkg.enable("*")
    assert registry.get("needs_dep") is None


def test_placeholder_never_replaces_a_registered_command(isolated):
    sentinel = registry.command("needs_dep", help="the real one")
    recipes_pkg.enable("*")
    assert registry.get("needs_dep") is sentinel
