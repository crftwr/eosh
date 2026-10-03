"""``awsut bedrock-agentcore harness`` — Harness resources at the service-API level.

Talks to ListHarnesses / GetHarness / ListHarnessVersions / ListHarnessEndpoints
/ GetHarnessEndpoint / DeleteHarness through the AgentCore control plane, so what
you see is the raw service response rather than an SDK rendering of it.

Read-only apart from ``delete``.  Creating and updating a harness means
authoring the ``model`` / ``tools`` / ``skills`` structures GetHarness prints
here, which is a file's worth of JSON rather than a line of flags — see
``doc/enhancements.md``.

Shape of the group:

* **A harness is named twice.**  ``harnessName`` is what a person remembers;
  ``harnessId`` is ``<name>-<10 chars>`` and is what every API takes.  Every
  leaf accepts either (see :func:`resolve_harness`) and every listing prints
  both, so a cell can be copied straight into the next command.
* ``describe`` prints **every member the response carries**, not a curated
  subset: the scalars as a labeled block, then one section per structural
  member (``model``, ``tools``, ``skills``, ``memory``, …) rendered as indented
  YAML-ish lines.  Those structures are the harness — hiding them behind a flag
  would hide the answer — and because nothing here knows a key name, a member
  this botocore has never heard of still prints.  ``--raw`` gives the whole API
  response as JSON.
* ``versions`` and ``endpoints`` are separate leaves rather than sections of
  ``describe``, because each is its own paginated API call and is worth asking
  for on its own.  A version and an endpoint carry a ``failureReason``, which
  is printed below the table for the rows that have one.
* ``watch`` prints only on change, so a create or a delete leaves a readable
  timeline rather than a repainted frame, with a ``.`` heartbeat every 5s while
  nothing moves.  ``Ctrl+C`` detaches.
* A successful delete ends with the harness **absent**, not with a status, so
  the watch loop treats "gone" as a terminal state — which is what makes it
  serve ``delete --wait`` as well.

``ListHarnesses`` takes no filters of its own (only ``maxResults`` /
``nextToken``), so ``list``'s ``--status`` and ``--contains`` are applied here in
the shell, after the fetch.  That interacts with ``--max``, and the note under
the table says so when it does.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

import botocore.exceptions

from ...commands import arg
from ...completion import ChoiceCompleter, Completer, Completion, CompletionContext
from ...completion_cache import get_or_fetch
from ...shell import passthrough_input
from .. import awsut
from .render import (
    DOT_INTERVAL,
    NOT_FOUND_CODES,
    REASON,
    Heartbeat,
    SmError,
    TERMINAL,
    api_message,
    cache_key,
    control_client,
    error_code,
    fmt_dur,
    fmt_time,
    guard,
    mark_for,
    paged,
    positionals,
    print_header,
    print_labeled,
    print_reasons,
    print_table,
    region_label,
    render_detail,
    require_input_member,
    require_operation,
    section,
    sleep_with_dots,
    statuses,
    version_key,
)

# How many harnesses a name lookup will page through before giving up.  A name
# can only be turned into an id by listing, so this bounds the cost of accepting
# one; an id needs no listing at all.
RESOLVE_MAX = 500

# Value-taking flags across the ``harness`` leaves, so :func:`positionals` can
# tell a flag's value from a positional in a completion context — in
# ``describe --version 3 <TAB>``, ``3`` is not the harness name being completed.
VALUE_FLAGS = ("--version", "--status", "--contains", "--max",
               "-n", "--interval", "--timeout")

# The labeled block a ``describe`` opens with: the API's own member names, in a
# reading order rather than the model's, with ``None`` marking a group break.
# Members absent from this list are printed too (see ``render.render_detail``) —
# it is a layout preference, not a filter.
DETAIL_SCALARS = (
    "harnessName", "harnessId", "arn", "harnessVersion", "executionRoleArn",
    None,
    "status", "createdAt", "updatedAt",
    None,
    "maxIterations", "maxTokens", "timeoutSeconds",
)


def harness_statuses() -> list[str]:
    """The status words a harness can report, for ``--status`` completion."""
    return list(statuses("ListHarnesses", "harnesses"))


# ─── API calls ──────────────────────────────────────────────────────────────

def fetch_harnesses(cli, limit=RESOLVE_MAX) -> list[dict]:
    require_operation(cli, "list_harnesses", "ListHarnesses")
    return paged(cli.list_harnesses, "harnesses", limit,
                 token_param="nextToken", token_key="nextToken",
                 maxResults=min(limit, 100))


def resolve_harness(cli, selector: str) -> dict:
    """The ListHarnesses summary for a harness named by name or by id.

    Both are accepted because the id is ``<name>-<10 chars>``: the name is what
    a person remembers and the id is what the API takes, and every listing here
    prints them side by side.  Matched against the id first, so a value copied
    out of ``list`` always resolves to itself even if some *other* harness is
    named that.

    A name is resolved by listing — there is no lookup-by-name API — so this
    costs one paginated call.  Not found names what does exist, since the usual
    cause is a typo or the wrong region.
    """
    found = fetch_harnesses(cli)
    for h in found:
        if h.get("harnessId") == selector:
            return h
    by_name = [h for h in found if h.get("harnessName") == selector]
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        ids = ", ".join(h.get("harnessId") or "?" for h in by_name)
        raise SmError(f"{len(by_name)} harnesses are named {selector!r} — "
                      f"name one by id: {ids}")
    known = sorted(h.get("harnessName") or "?" for h in found)
    listed = ", ".join(known[:10]) + (" …" if len(known) > 10 else "")
    raise SmError(f"no harness named {selector!r} in region {region_label()}"
                  + (f" — there is: {listed}" if known
                     else " — this region has no harnesses"))


def get_harness(cli, harness_id: str, version: str | None = None) -> dict:
    require_operation(cli, "get_harness", "GetHarness")
    params = {"harnessId": harness_id}
    if version:
        params["harnessVersion"] = version
    return cli.get_harness(**params)


def label_of(summary: dict) -> str:
    """``name (id)`` — how a harness is named in a header or a prompt."""
    return f"{summary.get('harnessName') or '?'} ({summary.get('harnessId') or '?'})"


# ─── completers ─────────────────────────────────────────────────────────────

def _cached_harnesses() -> list[dict]:
    """The region's harnesses, fetched once per typed token rather than per keystroke.

    TAB re-runs a completer on every keystroke while the picker is open, so the
    cache (TTL, plus invalidated after every command) is what keeps one ListHarnesses
    per token instead of one per character.
    """
    return get_or_fetch(cache_key("harnesses"),
                        lambda: fetch_harnesses(control_client()))


def _completion_harness_id(selector: str) -> str | None:
    """A harness id for an already-typed name or id, or None if unknown.

    Completion never probes the API to resolve a name: the cached listing has
    both spellings, and a selector it doesn't know simply yields no candidates.
    """
    for h in _cached_harnesses():
        if selector in (h.get("harnessId"), h.get("harnessName")):
            return h.get("harnessId")
    return None


class _HarnessCompleter(Completer):
    """Harness names — or ids, once what's typed can only be one.

    Names by default, since that is what a person types.  But an id *starts*
    with its harness's name (``<name>-<10 chars>``), so the moment the prefix
    stops matching any name it can only be someone spelling an id out, and this
    switches to offering those instead of going blank.
    """

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            found = _cached_harnesses()
        except Exception:
            return []
        by_name = [h for h in found
                   if (h.get("harnessName") or "").startswith(ctx.prefix)]
        if by_name:
            return [
                Completion(value=h["harnessName"],
                           fields=(h.get("harnessId") or "",
                                   h.get("status") or "",
                                   f"v{h.get('harnessVersion')}"
                                   if h.get("harnessVersion") else ""))
                for h in by_name if h.get("harnessName")
            ]
        return [
            Completion(value=h["harnessId"],
                       fields=(h.get("status") or "",
                               fmt_time(h.get("createdAt"))))
            for h in found
            if ctx.prefix and (h.get("harnessId") or "").startswith(ctx.prefix)
        ]


class _HarnessVersionCompleter(Completer):
    """Versions of the harness already named in this line, newest first."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        typed = positionals(ctx.args, VALUE_FLAGS)
        if not typed:
            return []               # no harness named yet — nothing to scope to
        try:
            rows = get_or_fetch(
                cache_key("harness_versions", typed[0]),
                lambda: _versions_for_completion(typed[0]),
            )
        except Exception:
            return []
        return [
            Completion(value=str(v["harnessVersion"]),
                       fields=(v.get("status") or "",
                               fmt_time(v.get("createdAt"))))
            for v in rows
            if str(v.get("harnessVersion") or "").startswith(ctx.prefix)
        ]


def _versions_for_completion(selector: str) -> list[dict]:
    harness_id = _completion_harness_id(selector)
    if not harness_id:
        return []
    cli = control_client()
    return sorted(fetch_versions(cli, harness_id, 100),
                  key=lambda v: version_key(v.get("harnessVersion")), reverse=True)


class _EndpointCompleter(Completer):
    """Endpoints of the harness already named in this line."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        typed = positionals(ctx.args, VALUE_FLAGS)
        if not typed:
            return []
        try:
            rows = get_or_fetch(
                cache_key("harness_endpoints", typed[0]),
                lambda: _endpoints_for_completion(typed[0]),
            )
        except Exception:
            return []
        return [
            Completion(value=e["endpointName"],
                       fields=(e.get("status") or "",
                               f"live v{e['liveVersion']}"
                               if e.get("liveVersion") else "",
                               e.get("description") or ""))
            for e in rows
            if (e.get("endpointName") or "").startswith(ctx.prefix)
        ]


def _endpoints_for_completion(selector: str) -> list[dict]:
    harness_id = _completion_harness_id(selector)
    if not harness_id:
        return []
    return fetch_endpoints(control_client(), harness_id, 100)


def fetch_versions(cli, harness_id: str, limit: int) -> list[dict]:
    require_operation(cli, "list_harness_versions", "ListHarnessVersions")
    return paged(cli.list_harness_versions, "harnessVersions", limit,
                 token_param="nextToken", token_key="nextToken",
                 harnessId=harness_id, maxResults=min(limit, 100))


def fetch_endpoints(cli, harness_id: str, limit: int) -> list[dict]:
    require_operation(cli, "list_harness_endpoints", "ListHarnessEndpoints")
    return paged(cli.list_harness_endpoints, "endpoints", limit,
                 token_param="nextToken", token_key="nextToken",
                 harnessId=harness_id, maxResults=min(limit, 100))


def _harness_param(help_text: str):
    """The ``HARNESS`` positional every leaf takes, spelled the same way."""
    return arg("harness", metavar="HARNESS", completer=_HarnessCompleter(),
               help=help_text)


# ─── command tree ───────────────────────────────────────────────────────────

def register_harness(agentcore) -> None:
    harness = agentcore.command(
        "harness",
        help="Harness resources — ListHarnesses / GetHarness / "
             "ListHarnessVersions / ListHarnessEndpoints / DeleteHarness "
             "(HARNESS is a harness name or a harness id)",
    )

    @harness.command(
        "list",
        help="List the region's harnesses, newest first "
             "(--status and --contains are applied in the shell after the "
             "fetch — ListHarnesses takes no filters of its own)",
        params=[
            arg("--status", metavar="STATUS",
                completer=ChoiceCompleter(harness_statuses()),
                help="only harnesses in this status"),
            arg("--contains", metavar="TEXT",
                help="only harnesses whose name or id contains TEXT"),
            arg("--max", type=int, default=50, dest="limit", metavar="N",
                help="cap on harnesses fetched (default 50)"),
            arg("--asc", action="store_true", help="oldest first"),
        ],
    )
    @guard
    def _harness_list(status, contains, limit, asc):
        cli = control_client()
        rows = fetch_harnesses(cli, limit)
        fetched = len(rows)
        if status:
            rows = [h for h in rows if h.get("status") == status]
        if contains:
            rows = [h for h in rows
                    if contains in (h.get("harnessName") or "")
                    or contains in (h.get("harnessId") or "")]
        hidden = fetched - len(rows)
        rows.sort(key=_created_key, reverse=not asc)

        cells = [[
            mark_for(h.get("status") or ""),
            h.get("harnessName") or "?",
            h.get("harnessId") or "?",
            h.get("status") or "?",
            str(h.get("harnessVersion") or "-"),
            fmt_time(h.get("createdAt")),
            fmt_time(h.get("updatedAt")),
        ] for h in rows]

        print_header(f"{len(rows)} harness(es)", f"region {region_label()}")

        notes = []
        if hidden:
            notes.append(f"{hidden} fetched harness(es) hidden by the filters")
        # --max bounds the *fetch*, and the filters ran after it, so a short
        # filtered listing must not read as "that is all there is".
        if fetched >= limit:
            notes.append(f"--max {limit} reached, so there may be more"
                         + (" that the filters never saw" if status or contains
                            else "") + "; raise --max to fetch further")

        print_table(["", "HARNESS NAME", "HARNESS ID", "STATUS", "VERSION",
                     "CREATED", "UPDATED"],
                    cells, note="\n".join(notes) or None)

    @harness.command(
        "describe",
        help="Describe one harness, including every structural member of the "
             "response (model, tools, skills, memory, …) rendered as indented "
             "lines",
        params=[
            _harness_param("harness to describe"),
            arg("--version", metavar="V", default=None,
                completer=_HarnessVersionCompleter(),
                help="a specific harnessVersion (default: the current one)"),
            arg("--raw", action="store_true",
                help="Show the raw API response as JSON"),
        ],
    )
    @guard
    def _harness_describe(harness, version, raw):
        cli = control_client()
        summary = resolve_harness(cli, harness)
        resp = get_harness(cli, summary["harnessId"], version)
        if raw:
            awsut._print_json(resp)
            return
        render_detail(resp.get("harness") or {}, DETAIL_SCALARS)

    @harness.command(
        "versions",
        help="List a harness's versions, newest first",
        params=[
            _harness_param("harness whose versions to list"),
            arg("--max", type=int, default=50, dest="limit", metavar="N",
                help="cap on versions fetched (default 50)"),
        ],
    )
    @guard
    def _harness_versions(harness, limit):
        cli = control_client()
        summary = resolve_harness(cli, harness)
        rows = fetch_versions(cli, summary["harnessId"], limit)
        rows.sort(key=lambda v: version_key(v.get("harnessVersion")), reverse=True)

        cells = [[
            mark_for(v.get("status") or ""),
            str(v.get("harnessVersion") or "?"),
            v.get("status") or "?",
            fmt_time(v.get("createdAt")),
            fmt_time(v.get("updatedAt")),
        ] for v in rows]

        current = summary.get("harnessVersion")
        print_header(f"{len(rows)} version(s)", f"harness {label_of(summary)}",
                     f"current v{current}" if current else "",
                     f"region {region_label()}")
        print_table(["", "VERSION", "STATUS", "CREATED", "UPDATED"], cells,
                    note=(f"--max {limit} reached, so there may be more"
                          if len(rows) >= limit else None))
        print_reasons(rows, lambda v: f"v{v.get('harnessVersion')}")

    @harness.command(
        "endpoints",
        help="List a harness's endpoints, or describe one of them",
        params=[
            _harness_param("harness whose endpoints to list"),
            arg("endpoint", nargs="?", default=None, metavar="ENDPOINT",
                completer=_EndpointCompleter(),
                help="one endpoint to describe (default: list them all)"),
            arg("--max", type=int, default=50, dest="limit", metavar="N",
                help="cap on endpoints fetched (default 50)"),
            arg("--raw", action="store_true",
                help="Show the raw API response as JSON (with ENDPOINT)"),
        ],
    )
    @guard
    def _harness_endpoints(harness, endpoint, limit, raw):
        cli = control_client()
        summary = resolve_harness(cli, harness)
        if endpoint:
            require_operation(cli, "get_harness_endpoint", "GetHarnessEndpoint")
            resp = cli.get_harness_endpoint(harnessId=summary["harnessId"],
                                            endpointName=endpoint)
            if raw:
                awsut._print_json(resp)
                return
            _render_endpoint(resp.get("endpoint") or {})
            return

        rows = fetch_endpoints(cli, summary["harnessId"], limit)
        cells = [[
            mark_for(e.get("status") or ""),
            e.get("endpointName") or "?",
            e.get("status") or "?",
            str(e.get("liveVersion") or "-"),
            str(e.get("targetVersion") or "-"),
            fmt_time(e.get("createdAt")),
            fmt_time(e.get("updatedAt")),
            e.get("description") or "",
        ] for e in rows]
        print_header(f"{len(rows)} endpoint(s)", f"harness {label_of(summary)}",
                     f"region {region_label()}")
        print_table(["", "ENDPOINT", "STATUS", "LIVE", "TARGET", "CREATED",
                     "UPDATED", "DESCRIPTION"], cells,
                    note=(f"--max {limit} reached, so there may be more"
                          if len(rows) >= limit else None))
        print_reasons(rows, lambda e: e.get("endpointName") or "?")

    @harness.command(
        "watch",
        help="Poll a harness and log its status changes (Ctrl+C detaches); a "
             "deleted harness reports as gone rather than as an error",
        params=[
            _harness_param("harness to watch"),
            arg("-n", "--interval", type=float, default=10.0, metavar="SEC",
                help="poll seconds (default 10)"),
            arg("--timeout", type=float, default=0.0, metavar="SEC",
                help="give up after this long; 0 = no limit"),
            arg("--endpoints", action="store_true", dest="with_endpoints",
                help="also report each endpoint's status changes, and wait for "
                     "them to settle too"),
        ],
    )
    @guard
    def _harness_watch(harness, interval, timeout, with_endpoints):
        cli = control_client()
        summary = resolve_harness(cli, harness)
        harness_id = summary["harnessId"]

        print(f"watching {summary.get('harnessName')}")
        print(f"  {summary.get('arn')}")
        print(f"  id {harness_id} · region {region_label()} · poll {interval}s · "
              f"heartbeat {DOT_INTERVAL}s")
        if with_endpoints:
            print("  also watching its endpoints")
        print("  Ctrl+C to detach\n")

        _watch_harness(cli, summary, interval, timeout,
                       with_endpoints=with_endpoints,
                       reattach=f"awsut bedrock-agentcore harness watch "
                                f"{harness_id}"
                                + (" --endpoints" if with_endpoints else ""))

    @harness.command(
        "delete",
        help="Delete a harness — every version and endpoint of it goes too, "
             "and this cannot be undone",
        params=[
            _harness_param("harness to delete"),
            arg("-y", "--yes", action="store_true", help="Skip confirmation"),
            arg("--delete-managed-memory", action="store_true",
                dest="delete_memory",
                help="also delete the memory the service manages for this "
                     "harness, discarding everything it has stored (default: "
                     "the memory is left in place)"),
            arg("--wait", action="store_true",
                help="poll until the harness is gone"),
            arg("-n", "--interval", type=float, default=10.0, metavar="SEC",
                help="--wait poll seconds (default 10)"),
        ],
    )
    @guard
    def _harness_delete(harness, yes, delete_memory, wait, interval):
        cli = control_client()
        require_operation(cli, "delete_harness", "DeleteHarness")
        if delete_memory:
            require_input_member("DeleteHarness", "deleteManagedMemory",
                                 "--delete-managed-memory cannot be honoured")
        summary = resolve_harness(cli, harness)
        harness_id = summary["harnessId"]
        status = summary.get("status") or "?"
        if status == "DELETING":
            print(f"harness is already {status}; nothing to delete")
            return

        if not yes:
            # Spell out what goes with it: the endpoints are what callers point
            # at, so their count is the part that decides whether this is safe.
            endpoints = fetch_endpoints(cli, harness_id, 100)
            extra = (", AND the managed memory with everything it has stored"
                     if delete_memory else "")
            try:
                answer = passthrough_input(
                    f"Delete harness {label_of(summary)} — currently {status}, "
                    f"with {len(endpoints)} endpoint(s){extra}? "
                    "This cannot be undone. [y/N] : ")
            except RuntimeError:
                # No terminal to prompt on (piped / decorator body).
                raise SmError(
                    "cannot confirm without a terminal — re-run with -y/--yes")
            if answer.strip().lower() not in ("y", "yes"):
                print("not deleted")
                return

        params = {"harnessId": harness_id}
        if delete_memory:
            params["deleteManagedMemory"] = True
        cli.delete_harness(**params)
        print(f"DeleteHarness sent for {label_of(summary)}")

        if wait:
            print(f"  polling every {interval}s · heartbeat {DOT_INTERVAL}s · "
                  "Ctrl+C to detach\n")
            _watch_harness(cli, summary, interval, 0.0,
                           reattach=f"awsut bedrock-agentcore harness watch "
                                    f"{harness_id}")


def _created_key(row):
    return row.get("createdAt") or datetime.min.replace(tzinfo=timezone.utc)


# ─── rendering ──────────────────────────────────────────────────────────────

def _render_endpoint(e) -> None:
    print_labeled([
        ("endpointName", e.get("endpointName")),
        ("arn", e.get("arn")),
        ("harnessName", e.get("harnessName")),
        ("harnessId", e.get("harnessId")),
        None,
        ("status", e.get("status") or "?"),
        ("liveVersion", e.get("liveVersion")),
        ("targetVersion", e.get("targetVersion")),
        ("createdAt", fmt_time(e.get("createdAt"))),
        ("updatedAt", fmt_time(e.get("updatedAt"))),
        None,
        ("description", e.get("description")),
    ])
    if e.get(REASON):
        print()
        section(REASON)
        print(e[REASON])


# ─── watch ──────────────────────────────────────────────────────────────────

def _watch_harness(cli, summary, interval, timeout, *, with_endpoints=False,
                   reattach=None) -> None:
    """Poll one harness until it settles, printing only what changed.

    "Settled" means a terminal status *or* gone: a successful delete leaves no
    resource to report a status, so a ``NOT_FOUND`` answer is the end of a
    ``DELETING`` timeline rather than an error — which is also what lets
    ``delete --wait`` reuse this loop.

    With *with_endpoints*, an endpoint's status changes are reported too and the
    loop waits for them as well: a harness reaching ``READY`` while its
    endpoints are still ``UPDATING`` is not yet usable.
    """
    harness_id = summary["harnessId"]
    name = summary.get("harnessName") or harness_id
    require_operation(cli, "get_harness", "GetHarness")
    if with_endpoints:
        require_operation(cli, "list_harness_endpoints", "ListHarnessEndpoints")

    beat = Heartbeat()
    deadline = time.monotonic() + timeout if timeout else None
    seen = None            # last (status, failureReason) printed
    seen_eps: dict[str, tuple] = {}
    status = summary.get("status") or "?"

    while True:
        stamp = datetime.now().strftime("%H:%M:%S")
        try:
            found = get_harness(cli, harness_id).get("harness") or {}
        except botocore.exceptions.ClientError as exc:
            if error_code(exc) in NOT_FOUND_CODES:
                beat.clear()
                print(f"\n[{stamp}] gone — {name} no longer exists")
                return
            beat.clear()
            print(f"error: poll failed: {api_message(exc)}", file=sys.stderr)
            found = None

        if found is not None:
            status = found.get("status") or "?"
            state = (status, found.get(REASON) or "")
            if state != seen:
                beat.clear()
                print(f"[{stamp}] {mark_for(status)} status {status}")
                for line in state[1].splitlines():
                    print(f"           {line}")
                seen = state

            if with_endpoints:
                _report_endpoints(cli, harness_id, seen_eps, stamp, beat)

            settled = status in TERMINAL and (
                not with_endpoints
                or all(s[0] in TERMINAL for s in seen_eps.values()))
            if settled:
                beat.clear()
                print(f"\n[{stamp}] terminal: {status}")
                return

        if deadline and time.monotonic() > deadline:
            beat.clear()
            print(f"\n[{stamp}] timeout after {fmt_dur(timeout)}; "
                  f"harness still {status}")
            return

        try:
            sleep_with_dots(interval, beat)
        except KeyboardInterrupt:
            beat.clear()
            print(f"\ndetached; harness is still {status}.")
            if reattach:
                print(f"Re-attach with:\n  {reattach}")
            return


def _report_endpoints(cli, harness_id, seen, stamp, beat) -> None:
    """Report every endpoint that appeared, changed, or went away."""
    try:
        rows = fetch_endpoints(cli, harness_id, 100)
    except botocore.exceptions.ClientError as exc:
        beat.clear()
        print(f"error: endpoint poll failed: {api_message(exc)}", file=sys.stderr)
        return

    live = set()
    for e in rows:
        name = e.get("endpointName")
        if not name:
            continue
        live.add(name)
        state = (e.get("status") or "?", e.get("liveVersion"),
                 e.get("targetVersion"))
        before = seen.get(name)
        if before == state:
            continue
        seen[name] = state
        beat.clear()
        detail = f"live v{state[1] or '-'} target v{state[2] or '-'}"
        if before is None:
            print(f"[{stamp}]   {mark_for(state[0])} endpoint {name}  "
                  f"{state[0]}  {detail}")
        else:
            print(f"[{stamp}]   {mark_for(state[0])} endpoint {name}  "
                  f"{before[0]} → {state[0]}  {detail}")
        if e.get(REASON):
            for line in e[REASON].splitlines():
                print(f"           {line}")

    for name in [n for n in seen if n not in live]:
        seen.pop(name)
        beat.clear()
        print(f"[{stamp}]   - endpoint {name} is gone")
