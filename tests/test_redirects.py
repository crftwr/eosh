"""Tests for shell-level redirect failure handling.

A missing/unwritable redirect target must not crash the shell — bash and
zsh both print an error and continue.  Issue #14 had the user typing
``squeue -j <JOBID>`` (literally, including the angle brackets as a
placeholder); the parser interpreted ``<JOBID>`` as a stdin redirect to
file ``JOBID>``, which raised an uncaught ``FileNotFoundError`` and
killed the shell.
"""

from __future__ import annotations

import pytest

from pitash.shell import Shell


@pytest.fixture
def shell():
    return Shell()


def test_missing_stdin_redirect_target_single_stage(shell, capsys):
    shell._execute("cat < /no/such/file_xyz123")
    err = capsys.readouterr().err
    assert "/no/such/file_xyz123" in err


def test_missing_stdin_redirect_target_in_pipeline(shell, capsys):
    shell._execute("cat < /no/such/file_xyz123 | cat")
    err = capsys.readouterr().err
    assert "/no/such/file_xyz123" in err


def test_squeue_jobid_placeholder_does_not_crash(shell, capsys):
    """`squeue -j <JOBID>` (the angle brackets are a placeholder) must
    not crash the shell: parser treats ``<JOBID>`` as stdin redirect to
    file ``JOBID>``, which doesn't exist."""
    shell._execute("squeue -j <JOBID>")
    err = capsys.readouterr().err
    assert "JOBID>" in err


def test_unwritable_stdout_redirect_does_not_crash(shell, capsys):
    """`echo hi > /no/such/dir/out.txt` — open(..., 'wb') raises
    FileNotFoundError; shell must report and continue."""
    shell._execute("echo hi > /no/such/dir_xyz/out.txt")
    err = capsys.readouterr().err
    assert "/no/such/dir_xyz/out.txt" in err
