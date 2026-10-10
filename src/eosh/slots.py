"""Python commands on threads: the slot a command runs in, and the plumbing
that lets it share one terminal with the shell.

* :class:`_ThreadLocalStream` — ``sys.stdin`` / ``stdout`` / ``stderr``
  routed per thread (:func:`install_stdio_routers`), so a command's slot
  thread or a pipeline stage writes to its own target, never the terminal
  another thread owns.
* :class:`_StdoutProxy` — a slot's output, live while its context is in
  front and buffered while it isn't.
* :class:`PythonCommandSlot` — a Python command on a thread, with the same
  runtime interface as :class:`~eosh.process.ProcessSlot` so the shell can
  park and resume either; :class:`_PyStageHandle` — a Python pipeline stage.
* :func:`run_handler` — how a handler's end becomes an exit status, for
  every execution path.
* :func:`_run_interactive`, :func:`_read_from_user`, :func:`_choose` — what
  :class:`~eosh.command_context.CommandContext`'s methods do: reach the user
  through the slot's key stream when on one, the terminal otherwise.
"""

from __future__ import annotations

import codecs
import contextlib
import ctypes
import io
import os
import select
import signal
import struct
import subprocess
import sys
import threading
import time
import traceback
from typing import Callable

# The PTY behind ctx.run_interactive is POSIX-only; on Windows a Python
# command runs on the main thread and never builds a slot.
IS_WINDOWS = os.name == "nt"
if not IS_WINDOWS:
    import fcntl
    import pty
    import termios

from . import terminal
from .process import ExitCallbackMixin, OutputBuffer, ProcessSlot

# ---------------------------------------------------------------------------
# Thread-local stdout routing + per-slot buffering proxy
# ---------------------------------------------------------------------------

class _ThreadLocalStream(io.TextIOBase):
    """A ``sys.stdin`` / ``sys.stdout`` / ``sys.stderr`` replacement that
    routes I/O per thread.

    A thread with no override (the main thread) uses *real*.  A thread that
    sets one — a Python command's slot thread (a :class:`_StdoutProxy`), a
    pipeline stage (a TextIOWrapper around its pipe end or redirect file) —
    reads and writes there instead, so concurrent commands never trample
    each other or the terminal.  One class serves all three streams; each
    wraps its own *real*.
    """

    def __init__(self, real: io.TextIOBase) -> None:
        self._real = real
        self._local = threading.local()

    @property
    def _target(self) -> io.TextIOBase:
        return getattr(self._local, "override", None) or self._real

    def set_override(self, stream) -> None:
        self._local.override = stream

    def clear_override(self) -> None:
        self._local.override = None

    def close(self) -> None:
        # Never close (or flush on finalization) the stream we wrap: the
        # router is replaced and collected while the real stream lives on.
        pass

    # writing
    def write(self, s: str) -> int:
        return self._target.write(s)

    def flush(self) -> None:
        self._target.flush()

    # reading
    def read(self, size: int = -1) -> str:
        return self._target.read(size)

    def readline(self, size: int = -1) -> str:
        return self._target.readline(size)

    def readlines(self, hint: int = -1) -> list:
        return self._target.readlines(hint)

    def __iter__(self):
        return iter(self._target)

    def __next__(self):
        return next(self._target)

    # file-like plumbing
    def fileno(self) -> int:
        return self._real.fileno()

    @property
    def buffer(self):
        # The override's own binary layer when it has one (a pipe end); a
        # _StdoutProxy has none, so bytes from a slot thread reach the real
        # terminal, as the PTY passthrough reader relies on.
        return getattr(self._target, "buffer", None) or self._real.buffer

    @property
    def encoding(self) -> str:
        return getattr(self._real, "encoding", "utf-8")

    @property
    def errors(self) -> str:
        return getattr(self._real, "errors", "strict")

    def isatty(self) -> bool:
        # An override answers for itself: a pipe end is never a tty, and a
        # _StdoutProxy forwards to the real stream and says so.
        try:
            return self._target.isatty()
        except (AttributeError, ValueError):
            return False


class _StdoutProxy(io.TextIOBase):
    """Per-command buffering proxy.

    Starts inactive (buffering); ``activate()`` replays the buffer to the
    real stream and forwards subsequent writes directly.  ``deactivate()``
    resumes buffering (used while the context is in the background).

    No CRLF translation happens here: the main loop runs Python commands
    under raw-input/cooked-output mode (see
    :func:`terminal.set_raw_input_cooked_output`), so the kernel re-adds
    carriage returns to bare LFs at the tty boundary.
    """

    def __init__(self, real: io.TextIOBase) -> None:
        self._real = real
        self._buf = io.StringIO()
        self._active = False
        self._lock = threading.Lock()
        # Last char written through the proxy.  Used by the switch handler
        # to skip its protective newline when the cursor is already at
        # column 0 (last char ``\n`` / ``\r``).
        self._last_char: str = "\n"

    @property
    def encoding(self) -> str:
        return getattr(self._real, "encoding", "utf-8")

    @property
    def errors(self) -> str:
        return getattr(self._real, "errors", "strict")

    def write(self, s: str) -> int:
        with self._lock:
            if s:
                self._last_char = s[-1]
            if self._active:
                return self._real.write(s)
            return self._buf.write(s)

    def flush(self) -> None:
        with self._lock:
            if self._active:
                self._real.flush()

    def fileno(self) -> int:
        return self._real.fileno()

    def isatty(self) -> bool:
        # The proxy buffers and forwards to the real stdout.  Whether the
        # destination is a tty is what callers actually want to know
        # (e.g. ``@watch`` checks ``sys.stdout.isatty()`` to decide
        # whether emitting ANSI clear-screen escapes makes sense).
        return self._real.isatty()

    def activate(self) -> None:
        """Replay buffer to real stdout and start writing live.

        CRLF translation is done by the kernel via OPOST/ONLCR.
        """
        with self._lock:
            content = self._buf.getvalue()
            if content:
                self._real.write(content)
                self._real.flush()
                self._buf = io.StringIO()
            self._active = True

    def deactivate(self) -> None:
        with self._lock:
            self._active = False

    def replay(self) -> None:
        """Drain buffer to real stdout (called on switch-back, cooked mode)."""
        with self._lock:
            content = self._buf.getvalue()
            if content:
                self._real.write(content)
                self._real.flush()
                self._buf = io.StringIO()


class _NullBuffer:
    """Stub matching OutputBuffer.drain() used in the run() loop."""
    def drain(self) -> list:
        return []


class _PyStageHandle:
    """Lightweight handle for a Python-command stage running in a thread.

    Mirrors the attributes the pipeline driver in _execute_pipeline uses
    on subprocess.Popen (`wait()`, `returncode`-style exit code) so the
    two worker types can share one wait loop.
    """

    __slots__ = ("cmd_name", "thread", "done", "exit_code", "_io_objs", "interrupted")

    def __init__(self, cmd_name: str) -> None:
        self.cmd_name = cmd_name
        self.thread: threading.Thread | None = None
        self.done = threading.Event()
        self.exit_code: int | None = None
        # File objects whose underlying fds the worker thread reads/writes.
        # On interrupt() we close them so any blocked I/O raises and the
        # thread can unwind.
        self._io_objs: list = []
        # Set by interrupt() so the worker can tell "the parent killed me"
        # apart from a genuine error.  Any I/O exception that arrives after
        # this flag is set is expected (closed wrappers) and is silenced.
        self.interrupted: bool = False

    def wait(self) -> int:
        if self.thread is not None:
            # Poll so KeyboardInterrupt in the main thread can break out.
            while not self.done.wait(timeout=0.1):
                pass
        return self.exit_code or 0

    def interrupt(self) -> None:
        """Best-effort interruption of the worker thread.

        Python threads can't be cancelled, so we close the file objects
        the worker is reading from / writing to.  Any in-flight read or
        write raises, the worker's exception handler converts it to a
        normal exit, and the wait below returns promptly.

        A pure-CPU loop inside a Python command is still uninterruptible
        — flagged in doc/limitations.md.
        """
        self.interrupted = True
        for obj in self._io_objs:
            try:
                obj.close()
            except Exception:
                pass


def _exit_status(result) -> int:
    """A handler's return value as an exit status: an ``int`` is the status
    (a ``bool`` is not), anything else — usually ``None`` — is success."""
    if isinstance(result, int) and not isinstance(result, bool):
        return result
    return 0


def run_handler(
    fn: Callable[[], object],
    label: str,
    *,
    announce_interrupt: bool = False,
    interrupted: Callable[[], bool] | None = None,
) -> int:
    """Call *fn* — a Python command or a decorator — and turn
    how it ended into an exit status.  The one place that decides this; every
    execution path (foreground slot, pipeline stage, background slot, the
    main-thread path) goes through here.

    * a return value → :func:`_exit_status` of it;
    * ``SystemExit`` → its code (a string is printed, status 1) — it never
      takes the shell down;
    * ``KeyboardInterrupt`` → 130, saying ``<label>: interrupted`` when
      *announce_interrupt* (the main-thread path, where nobody else does);
    * ``BrokenPipeError`` → 0: the reader went away, as ``head`` makes it;
    * anything else → 1, with the error and traceback on stderr — unless
      *interrupted()* says the parent tore the stage down, in which case the
      I/O error is expected and the status is 130.
    """
    try:
        return _exit_status(fn())
    except SystemExit as e:
        code = e.code
        if isinstance(code, str):
            print(code, file=sys.stderr)
            return 1
        return code if isinstance(code, int) else (1 if code else 0)
    except KeyboardInterrupt:
        if announce_interrupt:
            print(f"{label}: interrupted")
        return 130
    except BrokenPipeError:
        return 0
    except Exception as e:
        if interrupted is not None and interrupted():
            return 130
        print(f"{label}: error: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1


_current_slot = threading.local()

# Set on threads spawned by _execute_pipeline for Python-command stages.
# The CommandContext methods that talk to the user check this and refuse,
# since stdin and stdout are wired to pipe ends, not the terminal.
_in_pipeline = threading.local()

# Set on a pipeline stage's thread when the pipeline runs on a
# PipelineSlot: a decorator body re-entering the executor there joins that
# slot (its job leader, its PTY) instead of taking the terminal.
_job_local = threading.local()


def _dup_threadlocal_override_fd(stream) -> int | None:
    """Duplicate the underlying pipe fd of a thread-local stdio override.

    *stream* is one of the ``sys.std{in,out,err}`` thread-local routers.
    When the calling thread has set an override (a TextIOWrapper around a
    pipe end), this returns a freshly ``os.dup()``-ed fd that the caller
    owns and must close — used by ``_execute_pipeline`` to wire a
    decorator body's first/last stage to the outer pipeline's fds without
    invalidating the parent thread's wrappers (so the body can be re-run
    on the next iteration of e.g. ``@watch``).

    Returns ``None`` when no override is set.  Quietly returns ``None``
    if the override has no fileno — the caller has nothing to wire.
    Raises ``BrokenPipeError`` when the override is closed: the stage was
    interrupted (``_PyStageHandle.interrupt``), and returning ``None``
    would hand the body the real terminal — ``@watch {…} | cat`` kept
    printing there after Ctrl+C.
    """
    override = getattr(getattr(stream, "_local", None), "override", None)
    if override is None:
        return None
    if getattr(override, "closed", False):
        raise BrokenPipeError("the pipeline stage was interrupted")
    try:
        fd = override.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    try:
        return os.dup(fd)
    except OSError:
        return None


def _refuse_in_pipeline(what: str) -> None:
    if getattr(_in_pipeline, "flag", False):
        raise RuntimeError(
            f"{what} cannot be used inside a piped Python command "
            f"(stdin/stdout are wired to pipes, not the terminal)"
        )


def _run_interactive(argv: list[str], **popen_kwargs) -> int:
    """``CommandContext.run_interactive``: run *argv* on a PTY the enclosing
    :class:`PythonCommandSlot` owns, so the main thread stays the only reader
    of the terminal — it forwards keys to the PTY master, still intercepts
    ``Ctrl+]``, and the child sees a full TTY on fd 0/1/2.

    On the main thread (a ``sync`` command, or Windows) there is no slot and
    nobody else is reading: plain ``subprocess.run``.
    """
    _refuse_in_pipeline("ctx.run_interactive")
    slot = getattr(_current_slot, "slot", None)
    if slot is None:
        return subprocess.run(argv, **popen_kwargs).returncode
    return slot._run_in_pty(argv, popen_kwargs)


def _stdin_is_tty() -> bool:
    """True when real stdin is a terminal — i.e. a key stream exists to read."""
    try:
        return os.isatty(sys.stdin.fileno())
    except (OSError, ValueError, AttributeError):
        return False


def _read_from_user(prompt: str, *, block: bool, what: str = "input") -> str:
    """``CommandContext.input`` / ``input_block``: read a line (or a pasted
    block, up to a blank line or Ctrl+D) off the raw key stream — the keys
    the forwarding loop feeds the slot, or the terminal itself on the main
    thread — never through cooked mode, whose line buffer is capped at
    ``MAX_CANON``.  See :func:`_read_typed` for the editing keys.

    Without a terminal (or on Windows) falls back to :func:`input`.
    """
    _refuse_in_pipeline(what)
    if _stdin_is_tty():
        slot = getattr(_current_slot, "slot", None)
        if slot is not None:
            return slot._read_typed(prompt, block=block)
        if not IS_WINDOWS:
            with _terminal_keys() as next_bytes:
                return _read_typed(next_bytes, prompt, block=block)
    if not block:
        return input(prompt)
    if prompt:
        sys.stdout.write(prompt)
        sys.stdout.flush()
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines)


def _choose(items: list[str], title: str) -> str | None:
    """``CommandContext.choose``: an :class:`~eosh.tui.InlinePicker` over
    *items*, reading keys from the slot (a backgrounded-able command) or the
    terminal (the main thread)."""
    _refuse_in_pipeline("ctx.choose")
    if not items:
        return None
    if not _stdin_is_tty():
        return _choose_by_number(items, title)
    from .tui import InlinePicker

    slot = getattr(_current_slot, "slot", None)
    key_source = None                    # main thread: the picker reads the terminal
    if slot is not None:
        with slot._keybuf_lock:          # typed before the question: not an answer
            slot._keybuf.clear()
            slot._keybuf_event.clear()
        key_source = slot.poll_key

    if title:
        print(title)
    picker = InlinePicker(items, key_source=key_source)
    if slot is not None:
        slot._reading_input = True       # Ctrl+C is the picker's, not a kill
    try:
        choice = picker.run()
    finally:
        if slot is not None:
            slot._reading_input = False
    if picker.interrupted:
        raise KeyboardInterrupt
    if choice is not None and title:
        # Replace the title line with a record of the answer.
        sys.stdout.write(f"\x1b[1A\r\x1b[K{title} {choice}\n")
        sys.stdout.flush()
    return choice


def _choose_by_number(items: list[str], title: str) -> str | None:
    """:func:`_choose` without a terminal: a numbered list and a number."""
    if title:
        print(title)
    for i, item in enumerate(items, 1):
        print(f"  {i}) {item}")
    answer = input("? ").strip()
    if answer.isdigit() and 1 <= int(answer) <= len(items):
        return items[int(answer) - 1]
    return None


@contextlib.contextmanager
def _terminal_keys():
    """The main thread's own key source: the terminal, in raw-input /
    cooked-output mode for the duration (so a typed Ctrl+C arrives as a
    key rather than a signal, and echoed newlines still get their CR)."""
    fd = sys.stdin.fileno()
    saved = terminal.get_mode(fd)
    terminal.set_raw_input_cooked_output(fd)
    try:
        yield lambda: os.read(fd, 1024) if terminal.wait_readable(fd, 0.2) else b""
    finally:
        terminal.restore_mode(fd, saved)


def _read_typed(next_bytes: Callable[[], bytes], prompt: str, *, block: bool) -> str:
    """Assemble typed or pasted text off a raw key stream, echoing it.

    *next_bytes* returns whatever keys have arrived (possibly ``b""`` after a
    short wait — the bounded wait is what lets an injected
    ``KeyboardInterrupt`` land between calls instead of blocking in C).

    One line when not *block*: Enter ends it, Ctrl+D on an empty line raises
    ``EOFError``.  Otherwise lines until a blank line or Ctrl+D.  Editing is
    Backspace, Ctrl+U (the line) and Ctrl+W (a word).  Ctrl+C echoes ``^C``
    and raises ``KeyboardInterrupt``.  Escape sequences — arrow keys, the
    terminal's bracketed-paste markers — are dropped, and a CRLF in pasted
    text is one line ending, not two.
    """
    from .lineedit import _wcswidth

    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    lines: list[str] = []
    current: list[str] = []
    in_escape = False
    after_cr = False

    def echo(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def erase(chars: list[str]) -> None:
        width = _wcswidth("".join(chars))
        if width:
            echo("\b" * width + " " * width + "\b" * width)

    if prompt:
        echo(prompt)

    while True:
        data = next_bytes()
        if not data:
            continue
        for ch in decoder.decode(data):
            if in_escape:
                if ch.isalpha() or ch == "~":
                    in_escape = False
                continue
            if ch == "\x1b":
                in_escape = True
                continue
            if ch == "\x03":                         # Ctrl+C
                echo("^C\n")
                raise KeyboardInterrupt
            if ch == "\x04":                         # Ctrl+D
                if not block:
                    if current:
                        continue                     # like a tty: only ends an empty line
                    echo("\n")
                    raise EOFError
                if current:
                    lines.append("".join(current))
                echo("\n")
                return "\n".join(lines)
            if ch in ("\r", "\n"):
                if ch == "\n" and after_cr:
                    continue
                after_cr = ch == "\r"
                echo("\n")
                line = "".join(current)
                current = []
                if not block:
                    return line
                if not line.strip():
                    return "\n".join(lines)
                lines.append(line)
                continue
            after_cr = False
            if ch in ("\x7f", "\x08"):              # Backspace
                if current:
                    erase([current.pop()])
                continue
            if ch == "\x15":                         # Ctrl+U — the whole line
                erase(current)
                current = []
                continue
            if ch == "\x17":                         # Ctrl+W — the word before the caret
                cut = len(current)
                while cut and current[cut - 1].isspace():
                    cut -= 1
                while cut and not current[cut - 1].isspace():
                    cut -= 1
                erase(current[cut:])
                del current[cut:]
                continue
            if ch < " " and ch != "\t":
                continue                             # other C0 controls
            current.append(ch)
            echo(ch)


class PythonCommandSlot(ExitCallbackMixin):
    """Manages a Python @registry.command running in a background thread.

    Implements the same runtime interface as ProcessSlot so the shell's
    run() loop and context machinery can treat both uniformly.
    """

    def __init__(self, cmd, raw_args: list[str], on_exit=None, ctx=None) -> None:
        self._init_exit_callback(on_exit)
        self._cmd = cmd
        self._raw_args = raw_args
        self._ctx = ctx   # the CommandContext a pass_context handler gets
        self.argv: list[str] = [cmd.name] + raw_args
        self._thread: threading.Thread | None = None
        self._proxy: _StdoutProxy | None = None
        self._err_proxy: _StdoutProxy | None = None
        self._finished = threading.Event()
        # Stub attributes expected by the run() loop
        self.buffer = _NullBuffer()
        self.exit_code: int | None = None
        # PTY state — created on demand by ctx.run_interactive().  When a
        # subprocess is running here, the main thread reads stdin and writes
        # to master_fd; a reader thread copies master_fd output to stdout.
        self._pty_master_fd: int = -1
        self._pty_subproc: subprocess.Popen | None = None
        self._pty_reader: threading.Thread | None = None
        self._pty_buffer = OutputBuffer()
        self._pty_last_byte: bytes = b"\n"
        self._pty_active = False
        self._pty_lock = threading.Lock()
        # True while the command is reading a line or block from the user
        # (ctx.input / input_block): the forwarding loop then hands
        # Ctrl+C to the reader instead of interrupting the command.
        self._reading_input = False
        # Raw stdin bytes the main forwarding loop received while no PTY
        # subprocess was active.  :meth:`poll_key` drains them — that is
        # how ``ctx.input`` / ``input_block`` read the user's keys.
        self._keybuf: bytearray = bytearray()
        self._keybuf_lock = threading.Lock()
        self._keybuf_event = threading.Event()

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Spawn the command thread.  stdout starts buffered (inactive)."""
        self.mark_started()
        real = getattr(sys.stdout, "_real", sys.stdout)
        self._proxy = _StdoutProxy(real)
        real_err = getattr(sys.stderr, "_real", sys.stderr)
        self._err_proxy = _StdoutProxy(real_err)
        self._thread = threading.Thread(
            target=self._run,
            name=f"pycmd-{self._cmd.name}",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        if hasattr(sys.stdout, "set_override"):
            sys.stdout.set_override(self._proxy)
        if hasattr(sys.stderr, "set_override"):
            sys.stderr.set_override(self._err_proxy)
        _current_slot.slot = self
        try:
            # Errors are reported here, on the slot's own stderr proxy, so
            # they land with the command's output — live or replayed later.
            self.exit_code = run_handler(
                lambda: self._cmd.invoke(self._raw_args, ctx=self._ctx), self._cmd.name)
        finally:
            _current_slot.slot = None
            if hasattr(sys.stdout, "clear_override"):
                sys.stdout.clear_override()
            if hasattr(sys.stderr, "clear_override"):
                sys.stderr.clear_override()
            if self.exit_code is None:
                self.exit_code = 130   # interrupted before run_handler returned
            self._finished.set()
            self._fire_on_exit()

    # --- ProcessSlot-compatible interface ------------------------------------

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def activate(self) -> None:
        # No newline translation: the forwarding loop keeps the kernel's
        # ONLCR on (raw input, cooked output), which adds the CRs.
        if self._proxy:
            self._proxy.activate()
        if self._err_proxy:
            self._err_proxy.activate()
        # Drain any PTY output that arrived while inactive, then resume
        # live forwarding from the reader thread.
        with self._pty_lock:
            if self._pty_master_fd >= 0:
                chunks = self._pty_buffer.drain()
                for chunk in chunks:
                    try:
                        sys.stdout.buffer.write(chunk)
                    except OSError:
                        pass
                try:
                    sys.stdout.buffer.flush()
                except OSError:
                    pass
                self._pty_active = True

    def deactivate(self) -> None:
        if self._proxy:
            self._proxy.deactivate()
        if self._err_proxy:
            self._err_proxy.deactivate()
        with self._pty_lock:
            if self._pty_master_fd >= 0:
                self._pty_active = False

    def replay_buffer(self) -> None:
        if self._proxy:
            self._proxy.replay()
        if self._err_proxy:
            self._err_proxy.replay()
        with self._pty_lock:
            if self._pty_master_fd >= 0:
                chunks = self._pty_buffer.drain()
                for chunk in chunks:
                    try:
                        sys.stdout.buffer.write(chunk)
                    except OSError:
                        pass
                try:
                    sys.stdout.buffer.flush()
                except OSError:
                    pass

    def kill(self) -> None:
        """Inject KeyboardInterrupt into the command thread."""
        if self._thread and self._thread.is_alive():
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(self._thread.ident),
                ctypes.py_object(KeyboardInterrupt),
            )

    def restore_terminal_modes(self) -> str:
        return ""

    def suspend_terminal_modes(self) -> str:
        return ""

    def cursor_at_col0(self) -> bool:
        """Heuristic: did the most recent output leave the cursor at column 0?

        ``True`` when either the buffering proxy or an active PTY reader's
        most recent byte was a line terminator.  The switch handler uses
        this to skip its protective newline (which would otherwise leave a
        blank line for the common case of a subprocess whose output ends in
        ``\\n``).
        """
        if self._pty_master_fd >= 0:
            return self._pty_last_byte in (b"\n", b"\r")
        if self._proxy is not None:
            return self._proxy._last_char in ("\n", "\r")
        return True

    def write_stdin(self, data: bytes) -> None:
        """Forward bytes from the main loop's stdin reader.

        Two destinations:

        * If a ``ctx.run_interactive`` subprocess is active, the bytes go to
          its PTY master so the child sees the user's typing.
        * Otherwise the bytes are buffered in ``_keybuf`` so a Python
          command body running on this slot can read them via
          :meth:`poll_key` (``ctx.input_block`` does).
        """
        with self._pty_lock:
            fd = self._pty_master_fd
        if fd >= 0:
            try:
                os.write(fd, data)
            except OSError:
                pass
            return
        with self._keybuf_lock:
            self._keybuf.extend(data)
            self._keybuf_event.set()

    def take_unread(self) -> bytes:
        """The keys forwarded to this slot that its command never read —
        typed ahead of the next prompt.  Empties the buffer."""
        with self._keybuf_lock:
            data = bytes(self._keybuf)
            self._keybuf.clear()
            self._keybuf_event.clear()
        return data

    def poll_key(self, timeout: float | None) -> bytes:
        """Return up to one buffered keystroke worth of stdin, or ``b\"\"``.

        Blocks up to *timeout* seconds (``None`` = forever) for a key.
        Returns an empty ``bytes`` if the timeout expires.  Returns the
        full byte sequence for one logical key (which may be a multi-byte
        escape sequence — the caller is expected to interpret it).

        Intended for Python command bodies that want to react to user
        keystrokes while the main forwarding loop holds stdin.  Bytes
        consumed here will not later reach a ``ctx.run_interactive``
        subprocess (none is active by the time this returns data).
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._keybuf_lock:
                if self._keybuf:
                    data = bytes(self._keybuf)
                    self._keybuf.clear()
                    self._keybuf_event.clear()
                    return data
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if remaining == 0.0:
                return b""
            if not self._keybuf_event.wait(timeout=remaining):
                return b""

    def resize(self, rows: int, cols: int) -> None:
        """Propagate a SIGWINCH-driven resize to a ctx.run_interactive() subprocess."""
        with self._pty_lock:
            fd = self._pty_master_fd
            pid = self._pty_subproc.pid if self._pty_subproc else -1
        if fd < 0:
            return
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass
        if pid > 0:
            try:
                os.killpg(os.getpgid(pid), signal.SIGWINCH)
            except (OSError, ProcessLookupError):
                pass

    def tail_lines(self, n: int) -> list[str]:
        """Return up to *n* most recent buffered output lines for preview."""
        from .process import _tail_lines_from_bytes
        text_data = b""
        if self._proxy is not None:
            with self._proxy._lock:
                text = self._proxy._buf.getvalue()
            if text:
                text_data = text.encode("utf-8", errors="replace")
        pty_data = self._pty_buffer.peek()
        return _tail_lines_from_bytes(text_data + pty_data, n)

    # --- ctx.run_interactive() implementation -----------------------------------

    def _run_in_pty(self, argv: list[str], popen_kwargs: dict) -> int:
        """Run *argv* on a slot-owned PTY; main thread forwards stdin via write_stdin."""
        # Snapshot whether the proxy was active (i.e. the user was watching
        # this slot in the foreground) before deactivating it for the
        # subprocess.  The PTY reader thread mirrors this state — if the
        # subprocess starts while the slot is backgrounded, the reader
        # buffers to ``_pty_buffer`` instead of writing to the real terminal.
        proxy_was_active = bool(self._proxy and self._proxy._active)
        # Pause the buffering stdout proxy: while the subprocess runs, its
        # output is written to the slot's PTY master and copied to stdout
        # by a reader thread.
        self._proxy.deactivate()
        if self._err_proxy:
            self._err_proxy.deactivate()
        master_fd, slave_fd = pty.openpty()
        try:
            rows, cols = ProcessSlot._get_real_terminal_size()
            if rows and cols:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

        env = popen_kwargs.pop("env", None) or dict(os.environ)
        # Tell the child the terminal it sees is a TTY.
        env.setdefault("TERM", os.environ.get("TERM", "xterm-256color"))

        def _make_session_leader():
            # Mirror ProcessSlot.start(): make the slave PTY the child's
            # controlling terminal.  Without TIOCSCTTY there is no foreground
            # process group on this PTY, so the slave line discipline's ISIG
            # silently eats control bytes we forward via the master
            # (e.g. \x03 from Ctrl+C during `aws ssm start-session`).  With a
            # controlling terminal the kernel delivers SIGINT to the child,
            # which well-behaved clients then forward to the remote session.
            os.setsid()
            try:
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            except OSError:
                pass

        try:
            proc = subprocess.Popen(
                argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=env,
                preexec_fn=_make_session_leader,
                **popen_kwargs,
            )
        finally:
            os.close(slave_fd)

        with self._pty_lock:
            self._pty_master_fd = master_fd
            self._pty_subproc = proc
            self._pty_active = proxy_was_active

        reader = threading.Thread(
            target=self._pty_reader_loop,
            args=(master_fd,),
            name=f"pycmd-pty-{self._cmd.name}",
            daemon=True,
        )
        with self._pty_lock:
            self._pty_reader = reader
        reader.start()

        try:
            proc.wait()
        finally:
            # Wait for reader to drain any remaining output, then tear down.
            reader.join(timeout=1.0)
            with self._pty_lock:
                self._pty_active = False
                self._pty_master_fd = -1
                self._pty_subproc = None
                self._pty_reader = None
            try:
                os.close(master_fd)
            except OSError:
                pass
            # Re-enable the buffering proxy for any remaining
            # prints from the Python command after the subprocess returns.
            self._proxy.activate()
            if self._err_proxy:
                self._err_proxy.activate()

        return proc.returncode if proc.returncode is not None else 1

    def _pty_reader_loop(self, master_fd: int) -> None:
        while True:
            try:
                r, _, _ = select.select([master_fd], [], [], 0.05)
                if not r:
                    if self._pty_subproc and self._pty_subproc.poll() is not None:
                        # Drain any final bytes before returning.
                        try:
                            r2, _, _ = select.select([master_fd], [], [], 0)
                            if not r2:
                                break
                        except OSError:
                            break
                    continue
                data = os.read(master_fd, 4096)
            except OSError:
                break
            if not data:
                break
            self._pty_buffer.append(data)
            self._pty_last_byte = data[-1:]
            if self._pty_active:
                try:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                except OSError:
                    pass

    # --- ctx.input() / input_block() -------------------------------------

    def _read_typed(self, prompt: str, *, block: bool) -> str:
        """Read a line (or a pasted block) off the keys the forwarding loop
        feeds this slot — see :func:`_read_typed`."""
        if not block:
            # Keys typed while the command was busy were not meant as the
            # answer to a question it hadn't asked yet ("y" to a delete
            # prompt).  A paste, by contrast, may land before the first poll.
            with self._keybuf_lock:
                self._keybuf.clear()
                self._keybuf_event.clear()
        self._reading_input = True
        try:
            return _read_typed(lambda: self.poll_key(0.2), prompt, block=block)
        finally:
            self._reading_input = False


def install_stdio_routers() -> None:
    """Replace ``sys.stdin`` / ``stdout`` / ``stderr`` with thread-local
    routers (once), so Python command threads can rebind their own streams —
    to a buffering proxy or a pipe end — without disturbing the main thread."""
    for stream in ("stdin", "stdout", "stderr"):
        if not isinstance(getattr(sys, stream), _ThreadLocalStream):
            setattr(sys, stream, _ThreadLocalStream(getattr(sys, stream)))
