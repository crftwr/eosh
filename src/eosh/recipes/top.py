"""Completion recipe for top — platform-aware (macOS vs Linux/procps)."""

from __future__ import annotations

import shutil
import sys

from ..commands import arg, registry as command_registry
from ..completion import ChoiceCompleter
from .ps import PidCompleter


_MACOS_SORT_KEYS = [
    "pid", "command", "cpu", "cpu_me", "cpu_others", "csw", "time",
    "threads", "ports", "mregion", "mem", "rprvt", "purg", "vsize",
    "vprvt", "kprvt", "kshrd", "pgrp", "ppid", "state", "uid", "wq",
    "faults", "cow", "user", "msgsent", "msgrecv", "sysbsd", "sysmach",
    "pageins", "boosts", "instrs", "cycles",
]


def _macos_params() -> list:
    return [
        arg("-a", action="store_true", help="set event counting mode to absolute"),
        arg("-c", metavar="MODE", help="set event counting mode (a, d, e, n)",
            completer=ChoiceCompleter(["a", "d", "e", "n"])),
        arg("-d", action="store_true", help="set event counting mode to delta"),
        arg("-e", action="store_true", help="set event counting mode to events"),
        arg("-F", action="store_true", help="calculate statistics for shared libraries (Frameworks)"),
        arg("-f", action="store_true", help="do not calculate framework statistics"),
        arg("-h", action="store_true", help="print usage and exit"),
        arg("-i", metavar="INTERVAL", help="interval between framework updates (default 10)"),
        arg("-l", metavar="SAMPLES", help="number of samples (0 = infinite, default 0 in screen mode)"),
        arg("-ncols", metavar="COLUMNS", help="number of columns to display"),
        arg("-o", metavar="KEY", help="primary sort key",
            completer=ChoiceCompleter(_MACOS_SORT_KEYS)),
        arg("-O", metavar="SKEY", help="secondary sort key",
            completer=ChoiceCompleter(_MACOS_SORT_KEYS)),
        arg("-R", action="store_true", help="do not traverse and report memory object map (default)"),
        arg("-r", action="store_true", help="traverse and report memory object map"),
        arg("-S", action="store_true", help="display swap and purgeable values in legend"),
        arg("-s", metavar="SECONDS", help="delay between samples in seconds"),
        arg("-n", metavar="NPROCS", help="maximum number of processes to display"),
        arg("-stats", metavar="KEYS", help="comma-separated list of stats to display"),
        arg("-pid", metavar="PID", help="show only the given process ID", completer=PidCompleter()),
        arg("-user", metavar="USER", help="show only processes owned by the given user"),
        arg("-U", metavar="USER", help="show only processes owned by the given user (alias)"),
        arg("-u", action="store_true", help="sort by CPU and show only running processes"),
    ]


def _linux_params() -> list:
    return [
        arg("-b", action="store_true", help="batch mode — all output to stdout, no curses"),
        arg("-c", action="store_true", help="toggle command-line / program-name display"),
        arg("-d", metavar="SECS.TENTHS", help="delay between updates in seconds.tenths"),
        arg("-E", metavar="SCALE", help="force summary memory scale (k, m, g, t, p, e)"),
        arg("-e", metavar="SCALE", help="force task memory scale (k, m, g, t, p)"),
        arg("-H", action="store_true", help="show individual threads"),
        arg("-h", action="store_true", help="show help and exit"),
        arg("-i", action="store_true", help="toggle idle-process filter"),
        arg("-n", metavar="ITERATIONS", help="maximum number of iterations"),
        arg("-O", action="store_true", help="list available output fields and exit"),
        arg("-o", metavar="FIELD", help="sort by the given field"),
        arg("-p", metavar="PID[,PID]", help="monitor only the given PIDs (comma-separated)",
            completer=PidCompleter()),
        arg("-S", action="store_true", help="toggle cumulative-time mode"),
        arg("-s", action="store_true", help="secure mode"),
        arg("-U", metavar="USER", help="monitor processes owned by the given user (effective UID)"),
        arg("-u", metavar="USER", help="monitor processes owned by the given user (real UID)"),
        arg("-V", action="store_true", help="show version and exit"),
        arg("-w", metavar="COLUMNS", help="wide output (optional column count)"),
    ]


def register() -> None:
    if shutil.which("top") is None:
        return
    params = _macos_params() if sys.platform == "darwin" else _linux_params()
    command_registry.command(
        "top",
        help="display tasks and system resource usage",
        params=params,
    )
