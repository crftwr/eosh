"""Completion recipe for rm — platform-aware (BSD vs GNU)."""

from __future__ import annotations

import shutil
import sys

from ..commands import arg, registry as command_registry
from ..completion import FileCompleter


def _macos_flags() -> list:
    return [
        arg("-d", action="store_true", help="remove directories as well as other types of files"),
        arg("-f", action="store_true", help="force — ignore nonexistent files, never prompt"),
        arg("-i", action="store_true", help="interactive — prompt before each removal"),
        arg("-I", action="store_true", help="prompt once if removing 3+ files or recursively (with -R)"),
        arg("-P", action="store_true", help="overwrite regular files before deletion"),
        arg("-R", action="store_true", help="recursively remove directories and contents"),
        arg("-r", action="store_true", help="recursively remove directories and contents (same as -R)"),
        arg("-v", action="store_true", help="verbose — show files as they are removed"),
        arg("-W", action="store_true", help="attempt to undelete files (HFS / FFS only)"),
        arg("-x", action="store_true", help="stay on this filesystem when descending"),
    ]


def _linux_flags() -> list:
    return [
        arg("-d", action="store_true", help="remove empty directories"),
        arg("-f", action="store_true", help="force — ignore nonexistent files, never prompt"),
        arg("-i", action="store_true", help="interactive — prompt before each removal"),
        arg("-I", action="store_true", help="prompt once if removing 3+ files or recursively"),
        arg("-r", action="store_true", help="recursively remove directories and contents"),
        arg("-R", action="store_true", help="recursively remove directories and contents (same as -r)"),
        arg("-v", action="store_true", help="verbose — show files as they are removed"),
        arg("--interactive", metavar="WHEN", help="prompt according to WHEN (never, once, always)"),
        arg("--no-preserve-root", action="store_true", help="do not treat / specially"),
        arg("--preserve-root", action="store_true", help="do not remove / (default)"),
        arg("--one-file-system", action="store_true", help="stay on the source filesystem (with -r)"),
        arg("--", action="store_true", help="end of options — treat following args as filenames"),
    ]


def register() -> None:
    if shutil.which("rm") is None:
        return
    flags = _macos_flags() if sys.platform == "darwin" else _linux_flags()
    command_registry.command(
        "rm",
        help="remove files or directories",
        params=[
            arg("path", nargs="*", help="file or directory to remove", completer=FileCompleter()),
            *flags,
        ],
    )
