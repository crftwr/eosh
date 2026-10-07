"""The output contract for the whole ``awsut`` command tree.

Every ``awsut`` leaf that prints a resource listing prints it the same way, and
this module is the only place that shape is defined:

* **A header line, then a blank line.**  ``N thing(s) · <scope> · region <r>`` —
  how many, of what, from where.  It is also the answer when there are none:
  :func:`print_table` prints nothing for an empty row list, so the header is
  what says "asked, and there were zero".
* **A table.**  ALL-CAPS column labels, a ``-`` rule, two spaces between
  columns, and **widths from the data** so an identifier is never truncated —
  every cell can be copied straight into the next command.
* **Notes only for facts about this output**: rows hidden by a filter, rows cut
  by ``--max``, a query that was refused.  Not a legend, and not advice — a
  sentence that reads the same on every run belongs in the command's ``help=``,
  where it is available on demand instead of costing three lines per listing.
* **Detail views** (``describe``) open with a :func:`print_labeled` block whose
  labels are the API's own field names, then use tables for the nested lists.
  Anything after that block that isn't a table gets a :func:`section` heading,
  so the eye can find where one field's worth of output ends and the next
  begins without counting blank lines.
* **Failures** read like shell errors: ``error: <message>`` on stderr, lowercase,
  the resource quoted.  :func:`guard` on the leaf turns the expected AWS and
  usage failures into that one line.
* **Timestamps** are local and second-precision (:func:`fmt_time`); the change
  lines a ``watch`` loop emits are stamped ``[HH:MM:SS]``.

It lives at the top of the add-on — above ``cli.py`` and above every
per-service subpackage (``sagemaker``, ``agentcore``) — and
deliberately builds no AWS clients, so any of them can import it without an
import cycle.  Each subpackage's ``render`` module re-exports these names, since
its own modules already read their instruments from there.

Beyond the print contract it also holds the instruments that are about *AWS
shapes* rather than about any one service, because a second service group would
otherwise copy them:

* **Model introspection** (:func:`model_enum`, :func:`input_members`) — offline
  reads of the loaded botocore model, so a command can say "this botocore has no
  such operation" instead of raising ``AttributeError``.
* **Token pagination** (:func:`paged`), whose token member names differ per API
  and are therefore parameters.
* **JSON rendering** (:func:`yaml_lines`) — nesting by indentation, one value
  per line, never wrapped, because the values worth reading in an API response
  (ARNs, ``s3://`` URIs) are exactly the ones a wrapped dump breaks.  Purely
  structural: nothing here knows a single key name.
* **Value shapes** (:func:`split_arn`, :func:`split_s3`, :func:`version_key`).
* **Watch heartbeat** (:class:`Heartbeat`, :func:`sleep_with_dots`) — the run of
  dots that makes "polled, nothing changed" visible.
* **Completion-context readers** (:func:`flag_value`, :func:`positionals`) for a
  completer that must narrow by an already-typed flag.

What stays in a subpackage's ``render`` is whatever needs a *client* or knows
that service's own vocabulary.
"""

from __future__ import annotations

import functools
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import botocore.exceptions
import botocore.session

RESET = "\033[0m"
RED = "\033[91m"
YELLOW = "\033[93m"

# The error codes an AWS API answers with for "no such resource".  One list
# because the same lookup can come back as any of them depending on the API's
# vintage — a caller probing for existence has to treat all three alike.
NOT_FOUND_CODES = ("ValidationException", "ResourceNotFound",
                   "ResourceNotFoundException")


# ─── errors ─────────────────────────────────────────────────────────────────

class SmError(Exception):
    """A user-facing failure — printed as ``error: <message>``, no traceback."""


def api_message(exc: botocore.exceptions.ClientError) -> str:
    return exc.response["Error"].get("Message", str(exc))


def error_code(exc: botocore.exceptions.ClientError) -> str:
    return exc.response["Error"].get("Code", "")


def guard(fn):
    """Turn the expected AWS/usage failures into one-line messages and exit 1.

    An uncaught ``ClientError`` would put a traceback in the middle of the
    user's session.  Every leaf handler in the ``awsut`` tree is wrapped, so a
    failure reads like a shell error instead — and returns status 1, so
    ``awsut … && next`` stops there.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except SmError as exc:
            print(f"error: {exc}", file=sys.stderr)
        except botocore.exceptions.NoCredentialsError:
            print("error: no AWS credentials found — refresh them and retry",
                  file=sys.stderr)
        except botocore.exceptions.ClientError as exc:
            print(f"error: {api_message(exc)}", file=sys.stderr)
        return 1

    return wrapper


# ─── time / size formatting ─────────────────────────────────────────────────

def fmt_dur(seconds):
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def fmt_time(dt):
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S") if dt else "-"


def fmt_bytes(size):
    if size is None:
        return "-"
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:,.1f}{unit}"
        size /= 1024


def ms_time(value):
    """A millisecond epoch (CloudWatch's timestamp unit) as a local datetime."""
    return datetime.fromtimestamp(value / 1000).astimezone() if value else None


def parse_since(text):
    """``'90m'`` / ``'24h'`` / ``'7d'`` → timezone-aware datetime, or None."""
    if not text:
        return None
    m = re.fullmatch(r"(\d+)([mhd])", text.strip())
    if not m:
        raise SmError(f"--since wants forms like 30m, 24h, 7d (got {text!r})")
    n, unit = int(m.group(1)), m.group(2)
    delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n),
             "d": timedelta(days=n)}[unit]
    return datetime.now(timezone.utc) - delta


def age(dt):
    return (datetime.now(timezone.utc) - dt).total_seconds() if dt else None


def elapsed_of(started, ended):
    """Wall time between two API timestamps, or the age of *started*."""
    if started and ended:
        return (ended - started).total_seconds()
    return age(started)


# ─── listings ───────────────────────────────────────────────────────────────

def print_header(*parts) -> None:
    """The line above a table: how many, of what, from where.

    Joined with ``·`` and followed by a blank line, so the count and the scope
    read as one fact and the table below starts clean.  Empty parts drop out,
    which lets a caller pass a conditional scope without composing the string
    itself.
    """
    print(" · ".join(str(p) for p in parts if p) + "\n")


def print_table(header, rows, note=None, colorize=None) -> None:
    """Column widths from the data, so no identifier is ever truncated.

    ``colorize(col_index, value)`` may return an ANSI colour for a cell (a
    severity column, typically).  It is applied *after* padding and only on a
    TTY, so the escape bytes can never throw the alignment off or land in a
    file.

    No rows draws no table — the header line above it already said "zero" — but
    the *note* still prints, because "everything that matched was filtered out"
    is precisely the empty listing that needs explaining.
    """
    if not rows:
        if note:
            print(note)
        return
    widths = [max([len(h)] + [len(r[i]) for r in rows])
              for i, h in enumerate(header)]
    tty = colorize is not None and sys.stdout.isatty()

    def line(cells, paint=False):
        out = []
        for i, (value, width) in enumerate(zip(cells, widths)):
            # The trailing column needs no padding.
            text = value if i == len(widths) - 1 else value.ljust(width)
            color = colorize(i, value) if paint and tty else None
            out.append(f"{color}{text}{RESET}" if color else text)
        return "  ".join(out).rstrip()

    print(line(header))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(line(row, paint=True))
    if note:
        print(f"\n{note}")


def print_labeled(pairs) -> None:
    """``Label  value`` lines, labels padded to one width.

    The identifier block a detail view opens with: the API's own field names on
    the left, values unwrapped on the right so they can be copied out.  A pair
    whose value is empty is dropped (the caller doesn't have to branch), and a
    ``None`` in place of a pair prints a blank line — a group separator that
    still shares the one label width.
    """
    kept = [p for p in pairs if p is None or p[1] not in (None, "")]
    labels = [p[0] for p in kept if p is not None]
    if not labels:
        return
    width = max(len(label) for label in labels)
    for pair in kept:
        if pair is None:
            print()
            continue
        label, value = pair
        print(f"{label.ljust(width)}  {value}")


def section(label) -> None:
    """A named block inside one command's output.

    Two uses, one look: the repeated blocks a fan-out command emits (per node,
    per log stream, per S3 location) and the named blocks a detail view prints
    after its identifier block (``FailureReason``, ``Tags``, a document).  Both
    are "here starts a differently-shaped piece of output", which is what the
    rule marks.
    """
    print(f"--- {label} ---")


# ─── model introspection (offline — no API call) ─────────────────────────────

@functools.lru_cache(maxsize=None)
def model_enum(service_name: str, operation: str, member: str) -> tuple[str, ...]:
    """An input member's enum, read out of the loaded botocore model. Offline.

    Keyed by service name because a ``var <service>_service_name=`` can point a
    client at a different (e.g. private) model, whose enums are its own.  A
    refreshed model contributes new values without a code change at the call
    site.
    """
    try:
        model = botocore.session.Session().get_service_model(service_name)
        shape = model.operation_model(operation).input_shape.members[member]
        return tuple(getattr(shape, "enum", None) or ())
    except Exception:
        return ()


@functools.lru_cache(maxsize=None)
def input_members(service_name: str, operation: str) -> frozenset[str]:
    """An operation's input member names, from the loaded botocore model. Offline.

    botocore rejects a parameter its model does not declare *before* the call
    goes out, with a ``ParamValidationError`` that reads like a caller bug.  A
    command that sends an optional-but-recent member asks here first, so an old
    model produces an explanation instead.
    """
    try:
        model = botocore.session.Session().get_service_model(service_name)
        return frozenset(model.operation_model(operation).input_shape.members)
    except Exception:
        return frozenset()


# ─── pagination ─────────────────────────────────────────────────────────────

def paged(call, key, max_items, *, token_param="NextToken",
          token_key="NextToken", **params):
    """Collect up to *max_items* from a token-paginated list call.

    The token names are parameters because they are not spelled the same across
    APIs: S3's ``list_objects_v2`` sends ``ContinuationToken`` and returns
    ``NextContinuationToken``, and the newer lowercase-member services (Bedrock
    AgentCore among them) use ``nextToken``.  Defaulting silently would stop
    after the first page and report a short listing as a complete one.
    """
    items = []
    token = None
    while len(items) < max_items:
        if token:
            params[token_param] = token
        resp = call(**params)
        items.extend(resp.get(key, []))
        token = resp.get(token_key)
        if not token:
            break
    return items[:max_items]


# ─── JSON rendering ─────────────────────────────────────────────────────────

def parse_doc(doc):
    """A JSON string as parsed data, or None if it isn't parseable.

    Several of these APIs model a configuration document as an opaque string, so
    what comes back is plain JSON data with no schema this code can rely on:
    nothing here — or in the renderer below — knows a single key name, and a
    schema version never seen before renders the same way.  A value that is
    already parsed passes through.
    """
    try:
        return json.loads(doc) if isinstance(doc, str) else doc
    except (TypeError, ValueError):
        return None


def yaml_scalar(value):
    """A leaf value as one unquoted token, quoted only where bareness misleads."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, dict):
        return "{}"
    if isinstance(value, list):
        return "[]"
    text = str(value)
    # Quote only what would misread as structure: a value opening with '{' would
    # look like broken nesting, and ': ' like a key.  S3 URIs and ARNs contain
    # ':' but not ': ', so they stay bare and unwrapped — they are the copy
    # targets.
    if (not text or text.strip() != text or ": " in text
            or text[0] in "{[#&*!|>%@" or text.startswith("- ")):
        return json.dumps(text)
    return text


def yaml_lines(node, indent=0):
    """Parsed JSON as YAML-ish indented lines — a reading aid, purely structural.

    One-line JSON is unreadable at these depths, and the values worth reading
    (S3 URIs, ARNs) are exactly the ones a wrapped dump breaks.  So: nesting by
    indentation, one value per line, never truncated or wrapped.

    A string whose content is itself JSON is expanded rather than printed as an
    escaped blob — that is a shape check, not a key check, so it needs no schema
    knowledge.
    """
    pad = "  " * indent
    out = []

    if isinstance(node, dict):
        for key, value in node.items():
            nested = parse_doc(value) if _looks_like_json(value) else None
            if nested is not None:
                out.append(f"{pad}{key}:  # embedded JSON, expanded")
                out.extend(yaml_lines(nested, indent + 1))
            elif isinstance(value, (dict, list)) and value:
                out.append(f"{pad}{key}:")
                out.extend(yaml_lines(value, indent + 1))
            elif isinstance(value, str) and "\n" in value:
                out.append(f"{pad}{key}: |")
                out.extend(f"{pad}  {line}" for line in value.splitlines())
            else:
                out.append(f"{pad}{key}: {yaml_scalar(value)}")

    elif isinstance(node, list):
        for value in node:
            if isinstance(value, (dict, list)) and value:
                sub = yaml_lines(value, indent + 1)
                # Hang the item's first line off the dash so the entry reads as
                # one unit.
                out.append(f"{pad}- {sub[0].lstrip()}")
                out.extend(sub[1:])
            else:
                out.append(f"{pad}- {yaml_scalar(value)}")

    else:
        out.append(f"{pad}{yaml_scalar(node)}")

    return out


def _looks_like_json(value):
    return isinstance(value, str) and value.strip()[:1] in ("{", "[")


# ─── value shapes ───────────────────────────────────────────────────────────

def split_arn(value):
    """``('service', 'region', 'account', 'resource')`` for an ARN, else None.

    Structural only — the resource segment is what says which API can resolve
    it, so nothing here needs to know what any particular resource is called.
    """
    if not isinstance(value, str) or not value.startswith("arn:"):
        return None
    parts = value.split(":", 5)
    if len(parts) < 6:
        return None
    _arn, _partition, service, region, account, resource = parts
    return service, region, account, resource


def split_s3(uri):
    """``('bucket', 'key-prefix')`` for an ``s3://`` URI."""
    rest = uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    return bucket, key


def version_key(text):
    """Sort key for a dotted version: numeric where possible (0.0.10 > 0.0.9)."""
    parts = re.split(r"[.\-+]", str(text or ""))
    return tuple(int(p) if p.isdigit() else -1 for p in parts)


# ─── watch heartbeat ────────────────────────────────────────────────────────

# Heartbeat cadence for the watch loops, independent of the poll interval: the
# dots exist to prove the loop is alive between polls, so they must not slow
# down when --interval grows.
DOT_INTERVAL = 5


class Heartbeat:
    """A run of ``.`` on one line meaning "polled, nothing changed".

    Any real output must call :meth:`clear` first, otherwise the change line
    lands mid-dots and the timeline becomes unreadable.
    """

    def __init__(self, width=60):
        self.col = 0
        self.width = width
        # Dot phase lives here, not in the sleep call, so the cadence is
        # wall-clock: restarting it per poll would let the poll's own duration
        # eat a dot.
        self.next_dot = None

    def tick(self):
        sys.stdout.write(".")
        self.col += 1
        if self.col >= self.width:
            sys.stdout.write("\n")
            self.col = 0
        sys.stdout.flush()

    def clear(self):
        if self.col:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.col = 0


def sleep_with_dots(seconds, beat):
    """Sleep, emitting a heartbeat dot every ``DOT_INTERVAL`` seconds of the wait."""
    end = time.monotonic() + seconds
    if beat.next_dot is None:
        beat.next_dot = time.monotonic() + DOT_INTERVAL
    while True:
        now = time.monotonic()
        if now >= end:
            return
        if now >= beat.next_dot:
            beat.tick()
            # Re-base on the actual tick rather than accumulating, so a slow
            # poll doesn't come back and fire a burst of catch-up dots.
            beat.next_dot = now + DOT_INTERVAL
            continue
        time.sleep(min(beat.next_dot, end) - now)


# ─── flag reading from a completion context ─────────────────────────────────

def flag_value(args, *names):
    """The value of ``--flag VALUE`` / ``--flag=VALUE`` in a token list.

    Completion contexts hand a completer every token typed so far except the
    consumed sub-command names (``ctx.args``), flags included.  A positional or
    value completer that needs to narrow by an already-typed flag — the hub for
    a content name, the harness for an endpoint name — reads it from here.
    """
    for i, tok in enumerate(args):
        for name in names:
            if tok == name and i + 1 < len(args):
                return args[i + 1]
            if tok.startswith(name + "="):
                return tok[len(name) + 1:]
    return None


def positionals(args, value_flags=()):
    """The positional tokens in ``ctx.args``, with flags and their values dropped.

    ``ctx.args`` carries flags interleaved with positionals, so a completer that
    keys off "the first positional" — the content name a version list belongs to,
    say — has to drop the flags *and* the token after each value-taking one.  A
    flag's value is indistinguishable from a positional otherwise: in
    ``--hub MyHub <TAB>``, ``MyHub`` is not the name being completed.
    """
    out = []
    skip = False
    for tok in args:
        if skip:
            skip = False
            continue
        if tok.startswith("-"):
            skip = tok in value_flags        # `--flag=value` needs no skip
            continue
        out.append(tok)
    return out
