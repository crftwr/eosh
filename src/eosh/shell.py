"""Main shell loop — input handling, command dispatch, completion integration."""

from __future__ import annotations

import codecs
import contextlib
import ctypes
import io
import os
import re
import select
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# PTY multiplexing and raw-mode forwarding are POSIX-only.  On Windows these
# modules are absent and the code paths that use them are never reached (the
# shell runs external commands on the real console — see _execute_external).
IS_WINDOWS = os.name == "nt"
if not IS_WINDOWS:
    import fcntl
    import pty
    import termios
    import tty

from . import terminal
from .commands import (
    _FLAG_PREFIXES, arg, Command, CommandRegistry,
    registry as command_registry,
)
from .completion import (
    CommandNameCompleter,
    CompletionContext,
    FileCompleter,
    Completion,
    get_argcomplete_fallback,
)
from .variables import EnvVar, registry as var_registry, VarCompleter
from .command_context import CommandContext, ShellView
from .context import ContextManager, ContextState
from .history import HistoryStore, norm_dir
from .lineedit import CONTEXT_CHANGED_SENTINEL, LineEditor
from .parsing import expand_vars, split_for_completion, tokenize
from .paths import config_dir
from .pipeline import (
    DecoratorParseError,
    Redirect,
    Sequence,
    Stage,
    Pipeline,
    expand_globs,
    parse_line,
    set_pipeline_executor,
    _split_on_operators,
)
from . import hooks, notify
from .process import ExitCallbackMixin, OutputBuffer, ProcessSlot
from .prompt import get_prompt_func, set_prompt

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
def _cooked_output():
    """Keep ``\n`` → ``\r\n`` on output for the duration, whatever mode the
    terminal is in (the line editor holds it raw while a picker runs)."""
    if not sys.stdin.isatty():
        yield
        return
    fd = sys.stdin.fileno()
    saved = terminal.get_mode(fd)
    terminal.set_raw_input_cooked_output(fd)
    try:
        yield
    finally:
        terminal.restore_mode(fd, saved)


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


_DEFAULT_CONFIG_PATH = Path(__file__).parent / "_config.py"


def _is_continuation(line: str) -> bool:
    """Return True if *line* ends with an unescaped backslash (line continuation).

    An even number of trailing backslashes means the last one is escaped (e.g.
    ``echo \\\\`` has two backslashes, none of which continue the line).
    Trailing spaces/tabs after the backslash are ignored so that accidental
    trailing whitespace does not prevent continuation from being recognised.
    """
    s = line.rstrip(" \t")
    count = 0
    for ch in reversed(s):
        if ch == "\\":
            count += 1
        else:
            break
    return count % 2 == 1


def _strip_continuation(line: str) -> str:
    """Remove the trailing continuation backslash (and any trailing whitespace before it).

    The result is ready to be concatenated with the next continuation line.
    Leading whitespace in the next line is preserved so indented continuations
    (the common style) work naturally::

        docker run --rm \\
          -v /foo:/bar      →  joined as  "docker run --rm   -v /foo:/bar"
    """
    return line.rstrip(" \t")[:-1]


def _positional_index(args: list[str], node: Command) -> int:
    """Return the number of positional (non-flag) arguments in *args*.

    Flags are skipped without counting: boolean flags advance by 1 token;
    *node*'s value-taking flags advance by 2 because they consume the
    following token as their value.
    """
    pos = 0
    i = 0
    while i < len(args):
        token = args[i]
        if token.startswith(_FLAG_PREFIXES):
            i += 2 if node.takes_value(token) else 1
        else:
            pos += 1
            i += 1
    return pos


@dataclass
class _Slot:
    """What one token of a command line is — the single answer that TAB
    completion and the status bar both act on.

    *kind* is one of:

    * ``"delegate"`` — the command's ``delegate`` completer answers every slot;
    * ``"flag"`` — a flag being typed, and the node has flags;
    * ``"value"`` — the value of *flag*, the value-taking flag just before it;
    * ``"subcommand"`` — the first positional of a group: a child's name;
    * ``"positional"`` — positional slot *pos_idx* of *node*;
    * ``"unknown"`` — no registered command (*node* is ``None``).

    *args* are the tokens after the resolved node, sub-command names removed.
    """
    node: Command | None
    args: list[str]
    kind: str
    flag: str = ""
    pos_idx: int = 0


def _resolve_slot(cmd: Command | None, args: list[str], token: str) -> _Slot:
    """Classify *token*, typed after *args*, against command *cmd*.

    A flat command is a node with no children, so the same walk covers both:
    resolve to the deepest sub-command, then look at that node's own params.
    """
    if cmd is None:
        return _Slot(None, args, "unknown", pos_idx=len(args))
    node, rest = cmd.resolve(args)
    if node.delegate is not None:
        return _Slot(node, rest, "delegate")
    if token.startswith(_FLAG_PREFIXES) and node.options_completer() is not None:
        return _Slot(node, rest, "flag")
    if rest and rest[-1].startswith(_FLAG_PREFIXES) and node.takes_value(rest[-1]):
        return _Slot(node, rest, "value", flag=rest[-1])
    pos_idx = _positional_index(rest, node)
    if node.children and pos_idx == 0:
        return _Slot(node, rest, "subcommand")
    return _Slot(node, rest, "positional", pos_idx=pos_idx)


def _flag_label(flag: str, arg_hint: str, description: str) -> str:
    """Consistent status-bar label for a flag, used across all call sites."""
    if arg_hint and description:
        return f"{flag} <{arg_hint}>: {description}"
    if description:
        return f"{flag}: {description}"
    if arg_hint:
        return f"{flag} <{arg_hint}>"
    return flag


def _label_from_arg(param) -> str:
    """Format an Arg descriptor as a status-bar label."""
    name = param.names[0]
    help_text = param.kwargs.get("help", "")
    if help_text:
        return f"{name}: {help_text}"
    return name


def _positional_label(cmd, pos_idx: int, command_name: str, args: list[str]) -> str:
    """Return a status-bar label for the positional argument at *pos_idx*.

    First consults the slot's completer via ``describe_slot(args, pos_idx)``
    so completers whose role depends on preceding args (e.g. tar's first
    positional, which flips between archive and member when ``-f`` is used)
    can override the static label.  Falls back to the ``help=`` text on the
    matching ``arg()`` descriptor — wildcard positionals reuse their label
    for every slot from their declared position onward.
    """
    if cmd is None or cmd.params is None:
        return f"arg {pos_idx + 1}"
    completer = cmd.positional_completer(pos_idx)
    if completer is not None:
        dynamic = completer.describe_slot(args, pos_idx)
        if dynamic is not None:
            return dynamic
    from .commands import _is_flag_name
    positionals = [a for a in cmd.params if a.names and not _is_flag_name(a.names[0])]
    if not positionals:
        return f"arg {pos_idx + 1}"
    if pos_idx < len(positionals):
        return _label_from_arg(positionals[pos_idx])
    # Beyond the declared list: if the last positional is a wildcard, reuse
    # its label for every subsequent slot.
    last = positionals[-1]
    if last.kwargs.get("nargs") in ("*", "+"):
        return _label_from_arg(last)
    return f"arg {pos_idx + 1}"


# ── source-bash: run a bash script, then import its environment ────────────
#
# The whole point of `source-bash` is the *import*: a child bash exits and
# takes its `export`s with it, so the wrapper below arranges for the child to
# dump its final environment and cwd where the parent can read them back.
#
# The dump is NUL-delimited (a value may contain newlines, but never a NUL)
# with the cwd as the first record, and it is written from an EXIT trap rather
# than a trailing line so a script that ends in `exit 1` — or dies under
# `set -e` — still hands its environment back.
_BASH_ENV_DUMP_WRAPPER = """\
__eosh_dump() {{ {{ printf '%s\\0' "$PWD"; env -0; }} > {dump} 2>/dev/null; }}
trap __eosh_dump EXIT
{body}
"""

# Bash bookkeeping that says nothing about the script's intent.  `_` holds the
# last argument of the last command, `SHLVL` counts nesting, `PWD`/`OLDPWD` are
# handled as the cwd instead, and `BASH_FUNC_*` are exported shell functions in
# an encoding only bash understands.
_BASH_ENV_SKIP = frozenset({
    "_", "PWD", "OLDPWD", "SHLVL", "BASHOPTS", "SHELLOPTS",
    "BASH_EXECUTION_STRING",
})
_BASH_ENV_SKIP_PREFIXES = ("BASH_FUNC_",)

# Only plain identifiers are considered for *removal*.  A key bash cannot bind
# to a variable (`foo-bar=1`, put in the environment by some other program) may
# be missing from the dump without the script having unset anything.
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _bash_env_ignored(key: str) -> bool:
    """True when *key* must not be imported back from a bash environment dump."""
    return key in _BASH_ENV_SKIP or key.startswith(_BASH_ENV_SKIP_PREFIXES)


def _parse_bash_env_dump(data: str) -> tuple[str | None, dict[str, str]]:
    """Split a NUL-delimited ``cwd\\0KEY=VALUE\\0…`` dump into (cwd, env).

    Returns ``(None, {})`` for an empty dump — which is how the caller learns
    the child never reached its EXIT trap (killed, or the script installed a
    trap of its own).
    """
    records = data.split("\0")
    if records and records[-1] == "":
        records.pop()
    if not records:
        return None, {}
    env: dict[str, str] = {}
    for record in records[1:]:
        key, sep, value = record.partition("=")
        if sep and key:
            env[key] = value
    return records[0], env


class Shell:
    def __init__(self):
        # Enable VT output / disable newline translation (Windows) before any
        # rendering or stdout wrapping happens.
        terminal.init()
        os.environ.setdefault("PWD", os.getcwd())
        self.registry = command_registry
        self.context_manager = ContextManager()
        self.context_manager.create("default")
        # True while the line currently being executed handed its work to a
        # background context — see _execute / _park.
        self._backgrounded = False
        # Set by the `exit` built-in; run() ends after the current line.
        self._exit_requested = False
        # Everything registered here is built-in: `reload` keeps it, and a
        # config needs override=True to replace it.
        with self.registry.defining_builtins(), var_registry.defining_builtins():
            self._register_builtins()
        self._load_user_config()
        # (context, cwd) as the hooks last saw it — see _notice_state_change.
        self._seen_state = self._observed_state()
        # The line _execute is running, for _park to hand to the slot.
        self._current_line: str | None = None
        # Install thread-local stdio routers so Python command threads can
        # rebind their own stdin/stdout/stderr (for buffering proxies or pipe
        # ends) without disturbing the main thread.
        for stream in ("stdin", "stdout", "stderr"):
            if not isinstance(getattr(sys, stream), _ThreadLocalStream):
                setattr(sys, stream, _ThreadLocalStream(getattr(sys, stream)))

        # The history every eosh process shares (SQLite — see history.py).
        self._history = HistoryStore(config_dir() / "history.db")
        # The history row of the line _execute is running, for _park.
        self._current_history_id: int | None = None
        # Seed the default context's per-session Up/Down list from it. New
        # contexts snapshot their parent's list at create time; Ctrl+R and
        # the ghost suggestion always query the shared store.
        default_ctx = self.context_manager.current()
        if default_ctx is not None:
            default_ctx.history = self._history.recent_commands()
        self._line_editor = LineEditor(
            history=self._history,
            get_completions=self._get_completions,
            get_prompt=lambda: get_prompt_func()(self.context_manager),
            switch_fn=self._handle_switch,
            get_arg_info=self._get_arg_info,
            local_history_fn=self._current_context_history,
            suggest_fn=self._suggest,
        )

        self._command_completer = CommandNameCompleter(self.registry)
        self._file_completer = FileCompleter()
        self._var_completer = VarCompleter()

        # Wire Pipeline.run() so decorator bodies can re-enter execution.
        set_pipeline_executor(self._run_pipeline_from_decorator)

    def _suggest(self, buf: str) -> str | None:
        """The ghost suggestion for *buf*: the latest line run in this
        directory — by any context, in any eosh process — that extends it."""
        try:
            cwd = os.getcwd()
        except OSError:
            return None
        return self._history.suggest(buf, cwd)

    def _record_history(self, line: str) -> int | None:
        """Add *line* to the shared history and the context's Up/Down list."""
        self._line_editor.add_to_history(line)
        return self._history.add(line, ctx=self.context_manager.current_name)

    def _current_context_history(self) -> list[str]:
        """Return the current context's per-session Up/Down history list.

        This is the actual mutable list on the Context, so the line editor
        both reads (Up/Down) and appends (on Enter) through it. Falls back to
        an ephemeral list if somehow there is no active context.
        """
        ctx = self.context_manager.current()
        if ctx is None:
            return []
        return ctx.history

    def _maybe_decorator_completion(
        self,
        tokens: list[str],
        prefix: str,
        line_before_cursor: str,
    ):
        """Handle TAB completion for ``@name [flags] <body>`` lines.

        Returns one of three things:

        * ``None`` — the line is not decorator-prefixed.
        * ``("return", (completions, prefix, label))`` — decorator-specific
          completions; caller returns this directly.
        * ``("fallthrough", (stripped_tokens, prefix, stripped_line))`` —
          cursor is in the body; caller continues with normal completion
          using the rewritten locals.
        """
        # Case A: ``@<partial>`` — completing the decorator name itself.
        if not tokens and prefix.startswith("@"):
            completions = [
                Completion(value=name, description=self.registry.get(name).description)
                for name in sorted(self.registry.list_commands())
                if name.startswith("@") and name.startswith(prefix)
            ]
            return "return", (completions, prefix, "decorator")

        # Case B/C: a decorator name has already been typed.
        if not tokens or not tokens[0].startswith("@"):
            return None
        deco = self.registry.get(tokens[0])
        if deco is None:
            return None  # unknown decorator; fall through (treats it like a command)

        # Walk past the decorator's flags to find where the body starts.
        body_start = len(tokens)
        i = 1
        while i < len(tokens):
            tok = tokens[i]
            if tok == "--":
                body_start = i + 1
                break
            if not tok.startswith(("-", "+")):
                body_start = i
                break
            i += 2 if ("=" not in tok and deco.takes_value(tok)) else 1

        # Case B: still in the decorator's flags (``@watch -<TAB>``,
        # ``@watch -n <TAB>``) — the decorator is a command, so its flags and
        # their values complete exactly as a command's do.
        if body_start >= len(tokens):
            slot = _resolve_slot(deco, tokens[1:], prefix)
            if slot.kind in ("flag", "value"):
                ctx = CompletionContext(
                    command=deco.name,
                    args=slot.args,
                    arg_index=len(slot.args),
                    prefix=prefix,
                    line=line_before_cursor,
                    shell_context=self._shell_view(),
                )
                flags = deco.options_completer()
                if slot.kind == "flag":
                    found = flags.complete(ctx) if flags.should_activate(ctx) else []
                    return "return", (found, prefix, f"{deco.name} option")
                flag, arg_hint, description, value_completer = flags.get_preceding_flag_hint(ctx)
                found = value_completer.complete(ctx) if value_completer else []
                return "return", (found, prefix, _flag_label(flag, arg_hint, description))

        # Case C: cursor is in the body.  Strip the decorator portion and
        # let normal completion run on what's left.  We rebuild the line
        # so downstream completers see the body as if typed at top level.
        stripped_tokens = tokens[body_start:]
        stripped_line = self._strip_leading_tokens(
            line_before_cursor, len(tokens) - len(stripped_tokens)
        )
        return "fallthrough", (stripped_tokens, prefix, stripped_line)

    @staticmethod
    def _strip_leading_tokens(line: str, n: int) -> str:
        """Drop the first *n* whitespace-separated tokens from *line*.

        Tokens that contain quotes are tracked correctly so a quoted
        decorator-flag value isn't double-counted.
        """
        i = 0
        ln = len(line)
        dropped = 0
        while dropped < n and i < ln:
            # Skip leading whitespace
            while i < ln and line[i] in (" ", "\t"):
                i += 1
            if i >= ln:
                break
            # Read one token
            while i < ln and line[i] not in (" ", "\t"):
                ch = line[i]
                if ch in ('"', "'"):
                    quote = ch
                    i += 1
                    while i < ln and line[i] != quote:
                        if line[i] == "\\" and quote == '"' and i + 1 < ln:
                            i += 2
                        else:
                            i += 1
                    i += 1
                    continue
                if ch == "\\" and i + 1 < ln:
                    i += 2
                    continue
                i += 1
            dropped += 1
        # Skip whitespace after the dropped tokens — preserve a single
        # space so split_for_completion still sees an "empty prefix" if the
        # cursor is mid-whitespace.
        while i < ln and line[i] in (" ", "\t"):
            i += 1
        return line[i:]

    def _get_completions(self, line_before_cursor: str) -> tuple[list[Completion], str, str]:
        # Isolate the current pipeline stage so completions for `ls | grep -`
        # are computed against `grep`, not `ls`.
        stage_line = _split_on_operators(line_before_cursor, [";", "&&", "||", "|"])[-1][1]
        tokens, prefix = split_for_completion(stage_line)

        # Decorator-prefixed stage (e.g. ``@watch -n 1 ls -l <TAB>``): handle
        # the ``@``-name and decorator-flag completions here, then strip the
        # decorator's tokens from the line and fall through so the wrapped
        # command's completer sees ``ls -l <TAB>``.
        deco_result = self._maybe_decorator_completion(tokens, prefix, line_before_cursor)
        if deco_result is not None:
            kind, payload = deco_result
            if kind == "return":
                return payload  # (completions, prefix, label)
            # kind == "fallthrough"
            tokens, prefix, line_before_cursor = payload

        # Expand the first token if it is an alias, so completions for
        # `hp <TAB>` come from the expansion's resolved command.
        if tokens:
            expansion = self.registry.get_alias(tokens[0])
            if expansion is not None:
                expansion_tokens = tokenize(expansion)
                if expansion_tokens:
                    tokens = expansion_tokens + tokens[1:]

        if not tokens:
            # Bare KEY=VALUE assignment (e.g. "aws_region=us-<TAB>"): delegate
            # to VarCompleter for value-side completion.
            if "=" in prefix:
                ctx = CompletionContext(
                    command=None,
                    args=[],
                    arg_index=0,
                    prefix=prefix,
                    line=line_before_cursor,
                    shell_context=self._shell_view(),
                )
                return self._var_completer.complete(ctx), prefix, "variable"

            ctx = CompletionContext(
                command=None,
                args=[],
                arg_index=0,
                prefix=prefix,
                line=line_before_cursor,
                shell_context=self._shell_view(),
            )
            return self._command_completer.complete(ctx), prefix, "command"

        command_name = tokens[0]
        args = tokens[1:]
        arg_index = len(args)

        ctx = CompletionContext(
            command=command_name,
            args=args,
            arg_index=arg_index,
            prefix=prefix,
            line=line_before_cursor,
            shell_context=self._shell_view(),
        )

        slot = _resolve_slot(self.registry.get(command_name), args, prefix)
        node = slot.node
        # Completers see the tokens after the resolved node — for a flat
        # command that is every argument.
        ctx = CompletionContext(
            command=command_name,
            args=slot.args,
            arg_index=len(slot.args),
            prefix=prefix,
            line=line_before_cursor,
            shell_context=self._shell_view(),
        )

        # A registered completer that returns [] means "nothing here"; only a
        # slot with *no* completer falls back to argcomplete and then files.
        has_completer = slot.kind != "positional" and slot.kind != "unknown"
        completions: list[Completion] = []
        label = command_name

        if slot.kind == "delegate":
            if node.delegate.should_activate(ctx):
                completions = node.delegate.complete(ctx)
        elif slot.kind == "flag":
            flags = node.options_completer()
            if flags.should_activate(ctx):
                completions = flags.complete(ctx)
            label = f"{command_name} option"
        elif slot.kind == "value":
            # The slot belongs to the flag's value: its value completer, or
            # nothing — never positional/file candidates.  With nothing to
            # offer, the status bar (``_get_arg_info``) says what to type.
            flag, arg_hint, description, value_completer = (
                node.options_completer().get_preceding_flag_hint(ctx))
            if value_completer is not None:
                completions = value_completer.complete(ctx)
            label = _flag_label(flag, arg_hint, description)
        elif slot.kind == "subcommand":
            completions = [
                Completion(value=name, description=node.children[name].description)
                for name in sorted(node.children) if name.startswith(prefix)
            ]
            label = f"{command_name} subcommand"
        elif slot.kind == "positional":
            positional = node.positional_completer(slot.pos_idx)
            if positional is not None:
                has_completer = True
                if positional.should_activate(ctx):
                    completions = positional.complete(ctx)
                label = _positional_label(node, slot.pos_idx, command_name, slot.args)

        # argcomplete fallback: the de-facto Python CLI completion library
        # (pipx, conda, pre-commit, tox, pdm, httpie, …).  Detection is done
        # by inspecting the script for the ``PYTHON_ARGCOMPLETE_OK`` marker,
        # so it never invokes side-effecting tools blindly.  (Cobra tools
        # have no fallback — they are opted in per command as a
        # ``delegate``; see ``eosh.recipes.enable_cobra``.)
        if not has_completer:
            argc = get_argcomplete_fallback()
            if argc is not None and argc.should_activate(ctx):
                completions = argc.complete(ctx)

        if not completions and not has_completer:
            completions = self._file_completer.complete(ctx)

        return completions, prefix, label

    def _get_arg_info(self, buf: str, cursor: int) -> str | None:
        """Return a status-bar description for the token the caret sits on.

        Handles three cases:
        - Flag token (``--flag``): returns ``"--flag: description"``
        - Flag value (token immediately after a value-taking flag): returns
          the flag's own description so the context stays visible
        - Positional arg: returns the param name (and help text when available)
        """
        # Extract the word surrounding cursor (scan left and right past non-space).
        start, end = cursor, cursor
        while start > 0 and buf[start - 1] not in (" ", "\t"):
            start -= 1
        while end < len(buf) and buf[end] not in (" ", "\t"):
            end += 1
        token = buf[start:end]

        # Parse everything before the token to get the command/args context.
        pre = buf[:start].rstrip()
        stage_pre = _split_on_operators(pre, [";", "&&", "||", "|"])[-1][1]
        tokens_before, _ = split_for_completion(stage_pre + " ")

        if not tokens_before:
            if not token:
                return None
            # Caret is on the command name itself — show its help text.
            cmd = self.registry.get(token)
            if cmd is not None and cmd.description:
                return f"{token}: {cmd.description}"
            # If the token is an alias, fall back to the alias's expansion
            # (and the description of whatever it resolves to).
            expansion = self.registry.get_alias(token)
            if expansion is not None:
                expansion_tokens = tokenize(expansion)
                if expansion_tokens:
                    target = self.registry.get(expansion_tokens[0])
                    if target is not None and target.description:
                        return f"{token} → {expansion}: {target.description}"
                return f"{token} → {expansion}"
            return None

        command_name = tokens_before[0]
        preceding_args = tokens_before[1:]

        # Expand the leading token if it is an alias, so the status bar for
        # `hp <args>` resolves against the alias's expansion (e.g.
        # `awsut sagemaker hyperpod`).  Mirrors the alias handling in
        # _get_completions.
        expansion = self.registry.get_alias(command_name)
        if expansion is not None:
            expansion_tokens = tokenize(expansion)
            if expansion_tokens:
                command_name = expansion_tokens[0]
                preceding_args = expansion_tokens[1:] + preceding_args

        slot = _resolve_slot(self.registry.get(command_name), preceding_args, token)
        node = slot.node
        if slot.kind in ("unknown", "delegate"):
            return None   # nothing declared to describe

        if token.startswith(_FLAG_PREFIXES) or slot.kind == "value":
            # The flag itself, or the value of the flag just before the caret.
            flags = node.options_completer()
            flag = slot.flag or token
            if flags is None:
                return None
            label = _flag_label(flag, flags.args.get(flag, ""), flags.options.get(flag, ""))
            return label if label != flag else None

        if slot.kind == "subcommand":
            # The child's description — or, for a partial name that doesn't
            # match a child yet, just say what the slot is.
            child = node.children.get(token)
            if child is None:
                return f"{command_name} subcommand"
            if child.description:
                return f"{command_name} {token}: {child.description}"
            return f"{command_name} {token}"

        return _positional_label(node, slot.pos_idx, command_name, slot.args)

    def _register_builtins(self) -> None:
        from .completion import (
            CallbackCompleter, ChoiceCompleter, Completer, Completion, DirCompleter,
        )

        @self.registry.command(
            name="cd",
            sync=True,
            help="Change directory.",
            params=[arg("path", nargs="?", default="~", completer=DirCompleter())],
        )
        def cd(path):
            target = os.path.expanduser(path)
            try:
                os.chdir(target)
                os.environ["PWD"] = os.getcwd()
            except OSError as e:
                print(f"cd: {e}")

        @self.registry.command(name="exit", help="Exit the shell.", sync=True)
        def exit_shell():
            # A pipeline stage is a subshell in POSIX terms: `exit | cat`
            # does not end the shell.
            if getattr(_in_pipeline, "flag", False):
                return
            running = self._running_contexts()
            if running and not self._confirm_exit(running):
                return 1
            # A request, not a SystemExit: a handler's SystemExit is just an
            # exit status (see run_handler).  run() ends after this line.
            self._exit_requested = True

        @self.registry.command(name="reload", help="Reload ~/.eosh/config.py.", sync=True)
        def reload_config():
            self._reload_config()

        config = self.registry.command(
            "config", help="Work with ~/.eosh/config.py.", sync=True)

        @config.command("edit", help="Open config.py in $VISUAL / $EDITOR, "
                                     "then reload it when the editor exits.")
        def config_edit():
            return self._edit_config()

        @self.registry.command(
            name="var",
            sync=True,
            help=(
                "Set, unset, or list context variables.\n\n"
                "  var              list all registered vars and env vars\n"
                "  var NAME         print current value of NAME\n"
                "  var NAME=VALUE   set NAME to VALUE\n"
                "  var NAME=        unset NAME (remove from env)\n\n"
                "NAME may be a registered Python-backed variable (e.g. 'aws_region')\n"
                "or a plain environment variable.  Registered variables handle their\n"
                "own set logic (e.g. writing multiple env keys at once)."
            ),
            params=[arg("assignments", nargs="*", metavar="NAME[=VALUE]",
                        completer=VarCompleter())],
        )
        def var_cmd(assignments):
            if not assignments:
                # List registered Python-backed vars first, then plain env.
                py_vars = var_registry.all()
                if py_vars:
                    print("[vars]")
                    for v in py_vars:
                        val = v.get()
                        val_str = val if val is not None else "(unset)"
                        desc = f"  # {v.description}" if v.description else ""
                        print(f"  {v.name}={val_str}{desc}")
                    print("[env]")
                for key, value in sorted(os.environ.items()):
                    print(f"  {key}={value}")
                return
            for assignment in assignments:
                if "=" in assignment:
                    key, _, value = assignment.partition("=")
                    if value == "":
                        self._unset_variable(key)
                    else:
                        self._set_variable(key, value)
                elif var_registry.get(assignment) is not None:
                    # 'var NAME' with no '=' → print current value of Python-backed var
                    v = var_registry.get(assignment)
                    val = v.get()
                    print(f"{assignment}={val}" if val is not None else f"{assignment}=(unset)")
                elif assignment in os.environ:
                    # 'var NAME' for a plain env var → print its value
                    print(f"{assignment}={os.environ[assignment]}")
                else:
                    print(f"var: invalid argument '{assignment}' (expected NAME=VALUE or NAME= to unset)")

        @self.registry.command(
            name="source-bash",
            sync=True,
            help=(
                "Run a bash script and import its environment into this shell.\n\n"
                "  source-bash                 paste lines, end with a blank line or Ctrl+D\n"
                "  source-bash FILE [ARG…]     source FILE with the given arguments\n"
                "  source-bash -c 'TEXT'       run TEXT\n\n"
                "The script runs in a real bash — so `export`, `$(…)`, loops,\n"
                "heredocs and conditionals all behave as they do in bash — and its\n"
                "final environment and working directory are imported back here,\n"
                "the way bash's own `source` leaves them in the calling shell.\n\n"
                "Shell functions, aliases and shell options cannot be imported\n"
                "(eosh has no equivalent); only variables and the cwd come back."
            ),
            params=[
                arg("script", nargs="*", metavar="FILE|ARG", completer=FileCompleter()),
                arg("-c", "--command", metavar="TEXT",
                    help="run TEXT instead of a file"),
                arg("--no-cd", action="store_true",
                    help="keep the current directory, whatever the script left"),
                arg("-q", "--quiet", action="store_true",
                    help="don't print the summary of imported variables"),
            ],
        )
        def source_bash_cmd(script, command, no_cd, quiet):
            if command is not None and script:
                print("source-bash: -c takes the whole script; don't pass a FILE too")
                return 2
            if command is not None:
                body = command
            elif script:
                path = os.path.expanduser(script[0])
                if not os.path.isfile(path):
                    print(f"source-bash: {script[0]}: no such file")
                    return 1
                body = " ".join(shlex.quote(a) for a in ["source", path, *script[1:]])
            else:
                body = _read_from_user(
                    "Paste bash lines; end with a blank line or Ctrl+D:\n",
                    block=True, what="source-bash",
                )
                if not body.strip():
                    print("source-bash: nothing to run")
                    return

            code, cwd, env = self._run_bash_script(body)
            if cwd is None:
                # No dump: the script replaced our EXIT trap, or bash died
                # before running it.  Importing an empty environment here
                # would unset everything the shell has.
                print("source-bash: environment not imported (script did not exit normally)")
                if code != 0:
                    print(f"source-bash: exit status {code}")
                return code or 1

            changed, removed, new_cwd = self._apply_bash_env(
                cwd, env, import_cwd=not no_cd
            )
            if code != 0:
                print(f"source-bash: exit status {code}")
            if quiet:
                return code
            # Names only, never values — a sourced script is exactly where an
            # AWS_SESSION_TOKEN comes from, and this line lands in the scrollback.
            parts = []
            if changed:
                parts.append(f"set: {', '.join(changed)}")
            if removed:
                parts.append(f"unset: {', '.join(removed)}")
            if new_cwd:
                parts.append(f"cwd: {new_cwd}")
            print(f"source-bash: {'; '.join(parts)}" if parts else "source-bash: no changes")
            # The script's own status is the command's — `source-bash x && …`.
            return code

        @self.registry.command(
            name="alias",
            sync=True,
            help=(
                "Define or list command aliases.\n\n"
                "  alias                  list all aliases\n"
                "  alias NAME             show the expansion of NAME\n"
                "  alias NAME=EXPANSION   define NAME as a shorthand for EXPANSION\n\n"
                "Aliases expand the first token of a command line.  Quote the\n"
                "expansion if it contains spaces:\n"
                "  alias hp='awsut sagemaker hyperpod'"
            ),
            params=[arg("assignments", nargs="*", metavar="NAME[=EXPANSION]")],
        )
        def alias_cmd(assignments):
            if not assignments:
                aliases = self.registry.list_aliases()
                if not aliases:
                    return
                for name in sorted(aliases):
                    print(f"alias {name}={aliases[name]!r}")
                return
            for assignment in assignments:
                if "=" in assignment:
                    name, _, expansion = assignment.partition("=")
                    if not name:
                        print(f"alias: invalid name in '{assignment}'")
                        continue
                    self.registry.alias(name, expansion)
                else:
                    expansion = self.registry.get_alias(assignment)
                    if expansion is None:
                        print(f"alias: {assignment}: not found")
                    else:
                        print(f"alias {assignment}={expansion!r}")

        @self.registry.command(
            name="unalias",
            sync=True,
            help="Remove one or more aliases.",
            params=[arg("names", nargs="+", metavar="NAME",
                        completer=CallbackCompleter(
                            lambda: sorted(self.registry.list_aliases())))],
        )
        def unalias_cmd(names):
            for name in names:
                if not self.registry.unalias(name):
                    print(f"unalias: {name}: not found")

        @self.registry.command(
            name="help",
            sync=True,
            help="Show help for a command, or list all commands.",
            params=[arg("command_name", nargs="?", default="",
                        completer=CallbackCompleter(lambda: sorted(self.registry.list_commands())))],
        )
        def help_cmd(command_name: str = ""):
            if command_name:
                cmd = self.registry.get(command_name)
                if cmd:
                    print(f"{cmd.name}: {cmd.help_text or 'No help available.'}")
                else:
                    print(f"Unknown command: {command_name}")
            else:
                names = sorted(self.registry.list_commands())
                builtin = [n for n in names if self.registry.is_builtin(n)]
                user = [n for n in names if not self.registry.is_builtin(n)]
                for title, group in (
                    ("Built-in commands:", [n for n in builtin if not n.startswith("@")]),
                    ("Commands from your config:", [n for n in user if not n.startswith("@")]),
                    ("Decorators (@name [flags] {pipeline}):", [n for n in builtin if n.startswith("@")]),
                    ("Decorators from your config:", [n for n in user if n.startswith("@")]),
                ):
                    if not group:
                        continue
                    print(title)
                    for name in group:
                        cmd = self.registry.get(name)
                        desc = cmd.help_text.split("\n")[0] if cmd.help_text else ""
                        print(f"  {name:20s} {desc}")

        @self.registry.command(
            name="history",
            sync=True,
            help=(
                "List past command lines (every context and eosh process).\n\n"
                "  history              the last 25\n"
                "  history docker run   the last 25 containing every keyword\n"
                "  history -n 100 --here"
            ),
            params=[
                arg("keywords", nargs="*", metavar="KEYWORD",
                    help="only lines containing all of these (case-insensitive)"),
                arg("-n", type=int, default=25, metavar="N", dest="limit",
                    help="how many (default 25)"),
                arg("--here", action="store_true",
                    help="only lines run in this directory"),
            ],
        )
        def history_cmd(keywords, limit, here):
            entries = self._history.entries(
                limit, keywords=keywords, cwd=os.getcwd() if here else None)
            home = norm_dir(os.path.expanduser("~"))
            for e in entries:
                when = time.strftime("%m-%d %H:%M", time.localtime(e.ts))
                status = "" if not e.status else str(e.status)
                where = e.cwd or ""
                if where == home or where.startswith(home + os.sep):
                    where = "~" + where[len(home):]
                print(f"{when}  {status:>3}  {where.replace(os.sep, '/')}  {e.cmd}")

        _names_after_subcommands = {"switch", "kill"}

        class ContextNameCompleter(Completer):
            def __init__(self, cm):
                self._cm = cm

            def should_activate(self, ctx: CompletionContext) -> bool:
                return bool(ctx.args) and ctx.args[0] in _names_after_subcommands

            def complete(self, ctx: CompletionContext) -> list[Completion]:
                subcmd = ctx.args[0] if ctx.args else ""
                names = self._cm.list_contexts()
                if subcmd == "kill":
                    names = [
                        n for n in names
                        if self._cm.contexts[n].process_slot
                        and self._cm.contexts[n].process_slot.is_alive()
                    ]
                return [
                    Completion(value=n)
                    for n in names
                    if n.startswith(ctx.prefix)
                ]

        @self.registry.command(
            name="context",
            sync=True,
            help="Manage shell contexts: new, close, switch, list, kill.",
            params=[
                arg("subcommand", nargs="?", default="",
                    completer=ChoiceCompleter(["new", "close", "switch", "list", "kill"])),
                arg("name", nargs="?", default="",
                    completer=ContextNameCompleter(self.context_manager)),
            ],
        )
        def context_cmd(subcommand: str = "", name: str = ""):
            if not subcommand:
                ctx = self.context_manager.current()
                if ctx:
                    vars_str = f" {ctx.variables}" if ctx.variables else ""
                    print(f"Current: {ctx.name}{vars_str}")
                else:
                    print("No active context.")
                return

            if subcommand == "new":
                if not name:
                    print("Usage: context new <name>")
                    return
                if name in self.context_manager.contexts:
                    print(f"Context '{name}' already exists.")
                    return
                self.context_manager.new(name)
                self.context_manager.switch(name)
                print(f"Created context '{name}'")

            elif subcommand == "close":
                # The current context unless one is named; the same rules as
                # Ctrl+D in the Ctrl+] picker.
                target = name or self.context_manager.current_name
                if target not in self.context_manager.contexts:
                    print(f"No context named '{target}'")
                    return
                if len(self.context_manager.list_contexts()) <= 1:
                    print("Cannot close the last context.")
                    return
                slot = self.context_manager.contexts[target].process_slot
                if slot is not None and slot.is_alive():
                    print(f"Context '{target}' has a running process "
                          f"(context kill {target} first).")
                    return
                self.context_manager.remove(target)
                print(f"Closed '{target}', now in '{self.context_manager.current_name}'")

            elif subcommand == "switch":
                if not name:
                    print("Usage: context switch <name>")
                    return
                try:
                    self.context_manager.switch(name)
                except KeyError as e:
                    print(e)

            elif subcommand == "list":
                names = self.context_manager.list_contexts()
                if not names:
                    print("No contexts defined.")
                else:
                    current = self.context_manager.current_name
                    ordered = ([current] if current else []) + [n for n in names if n != current]
                    for n in ordered:
                        marker = "*" if n == current else " "
                        ctx = self.context_manager.contexts[n]
                        state = ctx.state.name.lower()
                        if state == "idle":
                            state_str = ""
                        elif state == "running" and ctx.process_slot and ctx.process_slot.argv:
                            cmd = " ".join(ctx.process_slot.argv)
                            state_str = f" (running: {cmd})"
                        else:
                            state_str = f" ({state})"
                        vars_str = f" {ctx.variables}" if ctx.variables else ""
                        print(f"  {marker} {n}{state_str}{vars_str}")

            elif subcommand == "kill":
                if not name:
                    print("Usage: context kill <name>")
                    return
                if name not in self.context_manager.contexts:
                    print(f"No context named '{name}'")
                    return
                target_ctx = self.context_manager.contexts[name]
                if target_ctx.process_slot and target_ctx.process_slot.is_alive():
                    target_ctx.process_slot.kill()
                    print(f"Sent SIGTERM to process in context '{name}'")
                else:
                    print(f"Context '{name}' has no running process.")

            else:
                print(f"Unknown subcommand: {subcommand}")

        # Built-in pipeline decorators — commands named `@watch`, `@time`, …
        from .decorators import register_builtins as register_builtin_decorators
        register_builtin_decorators()

        # `var notify=off` / `var notify_threshold=30`.
        notify.register_vars()

    def _reload_config(self) -> None:
        """``reload``: undo everything the config registered, then run it again."""
        self._clear_user_config()
        self._load_user_config()
        print("Config reloaded.")

    def _clear_user_config(self) -> None:
        """Put every registry a config writes to back to its built-in state.

        The one sweep ``reload`` runs, so nothing a config registered
        survives once the config stops registering it — including a
        built-in it replaced with ``override=True``, which comes back.
        """
        from . import recipes
        self.registry.clear_user_commands()   # commands, decorators, aliases
        var_registry.clear_user_vars()
        recipes.skipped_recipes.clear()
        set_prompt(None)
        notify.reset_config()
        hooks.clear()

    def _edit_config(self) -> int:
        """``config edit``: run the user's editor on config.py, then reload."""
        path = config_dir() / "config.py"
        editor = (os.environ.get("VISUAL") or os.environ.get("EDITOR")
                  or ("notepad" if IS_WINDOWS else "vi"))
        argv = shlex.split(editor) + [str(path)]
        try:
            code = _run_interactive(argv)
        except FileNotFoundError:
            print(f"config edit: editor not found: {argv[0]}", file=sys.stderr)
            return 127
        if code != 0:
            print(f"config edit: {argv[0]} exited with status {code}; "
                  f"not reloading", file=sys.stderr)
            return code
        self._reload_config()
        return 0

    def _load_user_config(self) -> None:
        config_path = config_dir() / "config.py"
        if not config_path.exists():
            # First launch: write the starter config, then load it like any
            # other — a fresh install should not need a restart to get recipes.
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(_DEFAULT_CONFIG_PATH.read_text())

        import importlib
        import importlib.util

        # ~/.eosh is on sys.path while config.py runs, as a script's own
        # directory is: your own recipes and decorators live in modules there
        # (`import my_tools`), or anywhere config.py adds to sys.path.
        home = config_path.parent.resolve()
        if str(home) not in sys.path:
            sys.path.insert(0, str(home))
        # A module imported from there on an earlier load is forgotten, so
        # `reload` runs it again — `reload` just cleared what it registered.
        for name, mod in list(sys.modules.items()):
            origin = getattr(mod, "__file__", None)
            if origin and Path(origin).resolve().is_relative_to(home):
                del sys.modules[name]
        importlib.invalidate_caches()

        sys.modules.pop("eosh_user_config", None)
        spec = importlib.util.spec_from_file_location("eosh_user_config", config_path)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            sys.modules["eosh_user_config"] = module
            try:
                spec.loader.exec_module(module)
            except KeyboardInterrupt:
                # VSCode and similar terminal integrations may inject a Ctrl+C
                # right after opening a terminal (to clear any in-progress input
                # before auto-activating a venv). If that lands during config
                # load — typically inside a slow import like boto3 — exit
                # cleanly so the user can re-run eosh once the terminal has
                # finished its startup dance, instead of crashing with a
                # traceback or starting up half-configured.
                print("Config load interrupted by Ctrl+C; exiting.", file=sys.stderr)
                sys.exit(130)
            except Exception as e:
                from .user_errors import format_user_exception
                print(f"Error loading config ({config_path}):", file=sys.stderr)
                print(format_user_exception(e), file=sys.stderr, end="")

    _ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)")

    def _split_env_prefix(self, tokens: list[str]) -> tuple[dict[str, str], list[str]]:
        """Split leading ``KEY=VALUE`` tokens into a per-command env prefix.

        Supports the POSIX idiom ``FOO=bar BAZ=qux cmd args`` — the assignments
        apply only to *cmd*'s environment, not the shell's.  Scanning stops at
        the first non-assignment token (the command name); everything after it
        is left untouched, so ``make FOO=bar`` keeps ``FOO=bar`` as an argument
        to ``make`` (a Makefile override) rather than an env prefix.

        Returns ``(env_prefix, rest)``.  When there is no command after the
        assignments (a pure-assignment line) ``rest`` is empty and the caller
        keeps the existing permanent-assignment behaviour.
        """
        env_prefix: dict[str, str] = {}
        idx = 0
        for token in tokens:
            m = self._ASSIGNMENT_RE.match(token)
            if m is None:
                break
            env_prefix[m.group(1)] = m.group(2)
            idx += 1
        rest = tokens[idx:]
        # No trailing command → not a prefix; let pure-assignment handling run.
        if not rest:
            return {}, tokens
        return env_prefix, rest

    @staticmethod
    def _merged_env(env_prefix: dict[str, str]) -> dict[str, str]:
        """Return a copy of ``os.environ`` overlaid with *env_prefix*."""
        env = dict(os.environ)
        env.update(env_prefix)
        return env

    @staticmethod
    def _env_prefix_refused(command_name: str, env_prefix: dict[str, str]) -> int:
        """A Python command can't take ``FOO=bar cmd``: it runs in the shell's
        own process, where the only environment is ``os.environ`` — shared by
        every thread, so a temporary change would leak into the other stages
        of a pipeline and outlive a backgrounded run.  Say so (status 2)."""
        names = " ".join(f"{k}=…" for k in env_prefix)
        print(f"eosh: {names} {command_name}: an environment prefix only applies to "
              f"external commands; set it with `var` for a Python command",
              file=sys.stderr)
        return 2

    def _env_keys_for(self, key: str) -> tuple[str, ...] | None:
        """The ``os.environ`` keys an assignment to *key* writes — an
        :class:`EnvVar`'s keys, *key* itself for a plain name — or ``None``
        for a PyVar / GlobalVar, whose value lives on the Python side."""
        var = var_registry.get(key)
        if var is None:
            return (key,)
        return var.keys if isinstance(var, EnvVar) else None

    def _set_variable(self, key: str, value: str) -> None:
        """``KEY=VALUE`` — every environment write goes through the context
        manager (so it is saved and restored per context); a PyVar or
        GlobalVar sets itself (and the context manager saves a PyVar's value
        when the context is left)."""
        keys = self._env_keys_for(key)
        if keys is None:
            var_registry.get(key).set(value)
            return
        for env_key in keys:
            self.context_manager.set_variable(env_key, value)

    def _unset_variable(self, key: str) -> None:
        """``KEY=`` — the mirror of :meth:`_set_variable`."""
        keys = self._env_keys_for(key)
        if keys is None:
            var_registry.get(key).unset()
            return
        for env_key in keys:
            self.context_manager.unset_variable(env_key)

    def _run_bash_script(self, body: str) -> tuple[int, str | None, dict[str, str]]:
        """Run *body* in a child bash and read back its final cwd + environment.

        Returns ``(exit_code, cwd, env)``.  ``cwd`` is None (and ``env`` empty)
        when the child never reached the dump — the caller reports that rather
        than importing an empty environment over the live one.

        The child runs through :func:`_run_interactive` so a script that prompts
        (``sudo``, an MFA code, ``read -p``) still owns the terminal, and the
        dump goes to a temp *file* rather than an extra fd so nothing has to be
        multiplexed alongside the script's own stdout.
        """
        bash = shutil.which("bash")
        if bash is None:
            print("source-bash: no 'bash' on PATH")
            return 127, None, {}

        fd, dump_path = tempfile.mkstemp(prefix="eosh-env-")
        os.close(fd)
        try:
            script = _BASH_ENV_DUMP_WRAPPER.format(
                dump=shlex.quote(dump_path), body=body
            )
            code = _run_interactive([bash, "-c", script])
            try:
                data = Path(dump_path).read_text(errors="replace")
            except OSError:
                data = ""
        finally:
            with contextlib.suppress(OSError):
                os.unlink(dump_path)

        cwd, env = _parse_bash_env_dump(data)
        return code, cwd, env

    def _apply_bash_env(
        self, cwd: str | None, env: dict[str, str], *, import_cwd: bool = True
    ) -> tuple[list[str], list[str], str | None]:
        """Import a bash dump into this shell: set, unset, and chdir.

        Assignments go through :meth:`_set_variable` / :meth:`_unset_variable`
        so a Var-backed name and the current context's save/restore table see
        the change — an imported variable is indistinguishable from one set
        with ``var NAME=VALUE``.

        Returns ``(set_names, unset_names, new_cwd)`` for the caller's summary;
        ``new_cwd`` is None when the directory did not change.
        """
        changed: list[str] = []
        for key, value in env.items():
            if _bash_env_ignored(key):
                continue
            if os.environ.get(key) != value:
                self._set_variable(key, value)
                changed.append(key)

        removed: list[str] = []
        for key in list(os.environ):
            if key in env or _bash_env_ignored(key) or not _ENV_NAME_RE.match(key):
                continue
            self._unset_variable(key)
            removed.append(key)

        new_cwd = None
        if import_cwd and cwd and os.path.realpath(cwd) != os.path.realpath(os.getcwd()):
            try:
                os.chdir(cwd)
            except OSError as e:
                print(f"source-bash: cannot enter {cwd}: {e}")
            else:
                os.environ["PWD"] = os.getcwd()
                new_cwd = os.getcwd()

        return sorted(changed), sorted(removed), new_cwd

    def _execute(self, line: str, history_id: int | None = None) -> None:
        try:
            seq = parse_line(expand_vars(line))
        except DecoratorParseError as e:
            print(f"eosh: {e}", file=sys.stderr)
            return
        last_exit = 0
        started = time.monotonic()
        # Cleared per line; set by whichever path hands the work to a
        # background context, so the notification comes from the slot's own
        # exit callback instead of from this (premature) return.
        self._backgrounded = False
        self._current_line = line
        self._current_history_id = history_id
        hooks.fire("on_command_starting", line)
        try:
            for op, pipeline in seq.items:
                if op == "&&" and last_exit != 0:
                    continue
                if op == "||" and last_exit == 0:
                    continue
                last_exit = self._execute_pipeline(pipeline)
                # `cd proj && make`: the move is reported before make runs.
                self._notice_state_change()
        finally:
            self._current_line = None
            self._current_history_id = None
            if not self._backgrounded:
                elapsed = time.monotonic() - started
                self._history.finish(history_id, last_exit, elapsed)
                notify.command_done(line, elapsed, last_exit)
                hooks.fire("on_command_finished", line, last_exit, elapsed)
            # User-run commands may have mutated remote state (e.g.
            # ``awsut sagemaker hyperpod scale``); drop cached completer
            # fetches so the next TAB session re-queries.
            from . import completion_cache
            completion_cache.invalidate_all()

    # --- long-command notifications ------------------------------------------

    def _park(self, slot, ctx) -> None:
        """Hand *slot* to *ctx* to keep running in the background (Ctrl+]).  From here on the slot's exit handler reports it, so the
        line that started it doesn't (see :meth:`_execute`)."""
        ctx.process_slot = slot
        slot.parked = True
        slot.line = self._current_line
        slot.history_id = self._current_history_id
        self._backgrounded = True

    def _slot_finished(self, slot) -> None:
        """Exit handler every slot is constructed with; runs on the slot's
        own thread, so it must not touch the terminal —
        :func:`notify.command_done` only spawns a notification helper.

        A slot that was never parked ran in the foreground, and
        :meth:`_execute` timed the whole line.  A parked one is reported
        here, with its context's name when that context isn't the current
        one — the user was looking elsewhere when it ended.  The owner is
        looked up, not remembered: a slot can move between contexts.
        """
        if not slot.parked:
            return
        owner = next(
            (name for name, ctx in self.context_manager.contexts.items()
             if ctx.process_slot is slot),
            None,
        )
        out_of_sight = owner is not None and owner != self.context_manager.current_name
        notify.command_done(" ".join(slot.argv), slot.elapsed(), slot.exit_code or 0,
                            context=owner if out_of_sight else None)
        self._history.finish(slot.history_id, slot.exit_code or 0, slot.elapsed())
        hooks.fire("on_command_finished", slot.line or " ".join(slot.argv),
                   slot.exit_code or 0, slot.elapsed())

    # --- what user code sees ---------------------------------------------------

    def _shell_view(self) -> ShellView:
        """The current context, read-only — ``CompletionContext.shell_context``."""
        return ShellView(self.context_manager, self.context_manager.current())

    def _command_context(self) -> CommandContext:
        """For a ``pass_context`` handler: bound to the context the command
        starts in, which it keeps if it is sent to the background."""
        return CommandContext(self.context_manager, self.context_manager.current(), self)

    # --- hooks ----------------------------------------------------------------

    def _observed_state(self) -> tuple[str | None, str | None]:
        try:
            cwd = os.getcwd()
        except OSError:          # the directory was removed under us
            cwd = None
        return self.context_manager.current_name, cwd

    def _notice_state_change(self) -> None:
        """Fire on_context_switched / on_directory_changed for whatever
        changed since the last look.

        Comparing state rather than hooking each mutation catches every way
        it can change — ``cd``, a context switch restoring its cwd,
        ``source-bash``, ``os.chdir`` in a Python command — from a handful of
        call sites: after each command of a line, after a context switch,
        and before each prompt.
        """
        old_name, old_cwd = self._seen_state
        name, cwd = self._seen_state = self._observed_state()
        if name != old_name:
            hooks.fire("on_context_switched", old_name, name)
        if cwd != old_cwd and cwd is not None:
            hooks.fire("on_directory_changed", old_cwd, cwd)

    def _tokenize_stage(self, stage: Stage) -> list[str]:
        """Expand variables, tokenize, alias-expand, and glob-expand a stage's text."""
        tokens = tokenize(stage.text + " ")
        tokens = [os.path.expanduser(t) for t in tokens]
        tokens = self._expand_alias(tokens)
        return expand_globs(tokens)

    def _expand_alias(self, tokens: list[str]) -> list[str]:
        """Replace the first token with its alias expansion, if any.

        Aliases never chain — the expansion's own first token is not
        re-expanded — so cycles are impossible.
        """
        if not tokens:
            return tokens
        expansion = self.registry.get_alias(tokens[0])
        if expansion is None:
            return tokens
        expansion_tokens = tokenize(expansion)
        if not expansion_tokens:
            return tokens
        return expansion_tokens + tokens[1:]

    def _run_pipeline_from_decorator(
        self, pipeline: Pipeline, *, stdin=None, stdout=None, stderr=None
    ) -> int:
        """Entry point used by ``Pipeline.run()`` from a decorator body.

        The MVP rejects explicit ``stdin``/``stdout``/``stderr`` overrides;
        the body inherits stdio from the decorator's caller via the
        thread-local routers.

        When the decorator itself is a stage of an outer pipeline
        (composition: ``@deco {body} | next``), the body's stdout must
        feed into the outer pipe — not the real terminal.  We detect that
        case via ``_in_pipeline.flag`` and pipe the body's last stage to
        the thread's rebound ``sys.stdout`` (which is the outer pipe's
        write end).  Without this, a body like ``@watch {ls}`` running
        under ``@watch {ls} | grep py`` would route through the
        standalone-command path (``ProcessSlot`` / ``PythonCommandSlot``)
        and grab the real terminal — wrong from a worker thread.
        """
        if any(x is not None for x in (stdin, stdout, stderr)):
            raise NotImplementedError(
                "Pipeline.run(stdin=, stdout=, stderr=) is not supported yet; "
                "the decorator body inherits stdio from the decorator's caller."
            )
        in_outer_pipe = getattr(_in_pipeline, "flag", False)
        return self._execute_pipeline(pipeline, _in_outer_pipe=in_outer_pipe)

    def _execute_pipeline(
        self,
        pipeline: Pipeline,
        *,
        _in_outer_pipe: bool = False,
    ) -> int:
        """Execute a pipeline; return exit code of last stage.

        ``_in_outer_pipe`` is set when this call is the body of a
        decorator that is itself a stage of an outer pipeline
        (``@deco {body} | next``).  In that case the body's last stage
        must write into the thread's rebound ``sys.stdout`` (the outer
        pipe's write end) rather than the real terminal — so we force
        every stage onto the worker-thread / Popen path even when the
        body has only one stage (where ``_execute_stage`` would
        otherwise grab a real PTY).
        """
        stages = pipeline.stages
        single = stages[0] if len(stages) == 1 else None
        # A lone stage gets the terminal (PTY / PythonCommandSlot) unless it
        # has redirects.  A redirected stage is a one-stage pipeline: the
        # loop below already binds a Python stage's stdio through the
        # thread-local routers and an external one through Popen, so there
        # is no second redirect path that swaps the process-global
        # ``sys.stdout`` under every other thread.  Decorator stages keep
        # the direct path — their redirects live inside the braced body.
        if (
            single is not None
            and not _in_outer_pipe
            and (not single.redirects or single.decorator is not None)
        ):
            return self._execute_stage(single)

        # Multi-stage pipeline, redirected single stage, or single-stage
        # decorator body running under an outer pipe: connect with OS
        # pipes.  External stages run via subprocess.Popen; registered
        # Python commands run in worker threads that rebind
        # sys.stdin/stdout/stderr to the pipe ends (or redirect files) via
        # the thread-local routers installed in __init__.

        n = len(stages)
        pipe_fds: list[tuple[int, int]] = []
        for _ in range(n - 1):
            pipe_fds.append(os.pipe())

        # When running under an outer pipe, dup the boundary fds from the
        # thread-local stdio overrides so the body's first stage reads
        # from whatever feeds the decorator and the last stage writes to
        # whatever follows it.  We dup so that the worker (Popen or
        # Python-stage thread) takes ownership of its own fd copy and the
        # parent thread's wrappers stay open for subsequent iterations
        # (e.g. ``@watch {ls} | grep py`` re-runs the body each tick).
        outer_in_fd = _dup_threadlocal_override_fd(sys.stdin) if _in_outer_pipe else None
        outer_out_fd = _dup_threadlocal_override_fd(sys.stdout) if _in_outer_pipe else None

        # Workers list contains either subprocess.Popen instances or
        # _PyStageHandle objects (see _start_stage_thread).
        workers: list = []
        for idx, stage in enumerate(stages):
            stdin_fd_pipe = pipe_fds[idx - 1][0] if idx > 0 else outer_in_fd
            stdout_fd_pipe = pipe_fds[idx][1] if idx < n - 1 else outer_out_fd

            # Decorator stage (e.g. ``@watch {ls} | grep foo``).  The
            # decorator body inherits the thread's rebound stdio via the
            # thread-local routers, so its writes flow through the pipe
            # to the next stage just like a Python command would.
            if stage.decorator is not None:
                # Decorator stages don't currently honour explicit redirects
                # on the stage itself — redirects belong inside the braced
                # body where the user can scope them precisely.  An MVP-level
                # warning would be noise; just ignore them silently.
                call = stage.decorator
                worker = self._start_stage_thread(
                    label=f"@{call.name}",
                    fn=lambda call=call: self._invoke_decorator(call),
                    stdin_fd=stdin_fd_pipe,
                    stdout_fd=stdout_fd_pipe,
                )
                workers.append(worker)
                continue

            tokens = self._tokenize_stage(stage)
            if not tokens:
                continue

            # ``FOO=bar > x`` at top level is still an assignment, as it is
            # without the redirect.  (Inside a real pipeline it is not —
            # POSIX would run it in a subshell, so it is left to fail as a
            # command, as before.)
            if n == 1 and not _in_outer_pipe and all(
                self._ASSIGNMENT_RE.match(t) for t in tokens
            ):
                for token in tokens:
                    m = self._ASSIGNMENT_RE.match(token)
                    self._set_variable(m.group(1), m.group(2))
                continue

            # Per-command env prefix (``FOO=bar cmd``) applies to this stage only.
            env_prefix, tokens = self._split_env_prefix(tokens)
            if not tokens:
                continue

            cmd = self.registry.get(tokens[0])
            if cmd is not None and not cmd.has_any_handler():
                cmd = None

            # Resolve explicit redirects (override the pipe ends).
            stdin_file = stdout_file = None
            stderr_dst: object | None = None
            redirect_error = False
            for redir in stage.redirects:
                try:
                    if redir.kind == "<":
                        stdin_file = open(redir.target, "rb")
                    elif redir.kind == ">":
                        stdout_file = open(redir.target, "wb")
                    elif redir.kind == ">>":
                        stdout_file = open(redir.target, "ab")
                    elif redir.kind == "2>":
                        stderr_dst = open(redir.target, "wb")
                    elif redir.kind == "2>>":
                        stderr_dst = open(redir.target, "ab")
                    elif redir.kind == "2>&1":
                        stderr_dst = subprocess.STDOUT
                except OSError as e:
                    print(
                        f"eosh: {redir.target}: {e.strerror or e}",
                        file=sys.stderr,
                    )
                    redirect_error = True
                    break

            stdin_pipe_used = stdin_fd_pipe is not None and stdin_file is None
            stdout_pipe_used = stdout_fd_pipe is not None and stdout_file is None
            is_py_stage = cmd is not None

            worker = None
            if redirect_error:
                pass  # leave worker=None; cleanup below closes any open files/pipes
            elif is_py_stage:
                if env_prefix:
                    # Refused (see _env_prefix_refused) — as the stage's own
                    # work, so its pipe ends close and its status is 2.
                    fn = lambda name=cmd.name, env=env_prefix: self._env_prefix_refused(name, env)
                else:
                    fn = (lambda cmd=cmd, args=tokens[1:], ctx=self._command_context():
                          cmd.invoke(args, ctx=ctx))
                worker = self._start_stage_thread(
                    label=cmd.name,
                    fn=fn,
                    stdin_fd=stdin_fd_pipe if stdin_pipe_used else None,
                    stdout_fd=stdout_fd_pipe if stdout_pipe_used else None,
                    stdin_file=stdin_file,
                    stdout_file=stdout_file,
                    stderr_dst=stderr_dst,
                )
            else:
                stdin_arg = stdin_file if stdin_file else stdin_fd_pipe
                stdout_arg = stdout_file if stdout_file else stdout_fd_pipe
                try:
                    worker = subprocess.Popen(
                        tokens,
                        stdin=stdin_arg,
                        stdout=stdout_arg,
                        stderr=stderr_dst,
                        env=self._merged_env(env_prefix),
                        cwd=os.getcwd(),
                    )
                except FileNotFoundError:
                    print(f"eosh: command not found: {tokens[0]}")
                except OSError as e:
                    print(f"eosh: {e}")

            if worker is not None:
                workers.append(worker)

            # Drop the parent's reference to each pipe end the stage used.
            # For Popen the OS-level dup has already happened, so closing here
            # is correct.  For a Python-thread stage the worker thread owns
            # the fd via the TextIOWrapper passed to it and will close it
            # itself — so close here only if the stage *didn't* use that end
            # (because of an explicit redirect, or because the stage failed
            # to start at all).
            #
            # Outer-pipe boundary fds (idx=0 stdin, idx=n-1 stdout when
            # ``_in_outer_pipe`` is set) were duped from the thread-local
            # overrides; they need to be closed by the parent under exactly
            # the same rules — Popen handles its own dup, Python-thread
            # owns the fd, otherwise close here.
            stdin_fd_is_owned = idx > 0 or (
                outer_in_fd is not None and stdin_fd_pipe == outer_in_fd
            )
            stdout_fd_is_owned = idx < n - 1 or (
                outer_out_fd is not None and stdout_fd_pipe == outer_out_fd
            )
            close_stdin_pipe = (
                stdin_fd_is_owned
                and stdin_fd_pipe is not None
                and (worker is None or not is_py_stage or not stdin_pipe_used)
            )
            close_stdout_pipe = (
                stdout_fd_is_owned
                and stdout_fd_pipe is not None
                and (worker is None or not is_py_stage or not stdout_pipe_used)
            )
            if close_stdin_pipe:
                os.close(stdin_fd_pipe)
            if close_stdout_pipe:
                os.close(stdout_fd_pipe)

            # Close redirect file objects we opened.  Popen has already dup'd
            # them; the Python-thread stage took ownership of them, so in
            # both cases the parent's copy is no longer needed — except when
            # the stage failed to start, in which case we must close them
            # ourselves to release the fd.
            if not is_py_stage or worker is None:
                if stdin_file:
                    stdin_file.close()
                if stdout_file:
                    stdout_file.close()
                if (
                    stderr_dst is not None
                    and stderr_dst is not subprocess.STDOUT
                    and hasattr(stderr_dst, "close")
                ):
                    try:
                        stderr_dst.close()
                    except Exception:
                        pass

        exit_code = 0
        try:
            for w in workers:
                if isinstance(w, _PyStageHandle):
                    w.wait()
                    exit_code = w.exit_code or 0
                else:
                    w.wait()
                    exit_code = w.returncode or 0
        except KeyboardInterrupt:
            for w in workers:
                if isinstance(w, _PyStageHandle):
                    w.interrupt()
                else:
                    try:
                        w.terminate()
                    except Exception:
                        pass
            for w in workers:
                if isinstance(w, _PyStageHandle):
                    w.wait()
                else:
                    try:
                        w.wait()
                    except Exception:
                        pass
            exit_code = 130
        return exit_code

    def _start_stage_thread(
        self,
        *,
        label: str,
        fn: Callable[[], object],
        stdin_fd: int | None,
        stdout_fd: int | None,
        stdin_file=None,
        stdout_file=None,
        stderr_dst=None,
    ) -> "_PyStageHandle":
        """Run *fn* — a Python command or a decorator — as one pipeline stage.

        The thread takes ownership of *stdin_fd* / *stdout_fd* (raw OS pipe
        ends) or, when an explicit redirect is in play, the corresponding
        opened file object, and binds them to its thread-local
        ``sys.stdin`` / ``sys.stdout`` / ``sys.stderr`` for the duration of
        *fn*.  A decorator body that re-enters ``_execute_pipeline`` through
        ``Pipeline.run()`` inherits that binding, so its output flows on to
        the next stage.  The exit status comes from :func:`run_handler`.
        """
        # Exactly one of (stdin_fd, stdin_file) is set when this stage has
        # any stdin source, and similarly for stdout.
        in_obj = None
        if stdin_file is not None:
            in_obj = stdin_file
        elif stdin_fd is not None:
            in_obj = os.fdopen(stdin_fd, "rb", buffering=0, closefd=True)

        out_obj = None
        if stdout_file is not None:
            out_obj = stdout_file
        elif stdout_fd is not None:
            out_obj = os.fdopen(stdout_fd, "wb", buffering=0, closefd=True)

        err_obj = stderr_dst  # a file, subprocess.STDOUT (2>&1), or None

        handle = _PyStageHandle(cmd_name=label)
        in_wrapper = io.TextIOWrapper(in_obj, encoding="utf-8", errors="replace") if in_obj is not None else None
        out_wrapper = io.TextIOWrapper(out_obj, encoding="utf-8", errors="replace", write_through=True) if out_obj is not None else None
        err_wrapper = None
        if err_obj is not None and err_obj is not subprocess.STDOUT:
            err_wrapper = io.TextIOWrapper(err_obj, encoding="utf-8", errors="replace", write_through=True)
        # Hand wrappers to the handle so interrupt() can close them.
        for w in (in_wrapper, out_wrapper, err_wrapper):
            if w is not None:
                handle._io_objs.append(w)

        def _target():
            _in_pipeline.flag = True
            try:
                if in_wrapper is not None:
                    sys.stdin.set_override(in_wrapper)
                if out_wrapper is not None:
                    sys.stdout.set_override(out_wrapper)
                if err_obj is subprocess.STDOUT:
                    sys.stderr.set_override(out_wrapper if out_wrapper is not None else sys.stdout)
                elif err_wrapper is not None:
                    sys.stderr.set_override(err_wrapper)
                # After interrupt() closed our wrappers, an I/O error is the
                # expected way out, not one to report.
                handle.exit_code = run_handler(
                    fn, label, interrupted=lambda: handle.interrupted)
            finally:
                sys.stdin.clear_override()
                sys.stdout.clear_override()
                sys.stderr.clear_override()
                # Flush, then close (which closes the underlying fds/files) —
                # output first, so a reader pipe sees all of it and then EOF.
                for w in (out_wrapper, err_wrapper):
                    if w is not None:
                        try:
                            w.flush()
                        except Exception:
                            pass
                for w in (out_wrapper, err_wrapper, in_wrapper):
                    if w is not None:
                        try:
                            w.close()
                        except Exception:
                            pass
                _in_pipeline.flag = False
                handle.done.set()

        t = threading.Thread(target=_target, name=f"pipe-{label}", daemon=True)
        handle.thread = t
        t.start()
        return handle

    def _invoke_decorator(self, decorator_call):
        """Run a ``@name`` decorator over its body; returns its result.

        A decorator is the command ``@name``: its flags are parsed like any
        command's (a flag error is status 2, already printed) and its handler
        gets the body ``Pipeline`` first.  An unknown decorator is 127, as an
        unknown command would be.
        """
        deco = self.registry.get(f"@{decorator_call.name}")
        if deco is None:
            print(f"eosh: unknown decorator: @{decorator_call.name}", file=sys.stderr)
            return 127
        return deco.invoke(decorator_call.flag_tokens, decorator_call.body,
                           ctx=self._command_context())

    def _execute_decorator_stage(self, stage: Stage) -> int:
        """Run a lone ``@name`` stage on the main thread."""
        call = stage.decorator
        return run_handler(lambda: self._invoke_decorator(call), f"@{call.name}",
                           announce_interrupt=True)

    def _execute_stage(self, stage: Stage) -> int:
        """Execute a lone stage with no redirects on the terminal.

        External commands get a PTY (``ProcessSlot``), Python commands a
        ``PythonCommandSlot``, so either can be backgrounded with Ctrl+].
        Redirected stages never reach here — :meth:`_execute_pipeline` runs
        them as a one-stage pipeline.  Returns the exit code.
        """
        if stage.decorator is not None:
            return self._execute_decorator_stage(stage)

        tokens = self._tokenize_stage(stage)
        if not tokens:
            return 0

        # Pure-assignment line
        if all(self._ASSIGNMENT_RE.match(t) for t in tokens):
            for token in tokens:
                m = self._ASSIGNMENT_RE.match(token)
                self._set_variable(m.group(1), m.group(2))
            return 0

        # Per-command env prefix (``FOO=bar cmd args``): the assignments apply
        # only to this command, not the shell.
        env_prefix, tokens = self._split_env_prefix(tokens)

        command_name = tokens[0]
        args = tokens[1:]

        cmd = self.registry.get(command_name)
        # External recipes are stored as Command nodes too (for unified
        # completion), but have no Python handler anywhere — fall through to
        # the system command path so the real binary runs.
        if cmd is not None and not cmd.has_any_handler():
            cmd = None
        if cmd and env_prefix:
            return self._env_prefix_refused(command_name, env_prefix)
        if cmd:
            if IS_WINDOWS or cmd.sync:
                # On the main thread: a `sync` command (the built-ins — they
                # finish at once or change the shell's own state), and every
                # command on Windows, which lacks the PTY-backed slot used for
                # thread-based context switching.  ctx.run_interactive falls
                # back to subprocess.run and ctx.input reads the terminal
                # directly, since no slot is registered.
                return run_handler(lambda: cmd.invoke(args, ctx=self._command_context()),
                                   command_name,
                                   announce_interrupt=True)
            else:
                # Interactive Python command — run in a thread so Ctrl+] works.
                ctx = self.context_manager.current()
                slot = PythonCommandSlot(cmd, args, on_exit=self._slot_finished,
                                         ctx=self._command_context())
                slot.start()
                result = self._enter_python_forwarding_mode(slot)
                if result == "switched":
                    slot.deactivate()
                    if ctx is not None:
                        self._park(slot, ctx)
                    self._handle_switch()
                    return 0
                if result == "interrupted":
                    print(f"{command_name}: interrupted")
                    return 130
                slot.deactivate()
                return slot.exit_code or 0

        return self._execute_external(command_name, args, env_prefix=env_prefix)

    def _execute_external_windows(
        self, command_name: str, args: list[str], env_prefix: dict[str, str] | None = None
    ) -> int:
        """Run an external command on the real console (Windows path).

        Without ConPTY-based multiplexing, the child simply inherits the
        terminal's stdio. Commands that are cmd.exe builtins (dir, echo, cls,
        …) rather than real executables are retried via ``cmd /c``.
        """
        argv = [command_name] + args
        env = self._merged_env(env_prefix or {})
        cwd = os.getcwd()
        try:
            return subprocess.run(argv, env=env, cwd=cwd).returncode
        except FileNotFoundError:
            try:
                return subprocess.run(["cmd", "/c", *argv], env=env, cwd=cwd).returncode
            except FileNotFoundError:
                return self._command_not_found(argv)
            except OSError as e:
                print(f"eosh: {e}")
                return 1
        except OSError as e:
            print(f"eosh: {e}")
            return 1

    def _command_not_found(self, argv: list[str]) -> int:
        """Offer *argv* to the on_command_not_found hooks; report it if none
        claims it."""
        if hooks.fire_until_claimed("on_command_not_found", argv):
            return 0
        print(f"eosh: command not found: {argv[0]}")
        return 127

    def _execute_external(
        self, command_name: str, args: list[str], env_prefix: dict[str, str] | None = None
    ) -> int:
        if IS_WINDOWS:
            return self._execute_external_windows(command_name, args, env_prefix)

        ctx = self.context_manager.current()

        slot = ProcessSlot(on_exit=self._slot_finished)
        try:
            slot.start(
                argv=[command_name] + args,
                env=self._merged_env(env_prefix or {}),
                cwd=os.getcwd(),
            )
        except FileNotFoundError:
            return self._command_not_found([command_name] + args)
        except OSError as e:
            print(f"eosh: {e}")
            return 1

        slot.activate()
        slot.replay_buffer()  # flush any output that arrived before activate()
        result = self._enter_forwarding_mode(slot)
        if result == "switched":
            self._park(slot, ctx or self.context_manager.current())
            slot.deactivate()
            self._handle_switch()
            return 0
        # result == "exited"
        slot.deactivate()
        if ctx is not None:
            ctx.process_slot = None
        exit_code = slot.exit_code or 0
        if exit_code != 0:
            print(f"\n[Process exited with code {exit_code}]")
        return exit_code

    def _enter_forwarding_mode(self, slot: ProcessSlot, force_redraw: bool = False) -> str:
        """Forward I/O between real terminal and subprocess PTY.

        Returns 'exited' if process finished, 'switched' if user pressed Ctrl+].
        """
        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        old_sigint = signal.getsignal(signal.SIGINT)
        old_sigwinch = signal.getsignal(signal.SIGWINCH)
        result = "exited"
        try:
            tty.setraw(fd)
            signal.signal(signal.SIGINT, signal.SIG_IGN)

            def on_resize(signum, frame):
                try:
                    size = os.get_terminal_size(fd)
                    slot.resize(size.lines, size.columns)
                except OSError:
                    pass

            signal.signal(signal.SIGWINCH, on_resize)

            if force_redraw:
                on_resize(None, None)

            while slot.is_alive():
                rlist, _, _ = select.select([fd], [], [], 0.1)
                if fd in rlist:
                    data = os.read(fd, 1024)
                    if not data:
                        break
                    if b"\x1d" in data:
                        idx = data.index(b"\x1d")
                        if idx > 0:
                            slot.write_stdin(data[:idx])
                        result = "switched"
                        break
                    slot.write_stdin(data)
            return result
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
            signal.signal(signal.SIGINT, old_sigint)
            signal.signal(signal.SIGWINCH, old_sigwinch)
            if result == "switched":
                suspend_seq = slot.suspend_terminal_modes()
                if suspend_seq:
                    sys.stdout.write(suspend_seq)
                    sys.stdout.flush()

    def _enter_python_forwarding_mode(self, slot: PythonCommandSlot) -> str:
        """Monitor stdin while a Python command runs in a background thread.

        Sets the terminal to raw mode, activates the slot's stdout proxy
        (replaying any buffered output), then loops:
          • Ctrl+] (\\x1d) — return 'switched' so caller can store the slot
          • Ctrl+C (\\x03) — forwarded to a ctx.run_interactive() subprocess if
            one is active (so e.g. SSH/SSM see the interrupt); otherwise
            inject KeyboardInterrupt into the command thread.
          • other keys    — forwarded to slot.write_stdin, which writes to
            a ctx.run_interactive() PTY master if active (no-op otherwise).

        A command reading input (ctx.input / input_block) gets the keys
        through ``slot.write_stdin`` like everything else; the loop keeps
        raw mode throughout.

        Returns 'exited' when the thread finishes, 'switched' on Ctrl+].
        """
        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        old_sigint = signal.getsignal(signal.SIGINT)
        old_sigwinch = signal.getsignal(signal.SIGWINCH)
        result = "exited"

        def on_resize(signum, frame):
            try:
                size = os.get_terminal_size(fd)
                slot.resize(size.lines, size.columns)
            except OSError:
                pass

        try:
            # Raw INPUT (so the main loop sees Ctrl+] / Ctrl+C / etc one key
            # at a time) but COOKED OUTPUT (kernel ONLCR re-adds CRs to bare
            # LFs).  Anything the Python command writes — print(), pexpect's
            # captured remote output, raw byte writes, piped subprocess
            # output — gets proper line endings without per-call effort.
            terminal.set_raw_input_cooked_output(fd)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGWINCH, on_resize)
            # Replay any output buffered before raw mode was set
            slot.activate()

            while slot.is_alive():
                rlist, _, _ = select.select([fd], [], [], 0.1)
                if fd in rlist:
                    data = os.read(fd, 1024)
                    if not data:
                        break
                    if b"\x1d" in data:
                        result = "switched"
                        break
                    if (b"\x03" in data and not slot._pty_active
                            and not slot._reading_input):
                        # No passthrough subprocess is running and the
                        # command isn't asking a question (whose reader
                        # turns Ctrl+C into its own KeyboardInterrupt) —
                        # interrupt the Python command itself.
                        slot.deactivate()
                        slot.kill()
                        result = "interrupted"
                        break
                    # Forward to slot.  When a ctx.run_interactive() subprocess
                    # is active, this writes to its PTY master.  Otherwise
                    # write_stdin() is a no-op (the command isn't reading).
                    slot.write_stdin(data)
            return result
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
            signal.signal(signal.SIGINT, old_sigint)
            signal.signal(signal.SIGWINCH, old_sigwinch)

    _PREVIEW_HEIGHT = 3
    # Action key bindings:  (key bytes, action name, hint label).
    # Ctrl+N overrides the picker's default "down" alias for the duration of
    # the context-switch picker; users can still navigate with the arrow keys
    # (down arrow + Ctrl+P up).
    _SWITCH_KEY_ACTIONS: tuple[tuple[bytes, str, str], ...] = (
        (b"\x0e",  "new",    "^N new"),
        (b"\x04",  "delete", "^D delete"),
        (b"\x12",  "rename", "^R rename"),
    )

    def _show_switch_menu(self) -> tuple[str, bool] | None:
        """Show TUI context picker.

        Returns ``(name, is_new)`` when the user picks a context (or creates one),
        ``None`` on cancel.  Action keys (new / delete / rename) mutate the
        context manager in place and re-open the picker until the user picks a
        context or cancels.
        """
        from .tui import InlineArgPrompt, InlinePicker

        def display_fn(name: str) -> str:
            cur = self.context_manager.current_name
            return ("* " if name == cur else "  ") + name

        def meta_fn(name: str) -> str:
            ctx = self.context_manager.contexts.get(name)
            if ctx is None:
                return ""
            slot = ctx.process_slot
            if slot and slot.is_alive() and slot.argv:
                parts = [os.path.basename(slot.argv[0])] + slot.argv[1:2]
                return " ".join(parts)
            return ""

        def preview_fn(name: str) -> list[str]:
            ctx = self.context_manager.contexts.get(name)
            if ctx is None or ctx.process_slot is None:
                return []
            slot = ctx.process_slot
            if hasattr(slot, "tail_lines"):
                try:
                    return slot.tail_lines(self._PREVIEW_HEIGHT)
                except Exception:
                    return []
            return []

        key_actions = {kb: name for kb, name, _ in self._SWITCH_KEY_ACTIONS}
        hints = "  ".join(label for _, _, label in self._SWITCH_KEY_ACTIONS)

        # Selection persists across re-openings (after delete/rename).
        selected_name: str | None = self.context_manager.current_name
        # One-shot status message shown in the next picker iteration — used to
        # explain why an action was refused (e.g. running process, last context).
        warning: str = ""

        while True:
            contexts = self.context_manager.list_contexts()

            picker = InlinePicker(
                contexts,
                display_fn=display_fn,
                meta_fn=meta_fn,
                max_height=10,
                min_width=32,
                hide_cursor=True,
                status_label=warning,
                status_hints=hints,
                transient_status=True,
                preview_fn=preview_fn,
                preview_height=self._PREVIEW_HEIGHT,
                key_actions=key_actions,
            )
            warning = ""  # one-shot — clear after attaching to the picker
            if selected_name in contexts:
                picker._selected = contexts.index(selected_name)

            selected = picker.run()

            if selected is None:
                return None

            action = picker.action
            if action == "new":
                sys.stdout.write("\n")
                sys.stdout.flush()
                arg_prompt = InlineArgPrompt(label="new context name")
                name = arg_prompt.run()
                sys.stdout.write("\033[1A")
                sys.stdout.flush()
                if not name:
                    selected_name = self.context_manager.current_name
                    continue
                if name in self.context_manager.contexts:
                    warning = f"context '{name}' already exists"
                    selected_name = self.context_manager.current_name
                    continue
                return (name, True)

            if action == "delete":
                if len(self.context_manager.list_contexts()) <= 1:
                    warning = "cannot delete: only one context remaining"
                    continue
                ctx = self.context_manager.contexts.get(selected)
                if ctx is None:
                    continue
                if ctx.process_slot and ctx.process_slot.is_alive():
                    warning = (
                        f"cannot delete '{selected}': process still running "
                        f"(use 'context kill {selected}' first)"
                    )
                    continue
                # Pick a sensible next selection before removing.
                remaining = [n for n in contexts if n != selected]
                idx = contexts.index(selected)
                next_sel = remaining[min(idx, len(remaining) - 1)] if remaining else None
                self.context_manager.remove(selected)
                # If we deleted the current context, remove() switched the
                # pointer for us; align selection with the new current so the
                # picker reopens with that highlighted.
                selected_name = self.context_manager.current_name or next_sel
                continue

            if action == "rename":
                old = selected
                sys.stdout.write("\n")
                sys.stdout.flush()
                arg_prompt = InlineArgPrompt(
                    label=f"rename '{old}' to",
                    initial=old,
                )
                new = arg_prompt.run()
                sys.stdout.write("\033[1A")
                sys.stdout.flush()
                if new and new != old:
                    if new in self.context_manager.contexts:
                        warning = f"context '{new}' already exists"
                        selected_name = old
                    else:
                        try:
                            self.context_manager.rename(old, new)
                            selected_name = new
                        except (KeyError, ValueError) as e:
                            warning = str(e)
                            selected_name = old
                else:
                    selected_name = old
                continue

            # No action key — user pressed Enter to accept the selection.
            if selected == self.context_manager.current_name:
                return None
            return (selected, False)

    def _resume_pty_slot(self, slot: ProcessSlot) -> None:
        """Restore terminal modes and re-activate a backgrounded PTY slot.

        Used both when resuming after a context switch and when the user
        cancels the switch picker.  Two strategies, picked by alt-screen
        state:
          • Alt-screen TUIs (vi, tfm, less): rely on the app's own
            redraw.  We force a SIGWINCH-driven repaint by wiggling the
            PTY size: (1, 1) then the real (rows, cols).  This guarantees
            ncurses sees a real resize event (not a no-op short-circuit)
            and triggers KEY_RESIZE → full clear+redraw.  Snapshot replay
            of the buffer is unsafe — paint commands are size-dependent
            and the deque-bounded history can be partial, leaving stale
            cells (selection markers, mis-positioned separators) that
            ncurses' shadow won't consider dirty.
          • Streaming output (logs, build): activate(replay_missed=True)
            atomically prints bytes that arrived while inactive and
            clears the missed-buffer under the buffer lock.
        """
        restore_seq = slot.restore_terminal_modes()
        if restore_seq:
            sys.stdout.write(restore_seq)
            sys.stdout.flush()
        if slot.terminal_modes.get("alt_screen", False):
            slot.activate()
            # Force the app to do a full clear+redraw.  The freshly-
            # restored alt-screen is blank, but the app's internal shadow
            # still matches what was on screen pre-suspend, so a passive
            # resume leaves the screen empty until the app repaints.
            #
            # Send Ctrl+L (\x0c, FF) — the universal TUI convention for
            # "force full redraw".  Bound by default in vim, nvim, nano,
            # less, mc, emacs, htop, etc.  Apps that don't handle it
            # (and aren't simply ignoring it) should — it's the standard
            # contract.  Wiggling the PTY size to fire SIGWINCH/KEY_RESIZE
            # works around non-conforming apps but introduces its own
            # artifacts (vim's per-column diff redraw confusion when the
            # file exceeds viewport height), so we no longer do that.
            slot.write_stdin(b"\x0c")
        else:
            slot.activate(replay_missed=True)

    def _emit_context_separator(self, new_name: str) -> None:
        """Print a dim full-width separator labelled with the new context name.

        Called when the active context changes (by Ctrl+] in either the line
        editor or a running process) so the new prompt is visually distinct
        from the previous context's output.  Cursor is assumed to be at column
        0 of an otherwise blank line; the separator ends with ``\\r\\n`` so the
        cursor lands on a fresh line below.
        """
        try:
            cols = os.get_terminal_size().columns
        except OSError:
            cols = 80
        from .lineedit import _wcswidth
        label = f"─ {new_name} "
        label_w = _wcswidth(label)
        fill = "─" * max(1, cols - label_w)
        sys.stdout.write(f"\r\033[2m{label}{fill}\033[22m\r\n")
        sys.stdout.flush()

    def _handle_switch(self) -> tuple[bool, str | None]:
        """Handle Ctrl+] switch request.

        Returns ``(needs_forward, new_context_name)``.  ``needs_forward`` is
        True when lineedit should exit (CONTEXT_CHANGED_SENTINEL) so the run()
        loop can take over — either to replay buffered output or to enter
        forwarding mode.  ``new_context_name`` is the name of the now-active
        context when it differs from the one we entered with (lineedit uses it
        to decide whether to preserve the old prompt); ``None`` when the
        context did not change (plain picker cancel).

        When the context changes, a dim separator labelled with the new
        context's name is printed before returning, so all call sites
        (line-editor Ctrl+] and forwarding-mode Ctrl+] from vi/etc.) get a
        consistent visual break between contexts.
        """
        ctx = self.context_manager.current()
        original_name = ctx.name if ctx else None
        if ctx and ctx.process_slot:
            slot = ctx.process_slot
            slot_alive = slot.is_alive()
            # Snapshot the cursor-column hint *before* deactivating — once
            # deactivated, new output is buffered (not displayed), but
            # ``cursor_at_col0()`` reflects what is already on screen, so
            # the ordering is not strictly required.  Done first anyway for
            # symmetry with how the line-editor path measures its caret.
            at_col0 = slot.cursor_at_col0() if slot_alive else True
            slot.deactivate()
            if slot_alive and not at_col0:
                # The picker is anchored at the current cursor position and
                # its first render does ``\r\033[J`` — clearing the anchor
                # row from column 0 onwards. If the subprocess's last write
                # did not end with ``\n`` / ``\r``, the cursor sits mid-line
                # and the picker would erase that last output line. Move
                # down to a fresh row first.  When the cursor is already at
                # column 0 (output ended with ``\n`` — the common case for
                # streaming tools like ``ping``), skip this so we don't add
                # a blank row.  The line-editor path (``_do_inline_switch``)
                # has its own pre-picker positioning and never reaches here
                # for a live slot.
                sys.stdout.write("\r\n")
                sys.stdout.flush()

        result = self._show_switch_menu()

        def _finish(needs_forward: bool) -> tuple[bool, str | None]:
            new_ctx = self.context_manager.current()
            new_name = new_ctx.name if new_ctx else None
            if new_name != original_name:
                if new_name is not None:
                    self._emit_context_separator(new_name)
                # Between the separator and the new prompt.  The line editor
                # may hold the terminal raw here; a hook prints normally.
                with _cooked_output():
                    self._notice_state_change()
                return (needs_forward, new_name)
            return (needs_forward, None)

        if result is None:
            # User cancelled.  If a menu action (pop) changed the current
            # context, behave as if the user had switched: let run()'s resume
            # path do the right thing for the new context's slot.  Otherwise
            # re-activate PTY slots so their reader thread can stream output
            # again.  PythonCommandSlots stay deactivated: their buffered
            # output will be replayed correctly the next
            # time _enter_python_forwarding_mode is called from run().
            new_ctx = self.context_manager.current()
            if new_ctx is None or (new_ctx.name != original_name):
                needs_forward = bool(new_ctx and new_ctx.process_slot)
                return _finish(needs_forward)
            if ctx and ctx.process_slot and ctx.process_slot.is_alive():
                slot = ctx.process_slot
                if not isinstance(slot, PythonCommandSlot):
                    self._resume_pty_slot(slot)
            return (False, None)

        target_name, is_new = result

        if is_new:
            self.context_manager.new(target_name)
        self.context_manager.switch(target_name)

        new_ctx = self.context_manager.current()
        # Don't activate a new-context slot here — leave that to run()'s resume
        # path so it can choose between snapshot replay (TUI) and missed-buffer
        # flush (streaming) based on alt-screen state.  Returning True makes
        # lineedit exit so run() takes over immediately rather than waiting for
        # the user to press Enter.
        needs_forward = bool(new_ctx and new_ctx.process_slot)
        return _finish(needs_forward)

    def _background_count(self) -> int:
        """Count contexts with running processes (excluding current)."""
        current = self.context_manager.current_name
        count = 0
        for name, ctx in self.context_manager.contexts.items():
            if name != current and ctx.state == ContextState.RUNNING:
                count += 1
        return count

    def run(self) -> None:
        self._install_sigwinch_handler()
        print("Eolith Shell — type 'help' for available commands, 'exit' to quit.")
        hooks.fire("on_startup")
        try:
            self._run_loop()
        finally:
            hooks.fire("on_exit")

    def _run_loop(self) -> None:
        while True:
            try:
                ctx = self.context_manager.current()

                if ctx and ctx.process_slot and ctx.process_slot.is_alive():
                    slot = ctx.process_slot
                    if isinstance(slot, PythonCommandSlot):
                        # Resume a backgrounded Python command.
                        result = self._enter_python_forwarding_mode(slot)
                        slot.deactivate()
                        if result == "switched":
                            self._handle_switch()
                            continue
                        elif result == "interrupted":
                            ctx.process_slot = None
                            print(f"{slot.argv[0]}: interrupted")
                            continue
                        else:
                            # Its exit handler already notified; an error
                            # was reported on the slot's own stderr.
                            ctx.process_slot = None
                            continue
                    else:
                        # Resume a PTY subprocess.
                        self._resume_pty_slot(slot)
                        result = self._enter_forwarding_mode(slot, force_redraw=True)
                        slot.deactivate()
                        if result == "switched":
                            self._handle_switch()
                            continue
                        else:
                            exit_code = slot.exit_code
                            ctx.process_slot = None
                            if exit_code and exit_code != 0:
                                print(f"\n[Process exited with code {exit_code}]")
                            continue

                if ctx and ctx.process_slot and not ctx.process_slot.is_alive():
                    slot = ctx.process_slot
                    slot.replay_buffer()
                    exit_code = slot.exit_code
                    ctx.process_slot = None
                    if isinstance(slot, PythonCommandSlot) and exit_code == 130:
                        print(f"{slot.argv[0]}: killed")
                    elif exit_code and exit_code != 0:
                        print(f"\n[Process exited with code {exit_code}]")

                # Anything a resumed or finished slot changed.
                self._notice_state_change()

                # Collect the primary line (history managed here, not inside the editor).
                text = self._line_editor.prompt()
                if text == CONTEXT_CHANGED_SENTINEL:
                    continue

                # Handle backslash line continuation: keep prompting with "> "
                # until a line that does NOT end with an unescaped backslash.
                # Ctrl+C propagates as KeyboardInterrupt and abandons the command.
                # A context-switch (Ctrl+]) during continuation also abandons it.
                full_text = text
                while _is_continuation(full_text):
                    partial = _strip_continuation(full_text)
                    cont = self._line_editor.prompt(prompt_str="> ")
                    if cont == CONTEXT_CHANGED_SENTINEL:
                        full_text = ""
                        break
                    full_text = partial + cont

                if full_text.strip():
                    history_id = self._record_history(full_text.strip())
                    self._execute(full_text.strip(), history_id=history_id)
                    if self._exit_requested:
                        break
            except KeyboardInterrupt:
                continue
            except EOFError:
                print("\nexit")
                running = self._running_contexts()
                if running and not self._confirm_exit(running):
                    continue
                break
            except SystemExit:
                break

    def _running_contexts(self) -> list[tuple[str, list[str]]]:
        return [
            (name, ctx.process_slot.argv)
            for name, ctx in self.context_manager.contexts.items()
            if ctx.process_slot and ctx.process_slot.is_alive()
        ]

    def _confirm_exit(self, running: list[tuple[str, list[str]]]) -> bool:
        print(f"There {'is' if len(running) == 1 else 'are'} {len(running)} context(s) with running processes:")
        for name, argv in running:
            print(f"  {name}: {' '.join(argv)}")
        try:
            answer = _read_from_user("Exit anyway? [y/N] ", block=False).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return answer in ("y", "yes")

    def _install_sigwinch_handler(self) -> None:
        # No SIGWINCH on Windows; live-process resize forwarding is part of the
        # PTY multiplexing path, which is POSIX-only.
        if not terminal.HAS_SIGWINCH:
            return

        def on_resize(signum, frame):
            ctx = self.context_manager.current()
            if ctx and ctx.process_slot and ctx.process_slot.is_alive():
                try:
                    rows, cols = os.get_terminal_size()
                    ctx.process_slot.resize(rows, cols)
                except OSError:
                    pass

        signal.signal(signal.SIGWINCH, on_resize)
