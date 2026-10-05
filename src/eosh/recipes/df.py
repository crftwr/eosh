"""Completion recipe for df."""

from __future__ import annotations

from ..commands import arg, registry as command_registry
from ..completion import FileCompleter


def register() -> None:
    command_registry.command(
        "df",
        help="report file system disk space usage",
        params=[
            arg("path", nargs="*", help="file or mount point", completer=FileCompleter()),
            arg("-a", action="store_true", help="include pseudo, duplicate, and inaccessible file systems"),
            arg("-B", metavar="SIZE", help="scale sizes by SIZE before printing (e.g. -BM for megabytes)"),
            arg("-h", action="store_true", help="print sizes in human readable format (e.g. 1K 234M 2G)"),
            arg("-H", action="store_true", help="like -h but use powers of 1000 not 1024"),
            arg("-i", action="store_true", help="list inode information instead of block usage"),
            arg("-k", action="store_true", help="use 1024-byte blocks (default on many systems)"),
            arg("-l", action="store_true", help="limit listing to local file systems"),
            arg("-m", action="store_true", help="use 1048576-byte (1 MiB) blocks"),
            arg("-P", action="store_true", help="use POSIX output format"),
            arg("-T", action="store_true", help="print file system type"),
            arg("-t", metavar="TYPE", help="limit listing to file systems of given type"),
            arg("-x", metavar="TYPE", help="exclude file systems of the given type"),
            arg("--total", action="store_true", help="show a final grand total"),
            arg("--output", metavar="FIELD[,FIELD...]",
                help="select output fields (source,fstype,size,used,avail,pcent,target)"),
            arg("--sync", action="store_true", help="invoke sync before getting usage info"),
            arg("--no-sync", action="store_true", help="do not invoke sync before getting usage info (default)"),
        ],
    )
