"""Tests for ``Shell._forward``, the one loop that hands the terminal to a
running slot — a ``ProcessSlot`` or a ``PythonCommandSlot``.

stdin is the slave end of a fresh PTY, so the loop can set raw mode on it;
the test types into the master end.
"""

import os
import sys
import threading
import time

import pytest

from eosh.shell import PythonCommandSlot, Shell

pty = pytest.importorskip("pty")
termios = pytest.importorskip("termios")


class _Stdin:
    def __init__(self, fd):
        self._fd = fd

    def fileno(self):
        return self._fd


@pytest.fixture
def typed(monkeypatch):
    """Return a function that runs ``Shell._forward(slot)`` with *keys*
    typed on the terminal, and gives back its result."""
    shell = Shell()
    master, slave = pty.openpty()
    monkeypatch.setattr(sys, "stdin", _Stdin(slave))

    def type_once_raw():
        # Entering raw mode flushes pending input, so wait for it first.
        deadline = time.monotonic() + 5
        while termios.tcgetattr(slave)[3] & termios.ICANON:
            if time.monotonic() > deadline:
                return
            time.sleep(0.005)
        os.write(master, keys_to_type[0])

    keys_to_type = [b""]

    def run(slot, keys: bytes) -> str:
        keys_to_type[0] = keys
        writer = threading.Thread(target=type_once_raw, daemon=True)
        writer.start()
        result = shell._forward(slot)
        writer.join()
        return result

    yield run
    os.close(master)
    os.close(slave)


class _Recorder:
    """What the loop needs of a slot, recording what it was sent."""

    def __init__(self):
        self.received = b""
        self.done = threading.Event()
        self.suspended = 0
        self.deadline = time.monotonic() + 5   # never hang the suite

    def is_alive(self):
        return not self.done.is_set() and time.monotonic() < self.deadline

    def write_stdin(self, data):
        self.received += data
        if b"q" in data:
            self.done.set()

    def resize(self, rows, cols):
        pass

    def suspend_terminal_modes(self):
        self.suspended += 1
        return ""


class _PtySlot(_Recorder):
    pass


class _PySlot(_Recorder, PythonCommandSlot):
    def __init__(self, *, pty_active=False, reading_input=False):
        _Recorder.__init__(self)
        self._pty_active = pty_active
        self._reading_input = reading_input
        self.activated = False
        self.killed = False

    def activate(self):
        self.activated = True

    def deactivate(self):
        pass

    def kill(self):
        self.killed = True
        self.done.set()


@pytest.mark.parametrize("make", [_PtySlot, _PySlot])
def test_keys_are_forwarded_until_the_slot_ends(typed, make):
    slot = make()
    assert typed(slot, b"abq") == "exited"
    assert slot.received == b"abq"


@pytest.mark.parametrize("make", [_PtySlot, _PySlot])
def test_switch_key_forwards_what_came_before_it(typed, make):
    slot = make()
    assert typed(slot, b"ab\x1dcd") == "switched"
    assert slot.received == b"ab"
    assert slot.suspended == 1


def test_a_python_slot_is_activated_by_the_loop(typed):
    slot = _PySlot()
    typed(slot, b"q")
    assert slot.activated


def test_ctrl_c_interrupts_a_python_command(typed):
    slot = _PySlot()
    assert typed(slot, b"\x03") == "interrupted"
    assert slot.killed
    assert slot.received == b""


@pytest.mark.parametrize("state", [{"pty_active": True}, {"reading_input": True}])
def test_ctrl_c_reaches_a_subprocess_or_a_question(typed, state):
    slot = _PySlot(**state)
    assert typed(slot, b"\x03q") == "exited"
    assert not slot.killed
    assert slot.received == b"\x03q"


def test_ctrl_c_is_just_a_byte_to_a_pty_child(typed):
    slot = _PtySlot()
    assert typed(slot, b"\x03q") == "exited"
    assert slot.received == b"\x03q"
