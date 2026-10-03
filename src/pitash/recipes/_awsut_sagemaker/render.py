"""Shared instruments for the ``awsut sagemaker`` tree.

Everything in here is presentation or plumbing that the ``jobs``, ``hub``,
``studio`` and ``hyperpod`` groups need.  It lives in one module so they cannot
drift in how they render the same data — several of these APIs hand back opaque
JSON documents, and all of them are read by following ARNs out of them.

The output contract itself — table shape, header line, note policy, ``error:``
lines — is one level up, in :mod:`pitash.recipes._awsut_common`, because the
rest of ``awsut`` (``ec2``, ``logs``, ``cloudformation``, and the
``bedrock-agentcore`` group) prints to the same contract.  So are the
instruments that are about AWS shapes rather than about SageMaker: model
introspection, token pagination, the YAML-ish JSON renderer, ARN/S3 splitting,
the watch heartbeat, and the completion-context flag readers.  All of those
names are re-exported here, so a module in this package still reads every
instrument it needs off ``render``.

What this module adds on top is SageMaker-specific:

* **Documents are reformatted, never interpreted.**  ``JobConfigDocument`` and
  ``HubContentDocument`` are modelled by the service as opaque strings.
  Nothing here knows a single key name: structure is followed by *value
  shape* (an ARN says what it points at, an ``s3://`` URI says it is a
  location) and the JSON path a value was read from travels with it as
  provenance.
* **Clients and CloudWatch reading**, plus the ``require_*`` guards that turn a
  model too old for an operation into an explanation rather than an
  ``AttributeError``.

Region, profile, endpoint and service-name all come from the enclosing
``awsut`` recipe, i.e. from ``var aws_region=`` / ``var aws_profile=`` /
``var sagemaker_endpoint=`` / ``var sagemaker_service_name=`` at the prompt —
these commands deliberately have no ``--region`` / ``--profile`` flags.
"""

from __future__ import annotations

import sys
import time

import botocore.exceptions

from .. import awsut
from .._awsut_common import (  # noqa: F401  (re-exported for this package)
    DOT_INTERVAL,
    NOT_FOUND_CODES,
    RED,
    RESET,
    YELLOW,
    Heartbeat,
    SmError,
    age,
    api_message,
    elapsed_of,
    error_code,
    flag_value,
    fmt_bytes,
    fmt_dur,
    fmt_time,
    guard,
    input_members,
    model_enum,
    ms_time,
    paged,
    parse_doc,
    parse_since,
    positionals,
    print_header,
    print_labeled,
    print_table,
    section,
    sleep_with_dots,
    split_arn,
    split_s3,
    version_key,
    yaml_lines,
    yaml_scalar,
)

# TAB candidates for the ``--instance-type`` flags.  A completion list, not a
# validation list — the service accepts far more than this, so nothing checks
# a typed value against it.  Shared because ``studio start`` and ``hyperpod``
# offer the same flag and should offer the same shortlist.
INSTANCE_TYPE_CHOICES = [
    "ml.trn1.32xlarge", "ml.p5.48xlarge", "ml.p5e.48xlarge",
    "ml.p5en.48xlarge", "ml.p4d.24xlarge", "ml.t3.xlarge",
    "ml.trn2.48xlarge", "ml.c4.large", "ml.c6i.large",
    "ml.t3.2xlarge", "ml.t3.large", "ml.c7g.medium",
]


# ─── AWS plumbing ───────────────────────────────────────────────────────────

def sm_client(region_name: str | None = None):
    """The SageMaker client, honouring the ``sagemaker_*`` Vars."""
    return awsut._get_sagemaker_client(region_name=region_name)


def s3_client():
    return awsut._get_boto3_client("s3")


def logs_client():
    return awsut._get_boto3_client("logs")


def region_label() -> str:
    return awsut._region_label()


def require_input_member(operation: str, member: str, why: str) -> None:
    """Fail with an explanation when the loaded model's *operation* lacks *member*."""
    members = input_members(awsut.sagemaker_service_name, operation)
    if members and member not in members:
        raise SmError(
            f"the loaded model for service {awsut.sagemaker_service_name!r} has no "
            f"{member} input on {operation}, so {why} — upgrade botocore, or point "
            f"`var sagemaker_service_name=` at a model that has it"
        )


def require_operation(client, method: str, api: str) -> None:
    """Fail with an explanation when the loaded model lacks *api*.

    The Job APIs in particular are not in every botocore release, and a bare
    ``AttributeError`` on ``client.list_jobs`` would read like a pitash bug
    rather than a model-vintage problem.
    """
    if not hasattr(client, method):
        raise SmError(
            f"the loaded model for service {awsut.sagemaker_service_name!r} has no "
            f"{api} operation — upgrade botocore, or point "
            f"`var sagemaker_service_name=` at a model that has it"
        )


# ─── CloudWatch Logs ────────────────────────────────────────────────────────
#
# Shared because two groups read logs the same way and should read them the
# same way: ``studio log`` reads a space's boot log, ``jobs log`` a job's run
# log.  Only the group/stream naming and the explanation for an absent stream
# differ, and those are the callers' business — everything below is naming-blind.

def log_streams(log_group, prefix="", *, max_items=200):
    """Streams under *prefix* in *log_group*, most recent activity first.

    Sorted here rather than by the API because ``describe_log_streams`` rejects
    ``orderBy`` together with ``logStreamNamePrefix`` — a prefixed listing comes
    back in *name* order, which for a timestamped stream name is only
    accidentally chronological.

    A log group that does not exist yet comes back as an empty list rather than
    an error: nothing having ever written to it is a real answer, and only the
    caller knows what it means (no app has started here; the job has not
    reached a phase that logs).
    """
    client = logs_client()
    params = {"logGroupName": log_group}
    if prefix:
        params["logStreamNamePrefix"] = prefix
    try:
        streams = paged(client.describe_log_streams, "logStreams", max_items,
                        token_param="nextToken", token_key="nextToken", **params)
    except client.exceptions.ResourceNotFoundException:
        return []
    except botocore.exceptions.ClientError as exc:
        raise SmError(f"{log_group}: {api_message(exc)}")
    streams.sort(key=lambda s: s.get("lastEventTimestamp") or 0, reverse=True)
    return streams


def stream_table_rows(streams, prefix):
    """``[[name_below_prefix, first, last]]`` for a stream listing.

    Names are printed relative to *prefix* because that is the part a
    ``--stream`` flag takes — the rest of the path is derived by the caller.

    No size column: ``describe_log_streams`` has reported ``storedBytes`` as a
    flat zero for every stream since 2019, so a size here could only ever be a
    column of ``0B``.
    """
    return [[
        s["logStreamName"][len(prefix):],
        fmt_time(ms_time(s.get("firstEventTimestamp"))),
        fmt_time(ms_time(s.get("lastEventTimestamp"))),
    ] for s in streams]


def read_log_stream(log_group, stream, *, follow=False, lookback=0,
                    on_missing=None):
    """Print a stream's events, optionally tailing it.

    An absent stream is the *expected* answer often enough to deserve an
    explanation rather than an error, and what that explanation is depends on
    the resource — so *on_missing* is the caller's to supply.
    """
    client = logs_client()
    params = {"logGroupName": log_group, "logStreamName": stream,
              "startFromHead": True, "limit": 1000}
    if lookback:
        params["startTime"] = int((time.time() - lookback * 60) * 1000)

    token = None
    printed = 0
    try:
        while True:
            if token:
                params["nextToken"] = token
                params.pop("startTime", None)
            try:
                resp = client.get_log_events(**params)
            except client.exceptions.ResourceNotFoundException:
                if on_missing:
                    on_missing()
                else:
                    print(f"no such log group or stream: {log_group} {stream}")
                return
            for event in resp["events"]:
                print(event["message"].replace("\0", "\\0").rstrip("\n"))
                printed += 1
            # Flush per pass so `... | grep ERROR` sees output as it arrives.
            sys.stdout.flush()
            if not follow:
                if not printed:
                    print("stream exists but has no events"
                          + (f" in the last {lookback} minute(s)" if lookback
                             else ""))
                return
            caught_up = resp["nextForwardToken"] == token
            token = resp["nextForwardToken"]
            if caught_up:
                # Short sleeps with a flush each tick: in a pipeline, Ctrl+C
                # closes our stdout and the next flush raises promptly rather
                # than after the whole wait.
                for _ in range(50):
                    time.sleep(0.1)
                    sys.stdout.flush()
    except (KeyboardInterrupt, BrokenPipeError):
        pass


# ─── opaque JSON documents ──────────────────────────────────────────────────

def show_document(label, doc, schema_version, raw=False, indent=0):
    """Print an opaque JSON-string document, reformatted (or verbatim)."""
    # Which of the two forms this is *does* vary per run (``--raw*`` picks), so
    # it belongs in the heading.  *Why* a reformat exists — the API models these
    # as one JSON string — does not, and lives in the flag's help instead.
    note = "verbatim" if raw else "reformatted"
    pad = "  " * indent
    print()
    if indent:
        # Nested inside ``trace``'s indented tree, where a section rule starting
        # at column zero would read as the end of the tree rather than a node
        # in it — so the label carries the caller's own padding instead.
        print(f"{label}  (schema {schema_version or '?'}; {note})")
    else:
        section(f"{label} · schema {schema_version or '?'} · {note}")
    if not doc:
        print(f"{pad}none returned")
        return
    if raw:
        for line in str(doc).splitlines():
            print(f"{pad}{line}")
        return
    parsed = parse_doc(doc)
    if parsed is None:
        print(f"{pad}not parseable JSON — verbatim:")
        for line in str(doc).splitlines():
            print(f"{pad}{line}")
        return
    for line in yaml_lines(parsed, indent):
        print(line)


# ─── references found by value shape ────────────────────────────────────────

def refs_in(node):
    """``[(json_path, value)]`` for every ARN and ``s3://`` URI in a document.

    Found by the shape of the value, not the name of the key: this code has no
    schema for these documents and must not pretend otherwise.  The JSON path
    travels along as provenance so every reported reference can be traced back
    to where it was read.
    """
    found = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")
        elif isinstance(node, str) and (node.startswith("arn:")
                                        or node.startswith("s3://")):
            found.append((path, node))

    walk(node, "")
    return found
