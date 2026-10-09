"""``awsut sagemaker jobs`` — SageMaker Job resources at the service-API level.

Talks to ListJobs / DescribeJob / StopJob through the ``awsut`` boto3 client
factory, so what you see is the raw service response rather than an SDK
rendering of it.  ``log`` reads the job's own output from CloudWatch, whose
group name is derived from the category (see :data:`JOB_LOG_GROUP_ROOT`).

Deliberate differences from the SDK's own status panel:

* Identifiers print in full, never wrapped or ellipsized, so they can be
  copied into other commands and tickets.  ``list`` is one line per job with
  a header row; column widths come from the data.
* ``describe`` always prints ``JobConfigDocument``, reformatted YAML-style.
  That document is where a job's S3 paths and model choices actually live —
  DescribeJob has no input/output members, only an opaque JSON string — so
  hiding it behind a flag would hide the most-wanted part of the response.
  ``--raw-config`` gives the string verbatim; ``--raw`` gives the whole API
  response as JSON.
* ``watch`` prints only on change, so a long job leaves a readable timeline
  rather than a repainted frame.  With no ``JOB_NAME`` it watches every job
  matching the ``list`` filters and reports each status change, printing a
  ``.`` heartbeat every 5s while nothing moves — so silence is visibly "still
  polling", not "hung".  ``Ctrl+C`` detaches.
* ``log`` finds the stream rather than making the user name it: a stream is
  ``<job>/<phase>-<timestamp>``, so it cannot be spelled from the job name
  alone.  A job with no stream is explained by its status rather than reported
  as an error — one too early to have logged and one whose logs aged out are
  both empty, and only the status tells them apart.

``JobCategory`` is **required** by both ListJobs and DescribeJob, so a job
cannot be looked up by name alone.  When ``--category`` is omitted,
``describe`` / ``watch`` / ``stop`` probe the known categories and report
which one matched, and ``list`` queries all of them.

The queried set is the loaded botocore model's ListJobs enum, unioned with
any category named in :data:`EXTRA_JOB_CATEGORIES` (empty by default;
configurable — see that constant).  ``--category extra`` narrows to the
latter.

Neither kind of category failure is fatal, and the two are told apart (see
:func:`category_not_offered`): a category the endpoint does not offer at all
is reported once as a compact "not offered by this endpoint" note, while a
category that should have worked and didn't (no permission, throttling) gets a
stderr line naming the service's own message.
"""

from __future__ import annotations

import concurrent.futures
import sys
import time
from datetime import datetime, timezone

import botocore.exceptions

from eosh.completion import ChoiceCompleter, Completer, Completion, CompletionContext
from eosh.commands import arg
from eosh.completion_cache import aws_env_key, get_or_fetch
from .. import cli as awsut
from .render import (
    DOT_INTERVAL,
    Heartbeat,
    SmError,
    age,
    elapsed_of,
    error_code,
    flag_value,
    fmt_dur,
    fmt_time,
    guard,
    log_streams,
    model_enum,
    NOT_FOUND_CODES,
    positionals,
    print_header,
    print_labeled,
    print_table,
    parse_since,
    read_log_stream,
    region_label,
    require_operation,
    section,
    show_document,
    sleep_with_dots,
    sm_client,
    stream_table_rows,
)

# Categories to query in addition to the ones the loaded botocore model declares.
#
# Empty by default: the model is the source of truth, and a value absent from it
# may not exist at the endpoint.  It is a list rather than a constant because
# botocore treats an enum as documentation and does not reject a value absent
# from it — so an endpoint whose model this botocore release does not carry can
# still be queried by naming its categories from ``~/.eosh/config.py``:
#
#     from eosh_addons.awsut.sagemaker import jobs
#     jobs.EXTRA_JOB_CATEGORIES = ["SomeCategory", "SomeOtherCategory"]
#
# Kept apart from :func:`declared_categories` because the two fail differently: a
# rejection from an undeclared category means "this endpoint does not offer it"
# and is reported as such, where the same rejection from a declared one is a real
# error.  ``--category extra`` narrows to this list.
EXTRA_JOB_CATEGORIES: list[str] = []

TERMINAL_OK = {"Completed"}
TERMINAL_BAD = {"Failed", "Stopped", "DeleteFailed"}
TERMINAL = TERMINAL_OK | TERMINAL_BAD | {"Deleting"}

JOB_STATUSES = sorted(TERMINAL | {"InProgress", "Stopping"})

# Where a job's own output lands.  Derived from the category rather than read
# back from the API because DescribeJob has no log-location member at all — the
# only place the service states it is prose inside a transition's
# ``StatusMessage``, which is not something to parse.  The trailing slash is
# part of the group's real name, not a separator added here.
JOB_LOG_GROUP_ROOT = "/aws/sagemaker/Job"

# Value-taking flags across the ``jobs`` leaves, so :func:`positionals` can tell
# a flag's value from a positional in a completion context — in
# ``log --category X <TAB>``, ``X`` is not the job name being completed.
VALUE_FLAGS = ("--category", "--status", "--contains", "--since", "--max",
               "--sort", "-n", "--interval", "--timeout", "--stream",
               "--lookback", "--log-group")

# Group selectors accepted anywhere a category is: they stand for a set of
# categories rather than one, and probing narrows over that set.
CATEGORY_GROUPS = {
    "all": "every category this shell knows",
    "extra": "only the categories added via EXTRA_JOB_CATEGORIES",
}


def declared_categories() -> list[str]:
    """The categories the loaded botocore model declares. Offline — no API call.

    Resolved per call rather than at registration time because
    ``var sagemaker_service_name=`` can swap the model underneath us.
    """
    return list(model_enum(awsut.sagemaker_service_name, "ListJobs", "JobCategory"))


def job_categories() -> list[str]:
    """Every JobCategory this shell knows how to list.

    The model's enum unioned with :data:`EXTRA_JOB_CATEGORIES`, so a refreshed
    service model contributes new categories without a code change here — and
    graduates a configured extra out of "extra" by declaring it.
    """
    found = declared_categories()
    return found + [c for c in EXTRA_JOB_CATEGORIES if c not in found]


def category_not_offered(category: str, exc) -> bool:
    """True when a ListJobs/DescribeJob failure just means "no such category here".

    A ``ValidationException`` on a category the loaded model does not declare is
    the *expected* answer from an endpoint that does not offer it.  Reporting each
    one as a failure would put a stderr line per configured extra under every
    `jobs list`.  The same code on a declared category, or any other code
    (``AccessDeniedException``) on any category, is a genuine failure and stays
    loud.
    """
    return (error_code(exc) == "ValidationException"
            and category not in declared_categories())


def job_log_group(category: str) -> str:
    """The CloudWatch log group a category's jobs write to."""
    return f"{JOB_LOG_GROUP_ROOT}/{category}/"


def job_stream_prefix(job_name: str) -> str:
    """The stream-name prefix that holds one job's streams.

    A stream is ``<job name>/<phase>-<timestamp>``, and the phase segment is
    named per category (``evaluation``, ``generation``), so the name cannot be
    derived from the job alone — it has to be looked up under this prefix.
    """
    return f"{job_name}/"


def resolve_categories(selector: str | None) -> list[str]:
    """Map a ``--category`` value to the list of categories to query."""
    categories = job_categories()
    if selector in (None, "all"):
        return categories
    if selector == "extra":
        return [c for c in categories if c in EXTRA_JOB_CATEGORIES]
    return [selector]


# ─── API calls ──────────────────────────────────────────────────────────────

def describe_job(cli, name: str, category: str) -> dict:
    require_operation(cli, "describe_job", "DescribeJob")
    return cli.describe_job(JobName=name, JobCategory=category)


def resolve_job(cli, name: str, category: str | None) -> tuple[dict, str]:
    """``(description, category)``, probing categories when none is given.

    ``all`` / ``extra`` are group selectors rather than categories, so they
    probe too — just over a narrowed set.
    """
    candidates = resolve_categories(category)
    if not candidates:
        raise SmError(f"no categories to search for {category!r}")
    if len(candidates) == 1:
        return describe_job(cli, name, candidates[0]), candidates[0]

    tried = []
    not_offered = []
    for cat in candidates:
        try:
            return describe_job(cli, name, cat), cat
        except botocore.exceptions.ClientError as exc:
            if error_code(exc) in NOT_FOUND_CODES:
                # A category this endpoint rejects outright never had a chance to
                # hold the job, so saying "not found in it" would be misleading.
                (not_offered if category_not_offered(cat, exc) else tried).append(cat)
                continue
            raise
    message = f"no job named {name!r} in any of: {', '.join(tried) or '(none)'}"
    if not_offered:
        message += (f"\n       not offered by this endpoint, so not searched: "
                    f"{', '.join(not_offered)}")
    message += ("\n       (JobCategory is required by DescribeJob; pass --category "
                "if it is a category this shell does not know about)")
    raise SmError(message)


def fetch_jobs(cli, categories, after, *, status=None, contains=None,
               limit=25, sort="CreationTime", asc=False, warn=True):
    """ListJobs across *categories* → ``(rows, skipped, not_offered)``.

    Rows come back in service order, unsorted and untruncated; callers decide
    how to present them.

    The two failure lists are kept apart because they mean different things to
    the user: *skipped* is a category that should have worked and didn't (no
    permission, throttling) and gets a stderr line, while *not_offered* is a
    category this endpoint simply does not have (see
    :func:`category_not_offered`) and is worth only a compact note.
    ``warn=False`` suppresses the stderr lines, so a polling loop reports an
    unauthorized category once rather than on every pass.
    """
    require_operation(cli, "list_jobs", "ListJobs")
    rows = []
    skipped = []
    not_offered = []
    for cat in categories:
        params = {"JobCategory": cat, "MaxResults": min(limit, 100)}
        if status:
            params["StatusEquals"] = status
        if contains:
            params["NameContains"] = contains
        if after:
            params["CreationTimeAfter"] = after
        if sort:
            params["SortBy"] = sort
            params["SortOrder"] = "Ascending" if asc else "Descending"

        # --max is a per-category page budget, not a shared one: sharing it lets
        # the first category consume the whole allowance and silently starve the
        # rest.
        got = 0
        token = None
        while got < limit:
            if token:
                params["NextToken"] = token
            try:
                resp = cli.list_jobs(**params)
            except botocore.exceptions.ClientError as exc:
                if category_not_offered(cat, exc):
                    not_offered.append(cat)
                else:
                    if warn:
                        msg = exc.response["Error"].get("Message", str(exc))
                        print(f"error: {cat}: {msg}", file=sys.stderr)
                    skipped.append(cat)
                break
            page = resp.get("JobSummaries", [])
            rows.extend(page)
            got += len(page)
            token = resp.get("NextToken")
            if not token:
                break
    return rows, skipped, not_offered


def _category_notes(skipped, not_offered) -> list[str]:
    """Footer lines for the categories a listing could not cover.

    Coverage is never trimmed silently, but the two reasons read differently: a
    category the endpoint does not have is a fact about the endpoint, not
    something the user can act on, so it gets one line naming the categories
    rather than a stderr line each.
    """
    notes = []
    if skipped:
        notes.append(f"not queried (see stderr): {', '.join(skipped)}")
    if not_offered:
        notes.append(f"not offered by this endpoint: {', '.join(not_offered)} "
                     f"(absent from the {awsut.sagemaker_service_name!r} model "
                     "and rejected by the service)")
    return notes


def transitions(desc):
    """``SecondaryStatusTransitions`` as ``(status, start, end, secs, running, msg)``.

    The service does not populate ``EndTime`` on these transitions — not even
    on a completed job — so a step's duration has to be derived from the *next*
    transition's ``StartTime``, falling back to the job's ``EndTime`` for the
    last step.  Deriving it this way means a finished step's duration stops
    changing, which is the whole point: reading ``StartTime`` alone gives an
    ever-growing age rather than a duration.

    Only the final transition of a job that has not reached a terminal state is
    reported as running.
    """
    steps = desc.get("SecondaryStatusTransitions", []) or []
    job_end = desc.get("EndTime")
    terminal = desc.get("JobStatus") in TERMINAL
    out = []

    for i, t in enumerate(steps):
        start = t.get("StartTime")
        end = t.get("EndTime")  # honoured if the service ever starts populating it
        if end is None:
            if i + 1 < len(steps):
                end = steps[i + 1].get("StartTime")
            elif terminal:
                end = job_end

        last = i + 1 == len(steps)
        running = last and not terminal
        if start and end:
            secs = (end - start).total_seconds()
        elif start:
            secs = age(start)
        else:
            secs = None
        out.append((t.get("Status", "?"), start, end, secs, running,
                    t.get("StatusMessage", "")))
    return out


def mark_for(status: str) -> str:
    return "✓" if status in TERMINAL_OK else ("✗" if status in TERMINAL_BAD else "…")


def state_of(summary) -> tuple[str, str]:
    """``(status, secondary)`` from a ListJobs summary.

    ListJobs names the secondary field ``JobSecondaryStatus``; DescribeJob
    calls the same thing ``SecondaryStatus``.
    """
    return (summary.get("JobStatus") or "?",
            summary.get("JobSecondaryStatus") or "-")


def fmt_state(state) -> str:
    status, secondary = state
    return f"{status} / {secondary}" if secondary and secondary != "-" else status


# ─── completers ─────────────────────────────────────────────────────────────

class _JobCategoryCompleter(Completer):
    """Categories plus the ``all`` / ``extra`` group selectors."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        out = [Completion(value=name, description=desc)
               for name, desc in CATEGORY_GROUPS.items()
               if name.startswith(ctx.prefix)]
        declared = declared_categories()
        for cat in job_categories():
            if not cat.startswith(ctx.prefix):
                continue
            # Flag an undeclared category before the call, not after: an endpoint
            # that does not offer it answers ValidationException.
            out.append(Completion(
                value=cat,
                description="" if cat in declared else
                            "from EXTRA_JOB_CATEGORIES — not in this model, "
                            "may be rejected",
            ))
        return out


class _JobNameCompleter(Completer):
    """Job names, narrowed by an already-typed ``--category``.

    Fans ListJobs out over the categories in one pass — a job cannot be listed
    without naming its category, so completing a name means asking every
    category that could hold it.  Results are cached (TTL + invalidated after
    every command) so the fan-out happens once per typed token, not once per
    keystroke.
    """

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        selector = flag_value(ctx.args, "--category")
        try:
            summaries = get_or_fetch(
                ("awsut.sagemaker_jobs", aws_env_key(), awsut.sagemaker_endpoint,
                 awsut.sagemaker_service_name, selector),
                lambda: _list_jobs_for_completion(selector),
            )
        except Exception:
            return []
        return [
            Completion(
                value=s["JobName"],
                # Category and state as columns: with one category per column
                # width, the states line up in a single readable strip.
                fields=(s.get("JobCategory") or "", fmt_state(state_of(s))),
            )
            for s in summaries
            if s.get("JobName", "").startswith(ctx.prefix)
        ]


def _list_jobs_for_completion(selector: str | None) -> list[dict]:
    """One newest-first page of jobs per category, fetched in parallel.

    One client shared across the pool, as ``awsut``'s HyperPod hostname lookup
    already does: boto3 clients are thread-safe once constructed, and building
    one per thread would dominate the latency this fan-out exists to hide.
    """
    cli = sm_client()
    require_operation(cli, "list_jobs", "ListJobs")
    categories = resolve_categories(selector)

    def one(category):
        try:
            resp = cli.list_jobs(JobCategory=category, MaxResults=100,
                                 SortBy="CreationTime", SortOrder="Descending")
        except Exception:
            return []          # unauthorized / unknown category — just skip it
        return resp.get("JobSummaries", [])

    rows: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for page in pool.map(one, categories):
            rows.extend(page)
    rows.sort(key=_creation_key, reverse=True)
    return rows


def _creation_key(row):
    return row.get("CreationTime") or datetime.min.replace(tzinfo=timezone.utc)


class _JobLogStreamCompleter(Completer):
    """Streams under an already-typed job name, minus the job-name prefix.

    Offers what ``--stream`` takes, which is the tail below the job — the rest
    of the path is derived.
    """

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        typed = positionals(ctx.args, VALUE_FLAGS)
        if not typed:
            return []                      # no job named yet — nothing to scope to
        job_name = typed[0]
        selector = flag_value(ctx.args, "--category")
        try:
            streams = get_or_fetch(
                ("awsut.sagemaker_job_streams", aws_env_key(), selector, job_name),
                lambda: _job_streams_for_completion(job_name, selector),
            )
        except Exception:
            return []
        prefix = job_stream_prefix(job_name)
        out = []
        for s in streams:
            tail = s["logStreamName"][len(prefix):]
            if tail.startswith(ctx.prefix):
                # No description: every row here is a log stream, so saying so
                # would only be a column of the same word down the list.
                out.append(Completion(value=tail))
        return out


def _job_streams_for_completion(job_name: str, selector: str | None) -> list[dict]:
    """A job's streams, without asking SageMaker which category it is.

    Completion cannot afford a DescribeJob probe per candidate category, and
    does not need one: the log group name embeds the category, so trying each
    candidate group and keeping whatever answers finds the streams in a single
    parallel pass.  A group that does not exist answers with an empty list, so a
    wrong guess costs nothing but the call.

    De-duplicated by stream name: only one category really holds a given job, but
    the fan-out cannot know which, and the same name surfacing from two groups
    would put the same candidate in the picker twice.
    """
    prefix = job_stream_prefix(job_name)

    def one(category):
        try:
            return log_streams(job_log_group(category), prefix)
        except Exception:
            return []                      # unauthorized / absent — just skip it

    seen: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for page in pool.map(one, resolve_categories(selector)):
            for stream in page:
                seen.setdefault(stream["logStreamName"], stream)
    return list(seen.values())


_SINCE_HINTS = ChoiceCompleter(["30m", "1h", "6h", "24h", "7d"])


def _filter_params(*, for_watch: bool) -> list:
    """The ``list`` filter flags, shared with ``watch``'s all-jobs mode.

    Declared here rather than on the ``jobs`` group so ``describe`` and
    ``stop`` — which cannot use them — don't offer them in completion.
    """
    prefix = "all-jobs mode: " if for_watch else ""
    return [
        arg("--status", metavar="STATUS", completer=ChoiceCompleter(JOB_STATUSES),
            help=f"{prefix}only jobs in this status"),
        arg("--contains", metavar="TEXT",
            help=f"{prefix}NameContains filter"),
        arg("--since", metavar="AGE", completer=_SINCE_HINTS,
            help=f"{prefix}only jobs created within e.g. 30m, 24h, 7d"),
        arg("--max", type=int, default=25, dest="limit", metavar="N",
            help="per-category cap on jobs fetched (default 25)"),
    ]


# ─── command tree ───────────────────────────────────────────────────────────

# Every ``jobs`` leaf takes it: JobCategory is required by the API.  Declared
# on each leaf rather than the group — flags are never inherited.
CATEGORY = arg("--category", metavar="CATEGORY", completer=_JobCategoryCompleter(),
               help="one category, or 'all' / 'extra' (default: all known)")

def register_jobs(sagemaker) -> None:
    jobs = sagemaker.command(
        "jobs",
        help="Job resources — ListJobs / DescribeJob / StopJob",
    )

    @jobs.command(
        "list",
        help="List jobs across categories (a '+' on DURATION means the job is "
             "still running)",
        params=[CATEGORY] + _filter_params(for_watch=False) + [
            arg("--sort", choices=["Name", "CreationTime", "Status"],
                default="CreationTime", help="service-side sort key"),
            arg("--asc", action="store_true", help="oldest first"),
        ],
    )
    @guard
    def _jobs_list(category, status, contains, since, limit, sort, asc):
        cli = sm_client()
        categories = resolve_categories(category)
        rows, skipped, not_offered = fetch_jobs(
            cli, categories, parse_since(since), status=status, contains=contains,
            limit=limit, sort=sort, asc=asc)
        queried = [c for c in categories if c not in not_offered]
        rows.sort(key=_creation_key, reverse=not asc)
        dropped = max(0, len(rows) - limit)
        rows = rows[:limit]

        cells = []
        for r in rows:
            started, ended = r.get("CreationTime"), r.get("EndTime")
            status_text = r.get("JobStatus", "?")
            cells.append([
                mark_for(status_text),
                r.get("JobName") or "?",
                r.get("JobCategory") or "?",
                status_text,
                r.get("JobSecondaryStatus") or "-",
                fmt_time(started),
                fmt_dur(elapsed_of(started, ended)) + ("" if ended else "+"),
            ])

        plural = "y" if len(queried) == 1 else "ies"
        print_header(f"{len(rows)} job(s)", f"region {region_label()}",
                     f"{len(queried)} categor{plural} queried")

        notes = []
        # Never cap coverage silently — a short list must not read as a
        # complete one.  'at least' because --max also bounded the per-category
        # fetch, so there may be more beyond these.
        if dropped:
            notes.append(f"at least {dropped} more job(s) matched but were cut by "
                         f"--max {limit}; raise --max or narrow with "
                         "--category/--since/--contains.")
        notes.extend(_category_notes(skipped, not_offered))

        print_table(
            ["", "JOB NAME", "CATEGORY", "STATUS", "SECONDARY", "CREATED", "DURATION"],
            cells,
            note="\n".join(notes) or None,
        )

    @jobs.command(
        "describe",
        help="Describe one job, including its config document "
             "(a '+' on a transition's duration means it is the current one — "
             "the service leaves EndTime unset, so durations are derived)",
        params=[
            CATEGORY,
            arg("job_name", help="job to describe",
                completer=_JobNameCompleter()),
            arg("--raw", action="store_true", help="Show the raw API response as JSON"),
            arg("--raw-config", action="store_true",
                help="Print JobConfigDocument verbatim — the API returns it as "
                     "one JSON string, which is reformatted for reading by "
                     "default"),
        ],
    )
    @guard
    def _jobs_describe(job_name, category, raw, raw_config):
        cli = sm_client()
        desc, resolved = resolve_job(cli, job_name, category)
        if raw:
            awsut._print_json(desc)
            return
        _render_job(desc, resolved, raw_config=raw_config)

    @jobs.command(
        "log",
        help="Read a job's own CloudWatch log stream",
        params=[
            CATEGORY,
            arg("job_name", help="job whose log to read",
                completer=_JobNameCompleter()),
            arg("--stream", metavar="NAME", default=None,
                completer=_JobLogStreamCompleter(),
                help="stream below the job (default: its most recent one; a "
                     "listed STREAM is what this takes)"),
            arg("--list", action="store_true", dest="list_streams",
                help="list the job's streams instead of reading one"),
            arg("-f", "--follow", action="store_true",
                help="keep polling for new events (Ctrl+C to stop)"),
            arg("--lookback", type=int, default=0, metavar="MINUTES",
                help="only events from the last N minutes (default: all)"),
            arg("--log-group", metavar="GROUP", default=None,
                help=f"override the derived {JOB_LOG_GROUP_ROOT}/<category>/ group"),
        ],
    )
    @guard
    def _jobs_log(job_name, category, stream, list_streams, follow, lookback,
                  log_group):
        # DescribeJob first even though only the category is strictly needed for
        # the group name: it also yields the canonical job name and the status,
        # which is what explains an empty log when there is one.
        cli = sm_client()
        desc, resolved = resolve_job(cli, job_name, category)
        name = desc.get("JobName") or job_name
        group = log_group or job_log_group(resolved)
        prefix = job_stream_prefix(name)

        if list_streams:
            _list_job_streams(group, prefix, desc, resolved)
            return

        if stream:
            full = f"{prefix}{stream}"
        else:
            found = log_streams(group, prefix)
            if not found:
                _explain_no_stream(group, prefix, desc, resolved)
                return
            full = found[0]["logStreamName"]
            if len(found) > 1:
                # Newest-first, so [0] is the current phase.  Say so rather than
                # silently showing one of several.
                print(f"{len(found)} streams under {prefix} — reading the most "
                      f"recent; --list shows them all")

        print_header(group, full)
        read_log_stream(group, full, follow=follow, lookback=lookback,
                        on_missing=lambda: _explain_no_stream(
                            group, prefix, desc, resolved, named=stream))

    @jobs.command(
        "watch",
        help="Poll a job and log changes; with no JOB_NAME, watch every matching job",
        params=[
            CATEGORY,
            arg("job_name", nargs="?", default=None, completer=_JobNameCompleter(),
                help="omit to watch every job matching the filters"),
            arg("-n", "--interval", type=float, default=15.0, metavar="SEC",
                help="poll seconds (default 15)"),
            arg("--timeout", type=float, default=0.0, metavar="SEC",
                help="give up after this long; 0 = no limit"),
            arg("--until-done", action="store_true",
                help="all-jobs mode: stop once every tracked job is terminal"),
        ] + _filter_params(for_watch=True),
    )
    @guard
    def _jobs_watch(job_name, category, interval, timeout, until_done,
                    status, contains, since, limit):
        if job_name:
            _watch_one(job_name, category, interval, timeout)
        else:
            _watch_all(category, interval, timeout, until_done,
                       status, contains, since, limit)

    @jobs.command(
        "stop", help="Stop a running job",
        params=[
            CATEGORY,
            arg("job_name", help="job to stop",
                completer=_JobNameCompleter()),
            arg("-y", "--yes", action="store_true", help="Skip confirmation"),
        ],
        pass_context=True,
    )
    @guard
    def _jobs_stop(ctx, job_name, category, yes):
        cli = sm_client()
        require_operation(cli, "stop_job", "StopJob")
        desc, resolved = resolve_job(cli, job_name, category)
        status = desc.get("JobStatus")
        if status in TERMINAL:
            print(f"job is already {status}; nothing to stop")
            return
        if not yes:
            try:
                answer = ctx.input(
                    f"Stop job [{desc.get('JobName')}] "
                    f"({resolved}, currently {status})? [y/N] : ")
            except RuntimeError:
                # No terminal to prompt on (piped / decorator body).
                raise SmError("cannot confirm without a terminal — re-run with -y/--yes")
            if answer.strip().lower() not in ("y", "yes"):
                print("not stopped")
                return
        cli.stop_job(JobName=desc.get("JobName"), JobCategory=resolved)
        print(f"StopJob sent for {desc.get('JobName')}")


# ─── log reading ────────────────────────────────────────────────────────────

def _list_job_streams(group, prefix, desc, category) -> None:
    rows = stream_table_rows(log_streams(group, prefix), prefix)
    print_header(f"{len(rows)} stream(s)", f"{group}{prefix}")
    print_table(["STREAM (below the job)", "FIRST EVENT", "LAST EVENT"],
                rows,
                note=(None if rows else
                      _no_stream_note(prefix, desc, category)))


def _explain_no_stream(group, prefix, desc, category, named=None) -> None:
    """Say why there is no log, rather than failing.

    A job that has not reached a logging phase yet has no stream, and that is a
    real answer about the job — so it is reported on stdout at exit 0, like
    ``studio log`` does for a space whose lifecycle config never fired.
    """
    if named:
        print(f"no stream {named!r} under {prefix} in {group}")
        print("what does exist: awsut sagemaker jobs log "
              f"{desc.get('JobName')} --list")
        return
    print(f"no log stream under {prefix} in {group}")
    print(_no_stream_note(prefix, desc, category))


def _no_stream_note(prefix, desc, category) -> str:
    """The status is the explanation, so lead with it.

    Two very different things produce an empty listing — a job too early to have
    logged, and a finished job whose logs have aged out — and only its status
    tells them apart.
    """
    state = fmt_state(state_of({
        "JobStatus": desc.get("JobStatus"),
        # DescribeJob spells the secondary status differently from ListJobs;
        # state_of() reads the ListJobs name, so translate.
        "JobSecondaryStatus": desc.get("SecondaryStatus"),
    }))
    if desc.get("JobStatus") in TERMINAL:
        why = ("the job is finished, so either it never logged or its log group "
               "has since been deleted")
    else:
        why = "a job writes nothing until it reaches a phase that logs"
    return (f"the job is {state} — {why}\n"
            f"if this category logs somewhere else, name the group with "
            f"--log-group (this one was derived from category {category})")


# ─── describe rendering ─────────────────────────────────────────────────────

def _render_job(desc, category, raw_config=False):
    started, ended = desc.get("CreationTime"), desc.get("EndTime")
    elapsed = elapsed_of(started, ended)
    status = desc.get("JobStatus", "?")

    # Identifiers first, one per line, unwrapped — these are the copy targets.
    secondary = desc.get("SecondaryStatus")
    print_labeled([
        ("JobName", desc.get("JobName")),
        ("JobArn", desc.get("JobArn")),
        ("JobCategory", category),
        ("RoleArn", desc.get("RoleArn")),
        None,
        ("Status", status + (f" / {secondary}" if secondary else "")),
        ("Created", fmt_time(started)),
        ("Modified", fmt_time(desc.get("LastModifiedTime"))),
        ("Ended", fmt_time(ended) if ended else None),
        ("Elapsed", fmt_dur(elapsed) + ("" if ended else " (running)")),
    ])
    if desc.get("FailureReason"):
        print()
        section("FailureReason")
        print(desc["FailureReason"])

    steps = transitions(desc)
    if steps:
        print()
        section("SecondaryStatusTransitions")
        # A timeline, not a listing: each step can carry a multi-line message
        # hanging off it, which no table column expresses.  Widths still come
        # from the data, so the durations line up however long a status name is.
        marked = [("…" if running else "✓", st,
                   fmt_dur(secs) + ("+" if running else ""),
                   fmt_time(start), msg)
                  for st, start, _end, secs, running, msg in steps]
        st_w = max(len(m[1]) for m in marked)
        dur_w = max(len(m[2]) for m in marked)
        for mark, st, duration, start, msg in marked:
            print(f"{mark} {st.ljust(st_w)}  {duration.ljust(dur_w)}  {start}")
            for line in (msg or "").splitlines():
                print(f"    {line}")

    show_document("JobConfigDocument", desc.get("JobConfigDocument"),
                  desc.get("JobConfigSchemaVersion"), raw=raw_config)

    if desc.get("Tags"):
        print()
        section("Tags")
        print_labeled([(t.get("Key") or "?", t.get("Value")) for t in desc["Tags"]])


# ─── watch: one job ─────────────────────────────────────────────────────────

def _watch_one(job_name, category, interval, timeout):
    cli = sm_client()
    desc, resolved = resolve_job(cli, job_name, category)

    print(f"watching {desc.get('JobName')}")
    print(f"  {desc.get('JobArn')}")
    print(f"  category {resolved} · region {region_label()} · poll {interval}s · "
          f"heartbeat {DOT_INTERVAL}s")
    print("  Ctrl+C to detach\n")

    seen = None            # last (status, secondary) pair printed
    seen_steps = set()     # transitions already reported
    beat = Heartbeat()
    deadline = time.monotonic() + timeout if timeout else None

    while True:
        status = desc.get("JobStatus", "?")
        secondary = desc.get("SecondaryStatus")
        stamp = datetime.now().strftime("%H:%M:%S")

        for st, _start, end, secs, _running, msg in transitions(desc):
            # Keyed on "has it finished" as well as the name, so a step is
            # reported once when it starts and once when it ends.
            key = (st, end is not None)
            if key in seen_steps:
                continue
            seen_steps.add(key)
            beat.clear()
            if end:
                print(f"[{stamp}] ✓ {st} finished in {fmt_dur(secs)}")
            else:
                print(f"[{stamp}] → {st} started")
                for line in (msg or "").splitlines():
                    print(f"           {line}")

        if (status, secondary) != seen:
            seen = (status, secondary)
            beat.clear()
            print(f"[{stamp}] status {status}"
                  + (f" / {secondary}" if secondary else ""))

        if status in TERMINAL:
            elapsed = None
            if desc.get("CreationTime"):
                end = desc.get("EndTime") or datetime.now(timezone.utc)
                elapsed = (end - desc["CreationTime"]).total_seconds()
            beat.clear()
            print(f"\n[{stamp}] terminal: {status} after {fmt_dur(elapsed)}")
            if desc.get("FailureReason"):
                print(f"FailureReason: {desc['FailureReason']}")
            return

        if deadline and time.monotonic() > deadline:
            beat.clear()
            print(f"\n[{stamp}] timeout after {fmt_dur(timeout)}; job still {status}")
            return

        try:
            sleep_with_dots(interval, beat)
        except KeyboardInterrupt:
            beat.clear()
            print(f"\ndetached; job is still {status}. Re-attach with:")
            print(f"  awsut sagemaker jobs watch {desc.get('JobName')} "
                  f"--category {resolved}")
            return

        try:
            desc = describe_job(cli, job_name, resolved)
        except botocore.exceptions.ClientError as exc:
            beat.clear()
            msg = exc.response["Error"].get("Message", str(exc))
            print(f"error: poll failed: {msg}", file=sys.stderr)


# ─── watch: every matching job ──────────────────────────────────────────────

def _watch_all(category, interval, timeout, until_done,
               status_filter, contains, since, limit):
    """Watch every job matching the ``list`` filters and log each status change.

    Polls ListJobs per category (not DescribeJob per job) so the cost is
    O(categories) regardless of fleet size, and jobs that appear after the
    watch starts are picked up.

    The filters are re-evaluated every pass, so a relative ``--since`` is a
    sliding window and a job can drop out of it.  When one does — including
    because ``--status InProgress`` stopped matching the moment it finished —
    it gets one DescribeJob so its final state is reported rather than
    silently vanishing.
    """
    cli = sm_client()
    categories = resolve_categories(category)
    beat = Heartbeat()
    parse_since(since)   # validate the format before printing the header

    filters = [f"category {category or 'all'}"]
    for label, value in (("status", status_filter), ("contains", contains),
                         ("since", since)):
        if value:
            filters.append(f"{label} {value}")
    print(f"watching all jobs · region {region_label()} · poll {interval}s · "
          f"heartbeat {DOT_INTERVAL}s")
    print(f"  filters: {', '.join(filters)} · max {limit}/category")
    print("  Ctrl+C to detach\n")

    known = {}       # JobName -> {"state": (status, secondary), "category": str}
    ever_seen = False
    first = True
    deadline = time.monotonic() + timeout if timeout else None

    while True:
        # Re-derived every pass so a relative --since slides forward with time.
        after = parse_since(since)
        rows, skipped, not_offered = fetch_jobs(
            cli, categories, after, status=status_filter, contains=contains,
            limit=limit, sort="CreationTime", asc=False, warn=first)
        if first:
            for note in _category_notes(skipped, not_offered):
                print(f"{note}\n")
        stamp = datetime.now().strftime("%H:%M:%S")

        seen = set()
        for r in rows:
            name = r.get("JobName")
            if not name:
                continue
            seen.add(name)
            state = state_of(r)
            entry = known.get(name)
            if entry is None:
                known[name] = {"state": state, "category": r.get("JobCategory")}
                ever_seen = True
                beat.clear()
                # '·' for the baseline pass, '+' for a job that showed up later.
                lead = "·" if first else "+"
                print(f"[{stamp}] {lead} {mark_for(state[0])} {name}  "
                      f"{fmt_state(state)}")
            elif entry["state"] != state:
                beat.clear()
                print(f"[{stamp}]   {mark_for(state[0])} {name}  "
                      f"{fmt_state(entry['state'])} → {fmt_state(state)}")
                entry["state"] = state

        # Departures: confirm the final state instead of letting a job just
        # disappear off the end of the timeline.
        for name in [n for n in known if n not in seen]:
            entry = known.pop(name)
            final = None
            if entry["category"]:
                try:
                    d = describe_job(cli, name, entry["category"])
                    final = (d.get("JobStatus") or "?",
                             d.get("SecondaryStatus") or "-")
                except botocore.exceptions.ClientError:
                    pass
            beat.clear()
            if final and final != entry["state"]:
                print(f"[{stamp}]   {mark_for(final[0])} {name}  "
                      f"{fmt_state(entry['state'])} → {fmt_state(final)}  "
                      "(left filter)")
            else:
                print(f"[{stamp}] - {name}  no longer matches the filter "
                      f"(last seen {fmt_state(entry['state'])})")

        if first:
            beat.clear()
            if not rows:
                print(f"[{stamp}] no jobs match yet; waiting for one to appear")
            print()
        first = False

        if until_done and ever_seen and all(
                e["state"][0] in TERMINAL for e in known.values()):
            beat.clear()
            print(f"[{stamp}] all {len(known)} tracked job(s) terminal; done")
            return

        if deadline and time.monotonic() > deadline:
            beat.clear()
            running = sum(1 for e in known.values()
                          if e["state"][0] not in TERMINAL)
            print(f"[{stamp}] timeout after {fmt_dur(timeout)}; "
                  f"{running} job(s) still running")
            return

        try:
            sleep_with_dots(interval, beat)
        except KeyboardInterrupt:
            beat.clear()
            running = [n for n, e in known.items()
                       if e["state"][0] not in TERMINAL]
            print(f"\ndetached; {len(running)} job(s) still running"
                  + (": " + ", ".join(running) if running else ""))
            return
