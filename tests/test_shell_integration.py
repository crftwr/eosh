"""Prompt and command marks for the terminal (issue #26)."""

import io
import os

import pytest

from eosh import shell_integration as si
from eosh.history import HistoryStore
from eosh.lineedit import LineEditor
from eosh.shell import Shell


class _Tty(io.StringIO):
    def isatty(self):
        return True


_TERMINAL_VARS = ("TERM_PROGRAM", "TERM", "WT_SESSION")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """No terminal detected, mode auto, nothing open, default allowlist."""
    for var in _TERMINAL_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(si, "_mode", si.AUTO)
    monkeypatch.setattr(si, "_open", None)
    monkeypatch.setattr(si, "_state", "idle")
    monkeypatch.setattr(si, "TERMINALS", list(si.DEFAULT_TERMINALS))


def _tty(monkeypatch):
    """A terminal stdout, captured.  Installed from the test body: pytest's
    own capture replaces ``sys.stdout`` after fixtures run."""
    stream = _Tty()
    monkeypatch.setattr("sys.stdout", stream)
    return stream


def _osc(n, body):
    return f"\x1b]{n};{body}\x07"


# ── which marks ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("var, value, expected", [
    ("TERM_PROGRAM", "vscode", si.OSC633),
    ("TERM_PROGRAM", "iTerm.app", si.OSC133),
    ("TERM_PROGRAM", "WezTerm", si.OSC133),
    ("TERM_PROGRAM", "ghostty", si.OSC133),
    ("TERM", "xterm-ghostty", si.OSC133),
    ("TERM", "xterm-kitty", si.OSC133),
    ("TERM", "foot", si.OSC133),
    ("WT_SESSION", "0f1e", si.OSC133),
    ("TERM_PROGRAM", "Apple_Terminal", None),
    ("TERM_PROGRAM", "tmux", None),
    ("TERM", "xterm-256color", None),
])
def test_detect(monkeypatch, var, value, expected):
    monkeypatch.setenv(var, value)
    assert si.detect() == expected


def test_vscode_wins_over_what_started_it(monkeypatch):
    monkeypatch.setenv("WT_SESSION", "x")             # `code .` from Windows Terminal
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    assert si.detect() == si.OSC633


def test_nothing_without_a_terminal(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    monkeypatch.setattr("sys.stdout", io.StringIO())
    assert si.dialect() is None
    assert si.prompt_marks() == ("", "")


def test_mode_overrides_detection(monkeypatch):
    _tty(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    si.set_mode(si.OSC133)
    assert si.dialect() == si.OSC133
    si.set_mode(si.OFF)
    assert si.dialect() is None
    monkeypatch.delenv("TERM_PROGRAM")
    si.set_mode(si.OSC633)                            # forced in an unknown terminal
    assert si.dialect() == si.OSC633
    with pytest.raises(ValueError):
        si.set_mode("osc7")


def test_config_extends_the_allowlist(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "tmux")
    si.TERMINALS.append(("TERM_PROGRAM", "tmux", si.OSC133))
    assert si.detect() == si.OSC133
    si.reset_config()
    assert si.detect() is None


# ── the sequences ────────────────────────────────────────────────────────────

def test_one_line_osc633(monkeypatch):
    out = _tty(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    assert si.prompt_marks() == (_osc(633, "A"), _osc(633, "B"))
    assert si.prompt_marks(continuation=True) == (_osc(633, "F"), _osc(633, "G"))
    si.command_started("ls -l")
    si.command_finished(1)
    cwd = si.escape_value(os.getcwd())
    assert out.getvalue() == (_osc(633, "E;ls -l") + _osc(633, "C")
                              + _osc(633, "D;1") + _osc(633, f"P;Cwd={cwd}"))


def test_one_line_osc133(monkeypatch):
    out = _tty(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    assert si.prompt_marks() == (_osc(133, "A"), _osc(133, "B"))
    assert si.prompt_marks(continuation=True) == ("", "")
    si.command_started("ls -l")
    si.command_finished(0)
    assert out.getvalue() == _osc(133, "C") + _osc(133, "D;0")


def test_a_line_that_ran_nothing_is_closed(monkeypatch):
    out = _tty(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    si.prompt_marks()
    si.command_finished(None)
    assert out.getvalue().startswith(
        _osc(633, "E;") + _osc(633, "C") + _osc(633, "D") + "\x1b]633;P;")


def test_backgrounded_line_has_no_status(monkeypatch):
    out = _tty(monkeypatch)
    monkeypatch.setenv("TERM", "xterm-kitty")
    si.prompt_marks()
    si.command_started("make")
    si.command_finished(None)
    assert out.getvalue() == _osc(133, "C") + _osc(133, "D")


def test_closed_in_the_dialect_it_was_opened_in(monkeypatch):
    out = _tty(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    si.prompt_marks()
    si.command_started("var shell_integration=osc133")
    si.set_mode(si.OSC133)
    si.command_finished(0)
    assert _osc(633, "D;0") in out.getvalue()
    assert "\x1b]133;" not in out.getvalue()


def test_finish_without_a_prompt_writes_nothing(monkeypatch):
    out = _tty(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    si.command_started("x")
    si.command_finished(0)
    assert out.getvalue() == ""


def test_escape_value():
    assert si.escape_value(r"a\b;c" + "\n") == r"a\\b\x3bc\x0a"


# ── the line editor and the shell ────────────────────────────────────────────

def test_editor_writes_marks_around_the_prompt(monkeypatch):
    out = _tty(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    ed = LineEditor(
        history=HistoryStore(None),
        get_completions=lambda line: ([], "", ""),
        get_prompt=lambda: "$ ",
    )
    ed._prompt_str, ed._prompt_len, ed._cols = "$ ", 2, 80
    ed._prompt_marks = si.prompt_marks()
    ed._buf, ed._cursor = "ls", 2
    ed._redraw()
    assert _osc(133, "A") + "$ " + _osc(133, "B") + "ls" in out.getvalue()


def test_execute_returns_the_status(tmp_path):
    sh = Shell()
    assert sh._execute(f"cd {tmp_path}") == 0
    assert sh._execute(f"cd {tmp_path}/missing") == 1
    assert sh._execute("@time ls | wc") == 2       # parse error


def test_the_var():
    sh = Shell()
    sh._execute("var shell_integration=osc133")
    assert si.get_mode() == si.OSC133
    sh._execute("var shell_integration=off")
    assert si.get_mode() == si.OFF
    sh._execute("var shell_integration=bogus")
    assert si.get_mode() == si.OFF
    sh._execute("var shell_integration=")
    assert si.get_mode() == si.AUTO


def test_reload_restores_the_allowlist():
    sh = Shell()
    si.TERMINALS.append(("TERM_PROGRAM", "tmux", si.OSC133))
    sh._clear_user_config()
    assert si.TERMINALS == list(si.DEFAULT_TERMINALS)
