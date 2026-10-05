"""Completion recipe for ssh."""

from __future__ import annotations

import os
import re

from ..commands import arg, registry as command_registry
from ..completion import Completer, Completion, CompletionContext


class SSHHostCompleter(Completer):
    """Completes SSH hostnames from ~/.ssh/config and ~/.ssh/known_hosts."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        hosts: dict[str, str] = {}
        self._load_ssh_config(hosts)
        self._load_known_hosts(hosts)
        prefix = ctx.prefix
        return [
            Completion(value=h, description=desc)
            for h, desc in sorted(hosts.items())
            if h.startswith(prefix)
        ]

    def _load_ssh_config(self, hosts: dict[str, str]) -> None:
        config_path = os.path.expanduser("~/.ssh/config")
        try:
            with open(config_path) as f:
                content = f.read()
        except OSError:
            return
        for line in content.splitlines():
            m = re.match(r"^\s*Host\s+(.+)", line, re.IGNORECASE)
            if m:
                for h in m.group(1).split():
                    if "*" not in h and "?" not in h:
                        hosts[h] = "~/.ssh/config"

    def _load_known_hosts(self, hosts: dict[str, str]) -> None:
        known_hosts_path = os.path.expanduser("~/.ssh/known_hosts")
        try:
            with open(known_hosts_path) as f:
                lines = f.readlines()
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("|"):
                continue
            host_part = line.split()[0]
            for h in host_part.split(","):
                h = h.strip()
                # Strip [host]:port format
                m = re.match(r"^\[(.+)\]:\d+$", h)
                if m:
                    h = m.group(1)
                if h and "*" not in h and h not in hosts:
                    hosts[h] = "known_hosts"


def register() -> None:
    command_registry.command(
        "ssh",
        help="OpenSSH remote login client",
        params=[
            arg("host", help="hostname or user@host", completer=SSHHostCompleter()),
            arg("-4", action="store_true", help="force IPv4 addresses only"),
            arg("-6", action="store_true", help="force IPv6 addresses only"),
            arg("-A", action="store_true", help="enable forwarding of the authentication agent connection"),
            arg("-a", action="store_true", help="disable forwarding of the authentication agent connection"),
            arg("-C", action="store_true", help="compress all data"),
            arg("-f", action="store_true", help="go to background just before command execution"),
            arg("-G", action="store_true", help="print the configuration and exit"),
            arg("-i", metavar="FILE", help="path to identity file (private key)"),
            arg("-J", metavar="HOST", help="connect via a jump host (ProxyJump)"),
            arg("-L", metavar="[BIND:]PORT:HOST:HOSTPORT", help="forward local port to remote side"),
            arg("-l", metavar="USER", help="specify login name"),
            arg("-N", action="store_true", help="do not execute a remote command (for port forwarding)"),
            arg("-n", action="store_true", help="redirect stdin from /dev/null"),
            arg("-p", metavar="PORT", help="port to connect to on the remote host"),
            arg("-q", action="store_true", help="quiet mode — suppress warnings and diagnostic messages"),
            arg("-R", metavar="[BIND:]PORT:HOST:HOSTPORT", help="forward remote port to local side"),
            arg("-T", action="store_true", help="disable pseudo-terminal allocation"),
            arg("-t", action="store_true", help="force pseudo-terminal allocation"),
            arg("-v", action="store_true", help="verbose mode (use -vvv for more verbosity)"),
            arg("-W", metavar="HOST:PORT", help="forward stdio to host:port over secure channel"),
            arg("-X", action="store_true", help="enable X11 forwarding"),
            arg("-x", action="store_true", help="disable X11 forwarding"),
            arg("-Y", action="store_true", help="enable trusted X11 forwarding"),
        ],
    )
