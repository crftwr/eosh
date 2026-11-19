"""Pasting at the prompt — bracketed paste, held blocks, and where they run.

A paste meant for bash (``export KEY=VALUE`` lines from a console) runs
through ``source-bash`` and its environment comes back; any other multi-line
paste runs as eosh lines, one by one.  Either way it waits for Enter.
"""

import os

import pytest

from eosh import paste, terminal
from eosh.history import HistoryStore
from eosh.lineedit import LineEditor
from eosh.paste import PastedBlock, eosh_lines, needs_bash, normalize
from eosh.shell import Shell


# ── classification ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    'export WS_REPO_SOURCE="s3"\nexport AWS_DEFAULT_REGION="us-east-1"',
    "echo hi; export A=1",
    "source ./env.sh",
    ". ./env.sh",
    "unset A",
    "for f in *; do echo $f; done",
    "if true; then\n  echo y\nfi",
    "cat <<EOF\nx\nEOF",
    "echo `date`",
    "echo $((1 + 2))",
    "echo ${A:-default}",
    "f() { echo; }",
    "(cd x; make)",
    "{ a; b; }",
    "[[ -f x ]] && echo y",
])
def test_bash_syntax_goes_to_bash(text):
    assert needs_bash(text)


@pytest.mark.parametrize("text", [
    "A=1\nB=2",
    "cd x\nls -l",
    'echo "export me; if"',
    "AWS_PROFILE=x aws s3 ls",
    "echo $(date)",                 # eosh expands $(…) itself
    "echo ${HOME}",
    "git log --oneline | head",
    "make 2>&1 | tee log",
    "# a comment\nls",
])
def test_eosh_syntax_stays_in_eosh(text):
    assert not needs_bash(text)


def test_normalize_line_ends_tabs_and_controls():
    assert normalize("a\r\nb\rc\td\x07") == "a\nb\nc    d"


def test_eosh_lines_join_continuations_and_drop_comments():
    assert eosh_lines("ls \\\n  -l\n\n# note\necho hi\n") == ["ls   -l", "echo hi"]


# ── the line editor ────────────────────────────────────────────────────────

def _editor():
    ed = LineEditor(
        history=HistoryStore(None),
        get_completions=lambda line: ([], "", ""),
        get_prompt=lambda: "> ",
        local_history_fn=lambda: [],
    )
    ed._prompt_str, ed._prompt_len, ed._cols, ed._lines = "> ", 2, 80, 24
    return ed


def _paste(ed, monkeypatch, text):
    """Feed *text* as a bracketed paste: the start marker as a key, the
    rest (and the end marker) through terminal.read_key."""
    keys = iter([ch.encode() for ch in text] + [paste.PASTE_END])
    monkeypatch.setattr(terminal, "read_key", lambda fd: next(keys))
    return ed._handle_key(paste.PASTE_START)


def test_one_line_eosh_paste_is_inserted_for_editing(monkeypatch):
    ed = _editor()
    ed._handle_key(b"l")
    _paste(ed, monkeypatch, "s -l\n")      # trailing newline doesn't run it
    assert ed._paste is None
    assert ed._buf == "ls -l" and ed._cursor == 5


def test_multi_line_paste_waits_for_enter(monkeypatch):
    ed = _editor()
    assert _paste(ed, monkeypatch, "cd x\r\nls\r\n") is None
    assert ed._paste == "cd x\nls" and not ed._paste.bash
    ed._handle_key(b"q")                    # ignored while held
    result = ed._handle_key(b"\r")
    assert isinstance(result, PastedBlock) and result == "cd x\nls"


def test_bash_paste_is_held_even_on_one_line(monkeypatch):
    ed = _editor()
    _paste(ed, monkeypatch, "export A=1")
    assert ed._paste == "export A=1" and ed._paste.bash


def test_esc_puts_a_one_line_paste_back_to_edit(monkeypatch):
    ed = _editor()
    _paste(ed, monkeypatch, "export A=1")
    ed._handle_key(b"\x1b")
    assert ed._paste is None and ed._buf == "export A=1"


def test_esc_discards_a_block_and_restores_the_buffer(monkeypatch):
    ed = _editor()
    ed._handle_key(b"x")
    _paste(ed, monkeypatch, "export A=1\nexport B=2")
    assert ed._paste == "xexport A=1\nexport B=2"
    ed._handle_key(b"\x7f")
    assert ed._paste is None and ed._buf == "x" and ed._cursor == 1


def test_ctrl_d_does_not_exit_while_a_paste_is_held(monkeypatch):
    ed = _editor()
    _paste(ed, monkeypatch, "a\nb")
    assert ed._handle_key(b"\x04") is None


def test_ctrl_c_drops_the_paste(monkeypatch):
    ed = _editor()
    _paste(ed, monkeypatch, "a\nb")
    with pytest.raises(KeyboardInterrupt):
        ed._handle_key(b"\x03")
    assert ed._paste is None


def test_preview_fits_a_screenful(monkeypatch):
    ed = _editor()
    ed._lines = 8
    _paste(ed, monkeypatch, "\n".join(f"line{i}" for i in range(20)))
    rows = ed._paste_preview()
    assert len(rows) == 5
    assert rows[0] == "line0" and rows[-1] == "line19"
    assert "16 more lines" in rows[-2]


def test_preview_cuts_long_lines(monkeypatch):
    ed = _editor()
    ed._cols = 20
    _paste(ed, monkeypatch, "export A=" + "x" * 50 + "\nexport B=1")
    first = ed._paste_preview()[0]
    assert first.endswith("…") and len(first) == 20 - 1 - 2


# ── the shell runs it ──────────────────────────────────────────────────────

@pytest.fixture
def sh():
    saved_env = dict(os.environ)
    saved_cwd = os.getcwd()
    shell = Shell()
    try:
        yield shell
    finally:
        os.chdir(saved_cwd)
        os.environ.clear()
        os.environ.update(saved_env)


def test_bash_block_imports_its_environment(sh, capsys):
    # A ' and a $ in the body: the block must reach bash exactly as pasted.
    block = PastedBlock(
        "export EOSH_PASTE_A=\"it's\"\nexport EOSH_PASTE_B='$HOME'", bash=True)
    sh._run_paste(block)
    assert os.environ["EOSH_PASTE_A"] == "it's"
    assert os.environ["EOSH_PASTE_B"] == "$HOME"
    out = capsys.readouterr().out
    assert "EOSH_PASTE_A" in out and "it's" not in out     # names only


def test_bash_block_is_not_recorded_in_history(sh):
    calls = []
    sh._record_history = lambda line: calls.append(line)
    sh._run_paste(PastedBlock("export EOSH_PASTE_C=1", bash=True))
    assert calls == []


def test_eosh_block_runs_line_by_line(sh):
    recorded = []
    real = sh._record_history
    sh._record_history = lambda line: (recorded.append(line), real(line))[1]
    sh._run_paste(PastedBlock("EOSH_PASTE_D=1\n\nEOSH_PASTE_E=2", bash=False))
    assert os.environ["EOSH_PASTE_D"] == "1"
    assert os.environ["EOSH_PASTE_E"] == "2"
    assert recorded == ["EOSH_PASTE_D=1", "EOSH_PASTE_E=2"]
