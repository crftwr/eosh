"""The job leader: a small helper process that owns one command line's session.

Started ahead of time by :mod:`eosh.job` as ``python -I -S _job_leader.py FD``,
in a new session (``start_new_session``) with no terminal, so a line never
waits for an interpreter to start.  A :class:`eosh.job.PipelineSlot` hands it
the slot's PTY slave; it takes that as its controlling terminal and stdio,
then starts the line's external stages on request.  They inherit its session, process group and
controlling terminal — as the stages of a job do in a POSIX shell — so
``/dev/tty`` works for ``| less`` / ``| sudo`` / ``| fzf``, Ctrl+C on the PTY
reaches every stage through the line discipline, and SIGWINCH goes to the
whole group.  eosh itself can't do that: a PTY becomes the controlling
terminal of a session leader only, and eosh's own children are in eosh's
session.

Protocol, over the socket on fd *FD*: length-prefixed JSON (``!I`` + body).

* eosh → leader, first: ``{"op": "attach"}`` with the PTY slave attached
  as an fd (``SCM_RIGHTS``); the answer is ``{"ok": true}`` or an error.
* eosh → leader: ``{"op": "spawn", "argv", "env", "cwd"}`` with the stage's
  stdin, stdout and stderr attached as fds.  The leader answers
  ``{"pid": n}`` or ``{"errno": n, "error": "..."}``, then later
  ``{"exit": pid, "status": n}`` when that stage ends (128+N for signal N).
* EOF from eosh — the line is done — and the leader exits.

Standard library only, and nothing from eosh: it runs with ``-I -S``.
"""

import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading


def _status(code: int) -> int:
    return 128 - code if code < 0 else code


def main() -> None:
    import fcntl
    import termios

    sock = socket.socket(fileno=int(sys.argv[1]))
    # Ctrl+C on the PTY is for the stages.  A handler (not SIG_IGN, which
    # children would inherit across exec) keeps the leader alive.
    signal.signal(signal.SIGINT, lambda *_: None)
    signal.signal(signal.SIGQUIT, lambda *_: None)

    send_lock = threading.Lock()

    def send(obj) -> None:
        data = json.dumps(obj).encode()
        with send_lock:
            sock.sendall(struct.pack("!I", len(data)) + data)

    def watch(proc: subprocess.Popen) -> None:
        send({"exit": proc.pid, "status": _status(proc.wait())})

    def recv_exact(n: int, first: bytes = b"") -> bytes:
        data = first
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    while True:
        head, fds, _, _ = socket.recv_fds(sock, 4, 3)
        if not head:
            return
        try:
            head = recv_exact(4, head)
            (size,) = struct.unpack("!I", head)
            req = json.loads(recv_exact(size))
        except EOFError:
            return
        if req.get("op") == "attach":
            try:
                (tty,) = fds
                fcntl.ioctl(tty, termios.TIOCSCTTY, 0)
                for n in (0, 1, 2):
                    os.dup2(tty, n)
            except (OSError, ValueError) as e:
                send({"errno": getattr(e, "errno", 0) or 0, "error": str(e)})
            else:
                send({"ok": True})
            finally:
                for fd in fds:
                    os.close(fd)
            continue
        try:
            stdin, stdout, stderr = fds
            proc = subprocess.Popen(req["argv"], stdin=stdin, stdout=stdout,
                                    stderr=stderr, env=req["env"], cwd=req["cwd"])
        except OSError as e:
            send({"errno": e.errno or 0, "error": e.strerror or str(e)})
        except Exception as e:      # a malformed request: say so, keep serving
            send({"errno": 0, "error": str(e)})
        else:
            send({"pid": proc.pid})
            threading.Thread(target=watch, args=(proc,), daemon=True).start()
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass


if __name__ == "__main__":
    main()
