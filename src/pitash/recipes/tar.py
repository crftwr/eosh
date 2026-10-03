"""Completion recipe for tar."""

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


# Flags that consume the next token — used by _TarPositionalCompleter to
# correctly skip flag values when counting positionals.  Kept in sync with
# the value-taking arg() declarations in register().
_VALUE_TAKING_FLAGS = {
    "-f", "-C", "-b", "-s",
    "--exclude", "--include", "--strip-components",
    "--format", "--newer", "--newer-mtime",
}


class TarArchiveCompleter(Completer):
    """Completes filesystem paths but lists tar-like archives first.

    Recognizes .tar, .tar.gz, .tgz, .tar.bz2, .tbz, .tbz2, .tar.xz, .txz,
    .tar.zst, .tzst, .tar.lzma, and bare .gz/.bz2/.xz/.zst when used with -f.
    """

    ARCHIVE_SUFFIXES = (
        ".tar",
        ".tar.gz", ".tgz",
        ".tar.bz2", ".tbz", ".tbz2",
        ".tar.xz", ".txz",
        ".tar.zst", ".tzst",
        ".tar.lzma", ".tar.lz",
    )

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
        archives: list[Completion] = []
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
            elif entry.lower().endswith(self.ARCHIVE_SUFFIXES):
                archives.append(Completion(value=display_path, display=entry))
            else:
                others.append(Completion(value=display_path, display=entry))
        return dirs + archives + others


class _TarPositionalCompleter(Completer):
    """Smart positional completer for ``tar``.

    The first positional is the archive (use the archive-aware completer);
    every subsequent positional is a member file (plain file completion).

    When the archive was supplied via standalone ``-f ARCHIVE`` instead of
    appearing as a positional, every positional is a member.  Note that
    short-flag clusters like ``-cvzf`` don't consume the next token as the
    archive — they're treated as a single boolean flag, so the archive
    still lands at positional 0.
    """

    def __init__(self) -> None:
        self._archive = TarArchiveCompleter()
        self._files = FileCompleter()

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if self._is_archive_slot(ctx.args):
            return self._archive.complete(ctx)
        return self._files.complete(ctx)

    def describe_slot(self, args: list[str], pos_idx: int) -> str | None:
        if self._is_archive_slot(args):
            return "archive: tar archive"
        return "file: file to add or extract"

    @staticmethod
    def _is_archive_slot(args: list[str]) -> bool:
        if "-f" in args:
            # Standalone -f consumed the next token as the archive — all
            # subsequent positionals are members.
            return False
        # Count completed positionals, skipping flags and their values.
        rank = 0
        i = 0
        while i < len(args):
            tok = args[i]
            if tok.startswith("-"):
                i += 2 if tok in _VALUE_TAKING_FLAGS else 1
            else:
                rank += 1
                i += 1
        return rank == 0


def register() -> None:
    if shutil.which("tar") is None:
        return
    command_registry.command(
        "tar",
        help="create, extract, or list tar archives",
        params=[
            arg("path", nargs="*", help="archive or member file",
                completer=_TarPositionalCompleter()),
            arg("-c", action="store_true", help="create archive"),
            arg("-x", action="store_true", help="extract from archive"),
            arg("-t", action="store_true", help="list archive contents"),
            arg("-r", action="store_true", help="append files to existing archive"),
            arg("-u", action="store_true", help="update — only newer files"),
            arg("-f", metavar="ARCHIVE", help="archive file path",
                completer=TarArchiveCompleter()),
            arg("-v", action="store_true", help="verbose"),
            arg("-z", action="store_true", help="compress with gzip"),
            arg("-j", action="store_true", help="compress with bzip2"),
            arg("-J", action="store_true", help="compress with xz"),
            arg("--lzma", action="store_true", help="compress with lzma"),
            arg("-Z", action="store_true", help="compress with compress(1)"),
            arg("-C", metavar="DIR", help="change to directory before processing",
                completer=DirCompleter()),
            arg("-p", action="store_true", help="preserve permissions on extract"),
            arg("-k", action="store_true", help="keep existing files (don't overwrite)"),
            arg("-m", action="store_true", help="don't restore modification times"),
            arg("-O", action="store_true", help="write to stdout, don't restore to disk"),
            arg("-w", action="store_true", help="interactive — confirm each file"),
            arg("-h", action="store_true", help="follow symlinks"),
            arg("-P", action="store_true", help="preserve absolute paths (don't strip leading /)"),
            arg("-s", metavar="PATTERN", help="modify file/archive names with substitution"),
            arg("-b", metavar="BLOCKING"),
            arg("--exclude", metavar="PATTERN", help="skip files matching pattern"),
            arg("--include", metavar="PATTERN", help="include only files matching pattern"),
            arg("--strip-components", metavar="N", help="strip leading components from paths"),
            arg("--no-recursion", action="store_true", help="don't recurse into directories"),
            arg("--format", metavar="FORMAT", help="select archive format"),
            arg("--totals", action="store_true", help="print total bytes after archive"),
            arg("--newer", metavar="DATE", help="only files newer than DATE"),
            arg("--newer-mtime", metavar="DATE", help="only files with mtime newer than DATE"),
        ],
    )
