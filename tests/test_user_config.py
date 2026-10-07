"""Tests for loading ``~/.eosh/config.py`` (``Shell._load_user_config``)."""

from __future__ import annotations

import sys

from eosh import shell as shell_mod
from eosh.shell import Shell

# Captured at import time, before conftest's autouse ``_no_user_config``
# replaces the method for every test.
_real_load_user_config = Shell.__dict__["_load_user_config"]


def test_first_launch_writes_and_loads_the_starter_config(tmp_path, monkeypatch):
    """A fresh install gets its recipes on the first launch, not the second."""
    starter = tmp_path / "starter.py"
    starter.write_text("import sys\nsys._eosh_test_config_loaded = True\n")
    home = tmp_path / "home"
    monkeypatch.setattr(shell_mod, "config_dir", lambda: home / ".eosh")
    monkeypatch.setattr(shell_mod, "_DEFAULT_CONFIG_PATH", starter)
    monkeypatch.setattr(sys, "_eosh_test_config_loaded", False, raising=False)

    _real_load_user_config(Shell.__new__(Shell))

    assert (home / ".eosh" / "config.py").read_text() == starter.read_text()
    assert sys._eosh_test_config_loaded is True


def test_config_imports_its_own_modules_and_reload_runs_them_again(tmp_path, monkeypatch):
    """~/.eosh is on sys.path while config.py runs, so your own recipes and
    decorators live in modules there — and `reload` re-runs them, since it
    just cleared what they registered."""
    home = tmp_path / ".eosh"
    home.mkdir()
    (home / "config.py").write_text("import eosh_t_my_tools\n")
    (home / "eosh_t_my_tools.py").write_text(
        "import sys\nsys._eosh_t_runs = getattr(sys, '_eosh_t_runs', 0) + 1\n")
    monkeypatch.setattr(shell_mod, "config_dir", lambda: home)
    monkeypatch.setattr(sys, "_eosh_t_runs", 0, raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    try:
        _real_load_user_config(Shell.__new__(Shell))
        _real_load_user_config(Shell.__new__(Shell))      # what `reload` does
        assert sys._eosh_t_runs == 2
    finally:
        sys.modules.pop("eosh_t_my_tools", None)
