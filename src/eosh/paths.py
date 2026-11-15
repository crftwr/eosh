"""Where eosh keeps per-user state.

Every file the shell reads or writes under the user's home — ``config.py``,
``history.db`` and the modules ``config.py`` imports — lives in one
directory, and this module is the only place that names it.  It imports
nothing from the package so any module can use it without an import cycle.

**Private by default.**  ``history.db`` holds every line typed (a pasted
``var AWS_SESSION_TOKEN=…``, a ``curl -H 'Authorization: …'``) and
``config.py`` is where people put account ids and tokens, so what eosh
creates here is the owner's alone — the directory ``0700``, files ``0600`` —
whatever the umask.  Only *creation* sets a mode: an existing file or
directory (a symlinked dotfiles checkout, a mode the user chose) is left as
it is.
"""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_DIR_NAME = ".eosh"

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def config_dir() -> Path:
    """Return ``~/.eosh`` (resolved against ``HOME`` at call time)."""
    return Path.home() / CONFIG_DIR_NAME


def ensure_private_dir(path: Path) -> Path:
    """Create *path* (and missing parents) if absent; the leaf is ``0700``.

    Parents get the default mode, as ``mkdir -p`` gives them; an existing
    directory keeps its own.
    """
    path.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    return path


def ensure_private_file(path: Path) -> bool:
    """Create *path* empty with mode ``0600`` if it doesn't exist.

    Returns True when this call created it.  An existing file is untouched,
    mode included.  The mode is given to ``open(2)`` itself, so the file is
    never readable by others even for an instant.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def write_private_text(path: Path, text: str) -> None:
    """Write *text* to *path*, creating it ``0600`` if it doesn't exist."""
    ensure_private_file(path)
    path.write_text(text)
