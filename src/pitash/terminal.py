"""Cross-platform terminal primitives — raw mode, key reading, resize.

The rest of the shell (lineedit, tui, shell) drives the terminal through this
module instead of touching ``termios``/``tty``/``select``/``msvcrt`` directly,
so the same rendering code runs on both POSIX and native Windows.

POSIX backend: ``termios`` + ``tty`` for raw mode, ``os.read`` + ``select`` for
input, ``SIGWINCH`` for resize notification.

Windows backend: ``msvcrt`` for unbuffered key reading (translating the
``\\x00``/``\\xe0`` scan-code prefixes into the same ANSI escape sequences the
POSIX path produces), and the Win32 console API (via ``ctypes``) to enable
virtual-terminal output processing so the ANSI escapes the renderer emits are
honoured.  There is no ``SIGWINCH`` on Windows, so resize is detected by
polling :func:`os.get_terminal_size` between key reads.
"""

from __future__ import annotations

import os
import re
import signal
import sys
import time

IS_WINDOWS = os.name == "nt"

# Bytes that arrived on stdin but haven't been delivered to a caller yet.
# Populated by :func:`query_cursor_position` when the DSR reply arrives
# interleaved with user keystrokes — those keystrokes are pushed here so
# the next :func:`read_key` consumes them before touching ``os.read``.
_pending_input: bytes = b""

# True when the platform delivers a signal on terminal resize.  Callers use
# this to decide between signal-driven reflow (POSIX) and poll-based resize
# detection (Windows).
HAS_SIGWINCH = hasattr(signal, "SIGWINCH")

if IS_WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes
else:
    import select
    import termios
    import tty


# ── Windows console mode management ─────────────────────────────────────────

if IS_WINDOWS:
    _kernel32 = ctypes.windll.kernel32

    _STD_OUTPUT_HANDLE = -11
    _ENABLE_PROCESSED_OUTPUT = 0x0001
    _ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    _DISABLE_NEWLINE_AUTO_RETURN = 0x0008

    def _out_handle() -> int:
        return _kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)

    def _get_console_mode(handle: int) -> int | None:
        mode = wintypes.DWORD()
        if _kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return mode.value
        return None

    def _set_console_mode(handle: int, mode: int) -> None:
        _kernel32.SetConsoleMode(handle, mode)

    def _enable_vt_output() -> None:
        """Turn on ANSI escape interpretation for stdout (idempotent)."""
        handle = _out_handle()
        mode = _get_console_mode(handle)
        if mode is None:
            return  # not a real console (redirected) — nothing to do
        _set_console_mode(
            handle,
            mode | _ENABLE_PROCESSED_OUTPUT | _ENABLE_VIRTUAL_TERMINAL_PROCESSING,
        )


def init() -> None:
    """One-time terminal setup. Safe to call repeatedly.

    On Windows this enables VT output processing and disables Python's
    automatic ``\\n`` → ``\\r\\n`` translation on the std streams so the
    renderer has the same byte-level control it has on POSIX (where raw mode
    turns off ONLCR).  Carriage returns are then governed entirely by console
    mode: :func:`set_raw` sets ``DISABLE_NEWLINE_AUTO_RETURN`` so a bare ``\\n``
    is a pure line-feed during rendering, and :func:`restore_mode` clears it so
    cooked output (normal ``print``) still wraps to column 0.
    """
    if not IS_WINDOWS:
        return
    _enable_vt_output()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(newline="")
        except (AttributeError, ValueError):
            pass


# ── raw mode ────────────────────────────────────────────────────────────────


def get_mode(fd: int):
    """Snapshot the current terminal mode so it can be restored later.

    Returns an opaque token (POSIX: termios attrs; Windows: None — restoration
    only needs to clear the newline flag).
    """
    if IS_WINDOWS:
        return None
    return termios.tcgetattr(fd)


def set_raw(fd: int) -> None:
    """Put the terminal into raw mode for character-at-a-time editing.

    Both input and output are raw: bare ``\\n`` does NOT get a CR appended.
    Renderers that paint at exact cursor positions (the line editor, TUI
    pickers, the watch decorator's alt screen) want this — they emit
    explicit ``\\r\\n`` and rely on the kernel staying out of the way.
    """
    if IS_WINDOWS:
        _enable_vt_output()
        handle = _out_handle()
        mode = _get_console_mode(handle)
        if mode is not None:
            _set_console_mode(handle, mode | _DISABLE_NEWLINE_AUTO_RETURN)
        return
    # TCSADRAIN (not TCSAFLUSH) preserves bytes already queued in the kernel's
    # input buffer — e.g. the remainder of a pasted multi-line block.
    tty.setraw(fd, termios.TCSADRAIN)


def set_raw_input_cooked_output(fd: int) -> None:
    """Raw input, but keep ``\\n`` → ``\\r\\n`` translation on output (POSIX).

    This is the right mode while a Python command's body is running:
    keystrokes still arrive one at a time so the main loop can intercept
    Ctrl+] / Ctrl+C, but anything the command writes — through ``print``,
    raw byte writes via ``sys.stdout.buffer``, ``os.write``, captured
    pexpect output, or a piped subprocess's direct fd-1 write — gets a
    proper carriage return prepended to each LF, so multi-line text isn't
    rendered as a staircase.

    Equivalent to ``cfmakeraw`` followed by re-enabling ``OPOST | ONLCR``.
    On Windows, console output is governed by leaving
    ``DISABLE_NEWLINE_AUTO_RETURN`` cleared (so the platform path is the
    same as :func:`restore_mode`'s output side); only the input side
    differs from cooked, and msvcrt-based reads already return one key
    at a time.
    """
    if IS_WINDOWS:
        # Windows: rely on default ENABLE_PROCESSED_OUTPUT (set by init())
        # for CRLF translation; raw INPUT is already the default for
        # msvcrt key reads. Just make sure DISABLE_NEWLINE_AUTO_RETURN is
        # NOT set, so a bare \n still carriage-returns to column 0.
        _enable_vt_output()
        handle = _out_handle()
        mode = _get_console_mode(handle)
        if mode is not None:
            _set_console_mode(handle, mode & ~_DISABLE_NEWLINE_AUTO_RETURN)
        return
    attrs = termios.tcgetattr(fd)
    # cfmakeraw equivalent — clear input, output, control, local flags
    # except we re-enable OPOST (output post-processing, including ONLCR).
    attrs[0] &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK |
                  termios.ISTRIP | termios.INLCR | termios.IGNCR |
                  termios.ICRNL | termios.IXON)
    attrs[1] &= ~termios.OPOST
    attrs[1] |= termios.OPOST | termios.ONLCR  # re-enable CRLF translation
    attrs[2] &= ~(termios.CSIZE | termios.PARENB)
    attrs[2] |= termios.CS8
    attrs[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON |
                  termios.ISIG | termios.IEXTEN)
    attrs[6][termios.VMIN] = 1
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSADRAIN, attrs)


def restore_mode(fd: int, saved) -> None:
    """Restore the terminal to the mode captured by :func:`get_mode`."""
    if IS_WINDOWS:
        handle = _out_handle()
        mode = _get_console_mode(handle)
        if mode is not None:
            _set_console_mode(handle, mode & ~_DISABLE_NEWLINE_AUTO_RETURN)
        return
    termios.tcsetattr(fd, termios.TCSADRAIN, saved)


# ── input ───────────────────────────────────────────────────────────────────

if IS_WINDOWS:
    # Map the second byte of a \x00 / \xe0 scan-code pair to the ANSI escape
    # sequence the POSIX path would produce, so the key parsers in lineedit/tui
    # can stay platform-agnostic.
    _WIN_SCANCODES = {
        "H": b"\x1b[A",   # up
        "P": b"\x1b[B",   # down
        "K": b"\x1b[D",   # left
        "M": b"\x1b[C",   # right
        "G": b"\x1b[H",   # home
        "O": b"\x1b[F",   # end
        "R": b"\x1b[2~",  # insert
        "S": b"\x1b[3~",  # delete
        "I": b"\x1b[5~",  # page up
        "Q": b"\x1b[6~",  # page down
    }

    def _win_read_key() -> bytes:
        """Read one logical key from the console, returning ANSI-style bytes."""
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            scan = msvcrt.getwch()
            return _WIN_SCANCODES.get(scan, b"")
        # Ordinary character (control or printable). Encode to UTF-8 so the
        # byte-oriented parsers see \x1b, \r, \t, control codes, and multibyte
        # characters exactly as they would on POSIX.
        return ch.encode("utf-8")


def wait_readable(fd: int, timeout: float) -> bool:
    """Return True if a key is available within *timeout* seconds."""
    if _pending_input:
        return True
    if IS_WINDOWS:
        deadline = time.monotonic() + timeout
        while True:
            if msvcrt.kbhit():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))
    r, _, _ = select.select([fd], [], [], timeout)
    return bool(r)


def _read_one_byte(fd: int) -> bytes:
    """Read one byte, taking from the pending-input buffer first."""
    global _pending_input
    if _pending_input:
        b, _pending_input = _pending_input[:1], _pending_input[1:]
        return b
    return os.read(fd, 1)


def _select_or_pending(fd: int, timeout: float) -> bool:
    """select-equivalent that also returns True when pending input is buffered."""
    if _pending_input:
        return True
    r, _, _ = select.select([fd], [], [], timeout)
    return bool(r)


def read_key(fd: int) -> bytes:
    """Block until one logical key is available and return it as bytes.

    A "logical key" is a complete unit: a single control byte, a full UTF-8
    character (all continuation bytes included), or a complete escape sequence
    (e.g. ``b"\\x1b[A"`` for the up arrow).  This lets callers compare against
    fixed byte patterns without re-reading the stream themselves.

    Bytes parked in ``_pending_input`` (typically by :func:`query_cursor_position`
    after a DSR exchange) are consumed before any new ``os.read``. To avoid
    re-ordering when SIGWINCH parks bytes *while* this function is blocked
    in ``os.read`` (Python retries via PEP 475 and the retry returns a
    byte read AFTER the parked bytes), we re-check ``_pending_input``
    after the initial read and prepend any parked bytes — those came from
    earlier in the input stream and must be returned first.
    """
    global _pending_input
    if IS_WINDOWS:
        if _pending_input:
            byte = _pending_input[:1]
            _pending_input = _pending_input[1:]
            return byte
        while not msvcrt.kbhit():
            time.sleep(0.005)
        return _win_read_key()

    had_pending_before = bool(_pending_input)
    data = _read_one_byte(fd)
    if not data:
        return data  # EOF
    # If SIGWINCH parked bytes *during* the blocking read above (i.e.,
    # ``_pending_input`` was empty before but is now populated), those
    # parked bytes are older than ``data`` — push ``data`` back and
    # return the parked head so the caller sees the input in arrival
    # order. We only do this when the buffer was empty before the read,
    # so we don't disrupt a multi-byte sequence we're already mid-way
    # through consuming from the buffer.
    if not had_pending_before and _pending_input:
        _pending_input = _pending_input + data
        data = _pending_input[:1]
        _pending_input = _pending_input[1:]
    first = data[0]
    if first == 0x1B:  # ESC — may begin an escape sequence
        # Read enough bytes to assemble exactly one escape sequence and
        # stop at its final byte. Without this, a queued second sequence
        # (e.g. two DSR replies arriving back-to-back as
        # ``ESC[13;25RESC[14;25R``) bleeds into the first read, and the
        # remainder leaks out as plain printable bytes that get inserted
        # into the buffer.
        #
        # Sequence shapes we care about:
        #   * CSI:  ESC [ <0..n params> <final-byte 0x40..0x7E>
        #   * SS3:  ESC O <single byte>
        #   * Plain ESC alone (no following byte within the timeout)
        #   * ESC <one byte> for Alt-keys (e.g. ESC b for Alt+B)
        if _select_or_pending(fd, 0.05):
            second = _read_one_byte(fd)
            if not second:
                return data
            data += second
            if second == b"[":
                # CSI: read until we see a final byte 0x40..0x7E.
                # Bound the read to a reasonable max (most CSI replies
                # are under 16 bytes; DSR is typically 8–10).
                for _ in range(64):
                    if not _select_or_pending(fd, 0.05):
                        break
                    more = _read_one_byte(fd)
                    if not more:
                        break
                    data += more
                    b = more[0]
                    if 0x40 <= b <= 0x7E:
                        break
            elif second == b"O":
                # SS3: exactly one more byte (e.g. ESC O A for up arrow
                # in application cursor mode).
                if _select_or_pending(fd, 0.05):
                    more = _read_one_byte(fd)
                    if more:
                        data += more
            # Else: ESC + one byte (Alt-key) — already captured.
        return data
    if first >= 0xC0:  # UTF-8 lead byte — pull in continuation bytes
        extra = 1 if first < 0xE0 else 2 if first < 0xF0 else 3
        for _ in range(extra):
            if not _select_or_pending(fd, 0.05):
                break
            more = _read_one_byte(fd)
            if not more:
                break
            data += more
    return data


# DSR (Device Status Report) reply format: ESC [ row ; col R
_DSR_REPLY_RE = re.compile(rb"\x1b\[(\d+);(\d+)R")


def query_cursor_position(fd: int, timeout: float = 0.2) -> tuple[int, int] | None:
    """Synchronously query the terminal for the cursor's absolute (row, col).

    Sends DSR (``ESC [ 6 n``) and waits for the reply ``ESC [ row ; col R``.
    Returns 1-indexed ``(row, col)``, or ``None`` if no reply arrives within
    *timeout* seconds (e.g. on a non-tty stdin or a terminal that doesn't
    speak DSR — such as our Windows path).

    Caller MUST hold the terminal in raw mode and own stdin so the reply
    isn't intercepted by another reader.

    Bytes that arrive *before* the reply (e.g. user keystrokes typed in
    the gap, or other escape sequences emitted by the terminal) are
    preserved by appending them to the module's pending-input buffer, so
    the next :func:`read_key` returns them in order. POSIX-only — the
    Windows console answers cursor-position queries through a different
    API that we don't need yet.
    """
    global _pending_input
    if IS_WINDOWS:
        return None
    sys.stdout.write("\033[6n")
    sys.stdout.flush()

    buf = b""
    deadline = time.monotonic() + timeout
    while True:
        match = _DSR_REPLY_RE.search(buf)
        if match:
            row, col = int(match.group(1)), int(match.group(2))
            # Preserve bytes around the reply (keystrokes, OSC chunks, …).
            leftover = buf[: match.start()] + buf[match.end():]
            if leftover:
                _pending_input += leftover
            return row, col
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # No reply — preserve whatever we received so it isn't lost.
            if buf:
                _pending_input += buf
            return None
        r, _, _ = select.select([fd], [], [], remaining)
        if not r:
            if buf:
                _pending_input += buf
            return None
        try:
            chunk = os.read(fd, 64)
        except OSError:
            if buf:
                _pending_input += buf
            return None
        if not chunk:
            if buf:
                _pending_input += buf
            return None
        buf += chunk


# ── resize handling ─────────────────────────────────────────────────────────


def install_resize_handler(handler):
    """Install a SIGWINCH handler, returning the previous one (or None).

    No-op on platforms without SIGWINCH; callers fall back to polling
    :func:`os.get_terminal_size` in that case.
    """
    if not HAS_SIGWINCH:
        return None
    old = signal.getsignal(signal.SIGWINCH)
    signal.signal(signal.SIGWINCH, handler)
    return old


def restore_resize_handler(old) -> None:
    if not HAS_SIGWINCH or old is None:
        return
    signal.signal(signal.SIGWINCH, old)


def terminal_size() -> tuple[int, int]:
    """Return (columns, lines), falling back to (80, 24)."""
    try:
        sz = os.get_terminal_size()
        return sz.columns, sz.lines
    except OSError:
        return 80, 24
