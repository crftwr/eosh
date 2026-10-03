"""Completion recipe for du — platform-aware (macOS vs Linux/GNU)."""

from __future__ import annotations

import sys

from ..commands import arg, registry as command_registry
from ..completion import FileCompleter


def _macos_flags() -> list:
    # macOS du  (BSD du — from `man du` on Darwin)
    # usage: du [-Aclnx] [-H | -L | -P] [-g | -h | -k | -m]
    #           [-a | -s | -d depth] [-B blocksize] [-I mask] [-t threshold] [file ...]
    return [
        arg("-A", action="store_true", help="display apparent sizes rather than disk usage"),
        arg("-a", action="store_true", help="write counts for all files, not just directories"),
        arg("-c", action="store_true", help="produce a grand total"),
        arg("-d", metavar="N",
            help="print the total for a directory only if it is N or fewer levels deep"),
        arg("-g", action="store_true", help="use 1-GiB (1073741824-byte) blocks"),
        arg("-h", action="store_true", help="print sizes in human-readable format (e.g. 1K 234M 2G)"),
        arg("-H", action="store_true", help="follow symbolic links on the command line only"),
        arg("-I", metavar="mask", help="ignore files and directories matching mask"),
        arg("-k", action="store_true", help="use 1024-byte blocks (default)"),
        arg("-l", action="store_true", help="count sizes many times if hard linked"),
        arg("-L", action="store_true", help="follow all symbolic links"),
        arg("-m", action="store_true", help="use 1-MiB (1048576-byte) blocks"),
        arg("-n", action="store_true", help="ignore files and directories with the 'nodump' flag set"),
        arg("-P", action="store_true", help="do not follow symbolic links (default)"),
        arg("-s", action="store_true", help="display only a total for each argument"),
        arg("-t", metavar="threshold",
            help="exclude entries smaller than threshold (negative → exclude larger)"),
        arg("-x", action="store_true", help="skip directories on different file systems"),
        arg("-B", metavar="blocksize"),
    ]


def _linux_flags() -> list:
    return [
        arg("-0", action="store_true", help="end each output line with NUL instead of newline"),
        arg("-a", action="store_true", help="write counts for all files, not just directories"),
        arg("-b", action="store_true", help="apparent bytes (equivalent to --apparent-size --block-size=1)"),
        arg("-c", action="store_true", help="produce a grand total"),
        arg("-d", metavar="N",
            help="print the total for a directory only if it is N or fewer levels deep"),
        arg("-D", action="store_true", help="dereference only symlinks listed on the command line"),
        arg("-h", action="store_true", help="print sizes in human-readable format (e.g. 1K 234M 2G)"),
        arg("-H", action="store_true", help="same as --si (use powers of 1000)"),
        arg("-k", action="store_true", help="use 1024-byte blocks"),
        arg("-l", action="store_true", help="count sizes many times if hard linked"),
        arg("-L", action="store_true", help="dereference all symbolic links"),
        arg("-m", action="store_true", help="use 1-MiB (1048576-byte) blocks"),
        arg("-P", action="store_true", help="do not follow symbolic links (default)"),
        arg("-s", action="store_true", help="display only a total for each argument"),
        arg("-S", action="store_true", help="do not include size of subdirectories in parent total"),
        arg("-t", metavar="SIZE",
            help="exclude entries smaller than SIZE (negative → exclude larger)"),
        arg("-x", action="store_true", help="skip directories on different file systems"),
        arg("-B", metavar="SIZE"),
        arg("--apparent-size", action="store_true", help="print apparent sizes rather than disk usage"),
        arg("--exclude", metavar="PATTERN", help="exclude files that match PATTERN"),
        arg("--max-depth", metavar="N",
            help="print total only if N or fewer levels below the command-line argument"),
        arg("--si", action="store_true", help="use powers of 1000 (kB, MB, GB …)"),
        arg("--time", action="store_true", help="show the modification time of any file in the directory"),
        arg("--block-size", metavar="SIZE"),
        arg("--threshold", metavar="SIZE"),
        arg("--time-style", metavar="STYLE"),
    ]


def register() -> None:
    flags = _macos_flags() if sys.platform == "darwin" else _linux_flags()
    command_registry.command(
        "du",
        help="estimate file space usage",
        params=[
            arg("path", nargs="*", help="file or directory", completer=FileCompleter()),
            *flags,
        ],
    )
