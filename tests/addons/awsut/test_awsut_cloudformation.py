"""Tests for `awsut cloudformation watch` — per-resource event reporting."""

from __future__ import annotations

import datetime

from eosh.commands import registry as command_registry
from eosh.recipes import enable
from eosh_addons.awsut import cli as awsut


UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)


def _event(n, status="CREATE_IN_PROGRESS", logical="Bucket", reason=None,
           seconds=None):
    event = {
        "EventId": f"e{n}",
        "Timestamp": T0 + datetime.timedelta(seconds=n if seconds is None
                                             else seconds),
        "LogicalResourceId": logical,
        "ResourceType": "AWS::S3::Bucket",
        "ResourceStatus": status,
    }
    if reason:
        event["ResourceStatusReason"] = reason
    return event


class _FakeCf:
    """``describe_stack_events``, newest first, in pages of ``page``."""

    def __init__(self, events, page=2):
        self.events = sorted(events, key=lambda e: e["Timestamp"], reverse=True)
        self.page = page
        self.calls = 0

    def describe_stack_events(self, StackName, NextToken=None):
        self.calls += 1
        start = int(NextToken or 0)
        chunk = self.events[start:start + self.page]
        response = {"StackEvents": chunk}
        if start + self.page < len(self.events):
            response["NextToken"] = str(start + self.page)
        return response


def test_new_events_come_back_oldest_first_across_pages():
    cf = _FakeCf([_event(n) for n in range(1, 6)])
    seen: set[str] = set()
    events = awsut._cf_new_events(cf, "arn", T0, seen)
    assert [e["EventId"] for e in events] == ["e1", "e2", "e3", "e4", "e5"]
    assert seen == {"e1", "e2", "e3", "e4", "e5"}


def test_events_before_the_watch_started_are_not_reported():
    cf = _FakeCf([_event(n) for n in range(1, 6)])
    since = T0 + datetime.timedelta(seconds=3)
    events = awsut._cf_new_events(cf, "arn", since, set())
    assert [e["EventId"] for e in events] == ["e3", "e4", "e5"]


def test_already_reported_events_stop_the_paging():
    cf = _FakeCf([_event(n) for n in range(1, 11)], page=2)
    seen = {f"e{n}" for n in range(1, 9)}
    events = awsut._cf_new_events(cf, "arn", T0, seen)
    assert [e["EventId"] for e in events] == ["e9", "e10"]
    # e10,e9 on page one; e8 on page two is seen — no third page.
    assert cf.calls == 2


def test_a_second_poll_reports_only_what_arrived_since():
    events = [_event(1), _event(2)]
    cf = _FakeCf(events)
    seen: set[str] = set()
    awsut._cf_new_events(cf, "arn", T0, seen)
    cf.events.insert(0, _event(3, status="CREATE_COMPLETE"))
    fresh = awsut._cf_new_events(cf, "arn", T0, seen)
    assert [e["EventId"] for e in fresh] == ["e3"]
    assert awsut._cf_new_events(cf, "arn", T0, seen) == []


def test_event_line_names_stack_resource_status_and_reason():
    line = awsut._cf_event_line(
        "my-stack",
        _event(1, status="CREATE_FAILED", reason="Bucket already exists"))
    assert line == ("my-stack · Bucket (AWS::S3::Bucket) · CREATE_FAILED · "
                    "Bucket already exists")


def test_event_line_colors_the_status_only_on_a_tty():
    event = _event(1, status="CREATE_FAILED")
    assert awsut.RED not in awsut._cf_event_line("s", event)
    assert f"{awsut.RED}CREATE_FAILED{awsut.RESET}" in \
        awsut._cf_event_line("s", event, tty=True)
    assert awsut._cf_event_line("s", _event(1), tty=True).endswith(
        "CREATE_IN_PROGRESS")


# ---------------------------------------------------------------------------
# the watch leaf, end to end
# ---------------------------------------------------------------------------

class _WatchCf:
    """A stack that creates a bucket, fails it, and rolls back to *_FAILED."""

    def __init__(self, state):
        self.state = state

    def describe_stack_events(self, StackName, NextToken=None):
        now = datetime.datetime.now(UTC)
        tick = self.state["tick"]
        events = []
        if tick >= 1:
            events.append({**_event(1), "Timestamp": now})
        if tick >= 2:
            events.append({**_event(2, status="CREATE_FAILED",
                                    reason="already exists"),
                           "Timestamp": now})
        return {"StackEvents": events[::-1]}

    def list_stack_resources(self, StackName, NextToken=None):
        return {"StackResourceSummaries": [{
            "LogicalResourceId": "Role", "ResourceType": "AWS::IAM::Role",
            "ResourceStatus": "CREATE_IN_PROGRESS"}]}


def _watch_leaf(monkeypatch, stacks_per_tick):
    enable("awsut")
    state = {"tick": 0}

    def list_stacks(client, **kw):
        stacks = stacks_per_tick(state["tick"], kw)
        if not kw.get("include_deleted"):
            state["tick"] += 1
        return stacks

    monkeypatch.setattr(awsut, "_get_boto3_client", lambda n: _WatchCf(state))
    monkeypatch.setattr(awsut, "_list_cf_stacks", list_stacks)
    monkeypatch.setattr(awsut, "_region_label", lambda: "us-west-2")
    monkeypatch.setattr(awsut, "sleep_with_dots", lambda s, b: None)
    monkeypatch.setattr(awsut, "_report_cf_failure",
                        lambda c, s: print(f"FAILED {s['StackName']}"))
    return command_registry.get("awsut").children["cloudformation"] \
        .children["watch"]


def test_watch_reports_each_resource_transition_then_the_failure(
        monkeypatch, capsys):
    def stacks(tick, kw):
        status = "CREATE_IN_PROGRESS" if tick < 3 else "ROLLBACK_FAILED"
        stack = {"StackId": "arn:demo", "StackName": "demo",
                 "StackStatus": status}
        return [stack]

    _watch_leaf(monkeypatch, stacks).invoke([])
    out = capsys.readouterr().out
    assert "1 stack(s) in progress · region us-west-2" in out
    assert "  Role (AWS::IAM::Role) · CREATE_IN_PROGRESS" in out  # baseline
    lines = [l for l in out.splitlines() if "Bucket (" in l]
    assert len(lines) == 2                                        # no repeats
    assert lines[0].endswith("demo · Bucket (AWS::S3::Bucket) · "
                             "CREATE_IN_PROGRESS")
    assert lines[1].endswith("CREATE_FAILED · already exists")
    assert "All operations finished." in out
    assert out.rstrip().endswith("FAILED demo")


def test_watch_with_nothing_in_progress_says_so_and_returns(monkeypatch, capsys):
    _watch_leaf(monkeypatch, lambda tick, kw: []).invoke([])
    out = capsys.readouterr().out
    assert "0 stack(s) in progress" in out
    assert "All operations finished." not in out
