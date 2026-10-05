"""Completion recipe for ls — flag completion with multi-select TUI."""

from __future__ import annotations

from ..commands import arg, registry as command_registry
from ..completion import FileCompleter


def register() -> None:
    command_registry.command(
        "ls",
        help="list directory contents",
        params=[
            arg("path", nargs="*", help="file or directory", completer=FileCompleter()),
            arg("-a", action="store_true", help="include entries starting with ."),
            arg("-A", action="store_true", help="do not list . and .."),
            arg("-c", action="store_true", help="sort by ctime (time of last status change)"),
            arg("-d", action="store_true", help="list directories themselves, not their contents"),
            arg("-F", action="store_true", help="append indicator (*/=>@|) to entries"),
            arg("-G", action="store_true", help="colorize output (macOS) / suppress group column"),
            arg("-g", action="store_true", help="like -l but do not list owner"),
            arg("-h", action="store_true", help="print sizes like 1K, 234M, 2G"),
            arg("-i", action="store_true", help="print the index (inode) number of each file"),
            arg("-l", action="store_true", help="use a long listing format"),
            arg("-L", action="store_true", help="show info for symbolic link destinations"),
            arg("-m", action="store_true", help="fill width with a comma-separated list"),
            arg("-n", action="store_true", help="like -l but list numeric user and group IDs"),
            arg("-o", action="store_true", help="like -l but do not list group information"),
            arg("-p", action="store_true", help="append / indicator to directories"),
            arg("-q", action="store_true", help="print ? instead of non-graphic characters"),
            arg("-r", action="store_true", help="reverse order while sorting"),
            arg("-R", action="store_true", help="list subdirectories recursively"),
            arg("-s", action="store_true", help="print the allocated size of each file, in blocks"),
            arg("-S", action="store_true", help="sort by file size, largest first"),
            arg("-t", action="store_true", help="sort by modification time, newest first"),
            arg("-u", action="store_true", help="sort by last access time"),
            arg("-U", action="store_true", help="do not sort; list entries in directory order"),
            arg("-v", action="store_true", help="natural sort of version numbers within text"),
            arg("-w", action="store_true", help="set output width"),
            arg("-x", action="store_true", help="list entries by lines instead of by columns"),
            arg("-X", action="store_true", help="sort alphabetically by entry extension"),
            arg("-1", action="store_true", help="list one file per line"),
        ],
    )
