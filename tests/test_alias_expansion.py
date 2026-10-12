"""Tests for alias expansion: as text, before the line is parsed, and in completion."""

import pytest

from eosh.commands import CommandRegistry, arg
from eosh.completion import CommandNameCompleter, CompletionContext
from eosh.pipeline import expand_aliases
from eosh.shell import Shell


def _expand(reg: CommandRegistry, line: str) -> str:
    return expand_aliases(line, reg.get_alias)


def _reg(**aliases) -> CommandRegistry:
    reg = CommandRegistry()
    for name, expansion in aliases.items():
        reg.alias(name, expansion)
    return reg


# ── expand_aliases: text, before the line is parsed (as bash does) ──────────

def test_the_first_word_is_replaced():
    reg = _reg(hp="awsut hyperpod")
    assert _expand(reg, "hp create foo") == "awsut hyperpod create foo"
    assert _expand(reg, "hp") == "awsut hyperpod"
    assert _expand(reg, "ls -la") == "ls -la"


def test_only_command_positions_expand():
    reg = _reg(hp="awsut hyperpod")
    assert _expand(reg, "echo hp create") == "echo hp create"
    assert _expand(reg, "cat < hp") == "cat < hp"


@pytest.mark.parametrize("line, expanded", [
    ("ls; hp", "ls; awsut hyperpod"),
    ("ls && hp || hp", "ls && awsut hyperpod || awsut hyperpod"),
    ("ls | hp", "ls | awsut hyperpod"),
    ("FOO=1 hp", "FOO=1 awsut hyperpod"),
])
def test_every_command_of_the_line_expands(line, expanded):
    assert _expand(_reg(hp="awsut hyperpod"), line) == expanded


@pytest.mark.parametrize("line", ["\\hp", "'hp'", '"hp"', "echo 'a; hp'"])
def test_a_quoted_or_escaped_word_is_not_an_alias(line):
    assert _expand(_reg(hp="awsut hyperpod"), line) == line


def test_an_alias_may_hold_operators_and_redirects():
    reg = _reg(gl="git log --oneline | head -20", both="make; make test")
    assert _expand(reg, "gl") == "git log --oneline | head -20"
    assert _expand(reg, "both > log") == "make; make test > log"


def test_aliases_chain_but_never_into_themselves():
    reg = _reg(ll="ls -l", ls="ls -G")
    assert _expand(reg, "ll") == "ls -G -l"
    assert _expand(reg, "ls") == "ls -G"
    loop = _reg(a="b", b="a")
    assert _expand(loop, "a") == "a"


def test_a_trailing_space_makes_the_next_word_a_command():
    reg = _reg(sudo="sudo ", ll="ls -l")
    assert _expand(reg, "sudo ll").split() == ["sudo", "ls", "-l"]


def test_a_decorators_body_is_a_command_position():
    Shell()       # registers the built-in decorators: their flags decide the body
    reg = _reg(ll="ls -l", w="@watch -n 1")
    assert _expand(reg, "@time ll") == "@time ls -l"
    assert _expand(reg, "@retry -n 3 ll") == "@retry -n 3 ls -l"
    assert _expand(reg, "@time {ll && ll}") == "@time {ls -l && ls -l}"
    assert _expand(reg, "w ll") == "@watch -n 1 ls -l"


# ── what the shell runs ─────────────────────────────────────────────────────

@pytest.fixture
def sh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shell = Shell()
    yield shell
    shell.registry.clear_user_commands()


def test_an_alias_with_a_pipe_runs_as_a_pipeline(sh, tmp_path):
    sh._execute("alias two='printf \"a\\nb\\nc\\n\" | head -2'")
    sh._execute(f"two > {tmp_path / 'out'}")
    assert (tmp_path / "out").read_text() == "a\nb\n"


def test_an_alias_with_a_sequence_runs_both(sh, tmp_path):
    out = tmp_path / "out"
    sh._execute(f"alias both='echo a >> {out}; echo b >> {out}'")
    sh._execute("both")
    assert out.read_text() == "a\nb\n"


def test_an_alias_reads_variables_when_it_runs(sh, tmp_path, monkeypatch):
    monkeypatch.setenv("EOSH_T_ALIAS", "first")
    sh._execute(f"alias show='echo $EOSH_T_ALIAS > {tmp_path / 'out'}'")
    monkeypatch.setenv("EOSH_T_ALIAS", "second")
    sh._execute("show")
    assert (tmp_path / "out").read_text() == "second\n"


def test_an_alias_can_ask_first(sh, tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    out = tmp_path / "out"
    sh._execute(f"alias deploy='confirm && echo ran > {out}'")
    assert sh._execute("deploy") == 1
    assert not out.exists()


def test_history_keeps_the_line_as_typed(sh):
    sh._execute("alias _t_e='echo hi'")
    line = "_t_e"
    sh._record_history(line)
    sh._execute(line)
    assert [e.cmd for e in sh._history.entries()][-1] == "_t_e"


# ── completion ──────────────────────────────────────────────────────────────

def test_completion_reads_through_an_alias(sh):
    sh.registry.command("_t_target", help="t", params=[arg("--mode", choices=["a", "b"])])
    sh.registry.alias("_t_al", "_t_target")
    comps, _, _ = sh._get_completions("_t_al --mo")
    assert [c.value for c in comps] == ["--mode"]


def test_the_word_being_typed_is_not_expanded(sh):
    sh.registry.alias("_t_al", "echo expanded")
    comps, prefix, label = sh._get_completions("_t_al")
    assert prefix == "_t_al" and label == "command"


def test_alias_listed_in_command_name_completion():
    reg = CommandRegistry()
    reg.alias("hp", "awsut hyperpod")
    completer = CommandNameCompleter(reg)
    ctx = CompletionContext(command=None, args=[], arg_index=0, prefix="hp",
                            line="hp", shell_context=None)
    results = completer.complete(ctx)
    aliases = [r for r in results if r.value == "hp"]
    assert len(aliases) == 1
    assert "alias" in aliases[0].description
    assert "awsut hyperpod" in aliases[0].description


# ---------------------------------------------------------------------------
# Status-bar help message (Shell._get_arg_info) for aliases — issue #8.
#
# The status bar should resolve aliases the same way completion does so the
# user gets meaningful help when they type, e.g., `hp create <TAB>` where
# `hp` is `alias hp='awsut sagemaker hyperpod'` (the fixtures below use a
# shorter two-token stand-in — what is under test is the expansion, not the
# tree it lands in).
# ---------------------------------------------------------------------------

def test_arg_info_on_alias_command_name_shows_expansion():
    """Caret on the alias name itself shows its expansion."""
    from eosh.commands import arg
    sh = Shell()
    sh.registry.alias("_t_hp", "_t_awsut hyperpod")

    # Register a fake target so the description is populated.
    sh.registry.command(
        "_t_awsut",
        help="awsut: Amazon Web Services User Tool",
        params=[arg("subcommand", choices=["hyperpod"])],
    )
    try:
        info = sh._get_arg_info("_t_hp", cursor=len("_t_hp"))
        assert info is not None
        assert "_t_hp" in info
        assert "_t_awsut hyperpod" in info
    finally:
        sh.registry.unalias("_t_hp")
        if "_t_awsut" in sh.registry._commands:
            del sh.registry._commands["_t_awsut"]


def test_arg_info_after_alias_resolves_to_expansion_target():
    """Caret on a positional after `hp` should describe the expanded
    command's positional, not return None."""
    from eosh.commands import arg
    sh = Shell()
    sh.registry.alias("_t_hp", "_t_awsut hyperpod")
    sh.registry.command(
        "_t_awsut",
        help="awsut: helper",
        params=[arg("subcommand", choices=["hyperpod"]),
                arg("action", help="action to take")],
    )
    try:
        # User typed `_t_hp <action>`; caret on the action token.
        buf = "_t_hp create"
        info = sh._get_arg_info(buf, cursor=len(buf))
        # Should yield the action positional's label rather than None.
        assert info is not None
    finally:
        sh.registry.unalias("_t_hp")
        if "_t_awsut" in sh.registry._commands:
            del sh.registry._commands["_t_awsut"]


def test_arg_info_on_alias_falls_back_when_target_missing():
    """If the alias's expansion target isn't registered, still surface
    the alias→expansion mapping so the user sees what it expands to."""
    sh = Shell()
    sh.registry.alias("_t_hp", "no_such_cmd")
    try:
        info = sh._get_arg_info("_t_hp", cursor=len("_t_hp"))
        assert info is not None
        assert "_t_hp" in info
        assert "no_such_cmd" in info
    finally:
        sh.registry.unalias("_t_hp")
