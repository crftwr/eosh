"""Completion recipe for rsync.

The path completer handles plain local paths and accepts ``user@host:`` /
``host:`` prefixes — the path portion after ``:`` is left to the user
since enumerating remote paths requires an SSH round-trip per keystroke.
"""

from __future__ import annotations

import shutil

from ..commands import arg, registry as command_registry
from ..completion import (
    Completer,
    Completion,
    CompletionContext,
    FileCompleter,
)
from .ssh import SSHHostCompleter


class _RemoteOrFileCompleter(Completer):
    """Completes ``user@host:`` / ``host:`` prefixes from SSH config; otherwise files.

    Users typing ``host:`` after a ``:`` get no completion for the remote
    path (would require an SSH round-trip).
    """

    def __init__(self) -> None:
        self._files = FileCompleter()
        self._hosts = SSHHostCompleter()

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        prefix = ctx.prefix
        if ":" in prefix:
            # Already past the colon — leave the remote-path part to the user.
            return []
        if "@" in prefix and "/" not in prefix:
            # Looks like "user@<TAB>" — complete the host portion.
            user_part, partial = prefix.split("@", 1)
            host_ctx = CompletionContext(
                command=ctx.command,
                args=ctx.args,
                arg_index=ctx.arg_index,
                prefix=partial,
                line=ctx.line,
                shell_context=ctx.shell_context,
            )
            host_completions = self._hosts.complete(host_ctx)
            return [
                Completion(
                    value=f"{user_part}@{c.value}:",
                    display=f"{user_part}@{c.value}",
                    description=c.description,
                )
                for c in host_completions
            ]
        # Plain prefix: offer files first, then hosts (with trailing ':' to
        # signal a remote target).
        results = self._files.complete(ctx)
        if not prefix or not prefix.startswith((".", "/", "~")):
            for c in self._hosts.complete(ctx):
                results.append(Completion(
                    value=f"{c.value}:",
                    display=f"{c.value}:",
                    description=c.description,
                ))
        return results


def register() -> None:
    if shutil.which("rsync") is None:
        return
    command_registry.command(
        "rsync",
        help="fast, incremental file transfer (local or over SSH)",
        params=[
            arg("path", nargs="*", help="source or destination", completer=_RemoteOrFileCompleter()),
            arg("-a", action="store_true", help="archive — equivalent to -rlptgoD"),
            arg("-v", action="store_true", help="verbose"),
            arg("-q", action="store_true", help="quiet — suppress non-error messages"),
            arg("-r", action="store_true", help="recurse into directories"),
            arg("-l", action="store_true", help="copy symlinks as symlinks"),
            arg("-L", action="store_true", help="transform symlinks into the referenced file/dir"),
            arg("-p", action="store_true", help="preserve permissions"),
            arg("-t", action="store_true", help="preserve modification times"),
            arg("-g", action="store_true", help="preserve group"),
            arg("-o", action="store_true", help="preserve owner (super-user only)"),
            arg("-D", action="store_true", help="preserve device + special files"),
            arg("-H", action="store_true", help="preserve hard links"),
            arg("-A", action="store_true", help="preserve ACLs (implies -p)"),
            arg("-X", action="store_true", help="preserve extended attributes"),
            arg("-S", action="store_true", help="handle sparse files efficiently"),
            arg("-x", action="store_true", help="don't cross filesystem boundaries"),
            arg("-c", action="store_true", help="skip based on checksum, not mod-time + size"),
            arg("-n", action="store_true", help="dry run — show what would be done"),
            arg("-u", action="store_true", help="skip files that are newer on the receiver"),
            arg("-z", action="store_true", help="compress file data during transfer"),
            arg("-h", action="store_true", help="human-readable numbers"),
            arg("-i", action="store_true", help="show itemized changes for each transfer"),
            arg("-P", action="store_true", help="same as --partial --progress"),
            arg("-e", metavar="REMOTE-SHELL", help="specify the remote shell to use (e.g. 'ssh -p 2222')"),
            arg("-W", action="store_true", help="copy whole files (no incremental delta-transfer)"),
            arg("-R", action="store_true", help="use relative path names"),
            arg("-b", action="store_true", help="make backups (see --suffix and --backup-dir)"),
            arg("--delete", action="store_true", help="delete extraneous files from destination dirs"),
            arg("--delete-before", action="store_true", help="receiver deletes before transfer (default)"),
            arg("--delete-after", action="store_true", help="receiver deletes after transfer, not before"),
            arg("--delete-excluded", action="store_true", help="also delete excluded files from destination"),
            arg("--exclude", metavar="PATTERN", help="exclude files matching PATTERN"),
            arg("--exclude-from", metavar="FILE", help="read exclude patterns from FILE", completer=FileCompleter()),
            arg("--include", metavar="PATTERN", help="include files matching PATTERN"),
            arg("--include-from", metavar="FILE", help="read include patterns from FILE", completer=FileCompleter()),
            arg("--filter", metavar="RULE", help="add a filter rule"),
            arg("--files-from", metavar="FILE", help="read list of source-file names from FILE", completer=FileCompleter()),
            arg("--from0", action="store_true", help="all *-from / files-from file lists are NUL-terminated"),
            arg("--checksum", action="store_true", help="skip based on checksum, not mod-time + size"),
            arg("--archive", action="store_true", help="archive — equivalent to -a"),
            arg("--no-implied-dirs", action="store_true", help="do not create implied directories with --relative"),
            arg("--existing", action="store_true", help="skip creating new files on destination"),
            arg("--ignore-existing", action="store_true", help="skip updating files that already exist"),
            arg("--remove-source-files", action="store_true", help="delete files on source after successful transfer"),
            arg("--partial", action="store_true", help="keep partially transferred files"),
            arg("--partial-dir", metavar="DIR", help="put partial transfers in DIR"),
            arg("--progress", action="store_true", help="show progress during transfer"),
            arg("--stats", action="store_true", help="give some file-transfer stats"),
            arg("--bwlimit", metavar="KBPS", help="limit socket I/O bandwidth (KBps)"),
            arg("--max-size", metavar="SIZE", help="skip files larger than SIZE"),
            arg("--min-size", metavar="SIZE", help="skip files smaller than SIZE"),
            arg("--timeout", metavar="SECONDS", help="I/O timeout in seconds"),
            arg("--port", metavar="PORT", help="specify daemon port (default 873)"),
            arg("--password-file", metavar="FILE", help="read daemon password from FILE", completer=FileCompleter()),
            arg("--rsh", metavar="REMOTE-SHELL", help="remote shell command (same as -e)"),
            arg("--rsync-path", metavar="PROGRAM", help="rsync command to run on remote"),
            arg("--compress-level", metavar="LEVEL", help="explicitly set compression level"),
            arg("--dry-run", action="store_true", help="show what would be done without doing it"),
            arg("--itemize-changes", action="store_true", help="show itemized changes"),
            arg("--info", metavar="FLAGS", help="fine-grained informational verbosity"),
            arg("--debug", metavar="FLAGS", help="fine-grained debug verbosity"),
            arg("--help", action="store_true", help="show help"),
            arg("--version", action="store_true", help="show version"),
        ],
    )
