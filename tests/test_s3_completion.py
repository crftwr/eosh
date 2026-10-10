"""S3 path completion and OverlayCompleter (discussion #33)."""

import json
import subprocess

import pytest

from eosh.completion import (
    ChoiceCompleter, Completer, Completion, CompletionContext, OverlayCompleter,
)
from eosh.recipes import aws as aws_recipe
from eosh.completion import FileCompleter
from eosh.recipes.aws import S3PathCompleter, _AwsS3PathArg, _S3_LOCAL_OPS, _S3_REMOTE_OPS


def _ctx(line):
    """A CompletionContext for an ``aws …`` line, cursor at the end."""
    words = line.split(" ")
    return CompletionContext(command=words[0], args=words[1:-1],
                             arg_index=len(words) - 2, prefix=words[-1], line=line)


@pytest.fixture
def aws_cli(monkeypatch):
    """Fake ``aws s3api``: records argv, answers from ``responses``."""
    calls = []
    responses = {
        "list-buckets": {"Buckets": [
            {"Name": "logs", "CreationDate": "2024-01-02T03:04:05Z"},
            {"Name": "data", "CreationDate": "2023-05-06T00:00:00Z"},
        ]},
        "list-objects-v2": {
            "CommonPrefixes": [{"Prefix": "2026/raw/"}],
            "Contents": [
                {"Key": "2026/", "Size": 0},                       # folder marker
                {"Key": "2026/report.csv", "Size": 2048,
                 "LastModified": "2026-10-01T12:34:56Z"},
            ],
        },
    }

    def run(argv, **kw):
        calls.append(argv)
        out = responses.get(argv[2])
        if out is None:
            return subprocess.CompletedProcess(argv, 255, "", "An error occurred (AccessDenied)\n")
        return subprocess.CompletedProcess(argv, 0, json.dumps(out), "")

    monkeypatch.setattr(aws_recipe.subprocess, "run", run)
    monkeypatch.setattr(aws_recipe.shutil, "which", lambda name: f"/bin/{name}")
    return calls


# ── OverlayCompleter ─────────────────────────────────────────────────────────

class _Never(Completer):
    def should_activate(self, ctx):
        return False

    def complete(self, ctx):
        raise AssertionError("not active")


def test_overlay_adds_extras_and_keeps_the_first_of_a_value():
    overlay = OverlayCompleter(ChoiceCompleter(["a", "b"]), _Never(), ChoiceCompleter(["b", "c"]))
    ctx = CompletionContext(command="x", args=[], arg_index=0, prefix="", line="x ")
    assert [c.value for c in overlay.complete(ctx)] == ["a", "b", "c"]
    assert overlay.should_activate(ctx)


# ── S3PathCompleter ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("prefix", ["", "s", "s3:/"])
def test_offers_the_scheme_without_calling_aws(aws_cli, prefix):
    c = S3PathCompleter()
    ctx = _ctx(f"mytool {prefix}")
    assert c.should_activate(ctx)
    assert [x.value for x in c.complete(ctx)] == ["s3://"]
    assert aws_cli == []


def test_not_active_on_other_words(aws_cli):
    assert not S3PathCompleter().should_activate(_ctx("mytool ./build"))


def test_buckets(aws_cli):
    out = S3PathCompleter().complete(_ctx("mytool s3://l"))
    assert [(c.value, c.display) for c in out] == [("s3://logs/", "logs/")]
    assert out[0].fields == ("", "2024-01-02")


def test_keys_one_level_at_a_time(aws_cli):
    out = S3PathCompleter().complete(_ctx("mytool s3://logs/2026/"))
    assert [c.value for c in out] == ["s3://logs/2026/raw/", "s3://logs/2026/report.csv"]
    assert out[1].display == "report.csv" and out[1].fields == ("2.0K", "2026-10-01 12:34")
    argv = aws_cli[-1]
    assert argv[argv.index("--prefix") + 1] == "2026/" and "--delimiter" in argv


def test_narrowing_reuses_the_listing(aws_cli):
    c = S3PathCompleter()
    c.complete(_ctx("mytool s3://logs/2026/"))
    out = c.complete(_ctx("mytool s3://logs/2026/rep"))
    assert [x.value for x in out] == ["s3://logs/2026/report.csv"]
    assert len(aws_cli) == 1


def test_profile_and_region_on_the_line_are_used(aws_cli):
    S3PathCompleter().complete(_ctx("aws --profile prod --region eu-west-1 s3 ls s3://"))
    argv = aws_cli[-1]
    assert argv[argv.index("--profile") + 1] == "prod"
    assert argv[argv.index("--region") + 1] == "eu-west-1"


def test_a_failure_is_reported_once_and_cached(aws_cli, capsys, monkeypatch):
    calls = []

    def denied(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 255, "", "An error occurred (AccessDenied)\n")

    monkeypatch.setattr(aws_recipe.subprocess, "run", denied)
    c = S3PathCompleter()
    ctx = _ctx("mytool s3://denied/")
    assert c.complete(ctx) == []
    assert c.complete(ctx) == []              # cached: no second call, no second notice
    assert len(calls) == 1
    assert capsys.readouterr().err.count("S3 listing failed: An error occurred (AccessDenied)") == 1


# ── aws s3 <op> ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("line, remote, local", [
    ("aws s3 cp ", True, True),
    ("aws s3 cp ./a s3://", True, True),
    ("aws s3 ls s3://", True, False),
    ("aws --region us-east-1 s3 sync ", True, True),
    ("aws s3 cp --profile ", False, False),      # a flag's value
    ("aws s3 cp --", False, False),              # a flag
    ("aws s3api ", False, False),                # another service
    ("aws s3 ", False, False),                   # the operation itself
    ("aws ec2 describe-instances ", False, False),
])
def test_where_the_paths_apply(aws_cli, line, remote, local):
    ctx = _ctx(line)
    assert _AwsS3PathArg(S3PathCompleter(), _S3_REMOTE_OPS).should_activate(ctx) is remote
    assert _AwsS3PathArg(FileCompleter(), _S3_LOCAL_OPS).should_activate(ctx) is local


def test_aws_overlay_keeps_aws_completer_answers(aws_cli, monkeypatch):
    from eosh.commands import registry

    monkeypatch.setattr(aws_recipe.AwsCompleter, "complete",
                        lambda self, ctx: [Completion(value="--recursive")])
    from eosh.variables import registry as var_registry
    monkeypatch.setattr(registry, "_commands", dict(registry._commands))   # undone after
    monkeypatch.setattr(var_registry, "_vars", dict(var_registry._vars))
    aws_recipe.register()
    delegate = registry.get("aws").delegate
    values = [c.value for c in delegate.complete(_ctx("aws s3 ls s3://l"))]
    assert values == ["--recursive", "s3://logs/"]
