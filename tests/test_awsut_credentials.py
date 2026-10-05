"""Tests for `awsut credentials set` — parsing a pasted block and writing it."""

from __future__ import annotations

import stat
import textwrap

import pytest

from eosh.recipes.awsut import (
    parse_credential_exports,
    write_credentials_profile,
)


# ---------------------------------------------------------------------------
# parse_credential_exports
# ---------------------------------------------------------------------------

def test_parses_the_console_paste():
    values, extras, unknown = parse_credential_exports(textwrap.dedent("""\
        export AWS_ACCESS_KEY_ID="AKIAIOSFODNN7EXAMPLE"
        export AWS_SECRET_ACCESS_KEY="wJalrXUtnFEMI/K7MDENG"
        export AWS_SESSION_TOKEN="FwoGZXIvYXdzEA=="
    """))
    assert values == {
        "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
        "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG",
        "aws_session_token": "FwoGZXIvYXdzEA==",
    }
    assert extras == {} and unknown == []


@pytest.mark.parametrize("line", [
    'export AWS_ACCESS_KEY_ID="AKIA1"',
    "export AWS_ACCESS_KEY_ID='AKIA1'",
    "export AWS_ACCESS_KEY_ID=AKIA1",
    "AWS_ACCESS_KEY_ID=AKIA1",
    "set AWS_ACCESS_KEY_ID=AKIA1",
    '$env:AWS_ACCESS_KEY_ID="AKIA1"',
    'export AWS_ACCESS_KEY_ID="AKIA1";',
    "  aws_access_key_id = AKIA1",
])
def test_accepts_every_assignment_shape(line):
    values, _, _ = parse_credential_exports(line)
    assert values == {"aws_access_key_id": "AKIA1"}


def test_legacy_and_region_aliases_fold_onto_one_key():
    values, _, _ = parse_credential_exports(
        "export AWS_SECURITY_TOKEN=tok\nexport AWS_DEFAULT_REGION=us-west-2\n"
    )
    assert values == {"aws_session_token": "tok", "region": "us-west-2"}


def test_expiry_is_reported_but_not_written():
    values, extras, _ = parse_credential_exports(
        "export AWS_CREDENTIAL_EXPIRATION=2026-09-14T12:00:00Z\n"
    )
    assert values == {}
    assert extras == {"AWS_CREDENTIAL_EXPIRATION": "2026-09-14T12:00:00Z"}


def test_comments_headers_blanks_and_empty_values_are_skipped():
    values, extras, unknown = parse_credential_exports(textwrap.dedent("""\
        # paste from the console
        [some-profile]

        export AWS_ACCESS_KEY_ID=
        export PATH=/usr/bin
        not an assignment at all
    """))
    assert values == {} and extras == {}
    assert unknown == ["PATH"]


# ---------------------------------------------------------------------------
# write_credentials_profile
# ---------------------------------------------------------------------------

VALUES = {
    "aws_access_key_id": "AKIA-NEW",
    "aws_secret_access_key": "SECRET-NEW",
    "aws_session_token": "TOKEN-NEW",
}


def test_creates_the_file_when_missing(tmp_path):
    path = tmp_path / "sub" / "credentials"
    assert write_credentials_profile(str(path), "dev", VALUES) is True
    assert path.read_text() == textwrap.dedent("""\
        [dev]
        aws_access_key_id = AKIA-NEW
        aws_secret_access_key = SECRET-NEW
        aws_session_token = TOKEN-NEW
    """)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_updates_in_place_and_keeps_everything_else(tmp_path):
    path = tmp_path / "credentials"
    path.write_text(textwrap.dedent("""\
        # hand-written header

        [default]
        aws_access_key_id = AKIA-DEFAULT
        aws_secret_access_key = SECRET-DEFAULT

        [dev]
        # notes about dev
        aws_access_key_id = AKIA-OLD
        aws_secret_access_key = SECRET-OLD
        region = eu-west-1

        [prod]
        aws_access_key_id = AKIA-PROD
    """))
    assert write_credentials_profile(str(path), "dev", VALUES) is False
    assert path.read_text() == textwrap.dedent("""\
        # hand-written header

        [default]
        aws_access_key_id = AKIA-DEFAULT
        aws_secret_access_key = SECRET-DEFAULT

        [dev]
        # notes about dev
        aws_access_key_id = AKIA-NEW
        aws_secret_access_key = SECRET-NEW
        region = eu-west-1
        aws_session_token = TOKEN-NEW

        [prod]
        aws_access_key_id = AKIA-PROD
    """)


def test_appends_a_new_profile_to_an_existing_file(tmp_path):
    path = tmp_path / "credentials"
    path.write_text("[default]\naws_access_key_id = AKIA-DEFAULT\n")
    assert write_credentials_profile(str(path), "dev", VALUES) is False
    assert path.read_text() == textwrap.dedent("""\
        [default]
        aws_access_key_id = AKIA-DEFAULT

        [dev]
        aws_access_key_id = AKIA-NEW
        aws_secret_access_key = SECRET-NEW
        aws_session_token = TOKEN-NEW
    """)


def test_duplicate_keys_collapse_onto_the_new_value(tmp_path):
    path = tmp_path / "credentials"
    path.write_text(textwrap.dedent("""\
        [dev]
        aws_access_key_id = AKIA-OLD
        AWS_ACCESS_KEY_ID = AKIA-SHADOW
    """))
    write_credentials_profile(str(path), "dev", {"aws_access_key_id": "AKIA-NEW"})
    assert path.read_text() == "[dev]\naws_access_key_id = AKIA-NEW\n"


def test_expanduser_and_no_temp_file_left_behind(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("os.path.expanduser",
                        lambda p: p.replace("~", str(tmp_path), 1))
    write_credentials_profile("~/.aws/credentials", "dev", VALUES)
    aws_dir = tmp_path / ".aws"
    assert (aws_dir / "credentials").exists()
    assert [p.name for p in aws_dir.iterdir()] == ["credentials"]
