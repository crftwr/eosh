"""Completion recipe for find."""

from __future__ import annotations

from ..commands import arg, registry as command_registry
from ..completion import FileCompleter


def register() -> None:
    command_registry.command(
        "find",
        help="search for files in a directory hierarchy",
        params=[
            arg("path", nargs="*", help="starting directory", completer=FileCompleter()),
            # Output / actions
            arg("-delete", action="store_true", help="delete matched files (implies -depth)"),
            arg("-exec", metavar="CMD {} \\;", help="execute command for each matched file"),
            arg("-execdir", metavar="CMD {} \\;", help="like -exec but run command in file's directory"),
            arg("-ls", action="store_true", help="list matched files as if by ls -dils"),
            arg("-print", action="store_true", help="print the full path of matched files (default)"),
            arg("-print0", action="store_true", help="print path followed by a null byte (for xargs -0)"),
            # Filters — name / path
            arg("-iname", metavar="PATTERN", help="like -name but case-insensitive"),
            arg("-ipath", metavar="PATTERN", help="like -path but case-insensitive"),
            arg("-iregex", metavar="PATTERN", help="like -regex but case-insensitive"),
            arg("-name", metavar="PATTERN", help="base of filename matches shell pattern"),
            arg("-path", metavar="PATTERN", help="file path matches shell pattern"),
            arg("-regex", metavar="PATTERN", help="file path matches regular expression"),
            # Filters — type / permissions
            arg("-empty", action="store_true", help="file is empty and is a regular file or directory"),
            arg("-executable", action="store_true", help="file is executable by the current user"),
            arg("-group", metavar="NAME", help="file belongs to the named group"),
            arg("-perm", metavar="MODE", help="file permission bits match mode"),
            arg("-readable", action="store_true", help="file is readable by the current user"),
            arg("-type", metavar="f|d|l|b|c|p|s",
                help="file type: f=file d=directory l=symlink b=block c=char p=pipe s=socket"),
            arg("-user", metavar="NAME", help="file is owned by the named user"),
            arg("-writable", action="store_true", help="file is writable by the current user"),
            # Filters — time
            arg("-atime", metavar="N", help="file was last accessed n*24 hours ago"),
            arg("-ctime", metavar="N", help="file status was last changed n*24 hours ago"),
            arg("-mtime", metavar="N", help="file was last modified n*24 hours ago"),
            arg("-newer", metavar="FILE", help="file was modified more recently than the given file"),
            arg("-newermt", metavar="TIMESTAMP", help="file was modified more recently than given timestamp"),
            # Filters — size
            arg("-size", metavar="N[ckMG]", help="file uses n units of space (k=KB M=MB G=GB)"),
            # Traversal
            arg("-depth", action="store_true", help="process directory contents before the directory itself"),
            arg("-follow", action="store_true", help="dereference symbolic links (deprecated, prefer -L)"),
            arg("-maxdepth", metavar="N", help="descend at most n levels of directories"),
            arg("-mindepth", metavar="N", help="do not apply tests at levels less than n"),
            arg("-mount", action="store_true", help="do not descend directories on other filesystems"),
            arg("-xdev", action="store_true", help="do not descend directories on other filesystems (alias)"),
            # Logical operators
            arg("-not", action="store_true", help="negate the following expression"),
            arg("-or", action="store_true", help="logical OR of two expressions"),
            arg("-and", action="store_true", help="logical AND of two expressions (default between expressions)"),
            arg("-prune", action="store_true", help="do not descend into matched directory"),
        ],
    )
