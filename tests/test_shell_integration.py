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
    monkeypatch.setattr(si, "TERMINAL_NAMES", list(si.DEFAULT_TERMINAL_NAMES))
    monkeypatch.setattr(si, "_reported", None)


def _tty(monkeypatch):
    """A terminal stdout, captured.  Installed from the test body: pytest's
    own capture replaces ``sys.stdout`` after fixtures run."""
    stream = _Tty()
    monkeypatch.setattr("sys.stdout", stream)
    return stream


def _osc(n, body):
    return f"\x1b]{n};{body}\x07"


def _osc7():
    return _osc(7, si.file_url(os.getcwd()))


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


# ── asking the terminal (XTVERSION) ─────────────────────────────────────────

@pytest.mark.parametrize("reply, expected", [
    ("kitty(0.36.4)", "kitty"),
    ("tmux 3.4", "tmux"),
    ("iTerm2 3.5.0", "iTerm2"),
    ("  foot(1.18.1) ", "foot"),
])
def test_terminal_name(reply, expected):
    assert si.terminal_name(reply) == expected


@pytest.mark.parametrize("reply, expected", [
    ("iTerm2 3.5.0", si.OSC133),                      # over ssh
    ("WezTerm 20240203-110809-5046fc22", si.OSC133),
    ("ghostty 1.1.0", si.OSC133),
    ("kitty(0.36.4)", si.OSC133),
    ("foot(1.18.1)", si.OSC133),
    ("tmux 3.4", si.OSC133),
    ("Konsole 24.02.1", None),                        # left out, as in TERMINALS
    ("xterm(390)", None),
])
def test_detect_by_reported_name(monkeypatch, reply, expected):
    monkeypatch.setattr(si, "_reported", reply)
    assert si.detect() == expected


def test_environment_wins_over_the_reported_name(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    monkeypatch.setattr(si, "_reported", "tmux 3.4")
    assert si.detect() == si.OSC633


def test_config_extends_the_name_allowlist(monkeypatch):
    monkeypatch.setattr(si, "_reported", "Konsole 24.02.1")
    si.TERMINAL_NAMES.append(("konsole", si.OSC133))  # case doesn't matter
    assert si.detect() == si.OSC133
    si.reset_config()
    assert si.detect() is None


class _TtyIn(io.StringIO):
    def isatty(self):
        return True

    def fileno(self):
        return 0


def _asked(monkeypatch, answer="tmux 3.4"):
    """Stub the round trip; the list records each question asked."""
    asked = []

    def query(fd):
        asked.append(fd)
        return answer
    monkeypatch.setattr("eosh.terminal.query_terminal_version", query)
    monkeypatch.setattr("sys.stdin", _TtyIn())
    return asked


def test_probe_asks_when_the_environment_says_nothing(monkeypatch):
    _tty(monkeypatch)
    asked = _asked(monkeypatch)
    si.probe()
    assert asked == [0]
    assert si.dialect() == si.OSC133


def test_probe_doesnt_ask_when_the_environment_says(monkeypatch):
    _tty(monkeypatch)
    asked = _asked(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    si.probe()
    assert asked == []


def test_probe_doesnt_ask_when_the_mode_is_set(monkeypatch):
    _tty(monkeypatch)
    asked = _asked(monkeypatch)
    for mode in (si.OFF, si.OSC133):
        si.set_mode(mode)
        si.probe()
    assert asked == []


def test_probe_doesnt_ask_without_a_terminal(monkeypatch):
    asked = _asked(monkeypatch)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    si.probe()
    assert asked == []


def test_startup_probes_then_reports_the_cwd(monkeypatch):
    out = _tty(monkeypatch)
    _asked(monkeypatch, "kitty(0.36.4)")
    si.startup()
    assert out.getvalue() == _osc7()


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
    assert out.getvalue() == _osc(133, "C") + _osc(133, "D;0") + _osc7()


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
    assert out.getvalue() == _osc(133, "C") + _osc(133, "D") + _osc7()


def test_closed_in_the_dialect_it_was_opened_in(monkeypatch):
    out = _tty(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    si.prompt_marks()
    si.command_started("var shell_integration=osc133")
    si.set_mode(si.OSC133)
    si.command_finished(0)
    assert _osc(633, "D;0") in out.getvalue()
    assert "\x1b]133;" not in out.getvalue()


# ── the cwd ──────────────────────────────────────────────────────────────────

def test_file_url(monkeypatch):
    monkeypatch.setattr(si.socket, "gethostname", lambda: "box")
    assert si.file_url("/home/me/my dir/100%") == "file://box/home/me/my%20dir/100%25"


def test_file_url_of_a_windows_path(monkeypatch):
    monkeypatch.setattr(si.socket, "gethostname", lambda: "box")
    monkeypatch.setattr(si.os, "name", "nt")
    assert si.file_url("C:\\Users\\me") == "file://box/C:/Users/me"


def test_windows_terminal_also_gets_osc_9_9(monkeypatch):
    out = _tty(monkeypatch)
    monkeypatch.setenv("WT_SESSION", "x")
    monkeypatch.setattr(si.os, "name", "nt")
    monkeypatch.setattr(si.os, "getcwd", lambda: "C:\\work")
    si.startup()
    assert out.getvalue().endswith(_osc(9, '9;"C:\\work"'))


def test_osc_9_9_only_on_native_windows(monkeypatch):
    out = _tty(monkeypatch)
    monkeypatch.setenv("WT_SESSION", "x")             # WSL passes it through
    monkeypatch.setattr(si.os, "name", "posix")
    si.startup()
    assert out.getvalue() == _osc7()


@pytest.mark.parametrize("env, expected", [
    (("TERM_PROGRAM", "vscode"), lambda: _osc(633, f"P;Cwd={si.escape_value(os.getcwd())}")),
    (("TERM_PROGRAM", "ghostty"), _osc7),
])
def test_startup_reports_the_cwd(monkeypatch, env, expected):
    out = _tty(monkeypatch)
    monkeypatch.setenv(*env)
    si.startup()
    assert out.getvalue() == expected()


def test_startup_off_writes_nothing(monkeypatch):
    out = _tty(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    si.set_mode(si.OFF)
    si.startup()
    assert out.getvalue() == ""


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
    si.TERMINAL_NAMES.append(("Konsole", si.OSC133))
    sh._clear_user_config()
    assert si.TERMINALS == list(si.DEFAULT_TERMINALS)
    assert si.TERMINAL_NAMES == list(si.DEFAULT_TERMINAL_NAMES)
