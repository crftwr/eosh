"""Completion recipe for kill and pkill."""

from __future__ import annotations

import subprocess

from ..commands import arg, registry as command_registry
from ..completion import Completer, Completion, CompletionContext


class ProcessCompleter(Completer):
    """Completes running process PIDs and names."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            result = subprocess.run(
                ["ps", "-e", "-o", "pid=,comm="],
                capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []

        prefix = ctx.prefix
        completions = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            pid, name = parts[0], parts[1].strip()
            if pid.startswith(prefix):
                completions.append(Completion(value=pid, description=name))
        return completions


class ProcessNameCompleter(Completer):
    """Completes running process names (for pkill)."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            result = subprocess.run(
                ["ps", "-e", "-o", "comm="],
                capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []

        prefix = ctx.prefix
        seen: set[str] = set()
        completions = []
        for line in result.stdout.splitlines():
            name = line.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            if name.startswith(prefix):
                completions.append(Completion(value=name))
        return sorted(completions, key=lambda c: c.value)


def register() -> None:
    command_registry.command(
        "kill",
        help="send a signal to a process",
        params=[
            arg("pid", nargs="*", help="process ID", completer=ProcessCompleter()),
            arg("-1", action="store_true", help="SIGHUP — reload config / hangup"),
            arg("-2", action="store_true", help="SIGINT — interrupt (same as Ctrl+C)"),
            arg("-3", action="store_true", help="SIGQUIT — quit and produce core dump"),
            arg("-9", action="store_true", help="SIGKILL — force kill (cannot be caught or ignored)"),
            arg("-15", action="store_true", help="SIGTERM — graceful termination (default)"),
            arg("-SIGHUP", action="store_true", help="reload config / hangup"),
            arg("-SIGINT", action="store_true", help="interrupt (same as Ctrl+C)"),
            arg("-SIGQUIT", action="store_true", help="quit and produce core dump"),
            arg("-SIGKILL", action="store_true", help="force kill (cannot be caught or ignored)"),
            arg("-SIGTERM", action="store_true", help="graceful termination (default)"),
            arg("-SIGUSR1", action="store_true", help="user-defined signal 1"),
            arg("-SIGUSR2", action="store_true", help="user-defined signal 2"),
            arg("-SIGSTOP", action="store_true", help="pause process (cannot be caught or ignored)"),
            arg("-SIGCONT", action="store_true", help="resume paused process"),
            arg("-l", action="store_true", help="list all available signal names"),
        ],
    )
    command_registry.command(
        "pkill",
        help="send a signal to processes by name",
        params=[
            arg("name", nargs="*", help="process name pattern", completer=ProcessNameCompleter()),
        ],
    )
