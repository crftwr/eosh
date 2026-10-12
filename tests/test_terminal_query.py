"""terminal.query_terminal_version — the XTVERSION round trip (POSIX)."""

import io
import os
import sys

import pytest

from eosh import terminal

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty")


@pytest.fixture
def pty_pair(monkeypatch):
    """(master, slave): the test writes the terminal's answer on the master
    ahead of time, the query reads it from the slave."""
    import pty
    master, slave = pty.openpty()
    monkeypatch.setattr(terminal, "_pending_input", b"")
    yield master, slave
    os.close(master)
    os.close(slave)


def _raw(slave):
    import tty
    tty.setraw(slave)


def _stdout(monkeypatch):
    """The question, captured.  Installed from the test body: pytest's own
    capture replaces ``sys.stdout`` after fixtures run."""
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    return out


def test_the_name_and_the_question(pty_pair, monkeypatch):
    master, slave = pty_pair
    out = _stdout(monkeypatch)
    _raw(slave)
    os.write(master, b"\x1bP>|kitty(0.36.4)\x1b\\\x1b[12;1R")
    assert terminal.query_terminal_version(slave) == "kitty(0.36.4)"
    assert out.getvalue() == "\x1b[>q\x1b[6n"
    assert terminal._pending_input == b""


def test_no_xtversion_only_the_cursor_report(pty_pair):
    master, slave = pty_pair
    _raw(slave)
    os.write(master, b"\x1b[12;1R")
    assert terminal.query_terminal_version(slave) is None


def test_bel_terminated_reply(pty_pair):
    master, slave = pty_pair
    _raw(slave)
    os.write(master, b"\x1bP>|tmux 3.4\x07\x1b[3;1R")
    assert terminal.query_terminal_version(slave) == "tmux 3.4"


def test_keys_typed_ahead_are_kept(pty_pair):
    master, slave = pty_pair
    _raw(slave)
    os.write(master, b"ls\x1bP>|foot(1.18.1)\x1b\\\x1b[3;1Rx")
    assert terminal.query_terminal_version(slave) == "foot(1.18.1)"
    assert terminal._pending_input == b"lsx"


def test_no_answer_times_out(pty_pair):
    master, slave = pty_pair
    _raw(slave)
    assert terminal.query_terminal_version(slave, timeout=0.05) is None


def test_terminal_mode_is_restored(pty_pair):
    import termios
    master, slave = pty_pair

    def mode():
        attrs = termios.tcgetattr(slave)
        # PENDIN: the kernel's note that input awaits a re-read, not a mode
        attrs[3] &= ~getattr(termios, "PENDIN", 0)
        return attrs[:6]
    before = mode()
    os.write(master, b"\x1b[3;1R")
    terminal.query_terminal_version(slave)
    assert mode() == before


def test_not_a_terminal():
    r, w = os.pipe()
    try:
        assert terminal.query_terminal_version(r) is None
    finally:
        os.close(r)
        os.close(w)
