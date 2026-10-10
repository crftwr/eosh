"""A command line's pipeline on one PTY, so Ctrl+] can park it.

On a POSIX terminal every external command runs here — alone, in a
pipeline, or redirected — and so does a lone decorator: several processes
and Python threads wired together with pipes, on one PTY, so Ctrl+] can park
them (discussion #76).

:class:`PipelineSlot` gives the whole pipeline one PTY.  Every
terminal-facing end — the first stage's stdin, the last stage's stdout, every
stage's stderr — is its slave; the master is read and buffered like
any :class:`~eosh.process.PtySlot`'s, so the shell parks, resumes and
previews it the same way.

The external stages are started by a *job leader* (``_job_leader.py``), a
helper process — started ahead of time, one spare always waiting — that is
the session leader with the PTY as its controlling terminal: the stages share its session and process group, so ``/dev/tty``,
Ctrl+C and SIGWINCH work as they do for a POSIX shell's job.  It starts only
when the line has an external stage.  Python stages stay on the shell's
threads, writing to the slave through the thread-local routers.

POSIX only, like every PTY slot.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

from .process import PtySlot

if os.name != "nt":
    import fcntl
    import pty
    import termios

_LEADER = str(Path(__file__).with_name("_job_leader.py"))


class _LeaderProc:
    """An external stage the job leader started: the bits of
    :class:`subprocess.Popen` the pipeline driver uses."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self._done = threading.Event()

    def _finished(self, status: int) -> None:
        self.returncode = status
        self._done.set()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        # In short waits, so a KeyboardInterrupt on the main thread lands.
        while not self._done.wait(0.1 if timeout is None else timeout):
            if timeout is not None:
                raise subprocess.TimeoutExpired(str(self.pid), timeout)
        return self.returncode

    def terminate(self) -> None:
        try:
            os.kill(self.pid, signal.SIGTERM)
        except OSError:
            pass


class _JobLeader:
    """The helper process, and the socket to it.  Started with no terminal;
    :meth:`attach` gives it one."""

    def __init__(self) -> None:
        self._sock, theirs = socket.socketpair()
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-I", "-S", _LEADER, str(theirs.fileno())],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(theirs.fileno(),), start_new_session=True,
            )
        finally:
            theirs.close()
        self._replies: queue.Queue = queue.Queue()
        self._procs: dict[int, _LeaderProc] = {}
        self._early: dict[int, int] = {}     # exits reported before the reply was handled
        self._lock = threading.Lock()
        threading.Thread(target=self._read_loop, daemon=True,
                         name="eosh-job-leader").start()

    def _recv(self) -> dict | None:
        head = b""
        while len(head) < 4:
            chunk = self._sock.recv(4 - len(head))
            if not chunk:
                return None
            head += chunk
        (size,) = struct.unpack("!I", head)
        body = b""
        while len(body) < size:
            chunk = self._sock.recv(size - len(body))
            if not chunk:
                return None
            body += chunk
        return json.loads(body)

    def _read_loop(self) -> None:
        while True:
            try:
                msg = self._recv()
            except OSError:
                msg = None
            if msg is None:
                break
            if "exit" in msg:
                with self._lock:
                    proc = self._procs.get(msg["exit"])
                    if proc is None:
                        self._early[msg["exit"]] = msg["status"]
                if proc is not None:
                    proc._finished(msg["status"])
            else:
                self._replies.put(msg)
        # The leader is gone: nothing more will be reported.
        self._replies.put({"errno": 0, "error": "job leader exited"})
        with self._lock:
            for proc in self._procs.values():
                if proc.returncode is None:
                    proc._finished(-1)

    def _request(self, req: dict, fds: list[int]) -> dict:
        data = json.dumps(req).encode()
        socket.send_fds(self._sock, [struct.pack("!I", len(data)) + data], fds)
        return self._replies.get()

    def attach(self, tty_fd: int) -> None:
        """Make *tty_fd* (a PTY slave) the leader's controlling terminal."""
        reply = self._request({"op": "attach"}, [tty_fd])
        if not reply.get("ok"):
            raise OSError(reply.get("errno") or 0, reply.get("error", "attach failed"))

    def spawn(self, argv: list[str], env: dict[str, str], cwd: str,
              fds: tuple[int, int, int]) -> _LeaderProc:
        reply = self._request({"op": "spawn", "argv": argv, "env": env, "cwd": cwd},
                              list(fds))
        if "pid" not in reply:
            err = reply.get("errno") or 0
            if err == 2:
                raise FileNotFoundError(err, reply.get("error", ""), argv[0])
            raise OSError(err, reply.get("error", "could not start"), argv[0])
        proc = _LeaderProc(reply["pid"])
        with self._lock:
            self._procs[proc.pid] = proc
            early = self._early.pop(proc.pid, None)
        if early is not None:
            proc._finished(early)
        return proc

    def close(self) -> None:
        """The line is done: let the leader exit, and reap it."""
        try:
            self._sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self._sock.close()


# One leader started ahead of time, so a line never waits ~30 ms for an
# interpreter: the line takes it, and the next one starts in the background.
_spare: _JobLeader | None = None
_spare_lock = threading.Lock()


def _warm() -> None:
    global _spare
    try:
        leader = _JobLeader()
    except OSError:
        return
    with _spare_lock:
        if _spare is None:
            _spare = leader
            return
    leader.close()


def prewarm() -> None:
    """Start the spare leader in the background (at shell startup)."""
    threading.Thread(target=_warm, daemon=True, name="eosh-job-warm").start()


def _take_leader() -> _JobLeader:
    global _spare
    with _spare_lock:
        leader, _spare = _spare, None
    if leader is None or leader.proc.poll() is not None:
        leader = _JobLeader()
    prewarm()
    return leader


class PipelineSlot(PtySlot):
    """One pipeline on one PTY — see the module docstring.

    The shell builds the stages with :meth:`spawn` (external) and
    :meth:`terminal_fd` (a Python stage's terminal-facing end), hands the
    workers to :meth:`start`, and then drives the slot like any PTY slot.
    """

    def __init__(self, label: str, on_exit=None) -> None:
        super().__init__(on_exit)
        self.argv = [label]
        self.mark_started()
        master, slave = pty.openpty()
        rows, cols = self._get_real_terminal_size()
        if rows and cols:
            try:
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            except OSError:
                pass
        self.master_fd = master
        self._slave_fd = slave
        self._leader: _JobLeader | None = None
        # Stages start from more than one thread (a decorator body spawns
        # from its stage's thread): one leader, one request at a time.
        self._spawn_lock = threading.Lock()
        self._workers: list = []
        self._status = 0
        self._unread = b""
        self._done = threading.Event()
        self._start_reader()

    # --- building the stages ------------------------------------------------------

    def terminal_fd(self) -> int:
        """A new fd on the PTY slave, for a stage's terminal-facing end; the
        caller owns it."""
        return os.dup(self._slave_fd)

    def spawn(self, argv: list[str], *, stdin, stdout, stderr,
              env: dict[str, str], cwd: str) -> _LeaderProc:
        """Start an external stage through the job leader.  Each of *stdin*,
        *stdout*, *stderr* is an fd, a file object, or None for the PTY."""
        def fd(x) -> int:
            if x is None:
                return self._slave_fd
            return x if isinstance(x, int) else x.fileno()

        with self._spawn_lock:
            if self._leader is None:
                leader = _take_leader()
                leader.attach(self._slave_fd)
                self._leader = leader
            return self._leader.spawn(argv, env, cwd, (fd(stdin), fd(stdout), fd(stderr)))

    def start(self, workers: list) -> None:
        """The stages are running: wait for them on a thread of our own."""
        self._workers = list(workers)
        threading.Thread(target=self._wait, daemon=True, name="eosh-pipeline").start()

    def discard(self) -> None:
        """Nothing was started: close the PTY."""
        self.start([])

    def _wait(self) -> None:
        status = 0
        for w in self._workers:
            w.wait()
            # A Popen-like stage has returncode, a Python stage exit_code.
            status = (w.returncode if hasattr(w, "returncode") else w.exit_code) or 0
        self._status = status
        # Before the leader goes: when a session's controlling process
        # exits, its terminal's input queue is flushed.
        self._unread = self._drain_typed_ahead()
        if self._leader is not None:
            self._leader.close()
        # The last slave end: the reader sees EOF once the stages' are gone.
        try:
            os.close(self._slave_fd)
        except OSError:
            pass
        self._done.set()

    def _drain_typed_ahead(self) -> bytes:
        """Keys typed into the PTY that no stage read — a pasted next line —
        so the shell can hand them to the prompt."""
        data = b""
        try:
            # Non-canonical, so a partial line (no Enter yet) is readable too.
            attrs = termios.tcgetattr(self._slave_fd)
            attrs[3] &= ~(termios.ICANON | termios.ECHO)
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self._slave_fd, termios.TCSANOW, attrs)
            os.set_blocking(self._slave_fd, False)
            while chunk := os.read(self._slave_fd, 4096):
                data += chunk
        except (OSError, termios.error):
            pass                       # BlockingIOError: nothing more queued
        return data

    # --- PtySlot ------------------------------------------------------------------

    def _reap(self) -> int:
        self._done.wait()
        return self._status

    def _pgid(self) -> int | None:
        return self._leader.proc.pid if self._leader is not None else None

    def take_unread(self) -> bytes:
        data, self._unread = self._unread, b""
        return data

    def ctrl_c_interrupts(self) -> bool:
        """Whether Ctrl+C on the PTY is an interrupt now, not a key: a stage
        like ``less`` may have turned ISIG off to read it."""
        try:
            return bool(termios.tcgetattr(self.master_fd)[3] & termios.ISIG)
        except (OSError, termios.error):
            return True

    def interrupt_python_stages(self) -> None:
        """What Ctrl+C does to the Python stages (the external ones get
        SIGINT from the line discipline)."""
        for w in self._workers:
            if getattr(w, "decorator", False):
                w.raise_keyboard_interrupt()
            elif hasattr(w, "interrupt"):
                w.interrupt()

    def kill(self) -> None:
        if not self.is_alive():
            return
        if self._leader is not None:
            try:
                os.killpg(self._leader.proc.pid, signal.SIGTERM)
            except OSError:
                pass
        self.interrupt_python_stages()
