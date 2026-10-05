"""``awsut sagemaker hub`` — hub content, and lineage tracing across it.

The other half of the ``jobs`` picture.  A job's output need not be just S3
objects: it can be a *hub-content* resource (e.g. ``HubContentType DataSet``)
in the account's own hub.  The job's ``JobConfigDocument`` records only the
output prefix you supplied; the hub-content resource records where the data
actually landed and which job produced it.  So "where is my data" is a
two-hop question:

    DescribeJob → a DatasetArn in the config document → DescribeHubContent
    → DatasetS3Uri

``trace`` walks exactly that, in either direction and any number of hops.

Same ground rules as ``jobs``: identifiers print in full, never wrapped or
ellipsized, and documents are reformatted for reading but nothing in here
knows a single key name.  Both ``JobConfigDocument`` and
``HubContentDocument`` are modelled as opaque strings, so this code navigates
them by *value shape* — an ARN says what it points at, an ``s3://`` URI says
it is a location — and reports the JSON path a value was found at so you can
see where a claim came from.  Nothing is inferred from key names.
"""

from __future__ import annotations

import sys

import botocore.exceptions

from ...commands import arg
from ...completion import Completer, Completion, CompletionContext
from ...completion_cache import aws_env_key, get_or_fetch
from .. import awsut
from .jobs import _JobNameCompleter, describe_job, job_categories
from .render import (
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
    model_enum,
    paged,
    parse_doc,
    positionals,
    print_header,
    print_labeled,
    print_table,
    refs_in,
    region_label,
    require_operation,
    s3_client,
    section,
    show_document,
    sm_client,
    split_arn,
    split_s3,
    version_key,
    yaml_lines,
)

# Fallback only.  The loaded botocore model supplies these (see
# :func:`hub_content_types`), and a refreshed model contributes new ones
# without a change here.
FALLBACK_CONTENT_TYPES = ["Model", "Notebook", "ModelReference", "DataSet", "JsonDoc"]


def hub_content_types() -> list[str]:
    """Every HubContentType this shell will probe, from the loaded model. Offline."""
    found = list(model_enum(awsut.sagemaker_service_name,
                            "ListHubContents", "HubContentType"))
    return found or list(FALLBACK_CONTENT_TYPES)


def resolve_types(selector: str | None) -> list[str]:
    if selector in (None, "all"):
        return hub_content_types()
    return [selector]


# ─── hub selection ──────────────────────────────────────────────────────────

def list_hubs(cli, max_items=50):
    require_operation(cli, "list_hubs", "ListHubs")
    return paged(cli.list_hubs, "HubSummaries", max_items,
                 MaxResults=min(max_items, 100))


def is_aws_owned(hub_summary) -> bool:
    """AWS-owned hubs are identified by the ARN's account segment, not by name."""
    parsed = split_arn(hub_summary.get("HubArn") or "")
    return bool(parsed) and parsed[2] == "aws"


def resolve_hub(cli, requested: str | None) -> str:
    """The hub to work in: the one asked for, else the account's own if unambiguous.

    The public SageMaker hub is excluded when auto-selecting — it is AWS-owned
    and read-only, so it is never where a job of yours writes.
    """
    if requested:
        return requested
    try:
        mine = [h for h in list_hubs(cli) if not is_aws_owned(h)]
    except botocore.exceptions.ClientError as exc:
        raise SmError(f"could not list hubs to pick one automatically: "
                      f"{api_message(exc)}")
    if not mine:
        raise SmError("no account-owned hub found in this region; pass --hub")
    if len(mine) > 1:
        names = ", ".join(h.get("HubName", "?") for h in mine)
        raise SmError(f"several account-owned hubs; pass --hub one of: {names}")
    return mine[0].get("HubName")


# ─── content lookup ─────────────────────────────────────────────────────────

def contents(cli, hub, content_type, max_items, contains=None):
    require_operation(cli, "list_hub_contents", "ListHubContents")
    params = {"HubName": hub, "HubContentType": content_type,
              "MaxResults": min(max_items, 100)}
    if contains:
        params["NameContains"] = contains
    return paged(cli.list_hub_contents, "HubContentSummaries", max_items, **params)


def find_content(cli, hub, name, content_type=None, max_items=100):
    """Locate a content item by name, probing types when none is given.

    Returns the newest version's summary, or ``None``.  ``ListHubContents``'
    ``NameContains`` is a substring filter, so the result is matched on the
    exact name afterwards.
    """
    for a_type in resolve_types(content_type):
        try:
            found = contents(cli, hub, a_type, max_items, contains=name)
        except botocore.exceptions.ClientError:
            continue
        exact = [s for s in found if s.get("HubContentName") == name]
        if exact:
            return max(exact, key=lambda s: version_key(s.get("HubContentVersion")))
    return None


def resolve_content_type(cli, hub, name, content_type):
    """The content type for *name*, probing when the user didn't say."""
    if content_type:
        return content_type
    summary = find_content(cli, hub, name)
    if not summary:
        raise SmError(f"no content named {name!r} in hub {hub} "
                      f"(types tried: {', '.join(hub_content_types())})")
    return summary.get("HubContentType")


def latest_version(cli, hub, content_type, name):
    require_operation(cli, "list_hub_content_versions", "ListHubContentVersions")
    versions = paged(cli.list_hub_content_versions, "HubContentSummaries", 100,
                     HubName=hub, HubContentType=content_type,
                     HubContentName=name, MaxResults=100)
    if not versions:
        return None, []
    newest = max(versions, key=lambda s: version_key(s.get("HubContentVersion")))
    return newest.get("HubContentVersion"), versions


def describe_content(cli, hub, content_type, name, version=None):
    require_operation(cli, "describe_hub_content", "DescribeHubContent")
    params = {"HubName": hub, "HubContentType": content_type,
              "HubContentName": name}
    if version:
        params["HubContentVersion"] = version
    return cli.describe_hub_content(**params)


# ─── completers ─────────────────────────────────────────────────────────────

def _cached_hubs() -> list[dict]:
    return get_or_fetch(
        ("awsut.sagemaker_hubs", aws_env_key(), awsut.sagemaker_endpoint,
         awsut.sagemaker_service_name),
        lambda: list_hubs(sm_client()),
    )


def _cached_contents(hub: str | None, content_type: str | None) -> list[dict]:
    """Every content item in the resolved hub, across the selected type(s)."""
    def fetch():
        cli = sm_client()
        resolved_hub = resolve_hub(cli, hub)
        rows = []
        for a_type in resolve_types(content_type):
            try:
                rows.extend(contents(cli, resolved_hub, a_type, 100))
            except botocore.exceptions.ClientError:
                continue      # unauthorized / unsupported type — just skip it
        return rows

    return get_or_fetch(
        ("awsut.sagemaker_hub_contents", aws_env_key(), awsut.sagemaker_endpoint,
         awsut.sagemaker_service_name, hub, content_type),
        fetch,
    )


class _HubNameCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            hubs = _cached_hubs()
        except Exception:
            return []
        out = []
        for h in hubs:
            name = h.get("HubName") or ""
            if not name.startswith(ctx.prefix):
                continue
            owner = "aws (public)" if is_aws_owned(h) else "account-owned"
            out.append(Completion(value=name, description=owner))
        return out


class _ContentTypeCompleter(Completer):
    def __init__(self, allow_all: bool = False):
        self.allow_all = allow_all

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        names = hub_content_types()
        if self.allow_all:
            names = ["all"] + names
        return [Completion(value=n) for n in names if n.startswith(ctx.prefix)]


class _ContentNameCompleter(Completer):
    """Content names in the hub, narrowed by an already-typed ``--hub``/``--type``."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            rows = _cached_contents(flag_value(ctx.args, "--hub"),
                                    flag_value(ctx.args, "--type"))
        except Exception:
            return []
        # One row per name: ListHubContents already returns only the newest
        # version, but probing several types can surface the same name twice.
        seen = {}
        for s in rows:
            name = s.get("HubContentName") or ""
            if not name.startswith(ctx.prefix) or name in seen:
                continue
            seen[name] = Completion(
                value=name,
                description=" ".join(p for p in (
                    s.get("HubContentType") or "",
                    f"v{s.get('HubContentVersion')}" if s.get("HubContentVersion") else "",
                ) if p),
            )
        return list(seen.values())


class _ContentVersionCompleter(Completer):
    """Versions of the content item named earlier on the line."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        typed = positionals(ctx.args, ("--hub", "--type", "--version"))
        if not typed:
            return []
        name = typed[0]
        hub = flag_value(ctx.args, "--hub")
        content_type = flag_value(ctx.args, "--type")
        try:
            versions = get_or_fetch(
                ("awsut.sagemaker_hub_versions", aws_env_key(),
                 awsut.sagemaker_endpoint, awsut.sagemaker_service_name,
                 hub, content_type, name),
                lambda: _fetch_versions(hub, content_type, name),
            )
        except Exception:
            return []
        out = []
        for s in sorted(versions, key=lambda s: version_key(s.get("HubContentVersion")),
                        reverse=True):
            value = str(s.get("HubContentVersion") or "")
            if not value.startswith(ctx.prefix):
                continue
            out.append(Completion(value=value,
                                  description=s.get("HubContentStatus") or ""))
        return out


def _fetch_versions(hub, content_type, name):
    cli = sm_client()
    resolved_hub = resolve_hub(cli, hub)
    resolved_type = resolve_content_type(cli, resolved_hub, name, content_type)
    return latest_version(cli, resolved_hub, resolved_type, name)[1]


class _TraceRefCompleter(Completer):
    """Anything ``trace`` accepts as a starting point: content names or job names.

    An ARN is also accepted but never completed — there is no list to offer it
    from, and an ARN reaches the prompt by being pasted, not typed.
    """

    def __init__(self):
        self._jobs = _JobNameCompleter()
        self._contents = _ContentNameCompleter()

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        return self._contents.complete(ctx) + self._jobs.complete(ctx)


# ─── shared flags ───────────────────────────────────────────────────────────
#
# Declared per leaf rather than on the ``hub`` group so completion stays
# honest: `hubs` has no hub to select and no type to filter, and offering it
# either flag would be a lie.

def _hub_flag():
    return arg("--hub", metavar="HUB", completer=_HubNameCompleter(),
               help="default: the account-owned hub, when there is exactly one")


def _type_flag(allow_all=False):
    return arg("--type", metavar="TYPE", dest="content_type",
               completer=_ContentTypeCompleter(allow_all=allow_all),
               help=("default: every known type" if allow_all
                     else "default: probe until the name is found"))


# ─── command tree ───────────────────────────────────────────────────────────

def register_hub(sagemaker) -> None:
    hub = sagemaker.command(
        "hub",
        help="Hub content — the datasets and models jobs read and write",
    )

    @hub.command(
        "hubs",
        help="List the hubs visible in this region (OWNER 'aws (public)' is the "
             "read-only SageMaker public hub; your own content lives in an "
             "account-owned one)",
        params=[arg("--max", type=int, default=25, dest="limit", metavar="N",
                    help="hubs to list (default 25)")],
    )
    @guard
    def _hub_hubs(limit):
        cli = sm_client()
        hubs = list_hubs(cli, limit)
        rows = []
        for h in hubs:
            parsed = split_arn(h.get("HubArn") or "")
            rows.append([
                h.get("HubName") or "?",
                h.get("HubDisplayName") or "-",
                h.get("HubStatus") or "?",
                "aws (public)" if is_aws_owned(h) else (parsed[2] if parsed else "?"),
                fmt_time(h.get("CreationTime")),
                h.get("HubArn") or "-",
            ])
        print_header(f"{len(rows)} hub(s)", f"region {region_label()}")
        print_table(
            ["HUB NAME", "DISPLAY NAME", "STATUS", "OWNER", "CREATED", "ARN"], rows)

    @hub.command(
        "list",
        help="List hub content — one row per item, its newest version only "
             "(`versions NAME` for an item's history)",
        params=[
            _hub_flag(),
            _type_flag(allow_all=True),
            arg("--contains", metavar="TEXT", help="NameContains filter"),
            arg("--max", type=int, default=50, dest="limit", metavar="N",
                help="per type (default 50)"),
        ],
    )
    @guard
    def _hub_list(hub, content_type, contains, limit):
        cli = sm_client()
        resolved_hub = resolve_hub(cli, hub)
        types = resolve_types(content_type)

        rows = []
        skipped = []
        for a_type in types:
            try:
                found = contents(cli, resolved_hub, a_type, limit, contains=contains)
            except botocore.exceptions.ClientError as exc:
                print(f"error: {a_type}: {api_message(exc)}", file=sys.stderr)
                skipped.append(a_type)
                continue
            for s in found:
                rows.append([
                    s.get("HubContentType") or a_type,
                    s.get("HubContentName") or "?",
                    str(s.get("HubContentVersion") or "?"),
                    s.get("HubContentStatus") or "?",
                    str(s.get("DocumentSchemaVersion") or "-"),
                    fmt_time(s.get("CreationTime")),
                    s.get("HubContentDescription") or "-",
                ])

        rows.sort(key=lambda r: (r[0], r[1], version_key(r[2])))
        # Verified against an item known to have both 0.0.1 and 0.0.2: only 0.0.2
        # comes back, so this listing is one row per item (as the help says) and a
        # version count here would mean nothing.
        print_header(f"{len(rows)} item(s)", f"hub {resolved_hub}",
                     f"region {region_label()}", f"{len(types)} type(s) queried")
        print_table(
            ["TYPE", "NAME", "VERSION", "STATUS", "DOC SCHEMA", "CREATED",
             "DESCRIPTION"],
            rows,
            note=(f"not queried (see stderr): {', '.join(skipped)}"
                  if skipped else None),
        )

    @hub.command(
        "versions", help="Version history of one content item",
        params=[
            arg("name", help="hub content item name",
                completer=_ContentNameCompleter()),
            _hub_flag(),
            _type_flag(),
        ],
    )
    @guard
    def _hub_versions(name, hub, content_type):
        cli = sm_client()
        resolved_hub = resolve_hub(cli, hub)
        summary = find_content(cli, resolved_hub, name, content_type)
        if not summary:
            raise SmError(
                f"no content named {name!r} in hub {resolved_hub} "
                f"(types tried: {', '.join(resolve_types(content_type))})")
        resolved_type = summary.get("HubContentType")

        _newest, versions = latest_version(cli, resolved_hub, resolved_type, name)
        versions.sort(key=lambda s: version_key(s.get("HubContentVersion")))
        rows = [[
            str(s.get("HubContentVersion") or "?"),
            s.get("HubContentStatus") or "?",
            str(s.get("DocumentSchemaVersion") or "-"),
            fmt_time(s.get("CreationTime")),
            s.get("HubContentDescription") or "-",
        ] for s in versions]
        print_header(f"{len(rows)} version(s) of {resolved_type} {name}",
                     f"hub {resolved_hub}")
        print_table(["VERSION", "STATUS", "DOC SCHEMA", "CREATED", "DESCRIPTION"],
                    rows)

    @hub.command(
        "describe", help="Describe one content item, including its document",
        params=[
            arg("name", help="hub content item name",
                completer=_ContentNameCompleter()),
            _hub_flag(),
            _type_flag(),
            arg("--version", metavar="V", completer=_ContentVersionCompleter(),
                help="default: newest"),
            arg("--raw", action="store_true",
                help="Show the raw API response as JSON"),
            arg("--raw-doc", action="store_true",
                help="Print HubContentDocument verbatim — the API returns it as "
                     "one JSON string, which is reformatted for reading by "
                     "default"),
        ],
    )
    @guard
    def _hub_describe(name, hub, content_type, version, raw, raw_doc):
        cli = sm_client()
        resolved_hub = resolve_hub(cli, hub)
        resolved_type = resolve_content_type(cli, resolved_hub, name, content_type)

        all_versions = []
        if not version:
            version, all_versions = latest_version(cli, resolved_hub,
                                                   resolved_type, name)
        d = describe_content(cli, resolved_hub, resolved_type, name, version)
        if raw:
            awsut._print_json(d)
            return

        # Identifiers first, one per line, unwrapped — these are the copy targets.
        suffix = f"  (newest of {len(all_versions)})" if all_versions else ""
        keywords = d.get("HubContentSearchKeywords")
        print_labeled([
            ("HubContentArn", d.get("HubContentArn")),
            ("HubArn", d.get("HubArn")),
            ("HubName", d.get("HubName")),
            ("HubContentType", d.get("HubContentType")),
            ("HubContentName", d.get("HubContentName")),
            None,
            ("Version", f"{d.get('HubContentVersion')}{suffix}"),
            ("Status", d.get("HubContentStatus")),
            ("DocSchema", d.get("DocumentSchemaVersion")),
            ("Created", fmt_time(d.get("CreationTime"))),
            ("Modified", fmt_time(d.get("LastModifiedTime"))),
            ("DisplayName", d.get("HubContentDisplayName")),
            ("Description", d.get("HubContentDescription")),
            ("SupportStatus", d.get("SupportStatus")),
            ("PublicHubArn", d.get("SageMakerPublicHubContentArn")),
            ("Keywords", ", ".join(keywords) if keywords else None),
        ])
        if d.get("FailureReason"):
            print()
            section("FailureReason")
            print(d["FailureReason"])

        if d.get("HubContentDependencies"):
            print()
            section("HubContentDependencies")
            for line in yaml_lines(d["HubContentDependencies"], 0):
                print(line)

        show_document("HubContentDocument", d.get("HubContentDocument"),
                      d.get("DocumentSchemaVersion"), raw=raw_doc)

        if d.get("HubContentMarkdown"):
            print()
            section("HubContentMarkdown")
            print(str(d["HubContentMarkdown"]).rstrip())

    @hub.command(
        "files", help="List what a content item actually holds, in S3",
        params=[
            arg("name", help="hub content item name",
                completer=_ContentNameCompleter()),
            _hub_flag(),
            _type_flag(),
            arg("--version", metavar="V", completer=_ContentVersionCompleter(),
                help="default: newest"),
            arg("--urls", action="store_true",
                help="also try CreateHubContentPresignedUrls (fails for DataSet; "
                     "the URLs it mints carry temporary credentials, so keep "
                     "them out of committed transcripts)"),
            arg("--max", type=int, default=100, dest="limit", metavar="N",
                help="objects per location (default 100)"),
        ],
    )
    @guard
    def _hub_files(name, hub, content_type, version, urls, limit):
        """What a content item actually consists of, listed in S3.

        There is no hub API that hands back a dataset's files:
        CreateHubContentPresignedUrls covers model hosting artifacts and fails
        on DataSet content with "Failed to parse hosting artifact URI".  So the
        real inventory has to be read from S3, at the locations the content
        document happens to carry — found by value shape, not by key name.
        This needs S3 read permission, which the hub APIs alone do not require;
        that gap is the point of showing it.
        """
        cli = sm_client()
        resolved_hub = resolve_hub(cli, hub)
        resolved_type = resolve_content_type(cli, resolved_hub, name, content_type)
        if not version:
            version = latest_version(cli, resolved_hub, resolved_type, name)[0]

        d = describe_content(cli, resolved_hub, resolved_type, name, version)
        parsed = parse_doc(d.get("HubContentDocument"))
        refs = [(p, v) for p, v in refs_in(parsed or {}) if v.startswith("s3://")]
        if not refs:
            # Not every document spells a location as one s3:// string — an
            # older schema version may carry bucket and prefix in separate
            # fields, which no shape-based scan can join without guessing key
            # names.  `describe` shows what is actually there.
            print_header(f"0 location(s) in {resolved_type} {name} "
                         f"v{d.get('HubContentVersion')}", f"hub {resolved_hub}")
            print("no s3:// value in this item's document, so there is nothing "
                  "to list by shape alone — run `describe`, the location may be "
                  "split across fields")
            return

        s3 = s3_client()

        # Distinct locations only, in document order: the same prefix is often
        # referenced more than once (data, report, per-evaluation metadata).
        seen = []
        for path, uri in refs:
            if uri not in [u for _p, u in seen]:
                seen.append((path, uri))

        print_header(f"{len(seen)} location(s) in {resolved_type} {name} "
                     f"v{d.get('HubContentVersion')}", f"hub {resolved_hub}")

        for path, uri in seen:
            bucket, key = split_s3(uri)
            section(f"{path} · {uri}")
            try:
                objects = paged(s3.list_objects_v2, "Contents", limit,
                                token_param="ContinuationToken",
                                token_key="NextContinuationToken",
                                Bucket=bucket, Prefix=key,
                                MaxKeys=min(limit, 1000))
            except botocore.exceptions.ClientError as exc:
                print(f"error: cannot list {uri}: {api_message(exc)}",
                      file=sys.stderr)
                print()
                continue
            rows = [[fmt_bytes(o.get("Size")), fmt_time(o.get("LastModified")),
                     o.get("Key") or "?"] for o in objects]
            total = sum(o.get("Size") or 0 for o in objects)
            print_header(f"{len(objects)} object(s)", fmt_bytes(total) if objects else "",
                         f"capped at --max {limit}" if len(objects) >= limit else "")
            print_table(["SIZE", "MODIFIED", "KEY"], rows)
            print()

        if urls:
            section("CreateHubContentPresignedUrls")
            params = {"HubName": resolved_hub, "HubContentType": resolved_type,
                      "HubContentName": name, "MaxResults": min(limit, 100)}
            if version:
                params["HubContentVersion"] = version
            try:
                require_operation(cli, "create_hub_content_presigned_urls",
                                  "CreateHubContentPresignedUrls")
                configs = paged(cli.create_hub_content_presigned_urls,
                                "AuthorizedUrlConfigs", limit, **params)
                # Not print_labeled: a presigned URL is hundreds of characters,
                # so it gets its own line rather than a padded column.
                for c in configs:
                    print(c.get("LocalPath") or "?")
                    print(f"  {c.get('Url')}")
            except botocore.exceptions.ClientError as exc:
                print(f"error: {api_message(exc)} — expected for DataSet "
                      "content, the operation serves model hosting artifacts",
                      file=sys.stderr)

    @hub.command(
        "trace", help="Follow ARNs between jobs, datasets and hubs",
        params=[
            arg("ref", completer=_TraceRefCompleter(),
                help="a content name, a job name, or any ARN"),
            _hub_flag(),
            arg("--depth", type=int, default=3, metavar="N",
                help="max hops (default 3)"),
            arg("--yaml", action="store_true",
                help="also print each resource's full document"),
        ],
    )
    @guard
    def _hub_trace(ref, hub, depth, yaml):
        _trace(ref, hub, depth, yaml)


# ─── trace ──────────────────────────────────────────────────────────────────

def resolve_arn(cli, arn):
    """``(kind, label, note, document, schema, identity)`` for an ARN, else None.

    *identity* is what the resource **is**, independent of how it was
    referenced, so a resource reached twice under two different ARN spellings
    is recognised as one thing.

    Dispatch is on the ARN's own resource segment, which is self-describing —
    no key names, no per-category knowledge.  Two shapes of job ARN are
    handled because the service emits both: DescribeJob returns
    ``job/<Category>/<name>``, while dataset documents refer to the same job as
    ``<some-kind>-job/<name>`` with no category in it — and the category is
    required by DescribeJob, so that form has to be probed.
    """
    parsed = split_arn(arn)
    if not parsed:
        return None
    _service, _region, _account, resource = parsed
    parts = resource.split("/")
    kind = parts[0]

    if kind == "hub-content" and len(parts) >= 5:
        a_hub, content_type, name, version = parts[1], parts[2], parts[3], parts[4]
        try:
            d = describe_content(cli, a_hub, content_type, name, version)
        except botocore.exceptions.ClientError as exc:
            return ("hub-content", f"{content_type} {name} v{version}",
                    f"unresolvable: {api_message(exc)}", None, None, None)
        label = (f"{d.get('HubContentType')} {d.get('HubContentName')} "
                 f"v{d.get('HubContentVersion')}")
        note = (f"{d.get('HubContentStatus')} · created "
                f"{fmt_time(d.get('CreationTime'))}")
        if d.get("HubContentDescription"):
            note += f"\n{d['HubContentDescription']}"
        identity = ("hub-content", d.get("HubName"), d.get("HubContentType"),
                    d.get("HubContentName"), d.get("HubContentVersion"))
        return ("hub-content", label, note, d.get("HubContentDocument"),
                d.get("DocumentSchemaVersion"), identity)

    if kind == "hub" and len(parts) >= 2:
        try:
            require_operation(cli, "describe_hub", "DescribeHub")
            d = cli.describe_hub(HubName=parts[1])
        except botocore.exceptions.ClientError as exc:
            return ("hub", parts[1], f"unresolvable: {api_message(exc)}",
                    None, None, None)
        return ("hub", d.get("HubName"),
                f"{d.get('HubDisplayName')} · {d.get('HubStatus')}", None, None,
                ("hub", d.get("HubName")))

    if kind == "job" and len(parts) >= 3:
        category, name = parts[1], parts[2]
        try:
            d = describe_job(cli, name, category)
        except botocore.exceptions.ClientError as exc:
            return ("job", f"{name} [{category}]",
                    f"unresolvable: {api_message(exc)}", None, None, None)
        return _job_node(d, category)

    if kind.endswith("-job") and len(parts) >= 2:
        name = parts[1]
        for category in job_categories():
            try:
                d = describe_job(cli, name, category)
            except botocore.exceptions.ClientError as exc:
                if error_code(exc) in NOT_FOUND_CODES:
                    continue
                break
            return _job_node(d, category, arn_kind=kind)
        return ("job", name,
                "unresolvable: no job by this name in any known category "
                f"(the ARN says '{kind}', which carries no JobCategory)",
                None, None, None)

    return None


def _job_node(desc, category, arn_kind=None):
    status = desc.get("JobStatus", "?")
    secondary = desc.get("SecondaryStatus")
    started, ended = desc.get("CreationTime"), desc.get("EndTime")
    label = f"{desc.get('JobName')} [{category}]"
    note = f"{status}" + (f" / {secondary}" if secondary else "")
    note += (f" · created {fmt_time(started)} · "
             f"{fmt_dur(elapsed_of(started, ended))}")
    if arn_kind:
        note += f"\nreferenced as '{arn_kind}/…', resolved by probing categories"
    if desc.get("FailureReason"):
        note += f"\nFailureReason: {desc['FailureReason']}"
    return ("job", label, note, desc.get("JobConfigDocument"),
            desc.get("JobConfigSchemaVersion"),
            ("job", desc.get("JobName"), category))


def start_points(cli, ref, hub):
    """The ARN(s) to start a trace from: an ARN, a content name, or a job name."""
    if ref.startswith("arn:"):
        return [ref]

    found = []
    resolved_hub = None
    try:
        resolved_hub = resolve_hub(cli, hub)
    except SmError:
        pass  # a job name is still traceable with no hub in the account
    if resolved_hub:
        summary = find_content(cli, resolved_hub, ref)
        if summary and summary.get("HubContentArn"):
            found.append(summary["HubContentArn"])

    for category in job_categories():
        try:
            d = describe_job(cli, ref, category)
        except botocore.exceptions.ClientError as exc:
            if error_code(exc) in NOT_FOUND_CODES:
                continue
            break
        if d.get("JobArn"):
            found.append(d["JobArn"])
        break

    return found


def _trace(ref, hub, depth, show_yaml):
    cli = sm_client()
    roots = start_points(cli, ref, hub)
    if not roots:
        raise SmError(f"{ref!r} is neither a hub content name, a job name, "
                      "nor an ARN")

    if len(roots) > 1:
        print(f"note: {ref!r} names more than one resource; tracing all of them\n")

    print(f"trace {ref} · region {region_label()} · max depth {depth}")
    print("each hop follows an ARN found in the parent's document; s3:// values "
          "are shown with the\nJSON path they were read from, and are never "
          "interpreted as input or output\n")

    seen = set()
    identities = {}
    # (arn, depth, via) — breadth-first, so the shortest path to a resource is
    # the one reported, and a diamond (two jobs referencing one dataset) is
    # only expanded once.
    queue = [(arn, 0, "requested") for arn in roots]
    unresolved = 0
    aliases = 0

    while queue:
        arn, at_depth, via = queue.pop(0)
        if arn in seen:
            continue
        seen.add(arn)
        pad = "  " * at_depth
        node = resolve_arn(cli, arn)

        if node is None:
            unresolved += 1
            print(f"{pad}? {arn}")
            print(f"{pad}    via {via} · no API in this shell resolves this "
                  "resource type")
            continue

        kind, label, note, doc, schema, identity = node
        print(f"{pad}{'▸' if at_depth else '●'} {kind}  {label}")
        print(f"{pad}    {arn}")

        # The same resource can be referenced under more than one ARN spelling.
        # Report that rather than expanding it twice — the aliasing is itself
        # worth seeing.
        if identity is not None and identity in identities:
            aliases += 1
            print(f"{pad}    via {via}")
            print(f"{pad}    same resource as an ARN already visited, spelled "
                  "differently:")
            print(f"{pad}      {identities[identity]}")
            print(f"{pad}    not expanded again\n")
            continue
        if identity is not None:
            identities[identity] = arn

        for line in str(note).splitlines():
            print(f"{pad}    {line}")
        if at_depth:
            print(f"{pad}    via {via}")

        parsed = parse_doc(doc) if doc else None
        if parsed is None:
            print()
            continue

        found = refs_in(parsed)
        locations = [(p, v) for p, v in found if v.startswith("s3://")]
        arns = [(p, v) for p, v in found if v.startswith("arn:")]

        if locations:
            print(f"{pad}    s3 references:")
            for path, value in locations:
                print(f"{pad}      {path}")
                print(f"{pad}        {value}")

        if show_yaml:
            show_document(f"{pad}    document", doc, schema, indent=at_depth + 3)

        nxt = [(v, p) for p, v in arns if v not in seen]
        if nxt and at_depth >= depth:
            print(f"{pad}    {len(nxt)} further reference(s) not followed "
                  f"(--depth {depth} reached)")
        else:
            for value, path in nxt:
                queue.append((value, at_depth + 1, f"{kind} {label} · {path}"))
        print()

    print(f"{len(seen)} reference(s) visited · {len(identities)} distinct "
          "resource(s)"
          + (f" · {aliases} duplicate ARN spelling(s)" if aliases else "")
          + (f" · {unresolved} unresolvable" if unresolved else ""))
