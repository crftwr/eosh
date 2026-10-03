"""Tests for the make recipe — target, variable, and value-side path completion."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from pitash.completion import CompletionContext
from pitash.recipes.make import (
    MakeTargetCompleter,
    _looks_like_path,
    _parse_targets,
    _parse_variables,
)


SAMPLE_MAKEFILE = textwrap.dedent("""\
    REGION = us-west-2
    SERVICE := sagemaker-ami-versioning
    ENDPOINT ?= https://example.invalid
    AWS = aws --region $(REGION) $(SERVICE) --endpoint $(ENDPOINT)

    install-service-model:
    \taws configure add-model --service-name $(SERVICE)

    describe-cluster:
    \t$(AWS) describe-cluster --cluster-name $(CLUSTER)

    create-cluster:
    \t$(AWS) create-cluster --cli-input-json file://./$(CONFIG)

    update-cluster-software-all:
    \tpython3 ./run.py \\
    \t\t--cluster $(CLUSTER) \\
    \t\t--image-release-version $(IMAGE_RELEASE_VERSION)
    """)


def _ctx(prefix: str, args=None) -> CompletionContext:
    return CompletionContext(
        command="make",
        args=args or [],
        arg_index=len(args) if args else 0,
        prefix=prefix,
        line=f"make {prefix}",
        shell_context=None,
    )


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def test_parse_targets_lists_recipe_targets():
    assert _parse_targets(SAMPLE_MAKEFILE) == [
        "create-cluster",
        "describe-cluster",
        "install-service-model",
        "update-cluster-software-all",
    ]


def test_parse_targets_skips_assignments():
    # `:=` and `?=` should not be picked up as targets.
    assert "REGION" not in _parse_targets(SAMPLE_MAKEFILE)
    assert "SERVICE" not in _parse_targets(SAMPLE_MAKEFILE)
    assert "ENDPOINT" not in _parse_targets(SAMPLE_MAKEFILE)


def test_parse_variables_unions_assignments_and_references():
    vars_ = _parse_variables(SAMPLE_MAKEFILE)
    # Top-level assignments.
    assert "REGION" in vars_
    assert "SERVICE" in vars_
    assert "ENDPOINT" in vars_
    assert "AWS" in vars_
    # Recipe-only references — the load-bearing case.
    assert "CLUSTER" in vars_
    assert "CONFIG" in vars_
    assert "IMAGE_RELEASE_VERSION" in vars_


def test_parse_variables_filters_make_auto_vars():
    src = "$(MAKE) something\nfoo:\n\t$(SHELL) -c '$(CURDIR)/run'\n"
    assert _parse_variables(src) == []


def test_parse_variables_handles_brace_form():
    src = "${FOO} ${BAR_QUUX}\nx:\n\techo ${BAZ}\n"
    assert _parse_variables(src) == ["BAR_QUUX", "BAZ", "FOO"]


# ---------------------------------------------------------------------------
# _looks_like_path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prefix", ["./", "../", "/", "/abs/path", "~/foo", "~user/foo"])
def test_looks_like_path_explicit_roots(prefix):
    assert _looks_like_path(prefix) is True


@pytest.mark.parametrize("prefix", ["", "foo", "foobar", "CONFIG"])
def test_looks_like_path_bare_words_skip(prefix):
    assert _looks_like_path(prefix) is False


def test_looks_like_path_existing_subdir(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("")
    monkeypatch.chdir(tmp_path)
    assert _looks_like_path("src/") is True
    assert _looks_like_path("src/main") is True


def test_looks_like_path_nonexistent_subdir_skip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Slash present but the directory portion doesn't exist — don't trigger.
    assert _looks_like_path("nonexistent/") is False
    assert _looks_like_path("nonexistent/foo") is False


# ---------------------------------------------------------------------------
# completer integration
# ---------------------------------------------------------------------------

def test_completer_lists_targets_and_vars(tmp_path, monkeypatch):
    (tmp_path / "Makefile").write_text(SAMPLE_MAKEFILE)
    monkeypatch.chdir(tmp_path)

    completions = MakeTargetCompleter().complete(_ctx(""))
    values = {c.value for c in completions}

    # Targets present.
    assert "describe-cluster" in values
    assert "create-cluster" in values
    # Variables present, with trailing '='.
    assert "CONFIG=" in values
    assert "IMAGE_RELEASE_VERSION=" in values
    assert "REGION=" in values


def test_completer_filters_by_prefix(tmp_path, monkeypatch):
    (tmp_path / "Makefile").write_text(SAMPLE_MAKEFILE)
    monkeypatch.chdir(tmp_path)

    completions = MakeTargetCompleter().complete(_ctx("CON"))
    values = {c.value for c in completions}

    assert "CONFIG=" in values
    # Targets with a different prefix excluded.
    assert "describe-cluster" not in values


def test_completer_var_completions_have_variable_description(tmp_path, monkeypatch):
    (tmp_path / "Makefile").write_text(SAMPLE_MAKEFILE)
    monkeypatch.chdir(tmp_path)

    completions = MakeTargetCompleter().complete(_ctx("CONFIG"))
    config_completion = next(c for c in completions if c.value == "CONFIG=")
    assert config_completion.description == "variable"


def test_completer_value_side_path_completion(tmp_path, monkeypatch):
    (tmp_path / "Makefile").write_text(SAMPLE_MAKEFILE)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cluster-1.json").write_text("{}")
    (tmp_path / "src" / "cluster-2.json").write_text("{}")
    monkeypatch.chdir(tmp_path)

    completions = MakeTargetCompleter().complete(_ctx("CONFIG=src/"))
    values = {c.value for c in completions}

    assert "CONFIG=src/cluster-1.json" in values
    assert "CONFIG=src/cluster-2.json" in values


def test_completer_value_side_explicit_dot_slash(tmp_path, monkeypatch):
    (tmp_path / "Makefile").write_text(SAMPLE_MAKEFILE)
    (tmp_path / "cluster-1.json").write_text("{}")
    monkeypatch.chdir(tmp_path)

    completions = MakeTargetCompleter().complete(_ctx("CONFIG=./"))
    values = {c.value for c in completions}

    assert "CONFIG=./cluster-1.json" in values


def test_completer_value_side_bare_word_no_completion(tmp_path, monkeypatch):
    (tmp_path / "Makefile").write_text(SAMPLE_MAKEFILE)
    (tmp_path / "cluster-1.json").write_text("{}")
    monkeypatch.chdir(tmp_path)

    # 'cluster' has no '/' and isn't an explicit root — we don't guess.
    assert MakeTargetCompleter().complete(_ctx("CONFIG=cluster")) == []
    # Empty value side: also no completion.
    assert MakeTargetCompleter().complete(_ctx("CONFIG=")) == []


def test_completer_respects_dash_c_for_makefile_lookup(tmp_path, monkeypatch):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "Makefile").write_text("PROJECT = thing\nbuild:\n\techo hi\n")
    monkeypatch.chdir(tmp_path)

    completions = MakeTargetCompleter().complete(
        _ctx("", args=["-C", str(sub)])
    )
    values = {c.value for c in completions}

    assert "build" in values
    assert "PROJECT=" in values


def test_completer_respects_dash_f_for_makefile_lookup(tmp_path, monkeypatch):
    custom = tmp_path / "custom.mk"
    custom.write_text("DEPLOY_ENV = prod\ndeploy:\n\techo hi\n")
    monkeypatch.chdir(tmp_path)

    completions = MakeTargetCompleter().complete(
        _ctx("", args=["-f", str(custom)])
    )
    values = {c.value for c in completions}

    assert "deploy" in values
    assert "DEPLOY_ENV=" in values


def test_completer_no_makefile_returns_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert MakeTargetCompleter().complete(_ctx("any")) == []
