"""Completion recipe for mv — platform-aware (BSD vs GNU)."""

from __future__ import annotations

import shutil
import sys

from ..commands import arg, registry as command_registry
from ..completion import FileCompleter


def _macos_flags() -> list:
    return [
        arg("-f", action="store_true", help="force overwrite without asking"),
        arg("-i", action="store_true", help="interactive — prompt before overwriting"),
        arg("-n", action="store_true", help="do not overwrite existing files"),
        arg("-h", action="store_true", help="if the target is a symlink to a directory, don't follow it"),
        arg("-v", action="store_true", help="verbose — print files as they are moved"),
    ]


def _linux_flags() -> list:
    return [
        arg("-b", action="store_true", help="make a backup of each existing destination file"),
        arg("-f", action="store_true", help="force overwrite without asking"),
        arg("-i", action="store_true", help="interactive — prompt before overwriting"),
        arg("-n", action="store_true", help="do not overwrite existing files"),
        arg("-T", action="store_true", help="treat dst as a normal file (no /dst/src semantics)"),
        arg("-t", metavar="DIR", help="move all sources into the given directory",
            completer=FileCompleter()),
        arg("-u", action="store_true", help="move only when source is newer or destination is missing"),
        arg("-v", action="store_true", help="verbose — print files as they are moved"),
        arg("-S", metavar="SUFFIX"),
        arg("--backup", metavar="CONTROL", help="make a backup (with optional CONTROL)"),
        arg("--strip-trailing-slashes", action="store_true", help="strip trailing / from each source"),
        arg("--update", action="store_true", help="move only when source newer or destination missing"),
        arg("--target-directory", metavar="DIR", completer=FileCompleter()),
    ]


def register() -> None:
    if shutil.which("mv") is None:
        return
    flags = _macos_flags() if sys.platform == "darwin" else _linux_flags()
    command_registry.command(
        "mv",
        help="move (rename) files",
        params=[
            arg("path", nargs="*", help="source or destination path", completer=FileCompleter()),
            *flags,
        ],
    )
