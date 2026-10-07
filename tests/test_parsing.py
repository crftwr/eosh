import os

import pytest

from eosh.parsing import (
    expand_vars,
    raw_token_start,
    split_for_completion,
    tokenize,
)
from eosh.variables import EnvVar, Var, VarRegistry, registry as var_registry


def test_expand_vars_basic():
    os.environ["EOSH_TEST_VAR"] = "hello"
    assert expand_vars("echo $EOSH_TEST_VAR") == "echo hello"
    del os.environ["EOSH_TEST_VAR"]


def test_expand_vars_braces():
    os.environ["EOSH_TEST_VAR"] = "world"
    assert expand_vars("echo ${EOSH_TEST_VAR}!") == "echo world!"
    del os.environ["EOSH_TEST_VAR"]


def test_expand_vars_unset_is_empty():
    os.environ.pop("EOSH_TEST_UNSET", None)
    assert expand_vars("echo $EOSH_TEST_UNSET") == "echo "


def test_expand_vars_single_quoted_not_expanded():
    os.environ["EOSH_TEST_VAR"] = "hello"
    assert expand_vars("echo '$EOSH_TEST_VAR'") == "echo '$EOSH_TEST_VAR'"
    del os.environ["EOSH_TEST_VAR"]


def test_expand_vars_no_vars():
    assert expand_vars("ls -la /tmp") == "ls -la /tmp"


def test_tokenize_simple():
    assert tokenize("ls -la /tmp") == ["ls", "-la", "/tmp"]


def test_tokenize_quoted():
    assert tokenize('echo "hello world"') == ["echo", "hello world"]


def test_tokenize_empty():
    assert tokenize("") == []


def test_split_for_completion_mid_word():
    tokens, prefix = split_for_completion("ls -l /tm")
    assert tokens == ["ls", "-l"]
    assert prefix == "/tm"


def test_split_for_completion_trailing_space():
    tokens, prefix = split_for_completion("ls -l ")
    assert tokens == ["ls", "-l"]
    assert prefix == ""


def test_split_for_completion_command_only():
    tokens, prefix = split_for_completion("gi")
    assert tokens == []
    assert prefix == "gi"


def test_split_for_completion_empty():
    tokens, prefix = split_for_completion("")
    assert tokens == []
    assert prefix == ""


@pytest.fixture
def temp_var():
    """Register a Var for the test, yield its name, clean up on exit."""
    registered: list[str] = []

    def _register(var: Var) -> str:
        var_registry.register(var)
        registered.append(var.name)
        return var.name

    yield _register

    for name in registered:
        var_registry._vars.pop(name, None)


def test_expand_vars_var_registry_takes_precedence(temp_var):
    temp_var(EnvVar("aws_region", keys=["AWS_REGION", "AWS_DEFAULT_REGION"]))
    os.environ["AWS_REGION"] = "us-west-2"
    os.environ["AWS_DEFAULT_REGION"] = "us-west-2"
    try:
        assert expand_vars("echo $aws_region") == "echo us-west-2"
        assert expand_vars("echo ${aws_region}") == "echo us-west-2"
    finally:
        del os.environ["AWS_REGION"]
        del os.environ["AWS_DEFAULT_REGION"]


def test_expand_vars_env_var_subclass(temp_var):
    temp_var(EnvVar("aws_profile", "AWS_PROFILE"))
    os.environ["AWS_PROFILE"] = "dev"
    try:
        assert expand_vars("aws --profile $aws_profile") == "aws --profile dev"
    finally:
        del os.environ["AWS_PROFILE"]


def test_expand_vars_var_unset_renders_empty(temp_var):
    temp_var(EnvVar("aws_profile", "AWS_PROFILE"))
    os.environ.pop("AWS_PROFILE", None)
    assert expand_vars("echo [$aws_profile]") == "echo []"


def test_expand_vars_falls_back_to_env_when_no_var(temp_var):
    os.environ["EOSH_PLAIN"] = "plain"
    try:
        # No Var registered for this name — env lookup still works.
        assert expand_vars("echo $EOSH_PLAIN") == "echo plain"
    finally:
        del os.environ["EOSH_PLAIN"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", 0),
        ("gi", 0),
        ("git ", 4),
        ("git com", 4),
        ("git commit  ", 12),          # runs of whitespace anchor after the last one
        ("cat 'My Documents/no", 4),   # unclosed quote — the rest is one token
        ("cat 'My Documents' x", 19),  # closed quote — the token after it
        ("ls | grep fo", 10),          # operators are not special here
    ],
)
def test_raw_token_start(text, expected):
    assert raw_token_start(text) == expected


def test_raw_token_start_agrees_with_split_for_completion():
    """For an unquoted token the anchor and the shlex prefix must line up."""
    line = "git com"
    _, prefix = split_for_completion(line)
    assert line[raw_token_start(line):] == prefix
