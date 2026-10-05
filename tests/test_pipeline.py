"""Tests for pipeline.py — operator parsing, redirect extraction, glob expansion."""

import pytest
from eosh.pipeline import (
    Redirect,
    Stage,
    Pipeline,
    Sequence,
    parse_line,
    expand_globs,
    _split_on_operators,
    _extract_redirects,
)


# ---------------------------------------------------------------------------
# _split_on_operators
# ---------------------------------------------------------------------------

def test_split_simple_pipe():
    parts = _split_on_operators("ls | grep foo", ["|"])
    assert len(parts) == 2
    assert parts[0] == (None, "ls ")
    assert parts[1] == ("|", " grep foo")


def test_split_pipe_quoted():
    # Pipe inside quotes should not split
    parts = _split_on_operators('echo "a | b"', ["|"])
    assert len(parts) == 1
    assert parts[0][0] is None


def test_split_and_or():
    parts = _split_on_operators("make && ./run || echo fail", ["&&", "||"])
    assert [op for op, _ in parts] == [None, "&&", "||"]


def test_split_semicolon():
    parts = _split_on_operators("cd foo; ls", [";"])
    assert len(parts) == 2
    assert parts[1][0] == ";"


def test_split_no_match():
    parts = _split_on_operators("echo hello", ["|"])
    assert len(parts) == 1
    assert parts[0] == (None, "echo hello")


def test_split_prefers_longer_operator():
    # "&&" must not be matched as two "&"
    parts = _split_on_operators("a && b", ["&&", "&"])
    assert len(parts) == 2
    assert parts[1][0] == "&&"


# ---------------------------------------------------------------------------
# _extract_redirects
# ---------------------------------------------------------------------------

def test_redirect_stdout():
    text, redirs = _extract_redirects("echo foo > out.txt")
    assert text.strip() == "echo foo"
    assert len(redirs) == 1
    assert redirs[0] == Redirect(kind=">", target="out.txt")


def test_redirect_append():
    text, redirs = _extract_redirects("echo bar >> log.txt")
    assert text.strip() == "echo bar"
    assert redirs[0].kind == ">>"


def test_redirect_stdin():
    text, redirs = _extract_redirects("sort < input.txt")
    assert text.strip() == "sort"
    assert redirs[0] == Redirect(kind="<", target="input.txt")


def test_redirect_stderr():
    text, redirs = _extract_redirects("cmd 2> err.log")
    assert text.strip() == "cmd"
    assert redirs[0] == Redirect(kind="2>", target="err.log")


def test_redirect_2_and_1():
    text, redirs = _extract_redirects("cmd 2>&1")
    assert text.strip() == "cmd"
    assert redirs[0] == Redirect(kind="2>&1", target="1")


def test_redirect_quoted_target():
    text, redirs = _extract_redirects('echo hi > "my file.txt"')
    assert text.strip() == "echo hi"
    assert redirs[0].target == "my file.txt"


def test_no_redirect():
    text, redirs = _extract_redirects("ls -la")
    assert text.strip() == "ls -la"
    assert redirs == []


# ---------------------------------------------------------------------------
# parse_line
# ---------------------------------------------------------------------------

def test_parse_single_command():
    seq = parse_line("echo hello")
    assert len(seq.items) == 1
    op, pipeline = seq.items[0]
    assert op is None
    assert len(pipeline.stages) == 1
    assert pipeline.stages[0].text == "echo hello"


def test_parse_pipe():
    seq = parse_line("ls | grep py")
    assert len(seq.items) == 1
    _, pipeline = seq.items[0]
    assert len(pipeline.stages) == 2
    assert pipeline.stages[0].text == "ls"
    assert pipeline.stages[1].text == "grep py"


def test_parse_pipe_three_stages():
    seq = parse_line("cat file | sort | uniq")
    _, pipeline = seq.items[0]
    assert len(pipeline.stages) == 3


def test_parse_sequence_semicolon():
    seq = parse_line("cd /tmp; ls")
    assert len(seq.items) == 2
    assert seq.items[0][0] is None
    assert seq.items[1][0] == ";"


def test_parse_and():
    seq = parse_line("make && ./run")
    assert seq.items[1][0] == "&&"


def test_parse_or():
    seq = parse_line("cmd || echo failed")
    assert seq.items[1][0] == "||"


def test_parse_redirect_in_pipeline():
    seq = parse_line("sort < input.txt | uniq > output.txt")
    _, pipeline = seq.items[0]
    assert len(pipeline.stages) == 2
    assert pipeline.stages[0].redirects[0] == Redirect(kind="<", target="input.txt")
    assert pipeline.stages[1].redirects[0] == Redirect(kind=">", target="output.txt")


def test_parse_empty():
    seq = parse_line("")
    assert seq.items == []


def test_parse_quoted_pipe_not_split():
    seq = parse_line('echo "a | b"')
    _, pipeline = seq.items[0]
    assert len(pipeline.stages) == 1


# ---------------------------------------------------------------------------
# expand_globs
# ---------------------------------------------------------------------------

def test_expand_globs_no_match(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = expand_globs(["*.nonexistent"])
    assert result == ["*.nonexistent"]


def test_expand_globs_match(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    result = expand_globs(["*.py"])
    assert sorted(result) == ["a.py", "b.py"]


def test_expand_globs_no_pattern():
    result = expand_globs(["hello", "world"])
    assert result == ["hello", "world"]


def test_expand_globs_recursive(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    (tmp_path / "a.py").write_text("")
    (sub / "b.py").write_text("")
    result = expand_globs(["**/*.py"])
    assert any("a.py" in p for p in result)
    assert any("b.py" in p for p in result)
