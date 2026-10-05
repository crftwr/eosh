"""Completion recipe for grep / egrep / fgrep / rgrep."""

from __future__ import annotations

from ..commands import arg, registry as command_registry
from ..completion import FileCompleter


def register() -> None:
    descriptions = {
        "grep":  "print lines matching a pattern",
        "egrep": "print lines matching an extended regex pattern",
        "fgrep": "print lines matching fixed strings",
        "rgrep": "recursively print lines matching a pattern",
    }
    for cmd in ("grep", "egrep", "fgrep", "rgrep"):
        command_registry.command(
            cmd,
            help=descriptions[cmd],
            params=[
                arg("pattern", help="regex pattern to search for"),
                arg("file", nargs="*", help="file to search", completer=FileCompleter()),
                arg("-a", action="store_true", help="process binary file as if it were text"),
                arg("-c", action="store_true", help="print only a count of matching lines"),
                arg("-E", action="store_true", help="interpret pattern as extended regular expression"),
                arg("-F", action="store_true", help="interpret pattern as fixed string, not regex"),
                arg("-G", action="store_true", help="interpret pattern as basic regular expression (default)"),
                arg("-h", action="store_true", help="suppress the prefixing of filenames on output"),
                arg("-H", action="store_true", help="print the filename with output lines"),
                arg("-i", action="store_true", help="perform case-insensitive matching"),
                arg("-I", action="store_true", help="process binary file as if it does not contain matches"),
                arg("-l", action="store_true", help="print only the names of files containing matches"),
                arg("-L", action="store_true", help="print only the names of files containing no matches"),
                arg("-m", metavar="N", help="stop reading after NUM matching lines"),
                arg("-n", action="store_true", help="prefix each output line with its line number"),
                arg("-o", action="store_true", help="print only the matched (non-empty) parts of a matching line"),
                arg("-P", action="store_true", help="interpret pattern as Perl-compatible regular expression"),
                arg("-q", action="store_true", help="do not write anything to stdout; exit 0 if match found"),
                arg("-r", action="store_true", help="read all files under each directory recursively"),
                arg("-R", action="store_true", help="like -r but follow all symbolic links"),
                arg("-s", action="store_true", help="suppress error messages about nonexistent or unreadable files"),
                arg("-v", action="store_true", help="invert the sense of matching"),
                arg("-w", action="store_true", help="select only lines containing matches that form whole words"),
                arg("-x", action="store_true", help="select only lines whose entire lines are matched"),
                arg("-z", action="store_true", help="treat input as a set of lines, each terminated by a zero byte"),
                arg("-A", metavar="N", help="print NUM lines of trailing context after each match"),
                arg("-B", metavar="N", help="print NUM lines of leading context before each match"),
                arg("-C", metavar="N", help="print NUM lines of output context"),
                arg("--color", action="store_true", help="mark up the matching text"),
                arg("--include", metavar="PATTERN", help="search only files whose name matches the given pattern"),
                arg("--exclude", metavar="PATTERN", help="skip files whose name matches the given pattern"),
                arg("--exclude-dir", metavar="PATTERN", help="skip directories whose name matches the given pattern"),
            ],
        )
