"""Completion recipe for scp.

Source/destination completion handles ``user@host:path`` / ``host:path``
prefixes via the SSH host completer; the path portion after ``:`` is left
to the user (would require a remote round-trip per keystroke).
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
    """Completes filesystem paths and ``[user@]host:`` prefixes for scp.

    A trailing ``:`` is appended so the user can chain a remote path after
    accepting a host completion.
    """

    def __init__(self) -> None:
        self._files = FileCompleter()
        self._hosts = SSHHostCompleter()

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        prefix = ctx.prefix
        if ":" in prefix:
            return []
        if "@" in prefix and "/" not in prefix:
            user_part, partial = prefix.split("@", 1)
            host_ctx = CompletionContext(
                command=ctx.command,
                args=ctx.args,
                arg_index=ctx.arg_index,
                prefix=partial,
                line=ctx.line,
                shell_context=ctx.shell_context,
            )
            return [
                Completion(
                    value=f"{user_part}@{c.value}:",
                    display=f"{user_part}@{c.value}",
                    description=c.description,
                )
                for c in self._hosts.complete(host_ctx)
            ]
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
    if shutil.which("scp") is None:
        return
    command_registry.command(
        "scp",
        help="secure copy files between hosts over SSH",
        params=[
            arg("path", nargs="*", help="source or destination", completer=_RemoteOrFileCompleter()),
            arg("-3", action="store_true", help="transfer between two remote hosts via the local machine"),
            arg("-4", action="store_true", help="force IPv4"),
            arg("-6", action="store_true", help="force IPv6"),
            arg("-A", action="store_true", help="forward authentication agent connection"),
            arg("-B", action="store_true", help="batch mode — disables passphrase/password prompts"),
            arg("-C", action="store_true", help="compress data"),
            arg("-c", metavar="CIPHER", help="select cipher"),
            arg("-D", metavar="PORT", help="use sftp protocol with specific port"),
            arg("-F", metavar="CONFIG", help="alternative ssh_config file", completer=FileCompleter()),
            arg("-i", metavar="KEY-FILE", help="identity (private key) file", completer=FileCompleter()),
            arg("-J", metavar="[USER@]HOST[:PORT]", help="ProxyJump host"),
            arg("-l", metavar="KBPS", help="limit bandwidth (Kbit/s)"),
            arg("-O", action="store_true", help="use the legacy SCP protocol (vs SFTP)"),
            arg("-o", metavar="OPTION", help="ssh option (KEY=VALUE)"),
            arg("-P", metavar="PORT", help="remote port to connect to"),
            arg("-p", action="store_true", help="preserve modification times, access times, modes"),
            arg("-q", action="store_true", help="quiet mode — disables progress meter"),
            arg("-R", action="store_true", help="transfer between two remote hosts directly (RFC 8341)"),
            arg("-r", action="store_true", help="recursively copy directories"),
            arg("-S", metavar="PROGRAM", help="program to use for the encrypted connection"),
            arg("-T", action="store_true", help="disable strict filename checking"),
            arg("-v", action="store_true", help="verbose"),
        ],
    )
