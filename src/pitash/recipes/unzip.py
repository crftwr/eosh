"""Completion recipe for unzip."""

from __future__ import annotations

import os
import shutil

from ..commands import arg, registry as command_registry
from ..completion import (
    Completer,
    Completion,
    CompletionContext,
    DirCompleter,
    FileCompleter,
    _to_slash,
)


class ZipArchiveCompleter(Completer):
    """Completes filesystem paths but lists *.zip files first."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        prefix = ctx.prefix
        if prefix:
            expanded = os.path.expanduser(prefix)
            directory = os.path.dirname(expanded) or "."
            partial = os.path.basename(expanded)
        else:
            directory = "."
            partial = ""
        try:
            entries = os.listdir(directory)
        except OSError:
            return []
        dirs: list[Completion] = []
        zips: list[Completion] = []
        others: list[Completion] = []
        for entry in sorted(entries):
            if entry.startswith(".") and not partial.startswith("."):
                continue
            if not entry.lower().startswith(partial.lower()):
                continue
            full = os.path.join(directory, entry)
            display_path = (
                os.path.join(os.path.dirname(prefix), entry)
                if prefix and os.path.dirname(prefix)
                else entry
            )
            display_path = _to_slash(display_path)
            if os.path.isdir(full):
                dirs.append(Completion(value=display_path + "/", display=entry + "/"))
            elif entry.lower().endswith(".zip"):
                zips.append(Completion(value=display_path, display=entry))
            else:
                others.append(Completion(value=display_path, display=entry))
        return dirs + zips + others


def register() -> None:
    if shutil.which("unzip") is None:
        return
    command_registry.command(
        "unzip",
        help="list, test, or extract files from a zip archive",
        params=[
            arg("archive", help="zip archive", completer=ZipArchiveCompleter()),
            arg("file", nargs="*", help="member file to extract", completer=FileCompleter()),
            arg("-Z", action="store_true", help="ZipInfo mode — archive listing"),
            arg("-p", action="store_true", help="extract files to pipe (no messages)"),
            arg("-l", action="store_true", help="list files (short format)"),
            arg("-f", action="store_true", help="freshen existing files, create none"),
            arg("-t", action="store_true", help="test compressed archive data"),
            arg("-u", action="store_true", help="update files, create if necessary"),
            arg("-z", action="store_true", help="display archive comment only"),
            arg("-v", action="store_true", help="list verbosely / show version info"),
            arg("-T", action="store_true", help="set timestamp on archive to latest"),
            arg("-x", action="store_true", help="exclude files that follow"),
            arg("-d", metavar="DIR", help="extract files into the given directory",
                completer=DirCompleter()),
            arg("-n", action="store_true", help="never overwrite existing files"),
            arg("-q", action="store_true", help="quiet mode (-qq for quieter)"),
            arg("-o", action="store_true", help="overwrite files WITHOUT prompting"),
            arg("-a", action="store_true", help="auto-convert text files"),
            arg("-aa", action="store_true", help="treat ALL files as text"),
            arg("-j", action="store_true", help="junk paths (do not make directories)"),
            arg("-C", action="store_true", help="match filenames case-insensitively"),
            arg("-L", action="store_true", help="make (some) names lowercase"),
            arg("-X", action="store_true", help="restore UID/GID info"),
            arg("-V", action="store_true", help="retain VMS version numbers"),
            arg("-K", action="store_true", help="keep setuid/setgid/sticky permissions"),
            arg("-M", action="store_true", help="pipe output through 'more' pager"),
            arg("-P", metavar="PASSWORD", help="use PASSWORD to decrypt entries"),
            arg("-h", action="store_true", help="show help"),
            arg("-hh", action="store_true", help="show extended help"),
        ],
    )
