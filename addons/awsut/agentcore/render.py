"""Shared instruments for the ``awsut bedrock-agentcore`` tree.

Everything in here is plumbing or vocabulary that the AgentCore groups need and
that is not specific to any one of them: the control-plane client, the status
words every AgentCore resource shares, and the cache-key shape its completers
use.  It lives in one module so the groups cannot drift in how they read the
same service.

The output contract — table shape, header line, note policy, ``error:`` lines —
and the service-agnostic instruments (pagination, the YAML-ish JSON renderer,
the watch heartbeat, model introspection, the completion-context flag readers)
are two levels up in :mod:`eosh_addons.awsut.common`, and re-exported here
so a module in this package reads every instrument off ``render``.  That is the
same arrangement ``sagemaker.render`` uses.

Two things are worth knowing before adding a group here:

* **The control plane and the data plane are different services**, and a
  resource can span both.  ``bedrock-agentcore-control`` creates and describes
  resources (harnesses, memories); ``bedrock-agentcore`` reaches *into* them —
  what a memory has stored, what a runtime answers.  So a group may need both
  clients: ``memory`` uses :func:`control_client` for the resource itself and
  :func:`data_client` for the actors, sessions, events and records inside it.
  :func:`require_operation` reads the service name off whichever client it is
  handed, so an error message always names the right one.
* **AgentCore's model is young and still growing.**  An operation this code
  calls may simply not exist in the installed botocore, so every leaf goes
  through :func:`require_operation` first — a missing operation is a
  botocore-vintage problem, and a bare ``AttributeError`` would read like a
  eosh bug.  For the same reason status words are read out of the loaded
  model where possible (:func:`statuses`) rather than hard-coded, and nothing
  validates a user-typed status against them.

Region and profile come from the enclosing ``awsut`` recipe, i.e. from
``var aws_region=`` / ``var aws_profile=`` at the prompt — these commands
deliberately have no ``--region`` / ``--profile`` flags.  What this group adds
is ``var agentcore_control_endpoint=`` and ``var agentcore_data_endpoint=``, for
pointing either plane at a non-default endpoint; like the ``sagemaker_*`` Vars
they are module-level Python state rather than env vars, so they don't leak into
subprocesses.
"""

from __future__ import annotations

import functools

import boto3
import botocore
import botocore.session

from eosh.completion_cache import aws_env_key
from eosh.variables import PyVar
from .. import cli as awsut
from ..common import (  # noqa: F401  (re-exported for this package)
    DOT_INTERVAL,
    NOT_FOUND_CODES,
    Heartbeat,
    SmError,
    api_message,
    error_code,
    fmt_dur,
    fmt_time,
    guard,
    input_members,
    model_enum,
    paged,
    positionals,
    print_header,
    print_labeled,
    print_table,
    section,
    sleep_with_dots,
    split_arn,
    version_key,
    yaml_lines,
)

#: The control-plane service — creates, updates, describes, deletes.
CONTROL_SERVICE = "bedrock-agentcore-control"

#: The data-plane service — reaches into a resource: what a memory has stored,
#: what a runtime answers.
DATA_SERVICE = "bedrock-agentcore"

# Set via ``var agentcore_control_endpoint=`` / ``var agentcore_data_endpoint=``
# at the prompt.  Module-level Python state (not env vars) so they don't leak
# into subprocesses.
control_endpoint: str = ""
data_endpoint: str = ""


# ─── AWS plumbing ───────────────────────────────────────────────────────────

def control_client(region_name: str | None = None):
    """The AgentCore control-plane client, honouring the endpoint setting."""
    if region_name is None:
        region_name = awsut._get_region()
    return boto3.Session().client(
        CONTROL_SERVICE,
        region_name=region_name,
        endpoint_url=control_endpoint or None,
    )


def data_client(region_name: str | None = None):
    """The AgentCore data-plane client, honouring the endpoint setting.

    A separate service from :func:`control_client`, not a separate endpoint of
    one: the two have different operation sets, so a leaf that lists a memory
    and then lists what is *inside* it holds both clients at once.
    """
    if region_name is None:
        region_name = awsut._get_region()
    return boto3.Session().client(
        DATA_SERVICE,
        region_name=region_name,
        endpoint_url=data_endpoint or None,
    )


def region_label() -> str:
    return awsut._region_label()


def service_of(client) -> str:
    """The service name a boto3 client speaks, for an error message."""
    try:
        return client.meta.service_model.service_name
    except AttributeError:
        return CONTROL_SERVICE


def require_operation(client, method: str, api: str) -> None:
    """Fail with an explanation when the loaded model lacks *api*.

    AgentCore ships operations faster than a pinned botocore picks them up, and
    a bare ``AttributeError`` on ``client.list_harnesses`` would read like a
    eosh bug rather than a model-vintage one.  The installed version is in
    the message because upgrading it is the fix.

    The service is read off the *client* rather than passed in, so a data-plane
    call's message names the data-plane service without every call site having
    to remember which plane it is on.
    """
    if not hasattr(client, method):
        raise SmError(
            f"the loaded model for {service_of(client)!r} has no {api} operation "
            f"— upgrade botocore (installed: {botocore.__version__})"
        )


def require_input_member(operation: str, member: str, why: str,
                         service: str = CONTROL_SERVICE) -> None:
    """Fail with an explanation when the loaded model's *operation* lacks *member*.

    botocore rejects an undeclared parameter before the request goes out, with a
    ``ParamValidationError`` that reads like a caller bug.  A flag that maps onto
    a recent input member asks here first.
    """
    members = input_members(service, operation)
    if members and member not in members:
        raise SmError(
            f"the loaded model for {service!r} has no {member} input on "
            f"{operation}, so {why} — upgrade botocore (installed: "
            f"{botocore.__version__})"
        )


def cache_key(*parts) -> tuple:
    """A completion-cache key scoped to the profile, region and both endpoints.

    The endpoints are part of the key for the same reason the profile is:
    pointing ``var agentcore_control_endpoint=`` somewhere else means a different
    set of resources, and a cached listing from the previous one would be wrong
    rather than merely stale.
    """
    return ("awsut.agentcore", aws_env_key(), control_endpoint, data_endpoint,
            *parts)


# ─── status vocabulary ──────────────────────────────────────────────────────
#
# AgentCore resources do not all share one word set — a harness settles on
# ``READY`` and fails as ``CREATE_FAILED``, a memory settles on ``ACTIVE`` and
# fails as plain ``FAILED`` — but they all report *a* status and the question
# asked of it is always the same three-way one.  So the marks and the
# terminal/in-flight split are the union of the vocabularies rather than one per
# group: a word this code has never seen simply reads as "still moving", which
# is the safe reading for a watcher.

#: Used when the loaded model can't be read.  The model is the source of truth
#: (see :func:`statuses`); this is only so a completion list is never empty.
STATUS_FALLBACK = ("CREATING", "CREATE_FAILED", "UPDATING", "UPDATE_FAILED",
                   "READY", "DELETING", "DELETE_FAILED")

TERMINAL_OK = {"READY", "ACTIVE"}
TERMINAL_BAD = {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED", "FAILED"}

#: A status that will not change on its own.  ``DELETING`` is deliberately not
#: here: a successful delete ends with the resource *absent*, not with a status,
#: so a watcher has to treat "gone" as the terminal state instead.
TERMINAL = TERMINAL_OK | TERMINAL_BAD


@functools.lru_cache(maxsize=None)
def statuses(operation: str, list_key: str,
             service: str = CONTROL_SERVICE) -> tuple[str, ...]:
    """A resource's status enum, read out of the loaded model. Offline.

    Read from the *output* shape of a list operation, which is where these
    services declare it — ``common.model_enum`` reads input members and
    so cannot reach it.  A refreshed botocore contributes new status words
    without a code change here, and a model too old to have the operation at all
    falls back to :data:`STATUS_FALLBACK` rather than offering nothing.
    """
    try:
        model = botocore.session.Session().get_service_model(service)
        shape = model.operation_model(operation).output_shape
        found = shape.members[list_key].member.members["status"].enum
        return tuple(found) or STATUS_FALLBACK
    except Exception:
        return STATUS_FALLBACK


def mark_for(status: str) -> str:
    """``✓`` / ``✗`` / ``…`` — settled well, settled badly, still moving."""
    return "✓" if status in TERMINAL_OK else ("✗" if status in TERMINAL_BAD else "…")


# ─── detail views ───────────────────────────────────────────────────────────

#: Printed as its own section instead of a labeled row: it is prose, often
#: several lines of it, and a value that long does not belong in a padded column.
REASON = "failureReason"

#: Members rendered through :func:`fmt_time` by :func:`render_detail`.  Named
#: rather than sniffed, because a member's *name* is the only thing here that
#: says a value is a timestamp.
TIMESTAMPS = ("createdAt", "updatedAt")


def scalar_text(key, value, timestamps=TIMESTAMPS) -> str:
    if key in timestamps:
        return fmt_time(value)
    return "" if value is None else str(value)


def render_detail(obj, layout, *, timestamps=TIMESTAMPS) -> None:
    """Print a ``Get*`` response body — all of it, scalars then structures.

    Member-name-blind by design: *layout* only fixes the reading order of the
    members worth leading with (with ``None`` for a group break), and everything
    else in the response follows.  A member a newer service model grows therefore
    still prints, rather than being silently dropped by a curated list — which
    matters more here than usual, since AgentCore's model is still growing.

    Structural members each get a :func:`section` and are rendered as indented
    YAML-ish lines; the empty ones collapse into a single trailing line, because
    four empty sections would bury the ones that have content.
    """
    laid_out = {k for k in layout if k} | {REASON}
    pairs = [None if key is None
             else (key, scalar_text(key, obj.get(key), timestamps))
             for key in layout]

    extra = [(k, scalar_text(k, v, timestamps)) for k, v in obj.items()
             if k not in laid_out and not isinstance(v, (dict, list))]
    if extra:
        pairs.append(None)
        pairs.extend(extra)
    print_labeled(pairs)

    if obj.get(REASON):
        print()
        section(REASON)
        print(obj[REASON])

    empty = []
    for key, value in obj.items():
        if not isinstance(value, (dict, list)):
            continue
        if not value:
            empty.append(key)
            continue
        print()
        section(key)
        for line in yaml_lines(value):
            print(line)
    if empty:
        print(f"\nempty: {', '.join(empty)}")


def print_reasons(rows, name_of) -> None:
    """Failure reasons below a table, one section per row that has one.

    Out of the table because a reason is prose of arbitrary length, and a column
    that wide would push every other column off the screen.
    """
    for row in rows:
        if row.get(REASON):
            print()
            section(f"{name_of(row)} · {REASON}")
            print(row[REASON])


# ─── per-context Python-side variables (`var NAME=…`) ───────────────────────

class ControlEndpointVar(PyVar):
    name = "agentcore_control_endpoint"
    description = (f"{CONTROL_SERVICE} endpoint URL (blank = AWS default)")

    def get(self) -> str | None:
        return control_endpoint or None

    def set(self, value: str) -> None:
        global control_endpoint
        control_endpoint = value

    def unset(self) -> None:
        global control_endpoint
        control_endpoint = ""


class DataEndpointVar(PyVar):
    name = "agentcore_data_endpoint"
    description = (f"{DATA_SERVICE} endpoint URL (blank = AWS default)")

    def get(self) -> str | None:
        return data_endpoint or None

    def set(self, value: str) -> None:
        global data_endpoint
        data_endpoint = value

    def unset(self) -> None:
        global data_endpoint
        data_endpoint = ""
