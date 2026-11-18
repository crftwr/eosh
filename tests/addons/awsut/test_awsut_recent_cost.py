"""Tests for `awsut recent-cost` — filters, slugs, axes and the table."""

from __future__ import annotations

import datetime

import pytest

from eosh.commands import registry as command_registry
from eosh.completion import CompletionContext
from eosh.recipes import enable
from eosh_addons.awsut import cli as awsut
from eosh_addons.awsut import cost
from eosh_addons.awsut.common import SmError


SERVICES = ["Amazon Elastic Compute Cloud - Compute", "EC2 - Other",
            "Amazon SageMaker", "AWS Lambda", "AWS Lambda@Edge"]


# ---------------------------------------------------------------------------
# slugs and resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("Amazon Elastic Compute Cloud - Compute", "elastic-compute-cloud-compute"),
    ("EC2 - Other", "ec2-other"),
    ("AWS Lambda", "lambda"),
    ("Amazon", "amazon"),
    ("On Demand Instances", "on-demand-instances"),
])
def test_slug_is_a_space_free_lowercase_name_without_the_vendor(name, expected):
    assert cost.slug(name) == expected


@pytest.mark.parametrize("given,expected", [
    ("Amazon SageMaker", ["Amazon SageMaker"]),         # exact
    ("amazon sagemaker", ["Amazon SageMaker"]),         # any case
    ("sagemaker", ["Amazon SageMaker"]),                # slug
    ("elastic-compute", ["Amazon Elastic Compute Cloud - Compute"]),  # prefix
    ("lambda", ["AWS Lambda"]),       # an exact slug beats a longer prefix hit
])
def test_resolve_maps_names_and_slugs_back_to_the_real_value(given, expected):
    assert cost.resolve(given, SERVICES, "service") == expected


def test_resolve_refuses_an_ambiguous_prefix_and_names_the_candidates():
    with pytest.raises(SmError, match="elastic-compute-cloud-compute, ec2-other"):
        cost.resolve("e", SERVICES, "service")


def test_resolve_refuses_a_name_nothing_matches():
    with pytest.raises(SmError, match="no service matches 'nope'"):
        cost.resolve("nope", SERVICES, "service")


# ---------------------------------------------------------------------------
# filter / axes
# ---------------------------------------------------------------------------

def test_one_flag_ors_its_values_and_different_flags_and():
    flt = cost.build_filter(
        {"service": ["A", "B"], "region": ["us-east-1"]},
        {"Project": ["x"]})
    assert flt == {"And": [
        {"Dimensions": {"Key": "SERVICE", "Values": ["A", "B"]}},
        {"Dimensions": {"Key": "REGION", "Values": ["us-east-1"]}},
        {"Tags": {"Key": "Project", "Values": ["x"]}},
    ]}


def test_a_single_clause_is_not_wrapped_and_no_clause_is_no_filter():
    assert cost.build_filter({"region": ["eu-west-1"]}, {}) == \
        {"Dimensions": {"Key": "REGION", "Values": ["eu-west-1"]}}
    assert cost.build_filter({}, {}) is None


def test_tag_flag_wants_key_equals_value():
    assert cost.parse_tags(["Team=ml", "Team=infra", "Env=dev"]) == \
        {"Team": ["ml", "infra"], "Env": ["dev"]}
    with pytest.raises(SmError):
        cost.parse_tags(["Team"])


@pytest.mark.parametrize("filtered,daily,expected", [
    (set(), False, ["service", "region"]),
    ({"service"}, False, ["usage-type", "region"]),
    ({"service", "usage-type"}, False, ["operation", "region"]),
    ({"region"}, False, ["service"]),       # a filtered axis is not a column
    ({"service"}, True, ["usage-type"]),    # daily: the days are the columns
])
def test_default_axes_go_one_level_below_the_filters(filtered, daily, expected):
    assert cost.default_by(filtered, daily) == expected


def test_by_accepts_dimensions_and_tags_and_rejects_the_rest():
    assert cost.parse_by("service,tag:Team") == ["service", "tag:Team"]
    for bad in ("bogus", "service,service", "a,b,c", "tag:"):
        with pytest.raises(SmError):
            cost.parse_by(bad)


def test_tag_group_keys_show_the_value_or_untagged():
    assert cost.key_label("tag:Team", "Team$ml") == "ml"
    assert cost.key_label("tag:Team", "Team$") == "(untagged)"


# ---------------------------------------------------------------------------
# the leaf, end to end
# ---------------------------------------------------------------------------

def _group(keys, amount):
    return {"Keys": keys, "Metrics": {"AmortizedCost": {"Amount": str(amount)}}}


class _Ce:
    def __init__(self, pages):
        self.pages = pages
        self.cost_calls: list[dict] = []
        self.dim_calls: list[dict] = []

    def get_dimension_values(self, **kw):
        self.dim_calls.append(kw)
        return {"DimensionValues": [{"Value": v} for v in SERVICES]}

    def get_cost_and_usage(self, **kw):
        self.cost_calls.append(kw)
        page = self.pages[len(self.cost_calls) - 1]
        return page


def _leaf(monkeypatch, ce):
    enable("awsut")
    monkeypatch.setattr(awsut, "_get_boto3_client", lambda n: ce)
    return command_registry.get("awsut").children["recent-cost"]


def test_service_slug_and_region_filter_drill_to_usage_types(monkeypatch, capsys):
    ce = _Ce([{"ResultsByTime": [{"TimePeriod": {"Start": "2026-11-01"},
                                  "Groups": [_group(["USE1-Hours"], 3.25)]}]}])
    _leaf(monkeypatch, ce).invoke(["--service", "sagemaker",
                                   "--region", "us-east-1"])
    call = ce.cost_calls[0]
    assert call["GroupBy"] == [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}]
    assert call["Filter"]["And"][0]["Dimensions"]["Values"] == ["Amazon SageMaker"]
    assert len(ce.dim_calls) == 1        # only the slugged flag is resolved
    out = capsys.readouterr().out
    assert "service Amazon SageMaker · region us-east-1" in out
    assert "USE1-Hours" in out and "3.25" in out


def test_daily_puts_every_day_in_a_column_and_follows_pages(monkeypatch, capsys):
    monkeypatch.setattr(cost, "period", lambda days: {
        "Start": "2026-11-16", "End": "2026-11-19"})
    ce = _Ce([
        {"ResultsByTime": [{"TimePeriod": {"Start": "2026-11-16"},
                            "Groups": [_group(["AWS Lambda"], 1)]}],
         "NextPageToken": "t"},
        {"ResultsByTime": [{"TimePeriod": {"Start": "2026-11-18"},
                            "Groups": [_group(["AWS Lambda"], 2)]}]},
    ])
    _leaf(monkeypatch, ce).invoke(["--daily", "--days", "3"])
    assert ce.cost_calls[0]["Granularity"] == "DAILY"
    assert ce.cost_calls[1]["NextPageToken"] == "t"
    out = capsys.readouterr().out
    assert "1 service(s) × 3 day(s)" in out
    header = next(l for l in out.splitlines() if l.startswith("SERVICE"))
    assert header.split() == ["SERVICE", "11-16", "11-17", "11-18", "TOTAL"]
    row = next(l for l in out.splitlines() if l.startswith("AWS Lambda"))
    assert row.split()[-1] == "3.00"


def test_an_unresolvable_service_is_an_error_not_an_empty_table(monkeypatch, capsys):
    ce = _Ce([])
    assert _leaf(monkeypatch, ce).invoke(["--service", "e"]) == 1
    assert "matches several services" in capsys.readouterr().err
    assert ce.cost_calls == []


# ---------------------------------------------------------------------------
# completion
# ---------------------------------------------------------------------------

def _ctx(args, prefix):
    return CompletionContext(command="awsut", args=args, arg_index=0,
                             prefix=prefix, line="")


def test_service_completes_to_slugs_with_the_full_name_beside_them(monkeypatch):
    monkeypatch.setattr(awsut, "_get_boto3_client", lambda n: _Ce([]))
    got = cost.DimensionCompleter("service").complete(
        _ctx(["recent-cost", "--service"], "e"))
    assert [(c.value, c.description) for c in got] == [
        ("ec2-other", "EC2 - Other"),
        ("elastic-compute-cloud-compute",
         "Amazon Elastic Compute Cloud - Compute"),
    ]
    assert all(" " not in c.value for c in got)


def test_by_completes_the_axis_after_the_last_comma():
    got = cost.ByCompleter().complete(_ctx([], "service,re"))
    assert [c.value for c in got] == ["service,region", "service,record-type"]


def test_period_is_the_last_n_days_with_today_included():
    assert cost.period(3, datetime.date(2026, 11, 18)) == {
        "Start": "2026-11-16", "End": "2026-11-19"}
