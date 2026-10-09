"""Process multiplexing — PTY-backed subprocess slots with output buffering."""

from __future__ import annotations

import collections
import os
import re
import struct
import sys
import threading
import time
from typing import Callable

# PTY-backed process multiplexing is POSIX-only.  On Windows these modules do
# not exist; ProcessSlot is simply never instantiated there (the shell runs
# external commands on the real console instead), but OutputBuffer and the
# module itself must still import cleanly.
if os.name != "nt":
    import fcntl
    import pty
    import select
    import termios


class OutputBuffer:
    """Thread-safe ring buffer of raw byte chunks."""

    def __init__(self, max_chunks: int = 1000):
        self._buf: collections.deque[bytes] = collections.deque(maxlen=max_chunks)
        self._lock = threading.Lock()

    def append(self, data: bytes) -> None:
        with self._lock:
            self._buf.append(data)

    def drain(self) -> list[bytes]:
        with self._lock:
            chunks = list(self._buf)
            self._buf.clear()
            return chunks

    def peek(self) -> bytes:
        """Return all currently buffered bytes (concatenated) without draining."""
        with self._lock:
            return b"".join(self._buf)


class ExitCallbackMixin:
    """The "this slot's work ended" hook and the facts it reports, shared by
    every slot type.

    A slot's work ends on a thread the shell isn't watching — ``ProcessSlot``'s
    reader thread, ``PythonCommandSlot``'s command thread — so the shell
    passes its handler (*on_exit*, called with the slot) when it constructs
    the slot, and the end of the work calls it exactly once.  Wired at
    construction, before anything runs, it can't race the slot's own end.

    :attr:`parked` is what the handler decides on: the shell sets it when it
    hands the slot to a context to keep running in the background (Ctrl+]).
    A slot that never was ran in the foreground, and the line's own timing
    already covered it.

    A mixin: ``ProcessSlot`` (here) and ``PythonCommandSlot`` (``shell.py``)
    share nothing else; each calls :meth:`_init_exit_callback` from its own
    constructor.
    """

    def _init_exit_callback(self, on_exit: "Callable[[object], None] | None" = None) -> None:
        self.on_exit = on_exit
        #: Set by the shell when the slot is parked on a context.
        self.parked = False
        #: The command line that started the slot, set with :attr:`parked`
        #: so the handler can report the line rather than just argv.
        self.line: str | None = None
        #: Its row in the shared history, so the handler can record the
        #: exit status there.
        self.history_id: int | None = None
        #: ``time.monotonic()`` at :meth:`start`, so the handler can report
        #: how long the work took.
        self.start_time: float | None = None
        self._on_exit_fired = False

    def mark_started(self) -> None:
        self.start_time = time.monotonic()

    def elapsed(self) -> float:
        """Seconds since :meth:`mark_started`, or 0.0 if never started."""
        if self.start_time is None:
            return 0.0
        return time.monotonic() - self.start_time

    def _fire_on_exit(self) -> None:
        """Signal end-of-work: call ``on_exit(self)`` once."""
        if self._on_exit_fired or self.on_exit is None:
            return
        self._on_exit_fired = True
        try:
            self.on_exit(self)
        except Exception:
            pass  # a slot's teardown must not die on a bad callback


# mode_num -> (name, default_action)
# default_action is the byte ('h' or 'l') the terminal sits at when no app
# has touched the mode.  True in self.terminal_modes means "an app has put
# the mode into its non-default state" — so suspend emits default_action to
# revert, and restore emits the opposite to re-apply.
#
# Adding a new mode here is only safe when all three hold:
#   1. Default polarity is unambiguous across the terminals we care about.
#      Wrong polarity → the parser misclassifies enable/disable and the
#      leak we were trying to plug becomes a silent no-op.
#   2. \x1b[?Nh / \x1b[?Nl is a true symmetric toggle with no compound side
#      effects (no save/restore of cursor position, no implicit clear).
#      Counter-examples to NOT track: 1047 (alt-screen without save) and
#      1048 (cursor save) overlap with 1049 — toggling them independently
#      on switch-back can lose the saved cursor or double-clear.  9 (X10
#      mouse) is fire-once, not a sustained mode, so re-enabling on
#      switch-back is meaningless.
#   3. Real apps leak it across context switches.  1005/1015/1016
#      (alternate mouse encodings) qualify if we ever see an app use them
#      alongside 1006; 7 (autowrap) and 12 (cursor blink) are rarely
#      toggled in a way that crosses contexts.
#
# Known limitations of the surrounding machinery (not fatal, but worth
# knowing before extending this table):
#   - The parser in _track_terminal_modes() reads one mode number per
#     ESC[?…h/l sequence.  Compound forms like \x1b[?1000;1002;1006h
#     register only the first number; subsequent ones are skipped.  Apps
#     in practice tend to send each mode separately, so this hasn't
#     bitten us yet.
#   - Mouse-tracking modes 1000/1002/1003 are mutually exclusive at the
#     terminal level (enabling one implicitly disables the others), but
#     we track them as independent booleans.  Suspend may emit redundant
#     resets — harmless, but it's not a faithful mirror of terminal state.
_TRACKED_MODES: dict[bytes, tuple[str, bytes]] = {
    b"1":    ("app_cursor_keys",  b"l"),  # DECCKM — application cursor key sequences
    b"25":   ("cursor_visible",   b"h"),  # DECTCEM — default is *visible*
    b"1000": ("mouse_click",      b"l"),
    b"1002": ("mouse_btn",        b"l"),
    b"1003": ("mouse_all",        b"l"),
    b"1004": ("focus",            b"l"),  # focus in/out reporting
    b"1006": ("mouse_sgr",        b"l"),
    b"1049": ("alt_screen",       b"l"),  # alternate screen buffer
    b"2004": ("bracketed_paste",  b"l"),  # paste wrapped in \x1b[200~ … \x1b[201~
}


def _opposite(action: bytes) -> bytes:
    return b"l" if action == b"h" else b"h"


class ProcessSlot(ExitCallbackMixin):
    """Manages a single PTY subprocess with output buffering for context switching."""

    def __init__(self, on_exit=None):
        self._init_exit_callback(on_exit)
        self.pid: int = -1
        self.master_fd: int = -1
        self.argv: list[str] = []
        # Full history — replayed for alt-screen TUIs (idempotent paints).
        self.buffer: OutputBuffer = OutputBuffer()
        # Bytes received while backgrounded — replayed once for streaming
        # output so users don't lose lines but also don't see duplicates.
        self.missed: OutputBuffer = OutputBuffer()
        self.active: bool = False
        # Last byte ever written to the real stdout from this slot.  Used by
        # the switch-handler to tell whether the cursor is already at column
        # 0 (last byte ``\n`` / ``\r``) so it can skip the protective newline
        # it would otherwise emit before opening the picker.  Bytes coming
        # off the PTY are raw — cooked-mode CRLF translation does not apply
        # here, so apps that emit a bare ``\n`` still register correctly.
        self._last_out_byte: bytes = b"\n"
        self.exit_code: int | None = None
        self._exit_event = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self.terminal_modes: dict[str, bool] = {
            name: False for name, _ in _TRACKED_MODES.values()
        }

    def start(self, argv: list[str], env: dict[str, str], cwd: str) -> None:
        self.argv = argv
        self.mark_started()
        master_fd, slave_fd = pty.openpty()

        # Set PTY size before fork so child sees correct dimensions immediately.
        # Don't set LINES/COLUMNS in env: ncurses honors those over TIOCGWINSZ
        # and then ignores SIGWINCH-driven resize (KEY_RESIZE never fires).
        rows, cols = self._get_real_terminal_size()
        if rows and cols:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

        # Pipe for the child to report exec failure to the parent. CLOEXEC
        # on the write end means a successful exec auto-closes it and the
        # parent reads EOF. On exec failure the child writes the errno and
        # exits, so the parent can surface a real FileNotFoundError instead
        # of leaving a dead PTY slot registered with the failed argv.
        err_r, err_w = os.pipe()
        flags = fcntl.fcntl(err_w, fcntl.F_GETFD)
        fcntl.fcntl(err_w, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)

        pid = os.fork()
        if pid == 0:
            # Child process
            try:
                os.close(master_fd)
                os.close(err_r)
                os.setsid()
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
                os.dup2(slave_fd, 0)
                os.dup2(slave_fd, 1)
                os.dup2(slave_fd, 2)
                if slave_fd > 2:
                    os.close(slave_fd)
                os.chdir(cwd)
                os.execvpe(argv[0], argv, env)
            except BaseException as e:
                errno_val = getattr(e, "errno", 0) or 0
                try:
                    os.write(err_w, errno_val.to_bytes(4, "little", signed=True))
                except OSError:
                    pass
            os._exit(127)
        else:
            # Parent process
            os.close(slave_fd)
            os.close(err_w)
            try:
                err_data = b""
                while len(err_data) < 4:
                    chunk = os.read(err_r, 4 - len(err_data))
                    if not chunk:
                        break
                    err_data += chunk
            finally:
                os.close(err_r)
            if err_data:
                # Child reported exec failure — reap it and raise.
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
                try:
                    os.close(master_fd)
                except OSError:
                    pass
                errno_val = int.from_bytes(err_data.ljust(4, b"\x00"), "little", signed=True)
                raise OSError(errno_val, os.strerror(errno_val) if errno_val else "exec failed", argv[0])
            self.pid = pid
            self.master_fd = master_fd
            self._reader_thread = threading.Thread(
                target=self._reader_loop, daemon=True
            )
            self._reader_thread.start()

    @staticmethod
    def _get_real_terminal_size() -> tuple[int, int]:
        """Get the real terminal size, trying multiple fds."""
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            try:
                fd = stream.fileno()
                size = os.get_terminal_size(fd)
                return size.lines, size.columns
            except (OSError, ValueError, AttributeError):
                continue
        import shutil
        size = shutil.get_terminal_size()
        return size.lines, size.columns

    def _reader_loop(self) -> None:
        try:
            while True:
                # Use select before read: on macOS, a blocking os.read() wakes
                # with EIO when the slave PTY closes, discarding buffered data.
                # select() correctly reports the fd as readable when data is
                # available even after slave close, so we get the data first.
                try:
                    r, _, _ = select.select([self.master_fd], [], [], 0.05)
                    if not r:
                        continue
                    data = os.read(self.master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                self._track_terminal_modes(data)
                self.buffer.append(data)
                self._last_out_byte = data[-1:]
                if self.active:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                else:
                    self.missed.append(data)
        finally:
            try:
                _, status = os.waitpid(self.pid, 0)
                if os.WIFEXITED(status):
                    self.exit_code = os.WEXITSTATUS(status)
                else:
                    self.exit_code = -1
            except ChildProcessError:
                self.exit_code = -1
            self._exit_event.set()
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self._fire_on_exit()

    def _track_terminal_modes(self, data: bytes) -> None:
        """Scan output for DEC private mode set/reset sequences."""
        # Match \x1b[?<number>h (enable) and \x1b[?<number>l (disable)
        i = 0
        while i < len(data):
            idx = data.find(b"\x1b[?", i)
            if idx == -1:
                break
            # Parse the number after \x1b[?
            start = idx + 3
            end = start
            while end < len(data) and data[end:end + 1].isdigit():
                end += 1
            if end < len(data) and end > start:
                mode_num = data[start:end]
                action = data[end:end + 1]
                if mode_num in _TRACKED_MODES:
                    name, default_action = _TRACKED_MODES[mode_num]
                    if action == _opposite(default_action):
                        self.terminal_modes[name] = True
                    elif action == default_action:
                        self.terminal_modes[name] = False
            i = end + 1 if end > idx else idx + 1

    def activate(self, *, replay_missed: bool = False) -> None:
        """Resume writing reader output to stdout.

        If *replay_missed* is True, flush bytes that arrived while inactive
        to stdout before clearing the missed-buffer. Done under the same
        buffer lock the reader takes, so nothing is dropped or duplicated
        across the activation boundary.
        """
        with self.missed._lock:
            if replay_missed:
                for chunk in self.missed._buf:
                    sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            self.missed._buf.clear()
            self.active = True

    def deactivate(self) -> None:
        self.active = False

    def cursor_at_col0(self) -> bool:
        """Heuristic: did the most recent PTY output leave the cursor at column 0?

        ``True`` when the last byte written to the real terminal was a line
        terminator.  The switch handler uses this to skip its protective
        newline (which would otherwise leave a blank row for the common
        case of a subprocess whose output ends in ``\\n``).
        """
        return self._last_out_byte in (b"\n", b"\r")

    def suspend_terminal_modes(self) -> str:
        """Return escape sequences to undo active terminal modes on switch-away."""
        _BY_NAME = {name: (num, default) for num, (name, default) in _TRACKED_MODES.items()}
        seq = ""
        for name, active in self.terminal_modes.items():
            if active:
                num, default_action = _BY_NAME[name]
                seq += f"\x1b[?{num.decode()}{default_action.decode()}"
        return seq

    def restore_terminal_modes(self) -> str:
        """Return escape sequences to re-enable terminal modes on switch-back."""
        _BY_NAME = {name: (num, default) for num, (name, default) in _TRACKED_MODES.items()}
        seq = ""
        for name, active in self.terminal_modes.items():
            if active:
                num, default_action = _BY_NAME[name]
                seq += f"\x1b[?{num.decode()}{_opposite(default_action).decode()}"
        return seq

    def replay_buffer(self) -> None:
        chunks = self.buffer.drain()
        for chunk in chunks:
            sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()

    def write_stdin(self, data: bytes) -> None:
        try:
            os.write(self.master_fd, data)
        except OSError:
            pass

    def resize(self, rows: int, cols: int) -> None:
        import signal as sig

        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass
        # On macOS, TIOCSWINSZ on master doesn't auto-deliver SIGWINCH.
        # Send to the child's process group so all foreground processes see it.
        try:
            os.killpg(os.getpgid(self.pid), sig.SIGWINCH)
        except (OSError, ProcessLookupError):
            pass

    def is_alive(self) -> bool:
        return self.exit_code is None

    def wait(self, timeout: float | None = None) -> bool:
        return self._exit_event.wait(timeout)

    def kill(self) -> None:
        import signal as sig

        if self.is_alive():
            try:
                os.kill(self.pid, sig.SIGTERM)
            except ProcessLookupError:
                pass

    def tail_lines(self, n: int) -> list[str]:
        """Return up to *n* most recent output lines from the buffer (non-destructive)."""
        return _tail_lines_from_bytes(self.buffer.peek(), n)


def _tail_lines_from_bytes(data: bytes, n: int) -> list[str]:
    """Decode *data* and return the last *n* non-empty lines, with ANSI escape sequences stripped."""
    if not data or n <= 0:
        return []
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return []
    text = _strip_ansi(text)
    # Normalize CRLF and bare CR (cursor returns) to LF before splitting.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    return lines[-n:]


# CSI (ESC [ ...), OSC (ESC ] ... BEL or ST), and single-char ESC sequences.
_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-Z\\-_]"
)


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)
