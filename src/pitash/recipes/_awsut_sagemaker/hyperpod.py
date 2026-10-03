"""``awsut sagemaker hyperpod`` — HyperPod cluster operations.

A HyperPod cluster is a SageMaker resource, so the group hangs off
``awsut sagemaker`` next to ``jobs``, ``hub`` and ``studio`` rather than off the
``awsut`` root, where it read as a service of its own.  The extra depth is paid
once, by an alias (``alias hp='awsut sagemaker hyperpod'``); the wrong path was
paid every time someone read the tree.

Ported from the legacy cshell, this module used to print with instruments of its
own — colon-separated one-liners where its neighbours printed tables — so the
same question answered about two SageMaker resources came back in two formats.
It now renders through :mod:`render` (i.e. :mod:`.._awsut_common`) like the rest
of the tree.  What is still its own is the machinery underneath: the client
factories (``awsut._get_sagemaker_client`` / ``awsut._get_boto3_client``) and
the pexpect-driven SSM plumbing (:func:`_ssm_run`).

Region, profile, endpoint and service-name come from the enclosing ``awsut``
recipe (``var aws_region=`` / ``var aws_profile=`` / ``var
sagemaker_endpoint=`` / ``var sagemaker_service_name=``), same as the rest of
the tree; ``list --all-regions`` and ``search-capacity`` sweep a fixed region
list of their own.
"""

from __future__ import annotations

import concurrent.futures
import copy
import csv
import datetime
import json
import os
import re
import sys
import threading
import time

from ...commands import arg
from ...completion import (
    ChoiceCompleter,
    Completer,
    Completion,
    CompletionContext,
    FileCompleter,
)
from ...completion_cache import aws_env_key, get_or_fetch
from ...shell import passthrough_input, passthrough_run
from .. import awsut
from .render import (
    INSTANCE_TYPE_CHOICES,
    RED,
    YELLOW,
    SmError,
    fmt_time,
    guard,
    print_header,
    print_labeled,
    print_table,
    region_label,
    section,
)


# ─── HyperPod region tables ─────────────────────────────────────────────────
#
# Where HyperPod is available, and (a narrower set) where reserved-capacity
# offerings are worth asking about.  Both are swept region-by-region, so they
# are explicit lists rather than "every region boto3 knows".

_hyperpod_regions = [
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "ap-south-1", "ap-northeast-1", "ap-southeast-2",
]

_search_capacity_regions = [
    "us-east-1", "us-east-2", "us-west-1", "us-west-2", "ap-northeast-1",
]


# ─── HyperPod helpers ───────────────────────────────────────────────────────

def _drop(d, key):
    d.pop(key, None)


def _rename(d, old, new):
    if old in d:
        d[new] = d.pop(old)


def _sanitize_instance_group(ig: dict) -> dict:
    """Strip read-only fields from a describe_cluster InstanceGroup so it can be
    fed back into update_cluster.  Mutates and returns the same dict."""
    _rename(ig, "TargetCount", "InstanceCount")
    for k in ("CurrentCount", "TargetCount", "Status",
              "SoftwareUpdateStatus", "TargetStateCount",
              "ActiveOperations", "FailureMessages",
              "TrainingPlanStatus", "CurrentImageId",
              "ImageVersionStatus", "CurrentImageReleaseVersion"):
        _drop(ig, k)
    _rename(ig, "DesiredImageId", "ImageId")
    _rename(ig, "DesiredImageReleaseVersion", "ImageReleaseVersion")
    if "KubernetesConfig" in ig:
        kc = ig["KubernetesConfig"]
        _rename(kc, "DesiredLabels", "Labels")
        _rename(kc, "DesiredTaints", "Taints")
        _drop(kc, "CurrentLabels")
        _drop(kc, "CurrentTaints")
    return ig


def _sanitize_restricted_instance_group(ig: dict) -> dict:
    """Same as `_sanitize_instance_group` but for RestrictedInstanceGroups."""
    _rename(ig, "TargetCount", "InstanceCount")
    for k in ("CurrentCount", "Status", "TrainingPlanStatus"):
        _drop(ig, k)
    if "EnvironmentConfig" in ig:
        _drop(ig["EnvironmentConfig"], "S3OutputPath")
    return ig


def _list_hyperpod_clusters_all(sagemaker_client) -> list[dict]:
    clusters: list[dict] = []
    next_token = None
    while True:
        params = {}
        if next_token:
            params["NextToken"] = next_token
        response = sagemaker_client.list_clusters(**params)
        clusters += response["ClusterSummaries"]
        next_token = response.get("NextToken")
        if not next_token:
            break
    return clusters


def _list_hyperpod_cluster_nodes_all(sagemaker_client, cluster_name: str) -> list[dict]:
    nodes: list[dict] = []
    next_token = None
    while True:
        params = {"ClusterName": cluster_name}
        if next_token:
            params["NextToken"] = next_token
        response = sagemaker_client.list_cluster_nodes(**params)
        nodes += response["ClusterNodeSummaries"]
        next_token = response.get("NextToken")
        if not next_token:
            break
    return nodes


def _list_hyperpod_cluster_events_all(
    sagemaker_client, cluster_name: str, event_time_after=None,
    max_results=None,
) -> list[dict]:
    """List cluster events (newest first).

    ``max_results`` caps how many events are returned and stops pagination
    early once enough have been collected.
    """
    events: list[dict] = []
    next_token = None
    while True:
        params = {"ClusterName": cluster_name}
        if event_time_after is not None:
            params["EventTimeAfter"] = event_time_after
        if next_token:
            params["NextToken"] = next_token
        response = sagemaker_client.list_cluster_events(**params)
        events += response["Events"]
        next_token = response.get("NextToken")
        if not next_token or (max_results is not None
                              and len(events) >= max_results):
            break
    return events[:max_results] if max_results is not None else events


def _hyperpod_event_failure_message(sagemaker_client, cluster_name: str,
                                    event_id: str) -> str | None:
    """Fetch an event's ``FailureMessage`` via ``describe_cluster_event``.

    The event summaries returned by ``list_cluster_events`` carry only a
    ``Description``; the failure message lives in the detailed event under
    ``EventDetails.EventDetails.EventMetadata.{Cluster|InstanceGroup|
    InstanceGroupScaling|Instance}.FailureMessage`` (the API nests an inner
    ``EventDetails`` inside the top-level one).  Returns the first non-empty
    message found, or ``None`` if the call fails or no message is present.
    """
    try:
        response = sagemaker_client.describe_cluster_event(
            ClusterName=cluster_name, EventId=event_id,
        )
    except Exception:                       # best-effort — never break the watch
        return None
    outer = response.get("EventDetails", {}) or {}
    # Metadata lives under the inner EventDetails; fall back to the flat shape
    # in case the API response layout changes.
    metadata = (outer.get("EventDetails", {}) or {}).get("EventMetadata")
    if not metadata:
        metadata = outer.get("EventMetadata", {}) or {}
    for scope in ("Instance", "InstanceGroupScaling", "InstanceGroup",
                  "Cluster"):
        message = (metadata.get(scope) or {}).get("FailureMessage")
        if message:
            return message
    return None


def _hyperpod_transitions_finished(cluster: dict, nodes: list[dict]) -> bool:
    """Return True when the cluster has no in-flight transitions left.

    This is the same condition the ``hyperpod wait`` command uses to decide
    it can stop, expressed as a single predicate:

    * cluster status is a terminal one (``InService`` / ``Failed``),
    * every instance group is terminal *and* its current count matches the
      target count (i.e. no scaling in progress), and
    * no node is in a non-terminal status (``Pending`` and every other
      not-yet-``Running``/``Failed`` state counts as still transitioning).
    """
    if cluster["ClusterStatus"] not in ("InService", "Failed"):
        return False
    for ig in (cluster.get("InstanceGroups", [])
               + cluster.get("RestrictedInstanceGroups", [])):
        if ig["Status"] not in ("InService", "Failed"):
            return False
        if ig["CurrentCount"] != ig["TargetCount"]:
            return False
    for node in nodes:
        if node["InstanceStatus"]["Status"] not in ("Running", "Failed"):
            return False
    return True


def _list_hyperpod_log_streams_all(logs_client, log_group: str) -> list[dict]:
    streams: list[dict] = []
    next_token = None
    while True:
        params = {"logGroupName": log_group, "limit": 50}
        if next_token:
            params["nextToken"] = next_token
        response = logs_client.describe_log_streams(**params)
        streams += response["logStreams"]
        next_token = response.get("nextToken")
        if not next_token:
            break
    return streams


class _HyperpodHostnames:
    """Maps cluster InstanceId ↔ short private DNS hostname."""

    _instance: "_HyperpodHostnames | None" = None

    @classmethod
    def instance(cls) -> "_HyperpodHostnames":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.node_id_to_hostname: dict[str, str] = {}
        self.hostname_to_node_id: dict[str, str] = {}

    def resolve(self, sagemaker_client, cluster, nodes):
        cluster_name = cluster["ClusterName"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            def resolve_one(node):
                node_id = node["InstanceId"]
                cached = self.node_id_to_hostname.get(node_id)
                if cached:
                    return cached
                response = sagemaker_client.describe_cluster_node(
                    ClusterName=cluster_name, NodeId=node_id,
                )
                return response["NodeDetails"]["PrivateDnsHostname"].split(".")[0]

            for node, hostname in zip(nodes, pool.map(resolve_one, nodes)):
                self.node_id_to_hostname[node["InstanceId"]] = hostname
                self.hostname_to_node_id[hostname] = node["InstanceId"]

    def get_hostname(self, node_id: str) -> str | None:
        return self.node_id_to_hostname.get(node_id)

    def get_node_id(self, hostname: str) -> str | None:
        return self.hostname_to_node_id.get(hostname)


def _resolve_hyperpod_node_id(sm, cluster, cluster_name, node_id_input: str) -> str:
    """Strip instance-group prefix and convert hostname to node_id when needed."""
    if "/" in node_id_input:
        node_id_input = node_id_input.split("/")[-1]
    if node_id_input.startswith("ip-"):
        nodes = _list_hyperpod_cluster_nodes_all(sm, cluster_name)
        hostnames = _HyperpodHostnames.instance()
        hostnames.resolve(sm, cluster, nodes)
        resolved = hostnames.get_node_id(node_id_input)
        if resolved:
            return resolved
    return node_id_input


def _print_hyperpod_log(logs_client, log_group, stream):
    start_time = int((time.time() - 24 * 60 * 60) * 1000)
    next_token = None
    try:
        while True:
            params = {
                "logGroupName":  log_group,
                "logStreamName": stream,
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
                # Raced with a retention sweep: it was listed a moment ago.
                print(f"error: log stream {stream!r} in {log_group} is gone",
                      file=sys.stderr)
                return

            for event in response["events"]:
                if start_time > event["timestamp"]:
                    continue
                print(event["message"].replace("\0", "\\0"))

            if response["nextForwardToken"] != next_token:
                next_token = response["nextForwardToken"]
            else:
                break
    except BrokenPipeError:
        # Downstream consumer closed early (e.g. `... | head`).  Normal.
        pass


# ─── shared SSM-pexpect helper ──────────────────────────────────────────────
#
# Both `awsut sagemaker hyperpod run` and `awsut sagemaker hyperpod ssh` need to drive
# a remote bash session over `aws ssm start-session`. The shape is identical:
# spawn the SSM client under a real PTY (so the remote shell sees a tty and
# prints prompts), wait for an initial prompt, send a single shell line, wait
# for the command to finish, then close cleanly. `_ssm_run` is the single
# place that touches pexpect; callers just describe the work as a shell line
# and decide whether they want the captured output.

# Initial-prompt patterns covering AL2 (`sh-4.2#`), AL2023 (`sh-5.2#`), and
# generic `# ` / `$ `. The TIMEOUT sentinel at the end is a fallback the
# caller can detect to nudge a prompt out of a terse SSM banner.
_SSM_PROMPT_PATTERNS = [
    r"sh-\d+\.\d+[#\$]\s*",
    r"[#\$]\s+",
]


def _ssm_run(
    ssm_target: str,
    shell_line: str,
    *,
    capture: bool = True,
    timeout: int = 30,
) -> tuple[str | None, str | None]:
    """Run a single shell line in an SSM session and return its output.

    Returns ``(output, error)`` where exactly one is non-None on success
    paths. ``output`` is the captured stdout/stderr of the shell line with
    line endings normalized to ``\\n``; ``error`` is a short one-line
    description of any pexpect-level failure (TIMEOUT, EOF, etc.). When
    ``capture=False`` the function still waits for completion but returns
    ``("", None)`` — useful for "fire and check" callers like install-key
    that just need to know the command finished.

    The end-of-output sentinel is built from two shell variables joined at
    runtime (``$S$T``) so that pexpect's first match for the literal
    sentinel string can only fire on the *resolved* echo line, not on the
    echo of the input line itself.
    """
    import pexpect

    sentinel = "__pitash_ssm_done_aef36c__"
    head = sentinel[: len(sentinel) // 2]
    tail = sentinel[len(sentinel) // 2 :]
    initial_patterns = [*_SSM_PROMPT_PATTERNS, pexpect.TIMEOUT]

    p = None
    try:
        p = pexpect.spawn(
            awsut.awscli[0],
            args=[*awsut.awscli[1:], "ssm", "start-session", "--target", ssm_target],
            timeout=timeout,
            encoding="utf-8",
        )
        idx = p.expect(initial_patterns, timeout=timeout)
        if idx == len(initial_patterns) - 1:
            # SSM banner ended without a prompt — nudge with a bare newline.
            p.sendline("")
            p.expect(_SSM_PROMPT_PATTERNS, timeout=10)

        p.sendline(f'S="{head}"; T="{tail}"; {shell_line}; echo "$S$T"')
        p.expect(sentinel, timeout=timeout)

        output = ""
        if capture:
            # Collapse PTY-doubled CRs ("\r\r\n") down to a single \n in one
            # pass — a naive \r\n→\n then \r→\n turns "\r\r\n" into "\n\n".
            raw = re.sub(r"\r+\n?", "\n", p.before or "")
            # First newline-delimited chunk is the shell's echo of our
            # sendline. Drop it; everything after, up to the sentinel, is
            # the real output. Strip the trailing `echo "$S$T"` echo line
            # bash emits just before the marker.
            _, _, after_echo = raw.partition("\n")
            output = re.sub(r'echo "\$S\$T"\s*\n?$', "", after_echo)

        # Graceful close — let SSM tear down rather than killing it.
        p.sendline("exit")
        try:
            p.expect(pexpect.EOF, timeout=5)
        except pexpect.TIMEOUT:
            pass
        return output, None
    except pexpect.TIMEOUT:
        tail_bytes = (p.before[-200:] if p is not None and p.before else "")
        return None, f"TIMEOUT (tail: {tail_bytes!r})" if tail_bytes else "TIMEOUT"
    except pexpect.EOF:
        tail_bytes = (p.before[-200:] if p is not None and p.before else "")
        return (None,
                f"EOF: SSM session ended before command completed "
                f"(tail: {tail_bytes!r})" if tail_bytes else
                "EOF: SSM session ended before command completed")
    except Exception as e:
        msg = str(e).splitlines()[0] if str(e) else ""
        return None, f"{type(e).__name__}{': ' + msg if msg else ''}"
    finally:
        if p is not None and p.isalive():
            try:
                p.terminate(force=True)
            except Exception:
                pass


# ─── HyperPod completers ────────────────────────────────────────────────────

def _cached_clusters() -> list[dict]:
    return get_or_fetch(
        ("awsut.hyperpod_clusters", aws_env_key(), awsut.sagemaker_endpoint,
         awsut.sagemaker_service_name),
        lambda: _list_hyperpod_clusters_all(awsut._get_sagemaker_client()),
    )


def _cached_describe_cluster(cluster_name: str) -> dict:
    return get_or_fetch(
        ("awsut.hyperpod_describe", aws_env_key(), awsut.sagemaker_endpoint,
         awsut.sagemaker_service_name, cluster_name),
        lambda: awsut._get_sagemaker_client().describe_cluster(ClusterName=cluster_name),
    )


def _cached_cluster_nodes(cluster_name: str) -> list[dict]:
    return get_or_fetch(
        ("awsut.hyperpod_nodes", aws_env_key(), awsut.sagemaker_endpoint,
         awsut.sagemaker_service_name, cluster_name),
        lambda: _list_hyperpod_cluster_nodes_all(awsut._get_sagemaker_client(), cluster_name),
    )


def _describe_vpc_subnets(seed_subnet: str) -> dict:
    ec2 = awsut._get_boto3_client("ec2")
    seed_resp = ec2.describe_subnets(SubnetIds=[seed_subnet])
    vpc_id = seed_resp["Subnets"][0]["VpcId"]
    return ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])


class _HyperpodClusterNameCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            clusters = _cached_clusters()
        except Exception:
            return []
        return [Completion(value=c["ClusterName"], description=c.get("ClusterStatus", ""))
                for c in clusters
                if c["ClusterName"].startswith(ctx.prefix)]


class _HyperpodInstanceGroupNameCompleter(Completer):
    """Completes instance group names. Reads cluster_name from preceding args."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not ctx.args:
            return []
        cluster_name = ctx.args[0]
        try:
            cluster = _cached_describe_cluster(cluster_name)
        except Exception:
            return []
        result: list[Completion] = []
        for ig in (cluster.get("InstanceGroups", [])
                   + cluster.get("RestrictedInstanceGroups", [])):
            name = ig["InstanceGroupName"]
            if not name.startswith(ctx.prefix):
                continue
            instance_type = ig.get("InstanceType", "")
            current = ig.get("CurrentCount")
            target = ig.get("TargetCount")
            if current is not None and target is not None:
                count = f"{current}=>{target}" if current != target else f"{current}"
            elif target is not None:
                count = str(target)
            elif current is not None:
                count = str(current)
            else:
                count = ""
            # Columns, so the counts line up under each other instead of
            # starting wherever the instance type happened to end.
            result.append(Completion(
                value=name,
                fields=(instance_type, f"({count})" if count else ""),
            ))
        return result


class _HyperpodSubnetIdCompleter(Completer):
    """Completes subnet IDs in the same VPC as the cluster.

    Reads ``cluster_name`` from ``ctx.args[0]``.  Locates the VPC by looking at
    a known subnet on the cluster (any IG's ``OverrideVpcConfig.Subnets`` or
    the cluster-level ``VpcConfig.Subnets``), then lists every subnet in that
    VPC.  Each completion is annotated with the subnet's Name tag and AZ.
    """

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not ctx.args:
            return []
        cluster_name = ctx.args[0]
        try:
            cluster = _cached_describe_cluster(cluster_name)
        except Exception:
            return []

        seed_subnet = None
        for ig in cluster.get("InstanceGroups", []) + cluster.get("RestrictedInstanceGroups", []):
            ovc = ig.get("OverrideVpcConfig") or {}
            subnets = ovc.get("Subnets") or []
            if subnets:
                seed_subnet = subnets[0]
                break
        if seed_subnet is None:
            cluster_vpc = cluster.get("VpcConfig") or {}
            subnets = cluster_vpc.get("Subnets") or []
            if subnets:
                seed_subnet = subnets[0]
        if seed_subnet is None:
            return []

        try:
            vpc_resp = get_or_fetch(
                ("awsut.hyperpod_vpc_subnets", aws_env_key(), seed_subnet),
                lambda: _describe_vpc_subnets(seed_subnet),
            )
        except Exception:
            return []

        result: list[Completion] = []
        for subnet in vpc_resp.get("Subnets", []):
            sid = subnet["SubnetId"]
            if not sid.startswith(ctx.prefix):
                continue
            name = ""
            for tag in subnet.get("Tags", []) or []:
                if tag.get("Key") == "Name":
                    name = tag.get("Value", "")
                    break
            az_id = subnet.get("AvailabilityZoneId", "")
            cidr = subnet.get("CidrBlock", "")
            result.append(Completion(value=sid, fields=(az_id, cidr, name)))
        return result


class _HyperpodNodeIdCompleter(Completer):
    """Completes node IDs (and IG/node_id, hostname). Reads cluster_name from preceding args."""

    def __init__(self, with_cwlog: bool = False):
        self.with_cwlog = with_cwlog

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if not ctx.args:
            return []
        cluster_name = ctx.args[0]
        try:
            unique = get_or_fetch(
                ("awsut.hyperpod_node_choices", aws_env_key(),
                 awsut.sagemaker_endpoint, awsut.sagemaker_service_name,
                 cluster_name, self.with_cwlog),
                lambda: self._fetch_choices(cluster_name),
            )
        except Exception:
            return []
        return [Completion(value=c, description="node")
                for c in unique if c.startswith(ctx.prefix)]

    def _fetch_choices(self, cluster_name: str) -> list[str]:
        sm = awsut._get_sagemaker_client()
        cluster = _cached_describe_cluster(cluster_name)
        nodes = _cached_cluster_nodes(cluster_name)

        hostnames = _HyperpodHostnames.instance()
        try:
            hostnames.resolve(sm, cluster, nodes)
        except Exception:
            pass

        choices: list[str] = []
        for node in nodes:
            node_id = node["InstanceId"]
            ig_name = node["InstanceGroupName"]
            choices.append(node_id)
            hn = hostnames.get_hostname(node_id)
            if hn:
                choices.append(hn)
            choices.append(f"{ig_name}/{node_id}")

        if self.with_cwlog:
            cluster_id = cluster["ClusterArn"].split("/")[-1]
            log_group = f"/aws/sagemaker/Clusters/{cluster_name}/{cluster_id}"
            try:
                streams = _list_hyperpod_log_streams_all(
                    awsut._get_boto3_client("logs"), log_group)
            except Exception:
                streams = []
            for stream in streams:
                stream_name = stream["logStreamName"]
                parts = stream_name.split("/")
                if len(parts) >= 2:
                    ig_name = parts[-2]
                    node_id = parts[-1]
                    choices.append(node_id)
                    choices.append(f"{ig_name}/{node_id}")

        seen: set[str] = set()
        unique: list[str] = []
        for c in choices:
            if c not in seen:
                seen.add(c)
                unique.append(c)
        return unique


def _no_cluster(cluster_name: str) -> SmError:
    """The one wording for a cluster name that resolves to nothing.

    A name that resolves to nothing is far more often a region mismatch than a
    typo, so the region it was looked for in is part of the message.
    """
    return SmError(f"no HyperPod cluster {cluster_name!r} in "
                   f"region {region_label()}")


def _describe_or_fail(sm, cluster_name: str) -> dict:
    """A cluster description, or one ``error:`` line — most leaves start here."""
    try:
        return sm.describe_cluster(ClusterName=cluster_name)
    except sm.exceptions.ResourceNotFound:
        raise _no_cluster(cluster_name) from None


def register_hyperpod(sagemaker) -> None:
    """Attach the ``hyperpod`` group to ``awsut sagemaker``."""
    hyperpod = sagemaker.command(
        "hyperpod", help="HyperPod cluster operations")

    @hyperpod.command(
        "create", help="Create a cluster with JSON file",
        params=[
            arg("cluster_name", help="Name of cluster"),
            arg("--eks-cluster-name", metavar="NAME",
                help="Name of EKS cluster"),
            arg("--instances", required=True, metavar="FILE",
                completer=FileCompleter(),
                help="JSON config file for instance groups"),
            arg("--restricted-instances", metavar="FILE",
                completer=FileCompleter(),
                help="JSON config file for restricted instance groups"),
            arg("--vpc", metavar="FILE", completer=FileCompleter(),
                help="JSON config file for VPC"),
        ],
    )
    @guard
    def _create(cluster_name, eks_cluster_name, instances,
                restricted_instances, vpc):
        params = {
            "ClusterName":  cluster_name,
            "NodeRecovery": "Automatic",
        }
        if eks_cluster_name:
            eks = awsut._get_boto3_client("eks")
            desc = eks.describe_cluster(name=eks_cluster_name)
            params["Orchestrator"] = {"Eks": {"ClusterArn": desc["cluster"]["arn"]}}
            params["NodeProvisioningMode"] = "Continuous"

        with open(os.path.expanduser(instances)) as fd:
            params["InstanceGroups"] = json.loads(fd.read())

        if restricted_instances:
            with open(os.path.expanduser(restricted_instances)) as fd:
                params["RestrictedInstanceGroups"] = json.loads(fd.read())

        if vpc:
            with open(os.path.expanduser(vpc)) as fd:
                params["VpcConfig"] = json.loads(fd.read())

        sm = awsut._get_sagemaker_client()
        print(f"CreateCluster · cluster {cluster_name} · region {region_label()}")
        response = sm.create_cluster(**params)
        print(f"ClusterArn {response['ClusterArn']}")

    @hyperpod.command(
        "update", help="Update a cluster with JSON file",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter(),
                help="Name of cluster"),
            arg("--instances", required=True, metavar="FILE",
                completer=FileCompleter(),
                help="JSON config file for instance groups"),
            arg("--restricted-instances", metavar="FILE",
                completer=FileCompleter(),
                help="JSON config file for restricted instance groups"),
        ],
    )
    @guard
    def _update(cluster_name, instances, restricted_instances):
        sm = awsut._get_sagemaker_client()
        cluster = _describe_or_fail(sm, cluster_name)

        params = {"ClusterName": cluster_name}
        if "NodeRecovery" in cluster:
            params["NodeRecovery"] = cluster["NodeRecovery"]

        with open(os.path.expanduser(instances)) as fd:
            params["InstanceGroups"] = json.loads(fd.read())

        if restricted_instances:
            with open(os.path.expanduser(restricted_instances)) as fd:
                params["RestrictedInstanceGroups"] = json.loads(fd.read())

        print(f"UpdateCluster · cluster {cluster_name} · from {instances}")
        response = sm.update_cluster(**params)
        print(f"ClusterArn {response['ClusterArn']}")

    @hyperpod.command(
        "scale", help="Scale up or down an instance group",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("instance_group_name", completer=_HyperpodInstanceGroupNameCompleter()),
            arg("target_instance_count", type=int),
        ],
    )
    @guard
    def _scale(cluster_name, instance_group_name, target_instance_count):
        sm = awsut._get_sagemaker_client()
        cluster = _describe_or_fail(sm, cluster_name)

        params = {"ClusterName": cluster_name}
        if "NodeRecovery" in cluster:
            params["NodeRecovery"] = cluster["NodeRecovery"]

        if cluster.get("InstanceGroups"):
            params["InstanceGroups"] = []
        for ig in cluster.get("InstanceGroups", []):
            _sanitize_instance_group(ig)
            if ig["InstanceGroupName"] == instance_group_name:
                ig["InstanceCount"] = target_instance_count
            params["InstanceGroups"].append(ig)

        if cluster.get("RestrictedInstanceGroups"):
            params["RestrictedInstanceGroups"] = []
        for ig in cluster.get("RestrictedInstanceGroups", []):
            _sanitize_restricted_instance_group(ig)
            if ig["InstanceGroupName"] == instance_group_name:
                ig["InstanceCount"] = target_instance_count
            params["RestrictedInstanceGroups"].append(ig)

        print(f"UpdateCluster · cluster {cluster_name} · instance group "
              f"{instance_group_name} → {target_instance_count} instance(s)")
        response = sm.update_cluster(**params)
        print(f"ClusterArn {response['ClusterArn']}")

    @hyperpod.command(
        "add-ig",
        help="Create a new instance group based on an existing template",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("instance_group_name", help="Name of the new instance group"),
            arg("--template", required=True, metavar="NAME",
                completer=_HyperpodInstanceGroupNameCompleter(),
                help="Existing instance group to copy as the template"),
            arg("--instance-type", metavar="TYPE",
                completer=ChoiceCompleter(INSTANCE_TYPE_CHOICES),
                help="Override instance type (default: copy from template)"),
            arg("--instance-count", type=int, default=0, metavar="N",
                help="Initial instance count (default: 0; scale up later "
                     "with `awsut sagemaker hyperpod scale`)"),
            arg("--subnet-id", metavar="SUBNET",
                completer=_HyperpodSubnetIdCompleter(),
                help="Override subnet ID. Security groups are inherited from "
                     "the template's OverrideVpcConfig if present, otherwise "
                     "from the cluster's VpcConfig."),
        ],
    )
    @guard
    def _create_instance_group(cluster_name, instance_group_name, template,
                               instance_type, instance_count, subnet_id):
        sm = awsut._get_sagemaker_client()
        cluster = _describe_or_fail(sm, cluster_name)

        # Locate the template across both regular and restricted IGs.
        template_ig = None
        is_restricted = False
        for ig in cluster.get("InstanceGroups", []):
            if ig["InstanceGroupName"] == template:
                template_ig = ig
                break
        if template_ig is None:
            for ig in cluster.get("RestrictedInstanceGroups", []):
                if ig["InstanceGroupName"] == template:
                    template_ig = ig
                    is_restricted = True
                    break
        if template_ig is None:
            raise SmError(f"no instance group {template!r} in cluster "
                          f"{cluster_name!r} to copy as the template")

        existing_names = {ig["InstanceGroupName"]
                          for ig in cluster.get("InstanceGroups", [])}
        existing_names |= {ig["InstanceGroupName"]
                           for ig in cluster.get("RestrictedInstanceGroups", [])}
        if instance_group_name in existing_names:
            raise SmError(f"instance group {instance_group_name!r} already exists "
                          f"in cluster {cluster_name!r}")

        # Sanitize first (strips read-only fields and renames TargetCount →
        # InstanceCount), THEN apply overrides — otherwise the rename would
        # clobber a user-supplied --instance-count with the template's
        # TargetCount.
        new_ig = copy.deepcopy(template_ig)
        if is_restricted:
            _sanitize_restricted_instance_group(new_ig)
        else:
            _sanitize_instance_group(new_ig)

        new_ig["InstanceGroupName"] = instance_group_name
        new_ig["InstanceCount"] = instance_count
        if instance_type is not None:
            new_ig["InstanceType"] = instance_type
        if subnet_id is not None:
            existing_override = template_ig.get("OverrideVpcConfig") or {}
            sgs = existing_override.get("SecurityGroupIds")
            if not sgs:
                cluster_vpc = cluster.get("VpcConfig") or {}
                sgs = cluster_vpc.get("SecurityGroupIds")
            if not sgs:
                raise SmError("cannot infer SecurityGroupIds for the new instance "
                              "group: neither the template's OverrideVpcConfig nor "
                              "the cluster's VpcConfig has them")
            new_ig["OverrideVpcConfig"] = {
                "Subnets": [subnet_id],
                "SecurityGroupIds": list(sgs),
            }

        # Build update_cluster params: keep all existing IGs untouched
        # (after sanitizing) and append the new one.
        params = {"ClusterName": cluster_name}
        if "NodeRecovery" in cluster:
            params["NodeRecovery"] = cluster["NodeRecovery"]

        regular = [_sanitize_instance_group(ig)
                   for ig in cluster.get("InstanceGroups", [])]
        restricted = [_sanitize_restricted_instance_group(ig)
                      for ig in cluster.get("RestrictedInstanceGroups", [])]

        if is_restricted:
            restricted.append(new_ig)
        else:
            regular.append(new_ig)

        if regular:
            params["InstanceGroups"] = regular
        if restricted:
            params["RestrictedInstanceGroups"] = restricted

        kind = "restricted instance group" if is_restricted else "instance group"
        print(f"UpdateCluster · cluster {cluster_name} · new {kind} "
              f"{instance_group_name} · {new_ig['InstanceType']} × "
              f"{instance_count} · from template {template}")
        response = sm.update_cluster(**params)
        print(f"ClusterArn {response['ClusterArn']}")

    @hyperpod.command(
        "remove-ig", help="Delete an instance group from a cluster",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("instance_group_name", completer=_HyperpodInstanceGroupNameCompleter()),
            arg("-y", "--yes", action="store_true", help="Skip confirmation"),
        ],
    )
    @guard
    def _delete_instance_group(cluster_name, instance_group_name, yes):
        sm = awsut._get_sagemaker_client()
        cluster = _describe_or_fail(sm, cluster_name)

        regular_names = [ig["InstanceGroupName"]
                         for ig in cluster.get("InstanceGroups", [])]
        restricted_names = [ig["InstanceGroupName"]
                            for ig in cluster.get("RestrictedInstanceGroups", [])]
        if (instance_group_name not in regular_names
                and instance_group_name not in restricted_names):
            raise SmError(f"no instance group {instance_group_name!r} in cluster "
                          f"{cluster_name!r}")

        if not yes:
            answer = passthrough_input(
                f"Delete instance group {instance_group_name!r} from cluster "
                f"{cluster_name!r}? Its instances are terminated. [y/N] : "
            )
            if answer.strip().lower() not in ("y", "yes"):
                print("not deleted")
                return

        params = {"ClusterName": cluster_name}
        if "NodeRecovery" in cluster:
            params["NodeRecovery"] = cluster["NodeRecovery"]
        params["InstanceGroupsToDelete"] = [instance_group_name]

        print(f"UpdateCluster · cluster {cluster_name} · deleting instance group "
              f"{instance_group_name}")
        response = sm.update_cluster(**params)
        print(f"ClusterArn {response['ClusterArn']}")

    def _batch_node_operation(operation_name, api, cluster_name, node_ids):
        """One node-batch API call, reported per node.

        The batch APIs answer per node — a partial success is the normal case —
        so the response is read out as a table rather than printed as the raw
        dict it used to be.
        """
        sm = awsut._get_sagemaker_client()
        cluster = _describe_or_fail(sm, cluster_name)
        resolved_ids = [
            _resolve_hyperpod_node_id(sm, cluster, cluster_name, n) for n in node_ids
        ]
        print_header(f"{operation_name} · cluster {cluster_name}",
                     f"{len(resolved_ids)} node(s)")
        response = api(ClusterName=cluster_name, NodeIds=resolved_ids)

        successful = set(response.get("Successful", []))
        failed = {f.get("NodeId"): f for f in response.get("Failed", [])}
        rows = []
        for node_id in resolved_ids:
            if node_id in successful:
                rows.append([node_id, "Accepted", ""])
            elif node_id in failed:
                fail = failed[node_id]
                rows.append([node_id, fail.get("Code") or "Failed",
                             fail.get("Message") or ""])
            else:
                # Neither list mentions it — the model may not report per node.
                rows.append([node_id, "-", ""])
        print_table(["NODE ID", "RESULT", "MESSAGE"], rows)

    @hyperpod.command(
        "delete-nodes", help="Delete specific nodes",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("node_ids", nargs="+", completer=_HyperpodNodeIdCompleter(with_cwlog=False)),
        ],
    )
    @guard
    def _delete_nodes(cluster_name, node_ids):
        sm = awsut._get_sagemaker_client()
        _batch_node_operation(
            "Delete nodes", sm.batch_delete_cluster_nodes, cluster_name, node_ids,
        )

    @hyperpod.command(
        "reboot-nodes", help="Reboot specific nodes",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("node_ids", nargs="+", completer=_HyperpodNodeIdCompleter(with_cwlog=False)),
        ],
    )
    @guard
    def _reboot_nodes(cluster_name, node_ids):
        sm = awsut._get_sagemaker_client()
        _batch_node_operation(
            "Reboot nodes", sm.batch_reboot_cluster_nodes, cluster_name, node_ids,
        )

    @hyperpod.command(
        "replace-nodes", help="Replace specific nodes",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("node_ids", nargs="+", completer=_HyperpodNodeIdCompleter(with_cwlog=False)),
        ],
    )
    @guard
    def _replace_nodes(cluster_name, node_ids):
        sm = awsut._get_sagemaker_client()
        _batch_node_operation(
            "Replace nodes", sm.batch_replace_cluster_nodes, cluster_name, node_ids,
        )

    @hyperpod.command(
        "upgrade-ami", help="Update the AMI of a cluster",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("--instance-group-name", metavar="NAME",
                completer=_HyperpodInstanceGroupNameCompleter(),
                help="Instance group to apply update (default: all)"),
            arg("--rolling-update-by", metavar="N|N%",
                help="Number or percentage of instances to update at once"),
        ],
    )
    @guard
    def _update_software(cluster_name, instance_group_name, rolling_update_by):
        params = {"ClusterName": cluster_name}
        if instance_group_name:
            params["InstanceGroups"] = [{"InstanceGroupName": instance_group_name}]

        if rolling_update_by:
            params["DeploymentConfig"] = {}
            m_count = re.match(r"([0-9]+)$", rolling_update_by)
            m_pct   = re.match(r"([0-9]+)%$", rolling_update_by)
            if m_pct:
                params["DeploymentConfig"]["RollingUpdatePolicy"] = {
                    "MaximumBatchSize": {
                        "Type": "CAPACITY_PERCENTAGE",
                        "Value": int(m_pct.group(1)),
                    }
                }
            elif m_count:
                params["DeploymentConfig"]["RollingUpdatePolicy"] = {
                    "MaximumBatchSize": {
                        "Type": "INSTANCE_COUNT",
                        "Value": int(m_count.group(1)),
                    }
                }
            else:
                raise SmError(f"--rolling-update-by wants N or N% "
                              f"(got {rolling_update_by!r})")

        sm = awsut._get_sagemaker_client()
        scope = instance_group_name or "every instance group"
        print(f"UpdateClusterSoftware · cluster {cluster_name} · {scope}"
              + (f" · {rolling_update_by} at a time" if rolling_update_by else ""))
        response = sm.update_cluster_software(**params)
        print(f"ClusterArn {response['ClusterArn']}")

    @hyperpod.command(
        "delete", help="Delete a cluster",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("-y", "--yes", action="store_true",
                help="Skip confirmation"),
        ],
    )
    @guard
    def _delete(cluster_name, yes):
        if not yes:
            answer = passthrough_input(
                f"Delete cluster {cluster_name!r} in region {region_label()}? "
                "Every node in it is terminated. [y/N] : ")
            if answer.strip().lower() not in ("y", "yes"):
                print("not deleted")
                return

        sm = awsut._get_sagemaker_client()
        try:
            response = sm.delete_cluster(ClusterName=cluster_name)
        except sm.exceptions.ResourceNotFound:
            raise _no_cluster(cluster_name) from None
        print(f"DeleteCluster · cluster {cluster_name} · region {region_label()}")
        print(f"ClusterArn {response['ClusterArn']}")

    @hyperpod.command(
        "list", help="List clusters in human readable format",
        params=[
            arg("--all-regions", action="store_true",
                help="List clusters in all regions"),
        ],
    )
    @guard
    def _list(all_regions):
        def _list_one(region_name=None):
            sm = awsut._get_sagemaker_client(region_name=region_name)
            clusters = _list_hyperpod_clusters_all(sm)
            print_header(f"{len(clusters)} cluster(s)",
                         f"region {region_name or region_label()}")
            print_table(
                ["NAME", "STATUS", "CREATED", "ARN"],
                [[c["ClusterName"], c["ClusterStatus"],
                  fmt_time(c.get("CreationTime")), c["ClusterArn"]]
                 for c in clusters],
            )
            # A failure reason is a paragraph, not a cell — it follows the
            # table, one block per cluster that has one.
            for cluster in clusters:
                if cluster["ClusterStatus"] not in ("Failed", "RollingBack"):
                    continue
                print()
                section(f"{cluster['ClusterName']} · {cluster['ClusterStatus']}")
                try:
                    details = sm.describe_cluster(
                        ClusterName=cluster["ClusterName"])
                except sm.exceptions.ResourceNotFound:
                    print("no FailureMessage — the cluster is already gone")
                    continue
                for line in (details.get("FailureMessage")
                             or "no FailureMessage returned").splitlines():
                    print(line)

        if all_regions:
            for i, region in enumerate(_hyperpod_regions):
                if i:
                    print()
                _list_one(region_name=region)
        else:
            _list_one()

    @hyperpod.command(
        "describe", help="Describe cluster and its nodes in depth",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("--raw", action="store_true",
                help="Show raw JSON output from boto3 APIs"),
        ],
    )
    @guard
    def _describe(cluster_name, raw):
        sm = awsut._get_sagemaker_client()
        cluster = _describe_or_fail(sm, cluster_name)

        cluster_id = cluster["ClusterArn"].split("/")[-1]
        nodes = _list_hyperpod_cluster_nodes_all(sm, cluster_name)

        if raw:
            cluster.pop("ResponseMetadata", None)
            section("Cluster")
            awsut._print_json(cluster)
            print()
            section("Nodes")
            awsut._print_json(nodes)
            return

        hostnames = _HyperpodHostnames.instance()
        hostnames.resolve(sm, cluster, nodes)

        print_labeled([
            ("ClusterName", cluster["ClusterName"]),
            ("ClusterArn", cluster["ClusterArn"]),
            ("ClusterStatus", cluster["ClusterStatus"]),
            ("FailureMessage", cluster.get("FailureMessage")),
        ])

        groups = cluster["InstanceGroups"] + cluster["RestrictedInstanceGroups"]
        print()
        print_header(f"{len(groups)} instance group(s)")
        print_table(
            ["NAME", "INSTANCE TYPE", "STATUS", "COUNT"],
            [[ig["InstanceGroupName"], ig["InstanceType"], ig["Status"],
              f"{ig['CurrentCount']} → {ig['TargetCount']}"] for ig in groups],
        )

        # Nodes ordered by their instance group, so the grouping still reads
        # down the page — but as one table, so every row carries the group it
        # belongs to next to the SSM target that reaches it.
        ordered = [node for ig in groups for node in nodes
                   if node["InstanceGroupName"] == ig["InstanceGroupName"]]

        # Pre-scan: a status message is a paragraph, so it becomes a numbered
        # footnote below and the number rides along in the STATUS cell.
        failures: list[tuple[int, str, str, str]] = []
        node_tag: dict[str, int] = {}
        for node in ordered:
            msg = node["InstanceStatus"].get("Message")
            if msg:
                tag = len(failures) + 1
                node_tag[node["InstanceId"]] = tag
                failures.append((tag, node["InstanceGroupName"],
                                 node["InstanceId"], msg))

        def _status_label(node):
            status = node["InstanceStatus"]["Status"]
            tag = node_tag.get(node["InstanceId"])
            return f"{status} [{tag}]" if tag is not None else status

        print()
        print_header(f"{len(ordered)} node(s)")
        print_table(
            ["INSTANCE GROUP", "NODE ID", "HOSTNAME", "STATUS", "LAUNCHED",
             "SSM TARGET"],
            [[node["InstanceGroupName"], node["InstanceId"],
              hostnames.get_hostname(node["InstanceId"]) or "-",
              _status_label(node), fmt_time(node.get("LaunchTime")),
              f"sagemaker-cluster:{cluster_id}"
              f"_{node['InstanceGroupName']}-{node['InstanceId']}"]
             for node in ordered],
        )

        if failures:
            print()
            section("Node status messages")
            for tag, ig_name, node_id, msg in failures:
                lines = msg.splitlines() or [""]
                prefix = f"[{tag}] {ig_name}/{node_id}: "
                indent = " " * len(prefix)
                print(prefix + lines[0])
                for line in lines[1:]:
                    print(indent + line)

    @hyperpod.command(
        "watch",
        help="Watch and report cluster status changes (events + nodes)",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("-f", "--follow", action="store_true",
                help="Keep watching after all transitions finish "
                     "(default: stop once settled)"),
            arg("-n", "--interval", type=float, default=10.0, metavar="SEC",
                help="Poll interval in seconds (default 10)"),
        ],
    )
    @guard
    def _watch(cluster_name, follow=False, interval=10.0):
        sm = awsut._get_sagemaker_client()

        def stamp() -> str:
            # Time of day only, like the other watch loops in the tree: the date
            # is on every line of a timeline that rarely outlives the hour.
            return datetime.datetime.now().strftime("%H:%M:%S")

        def emit(line: str) -> None:
            print(f"[{stamp()}] {line}", flush=True)

        # Baseline snapshot — report changes relative to it, not the whole
        # world on the first tick.
        #
        # Events are fetched with a moving ``EventTimeAfter`` watermark rather
        # than pulling the full history every tick and filtering client-side:
        #   * the watermark starts at command-execution time, so events older
        #     than "now" are never fetched or reported, and
        #   * each tick advances it to the newest event seen, so subsequent
        #     calls only return events that arrived since.
        # ``seen_events`` holds just the event ids sitting exactly on the
        # watermark, to de-dup the inclusive-boundary event(s) between ticks.
        event_watermark = datetime.datetime.now(datetime.timezone.utc)
        seen_events: set[str] = set()
        prev_cluster_status: str | None = None
        prev_ig: dict[str, tuple] = {}        # ig_name -> (status, current, target)
        prev_nodes: dict[str, str] = {}       # node_id -> node status
        # Scaling / readiness snapshots — re-reported whenever they change (and
        # at baseline), independent of the per-item transition lines above.
        prev_scaling: dict[str, tuple] = {}   # ig_name -> (current, target), only when current != target
        prev_not_running: dict[str, tuple] = {}  # node_id -> (ig_name, status), only when status != Running
        # Some clusters (NodeProvisioningMode != Continuous) don't support
        # ListClusterEvents at all.  Warn once, then stop calling it — the
        # error is identical every tick and would otherwise spam the output.
        events_supported = True
        first = True

        print(f"watching {cluster_name}")
        print(f"  region {region_label()} · poll {interval}s · "
              f"{'follow' if follow else 'until settled'}")
        print("  Ctrl+C to detach\n")

        while True:
            try:
                cluster = sm.describe_cluster(ClusterName=cluster_name)
            except sm.exceptions.ResourceNotFound:
                raise _no_cluster(cluster_name) from None

            try:
                nodes = _list_hyperpod_cluster_nodes_all(sm, cluster_name)
            except sm.exceptions.ResourceNotFound:
                raise _no_cluster(cluster_name) from None

            events = []
            if events_supported:
                try:
                    events = _list_hyperpod_cluster_events_all(
                        sm, cluster_name, event_time_after=event_watermark,
                    )
                except Exception as e:          # events are best-effort
                    emit(f"(warning: could not list cluster events: {e})")
                    # ValidationException means the cluster fundamentally
                    # doesn't support ListClusterEvents (e.g. non-Continuous
                    # provisioning); the error repeats every tick, so stop
                    # calling it after warning once.  Other errors may be
                    # transient, so keep retrying those.
                    if "ValidationException" in str(e):
                        events_supported = False

            # ── cluster status ──────────────────────────────────────────
            cluster_status = cluster["ClusterStatus"]
            if cluster_status != prev_cluster_status:
                if not first:
                    emit(f"Cluster status: {prev_cluster_status} → "
                         f"{cluster_status}")
                    if cluster.get("FailureMessage"):
                        emit(f"  Failure message: {cluster['FailureMessage']}")
                prev_cluster_status = cluster_status

            # ── instance groups (status + scaling) ──────────────────────
            all_igs = (cluster.get("InstanceGroups", [])
                       + cluster.get("RestrictedInstanceGroups", []))
            cur_ig: dict[str, tuple] = {}
            for ig in all_igs:
                name = ig["InstanceGroupName"]
                state = (ig["Status"], ig["CurrentCount"], ig["TargetCount"])
                cur_ig[name] = state
                if not first and prev_ig.get(name) != state:
                    prev = prev_ig.get(name)
                    if prev is None:
                        emit(f"InstanceGroup {name}: appeared "
                             f"[{state[0]} {state[1]}/{state[2]}]")
                    else:
                        emit(f"InstanceGroup {name}: "
                             f"{prev[0]}({prev[1]}/{prev[2]}) → "
                             f"{state[0]}({state[1]}/{state[2]})")
            if not first:
                for name in prev_ig.keys() - cur_ig.keys():
                    emit(f"InstanceGroup {name}: removed")
            prev_ig = cur_ig

            # ── nodes (appeared / disappeared / status change) ──────────
            cur_nodes: dict[str, str] = {}
            node_ig: dict[str, str] = {}
            for node in nodes:
                node_id = node["InstanceId"]
                status = node["InstanceStatus"]["Status"]
                cur_nodes[node_id] = status
                node_ig[node_id] = node["InstanceGroupName"]
                if not first:
                    prev = prev_nodes.get(node_id)
                    if prev is None:
                        emit(f"Node {node['InstanceGroupName']}/{node_id}: "
                             f"appeared [{status}]")
                    elif prev != status:
                        msg = node["InstanceStatus"].get("Message")
                        line = (f"Node {node['InstanceGroupName']}/{node_id}: "
                                f"{prev} → {status}")
                        if msg:
                            line += f"  ({msg})"
                        emit(line)
            if not first:
                for node_id in prev_nodes.keys() - cur_nodes.keys():
                    emit(f"Node {node_id}: disappeared "
                         f"(was {prev_nodes[node_id]})")
            prev_nodes = cur_nodes

            if first:
                emit(f"Baseline: cluster={cluster_status}, "
                     f"{len(all_igs)} instance group(s), "
                     f"{len(nodes)} node(s)")

            # ── scaling summary (instance groups where current != target) ──
            # Report the set of in-flight scaling operations at baseline and
            # again whenever that set changes.
            cur_scaling = {
                name: (state[1], state[2])
                for name, state in cur_ig.items()
                if state[1] != state[2]
            }
            if cur_scaling != prev_scaling:
                if cur_scaling:
                    summary = ", ".join(
                        f"{name} {cur:d}/{tgt:d}"
                        for name, (cur, tgt) in sorted(cur_scaling.items())
                    )
                    emit(f"Scaling in progress: {summary}")
                elif not first:
                    emit("Scaling in progress: none (all groups at target)")
                prev_scaling = cur_scaling

            # ── non-Running nodes ───────────────────────────────────────
            # List every node that is not yet Running at baseline and again
            # whenever that list (or any node's status in it) changes.
            cur_not_running = {
                node_id: (node_ig[node_id], status)
                for node_id, status in cur_nodes.items()
                if status != "Running"
            }
            if cur_not_running != prev_not_running:
                if cur_not_running:
                    emit(f"Nodes not Running ({len(cur_not_running)}):")
                    for node_id, (ig_name, status) in sorted(
                        cur_not_running.items(), key=lambda kv: (kv[1][0], kv[0])
                    ):
                        emit(f"    {ig_name}/{node_id}: {status}")
                elif not first:
                    emit("Nodes not Running: none (all nodes Running)")
                prev_not_running = cur_not_running

            # ── new events ──────────────────────────────────────────────
            # The API filtered to events at/after the watermark; drop the
            # boundary event(s) we already reported last tick, then report the
            # rest in chronological order.
            new_events = [e for e in events if e["EventId"] not in seen_events]
            new_events.sort(key=lambda e: e["EventTime"])
            for event in new_events:
                target = event.get("InstanceId") or event.get("InstanceGroupName") or ""
                target = f" {target}" if target else ""
                level = event.get("EventLevel")
                level_prefix = f"[{level}] " if level else ""
                emit(f"Event {level_prefix}{event['ResourceType']}{target}: "
                     f"{event['Description']}")
                # Error/Warn events carry a FailureMessage only in the detailed
                # event; fetch and print it so the cause isn't hidden.
                if level in ("Error", "Warn"):
                    message = _hyperpod_event_failure_message(
                        sm, cluster_name, event["EventId"])
                    if message:
                        for line in message.splitlines():
                            emit(f"  Failure message: {line}")

            # Advance the watermark to the newest event time seen so the next
            # call only returns events after it.  Keep the ids sitting exactly
            # on the new watermark in ``seen_events`` to de-dup the inclusive
            # boundary next tick.
            if events:
                newest = max(e["EventTime"] for e in events)
                event_watermark = newest
                seen_events = {e["EventId"] for e in events
                               if e["EventTime"] == newest}

            first = False

            # ── stop condition ──────────────────────────────────────────
            settled = _hyperpod_transitions_finished(cluster, nodes)
            if settled and not follow:
                emit("All transitions finished. Stopping.")
                return

            time.sleep(interval)

    @hyperpod.command(
        "log", help="Print log from a cluster node",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("node_id", completer=_HyperpodNodeIdCompleter(with_cwlog=True)),
        ],
    )
    @guard
    def _log(cluster_name, node_id):
        sm = awsut._get_sagemaker_client()
        logs = awsut._get_boto3_client("logs")

        cluster = _describe_or_fail(sm, cluster_name)

        cluster_id = cluster["ClusterArn"].split("/")[-1]
        log_group = f"/aws/sagemaker/Clusters/{cluster_name}/{cluster_id}"

        try:
            streams = _list_hyperpod_log_streams_all(logs, log_group)
        except logs.exceptions.ResourceNotFoundException:
            raise SmError(f"no log group {log_group!r} — the cluster has not "
                          f"written any node logs yet") from None

        if node_id.startswith("ip-"):
            nodes = _list_hyperpod_cluster_nodes_all(sm, cluster_name)
            hostnames = _HyperpodHostnames.instance()
            hostnames.resolve(sm, cluster, nodes)
            node_id = hostnames.get_node_id(node_id) or node_id

        matched = [s["logStreamName"] for s in streams
                   if node_id == "*" or s["logStreamName"].endswith(node_id)]
        if not matched:
            raise SmError(f"no log stream for {node_id!r} in {log_group}")

        for i, stream_name in enumerate(matched):
            if i:
                print()
            section(f"{log_group} {stream_name}")
            _print_hyperpod_log(logs, log_group, stream_name)

    @hyperpod.command(
        "ssm", help="Login to a cluster node with SSM",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("node_id", completer=_HyperpodNodeIdCompleter(with_cwlog=False)),
        ],
    )
    @guard
    def _ssm(cluster_name, node_id):
        sm = awsut._get_sagemaker_client()
        cluster = _describe_or_fail(sm, cluster_name)

        nodes = _list_hyperpod_cluster_nodes_all(sm, cluster_name)
        cluster_id = cluster["ClusterArn"].split("/")[-1]
        node_id = _resolve_hyperpod_node_id(sm, cluster, cluster_name, node_id)

        ig_name = None
        for node in nodes:
            if node["InstanceId"] == node_id:
                ig_name = node["InstanceGroupName"]
                break
        else:
            raise SmError(f"no node {node_id!r} in cluster {cluster_name!r}")

        ssm_target = f"sagemaker-cluster:{cluster_id}_{ig_name}-{node_id}"
        passthrough_run(["aws", "ssm", "start-session", "--target", ssm_target])

    @hyperpod.command(
        "ssh",
        help="Install SSH public key on cluster nodes and add Host entries to "
             "~/.ssh/config",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("public_key_file", completer=FileCompleter(),
                help="SSH public key file (e.g. ~/.ssh/id_rsa.pub)"),
            arg("user", nargs="?",
                choices=["ubuntu", "ec2-user"],
                help="Login user. Default: 'ubuntu' for Slurm clusters, "
                     "'ec2-user' for EKS clusters."),
            arg("--instance-group-name", metavar="NAME",
                completer=_HyperpodInstanceGroupNameCompleter(),
                help="Restrict to nodes in this instance group"),
            arg("--node-id", nargs="+", default=[], metavar="NODE",
                completer=_HyperpodNodeIdCompleter(with_cwlog=False),
                help="Restrict to these specific nodes"),
        ],
    )
    @guard
    def _ssh(cluster_name, public_key_file, user, instance_group_name, node_id):
        try:
            import pexpect  # noqa: F401  (helper imports it; surface the error here)
        except ImportError:
            raise SmError("pexpect is required for `awsut sagemaker hyperpod ssh` "
                          "— install it with `pip install pexpect`") from None

        sm = awsut._get_sagemaker_client()
        cluster = _describe_or_fail(sm, cluster_name)

        if user is None:
            is_eks = bool(cluster.get("Orchestrator", {}).get("Eks"))
            user = "ec2-user" if is_eks else "ubuntu"

        with open(os.path.expanduser(public_key_file)) as fd:
            public_key = fd.read().strip()
        if len(public_key.splitlines()) > 1:
            raise SmError(f"{public_key_file} holds more than one line — pass a "
                          f"single public key")

        all_nodes = _list_hyperpod_cluster_nodes_all(sm, cluster_name)
        cluster_id = cluster["ClusterArn"].split("/")[-1]

        # Resolve --node-id filter values (strip ig/ prefix, hostname → node_id).
        filter_node_ids: list[str] = []
        if node_id:
            hostnames = _HyperpodHostnames.instance()
            need_hostnames = any(n.startswith("ip-") or "/ip-" in n for n in node_id)
            if need_hostnames:
                try:
                    hostnames.resolve(sm, cluster, all_nodes)
                except Exception:
                    pass
            for n in node_id:
                if "/" in n:
                    n = n.split("/")[-1]
                if n.startswith("ip-"):
                    n = hostnames.get_node_id(n) or n
                filter_node_ids.append(n)

        targets = []
        for node in all_nodes:
            if instance_group_name and node["InstanceGroupName"] != instance_group_name:
                continue
            if filter_node_ids and node["InstanceId"] not in filter_node_ids:
                continue
            targets.append(node)

        if not targets:
            raise SmError("no node matched the given filters")

        # 1. Install the public key and capture each node's home directory.
        print_lock = threading.Lock()
        node_homes: dict[str, str] = {}

        def install(node):
            ig_name = node["InstanceGroupName"]
            node_id_local = node["InstanceId"]
            ssm_target = f"sagemaker-cluster:{cluster_id}_{ig_name}-{node_id_local}"
            shell_line = (
                f'HOME_DIR=$(getent passwd {user} | cut -d: -f6); '
                f'if [ -z "$HOME_DIR" ]; then echo "HOME_DIR_NOT_FOUND"; exit 1; fi; '
                f'mkdir -p "$HOME_DIR/.ssh" && chmod 700 "$HOME_DIR/.ssh" && '
                f'chown {user} "$HOME_DIR/.ssh" && '
                f'touch "$HOME_DIR/.ssh/authorized_keys" && '
                f'chmod 600 "$HOME_DIR/.ssh/authorized_keys" && '
                f'chown {user} "$HOME_DIR/.ssh/authorized_keys" && '
                f'if ! grep -qF "{public_key}" "$HOME_DIR/.ssh/authorized_keys"; '
                f'then echo "{public_key}" >> "$HOME_DIR/.ssh/authorized_keys"; fi; '
                f'echo "HOME_DIR=$HOME_DIR"'
            )
            output, error = _ssm_run(ssm_target, shell_line, capture=True)
            with print_lock:
                if error:
                    print(f"error: {ig_name}/{node_id_local}: {error}",
                          file=sys.stderr)
                    return
                home_match = re.search(r"^HOME_DIR=(\S+)$", output or "", re.MULTILINE)
                if not home_match or "HOME_DIR_NOT_FOUND" in (output or ""):
                    print(f"error: {ig_name}/{node_id_local}: no home directory "
                          f"for user {user}", file=sys.stderr)
                    return
                node_homes[node_id_local] = home_match.group(1)
                print(f"Installed SSH key on {ig_name}/{node_id_local} "
                      f"(home: {node_homes[node_id_local]})")

        workers = max(1, min(16, len(targets)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for _ in pool.map(install, targets):
                pass

        if not node_homes:
            raise SmError("the key could not be installed on any targeted node "
                          "(see above); ~/.ssh/config left untouched")

        # 2. Build the SSH config block(s) and merge into ~/.ssh/config.
        identity_file = public_key_file
        if identity_file.endswith(".pub"):
            identity_file = identity_file[:-4]

        profile = awsut._get_profile()
        region = awsut._get_region() or ""

        config_path = os.path.expanduser("~/.ssh/config")
        existing = ""
        if os.path.exists(config_path):
            with open(config_path) as fd:
                existing = fd.read()

        added_blocks: list[str] = []
        host_aliases: list[str] = []
        for node in targets:
            node_id_local = node["InstanceId"]
            if node_id_local not in node_homes:
                continue
            ig_name = node["InstanceGroupName"]
            host_alias = f"{cluster_name}-{ig_name}-{node_id_local}"
            host_aliases.append(host_alias)

            if re.search(rf"(?m)^Host\s+{re.escape(host_alias)}\s*$", existing):
                continue

            block = (
                f"Host {host_alias}\n"
                f"    HostName sagemaker-cluster:{cluster_id}_{ig_name}-{node_id_local}\n"
                f"    User {user}\n"
                f"    IdentityFile {identity_file}\n"
                f"    ProxyCommand aws --profile {profile} --region {region} "
                f"ssm start-session --target %h --document-name AWS-StartSSHSession "
                f"--parameters portNumber=%p\n"
            )
            added_blocks.append(block)

        if added_blocks:
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            sep = "" if not existing or existing.endswith("\n") else "\n"
            with open(config_path, "a") as fd:
                fd.write(sep + "\n" + "\n".join(added_blocks))
            print()
            print(f"Added {len(added_blocks)} Host entr"
                  f"{'y' if len(added_blocks) == 1 else 'ies'} to {config_path}:")
            print()
            for block in added_blocks:
                print(block, end="")
        else:
            print()
            print(f"All Host entries already present in {config_path}.")

        if host_aliases:
            print()
            print("Examples:")
            print(f"  ssh {host_aliases[0]}")
            print(f"  code --remote ssh-remote+{host_aliases[0]} /home/{user}")

    @hyperpod.command(
        "run",
        help="Run a single line command on cluster nodes (in parallel)",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("--instance-group-name", metavar="NAME",
                completer=_HyperpodInstanceGroupNameCompleter(),
                help="Restrict to nodes in this instance group"),
            arg("--instances", nargs="+", default=[], metavar="NODE",
                completer=_HyperpodNodeIdCompleter(with_cwlog=False),
                help="Restrict to these specific nodes"),
            arg("--all", dest="all_nodes", action="store_true",
                help="Run on every node in the cluster (required if neither "
                     "--instance-group-name nor --instances is given)"),
            arg("--max-parallel", type=int, default=16, metavar="N",
                help="Maximum number of nodes to run on concurrently (default: 16)"),
            arg("--command", required=True, metavar="CMD",
                help="Single line of command to run"),
        ],
    )
    @guard
    def _run(cluster_name, instance_group_name, instances, all_nodes,
             max_parallel, command):
        # Validate targeting first — cheap and useful even without pexpect.
        if not (all_nodes or instance_group_name or instances):
            raise SmError("refusing to run without an explicit target — pass "
                          "--instance-group-name NAME, --instances NODE [NODE ...], "
                          "or --all for every node in the cluster")
        if all_nodes and (instance_group_name or instances):
            raise SmError("--all is mutually exclusive with "
                          "--instance-group-name and --instances")

        try:
            import pexpect  # noqa: F401  (helper imports it; surface the error here)
        except ImportError:
            raise SmError("pexpect is required for `awsut sagemaker hyperpod run` "
                          "— install it with `pip install pexpect`") from None

        sm = awsut._get_sagemaker_client()
        cluster = _describe_or_fail(sm, cluster_name)

        nodes = _list_hyperpod_cluster_nodes_all(sm, cluster_name)
        cluster_id = cluster["ClusterArn"].split("/")[-1]

        node_ids: list[str] = []
        for inst in instances:
            if "/" in inst:
                inst = inst.split("/")[-1]
            if inst.startswith("ip-"):
                hostnames = _HyperpodHostnames.instance()
                hostnames.resolve(sm, cluster, nodes)
                inst = hostnames.get_node_id(inst) or inst
            node_ids.append(inst)

        targets = []
        for node in nodes:
            ig_name = node["InstanceGroupName"]
            node_id = node["InstanceId"]
            if instance_group_name and ig_name != instance_group_name:
                continue
            if node_ids and node_id not in node_ids:
                continue
            targets.append(node)

        if not targets:
            raise SmError("no node matched the given filters")

        print_lock = threading.Lock()

        def run_on(node):
            ig_name = node["InstanceGroupName"]
            node_id = node["InstanceId"]
            ssm_target = f"sagemaker-cluster:{cluster_id}_{ig_name}-{node_id}"
            output, error = _ssm_run(ssm_target, command, capture=True)

            with print_lock:
                section(f"{ig_name}/{node_id}")
                if output:
                    print(output, end="")
                    if not output.endswith("\n"):
                        print()
                if error:
                    print(f"error: {ig_name}/{node_id}: {error}", file=sys.stderr)
                print()

        workers = max(1, min(max_parallel, len(targets)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for _ in pool.map(run_on, targets):
                pass

    @hyperpod.command(
        "search-capacity",
        help="Search Flexible Training Plans offerings in all regions",
        params=[
            arg("--instance-type", required=True, metavar="TYPE",
                completer=ChoiceCompleter(INSTANCE_TYPE_CHOICES),
                help="Instance type (e.g. ml.p5.48xlarge)"),
            arg("--instance-count", required=True, type=int, metavar="N",
                help="Number of instances"),
            arg("--duration-hours", required=True, type=int, metavar="N",
                help="Requested duration in hours"),
        ],
    )
    @guard
    def _search_capacity(instance_type, instance_count, duration_hours):
        rows = []
        unsupported = []
        for region in _search_capacity_regions:
            params = {
                "TargetResources": ["hyperpod-cluster"],
                "InstanceType":    instance_type,
                "InstanceCount":   instance_count,
                "DurationHours":   duration_hours,
            }
            sm = awsut._get_sagemaker_client(region_name=region)
            try:
                response = sm.search_training_plan_offerings(**params)
            except sm.exceptions.ClientError as e:
                if "Invalid instance type" in str(e):
                    # The type isn't offered in this region at all — a fact
                    # about the sweep, reported once at the end.
                    unsupported.append(region)
                    continue
                raise

            for tp in response["TrainingPlanOfferings"]:
                for offering in tp["ReservedCapacityOfferings"]:
                    rows.append([
                        region,
                        offering["AvailabilityZone"],
                        f"{offering['DurationHours']}h"
                        f"{offering['DurationMinutes']:02d}m",
                        fmt_time(offering.get("StartTime")),
                        fmt_time(offering.get("EndTime")),
                    ])

        print_header(f"{len(rows)} offering(s)",
                     f"{instance_count} × {instance_type}",
                     f"{duration_hours}h",
                     f"{len(_search_capacity_regions)} region(s) searched")
        note = None
        if unsupported:
            note = (f"{instance_type} is not offered in "
                    f"{', '.join(unsupported)}.")
        print_table(["REGION", "AZ", "DURATION", "START", "END"], rows, note=note)

    @hyperpod.command(
        "kubeconfig", help="Update kubeconfig with the EKS cluster",
        params=[arg("cluster_name", completer=_HyperpodClusterNameCompleter())],
    )
    @guard
    def _kubeconfig(cluster_name):
        sm = awsut._get_sagemaker_client()
        cluster = _describe_or_fail(sm, cluster_name)

        try:
            eks_arn = cluster["Orchestrator"]["Eks"]["ClusterArn"]
        except KeyError:
            raise SmError(f"cluster {cluster_name!r} has no EKS orchestrator — "
                          f"there is no kubeconfig to update (a Slurm cluster is "
                          f"reached with `ssm` or `ssh`)") from None

        eks_name = eks_arn.split("/")[-1]
        passthrough_run(["aws", "eks", "update-kubeconfig", "--name", eks_name])

    @hyperpod.command(
        "events", help="Print historical events",
        params=[
            arg("cluster_name", completer=_HyperpodClusterNameCompleter()),
            arg("--format", choices=["table", "csv", "jsonl"], default="table",
                help="Output format (default: aligned table)"),
            arg("--max", dest="limit", type=int, default=100, metavar="N",
                help="Maximum number of (most recent) events to show "
                     "(default 100; 0 = no limit)"),
            arg("--details", action="store_true",
                help="Dump detailed JSON description of each event"),
        ],
    )
    @guard
    def _events(cluster_name, format, limit, details):
        sm = awsut._get_sagemaker_client()
        limit = None if limit in (0, None) else limit
        try:
            events = _list_hyperpod_cluster_events_all(
                sm, cluster_name, max_results=limit)
        except sm.exceptions.ResourceNotFound:
            raise _no_cluster(cluster_name) from None

        def failure_message(event):
            # Error/Warn events carry a FailureMessage only in the detailed
            # event; fetch it so the cause isn't hidden.
            if event.get("EventLevel") not in ("Error", "Warn"):
                return ""
            return _hyperpod_event_failure_message(
                sm, cluster_name, event["EventId"]) or ""

        if details:
            for event in events:
                event_id = event["EventId"]
                try:
                    response = sm.describe_cluster_event(
                        ClusterName=cluster_name, EventId=event_id,
                    )
                    print(json.dumps(response, default=str, indent=2))
                except Exception as e:
                    print(f"error: event {event_id}: {e}", file=sys.stderr)
        elif format in ("table", "csv"):
            # Description and FailureMessage share the last column: the
            # description's length is unpredictable, so padding it to the widest
            # cell would leave a ragged gap before a separate FailureMessage
            # column.  Appending the message keeps every row single-line and the
            # cause right next to its description.
            headers = ["TIMESTAMP", "LEVEL", "TYPE",
                       "GROUP", "INSTANCE", "DESCRIPTION"]
            rows = []
            for event in events:
                # Collapse newlines so each row stays a single line.
                message = " ".join(failure_message(event).split())
                description = event["Description"]
                if message:
                    description = f"{description} — {message}"
                event_time = event["EventTime"]
                timestamp = (fmt_time(event_time)
                             if hasattr(event_time, "strftime")
                             else str(event_time))
                rows.append([
                    timestamp,
                    event.get("EventLevel", ""),
                    event["ResourceType"],
                    event.get("InstanceGroupName", ""),
                    event.get("InstanceId", ""),
                    description,
                ])
            if format == "csv":
                # Real CSV, quoted by the stdlib: a Description carries commas
                # and the FailureMessage appended to it can carry quotes, so
                # hand-joining would produce a file no reader could parse back.
                writer = csv.writer(sys.stdout, lineterminator="\n")
                writer.writerow(headers)
                writer.writerows(rows)
            else:
                def level_color(col, value):
                    # Only the LEVEL column, and only the two levels worth
                    # interrupting a scan for.
                    if col != 1:
                        return None
                    return {"Error": RED, "Warn": YELLOW}.get(value)

                print_header(f"{len(rows)} event(s)", f"cluster {cluster_name}",
                             f"region {region_label()}")
                print_table(headers, rows, colorize=level_color)
        elif format == "jsonl":
            for event in events:
                # Enrich Error/Warn summaries with the FailureMessage that only
                # lives in the detailed event, mirroring the table output.
                if "FailureMessage" not in event:
                    message = failure_message(event)
                    if message:
                        event = {**event, "FailureMessage": message}
                print(json.dumps(event, default=str))

