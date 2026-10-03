"""Tests for slot-side stdin reads — passthrough_input / passthrough_input_block."""

from __future__ import annotations

import os
import threading
import types

import pytest

from pitash.shell import PythonCommandSlot, passthrough_input_block


class _StubProxy:
    """The bits of _StdoutProxy that _run_input drives."""

    def deactivate(self) -> None: ...
    def replay(self) -> None: ...
    def activate(self, raw_mode: bool = False) -> None: ...


def _slot() -> PythonCommandSlot:
    slot = PythonCommandSlot(types.SimpleNamespace(name="stub"), [])
    slot._proxy = _StubProxy()
    slot._err_proxy = _StubProxy()
    return slot


# ---------------------------------------------------------------------------
# _run_input — one line, through the terminal's cooked mode
# ---------------------------------------------------------------------------

def _read_line(slot: PythonCommandSlot, text: bytes, monkeypatch) -> str:
    """Run _run_input with a pipe for stdin and the main loop's half faked."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, text)
    os.close(write_fd)          # so an unterminated tail still reaches EOF
    monkeypatch.setattr("pitash.shell.sys.stdin",
                        types.SimpleNamespace(fileno=lambda: read_fd))

    def release():
        slot._input_request.wait(5)
        slot._input_released.set()

    releaser = threading.Thread(target=release, daemon=True)
    releaser.start()
    try:
        return slot._run_input("")
    finally:
        releaser.join(1)
        os.close(read_fd)


def test_single_line_read_stops_at_the_first_newline(monkeypatch):
    assert _read_line(_slot(), b"first\nsecond\n", monkeypatch) == "first"


def test_single_line_read_takes_an_unterminated_tail(monkeypatch):
    assert _read_line(_slot(), b"no newline", monkeypatch) == "no newline"


def test_single_line_read_raises_eof_on_empty_stdin(monkeypatch):
    with pytest.raises(EOFError):
        _read_line(_slot(), b"", monkeypatch)


# ---------------------------------------------------------------------------
# _run_input_block — a pasted block, off the raw key stream
# ---------------------------------------------------------------------------

def _read_block(keys: bytes, capsys=None) -> str:
    """Feed *keys* to a slot's key buffer the way the forwarding loop does."""
    slot = _slot()
    slot.write_stdin(keys)
    return slot._run_input_block()


def test_block_ends_at_the_blank_line():
    assert _read_block(b'export A="1"\rexport B="2"\r\r') == 'export A="1"\nexport B="2"'


def test_block_takes_a_line_far_past_the_cooked_mode_limit():
    # The reason this path is raw: MAX_CANON (1024 on macOS) discards an
    # over-long line whole, and a real session token is longer than that.
    token = "F" * 4000
    assert _read_block(f'export AWS_SESSION_TOKEN="{token}"\r\r'.encode()) == (
        f'export AWS_SESSION_TOKEN="{token}"'
    )


def test_block_ends_at_ctrl_d_without_a_blank_line():
    assert _read_block(b"one\rtwo\x04") == "one\ntwo"


def test_block_of_nothing_is_empty():
    assert _read_block(b"\r") == ""


def test_crlf_pasted_text_is_one_line_ending_not_two():
    assert _read_block(b"one\r\ntwo\r\n\r\n") == "one\ntwo"


def test_bracketed_paste_markers_and_arrow_keys_are_dropped():
    keys = b"\x1b[200~export A=1\r\x1b[Aexport B=2\r\x1b[201~\r"
    assert _read_block(keys) == "export A=1\nexport B=2"


def test_backspace_edits_the_current_line_only():
    assert _read_block(b"abx\x7f\rcd\x7f\x7f\x7fef\r\r") == "ab\nef"


def test_ctrl_c_cancels_the_block():
    with pytest.raises(KeyboardInterrupt):
        _read_block(b"export A=1\r\x03")


def test_block_echoes_what_it_reads(capsys):
    _read_block(b"abc\r\r")
    assert capsys.readouterr().out == "abc\n\n"


def test_block_falls_back_to_input_outside_a_slot_thread(monkeypatch):
    lines = iter(["export A=1", "export B=2", "", "later"])
    monkeypatch.setattr("builtins.input", lambda *a: next(lines))
    assert passthrough_input_block() == "export A=1\nexport B=2"


def test_block_falls_back_to_input_when_stdin_is_not_a_terminal(monkeypatch):
    slot = _slot()
    monkeypatch.setattr("pitash.shell._current_slot",
                        types.SimpleNamespace(slot=slot))
    monkeypatch.setattr("pitash.shell._stdin_is_tty", lambda: False)
    lines = iter(["export A=1", ""])
    monkeypatch.setattr("builtins.input", lambda *a: next(lines))
    assert passthrough_input_block() == "export A=1"
