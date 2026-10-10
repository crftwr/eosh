"""Tests for the AWS recipe — AwsCompleter, registration, and var preservation."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from eosh.commands import registry as command_registry
from eosh.completion import Completion, CompletionContext
from eosh.recipes import aws as aws_recipe
from eosh.recipes.aws import (
    AwsCompleter,
    AwsProfileCompleter,
    AwsRegionCompleter,
    AWS_REGIONS,
)
from eosh.variables import registry as var_registry


def make_ctx(line: str, prefix: str, args=None, command="aws"):
    return CompletionContext(
        command=command,
        args=args or [],
        arg_index=len(args) if args else 0,
        prefix=prefix,
        line=line,
        shell_context=None,
    )


def _completed(stdout: str, returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


# ---------------------------------------------------------------------------
# AwsCompleter
# ---------------------------------------------------------------------------

def test_completer_invokes_aws_completer_with_comp_line_and_point():
    cc = AwsCompleter()
    line = "aws ec2 describe-"
    with patch("eosh.recipes.aws.shutil.which", return_value="/usr/local/bin/aws_completer"), \
         patch("eosh.recipes.aws.subprocess.run", return_value=_completed("describe-instances\n")) as run:
        cc.complete(make_ctx(line, "describe-", args=["ec2"]))
    args, kwargs = run.call_args
    assert args[0] == ["aws_completer"]
    env = kwargs["env"]
    assert env["COMP_LINE"] == line
    assert env["COMP_POINT"] == str(len(line))
    # Default timeout is generous enough for AWS API calls.
    assert kwargs["timeout"] == 5.0


def test_completer_filters_by_prefix():
    cc = AwsCompleter()
    with patch("eosh.recipes.aws.shutil.which", return_value="/usr/local/bin/aws_completer"), \
         patch("eosh.recipes.aws.subprocess.run",
               return_value=_completed("ec2\necr\nec2-instance-connect\nec2messages\nelasticache\n")):
        results = cc.complete(make_ctx("aws ec", "ec", args=[]))
    # Order matches aws_completer's stdout.
    assert [c.value for c in results] == ["ec2", "ecr", "ec2-instance-connect", "ec2messages"]


def test_completer_returns_plain_completions_no_descriptions():
    cc = AwsCompleter()
    with patch("eosh.recipes.aws.shutil.which", return_value="/usr/local/bin/aws_completer"), \
         patch("eosh.recipes.aws.subprocess.run", return_value=_completed("us-east-1\nus-west-2\n")):
        results = cc.complete(make_ctx("aws ec2 describe-instances --region ", "", args=["ec2", "describe-instances", "--region"]))
    assert all(isinstance(c, Completion) for c in results)
    assert all(c.description == "" for c in results)


def test_completer_skips_when_binary_missing():
    cc = AwsCompleter()
    with patch("eosh.recipes.aws.shutil.which", return_value=None), \
         patch("eosh.recipes.aws.subprocess.run") as run:
        results = cc.complete(make_ctx("aws ec2 ", "", args=["ec2"]))
    assert results == []
    run.assert_not_called()


def test_completer_handles_subprocess_failure():
    cc = AwsCompleter()
    with patch("eosh.recipes.aws.shutil.which", return_value="/usr/local/bin/aws_completer"), \
         patch("eosh.recipes.aws.subprocess.run", side_effect=OSError("boom")):
        assert cc.complete(make_ctx("aws ec2 ", "", args=["ec2"])) == []


def test_completer_handles_timeout(capsys):
    cc = AwsCompleter(timeout=0.1)
    with patch("eosh.recipes.aws.shutil.which", return_value="/usr/local/bin/aws_completer"), \
         patch("eosh.recipes.aws.subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd="aws_completer", timeout=0.1)):
        assert cc.complete(make_ctx("aws ec2 ", "", args=["ec2"])) == []
    # User sees a brief notice on stderr — better than silent file fallback.
    err = capsys.readouterr().err
    assert "aws_completer timed out" in err
    assert "0.1s" in err


def test_completer_handles_nonzero_exit():
    cc = AwsCompleter()
    with patch("eosh.recipes.aws.shutil.which", return_value="/usr/local/bin/aws_completer"), \
         patch("eosh.recipes.aws.subprocess.run",
               return_value=_completed("ignored\n", returncode=1)):
        assert cc.complete(make_ctx("aws ec2 ", "", args=["ec2"])) == []


def test_completer_drops_blank_lines():
    cc = AwsCompleter()
    with patch("eosh.recipes.aws.shutil.which", return_value="/usr/local/bin/aws_completer"), \
         patch("eosh.recipes.aws.subprocess.run",
               return_value=_completed("ec2\n\necr\n\n")):
        results = cc.complete(make_ctx("aws e", "e", args=[]))
    assert [c.value for c in results] == ["ec2", "ecr"]


def test_completer_caches_per_line():
    cc = AwsCompleter()
    with patch("eosh.recipes.aws.shutil.which", return_value="/usr/local/bin/aws_completer"), \
         patch("eosh.recipes.aws.subprocess.run",
               return_value=_completed("ec2\n")) as run:
        cc.complete(make_ctx("aws e", "e", args=[]))
        cc.complete(make_ctx("aws e", "e", args=[]))
    assert run.call_count == 1


def test_completer_cache_invalidates_on_line_change():
    cc = AwsCompleter()
    with patch("eosh.recipes.aws.shutil.which", return_value="/usr/local/bin/aws_completer"), \
         patch("eosh.recipes.aws.subprocess.run",
               return_value=_completed("ec2\n")) as run:
        cc.complete(make_ctx("aws e", "e", args=[]))
        cc.complete(make_ctx("aws ec", "ec", args=[]))
    assert run.call_count == 2


# ---------------------------------------------------------------------------
# Recipe registration — delegate completer routes flags + every positional
# ---------------------------------------------------------------------------

@pytest.fixture
def _clean_aws_registration(monkeypatch):
    """Snapshot/restore the registry state mutated by aws_recipe.register()."""
    had_aws = "aws" in command_registry._commands
    saved_aws = command_registry._commands.get("aws")
    yield
    if had_aws:
        command_registry._commands["aws"] = saved_aws
    else:
        command_registry._commands.pop("aws", None)
    for name in ("aws_region", "aws_profile"):
        if name not in var_registry._builtins:
            var_registry._vars.pop(name, None) if hasattr(var_registry, "_vars") else None


def test_register_installs_delegate_completer(_clean_aws_registration):
    aws_recipe.register()
    cmd = command_registry.get("aws")
    assert cmd is not None
    # AwsCompleter answers every slot; S3 and local paths are overlaid on it.
    from eosh.completion import OverlayCompleter
    assert isinstance(cmd.delegate, OverlayCompleter)
    assert isinstance(cmd.delegate._parts[0], AwsCompleter)


def test_register_preserves_aws_region_var(_clean_aws_registration):
    from eosh.variables import EnvVar
    aws_recipe.register()
    region = var_registry.get("aws_region")
    assert isinstance(region, EnvVar)
    assert region.keys == ("AWS_REGION", "AWS_DEFAULT_REGION")
    assert isinstance(region.value_completer, AwsRegionCompleter)


def test_register_preserves_aws_profile_var(_clean_aws_registration):
    aws_recipe.register()
    profile = var_registry.get("aws_profile")
    assert profile is not None
    assert profile.name == "aws_profile"
    assert isinstance(profile.value_completer, AwsProfileCompleter)


# ---------------------------------------------------------------------------
# aws_region — the shell writes both keys; value completion
# ---------------------------------------------------------------------------

def test_aws_region_assignment_sets_and_unsets_both_keys(_clean_aws_registration, monkeypatch):
    """The shell writes every key of the EnvVar, through the context manager."""
    import os
    from eosh.shell import Shell

    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    aws_recipe.register()
    sh = Shell()
    sh._set_variable("aws_region", "us-east-1")
    assert os.environ["AWS_REGION"] == os.environ["AWS_DEFAULT_REGION"] == "us-east-1"
    assert sh.context_manager.current().variables["AWS_DEFAULT_REGION"] == "us-east-1"
    sh._unset_variable("aws_region")
    assert "AWS_REGION" not in os.environ and "AWS_DEFAULT_REGION" not in os.environ


def test_aws_region_completer_returns_descriptions():
    c = AwsRegionCompleter()
    results = c.complete(make_ctx("var aws_region=us-e", "us-e"))
    values = [r.value for r in results]
    assert "us-east-1" in values
    assert "us-east-2" in values
    # Descriptions are surfaced from AWS_REGIONS.
    east1 = next(r for r in results if r.value == "us-east-1")
    assert "N. Virginia" in east1.description
