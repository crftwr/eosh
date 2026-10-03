"""Completion recipe for cp — platform-aware (BSD vs GNU)."""

from __future__ import annotations

import shutil
import sys

from ..commands import arg, registry as command_registry
from ..completion import FileCompleter


def _macos_flags() -> list:
    # BSD cp (macOS): subset of useful flags from `man cp` on Darwin.
    return [
        arg("-a", action="store_true", help="archive — preserve attrs, like -pPR"),
        arg("-c", action="store_true", help="use clonefile() if possible (copy-on-write)"),
        arg("-f", action="store_true", help="force overwrite without asking"),
        arg("-H", action="store_true", help="with -R, follow symlinks on the command line only"),
        arg("-i", action="store_true", help="interactive — prompt before overwriting"),
        arg("-L", action="store_true", help="with -R, follow all symlinks"),
        arg("-n", action="store_true", help="do not overwrite existing files"),
        arg("-P", action="store_true", help="do not follow symlinks (default with -R)"),
        arg("-p", action="store_true", help="preserve attributes — mode, ownership, timestamps"),
        arg("-R", action="store_true", help="recursive copy"),
        arg("-r", action="store_true", help="recursive copy (same as -R)"),
        arg("-S", action="store_true", help="do not attempt to make sparse holes"),
        arg("-s", action="store_true", help="create symbolic links instead of copying"),
        arg("-v", action="store_true", help="verbose — print files as they are copied"),
        arg("-X", action="store_true", help="do not copy extended attrs / resource forks"),
    ]


def _linux_flags() -> list:
    # GNU cp (Linux/coreutils).
    return [
        arg("-a", action="store_true", help="archive — equivalent to -dR --preserve=all"),
        arg("-b", action="store_true", help="make backup of each existing destination file"),
        arg("-d", action="store_true",
            help="preserve symlinks (same as --no-dereference --preserve=links)"),
        arg("-f", action="store_true", help="force overwrite without asking"),
        arg("-i", action="store_true", help="interactive — prompt before overwriting"),
        arg("-H", action="store_true", help="follow command-line symlinks in source"),
        arg("-l", action="store_true", help="hard link files instead of copying"),
        arg("-L", action="store_true", help="always follow symlinks in source"),
        arg("-n", action="store_true", help="do not overwrite existing files"),
        arg("-P", action="store_true", help="never follow symlinks in source"),
        arg("-p", action="store_true", help="preserve attributes (mode, ownership, timestamps)"),
        arg("-R", action="store_true", help="recursive copy"),
        arg("-r", action="store_true", help="recursive copy (same as -R)"),
        arg("-s", action="store_true", help="create symbolic links instead of copying"),
        arg("-T", action="store_true", help="treat dst as a normal file (no /dst/src semantics)"),
        arg("-t", metavar="DIR", help="copy all sources into the given directory",
            completer=FileCompleter()),
        arg("-u", action="store_true", help="copy only when source is newer or destination is missing"),
        arg("-v", action="store_true", help="verbose — print files as they are copied"),
        arg("-x", action="store_true", help="stay on this filesystem"),
        arg("-S", metavar="SUFFIX"),
        arg("--backup", metavar="CONTROL", help="make a backup (with optional CONTROL)"),
        arg("--preserve", metavar="ATTR_LIST", help="preserve the listed attributes"),
        arg("--no-preserve", metavar="ATTR_LIST", help="don't preserve the listed attributes"),
        arg("--reflink", metavar="WHEN", help="use copy-on-write when possible"),
        arg("--sparse", metavar="WHEN", help="control sparse-file handling"),
        arg("--strip-trailing-slashes", action="store_true", help="strip trailing / from each source"),
        arg("--update", action="store_true", help="copy only when source newer or destination missing"),
        arg("--target-directory", metavar="DIR", completer=FileCompleter()),
    ]


def register() -> None:
    if shutil.which("cp") is None:
        return
    flags = _macos_flags() if sys.platform == "darwin" else _linux_flags()
    command_registry.command(
        "cp",
        help="copy files and directories",
        params=[
            arg("path", nargs="*", help="source or destination path", completer=FileCompleter()),
            *flags,
        ],
    )
