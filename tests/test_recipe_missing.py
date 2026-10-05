"""A recipe skipped for a missing dependency explains itself where it's used.

Covers ``eosh.recipes._missing`` (which install command fits *this*
environment) and the placeholder command ``enable("*")`` registers in the
skipped recipe's place.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

import eosh.recipes as recipes_pkg
from eosh.commands import registry
from eosh.recipes import _missing
from eosh.recipes._missing import install_command, missing_message


def _uv_venv(tmp_path: Path, requirements: str) -> Path:
    (tmp_path / "uv-receipt.toml").write_text(
        f"[tool]\nrequirements = [\n{requirements}\n]\n"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# install_command
# ---------------------------------------------------------------------------

class TestUvTool:
    def test_extra_is_added_to_eosh(self, tmp_path):
        venv = _uv_venv(tmp_path, '{ name = "eosh" },')
        assert install_command(extra="aws", prefix=venv) == \
            "uv tool install 'eosh[aws]'"

    def test_existing_with_entries_are_kept(self, tmp_path):
        """``--with`` replaces the list, so the command must repeat it."""
        venv = _uv_venv(tmp_path, (
            '{ name = "eosh", specifier = ">=0.1" },'
            '{ name = "six", specifier = "==1.17.0" },'
            '{ name = "idna" },'
        ))
        assert install_command(module="yaml", prefix=venv) == (
            "uv tool install 'eosh>=0.1' --with six==1.17.0 "
            "--with idna --with yaml"
        )

    def test_existing_extras_are_kept(self, tmp_path):
        venv = _uv_venv(tmp_path, '{ name = "eosh", extras = ["k8s"] },')
        assert install_command(extra="aws", prefix=venv) == \
            "uv tool install 'eosh[k8s,aws]'"

    def test_package_already_listed_is_not_repeated(self, tmp_path):
        venv = _uv_venv(tmp_path, '{ name = "eosh" }, { name = "requests" },')
        assert install_command(module="requests.adapters", prefix=venv) == \
            "uv tool install eosh --with requests"

    def test_unreconstructable_requirement_falls_back(self, tmp_path):
        venv = _uv_venv(tmp_path, '{ name = "eosh", directory = "/src/eosh" },')
        cmd = install_command(extra="aws", prefix=venv)
        assert cmd.startswith("uv tool install eosh[aws]")
        assert "uv tool list --show-with" in cmd

    def test_corrupt_receipt_falls_back(self, tmp_path):
        (tmp_path / "uv-receipt.toml").write_text("not = [toml")
        cmd = install_command(module="yaml", prefix=tmp_path)
        assert cmd.startswith("uv tool install eosh --with yaml")


class TestPipx:
    def test_module_is_injected(self, tmp_path):
        (tmp_path / "pipx_metadata.json").write_text(
            json.dumps({"main_package": {"package": "eosh"}}))
        assert install_command(module="yaml", prefix=tmp_path) == \
            "pipx inject eosh yaml"

    def test_extra_injects_its_requirements(self, tmp_path, monkeypatch):
        (tmp_path / "pipx_metadata.json").write_text("{}")
        monkeypatch.setattr(_missing, "_extra_requirements",
                            lambda extra: ["boto3", "pexpect"])
        assert install_command(extra="aws", prefix=tmp_path) == \
            "pipx inject eosh boto3 pexpect"


def test_plain_interpreter_uses_its_own_pip(tmp_path):
    cmd = install_command(extra="aws", prefix=tmp_path)
    assert cmd.endswith("-m pip install 'eosh[aws]'")
    assert sys.executable in cmd


def test_message_for_a_builtin_extra_names_it(tmp_path):
    msg = missing_message("awsut", "boto3", prefix=_uv_venv(tmp_path, '{ name = "eosh" },'))
    assert "[aws] extra" in msg
    assert "uv tool install 'eosh[aws]'" in msg


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
    assert "needs_dep: unavailable" in err
    assert "eosh_no_such_dependency" in err


def test_placeholder_never_shadows_an_executable(isolated, monkeypatch):
    """A completion-only recipe for ``git`` must leave ``git`` runnable."""
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    recipes_pkg.enable("*")
    assert registry.get("needs_dep") is None


def test_placeholder_never_replaces_a_registered_command(isolated):
    sentinel = registry.command("needs_dep", help="the real one")
    recipes_pkg.enable("*")
    assert registry.get("needs_dep") is sentinel
