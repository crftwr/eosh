"""Tests for reading user input from a command — ctx.input / ctx.input_block.

Both read the raw key stream (discussion #38): off the slot's key buffer,
which the forwarding loop feeds, or — on the main thread — the terminal
itself.  ``_read_typed`` does the line assembly for every source.
"""

from __future__ import annotations

import threading
import time
import types

import pytest

from eosh.shell import (
    PythonCommandSlot, _read_from_user, _read_typed,
)


def _feed(*chunks: bytes):
    """A next_bytes() that hands out *chunks*, then nothing."""
    it = iter(chunks)
    return lambda: next(it, b"")


def _line(keys: bytes) -> str:
    return _read_typed(_feed(keys), "", block=False)


def _block(keys: bytes) -> str:
    return _read_typed(_feed(keys), "", block=True)


def _slot() -> PythonCommandSlot:
    return PythonCommandSlot(types.SimpleNamespace(name="stub"), [])


# ---------------------------------------------------------------------------
# One line
# ---------------------------------------------------------------------------

def test_a_line_ends_at_enter():
    assert _line(b"yes\r") == "yes"


def test_a_line_may_arrive_in_pieces():
    assert _read_typed(_feed(b"y", b"", b"es", b"\r"), "", block=False) == "yes"


def test_ctrl_d_on_an_empty_line_is_eof():
    with pytest.raises(EOFError):
        _line(b"\x04")


def test_ctrl_d_mid_line_is_ignored_like_a_tty():
    assert _line(b"ab\x04c\r") == "abc"


def test_line_editing_backspace_ctrl_u_ctrl_w():
    assert _line(b"abx\x7f\r") == "ab"
    assert _line(b"junk\x15ok\r") == "ok"
    assert _line(b"delete my-cluster\x17keep\r") == "delete keep"


def test_ctrl_c_echoes_and_raises(capsys):
    with pytest.raises(KeyboardInterrupt):
        _line(b"ab\x03")
    assert capsys.readouterr().out.endswith("^C\n")


def test_prompt_and_echo(capsys):
    _read_typed(_feed(b"y\r"), "Delete? [y/N] ", block=False)
    assert capsys.readouterr().out == "Delete? [y/N] y\n"


def test_wide_characters_are_erased_by_their_width(capsys):
    _line("日\x7f\r".encode())
    assert capsys.readouterr().out == "日\b\b  \b\b\n"


# ---------------------------------------------------------------------------
# A pasted block
# ---------------------------------------------------------------------------

def test_block_ends_at_the_blank_line():
    assert _block(b'export A="1"\rexport B="2"\r\r') == 'export A="1"\nexport B="2"'


def test_block_takes_a_line_far_past_the_cooked_mode_limit():
    # MAX_CANON (1024 on macOS) discards an over-long line whole in cooked
    # mode, and a real session token is longer than that.
    token = "F" * 4000
    assert _block(f'export AWS_SESSION_TOKEN="{token}"\r\r'.encode()) == (
        f'export AWS_SESSION_TOKEN="{token}"'
    )


def test_block_ends_at_ctrl_d_without_a_blank_line():
    assert _block(b"one\rtwo\x04") == "one\ntwo"


def test_block_of_nothing_is_empty():
    assert _block(b"\r") == ""


def test_crlf_pasted_text_is_one_line_ending_not_two():
    assert _block(b"one\r\ntwo\r\n\r\n") == "one\ntwo"


def test_bracketed_paste_markers_and_arrow_keys_are_dropped():
    keys = b"\x1b[200~export A=1\r\x1b[Aexport B=2\r\x1b[201~\r"
    assert _block(keys) == "export A=1\nexport B=2"


def test_backspace_edits_the_current_line_only():
    assert _block(b"abx\x7f\rcd\x7f\x7f\x7fef\r\r") == "ab\nef"


def test_ctrl_c_cancels_the_block():
    with pytest.raises(KeyboardInterrupt):
        _block(b"export A=1\r\x03")


def test_block_echoes_what_it_reads(capsys):
    _block(b"abc\r\r")
    assert capsys.readouterr().out == "abc\n\n"


# ---------------------------------------------------------------------------
# From a slot: the forwarding loop's key buffer
# ---------------------------------------------------------------------------

def test_a_slot_block_reads_a_paste_that_landed_before_the_first_poll():
    slot = _slot()
    slot.write_stdin(b"export A=1\r\r")
    assert slot._read_typed("", block=True) == "export A=1"


def test_a_slot_line_ignores_keys_typed_before_the_question():
    """Typeahead was not an answer to a question not yet asked."""
    slot = _slot()
    slot.write_stdin(b"y\r")            # typed while the command was busy

    def answer():
        time.sleep(0.05)
        slot.write_stdin(b"n\r")

    threading.Thread(target=answer, daemon=True).start()
    assert slot._read_typed("", block=False) == "n"


def test_a_slot_marks_itself_reading_so_ctrl_c_reaches_the_reader():
    slot = _slot()
    seen = []

    def answer():
        time.sleep(0.05)
        seen.append(slot._reading_input)
        slot.write_stdin(b"\x03")

    threading.Thread(target=answer, daemon=True).start()
    with pytest.raises(KeyboardInterrupt):
        slot._read_typed("", block=False)
    assert seen == [True]
    assert slot._reading_input is False


# ---------------------------------------------------------------------------
# Fallbacks — no terminal to read keys from
# ---------------------------------------------------------------------------

def test_line_falls_back_to_input_without_a_terminal(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "typed")
    assert _read_from_user("? ", block=False) == "typed"


def test_block_falls_back_to_input_without_a_terminal(monkeypatch):
    lines = iter(["export A=1", "export B=2", "", "later"])
    monkeypatch.setattr("builtins.input", lambda *a: next(lines))
    assert _read_from_user("", block=True) == "export A=1\nexport B=2"


def test_block_falls_back_to_input_in_a_slot_when_stdin_is_not_a_terminal(monkeypatch):
    slot = _slot()
    monkeypatch.setattr("eosh.shell._current_slot",
                        types.SimpleNamespace(slot=slot))
    monkeypatch.setattr("eosh.shell._stdin_is_tty", lambda: False)
    lines = iter(["export A=1", ""])
    monkeypatch.setattr("builtins.input", lambda *a: next(lines))
    assert _read_from_user("", block=True) == "export A=1"
