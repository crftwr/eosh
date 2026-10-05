"""Completion recipe for zip."""

from __future__ import annotations

import shutil

from ..commands import arg, registry as command_registry
from ..completion import FileCompleter


def register() -> None:
    if shutil.which("zip") is None:
        return
    command_registry.command(
        "zip",
        help="package and compress files into a zip archive",
        params=[
            arg("archive", help="output zip archive to create", completer=FileCompleter()),
            arg("input", nargs="*", help="files or directories to add", completer=FileCompleter()),
            arg("-r", action="store_true", help="recurse into directories"),
            arg("-q", action="store_true", help="quiet operation"),
            arg("-v", action="store_true", help="verbose / show version info"),
            arg("-u", action="store_true", help="update — only changed or new files"),
            arg("-f", action="store_true", help="freshen — only changed files"),
            arg("-d", action="store_true", help="delete entries from zipfile"),
            arg("-m", action="store_true", help="move into zipfile (delete OS files after)"),
            arg("-j", action="store_true", help="junk paths (don't record directory names)"),
            arg("-0", action="store_true", help="store only (no compression)"),
            arg("-1", action="store_true", help="compress faster"),
            arg("-9", action="store_true", help="compress better"),
            arg("-c", action="store_true", help="add one-line comments"),
            arg("-z", action="store_true", help="add zipfile comment"),
            arg("-T", action="store_true", help="test zipfile integrity"),
            arg("-x", action="store_true", help="exclude the following names"),
            arg("-i", action="store_true", help="include only the following names"),
            arg("-D", action="store_true", help="do not add directory entries"),
            arg("-X", action="store_true", help="exclude extra file attributes"),
            arg("-y", action="store_true", help="store symlinks as the link, not the referenced file"),
            arg("-e", action="store_true", help="encrypt — prompt for password"),
            arg("-n", metavar="SUFFIX_LIST", help="don't compress files with these suffixes"),
            arg("-o", action="store_true", help="make zipfile as old as latest entry"),
            arg("-@", action="store_true", help="read names from stdin"),
            arg("-h", action="store_true", help="show help"),
            arg("-h2", action="store_true", help="show extended help"),
            arg("-L", action="store_true", help="show software license"),
            arg("-A", action="store_true", help="adjust self-extracting executable"),
            arg("-F", action="store_true", help="fix zipfile"),
            arg("-FF", action="store_true", help="try harder to fix zipfile"),
            arg("-J", action="store_true", help="junk zipfile prefix (unzipsfx)"),
            arg("-t", metavar="MMDDYYYY"),
            arg("-b", metavar="PATH"),
        ],
    )
