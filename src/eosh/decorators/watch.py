"""@watch — re-run a pipeline on a timer with header/footer/scrollbar."""

from __future__ import annotations

import datetime
import os
import re
import shutil
import sys
import tempfile
import threading
import time

from dataclasses import replace

from .. import terminal
from ..colors import _bg, _fg, get_color_scheme
from ..commands import arg
from ..pipeline import Pipeline, Redirect
from ..scrollbar import render_column as _scrollbar_column
from . import registry as decorator_registry


# Alt-screen entry/exit + cursor-home, matching POSIX watch(1):
# entering the alt-screen gives a separate buffer that's discarded on
# exit, so iterations replace each other instead of accumulating into
# scrollback.  ``\x1b[2J\x1b[H`` alone only blanks the visible region —
# old output still scrolls back, which is what users see as "always
# prints new lines."
_ALT_SCREEN_ENTER = "\x1b[?1049h"
_ALT_SCREEN_EXIT = "\x1b[?1049l"
_HIDE_CURSOR = "\x1b[?25l"
_SHOW_CURSOR = "\x1b[?25h"
_CLEAR_SCREEN = "\x1b[2J"
_CURSOR_HOME = "\x1b[H"
_RESET = "\x1b[0m"
# Disable / enable line wrap.  Drawing the last cell of the bottom-right
# corner of the terminal otherwise advances the cursor off-screen and
# triggers an upward scroll on most terminals (DEC margin-overflow
# behaviour).  We turn wrap off for the duration of a frame and back on
# during the inter-frame idle so other writers (e.g. shell prompts after
# the loop exits) behave normally.
_WRAP_OFF = "\x1b[?7l"
_WRAP_ON = "\x1b[?7h"


# Quit keys (POSIX watch(1) uses 'q'; Ctrl+C is the universal abort).
_QUIT_KEYS = (b"q", b"Q", b"\x03")
# Scroll keys — translated to dy/dx deltas applied to the scroll offset.
_KEY_UP = b"\x1b[A"
_KEY_DOWN = b"\x1b[B"
_KEY_RIGHT = b"\x1b[C"
_KEY_LEFT = b"\x1b[D"
_KEY_PAGE_UP = b"\x1b[5~"
_KEY_PAGE_DOWN = b"\x1b[6~"
_KEY_HOME = b"\x1b[H"
_KEY_END = b"\x1b[F"


def _pipeline_redirected_to(pipeline: Pipeline, path: str) -> Pipeline:
    """Return a new Pipeline whose last stage's stdout (and stderr) goes to *path*.

    The original AST is left intact so subsequent iterations re-run the
    user's pipeline as written.  Stderr is folded into the same file
    via ``2>&1`` so error messages end up in the watch frame instead of
    bleeding through onto the alt-screen UI chrome.
    """
    new_stages = list(pipeline.stages)
    last = new_stages[-1]
    new_stages[-1] = replace(
        last,
        redirects=list(last.redirects) + [
            Redirect(kind=">", target=path),
            Redirect(kind="2>&1", target="1"),
        ],
    )
    return Pipeline(stages=new_stages)


def _pipeline_text(pipeline: Pipeline) -> str:
    """Best-effort one-line rendering of the wrapped pipeline."""
    return " | ".join(s.text for s in pipeline.stages)


def _drain_keys(fd: int) -> bytes:
    """Pull every queued key off *fd* without blocking."""
    out = bytearray()
    while terminal.wait_readable(fd, 0):
        chunk = terminal.read_key(fd)
        if not chunk:
            break
        out.extend(chunk)
    return bytes(out)


def _pad_or_trunc(s: str, width: int) -> str:
    """Cut or right-pad with spaces so *s* occupies exactly *width* cells."""
    if width <= 0:
        return ""
    if len(s) >= width:
        return s[:width]
    return s + " " * (width - len(s))


def _format_header(command: str, interval: float, cols: int) -> str:
    """Render the top status bar.

    Layout (POSIX watch(1)-ish):
        Every 2.0s: <command>          <YYYY-MM-DD HH:MM:SS>
    The command is truncated in the middle if necessary to keep both
    the prefix and the timestamp visible.
    """
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefix = f"Every {interval:g}s: "
    # Reserve room for the timestamp on the right side, plus one space gap.
    timestamp_room = len(now) + 1
    cmd_room = max(0, cols - len(prefix) - timestamp_room)
    if len(command) > cmd_room and cmd_room > 1:
        # Middle-ellipsize so both the start and end of the pipeline are
        # visible.
        keep = cmd_room - 1
        left = keep // 2
        right = keep - left
        cmd = command[:left] + "…" + command[-right:] if right > 0 else command[:keep]
    else:
        cmd = command
    body = prefix + cmd
    body = _pad_or_trunc(body, cols - len(now))
    return body + now


_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


def _format_footer(
    cols: int,
    *,
    scroll_y: int,
    total_lines: int,
    visible_rows: int,
    scroll_x: int,
    max_line_len: int,
    paused: bool,
    running: bool = False,
    spinner_frame: int = 0,
) -> str:
    """Render the bottom status bar with scroll info and key hints."""
    # Position indicator — match common pagers: "12-30/200" plus a percent.
    if total_lines == 0:
        pos = "0/0"
        pct = "100%"
    else:
        first = scroll_y + 1 if total_lines else 0
        last = min(total_lines, scroll_y + visible_rows)
        pos = f"{first}-{last}/{total_lines}"
        if total_lines <= visible_rows:
            pct = "ALL"
        elif scroll_y == 0:
            pct = "TOP"
        elif last >= total_lines:
            pct = "BOT"
        else:
            pct = f"{int(round((last / total_lines) * 100))}%"
    hints = "↑↓ PgUp/PgDn g/G ←→  space pause  q quit"
    status_bits = []
    if paused:
        status_bits.append("[PAUSED]")
    if running:
        spin = _SPINNER[spinner_frame % len(_SPINNER)]
        status_bits.append(f"{spin} running")
    status = "  ".join(status_bits)
    h_off = f"  col {scroll_x + 1}/{max(1, max_line_len)}" if max_line_len > cols else ""
    left = f"{pos} {pct}{h_off}"
    if status:
        right = f"{status}   {hints}"
    else:
        right = hints
    gap = max(1, cols - len(left) - len(right))
    return left + " " * gap + right


def _read_scroll_key(fd: int, timeout: float) -> bytes:
    """Wait up to *timeout* seconds for a key and return its bytes (or b"")."""
    if not terminal.wait_readable(fd, timeout):
        return b""
    key = terminal.read_key(fd)
    return key or b""


def _apply_scroll_key(
    key: bytes,
    *,
    scroll_y: int,
    scroll_x: int,
    body_rows: int,
    total_lines: int,
    max_line_len: int,
    body_cols: int,
) -> tuple[int, int]:
    """Apply a scroll key to (scroll_y, scroll_x); clamp to valid range."""
    max_y = max(0, total_lines - body_rows)
    max_x = max(0, max_line_len - body_cols)

    if key == _KEY_UP:
        scroll_y -= 1
    elif key == _KEY_DOWN:
        scroll_y += 1
    elif key == _KEY_PAGE_UP:
        scroll_y -= body_rows
    elif key == _KEY_PAGE_DOWN:
        scroll_y += body_rows
    elif key in (b"g", _KEY_HOME):
        scroll_y = 0
    elif key in (b"G", _KEY_END):
        scroll_y = max_y
    elif key == _KEY_LEFT:
        scroll_x -= 4
    elif key == _KEY_RIGHT:
        scroll_x += 4

    return max(0, min(scroll_y, max_y)), max(0, min(scroll_x, max_x))


def _slice_for_render(
    lines: list[str],
    *,
    scroll_y: int,
    scroll_x: int,
    body_rows: int,
    body_cols: int,
) -> list[str]:
    """Return the visible rectangle of *lines* — body_rows tall, body_cols wide."""
    if body_rows <= 0 or body_cols <= 0:
        return []
    visible = lines[scroll_y:scroll_y + body_rows]
    out: list[str] = []
    for line in visible:
        if scroll_x:
            line = line[scroll_x:]
        if len(line) > body_cols:
            line = line[:body_cols]
        out.append(line)
    # Pad with blanks so the previous frame's tail can't bleed through
    # when the new output is shorter.
    while len(out) < body_rows:
        out.append("")
    return out


def _render_scrollbar(
    *,
    body_rows: int,
    scroll_y: int,
    total_lines: int,
) -> list[str]:
    """Return body_rows single-char strings — the scrollbar column.

    Empty when content fits on screen.  Otherwise a thumb proportional
    to the visible window; track is the rest.  Colors come from the
    active ColorScheme (shared with the inline picker's scrollbar).

    The thumb slides in 1/8-row steps via the shared sub-cell renderer
    (see ``eosh.scrollbar``) so scrolling reads smoothly instead of
    snapping a whole row at a time.
    """
    if body_rows <= 0:
        return []
    if total_lines <= body_rows:
        return [" "] * body_rows
    s = get_color_scheme()
    return _scrollbar_column(
        height=body_rows,
        offset=scroll_y,
        visible=body_rows,
        total=total_lines,
        thumb=s.scroll_thumb,
        track=s.scroll_track,
    )


# Escape-sequence stripper.  Tries to cover the common forms of ANSI
# escape sequences a CLI may emit even when stdout is redirected to a
# file (TTY-autodetection in the wild is unreliable):
#
#   * CSI:  ESC [ <params> <intermediate>* <final 0x40..0x7e>
#   * 8-bit CSI: a single 0x9b byte playing the same role
#   * OSC:  ESC ] ... terminated by BEL (\x07) or ST (ESC \)
#   * DCS:  ESC P ... ESC \
#   * Generic two-/three-byte ESC sequences (ESC + intermediate* + final)
#
# Order matters: CSI is matched before the generic ESC rule so the
# full parameter list is consumed in one shot rather than just `ESC [`.
_ANSI_RE = re.compile(
    r"\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"
    r"|\x9b[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1bP[^\x1b]*\x1b\\"
    r"|\x1b[\x20-\x2f]*[\x30-\x7e]"
)

# Stray C0/C1 control bytes that survive the escape strip (e.g. BS, BEL,
# DEL emitted mid-line for crude over-print effects).  Drop them so they
# don't inflate ``len(line)`` in the column-count display.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _split_to_lines(text: str) -> list[str]:
    """Split *text* into displayable rows.

    Real-world command output isn't always pure text — tools that
    autodetect a TTY can still emit colour, cursor-position, or
    bracketed-paste escape sequences when their stdout is redirected to
    a file.  Those would otherwise:

      * inflate ``len(line)`` so the footer reports an absurd
        ``col 1/14488``;
      * occasionally make a row render in the wrong colour (if a
        non-resetting SGR runs to end-of-line);
      * hide the start of a row if a ``\\r`` appears mid-line.

    Strip ANSI / OSC sequences, normalise CRLF and bare CR to LF, then
    expand tabs.
    """
    # Normalise line endings before splitting.
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _ANSI_RE.sub("", cleaned)
    cleaned = _CTRL_RE.sub("", cleaned)
    return [line.expandtabs(8) for line in cleaned.splitlines()]


def register() -> None:
    @decorator_registry.decorator(
        name="watch",
        help="Repeatedly run a pipeline (q quits; arrows / PgUp / PgDn scroll).",
        params=[
            arg("-n", "--interval", type=float, default=2.0, metavar="SEC",
                help="seconds between runs"),
            arg("--no-clear", action="store_true",
                help="stream output continuously (no alt-screen, no UI chrome)"),
        ],
    )
    def watch(pipeline, *, interval: float, no_clear: bool) -> None:
        # Each iteration's capture (``> tmpfile``) rebinds stdout only on
        # the stage's own thread, so this thread's ``sys.stdout`` stays
        # wherever ``@watch`` itself writes: the terminal, or the outer
        # pipe in ``@watch {ls} | grep py`` (where it isn't a tty, so we
        # stream into the pipe instead of drawing a UI).
        out = sys.stdout
        is_tty = out.isatty()
        use_alt_screen = is_tty and not no_clear
        cmd_text = _pipeline_text(pipeline)

        if not use_alt_screen:
            # Streaming mode — equivalent to a `while true; do …; sleep N; done` loop.
            try:
                while True:
                    try:
                        pipeline.run()
                    except BrokenPipeError:
                        return
                    time.sleep(interval)
            except KeyboardInterrupt:
                return
            return

        # ── alt-screen mode ────────────────────────────────────────────
        try:
            stdin_fd = sys.stdin.fileno()
        except (OSError, ValueError):
            stdin_fd = -1

        saved_mode = None
        if stdin_fd >= 0:
            try:
                saved_mode = terminal.get_mode(stdin_fd)
                terminal.set_raw(stdin_fd)
            except Exception:
                saved_mode = None

        # Reset SGR *before* clearing — \x1b[2J clears the screen with the
        # current background colour, so a leftover reverse-video state
        # (or any non-default background) would leave the entire alt
        # screen painted with the wrong colour.
        out.write(
            _ALT_SCREEN_ENTER + _HIDE_CURSOR + _WRAP_OFF + _RESET
            + _CLEAR_SCREEN + _CURSOR_HOME
        )
        out.flush()

        scroll_y = 0     # preserved across iterations — see the redraw loop
        scroll_x = 0
        body_lines: list[str] = []
        last_run_finished_at = 0.0
        paused = False
        spinner_frame = 0

        # Worker-thread coordination.  ``_do_run`` runs on a worker so the
        # main thread can keep polling keys and redrawing while the
        # pipeline is in flight.  ``result_lock`` guards ``_pending_lines``
        # / ``_run_done``; ``run_done_event`` lets the main loop wake the
        # instant the pipeline finishes (no polling delay).
        result_lock = threading.Lock()
        run_done_event = threading.Event()
        _pending: dict[str, object] = {"lines": None, "error": None}

        def _do_run() -> None:
            """Run the pipeline; deposit lines (or error) under the lock."""
            fd_, path = tempfile.mkstemp(prefix="eosh-watch-")
            os.close(fd_)
            lines: list[str] = []
            err: BaseException | None = None
            try:
                try:
                    redirected = _pipeline_redirected_to(pipeline, path)
                    redirected.run()
                    try:
                        with open(path, "rb") as f:
                            raw = f.read()
                    except OSError:
                        raw = b""
                    # Optional debug aid: when EOSH_WATCH_DEBUG is set,
                    # mirror the captured raw bytes to /tmp so the user
                    # can inspect what the wrapped command actually emits
                    # (helps diagnose ANSI / line-count surprises).
                    if os.environ.get("EOSH_WATCH_DEBUG"):
                        try:
                            with open("/tmp/eosh-watch-last.raw", "wb") as df:
                                df.write(raw)
                        except OSError:
                            pass
                    lines = _split_to_lines(raw.decode(errors="replace"))
                except BaseException as e:
                    err = e
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            with result_lock:
                _pending["lines"] = lines
                _pending["error"] = err
            run_done_event.set()

        def _start_run() -> threading.Thread:
            """Spawn a worker for one iteration; return the Thread handle."""
            run_done_event.clear()
            with result_lock:
                _pending["lines"] = None
                _pending["error"] = None
            t = threading.Thread(target=_do_run, name="watch-iter", daemon=True)
            t.start()
            return t

        def _redraw(running: bool) -> None:
            cols, rows = terminal.terminal_size()
            body_rows = max(0, rows - 2)
            total = len(body_lines)
            max_line_len = max((len(l) for l in body_lines), default=0)
            # Reserve the right edge for the scrollbar only when we
            # actually need one — otherwise let the body use the full
            # width.  (Showing a scrollbar against content that fits
            # would just steal a column for nothing.)
            need_v_scroll = total > body_rows
            scroll_col_w = 1 if need_v_scroll else 0
            body_cols = max(0, cols - scroll_col_w)
            # Clamp scroll if the current content can't accommodate it.
            sy = min(scroll_y, max(0, total - body_rows))
            sx = min(scroll_x, max(0, max_line_len - max(1, body_cols)))
            # Header and footer render one cell short of the full
            # width — writing the bottom-right cell would advance the
            # cursor past the margin and trigger a scroll on some
            # terminals (xterm.js / VS Code) even with wrap disabled.
            # The right-edge cell on each bar is then painted by EL
            # (\x1b[K) under the status-bar SGR — see the assembly
            # below — so the background reaches the edge without any
            # character actually being written there.
            bar_w = max(0, cols - 1)
            header = _format_header(cmd_text, interval, bar_w)
            footer = _format_footer(
                bar_w,
                scroll_y=sy,
                total_lines=total,
                visible_rows=body_rows,
                scroll_x=sx,
                max_line_len=max_line_len,
                paused=paused,
                running=running,
                spinner_frame=spinner_frame,
            )
            visible = _slice_for_render(
                body_lines,
                scroll_y=sy,
                scroll_x=sx,
                body_rows=body_rows,
                body_cols=body_cols,
            )
            bar = (
                _render_scrollbar(body_rows=body_rows, scroll_y=sy, total_lines=total)
                if need_v_scroll else []
            )
            # Build the whole frame.  We always stop one short of the
            # bottom-right cell — writing it would advance the cursor
            # past the margin, and on some terminals (xterm.js / VS
            # Code) that triggers a scroll even with wrap disabled.
            # The body and header rows are safe to fill edge-to-edge
            # because the cursor is repositioned absolutely on the next
            # row before it can wrap.
            scheme = get_color_scheme()
            statusbar_sgr = _bg(*scheme.statusbar_bg) + _fg(*scheme.statusbar_fg)
            # EL (\x1b[K) clears to end-of-line in the *current* SGR
            # background (back-color-erase), so issuing it after the
            # status-bar SGR paints the unwritten right-edge cell with
            # the bar's background colour.  EL doesn't move the cursor,
            # so it can't trip the margin-overflow scroll the way
            # writing a character into that cell would.
            parts: list[str] = [
                "\x1b[1;1H",
                statusbar_sgr,
                "\x1b[K",
                _pad_or_trunc(header, bar_w),
                _RESET,
            ]
            for i in range(body_rows):
                line = visible[i] if i < len(visible) else ""
                row = _pad_or_trunc(line, body_cols)
                if need_v_scroll:
                    row += bar[i] if i < len(bar) else " "
                parts.append(f"\x1b[{i + 2};1H{_RESET}\x1b[K{row}")
            parts.append(
                f"\x1b[{rows};1H{statusbar_sgr}\x1b[K{footer}{_RESET}"
            )
            out.write("".join(parts))
            out.flush()

        def _consume_finished_run() -> bool:
            """Pick up worker results.  Returns True if a BrokenPipeError
            arrived (caller should exit)."""
            nonlocal body_lines, last_run_finished_at, scroll_y, scroll_x
            with result_lock:
                lines = _pending["lines"]
                err = _pending["error"]
            if isinstance(err, BrokenPipeError):
                return True
            if lines is not None:
                body_lines = lines  # type: ignore[assignment]
            last_run_finished_at = time.monotonic()
            cols, rows = terminal.terminal_size()
            body_rows = max(0, rows - 2)
            max_line_len = max((len(l) for l in body_lines), default=0)
            need_v_scroll = len(body_lines) > body_rows
            body_cols = max(0, cols - (1 if need_v_scroll else 0))
            scroll_y = min(scroll_y, max(0, len(body_lines) - body_rows))
            scroll_x = min(scroll_x, max(0, max_line_len - max(1, body_cols)))
            return False

        def _handle_keys(burst: bytes) -> bool:
            """Apply a key burst.  Returns True if the user asked to quit."""
            nonlocal scroll_y, scroll_x, paused
            if any(q in burst for q in _QUIT_KEYS):
                return True
            if b" " in burst:
                paused = not paused
            cols, rows = terminal.terminal_size()
            body_rows = max(0, rows - 2)
            need_v_scroll = len(body_lines) > body_rows
            body_cols = max(0, cols - (1 if need_v_scroll else 0))
            max_line_len = max((len(l) for l in body_lines), default=0)
            for k in (
                _KEY_UP, _KEY_DOWN, _KEY_PAGE_UP, _KEY_PAGE_DOWN,
                _KEY_HOME, _KEY_END, _KEY_LEFT, _KEY_RIGHT,
                b"g", b"G",
            ):
                if k in burst:
                    scroll_y, scroll_x = _apply_scroll_key(
                        k,
                        scroll_y=scroll_y,
                        scroll_x=scroll_x,
                        body_rows=body_rows,
                        total_lines=len(body_lines),
                        max_line_len=max_line_len,
                        body_cols=body_cols,
                    )
                    break
            return False

        try:
            # First iteration starts immediately on a worker.  The main
            # loop below polls for keys and pumps the spinner while it
            # runs.
            worker = _start_run()
            running = True
            spinner_last_tick = time.monotonic()
            _redraw(running=True)

            while True:
                # While the worker is running, poll keys frequently and
                # advance the spinner; otherwise wait for the interval to
                # elapse before kicking off the next iteration.
                if running:
                    timeout = 0.1   # keep input/spinner responsive
                else:
                    if paused:
                        timeout = 0.25
                    else:
                        elapsed = time.monotonic() - last_run_finished_at
                        timeout = max(0.0, interval - elapsed)

                # Read at most one key per pass, but cap timeout so we can
                # also notice run-done / spinner-tick / next-interval.
                if stdin_fd >= 0:
                    key = _read_scroll_key(stdin_fd, min(timeout, 0.1))
                else:
                    time.sleep(timeout)
                    key = b""

                if key:
                    extra = _drain_keys(stdin_fd) if stdin_fd >= 0 else b""
                    if _handle_keys(key + extra):
                        return
                    _redraw(running=running)
                    continue

                # Worker finished → swap in its output and redraw.
                if running and run_done_event.is_set():
                    if _consume_finished_run():
                        return
                    running = False
                    _redraw(running=False)
                    continue

                # Spinner tick — advance one frame ~10×/s while running.
                if running and time.monotonic() - spinner_last_tick >= 0.1:
                    spinner_frame = (spinner_frame + 1) % len(_SPINNER)
                    spinner_last_tick = time.monotonic()
                    _redraw(running=True)
                    continue

                # Idle — kick off the next iteration if it's time and
                # we're not paused.
                if not running and not paused:
                    elapsed = time.monotonic() - last_run_finished_at
                    if elapsed >= interval:
                        worker = _start_run()
                        running = True
                        spinner_last_tick = time.monotonic()
                        _redraw(running=True)
        except KeyboardInterrupt:
            return
        finally:
            out.write(_WRAP_ON + _SHOW_CURSOR + _ALT_SCREEN_EXIT)
            out.flush()
            if saved_mode is not None and stdin_fd >= 0:
                try:
                    terminal.restore_mode(stdin_fd, saved_mode)
                except Exception:
                    pass
