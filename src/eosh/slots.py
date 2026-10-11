"""Python commands on threads: the plumbing that lets a command run on a
thread of the shell's own process and still have a terminal of its own.

A Python command typed at a POSIX terminal runs as a stage of its line's
:class:`~eosh.job.PipelineSlot`, on a thread whose ``sys.std*`` are that
slot's PTY — just as an external command's fds are (discussion #76).

* :class:`_ThreadLocalStream` — ``sys.stdin`` / ``stdout`` / ``stderr``
  routed per thread (:func:`install_stdio_routers`), so a stage writes to its
  own target — a pipe, a file, its slot's PTY — never another thread's.
* :class:`_PyStageHandle` — a Python stage on its thread.
* :func:`run_handler` — how a handler's end becomes an exit status, for
  every execution path.
* :func:`_run_interactive`, :func:`_read_from_user`, :func:`_choose` — what
  :class:`~eosh.command_context.CommandContext`'s methods do: reach the user
  through the stage's PTY, or the real terminal on the main thread.
"""

from __future__ import annotations

import codecs
import contextlib
import ctypes
import io
import os
import subprocess
import sys
import threading
import traceback
from typing import Callable

# On Windows a Python command runs on the main thread, on the real console.
IS_WINDOWS = os.name == "nt"
if not IS_WINDOWS:
    import termios

from . import terminal

# ---------------------------------------------------------------------------
# Thread-local stdio routing
# ---------------------------------------------------------------------------

class _ThreadLocalStream(io.TextIOBase):
    """A ``sys.stdin`` / ``sys.stdout`` / ``sys.stderr`` replacement that
    routes I/O per thread.

    A thread with no override (the main thread) uses *real*.  A thread that
    sets one — a Python stage, with a TextIOWrapper around its pipe end,
    redirect file or slot's PTY — reads and writes there instead, so concurrent commands never trample
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
        # The override's own fd when it has one (a pipe end, a redirect
        # file, a PipelineSlot's PTY): what `subprocess.run(stdout=
        # sys.stdout)` and a key reader (@watch's `q`) must use.
        target = self._target
        if target is not self._real:
            try:
                return target.fileno()
            except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
                pass
        return self._real.fileno()

    @property
    def buffer(self):
        # The override's own binary layer when it has one (a pipe end, a
        # slot's PTY).
        return getattr(self._target, "buffer", None) or self._real.buffer

    @property
    def encoding(self) -> str:
        return getattr(self._real, "encoding", "utf-8")

    @property
    def errors(self) -> str:
        return getattr(self._real, "errors", "strict")

    def isatty(self) -> bool:
        # An override answers for itself: a pipe end is never a tty, a
        # slot's PTY is.
        try:
            return self._target.isatty()
        except (AttributeError, ValueError):
            return False


class _PyStageHandle:
    """Lightweight handle for a Python-command stage running in a thread.

    Mirrors the attributes the pipeline driver in _execute_pipeline uses
    on subprocess.Popen (`wait()`, `returncode`-style exit code) so the
    two worker types can share one wait loop.
    """

    __slots__ = ("cmd_name", "thread", "done", "exit_code", "_io_objs", "interrupted",
                 "graceful")

    def __init__(self, cmd_name: str, graceful: bool = False) -> None:
        self.cmd_name = cmd_name
        # A decorator, or a command on the terminal, is interrupted with
        # KeyboardInterrupt rather than by closing its stdio: it can still
        # report (@time's timing line), and one waiting on nothing but the
        # clock (time.sleep) still stops.
        self.graceful = graceful
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

    def raise_keyboard_interrupt(self) -> None:
        """Ctrl+C for a graceful stage: KeyboardInterrupt in its thread, at
        its next bytecode — a blocked wait for its body returns first."""
        if self.thread is not None and self.thread.is_alive():
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(self.thread.ident), ctypes.py_object(KeyboardInterrupt))

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
    # A stage whose stdin and stdout are both a terminal — a lone decorator
    # on a PipelineSlot, and its body — can still talk to the user.
    if getattr(_in_pipeline, "flag", False) and not getattr(_in_pipeline, "on_terminal", False):
        raise RuntimeError(
            f"{what} cannot be used inside a piped Python command "
            f"(stdin/stdout are wired to pipes, not the terminal)"
        )


def _run_interactive(argv: list[str], **popen_kwargs) -> int:
    """``CommandContext.run_interactive``: run *argv* on the PTY of the
    command's slot, so the main thread stays the only reader of the terminal
    — it forwards keys to the PTY master, still intercepts ``Ctrl+]``, and
    the child sees a full TTY on fd 0/1/2.

    On the main thread (a ``sync`` command, Windows, no terminal) there is no
    slot and nobody else is reading: plain ``subprocess.run``.
    """
    _refuse_in_pipeline("ctx.run_interactive")
    job = getattr(_job_local, "job", None)
    if job is not None:
        # A command on its slot's terminal: the program gets the slot's PTY,
        # through its job leader — as a controlling terminal, so it can open
        # /dev/tty, and Ctrl+C reaches it, not the command.
        with job.interactive():
            proc = job.spawn(list(argv), stdin=None, stdout=None, stderr=None,
                             env=popen_kwargs.get("env") or dict(os.environ),
                             cwd=popen_kwargs.get("cwd") or os.getcwd())
            return proc.wait()
    return subprocess.run(argv, **popen_kwargs).returncode


def _stdin_is_tty() -> bool:
    """True when real stdin is a terminal — i.e. a key stream exists to read."""
    try:
        return os.isatty(sys.stdin.fileno())
    except (OSError, ValueError, AttributeError):
        return False


def _read_from_user(prompt: str, *, block: bool, what: str = "input") -> str:
    """``CommandContext.input`` / ``input_block``: read a line (or a pasted
    block, up to a blank line or Ctrl+D) off the raw key stream of the
    command's terminal — its slot's PTY, or the real one on the main thread
    — never through cooked mode, whose line buffer is capped at
    ``MAX_CANON``.  See :func:`_read_typed` for the editing keys.

    Without a terminal (or on Windows) falls back to :func:`input`.
    """
    _refuse_in_pipeline(what)
    if _stdin_is_tty() and not IS_WINDOWS:
        on_slot = getattr(_job_local, "job", None) is not None
        if on_slot and not block:
            # Keys typed while the command was busy were not meant as the
            # answer to a question it hadn't asked yet ("y" to a delete
            # prompt).  A paste, by contrast, may land before the first read.
            _drop_typed_ahead()
        # On a slot the switch key never arrives (the forwarding loop takes
        # it); on the main thread nothing can park the command — say so.
        with _terminal_keys() as next_bytes:
            return _read_typed(next_bytes, prompt, block=block,
                               refuse_switch=not on_slot)
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
    *items* on the command's terminal — its slot's PTY, or the real one on
    the main thread."""
    _refuse_in_pipeline("ctx.choose")
    if not items:
        return None
    if not _stdin_is_tty():
        return _choose_by_number(items, title)
    from .tui import InlinePicker

    if getattr(_job_local, "job", None) is not None:
        _drop_typed_ahead()             # typed before the question: not an answer
    if title:
        print(title)
    picker = InlinePicker(items)
    choice = picker.run()
    if picker.interrupted:
        raise KeyboardInterrupt
    if choice is not None and title:
        # Replace the title line with a record of the answer.
        sys.stdout.write(f"\x1b[1A\r\x1b[K{title} {choice}\n")
        sys.stdout.flush()
    return choice


def _drop_typed_ahead() -> None:
    """Discard keys already queued on this thread's terminal."""
    if IS_WINDOWS:
        return
    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (OSError, ValueError, AttributeError, termios.error):
        pass


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


#: What a command on the main thread says when Ctrl+] can't park it
#: (discussion #76): it is reading the answer to a question itself.
PARK_REFUSED = "eosh: this command runs on the main thread; it can't be sent to the background"


def _read_typed(next_bytes: Callable[[], bytes], prompt: str, *, block: bool,
                refuse_switch: bool = False) -> str:
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

    With *refuse_switch* (the main thread, where nothing can park the
    command), the context-switch key prints :data:`PARK_REFUSED` and the
    question again, instead of being dropped without a word.
    """
    from . import keys as keymap
    from .lineedit import _wcswidth

    switch_keys = set()
    if refuse_switch:
        switch_keys = {seq.decode() for seq in keymap.sequences("prompt.switch_context")
                       if len(seq) == 1}

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
            if ch in switch_keys:
                echo(f"\n{PARK_REFUSED}\n{prompt}{''.join(current)}")
                continue
            if ch < " " and ch != "\t":
                continue                             # other C0 controls
            current.append(ch)
            echo(ch)


def install_stdio_routers() -> None:
    """Replace ``sys.stdin`` / ``stdout`` / ``stderr`` with thread-local
    routers (once), so Python command threads can rebind their own streams —
    to a buffering proxy or a pipe end — without disturbing the main thread."""
    for stream in ("stdin", "stdout", "stderr"):
        if not isinstance(getattr(sys, stream), _ThreadLocalStream):
            setattr(sys, stream, _ThreadLocalStream(getattr(sys, stream)))
