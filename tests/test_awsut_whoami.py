"""Tests for `awsut whoami` — the blocks it assembles and the failures it survives."""

from __future__ import annotations

import datetime

import botocore.exceptions
import pytest

from cshell2.commands import registry as command_registry
from cshell2.recipes import awsut, enable


UTC = datetime.timezone.utc


# ---------------------------------------------------------------------------
# parse_identity_arn / identity_pairs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("arn,expected", [
    ("arn:aws:sts::123456789012:assumed-role/Admin/alice",
     ("assumed role", "Admin", "alice")),
    ("arn:aws:iam::123456789012:user/alice", ("IAM user", "alice", "")),
    ("arn:aws:iam::123456789012:root", ("account root", "", "")),
    ("arn:aws:sts::123456789012:federated-user/alice",
     ("federated user", "alice", "")),
])
def test_parses_the_principal_out_of_a_caller_arn(arn, expected):
    assert awsut.parse_identity_arn(arn) == expected


def test_unknown_arn_shapes_are_reported_verbatim_not_guessed_at():
    kind, name, session = awsut.parse_identity_arn(
        "arn:aws:iam::123456789012:some-future-thing/x")
    assert (kind, name) == ("some-future-thing", "x")


def test_a_malformed_arn_contributes_nothing_rather_than_raising():
    assert awsut.parse_identity_arn("not-an-arn") == ("", "", "")
    assert awsut.parse_identity_arn("") == ("", "", "")


def test_identity_pairs_carry_both_the_api_fields_and_their_meaning():
    pairs = dict(awsut.identity_pairs({
        "Account": "123456789012",
        "UserId": "AROAEXAMPLE:alice",
        "Arn": "arn:aws:sts::123456789012:assumed-role/Admin/alice",
    }))
    assert pairs["Account"] == "123456789012"
    assert pairs["UserId"] == "AROAEXAMPLE:alice"
    assert pairs["Type"] == "assumed role"
    assert pairs["Name"] == "Admin"
    assert pairs["Session"] == "alice"


# ---------------------------------------------------------------------------
# expiry_label
# ---------------------------------------------------------------------------

def test_no_expiry_reads_as_nothing_so_the_line_drops_out():
    assert awsut.expiry_label(None) == ""


def test_expiry_shows_the_time_left():
    now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    label = awsut.expiry_label(now + datetime.timedelta(hours=2), now)
    assert "2h00m left" in label


def test_a_past_expiry_says_expired():
    now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert "expired" in awsut.expiry_label(now - datetime.timedelta(minutes=1), now)


def test_a_naive_expiry_is_read_as_utc_not_local():
    now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    naive = datetime.datetime(2026, 1, 1, 13, 0)
    assert "1h00m left" in awsut.expiry_label(naive, now)


def test_an_expiry_about_to_land_is_coloured_only_when_asked():
    now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    soon = now + datetime.timedelta(minutes=5)
    assert awsut.YELLOW in awsut.expiry_label(soon, now, colorize=True)
    assert awsut.YELLOW not in awsut.expiry_label(soon, now)
    assert awsut.RED in awsut.expiry_label(
        now - datetime.timedelta(minutes=5), now, colorize=True)


# ---------------------------------------------------------------------------
# credential_pairs
# ---------------------------------------------------------------------------

class _FakeFrozen:
    access_key = "ASIAIOSFODNN7EXAMPLE"


class _FakeCredentials:
    method = "custom-process"

    def __init__(self, expiry=None, raises=False):
        self._expiry_time = expiry
        self._raises = raises

    def get_frozen_credentials(self):
        if self._raises:
            raise botocore.exceptions.CredentialRetrievalError(
                provider="custom-process", error_msg="helper failed")
        return _FakeFrozen()


def test_credentials_report_their_provider_and_a_masked_key():
    pairs = dict(awsut.credential_pairs(_FakeCredentials()))
    assert pairs["Credentials"] == "custom-process"
    assert pairs["Access key"] == "ASIA********MPLE"
    assert "IOSFODNN7EXA" not in pairs["Access key"]


def test_a_provider_that_cannot_hand_over_a_key_still_reports_the_provider():
    pairs = dict(awsut.credential_pairs(_FakeCredentials(raises=True)))
    assert pairs["Credentials"] == "custom-process"
    assert "Access key" not in pairs


def test_no_credentials_says_so_instead_of_pretending():
    pairs = dict(awsut.credential_pairs(None))
    assert "none found" in pairs["Credentials"]


def test_credential_expiry_is_read_out_of_the_botocore_attribute():
    now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    creds = _FakeCredentials(expiry=now + datetime.timedelta(minutes=90))
    assert "1h30m left" in dict(awsut.credential_pairs(creds, now))["Expires"]


# ---------------------------------------------------------------------------
# profile / region / environment blocks
# ---------------------------------------------------------------------------

def test_the_profile_block_names_the_profile_and_its_configured_account(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "team-admin")
    monkeypatch.setattr(awsut, "_get_all_profiles",
                        lambda: {"team-admin": {"account": "123456789012",
                                                "role": "Admin"}})
    pairs = dict(awsut.profile_pairs())
    assert pairs["Profile"].startswith("team-admin")
    assert pairs["Profile account"] == "123456789012"
    assert pairs["Profile role"] == "Admin"


def test_an_unset_profile_says_default_and_why(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setattr(awsut, "_get_all_profiles", lambda: {})
    assert dict(awsut.profile_pairs())["Profile"] == "default  (AWS_PROFILE not set)"


def test_the_region_block_names_the_region_its_label_and_its_source(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    value = dict(awsut.region_pairs())["Region"]
    assert "us-west-2" in value
    assert "US West (Oregon)" in value
    assert "AWS_REGION" in value


def test_a_region_from_the_profile_is_not_attributed_to_the_environment(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.setattr(awsut, "_get_region", lambda: "eu-west-1")
    assert "profile config" in dict(awsut.region_pairs())["Region"]


def test_no_region_points_at_the_var_that_sets_one(monkeypatch):
    monkeypatch.setattr(awsut, "_get_region", lambda: None)
    assert "var aws_region" in dict(awsut.region_pairs())["Region"]


def test_the_environment_block_lists_aws_vars_with_secrets_masked(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "FwoGZXIvYXdzEExampleToken")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ASIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv("NOT_AWS_RELATED", "visible")
    pairs = dict(awsut.aws_env_pairs())
    assert pairs["AWS_REGION"] == "us-west-2"
    assert pairs["AWS_SESSION_TOKEN"] == "FwoG********oken"
    assert pairs["AWS_ACCESS_KEY_ID"] == "ASIA********MPLE"
    assert "NOT_AWS_RELATED" not in pairs


# ---------------------------------------------------------------------------
# endpoint overrides
# ---------------------------------------------------------------------------

def test_only_endpoint_vars_that_differ_from_their_default_are_reported(monkeypatch):
    enable("awsut")
    monkeypatch.setattr(awsut, "sagemaker_endpoint", "")
    monkeypatch.setattr(awsut, "sagemaker_service_name", "sagemaker")
    assert awsut.endpoint_override_pairs() == []

    monkeypatch.setattr(awsut, "sagemaker_endpoint", "https://private.example")
    monkeypatch.setattr(awsut, "sagemaker_service_name", "sagemaker-private")
    assert dict(awsut.endpoint_override_pairs()) == {
        "sagemaker_endpoint": "https://private.example",
        "sagemaker_service_name": "sagemaker-private",
    }


# ---------------------------------------------------------------------------
# the command itself
# ---------------------------------------------------------------------------

class _FakeSession:
    """A boto3 session stand-in whose clients answer the two whoami calls."""

    def __init__(self, identity=None, error=None, aliases=("my-team",)):
        self._identity = identity or {
            "Account": "123456789012",
            "UserId": "AROAEXAMPLE:alice",
            "Arn": "arn:aws:sts::123456789012:assumed-role/Admin/alice",
        }
        self._error = error
        self._aliases = list(aliases)

    def client(self, service_name, **kwargs):
        session = self

        class _Client:
            def get_caller_identity(self):
                if session._error:
                    raise session._error
                return session._identity

            def list_account_aliases(self):
                return {"AccountAliases": session._aliases}

        return _Client()

    def get_credentials(self):
        return _FakeCredentials()


@pytest.fixture
def whoami(monkeypatch):
    enable("awsut")
    monkeypatch.setattr(awsut, "_get_all_profiles", lambda: {})
    return command_registry.get("awsut").children["whoami"]


def test_whoami_prints_identity_settings_and_credentials(whoami, monkeypatch, capsys):
    monkeypatch.setattr(awsut, "_aws_session", lambda: (_FakeSession(), ""))
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    whoami.invoke([])
    out = capsys.readouterr().out
    assert "123456789012" in out
    assert "assumed role" in out
    assert "my-team" in out                  # account alias
    assert "custom-process" in out           # credential provider
    assert "us-west-2" in out
    assert "--- environment ---" in out


def test_no_alias_skips_the_iam_call(whoami, monkeypatch, capsys):
    session = _FakeSession(aliases=("my-team",))
    calls = []
    monkeypatch.setattr(awsut, "_aws_session", lambda: (session, ""))
    monkeypatch.setattr(awsut, "_account_alias",
                        lambda s: calls.append(s) or "my-team")
    whoami.invoke(["--no-alias"])
    assert calls == []
    assert "my-team" not in capsys.readouterr().out


def test_raw_prints_the_api_response(whoami, monkeypatch, capsys):
    monkeypatch.setattr(awsut, "_aws_session", lambda: (_FakeSession(), ""))
    whoami.invoke(["--raw"])
    out = capsys.readouterr().out
    assert '"Account": "123456789012"' in out
    assert "--- environment ---" not in out


def test_an_unknown_profile_still_prints_the_settings_that_explain_it(
        whoami, monkeypatch, capsys):
    monkeypatch.setattr(awsut, "_aws_session",
                        lambda: (None, "The config profile (nope) could not be found"))
    monkeypatch.setenv("AWS_PROFILE", "nope")
    whoami.invoke([])
    out = capsys.readouterr().out
    assert "unavailable" in out
    assert "could not be found" in out
    assert "nope" in out                     # the profile block still printed
    assert "none found" in out               # and the credential line


def test_a_denied_identity_call_is_reported_as_one_line(whoami, monkeypatch, capsys):
    error = botocore.exceptions.ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "not authorized"}},
        "GetCallerIdentity")
    monkeypatch.setattr(awsut, "_aws_session",
                        lambda: (_FakeSession(error=error), ""))
    whoami.invoke([])
    out = capsys.readouterr().out
    assert "not authorized" in out
    assert "Traceback" not in out


def test_an_unreadable_alias_does_not_sink_the_identity(whoami, monkeypatch, capsys):
    class _NoAliasSession(_FakeSession):
        def client(self, service_name, **kwargs):
            if service_name == "iam":
                raise botocore.exceptions.ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "nope"}},
                    "ListAccountAliases")
            return super().client(service_name, **kwargs)

    monkeypatch.setattr(awsut, "_aws_session", lambda: (_NoAliasSession(), ""))
    whoami.invoke([])
    assert "123456789012" in capsys.readouterr().out
