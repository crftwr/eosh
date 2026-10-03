"""Completion recipe for ps."""

from __future__ import annotations

import shutil
import subprocess

from ..commands import arg, registry as command_registry
from ..completion import (
    Completer,
    Completion,
    CompletionContext,
)


class PidCompleter(Completer):
    """Completes running PIDs (with command name as description)."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            result = subprocess.run(
                ["ps", "-e", "-o", "pid=,comm="],
                capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        prefix = ctx.prefix
        out = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            pid, name = parts[0], parts[1].strip()
            if pid.startswith(prefix):
                out.append(Completion(value=pid, description=name))
        return out


def register() -> None:
    if shutil.which("ps") is None:
        return
    command_registry.command(
        "ps",
        help="report a snapshot of current processes",
        params=[
            arg("-A", action="store_true", help="show all processes"),
            arg("-a", action="store_true",
                help="show all processes with a controlling terminal except session leaders"),
            arg("-d", action="store_true", help="show all processes except session leaders"),
            arg("-e", action="store_true", help="show all processes (same as -A)"),
            arg("-f", action="store_true", help="full-format listing"),
            arg("-l", action="store_true", help="long format"),
            arg("-j", action="store_true", help="jobs format (sid, pgid)"),
            arg("-u", metavar="USER", help="select by effective user"),
            arg("-U", metavar="USER", help="select by real user"),
            arg("-p", metavar="PID", help="select by process ID", completer=PidCompleter()),
            arg("-G", metavar="GROUP", help="select by real group"),
            arg("-g", metavar="PGID|SID", help="select by session or by group (BSD vs GNU)"),
            arg("-t", metavar="TTY", help="select by tty"),
            arg("-x", action="store_true", help="include processes with no controlling terminal (BSD)"),
            arg("-w", action="store_true", help="wide output (use -ww for unlimited)"),
            arg("-o", metavar="FORMAT", help="user-defined output format"),
            arg("-c", action="store_true", help="show command name only (no args)"),
            arg("-H", action="store_true", help="show process hierarchy"),
            arg("-r", action="store_true", help="restrict to running processes"),
            arg("-T", action="store_true", help="show all processes on this terminal"),
            arg("-v", action="store_true", help="virtual memory format"),
            arg("-y", action="store_true", help="do not show flags; show RSS instead of ADDR (with -l)"),
        ],
    )
