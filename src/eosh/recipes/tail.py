"""Completion recipe for tail."""

from __future__ import annotations

from ..commands import arg, registry as command_registry
from ..completion import FileCompleter


def register() -> None:
    command_registry.command(
        "tail",
        help="output the last part of files",
        params=[
            arg("file", nargs="*", help="file to tail", completer=FileCompleter()),
            arg("-c", metavar="N[bkm]", help="output the last N bytes"),
            arg("-f", action="store_true", help="follow the file as it grows"),
            arg("-F", action="store_true", help="like -f but retry if file becomes inaccessible"),
            arg("-n", metavar="N", help="output the last N lines (default: 10)"),
            arg("-q", action="store_true", help="suppress filename headers when multiple files given"),
            arg("-r", action="store_true", help="print lines in reverse order (macOS/BSD)"),
            arg("-v", action="store_true", help="always print filename headers"),
        ],
    )
