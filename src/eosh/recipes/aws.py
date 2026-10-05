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

This recipe also registers the ``aws_region`` and ``aws_profile`` Python-
backed variables, so users can do ``var aws_region=us-east-1`` to set
``AWS_REGION`` (and ``AWS_DEFAULT_REGION``) without remembering both keys.
"""

from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..commands import registry as command_registry
from ..completion import Completer, Completion, CompletionContext
from ..completion_cache import aws_env_key, get_or_fetch
from ..variables import EnvVar, Var, registry as var_registry


# ─── AWS regions (used by AwsRegionVar's value completer) ───────────────────

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


# ─── Variables ───────────────────────────────────────────────────────────────

class _AwsRegionVar(Var):
    """Sets AWS_REGION and AWS_DEFAULT_REGION together from one logical name."""

    @property
    def name(self) -> str:
        return "aws_region"

    @property
    def description(self) -> str:
        return "AWS region — sets AWS_REGION + AWS_DEFAULT_REGION"

    @property
    def env_keys(self) -> list[str]:
        return ["AWS_REGION", "AWS_DEFAULT_REGION"]

    def get(self) -> str | None:
        return os.environ.get("AWS_REGION")

    def set(self, value: str) -> None:
        os.environ["AWS_REGION"] = value
        os.environ["AWS_DEFAULT_REGION"] = value

    def unset(self) -> None:
        os.environ.pop("AWS_REGION", None)
        os.environ.pop("AWS_DEFAULT_REGION", None)

    @property
    def value_completer(self) -> Completer:
        return AwsRegionCompleter()


# ─── Recipe entry point ──────────────────────────────────────────────────────

def register() -> None:
    command_registry.command(
        "aws",
        help="AWS Command Line Interface",
        delegate=AwsCompleter(),
    )

    var_registry.register(_AwsRegionVar())
    var_registry.register(EnvVar(
        name="aws_profile",
        env_var="AWS_PROFILE",
        completer=AwsProfileCompleter(),
        description="AWS named profile",
    ))
