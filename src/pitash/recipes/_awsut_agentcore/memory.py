"""``awsut bedrock-agentcore memory`` — Memory resources, and what they hold.

A memory is the one AgentCore resource that lives on **both planes**, and the
group is shaped by that:

* The **control plane** (``bedrock-agentcore-control``) owns the resource — its
  strategies, its encryption, its event-expiry window.  That is ``list``,
  ``describe``, ``strategies``, ``watch``, ``delete``.
* The **data plane** (``bedrock-agentcore``) owns everything *inside* it, as a
  four-level hierarchy: a memory has **actors** (who is talking), an actor has
  **sessions** (one conversation each), a session has **events** (the turns),
  and out of those the service extracts **records** (what it decided is worth
  remembering).  That is ``actors``, ``sessions``, ``events``, ``event``,
  ``records``, ``record``, ``search`` and ``jobs``.

They are one group rather than two because "memory" and "the conversation stored
in a memory" are one thing to the person asking, and because every data-plane
call is scoped by a memory id anyway — so every leaf here opens with the same
``MEMORY`` positional.  It is the arrangement ``awsut sagemaker studio`` already
uses for domain → space → app: one group, a plural-named leaf per level of the
hierarchy, each taking the levels above it as positionals.

**"Session" here means a memory session** — a conversation held in a memory.
AgentCore also has browser sessions and code-interpreter sessions, which belong
to browser and code-interpreter resources; those resources are not in this tree
yet, and when they arrive their sessions belong to their own groups.

Two things about naming, both consequences of what the API returns:

* **``ListMemories`` does not return the name**, only the id.  But the id is
  ``<name>-<10 chars>`` and a memory name cannot contain ``-``, so the name is
  exactly the id up to its last dash — see :func:`name_of`.  Every listing shows
  it and every leaf accepts either spelling (:func:`resolve_memory_id`), with
  the name column marked as derived in ``list``'s ``help``.
* **An id-shaped selector costs no lookup.**  A name has to be resolved by
  listing, since there is no lookup-by-name API; an id is passed straight
  through, so pasting a cell out of ``list`` is one call rather than two.

Read-only apart from ``delete``, which deletes the *resource*.  Deleting
individual events and records (``DeleteEvent`` / ``DeleteMemoryRecord``) and
writing turns into a memory (``CreateEvent``) are deliberately not here — see
``doc/enhancements.md``.
"""

from __future__ import annotations

import re
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
    CONTROL_SERVICE,
    DATA_SERVICE,
    DOT_INTERVAL,
    NOT_FOUND_CODES,
    REASON,
    Heartbeat,
    SmError,
    TERMINAL,
    api_message,
    cache_key,
    control_client,
    data_client,
    error_code,
    fmt_dur,
    fmt_time,
    guard,
    mark_for,
    model_enum,
    paged,
    positionals,
    print_header,
    print_labeled,
    print_reasons,
    print_table,
    region_label,
    render_detail,
    require_operation,
    section,
    sleep_with_dots,
    split_arn,
    statuses,
    yaml_lines,
)

# How many memories a name lookup will page through before giving up.  A name can
# only be turned into an id by listing, so this bounds the cost of accepting one.
RESOLVE_MAX = 500

# ListMemoryExtractionJobs caps maxResults at 50 where every other list call here
# caps it at 100, so the per-call ceiling is a parameter rather than a constant.
PAGE_MAX = 100
JOB_PAGE_MAX = 50

# Value-taking flags across the ``memory`` leaves, so :func:`positionals` can tell
# a flag's value from a positional in a completion context — in
# ``sessions --max 5 <TAB>``, ``5`` is not the memory being completed.
VALUE_FLAGS = ("--status", "--contains", "--max", "-n", "--interval",
               "--timeout", "--view", "--namespace", "--namespace-path",
               "--strategy", "--branch", "--top-k", "--actor", "--session")

# The labeled block ``describe`` opens with: the API's own member names, in a
# reading order rather than the model's, with ``None`` marking a group break.
# Members absent from this list are printed too (see ``render.render_detail``) —
# it is a layout preference, not a filter.
DETAIL_SCALARS = (
    "name", "id", "arn",
    None,
    "status", "createdAt", "updatedAt",
    None,
    "eventExpiryDuration", "memoryExecutionRoleArn", "encryptionKeyArn",
    None,
    "description", "managedByResourceArn",
)

# A memory id is ``<name>-<10 chars>`` and a memory *name* matches
# ``[a-zA-Z][a-zA-Z0-9_]{0,47}`` — no dash.  So this tail is what tells the two
# spellings apart with no API call, which is what lets an id skip the lookup.
ID_TAIL = re.compile(r"-[a-zA-Z0-9]{10}$")


def memory_statuses() -> list[str]:
    """The status words a memory can report, for ``--status`` completion."""
    return list(statuses("ListMemories", "memories"))


def memory_views() -> list[str]:
    """GetMemory's ``view`` enum, for ``--view`` completion."""
    return list(model_enum(CONTROL_SERVICE, "GetMemory", "view"))


# ─── names and ids ──────────────────────────────────────────────────────────

def name_of(memory_id: str) -> str:
    """A memory's name, recovered from its id.

    ``ListMemories`` returns no name, so the alternative to deriving it is a
    GetMemory per row.  The id is ``<name>-<10 chars>`` and a name cannot contain
    a dash, so the split is unambiguous — and an id that doesn't have that shape
    is returned whole rather than mangled.
    """
    return ID_TAIL.sub("", memory_id) if ID_TAIL.search(memory_id or "") \
        else (memory_id or "?")


def memory_label(memory_id: str) -> str:
    """``name (id)`` — how a memory is named in a header or a prompt."""
    return f"{name_of(memory_id)} ({memory_id})"


def _id_from_arn(selector: str) -> str | None:
    """The memory id inside a memory ARN, or None if it isn't one.

    The data plane accepts an ARN where the control plane wants a bare id, so a
    value copied out of ``describe`` works in both places only if this normalises
    it once, up front.
    """
    parts = split_arn(selector)
    if not parts:
        return None
    resource = parts[3]
    return resource.split("/")[-1] or None


# ─── control-plane calls ────────────────────────────────────────────────────

def fetch_memories(cli, limit=RESOLVE_MAX) -> list[dict]:
    require_operation(cli, "list_memories", "ListMemories")
    return paged(cli.list_memories, "memories", limit,
                 token_param="nextToken", token_key="nextToken",
                 maxResults=min(limit, PAGE_MAX))


def get_memory(cli, memory_id: str, view: str | None = None) -> dict:
    require_operation(cli, "get_memory", "GetMemory")
    params = {"memoryId": memory_id}
    if view:
        params["view"] = view
    return cli.get_memory(**params)


def resolve_memory_id(cli, selector: str) -> str:
    """A memory id for a selector given as an id, a name, or an ARN.

    An id (or an ARN carrying one) is passed straight through — no listing, no
    validation: the API's own ``ValidationException`` for a malformed id already
    reads correctly through ``guard``, and paying a paginated ListMemories to
    pre-empt it would tax the common case for the rare one.

    A *name* has no lookup API, so it costs one paginated listing.  Ambiguity and
    not-found both name what does exist, since the usual cause is a typo or the
    wrong region.
    """
    ident = _id_from_arn(selector) or selector
    if ID_TAIL.search(ident):
        return ident

    found = fetch_memories(cli)
    matches = [m["id"] for m in found
               if m.get("id") and name_of(m["id"]) == selector]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise SmError(f"{len(matches)} memories are named {selector!r} — "
                      f"name one by id: {', '.join(matches)}")
    known = sorted({name_of(m["id"]) for m in found if m.get("id")})
    listed = ", ".join(known[:10]) + (" …" if len(known) > 10 else "")
    raise SmError(f"no memory named {selector!r} in region {region_label()}"
                  + (f" — there is: {listed}" if known
                     else " — this region has no memories"))


# ─── data-plane calls ───────────────────────────────────────────────────────

def fetch_actors(dat, memory_id: str, limit: int) -> list[dict]:
    require_operation(dat, "list_actors", "ListActors")
    return paged(dat.list_actors, "actorSummaries", limit,
                 token_param="nextToken", token_key="nextToken",
                 memoryId=memory_id, maxResults=min(limit, PAGE_MAX))


def fetch_sessions(dat, memory_id: str, actor_id: str, limit: int,
                   has_events: bool = False) -> list[dict]:
    require_operation(dat, "list_sessions", "ListSessions")
    params = {"memoryId": memory_id, "actorId": actor_id,
              "maxResults": min(limit, PAGE_MAX)}
    if has_events:
        params["filter"] = {"eventFilter": "HAS_EVENTS"}
    return paged(dat.list_sessions, "sessionSummaries", limit,
                 token_param="nextToken", token_key="nextToken", **params)


def fetch_events(dat, memory_id: str, actor_id: str, session_id: str,
                 limit: int, payloads: bool = False,
                 branch: str | None = None) -> list[dict]:
    require_operation(dat, "list_events", "ListEvents")
    params = {"memoryId": memory_id, "actorId": actor_id,
              "sessionId": session_id, "maxResults": min(limit, PAGE_MAX)}
    if payloads:
        params["includePayloads"] = True
    if branch:
        params["filter"] = {"branch": {"name": branch,
                                       "includeParentBranches": True}}
    return paged(dat.list_events, "events", limit,
                 token_param="nextToken", token_key="nextToken", **params)


def fetch_records(dat, memory_id: str, limit: int, namespace: str | None = None,
                  namespace_path: str | None = None,
                  strategy: str | None = None) -> list[dict]:
    require_operation(dat, "list_memory_records", "ListMemoryRecords")
    params = {"memoryId": memory_id, "maxResults": min(limit, PAGE_MAX)}
    if namespace:
        params["namespace"] = namespace
    if namespace_path:
        params["namespacePath"] = namespace_path
    if strategy:
        params["memoryStrategyId"] = strategy
    return paged(dat.list_memory_records, "memoryRecordSummaries", limit,
                 token_param="nextToken", token_key="nextToken", **params)


def fetch_jobs(dat, memory_id: str, limit: int, **filters) -> list[dict]:
    require_operation(dat, "list_memory_extraction_jobs", "ListMemoryExtractionJobs")
    params = {"memoryId": memory_id, "maxResults": min(limit, JOB_PAGE_MAX)}
    kept = {k: v for k, v in filters.items() if v}
    if kept:
        params["filter"] = kept
    return paged(dat.list_memory_extraction_jobs, "jobs", limit,
                 token_param="nextToken", token_key="nextToken", **params)


# ─── completers ─────────────────────────────────────────────────────────────

def _cached_memories() -> list[dict]:
    """The region's memories, fetched once per typed token, not per keystroke.

    TAB re-runs a completer on every keystroke while the picker is open, so the
    cache (TTL, plus invalidated after every command) is what keeps one
    ListMemories per token instead of one per character.
    """
    return get_or_fetch(cache_key("memories"),
                        lambda: fetch_memories(control_client()))


def _completion_memory_id(selector: str) -> str | None:
    """A memory id for an already-typed selector, or None if unknown.

    Completion never probes the API to resolve a name: an id-shaped selector is
    taken at face value, a name is matched against the cached listing, and
    anything else simply yields no candidates.
    """
    ident = _id_from_arn(selector) or selector
    if ID_TAIL.search(ident or ""):
        return ident
    for m in _cached_memories():
        if m.get("id") and name_of(m["id"]) == selector:
            return m["id"]
    return None


class _MemoryCompleter(Completer):
    """Memory names — or ids, once what's typed can only be one.

    Names by default, since that is what a person types.  But an id *starts* with
    its memory's name (``<name>-<10 chars>``), so the moment the prefix stops
    matching any name it can only be someone spelling an id out, and this
    switches to offering those instead of going blank.
    """

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            found = [m for m in _cached_memories() if m.get("id")]
        except Exception:
            return []
        by_name = [m for m in found if name_of(m["id"]).startswith(ctx.prefix)]
        if by_name:
            return [
                Completion(value=name_of(m["id"]),
                           fields=(m["id"], m.get("status") or "",
                                   fmt_time(m.get("createdAt"))))
                for m in by_name
            ]
        return [
            Completion(value=m["id"],
                       fields=(m.get("status") or "",
                               fmt_time(m.get("createdAt"))))
            for m in found
            if ctx.prefix and m["id"].startswith(ctx.prefix)
        ]


class _ScopedCompleter(Completer):
    """Base for the completers that narrow by the positionals already typed.

    Every data-plane leaf here names its scope as leading positionals (memory,
    then actor, then session), so a completer for the next slot reads them out of
    the line rather than being told.  ``needed`` is how many must already be
    there; fewer means there is nothing to scope to yet, and the completer stays
    quiet rather than offering the wrong resource's children.
    """

    needed = 1

    def rows(self, memory_id, typed):            # pragma: no cover - abstract
        raise NotImplementedError

    def key(self, memory_id, typed):             # pragma: no cover - abstract
        raise NotImplementedError

    def completions(self, rows, prefix):         # pragma: no cover - abstract
        raise NotImplementedError

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        typed = positionals(ctx.args, VALUE_FLAGS)
        if len(typed) < self.needed:
            return []
        memory_id = _completion_memory_id(typed[0])
        if not memory_id:
            return []
        try:
            rows = get_or_fetch(cache_key(*self.key(memory_id, typed)),
                                lambda: self.rows(memory_id, typed))
        except Exception:
            return []
        return self.completions(rows, ctx.prefix)


class _ActorCompleter(_ScopedCompleter):
    """Actors in the memory already named in this line."""

    def key(self, memory_id, typed):
        return ("actors", memory_id)

    def rows(self, memory_id, typed):
        return fetch_actors(data_client(), memory_id, PAGE_MAX)

    def completions(self, rows, prefix):
        return [Completion(value=a["actorId"]) for a in rows
                if (a.get("actorId") or "").startswith(prefix)]


class _SessionCompleter(_ScopedCompleter):
    """Sessions of the memory + actor already named in this line."""

    needed = 2

    def key(self, memory_id, typed):
        return ("sessions", memory_id, typed[1])

    def rows(self, memory_id, typed):
        return fetch_sessions(data_client(), memory_id, typed[1], PAGE_MAX)

    def completions(self, rows, prefix):
        return [Completion(value=s["sessionId"],
                           fields=(fmt_time(s.get("createdAt")),))
                for s in rows if (s.get("sessionId") or "").startswith(prefix)]


class _EventCompleter(_ScopedCompleter):
    """Events of the memory + actor + session already named in this line."""

    needed = 3

    def key(self, memory_id, typed):
        return ("events", memory_id, typed[1], typed[2])

    def rows(self, memory_id, typed):
        return fetch_events(data_client(), memory_id, typed[1], typed[2],
                            PAGE_MAX)

    def completions(self, rows, prefix):
        return [Completion(value=e["eventId"],
                           fields=(fmt_time(e.get("eventTimestamp")),
                                   (e.get("branch") or {}).get("name") or ""))
                for e in rows if (e.get("eventId") or "").startswith(prefix)]


class _StrategyCompleter(_ScopedCompleter):
    """Strategy ids of the memory already named in this line."""

    def key(self, memory_id, typed):
        return ("strategies", memory_id)

    def rows(self, memory_id, typed):
        resp = get_memory(control_client(), memory_id)
        return (resp.get("memory") or {}).get("strategies") or []

    def completions(self, rows, prefix):
        return [Completion(value=s["strategyId"],
                           fields=(s.get("type") or "", s.get("name") or ""))
                for s in rows if (s.get("strategyId") or "").startswith(prefix)]


def _memory_param(help_text: str):
    """The ``MEMORY`` positional every leaf takes, spelled the same way."""
    return arg("memory", metavar="MEMORY", completer=_MemoryCompleter(),
               help=help_text)


def _actor_param(help_text: str, required: bool = True):
    kwargs = {} if required else {"nargs": "?", "default": None}
    return arg("actor", metavar="ACTOR", completer=_ActorCompleter(),
               help=help_text, **kwargs)


def _session_param(help_text: str):
    return arg("session", metavar="SESSION", completer=_SessionCompleter(),
               help=help_text)


# ─── command tree ───────────────────────────────────────────────────────────

def register_memory(agentcore) -> None:
    memory = agentcore.command(
        "memory",
        help="Memory resources and their contents — the resource itself on the "
             f"{CONTROL_SERVICE} plane, and the actors / sessions / events / "
             f"records inside it on the {DATA_SERVICE} plane (MEMORY is a "
             "memory name, id, or ARN)",
    )

    _register_resource(memory)
    _register_conversation(memory)
    _register_records(memory)


def _register_resource(memory) -> None:
    """The control-plane leaves: the memory as a resource."""

    @memory.command(
        "list",
        help="List the region's memories, newest first (the NAME column is "
             "derived from the id — ListMemories returns only the id; --status "
             "and --contains are applied in the shell after the fetch, since "
             "ListMemories takes no filters of its own)",
        params=[
            arg("--status", metavar="STATUS",
                completer=ChoiceCompleter(memory_statuses()),
                help="only memories in this status"),
            arg("--contains", metavar="TEXT",
                help="only memories whose id contains TEXT"),
            arg("--max", type=int, default=50, dest="limit", metavar="N",
                help="cap on memories fetched (default 50)"),
            arg("--asc", action="store_true", help="oldest first"),
        ],
    )
    @guard
    def _memory_list(status, contains, limit, asc):
        cli = control_client()
        rows = fetch_memories(cli, limit)
        fetched = len(rows)
        if status:
            rows = [m for m in rows if m.get("status") == status]
        if contains:
            rows = [m for m in rows if contains in (m.get("id") or "")]
        hidden = fetched - len(rows)
        rows.sort(key=_created_key, reverse=not asc)

        cells = [[
            mark_for(m.get("status") or ""),
            name_of(m.get("id") or ""),
            m.get("id") or "?",
            m.get("status") or "?",
            fmt_time(m.get("createdAt")),
            fmt_time(m.get("updatedAt")),
            _managed_by(m),
        ] for m in rows]

        print_header(f"{len(rows)} memory/memories", f"region {region_label()}")

        notes = []
        if hidden:
            notes.append(f"{hidden} fetched memory/memories hidden by the filters")
        # --max bounds the *fetch*, and the filters ran after it, so a short
        # filtered listing must not read as "that is all there is".
        if fetched >= limit:
            notes.append(f"--max {limit} reached, so there may be more"
                         + (" that the filters never saw" if status or contains
                            else "") + "; raise --max to fetch further")

        print_table(["", "NAME", "MEMORY ID", "STATUS", "CREATED", "UPDATED",
                     "MANAGED BY"],
                    cells, note="\n".join(notes) or None)

    @memory.command(
        "describe",
        help="Describe one memory, including every structural member of the "
             "response (strategies, indexedKeys, streamDeliveryResources, …) "
             "rendered as indented lines",
        params=[
            _memory_param("memory to describe"),
            arg("--view", metavar="VIEW", default=None,
                completer=ChoiceCompleter(memory_views()),
                help="GetMemory's view of the response (e.g. "
                     "without_decryption to leave encrypted fields alone)"),
            arg("--raw", action="store_true",
                help="Show the raw API response as JSON"),
        ],
    )
    @guard
    def _memory_describe(memory, view, raw):
        cli = control_client()
        resp = get_memory(cli, resolve_memory_id(cli, memory), view)
        if raw:
            awsut._print_json(resp)
            return
        render_detail(resp.get("memory") or {}, DETAIL_SCALARS)

    @memory.command(
        "strategies",
        help="List a memory's extraction strategies as a table — the same "
             "structures describe prints, but one row each",
        params=[_memory_param("memory whose strategies to list")],
    )
    @guard
    def _memory_strategies(memory):
        cli = control_client()
        memory_id = resolve_memory_id(cli, memory)
        found = (get_memory(cli, memory_id).get("memory") or {})
        rows = found.get("strategies") or []

        cells = [[
            mark_for(s.get("status") or ""),
            s.get("name") or "?",
            s.get("strategyId") or "?",
            s.get("type") or "?",
            s.get("status") or "?",
            ", ".join(s.get("namespaces") or []) or "-",
            fmt_time(s.get("createdAt")),
            s.get("description") or "",
        ] for s in rows]

        print_header(f"{len(rows)} strategy/strategies",
                     f"memory {memory_label(memory_id)}",
                     f"region {region_label()}")
        print_table(["", "NAME", "STRATEGY ID", "TYPE", "STATUS", "NAMESPACES",
                     "CREATED", "DESCRIPTION"], cells)

    @memory.command(
        "watch",
        help="Poll a memory and log its status changes, and its strategies' "
             "(Ctrl+C detaches); a deleted memory reports as gone rather than "
             "as an error",
        params=[
            _memory_param("memory to watch"),
            arg("-n", "--interval", type=float, default=10.0, metavar="SEC",
                help="poll seconds (default 10)"),
            arg("--timeout", type=float, default=0.0, metavar="SEC",
                help="give up after this long; 0 = no limit"),
        ],
    )
    @guard
    def _memory_watch(memory, interval, timeout):
        cli = control_client()
        memory_id = resolve_memory_id(cli, memory)
        found = get_memory(cli, memory_id).get("memory") or {}

        print(f"watching {found.get('name') or name_of(memory_id)}")
        print(f"  {found.get('arn')}")
        print(f"  id {memory_id} · region {region_label()} · poll {interval}s · "
              f"heartbeat {DOT_INTERVAL}s")
        print("  Ctrl+C to detach\n")

        _watch_memory(cli, memory_id, interval, timeout,
                      reattach=f"awsut bedrock-agentcore memory watch "
                               f"{memory_id}")

    @memory.command(
        "delete",
        help="Delete a memory — every actor, session, event and extracted "
             "record it holds goes with it, and this cannot be undone",
        params=[
            _memory_param("memory to delete"),
            arg("-y", "--yes", action="store_true", help="Skip confirmation"),
            arg("--wait", action="store_true",
                help="poll until the memory is gone"),
            arg("-n", "--interval", type=float, default=10.0, metavar="SEC",
                help="--wait poll seconds (default 10)"),
        ],
    )
    @guard
    def _memory_delete(memory, yes, wait, interval):
        cli = control_client()
        require_operation(cli, "delete_memory", "DeleteMemory")
        memory_id = resolve_memory_id(cli, memory)
        found = get_memory(cli, memory_id).get("memory") or {}
        status = found.get("status") or "?"
        if status == "DELETING":
            print(f"memory is already {status}; nothing to delete")
            return

        if not yes:
            if not _confirm_delete(memory_id, found, status):
                print("not deleted")
                return

        cli.delete_memory(memoryId=memory_id)
        print(f"DeleteMemory sent for {memory_label(memory_id)}")

        if wait:
            print(f"  polling every {interval}s · heartbeat {DOT_INTERVAL}s · "
                  "Ctrl+C to detach\n")
            _watch_memory(cli, memory_id, interval, 0.0,
                          reattach=f"awsut bedrock-agentcore memory watch "
                                   f"{memory_id}")


def _register_conversation(memory) -> None:
    """The data-plane leaves for what was said: actors, sessions, events."""

    @memory.command(
        "actors",
        help="List the actors a memory holds — one per participant the service "
             "has stored anything for",
        params=[
            _memory_param("memory whose actors to list"),
            arg("--max", type=int, default=50, dest="limit", metavar="N",
                help="cap on actors fetched (default 50)"),
        ],
    )
    @guard
    def _memory_actors(memory, limit):
        memory_id = resolve_memory_id(control_client(), memory)
        rows = fetch_actors(data_client(), memory_id, limit)
        print_header(f"{len(rows)} actor(s)",
                     f"memory {memory_label(memory_id)}",
                     f"region {region_label()}")
        print_table(["ACTOR ID"], [[a.get("actorId") or "?"] for a in rows],
                    note=(f"--max {limit} reached, so there may be more"
                          if len(rows) >= limit else None))

    @memory.command(
        "sessions",
        help="List a memory's sessions — one per conversation.  With no ACTOR, "
             "every actor is listed in turn, which costs one call per actor",
        params=[
            _memory_param("memory whose sessions to list"),
            _actor_param("only this actor's sessions (default: every actor)",
                         required=False),
            arg("--has-events", action="store_true", dest="has_events",
                help="only sessions that have at least one event "
                     "(ListSessions' HAS_EVENTS filter)"),
            arg("--max", type=int, default=50, dest="limit", metavar="N",
                help="cap on sessions fetched (default 50)"),
            arg("--asc", action="store_true", help="oldest first"),
        ],
    )
    @guard
    def _memory_sessions(memory, actor, has_events, limit, asc):
        memory_id = resolve_memory_id(control_client(), memory)
        dat = data_client()
        if actor:
            actors = [actor]
            scanned = 1
        else:
            actors = [a["actorId"] for a in fetch_actors(dat, memory_id, PAGE_MAX)
                      if a.get("actorId")]
            scanned = 0

        rows: list[dict] = []
        for actor_id in actors:
            if len(rows) >= limit:
                break
            if not actor:
                scanned += 1
            rows.extend(fetch_sessions(dat, memory_id, actor_id,
                                       limit - len(rows), has_events))
        rows.sort(key=_created_key, reverse=not asc)

        cells = [[s.get("actorId") or "?", s.get("sessionId") or "?",
                  fmt_time(s.get("createdAt"))] for s in rows]
        print_header(f"{len(rows)} session(s)",
                     f"memory {memory_label(memory_id)}",
                     f"actor {actor}" if actor else "",
                     f"region {region_label()}")

        notes = []
        if not actor:
            notes.append(f"scanned {scanned} of {len(actors)} actor(s)")
        if len(rows) >= limit:
            notes.append(f"--max {limit} reached, so there may be more")
        print_table(["ACTOR ID", "SESSION ID", "CREATED"], cells,
                    note="\n".join(notes) or None)

    @memory.command(
        "events",
        help="List a session's events — the turns of one conversation, oldest "
             "first so it reads forward.  --payloads asks the API for the "
             "content too and prints each turn below the table",
        params=[
            _memory_param("memory the session belongs to"),
            _actor_param("actor the session belongs to"),
            _session_param("session whose events to list"),
            arg("--payloads", action="store_true",
                help="fetch and print each event's content "
                     "(ListEvents' includePayloads)"),
            arg("--branch", metavar="NAME",
                help="only this branch and its parents"),
            arg("--max", type=int, default=50, dest="limit", metavar="N",
                help="cap on events fetched (default 50)"),
            arg("--desc", action="store_true", help="newest first"),
        ],
    )
    @guard
    def _memory_events(memory, actor, session, payloads, branch, limit, desc):
        memory_id = resolve_memory_id(control_client(), memory)
        rows = fetch_events(data_client(), memory_id, actor, session, limit,
                            payloads, branch)
        rows.sort(key=lambda e: e.get("eventTimestamp")
                  or datetime.min.replace(tzinfo=timezone.utc), reverse=desc)

        header = ["EVENT ID", "TIMESTAMP", "BRANCH", "META"]
        cells = [[
            e.get("eventId") or "?",
            fmt_time(e.get("eventTimestamp")),
            (e.get("branch") or {}).get("name") or "-",
            str(len(e.get("metadata") or {}) or "-"),
        ] for e in rows]
        if payloads:
            header.insert(3, "ROLES")
            for row, e in zip(cells, rows):
                row.insert(3, _roles_of(e) or "-")

        print_header(f"{len(rows)} event(s)",
                     f"memory {memory_label(memory_id)}",
                     f"actor {actor}", f"session {session}",
                     f"region {region_label()}")
        print_table(header, cells,
                    note=(f"--max {limit} reached, so there may be more"
                          if len(rows) >= limit else None))

        if payloads:
            for e in rows:
                print()
                section(f"{e.get('eventId')} · {fmt_time(e.get('eventTimestamp'))}")
                for line in _payload_lines(e.get("payload")):
                    print(line)

    @memory.command(
        "event",
        help="Describe one event, with its full payload",
        params=[
            _memory_param("memory the event belongs to"),
            _actor_param("actor the event belongs to"),
            _session_param("session the event belongs to"),
            arg("event", metavar="EVENT", completer=_EventCompleter(),
                help="eventId to describe"),
            arg("--raw", action="store_true",
                help="Show the raw API response as JSON"),
        ],
    )
    @guard
    def _memory_event(memory, actor, session, event, raw):
        memory_id = resolve_memory_id(control_client(), memory)
        dat = data_client()
        require_operation(dat, "get_event", "GetEvent")
        resp = dat.get_event(memoryId=memory_id, actorId=actor,
                            sessionId=session, eventId=event)
        if raw:
            awsut._print_json(resp)
            return
        _render_event(resp.get("event") or {})


def _register_records(memory) -> None:
    """The data-plane leaves for what was remembered: records and extraction."""

    @memory.command(
        "records",
        help="List a memory's extracted records — what the strategies decided "
             "was worth keeping.  Each record's text prints below the table, "
             "since it is prose rather than a column",
        params=[
            _memory_param("memory whose records to list"),
            arg("--namespace", metavar="NS",
                help="only records in this namespace (`*` is allowed)"),
            arg("--namespace-path", metavar="NS", dest="namespace_path",
                help="only records under this namespace path"),
            arg("--strategy", metavar="ID", completer=_StrategyCompleter(),
                help="only records from this strategy"),
            arg("--max", type=int, default=20, dest="limit", metavar="N",
                help="cap on records fetched (default 20)"),
            arg("--brief", action="store_true",
                help="the table only — leave out each record's text"),
            arg("--asc", action="store_true", help="oldest first"),
        ],
    )
    @guard
    def _memory_records(memory, namespace, namespace_path, strategy, limit,
                        brief, asc):
        memory_id = resolve_memory_id(control_client(), memory)
        rows = fetch_records(data_client(), memory_id, limit, namespace,
                             namespace_path, strategy)
        rows.sort(key=_created_key, reverse=not asc)
        print_header(f"{len(rows)} record(s)",
                     f"memory {memory_label(memory_id)}",
                     f"namespace {namespace}" if namespace else "",
                     f"region {region_label()}")
        _print_records(rows, limit, brief)

    @memory.command(
        "record",
        help="Describe one extracted record, with its full text and metadata",
        params=[
            _memory_param("memory the record belongs to"),
            arg("record", metavar="RECORD",
                help="memoryRecordId to describe"),
            arg("--raw", action="store_true",
                help="Show the raw API response as JSON"),
        ],
    )
    @guard
    def _memory_record(memory, record, raw):
        memory_id = resolve_memory_id(control_client(), memory)
        dat = data_client()
        require_operation(dat, "get_memory_record", "GetMemoryRecord")
        resp = dat.get_memory_record(memoryId=memory_id, memoryRecordId=record)
        if raw:
            awsut._print_json(resp)
            return
        _render_record(resp.get("memoryRecord") or {})

    @memory.command(
        "search",
        help="Search a memory's records semantically (RetrieveMemoryRecords) — "
             "the same rows as records, plus the relevance score the service "
             "assigned, best first",
        params=[
            _memory_param("memory to search"),
            arg("query", metavar="QUERY", help="what to search for"),
            arg("--namespace", metavar="NS",
                help="only records in this namespace (`*` is allowed)"),
            arg("--namespace-path", metavar="NS", dest="namespace_path",
                help="only records under this namespace path"),
            arg("--strategy", metavar="ID", completer=_StrategyCompleter(),
                help="only records from this strategy"),
            arg("--top-k", type=int, default=None, dest="top_k", metavar="N",
                help="how many candidates the service should consider"),
            arg("--max", type=int, default=20, dest="limit", metavar="N",
                help="cap on records fetched (default 20)"),
            arg("--brief", action="store_true",
                help="the table only — leave out each record's text"),
        ],
    )
    @guard
    def _memory_search(memory, query, namespace, namespace_path, strategy,
                       top_k, limit, brief):
        memory_id = resolve_memory_id(control_client(), memory)
        dat = data_client()
        require_operation(dat, "retrieve_memory_records", "RetrieveMemoryRecords")
        criteria = {"searchQuery": query}
        if strategy:
            criteria["memoryStrategyId"] = strategy
        if top_k:
            criteria["topK"] = top_k
        params = {"memoryId": memory_id, "searchCriteria": criteria,
                  "maxResults": min(limit, PAGE_MAX)}
        if namespace:
            params["namespace"] = namespace
        if namespace_path:
            params["namespacePath"] = namespace_path
        rows = paged(dat.retrieve_memory_records, "memoryRecordSummaries",
                     limit, token_param="nextToken", token_key="nextToken",
                     **params)
        print_header(f"{len(rows)} record(s)",
                     f"memory {memory_label(memory_id)}",
                     f"query {query!r}", f"region {region_label()}")
        _print_records(rows, limit, brief, scored=True)

    @memory.command(
        "jobs",
        help="List a memory's extraction jobs — how a session's events became "
             "records, and why some didn't.  The API reports only the failed "
             "ones, so an empty listing means nothing has failed",
        params=[
            _memory_param("memory whose extraction jobs to list"),
            arg("--actor", metavar="ACTOR", completer=_ActorCompleter(),
                help="only jobs for this actor"),
            arg("--session", metavar="SESSION",
                help="only jobs for this session"),
            arg("--strategy", metavar="ID", completer=_StrategyCompleter(),
                help="only jobs for this strategy"),
            arg("--max", type=int, default=50, dest="limit", metavar="N",
                help="cap on jobs fetched (default 50)"),
        ],
    )
    @guard
    def _memory_jobs(memory, actor, session, strategy, limit):
        memory_id = resolve_memory_id(control_client(), memory)
        rows = fetch_jobs(data_client(), memory_id, limit, actorId=actor,
                          sessionId=session, strategyId=strategy)

        cells = [[
            mark_for(j.get("status") or ""),
            j.get("jobID") or "?",
            j.get("status") or "?",
            j.get("strategyId") or "-",
            j.get("actorId") or "-",
            j.get("sessionId") or "-",
            str(len(((j.get("messages") or {}).get("messagesList")) or [])),
        ] for j in rows]

        print_header(f"{len(rows)} extraction job(s)",
                     f"memory {memory_label(memory_id)}",
                     f"region {region_label()}")
        print_table(["", "JOB ID", "STATUS", "STRATEGY", "ACTOR", "SESSION",
                     "EVENTS"], cells,
                    note=(f"--max {limit} reached, so there may be more"
                          if len(rows) >= limit else None))
        print_reasons(rows, lambda j: j.get("jobID") or "?")


def _created_key(row):
    return row.get("createdAt") or datetime.min.replace(tzinfo=timezone.utc)


def _managed_by(summary) -> str:
    """The resource that owns a service-managed memory, or blank.

    The ARN's resource segment rather than the whole ARN: the account and region
    are already in the header, and ``harness/Foo-abc1234567`` is what says "this
    memory is not yours to delete directly".
    """
    parts = split_arn(summary.get("managedByResourceArn") or "")
    return parts[3] if parts else ""


# ─── rendering ──────────────────────────────────────────────────────────────

def _roles_of(event) -> str:
    """``USER, ASSISTANT`` — who spoke in an event, in payload order."""
    roles = [(item.get("conversational") or {}).get("role")
             for item in event.get("payload") or []]
    return ", ".join(r for r in roles if r)


def _payload_lines(payload) -> list[str]:
    """An event's payload as readable lines: a turn per conversational item.

    A conversational item is the common case and gets ``ROLE:`` plus its text
    verbatim, unwrapped — it is prose, and re-flowing a model's answer changes
    what the user is reading.  Anything else (a blob, or a member this botocore
    has never seen) falls through to the structural renderer, so a payload kind
    added later still prints.
    """
    out: list[str] = []
    for i, item in enumerate(payload or [], 1):
        conv = item.get("conversational")
        if conv:
            out.append(f"{conv.get('role') or '?'}:")
            text = (conv.get("content") or {}).get("text") or ""
            out.extend(f"  {line}" for line in text.splitlines() or [""])
            continue
        out.append(f"payload {i}:")
        out.extend(f"  {line}" for line in yaml_lines(item))
    if not out:
        out.append("(no payload — pass --payloads to fetch it)")
    return out


def _render_event(e) -> None:
    print_labeled([
        ("eventId", e.get("eventId")),
        ("eventTimestamp", fmt_time(e.get("eventTimestamp"))),
        None,
        ("memoryId", e.get("memoryId")),
        ("actorId", e.get("actorId")),
        ("sessionId", e.get("sessionId")),
    ])
    for key in ("branch", "metadata"):
        if e.get(key):
            print()
            section(key)
            for line in yaml_lines(e[key]):
                print(line)
    print()
    section("payload")
    for line in _payload_lines(e.get("payload")):
        print(line)


def _render_record(r) -> None:
    print_labeled([
        ("memoryRecordId", r.get("memoryRecordId")),
        ("memoryStrategyId", r.get("memoryStrategyId")),
        ("namespaces", ", ".join(r.get("namespaces") or [])),
        ("createdAt", fmt_time(r.get("createdAt"))),
        ("score", None if r.get("score") is None else f"{r['score']:.4f}"),
    ])
    if r.get("metadata"):
        print()
        section("metadata")
        for line in yaml_lines(r["metadata"]):
            print(line)
    print()
    section("content")
    print((r.get("content") or {}).get("text") or "")


def _print_records(rows, limit, brief, scored=False) -> None:
    """A record listing: identifiers in the table, text in sections below it.

    The text is up to 16 000 characters of prose, so it cannot be a column
    without either truncating it or pushing every id off the screen.  Splitting
    it out keeps the table copy-pasteable and the text intact — the same division
    the failure reasons under a ``harness versions`` table use.
    """
    header = ["RECORD ID", "STRATEGY", "NAMESPACE", "CREATED"]
    cells = [[
        r.get("memoryRecordId") or "?",
        r.get("memoryStrategyId") or "-",
        ", ".join(r.get("namespaces") or []) or "-",
        fmt_time(r.get("createdAt")),
    ] for r in rows]
    if scored:
        header.insert(0, "SCORE")
        for row, r in zip(cells, rows):
            row.insert(0, "-" if r.get("score") is None else f"{r['score']:.4f}")

    print_table(header, cells,
                note=(f"--max {limit} reached, so there may be more"
                      if len(rows) >= limit else None))
    if brief:
        return
    for r in rows:
        print()
        section(r.get("memoryRecordId") or "?")
        print((r.get("content") or {}).get("text") or "")


# ─── delete confirmation ────────────────────────────────────────────────────

def _confirm_delete(memory_id, found, status) -> bool:
    """Ask before a DeleteMemory, having said what goes with it.

    The counts are what decide whether this is safe, so they are fetched rather
    than described in the abstract — but on the *data* plane, which the caller
    may not be able to reach, so a failure there degrades to "unknown" instead of
    blocking the delete.  ``managedByResourceArn`` is called out separately: a
    memory the service created for a harness is not really the user's to remove.
    """
    scale = []
    try:
        actors = fetch_actors(data_client(), memory_id, PAGE_MAX)
        more = "+" if len(actors) >= PAGE_MAX else ""
        scale.append(f"{len(actors)}{more} actor(s)")
    except Exception:
        scale.append("an unknown number of actors")
    strategies = found.get("strategies") or []
    scale.append(f"{len(strategies)} strategy/strategies")

    owner = _managed_by(found)
    warn = (f"\nNOTE: this memory is managed by {owner} — deleting it here may "
            "break that resource." if owner else "")

    try:
        answer = passthrough_input(
            f"{warn}\nDelete memory {memory_label(memory_id)} — currently "
            f"{status}, holding {', '.join(scale)}.  Everything stored in it "
            "(sessions, events, extracted records) goes too and cannot be "
            "recovered. [y/N] : ")
    except RuntimeError:
        # No terminal to prompt on (piped / decorator body).
        raise SmError("cannot confirm without a terminal — re-run with -y/--yes")
    return answer.strip().lower() in ("y", "yes")


# ─── watch ──────────────────────────────────────────────────────────────────

def _watch_memory(cli, memory_id, interval, timeout, *, reattach=None) -> None:
    """Poll one memory until it settles, printing only what changed.

    "Settled" means the memory *and* every strategy has reached a terminal
    status, or the memory is gone: a memory reporting ``ACTIVE`` while a strategy
    is still ``CREATING`` cannot extract anything yet.  The strategies come back
    inside the same GetMemory response, so watching them costs no extra call —
    which is why it is unconditional here rather than behind a flag.

    A successful delete leaves no resource to report a status, so a ``NOT_FOUND``
    answer ends a ``DELETING`` timeline rather than erroring — which is what lets
    ``delete --wait`` reuse this loop.
    """
    require_operation(cli, "get_memory", "GetMemory")
    name = name_of(memory_id)

    beat = Heartbeat()
    deadline = time.monotonic() + timeout if timeout else None
    seen = None                          # last (status, failureReason) printed
    seen_strategies: dict[str, str] = {}
    status = "?"

    while True:
        stamp = datetime.now().strftime("%H:%M:%S")
        try:
            found = get_memory(cli, memory_id).get("memory") or {}
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

            _report_strategies(found, seen_strategies, stamp, beat)

            if status in TERMINAL and all(s in TERMINAL
                                          for s in seen_strategies.values()):
                beat.clear()
                print(f"\n[{stamp}] terminal: {status}")
                return

        if deadline and time.monotonic() > deadline:
            beat.clear()
            print(f"\n[{stamp}] timeout after {fmt_dur(timeout)}; "
                  f"memory still {status}")
            return

        try:
            sleep_with_dots(interval, beat)
        except KeyboardInterrupt:
            beat.clear()
            print(f"\ndetached; memory is still {status}.")
            if reattach:
                print(f"Re-attach with:\n  {reattach}")
            return


def _report_strategies(found, seen, stamp, beat) -> None:
    """Report every strategy that appeared, changed status, or went away."""
    live = set()
    for s in found.get("strategies") or []:
        key = s.get("strategyId")
        if not key:
            continue
        live.add(key)
        status = s.get("status") or "?"
        before = seen.get(key)
        if before == status:
            continue
        seen[key] = status
        beat.clear()
        label = f"{s.get('name') or key} ({s.get('type') or '?'})"
        moved = f"{before} → {status}" if before else status
        print(f"[{stamp}]   {mark_for(status)} strategy {label}  {moved}")

    for key in [k for k in seen if k not in live]:
        seen.pop(key)
        beat.clear()
        print(f"[{stamp}]   - strategy {key} is gone")
