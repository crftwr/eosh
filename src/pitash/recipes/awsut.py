"""AWS utility commands ported from the legacy cshell.

Provides the ``awsut`` command tree:

* ``awsut whoami``                   — identity, account, region, credential
  expiry and every setting that got them
* ``awsut console <page>``           — open a Management Console URL
* ``awsut credentials set``          — write pasted ``export AWS_…`` lines into
  a ``~/.aws/credentials`` profile
* ``awsut recent-cost``              — show recent AWS cost
* ``awsut ec2 list|start|stop|reboot``
* ``awsut logs list|monitor|export``
* ``awsut cloudformation list|watch|open``
* ``awsut sagemaker jobs list|describe|watch|stop``
* ``awsut sagemaker hub hubs|list|versions|describe|files|trace``
* ``awsut sagemaker studio domains|spaces|apps|profiles|logs|start|url|stop``
* ``awsut sagemaker hyperpod create|update|scale|add-ig|remove-ig|
  delete-nodes|reboot-nodes|replace-nodes|upgrade-ami|delete|
  list|describe|watch|log|ssm|ssh|run|search-capacity|
  kubeconfig|events``
* ``awsut bedrock-agentcore harness list|describe|versions|endpoints|watch|
  delete``
* ``awsut bedrock-agentcore memory list|describe|strategies|watch|delete|
  actors|sessions|events|event|records|record|search|jobs``

Each service group lives in its own subpackage — ``_awsut_sagemaker``,
``_awsut_agentcore`` (this module is already long enough) — and is attached
from :func:`register` because ``CommandRegistry`` roots cannot be re-opened
from a second module.

Every group prints to one contract — header line, table, ``error:`` on stderr —
defined in :mod:`pitash.recipes._awsut_common`.  The leaves here were ported
from a shell that printed colon-separated one-liners; they render through those
helpers now, so ``awsut ec2 list`` and ``awsut sagemaker studio apps`` line up
column for column.

Profile and region switching live in the ``aws`` recipe as ``Var`` entries
(``var aws_profile=...``, ``var aws_region=...``).  The SageMaker endpoint
and SageMaker service name are also exposed as ``Var`` entries — set them
at the prompt with ``var sagemaker_endpoint=...`` /
``var sagemaker_service_name=...`` (or ``var sagemaker_endpoint=`` to
unset), as are the AgentCore endpoints — one per plane, since a memory is
reached through both: ``var agentcore_control_endpoint=...`` and
``var agentcore_data_endpoint=...``, registered by that subpackage.  All
of these are stored in module-level Python variables (not ``os.environ``), so
they don't leak into subprocesses.

User-customisable defaults (read from ``~/.pitash/config.py`` if set):

    from pitash.recipes import awsut
    awsut.console_pages = {"home": "https://...", ...}
    awsut.console_url_modifier_func = lambda account, role, url: ...
    awsut.awscli = ["aws"]
"""

from __future__ import annotations

import datetime
import fnmatch
import json
import os
import re
import sys
import time
import urllib.parse
import webbrowser
from typing import Callable

try:
    import boto3
    import botocore.exceptions
except ModuleNotFoundError as e:
    # Keep ``e.name`` so ``enable("*")`` still recognises a missing dependency.
    raise ModuleNotFoundError(
        f"awsut needs {e.name}: pip install 'pitash[aws]'", name=e.name
    ) from e

from ..commands import registry as command_registry, arg
from ..completion import Completer, Completion, CompletionContext, FileCompleter
from ..completion_cache import aws_env_key, get_or_fetch
from ..shell import passthrough_input_block
from ..variables import Var, registry as var_registry
from ._awsut_common import (
    RED,
    RESET,
    YELLOW,
    SmError,
    api_message,
    fmt_bytes,
    fmt_dur,
    fmt_time,
    guard,
    print_header,
    print_labeled,
    print_table,
    section,
)
from .aws import AWS_REGIONS, AwsProfileCompleter


# ─── User-customisable module-level config ──────────────────────────────────
#
# Override from ~/.pitash/config.py:
#
#     from pitash.recipes import awsut
#     awsut.console_pages = {...}
#     awsut.console_url_modifier_func = lambda account, role, url: ...

console_pages: dict[str, str] = {
    "home":           "https://console.aws.amazon.com/console/home",
    "s3":             "https://console.aws.amazon.com/s3/home",
    "iam":            "https://console.aws.amazon.com/iam/home",
    "cloudformation": "https://console.aws.amazon.com/cloudformation/home",
    "cost":           "https://console.aws.amazon.com/costmanagement/home#/cost-explorer?granularity=Daily&historicalRelativeRange=LAST_7_DAYS",
    "sagemaker":      "https://console.aws.amazon.com/sagemaker/home",
    "hyperpod":       "https://console.aws.amazon.com/sagemaker/home#/cluster-management",
}

console_url_modifier_func: Callable[[str, str, str], str] | None = None

awscli: list[str] = ["aws"]

# Set via ``var sagemaker_endpoint=...`` / ``var sagemaker_service_name=...``
# at the prompt.  Module-level Python state (not env vars) so they don't
# leak into subprocesses.
sagemaker_endpoint: str = ""
sagemaker_service_name: str = "sagemaker"


# ─── boto3 / AWS helpers ────────────────────────────────────────────────────

def _get_boto3_client(service_name: str):
    region_name = os.environ.get("AWS_REGION")
    return boto3.Session().client(service_name, region_name=region_name)


def _get_sagemaker_client(region_name: str | None = None):
    endpoint_url = sagemaker_endpoint or None
    if region_name is None:
        region_name = os.environ.get("AWS_REGION")
    return boto3.Session().client(
        sagemaker_service_name,
        region_name=region_name,
        endpoint_url=endpoint_url,
    )


def _get_region() -> str | None:
    if "AWS_REGION" in os.environ:
        return os.environ["AWS_REGION"]
    try:
        return boto3.session.Session().region_name
    except Exception:
        return None


def _region_label() -> str:
    """The region for a header line — or how to set one, when there is none.

    Every listing names the region it read, so "nothing found" can never be
    confused with "looked in the wrong place".
    """
    return _get_region() or "(no region set — var aws_region=…)"


def _get_profile() -> str:
    return os.environ.get("AWS_PROFILE", "default")


def _get_all_profiles() -> dict[str, dict]:
    profiles: dict[str, dict] = {}
    aws_config_path = os.path.expanduser("~/.aws/config")
    if not os.path.exists(aws_config_path):
        return profiles

    with open(aws_config_path) as fd:
        profile_name = ""
        for line in fd:
            stripped = line.strip()
            if re.match(r"\[default\]", stripped):
                profile_name = "default"
                profiles[profile_name] = {}
                continue
            m = re.match(r"\[profile\s(.+)\]", stripped)
            if m:
                profile_name = m.group(1)
                profiles[profile_name] = {}
                continue
            m = re.match(
                r"credential_process.*\-\-awscli\s+\b(\d{12})\b.*\-\-role\s+([A-Za-z0-9_-]+)",
                stripped,
            )
            if m and profile_name:
                profiles[profile_name]["account"] = m.group(1)
                profiles[profile_name]["role"] = m.group(2)
    return profiles


def _print_json(obj) -> None:
    """Print JSON, syntax-highlighted when stdout is a TTY."""
    text = json.dumps(obj, indent=2, default=str)
    if sys.stdout.isatty():
        try:
            from pygments import highlight
            from pygments.formatters import Terminal256Formatter
            from pygments.lexers import JsonLexer
            sys.stdout.write(
                highlight(text, JsonLexer(), Terminal256Formatter(style="monokai"))
            )
            return
        except Exception:
            pass
    print(text)


# ─── ~/.aws/credentials writing (used by `awsut credentials set`) ───────────
#
# The console's "get credentials" panel — and isengardcli, and `aws sso`, and
# every internal wrapper around them — hands out the same three shell lines.
# These helpers turn a pasted block of them into a credentials-file profile.

CREDENTIALS_PATH = "~/.aws/credentials"

# Env var name → credentials-file key.  Both spellings of a value map to the
# one key the file uses, so a paste that carries either (or both) lands right.
_CREDENTIAL_KEYS: dict[str, str] = {
    "AWS_ACCESS_KEY_ID":     "aws_access_key_id",
    "AWS_SECRET_ACCESS_KEY": "aws_secret_access_key",
    "AWS_SESSION_TOKEN":     "aws_session_token",
    "AWS_SECURITY_TOKEN":    "aws_session_token",
    "AWS_REGION":            "region",
    "AWS_DEFAULT_REGION":    "region",
}

# Recognised but deliberately *not* written: no SDK reads an expiry key out of
# the credentials file, so it would be dead weight there.  Reported instead.
_EXPIRATION_KEYS = ("AWS_SESSION_EXPIRATION", "AWS_CREDENTIAL_EXPIRATION")

_ASSIGNMENT_RE = re.compile(
    r"""^\s*
        (?:export\s+|set\s+|setenv\s+|\$env:)?    # sh / csh / PowerShell prefix
        (?P<name>[A-Za-z_][A-Za-z0-9_]*)
        \s*=\s*
        (?P<value>.*?)
        \s*;?\s*$                                  # optional trailing semicolon
    """,
    re.VERBOSE,
)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_credential_exports(text: str) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Split a pasted credentials block into what to write and what to say.

    Returns ``(values, extras, unknown)``: *values* keyed by credentials-file
    key (ready to write), *extras* the recognised-but-not-written names (the
    expiry), and *unknown* every other assignment found, so the command can
    say what it ignored rather than silently dropping it.

    Accepts the shapes the same three lines arrive in — ``export K="v"``,
    ``set K=v``, ``$env:K="v"``, bare ``K=v``, and the credentials file's own
    ``aws_access_key_id = v`` — and ignores blank and ``#`` comment lines.
    """
    values: dict[str, str] = {}
    extras: dict[str, str] = {}
    unknown: list[str] = []
    lower_keys = {v: v for v in _CREDENTIAL_KEYS.values()}

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("["):
            continue
        m = _ASSIGNMENT_RE.match(stripped)
        if not m:
            continue
        name = m.group("name")
        value = _unquote(m.group("value"))
        if not value:
            continue
        if name.upper() in _CREDENTIAL_KEYS:
            values[_CREDENTIAL_KEYS[name.upper()]] = value
        elif name.lower() in lower_keys:
            values[lower_keys[name.lower()]] = value
        elif name.upper() in _EXPIRATION_KEYS:
            extras[name.upper()] = value
        else:
            unknown.append(name)
    return values, extras, unknown


def write_credentials_profile(path: str, profile: str,
                              values: dict[str, str]) -> bool:
    """Write *values* into ``[profile]`` of *path*.  Returns True if created.

    An edit in place, not a rewrite: only the keys being set are touched, so
    every other profile, every unrelated key inside this one, and the file's
    comments and blank lines survive.  ``configparser`` would drop the
    comments — a credentials file is hand-maintained, so it keeps them.

    The new file is written beside the old one and renamed over it at mode
    0600, so a crash mid-write can't leave a half-file of secrets behind and
    the result is never world-readable.
    """
    path = os.path.expanduser(path)
    try:
        with open(path) as fd:
            lines = fd.read().splitlines()
    except FileNotFoundError:
        lines = []
        created = True
    else:
        created = False

    header_re = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
    start = None
    for i, line in enumerate(lines):
        m = header_re.match(line)
        if m and m.group("name").strip() == profile:
            start = i
            break

    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{profile}]")
        lines.extend(f"{k} = {v}" for k, v in values.items())
    else:
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if header_re.match(lines[i]):
                end = i
                break
        remaining = dict(values)
        body: list[str] = []
        for line in lines[start + 1:end]:
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
            key = m.group(1).lower() if m else None
            if key in values:
                # First occurrence carries the new value; later duplicates of
                # the same key would shadow it, so they go.
                if key in remaining:
                    body.append(f"{key} = {remaining.pop(key)}")
                continue
            body.append(line)
        insert_at = len(body)
        while insert_at > 0 and not body[insert_at - 1].strip():
            insert_at -= 1
        for k, v in remaining.items():
            body.insert(insert_at, f"{k} = {v}")
            insert_at += 1
        lines[start + 1:end] = body

    text = "\n".join(lines) + "\n"
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.pitash.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)
    return created


def _mask(value: str) -> str:
    """Show enough of a secret to recognise it, not enough to use it."""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 8}{value[-4:]}"


# ─── `awsut whoami` — what the next AWS call will do ────────────────────────
#
# A session asks the same four questions over and over: who am I, in which
# account, in which region, and for how much longer.  Each answer lives
# somewhere else — ``sts:GetCallerIdentity`` for the first two, the shell's own
# env/profile for the third, and the credential object boto3 already resolved
# for the fourth — so they get collected here instead of being pieced together
# from `aws sts get-caller-identity` plus `env | grep AWS`.

# Values whose whole point is to be unusable by a reader over your shoulder.
_SECRET_ENV_PATTERN = re.compile(r"SECRET|TOKEN|PASSWORD|ACCESS_KEY_ID")

# Caller-identity ARN resource types, in the words the console uses.
_ARN_KINDS: dict[str, str] = {
    "assumed-role":   "assumed role",
    "user":           "IAM user",
    "federated-user": "federated user",
    "root":           "account root",
}

# A `var` override worth reporting is one that differs from the built-in
# default; only a Var that *has* a non-empty default needs an entry here.
_VAR_DEFAULTS: dict[str, str] = {"sagemaker_service_name": "sagemaker"}

# The Vars that redirect a client somewhere other than the public endpoint.
# Named rather than discovered, because "every registered Var" would drag the
# whole user config into an answer about AWS.
_ENDPOINT_VARS: tuple[str, ...] = (
    "sagemaker_endpoint",
    "sagemaker_service_name",
    "agentcore_control_endpoint",
    "agentcore_data_endpoint",
)

# Under this much time left, the expiry line is the point of the output.
_EXPIRY_WARN_SECONDS = 15 * 60


def parse_identity_arn(arn: str) -> tuple[str, str, str]:
    """``(kind, name, session)`` — the three facts a caller ARN carries.

    ``…:assumed-role/Admin/alice`` → ``("assumed role", "Admin", "alice")``;
    ``…:user/alice`` → ``("IAM user", "alice", "")``; ``…:root`` →
    ``("account root", "", "")``.  A resource type this doesn't know is
    reported verbatim rather than guessed at — the full ARN prints anyway, so
    an unfamiliar shape costs nothing.
    """
    parts = arn.split(":", 5)
    if len(parts) < 6:
        return ("", "", "")
    kind, _, rest = parts[5].partition("/")
    name, _, session = rest.partition("/")
    return (_ARN_KINDS.get(kind, kind), name, session)


def identity_pairs(identity: dict) -> list[tuple[str, str]]:
    """The caller-identity block: the API's three fields, then what they mean."""
    arn = identity.get("Arn") or ""
    kind, name, session = parse_identity_arn(arn)
    return [
        ("Account", identity.get("Account") or ""),
        ("UserId",  identity.get("UserId") or ""),
        ("Arn",     arn),
        ("Type",    kind),
        ("Name",    name),
        ("Session", session),
    ]


def expiry_label(expiry, now=None, *, colorize: bool = False) -> str:
    """When the credentials die, and how long that is from now.

    Reads botocore's ``_expiry_time``: private, but the only place the answer
    exists, and the question ("do I need to refresh before starting this?") is
    asked too often to leave out.  ``None`` means no expiry — which is itself
    the answer for a long-lived access key.
    """
    if expiry is None:
        return ""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=datetime.timezone.utc)
    remaining = (expiry - now).total_seconds()
    if remaining <= 0:
        text, color = f"{fmt_time(expiry)}  (expired)", RED
    elif remaining <= _EXPIRY_WARN_SECONDS:
        text, color = f"{fmt_time(expiry)}  ({fmt_dur(remaining)} left)", YELLOW
    else:
        text, color = f"{fmt_time(expiry)}  ({fmt_dur(remaining)} left)", ""
    return f"{color}{text}{RESET}" if color and colorize else text


def credential_pairs(credentials, now=None, *,
                     colorize: bool = False) -> list[tuple[str, str]]:
    """Where the credentials came from, which key they are, and their expiry.

    ``credentials.method`` is botocore's own name for the provider that won
    (``env``, ``shared-credentials-file``, ``custom-process``, ``sso``,
    ``assume-role``, ``iam-role``) — the fact that explains why a profile
    switch did or didn't take effect, since an ``AWS_ACCESS_KEY_ID`` in the
    environment silently outranks ``AWS_PROFILE``.
    """
    if credentials is None:
        return [("Credentials", "none found — refresh them or set a profile")]
    pairs = [("Credentials", getattr(credentials, "method", "") or "unknown")]
    try:
        frozen = credentials.get_frozen_credentials()
    except Exception:
        frozen = None
    if frozen is not None and frozen.access_key:
        pairs.append(("Access key", _mask(frozen.access_key)))
    pairs.append(("Expires", expiry_label(
        getattr(credentials, "_expiry_time", None), now, colorize=colorize)))
    return pairs


def region_pairs() -> list[tuple[str, str]]:
    """The effective region, its human name, and which setting chose it."""
    region = _get_region()
    if not region:
        return [("Region", "(not set — var aws_region=…)")]
    from_env = bool(os.environ.get("AWS_REGION")
                    or os.environ.get("AWS_DEFAULT_REGION"))
    name = dict(AWS_REGIONS).get(region, "")
    source = "AWS_REGION" if from_env else "profile config"
    return [("Region", " · ".join(p for p in (region, name) if p)
                       + f"  ({source})")]


def profile_pairs() -> list[tuple[str, str]]:
    """The profile in effect, plus the account/role its config file names.

    The config-file account is printed next to the live one from
    ``GetCallerIdentity`` on purpose: when they disagree, the profile is not
    the thing supplying the credentials.
    """
    name = os.environ.get("AWS_PROFILE")
    pairs = [("Profile", f"{name}  (AWS_PROFILE)" if name
                         else "default  (AWS_PROFILE not set)")]
    config = _get_all_profiles().get(name or "default", {})
    if config.get("account"):
        pairs.append(("Profile account", config["account"]))
    if config.get("role"):
        pairs.append(("Profile role", config["role"]))
    return pairs


def aws_env_pairs() -> list[tuple[str, str]]:
    """Every ``AWS_*`` variable that is set, secrets masked.

    The block that explains a surprise: an ``AWS_ENDPOINT_URL`` or a stale
    ``AWS_SESSION_TOKEN`` left behind by a sourced script redirects calls
    without showing up anywhere else in the output.
    """
    return [
        (key, _mask(value) if _SECRET_ENV_PATTERN.search(key) else value)
        for key, value in sorted(os.environ.items())
        if key.startswith("AWS_") and value
    ]


def endpoint_override_pairs() -> list[tuple[str, str]]:
    """The ``var`` settings pointing a client away from the public endpoint."""
    pairs = []
    for name in _ENDPOINT_VARS:
        var = var_registry.get(name)
        value = var.get() if var is not None else None
        if value and value != _VAR_DEFAULTS.get(name):
            pairs.append((name, value))
    return pairs


def _aws_session() -> tuple[object | None, str]:
    """``(session, error)`` — constructing one fails on an unknown profile.

    ``boto3.Session()`` reads the config file eagerly, so a stale
    ``var aws_profile=`` raises before any call goes out.  That is the answer
    ``whoami`` is being asked for, not a reason to print nothing.
    """
    try:
        return (boto3.Session(), "")
    except Exception as exc:
        return (None, str(exc))


def _session_credentials(session):
    """The resolved credentials, or ``None`` when the chain can't produce any."""
    if session is None:
        return None
    try:
        return session.get_credentials()
    except Exception:
        return None


def _caller_identity(session) -> tuple[dict, str]:
    """``(identity, error)`` — never raises, because the rest still prints.

    A failure here is the common case worth reporting *with* the settings
    rather than instead of them: an expired or mis-pointed profile is exactly
    what the profile/region/environment blocks below diagnose.
    """
    try:
        client = session.client("sts", region_name=os.environ.get("AWS_REGION"))
        return (client.get_caller_identity(), "")
    except botocore.exceptions.NoCredentialsError:
        return ({}, "no AWS credentials found — refresh them and retry")
    except botocore.exceptions.ClientError as exc:
        return ({}, api_message(exc))
    except Exception as exc:
        # Unknown profile, unreachable endpoint, bad credential_process, …
        return ({}, str(exc))


def _account_alias(session) -> str:
    """The account's IAM alias — a name a human recognises faster than 12 digits.

    Best effort: plenty of roles can call ``sts`` but not ``iam``, and a
    missing alias must not turn a successful identity lookup into an error.
    """
    try:
        aliases = session.client(
            "iam", region_name=os.environ.get("AWS_REGION")
        ).list_account_aliases()
        return (aliases.get("AccountAliases") or [""])[0]
    except Exception:
        return ""


# ─── progress dots used by `cloudformation watch` ───────────────────────────

class _ProgressDots:
    def __init__(self):
        self.status = None

    def tick(self, status):
        if self.status != status:
            if self.status is not None:
                print()
            self.status = status
            if self.status is not None:
                print(self.status, end=" ", flush=True)
            return
        if self.status is not None:
            print(".", end="", flush=True)


# ─── completers ─────────────────────────────────────────────────────────────

class _ConsolePageCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        return [
            Completion(value=name, description=url)
            for name, url in console_pages.items()
            if name.startswith(ctx.prefix)
        ]


class _CostServiceCompleter(Completer):
    """Completes service names from Cost Explorer (last 30 days)."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            values = get_or_fetch(
                ("awsut.cost_services", aws_env_key()),
                self._fetch,
            )
        except Exception:
            return []
        return [Completion(value=v) for v in values if v.startswith(ctx.prefix)]

    @staticmethod
    def _fetch() -> list[str]:
        today = datetime.datetime.now().date()
        client = _get_boto3_client("ce")
        response = client.get_dimension_values(
            TimePeriod={
                "Start": (today - datetime.timedelta(days=30)).strftime("%Y-%m-%d"),
                "End":   (today + datetime.timedelta(days=1)).strftime("%Y-%m-%d"),
            },
            Dimension="SERVICE",
        )
        return [d["Value"] for d in response.get("DimensionValues", [])]


def _describe_instances() -> dict:
    return _get_boto3_client("ec2").describe_instances()


def _ec2_name(instance) -> str:
    """An instance's Name tag — what the ``ec2`` leaves address it by."""
    for tag in instance.get("Tags", []) or []:
        if tag["Key"] == "Name":
            return tag["Value"]
    return ""


def _ec2_public_dns(instance) -> str:
    for interface in instance.get("NetworkInterfaces", []):
        if "Association" in interface:
            return interface["Association"]["PublicDnsName"]
    return ""


class _Ec2InstanceNameCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            names = get_or_fetch(
                ("awsut.ec2_instance_names", aws_env_key()),
                self._fetch,
            )
        except Exception:
            return []
        return [Completion(value=n, description="ec2 instance")
                for n in names if n.startswith(ctx.prefix)]

    @staticmethod
    def _fetch() -> list[str]:
        return [name for reservation in _describe_instances().get("Reservations", [])
                for instance in reservation.get("Instances", [])
                if (name := _ec2_name(instance))]


def _epoch_ms(value):
    """A CloudWatch Logs millisecond timestamp as a local datetime, or None."""
    return (datetime.datetime.fromtimestamp(value / 1000).astimezone()
            if value else None)


def _list_log_groups(prefix: str = "") -> list[dict]:
    logs = _get_boto3_client("logs")
    groups: list[dict] = []
    next_token = None
    while True:
        params = {"limit": 50}
        if prefix:
            params["logGroupNamePrefix"] = prefix
        if next_token:
            params["nextToken"] = next_token
        response = logs.describe_log_groups(**params)
        groups += response["logGroups"]
        next_token = response.get("nextToken")
        if not next_token:
            break
    return groups


class _LogGroupCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        # ``_list_log_groups`` takes a server-side prefix filter — using
        # ``ctx.prefix`` directly would defeat caching (every keystroke is a
        # new key). Cache the unfiltered list per profile/region instead and
        # filter locally; the keystroke rate is low enough that the larger
        # initial fetch is the right tradeoff.
        try:
            groups = get_or_fetch(
                ("awsut.log_groups", aws_env_key()),
                lambda: _list_log_groups(""),
            )
        except Exception:
            return []
        return [Completion(value=g["logGroupName"], description="log group")
                for g in groups if g["logGroupName"].startswith(ctx.prefix)]


class _LogStreamCompleter(Completer):
    """Completes log stream names. Reads the group_name from preceding args."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not ctx.args:
            return []
        group_name = ctx.args[0]
        try:
            streams = get_or_fetch(
                ("awsut.log_streams", aws_env_key(), group_name),
                lambda: _get_boto3_client("logs")
                            .describe_log_streams(logGroupName=group_name)
                            .get("logStreams", []),
            )
        except Exception:
            return []
        return [Completion(value=s["logStreamName"], description="log stream")
                for s in streams
                if s["logStreamName"].startswith(ctx.prefix)]


def _list_cf_stacks(
    cf_client,
    *,
    include_deleted: bool = False,
    include_successfully_completed: bool = False,
    include_nested: bool = False,
) -> list[dict]:
    status_filter = {
        "CREATE_IN_PROGRESS", "CREATE_FAILED", "CREATE_COMPLETE",
        "ROLLBACK_IN_PROGRESS", "ROLLBACK_FAILED", "ROLLBACK_COMPLETE",
        "DELETE_IN_PROGRESS", "DELETE_FAILED", "DELETE_COMPLETE",
        "UPDATE_IN_PROGRESS", "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
        "UPDATE_COMPLETE", "UPDATE_ROLLBACK_IN_PROGRESS",
        "UPDATE_ROLLBACK_FAILED",
        "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS",
        "UPDATE_ROLLBACK_COMPLETE", "REVIEW_IN_PROGRESS",
        "IMPORT_IN_PROGRESS", "IMPORT_COMPLETE",
        "IMPORT_ROLLBACK_IN_PROGRESS", "IMPORT_ROLLBACK_FAILED",
        "IMPORT_ROLLBACK_COMPLETE",
    }
    if not include_deleted:
        status_filter.discard("DELETE_COMPLETE")
    if not include_successfully_completed:
        status_filter -= {
            "CREATE_COMPLETE", "DELETE_COMPLETE",
            "UPDATE_COMPLETE", "IMPORT_COMPLETE",
        }

    stacks: list[dict] = []
    next_token = None
    while True:
        params = {"StackStatusFilter": list(status_filter)}
        if next_token:
            params["NextToken"] = next_token
        response = cf_client.list_stacks(**params)
        stacks += response.get("StackSummaries", [])
        next_token = response.get("NextToken")
        if not next_token:
            break
    if not include_nested:
        stacks = [s for s in stacks if "ParentId" not in s]
    return stacks


def _osc8(url: str, label: str) -> str:
    """Wrap ``label`` in an OSC 8 hyperlink escape so terminals that support
    it (including the VSCode integrated terminal) render a clickable link.
    Terminals that don't support OSC 8 simply show ``label`` (the URL), which
    most terminals auto-link anyway."""
    return f"\x1b]8;;{url}\x1b\\{label}\x1b]8;;\x1b\\"


def _cf_console_url(stack_arn: str) -> str:
    """Build the CloudFormation console URL for a stack's events view,
    applying ``console_url_modifier_func`` when configured."""
    region = _get_region() or "us-east-1"
    encoded = urllib.parse.quote(stack_arn)
    url = (f"https://{region}.console.aws.amazon.com/cloudformation/home"
           f"?region={region}#/stacks/events?stackId={encoded}")
    if console_url_modifier_func is not None:
        profile_name = _get_profile()
        profile = _get_all_profiles().get(profile_name, {})
        url = console_url_modifier_func(
            profile.get("account", ""), profile.get("role", ""), url,
        )
    return url


def _cf_failure_reasons(cf_client, stack_name: str) -> list[str]:
    """Return the resource-level failure reasons for a stack's most recent
    operation, taken from ``describe_stack_events`` (most-recent page)."""
    try:
        response = cf_client.describe_stack_events(StackName=stack_name)
    except Exception:                       # best-effort — never break the watch
        return []
    reasons: list[str] = []
    for event in response.get("StackEvents", []):
        if not event.get("ResourceStatus", "").endswith("_FAILED"):
            continue
        reason = event.get("ResourceStatusReason")
        if not reason:
            continue
        line = f"{event.get('LogicalResourceId', '')}: {reason}"
        if line not in reasons:
            reasons.append(line)
    return reasons


def _report_cf_failure(cf_client, stack: dict) -> None:
    """A failed stack's reason(s) and a clickable console hyperlink, as a section.

    Same shape as the failure blocks under ``sagemaker hyperpod list``: the
    listing (or the watch) says *which* stacks failed, and one section per stack
    says why — so the reasons never interleave with the rows.
    """
    name = stack["StackName"]
    section(f"{name} · {stack['StackStatus']}")
    reason = stack.get("StackStatusReason")
    if reason:
        print(reason)
    for line in _cf_failure_reasons(cf_client, name):
        print(line)
    url = _cf_console_url(stack.get("StackId", name))
    print(f"Console: {_osc8(url, url)}")


class _CfStackNameCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            stacks = get_or_fetch(
                ("awsut.cf_stacks", aws_env_key()),
                lambda: _list_cf_stacks(
                    _get_boto3_client("cloudformation"),
                    include_deleted=False,
                    include_successfully_completed=True,
                    include_nested=True,
                ),
            )
        except Exception:
            return []
        return [Completion(value=s["StackName"], description=s["StackStatus"])
                for s in stacks if s["StackName"].startswith(ctx.prefix)]


# ─── module-level Python-backed Vars ────────────────────────────────────────

class _SagemakerEndpointVar(Var):
    name = "sagemaker_endpoint"
    description = "SageMaker endpoint URL (blank = AWS default)"

    def get(self) -> str | None:
        return sagemaker_endpoint or None

    def set(self, value: str) -> None:
        global sagemaker_endpoint
        sagemaker_endpoint = value

    def unset(self) -> None:
        global sagemaker_endpoint
        sagemaker_endpoint = ""


class _SagemakerServiceNameVar(Var):
    name = "sagemaker_service_name"
    description = "boto3 service name for SageMaker client (default: sagemaker)"

    def get(self) -> str | None:
        return sagemaker_service_name

    def set(self, value: str) -> None:
        global sagemaker_service_name
        sagemaker_service_name = value or "sagemaker"

    def unset(self) -> None:
        global sagemaker_service_name
        sagemaker_service_name = "sagemaker"


# ─── tree definition ────────────────────────────────────────────────────────

def register() -> None:
    awsut = command_registry.command("awsut", help="AWS utility commands")

    _register_whoami(awsut)
    _register_console(awsut)
    _register_credentials(awsut)
    _register_recent_cost(awsut)
    _register_ec2(awsut)
    _register_logs(awsut)
    _register_cf(awsut)

    # Lazy: these subpackages do `from .. import awsut`, so they can only be
    # imported once this module is fully loaded.  Each registers its own Vars.
    from ._awsut_agentcore import register_agentcore
    from ._awsut_sagemaker import register_sagemaker
    register_sagemaker(awsut)
    register_agentcore(awsut)

    var_registry.register(_SagemakerEndpointVar())
    var_registry.register(_SagemakerServiceNameVar())


def _register_whoami(awsut) -> None:
    @awsut.command(
        "whoami",
        help="Show the identity, account, region and credential expiry the "
             "next AWS call will use",
        params=[
            arg("--raw", action="store_true",
                help="Show the raw sts:GetCallerIdentity response as JSON"),
            arg("--no-alias", action="store_true",
                help="Skip the iam:ListAccountAliases lookup (one fewer call)"),
        ],
    )
    @guard
    def _whoami(raw=False, no_alias=False):
        # One session for all three lookups: a fresh one re-resolves the
        # credential chain, which for a `credential_process` profile means
        # running the helper (and any prompt it carries) a second time.
        session, error = _aws_session()
        identity: dict = {}
        if session is not None:
            identity, error = _caller_identity(session)
        if raw:
            if not identity:
                raise SmError(error)
            _print_json(identity)
            return

        colorize = sys.stdout.isatty()
        alias = _account_alias(session) if identity and not no_alias else ""
        pairs: list = []
        if identity:
            pairs += identity_pairs(identity)
            pairs += [("Alias", alias)]
        else:
            pairs += [("Identity", f"{RED}unavailable{RESET}" if colorize
                                   else "unavailable"),
                      ("Reason", error)]
        pairs += [None]
        pairs += profile_pairs()
        pairs += region_pairs()
        pairs += credential_pairs(_session_credentials(session),
                                  colorize=colorize)
        print_labeled(pairs)

        for label, block in (("environment", aws_env_pairs()),
                             ("endpoint overrides", endpoint_override_pairs())):
            if block:
                print()
                section(label)
                print_labeled(block)


def _register_console(awsut) -> None:
    @awsut.command(
        "console",
        help="Open Management Console",
        params=[arg("page_name", completer=_ConsolePageCompleter())],
    )
    @guard
    def _console(page_name):
        if page_name not in console_pages:
            raise SmError(f"no console page {page_name!r} — known pages: "
                          f"{', '.join(sorted(console_pages))}")
        url = console_pages[page_name]

        region = _get_region() or "us-east-1"
        parsed = urllib.parse.urlparse(url)
        query_params = urllib.parse.parse_qs(parsed.query)
        query_params["region"] = [region]
        new_query = urllib.parse.urlencode(query_params, doseq=True)
        url = urllib.parse.urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path,
             parsed.params, new_query, parsed.fragment)
        )

        if console_url_modifier_func is not None:
            profile_name = _get_profile()
            profile = _get_all_profiles().get(profile_name, {})
            account = profile.get("account", "")
            role = profile.get("role", "")
            url = console_url_modifier_func(account, role, url)

        print(f"Opening {url}")
        webbrowser.open(url)


def _register_credentials(awsut) -> None:
    credentials = awsut.command("credentials", help="Local credentials file")

    @credentials.command(
        "set",
        help="Update ~/.aws/credentials from a pasted block of export lines",
        params=[
            arg("profile", nargs="?", metavar="PROFILE",
                completer=AwsProfileCompleter(),
                help="Profile to write (default: $AWS_PROFILE, else 'default')"),
            arg("--file", default=CREDENTIALS_PATH, metavar="PATH",
                dest="file", completer=FileCompleter(),
                help=f"Credentials file to update (default: {CREDENTIALS_PATH})"),
            arg("-n", "--dry-run", action="store_true",
                help="Show what would be written, don't touch the file"),
        ],
    )
    @guard
    def _credentials_set(profile=None, file=CREDENTIALS_PATH, dry_run=False):
        profile = profile or _get_profile()
        print(f"Paste the credentials for profile [{profile}], "
              f"then press Enter on an empty line (Ctrl+C to cancel):")
        try:
            text = passthrough_input_block()
        except (EOFError, KeyboardInterrupt):
            print()
            raise SmError("cancelled — nothing written")

        values, extras, unknown = parse_credential_exports(text)
        if not values:
            raise SmError("no AWS credential assignments found in the pasted text")
        missing = [k for k in ("aws_access_key_id", "aws_secret_access_key")
                   if k not in values]
        if missing:
            raise SmError(f"incomplete credentials — missing {', '.join(missing)}")

        print_labeled(
            [("profile", profile), ("file", file)]
            + [(k, v if k == "region" else _mask(v)) for k, v in values.items()]
            + [(k.lower(), v) for k, v in extras.items()]
        )
        if unknown:
            print(f"ignored: {', '.join(sorted(set(unknown)))}")

        if dry_run:
            print("\ndry run — file unchanged")
            return

        created = write_credentials_profile(file, profile, values)
        verb = "created" if created else "updated"
        print(f"\n{verb} [{profile}] in {file}")
        if profile != _get_profile():
            print(f"(current profile is {_get_profile()!r} — "
                  f"switch with `var aws_profile={profile}`)")


def _register_recent_cost(awsut) -> None:
    @awsut.command(
        "recent-cost",
        help="Show recent cost",
        params=[
            arg("--days", type=int, default=14, metavar="N",
                help="Number of days to show"),
            arg("--filter", nargs="+", metavar="SERVICE",
                completer=_CostServiceCompleter(),
                help="Filter by service name(s) and show usage type breakdown"),
            arg("--min", type=float, default=0.1, dest="min_amount",
                metavar="AMOUNT",
                help="Hide rows/columns with total below this amount"),
        ],
    )
    @guard
    def _recent_cost(days, filter, min_amount):
        client = _get_boto3_client("ce")

        today = datetime.datetime.now().date()
        period_end = today + datetime.timedelta(days=1)
        period_start = period_end - datetime.timedelta(days=days)

        ce_params = {
            "TimePeriod": {
                "Start": period_start.strftime("%Y-%m-%d"),
                "End":   period_end.strftime("%Y-%m-%d"),
            },
            "Granularity": "MONTHLY",
            "Metrics": ["AmortizedCost"],
        }

        if filter:
            ce_params["Filter"] = {"Dimensions": {"Key": "SERVICE", "Values": filter}}
            ce_params["GroupBy"] = [
                {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
                {"Type": "DIMENSION", "Key": "REGION"},
            ]
            row_label = "USAGE TYPE"
        else:
            ce_params["GroupBy"] = [
                {"Type": "DIMENSION", "Key": "SERVICE"},
                {"Type": "DIMENSION", "Key": "REGION"},
            ]
            row_label = "SERVICE"

        response = client.get_cost_and_usage(**ce_params)

        data: dict[str, dict[str, float]] = {}
        region_totals: dict[str, float] = {}
        row_totals: dict[str, float] = {}

        for item in response["ResultsByTime"]:
            for group in item["Groups"]:
                row_key = group["Keys"][0]
                region = group["Keys"][1]
                amount = float(group["Metrics"]["AmortizedCost"]["Amount"])
                if amount < 0.01:
                    continue
                data.setdefault(row_key, {})[region] = (
                    data.get(row_key, {}).get(region, 0.0) + amount
                )
                region_totals[region] = region_totals.get(region, 0.0) + amount
                row_totals[row_key]   = row_totals.get(row_key, 0.0) + amount

        regions = sorted(
            [r for r, t in region_totals.items() if t >= min_amount],
            key=lambda r: region_totals[r], reverse=True,
        )
        rows = sorted(
            [r for r, t in row_totals.items() if t >= min_amount],
            key=lambda s: row_totals[s], reverse=True,
        )

        print_header(
            f"{len(rows)} {row_label.lower()}(s) × {len(regions)} region(s)",
            f"last {days} days",
            f"service {', '.join(filter)}" if filter else "",
            "amortized cost",
        )
        if not regions or not rows:
            print(f"nothing above --min {min_amount} in this period")
            return

        all_values = sorted(
            [v for row_data in data.values() for v in row_data.values()],
            reverse=True,
        )
        max_val = all_values[0] if all_values else 0
        top_10_pct_idx = max(1, len(all_values) // 10)
        yellow_threshold = all_values[top_10_pct_idx - 1] if all_values else 0

        def colorize(val, text):
            if not sys.stdout.isatty():
                return text
            if val >= max_val:
                return f"{RED}{text}{RESET}"
            if val >= yellow_threshold:
                return f"{YELLOW}{text}{RESET}"
            return text

        # A cross-tab, not a resource listing: the cells are money, so they are
        # right-aligned, and the last row is the column totals below a second
        # rule.  print_table expresses neither, so the frame is drawn here — to
        # the same shape it draws (labels, '-' rule, two-space gaps).
        label_w = max([len(row_label), len("TOTAL")] + [len(r) for r in rows])
        col_w = {region: max(len(region), 10) for region in regions}
        total_w = max(len("TOTAL"), 10)
        widths = [label_w] + [col_w[r] for r in regions] + [total_w]

        def rule():
            print("  ".join("-" * w for w in widths))

        def render(label, cells):
            print("  ".join([label.ljust(label_w)] + cells))

        render(row_label, [region.rjust(col_w[region]) for region in regions]
                          + ["TOTAL".rjust(total_w)])
        rule()
        for row_key in rows:
            cells = []
            for region in regions:
                w = col_w[region]
                val = data.get(row_key, {}).get(region, 0.0)
                cells.append(colorize(val, f"{val:{w}.2f}") if val >= 0.01
                             else " " * w)
            cells.append(f"{row_totals[row_key]:{total_w}.2f}")
            render(row_key, cells)
        rule()
        render("TOTAL",
               [f"{region_totals[r]:{col_w[r]}.2f}" for r in regions]
               + [f"{sum(row_totals.values()):{total_w}.2f}"])


def _register_ec2(awsut) -> None:
    ec2 = awsut.command("ec2", help="EC2 commands")

    @ec2.command("list", help="List EC2 instances with status")
    @guard
    def _ec2_list():
        instances = [i for r in _describe_instances()["Reservations"]
                     for i in r["Instances"]]
        rows = [[
            _ec2_name(i) or "-",
            i["InstanceId"],
            i["State"]["Name"],
            i.get("InstanceType") or "-",
            _ec2_public_dns(i) or "-",
        ] for i in instances]
        rows.sort(key=lambda r: (r[0], r[1]))
        print_header(f"{len(rows)} instance(s)", f"region {_region_label()}")
        print_table(["NAME", "INSTANCE ID", "STATE", "TYPE", "PUBLIC DNS"], rows)

    def _ec2_action(instance_name, operation, api_method):
        for reservation in _describe_instances()["Reservations"]:
            for instance in reservation["Instances"]:
                if _ec2_name(instance) == instance_name:
                    api_method(InstanceIds=[instance["InstanceId"]])
                    print(f"{operation} · instance {instance_name} · "
                          f"region {_region_label()}")
                    print(f"InstanceId {instance['InstanceId']}")
                    return
        raise SmError(f"no EC2 instance named {instance_name!r} in "
                      f"region {_region_label()}")

    @ec2.command(
        "start", help="Start instance by name",
        params=[arg("instance_name", completer=_Ec2InstanceNameCompleter())],
    )
    @guard
    def _ec2_start(instance_name):
        ec2_client = _get_boto3_client("ec2")
        _ec2_action(instance_name, "StartInstances", ec2_client.start_instances)

    @ec2.command(
        "stop", help="Stop instance by name",
        params=[arg("instance_name", completer=_Ec2InstanceNameCompleter())],
    )
    @guard
    def _ec2_stop(instance_name):
        ec2_client = _get_boto3_client("ec2")
        _ec2_action(instance_name, "StopInstances", ec2_client.stop_instances)

    @ec2.command(
        "reboot", help="Reboot instance by name",
        params=[arg("instance_name", completer=_Ec2InstanceNameCompleter())],
    )
    @guard
    def _ec2_reboot(instance_name):
        ec2_client = _get_boto3_client("ec2")
        _ec2_action(instance_name, "RebootInstances", ec2_client.reboot_instances)


def _register_logs(awsut) -> None:
    logs = awsut.command("logs", help="CloudWatch Logs commands")

    def _retention(group) -> str:
        days = group.get("retentionInDays")
        return f"{days}d" if days else "never"

    @logs.command(
        "list", help="List log groups",
        params=[arg("group_name", nargs="?",
                    completer=_LogGroupCompleter(),
                    help="Log group name pattern with widecards")],
    )
    @guard
    def _logs_list(group_name=None):
        logs_client = _get_boto3_client("logs")
        pattern = group_name if group_name is not None else "*"

        prefix = pattern
        for ch in ("*", "?"):
            pos = prefix.find(ch)
            if pos >= 0:
                prefix = prefix[:pos]

        found = [g for g in _list_log_groups(prefix)
                 if fnmatch.fnmatch(g["logGroupName"], pattern)]
        print_header(f"{len(found)} log group(s)", f"matching {pattern}",
                     f"region {_region_label()}")
        print_table(
            ["LOG GROUP", "RETENTION", "STORED"],
            [[g["logGroupName"], _retention(g), fmt_bytes(g.get("storedBytes"))]
             for g in found],
        )

        # One match means the pattern named a group, so go one level down: its
        # streams are what `monitor` takes next.
        if len(found) != 1:
            return
        group = found[0]["logGroupName"]
        streams = logs_client.describe_log_streams(
            logGroupName=group).get("logStreams", [])
        print()
        print_header(f"{len(streams)} stream(s)", f"log group {group}")
        print_table(
            ["STREAM", "FIRST EVENT", "LAST EVENT", "SIZE"],
            [[s["logStreamName"],
              fmt_time(_epoch_ms(s.get("firstEventTimestamp"))),
              fmt_time(_epoch_ms(s.get("lastEventTimestamp"))),
              fmt_bytes(s.get("storedBytes"))] for s in streams],
        )

    @logs.command(
        "monitor", help="Monitor a log stream",
        params=[
            arg("group_name",  completer=_LogGroupCompleter(),
                help="Log group name"),
            arg("stream_name", completer=_LogStreamCompleter(),
                help="Log stream name to monitor"),
            arg("--freq",     type=int, default=5, metavar="SECONDS",
                help="Polling frequency in seconds"),
            arg("--lookback", type=int, default=60, metavar="MINUTES",
                help="Lookback window in minutes"),
        ],
    )
    @guard
    def _logs_monitor(group_name, stream_name, freq, lookback):
        logs_client = _get_boto3_client("logs")
        start_time = int((time.time() - lookback * 60) * 1000)

        next_token = None
        try:
            while True:
                params = {
                    "logGroupName":  group_name,
                    "logStreamName": stream_name,
                    "startFromHead": True,
                    "limit":         1000,
                }
                if next_token:
                    params["nextToken"] = next_token
                else:
                    params["startTime"] = start_time

                try:
                    response = logs_client.get_log_events(**params)
                except logs_client.exceptions.ResourceNotFoundException:
                    raise SmError(f"no log stream {stream_name!r} in log group "
                                  f"{group_name!r}") from None

                for event in response["events"]:
                    if start_time > event["timestamp"]:
                        continue
                    msg = event["message"].replace("\0", "\\0")
                    print(msg)
                # Flush so a piped consumer (e.g. `... | grep ERROR`) sees
                # output as it arrives instead of one block-buffered chunk.
                sys.stdout.flush()

                if response["nextForwardToken"] != next_token:
                    next_token = response["nextForwardToken"]
                else:
                    # Short sleeps with a flush each tick — in a pipeline,
                    # Ctrl+C closes our stdout and the next flush() raises
                    # promptly instead of blocking until the full freq sec.
                    for _ in range(freq * 10):
                        time.sleep(0.1)
                        sys.stdout.flush()
        except (KeyboardInterrupt, BrokenPipeError):
            pass

    @logs.command(
        "export", help="Export a log group in a Zip file",
        params=[
            arg("group_name", completer=_LogGroupCompleter(),
                help="Log group name to export"),
            arg("s3_path", help="S3 path as a working place"),
            arg("--start-datetime", required=True, metavar="YYYYMMDD_HHMMSS",
                help="Start date-time in UTC, in YYYYMMDD_HHMMSS format"),
            arg("--end-datetime",   required=True, metavar="YYYYMMDD_HHMMSS",
                help="End date-time in UTC, in YYYYMMDD_HHMMSS format"),
        ],
    )
    @guard
    def _logs_export(group_name, s3_path, start_datetime, end_datetime):
        exporter = _LogsExporter(
            logs_client=_get_boto3_client("logs"),
            log_group=group_name,
            s3_path=s3_path,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
        )
        exporter.run()


def _register_cf(awsut) -> None:
    cf = awsut.command("cloudformation", help="CloudFormation commands")

    @cf.command(
        "list", help="List CloudFormation stacks",
        params=[
            arg("--include-deleted", action="store_true",
                help="Include deleted stacks"),
            arg("--include-nested", action="store_true",
                help="Include nested stacks"),
        ],
    )
    @guard
    def _cf_list(include_deleted, include_nested):
        cf_client = _get_boto3_client("cloudformation")
        stacks = _list_cf_stacks(
            cf_client,
            include_deleted=include_deleted,
            include_successfully_completed=True,
            include_nested=include_nested,
        )
        print_header(f"{len(stacks)} stack(s)", f"region {_region_label()}")
        print_table(
            ["STACK NAME", "STATUS", "NESTED", "UPDATED"],
            [[s["StackName"], s["StackStatus"],
              "nested" if "ParentId" in s else "-",
              fmt_time(s.get("LastUpdatedTime") or s.get("CreationTime"))]
             for s in stacks],
        )
        # A failed stack says why, in a section of its own below the table.
        for stack in stacks:
            if stack["StackStatus"].endswith("_FAILED"):
                print()
                _report_cf_failure(cf_client, stack)

    @cf.command("watch", help="Watch CloudFormation operations until they finish")
    @guard
    def _cf_watch():
        cf_client = _get_boto3_client("cloudformation")
        progress = _ProgressDots()
        stacks: list[dict] = []
        while True:
            status_list = []
            stacks = _list_cf_stacks(cf_client, include_nested=True)
            for stack in stacks:
                if stack["StackStatus"].endswith("_IN_PROGRESS"):
                    status_list.append(f"{stack['StackName']}:{stack['StackStatus']}")
            progress.tick(", ".join(status_list))
            if not status_list:
                progress.tick(None)
                break
            time.sleep(5)

        # Report any stacks that ended in a failed state (e.g. DELETE_FAILED),
        # including the failure reason and a clickable console hyperlink.
        failed = [s for s in stacks if s["StackStatus"].endswith("_FAILED")]
        for stack in failed:
            print()
            _report_cf_failure(cf_client, stack)

    @cf.command(
        "open", help="Open the CloudFormation management console",
        params=[arg("stack_name", completer=_CfStackNameCompleter())],
    )
    @guard
    def _cf_open(stack_name):
        region = _get_region() or "us-east-1"
        cf_client = _get_boto3_client("cloudformation")
        stacks = _list_cf_stacks(
            cf_client,
            include_deleted=False,
            include_successfully_completed=True,
            include_nested=True,
        )
        stack_arn = next(
            (s["StackId"] for s in stacks if s["StackName"] == stack_name),
            None,
        )
        if not stack_arn:
            raise SmError(f"no stack named {stack_name!r} in region {region}")

        encoded = urllib.parse.quote(stack_arn)
        url = (f"https://{region}.console.aws.amazon.com/cloudformation/home"
               f"?region={region}#/stacks?stackId={encoded}")

        if console_url_modifier_func is not None:
            profile_name = _get_profile()
            profile = _get_all_profiles().get(profile_name, {})
            url = console_url_modifier_func(
                profile.get("account", ""), profile.get("role", ""), url,
            )

        print(f"Opening {url}")
        webbrowser.open(url)


# ─── log exporter (used by `awsut logs export`) ─────────────────────────────

class _LogsExporter:
    def __init__(self, logs_client, log_group, s3_path, start_datetime, end_datetime):
        self.logs_client = logs_client
        self.log_group = log_group
        self.s3_path = s3_path
        self.start_datetime = start_datetime
        self.end_datetime = end_datetime

    def run(self):
        import gzip
        import shutil
        import tempfile

        utcnow = datetime.datetime.utcnow()

        with tempfile.TemporaryDirectory() as export_dir:
            with tempfile.TemporaryDirectory() as plaintext_dir:
                self._export_single_log_group(local_dirname=export_dir)
                self._convert_and_normalize(export_dir, plaintext_dir, gzip)
                self._create_account_info_file(plaintext_dir)
                self._create_zip_file(
                    plaintext_dir,
                    f"./exported_logs_{utcnow.strftime('%Y%m%d_%H%M%S')}",
                    shutil,
                )

    @staticmethod
    def _split_s3_path(s3_path):
        m = re.match(r"s3://([^/]+)/(.*)", s3_path)
        if not m:
            raise SmError(f"not an s3 path: {s3_path!r} (want s3://bucket/prefix)")
        return m.group(1), m.group(2).rstrip("/")

    def _export_single_log_group(self, local_dirname):
        s3_bucket, s3_prefix = self._split_s3_path(self.s3_path)
        start = datetime.datetime.strptime(self.start_datetime, "%Y%m%d_%H%M%S")
        end   = datetime.datetime.strptime(self.end_datetime,   "%Y%m%d_%H%M%S")

        response = self.logs_client.create_export_task(
            logGroupName=self.log_group,
            fromTime=int(start.timestamp() * 1000),
            to=int(end.timestamp() * 1000),
            destination=s3_bucket,
            destinationPrefix=s3_prefix,
        )
        task_id = response["taskId"]
        print(f"CreateExportTask · log group {self.log_group} · "
              f"s3://{s3_bucket}/{s3_prefix}")
        print(f"taskId {task_id}\n")

        # Stamped, and only when it changes — the same convention the `watch`
        # leaves follow, so a long export reads as a timeline.
        last = None
        while True:
            completed = False
            response = self.logs_client.describe_export_tasks(taskId=task_id)
            for task in response["exportTasks"]:
                if task["taskId"] == task_id:
                    code = task["status"]["code"]
                    msg = task["status"].get("message", "")
                    if (code, msg) != last:
                        stamp = datetime.datetime.now().strftime("%H:%M:%S")
                        print(f"[{stamp}] {code}" + (f" · {msg}" if msg else ""))
                        last = (code, msg)
                    if code in ("COMPLETED", "CANCELLED", "FAILED"):
                        completed = True
            if completed:
                break
            time.sleep(10)

        s3 = _get_boto3_client("s3")
        exported_prefix = f"{s3_prefix}/{task_id}"
        response = s3.list_objects_v2(Bucket=s3_bucket, Prefix=exported_prefix)
        for obj in response.get("Contents", []):
            key = obj["Key"]
            assert key.startswith(exported_prefix)
            rel = key[len(exported_prefix):].lstrip("/")
            local = os.path.join(local_dirname, rel)
            os.makedirs(os.path.split(local)[0], exist_ok=True)
            print(f"downloading {key}")
            s3.download_file(Bucket=s3_bucket, Key=key, Filename=local)

    def _convert_and_normalize(self, src, dst, gzip):
        for place, _, files in os.walk(src):
            line_groups: list[list[bytes]] = []
            for filename in files:
                if not filename.endswith(".gz"):
                    continue
                src_filepath = os.path.join(place, filename)
                print(f"reading {src_filepath}")
                with gzip.open(src_filepath) as fd:
                    raw = fd.read()
                for line in raw.splitlines():
                    if re.match(
                        rb"[0-9]{4}\-[0-9]{2}\-[0-9]{2}T[0-9]{2}\:[0-9]{2}\:[0-9]{2}\.[0-9]{3}Z .*",
                        line,
                    ):
                        line_groups.append([line])
                    elif line_groups:
                        line_groups[-1].append(line)

            if not line_groups:
                continue
            dst_filepath = os.path.join(
                dst, place[len(src):].lstrip("/\\") + ".log",
            )
            print(f"writing {dst_filepath}")
            line_groups.sort()
            lines: list[bytes] = []
            for grp in line_groups:
                lines += grp
            data = b"\n".join(lines).replace(b"\0", b"\\0")
            os.makedirs(os.path.split(dst_filepath)[0], exist_ok=True)
            with open(dst_filepath, "wb") as fd:
                fd.write(data)

    def _create_account_info_file(self, dirname):
        sts = _get_boto3_client("sts")
        account_id = sts.get_caller_identity()["Account"]
        region_name = sts.meta.region_name
        with open(os.path.join(dirname, "info.json"), "w") as fd:
            fd.write(json.dumps({
                "account_id":  account_id,
                "region_name": region_name,
            }))

    def _create_zip_file(self, dirname, zip_filename_no_ext, shutil):
        print(f"creating {zip_filename_no_ext}.zip")
        shutil.make_archive(zip_filename_no_ext, "zip", dirname)
