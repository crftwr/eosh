"""Tests for the ``awsut sagemaker`` recipe (jobs, hub content, Studio, HyperPod).

Nothing here touches AWS.  The four things worth pinning are:

* the renderers never truncate an identifier and never interpret a document by
  key name (:mod:`render`),
* category / content-type probing behaves the same whether the value came from
  a flag, a group selector, or an ARN (:mod:`jobs`, :mod:`hub`),
* Studio's domain/space resolution never picks silently when it is ambiguous,
  and what ``start`` / ``url`` / ``stop`` send is read off the space rather
  than assumed (:mod:`studio`),
* the command tree offers each flag only where it applies.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone

import botocore.exceptions
import pytest

from pitash.commands import CmdParser, _collect_inherited_params
from pitash.commands import registry as command_registry
from pitash.completion import CompletionContext
from pitash.recipes import awsut as awsut_recipe
from pitash.recipes._awsut_sagemaker import hub, hyperpod, jobs, render, studio
from pitash.tui import _compose_meta, _meta_col_widths


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def client_error(code="ValidationException", message="nope", op="DescribeJob"):
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": message}}, op)


class FakeClient:
    """A boto3-shaped stub: only the operations handed in exist on it.

    ``require_operation`` gates on ``hasattr``, so a client that lacks an
    operation must really lack the attribute — hence ``__getattr__`` rather
    than a method that raises.
    """

    def __init__(self, **ops):
        self._ops = ops
        self.calls = []

    def __getattr__(self, name):
        if name not in self._ops:
            raise AttributeError(name)
        impl = self._ops[name]

        def call(**params):
            self.calls.append((name, params))
            return impl(**params) if callable(impl) else impl

        return call


class FakeLogs:
    """A CloudWatch Logs stub, including the ``exceptions`` namespace.

    Shared by the ``studio log`` and ``jobs log`` tests, because both drive the
    same reader in :mod:`render`.
    """

    class ResourceNotFoundException(Exception):
        pass

    def __init__(self, events=None, streams=None, missing=False):
        self.events = events or []
        self.streams = streams or []
        self.missing = missing
        self.calls = []
        self.exceptions = self

    def get_log_events(self, **params):
        self.calls.append(("get_log_events", params))
        if self.missing:
            raise self.ResourceNotFoundException()
        return {"events": self.events, "nextForwardToken": "f/1"}

    def describe_log_streams(self, **params):
        self.calls.append(("describe_log_streams", params))
        if self.missing:
            raise self.ResourceNotFoundException()
        # Name order, as the real API returns for a prefixed query — never
        # chronological.
        streams = sorted(self.streams, key=lambda s: s["logStreamName"])
        return {"logStreams": streams}


def ctx(prefix="", args=None, command="awsut"):
    args = args or []
    return CompletionContext(command=command, args=args, arg_index=len(args),
                             prefix=prefix, line="", shell_context=None)


# Invented category names for the extras mechanism.  ``EXTRA_JOB_CATEGORIES`` is
# empty by default — the loaded model is the source of truth — so a test that
# exercises "a category this model does not declare" supplies its own.
EXTRA_CATEGORIES = ["MyCategory", "OtherCategory"]


@pytest.fixture
def extras(monkeypatch):
    monkeypatch.setattr(jobs, "EXTRA_JOB_CATEGORIES", EXTRA_CATEGORIES)
    return EXTRA_CATEGORIES


# ---------------------------------------------------------------------------
# render — time / size formatting
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,delta", [
    ("30m", timedelta(minutes=30)),
    ("6h", timedelta(hours=6)),
    ("7d", timedelta(days=7)),
    (" 24h ", timedelta(hours=24)),
])
def test_parse_since_accepts_m_h_d(text, delta):
    before = datetime.now(timezone.utc) - delta
    got = render.parse_since(text)
    assert got.tzinfo is not None
    assert abs((got - before).total_seconds()) < 5


def test_parse_since_empty_means_no_window():
    assert render.parse_since(None) is None
    assert render.parse_since("") is None


@pytest.mark.parametrize("bad", ["24", "h", "1w", "-1h", "1.5h", "24 h"])
def test_parse_since_rejects_other_forms(bad):
    with pytest.raises(render.SmError) as exc:
        render.parse_since(bad)
    assert "30m, 24h, 7d" in str(exc.value)


@pytest.mark.parametrize("secs,text", [
    (None, "-"), (0, "0s"), (59, "59s"),
    (60, "1m00s"), (125, "2m05s"), (3599, "59m59s"),
    (3600, "1h00m"), (7380, "2h03m"),
])
def test_fmt_dur(secs, text):
    assert render.fmt_dur(secs) == text


def test_fmt_bytes_scales_and_keeps_bytes_integral():
    assert render.fmt_bytes(None) == "-"
    assert render.fmt_bytes(512) == "512B"
    assert render.fmt_bytes(2048) == "2.0KB"
    assert render.fmt_bytes(5 * 1024 ** 3) == "5.0GB"


def test_elapsed_of_uses_both_stamps_when_finished():
    assert render.elapsed_of(utc(2026, 1, 1, 0, 0), utc(2026, 1, 1, 0, 5)) == 300


def test_elapsed_of_falls_back_to_age_while_running():
    started = datetime.now(timezone.utc) - timedelta(seconds=90)
    assert 85 < render.elapsed_of(started, None) < 120


def test_fmt_time_of_none_is_a_dash():
    assert render.fmt_time(None) == "-"


# ---------------------------------------------------------------------------
# render — tables never truncate an identifier
# ---------------------------------------------------------------------------

def test_print_table_widths_come_from_the_data(capsys):
    arn = "arn:aws:sagemaker:us-west-2:123456789012:hub-content/MyHub/DataSet/some-long-name/0.0.2"
    render.print_table(["NAME", "ARN"], [["a", arn], ["a-much-longer-name", "-"]])
    out = capsys.readouterr().out.splitlines()
    assert arn in out[2]            # in full, unwrapped
    # The rule stretches to the widest cell in each column, header included.
    assert out[1].split("  ") == ["-" * len("a-much-longer-name"), "-" * len(arn)]
    # The trailing column is never padded.
    assert out[2] == out[2].rstrip()


def test_print_table_prints_no_column_labels_for_no_rows(capsys):
    """The header line above the table already said "zero"."""
    render.print_table(["NAME"], [])
    assert capsys.readouterr().out == ""


def test_print_table_note_is_separated_from_the_rows(capsys):
    render.print_table(["N"], [["1"]], note="caveat")
    assert capsys.readouterr().out.endswith("\ncaveat\n")


def test_print_table_still_prints_the_note_with_no_rows(capsys):
    """An empty listing is exactly when "everything was filtered out" matters.

    Dropping the note with the table would make a filtered-to-nothing listing
    indistinguishable from a genuinely empty one.
    """
    render.print_table(["N"], [], note="3 row(s) hidden")
    assert capsys.readouterr().out == "3 row(s) hidden\n"


# ---------------------------------------------------------------------------
# render — documents are reformatted, never interpreted
# ---------------------------------------------------------------------------

def test_parse_doc_returns_none_for_unparseable():
    assert render.parse_doc("not json") is None
    assert render.parse_doc(None) is None
    assert render.parse_doc('{"a": 1}') == {"a": 1}
    assert render.parse_doc({"a": 1}) == {"a": 1}


def test_yaml_lines_expands_a_json_string_value():
    lines = render.yaml_lines({"Config": '{"Inner": 2}'})
    assert lines[0].endswith("# embedded JSON, expanded")
    assert lines[1] == "  Inner: 2"


def test_yaml_lines_keeps_arns_and_uris_bare():
    arn = "arn:aws:sagemaker:us-west-2:1:job/MyCategory/j1"
    lines = render.yaml_lines({"Ref": arn, "Loc": "s3://bucket/prefix/"})
    assert lines == [f"Ref: {arn}", "Loc: s3://bucket/prefix/"]


def test_yaml_lines_quotes_only_what_reads_as_structure():
    assert render.yaml_scalar("plain") == "plain"
    assert render.yaml_scalar("key: value") == '"key: value"'
    assert render.yaml_scalar("{brace") == '"{brace"'
    assert render.yaml_scalar(" padded ") == '" padded "'
    assert render.yaml_scalar(None) == "null"
    assert render.yaml_scalar(True) == "true"


def test_yaml_lines_hangs_a_list_item_off_its_dash():
    lines = render.yaml_lines({"Items": [{"A": 1, "B": 2}]})
    assert lines == ["Items:", "  - A: 1", "    B: 2"]


def test_yaml_lines_uses_a_block_for_multiline_strings():
    lines = render.yaml_lines({"Msg": "one\ntwo"})
    assert lines == ["Msg: |", "  one", "  two"]


def test_show_document_says_which_form_it_printed(capsys):
    """Which form is the fact that varies per run; *why* is in the flag's help."""
    render.show_document("Doc", '{"a": 1}', "1.0")
    out = capsys.readouterr().out
    assert "schema 1.0" in out and "reformatted" in out
    render.show_document("Doc", '{"a": 1}', "1.0", raw=True)
    assert "verbatim" in capsys.readouterr().out


def test_show_document_falls_back_to_verbatim_when_unparseable(capsys):
    render.show_document("Doc", "<xml/>", None)
    out = capsys.readouterr().out
    assert "not parseable JSON" in out and "<xml/>" in out


# ---------------------------------------------------------------------------
# render — references found by shape, with provenance
# ---------------------------------------------------------------------------

def test_split_arn_is_structural():
    assert render.split_arn(
        "arn:aws:sagemaker:us-west-2:123456789012:hub/MyHub"
    ) == ("sagemaker", "us-west-2", "123456789012", "hub/MyHub")
    # Region and account are legitimately empty for some services.
    assert render.split_arn("arn:aws:s3:::bucket") == ("s3", "", "", "bucket")
    assert render.split_arn("arn:aws:sagemaker:us-west-2:1") is None  # truncated
    assert render.split_arn("s3://bucket/key") is None
    assert render.split_arn(None) is None


def test_split_s3_splits_bucket_from_prefix():
    assert render.split_s3("s3://bucket/some/prefix/") == ("bucket", "some/prefix/")
    assert render.split_s3("s3://bucket") == ("bucket", "")


def test_refs_in_finds_by_value_shape_not_key_name():
    doc = {
        "SomeUnknownKey": "arn:aws:sagemaker:us-west-2:1:hub/MyHub",
        "DatasetArn": "not-an-arn-at-all",
        "Nested": {"Items": [{"Where": "s3://bucket/out/"}]},
    }
    found = render.refs_in(doc)
    assert found == [
        ("SomeUnknownKey", "arn:aws:sagemaker:us-west-2:1:hub/MyHub"),
        ("Nested.Items[0].Where", "s3://bucket/out/"),
    ]


def test_version_key_orders_numerically():
    assert render.version_key("0.0.10") > render.version_key("0.0.9")
    assert sorted(["0.0.2", "0.0.10", "0.0.1"], key=render.version_key) == \
        ["0.0.1", "0.0.2", "0.0.10"]


# ---------------------------------------------------------------------------
# render — plumbing
# ---------------------------------------------------------------------------

def test_paged_follows_next_token_and_stops_at_max():
    pages = [
        {"Items": [1, 2], "NextToken": "t1"},
        {"Items": [3, 4], "NextToken": "t2"},
        {"Items": [5, 6]},
    ]
    seen = []

    def call(**params):
        seen.append(params.get("NextToken"))
        return pages[len(seen) - 1]

    assert render.paged(call, "Items", 5) == [1, 2, 3, 4, 5]
    assert seen == [None, "t1", "t2"]


def test_paged_stops_when_a_page_has_no_token():
    assert render.paged(lambda **_: {"Items": [1]}, "Items", 100) == [1]


def test_paged_honours_s3s_different_token_names():
    """S3 sends ContinuationToken and returns NextContinuationToken."""
    pages = [{"Contents": [1], "NextContinuationToken": "c1"}, {"Contents": [2]}]
    seen = []

    def call(**params):
        seen.append(params.get("ContinuationToken"))
        return pages[len(seen) - 1]

    got = render.paged(call, "Contents", 10, token_param="ContinuationToken",
                       token_key="NextContinuationToken", Bucket="b")
    assert got == [1, 2]
    assert seen == [None, "c1"]


def test_paged_would_stop_early_with_the_wrong_token_names():
    """Guards the reason the token names are parameters at all."""
    call = lambda **_: {"Contents": [1], "NextContinuationToken": "c1"}
    assert render.paged(call, "Contents", 10) == [1]


def test_require_operation_names_the_service_var(monkeypatch):
    monkeypatch.setattr(awsut_recipe, "sagemaker_service_name", "sagemaker-private")
    with pytest.raises(render.SmError) as exc:
        render.require_operation(FakeClient(), "list_jobs", "ListJobs")
    assert "ListJobs" in str(exc.value)
    assert "sagemaker-private" in str(exc.value)
    assert "sagemaker_service_name" in str(exc.value)


def test_require_operation_passes_when_present():
    render.require_operation(FakeClient(list_jobs={}), "list_jobs", "ListJobs")


def test_model_enum_is_offline_and_never_raises():
    assert render.model_enum("no-such-service-at-all", "Nope", "Nope") == ()


def test_model_enum_reads_the_loaded_model():
    # HubContentType is stable in every botocore that has the hub APIs.
    got = render.model_enum("sagemaker", "ListHubContents", "HubContentType")
    assert "DataSet" in got


@pytest.mark.parametrize("raised,expected", [
    (render.SmError("bad input"), "error: bad input"),
    (client_error(message="AccessDenied for you"), "error: AccessDenied for you"),
    (botocore.exceptions.NoCredentialsError(), "error: no AWS credentials found"),
])
def test_guard_turns_expected_failures_into_one_liners(capsys, raised, expected):
    @render.guard
    def boom():
        raise raised

    boom()                                   # must not propagate
    captured = capsys.readouterr()
    assert expected in captured.err
    assert "Traceback" not in captured.err


def test_guard_lets_unexpected_errors_through():
    @render.guard
    def boom():
        raise ZeroDivisionError

    with pytest.raises(ZeroDivisionError):
        boom()


def test_guard_preserves_the_signature_for_kwarg_filtering():
    import inspect

    @render.guard
    def handler(alpha, beta=1):
        return alpha

    assert list(inspect.signature(handler).parameters) == ["alpha", "beta"]


def test_heartbeat_clear_only_breaks_the_line_when_dots_are_pending(capsys):
    beat = render.Heartbeat()
    beat.clear()
    assert capsys.readouterr().out == ""
    beat.tick()
    beat.tick()
    beat.clear()
    assert capsys.readouterr().out == "..\n"
    assert beat.col == 0


def test_heartbeat_wraps_at_its_width(capsys):
    beat = render.Heartbeat(width=3)
    for _ in range(3):
        beat.tick()
    assert capsys.readouterr().out == "...\n"
    assert beat.col == 0


@pytest.mark.parametrize("args,expected", [
    (["--hub", "MyHub"], "MyHub"),
    (["--hub=MyHub"], "MyHub"),
    (["list", "--type", "DataSet", "--hub", "MyHub"], "MyHub"),
    (["--hub"], None),                 # value not typed yet
    (["--hubbub", "x"], None),         # not a prefix match
    ([], None),
])
def test_flag_value_reads_both_spellings(args, expected):
    assert render.flag_value(args, "--hub") == expected


@pytest.mark.parametrize("args,expected", [
    (["my-set"], ["my-set"]),
    (["--hub", "MyHub", "my-set"], ["my-set"]),      # MyHub is a flag's value
    (["--hub=MyHub", "my-set"], ["my-set"]),
    (["--raw", "my-set"], ["my-set"]),               # boolean flag takes no value
    (["--hub", "MyHub"], []),
    ([], []),
])
def test_positionals_drops_flags_and_their_values(args, expected):
    assert render.positionals(args, ("--hub", "--type")) == expected


# ---------------------------------------------------------------------------
# jobs — categories
# ---------------------------------------------------------------------------

def test_the_model_is_the_default_source_of_categories():
    """No category is hard-coded: out of the box the loaded model decides."""
    assert jobs.EXTRA_JOB_CATEGORIES == []
    assert jobs.job_categories() == jobs.declared_categories()


def test_job_categories_unions_the_model_with_the_configured_extras(extras):
    got = jobs.job_categories()
    assert set(extras) <= set(got)
    assert set(jobs.declared_categories()) <= set(got)
    assert len(got) == len(set(got))          # the union does not duplicate


def test_a_configured_extra_already_in_the_model_is_not_queried_twice(monkeypatch):
    """The point of the union: a category that graduates costs nothing extra."""
    declared = jobs.declared_categories()[0]
    monkeypatch.setattr(jobs, "EXTRA_JOB_CATEGORIES", [declared])
    assert jobs.job_categories() == jobs.declared_categories()


def test_resolve_categories_group_selectors(extras):
    every = jobs.resolve_categories(None)
    assert jobs.resolve_categories("all") == every
    assert jobs.resolve_categories("extra") == extras
    assert jobs.resolve_categories("SomeFutureCategory") == ["SomeFutureCategory"]


def test_category_completer_offers_the_group_selectors():
    got = {c.value: c.description for c in jobs._JobCategoryCompleter().complete(ctx())}
    assert "all" in got and "extra" in got
    for category in jobs.declared_categories():
        assert category in got


def test_category_completer_warns_about_a_category_the_model_lacks(extras):
    got = {c.value: c.description for c in jobs._JobCategoryCompleter().complete(ctx())}
    for category in jobs.declared_categories():
        assert "may be rejected" not in got[category]
    for category in extras:
        assert "may be rejected" in got[category]


def test_category_completer_filters_by_prefix(extras):
    values = [c.value for c in jobs._JobCategoryCompleter().complete(ctx("My"))]
    assert values == ["MyCategory"]


# ---------------------------------------------------------------------------
# jobs — lookup by name requires probing
# ---------------------------------------------------------------------------

def test_resolve_job_with_one_category_does_not_probe():
    cli = FakeClient(describe_job=lambda **p: {"JobName": p["JobName"]})
    desc, category = jobs.resolve_job(cli, "j1", "MyCategory")
    assert category == "MyCategory"
    assert desc["JobName"] == "j1"
    assert len(cli.calls) == 1


def test_resolve_job_probes_until_a_category_answers(extras):
    def describe(**p):
        if p["JobCategory"] != "OtherCategory":
            raise client_error("ValidationException", "not found")
        return {"JobName": p["JobName"]}

    cli = FakeClient(describe_job=describe)
    _desc, category = jobs.resolve_job(cli, "j1", "extra")
    assert category == "OtherCategory"


def test_resolve_job_reports_every_category_it_tried(extras):
    cli = FakeClient(describe_job=lambda **p: (_ for _ in ()).throw(
        client_error("ValidationException", "not found")))
    with pytest.raises(render.SmError) as exc:
        jobs.resolve_job(cli, "ghost", "extra")
    message = str(exc.value)
    assert "ghost" in message
    for category in extras:
        assert category in message
    assert "--category" in message


def test_resolve_job_separates_searched_from_not_offered(extras):
    """A category the endpoint rejects never had a chance to hold the job.

    Listing it as "searched, not found" would claim a lookup that never
    happened, so it is reported under its own heading.
    """
    declared = jobs.declared_categories()[0]
    undeclared = extras[0]
    cli = FakeClient(describe_job=lambda **p: (_ for _ in ()).throw(
        client_error("ValidationException", "not found")))
    with pytest.raises(render.SmError) as exc:
        jobs.resolve_job(cli, "ghost", None)
    searched, _, rest = str(exc.value).partition("not offered by this endpoint")
    assert declared in searched and undeclared not in searched
    assert undeclared in rest


def test_resolve_job_reraises_a_non_lookup_failure(extras):
    """An AccessDenied must not be swallowed as "no such job"."""
    cli = FakeClient(describe_job=lambda **p: (_ for _ in ()).throw(
        client_error("AccessDeniedException", "nope")))
    with pytest.raises(botocore.exceptions.ClientError):
        jobs.resolve_job(cli, "j1", None)


# ---------------------------------------------------------------------------
# jobs — listing
# ---------------------------------------------------------------------------

def test_fetch_jobs_budgets_max_per_category_not_across_them():
    def list_jobs(**p):
        return {"JobSummaries": [{"JobName": f"{p['JobCategory']}-{i}"}
                                 for i in range(2)]}

    cli = FakeClient(list_jobs=list_jobs)
    rows, skipped, not_offered = jobs.fetch_jobs(cli, ["A", "B"], None, limit=2)
    assert len(rows) == 4          # 2 per category, not 2 in total
    assert skipped == [] and not_offered == []


def test_fetch_jobs_passes_the_filters_through():
    cli = FakeClient(list_jobs=lambda **p: {"JobSummaries": []})
    after = utc(2026, 1, 1)
    jobs.fetch_jobs(cli, ["A"], after, status="InProgress", contains="abc",
                    limit=5, sort="Name", asc=True)
    _op, params = cli.calls[0]
    assert params == {"JobCategory": "A", "MaxResults": 5,
                      "StatusEquals": "InProgress", "NameContains": "abc",
                      "CreationTimeAfter": after,
                      "SortBy": "Name", "SortOrder": "Ascending"}


def test_fetch_jobs_skips_an_unauthorized_category(capsys):
    def list_jobs(**p):
        if p["JobCategory"] == "B":
            raise client_error("AccessDeniedException", "no access to B")
        return {"JobSummaries": [{"JobName": "a1"}]}

    cli = FakeClient(list_jobs=list_jobs)
    rows, skipped, not_offered = jobs.fetch_jobs(cli, ["A", "B"], None)
    assert [r["JobName"] for r in rows] == ["a1"]
    assert skipped == ["B"] and not_offered == []
    assert "no access to B" in capsys.readouterr().err


def test_fetch_jobs_can_stay_quiet_for_a_polling_loop(capsys):
    cli = FakeClient(list_jobs=lambda **p: (_ for _ in ()).throw(
        client_error("AccessDeniedException", "no")))
    _rows, skipped, _not_offered = jobs.fetch_jobs(cli, ["A"], None, warn=False)
    assert skipped == ["A"]
    assert capsys.readouterr().err == ""


def test_fetch_jobs_does_not_shout_about_a_category_the_endpoint_lacks(capsys, extras):
    """A rejection per configured extra must not be an error per configured extra.

    ``ValidationException`` on a category the loaded model never declared means
    the endpoint does not offer it — expected, so it is a compact note rather
    than a stderr line per category on every single invocation.
    """
    undeclared = extras
    declared = jobs.declared_categories()[0]

    def list_jobs(**p):
        if p["JobCategory"] in undeclared:
            raise client_error("ValidationException",
                               "1 validation error detected: JobCategory")
        return {"JobSummaries": [{"JobName": "a1"}]}

    cli = FakeClient(list_jobs=list_jobs)
    rows, skipped, not_offered = jobs.fetch_jobs(cli, [declared, *undeclared], None)
    assert [r["JobName"] for r in rows] == ["a1"]
    assert skipped == []
    assert not_offered == list(undeclared)
    assert capsys.readouterr().err == ""


def test_fetch_jobs_still_shouts_when_a_declared_category_is_rejected(capsys):
    """The same code on a *declared* category is a real problem, not a fact."""
    declared = jobs.declared_categories()[0]
    cli = FakeClient(list_jobs=lambda **p: (_ for _ in ()).throw(
        client_error("ValidationException", "something is genuinely wrong")))
    _rows, skipped, not_offered = jobs.fetch_jobs(cli, [declared], None)
    assert skipped == [declared] and not_offered == []
    assert "something is genuinely wrong" in capsys.readouterr().err


def test_category_notes_name_both_kinds_of_gap():
    notes = "\n".join(jobs._category_notes(["Denied"], ["Absent"]))
    assert "Denied" in notes and "stderr" in notes
    assert "Absent" in notes and "not offered by this endpoint" in notes
    assert jobs._category_notes([], []) == []


# ---------------------------------------------------------------------------
# jobs — transitions get a duration the service doesn't supply
# ---------------------------------------------------------------------------

def test_transitions_derive_duration_from_the_next_start():
    desc = {
        "JobStatus": "Completed",
        "EndTime": utc(2026, 1, 1, 0, 10),
        "SecondaryStatusTransitions": [
            {"Status": "Starting", "StartTime": utc(2026, 1, 1, 0, 0)},
            {"Status": "Running", "StartTime": utc(2026, 1, 1, 0, 2)},
        ],
    }
    got = jobs.transitions(desc)
    assert [(t[0], t[3], t[4]) for t in got] == [
        ("Starting", 120.0, False),      # next transition's StartTime
        ("Running", 480.0, False),       # job EndTime for the last step
    ]


def test_transitions_mark_only_the_last_step_of_a_live_job_running():
    desc = {
        "JobStatus": "InProgress",
        "SecondaryStatusTransitions": [
            {"Status": "Starting", "StartTime": utc(2026, 1, 1, 0, 0)},
            {"Status": "Running",
             "StartTime": datetime.now(timezone.utc) - timedelta(seconds=30)},
        ],
    }
    got = jobs.transitions(desc)
    assert [t[4] for t in got] == [False, True]
    assert got[0][3] is not None                 # finished step: fixed duration
    assert 25 < got[1][3] < 90                   # running step: its age


def test_transitions_honour_an_end_time_if_the_service_ever_sends_one():
    desc = {
        "JobStatus": "Completed",
        "SecondaryStatusTransitions": [
            {"Status": "Starting", "StartTime": utc(2026, 1, 1, 0, 0),
             "EndTime": utc(2026, 1, 1, 0, 1)},
            {"Status": "Running", "StartTime": utc(2026, 1, 1, 0, 2)},
        ],
    }
    assert jobs.transitions(desc)[0][3] == 60.0


def test_transitions_of_a_job_with_none_is_empty():
    assert jobs.transitions({"JobStatus": "InProgress"}) == []


@pytest.mark.parametrize("status,mark", [
    ("Completed", "✓"), ("Failed", "✗"), ("Stopped", "✗"),
    ("InProgress", "…"), ("Whatever", "…"),
])
def test_mark_for(status, mark):
    assert jobs.mark_for(status) == mark


def test_state_of_bridges_the_two_secondary_status_spellings():
    assert jobs.state_of({"JobStatus": "InProgress",
                          "JobSecondaryStatus": "Running"}) == ("InProgress", "Running")
    assert jobs.state_of({}) == ("?", "-")


def test_fmt_state_omits_a_missing_secondary():
    assert jobs.fmt_state(("Completed", "-")) == "Completed"
    assert jobs.fmt_state(("InProgress", "Running")) == "InProgress / Running"


# ---------------------------------------------------------------------------
# jobs — reading the log
# ---------------------------------------------------------------------------

JOB = "sdg-thing-2026-08-31-18-17-06-446-1"


def _job_stream(name=JOB, phase="generation", when="2026-08-31T18-17-06.7Z",
                last=1_700_000_000_000):
    return {"logStreamName": f"{name}/{phase}-{when}",
            "firstEventTimestamp": last - 1000,
            "lastEventTimestamp": last}


def job_described(status="Completed", secondary="Completed", name=JOB):
    return {"JobName": name, "JobStatus": status, "SecondaryStatus": secondary}


@pytest.fixture
def job_logs(monkeypatch):
    """Install a FakeLogs plus a DescribeJob that resolves into one category."""
    def install(logs, *, described=None, category="MyCategory"):
        desc = described if described is not None else job_described()

        def describe_job(**p):
            if p["JobCategory"] != category:
                raise client_error("ValidationException", "no such job")
            return dict(desc, JobCategory=p["JobCategory"])

        monkeypatch.setattr(jobs, "sm_client",
                            lambda *a, **k: FakeClient(describe_job=describe_job))
        monkeypatch.setattr(render, "logs_client", lambda: logs)
        return logs

    return install


def run_jobs(tree, *args):
    tree.children["jobs"].invoke(list(args))


def test_the_log_group_is_derived_from_the_category_slash_included():
    """The trailing slash is part of the group's real name, not a separator."""
    assert jobs.job_log_group("DataQualityEvaluation") == \
        "/aws/sagemaker/Job/DataQualityEvaluation/"
    assert jobs.job_stream_prefix(JOB) == f"{JOB}/"


def test_log_finds_the_stream_under_the_derived_group(
        sagemaker_tree, job_logs, capsys):
    """A stream name embeds a phase and a timestamp, so it has to be looked up."""
    logs = job_logs(FakeLogs(events=[{"message": "generating rows\n"}],
                             streams=[_job_stream()]))
    run_jobs(sagemaker_tree, "log", JOB, "--category", "MyCategory")

    described = [p for op, p in logs.calls if op == "describe_log_streams"][0]
    assert described["logGroupName"] == "/aws/sagemaker/Job/MyCategory/"
    assert described["logStreamNamePrefix"] == f"{JOB}/"
    read = [p for op, p in logs.calls if op == "get_log_events"][0]
    assert read["logStreamName"] == f"{JOB}/generation-2026-08-31T18-17-06.7Z"
    assert "generating rows" in capsys.readouterr().out


def test_log_reads_the_most_recent_stream_and_says_there_were_others(
        sagemaker_tree, job_logs, capsys):
    """Newest-first has to be imposed here: a prefixed query comes back by name.

    The names sort the wrong way round on purpose — 'a-old' precedes 'z-new'
    alphabetically while being the older of the two.
    """
    logs = job_logs(FakeLogs(events=[], streams=[
        _job_stream(phase="a-old", last=1_000),
        _job_stream(phase="z-new", last=9_000),
    ]))
    run_jobs(sagemaker_tree, "log", JOB, "--category", "MyCategory")
    read = [p for op, p in logs.calls if op == "get_log_events"][0]
    assert read["logStreamName"].endswith("z-new-2026-08-31T18-17-06.7Z")
    assert "2 streams" in capsys.readouterr().out


def test_log_stream_flag_takes_the_tail_below_the_job(
        sagemaker_tree, job_logs, capsys):
    logs = job_logs(FakeLogs(events=[]))
    run_jobs(sagemaker_tree, "log", JOB, "--category", "MyCategory",
             "--stream", "generation-whenever")
    read = [p for op, p in logs.calls if op == "get_log_events"][0]
    assert read["logStreamName"] == f"{JOB}/generation-whenever"
    # Naming it skips the lookup entirely.
    assert not [op for op, _ in logs.calls if op == "describe_log_streams"]


def test_log_group_can_be_overridden(sagemaker_tree, job_logs):
    logs = job_logs(FakeLogs(events=[], streams=[_job_stream()]))
    run_jobs(sagemaker_tree, "log", JOB, "--category", "MyCategory",
             "--log-group", "/my/own/group")
    assert all(p["logGroupName"] == "/my/own/group"
               for _op, p in logs.calls)


@pytest.mark.parametrize("status,secondary,expected", [
    ("InProgress", "Validating", "reaches a phase that logs"),
    ("Completed", "Completed", "has since been deleted"),
])
def test_log_explains_an_empty_group_by_the_jobs_status(
        sagemaker_tree, job_logs, capsys, status, secondary, expected):
    """Too-early and aged-out both list nothing; only the status tells them apart."""
    job_logs(FakeLogs(streams=[]),
             described=job_described(status=status, secondary=secondary))
    run_jobs(sagemaker_tree, "log", JOB, "--category", "MyCategory")
    out = capsys.readouterr().out
    assert "no log stream under" in out
    assert f"{status} / {secondary}" in out
    assert expected in out
    # An absent log is a fact about the job, not a failure — so it is reported,
    # not raised, and the derived group is named in case it is the wrong guess.
    assert "--log-group" in out


def test_log_explains_a_named_stream_that_does_not_exist(
        sagemaker_tree, job_logs, capsys):
    job_logs(FakeLogs(missing=True))
    run_jobs(sagemaker_tree, "log", JOB, "--category", "MyCategory",
             "--stream", "typo")
    out = capsys.readouterr().out
    assert "no stream 'typo'" in out and "--list" in out


def test_log_list_shows_the_streams_relative_to_the_job(
        sagemaker_tree, job_logs, capsys):
    job_logs(FakeLogs(streams=[_job_stream(phase="generation")]))
    run_jobs(sagemaker_tree, "log", JOB, "--category", "MyCategory", "--list")
    out = capsys.readouterr().out
    rows = [ln for ln in out.splitlines() if ln.startswith("generation-")]
    assert rows
    # Listed below the job, because that is what --stream takes.
    assert JOB not in rows[0]
    # No size: CloudWatch reports storedBytes as zero for every stream, so the
    # column could only ever have said 0B.
    assert "SIZE" not in out and "0B" not in out


def test_log_probes_categories_when_none_is_given(sagemaker_tree, job_logs, extras):
    """`log` needs the category for the group name, so it resolves like `describe`."""
    logs = job_logs(FakeLogs(events=[], streams=[_job_stream()]),
                    category="OtherCategory")
    run_jobs(sagemaker_tree, "log", JOB)
    described = [p for op, p in logs.calls if op == "describe_log_streams"][0]
    assert described["logGroupName"] == "/aws/sagemaker/Job/OtherCategory/"


def test_the_log_flags_are_only_on_log(sagemaker_tree):
    leaves = sagemaker_tree.children["jobs"].children
    log_only = {"--stream", "--list", "--lookback", "--log-group"}
    assert log_only <= _flags(leaves["log"])
    for name in ("list", "describe", "watch", "stop"):
        assert not _flags(leaves[name]) & log_only, name
    # And the listing filters do not appear on a single job's log.
    assert not _flags(leaves["log"]) & {"--since", "--status", "--contains"}


# ---------------------------------------------------------------------------
# jobs — the log-stream completer
# ---------------------------------------------------------------------------

def test_the_stream_completer_needs_a_job_name_first(monkeypatch):
    called = []
    monkeypatch.setattr(render, "logs_client", lambda: called.append(1))
    assert jobs._JobLogStreamCompleter().complete(
        ctx("", args=["--category", "MyCategory"])) == []
    assert called == []


def test_the_stream_completer_offers_tails_and_asks_no_sagemaker(
        monkeypatch, extras):
    """It finds the category by trying each group, not by probing DescribeJob."""
    monkeypatch.setattr(render, "logs_client",
                        lambda: FakeLogs(streams=[_job_stream(phase="generation")]))
    monkeypatch.setattr(jobs, "sm_client",
                        lambda *a, **k: pytest.fail("completion called DescribeJob"))
    got = [c.value for c in jobs._JobLogStreamCompleter().complete(
        ctx("gen", args=[JOB]))]
    # One row, not one per category tried: every candidate group answers with the
    # same stream here, and the picker must not show it len(categories) times.
    assert got == ["generation-2026-08-31T18-17-06.7Z"]


# ---------------------------------------------------------------------------
# hub — content types and hub selection
# ---------------------------------------------------------------------------

def test_hub_content_types_falls_back_when_the_model_has_no_enum(monkeypatch):
    monkeypatch.setattr(hub, "model_enum", lambda *a: ())
    assert hub.hub_content_types() == hub.FALLBACK_CONTENT_TYPES


def test_hub_content_types_prefers_the_loaded_model():
    assert "DataSet" in hub.hub_content_types()


def test_resolve_types_all_means_every_known_type():
    assert hub.resolve_types("all") == hub.hub_content_types()
    assert hub.resolve_types(None) == hub.hub_content_types()
    assert hub.resolve_types("DataSet") == ["DataSet"]


def _hub(name, account="123456789012"):
    return {"HubName": name,
            "HubArn": f"arn:aws:sagemaker:us-west-2:{account}:hub/{name}"}


def test_is_aws_owned_reads_the_arn_account_not_the_name():
    assert hub.is_aws_owned(_hub("SageMakerPublicHub", account="aws"))
    assert not hub.is_aws_owned(_hub("SageMakerPublicHub"))
    assert not hub.is_aws_owned({"HubName": "x"})       # no ARN → not AWS's


def test_resolve_hub_returns_the_requested_hub_without_listing():
    cli = FakeClient()
    assert hub.resolve_hub(cli, "MyHub") == "MyHub"
    assert cli.calls == []


def test_resolve_hub_auto_selects_the_only_account_owned_hub():
    cli = FakeClient(list_hubs={"HubSummaries": [
        _hub("PublicHub", account="aws"), _hub("MyHub")]})
    assert hub.resolve_hub(cli, None) == "MyHub"


def test_resolve_hub_refuses_to_guess_between_two():
    cli = FakeClient(list_hubs={"HubSummaries": [_hub("A"), _hub("B")]})
    with pytest.raises(render.SmError) as exc:
        hub.resolve_hub(cli, None)
    assert "--hub" in str(exc.value) and "A, B" in str(exc.value)


def test_resolve_hub_explains_when_only_the_public_hub_exists():
    cli = FakeClient(list_hubs={"HubSummaries": [_hub("PublicHub", account="aws")]})
    with pytest.raises(render.SmError) as exc:
        hub.resolve_hub(cli, None)
    assert "no account-owned hub" in str(exc.value)


def test_resolve_hub_reports_a_listing_failure_as_a_user_error():
    cli = FakeClient(list_hubs=lambda **_: (_ for _ in ()).throw(
        client_error("AccessDeniedException", "cannot list hubs")))
    with pytest.raises(render.SmError) as exc:
        hub.resolve_hub(cli, None)
    assert "cannot list hubs" in str(exc.value)


# ---------------------------------------------------------------------------
# hub — content lookup
# ---------------------------------------------------------------------------

def _summary(name, version, content_type="DataSet"):
    return {"HubContentName": name, "HubContentVersion": version,
            "HubContentType": content_type}


def test_find_content_discards_substring_matches():
    """NameContains is a substring filter, so the exact name must be re-checked."""
    cli = FakeClient(list_hub_contents=lambda **p: {"HubContentSummaries": [
        _summary("my-set-v2", "0.0.1"), _summary("my-set", "0.0.1")]})
    assert hub.find_content(cli, "H", "my-set", "DataSet")["HubContentName"] == "my-set"


def test_find_content_picks_the_newest_version_numerically():
    cli = FakeClient(list_hub_contents=lambda **p: {"HubContentSummaries": [
        _summary("s", "0.0.9"), _summary("s", "0.0.10")]})
    assert hub.find_content(cli, "H", "s", "DataSet")["HubContentVersion"] == "0.0.10"


def test_find_content_probes_types_and_skips_the_ones_that_error():
    def list_contents(**p):
        if p["HubContentType"] != "DataSet":
            raise client_error("AccessDeniedException", "no")
        return {"HubContentSummaries": [_summary("s", "0.0.1")]}

    cli = FakeClient(list_hub_contents=list_contents)
    assert hub.find_content(cli, "H", "s")["HubContentType"] == "DataSet"


def test_find_content_returns_none_when_nothing_matches():
    cli = FakeClient(list_hub_contents=lambda **p: {"HubContentSummaries": []})
    assert hub.find_content(cli, "H", "s") is None


def test_resolve_content_type_probes_only_when_not_given():
    cli = FakeClient()
    assert hub.resolve_content_type(cli, "H", "s", "Model") == "Model"
    assert cli.calls == []


def test_resolve_content_type_lists_the_types_it_tried():
    cli = FakeClient(list_hub_contents=lambda **p: {"HubContentSummaries": []})
    with pytest.raises(render.SmError) as exc:
        hub.resolve_content_type(cli, "H", "ghost", None)
    assert "ghost" in str(exc.value) and "DataSet" in str(exc.value)


def test_latest_version_returns_the_newest_and_the_whole_history():
    cli = FakeClient(list_hub_content_versions={"HubContentSummaries": [
        _summary("s", "0.0.1"), _summary("s", "0.0.10"), _summary("s", "0.0.2")]})
    newest, versions = hub.latest_version(cli, "H", "DataSet", "s")
    assert newest == "0.0.10"
    assert len(versions) == 3


def test_latest_version_of_an_unknown_item_is_none():
    cli = FakeClient(list_hub_content_versions={"HubContentSummaries": []})
    assert hub.latest_version(cli, "H", "DataSet", "s") == (None, [])


def test_describe_content_omits_the_version_when_not_pinned():
    cli = FakeClient(describe_hub_content=lambda **p: p)
    assert "HubContentVersion" not in hub.describe_content(cli, "H", "DataSet", "s")
    assert hub.describe_content(cli, "H", "DataSet", "s", "0.0.2")[
        "HubContentVersion"] == "0.0.2"


# ---------------------------------------------------------------------------
# hub — ARN resolution, the basis of trace
# ---------------------------------------------------------------------------

HUB_CONTENT_ARN = ("arn:aws:sagemaker:us-west-2:123456789012:"
                   "hub-content/MyHub/DataSet/my-set/0.0.2")


def test_resolve_arn_reads_a_hub_content_arn():
    cli = FakeClient(describe_hub_content=lambda **p: {
        "HubName": p["HubName"], "HubContentType": p["HubContentType"],
        "HubContentName": p["HubContentName"],
        "HubContentVersion": p["HubContentVersion"],
        "HubContentStatus": "Available", "CreationTime": utc(2026, 1, 1),
        "HubContentDocument": '{"DatasetS3Uri": "s3://bucket/out/"}',
        "DocumentSchemaVersion": "1.0"})
    kind, label, note, doc, schema, identity = hub.resolve_arn(cli, HUB_CONTENT_ARN)
    assert kind == "hub-content"
    assert label == "DataSet my-set v0.0.2"
    assert "Available" in note
    assert schema == "1.0" and "s3://bucket/out/" in doc
    assert identity == ("hub-content", "MyHub", "DataSet", "my-set", "0.0.2")


def test_resolve_arn_reports_an_unresolvable_hub_content_without_raising():
    cli = FakeClient(describe_hub_content=lambda **p: (_ for _ in ()).throw(
        client_error("ValidationException", "gone", "DescribeHubContent")))
    kind, label, note, doc, _schema, identity = hub.resolve_arn(cli, HUB_CONTENT_ARN)
    assert kind == "hub-content" and "unresolvable: gone" in note
    assert doc is None and identity is None


def test_resolve_arn_reads_a_hub_arn():
    cli = FakeClient(describe_hub=lambda **p: {
        "HubName": p["HubName"], "HubDisplayName": "My Hub", "HubStatus": "InService"})
    kind, label, note, _doc, _schema, identity = hub.resolve_arn(
        cli, "arn:aws:sagemaker:us-west-2:1:hub/MyHub")
    assert (kind, label) == ("hub", "MyHub")
    assert note == "My Hub · InService"
    assert identity == ("hub", "MyHub")


def test_resolve_arn_reads_the_category_out_of_a_job_arn():
    cli = FakeClient(describe_job=lambda **p: {
        "JobName": p["JobName"], "JobStatus": "Completed",
        "CreationTime": utc(2026, 1, 1), "EndTime": utc(2026, 1, 1, 0, 5),
        "JobConfigDocument": "{}"})
    kind, label, note, _doc, _schema, identity = hub.resolve_arn(
        cli, "arn:aws:sagemaker:us-west-2:1:job/MyCategory/j1")
    assert (kind, label) == ("job", "j1 [MyCategory]")
    assert "Completed" in note and "5m00s" in note
    assert identity == ("job", "j1", "MyCategory")
    assert cli.calls[0][1]["JobCategory"] == "MyCategory"


def test_resolve_arn_probes_a_categoryless_job_arn(extras):
    """Dataset documents spell a job ARN without its category, which DescribeJob needs."""
    def describe(**p):
        if p["JobCategory"] != "MyCategory":
            raise client_error("ValidationException", "not found")
        return {"JobName": p["JobName"], "JobStatus": "Completed",
                "CreationTime": utc(2026, 1, 1)}

    cli = FakeClient(describe_job=describe)
    kind, label, note, _doc, _schema, identity = hub.resolve_arn(
        cli, "arn:aws:sagemaker:us-west-2:1:transformation-job/j1")
    assert (kind, label) == ("job", "j1 [MyCategory]")
    assert "resolved by probing categories" in note
    assert identity == ("job", "j1", "MyCategory")


def test_the_two_job_arn_spellings_share_one_identity(extras):
    """This is what lets trace call out an alias instead of expanding it twice."""
    def describe(**p):
        if p["JobCategory"] != "MyCategory":
            raise client_error("ValidationException", "not found")
        return {"JobName": "j1", "JobStatus": "Completed",
                "CreationTime": utc(2026, 1, 1)}

    cli = FakeClient(describe_job=describe)
    a = hub.resolve_arn(cli, "arn:aws:sagemaker:us-west-2:1:job/MyCategory/j1")
    b = hub.resolve_arn(cli, "arn:aws:sagemaker:us-west-2:1:transformation-job/j1")
    assert a[5] == b[5]
    assert a[0] == b[0] == "job"


def test_resolve_arn_explains_a_categoryless_job_it_cannot_find():
    cli = FakeClient(describe_job=lambda **p: (_ for _ in ()).throw(
        client_error("ValidationException", "not found")))
    kind, _label, note, _doc, _schema, identity = hub.resolve_arn(
        cli, "arn:aws:sagemaker:us-west-2:1:mystery-job/j1")
    assert kind == "job" and identity is None
    assert "no JobCategory" in note


def test_resolve_arn_gives_up_on_an_unknown_resource_type():
    assert hub.resolve_arn(FakeClient(), "arn:aws:s3:::bucket/key") is None
    assert hub.resolve_arn(FakeClient(), "arn:aws:sagemaker:us-west-2:1:model/m") is None
    assert hub.resolve_arn(FakeClient(), "not-an-arn") is None


def test_start_points_passes_an_arn_straight_through():
    cli = FakeClient()
    assert hub.start_points(cli, HUB_CONTENT_ARN, None) == [HUB_CONTENT_ARN]
    assert cli.calls == []


def test_start_points_finds_a_hub_content_name():
    cli = FakeClient(
        list_hubs={"HubSummaries": [_hub("MyHub")]},
        list_hub_contents=lambda **p: {"HubContentSummaries": [
            dict(_summary("my-set", "0.0.2"), HubContentArn=HUB_CONTENT_ARN)]},
        describe_job=lambda **p: (_ for _ in ()).throw(
            client_error("ValidationException", "not found")),
    )
    assert hub.start_points(cli, "my-set", None) == [HUB_CONTENT_ARN]


def test_start_points_finds_a_job_name_with_no_hub_in_the_account(extras):
    job_arn = "arn:aws:sagemaker:us-west-2:1:job/MyCategory/j1"

    def describe(**p):
        if p["JobCategory"] != "MyCategory":
            raise client_error("ValidationException", "not found")
        return {"JobName": "j1", "JobArn": job_arn}

    cli = FakeClient(list_hubs={"HubSummaries": []}, describe_job=describe)
    assert hub.start_points(cli, "j1", None) == [job_arn]


def test_start_points_of_an_unknown_name_is_empty():
    cli = FakeClient(
        list_hubs={"HubSummaries": [_hub("MyHub")]},
        list_hub_contents=lambda **p: {"HubContentSummaries": []},
        describe_job=lambda **p: (_ for _ in ()).throw(
            client_error("ValidationException", "not found")),
    )
    assert hub.start_points(cli, "ghost", None) == []


# ---------------------------------------------------------------------------
# studio — domain / space resolution
# ---------------------------------------------------------------------------

DOMAIN_A = {"DomainId": "d-aaa", "DomainName": "data-prep", "Status": "InService"}
DOMAIN_B = {"DomainId": "d-bbb", "DomainName": "other-prep", "Status": "InService"}

IMAGE_ARN = "arn:aws:sagemaker:us-west-2:111122223333:image/dataprep-beta"
IMAGE_VERSION_ARN = ("arn:aws:sagemaker:us-west-2:111122223333:"
                     "image-version/dataprep-beta/1")


def _space(name="data-prep-space", *, sharing="Private", owner="data-prep-user",
           app_type="JupyterLab", instance="ml.t3.medium", image=IMAGE_ARN):
    """A DescribeSpace-shaped response."""
    settings = {"SpaceStorageSettings":
                {"EbsStorageSettings": {"EbsVolumeSizeInGb": 100}}}
    if app_type:
        settings["AppType"] = app_type
    key = studio.APP_SETTINGS_KEYS.get(app_type)
    if key and instance:
        spec = {"InstanceType": instance}
        if image:
            spec["SageMakerImageArn"] = image
        settings[key] = {"DefaultResourceSpec": spec}
    return {"DomainId": "d-aaa", "SpaceName": name, "Status": "InService",
            "OwnershipSettings": {"OwnerUserProfileName": owner},
            "SpaceSharingSettings": {"SharingType": sharing},
            "SpaceSettings": settings}


def _space_summary(name, *, sharing="Private", app_type="JupyterLab",
                   owner="data-prep-user"):
    """A ListSpaces-shaped summary — different member names for the same facts."""
    return {"DomainId": "d-aaa", "SpaceName": name, "Status": "InService",
            "SpaceSharingSettingsSummary": {"SharingType": sharing},
            "SpaceSettingsSummary": {"AppType": app_type},
            "OwnershipSettingsSummary": {"OwnerUserProfileName": owner}}


def _app(space="data-prep-space", *, status="InService", app_type="JupyterLab",
         name="default", instance="ml.t3.medium"):
    return {"DomainId": "d-aaa", "SpaceName": space, "AppType": app_type,
            "AppName": name, "Status": status, "CreationTime": utc(2026, 1, 1),
            "ResourceSpec": {"InstanceType": instance}}


def studio_client(*, domains=(DOMAIN_A,), spaces=None, space=None, apps=(),
                  profiles=(), **extra):
    """A FakeClient wired for the Studio group's read path."""
    spaces = [_space_summary("data-prep-space")] if spaces is None else spaces
    described = space if space is not None else _space()

    def describe(**p):
        if isinstance(described, Exception):
            raise described
        if p["SpaceName"] != described["SpaceName"]:
            raise client_error("ValidationException", "no such space",
                              "DescribeSpace")
        return described

    ops = {
        "list_domains": lambda **p: {"Domains": list(domains)},
        "list_spaces": lambda **p: {"Spaces": list(spaces)},
        "describe_space": describe,
        "list_apps": lambda **p: {"Apps": [
            a for a in apps
            if "SpaceNameEquals" not in p or a["SpaceName"] == p["SpaceNameEquals"]]},
        "list_user_profiles": lambda **p: {"UserProfiles": list(profiles)},
    }
    ops.update(extra)
    return FakeClient(**ops)


@pytest.fixture
def studio_sm(monkeypatch):
    """Install a FakeClient as the group's SageMaker client; return the installer."""
    def install(cli):
        monkeypatch.setattr(studio, "sm_client", lambda *a, **k: cli)
        return cli

    return install


def calls_of(cli, name):
    return [params for op, params in cli.calls if op == name]


def test_resolve_domain_accepts_a_name_or_an_id():
    cli = studio_client(domains=(DOMAIN_A, DOMAIN_B))
    assert studio.resolve_domain(cli, "other-prep")["DomainId"] == "d-bbb"
    assert studio.resolve_domain(cli, "d-bbb")["DomainName"] == "other-prep"


def test_resolve_domain_falls_back_to_the_only_domain():
    assert studio.resolve_domain(studio_client(), None)["DomainId"] == "d-aaa"


def test_resolve_domain_never_picks_between_several():
    cli = studio_client(domains=(DOMAIN_A, DOMAIN_B))
    with pytest.raises(render.SmError) as exc:
        studio.resolve_domain(cli, None)
    # Ambiguity must be actionable: the message carries the whole menu.
    assert "--domain" in str(exc.value)
    assert "data-prep (d-aaa)" in str(exc.value)
    assert "other-prep (d-bbb)" in str(exc.value)


def test_resolve_domain_reports_an_unknown_name_with_the_known_ones():
    with pytest.raises(render.SmError) as exc:
        studio.resolve_domain(studio_client(), "ghost")
    assert "'ghost'" in str(exc.value) and "data-prep (d-aaa)" in str(exc.value)


def test_resolve_domain_disambiguates_duplicate_names_by_id():
    twin = dict(DOMAIN_B, DomainName="data-prep")
    with pytest.raises(render.SmError) as exc:
        studio.resolve_domain(studio_client(domains=(DOMAIN_A, twin)), "data-prep")
    assert "d-aaa" in str(exc.value) and "d-bbb" in str(exc.value)


def test_resolve_domain_says_so_when_the_region_has_none():
    with pytest.raises(render.SmError) as exc:
        studio.resolve_domain(studio_client(domains=()), None)
    assert "no Studio domain" in str(exc.value)


def test_resolve_space_falls_back_to_the_only_space():
    cli = studio_client()
    assert studio.resolve_space(cli, "d-aaa", None)["SpaceName"] == "data-prep-space"


def test_resolve_space_never_picks_between_several():
    cli = studio_client(spaces=[_space_summary("data-prep-space"),
                                _space_summary("data-prep-space-shared")])
    with pytest.raises(render.SmError) as exc:
        studio.resolve_space(cli, "d-aaa", None)
    assert "data-prep-space-shared" in str(exc.value)
    # ...and asking for one by name still works.
    assert studio.resolve_space(cli, "d-aaa", "data-prep-space")["SpaceName"] \
        == "data-prep-space"


def test_resolve_space_reports_an_unknown_name_with_the_known_ones():
    cli = studio_client(spaces=[_space_summary("data-prep-space")])
    with pytest.raises(render.SmError) as exc:
        studio.resolve_space(cli, "d-aaa", "ghost")
    assert "'ghost'" in str(exc.value) and "data-prep-space" in str(exc.value)


# ---------------------------------------------------------------------------
# studio — what a space says about itself
# ---------------------------------------------------------------------------

def test_app_type_prefers_the_flag_then_the_spaces_own():
    space = _space(app_type="JupyterLab")
    assert studio.space_app_type(space) == "JupyterLab"
    assert studio.space_app_type(space, "CodeEditor") == "CodeEditor"


def test_app_type_is_inferred_from_the_only_settings_block_present():
    space = _space(app_type="CodeEditor")
    del space["SpaceSettings"]["AppType"]
    assert studio.space_app_type(space) == "CodeEditor"


def test_app_type_refuses_to_guess_between_two_settings_blocks():
    space = _space(app_type="JupyterLab")
    del space["SpaceSettings"]["AppType"]
    space["SpaceSettings"]["CodeEditorAppSettings"] = {"DefaultResourceSpec": {}}
    with pytest.raises(render.SmError) as exc:
        studio.space_app_type(space)
    assert "--app-type" in str(exc.value)


def test_app_type_defaults_to_jupyterlab_when_the_space_says_nothing():
    assert studio.space_app_type({"SpaceSettings": {}}) == "JupyterLab"


def test_default_resource_spec_is_read_off_the_space_and_copied():
    space = _space()
    spec = studio.default_resource_spec(space, "JupyterLab")
    assert spec == {"InstanceType": "ml.t3.medium", "SageMakerImageArn": IMAGE_ARN}
    spec["InstanceType"] = "ml.m5.large"          # callers mutate it for --instance-type
    assert (space["SpaceSettings"]["JupyterLabAppSettings"]
            ["DefaultResourceSpec"]["InstanceType"]) == "ml.t3.medium"


def test_default_resource_spec_is_empty_for_an_app_type_the_space_has_no_settings_for():
    assert studio.default_resource_spec(_space(), "CodeEditor") == {}


def test_default_resource_spec_drops_image_arns_echoed_into_the_environment_aliases():
    # DescribeSpace reports a custom image in all four members; CreateApp only
    # accepts environment ARNs in the Environment* two.
    space = _space()
    spec = (space["SpaceSettings"]["JupyterLabAppSettings"]
            ["DefaultResourceSpec"])
    spec["SageMakerImageVersionArn"] = IMAGE_VERSION_ARN
    spec["EnvironmentArn"] = IMAGE_ARN
    spec["EnvironmentVersionArn"] = IMAGE_VERSION_ARN
    assert studio.default_resource_spec(space, "JupyterLab") == {
        "InstanceType": "ml.t3.medium",
        "SageMakerImageArn": IMAGE_ARN,
        "SageMakerImageVersionArn": IMAGE_VERSION_ARN,
    }


def test_default_resource_spec_moves_an_image_arn_onto_the_member_that_admits_it():
    # Same aliasing, but with no image member to fall back on — the ARN has to
    # survive the move or the app would launch on the service's default image.
    space = _space(image=None)
    (space["SpaceSettings"]["JupyterLabAppSettings"]["DefaultResourceSpec"]
     .update({"EnvironmentArn": IMAGE_ARN,
              "EnvironmentVersionArn": IMAGE_VERSION_ARN}))
    assert studio.default_resource_spec(space, "JupyterLab") == {
        "InstanceType": "ml.t3.medium",
        "SageMakerImageArn": IMAGE_ARN,
        "SageMakerImageVersionArn": IMAGE_VERSION_ARN,
    }


def test_default_resource_spec_keeps_a_genuine_environment_arn():
    space = _space(image=None)
    env = "arn:aws:sagemaker:us-west-2:111122223333:environment/data-prep"
    env_version = ("arn:aws:sagemaker:us-west-2:111122223333:"
                   "environment-version/data-prep/1")
    (space["SpaceSettings"]["JupyterLabAppSettings"]["DefaultResourceSpec"]
     .update({"EnvironmentArn": env, "EnvironmentVersionArn": env_version}))
    assert studio.default_resource_spec(space, "JupyterLab") == {
        "InstanceType": "ml.t3.medium",
        "EnvironmentArn": env,
        "EnvironmentVersionArn": env_version,
    }


def test_sharing_type_reads_either_api_shape():
    assert studio.sharing_type(_space(sharing="Shared")) == "Shared"
    assert studio.sharing_type(_space_summary("s", sharing="Shared")) == "Shared"
    assert studio.sharing_type({}) == "-"


# ---------------------------------------------------------------------------
# studio — start / url / stop send what the space says
# ---------------------------------------------------------------------------

def run_studio(tree, *args):
    tree.children["studio"].invoke(list(args))


def test_start_sends_the_spaces_own_resource_spec(sagemaker_tree, studio_sm, capsys):
    def never_existed(**p):
        raise client_error("ValidationException", "not found", "DescribeApp")

    cli = studio_sm(studio_client(
        create_app={"AppArn": "arn:aws:sagemaker:us-west-2:1:app/d-aaa/s/JupyterLab/default"},
        describe_app=never_existed,
    ))
    run_studio(sagemaker_tree, "start")
    params, = calls_of(cli, "create_app")
    assert params == {
        "DomainId": "d-aaa", "SpaceName": "data-prep-space",
        "AppType": "JupyterLab", "AppName": "default",
        "ResourceSpec": {"InstanceType": "ml.t3.medium",
                         "SageMakerImageArn": IMAGE_ARN},
    }
    # The image pin is the whole reason the spec is sent, so it must be visible.
    assert IMAGE_ARN in capsys.readouterr().out


def test_start_instance_type_overrides_only_the_instance(sagemaker_tree, studio_sm):
    cli = studio_sm(studio_client(
        create_app={"AppArn": "a"},
        describe_app=lambda **p: {"Status": "Deleted"},
    ))
    run_studio(sagemaker_tree, "start", "--instance-type", "ml.m5.xlarge")
    params, = calls_of(cli, "create_app")
    assert params["ResourceSpec"] == {"InstanceType": "ml.m5.xlarge",
                                      "SageMakerImageArn": IMAGE_ARN}


def test_start_can_be_told_to_send_no_spec_at_all(sagemaker_tree, studio_sm):
    cli = studio_sm(studio_client(
        create_app={"AppArn": "a"}, describe_app=lambda **p: {"Status": "Deleted"}))
    run_studio(sagemaker_tree, "start", "--no-resource-spec")
    params, = calls_of(cli, "create_app")
    assert "ResourceSpec" not in params


def test_start_does_not_start_a_second_app(sagemaker_tree, studio_sm, capsys):
    cli = studio_sm(studio_client(
        create_app={"AppArn": "a"}, describe_app=lambda **p: {"Status": "InService"}))
    run_studio(sagemaker_tree, "start")
    assert calls_of(cli, "create_app") == []
    assert "already InService" in capsys.readouterr().out


def test_url_defaults_to_the_spaces_owner_and_lands_on_the_app(
        sagemaker_tree, studio_sm):
    cli = studio_sm(studio_client(
        space=_space("data-prep-space-shared", sharing="Shared", owner="annotator-a"),
        spaces=[_space_summary("data-prep-space-shared", sharing="Shared")],
        apps=[_app("data-prep-space-shared")],
        create_presigned_domain_url={"AuthorizedUrl": "https://example/x"},
    ))
    run_studio(sagemaker_tree, "url")
    params, = calls_of(cli, "create_presigned_domain_url")
    assert params == {"DomainId": "d-aaa", "UserProfileName": "annotator-a",
                      "SpaceName": "data-prep-space-shared",
                      "LandingUri": "app:JupyterLab:",
                      "ExpiresInSeconds": 300,
                      "SessionExpirationDurationInSeconds": 43200}


def test_url_takes_another_profile_for_the_same_shared_space(
        sagemaker_tree, studio_sm):
    cli = studio_sm(studio_client(
        space=_space("shared", sharing="Shared", owner="annotator-a"),
        spaces=[_space_summary("shared", sharing="Shared")],
        apps=[_app("shared")],
        create_presigned_domain_url={"AuthorizedUrl": "https://example/x"},
    ))
    run_studio(sagemaker_tree, "url", "shared", "--user-profile", "annotator-b",
               "--expires", "60")
    params, = calls_of(cli, "create_presigned_domain_url")
    assert params["UserProfileName"] == "annotator-b"
    assert params["ExpiresInSeconds"] == 60


def test_url_with_a_profile_and_no_space_is_the_domain_home(
        sagemaker_tree, studio_sm):
    cli = studio_sm(studio_client(
        create_presigned_domain_url={"AuthorizedUrl": "https://example/x"}))
    run_studio(sagemaker_tree, "url", "--user-profile", "data-prep-user")
    params, = calls_of(cli, "create_presigned_domain_url")
    assert "SpaceName" not in params and "LandingUri" not in params
    assert calls_of(cli, "describe_space") == []
    # No app is addressed, so there is no app to insist on.
    assert calls_of(cli, "list_apps") == []


def test_url_explains_a_model_that_cannot_point_at_a_space(
        sagemaker_tree, studio_sm, monkeypatch, capsys):
    studio_sm(studio_client(
        create_presigned_domain_url={"AuthorizedUrl": "https://example/x"}))
    monkeypatch.setattr(render, "input_members",
                        lambda *a: frozenset({"DomainId", "UserProfileName"}))
    run_studio(sagemaker_tree, "url")
    err = capsys.readouterr().err
    assert "SpaceName" in err and "upgrade botocore" in err


# ── studio — a URL is only worth minting if there is an app to land on ──────
#
# The URL goes to someone without an AWS identity and lasts minutes: a link to a
# stopped app fails in their browser, looking exactly like an expired one. So the
# caller — the only party who can start the app — hears about it here instead.

def _presigning_client(**extra):
    return studio_client(create_presigned_domain_url={
        "AuthorizedUrl": "https://example/x"}, **extra)


def test_url_refuses_when_nothing_is_running_in_the_space(
        sagemaker_tree, studio_sm, capsys):
    cli = studio_sm(_presigning_client(apps=[]))
    run_studio(sagemaker_tree, "url")
    err = capsys.readouterr().err
    assert "no live JupyterLab app in data-prep-space" in err
    assert "studio start data-prep-space" in err       # names the way out
    assert calls_of(cli, "create_presigned_domain_url") == []


def test_url_does_not_count_an_app_that_is_on_its_way_out(
        sagemaker_tree, studio_sm, capsys):
    """`Deleting` / `Deleted` / `Failed` are not something to land on."""
    cli = studio_sm(_presigning_client(
        apps=[_app("data-prep-space", status="Deleting"),
              _app("data-prep-space", status="Failed", name="earlier")]))
    run_studio(sagemaker_tree, "url")
    assert "no live JupyterLab app" in capsys.readouterr().err
    assert calls_of(cli, "create_presigned_domain_url") == []


def test_url_wants_the_app_type_it_is_actually_landing_on(
        sagemaker_tree, studio_sm, capsys):
    """A live JupyterLab does not make `--app-type KernelGateway` reachable."""
    cli = studio_sm(_presigning_client(apps=[_app("data-prep-space")]))
    run_studio(sagemaker_tree, "url", "--app-type", "KernelGateway")
    assert "no live KernelGateway app" in capsys.readouterr().err
    assert calls_of(cli, "create_presigned_domain_url") == []


def test_no_app_check_mints_the_url_anyway(sagemaker_tree, studio_sm):
    """Studio's own space page can start the app, so the link is still useful."""
    cli = studio_sm(_presigning_client(apps=[]))
    run_studio(sagemaker_tree, "url", "--no-app-check")
    params, = calls_of(cli, "create_presigned_domain_url")
    assert params["LandingUri"] == "app:JupyterLab:"


def test_an_explicit_landing_uri_is_left_alone(sagemaker_tree, studio_sm):
    """--landing-uri is the caller aiming somewhere that need not be an app."""
    cli = studio_sm(_presigning_client(apps=[]))
    run_studio(sagemaker_tree, "url", "--landing-uri", "studio::/spaces")
    params, = calls_of(cli, "create_presigned_domain_url")
    assert params["LandingUri"] == "studio::/spaces"
    assert calls_of(cli, "list_apps") == []


def test_url_mints_for_a_pending_app_but_says_it_is_not_ready(
        sagemaker_tree, studio_sm, capsys):
    """Having the link ready while the app boots is the normal thing to want."""
    cli = studio_sm(_presigning_client(
        apps=[_app("data-prep-space", status="Pending")]))
    run_studio(sagemaker_tree, "url")
    assert calls_of(cli, "create_presigned_domain_url")
    assert "is Pending, not InService yet" in capsys.readouterr().err


def test_url_still_works_when_apps_cannot_be_listed(
        sagemaker_tree, studio_sm, capsys):
    """A missing ListApps permission is not evidence that nothing is running."""
    def denied(**p):
        raise client_error("AccessDeniedException", "not authorized", "ListApps")

    cli = studio_sm(_presigning_client(list_apps=denied))
    run_studio(sagemaker_tree, "url")
    out = capsys.readouterr()
    assert "https://example/x" in out.out
    assert "could not check for a running app" in out.err


def test_open_mints_the_same_url_url_would_and_hands_it_to_the_browser(
        sagemaker_tree, studio_sm, monkeypatch, capsys):
    """`open` is `url` plus the browser — same call, same printed link."""
    cli = studio_sm(_presigning_client(apps=[_app("data-prep-space")]))
    opened = []

    def fake_open(url):
        opened.append(url)
        return True

    monkeypatch.setattr(studio.webbrowser, "open", fake_open)
    run_studio(sagemaker_tree, "open")
    params, = calls_of(cli, "create_presigned_domain_url")
    assert params["LandingUri"] == "app:JupyterLab:"
    assert opened == ["https://example/x"]
    # Printed as well: a browser can open the wrong identity's window.
    assert "https://example/x" in capsys.readouterr().out


def test_open_refuses_to_start_an_app_it_would_land_on_nothing(
        sagemaker_tree, studio_sm, monkeypatch, capsys):
    """The app check is in the shared minter, so it guards both leaves."""
    cli = studio_sm(_presigning_client(apps=[]))
    monkeypatch.setattr(studio.webbrowser, "open",
                        lambda url: pytest.fail("should not have opened"))
    run_studio(sagemaker_tree, "open")
    assert calls_of(cli, "create_presigned_domain_url") == []
    assert "would open on nothing" in capsys.readouterr().err


def test_open_says_so_when_there_is_no_browser_to_open(
        sagemaker_tree, studio_sm, monkeypatch, capsys):
    """A headless host must not report success — the URL expires in minutes."""
    studio_sm(_presigning_client(apps=[_app("data-prep-space")]))
    monkeypatch.setattr(studio.webbrowser, "open", lambda url: False)
    run_studio(sagemaker_tree, "open")
    out = capsys.readouterr()
    assert "https://example/x" in out.out
    assert "no browser could be opened" in out.err


def test_stop_confirms_before_deleting(sagemaker_tree, studio_sm, monkeypatch,
                                       capsys):
    cli = studio_sm(studio_client(
        describe_app=lambda **p: {"Status": "InService"}, delete_app={}))
    monkeypatch.setattr(studio, "passthrough_input", lambda prompt: "n")
    run_studio(sagemaker_tree, "stop")
    assert calls_of(cli, "delete_app") == []
    assert "not stopped" in capsys.readouterr().out


def test_stop_with_yes_deletes_the_app(sagemaker_tree, studio_sm, monkeypatch):
    cli = studio_sm(studio_client(
        describe_app=lambda **p: {"Status": "InService"}, delete_app={}))
    monkeypatch.setattr(studio, "passthrough_input",
                        lambda prompt: pytest.fail("-y must not prompt"))
    run_studio(sagemaker_tree, "stop", "-y")
    assert calls_of(cli, "delete_app") == [
        {"DomainId": "d-aaa", "SpaceName": "data-prep-space",
         "AppType": "JupyterLab", "AppName": "default"}]


def test_stop_all_covers_every_live_app_and_skips_the_dead(
        sagemaker_tree, studio_sm):
    cli = studio_sm(studio_client(
        apps=[_app("data-prep-space"),
              _app("data-prep-space-shared", status="Pending"),
              _app("old-space", status="Deleted")],
        delete_app={},
    ))
    run_studio(sagemaker_tree, "stop", "--all", "-y")
    stopped = [p["SpaceName"] for p in calls_of(cli, "delete_app")]
    assert stopped == ["data-prep-space", "data-prep-space-shared"]


def test_stop_all_keeps_going_after_one_app_refuses(sagemaker_tree, studio_sm,
                                                    capsys):
    def delete(**p):
        if p["SpaceName"] == "a":
            raise client_error("ResourceInUse", "busy", "DeleteApp")
        return {}

    cli = studio_sm(studio_client(apps=[_app("a"), _app("b")], delete_app=delete))
    run_studio(sagemaker_tree, "stop", "--all", "-y")
    assert [p["SpaceName"] for p in calls_of(cli, "delete_app")] == ["a", "b"]
    assert "busy" in capsys.readouterr().err


def test_stop_all_with_a_space_is_a_usage_error(sagemaker_tree, studio_sm, capsys):
    cli = studio_sm(studio_client(apps=[_app()], delete_app={}))
    run_studio(sagemaker_tree, "stop", "data-prep-space", "--all", "-y")
    assert calls_of(cli, "delete_app") == []
    assert "--all" in capsys.readouterr().err


def test_stop_says_nothing_to_stop_for_a_dead_app(sagemaker_tree, studio_sm, capsys):
    cli = studio_sm(studio_client(
        describe_app=lambda **p: {"Status": "Deleted"}, delete_app={}))
    run_studio(sagemaker_tree, "stop", "-y")
    assert calls_of(cli, "delete_app") == []
    assert "already Deleted" in capsys.readouterr().out


def test_apps_hides_the_dead_ones_unless_asked(sagemaker_tree, studio_sm, capsys):
    studio_sm(studio_client(apps=[_app("live"), _app("gone", status="Deleted")]))
    run_studio(sagemaker_tree, "apps")
    out = capsys.readouterr().out
    assert "live" in out and "gone" not in out
    assert "--include-dead" in out

    run_studio(sagemaker_tree, "apps", "--include-dead")
    assert "gone" in capsys.readouterr().out


def test_profiles_lists_one_row_per_profile_with_the_role_it_assumes(
        sagemaker_tree, studio_sm, capsys):
    domain_role = "arn:aws:iam::1:role/domain-default"
    space_role = "arn:aws:iam::1:role/shared-space"
    own_role = "arn:aws:iam::1:role/annotator"
    studio_sm(studio_client(
        profiles=[{"UserProfileName": "annotator-a", "Status": "InService",
                   "CreationTime": utc(2026, 2, 1)}],
        describe_domain={"AuthMode": "IAM",
                         "DefaultUserSettings": {"ExecutionRole": domain_role},
                         "DefaultSpaceSettings": {"ExecutionRole": space_role}},
        describe_user_profile={"UserSettings": {"ExecutionRole": own_role}},
    ))
    run_studio(sagemaker_tree, "profiles")
    out = capsys.readouterr().out
    for role in (domain_role, space_role, own_role):
        assert role in out                       # in full, never truncated
    # The domain's two defaults are its properties, so they head the output
    # rather than sitting in the table as pseudo-profiles.
    header, _, table = out.partition("USER PROFILE")
    assert domain_role in header and space_role in header
    assert own_role in table and "annotator-a" in table


def test_profiles_marks_one_that_inherits_rather_than_showing_a_blank(
        sagemaker_tree, studio_sm, capsys):
    studio_sm(studio_client(
        profiles=[{"UserProfileName": "data-prep-user"}],
        describe_domain={"AuthMode": "IAM"},
        describe_user_profile={"UserSettings": {}},
    ))
    run_studio(sagemaker_tree, "profiles")
    assert "inherits DefaultUserSettings" in capsys.readouterr().out


def test_profiles_says_so_for_a_domain_with_none(
        sagemaker_tree, studio_sm, capsys):
    """A domain used only for shared spaces legitimately has no profiles.

    Said by the header line the listing always prints, not by a sentence only
    the empty case has — that is what makes "zero" read the same across the
    whole tree.
    """
    studio_sm(studio_client(profiles=[], describe_domain={"AuthMode": "IAM"}))
    run_studio(sagemaker_tree, "profiles")
    out = capsys.readouterr().out
    assert "0 user profile(s)" in out
    assert "USER PROFILE" not in out


# ---------------------------------------------------------------------------
# studio watch — a timeline of what is starting and stopping
# ---------------------------------------------------------------------------

@pytest.fixture
def instant_polls(monkeypatch):
    """Make every poll interval zero, so a watch test runs at full speed.

    Returns the installer, so a test of ``-f`` — which by design never returns
    on its own — can bound the run the way a person does, with Ctrl+C, instead
    of racing a tiny ``--timeout``.
    """
    def install(detach_after=None):
        polls = {"n": 0}

        def sleep(seconds, beat):
            polls["n"] += 1
            if detach_after is not None and polls["n"] >= detach_after:
                raise KeyboardInterrupt

        monkeypatch.setattr(studio, "sleep_with_dots", sleep)

    install()
    return install


def ticking(*frames):
    """An operation whose successive calls return successive *frames*.

    The last frame repeats, so a watch that polls once more than the test
    scripted sees no spurious change.
    """
    state = {"i": 0}

    def call(**params):
        frame = frames[min(state["i"], len(frames) - 1)]
        state["i"] += 1
        return frame

    return call


def app_frames(*statuses, space="data-prep-space"):
    """One ListApps response per status, for a single app changing state."""
    return ticking(*({"Apps": [_app(space, status=s)]} for s in statuses))


def test_watch_follows_an_app_from_pending_to_inservice(
        sagemaker_tree, studio_sm, instant_polls, capsys):
    studio_sm(studio_client(
        list_apps=app_frames("Pending", "Pending", "InService")))
    run_studio(sagemaker_tree, "watch")
    out = capsys.readouterr().out
    # Baseline states it, the transition line names both ends, and the run ends
    # on its own once nothing is in flight — no Ctrl+C needed.
    assert "app data-prep-space/JupyterLab/default  Pending" in out
    assert "Pending → InService" in out
    assert "settled" in out


def test_watch_follows_a_stop_to_gone(
        sagemaker_tree, studio_sm, instant_polls, capsys):
    """``Deleting`` is in flight, so it is baselined and then followed."""
    studio_sm(studio_client(list_apps=app_frames("Deleting", "Deleted")))
    run_studio(sagemaker_tree, "watch")
    out = capsys.readouterr().out
    assert "Deleting" in out and "Deleting → Deleted" in out
    assert "settled" in out


def test_watch_keeps_history_out_of_the_baseline(
        sagemaker_tree, studio_sm, instant_polls, capsys):
    """ListApps returns every app the domain ever ran; a watch is about now.

    A hundred rows of yesterday's apps would bury the one that is moving, and
    neither a ``Deleted`` nor an already-``Failed`` app can transition again.
    """
    history = [_app("old-space", status="Deleted"),
               _app("burnt-space", status="Failed")]
    studio_sm(studio_client(list_apps=ticking(
        {"Apps": history + [_app("live-space", status="Pending")]},
        {"Apps": history + [_app("live-space", status="InService")]},
    )))
    run_studio(sagemaker_tree, "watch")
    out = capsys.readouterr().out
    assert "Pending → InService" in out
    # Not on the second pass either: a row filed as history stays filed while
    # its status holds, rather than resurfacing as one that just appeared.
    assert "old-space" not in out and "burnt-space" not in out


def test_watch_reports_a_relaunch_into_a_dead_apps_row(
        sagemaker_tree, studio_sm, instant_polls, capsys):
    """A space's app is always named ``default``, so a relaunch reuses the row.

    Filing yesterday's ``Deleted`` row away must therefore be conditional on it
    not moving — otherwise the launch this watch exists to follow is hidden
    behind the corpse of the last one.  Needs ``-f``: with only a dead app in
    the domain there is nothing in flight, and a plain watch has an answer for
    that already.
    """
    studio_sm(studio_client(
        list_apps=app_frames("Deleted", "Pending", "InService")))
    instant_polls(detach_after=3)
    run_studio(sagemaker_tree, "watch", "-f")
    out = capsys.readouterr().out
    assert "app data-prep-space/JupyterLab/default  Pending" in out
    assert "Pending → InService" in out
    assert "detached" in out


def test_watch_returns_at_once_when_nothing_is_moving(
        sagemaker_tree, studio_sm, instant_polls, capsys):
    """An idle domain gets an answer, not a wait — and is told how to wait."""
    studio_sm(studio_client(apps=[_app(status="InService")]))
    run_studio(sagemaker_tree, "watch")
    out = capsys.readouterr().out
    assert "nothing is starting or stopping" in out
    assert "--follow" in out


def test_watch_explains_a_failure_instead_of_just_naming_it(
        sagemaker_tree, studio_sm, instant_polls, capsys):
    """A bare ``Failed`` is the one line nobody can act on, so pull the reason."""
    studio_sm(studio_client(
        list_apps=app_frames("Pending", "Failed"),
        describe_app={"Status": "Failed",
                      "FailureReason": "lifecycle config exited 1"},
    ))
    run_studio(sagemaker_tree, "watch")
    out = capsys.readouterr().out
    assert "Pending → Failed" in out
    assert "lifecycle config exited 1" in out


def test_watch_reports_a_space_that_is_still_pending(
        sagemaker_tree, studio_sm, instant_polls, capsys):
    """The space is the container: no app can start until it is InService."""
    pending = _space_summary("data-prep-space")
    pending["Status"] = "Pending"
    ready = dict(pending, Status="InService")
    studio_sm(studio_client(
        list_spaces=ticking({"Spaces": [pending]}, {"Spaces": [ready]})))
    run_studio(sagemaker_tree, "watch")
    out = capsys.readouterr().out
    assert "space data-prep-space  Pending" in out
    assert "Pending → InService" in out


def test_watch_scopes_to_one_space_server_side(
        sagemaker_tree, studio_sm, instant_polls, capsys):
    cli = studio_sm(studio_client(
        spaces=[_space_summary("data-prep-space"), _space_summary("other")],
        apps=[_app("data-prep-space", status="InService")]))
    run_studio(sagemaker_tree, "watch", "--space", "data-prep-space")
    out = capsys.readouterr().out
    assert "other" not in out
    assert all(p.get("SpaceNameEquals") == "data-prep-space"
               for p in calls_of(cli, "list_apps"))


def test_watch_refuses_an_unknown_space_rather_than_reading_idle(
        sagemaker_tree, studio_sm, instant_polls, capsys):
    """A typo would otherwise match nothing every tick and look like an idle
    domain — the opposite of an answer."""
    studio_sm(studio_client(spaces=[_space_summary("data-prep-space")]))
    run_studio(sagemaker_tree, "watch", "--space", "ghost")
    err = capsys.readouterr().err
    assert "ghost" in err and "data-prep-space" in err


def test_watch_survives_a_track_it_cannot_read(
        sagemaker_tree, studio_sm, instant_polls, capsys):
    """ListSpaces is a permission of its own; losing it costs that track only."""
    denied = client_error("AccessDeniedException", "no ListSpaces", "ListSpaces")

    def refuse(**p):
        raise denied

    studio_sm(studio_client(list_spaces=refuse,
                            list_apps=app_frames("Pending", "InService")))
    run_studio(sagemaker_tree, "watch")
    captured = capsys.readouterr()
    assert "spaces not listed" in captured.err
    assert "Pending → InService" in captured.out


def test_watch_says_what_is_still_moving_when_it_gives_up(
        sagemaker_tree, studio_sm, instant_polls, capsys):
    studio_sm(studio_client(apps=[_app(status="Pending")]))
    run_studio(sagemaker_tree, "watch", "--timeout", "0.0001")
    out = capsys.readouterr().out
    assert "timeout" in out
    assert "app data-prep-space/JupyterLab/default" in out


def test_watch_keeps_going_past_settled_with_follow(
        sagemaker_tree, studio_sm, instant_polls, capsys):
    """``-f`` is what makes watching *before* a start possible."""
    studio_sm(studio_client(apps=[_app(status="InService")]))
    instant_polls(detach_after=2)
    run_studio(sagemaker_tree, "watch", "-f")
    out = capsys.readouterr().out
    assert "nothing is starting or stopping" not in out
    assert "detached; 0 resource(s) still moving" in out


# ---------------------------------------------------------------------------
# studio — "is anything running here?" reads the same everywhere
# ---------------------------------------------------------------------------

@pytest.fixture
def no_completion_cache(monkeypatch):
    """Bypass the TTL cache so each completer call sees its own fake client."""
    monkeypatch.setattr(studio, "get_or_fetch", lambda key, fn, **kw: fn())


def space_descriptions(prefix=""):
    """``{space name: the metadata strip the picker draws beside it}``.

    Composed through the picker's own column layout, so what these tests read
    is what lands on screen — padding, dropped-empty columns and all.
    """
    rows = studio._SpaceCompleter().complete(ctx(prefix))
    widths = _meta_col_widths(rows, lambda c: c.meta)
    return {c.value: _compose_meta(c.meta, widths) for c in rows}


def test_the_space_completer_reports_the_app_not_the_spaces_own_status(
        studio_sm, no_completion_cache):
    """A stopped space is InService too — saying so unqualified misreads as live.

    Regression: both rows of a two-space domain showed a bare ``InService``
    while ``apps`` listed one live app, because the status came from ListSpaces.
    """
    studio_sm(studio_client(
        spaces=[_space_summary("data-prep-space"),
                _space_summary("data-prep-space-shared", sharing="Shared",
                               owner="annotator-a")],
        apps=[_app("data-prep-space"),
              _app("data-prep-space-shared", status="Deleted")],
    ))
    got = space_descriptions()
    # Aligned columns, no owner: the sharing cell is padded to the width of the
    # widest one so the app answers start at the same screen column.
    assert got["data-prep-space"] == "Private  JupyterLab  app InService"
    assert got["data-prep-space-shared"] == "Shared   JupyterLab  no app"


def test_the_space_completer_leaves_the_owner_out(studio_sm, no_completion_cache):
    """Nothing picks a space by owner, so the column is only noise here."""
    studio_sm(studio_client(
        spaces=[_space_summary("data-prep-space", owner="annotator-a")], apps=[]))
    assert "annotator-a" not in space_descriptions()["data-prep-space"]


def test_the_space_completer_labels_a_space_status_worth_seeing(
        studio_sm, no_completion_cache):
    summary = _space_summary("pending-space")
    summary["Status"] = "Pending"
    studio_sm(studio_client(spaces=[summary], apps=[]))
    assert "space Pending" in space_descriptions()["pending-space"]


def test_the_space_completer_counts_several_live_apps(
        studio_sm, no_completion_cache):
    studio_sm(studio_client(
        spaces=[_space_summary("busy")],
        apps=[_app("busy", app_type="JupyterServer", name="default"),
              _app("busy", app_type="KernelGateway", name="kg-1")],
    ))
    assert "2 apps live" in space_descriptions()["busy"]


def test_the_space_completer_stays_silent_about_apps_it_cannot_list(
        studio_sm, no_completion_cache):
    """No ListApps permission must not render as "nothing is running"."""
    def denied(**p):
        raise client_error("AccessDeniedException", "no ListApps", "ListApps")

    studio_sm(studio_client(spaces=[_space_summary("s")], list_apps=denied))
    got = space_descriptions()["s"]
    assert "s" in space_descriptions()      # still a usable candidate
    assert "no app" not in got and "app " not in got


def test_the_spaces_table_and_the_completer_read_the_same_source(
        sagemaker_tree, studio_sm, no_completion_cache, capsys):
    cli = studio_client(
        spaces=[_space_summary("live-space"), _space_summary("idle-space")],
        apps=[_app("live-space"), _app("idle-space", status="Deleting")],
    )
    studio_sm(cli)
    run_studio(sagemaker_tree, "spaces")
    out = capsys.readouterr().out
    table = {ln.split()[0]: ln for ln in out.splitlines()
             if ln.startswith(("live-space", "idle-space"))}
    # Deleting counts as dead in both places, so neither calls it running.
    assert "JupyterLab:InService" in table["live-space"]
    assert "JupyterLab:" not in table["idle-space"]
    described = space_descriptions()
    assert described["live-space"].endswith("app InService")
    assert described["idle-space"].endswith("no app")
    # Which STATUS the column is stays documented — in the leaf's help, not as a
    # note under every listing (it reads the same on every run).
    assert "STATUS is the space's own" in (
        sagemaker_tree.children["studio"].children["spaces"].help_text)


# ---------------------------------------------------------------------------
# studio — the boot log
# ---------------------------------------------------------------------------

def test_logs_builds_the_stream_path_from_the_domain_and_space(
        sagemaker_tree, studio_sm, monkeypatch, capsys):
    logs = FakeLogs(events=[{"message": "installing skills\n"}])
    studio_sm(studio_client())
    monkeypatch.setattr(render, "logs_client", lambda: logs)
    run_studio(sagemaker_tree, "log")
    params = dict(logs.calls[0][1])
    assert params["logGroupName"] == "/aws/sagemaker/studio"
    assert params["logStreamName"] == \
        "d-aaa/data-prep-space/JupyterLab/default/LifecycleConfigOnStart"
    assert "installing skills" in capsys.readouterr().out


def test_logs_reads_another_stream_of_the_same_app(
        sagemaker_tree, studio_sm, monkeypatch):
    logs = FakeLogs(events=[])
    studio_sm(studio_client())
    monkeypatch.setattr(render, "logs_client", lambda: logs)
    run_studio(sagemaker_tree, "log", "--stream", "JupyterLab")
    assert logs.calls[0][1]["logStreamName"].endswith("/default/JupyterLab")


def test_logs_explains_an_absent_stream_instead_of_failing(
        sagemaker_tree, studio_sm, monkeypatch, capsys):
    """A lifecycle config that does not fire for a space is a real answer."""
    studio_sm(studio_client())
    monkeypatch.setattr(render, "logs_client", lambda: FakeLogs(missing=True))
    run_studio(sagemaker_tree, "log")
    out = capsys.readouterr().out
    assert "no such log group or stream" in out and "--list" in out


def test_logs_list_shows_what_streams_the_space_has(
        sagemaker_tree, studio_sm, monkeypatch, capsys):
    prefix = "d-aaa/data-prep-space/"
    logs = FakeLogs(streams=[
        {"logStreamName": prefix + "JupyterLab/default/LifecycleConfigOnStart"},
    ])
    studio_sm(studio_client())
    monkeypatch.setattr(render, "logs_client", lambda: logs)
    run_studio(sagemaker_tree, "log", "--list")
    out = capsys.readouterr().out
    # Listed relative to the space, because that is what --stream takes.
    rows = [ln for ln in out.splitlines() if ln.startswith("JupyterLab/")]
    assert rows == [] or prefix not in rows[0]
    assert rows and rows[0].startswith("JupyterLab/default/LifecycleConfigOnStart")
    # That a listed row feeds --stream is said once, on the flag that lists them,
    # not as a note under the listing itself.
    logs_leaf = sagemaker_tree.children["studio"].children["log"]
    list_flag = next(p for p in logs_leaf.params if "--list" in p.names)
    assert "--stream" in list_flag.kwargs["help"]


# ---------------------------------------------------------------------------
# Completers degrade to silence, never to a traceback
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("completer", [
    jobs._JobNameCompleter(),
    hub._HubNameCompleter(),
    hub._ContentNameCompleter(),
    hub._ContentVersionCompleter(),
    hub._TraceRefCompleter(),
    studio._DomainCompleter(),
    studio._SpaceCompleter(),
    studio._UserProfileCompleter(),
    studio._StreamCompleter(),
])
def test_completers_return_nothing_when_the_api_is_unreachable(monkeypatch, completer):
    def boom(*a, **k):
        raise client_error("AccessDeniedException", "no")

    monkeypatch.setattr(render, "sm_client", boom)
    monkeypatch.setattr(jobs, "sm_client", boom)
    monkeypatch.setattr(hub, "sm_client", boom)
    monkeypatch.setattr(studio, "sm_client", boom)
    monkeypatch.setattr(studio, "logs_client", boom)
    monkeypatch.setattr(render, "logs_client", boom)
    assert completer.complete(ctx("x", args=["some-name"])) == []


def test_content_version_completer_needs_a_name_first(monkeypatch):
    called = []
    monkeypatch.setattr(hub, "sm_client", lambda *a, **k: called.append(1))
    assert hub._ContentVersionCompleter().complete(ctx("", args=["--hub", "H"])) == []
    assert called == []


def test_content_type_completer_offers_all_only_where_it_applies():
    assert "all" in [c.value for c in
                     hub._ContentTypeCompleter(allow_all=True).complete(ctx())]
    assert "all" not in [c.value for c in
                         hub._ContentTypeCompleter().complete(ctx())]


# ---------------------------------------------------------------------------
# Command tree
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def sagemaker_tree():
    from pitash.recipes import enable
    enable("awsut")
    return command_registry.get("awsut").children["sagemaker"]


def test_the_tree_has_every_group(sagemaker_tree):
    assert set(sagemaker_tree.children) == {"jobs", "hub", "studio", "hyperpod"}
    assert set(sagemaker_tree.children["jobs"].children) == {
        "list", "describe", "log", "watch", "stop"}
    assert set(sagemaker_tree.children["hub"].children) == {
        "hubs", "list", "versions", "describe", "files", "trace"}
    assert set(sagemaker_tree.children["studio"].children) == {
        "domains", "spaces", "apps", "profiles", "log", "watch", "start", "url",
        "open", "stop"}
    assert set(sagemaker_tree.children["hyperpod"].children) == {
        "create", "update", "scale", "add-ig", "remove-ig", "delete-nodes",
        "reboot-nodes", "replace-nodes", "upgrade-ami", "delete", "list",
        "describe", "watch", "log", "ssm", "ssh", "run", "search-capacity",
        "kubeconfig", "events"}


def test_hyperpod_is_a_sagemaker_group_not_a_root_of_its_own():
    """A HyperPod cluster is a SageMaker resource; `hp` pays the depth."""
    assert "hyperpod" not in command_registry.get("awsut").children


def test_events_csv_is_csv_and_survives_a_comma(
        sagemaker_tree, monkeypatch, capsys):
    """``--format csv`` writes quoted CSV, not the tab-joined text it once did.

    An event Description routinely contains a comma, so hand-joining produced a
    file whose column count changed from row to row — the one output here meant
    to be read by another program.
    """
    events = [{
        "EventId": "e1",
        "EventTime": utc(2026, 1, 2, 3, 4, 5),
        "EventLevel": "Info",
        "ResourceType": "Cluster",
        "Description": 'scaled up, then down; said "ok"',
    }]
    monkeypatch.setattr(hyperpod.awsut, "_get_sagemaker_client", lambda **kw: None)
    monkeypatch.setattr(hyperpod, "_list_hyperpod_cluster_events_all",
                        lambda sm, name, **kw: events)

    sagemaker_tree.children["hyperpod"].children["events"].invoke(
        ["c1", "--format", "csv"])
    rows = capsys.readouterr().out.splitlines()
    assert rows[0].startswith("TIMESTAMP,LEVEL,TYPE")
    # The comma-bearing description is one field, quoted, with its own quotes
    # doubled — i.e. parseable back into exactly six columns.
    assert rows[1].endswith('"scaled up, then down; said ""ok"""')
    assert len(next(csv.reader([rows[1]]))) == 6


def _flags(node):
    out = set()
    for param in node.params or []:
        out.update(n for n in param.names if n.startswith("-"))
    return out


def _parser(node):
    """The parser a leaf is actually dispatched with (mirrors ``_invoke_tree``)."""
    parser = CmdParser(node.name, description=node.description or None)
    for param in _collect_inherited_params(node):
        parser.add_argument(*param.names, **param.kwargs)
    return parser


def test_every_jobs_leaf_inherits_category(sagemaker_tree):
    group = sagemaker_tree.children["jobs"]
    assert "--category" in _flags(group)
    # The leaves that take a job name need one supplied to parse at all.
    needs_name = ("describe", "log", "stop")
    for name, leaf in group.children.items():
        ns = _parser(leaf).parse_args(["j1"] if name in needs_name else [])
        assert ns.category is None, name
        assert _parser(leaf).parse_args(
            ["j1", "--category", "MyCategory"] if name in needs_name
            else ["--category", "MyCategory"]).category == "MyCategory"


def test_the_filter_flags_are_only_where_they_work(sagemaker_tree):
    leaves = sagemaker_tree.children["jobs"].children
    assert {"--since", "--status", "--contains"} <= _flags(leaves["list"])
    assert {"--since", "--status", "--contains"} <= _flags(leaves["watch"])
    assert not _flags(leaves["describe"]) & {"--since", "--status", "--contains"}
    assert not _flags(leaves["stop"]) & {"--since", "--status", "--contains"}


def test_hub_flag_is_not_offered_where_there_is_no_hub_to_pick(sagemaker_tree):
    leaves = sagemaker_tree.children["hub"].children
    assert "--hub" not in _flags(leaves["hubs"])
    assert "--type" not in _flags(leaves["hubs"])
    for name in ("list", "versions", "describe", "files", "trace"):
        assert "--hub" in _flags(leaves[name]), name


def test_domain_flag_is_not_offered_where_there_is_no_domain_to_pick(sagemaker_tree):
    leaves = sagemaker_tree.children["studio"].children
    assert "--domain" not in _flags(leaves["domains"])
    for name in ("spaces", "apps", "profiles", "log", "start", "url", "open",
                 "stop"):
        assert "--domain" in _flags(leaves[name]), name


def test_app_and_wait_flags_are_only_where_an_app_is_addressed(sagemaker_tree):
    leaves = sagemaker_tree.children["studio"].children
    for name in ("log", "start", "stop"):
        assert "--app-name" in _flags(leaves[name]), name
    for name in ("domains", "spaces", "apps", "profiles", "url", "open"):
        assert "--app-name" not in _flags(leaves[name]), name
    # Waiting only means something for the two leaves that change an app's state.
    assert {"--wait", "--timeout"} <= _flags(leaves["start"])
    assert {"--wait", "--timeout"} <= _flags(leaves["stop"])
    assert not _flags(leaves["log"]) & {"--wait", "--timeout"}


def test_only_the_stopping_leaf_takes_yes(sagemaker_tree):
    """-y is a confirmation bypass; offering it elsewhere would imply a prompt."""
    leaves = sagemaker_tree.children["studio"].children
    assert "-y" in _flags(leaves["stop"])
    for name in ("domains", "spaces", "apps", "profiles", "log", "start", "url",
                 "open"):
        assert "-y" not in _flags(leaves[name]), name


def test_studio_leaf_parsers_accept_the_documented_invocations(sagemaker_tree):
    leaves = sagemaker_tree.children["studio"].children

    start = _parser(leaves["start"])
    ns = start.parse_args(["my-space", "--instance-type", "ml.m5.large", "--wait"])
    assert (ns.space, ns.instance_type, ns.wait, ns.app_name, ns.timeout) == \
        ("my-space", "ml.m5.large", True, "default", 900)
    assert start.parse_args([]).space is None

    url = _parser(leaves["url"])
    ns = url.parse_args(["--user-profile", "me"])
    assert (ns.space, ns.user_profile, ns.expires, ns.session_duration) == \
        (None, "me", 300, 43200)
    assert ns.no_app_check is False
    assert url.parse_args(["--no-app-check"]).no_app_check is True

    logs = _parser(leaves["log"])
    ns = logs.parse_args(["my-space", "-f"])       # -f, as `hyperpod watch` spells it
    assert (ns.space, ns.follow, ns.stream, ns.log_group) == \
        ("my-space", True, studio.LIFECYCLE_STREAM, studio.STUDIO_LOG_GROUP)

    stop = _parser(leaves["stop"])
    ns = stop.parse_args(["--all", "-y"])
    assert (ns.stop_all, ns.yes, ns.space) == (True, True, None)   # not `all`


def test_max_never_shadows_the_builtin_in_a_handler(sagemaker_tree):
    """--max is stored as `limit`; a handler parameter named `max` would shadow it."""
    for group in sagemaker_tree.children.values():
        for leaf in group.children.values():
            for param in leaf.params or []:
                if "--max" in param.names:
                    assert param.kwargs.get("dest") == "limit"


def test_leaf_parsers_accept_the_documented_invocations(sagemaker_tree):
    jobs_list = _parser(sagemaker_tree.children["jobs"].children["list"])
    ns = jobs_list.parse_args(["--since", "24h", "--category", "extra"])
    assert (ns.since, ns.category, ns.limit, ns.sort) == \
        ("24h", "extra", 25, "CreationTime")

    watch = _parser(sagemaker_tree.children["jobs"].children["watch"])
    ns = watch.parse_args(["my-job", "--until-done", "-n", "5"])
    assert (ns.job_name, ns.until_done, ns.interval) == ("my-job", True, 5.0)
    assert watch.parse_args([]).job_name is None

    trace = _parser(sagemaker_tree.children["hub"].children["trace"])
    ns = trace.parse_args(["my-set", "--depth", "3"])
    assert (ns.ref, ns.depth, ns.yaml) == ("my-set", 3, False)

    describe = _parser(sagemaker_tree.children["hub"].children["describe"])
    ns = describe.parse_args(["my-set", "--type", "DataSet"])
    assert ns.content_type == "DataSet"       # dest avoids shadowing `type`


def test_studio_names_follow_the_conventions_the_rest_of_awsut_uses(sagemaker_tree):
    """The two idioms in this tree, and the one it does not use.

    Across ``awsut``, a listing is either a bare ``list`` (where the group has
    one obvious resource: ``ec2``, ``logs``, ``sagemaker jobs`` / ``hyperpod``)
    or a plural noun (where it has several: ``hub hubs`` / ``versions`` /
    ``files``).  Nothing anywhere is spelled ``list-<noun>``, and state changes
    are ``start`` / ``stop`` — so ``studio`` must not introduce a third style.
    """
    root = command_registry.get("awsut")

    def leaves(node, path=()):
        if not node.children:
            yield path, node
        for name, child in node.children.items():
            yield from leaves(child, path + (name,))

    for path, _leaf in leaves(root):
        assert not path[-1].startswith("list-"), " ".join(path)

    studio_leaves = sagemaker_tree.children["studio"].children
    assert {"start", "stop"} <= set(studio_leaves)   # the ec2 pair, not launch/stop
    assert "launch" not in studio_leaves
    # Every listing here is a plural noun, since the group holds three resource
    # types and a bare `list` could mean any of them.
    for name in ("domains", "spaces", "apps", "profiles"):
        assert name.endswith("s") and name in studio_leaves


def test_no_leaf_takes_region_or_profile_flags(sagemaker_tree):
    """Region and profile come from the aws recipe's Vars, not from flags."""
    for group in sagemaker_tree.children.values():
        for leaf in group.children.values():
            assert not _flags(leaf) & {"--region", "--profile"}
