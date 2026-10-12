"""Tests for the ``confirm`` built-in: ask, and answer with the exit status."""

from __future__ import annotations

import pytest

from eosh.commands import registry as command_registry
from eosh.shell import Shell


@pytest.fixture
def sh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return Shell()


@pytest.fixture
def answer(monkeypatch):
    """Answer the question (off a terminal, ctx.confirm reads input())."""
    asked = []

    def set_answer(reply):
        def fake_input(prompt=""):
            asked.append(prompt)
            if isinstance(reply, BaseException):
                raise reply
            return reply
        monkeypatch.setattr("builtins.input", fake_input)
        return asked
    return set_answer


def test_confirm_is_a_builtin(sh):
    assert command_registry.is_builtin("confirm")
    assert not sh.registry.get("confirm").sync     # it reads the line's own terminal


@pytest.mark.parametrize("reply", ["y", "yes", "Y"])
def test_yes_is_success(sh, tmp_path, answer, reply):
    answer(reply)
    assert sh._execute(f"confirm && echo ran > {tmp_path / 'out'}") == 0
    assert (tmp_path / "out").read_text() == "ran\n"


@pytest.mark.parametrize("reply", ["", "n", "no", "whatever"])
def test_anything_else_is_failure(sh, tmp_path, answer, reply):
    answer(reply)
    assert sh._execute(f"confirm && echo ran > {tmp_path / 'out'}") == 1
    assert not (tmp_path / "out").exists()


def test_ctrl_d_is_failure(sh, tmp_path, answer):
    answer(EOFError())
    assert sh._execute(f"confirm && echo ran > {tmp_path / 'out'}") == 1
    assert not (tmp_path / "out").exists()


def test_the_default_question(sh, answer):
    asked = answer("n")
    sh._execute("confirm")
    assert asked == ["Continue? [y/N] "]


def test_the_message_is_the_question(sh, answer):
    asked = answer("n")
    sh._execute("confirm 'Destroy prod?'")
    assert asked == ["Destroy prod? [y/N] "]


def test_or_runs_on_no(sh, tmp_path, answer):
    answer("n")
    assert sh._execute(f"confirm || echo skipped > {tmp_path / 'out'}") == 0
    assert (tmp_path / "out").read_text() == "skipped\n"


@pytest.mark.requires_real_stdio
def test_in_a_pipeline_it_refuses(sh, tmp_path, answer):
    # Its stdin is a pipe: there is no one to answer.
    asked = answer("y")
    sh._execute(f"confirm | cat > {tmp_path / 'out'}")
    assert asked == []
