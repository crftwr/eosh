"""``awsut recent-cost`` — a Cost Explorer cross-tab you drill into.

Three knobs, each independent:

* **what to look at** — one flag per Cost Explorer dimension (``--service``,
  ``--region``, ``--usage-type``, …) plus ``--tag KEY=VALUE``.  A flag given
  twice ORs its values; different flags AND together.
* **how to slice it** — ``--by ROW[,COL]`` picks the GroupBy axes.  Left out,
  the rows go one level below what is filtered (service → usage type →
  operation) and the columns are regions, unless the regions are filtered.
* **over what time** — ``--days N``, and ``--daily`` to put the days in the
  columns instead of summing them.

Service names have spaces ("Amazon Elastic Compute Cloud - Compute"), which a
TAB-completed word handles badly: the picker treats the space as the end of
the word.  So ``--service`` completes to a *slug* —
``elastic-compute-cloud-compute``, the full name shown beside it — and the
leaf maps a slug (or a unique slug prefix, or the full name in any case)
back to the real name before it queries.

Every Cost Explorer request is billed (USD 0.01 each), so the leaf only asks
for dimension values when a flag needs resolving, and pages are capped.

Attached from ``cli.register()`` like the ``sagemaker`` and ``agentcore``
groups, since this module does ``from . import cli``.
"""

from __future__ import annotations

import datetime
import json
import re
import sys
from dataclasses import dataclass

from eosh import color_enabled
from eosh.commands import arg
from eosh.completion import Completer, Completion, CompletionContext
from eosh.completion_cache import aws_env_key, get_or_fetch

from . import cli as awsut
from .common import RED, RESET, YELLOW, SmError, flag_value, guard, print_header

METRIC = "AmortizedCost"
DEFAULT_DAYS = 14
# Completion asks for values seen in this window unless --days is on the line.
COMPLETION_DAYS = 30
# get_dimension_values / get_tags pages read per question (each one is billed).
MAX_VALUE_PAGES = 5


@dataclass(frozen=True)
class Axis:
    name: str          # flag and --by name: --usage-type, --by usage-type
    key: str           # Cost Explorer dimension
    label: str         # table header
    # Values have spaces (or odd case): complete to a slug, resolve it back.
    slugged: bool = False

    @property
    def flag(self) -> str:
        return f"--{self.name}"

    @property
    def dest(self) -> str:
        return self.name.replace("-", "_")


AXES: dict[str, Axis] = {a.name: a for a in (
    Axis("service",       "SERVICE",        "SERVICE",       slugged=True),
    Axis("region",        "REGION",         "REGION"),
    Axis("usage-type",    "USAGE_TYPE",     "USAGE TYPE"),
    Axis("operation",     "OPERATION",      "OPERATION"),
    Axis("account",       "LINKED_ACCOUNT", "ACCOUNT"),
    Axis("instance-type", "INSTANCE_TYPE",  "INSTANCE TYPE"),
    Axis("purchase-type", "PURCHASE_TYPE",  "PURCHASE TYPE", slugged=True),
    Axis("record-type",   "RECORD_TYPE",    "RECORD TYPE",   slugged=True),
)}

# Default rows: the first of these the filters leave open.
_DRILL_CHAIN = ("service", "usage-type", "operation")


# ─── slugs ──────────────────────────────────────────────────────────────────

def slug(value: str) -> str:
    """``Amazon Elastic Compute Cloud - Compute`` → ``elastic-compute-cloud-compute``.

    Lower-case, every run of non-alphanumerics a single ``-``, and the vendor
    prefix dropped — it is on half the names and tells none of them apart.
    """
    s = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return re.sub(r"^(amazon|aws)-(?=.)", "", s)


def resolve(given: str, known, what: str) -> list[str]:
    """The real values *given* names, out of *known*.

    Tried in order — exact, case-insensitive, slug, unique slug prefix — so a
    full name always wins and a short slug works as long as it is unambiguous.
    """
    if given in known:
        return [given]
    low = given.lower()
    hits = sorted(k for k in known if k.lower() == low)
    if hits:
        return hits
    want = slug(given)
    hits = sorted(k for k in known if slug(k) == want)
    if hits:
        return hits
    hits = sorted(k for k in known if want and slug(k).startswith(want))
    if len(hits) == 1:
        return hits
    if not hits:
        raise SmError(f"no {what} matches {given!r} in this period")
    raise SmError(f"{given!r} matches several {what}s: "
                  + ", ".join(slug(h) for h in hits))


# ─── query building ─────────────────────────────────────────────────────────

def period(days: int, today: datetime.date | None = None) -> dict:
    """The last *days* days, today included (``End`` is exclusive)."""
    today = today or datetime.datetime.now().date()
    end = today + datetime.timedelta(days=1)
    start = end - datetime.timedelta(days=days)
    return {"Start": start.isoformat(), "End": end.isoformat()}


def parse_tags(items) -> dict[str, list[str]]:
    tags: dict[str, list[str]] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key or not value:
            raise SmError(f"--tag wants KEY=VALUE, got {item!r}")
        tags.setdefault(key, []).append(value)
    return tags


def build_filter(dims: dict[str, list[str]], tags: dict[str, list[str]]):
    """Cost Explorer ``Filter``: values of one flag OR, flags AND."""
    clauses = [{"Dimensions": {"Key": AXES[name].key, "Values": values}}
               for name, values in dims.items() if values]
    clauses += [{"Tags": {"Key": key, "Values": values}}
                for key, values in tags.items()]
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"And": clauses}


def parse_by(text: str) -> list[str]:
    names = [n.strip() for n in text.split(",")]
    if not 1 <= len(names) <= 2:
        raise SmError("--by takes one or two axes: ROW or ROW,COL")
    if len(set(names)) != len(names):
        raise SmError(f"--by names {names[0]!r} twice")
    for n in names:
        if n not in AXES and not (n.startswith("tag:") and len(n) > 4):
            raise SmError(f"unknown --by axis {n!r} (one of: "
                          + ", ".join(AXES) + ", tag:KEY)")
    return names


def default_by(filtered: set[str], daily: bool) -> list[str]:
    row = next((a for a in _DRILL_CHAIN if a not in filtered), _DRILL_CHAIN[-1])
    if daily or "region" in filtered:
        return [row]
    return [row, "region"]


def group_by(name: str) -> dict:
    if name.startswith("tag:"):
        return {"Type": "TAG", "Key": name[4:]}
    return {"Type": "DIMENSION", "Key": AXES[name].key}


def axis_label(name: str) -> str:
    return f"TAG {name[4:]}" if name.startswith("tag:") else AXES[name].label


def key_label(name: str, key: str) -> str:
    """A group key as shown: tag keys come back as ``Key$value``."""
    if name.startswith("tag:"):
        value = key.partition("$")[2]
        return value or "(untagged)"
    return key


# ─── API calls ──────────────────────────────────────────────────────────────

def dimension_values(client, key: str, time_period: dict, flt=None) -> dict[str, str]:
    """``{value: description}`` for one dimension (the account name, for
    ``LINKED_ACCOUNT``; empty for most)."""
    params = {"TimePeriod": time_period, "Dimension": key,
              "Context": "COST_AND_USAGE"}
    if flt:
        params["Filter"] = flt
    out: dict[str, str] = {}
    for _ in range(MAX_VALUE_PAGES):
        response = client.get_dimension_values(**params)
        for d in response.get("DimensionValues", []):
            out[d["Value"]] = (d.get("Attributes") or {}).get("description", "")
        token = response.get("NextPageToken")
        if not token:
            break
        params["NextPageToken"] = token
    return out


def tag_values(client, time_period: dict, tag_key: str | None = None) -> list[str]:
    """Tag keys, or with *tag_key* the values seen for it."""
    params = {"TimePeriod": time_period}
    if tag_key:
        params["TagKey"] = tag_key
    out: list[str] = []
    for _ in range(MAX_VALUE_PAGES):
        response = client.get_tags(**params)
        out += [t for t in response.get("Tags", []) if t]
        token = response.get("NextPageToken")
        if not token:
            break
        params["NextPageToken"] = token
    return out


def cost_groups(client, params: dict):
    """``(period start, group keys, amount)`` for every group, every page."""
    params = dict(params)
    while True:
        response = client.get_cost_and_usage(**params)
        for item in response["ResultsByTime"]:
            start = item["TimePeriod"]["Start"]
            for group in item.get("Groups", []):
                yield (start, group["Keys"],
                       float(group["Metrics"][METRIC]["Amount"]))
        token = response.get("NextPageToken")
        if not token:
            return
        params["NextPageToken"] = token


def resolve_dims(client, given: dict[str, list[str]], time_period: dict):
    """Map every slugged flag's values to real ones; the rest pass through."""
    out: dict[str, list[str]] = {}
    for name, values in given.items():
        if not values:
            continue
        axis = AXES[name]
        if not axis.slugged:
            out[name] = list(dict.fromkeys(values))
            continue
        known = dimension_values(client, axis.key, time_period)
        resolved: list[str] = []
        for v in values:
            resolved += resolve(v, known, name)
        out[name] = list(dict.fromkeys(resolved))
    return out


# ─── the table ──────────────────────────────────────────────────────────────

def tabulate(groups, by: list[str], daily: bool, time_period: dict):
    """Fold ``(start, keys, amount)`` into ``{row: {col: amount}}``.

    Daily: the columns are the days and every axis goes into the row label.
    Otherwise: rows are the first axis, columns the second ("" when none).
    """
    cells: dict[str, dict[str, float]] = {}
    for start, keys, amount in groups:
        shown = [key_label(n, k) for n, k in zip(by, keys)]
        if daily:
            row, col = " · ".join(shown), start
        else:
            row, col = shown[0], (shown[1] if len(shown) > 1 else "")
        cells.setdefault(row, {})
        cells[row][col] = cells[row].get(col, 0.0) + amount
    return cells


def days_in(time_period: dict) -> list[str]:
    day = datetime.date.fromisoformat(time_period["Start"])
    end = datetime.date.fromisoformat(time_period["End"])
    out = []
    while day < end:
        out.append(day.isoformat())
        day += datetime.timedelta(days=1)
    return out


def render(cells, *, row_label: str, cols: list[str], col_labels: list[str],
           min_amount: float, out=None) -> int:
    """Print the cross-tab; return how many rows it showed.

    A cross-tab, not a resource listing: the cells are money, so they are
    right-aligned, and the last row is the column totals below a second rule.
    ``print_table`` expresses neither, so the frame is drawn here — to the
    same shape it draws (labels, ``-`` rule, two-space gaps).  With no
    columns the TOTAL column is the only one.
    """
    out = out or sys.stdout
    row_totals = {r: sum(c.values()) for r, c in cells.items()}
    rows = sorted((r for r, t in row_totals.items() if abs(t) >= min_amount),
                  key=lambda r: row_totals[r], reverse=True)
    if not rows:
        return 0
    col_totals = {c: sum(cells[r].get(c, 0.0) for r in rows) for c in cols}
    grand = sum(row_totals[r] for r in rows)

    # Red for the biggest cell, yellow for the top tenth — over the money
    # cells, or the row totals when they are the only column.
    if cols:
        values = [cells[r].get(c, 0.0) for r in rows for c in cols]
    else:
        values = [row_totals[r] for r in rows]
    values = sorted((v for v in values if v > 0), reverse=True)
    max_val = values[0] if values else 0.0
    yellow_at = values[max(1, len(values) // 10) - 1] if values else 0.0
    tty = color_enabled(out)

    def money(val, w, hot=True):
        text = f"{val:{w}.2f}"
        if not (tty and hot) or val <= 0:
            return text
        if val >= max_val:
            return f"{RED}{text}{RESET}"
        if val >= yellow_at:
            return f"{YELLOW}{text}{RESET}"
        return text

    label_w = max([len(row_label), len("TOTAL")] + [len(r) for r in rows])
    col_w = {c: max(len(lbl), len(f"{col_totals[c]:.2f}"),
                    *(len(f"{cells[r].get(c, 0.0):.2f}") for r in rows))
             for c, lbl in zip(cols, col_labels)}
    total_w = max(len("TOTAL"), len(f"{grand:.2f}"))
    widths = [label_w] + [col_w[c] for c in cols] + [total_w]

    def line(label, cells_):
        print("  ".join([label.ljust(label_w)] + cells_).rstrip(), file=out)

    def rule():
        print("  ".join("-" * w for w in widths), file=out)

    line(row_label, [lbl.rjust(col_w[c]) for c, lbl in zip(cols, col_labels)]
                    + ["TOTAL".rjust(total_w)])
    rule()
    for r in rows:
        row_cells = []
        for c in cols:
            v = cells[r].get(c, 0.0)
            row_cells.append(money(v, col_w[c]) if abs(v) >= 0.005
                             else " " * col_w[c])
        row_cells.append(money(row_totals[r], total_w, hot=not cols))
        line(r, row_cells)
    rule()
    line("TOTAL", [f"{col_totals[c]:{col_w[c]}.2f}" for c in cols]
                  + [f"{grand:{total_w}.2f}"])
    return len(rows)


# ─── completers ─────────────────────────────────────────────────────────────

def _flag_values(args, flag: str) -> list[str]:
    out = []
    for i, tok in enumerate(args):
        if tok == flag and i + 1 < len(args):
            out.append(args[i + 1])
        elif tok.startswith(flag + "="):
            out.append(tok[len(flag) + 1:])
    return out


def _line_period(args) -> dict:
    try:
        days = int(flag_value(args, "--days") or COMPLETION_DAYS)
    except ValueError:
        days = COMPLETION_DAYS
    return period(max(1, days))


def _cached_values(key: str, time_period: dict, flt=None) -> dict[str, str]:
    cache_key = ("awsut.cost_values", aws_env_key(), key,
                 json.dumps(time_period, sort_keys=True),
                 json.dumps(flt, sort_keys=True))
    return get_or_fetch(cache_key, lambda: dimension_values(
        awsut._get_boto3_client("ce"), key, time_period, flt))


def _line_scope(args, time_period: dict, skip: str | None = None):
    """The filter the line already spells out, so ``--service sagemaker
    --usage-type <TAB>`` offers only SageMaker's usage types.  A value that
    does not resolve (yet) is left out of the scope rather than failing."""
    dims: dict[str, list[str]] = {}
    for name, axis in AXES.items():
        if name == skip:
            continue
        values = _flag_values(args, axis.flag)
        if not values:
            continue
        if axis.slugged:
            known = _cached_values(axis.key, time_period)
            resolved = []
            for v in values:
                try:
                    resolved += resolve(v, known, name)
                except SmError:
                    pass
            values = resolved
        if values:
            dims[name] = values
    try:
        tags = {} if skip == "tag" else parse_tags(_flag_values(args, "--tag"))
    except SmError:
        tags = {}
    return build_filter(dims, tags)


class DimensionCompleter(Completer):
    """Values of one dimension seen in the period, narrowed by the line's
    other filters.  Slugged axes complete to the slug, real name beside it."""

    def __init__(self, name: str):
        self.axis = AXES[name]

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            time_period = _line_period(ctx.args)
            scope = _line_scope(ctx.args, time_period, skip=self.axis.name)
            values = _cached_values(self.axis.key, time_period, scope)
        except Exception:
            return []
        out: dict[str, Completion] = {}
        show = slug if self.axis.slugged else (lambda v: v)
        for value, desc in sorted(values.items(), key=lambda kv: show(kv[0])):
            shown = show(value)
            if not shown.startswith(ctx.prefix) or shown in out:
                continue
            note = value if shown != value else desc
            out[shown] = Completion(value=shown, description=note)
        return list(out.values())


class TagCompleter(Completer):
    """``KEY=`` first (the picker leaves the caret after ``=``), then
    ``KEY=VALUE`` for the values Cost Explorer has seen for that key."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            time_period = _line_period(ctx.args)
            key, sep, _ = ctx.prefix.partition("=")
            if not sep:
                keys = get_or_fetch(
                    ("awsut.cost_tags", aws_env_key(),
                     json.dumps(time_period, sort_keys=True)),
                    lambda: tag_values(awsut._get_boto3_client("ce"), time_period))
                return [Completion(value=f"{k}=") for k in sorted(keys)
                        if f"{k}=".startswith(ctx.prefix)]
            values = get_or_fetch(
                ("awsut.cost_tags", aws_env_key(),
                 json.dumps(time_period, sort_keys=True), key),
                lambda: tag_values(awsut._get_boto3_client("ce"),
                                   time_period, key))
        except Exception:
            return []
        return [Completion(value=f"{key}={v}") for v in sorted(values)
                if f"{key}={v}".startswith(ctx.prefix)]


class ByCompleter(Completer):
    """Axis names, completing the part after the last comma; ``tag:KEY``
    once the word starts with ``t`` (no axis does, so the tag keys — one
    billed request — are only fetched when they are wanted)."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        head, comma, last = ctx.prefix.rpartition(",")
        lead = head + comma
        taken = set(head.split(",")) if comma else set()
        names = [n for n in AXES if n not in taken]
        if last.startswith("t"):
            try:
                time_period = _line_period(ctx.args)
                keys = get_or_fetch(
                    ("awsut.cost_tags", aws_env_key(),
                     json.dumps(time_period, sort_keys=True)),
                    lambda: tag_values(awsut._get_boto3_client("ce"), time_period))
                names += [f"tag:{k}" for k in sorted(keys)]
            except Exception:
                pass
        return [Completion(value=lead + n) for n in names if n.startswith(last)]


# ─── the leaf ───────────────────────────────────────────────────────────────

def register_recent_cost(awsut_root) -> None:
    dim_params = [
        arg(axis.flag, action="append", metavar="VALUE", dest=axis.dest,
            completer=DimensionCompleter(name),
            help=f"Only this {name.replace('-', ' ')} (repeat to OR)"
                 + (" — a slug or the full name" if axis.slugged else ""))
        for name, axis in AXES.items()
    ]

    @awsut_root.command(
        "recent-cost",
        help="Show recent cost as a table: filter with --service/--region/"
             "--usage-type/… (repeat a flag to OR, combine flags to AND), "
             "slice with --by ROW[,COL], --daily for days as columns",
        params=[
            arg("--days", type=int, default=DEFAULT_DAYS, metavar="N",
                help="Number of days to show"),
            arg("--daily", action="store_true",
                help="One column per day instead of summing the period"),
            arg("--by", metavar="ROW[,COL]", completer=ByCompleter(),
                help="Group by these axes ("
                     + ", ".join(AXES) + ", tag:KEY); default: one level "
                     "below the filters, by region"),
            *dim_params,
            arg("--tag", action="append", metavar="KEY=VALUE",
                completer=TagCompleter(),
                help="Only resources with this tag (repeat to OR per key)"),
            arg("--min", type=float, default=0.1, dest="min_amount",
                metavar="AMOUNT",
                help="Hide rows/columns with total below this amount"),
        ],
    )
    @guard
    def _recent_cost(days, daily, by, tag, min_amount, **dims):
        if days < 1:
            raise SmError("--days must be at least 1")
        client = awsut._get_boto3_client("ce")
        time_period = period(days)

        tags = parse_tags(tag)
        resolved = resolve_dims(client, dims, time_period)
        axes = parse_by(by) if by else default_by(set(resolved), daily)

        params = {
            "TimePeriod": time_period,
            "Granularity": "DAILY" if daily else "MONTHLY",
            "Metrics": [METRIC],
            "GroupBy": [group_by(n) for n in axes],
        }
        flt = build_filter(resolved, tags)
        if flt:
            params["Filter"] = flt

        cells = tabulate(cost_groups(client, params), axes, daily, time_period)

        if daily:
            row_label = " · ".join(axis_label(n) for n in axes)
            cols = days_in(time_period)
            col_labels = [c[5:] for c in cols]           # MM-DD
            col_part = f"{len(cols)} day(s)"
        else:
            row_label = axis_label(axes[0])
            col_part = ""
            cols, col_labels = [], []
            if len(axes) > 1:
                col_totals: dict[str, float] = {}
                for row in cells.values():
                    for c, v in row.items():
                        col_totals[c] = col_totals.get(c, 0.0) + v
                cols = sorted((c for c, t in col_totals.items()
                               if abs(t) >= min_amount),
                              key=lambda c: col_totals[c], reverse=True)
                col_labels = cols
                col_part = f"{len(cols)} {axis_label(axes[1]).lower()}(s)"

        row_totals = {r: sum(c.values()) for r, c in cells.items()}
        n_rows = sum(1 for t in row_totals.values() if abs(t) >= min_amount)
        noun = (f"{row_label.lower()}(s)" if len(axes) == 1 or not daily
                else "row(s)")
        scope = [f"{n.replace('-', ' ')} {', '.join(v)}"
                 for n, v in resolved.items()]
        scope += [f"tag {k}={', '.join(v)}" for k, v in tags.items()]
        print_header(
            f"{n_rows} {noun}" + (f" × {col_part}" if col_part else ""),
            f"last {days} days",
            *scope,
            "daily" if daily else "",
            "amortized cost",
        )
        if not n_rows or (len(axes) > 1 and not daily and not cols):
            print(f"nothing above --min {min_amount} in this period")
            return
        render(cells, row_label=row_label, cols=cols, col_labels=col_labels,
               min_amount=min_amount)
