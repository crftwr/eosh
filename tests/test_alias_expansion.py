"""Tests for alias expansion in command-line tokenization and completion."""

from pitash.commands import CommandRegistry
from pitash.completion import CommandNameCompleter, CompletionContext
from pitash.shell import Shell


def _expand(reg: CommandRegistry, tokens: list[str]) -> list[str]:
    """Run Shell._expand_alias against *reg*'s alias table without instantiating Shell."""
    # Bound-method call without a real Shell — _expand_alias only touches self.registry
    class _Stub:
        registry = reg
    return Shell._expand_alias(_Stub(), tokens)


def test_first_token_alias_replaced_with_expansion_tokens():
    reg = CommandRegistry()
    reg.alias("hp", "awsut hyperpod")
    assert _expand(reg, ["hp", "create", "foo"]) == ["awsut", "hyperpod", "create", "foo"]


def test_alias_with_no_args():
    reg = CommandRegistry()
    reg.alias("hp", "awsut hyperpod")
    assert _expand(reg, ["hp"]) == ["awsut", "hyperpod"]


def test_unaliased_first_token_passes_through():
    reg = CommandRegistry()
    reg.alias("hp", "awsut hyperpod")
    assert _expand(reg, ["ls", "-la"]) == ["ls", "-la"]


def test_alias_does_not_chain():
    """If 'hp' expands to 'awsut hyperpod' and 'awsut' itself is also an alias,
    the second alias must NOT be applied — preventing cycles."""
    reg = CommandRegistry()
    reg.alias("hp", "awsut hyperpod")
    reg.alias("awsut", "should_not_expand")
    assert _expand(reg, ["hp", "create"]) == ["awsut", "hyperpod", "create"]


def test_alias_expansion_with_flags():
    reg = CommandRegistry()
    reg.alias("la", "ls -la")
    assert _expand(reg, ["la", "/tmp"]) == ["ls", "-la", "/tmp"]


def test_alias_only_replaces_first_token():
    reg = CommandRegistry()
    reg.alias("hp", "awsut hyperpod")
    # 'hp' as the second token must NOT expand — only the command position does.
    assert _expand(reg, ["echo", "hp", "create"]) == ["echo", "hp", "create"]


def test_empty_token_list_returned_unchanged():
    reg = CommandRegistry()
    reg.alias("hp", "awsut hyperpod")
    assert _expand(reg, []) == []


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
    from pitash.commands import arg
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
    from pitash.commands import arg
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
