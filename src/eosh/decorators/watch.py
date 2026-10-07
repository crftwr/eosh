"""@watch — re-run a pipeline on a timer, showing only its latest output.

Like POSIX watch(1): an alternate screen with a one-line header, the last
run's output below it (cut to the screen — there is no scrolling), and
``q`` or Ctrl+C to quit.  Off a terminal, or with ``--no-clear``, it just
streams each run's output.
"""

from __future__ import annotations

import datetime
import os
import re
import sys
import tempfile
import time

from dataclasses import replace

from .. import terminal
from ..commands import arg
from ..commands import registry as command_registry
from ..pipeline import Pipeline, Redirect

_ENTER = "\x1b[?1049h\x1b[?25l"     # alternate screen, hide cursor
_LEAVE = "\x1b[?25h\x1b[?1049l"
_QUIT_KEYS = (b"q", b"Q", b"\x03")

# Escape sequences (CSI, OSC, DCS, two-byte ESC) and stray control bytes.
# A tool can colour or move the cursor even with stdout in a file, and
# either would break cutting lines to the screen width.
_ANSI_RE = re.compile(
    r"\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1bP[^\x1b]*\x1b\\"
    r"|\x1b[\x20-\x2f]*[\x30-\x7e]"
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]"
)


def _captured(pipeline: Pipeline, path: str) -> Pipeline:
    """*pipeline* with its last stage's stdout and stderr sent to *path*."""
    stages = list(pipeline.stages)
    last = stages[-1]
    stages[-1] = replace(last, redirects=list(last.redirects) + [
        Redirect(kind=">", target=path), Redirect(kind="2>&1", target="1"),
    ])
    return Pipeline(stages=stages)


def _split_to_lines(text: str) -> list[str]:
    """Displayable rows: line endings normalised, escapes stripped, tabs expanded."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return [line.expandtabs(8) for line in _ANSI_RE.sub("", text).splitlines()]


def _frame(header: str, lines: list[str], cols: int, rows: int) -> str:
    """One screenful: header, a blank row, then as much output as fits.

    Every row is cut one cell short of the width and the last one has no
    newline, so nothing wraps or scrolls the screen.
    """
    width = max(0, cols - 1)
    body = [header[:width], ""] + [line[:width] for line in lines]
    return "\x1b[H\x1b[J" + "\r\n".join(body[:max(1, rows)])


def _run_once(pipeline: Pipeline) -> tuple[int, list[str]]:
    fd, path = tempfile.mkstemp(prefix="eosh-watch-")
    os.close(fd)
    try:
        code = _captured(pipeline, path).run()
        with open(path, "rb") as f:
            return code, _split_to_lines(f.read().decode(errors="replace"))
    finally:
        os.unlink(path)


def _interrupted(code: int) -> bool:
    """Whether a run ended on Ctrl+C — which reaches the body, not @watch,
    when it lands mid-run (a PTY child, or SIGINT to the process group)."""
    return code == 130 or code < 0


def register() -> None:
    @command_registry.command(
        "@watch",
        help="Repeatedly run a pipeline, showing its latest output (q quits).",
        params=[
            arg("-n", "--interval", type=float, default=2.0, metavar="SEC",
                help="seconds between runs"),
            arg("--no-clear", action="store_true",
                help="stream output continuously instead of redrawing"),
        ],
    )
    def watch(pipeline, *, interval: float, no_clear: bool) -> None:
        # In ``@watch {ls} | grep py`` this thread's stdout is the outer
        # pipe, not a terminal, so the output streams into it.
        out = sys.stdout
        if no_clear or not out.isatty():
            try:
                while not _interrupted(pipeline.run()):
                    time.sleep(interval)
            except (KeyboardInterrupt, BrokenPipeError):
                pass
            return
        try:
            fd = sys.stdin.fileno()
            saved = terminal.get_mode(fd)
        except Exception:
            fd, saved = -1, None
        command = " | ".join(s.text for s in pipeline.stages)
        out.write(_ENTER)
        try:
            while True:
                # The run itself gets the cooked terminal, so Ctrl+C is a
                # signal that stops it (and comes back as status 130).
                code, lines = _run_once(pipeline)
                if _interrupted(code):
                    return
                cols, rows = terminal.terminal_size()
                now = datetime.datetime.now().strftime("%H:%M:%S")
                out.write(_frame(f"Every {interval:g}s: {command}  [{now}]",
                                 lines, cols, rows))
                out.flush()
                if saved is None:
                    time.sleep(interval)
                    continue
                terminal.set_raw(fd)
                try:
                    deadline = time.monotonic() + interval
                    while (left := deadline - time.monotonic()) > 0:
                        if terminal.wait_readable(fd, left) and \
                                terminal.read_key(fd) in _QUIT_KEYS:
                            return
                finally:
                    terminal.restore_mode(fd, saved)
        except KeyboardInterrupt:
            return
        finally:
            out.write(_LEAVE)
            out.flush()
