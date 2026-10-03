"""Tests for loading ``~/.pitash/config.py`` (``Shell._load_user_config``)."""

from __future__ import annotations

import sys

from pitash import shell as shell_mod
from pitash.shell import Shell

# Captured at import time, before conftest's autouse ``_no_user_config``
# replaces the method for every test.
_real_load_user_config = Shell.__dict__["_load_user_config"]


def test_first_launch_writes_and_loads_the_starter_config(tmp_path, monkeypatch):
    """A fresh install gets its recipes on the first launch, not the second."""
    starter = tmp_path / "starter.py"
    starter.write_text("import sys\nsys._pitash_test_config_loaded = True\n")
    home = tmp_path / "home"
    monkeypatch.setattr(shell_mod, "config_dir", lambda: home / ".pitash")
    monkeypatch.setattr(shell_mod, "_DEFAULT_CONFIG_PATH", starter)
    monkeypatch.setattr(sys, "_pitash_test_config_loaded", False, raising=False)

    _real_load_user_config(Shell.__new__(Shell))

    assert (home / ".pitash" / "config.py").read_text() == starter.read_text()
    assert sys._pitash_test_config_loaded is True
