"""Tests for per-command environment prefixes (``FOO=bar cmd args``).

Issue #24: support the POSIX idiom of prefixing a command with one or more
``KEY=VALUE`` assignments that apply only to that command's environment,
without mutating the shell's own environment.
"""

from __future__ import annotations

import os
import sys

import pytest

from eosh.shell import Shell


@pytest.fixture
def shell():
    return Shell()


# ── _split_env_prefix unit behaviour ────────────────────────────────────────


def test_split_env_prefix_basic(shell):
    env, rest = shell._split_env_prefix(["FOO=bar", "BAZ=qux", "make", "run"])
    assert env == {"FOO": "bar", "BAZ": "qux"}
    assert rest == ["make", "run"]


def test_split_env_prefix_stops_at_command(shell):
    """Assignments after the command name are left as command arguments
    (e.g. ``make FOO=bar`` — a Makefile override, not an env prefix)."""
    env, rest = shell._split_env_prefix(["make", "FOO=bar"])
    assert env == {}
    assert rest == ["make", "FOO=bar"]


def test_split_env_prefix_pure_assignment(shell):
    """A line that is only assignments is not a prefix — the caller keeps
    the permanent-assignment behaviour."""
    env, rest = shell._split_env_prefix(["FOO=bar", "BAZ=qux"])
    assert env == {}
    assert rest == ["FOO=bar", "BAZ=qux"]


def test_split_env_prefix_none(shell):
    env, rest = shell._split_env_prefix(["ls", "-la"])
    assert env == {}
    assert rest == ["ls", "-la"]


def test_split_env_prefix_empty_value(shell):
    env, rest = shell._split_env_prefix(["FOO=", "cmd"])
    assert env == {"FOO": ""}
    assert rest == ["cmd"]


# ── integration: external command receives the env, shell does not keep it ───


def test_external_command_receives_env_prefix(shell, tmp_path):
    out = tmp_path / "out.txt"
    marker = "eosh_env_prefix_marker"
    assert marker not in os.environ  # sanity
    shell._execute(f"EOSH_TEST_VAR={marker} sh -c 'echo $EOSH_TEST_VAR' > {out}")
    assert out.read_text().strip() == marker


def test_env_prefix_does_not_persist_in_shell(shell, tmp_path):
    out = tmp_path / "out.txt"
    shell._execute(f"EOSH_TEST_ONESHOT=xyz sh -c 'echo hi' > {out}")
    # The prefix applied to the child only — the shell's own environ is clean.
    assert "EOSH_TEST_ONESHOT" not in os.environ


def test_env_prefix_restores_prior_value(shell, tmp_path):
    """A prefix that shadows an existing env var restores it afterward."""
    os.environ["EOSH_TEST_PRESET"] = "original"
    try:
        out = tmp_path / "out.txt"
        shell._execute(
            f"EOSH_TEST_PRESET=temporary sh -c 'echo $EOSH_TEST_PRESET' > {out}"
        )
        assert out.read_text().strip() == "temporary"
        assert os.environ["EOSH_TEST_PRESET"] == "original"
    finally:
        os.environ.pop("EOSH_TEST_PRESET", None)


def test_env_prefix_in_pipeline_stage(shell, tmp_path):
    out = tmp_path / "out.txt"
    shell._execute(
        f"EOSH_TEST_PIPE=piped sh -c 'echo $EOSH_TEST_PIPE' | cat > {out}"
    )
    assert out.read_text().strip() == "piped"
    assert "EOSH_TEST_PIPE" not in os.environ
