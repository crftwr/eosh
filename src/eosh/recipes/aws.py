"""Completion recipe for the AWS CLI.

Drives the official ``aws_completer`` binary that ships with AWS CLI v2.
It speaks a simple protocol: set ``COMP_LINE`` and ``COMP_POINT`` in the
environment, run ``aws_completer`` with no args, and read candidates one
per line on stdout.

What aws_completer knows out of the box:

* every service (``ec2``, ``s3``, ``iam``, …) and every operation per service
* every flag for the current operation (operation-specific + global)
* values for ``--region``, ``--profile``, ``--output``
* live AWS API resource discovery — EC2 instance IDs, IAM roles, etc.
  (uses your current credentials and respects ``--region``/``--profile``
  already typed on the command line)

If the user has AWS CLI v2 installed, a single ``enable("aws")`` call gives
them account-aware completion for the entire AWS surface — no per-service
recipe needed.

What it doesn't know is paths: ``aws s3 cp <TAB>`` gets nothing from it,
local or ``s3://``.  The recipe overlays two completers on it
(:class:`~eosh.completion.OverlayCompleter`) for the path arguments of
``aws s3 <op>``: :class:`S3PathCompleter` for ``s3://bucket/key`` and the
filesystem for local paths.  ``S3PathCompleter`` is public, for your own
commands' arguments too.

This recipe also registers the ``aws_region`` and ``aws_profile`` Python-
backed variables, so users can do ``var aws_region=us-east-1`` to set
``AWS_REGION`` (and ``AWS_DEFAULT_REGION``) without remembering both keys.
"""

from __future__ import annotations

import configparser
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..commands import registry as command_registry
from ..completion import (
    Completer, Completion, CompletionContext, FileCompleter, OverlayCompleter,
)
from ..completion_cache import aws_env_key, get_or_fetch
from ..variables import EnvVar, registry as var_registry


# ─── AWS regions (the aws_region value completer) ───────────────────

AWS_REGIONS: list[tuple[str, str]] = [
    ("af-south-1",      "Africa (Cape Town)"),
    ("ap-east-1",       "Asia Pacific (Hong Kong)"),
    ("ap-northeast-1",  "Asia Pacific (Tokyo)"),
    ("ap-northeast-2",  "Asia Pacific (Seoul)"),
    ("ap-northeast-3",  "Asia Pacific (Osaka)"),
    ("ap-south-1",      "Asia Pacific (Mumbai)"),
    ("ap-south-2",      "Asia Pacific (Hyderabad)"),
    ("ap-southeast-1",  "Asia Pacific (Singapore)"),
    ("ap-southeast-2",  "Asia Pacific (Sydney)"),
    ("ap-southeast-3",  "Asia Pacific (Jakarta)"),
    ("ap-southeast-4",  "Asia Pacific (Melbourne)"),
    ("ap-southeast-5",  "Asia Pacific (Malaysia)"),
    ("ca-central-1",    "Canada (Central)"),
    ("ca-west-1",       "Canada West (Calgary)"),
    ("eu-central-1",    "Europe (Frankfurt)"),
    ("eu-central-2",    "Europe (Zurich)"),
    ("eu-north-1",      "Europe (Stockholm)"),
    ("eu-south-1",      "Europe (Milan)"),
    ("eu-south-2",      "Europe (Spain)"),
    ("eu-west-1",       "Europe (Ireland)"),
    ("eu-west-2",       "Europe (London)"),
    ("eu-west-3",       "Europe (Paris)"),
    ("il-central-1",    "Israel (Tel Aviv)"),
    ("me-central-1",    "Middle East (UAE)"),
    ("me-south-1",      "Middle East (Bahrain)"),
    ("mx-central-1",    "Mexico (Central)"),
    ("sa-east-1",       "South America (São Paulo)"),
    ("us-east-1",       "US East (N. Virginia)"),
    ("us-east-2",       "US East (Ohio)"),
    ("us-gov-east-1",   "AWS GovCloud (US-East)"),
    ("us-gov-west-1",   "AWS GovCloud (US-West)"),
    ("us-west-1",       "US West (N. California)"),
    ("us-west-2",       "US West (Oregon)"),
]


# ─── Completers ──────────────────────────────────────────────────────────────

class AwsRegionCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        return [
            Completion(value=region, description=description)
            for region, description in AWS_REGIONS
            if region.startswith(ctx.prefix)
        ]


class AwsProfileCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        profiles = self._get_profiles()
        return [
            Completion(value=p, description="AWS profile")
            for p in profiles if p.startswith(ctx.prefix)
        ]

    def _get_profiles(self) -> list[str]:
        profiles: set[str] = set()
        for config_file in (
            Path.home() / ".aws" / "credentials",
            Path.home() / ".aws" / "config",
        ):
            if not config_file.exists():
                continue
            parser = configparser.ConfigParser()
            try:
                parser.read(config_file)
            except configparser.Error:
                continue
            for section in parser.sections():
                if section.startswith("profile "):
                    profiles.add(section[len("profile "):])
                elif section.lower() != "default":
                    profiles.add(section)
        for config_file in (
            Path.home() / ".aws" / "credentials",
            Path.home() / ".aws" / "config",
        ):
            if config_file.exists():
                parser = configparser.ConfigParser()
                try:
                    parser.read(config_file)
                    if "default" in parser:
                        profiles.add("default")
                except configparser.Error:
                    pass
        return sorted(profiles)


class AwsCompleter(Completer):
    """Drives the AWS CLI v2 ``aws_completer`` binary.

    The protocol: set ``COMP_LINE`` (the full command line up to the cursor)
    and ``COMP_POINT`` (cursor byte offset) in the environment, run
    ``aws_completer`` with no args, and read candidates one per line.
    Returns ``[]`` when the binary is missing, errors, or times out.
    """

    def __init__(self, *, timeout: float = 5.0, binary: str = "aws_completer") -> None:
        # 5s default: aws_completer's live AWS API calls (e.g. listing
        # instance IDs, IAM roles) routinely take 1.5–3s on a typical
        # network.  Tight timeouts cause silent empty results.
        self._timeout = timeout
        self._binary = binary

    def should_activate(self, ctx: CompletionContext) -> bool:
        return shutil.which(self._binary) is not None

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not self.should_activate(ctx):
            return []
        line = ctx.line
        # ctx.line is the input up to the cursor, so cursor position equals
        # its byte length.
        point = len(line.encode("utf-8"))
        key = ("aws_completer", aws_env_key(), line, point)
        words = get_or_fetch(key, lambda: self._invoke(line, point))
        prefix = ctx.prefix
        return [Completion(value=w) for w in words if w.startswith(prefix)]

    def _invoke(self, line: str, point: int) -> list[str]:
        env = dict(os.environ)
        env["COMP_LINE"] = line
        env["COMP_POINT"] = str(point)
        try:
            proc = subprocess.run(
                [self._binary],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired:
            # AWS API calls can be slow.  Tell the user instead of silently
            # falling through to file completion (which is misleading).
            sys.stderr.write(
                f"\r\n[aws_completer timed out after {self._timeout:.1f}s — "
                "try again or narrow the query]\r\n"
            )
            sys.stderr.flush()
            return []
        except OSError:
            return []
        if proc.returncode != 0:
            return []
        return [w for w in proc.stdout.splitlines() if w]


# ─── S3 paths ────────────────────────────────────────────────────────────────

def _aws_cli_options(args: list[str]) -> list[str]:
    """``--profile`` / ``--region`` already typed on the line, so a listing
    sees the account the command will run against."""
    opts: list[str] = []
    for flag in ("--profile", "--region"):
        if flag in args[:-1]:
            opts += [flag, args[args.index(flag) + 1]]
    return opts


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return str(n)


class S3PathCompleter(Completer):
    """``s3://bucket/key`` paths: the buckets, then one "directory" level of
    keys at a time (``/`` as the delimiter), with size and date columns.

    Lists through the ``aws`` CLI (``s3api list-buckets`` /
    ``list-objects-v2``), so the core stays free of boto3, using
    ``--profile`` / ``--region`` when the line already has them.  Results
    are cached per account by :mod:`~eosh.completion_cache` — a listing
    serves every keystroke that narrows it — and a failure is reported once
    and cached as empty until the next command runs.

    Active on a token that starts with ``s3://``, and on one that could
    still become it (``""``, ``s``, ``s3:``), where it offers ``s3://``
    itself without calling AWS.
    """

    SCHEME = "s3://"

    def __init__(self, *, timeout: float = 5.0, max_keys: int = 1000) -> None:
        self._timeout = timeout
        self._max_keys = max_keys

    def should_activate(self, ctx: CompletionContext) -> bool:
        p = ctx.prefix
        return (p.startswith(self.SCHEME) or self.SCHEME.startswith(p)) \
            and shutil.which("aws") is not None

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not ctx.prefix.startswith(self.SCHEME):
            return [Completion(value=self.SCHEME, description="S3 path")]
        rest = ctx.prefix[len(self.SCHEME):]
        opts = _aws_cli_options(ctx.args)
        account = (aws_env_key(), tuple(opts))
        if "/" not in rest:
            buckets = get_or_fetch(("s3-buckets", account), lambda: self._buckets(opts))
            return [Completion(value=f"{self.SCHEME}{name}/", display=f"{name}/",
                               fields=("", created))
                    for name, created in buckets if name.startswith(rest)]
        bucket, _, key = rest.partition("/")
        folder = key[: key.rfind("/") + 1]
        entries = get_or_fetch(("s3-keys", account, bucket, folder),
                               lambda: self._keys(opts, bucket, folder))
        partial = key[len(folder):]
        return [Completion(value=f"{self.SCHEME}{bucket}/{folder}{name}", display=name,
                           fields=fields)
                for name, fields in entries if name.startswith(partial)]

    def _buckets(self, opts: list[str]) -> list[tuple[str, str]]:
        data = self._run(["s3api", "list-buckets", *opts])
        return [(b["Name"], str(b.get("CreationDate", ""))[:10])
                for b in data.get("Buckets", [])]

    def _keys(self, opts: list[str], bucket: str, folder: str) -> list[tuple[str, tuple]]:
        data = self._run(["s3api", "list-objects-v2", "--bucket", bucket,
                          "--prefix", folder, "--delimiter", "/",
                          "--max-items", str(self._max_keys), *opts])
        dirs = [(p["Prefix"][len(folder):], ("", ""))
                for p in data.get("CommonPrefixes") or []]
        files = [(o["Key"][len(folder):],
                  (_human_size(o.get("Size", 0)),
                   str(o.get("LastModified", ""))[:16].replace("T", " ")))
                 for o in data.get("Contents") or []
                 if o["Key"] != folder]                # a "folder" marker object
        return dirs + files

    def _run(self, argv: list[str]) -> dict:
        """One ``aws … --output json`` call; on failure, say why once and
        return ``{}`` (cached like any answer until the next command)."""
        env = dict(os.environ, AWS_PAGER="")
        try:
            proc = subprocess.run(["aws", *argv, "--output", "json"], env=env,
                                  stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, timeout=self._timeout)
        except subprocess.TimeoutExpired:
            _notice(f"S3 listing timed out after {self._timeout:.1f}s")
            return {}
        except OSError:
            return {}
        if proc.returncode != 0:
            lines = [l for l in proc.stderr.strip().splitlines() if l.strip()]
            _notice(f"S3 listing failed: {lines[-1] if lines else proc.returncode}")
            return {}
        try:
            return json.loads(proc.stdout or "{}")
        except ValueError:
            return {}


def _notice(message: str) -> None:
    sys.stderr.write(f"\r\n[{message}]\r\n")
    sys.stderr.flush()


# ``aws s3`` operations whose positionals are S3 paths, and those that also
# take local ones.
_S3_REMOTE_OPS = frozenset({"ls", "cp", "mv", "rm", "sync", "rb", "presign"})
_S3_LOCAL_OPS = frozenset({"cp", "mv", "sync"})

# Flags (global and ``aws s3``) whose next word is a value, not a path.
_AWS_VALUE_FLAGS = frozenset({
    "--profile", "--region", "--output", "--endpoint-url", "--query",
    "--ca-bundle", "--cli-read-timeout", "--cli-connect-timeout", "--color",
    "--cli-binary-format", "--exclude", "--include", "--acl", "--sse",
    "--sse-c", "--sse-c-key", "--sse-kms-key-id", "--sse-c-copy-source",
    "--sse-c-copy-source-key", "--storage-class", "--grants",
    "--website-redirect", "--content-type", "--cache-control",
    "--content-disposition", "--content-encoding", "--content-language",
    "--expires", "--source-region", "--metadata", "--metadata-directive",
    "--expected-size", "--page-size", "--request-payer", "--checksum-mode",
    "--checksum-algorithm", "--copy-props", "--expires-in",
})


class _AwsS3PathArg(Completer):
    """*inner*, but only on a path argument of ``aws s3 <op>`` for one of
    *ops* — not a flag, not a flag's value, not another service."""

    def __init__(self, inner: Completer, ops: frozenset[str]):
        self._inner = inner
        self._ops = ops

    def should_activate(self, ctx: CompletionContext) -> bool:
        args = ctx.args
        if ctx.prefix.startswith("-") or (args and args[-1] in _AWS_VALUE_FLAGS):
            return False
        for i in range(len(args) - 1):
            if args[i] == "s3" and (i == 0 or args[i - 1] not in _AWS_VALUE_FLAGS):
                return args[i + 1] in self._ops and self._inner.should_activate(ctx)
        return False

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        return self._inner.complete(ctx)


# ─── Variables ───────────────────────────────────────────────────────────────

# ─── Recipe entry point ──────────────────────────────────────────────────────

def register() -> None:
    command_registry.command(
        "aws",
        help="AWS Command Line Interface",
        # aws_completer has nothing for path arguments; add S3 and local
        # paths there without touching what it does answer.
        delegate=OverlayCompleter(
            AwsCompleter(),
            _AwsS3PathArg(S3PathCompleter(), _S3_REMOTE_OPS),
            _AwsS3PathArg(FileCompleter(), _S3_LOCAL_OPS),
        ),
    )

    var_registry.register(EnvVar(
        "aws_region", keys=["AWS_REGION", "AWS_DEFAULT_REGION"],
        completer=AwsRegionCompleter(),
        description="AWS region — sets AWS_REGION + AWS_DEFAULT_REGION",
    ))
    var_registry.register(EnvVar(
        "aws_profile", keys="AWS_PROFILE",
        completer=AwsProfileCompleter(),
        description="AWS named profile",
    ))
