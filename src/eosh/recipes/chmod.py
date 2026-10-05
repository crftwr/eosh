"""Completion recipe for chmod."""

from __future__ import annotations

import shutil

from ..commands import arg, registry as command_registry
from ..completion import ChoiceCompleter, FileCompleter

COMMON_MODES: list[str] = [
    "644",
    "664",
    "666",
    "700",
    "750",
    "755",
    "775",
    "777",
    "u+x",
    "u+w",
    "u+r",
    "u-x",
    "u-w",
    "g+x",
    "g+w",
    "g-w",
    "o+r",
    "o-r",
    "o-w",
    "a+x",
    "a+r",
    "a-w",
    "+x",
    "+w",
    "+r",
    "-x",
    "-w",
]


def register() -> None:
    if shutil.which("chmod") is None:
        return
    command_registry.command(
        "chmod",
        help="change file mode bits",
        params=[
            arg("mode", help="mode bits (e.g. 755 or u+x)", completer=ChoiceCompleter(COMMON_MODES)),
            arg("file", nargs="*", help="file or directory", completer=FileCompleter()),
            arg("-f", action="store_true", help="do not display diagnostic messages on failure"),
            arg("-h", action="store_true", help="change mode of symlink itself, not target"),
            arg("-H", action="store_true", help="with -R, follow symlinks on the command line only"),
            arg("-L", action="store_true", help="with -R, follow all symlinks"),
            arg("-P", action="store_true", help="with -R, do not follow any symlinks (default)"),
            arg("-R", action="store_true", help="recurse into directories"),
            arg("-v", action="store_true", help="verbose — show files as their mode is changed"),
        ],
    )
