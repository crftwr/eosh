"""``awsut sagemaker studio`` — the Domain / Space / App triple, as one group.

Where ``jobs`` and ``hub`` are about what a job *produced*, this group is about
the environment a person works in: a **Domain** holds **Spaces** and **user
profiles**, and an **App** is the running compute inside a space.  The three are
one group rather than three because no operation on any of them is expressible
without the other two — a space is named only within a domain, an app only
within a space, and a presigned URL needs the domain, the space *and* a user
profile.

The operations are the ones a CloudFormation-driven Studio setup ends up
needing outside CloudFormation, because CFN cannot create a space app at all:
list what exists, start an app, mint a URL into it, stop it again, and read the
lifecycle-config log to find out what happened at boot.  A Makefile that drives
such a stack gets each of those from a *stack output* (``LaunchAppCommand``,
``PresignedUrlCommand``, …), which ties them to one stack.  Here the same
operations are derived from the resources themselves:

===========================  ====================================================
Makefile target              this group
===========================  ====================================================
``make show-apps``           ``apps``
``make show-roles``          ``profiles``           — the roles are its columns
``make logs``                ``logs [SPACE]``
``make launch-private``      ``start [SPACE]``      — ResourceSpec from DescribeSpace
``make url-private``         ``url [SPACE]``        — profile from the space's owner
(open that URL by hand)      ``open [SPACE]``       — the same URL, spent here
``make stop-private``        ``stop [SPACE]``
``make stop-all``            ``stop --all``
===========================  ====================================================

So there is no ``-private`` / ``-shared`` pair of anything: the space is an
argument, and what the Makefile had to hard-code per space (which image and
instance type to launch, which user profile owns it, which app type it runs) is
read from the space itself.

The Makefile's ``launch`` → ``url`` ordering is enforced rather than documented:
``url`` lands on the space's *app*, so with no app running it refuses and points
at ``start`` instead of minting a link that fails in the recipient's browser.

``url`` and ``open`` mint the identical link (one ``mint_url``, one flag set)
and differ only in where it is spent: printed for someone else, or handed to
this machine's browser.

``watch`` is the read-only view of the two slow operations in the table:
``start`` and ``stop`` both return long before the app they asked for is
actually there or actually gone, and their ``--wait`` only follows the *one*
app that leaf acted on.  ``watch`` follows a whole domain — every space and
every app in it — so a start triggered from the console, a ``stop --all``, and
a space that is still ``Pending`` all show up on the same timeline.  Its space
selector is a ``--space`` flag rather than the positional ``SPACE`` every other
leaf here takes, because its no-argument default is the whole domain, not "the
domain's only space": the positional's meaning is fixed across the group, and
the same word must not resolve one way in one leaf and another way next door.

Naming follows the rest of ``awsut``: a plural noun lists that resource type
(as ``hub`` does with ``hubs`` / ``versions`` / ``files``, because a group
holding several resource types has no single thing a bare ``list`` could mean),
the state-changing pair is ``start`` / ``stop`` — the same pair as
``awsut ec2 start`` / ``awsut ec2 stop``, and for the same reason: compute goes
away, storage stays — and ``open`` opens a browser, the same leaf name
``awsut cloudformation open`` already uses.

Two resolution rules, applied everywhere in this module:

* ``--domain`` accepts a domain **name** or a ``d-…`` **id**, and may be
  omitted when the region holds exactly one domain (the common case).
* ``SPACE`` may be omitted when the domain holds exactly one space.  Ambiguity
  is always an error naming the candidates — never a silent pick.

Same ground rules as the rest of the tree: identifiers print in full, column
widths come from the data, and region/profile come from ``var aws_region=`` /
``var aws_profile=`` rather than from flags.
"""

from __future__ import annotations

import sys
import time
import webbrowser
from datetime import datetime

import botocore.exceptions

from ...commands import arg
from ...completion import ChoiceCompleter, Completer, Completion, CompletionContext
from ...completion_cache import aws_env_key, get_or_fetch
from ...shell import passthrough_input
from .. import awsut
from .render import (
    DOT_INTERVAL,
    Heartbeat,
    INSTANCE_TYPE_CHOICES,
    NOT_FOUND_CODES,
    SmError,
    api_message,
    elapsed_of,
    error_code,
    flag_value,
    fmt_bytes,
    fmt_dur,
    fmt_time,
    guard,
    log_streams,
    logs_client,
    model_enum,
    paged,
    positionals,
    print_header,
    print_labeled,
    print_table,
    read_log_stream,
    region_label,
    require_input_member,
    require_operation,
    sleep_with_dots,
    sm_client,
    stream_table_rows,
)

# Studio writes every space's app output to one log group, one stream per
# app-and-purpose:  <domain-id>/<space>/<app-type>/<app-name>/<stream>
STUDIO_LOG_GROUP = "/aws/sagemaker/studio"

# The boot-time stream: what a lifecycle config printed while the app started.
# It is the stream worth reading by default because it is where setup either
# worked or didn't — and an app that never reached InService has nothing else.
LIFECYCLE_STREAM = "LifecycleConfigOnStart"

# JupyterLab and CodeEditor space apps must be named "default" — the service
# allows exactly one per space.
DEFAULT_APP_NAME = "default"

# Which member of SpaceSettings carries an app type's DefaultResourceSpec.  Used
# to answer "launch this space the way the space says it should be launched"; a
# type absent from here still works, it just contributes no default spec.
APP_SETTINGS_KEYS = {
    "JupyterLab": "JupyterLabAppSettings",
    "CodeEditor": "CodeEditorAppSettings",
    "JupyterServer": "JupyterServerAppSettings",
    "KernelGateway": "KernelGatewayAppSettings",
}

APP_STATUSES = ["Deleted", "Deleting", "Failed", "InService", "Pending"]

# An app in one of these is gone or going: nothing to stop, nothing billing.
APP_DEAD = {"Deleted", "Deleting", "Failed"}

# The statuses a resource leaves on its own, so ``watch`` has something coming
# while anything sits in one.  Everything else — InService, Deleted, and the
# three ``*Failed`` spellings a space can carry — only moves if someone acts,
# which is what "settled" means when the watch decides whether it is done.
APP_TRANSIENT = {"Pending", "Deleting"}
SPACE_TRANSIENT = {"Pending", "Updating", "Deleting"}
TRANSIENT = {"app": APP_TRANSIENT, "space": SPACE_TRANSIENT}

# Every value-taking flag in this group, so :func:`positionals` can tell a
# flag's value from a positional when a completer reads back what was typed.
VALUE_FLAGS = (
    "--domain", "--space", "--app-type", "--app-name", "--instance-type",
    "--user-profile", "--landing-uri", "--expires", "--session-duration",
    "--stream", "--log-group", "--lookback", "--timeout", "--max",
    "-n", "--interval",
)


# ─── domains ────────────────────────────────────────────────────────────────

def list_domains(cli, max_items=100) -> list[dict]:
    require_operation(cli, "list_domains", "ListDomains")
    return paged(cli.list_domains, "Domains", max_items,
                 MaxResults=min(max_items, 100))


def resolve_domain(cli, requested: str | None) -> dict:
    """The domain to work in, as its ListDomains summary.

    Accepts a name or a ``d-…`` id — a domain id is what every API here
    actually takes, but a name is what a human has — and falls back to the
    region's only domain when there is exactly one.  Matching happens locally
    against one ListDomains call rather than by DescribeDomain, so the same
    single call serves both spellings and produces the candidate list an
    ambiguous or missing name is reported with.
    """
    domains = list_domains(cli)
    if requested:
        matches = [d for d in domains
                   if requested in (d.get("DomainId"), d.get("DomainName"))]
        if not matches:
            raise SmError(f"no domain named or identified by {requested!r} in region "
                          f"{region_label()}{_domain_menu(domains)}")
        if len(matches) > 1:
            # Domain names are not unique; ids are.
            ids = ", ".join(d.get("DomainId", "?") for d in matches)
            raise SmError(f"{len(matches)} domains are named {requested!r}; "
                          f"pass one of these ids instead: {ids}")
        return matches[0]
    if not domains:
        raise SmError(f"no Studio domain in region {region_label()}")
    if len(domains) > 1:
        raise SmError(f"{len(domains)} domains in region {region_label()}; "
                      f"pass --domain{_domain_menu(domains)}")
    return domains[0]


def _domain_menu(domains) -> str:
    if not domains:
        return ""
    return "\n       known: " + ", ".join(
        f"{d.get('DomainName')} ({d.get('DomainId')})" for d in domains)


def domain_label(domain: dict) -> str:
    """``name (d-id)`` — every listing header says which domain it read."""
    return f"{domain.get('DomainName')} ({domain.get('DomainId')})"


def describe_domain(cli, domain_id: str) -> dict:
    require_operation(cli, "describe_domain", "DescribeDomain")
    return cli.describe_domain(DomainId=domain_id)


# ─── spaces ─────────────────────────────────────────────────────────────────

def list_spaces(cli, domain_id: str, max_items=100) -> list[dict]:
    require_operation(cli, "list_spaces", "ListSpaces")
    return paged(cli.list_spaces, "Spaces", max_items,
                 DomainIdEquals=domain_id, MaxResults=min(max_items, 100))


def describe_space(cli, domain_id: str, space: str) -> dict:
    require_operation(cli, "describe_space", "DescribeSpace")
    try:
        return cli.describe_space(DomainId=domain_id, SpaceName=space)
    except botocore.exceptions.ClientError as exc:
        if error_code(exc) in NOT_FOUND_CODES:
            names = [s.get("SpaceName", "?") for s in list_spaces(cli, domain_id)]
            raise SmError(f"no space named {space!r} in this domain"
                          + (f"\n       known: {', '.join(names)}" if names else ""))
        raise


def resolve_space(cli, domain_id: str, requested: str | None) -> dict:
    """DescribeSpace for the space asked for, or for the domain's only one.

    The description (not just the name) is the return value because every
    caller needs something out of it: the app type to act on, the resource spec
    to launch with, or the user profile that owns it.
    """
    if requested:
        return describe_space(cli, domain_id, requested)
    spaces = list_spaces(cli, domain_id)
    if not spaces:
        raise SmError("this domain has no spaces")
    if len(spaces) > 1:
        names = ", ".join(s.get("SpaceName", "?") for s in spaces)
        raise SmError(f"{len(spaces)} spaces in this domain; name one of: {names}")
    return describe_space(cli, domain_id, spaces[0]["SpaceName"])


def space_app_type(space_desc: dict, override: str | None = None) -> str:
    """The app type to act on: what was asked for, else what the space declares."""
    if override:
        return override
    settings = space_desc.get("SpaceSettings") or {}
    declared = settings.get("AppType")
    if declared:
        return declared
    # A space created before AppType existed (or one carrying several app
    # settings) declares none; the only settings block present says as much.
    present = [t for t, key in APP_SETTINGS_KEYS.items() if settings.get(key)]
    if len(present) == 1:
        return present[0]
    if present:
        raise SmError(f"this space declares no AppType and carries settings for "
                      f"{', '.join(present)} — pass --app-type")
    return "JupyterLab"


def space_owner(space_desc: dict) -> str | None:
    return (space_desc.get("OwnershipSettings") or {}).get("OwnerUserProfileName")


def sharing_type(space: dict) -> str:
    """``Private`` / ``Shared``, from either the summary or the description shape."""
    for key in ("SpaceSharingSettings", "SpaceSharingSettingsSummary"):
        value = (space.get(key) or {}).get("SharingType")
        if value:
            return value
    return "-"


# The newer ResourceSpec members and the image members they alias, with the
# ARN resource type each newer member is actually allowed to name.
_ENVIRONMENT_ALIASES = {
    "EnvironmentArn": (":environment/", "SageMakerImageArn"),
    "EnvironmentVersionArn": (":environment-version/",
                              "SageMakerImageVersionArn"),
}


def _resolve_environment_aliases(spec: dict) -> dict:
    """Move an image ARN out of the ``Environment*Arn`` member holding it.

    DescribeSpace fills ``EnvironmentArn`` / ``EnvironmentVersionArn`` in as
    aliases of the image members, echoing the *image* ARNs into them — but
    CreateApp validates those two against ``environment/…`` and
    ``environment-version/…`` and rejects an ``image/…`` ARN outright, so the
    spec cannot be sent back as it arrived.  Each alias holding an image ARN
    is therefore moved onto the image member that admits it (a no-op when the
    space already carries that member, which is the common case), and one
    genuinely naming an environment is left where it is.
    """
    for member, (resource, image_member) in _ENVIRONMENT_ALIASES.items():
        value = spec.get(member)
        if not value or resource in value:
            continue
        spec.pop(member)
        spec.setdefault(image_member, value)
    return spec


def default_resource_spec(space_desc: dict, app_type: str) -> dict:
    """The space's own DefaultResourceSpec for *app_type*, or ``{}``.

    CreateApp's ResourceSpec is optional, but leaving it out asks the service
    for *its* default rather than the space's — which for a space pinned to a
    custom image version is a different image.  Reading the spec off the space
    and sending it back is what makes ``start`` equivalent to the launch
    command a CloudFormation stack emits, without knowing anything about the
    stack.  What comes back is not accepted verbatim, though — see
    :func:`_resolve_environment_aliases`.
    """
    settings = space_desc.get("SpaceSettings") or {}
    key = APP_SETTINGS_KEYS.get(app_type)
    spec = (settings.get(key) or {}).get("DefaultResourceSpec") if key else None
    return _resolve_environment_aliases(dict(spec or {}))


def ebs_size(space: dict) -> str:
    storage = ((space.get("SpaceSettings") or {}).get("SpaceStorageSettings")
               or (space.get("SpaceSettingsSummary") or {}).get("SpaceStorageSettings")
               or {})
    size = (storage.get("EbsStorageSettings") or {}).get("EbsVolumeSizeInGb")
    return f"{size}GB" if size else "-"


# ─── user profiles ──────────────────────────────────────────────────────────

def list_user_profiles(cli, domain_id: str, max_items=100) -> list[dict]:
    require_operation(cli, "list_user_profiles", "ListUserProfiles")
    return paged(cli.list_user_profiles, "UserProfiles", max_items,
                 DomainIdEquals=domain_id, MaxResults=min(max_items, 100))


def profile_role(cli, domain_id: str, profile: str) -> str:
    """A user profile's execution role, or a note saying why it isn't shown.

    A profile that sets no role of its own inherits the domain's, and the API
    reports that as an absent member rather than as the inherited value — so
    the two cases have to read differently.
    """
    require_operation(cli, "describe_user_profile", "DescribeUserProfile")
    try:
        desc = cli.describe_user_profile(DomainId=domain_id,
                                         UserProfileName=profile)
    except botocore.exceptions.ClientError as exc:
        return f"(not readable: {api_message(exc)})"
    role = (desc.get("UserSettings") or {}).get("ExecutionRole")
    return role or "(none of its own — inherits DefaultUserSettings)"


# ─── apps ───────────────────────────────────────────────────────────────────

def list_apps(cli, domain_id: str, space: str | None = None,
              max_items=100) -> list[dict]:
    require_operation(cli, "list_apps", "ListApps")
    params = {"DomainIdEquals": domain_id, "MaxResults": min(max_items, 100)}
    if space:
        params["SpaceNameEquals"] = space
    return paged(cli.list_apps, "Apps", max_items, **params)


def live_apps_by_space(cli, domain_id: str) -> dict[str, list[dict]]:
    """The domain's not-dead apps, grouped by space name.  One ListApps call.

    "Is anything running in this space?" is the question both the ``spaces``
    table and the space completer answer, and they read it from here so the two
    cannot disagree — a space's own ``Status`` is ``InService`` as soon as the
    space *exists*, which says nothing about whether an app is billing, and
    showing that status unqualified next to an app listing invites exactly that
    misreading.

    A list per space, not one app: nothing stops a space from running a
    JupyterLab *and* a KernelGateway app at once, and dropping one would
    under-report what is billing.
    """
    grouped: dict[str, list[dict]] = {}
    for app in list_apps(cli, domain_id):
        space = app.get("SpaceName")
        if space and app.get("Status") not in APP_DEAD:
            grouped.setdefault(space, []).append(app)
    return grouped


def live_apps_in_space(cli, domain_id: str, space: str,
                       app_type: str | None = None) -> list[dict]:
    """One space's not-dead apps, optionally narrowed to a single app type.

    Scoped server-side (``SpaceNameEquals``), unlike :func:`live_apps_by_space`,
    which answers the same question for a whole domain in one call.  Use this
    one when a single space is already named on the line.
    """
    return [a for a in list_apps(cli, domain_id, space)
            if a.get("Status") not in APP_DEAD
            and (app_type is None or a.get("AppType") == app_type)]


def app_label(app: dict) -> str:
    """``JupyterLab:InService`` — an app named by the two things that matter."""
    return f"{app.get('AppType') or '?'}:{app.get('Status') or '?'}"


def describe_app(cli, domain_id, space, app_type, app_name) -> dict:
    require_operation(cli, "describe_app", "DescribeApp")
    return cli.describe_app(DomainId=domain_id, SpaceName=space,
                            AppType=app_type, AppName=app_name)


def app_status(cli, domain_id, space, app_type, app_name) -> str | None:
    """An app's status, or ``None`` when no such app has ever existed."""
    try:
        return describe_app(cli, domain_id, space, app_type, app_name).get("Status")
    except botocore.exceptions.ClientError as exc:
        if error_code(exc) in NOT_FOUND_CODES:
            return None
        raise


def instance_of(app: dict) -> str:
    return (app.get("ResourceSpec") or {}).get("InstanceType") or "-"


# ─── completers ─────────────────────────────────────────────────────────────
#
# All of them fail silently to ``[]``: a completer runs on the keystroke path,
# where an exception would be noise rather than information.  The fetches go
# through the TTL cache so a typed token costs one call, not one per keystroke.

def _cached_domains() -> list[dict]:
    return get_or_fetch(
        ("awsut.studio_domains", aws_env_key(), awsut.sagemaker_endpoint,
         awsut.sagemaker_service_name),
        lambda: list_domains(sm_client()),
    )


def _cached_in_domain(kind: str, requested: str | None, fetch) -> list[dict]:
    """Run *fetch(cli, domain_id)* against the domain a line names, cached."""
    def go():
        cli = sm_client()
        return fetch(cli, resolve_domain(cli, requested)["DomainId"])

    return get_or_fetch(
        (f"awsut.studio_{kind}", aws_env_key(), awsut.sagemaker_endpoint,
         awsut.sagemaker_service_name, requested),
        go,
    )


class _DomainCompleter(Completer):
    """Domain names *and* ids — either spelling is accepted by ``--domain``."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            domains = _cached_domains()
        except Exception:
            return []
        out = []
        for d in domains:
            status = d.get("Status") or "?"
            # The other spelling of the same domain, then its status — two
            # columns, so ids line up under ids and statuses under statuses.
            for value, other in ((d.get("DomainName"), d.get("DomainId")),
                                 (d.get("DomainId"), d.get("DomainName"))):
                if value and value.startswith(ctx.prefix):
                    out.append(Completion(value=value, fields=(other or "", status)))
        return out


class _SpaceCompleter(Completer):
    """Spaces in the domain named on the line, described by what they are.

    Three aligned columns — sharing, app type, what is running — plus a fourth
    that appears only for a space whose own ``Status`` is worth reading.  The
    owner profile is deliberately *not* among them: no leaf taking a space asks
    the user to choose by owner, so on the keystroke path it is a column of
    noise between the two answers that do decide the pick (``url``'s default
    profile does come from the owner, but that is derived, not typed).

    Whether an **app** is live is the column the leaves actually branch on:
    ``start`` wants a space with none, ``stop`` and ``url`` want one with
    something running.  A space's own ``Status`` is *not* that answer — it reads
    ``InService`` from the moment the space exists — so it is shown only when it
    is something other than ``InService`` and is labelled ``space …`` when it
    is.  An unlabelled ``InService`` here would contradict the ``apps`` listing
    for a space whose app has been stopped.
    """

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        requested = flag_value(ctx.args, "--domain")
        try:
            spaces = _cached_in_domain("spaces", requested, list_spaces)
        except Exception:
            return []
        # A second permission (ListApps), so its absence costs the app note
        # rather than the candidate list.  `None` means "not known", which must
        # not be rendered as "nothing is running".
        try:
            live = _cached_in_domain("live_apps", requested, live_apps_by_space)
        except Exception:
            live = None

        out = []
        for s in spaces:
            name = s.get("SpaceName") or ""
            if not name.startswith(ctx.prefix):
                continue
            summary = s.get("SpaceSettingsSummary") or {}
            sharing = sharing_type(s)
            status = s.get("Status") or ""
            out.append(Completion(value=name, fields=(
                "" if sharing == "-" else sharing,
                summary.get("AppType") or "",
                f"space {status}" if status and status != "InService" else "",
                _app_note(live, name),
            )))
        return out


def _app_note(live: dict[str, list[dict]] | None, space: str) -> str:
    """What is running in *space*, or ``""`` when ListApps could not be read."""
    if live is None:
        return ""
    apps = live.get(space) or []
    if not apps:
        return "no app"
    if len(apps) == 1:
        return f"app {apps[0].get('Status') or '?'}"
    return f"{len(apps)} apps live"


class _UserProfileCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            profiles = _cached_in_domain("profiles", flag_value(ctx.args, "--domain"),
                                         list_user_profiles)
        except Exception:
            return []
        return [Completion(value=p["UserProfileName"], description=p.get("Status") or "")
                for p in profiles
                if p.get("UserProfileName", "").startswith(ctx.prefix)]


class _AppTypeCompleter(Completer):
    """App types from the loaded model — offline, so it costs nothing."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        types = list(model_enum(awsut.sagemaker_service_name, "CreateApp", "AppType"))
        return [Completion(value=t) for t in (types or list(APP_SETTINGS_KEYS))
                if t.startswith(ctx.prefix)]


class _StreamCompleter(Completer):
    """Log streams that exist for the space named on the line.

    Offered as the bare last segment (``LifecycleConfigOnStart``) rather than
    the full ``<domain>/<space>/…`` path, because that is what ``--stream``
    takes — the rest of the path is derived.
    """

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        typed = positionals(ctx.args, VALUE_FLAGS)
        try:
            cli = sm_client()
            domain = resolve_domain(cli, flag_value(ctx.args, "--domain"))
            space = typed[0] if typed else resolve_space(
                cli, domain["DomainId"], None).get("SpaceName")
            prefix = f"{domain['DomainId']}/{space}/"
            streams = get_or_fetch(
                ("awsut.studio_streams", aws_env_key(), prefix),
                lambda: _describe_streams(prefix),
            )
        except Exception:
            return []
        out = []
        for s in streams:
            tail = s["logStreamName"][len(prefix):]
            if tail.startswith(ctx.prefix):
                # No description: every row here is a log stream, so saying so
                # would only be a column of the same word down the list.
                out.append(Completion(value=tail))
        return out


def _describe_streams(prefix: str) -> list[dict]:
    return logs_client().describe_log_streams(
        logGroupName=STUDIO_LOG_GROUP, logStreamNamePrefix=prefix,
    ).get("logStreams", [])


# ─── shared flags ───────────────────────────────────────────────────────────
#
# Declared per leaf rather than on the group, following ``hub``: a flag offered
# where it does nothing is a lie, and ``domains`` has no domain to select.

def _domain_flag():
    return arg("--domain", metavar="NAME_OR_ID", completer=_DomainCompleter(),
               help="domain name or d-… id (default: the region's only domain)")


def _space_arg(help_text="space to act on (default: the domain's only space)"):
    return arg("space", nargs="?", default=None, completer=_SpaceCompleter(),
               help=help_text)


def _app_flags():
    return [
        arg("--app-type", metavar="TYPE", completer=_AppTypeCompleter(),
            help="default: the space's own AppType"),
        arg("--app-name", metavar="NAME", default=DEFAULT_APP_NAME,
            help=f"default {DEFAULT_APP_NAME!r} — the only name a JupyterLab "
                 "or CodeEditor space app may have"),
    ]


def _wait_flags(what: str):
    return [
        arg("--wait", action="store_true", help=f"poll until the app is {what}"),
        arg("--timeout", type=float, default=900.0, metavar="SEC",
            help="give up waiting after this long (default 900); 0 = no limit"),
    ]


def _url_flags():
    """The flags that describe *which* URL to mint — shared by ``url``/``open``.

    One list rather than two, because the two leaves differ only in what they
    do with the URL afterwards: every knob that changes the link itself has to
    read the same way on both, or the pair becomes two subtly different
    commands.  A fresh list per call — each leaf gets its own ``Arg`` objects.
    """
    return [
        _space_arg("space to land in (omit together with --user-profile "
                   "for the domain's Studio home)"),
        _domain_flag(),
        arg("--user-profile", metavar="NAME", completer=_UserProfileCompleter(),
            help="default: the space's owner profile"),
        arg("--landing-uri", metavar="URI",
            help="default: app:<app type>: for a space, none for the domain"),
        arg("--expires", type=int, default=300, metavar="SEC",
            help="how long the URL itself is usable (default 300)"),
        arg("--session-duration", type=int, default=43200, metavar="SEC",
            help="how long the session it opens lasts (default 43200)"),
        arg("--app-type", metavar="TYPE", completer=_AppTypeCompleter(),
            help="default: the space's own AppType (picks the landing URI)"),
        arg("--no-app-check", action="store_true",
            help="mint the URL even with no app running in the space"),
    ]


# ─── command tree ───────────────────────────────────────────────────────────

def register_studio(sagemaker) -> None:
    studio = sagemaker.command(
        "studio",
        help="Studio domains, spaces, user profiles and the apps that bill",
    )

    # ── read-only ──────────────────────────────────────────────────────────

    @studio.command(
        "domains",
        help="List the Studio domains in this region (URL is the console entry "
             "point, and needs an AWS identity — `url` mints one for a profile "
             "that has none)",
        params=[arg("--max", type=int, default=50, dest="limit", metavar="N",
                    help="domains to list (default 50)")],
    )
    @guard
    def _domains(limit):
        cli = sm_client()
        domains = list_domains(cli, limit)
        rows = [[
            d.get("DomainName") or "?",
            d.get("DomainId") or "?",
            d.get("Status") or "?",
            fmt_time(d.get("CreationTime")),
            d.get("Url") or "-",
        ] for d in domains]
        print_header(f"{len(rows)} domain(s)", f"region {region_label()}")
        print_table(["DOMAIN NAME", "DOMAIN ID", "STATUS", "CREATED", "URL"], rows)

    @studio.command(
        "spaces",
        help="List the spaces in a domain (creates nothing). APP '-' means "
             "nothing is running there, so it bills no compute — its EBS volume "
             "is charged either way, and STATUS is the space's own, InService "
             "whether or not an app runs",
        params=[_domain_flag(),
                arg("--max", type=int, default=100, dest="limit", metavar="N",
                    help="spaces to list (default 100)")],
    )
    @guard
    def _spaces(domain, limit):
        cli = sm_client()
        resolved = resolve_domain(cli, domain)
        spaces = list_spaces(cli, resolved["DomainId"], limit)
        # Which space is billing is the first thing anyone wants from this
        # listing, but it needs a second permission (ListApps) that ListSpaces
        # does not — so a refusal costs the column, not the listing.
        running = {}
        app_note = None
        if spaces:
            try:
                for space, apps in live_apps_by_space(
                        cli, resolved["DomainId"]).items():
                    running[space] = ",".join(app_label(a) for a in apps)
            except botocore.exceptions.ClientError as exc:
                print(f"error: apps not listed: {api_message(exc)}",
                      file=sys.stderr)
                app_note = "APP could not be filled in — see stderr."
        rows = [[
            s.get("SpaceName") or "?",
            sharing_type(s),
            (s.get("OwnershipSettingsSummary") or {}).get(
                "OwnerUserProfileName") or "-",
            (s.get("SpaceSettingsSummary") or {}).get("AppType") or "-",
            ebs_size(s),
            s.get("Status") or "?",
            running.get(s.get("SpaceName"), "-"),
            fmt_time(s.get("CreationTime")),
        ] for s in spaces]
        rows.sort(key=lambda r: r[0])
        print_header(f"{len(rows)} space(s)", f"domain {domain_label(resolved)}",
                     f"region {region_label()}")
        print_table(
            ["SPACE", "SHARING", "OWNER", "APP TYPE", "EBS", "STATUS", "APP",
             "CREATED"],
            rows, note=app_note,
        )

    @studio.command(
        "apps",
        help="List the apps in a domain and their status (creates nothing). An "
             "InService app bills for its instance until `stop` deletes it; AGE "
             "is time since creation",
        params=[
            _domain_flag(),
            arg("--space", metavar="SPACE", completer=_SpaceCompleter(),
                help="only this space's apps"),
            arg("--include-dead", action="store_true",
                help="also show Deleted/Deleting/Failed apps"),
            arg("--max", type=int, default=100, dest="limit", metavar="N",
                help="apps to list (default 100)"),
        ],
    )
    @guard
    def _apps(domain, space, include_dead, limit):
        cli = sm_client()
        resolved = resolve_domain(cli, domain)
        apps = list_apps(cli, resolved["DomainId"], space, limit)
        shown = [a for a in apps
                 if include_dead or a.get("Status") not in APP_DEAD]
        scope = f"domain {domain_label(resolved)}"
        if space:
            scope += f" · space {space}"
        rows = [[
            a.get("SpaceName") or "-",
            a.get("UserProfileName") or "-",
            a.get("AppType") or "?",
            a.get("AppName") or "?",
            a.get("Status") or "?",
            instance_of(a),
            fmt_time(a.get("CreationTime")),
            fmt_dur(elapsed_of(a.get("CreationTime"), None)),
        ] for a in shown]
        rows.sort(key=lambda r: (r[0], r[2], r[3]))
        print_header(f"{len(rows)} app(s)", scope, f"region {region_label()}")
        note = None
        if not include_dead and len(apps) != len(shown):
            note = (f"{len(apps) - len(shown)} Deleted/Deleting/Failed app(s) "
                    "hidden; --include-dead shows them.")
        print_table(
            ["SPACE", "USER PROFILE", "APP TYPE", "APP NAME", "STATUS", "INSTANCE",
             "CREATED", "AGE"],
            rows, note=note,
        )

    @studio.command(
        "profiles",
        help="List a domain's user profiles and the role each assumes (what a "
             "running app ACTUALLY assumed is in its boot log: `logs SPACE`)",
        params=[_domain_flag()],
    )
    @guard
    def _profiles(domain):
        """The domain's user profiles — the identities its spaces run as.

        A profile is what ``url --user-profile`` takes and what a private space
        records as its owner, so this is the listing that says which values those
        accept.  The execution role travels with each row because that is the
        non-obvious part: a private space runs as the *profile*'s role, a shared
        space as the domain's ``DefaultSpaceSettings`` role, and a profile that
        sets no role of its own inherits ``DefaultUserSettings`` — three
        different roles can be in play in one domain and the console shows none
        of it.  The two domain-level defaults belong in the identifier block
        rather than as rows in the table: they are properties of the domain, and
        what every row falls back to.
        """
        cli = sm_client()
        resolved = resolve_domain(cli, domain)
        domain_id = resolved["DomainId"]
        desc = describe_domain(cli, domain_id)

        def default_role(setting):
            return (desc.get(setting) or {}).get("ExecutionRole") or "-"

        print_labeled([
            ("Domain", domain_label(resolved)),
            ("Region", region_label()),
            ("AuthMode", desc.get("AuthMode") or "?"),
            None,
            ("DefaultUserSettings", f"{default_role('DefaultUserSettings')} · "
                                    "what a private space falls back to"),
            ("DefaultSpaceSettings", f"{default_role('DefaultSpaceSettings')} · "
                                     "what a shared space falls back to"),
        ])
        print()

        profiles = list_user_profiles(cli, domain_id)
        rows = [[
            p.get("UserProfileName") or "?",
            p.get("Status") or "?",
            fmt_time(p.get("CreationTime")),
            profile_role(cli, domain_id, p.get("UserProfileName") or ""),
        ] for p in profiles]
        rows.sort(key=lambda r: r[0])
        print_header(f"{len(rows)} user profile(s)")
        print_table(["USER PROFILE", "STATUS", "CREATED", "EXECUTION ROLE"], rows)

    @studio.command(
        "log", help="A space app's boot log — what its lifecycle config did",
        params=[
            _space_arg("space whose app log to read"),
            _domain_flag(),
            arg("--stream", metavar="NAME", default=LIFECYCLE_STREAM,
                completer=_StreamCompleter(),
                help=f"last path segment of the stream (default {LIFECYCLE_STREAM})"),
            arg("--list", action="store_true", dest="list_streams",
                help="list this space's streams instead of reading one (a listed "
                     "STREAM is what --stream takes)"),
            arg("-f", "--follow", action="store_true",
                help="keep polling for new events (Ctrl+C to stop)"),
            arg("--lookback", type=int, default=0, metavar="MINUTES",
                help="only events from the last N minutes (default: all)"),
            arg("--log-group", metavar="GROUP", default=STUDIO_LOG_GROUP,
                help=f"default {STUDIO_LOG_GROUP}"),
        ] + _app_flags(),
    )
    @guard
    def _logs(space, domain, stream, list_streams, follow, lookback, log_group,
              app_type, app_name):
        cli = sm_client()
        resolved = resolve_domain(cli, domain)
        domain_id = resolved["DomainId"]
        space_desc = resolve_space(cli, domain_id, space)
        space_name = space_desc.get("SpaceName")
        prefix = f"{domain_id}/{space_name}/"

        if list_streams:
            _list_space_streams(log_group, prefix, space_name)
            return

        resolved_type = space_app_type(space_desc, app_type)
        full = f"{prefix}{resolved_type}/{app_name}/{stream}"
        print_header(log_group, full)
        _read_stream(log_group, full, follow, lookback)

    @studio.command(
        "watch",
        help="Log every space and app status change in a domain — the progress "
             "of a start or a stop (creates nothing; returns once nothing is in "
             "flight, unless -f)",
        params=[
            _domain_flag(),
            arg("--space", metavar="SPACE", completer=_SpaceCompleter(),
                help="only this space and its apps (default: the whole domain)"),
            arg("-f", "--follow", action="store_true",
                help="keep watching after everything settles, rather than "
                     "returning"),
            arg("-n", "--interval", type=float, default=10.0, metavar="SEC",
                help="poll seconds (default 10)"),
            arg("--timeout", type=float, default=0.0, metavar="SEC",
                help="give up after this long; 0 = no limit"),
            arg("--max", type=int, default=100, dest="limit", metavar="N",
                help="spaces and apps to track (default 100)"),
        ],
    )
    @guard
    def _watch(domain, space, follow, interval, timeout, limit):
        """Poll the domain and print one line per change, as ``jobs watch`` does.

        Returning once everything is settled is the default because that is what
        the command is for — following an app from ``Pending`` to ``InService``,
        or from ``Deleting`` to gone — and a watch that had to be interrupted to
        end could not be put in a script or a ``&&`` chain.  ``-f`` turns it into
        an open-ended monitor, which is also what makes a *pre-emptive* watch
        possible: with nothing in flight yet, plain ``watch`` says so and
        returns rather than sitting on a domain that may never move.
        """
        cli = sm_client()
        resolved = resolve_domain(cli, domain)
        # Resolve the space before the loop: an unknown name would otherwise
        # match nothing every tick and read as "this domain is idle", when the
        # truth is that the name is wrong.  DescribeSpace names the real ones.
        if space:
            space = resolve_space(cli, resolved["DomainId"], space)["SpaceName"]
        _watch_studio(cli, resolved, space, interval, timeout, follow, limit)

    # ── starts and stops billing ───────────────────────────────────────────

    @studio.command(
        "start", help="Start a space's app — THIS STARTS BILLING",
        params=[
            _space_arg("space to start an app in"),
            _domain_flag(),
            arg("--instance-type", metavar="TYPE",
                completer=ChoiceCompleter(INSTANCE_TYPE_CHOICES),
                help="override the space's own DefaultResourceSpec instance type"),
            arg("--no-resource-spec", action="store_true",
                help="send no ResourceSpec — let the service pick, not the space"),
        ] + _app_flags() + _wait_flags("InService or Failed"),
    )
    @guard
    def _start(space, domain, instance_type, no_resource_spec, app_type, app_name,
               wait, timeout):
        cli = sm_client()
        require_operation(cli, "create_app", "CreateApp")
        resolved = resolve_domain(cli, domain)
        domain_id = resolved["DomainId"]
        space_desc = resolve_space(cli, domain_id, space)
        space_name = space_desc.get("SpaceName")
        resolved_type = space_app_type(space_desc, app_type)

        existing = app_status(cli, domain_id, space_name, resolved_type, app_name)
        if existing and existing not in APP_DEAD:
            print(f"{resolved_type} app {app_name!r} in {space_name} is already "
                  f"{existing}; nothing to start")
            if wait:
                _wait_for_app(cli, domain_id, space_name, resolved_type, app_name,
                              {"InService", "Failed"}, timeout)
            return
        if existing == "Deleting":
            raise SmError(f"the previous app is still Deleting; wait for it to "
                          f"finish (`apps --include-dead`) and re-run")

        params = {"DomainId": domain_id, "SpaceName": space_name,
                  "AppType": resolved_type, "AppName": app_name}
        spec = {} if no_resource_spec else default_resource_spec(space_desc,
                                                                resolved_type)
        if instance_type:
            spec["InstanceType"] = instance_type
        if spec:
            params["ResourceSpec"] = spec

        if spec:
            source = "the space's DefaultResourceSpec" + (
                ", instance type overridden" if instance_type else "")
            spec_line = (" · ".join(f"{k} {v}" for k, v in spec.items())
                         + f"  ({source})")
        else:
            spec_line = "none sent — the service's own default applies"
        print(f"CreateApp · domain {domain_label(resolved)} · space {space_name}")
        print_labeled([
            ("AppType", resolved_type),
            ("AppName", app_name),
            ("ResourceSpec", spec_line),
        ])
        print()

        resp = cli.create_app(**params)
        print(f"AppArn {resp.get('AppArn')}")
        if wait:
            _wait_for_app(cli, domain_id, space_name, resolved_type, app_name,
                          {"InService", "Failed"}, timeout)
        else:
            print(f"Status: `awsut sagemaker studio apps --space {space_name}`")

    @studio.command(
        "url", help="Presigned URL into a space, as a user profile",
        params=_url_flags(),
    )
    @guard
    def _url(space, domain, user_profile, landing_uri, expires, session_duration,
             app_type, no_app_check):
        """Print the URL — the leaf for a link that goes to someone else.

        ``open`` is the same URL opened here; see :func:`mint_url` for what
        makes one.
        """
        url, target = mint_url(space, domain, user_profile, landing_uri, expires,
                               session_duration, app_type, no_app_check)
        print(_url_header(target, expires, session_duration))
        print(url)

    @studio.command(
        "open", help="Mint a presigned URL and open it in a browser",
        params=_url_flags(),
    )
    @guard
    def _open(space, domain, user_profile, landing_uri, expires, session_duration,
              app_type, no_app_check):
        """The same URL as ``url``, spent here instead of handed over.

        Its own leaf rather than ``url --open`` because that flag made the
        command's *subject* a flag: minting a link to paste into a chat and
        opening one's own workspace are two things to ask for, and opening is a
        whole word elsewhere in ``awsut`` (``awsut cloudformation open``,
        ``awsut console``).

        The URL is still printed.  ``--expires`` is short by design, and a
        browser that opens the wrong identity's window — the usual failure with
        a presigned URL — leaves the reader needing the link itself.
        """
        url, target = mint_url(space, domain, user_profile, landing_uri, expires,
                               session_duration, app_type, no_app_check)
        print(_url_header(target, expires, session_duration))
        print(url)
        if not webbrowser.open(url):
            raise SmError("no browser could be opened here — the URL above is "
                          f"good for {fmt_dur(expires)}, open it yourself")

    @studio.command(
        "stop", help="Stop a space's app — ends its compute charges",
        params=[
            _space_arg("space whose app to stop"),
            _domain_flag(),
            arg("--all", action="store_true", dest="stop_all",
                help="stop every live app in the domain (the end-of-day command)"),
            arg("-y", "--yes", action="store_true", help="skip confirmation"),
        ] + _app_flags() + _wait_flags("gone"),
    )
    @guard
    def _stop(space, domain, stop_all, yes, app_type, app_name, wait, timeout):
        """Deleting the app *is* how Studio stops one.

        The space's EBS volume and its contents survive, so relaunching returns
        to the same ``$HOME`` — which is why this is called ``stop`` and not
        ``delete``.  It is also what a domain teardown needs first: a domain
        with apps attached cannot be deleted.
        """
        cli = sm_client()
        require_operation(cli, "delete_app", "DeleteApp")
        resolved = resolve_domain(cli, domain)
        domain_id = resolved["DomainId"]

        if stop_all:
            if space:
                raise SmError("--all stops every app in the domain; drop the "
                              "SPACE argument (or drop --all)")
            targets = [(a.get("SpaceName"), a.get("AppType"), a.get("AppName"))
                       for a in list_apps(cli, domain_id)
                       if a.get("Status") not in APP_DEAD and a.get("SpaceName")]
            if not targets:
                print(f"no live app in domain {domain_label(resolved)}; "
                      "nothing to stop")
                return
        else:
            space_desc = resolve_space(cli, domain_id, space)
            space_name = space_desc.get("SpaceName")
            resolved_type = space_app_type(space_desc, app_type)
            status = app_status(cli, domain_id, space_name, resolved_type, app_name)
            if status is None:
                print(f"no {resolved_type} app {app_name!r} in {space_name}; "
                      "nothing to stop")
                return
            if status in APP_DEAD:
                print(f"app is already {status}; nothing to stop")
                return
            targets = [(space_name, resolved_type, app_name)]

        listing = ", ".join(f"{s}/{t}/{n}" for s, t, n in targets)
        if not yes:
            try:
                answer = passthrough_input(
                    f"Stop {len(targets)} app(s) in {domain_label(resolved)} "
                    f"[{listing}]? Files on the spaces' EBS volumes survive. "
                    "[y/N] : ")
            except RuntimeError:
                raise SmError("cannot confirm without a terminal — "
                              "re-run with -y/--yes")
            if answer.strip().lower() not in ("y", "yes"):
                print("not stopped")
                return

        for space_name, a_type, a_name in targets:
            try:
                cli.delete_app(DomainId=domain_id, SpaceName=space_name,
                               AppType=a_type, AppName=a_name)
                print(f"DeleteApp sent for {space_name}/{a_type}/{a_name}")
            except botocore.exceptions.ClientError as exc:
                # One unstoppable app must not strand the rest — this is the
                # command that ends the day's charges.
                print(f"error: {space_name}/{a_type}/{a_name}: "
                      f"{api_message(exc)}", file=sys.stderr)
        if wait:
            for space_name, a_type, a_name in targets:
                _wait_for_app(cli, domain_id, space_name, a_type, a_name,
                              {"Deleted", "Failed"}, timeout, gone_is_done=True)


# ─── minting a presigned URL ────────────────────────────────────────────────

def mint_url(space, domain, user_profile, landing_uri, expires, session_duration,
             app_type, no_app_check) -> tuple[str, str]:
    """One CreatePresignedDomainUrl call — a URL a person without an AWS
    identity can use — and a label saying what it lands on.

    Two members are what make it land *inside a space*: ``SpaceName`` and
    ``LandingUri``.  Both are optional to the API and recent enough that an old
    botocore lacks them, which is why they are checked against the model rather
    than sent hopefully.

    Because the default landing URI aims at the space's *app*, the app has to
    exist for the URL to be worth minting at all — see
    :func:`_require_live_app`.

    Shared by ``url`` and ``open`` so that the two cannot drift into producing
    different links from the same flags; each caller only decides what to do
    with the one it gets.
    """
    cli = sm_client()
    require_operation(cli, "create_presigned_domain_url",
                      "CreatePresignedDomainUrl")
    resolved = resolve_domain(cli, domain)
    domain_id = resolved["DomainId"]

    params = {"DomainId": domain_id,
              "ExpiresInSeconds": expires,
              "SessionExpirationDurationInSeconds": session_duration}
    target = f"domain {domain_label(resolved)}"

    if space is None and user_profile:
        # An explicit profile with no space: the domain's Studio home.
        params["UserProfileName"] = user_profile
    else:
        space_desc = resolve_space(cli, domain_id, space)
        space_name = space_desc.get("SpaceName")
        profile = user_profile or space_owner(space_desc)
        if not profile:
            raise SmError(f"space {space_name} records no owner profile; "
                          "pass --user-profile")
        require_input_member("CreatePresignedDomainUrl", "SpaceName",
                             "a URL cannot be pointed at a space")
        params["UserProfileName"] = profile
        params["SpaceName"] = space_name
        resolved_type = space_app_type(space_desc, app_type)
        if not landing_uri and not no_app_check:
            # Only for the default landing URI: an explicit --landing-uri is
            # the caller aiming somewhere of their own choosing, which may
            # legitimately not be an app.
            _require_live_app(cli, domain_id, space_name, resolved_type)
        uri = landing_uri or f"app:{resolved_type}:"
        if uri:
            require_input_member("CreatePresignedDomainUrl", "LandingUri",
                                 "the URL cannot land on an app")
            params["LandingUri"] = uri
        target = f"space {space_name} as {profile}"

    if landing_uri and "LandingUri" not in params:
        require_input_member("CreatePresignedDomainUrl", "LandingUri",
                             "--landing-uri cannot be honoured")
        params["LandingUri"] = landing_uri

    return cli.create_presigned_domain_url(**params).get("AuthorizedUrl"), target


def _url_header(target: str, expires: int, session_duration: int) -> str:
    """What the link is for and how long it lasts — both leaves print this."""
    return (f"{target} · valid {fmt_dur(expires)} · "
            f"session {fmt_dur(session_duration)}")


# ─── the app a URL lands on ─────────────────────────────────────────────────

def _require_live_app(cli, domain_id: str, space: str, app_type: str) -> None:
    """Refuse to mint a URL that would land on an app which is not there.

    An error rather than a warning, because of who the URL is *for*.  A
    presigned URL exists to be handed to someone without an AWS identity, and
    it stays valid for minutes; if it opens on a stopped app, the failure
    surfaces in their browser, indistinguishable from an expired link, with
    nothing they can do about it.  The person running this command is the only
    one in the exchange who can fix it — by starting the app first — so this is
    the moment to say so.

    ``Pending`` passes with a note: the app is on its way to ``InService``, and
    minting the link while it boots is the normal way to have it ready.
    """
    try:
        live = live_apps_in_space(cli, domain_id, space, app_type)
    except botocore.exceptions.ClientError as exc:
        # No ListApps permission is not evidence that nothing is running, and
        # CreatePresignedDomainUrl may well be within reach — say so, go on.
        print(f"warning: could not check for a running app: {api_message(exc)}",
              file=sys.stderr)
        return

    if not live:
        raise SmError(
            f"no live {app_type} app in {space}, so this URL would open on "
            f"nothing. Start one first:\n"
            f"  awsut sagemaker studio start {space} --wait\n"
            f"or pass --no-app-check to mint it anyway (Studio's own space page "
            f"can start the app), or --landing-uri to aim elsewhere")

    if all(a.get("Status") != "InService" for a in live):
        states = ", ".join(sorted({a.get("Status") or "?" for a in live}))
        print(f"note: the {app_type} app is {states}, not InService yet — the "
              f"URL will work once it is", file=sys.stderr)


# ─── waiting on an app ──────────────────────────────────────────────────────

def _wait_for_app(cli, domain_id, space, app_type, app_name, until, timeout,
                  gone_is_done=False):
    """Poll DescribeApp until the status is in *until*, printing each change.

    Prints on change only, with a heartbeat between polls, so a five-minute
    launch leaves a readable timeline instead of a repainted frame — the same
    shape as ``jobs watch``.  ``Ctrl+C`` detaches without touching the app.
    """
    beat = Heartbeat()
    seen = None
    deadline = time.monotonic() + timeout if timeout else None
    print(f"waiting for {space}/{app_type}/{app_name} · poll 10s · "
          f"heartbeat {DOT_INTERVAL}s · Ctrl+C to detach")

    while True:
        stamp = datetime.now().strftime("%H:%M:%S")
        try:
            status = app_status(cli, domain_id, space, app_type, app_name)
        except botocore.exceptions.ClientError as exc:
            beat.clear()
            print(f"error: poll failed: {api_message(exc)}", file=sys.stderr)
            status = seen
        if status is None:
            # An app the service no longer has a record of at all: for a stop
            # that is success, for a launch it is something to keep watching.
            # Named rather than left as None so it takes part in change
            # detection — otherwise it would print once per poll.
            status = "(no such app)"
            if gone_is_done:
                beat.clear()
                print(f"[{stamp}] {status}")
                return
        if status != seen:
            seen = status
            beat.clear()
            print(f"[{stamp}] {status}")
            if status in until:
                if status == "Failed":
                    reason = _failure_reason(cli, domain_id, space, app_type,
                                             app_name)
                    if reason:
                        print(f"FailureReason: {reason}")
                return

        if deadline and time.monotonic() > deadline:
            beat.clear()
            print(f"[{stamp}] gave up after {fmt_dur(timeout)}; app is {seen}")
            return
        try:
            sleep_with_dots(10, beat)
        except KeyboardInterrupt:
            beat.clear()
            print(f"\ndetached; app is {seen}")
            return


def _failure_reason(cli, domain_id, space, app_type, app_name) -> str:
    try:
        return describe_app(cli, domain_id, space, app_type,
                            app_name).get("FailureReason") or ""
    except botocore.exceptions.ClientError:
        return ""


# ─── watching a whole domain ────────────────────────────────────────────────

def _watch_studio(cli, resolved, space, interval, timeout, follow, limit):
    """Poll a domain's spaces and apps, logging each change until settled.

    Two list calls per tick — ListSpaces and ListApps — rather than DescribeApp
    per app, so the cost is O(1) in the size of the domain and a resource that
    appears after the watch starts is picked up like any other change.  Prints
    on change only, with a heartbeat between polls, so a five-minute launch
    leaves a readable timeline instead of a repainted frame.

    The two tracks are polled and reported independently: ListSpaces and
    ListApps are separate permissions, and a tick that could not read one must
    not read its own silence as "every space was deleted".
    """
    domain_id = resolved["DomainId"]
    beat = Heartbeat()
    scope = f"domain {domain_label(resolved)}"
    if space:
        scope += f" · space {space}"
    print(f"watching Studio · {scope} · region {region_label()}")
    print(f"  poll {interval}s · heartbeat {DOT_INTERVAL}s · "
          f"{'follow' if follow else 'until settled'} · max {limit} spaces/apps")
    print("  Ctrl+C to detach\n")

    known: dict[tuple[str, str], str] = {}   # (kind, label) -> last status shown
    quiet: dict[str, str] = {}               # app label -> the history it holds
    polling = {"space", "app"}
    first = True
    deadline = time.monotonic() + timeout if timeout else None

    while True:
        stamp = datetime.now().strftime("%H:%M:%S")
        baseline = first

        if "space" in polling:
            seen, records, keep = _poll_spaces(cli, domain_id, space, limit, beat)
            if seen is not None:
                _report_track(
                    "space", seen, records, known, baseline=baseline,
                    stamp=stamp, beat=beat, detail=_space_detail,
                    reason=lambda s: _space_failure_reason(cli, domain_id, s))
            if not keep:
                _drop_track("space", known, polling)

        if "app" in polling:
            seen, records, keep = _poll_apps(
                cli, domain_id, space, limit, baseline,
                {label for kind, label in known if kind == "app"}, quiet, beat)
            if seen is not None:
                _report_track(
                    "app", seen, records, known, baseline=baseline,
                    stamp=stamp, beat=beat, detail=instance_of,
                    reason=lambda a: _failure_reason(
                        cli, domain_id, a.get("SpaceName"), a.get("AppType"),
                        a.get("AppName")))
            if not keep:
                _drop_track("app", known, polling)

        if not polling:
            beat.clear()
            print(f"[{stamp}] nothing left to poll; stopping")
            return

        if first:
            beat.clear()
            if not known:
                print(f"[{stamp}] nothing to watch here yet")
            print()
            first = False

        moving = _moving(known)
        if not moving and not follow:
            beat.clear()
            if baseline:
                print(f"[{stamp}] nothing is starting or stopping; "
                      "-f/--follow waits for something to")
            else:
                print(f"[{stamp}] settled — nothing is starting or stopping "
                      "any more")
            return

        if deadline and time.monotonic() > deadline:
            beat.clear()
            print(f"[{stamp}] timeout after {fmt_dur(timeout)}; still moving: "
                  + (", ".join(moving) or "nothing"))
            return

        try:
            sleep_with_dots(interval, beat)
        except KeyboardInterrupt:
            beat.clear()
            print(f"\ndetached; {len(moving)} resource(s) still moving"
                  + (": " + ", ".join(moving) if moving else ""))
            return


def _moving(known) -> list[str]:
    """The tracked resources still on their way somewhere, as ``kind label``."""
    return sorted(f"{kind} {label}" for (kind, label), status in known.items()
                  if status in TRANSIENT[kind])


def _drop_track(kind, known, polling) -> None:
    """Stop polling one kind, and forget what it had — a track that can no
    longer be read must not leave a resource pinned as "still moving" forever,
    which would keep the watch from ever settling."""
    polling.discard(kind)
    for key in [k for k in list(known) if k[0] == kind]:
        known.pop(key)


def _poll_spaces(cli, domain_id, space, limit, beat):
    """``({label: status}, {label: summary}, keep_polling)`` for the spaces."""
    try:
        spaces = list_spaces(cli, domain_id, limit)
    except botocore.exceptions.ClientError as exc:
        return None, None, _survives(exc, "spaces not listed", beat)
    wanted = [s for s in spaces if s.get("SpaceName")
              and (space is None or s["SpaceName"] == space)]
    return ({s["SpaceName"]: s.get("Status") or "?" for s in wanted},
            {s["SpaceName"]: s for s in wanted},
            True)


def _poll_apps(cli, domain_id, space, limit, baseline, tracked, quiet, beat):
    """``({label: status}, {label: summary}, keep_polling)`` for the apps.

    Labelled ``space/type/name`` — the triple DeleteApp takes, and the only
    thing that identifies an app, since a space can run more than one.

    *quiet* is this function's memory of the rows it filed away as history (see
    :func:`_stays_quiet`) and is rewritten each call, so a filed row that later
    moves is handed back as news and one that drops out of the listing is
    forgotten without a word.
    """
    try:
        apps = list_apps(cli, domain_id, space, limit)
    except botocore.exceptions.ClientError as exc:
        return None, None, _survives(exc, "apps not listed", beat)
    seen, records, filed = {}, {}, {}
    for a in apps:
        label = (f"{a.get('SpaceName') or '-'}/{a.get('AppType') or '?'}"
                 f"/{a.get('AppName') or '?'}")
        status = a.get("Status") or "?"
        if _stays_quiet(label, status, tracked, quiet, baseline):
            filed[label] = status
            continue
        seen[label] = status
        records[label] = a
    quiet.clear()
    quiet.update(filed)
    return seen, records, True


def _stays_quiet(label, status, tracked, quiet, baseline) -> bool:
    """Is this app row history to be filed away rather than reported?

    ListApps keeps returning an app long after it is gone, so a domain that has
    launched a hundred apps answers with a hundred rows — and announcing them
    would bury the one row that is actually moving.  A ``Deleted`` app the watch
    has never seen alive is never news; a ``Failed`` one is news only if it
    failed *during* the watch, which at baseline it cannot have.  ``Deleting``
    is in flight and is exactly what someone who just ran ``stop`` is watching
    for, so it is never quiet.

    Two things make this a filing rather than a skip.  An app already being
    followed is never quiet, because one followed into ``Deleting`` has to be
    allowed to reach ``Deleted`` — that transition is the whole answer to "did
    the stop finish".  And a row filed at baseline stays filed only while its
    status does not move: an app name is reused (a space's app is always
    ``default``), so yesterday's ``Deleted`` row is where a fresh ``Pending``
    shows up, and re-filing it on status alone would hide the very launch the
    watch was started for.
    """
    if label in tracked:
        return False
    if label in quiet:
        return quiet[label] == status
    if status == "Deleted":
        return True
    return baseline and status == "Failed"


def _survives(exc, what: str, beat) -> bool:
    """Report a failed poll; say whether the track is worth polling again.

    A denial is the same denial every tick and would spam the timeline under
    every dot, so that track is dropped after saying so once.  Anything else
    may be throttling or a blip, which is worth another try.
    """
    beat.clear()
    print(f"error: {what}: {api_message(exc)}", file=sys.stderr)
    if error_code(exc) in ("AccessDeniedException", "UnrecognizedClientException"):
        print(f"       not polling {what.split()[0]} again", file=sys.stderr)
        return False
    return True


def _space_detail(space_summary: dict) -> str:
    """What a space is, for the line announcing it — sharing and app type."""
    sharing = sharing_type(space_summary)
    app_type = (space_summary.get("SpaceSettingsSummary") or {}).get("AppType")
    return " ".join(p for p in ("" if sharing == "-" else sharing,
                                app_type or "") if p)


def _space_failure_reason(cli, domain_id, space_summary) -> str:
    try:
        return describe_space(
            cli, domain_id, space_summary["SpaceName"]).get("FailureReason") or ""
    except (botocore.exceptions.ClientError, SmError):
        return ""


def _mark(status: str) -> str:
    """One glyph per state, so the timeline scans without reading the words."""
    if status == "InService":
        return "✓"
    if status.endswith("Failed"):        # Failed / Update_Failed / Delete_Failed
        return "✗"
    if status == "Deleted":
        return "·"                       # gone — which for a stop is success
    return "…"


def _report_track(kind, seen, records, known, *, baseline, stamp, beat,
                  detail, reason):
    """Log one kind's appearances, status changes and departures.

    A leading ``·`` marks the baseline pass, ``+`` a resource that showed up
    later, ``-`` one that stopped being listed, and an indented status change
    the rest.  Any transition into a failed state pulls the reason with a
    Describe call — a bare ``Failed`` on the timeline is the one line nobody can
    act on.
    """
    for label, status in sorted(seen.items()):
        prev = known.get((kind, label))
        if prev == status:
            continue
        known[(kind, label)] = status
        beat.clear()
        if prev is None:
            extra = detail(records[label])
            print(f"[{stamp}] {'·' if baseline else '+'} {_mark(status)} "
                  f"{kind} {label}  {status}" + (f"  {extra}" if extra else ""))
        else:
            print(f"[{stamp}]   {_mark(status)} {kind} {label}  "
                  f"{prev} → {status}")
        if status.endswith("Failed"):
            for line in (reason(records[label]) or "").splitlines():
                print(f"           {line}")

    for label in [lbl for k, lbl in list(known) if k == kind and lbl not in seen]:
        status = known.pop((kind, label))
        beat.clear()
        print(f"[{stamp}] - {kind} {label}  no longer listed (was {status})")


# ─── reading the boot log ───────────────────────────────────────────────────

def _list_space_streams(log_group, prefix, space_name) -> None:
    rows = stream_table_rows(log_streams(log_group, prefix), prefix)
    print_header(f"{len(rows)} stream(s)", f"{log_group}/{prefix}")
    print_table(["STREAM (below the space)", "FIRST EVENT", "LAST EVENT"],
                rows,
                note=(None if rows else
                      f"nothing has ever written under {prefix} — no app has "
                      f"started in space {space_name}"))


def _read_stream(log_group, stream, follow, lookback) -> None:
    """Print a space app's stream, explaining an absent one in Studio's terms.

    A lifecycle config attached at domain level does not fire for every space,
    so "nothing here" is a real result rather than a failure.
    """
    def missing():
        print("no such log group or stream — nothing has written to it: "
              "either no app has started here, or this app runs no "
              "lifecycle config")
        print("what does exist: awsut sagemaker studio log --list")

    read_log_stream(log_group, stream, follow=follow, lookback=lookback,
                    on_missing=missing)
