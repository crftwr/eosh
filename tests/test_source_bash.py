"""Tests for the `source-bash` built-in — run bash, import what it left behind.

The interesting part is the round trip: a child bash exits and takes its
`export`s with it, so `_run_bash_script` has the child dump its final
environment and cwd, and `_apply_bash_env` folds that back into this shell.
"""

import os

import pytest

from pitash.shell import (
    Shell,
    _bash_env_ignored,
    _parse_bash_env_dump,
)


@pytest.fixture
def sh(monkeypatch, tmp_path):
    """A Shell with the process environment and cwd restored afterwards.

    `_apply_bash_env` writes real `os.environ` keys and calls `os.chdir`, so
    every test that touches it needs the session's state put back.
    """
    saved_env = dict(os.environ)
    saved_cwd = os.getcwd()
    shell = Shell()
    try:
        yield shell
    finally:
        os.chdir(saved_cwd)
        os.environ.clear()
        os.environ.update(saved_env)


# ── dump parsing ───────────────────────────────────────────────────────────

def test_parse_dump_splits_cwd_and_env():
    cwd, env = _parse_bash_env_dump("/tmp\0A=1\0B=two\0")
    assert cwd == "/tmp"
    assert env == {"A": "1", "B": "two"}


def test_parse_dump_keeps_newlines_and_equals_in_values():
    cwd, env = _parse_bash_env_dump("/x\0MULTI=one\ntwo\0EQ=a=b\0EMPTY=\0")
    assert cwd == "/x"
    assert env == {"MULTI": "one\ntwo", "EQ": "a=b", "EMPTY": ""}


def test_parse_dump_empty_means_no_dump():
    assert _parse_bash_env_dump("") == (None, {})


def test_parse_dump_cwd_only():
    assert _parse_bash_env_dump("/only\0") == ("/only", {})


def test_bookkeeping_keys_are_ignored():
    assert _bash_env_ignored("_")
    assert _bash_env_ignored("SHLVL")
    assert _bash_env_ignored("PWD")
    assert _bash_env_ignored("BASH_FUNC_foo%%")
    assert not _bash_env_ignored("AWS_REGION")


# ── running a script and reading the dump back ─────────────────────────────

def test_run_bash_script_reports_exports_and_cwd(sh, tmp_path):
    code, cwd, env = sh._run_bash_script(
        f"export PITASH_TEST_A=hello\ncd {tmp_path}\n"
    )
    assert code == 0
    assert os.path.realpath(cwd) == os.path.realpath(str(tmp_path))
    assert env["PITASH_TEST_A"] == "hello"


def test_run_bash_script_dumps_even_when_the_script_exits_nonzero(sh):
    """The dump runs from an EXIT trap, so `exit 3` still hands the env back."""
    code, cwd, env = sh._run_bash_script("export PITASH_TEST_B=bee\nexit 3\n")
    assert code == 3
    assert cwd is not None
    assert env["PITASH_TEST_B"] == "bee"


def test_run_bash_script_dumps_after_set_e_failure(sh):
    code, cwd, env = sh._run_bash_script(
        "set -e\nexport PITASH_TEST_C=cee\nfalse\nexport PITASH_TEST_D=dee\n"
    )
    assert code != 0
    assert env["PITASH_TEST_C"] == "cee"
    assert "PITASH_TEST_D" not in env


def test_run_bash_script_handles_bash_only_syntax(sh):
    """The point of delegating: `$(…)` and loops that pitash cannot parse."""
    code, _, env = sh._run_bash_script(
        'export PITASH_TEST_SUB="$(echo sub)"\n'
        'for i in 1 2 3; do export PITASH_TEST_LOOP="$i"; done\n'
    )
    assert code == 0
    assert env["PITASH_TEST_SUB"] == "sub"
    assert env["PITASH_TEST_LOOP"] == "3"


# ── importing the dump into the shell ──────────────────────────────────────

def test_apply_sets_new_and_changed_vars(sh):
    os.environ["PITASH_TEST_OLD"] = "before"
    env = dict(os.environ)
    env["PITASH_TEST_OLD"] = "after"
    env["PITASH_TEST_NEW"] = "fresh"

    changed, removed, new_cwd = sh._apply_bash_env(os.getcwd(), env)

    assert os.environ["PITASH_TEST_OLD"] == "after"
    assert os.environ["PITASH_TEST_NEW"] == "fresh"
    assert changed == ["PITASH_TEST_NEW", "PITASH_TEST_OLD"]
    assert removed == []
    assert new_cwd is None


def test_apply_unsets_what_the_script_unset(sh):
    os.environ["PITASH_TEST_GONE"] = "x"
    env = {k: v for k, v in os.environ.items() if k != "PITASH_TEST_GONE"}

    changed, removed, _ = sh._apply_bash_env(os.getcwd(), env)

    assert "PITASH_TEST_GONE" not in os.environ
    assert removed == ["PITASH_TEST_GONE"]
    assert changed == []


def test_apply_ignores_bash_bookkeeping(sh):
    env = dict(os.environ)
    env["_"] = "/usr/bin/env"
    env["SHLVL"] = "9"
    env["OLDPWD"] = "/nowhere"

    changed, removed, _ = sh._apply_bash_env(os.getcwd(), env)

    assert changed == []
    assert removed == []
    assert os.environ.get("SHLVL") != "9"


def test_apply_imports_cwd(sh, tmp_path):
    changed, removed, new_cwd = sh._apply_bash_env(str(tmp_path), dict(os.environ))
    assert os.path.realpath(os.getcwd()) == os.path.realpath(str(tmp_path))
    assert os.path.realpath(new_cwd) == os.path.realpath(str(tmp_path))
    assert os.environ["PWD"] == os.getcwd()


def test_apply_respects_no_cd(sh, tmp_path):
    here = os.getcwd()
    _, _, new_cwd = sh._apply_bash_env(
        str(tmp_path), dict(os.environ), import_cwd=False
    )
    assert new_cwd is None
    assert os.getcwd() == here


def test_apply_leaves_non_identifier_keys_alone(sh):
    """A key bash can't bind to a variable may be missing from the dump
    without the script having unset anything — don't remove it."""
    os.environ["not-an-identifier"] = "keep"
    env = {k: v for k, v in os.environ.items() if k != "not-an-identifier"}

    _, removed, _ = sh._apply_bash_env(os.getcwd(), env)

    assert removed == []
    assert os.environ["not-an-identifier"] == "keep"


# ── end to end through the registered command ──────────────────────────────

def test_command_sources_a_file_with_arguments(sh, tmp_path, capfd):
    script = tmp_path / "setup.sh"
    script.write_text('export PITASH_TEST_ARG="$1"\n')

    sh.registry.get("source-bash").invoke([str(script), "s3"])

    assert os.environ["PITASH_TEST_ARG"] == "s3"
    out = capfd.readouterr().out
    assert "PITASH_TEST_ARG" in out


def test_command_c_flag_runs_inline_script(sh):
    sh.registry.get("source-bash").invoke(["-c", "export PITASH_TEST_INLINE=yes", "-q"])
    assert os.environ["PITASH_TEST_INLINE"] == "yes"


def test_command_summary_never_prints_values(sh, capfd):
    sh.registry.get("source-bash").invoke(["-c", "export PITASH_TEST_SECRET=hunter2"])
    out = capfd.readouterr().out
    assert "PITASH_TEST_SECRET" in out
    assert "hunter2" not in out


def test_command_reports_missing_file(sh, capfd):
    sh.registry.get("source-bash").invoke([str(tmp := "no_such_script.sh")])
    assert tmp in capfd.readouterr().out


def test_command_rejects_c_together_with_a_file(sh, capfd):
    sh.registry.get("source-bash").invoke(["-c", "true", "extra.sh"])
    assert "don't pass a FILE too" in capfd.readouterr().out


def test_command_reads_a_pasted_block(sh, monkeypatch, capfd):
    """Paste mode: the block reader supplies the script body."""
    import pitash.shell as shell_mod

    monkeypatch.setattr(
        shell_mod, "passthrough_input_block",
        lambda prompt="": 'export PITASH_TEST_PASTE_A="s3"\n'
                          'export PITASH_TEST_PASTE_B="us-east-1"\n',
    )
    sh.registry.get("source-bash").invoke([])

    assert os.environ["PITASH_TEST_PASTE_A"] == "s3"
    assert os.environ["PITASH_TEST_PASTE_B"] == "us-east-1"


def test_command_reports_empty_paste(sh, monkeypatch, capfd):
    import pitash.shell as shell_mod

    monkeypatch.setattr(shell_mod, "passthrough_input_block", lambda prompt="": "  \n")
    sh.registry.get("source-bash").invoke([])
    assert "nothing to run" in capfd.readouterr().out
