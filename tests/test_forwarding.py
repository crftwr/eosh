"""Tests for ``Shell._forward``, the loop that hands the terminal to a
running slot (a PTY: ``PipelineSlot``).

stdin is the slave end of a fresh PTY, so the loop can set raw mode on it;
the test types into the master end.
"""

import os
import sys
import threading
import time

import pytest

from eosh.shell import Shell

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

    def take_unread(self):
        return b""


class _Slot(_Recorder):
    """A PTY slot: Ctrl+C is an interrupt unless a stage turned ISIG off."""

    def __init__(self, *, isig=True, unread=b""):
        super().__init__()
        self.isig = isig
        self.unread = unread
        self.interrupts = 0

    def ctrl_c_interrupts(self):
        return self.isig

    def interrupt_python_stages(self):
        self.interrupts += 1

    def take_unread(self):
        return self.unread


def test_keys_are_forwarded_until_the_slot_ends(typed):
    slot = _Slot()
    assert typed(slot, b"abq") == "exited"
    assert slot.received == b"abq"


def test_switch_key_forwards_what_came_before_it(typed):
    slot = _Slot()
    assert typed(slot, b"ab\x1dcd") == "switched"
    assert slot.received == b"ab"
    assert slot.suspended == 1


def test_ctrl_c_reaches_the_pty_and_the_python_stages(typed):
    slot = _Slot()
    assert typed(slot, b"\x03q") == "exited"
    assert slot.received == b"\x03q"         # the line discipline signals the processes
    assert slot.interrupts == 1               # the threads are told separately


def test_ctrl_c_is_only_a_key_when_the_pty_says_so(typed):
    slot = _Slot(isig=False)                  # `less`, or a ctx.input question
    assert typed(slot, b"\x03q") == "exited"
    assert slot.received == b"\x03q"
    assert slot.interrupts == 0


def test_keys_nothing_read_go_back_to_the_prompt(typed, monkeypatch):
    from eosh import terminal
    monkeypatch.setattr(terminal, "_pending_input", b"")
    typed(_Slot(unread=b"ls\n"), b"q")          # typed ahead while `cd x` ran
    assert terminal._pending_input == b"ls\n"
