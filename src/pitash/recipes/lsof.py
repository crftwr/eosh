"""Completion recipe for lsof."""

from __future__ import annotations

import shutil

from ..commands import arg, registry as command_registry
from ..completion import FileCompleter
from .ps import PidCompleter


def register() -> None:
    if shutil.which("lsof") is None:
        return
    command_registry.command(
        "lsof",
        help="list open files",
        params=[
            arg("path", nargs="*", help="file or directory", completer=FileCompleter()),
            arg("-a", action="store_true", help="AND the selection criteria (default is OR)"),
            arg("-b", action="store_true", help="avoid kernel functions that may block"),
            arg("-c", metavar="COMMAND", help="list files for command names matching regex/string"),
            arg("-d", metavar="FD-LIST", help="include / exclude files by file descriptor (e.g. 0,1,2)"),
            arg("-D", metavar="MODE", help="directory selection (a, b, i, l, r, R, u)"),
            arg("-e", action="store_true", help="tolerate errors on the named filesystem path"),
            arg("-F", action="store_true", help="produce output for parsing by other programs"),
            arg("-g", metavar="PGID", help="list files for processes whose group ID matches"),
            arg("-h", action="store_true", help="show help"),
            arg("-i", metavar="[i46][proto][@host][:port]",
                help="internet sockets (e.g. -i:80, -i tcp, -i @host)"),
            arg("-K", action="store_true", help="list tasks (threads) for owning processes"),
            arg("-l", action="store_true", help="show user IDs as numbers (don't look up names)"),
            arg("-n", action="store_true", help="no host name lookup"),
            arg("-N", action="store_true", help="list NFS files"),
            arg("-o", action="store_true", help="show file offset"),
            arg("-O", action="store_true", help="bypass overhead-warning prompt"),
            arg("-p", metavar="PID-LIST", help="list files for the given process IDs",
                completer=PidCompleter()),
            arg("-P", action="store_true", help="no port name lookup"),
            arg("-r", metavar="SECONDS", help="repeat every N seconds until SIGINT"),
            arg("-R", action="store_true", help="list parent process IDs"),
            arg("-s", action="store_true", help="show file size"),
            arg("-S", metavar="SECONDS", help="set timeout for kernel functions (default 15s)"),
            arg("-t", action="store_true", help="terse output (PIDs only)"),
            arg("-T", metavar="MODE", help="TCP/UDP socket info (q, st, qs)"),
            arg("-u", metavar="USER-LIST", help="list files for users (login or UID)"),
            arg("-U", action="store_true", help="list UNIX domain socket files"),
            arg("-v", action="store_true", help="show version info"),
            arg("-w", action="store_true", help="suppress warnings"),
            arg("+c", metavar="WIDTH", help="command-name column width (0 = unlimited)"),
            arg("+d", metavar="DIR", help="search files in directory (one level deep)"),
            arg("+D", metavar="DIR", help="recursively search files in directory"),
            arg("+f", action="store_true", help="format flags for fields"),
            arg("+L", action="store_true", help="list link counts"),
        ],
    )
