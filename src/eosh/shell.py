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
from pathlib import Path

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
from .commands import arg, CommandRegistry, get_positional_completer, registry as command_registry
from .completion import (
    CommandNameCompleter,
    CompletionContext,
    FileCompleter,
    Completion,
    HistoryCompleter,
    get_argcomplete_fallback,
)
from .variables import registry as var_registry, VarCompleter
from .context import ContextManager, ContextState
from .lineedit import CONTEXT_CHANGED_SENTINEL, History, LineEditor
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
from . import notify
from .process import ExitCallbackMixin, OutputBuffer, ProcessSlot
from .prompt import get_prompt_func, set_prompt

# ---------------------------------------------------------------------------
# Thread-local stdout routing + per-slot buffering proxy
# ---------------------------------------------------------------------------

class _ThreadLocalStdout(io.TextIOBase):
    """A sys.stdout replacement that routes writes per thread.

    The main thread (no override set) writes directly to *real*.
    A Python-command thread sets an override via set_override() so its
    print() calls go to a _StdoutProxy, keeping them separate from the
    main thread's terminal output.
    """

    def __init__(self, real: io.TextIOBase) -> None:
        self._real = real
        self._local = threading.local()

    @property
    def _target(self) -> io.TextIOBase:
        return getattr(self._local, "override", None) or self._real

    def write(self, s: str) -> int:
        return self._target.write(s)

    def flush(self) -> None:
        self._target.flush()

    def fileno(self) -> int:
        return self._real.fileno()

    @property
    def buffer(self):
        return self._real.buffer

    @property
    def encoding(self) -> str:
        return getattr(self._real, "encoding", "utf-8")

    @property
    def errors(self) -> str:
        return getattr(self._real, "errors", "strict")

    def isatty(self) -> bool:
        # A pipe-end override is never a tty.  When no override is active
        # (main thread) or the override forwards to the real stdout
        # (_StdoutProxy in a PythonCommandSlot), defer to the real stream.
        target = getattr(self._local, "override", None)
        if target is not None:
            return target.isatty()
        return self._real.isatty()

    def set_override(self, proxy) -> None:
        self._local.override = proxy

    def clear_override(self) -> None:
        self._local.override = None


class _ThreadLocalStdin(io.TextIOBase):
    """A sys.stdin replacement that routes reads per thread.

    The main thread (no override set) reads from *real*.  A pipeline
    thread sets an override pointing at a TextIOWrapper around its pipe
    fd so ``input()`` / ``sys.stdin.read()`` consume from the pipe
    instead of the terminal.
    """

    def __init__(self, real: io.TextIOBase) -> None:
        self._real = real
        self._local = threading.local()

    @property
    def _target(self) -> io.TextIOBase:
        return getattr(self._local, "override", None) or self._real

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

    def fileno(self) -> int:
        return self._real.fileno()

    @property
    def buffer(self):
        target = getattr(self._local, "override", None)
        return getattr(target, "buffer", None) or self._real.buffer

    @property
    def encoding(self) -> str:
        return getattr(self._real, "encoding", "utf-8")

    @property
    def errors(self) -> str:
        return getattr(self._real, "errors", "strict")

    def isatty(self) -> bool:
        target = getattr(self._local, "override", None)
        if target is not None:
            return False
        return self._real.isatty()

    def set_override(self, stream) -> None:
        self._local.override = stream

    def clear_override(self) -> None:
        self._local.override = None


class _ThreadLocalStderr(io.TextIOBase):
    """A sys.stderr replacement that routes writes per thread.

    Mirrors :class:`_ThreadLocalStdout` for stderr so a pipeline thread
    can redirect its diagnostic output independently of the main
    thread.
    """

    def __init__(self, real: io.TextIOBase) -> None:
        self._real = real
        self._local = threading.local()

    @property
    def _target(self) -> io.TextIOBase:
        return getattr(self._local, "override", None) or self._real

    def write(self, s: str) -> int:
        return self._target.write(s)

    def flush(self) -> None:
        self._target.flush()

    def fileno(self) -> int:
        return self._real.fileno()

    @property
    def buffer(self):
        return self._real.buffer

    @property
    def encoding(self) -> str:
        return getattr(self._real, "encoding", "utf-8")

    @property
    def errors(self) -> str:
        return getattr(self._real, "errors", "strict")

    def set_override(self, stream) -> None:
        self._local.override = stream

    def clear_override(self) -> None:
        self._local.override = None


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


_current_slot = threading.local()

# Set on threads spawned by _execute_pipeline for Python-command stages.
# passthrough_run / passthrough_input check this and refuse, since stdin
# and stdout are wired to pipe ends, not the terminal.
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
    """
    override = getattr(getattr(stream, "_local", None), "override", None)
    if override is None:
        return None
    try:
        fd = override.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    try:
        return os.dup(fd)
    except OSError:
        return None


def passthrough_run(argv: list[str], **popen_kwargs) -> int:
    """Run an interactive subprocess from inside a Python command thread.

    A Python @registry.command runs in a background thread while the main
    thread holds stdin in raw mode and forwards bytes to the slot.  Calling
    ``subprocess.run`` directly inside such a command makes the child
    inherit the real stdin — which the main thread is also reading — and
    keystrokes are split unpredictably between the two.

    ``passthrough_run`` runs the subprocess against a PTY owned by the
    enclosing :class:`PythonCommandSlot` so the main thread keeps a single
    consistent view of stdin: it forwards bytes to the slot's PTY master,
    intercepts ``Ctrl+]`` for context switching, and the subprocess sees a
    full TTY on its fd 0/1/2.

    Outside a Python command thread (e.g. from a synchronous handler) the
    function falls back to ``subprocess.run(argv, **popen_kwargs)``.

    Returns the subprocess's exit code.
    """
    if getattr(_in_pipeline, "flag", False):
        raise RuntimeError(
            "passthrough_run cannot be used inside a piped Python command "
            "(stdin/stdout are wired to pipes, not the terminal)"
        )
    slot = getattr(_current_slot, "slot", None)
    if slot is None:
        return subprocess.run(argv, **popen_kwargs).returncode

    return slot._run_in_pty(argv, popen_kwargs)


def passthrough_input(prompt: str = "") -> str:
    """Read a line from real stdin from inside a Python command thread.

    Built-in ``input()`` would race the main forwarding thread for stdin
    bytes, and even when it won, raw mode would suppress echo and turn
    Enter into ``\\r``.  ``passthrough_input`` coordinates with the main
    forwarding loop: it asks the loop to surrender stdin and restore
    cooked terminal mode, calls :func:`input` on the slot thread, then
    hands control back.

    Outside a Python command thread, falls back to plain ``input(prompt)``.
    """
    if getattr(_in_pipeline, "flag", False):
        raise RuntimeError(
            "passthrough_input cannot be used inside a piped Python command "
            "(stdin/stdout are wired to pipes, not the terminal)"
        )
    slot = getattr(_current_slot, "slot", None)
    if slot is None:
        return input(prompt)
    return slot._run_input(prompt)


def _stdin_is_tty() -> bool:
    """True when real stdin is a terminal — i.e. a key stream exists to read."""
    try:
        return os.isatty(sys.stdin.fileno())
    except (OSError, ValueError, AttributeError):
        return False


def passthrough_input_block(prompt: str = "") -> str:
    """Read a *block* of lines from real stdin, ending at a blank line or EOF.

    The multi-line sibling of :func:`passthrough_input`, for commands whose
    input is pasted rather than typed (a set of ``export KEY=…`` lines, a
    policy document, …).  Two things rule out looping over
    ``passthrough_input``: between two calls the main forwarding loop takes
    stdin back into raw mode, so the tail of a paste still in the tty buffer
    is read as keystrokes instead of input — and the cooked mode each call
    asks for caps a line at ``MAX_CANON``, which a pasted session token
    exceeds.  This reads the block off the raw key stream instead; see
    :meth:`PythonCommandSlot._run_input_block`.

    Returns the lines joined by ``\\n``, without the terminating blank line
    (so an immediate blank line or Ctrl+D yields ``""``).  Outside a Python
    command thread — or with stdin not a terminal — falls back to reading
    :data:`sys.stdin` line by line.
    """
    if getattr(_in_pipeline, "flag", False):
        raise RuntimeError(
            "passthrough_input_block cannot be used inside a piped Python command "
            "(stdin/stdout are wired to pipes, not the terminal)"
        )
    slot = getattr(_current_slot, "slot", None)
    if slot is not None and _stdin_is_tty():
        return slot._run_input_block(prompt)
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


class PythonCommandSlot(ExitCallbackMixin):
    """Manages a Python @registry.command running in a background thread.

    Implements the same runtime interface as ProcessSlot so the shell's
    run() loop and context machinery can treat both uniformly.
    """

    def __init__(self, cmd, raw_args: list[str]) -> None:
        self._init_exit_callback()
        self._cmd = cmd
        self._raw_args = raw_args
        self.argv: list[str] = [cmd.name] + raw_args
        self._thread: threading.Thread | None = None
        self._proxy: _StdoutProxy | None = None
        self._err_proxy: _StdoutProxy | None = None
        self._exit_exception: BaseException | None = None
        self._finished = threading.Event()
        # Stub attributes expected by the run() loop
        self.buffer = _NullBuffer()
        self.exit_code: int | None = None
        # PTY state — created on demand by passthrough_run().  When a
        # subprocess is running here, the main thread reads stdin and writes
        # to master_fd; a reader thread copies master_fd output to stdout.
        self._pty_master_fd: int = -1
        self._pty_subproc: subprocess.Popen | None = None
        self._pty_reader: threading.Thread | None = None
        self._pty_buffer = OutputBuffer()
        self._pty_last_byte: bytes = b"\n"
        self._pty_active = False
        self._pty_lock = threading.Lock()
        # passthrough_input() coordination — events are flipped by the
        # slot thread; the main forwarding loop watches _input_request.
        self._input_request = threading.Event()
        self._input_released = threading.Event()
        self._input_resume = threading.Event()
        # Set by the main forwarding loop's SIGINT handler when Ctrl+C is
        # pressed during a passthrough_input() prompt.  _run_input polls
        # for it and raises KeyboardInterrupt on the slot thread instead
        # of leaving the user with an unresponsive prompt.
        self._input_interrupted = threading.Event()
        # Raw stdin bytes the main forwarding loop received while no PTY
        # subprocess was active.  :meth:`poll_key` drains them — that is
        # how ``passthrough_input_block`` reads a paste off the raw key
        # stream.  Cleared each time the slot thread picks up a Python
        # command.
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
            self._cmd.invoke(self._raw_args)
        except SystemExit as e:
            self._exit_exception = e
        except KeyboardInterrupt as e:
            self._exit_exception = e
        except Exception as e:
            self._exit_exception = e
        finally:
            _current_slot.slot = None
            if hasattr(sys.stdout, "clear_override"):
                sys.stdout.clear_override()
            if hasattr(sys.stderr, "clear_override"):
                sys.stderr.clear_override()
            self.exit_code = self._compute_exit_code()
            self._finished.set()
            self._fire_on_exit()

    def _compute_exit_code(self) -> int:
        exc = self._exit_exception
        if exc is None:
            return 0
        if isinstance(exc, SystemExit):
            code = exc.code
            return code if isinstance(code, int) else (1 if code else 0)
        if isinstance(exc, KeyboardInterrupt):
            return 130
        return 1

    # --- ProcessSlot-compatible interface ------------------------------------

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def activate(self, raw_mode: bool = False) -> None:  # noqa: ARG002
        # ``raw_mode`` is part of the slot interface (``ProcessSlot`` and
        # ``PipelineSlot`` use it); a Python command's output needs no
        # translation — the kernel adds CRs (see ``_StdoutProxy``).
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

        * If a ``passthrough_run`` subprocess is active, the bytes go to
          its PTY master so the child sees the user's typing.
        * Otherwise the bytes are buffered in ``_keybuf`` so a Python
          command body running on this slot can read them via
          :meth:`poll_key` (``passthrough_input_block`` does).
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
        consumed here will not later reach a ``passthrough_run``
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
        """Propagate a SIGWINCH-driven resize to a passthrough_run() subprocess."""
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

    # --- passthrough_run() implementation -----------------------------------

    def _run_in_pty(self, argv: list[str], popen_kwargs: dict) -> int:
        """Run *argv* on a slot-owned PTY; main thread forwards stdin via write_stdin."""
        # Snapshot whether the proxy was active (i.e. the user was watching
        # this slot in the foreground) before deactivating it for the
        # subprocess.  The PTY reader thread mirrors this state — if the
        # subprocess starts while the slot is backgrounded (e.g. a Python
        # ``@bg`` body that calls ``passthrough_run``), the reader buffers
        # to ``_pty_buffer`` instead of writing to the real terminal.
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

    # --- passthrough_input() implementation ---------------------------------

    def _run_input(self, prompt: str) -> str:
        """Read a line of input while the main loop yields stdin and cooked mode.

        The naive ``input()`` call would leave Ctrl+C unresponsive: the main
        forwarding loop has SIGINT ignored, and the slot thread's blocking
        ``read()`` can't be interrupted from outside.  Instead we poll fd 0
        through ``select`` so the slot can periodically check
        ``_input_interrupted`` (set by the main loop's SIGINT handler) and
        raise ``KeyboardInterrupt`` promptly.

        Cooked terminal mode is still in effect, so the kernel handles line
        editing (backspace, delete-word, etc.) and ``select`` reports the
        fd readable only once the user hits Enter.  That gives us full line
        editing without reimplementing it.

        The kernel's line buffer is also the limit of this method: a line at
        or above ``MAX_CANON`` (1024 bytes on macOS) is discarded by the line
        discipline, not truncated.  Prompts answered by a word or two never
        approach it; :meth:`_run_input_block`, which has to take a pasted
        credential block, reads raw for exactly that reason.
        """
        import select as _select
        # Drain pending output so the prompt isn't preceded by buffered text.
        self._proxy.deactivate()
        self._proxy.replay()
        if self._err_proxy:
            self._err_proxy.deactivate()
            self._err_proxy.replay()
        self._input_interrupted.clear()
        self._input_resume.clear()
        self._input_released.clear()
        self._input_request.set()
        # Wait for the main loop to release stdin and restore cooked mode.
        self._input_released.wait()
        try:
            # Print the prompt directly to the real terminal.
            real = getattr(sys.stdout, "_real", sys.stdout)
            real.write(prompt)
            real.flush()
            fd = sys.stdin.fileno()
            buf = b""
            while True:
                if self._input_interrupted.is_set():
                    raise KeyboardInterrupt
                rlist, _, _ = _select.select([fd], [], [], 0.1)
                if fd not in rlist:
                    continue
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    raise EOFError
                if not chunk:
                    if buf:
                        return buf.decode("utf-8", errors="replace")
                    raise EOFError
                buf += chunk
                # In cooked mode, select fires only on full lines (terminal
                # delivers everything up to and including the newline).
                if b"\n" in buf:
                    line, _, _rest = buf.partition(b"\n")
                    return line.decode("utf-8", errors="replace")
        finally:
            self._input_request.clear()
            self._input_resume.set()
            self._proxy.activate()
            if self._err_proxy:
                self._err_proxy.activate()

    def _run_input_block(self, prompt: str = "") -> str:
        """Read lines until a blank line or Ctrl+D, off the raw key stream.

        Not a loop around :meth:`_run_input`, and not a cooked-mode read at
        all, because the terminal's canonical line buffer is too small for
        what this is for: at ``MAX_CANON`` (1024 bytes on macOS) the line
        discipline throws the *whole* over-long line away rather than
        truncating it, and one pasted ``export AWS_SESSION_TOKEN=…`` line
        clears that on its own.  So the block is read in the raw mode the
        forwarding loop already holds — no line discipline in the way — and
        the echo and line assembly the kernel would have done happen here.

        Bytes arrive through :meth:`poll_key`, which is fed by the main
        loop's stdin reader, so a paste that lands before the first poll is
        already buffered rather than lost.  Line editing is deliberately
        minimal (backspace only): this reads pasted text, not typed text.
        """
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        lines: list[str] = []
        current: list[str] = []
        in_escape = False
        after_cr = False

        def echo(text: str) -> None:
            sys.stdout.write(text)
            sys.stdout.flush()

        if prompt:
            echo(prompt)

        while True:
            # A bounded wait, so an injected KeyboardInterrupt (slot.kill()
            # on Ctrl+C) lands between calls instead of blocking in C.
            data = self.poll_key(0.2)
            if not data:
                continue
            for ch in decoder.decode(data):
                if in_escape:
                    # Swallow the rest of an escape sequence — arrow keys and
                    # the terminal's bracketed-paste markers alike.
                    if ch.isalpha() or ch == "~":
                        in_escape = False
                    continue
                if ch == "\x1b":
                    in_escape = True
                    continue
                if ch == "\x03":
                    raise KeyboardInterrupt
                if ch == "\x04":                     # Ctrl+D — end the block
                    if current:
                        lines.append("".join(current))
                    return "\n".join(lines)
                if ch in ("\r", "\n"):
                    # CRLF in pasted text is one line ending, not two.
                    if ch == "\n" and after_cr:
                        continue
                    after_cr = ch == "\r"
                    echo("\n")
                    line = "".join(current)
                    current = []
                    if not line.strip():
                        return "\n".join(lines)
                    lines.append(line)
                    continue
                after_cr = False
                if ch in ("\x7f", "\x08"):
                    if current:
                        current.pop()
                        echo("\b \b")
                    continue
                if ch < " " and ch != "\t":
                    continue                          # other C0 controls
                current.append(ch)
                echo(ch)


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


def _positional_index(args: list[str], options_completer) -> int:
    """Return the number of positional (non-flag) arguments in *args*.

    Flags are skipped without counting: boolean flags advance by 1 token;
    value-taking flags (those listed in ``options_completer.args``) advance
    by 2 tokens because they consume the following token as their value.
    """
    pos = 0
    i = 0
    value_taking = (
        set(options_completer.args)
        if options_completer and hasattr(options_completer, "args")
        else set()
    )
    while i < len(args):
        token = args[i]
        if token.startswith(("-", "+")):
            i += 2 if token in value_taking else 1
        else:
            pos += 1
            i += 1
    return pos


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
    completer = get_positional_completer(cmd.completers, pos_idx)
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


def _one_token(value: str) -> str:
    """*value* as the single token it holds, unquoted — ``""`` if it holds more.

    Used to compare a history candidate with a completer's candidates, which
    live in the same anchored space but are stored unquoted (``lineedit._apply``
    quotes them on insert, where a verbatim history value is already shell
    syntax).  So ``'My Documents/'`` and ``My Documents/`` are recognised as the
    same one token, while ``spaces --max 5`` is not one token at all.
    """
    try:
        parts = shlex.split(value)
    except ValueError:          # unbalanced quote — compare the raw text
        return value.strip()
    return parts[0] if len(parts) == 1 else ""


def _drop_history_duplicates(
    history: list[Completion], completions: list[Completion]
) -> list[Completion]:
    """Drop history rows that only repeat a candidate the completer offers.

    Both lists are anchored at :func:`parsing.raw_token_start`, so a one-token
    history entry and the command's own candidate for that token insert the very
    same text — ``awsut sagemaker studio <TAB>`` listed ``spaces`` twice, once
    tagged ``history``, for two rows that did the same thing.  The history row is
    the one to lose: it is the derived one, and the completer's carries the
    description (``List the spaces in a domain``) that makes the row worth
    reading.

    Only *exact* duplicates go.  ``spaces --max 5`` keeps its row, because no
    per-argument completer can produce it — spanning several arguments is the
    whole reason history is in the list.
    """
    if not history or not completions:
        return history
    offered = {c.value for c in completions if not c.verbatim}
    if not offered:
        return history
    def duplicate(value: str) -> bool:
        if value in offered:
            return True
        token = _one_token(value)
        return bool(token) and token in offered

    return [h for h in history if not duplicate(h.value)]


class PipelineSlot(PythonCommandSlot):
    """Background slot whose work unit is a parsed pipeline, not a single Python command.

    Subclasses :class:`PythonCommandSlot` so the run-loop's resume path
    (``isinstance(slot, PythonCommandSlot)``) treats it the same as a
    Python-command slot — proxy-buffered output, no PTY, ``Ctrl+]`` switching.

    Body output capture works at the kernel fd level via an OS pipe, not the
    Python ``sys.stdout`` abstraction.  The slot thread sets
    ``_in_pipeline.flag = True`` so :meth:`Shell._run_pipeline_from_decorator`
    takes the ``_in_outer_pipe=True`` branch, which forces single-stage
    external commands onto the ``subprocess.Popen`` path with stdout/stdin
    bound to our pipe and ``/dev/null`` respectively.  Without that, a body
    like ``df`` would allocate a real PTY-backed :class:`ProcessSlot` whose
    reader writes straight to fd 1 (the real terminal), bypassing all of
    ``@bg``'s capture and racing the main thread for stdin.
    """

    def __init__(self, pipeline: "Pipeline", display_text: str) -> None:
        # Bypass PythonCommandSlot.__init__ — it expects a Command, which we
        # don't have.  We mirror its attribute set ourselves.  ``argv`` is what
        # the context list / picker shows as the "command line" for the slot.
        self._init_exit_callback()
        self._cmd = None  # never used; kept so ``_pty_lock``/etc. branches are safe
        self._raw_args: list[str] = []
        self.argv: list[str] = [display_text]
        self._pipeline = pipeline
        self._thread: threading.Thread | None = None
        # Unused: PipelineSlot captures output via an OS pipe (see below) so
        # external subprocess fd-1 writes are caught.  The base-class
        # methods that touch _proxy (activate/deactivate/replay_buffer)
        # are overridden here.
        self._proxy: _StdoutProxy | None = None
        self._exit_exception: BaseException | None = None
        self._finished = threading.Event()
        self.buffer = _NullBuffer()
        self.exit_code: int | None = None
        # Inherited PTY-passthrough state (passthrough_run() is unreachable
        # from a piped @bg body, but the base class's resize/write_stdin
        # still inspect these).
        self._pty_master_fd: int = -1
        self._pty_subproc: subprocess.Popen | None = None
        self._pty_reader: threading.Thread | None = None
        self._pty_buffer = OutputBuffer()
        self._pty_last_byte: bytes = b"\n"
        self._pty_active = False
        self._pty_lock = threading.Lock()
        self._input_request = threading.Event()
        self._input_released = threading.Event()
        self._input_resume = threading.Event()
        self._input_interrupted = threading.Event()
        self._keybuf: bytearray = bytearray()
        self._keybuf_lock = threading.Lock()
        self._keybuf_event = threading.Event()
        # Body output capture: an OS pipe so external subprocess output goes
        # to a fd we own rather than the real terminal.  The reader thread
        # buffers bytes while the slot is inactive and streams them live to
        # stdout (with raw-mode \n→\r\n conversion) once the user switches in.
        self._out_lock = threading.Lock()
        self._out_buffer = OutputBuffer()
        self._out_active = False
        self._out_raw_mode = False
        self._out_read_fd: int = -1
        self._out_write_fd: int = -1
        self._out_reader_thread: threading.Thread | None = None
        # Stdin sink so external commands in the body don't block on the
        # real terminal (which the main thread is reading in cooked mode).
        self._in_devnull_fd: int = -1

    def start(self) -> None:
        self.mark_started()
        self._out_read_fd, self._out_write_fd = os.pipe()
        self._in_devnull_fd = os.open(os.devnull, os.O_RDONLY)
        self._out_reader_thread = threading.Thread(
            target=self._out_reader_loop,
            name=f"bg-reader-{self.argv[0][:24]}",
            daemon=True,
        )
        self._out_reader_thread.start()
        self._thread = threading.Thread(
            target=self._run,
            name=f"bg-{self.argv[0][:32]}",
            daemon=True,
        )
        self._thread.start()

    def _out_reader_loop(self) -> None:
        try:
            while True:
                try:
                    data = os.read(self._out_read_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                with self._out_lock:
                    if self._out_active:
                        chunk = data.replace(b"\n", b"\r\n") if self._out_raw_mode else data
                        try:
                            sys.stdout.buffer.write(chunk)
                            sys.stdout.buffer.flush()
                        except OSError:
                            pass
                    else:
                        self._out_buffer.append(data)
        finally:
            try:
                os.close(self._out_read_fd)
            except OSError:
                pass
            self._out_read_fd = -1

    def _run(self) -> None:
        # Force the worker into the outer-pipe execution path so single-stage
        # external bodies use subprocess.Popen with our pipe fd rather than
        # _execute_external's PTY-backed ProcessSlot.
        _in_pipeline.flag = True
        out_obj = os.fdopen(self._out_write_fd, "wb", buffering=0, closefd=False)
        out_wrapper = io.TextIOWrapper(
            out_obj, encoding="utf-8", errors="replace", write_through=True
        )
        in_obj = os.fdopen(self._in_devnull_fd, "rb", buffering=0, closefd=False)
        in_wrapper = io.TextIOWrapper(in_obj, encoding="utf-8", errors="replace")
        if hasattr(sys.stdout, "set_override"):
            sys.stdout.set_override(out_wrapper)
        if hasattr(sys.stderr, "set_override"):
            sys.stderr.set_override(out_wrapper)
        if hasattr(sys.stdin, "set_override"):
            sys.stdin.set_override(in_wrapper)
        _current_slot.slot = self
        try:
            code = self._pipeline.run()
            self.exit_code = code if isinstance(code, int) else 0
        except SystemExit as e:
            self._exit_exception = e
        except KeyboardInterrupt as e:
            self._exit_exception = e
        except Exception as e:
            self._exit_exception = e
        finally:
            _current_slot.slot = None
            if hasattr(sys.stdout, "clear_override"):
                sys.stdout.clear_override()
            if hasattr(sys.stderr, "clear_override"):
                sys.stderr.clear_override()
            if hasattr(sys.stdin, "clear_override"):
                sys.stdin.clear_override()
            _in_pipeline.flag = False
            try:
                out_wrapper.close()
            except Exception:
                pass
            try:
                in_wrapper.close()
            except Exception:
                pass
            # Close our pipe write end so the reader thread sees EOF and
            # drains.  closefd=False on the wrappers keeps us in charge of
            # the underlying fd lifetimes.
            if self._out_write_fd >= 0:
                try:
                    os.close(self._out_write_fd)
                except OSError:
                    pass
                self._out_write_fd = -1
            if self._in_devnull_fd >= 0:
                try:
                    os.close(self._in_devnull_fd)
                except OSError:
                    pass
                self._in_devnull_fd = -1
            if self._out_reader_thread is not None:
                self._out_reader_thread.join(timeout=1.0)
            if self.exit_code is None:
                self.exit_code = self._compute_exit_code()
            self._finished.set()
            self._fire_on_exit()

    def activate(self, raw_mode: bool = False) -> None:
        with self._out_lock:
            chunks = self._out_buffer.drain()
            self._out_active = True
            self._out_raw_mode = raw_mode
        for chunk in chunks:
            if raw_mode:
                chunk = chunk.replace(b"\n", b"\r\n")
            try:
                sys.stdout.buffer.write(chunk)
            except OSError:
                pass
        try:
            sys.stdout.buffer.flush()
        except OSError:
            pass

    def deactivate(self) -> None:
        with self._out_lock:
            self._out_active = False
            self._out_raw_mode = False

    def replay_buffer(self) -> None:
        # Cooked-mode replay (no \n→\r\n conversion): used by run() once the
        # slot has exited and the terminal is back in cooked mode.
        chunks = self._out_buffer.drain()
        for chunk in chunks:
            try:
                sys.stdout.buffer.write(chunk)
            except OSError:
                pass
        try:
            sys.stdout.buffer.flush()
        except OSError:
            pass

    def tail_lines(self, n: int) -> list[str]:
        """Return up to *n* most recent buffered output lines for preview."""
        from .process import _tail_lines_from_bytes
        return _tail_lines_from_bytes(self._out_buffer.peek(), n)


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
        # background context — see _execute / _notify_when_backgrounded.
        self._backgrounded = False
        self._register_builtins()
        self.registry.mark_builtins()
        var_registry.mark_builtins()
        from .decorators import registry as decorator_registry
        decorator_registry.mark_builtins()
        self._load_user_config()
        # Install thread-local stdio routers so Python command threads can
        # rebind their own stdin/stdout/stderr (for buffering proxies or pipe
        # ends) without disturbing the main thread.
        if not isinstance(sys.stdout, _ThreadLocalStdout):
            sys.stdout = _ThreadLocalStdout(sys.stdout)
        if not isinstance(sys.stdin, _ThreadLocalStdin):
            sys.stdin = _ThreadLocalStdin(sys.stdin)
        if not isinstance(sys.stderr, _ThreadLocalStderr):
            sys.stderr = _ThreadLocalStderr(sys.stderr)

        history_path = config_dir() / "history"
        history_path.parent.mkdir(parents=True, exist_ok=True)

        history = History(history_path)
        # Seed the default context's per-session Up/Down list from the global
        # on-disk history. New contexts snapshot their parent's list at create
        # time; Ctrl-R always searches the global store (self._history).
        default_ctx = self.context_manager.current()
        if default_ctx is not None:
            default_ctx.history = list(history.entries)
        self._line_editor = LineEditor(
            history=history,
            get_completions=self._get_completions,
            get_prompt=lambda: get_prompt_func()(self.context_manager),
            switch_fn=self._handle_switch,
            get_arg_info=self._get_arg_info,
            local_history_fn=self._current_context_history,
        )

        self._command_completer = CommandNameCompleter(self.registry)
        self._file_completer = FileCompleter()
        self._var_completer = VarCompleter()
        # Whole-line suggestions from the *current context's* history — the same
        # list Up/Down walks, so TAB recall and arrow recall agree on scope.
        # (Ctrl+R is the one that searches the global store.)  Scoped further to
        # the lines recorded as run in the cwd; the directory side table is
        # global, so it keeps working for a context's inherited entries.
        self._history_completer = HistoryCompleter(
            self._current_context_history, ran_here_fn=history.ran_here
        )

        # Wire Pipeline.run() so decorator bodies can re-enter execution.
        set_pipeline_executor(self._run_pipeline_from_decorator)
        # Wire @bg so it can ask the running shell for a new background slot.
        from .decorators import set_background_runner
        set_background_runner(self._run_in_background)

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
        from .decorators import registry as decorator_registry

        # Case A: ``@<partial>`` — completing the decorator name itself.
        if not tokens and prefix.startswith("@"):
            partial = prefix[1:]
            decos = decorator_registry.list_decorators()
            completions = [
                Completion(
                    value=f"@{name}",
                    display=f"@{name}",
                    description=decorator_registry.get(name).description,
                )
                for name in sorted(decos)
                if name.startswith(partial)
            ]
            return "return", (completions, prefix, "decorator")

        # Case B/C: a decorator name has already been typed.
        if not tokens or not tokens[0].startswith("@"):
            return None
        deco_name = tokens[0][1:]
        deco = decorator_registry.get(deco_name)
        if deco is None:
            return None  # unknown decorator; fall through (treats it like a command)

        # Walk past the decorator's flags to find where the body starts.
        i = 1
        body_start = 1
        while i < len(tokens):
            tok = tokens[i]
            if not tok.startswith(("-", "+")):
                body_start = i
                break
            if tok == "--":
                body_start = i + 1
                break
            i += 1
            # If the flag we just consumed takes a value, consume the value too.
            if (
                i < len(tokens)
                and "=" not in tok
                and decorator_registry.flag_takes_value(deco_name, tok)
            ):
                i += 1
            body_start = i
        else:
            body_start = len(tokens)

        # Case B: cursor is on a decorator-flag token (``@watch -<TAB>``).
        if prefix.startswith(("-", "+")) and body_start >= len(tokens):
            options_completer = deco.completers.get(None)
            if options_completer is None:
                return "return", ([], prefix, f"@{deco_name}")
            ctx = CompletionContext(
                command=f"@{deco_name}",
                args=tokens[1:],
                arg_index=0,
                prefix=prefix,
                line=line_before_cursor,
                shell_context=self.context_manager.current(),
            )
            if options_completer.should_activate(ctx):
                return "return", (options_completer.complete(ctx), prefix, f"@{deco_name} option")
            return "return", ([], prefix, f"@{deco_name} option")

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
        """Return ``(completions, prefix, status_label)`` for the cursor position.

        Wraps the completer-driven candidates from :meth:`_get_base_completions`
        with history suggestions (see :class:`HistoryCompleter`) — the tails of
        past command lines that continue what is typed — listed first so "what I
        ran before" is the top candidate.

        A history row that only repeats a candidate the command's own completer
        already offers is dropped — see :func:`_drop_history_duplicates`.

        A unique token candidate still auto-applies without showing the picker
        (that check lives in ``lineedit._complete`` and counts only non-history
        candidates), so history stays one more TAB away in that case rather
        than costing a keystroke on every completion.
        """
        completions, prefix, label = self._get_base_completions(line_before_cursor)

        history = self._history_completer.complete(CompletionContext(
            command=None,
            args=[],
            arg_index=0,
            prefix=prefix,
            line=line_before_cursor,
            shell_context=self.context_manager.current(),
        ))
        history = _drop_history_duplicates(history, completions)
        if not history:
            return completions, prefix, label
        # With no completer candidates the picker is showing history and nothing
        # else — say so rather than labelling it with the command name.
        return history + completions, prefix, (label if completions else "history")

    def _get_base_completions(self, line_before_cursor: str) -> tuple[list[Completion], str, str]:
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
                    shell_context=self.context_manager.current(),
                )
                return self._var_completer.complete(ctx), prefix, "variable"

            ctx = CompletionContext(
                command=None,
                args=[],
                arg_index=0,
                prefix=prefix,
                line=line_before_cursor,
                shell_context=self.context_manager.current(),
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
            shell_context=self.context_manager.current(),
        )

        cmd = self.registry.get(command_name)

        # Tree-shaped command: resolve to the current node, then offer
        # sub-command names / positionals / inherited flags.
        if cmd is not None and cmd.children:
            node, remaining_args = cmd.resolve(args)
            # Update ctx so positional completers see the correct index
            # (the position relative to the resolved node).
            tree_ctx = CompletionContext(
                command=command_name,
                args=remaining_args,
                arg_index=len(remaining_args),
                prefix=prefix,
                line=line_before_cursor,
                shell_context=self.context_manager.current(),
            )
            return self._complete_tree_node(node, tree_ctx, prefix)

        completers_dict = cmd.completers if cmd else None

        has_completer = False
        completions: list[Completion] = []
        label = command_name  # default: just show the command name

        if completers_dict:
            options_completer = completers_dict.get(None)
            pos_idx = _positional_index(args, options_completer)
            positional_completer = get_positional_completer(completers_dict, pos_idx)

            # When the last arg is a value-taking flag (e.g. "du -d <TAB>"),
            # the slot belongs to its value: offer the flag's value completer,
            # or nothing — never positional/file candidates.  With nothing to
            # offer, the status bar (``_get_arg_info``) already says what to
            # type.  Skip when the user is already typing another flag (prefix
            # starts with "-" or "+").
            if (options_completer and ctx.args and not ctx.prefix.startswith(("-", "+"))
                    and hasattr(options_completer, "get_preceding_flag_hint")):
                hint_info = options_completer.get_preceding_flag_hint(ctx)
                if hint_info:
                    flag, arg_hint, description, value_completer = hint_info
                    flag_label = _flag_label(flag, arg_hint, description)
                    if value_completer:
                        # Flag has a dedicated value completer (e.g. -C DIR → DirCompleter).
                        return value_completer.complete(ctx), ctx.prefix, flag_label
                    return [], ctx.prefix, flag_label

            # Options completer takes priority when typing a flag-prefixed token.
            if options_completer and ctx.prefix.startswith(("-", "+")):
                has_completer = True
                if options_completer.should_activate(ctx):
                    completions = options_completer.complete(ctx)
                    label = f"{command_name} option"

            # Positional completer as fallback (or primary when no "-" prefix).
            if not completions and positional_completer:
                has_completer = True
                if positional_completer.should_activate(ctx):
                    completions = positional_completer.complete(ctx)
                    label = _positional_label(cmd, pos_idx, command_name, args)

        # argcomplete fallback: the de-facto Python CLI completion library
        # (pipx, conda, pre-commit, tox, pdm, httpie, …).  Detection is done
        # by inspecting the script for the ``PYTHON_ARGCOMPLETE_OK`` marker,
        # so it never invokes side-effecting tools blindly.  Only where no
        # completer is registered: one that returned ``[]`` meant "nothing".
        # (Cobra tools have no fallback — they are opted in per command as a
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
        # _get_base_completions.
        expansion = self.registry.get_alias(command_name)
        if expansion is not None:
            expansion_tokens = tokenize(expansion)
            if expansion_tokens:
                command_name = expansion_tokens[0]
                preceding_args = expansion_tokens[1:] + preceding_args

        cmd = self.registry.get(command_name)
        if cmd is None:
            return None

        # Resolve to the right node and options_completer.
        if cmd.children:
            node, remaining_args = cmd.resolve(preceding_args)
            options_completer = node.merged_options_completer()
        else:
            node = cmd
            remaining_args = preceding_args
            options_completer = cmd.completers.get(None)

        if token.startswith(("-", "+")):
            # ── Flag token ────────────────────────────────────────────────────
            if options_completer is None or not hasattr(options_completer, "options"):
                return None
            desc = options_completer.options.get(token, "")
            arg_hint = getattr(options_completer, "args", {}).get(token, "")
            result = _flag_label(token, arg_hint, desc)
            return result if result != token else None

        # ── Non-flag token: positional arg or value for a preceding flag ───────

        value_taking = set(getattr(options_completer, "args", {})) if options_completer else set()
        if remaining_args and remaining_args[-1].startswith(("-", "+")) and remaining_args[-1] in value_taking:
            # Caret is on a flag value — show the flag's description.
            flag = remaining_args[-1]
            desc = getattr(options_completer, "options", {}).get(flag, "")
            arg_hint = getattr(options_completer, "args", {}).get(flag, "")
            result = _flag_label(flag, arg_hint, desc)
            return result if result != flag else None

        # Caret is on a positional arg.
        pos_idx = _positional_index(remaining_args, options_completer)

        # For tree commands: if this slot is a sub-command name, show its
        # description (or fall back to "<cmd> subcommand" for a partial token
        # that doesn't yet match a child — mirrors _complete_tree_node).
        if node.children and pos_idx == 0:
            if token in node.children:
                child = node.children[token]
                if child.description:
                    return f"{command_name} {token}: {child.description}"
                return f"{command_name} {token}"
            return f"{command_name} subcommand"

        return _positional_label(node, pos_idx, command_name, remaining_args)

    def _complete_tree_node(self, node, ctx, prefix):
        """Compute completions when the user is typing within a sub-command tree.

        *node* is the resolved node (the deepest sub-command in ctx.args).
        *ctx.args* are the tokens remaining after stripping consumed
        sub-command names; *ctx.arg_index* reflects that.
        """
        cmd_name = ctx.command or ""
        # Build a merged options completer (this node + ancestors).
        merged_options = node.merged_options_completer()

        # Preceding value-taking flag known at this node or an ancestor: the
        # slot belongs to its value — its value completer, or nothing (the
        # status bar says what to type).
        if (merged_options and ctx.args and not ctx.prefix.startswith("-")
                and hasattr(merged_options, "get_preceding_flag_hint")):
            hint_info = merged_options.get_preceding_flag_hint(ctx)
            if hint_info:
                flag, arg_hint, description, value_completer = hint_info
                flag_label = _flag_label(flag, arg_hint, description)
                if value_completer:
                    return value_completer.complete(ctx), ctx.prefix, flag_label
                return [], ctx.prefix, flag_label

        # Typing a flag → offer all flags from this node + ancestors.
        if ctx.prefix.startswith("-"):
            if merged_options and merged_options.should_activate(ctx):
                return merged_options.complete(ctx), prefix, f"{cmd_name} option"
            return [], prefix, f"{cmd_name} option"

        # Compute positional index relative to this node, ignoring flag tokens
        # and their values.
        pos_idx = _positional_index(ctx.args, merged_options)

        # If this is an interior group, the next positional is a sub-command
        # name.  Offer the children at the leftmost positional slot only —
        # extra positionals after a missing match should fall back to file
        # completion (or this node's own positional completer, if any).
        if node.children and pos_idx == 0:
            results = []
            for name in sorted(node.children):
                child = node.children[name]
                if name.startswith(ctx.prefix):
                    results.append(Completion(value=name, description=child.description))
            return results, prefix, f"{cmd_name} subcommand"

        # Leaf-or-deeper: use the resolved node's own positional completers.
        positional_completer = get_positional_completer(node.completers, pos_idx)
        if positional_completer is not None and positional_completer.should_activate(ctx):
            return positional_completer.complete(ctx), prefix, _positional_label(node, pos_idx, cmd_name, ctx.args)

        # If the node has no positional completer registered for this slot,
        # fall back to file completion only when the node has no positional
        # completers at all (i.e. it didn't declare any positionals).  This
        # matches the flat-command behaviour where a registered completer
        # returning [] suppresses file fallback.
        if not any(k is not None for k in node.completers):
            return self._file_completer.complete(ctx), prefix, cmd_name
        return [], prefix, cmd_name

    def _register_builtins(self) -> None:
        from .completion import (
            CallbackCompleter, ChoiceCompleter, Completer, Completion, DirCompleter,
        )

        @self.registry.command(
            name="cd",
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

        @self.registry.command(name="exit", help="Exit the shell.")
        def exit_shell():
            running = self._running_contexts()
            if running and not self._confirm_exit(running):
                return
            raise SystemExit(0)

        @self.registry.command(name="reload", help="Reload ~/.eosh/config.py.")
        def reload_config():
            from . import recipes
            self.registry.clear_user_commands()
            recipes.skipped_recipes.clear()
            var_registry.clear_user_vars()
            set_prompt(None)
            self._load_user_config()
            print("Config reloaded.")

        @self.registry.command(
            name="var",
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
                return
            if command is not None:
                body = command
            elif script:
                path = os.path.expanduser(script[0])
                if not os.path.isfile(path):
                    print(f"source-bash: {script[0]}: no such file")
                    return
                body = " ".join(shlex.quote(a) for a in ["source", path, *script[1:]])
            else:
                body = passthrough_input_block(
                    "Paste bash lines; end with a blank line or Ctrl+D:\n"
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
                return

            changed, removed, new_cwd = self._apply_bash_env(
                cwd, env, import_cwd=not no_cd
            )
            if code != 0:
                print(f"source-bash: exit status {code}")
            if quiet:
                return
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

        @self.registry.command(
            name="alias",
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
                print("Available commands:")
                for name in sorted(self.registry.list_commands()):
                    cmd = self.registry.get(name)
                    desc = cmd.help_text.split("\n")[0] if cmd.help_text else ""
                    print(f"  {name:20s} {desc}")

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
            help="Manage shell contexts: push, pop, switch, list, kill.",
            params=[
                arg("subcommand", nargs="?", default="",
                    completer=ChoiceCompleter(["push", "pop", "switch", "list", "kill"])),
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

            if subcommand == "push":
                if not name:
                    print("Usage: context push <name>")
                    return
                if name in self.context_manager.contexts:
                    print(f"Context '{name}' already exists.")
                    return
                parent = self.context_manager.current()
                inherited = dict(parent.variables) if parent else {}
                inherited_history = list(parent.history) if parent else []
                self.context_manager.create(
                    name, variables=inherited, history=inherited_history
                )
                self.context_manager.push(name)
                print(f"Pushed context '{name}'")

            elif subcommand == "pop":
                ctx = self.context_manager.current()
                if ctx is None:
                    print("No active context.")
                    return
                if len(self.context_manager.list_contexts()) <= 1:
                    print("Cannot remove the last context.")
                    return
                popped_name = ctx.name
                self.context_manager.pop()
                self.context_manager.remove(popped_name)
                prev = self.context_manager.current()
                if prev is None:
                    remaining = self.context_manager.list_contexts()
                    if remaining:
                        self.context_manager.switch(remaining[0])
                        prev = self.context_manager.current()
                if prev:
                    print(f"Popped '{popped_name}', now in '{prev.name}'")
                else:
                    print(f"Popped '{popped_name}'")

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

        # Register built-in pipeline decorators.
        from .decorators import enable as enable_decorators
        enable_decorators("watch", "time", "retry", "quiet", "bg")

        # `var notify=off` / `var notify_threshold=30`.
        notify.register_vars()

    def _load_user_config(self) -> None:
        config_path = config_dir() / "config.py"
        if not config_path.exists():
            # First launch: write the starter config, then load it like any
            # other — a fresh install should not need a restart to get recipes.
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(_DEFAULT_CONFIG_PATH.read_text())

        import importlib.util
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

    @contextlib.contextmanager
    def _temp_environ(self, env_prefix: dict[str, str]):
        """Apply *env_prefix* to ``os.environ`` for the duration of the block.

        Used to give a Python ``@registry.command`` the same per-command
        environment an external child gets from ``FOO=bar cmd``.  Restores the
        prior values (or deletes keys that weren't set before) on exit.

        In-process caveat: a Python stage in a *multi-stage* pipeline runs in a
        worker thread, so a temporary mutation of the process-wide
        ``os.environ`` is visible to sibling stages for the overlap window.
        This mirrors the existing "stateful built-ins mutate the parent in
        pipelines" limitation and only bites the rare ``FOO=bar pycmd | …``
        case; external children are unaffected (they get their own ``env=``).
        """
        if not env_prefix:
            yield
            return
        saved: dict[str, str | None] = {}
        for key, value in env_prefix.items():
            saved[key] = os.environ.get(key)
            os.environ[key] = value
        try:
            yield
        finally:
            for key, prev in saved.items():
                if prev is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = prev

    def _unset_variable(self, key: str) -> None:
        """Remove a variable via var_registry, or fall back to plain os.environ / context removal."""
        py_var = var_registry.get(key)
        if py_var is not None:
            py_var.unset()
            for env_key in py_var.env_keys:
                self.context_manager.unset_variable(env_key)
        else:
            self.context_manager.unset_variable(key)

    def _set_variable(self, key: str, value: str) -> None:
        """Dispatch a KEY=VALUE assignment through var_registry, or fall back to plain os.environ.

        When a registered Var handles the key its env_keys are registered with
        the context manager first (so original values are captured as the
        save/restore backup), then Var.set() is called to apply the change.
        """
        py_var = var_registry.get(key)
        if py_var is not None:
            for env_key in py_var.env_keys:
                self.context_manager.set_variable(env_key, os.environ.get(env_key, value))
            py_var.set(value)
            # Sync context's stored value to what set() actually wrote.
            ctx = self.context_manager.current()
            if ctx is not None:
                for env_key in py_var.env_keys:
                    ctx.variables[env_key] = os.environ.get(env_key, value)
        else:
            self.context_manager.set_variable(key, value)

    def _run_bash_script(self, body: str) -> tuple[int, str | None, dict[str, str]]:
        """Run *body* in a child bash and read back its final cwd + environment.

        Returns ``(exit_code, cwd, env)``.  ``cwd`` is None (and ``env`` empty)
        when the child never reached the dump — the caller reports that rather
        than importing an empty environment over the live one.

        The child runs through :func:`passthrough_run` so a script that prompts
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
            code = passthrough_run([bash, "-c", script])
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

    def _execute(self, line: str) -> None:
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
        try:
            for op, pipeline in seq.items:
                if op == "&&" and last_exit != 0:
                    continue
                if op == "||" and last_exit == 0:
                    continue
                last_exit = self._execute_pipeline(pipeline)
        finally:
            if not self._backgrounded:
                notify.command_done(line, time.monotonic() - started, last_exit)
            # User-run commands may have mutated remote state (e.g.
            # ``awsut sagemaker hyperpod scale``); drop cached completer
            # fetches so the next TAB session re-queries.
            from . import completion_cache
            completion_cache.invalidate_all()

    # --- long-command notifications ------------------------------------------

    def _notify_when_backgrounded(self, slot) -> None:
        """Arrange a desktop notification for *slot* finishing out of sight.

        Called right after the slot is parked on a context (``Ctrl+]`` or
        ``@bg``).  The callback runs on the slot's own thread, so it must not
        touch the terminal — :func:`notify.command_done` only spawns a
        notification helper.
        """
        slot.arm_exit_callback(lambda: self._notify_slot_done(slot))

    def _notify_slot_done(self, slot) -> None:
        """Notify that a backgrounded *slot* finished, if the user isn't watching.

        The owning context is looked up rather than remembered: a slot can be
        moved between contexts, and "which context holds it *now*" is exactly
        the question that decides whether the user saw it finish.  A slot with
        no owner ran in the foreground (``_execute`` already timed it), and a
        slot owned by the current context finished on screen — neither needs a
        popup.
        """
        owner = next(
            (
                name
                for name, ctx in self.context_manager.contexts.items()
                if ctx.process_slot is slot
            ),
            None,
        )
        if owner is None or owner == self.context_manager.current_name:
            return
        notify.command_done(
            " ".join(slot.argv), slot.elapsed(), slot.exit_code or 0, context=owner
        )

    def _notify_resumed_done(self, slot) -> None:
        """Notify for a slot that was resumed into the foreground and then ended.

        Its exit callback stayed silent (the slot's context was current by
        then), and ``_execute`` returned when the slot was backgrounded, so
        this is the only path that can report the total runtime.
        """
        notify.command_done(" ".join(slot.argv), slot.elapsed(), slot.exit_code or 0)

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

    def _run_in_background(self, pipeline: Pipeline, *, name: str | None = None) -> str:
        """Spawn *pipeline* in a new background context; return the context name.

        Used by the ``@bg`` decorator via the ``set_background_runner`` hook.
        Refuses outer-pipeline composition: ``@bg`` returns immediately, so a
        downstream ``| next`` stage would have nothing to read.
        """
        if getattr(_in_pipeline, "flag", False):
            raise ValueError(
                "@bg cannot be used as a stage of an outer pipeline "
                "(the decorator returns immediately, so downstream stages would block)"
            )
        existing = self.context_manager.contexts
        if name is None:
            i = 1
            while f"bg-{i}" in existing:
                i += 1
            name = f"bg-{i}"
        else:
            ctx = existing.get(name)
            if ctx is not None and ctx.process_slot and ctx.process_slot.is_alive():
                raise ValueError(
                    f"context '{name}' already has a running process; "
                    f"choose a different name or kill the existing one first"
                )

        display = " | ".join(s.text for s in pipeline.stages) or f"@bg {name}"
        slot = self._make_background_slot(pipeline, display)
        if slot is None:
            # Failed to start an external ProcessSlot (FileNotFoundError etc.);
            # the helper already printed an error.  Don't create the context.
            raise ValueError(f"@bg: failed to start '{display}'")

        if name in existing:
            target = existing[name]
            target.process_slot = slot
        else:
            parent = self.context_manager.current()
            inherited = dict(parent.variables) if parent else {}
            inherited_history = list(parent.history) if parent else []
            target = self.context_manager.create(
                name, variables=inherited, history=inherited_history
            )
            target.process_slot = slot
        # ``@bg`` returns immediately, so the enclosing line's own timing says
        # nothing about the body; the slot's exit callback is what reports it.
        self._backgrounded = True
        self._notify_when_backgrounded(slot)
        return name

    def _make_background_slot(self, pipeline: Pipeline, display: str):
        """Return a started slot for *pipeline*, or ``None`` on startup error.

        Three-way dispatch matches the foreground execution path for the
        same body shape, so resume after ``Ctrl+]`` works the same as if
        the user had backgrounded a foreground job:

        * **Single-stage external** (``@bg vi``) → :class:`ProcessSlot`
          (real PTY).  Interactive TUIs work; alt-screen restore works.
        * **Single-stage Python command** (``@bg hp ssm xyz``) →
          :class:`PythonCommandSlot`.  ``_in_pipeline.flag`` stays ``False``
          so :func:`passthrough_run` can allocate its own PTY for the
          subprocess just like in the foreground path.
        * **Anything else** — multi-stage pipelines, decorator-bodied
          stages, redirected bodies → :class:`PipelineSlot` (OS pipe +
          ``subprocess.Popen``).

        On Windows ``ProcessSlot`` and ``PythonCommandSlot``'s passthrough
        machinery are unavailable, so everything goes through
        ``PipelineSlot`` as before.
        """
        if not IS_WINDOWS:
            ext = self._pipeline_external_argv(pipeline)
            if ext is not None:
                argv, env_prefix = ext
                slot = ProcessSlot()
                try:
                    slot.start(argv=argv, env=self._merged_env(env_prefix), cwd=os.getcwd())
                except FileNotFoundError:
                    print(f"eosh: command not found: {argv[0]}")
                    return None
                except OSError as e:
                    print(f"eosh: {e}")
                    return None
                return slot

            py = self._pipeline_python_command(pipeline)
            if py is not None:
                cmd, args = py
                slot = PythonCommandSlot(cmd, args)
                slot.start()
                return slot

        slot = PipelineSlot(pipeline, display)
        slot.start()
        return slot

    def _pipeline_python_command(self, pipeline: Pipeline):
        """Return ``(cmd, args)`` if *pipeline* is a single-stage Python command.

        Same gating as :meth:`_pipeline_external_argv` but inverted on the
        registry check: returns the registered :class:`Command` (with its
        Python handler) rather than raw argv.  A per-command env prefix is not
        threaded here — a backgrounded Python command runs in a long-lived
        slot thread, so a temporary ``os.environ`` mutation can't be scoped to
        it; ``@bg FOO=bar pycmd`` therefore falls through to ``PipelineSlot``.
        """
        if type(pipeline).run is not Pipeline.run:
            return None
        if len(pipeline.stages) != 1:
            return None
        stage = pipeline.stages[0]
        if stage.decorator is not None:
            return None
        if stage.redirects:
            return None
        tokens = self._tokenize_stage(stage)
        if not tokens:
            return None
        if all(self._ASSIGNMENT_RE.match(t) for t in tokens):
            return None
        env_prefix, tokens = self._split_env_prefix(tokens)
        if env_prefix:
            # Let PipelineSlot handle it — it applies the prefix per-stage.
            return None
        cmd = self.registry.get(tokens[0])
        if cmd is None or not cmd.has_any_handler():
            return None
        return cmd, tokens[1:]

    def _pipeline_external_argv(self, pipeline: Pipeline):
        """Return ``(argv, env_prefix)`` if *pipeline* is a single-stage external command.

        Returns ``None`` for any pipeline that needs the PipelineSlot path:
        multi-stage, decorator-bodied, redirected, pure-assignment, empty,
        or whose first token resolves to a registered Python handler.
        Subclasses that override ``Pipeline.run`` (e.g. test helpers) also
        take the PipelineSlot path so the override actually runs.

        A leading ``FOO=bar`` env prefix is split off and returned separately
        so the caller can bake it into the ProcessSlot's environment.
        """
        if type(pipeline).run is not Pipeline.run:
            return None
        if len(pipeline.stages) != 1:
            return None
        stage = pipeline.stages[0]
        if stage.decorator is not None:
            return None
        if stage.redirects:
            return None
        tokens = self._tokenize_stage(stage)
        if not tokens:
            return None
        if all(self._ASSIGNMENT_RE.match(t) for t in tokens):
            return None
        env_prefix, tokens = self._split_env_prefix(tokens)
        cmd = self.registry.get(tokens[0])
        # Recipes are registered as commands but have no Python handler —
        # they fall through to the system-command path, which is exactly
        # what we want a ProcessSlot for.
        if cmd is not None and cmd.has_any_handler():
            return None
        return tokens, env_prefix

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
        # _PyStageHandle objects (see _start_python_stage_thread).
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
                worker = self._start_decorator_stage_thread(
                    decorator_call=stage.decorator,
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
                worker = self._start_python_stage_thread(
                    cmd=cmd,
                    args=tokens[1:],
                    stdin_fd=stdin_fd_pipe if stdin_pipe_used else None,
                    stdout_fd=stdout_fd_pipe if stdout_pipe_used else None,
                    stdin_file=stdin_file,
                    stdout_file=stdout_file,
                    stderr_dst=stderr_dst,
                    env_prefix=env_prefix,
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

    def _start_python_stage_thread(
        self,
        *,
        cmd,
        args: list[str],
        stdin_fd: int | None,
        stdout_fd: int | None,
        stdin_file,
        stdout_file,
        stderr_dst,
        env_prefix: dict[str, str] | None = None,
    ) -> "_PyStageHandle":
        """Run a registered Python command as one stage of a pipeline.

        The thread takes ownership of *stdin_fd* / *stdout_fd* (raw OS pipe
        ends) or, when an explicit redirect is in play, the corresponding
        opened file object.  It binds them to thread-local
        sys.stdin/sys.stdout/sys.stderr for the duration of cmd.invoke().

        *env_prefix* (from a ``FOO=bar pycmd`` stage) is applied to the
        process-wide ``os.environ`` around ``cmd.invoke()``.  See
        :meth:`_temp_environ` for the sibling-visibility caveat this carries
        in a multi-stage pipeline.
        """
        # Decide which underlying object the thread owns.  Exactly one of
        # (stdin_fd, stdin_file) is set when this stage has any stdin source,
        # and similarly for stdout.
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

        err_obj = stderr_dst  # may be a file, "stdout" sentinel (subprocess.STDOUT), or None

        handle = _PyStageHandle(cmd_name=cmd.name)
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

                try:
                    with self._temp_environ(env_prefix or {}):
                        cmd.invoke(args)
                    handle.exit_code = 0
                except SystemExit as e:
                    # Don't propagate; an `exit | cat` should not kill the shell.
                    code = e.code
                    handle.exit_code = code if isinstance(code, int) else (1 if code else 0)
                except BrokenPipeError:
                    handle.exit_code = 0
                except KeyboardInterrupt:
                    handle.exit_code = 130
                except Exception as e:
                    if handle.interrupted:
                        # The parent closed our wrappers as part of Ctrl+C
                        # handling.  Any I/O the worker did afterward will
                        # raise (ValueError: closed file, or OSError) — that's
                        # expected, not an error to report.
                        handle.exit_code = 130
                    else:
                        print(f"{cmd.name}: error: {e}", file=sys.stderr)
                        traceback.print_exc()
                        handle.exit_code = 1
            finally:
                sys.stdin.clear_override()
                sys.stdout.clear_override()
                sys.stderr.clear_override()
                # Flush wrappers so downstream readers see all output before
                # the pipe closes.
                for w in (out_wrapper, err_wrapper):
                    if w is not None:
                        try:
                            w.flush()
                        except Exception:
                            pass
                # Close the wrappers (which closes the underlying fds/files).
                # Order matters: close output first so a reader pipe sees EOF.
                for w in (out_wrapper, err_wrapper, in_wrapper):
                    if w is not None:
                        try:
                            w.close()
                        except Exception:
                            pass
                _in_pipeline.flag = False
                handle.done.set()

        t = threading.Thread(target=_target, name=f"pipe-{cmd.name}", daemon=True)
        handle.thread = t
        t.start()
        return handle

    def _start_decorator_stage_thread(
        self,
        *,
        decorator_call,
        stdin_fd: int | None,
        stdout_fd: int | None,
    ) -> "_PyStageHandle":
        """Run a ``@name`` decorator as one stage of a multi-stage pipeline.

        Mirrors :meth:`_start_python_stage_thread` but invokes the decorator
        function directly rather than dispatching through the command
        registry.  The decorator's body re-enters ``_execute_pipeline`` via
        ``Pipeline.run()``; that nested execution inherits this thread's
        rebound ``sys.stdout`` (the pipe's write end) through the
        thread-local routers, so the body's output flows downstream
        transparently.
        """
        from .decorators import registry as decorator_registry, parse_decorator_args

        in_obj = os.fdopen(stdin_fd, "rb", buffering=0, closefd=True) if stdin_fd is not None else None
        out_obj = os.fdopen(stdout_fd, "wb", buffering=0, closefd=True) if stdout_fd is not None else None

        handle = _PyStageHandle(cmd_name=f"@{decorator_call.name}")
        in_wrapper = io.TextIOWrapper(in_obj, encoding="utf-8", errors="replace") if in_obj is not None else None
        out_wrapper = io.TextIOWrapper(out_obj, encoding="utf-8", errors="replace", write_through=True) if out_obj is not None else None
        for w in (in_wrapper, out_wrapper):
            if w is not None:
                handle._io_objs.append(w)

        deco = decorator_registry.get(decorator_call.name)

        def _target():
            _in_pipeline.flag = True
            try:
                if in_wrapper is not None:
                    sys.stdin.set_override(in_wrapper)
                if out_wrapper is not None:
                    sys.stdout.set_override(out_wrapper)

                if deco is None:
                    print(
                        f"eosh: unknown decorator: @{decorator_call.name}",
                        file=sys.stderr,
                    )
                    handle.exit_code = 127
                    return

                kwargs = parse_decorator_args(deco, decorator_call.flag_tokens)
                if kwargs is None:
                    handle.exit_code = 2
                    return

                try:
                    deco.func(decorator_call.body, **kwargs)
                    handle.exit_code = 0
                except SystemExit as e:
                    code = e.code
                    handle.exit_code = code if isinstance(code, int) else (1 if code else 0)
                except BrokenPipeError:
                    handle.exit_code = 0
                except KeyboardInterrupt:
                    handle.exit_code = 130
                except Exception as e:
                    if handle.interrupted:
                        handle.exit_code = 130
                    else:
                        print(f"@{deco.name}: error: {e}", file=sys.stderr)
                        traceback.print_exc()
                        handle.exit_code = 1
            finally:
                sys.stdin.clear_override()
                sys.stdout.clear_override()
                sys.stderr.clear_override()
                if out_wrapper is not None:
                    try:
                        out_wrapper.flush()
                    except Exception:
                        pass
                for w in (out_wrapper, in_wrapper):
                    if w is not None:
                        try:
                            w.close()
                        except Exception:
                            pass
                _in_pipeline.flag = False
                handle.done.set()

        t = threading.Thread(
            target=_target, name=f"pipe-@{decorator_call.name}", daemon=True
        )
        handle.thread = t
        t.start()
        return handle

    def _execute_decorator_stage(self, stage: Stage) -> int:
        """Invoke a ``@name`` decorator with its parsed body pipeline."""
        # Local import — keeps decorators-package init lazy and avoids any
        # circular-import surprises during module load.
        from .decorators import registry as decorator_registry, parse_decorator_args

        deco_call = stage.decorator
        deco = decorator_registry.get(deco_call.name)
        if deco is None:
            print(f"eosh: unknown decorator: @{deco_call.name}", file=sys.stderr)
            return 127

        kwargs = parse_decorator_args(deco, deco_call.flag_tokens)
        if kwargs is None:
            return 2  # parse error already printed by CmdParser

        try:
            deco.func(deco_call.body, **kwargs)
        except SystemExit:
            raise
        except KeyboardInterrupt:
            print(f"@{deco.name}: interrupted")
            return 130
        except Exception as e:
            print(f"@{deco.name}: error: {e}", file=sys.stderr)
            traceback.print_exc()
            return 1
        return 0

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
        if cmd:
            if IS_WINDOWS:
                # Windows lacks the PTY-backed slot used for thread-based
                # context switching, so run the Python command synchronously.
                # passthrough_run/passthrough_input fall back to direct
                # subprocess.run/input since no slot is registered.
                with self._temp_environ(env_prefix):
                    return self._run_python_command_sync(cmd, command_name, args)
            else:
                # Interactive Python command — run in a thread so Ctrl+] works.
                # The per-command env prefix is applied for the duration of the
                # foreground run.  If the command is backgrounded via Ctrl+]
                # (result == "switched") the prefix is restored here — a
                # backgrounded Python command does not keep the temporary env,
                # matching the in-process-model caveats in doc/limitations.md.
                ctx = self.context_manager.current()
                slot = PythonCommandSlot(cmd, args)
                with self._temp_environ(env_prefix):
                    slot.start()
                    result = self._enter_python_forwarding_mode(slot)
                if result == "switched":
                    slot.deactivate()
                    if ctx is not None:
                        ctx.process_slot = slot
                        self._backgrounded = True
                        self._notify_when_backgrounded(slot)
                    self._handle_switch()
                    return 0
                if result == "interrupted":
                    print(f"{command_name}: interrupted")
                    return 130
                slot.deactivate()
                exc = slot._exit_exception
                if isinstance(exc, SystemExit):
                    raise exc
                if exc is not None and not isinstance(exc, KeyboardInterrupt):
                    print(f"{command_name}: error: {exc}")
                return slot.exit_code or 0

        return self._execute_external(command_name, args, env_prefix=env_prefix)

    def _run_python_command_sync(self, cmd, command_name: str, args: list[str]) -> int:
        """Invoke a Python command on the main thread (Windows path)."""
        try:
            cmd.invoke(args)
        except SystemExit:
            raise
        except KeyboardInterrupt:
            print(f"{command_name}: interrupted")
            return 130
        except TypeError as e:
            print(f"{command_name}: {e}")
            return 1
        except Exception as e:
            print(f"{command_name}: error: {e}")
            traceback.print_exc()
            return 1
        return 0

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
                print(f"eosh: command not found: {command_name}")
                return 127
            except OSError as e:
                print(f"eosh: {e}")
                return 1
        except OSError as e:
            print(f"eosh: {e}")
            return 1

    def _execute_external(
        self, command_name: str, args: list[str], env_prefix: dict[str, str] | None = None
    ) -> int:
        if IS_WINDOWS:
            return self._execute_external_windows(command_name, args, env_prefix)

        ctx = self.context_manager.current()

        slot = ProcessSlot()
        try:
            slot.start(
                argv=[command_name] + args,
                env=self._merged_env(env_prefix or {}),
                cwd=os.getcwd(),
            )
        except FileNotFoundError:
            print(f"eosh: command not found: {command_name}")
            return 127
        except OSError as e:
            print(f"eosh: {e}")
            return 1

        slot.activate()
        slot.replay_buffer()  # flush any output that arrived before activate()
        result = self._enter_forwarding_mode(slot)
        if result == "switched":
            if ctx is None:
                ctx = self.context_manager.current()
            ctx.process_slot = slot
            self._backgrounded = True
            self._notify_when_backgrounded(slot)
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
          • Ctrl+C (\\x03) — forwarded to a passthrough_run() subprocess if
            one is active (so e.g. SSH/SSM see the interrupt); otherwise
            inject KeyboardInterrupt into the command thread.
          • other keys    — forwarded to slot.write_stdin, which writes to
            a passthrough_run() PTY master if active (no-op otherwise).

        If the command thread enters passthrough_input(), the loop restores
        cooked terminal mode and stops reading stdin until the input() call
        returns.  That gives the slot thread direct, line-buffered access
        to the terminal for the prompt.

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

        # SIGINT handler used only while the slot is in passthrough_input().
        # The main loop is waiting on _input_resume; the slot is polling fd 0.
        # Setting _input_interrupted causes the slot's poll loop to raise
        # KeyboardInterrupt and unwind.  Cooked mode's ECHOCTL already
        # prints "^C" on the user's terminal — we just need to bump down a
        # line so the next prompt doesn't overwrite the input line.
        def on_sigint_during_input(signum, frame):
            try:
                os.write(fd, b"\r\n")
            except OSError:
                pass
            slot._input_interrupted.set()

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
            slot.activate(raw_mode=True)

            while slot.is_alive():
                if slot._input_request.is_set():
                    # Hand stdin and cooked mode over to the slot thread for
                    # the duration of its input() call.  Replace SIGINT
                    # handling for the input window: the slot thread polls
                    # _input_interrupted and raises KeyboardInterrupt.
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
                    signal.signal(signal.SIGINT, on_sigint_during_input)
                    slot._input_released.set()
                    while slot.is_alive() and not slot._input_resume.is_set():
                        slot._input_resume.wait(timeout=0.1)
                    signal.signal(signal.SIGINT, signal.SIG_IGN)
                    if not slot.is_alive():
                        break
                    terminal.set_raw_input_cooked_output(fd)
                    continue

                rlist, _, _ = select.select([fd], [], [], 0.1)
                if fd in rlist:
                    data = os.read(fd, 1024)
                    if not data:
                        break
                    if b"\x1d" in data:
                        result = "switched"
                        break
                    if b"\x03" in data and not slot._pty_active:
                        # No passthrough subprocess is running — interrupt
                        # the Python command itself.
                        slot.deactivate()
                        slot.kill()
                        result = "interrupted"
                        break
                    # Forward to slot.  When a passthrough_run() subprocess
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
                return (needs_forward, new_name)
            return (needs_forward, None)

        if result is None:
            # User cancelled.  If a menu action (pop) changed the current
            # context, behave as if the user had switched: let run()'s resume
            # path do the right thing for the new context's slot.  Otherwise
            # re-activate PTY slots so their reader thread can stream output
            # again.  PythonCommandSlots stay deactivated: their buffered
            # output will be replayed correctly (with raw_mode=True) the next
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
            parent = self.context_manager.current()
            inherited = dict(parent.variables) if parent else {}
            inherited_history = list(parent.history) if parent else []
            self.context_manager.create(
                target_name, variables=inherited, history=inherited_history
            )
            self.context_manager.push(target_name)
        else:
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
                            exc = slot._exit_exception
                            ctx.process_slot = None
                            self._notify_resumed_done(slot)
                            if isinstance(exc, SystemExit):
                                raise exc
                            if exc is not None and not isinstance(exc, KeyboardInterrupt):
                                print(f"\n[Python command error: {exc}]")
                            elif slot.exit_code and slot.exit_code != 0:
                                print(f"\n[Process exited with code {slot.exit_code}]")
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
                            self._notify_resumed_done(slot)
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
                    self._line_editor.add_to_history(full_text)
                    self._execute(full_text.strip())
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
            answer = passthrough_input("Exit anyway? [y/N] ").strip().lower()
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
