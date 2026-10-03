"""Where pitash keeps per-user state.

Every file the shell reads or writes under the user's home — ``config.py``,
``history``, ``history.dirs``, ``recipes/``, ``decorators/`` — lives in one
directory, and this module is the only place that names it.  It imports
nothing from the package so any module can use it without an import cycle.
"""

from __future__ import annotations

from pathlib import Path

CONFIG_DIR_NAME = ".pitash"


def config_dir() -> Path:
    """Return ``~/.pitash`` (resolved against ``HOME`` at call time)."""
    return Path.home() / CONFIG_DIR_NAME
