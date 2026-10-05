"""Tests for CobraCompleter — the cobra ``__complete`` protocol fallback."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from eosh import completion as completion_mod
from eosh.completion import (
    CobraCompleter,
    Completion,
    CompletionContext,
    _parse_cobra_output,
    disable_cobra_fallback,
    enable_cobra_fallback,
    get_cobra_fallback,
)


def make_ctx(line: str, prefix: str, command: str = "kubectl", args=None):
    return CompletionContext(
        command=command,
        args=args or [],
        arg_index=len(args) if args else 0,
        prefix=prefix,
        line=line,
        shell_context=None,
    )


def _completed(stdout: str, returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


# ---------------------------------------------------------------------------
# Output parser
# ---------------------------------------------------------------------------

def test_parse_strips_directive_byte():
    out = "pod\npods\npoddisruptionbudget\n:4\n"
    assert _parse_cobra_output(out) == [
        ("pod", ""),
        ("pods", ""),
        ("poddisruptionbudget", ""),
    ]


def test_parse_extracts_descriptions():
    out = "pod\tretrieve a list of pods\npods\t(alias)\n:0\n"
    assert _parse_cobra_output(out) == [
        ("pod", "retrieve a list of pods"),
        ("pods", "(alias)"),
    ]


def test_parse_drops_blank_and_trace_lines():
    out = "checkout\n\nclone\nCompletion ended with directive: ShellCompDirectiveNoFileComp\n:4\n"
    assert _parse_cobra_output(out) == [("checkout", ""), ("clone", "")]


def test_parse_empty_output():
    assert _parse_cobra_output("") == []
    assert _parse_cobra_output(":0\n") == []


# ---------------------------------------------------------------------------
# Detection (probe)
# ---------------------------------------------------------------------------

def test_probe_recognizes_cobra_help_via_phrase():
    cc = CobraCompleter()
    help_text = (
        "Cobra command \"__complete\"\n"
        "Generate the autocompletion script for the specified shell.\n"
    )
    with patch("eosh.completion.subprocess.run", return_value=_completed(help_text)):
        assert cc._probe("kubectl") is True


def test_probe_recognizes_via_shellcompdirective():
    cc = CobraCompleter()
    help_text = "Usage: tool __complete\n\nReturns ShellCompDirective bytes.\n"
    with patch("eosh.completion.subprocess.run", return_value=_completed(help_text)):
        assert cc._probe("kubectl") is True


def test_probe_rejects_non_cobra_tool():
    cc = CobraCompleter()
    # A non-cobra tool sees __complete as an unknown subcommand.
    with patch(
        "eosh.completion.subprocess.run",
        return_value=_completed("error: unknown command 'completion-mode'\n", returncode=2),
    ):
        assert cc._probe("ls") is False


def test_probe_rejects_on_timeout():
    cc = CobraCompleter(timeout=0.1)
    with patch(
        "eosh.completion.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="x", timeout=0.1),
    ):
        assert cc._probe("hangs") is False


def test_probe_rejects_on_oserror():
    cc = CobraCompleter()
    with patch("eosh.completion.subprocess.run", side_effect=OSError("boom")):
        assert cc._probe("missing") is False


def test_probe_cached_per_command():
    cc = CobraCompleter()
    help_text = "shell completion ShellCompDirective\n"
    with patch("eosh.completion.subprocess.run", return_value=_completed(help_text)) as run:
        assert cc._is_cobra_command("kubectl") is True
        assert cc._is_cobra_command("kubectl") is True
    # Probe ran exactly once for kubectl.
    assert run.call_count == 1


# ---------------------------------------------------------------------------
# Completion path: invocation
# ---------------------------------------------------------------------------

def test_complete_calls_cmd_with_complete_args():
    cc = CobraCompleter()
    cc._is_cobra["kubectl"] = True  # short-circuit probe
    with patch(
        "eosh.completion.subprocess.run",
        return_value=_completed("pod\nservice\n:4\n"),
    ) as run:
        results = cc.complete(make_ctx("kubectl get po", "po", "kubectl", ["get"]))
    args, kwargs = run.call_args
    assert args[0] == ["kubectl", "__complete", "get", "po"]
    assert kwargs["timeout"] == 1.5
    assert kwargs["text"] is True
    assert [c.value for c in results] == ["pod"]
    assert results[0].description == ""


def test_complete_filters_by_prefix():
    cc = CobraCompleter()
    cc._is_cobra["kubectl"] = True
    with patch(
        "eosh.completion.subprocess.run",
        return_value=_completed("pod\tcore type\nservice\nclusterrole\n:4\n"),
    ):
        results = cc.complete(make_ctx("kubectl get po", "po", "kubectl", ["get"]))
    assert [c.value for c in results] == ["pod"]
    assert results[0].description == "core type"


def test_complete_returns_descriptions():
    cc = CobraCompleter()
    cc._is_cobra["kubectl"] = True
    with patch(
        "eosh.completion.subprocess.run",
        return_value=_completed("pod\tretrieve pods\npods\t(alias)\n:0\n"),
    ):
        results = cc.complete(make_ctx("kubectl get po", "po", "kubectl", ["get"]))
    assert results == [
        Completion(value="pod", description="retrieve pods"),
        Completion(value="pods", description="(alias)"),
    ]


def test_complete_skips_when_not_cobra():
    cc = CobraCompleter()
    cc._is_cobra["ls"] = False
    with patch("eosh.completion.subprocess.run") as run:
        assert cc.complete(make_ctx("ls f", "f", "ls", [])) == []
    run.assert_not_called()


def test_complete_handles_subprocess_failure():
    cc = CobraCompleter()
    cc._is_cobra["kubectl"] = True
    with patch("eosh.completion.subprocess.run", side_effect=OSError("boom")):
        assert cc.complete(make_ctx("kubectl get po", "po", "kubectl", ["get"])) == []


def test_complete_handles_nonzero_exit():
    cc = CobraCompleter()
    cc._is_cobra["kubectl"] = True
    with patch(
        "eosh.completion.subprocess.run",
        return_value=_completed("ignored\n", returncode=1),
    ):
        assert cc.complete(make_ctx("kubectl get po", "po", "kubectl", ["get"])) == []


def test_complete_handles_timeout():
    cc = CobraCompleter(timeout=0.1)
    cc._is_cobra["kubectl"] = True
    with patch(
        "eosh.completion.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="x", timeout=0.1),
    ):
        assert cc.complete(make_ctx("kubectl get po", "po", "kubectl", ["get"])) == []


# ---------------------------------------------------------------------------
# should_activate
# ---------------------------------------------------------------------------

def test_should_activate_skips_unknown_command():
    cc = CobraCompleter()
    with patch("eosh.completion.shutil.which", return_value=None):
        assert cc.should_activate(make_ctx("doesnotexist x", "x", "doesnotexist")) is False


def test_should_activate_skips_when_no_command():
    cc = CobraCompleter()
    ctx = CompletionContext(command=None, args=[], arg_index=0, prefix="", line="", shell_context=None)
    assert cc.should_activate(ctx) is False


def test_should_activate_runs_probe_for_known_command():
    cc = CobraCompleter()
    with patch("eosh.completion.shutil.which", return_value="/usr/local/bin/kubectl"), \
         patch("eosh.completion.subprocess.run", return_value=_completed("ShellCompDirective\n")):
        assert cc.should_activate(make_ctx("kubectl ", "", "kubectl")) is True


# ---------------------------------------------------------------------------
# Caching of completion results
# ---------------------------------------------------------------------------

def test_results_cached_per_line():
    cc = CobraCompleter()
    cc._is_cobra["kubectl"] = True
    with patch(
        "eosh.completion.subprocess.run",
        return_value=_completed("pod\n:0\n"),
    ) as run:
        cc.complete(make_ctx("kubectl get po", "po", "kubectl", ["get"]))
        cc.complete(make_ctx("kubectl get po", "po", "kubectl", ["get"]))
    assert run.call_count == 1


def test_results_recomputed_on_line_change():
    cc = CobraCompleter()
    cc._is_cobra["kubectl"] = True
    with patch(
        "eosh.completion.subprocess.run",
        return_value=_completed("pod\n:0\n"),
    ) as run:
        cc.complete(make_ctx("kubectl get po", "po", "kubectl", ["get"]))
        cc.complete(make_ctx("kubectl get pod", "pod", "kubectl", ["get"]))
    assert run.call_count == 2


# ---------------------------------------------------------------------------
# Module-level enable/disable API
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_state():
    saved = completion_mod._cobra_fallback
    saved_enabled = completion_mod._cobra_enabled
    completion_mod._cobra_fallback = None
    completion_mod._cobra_enabled = True
    try:
        yield
    finally:
        completion_mod._cobra_fallback = saved
        completion_mod._cobra_enabled = saved_enabled


def test_disable_returns_none():
    enable_cobra_fallback()
    disable_cobra_fallback()
    assert get_cobra_fallback() is None


def test_enable_after_disable_restores():
    disable_cobra_fallback()
    assert get_cobra_fallback() is None
    cc = enable_cobra_fallback(timeout=2.0)
    assert cc is not None
    assert cc._timeout == 2.0
    assert get_cobra_fallback() is cc


def test_get_lazy_initialises():
    assert completion_mod._cobra_fallback is None
    cc = get_cobra_fallback()
    assert cc is not None
    # Subsequent calls reuse the singleton.
    assert get_cobra_fallback() is cc
