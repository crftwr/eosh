"""Tests for cobra ``__complete`` completion: the completer and its opt-in list."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from eosh.commands import registry as command_registry
from eosh.completion import (
    CobraCompleter,
    Completion,
    CompletionContext,
    _parse_cobra_output,
)
from eosh.recipes import cobra as cobra_recipe
from eosh.recipes import enable_cobra
from eosh.shell import Shell


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


@pytest.fixture(autouse=True)
def _isolate_commands():
    """Drop commands a test registered (enable_cobra writes the global registry)."""
    before = dict(command_registry._commands)
    yield
    for name in list(command_registry._commands):
        if name not in before:
            del command_registry._commands[name]


# ---------------------------------------------------------------------------
# Output parser
# ---------------------------------------------------------------------------

def test_parse_reads_directive():
    out = "pod\npods\npoddisruptionbudget\n:4\n"
    assert _parse_cobra_output(out) == (
        [("pod", ""), ("pods", ""), ("poddisruptionbudget", "")],
        4,
    )


def test_parse_extracts_descriptions():
    out = "pod\tretrieve a list of pods\npods\t(alias)\n:0\n"
    assert _parse_cobra_output(out) == (
        [("pod", "retrieve a list of pods"), ("pods", "(alias)")],
        0,
    )


def test_parse_drops_blank_and_trace_lines():
    out = "checkout\n\nclone\nCompletion ended with directive: ShellCompDirectiveNoFileComp\n:4\n"
    assert _parse_cobra_output(out) == ([("checkout", ""), ("clone", "")], 4)


def test_parse_empty_output():
    assert _parse_cobra_output("") == ([], 0)
    assert _parse_cobra_output(":0\n") == ([], 0)


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------

def test_complete_calls_cmd_with_complete_args():
    cc = CobraCompleter()
    with patch(
        "eosh.completion.subprocess.run",
        return_value=_completed("pod\nservice\n:4\n"),
    ) as run:
        results = cc.complete(make_ctx("kubectl get po", "po", "kubectl", ["get"]))
    args, kwargs = run.call_args
    assert args[0] == ["kubectl", "__complete", "get", "po"]
    assert kwargs["timeout"] == 1.5
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert [c.value for c in results] == ["pod"]


def test_complete_returns_descriptions():
    cc = CobraCompleter()
    with patch(
        "eosh.completion.subprocess.run",
        return_value=_completed("pod\tretrieve pods\npods\t(alias)\n:4\n"),
    ):
        results = cc.complete(make_ctx("kubectl get po", "po", "kubectl", ["get"]))
    assert results == [
        Completion(value="pod", description="retrieve pods"),
        Completion(value="pods", description="(alias)"),
    ]


@pytest.mark.parametrize("effect", [
    OSError("boom"),
    subprocess.TimeoutExpired(cmd="x", timeout=0.1),
])
def test_complete_handles_subprocess_failure(effect):
    cc = CobraCompleter(timeout=0.1)
    with patch("eosh.completion.subprocess.run", side_effect=effect):
        assert cc.complete(make_ctx("kubectl get po", "po", "kubectl", ["get"])) == []


def test_complete_handles_nonzero_exit():
    cc = CobraCompleter()
    with patch(
        "eosh.completion.subprocess.run",
        return_value=_completed("ignored\n", returncode=1),
    ):
        assert cc.complete(make_ctx("kubectl get po", "po", "kubectl", ["get"])) == []


def test_error_directive_yields_nothing():
    cc = CobraCompleter()
    with patch("eosh.completion.subprocess.run", return_value=_completed("pod\n:1\n")):
        assert cc.complete(make_ctx("kubectl get po", "po", "kubectl", ["get"])) == []


# ---------------------------------------------------------------------------
# Directive → file completion
# ---------------------------------------------------------------------------

@pytest.fixture
def files(tmp_path, monkeypatch):
    (tmp_path / "app.yaml").write_text("")
    (tmp_path / "notes.txt").write_text("")
    (tmp_path / "manifests").mkdir()
    monkeypatch.chdir(tmp_path)


def _values(stdout: str) -> set[str]:
    cc = CobraCompleter()
    with patch("eosh.completion.subprocess.run", return_value=_completed(stdout)):
        return {c.value for c in cc.complete(
            make_ctx("kubectl apply -f ", "", "kubectl", ["apply", "-f"])
        )}


def test_no_candidates_falls_back_to_files(files):
    assert _values(":0\n") == {"app.yaml", "notes.txt", "manifests/"}


def test_no_file_comp_suppresses_file_fallback(files):
    assert _values(":4\n") == set()


def test_filter_file_ext_keeps_matching_files_and_dirs(files):
    assert _values("yaml\nyml\njson\n:8\n") == {"app.yaml", "manifests/"}


def test_filter_dirs_offers_directories_only(files):
    assert _values(":16\n") == {"manifests/"}


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

def test_results_cached_per_words():
    cc = CobraCompleter()
    with patch("eosh.completion.subprocess.run", return_value=_completed("pod\n:4\n")) as run:
        cc.complete(make_ctx("kubectl get po", "po", "kubectl", ["get"]))
        cc.complete(make_ctx("kubectl get po", "po", "kubectl", ["get"]))
    assert run.call_count == 1


def test_results_recomputed_on_prefix_change():
    cc = CobraCompleter()
    with patch("eosh.completion.subprocess.run", return_value=_completed("pod\n:4\n")) as run:
        cc.complete(make_ctx("kubectl get po", "po", "kubectl", ["get"]))
        cc.complete(make_ctx("kubectl get pod", "pod", "kubectl", ["get"]))
    assert run.call_count == 2


def test_cache_dropped_on_invalidate():
    from eosh import completion_cache

    cc = CobraCompleter()
    with patch("eosh.completion.subprocess.run", return_value=_completed("pod\n:4\n")) as run:
        cc.complete(make_ctx("kubectl get po", "po", "kubectl", ["get"]))
        completion_cache.invalidate_all()
        cc.complete(make_ctx("kubectl get po", "po", "kubectl", ["get"]))
    assert run.call_count == 2


# ---------------------------------------------------------------------------
# Opt-in list: enable_cobra / the cobra recipe
# ---------------------------------------------------------------------------

def _on_path(*names):
    return lambda n: f"/usr/bin/{n}" if n in names else None


def test_enable_cobra_installs_delegate():
    with patch("eosh.recipes.cobra.shutil.which", _on_path("mytool")):
        enable_cobra("mytool")
    cmd = command_registry.get("mytool")
    assert cmd is not None
    assert not cmd.has_any_handler()  # completion-only; runs as a system command
    assert isinstance(cmd.delegate, CobraCompleter)


def test_enable_cobra_skips_names_not_on_path():
    with patch("eosh.recipes.cobra.shutil.which", _on_path()):
        enable_cobra("mytool")
    assert not command_registry.has("mytool")


def test_enable_cobra_never_replaces_an_existing_command():
    existing = command_registry.command("mytool", help="hand-written recipe")
    with patch("eosh.recipes.cobra.shutil.which", _on_path("mytool")):
        enable_cobra("mytool")
    assert command_registry.get("mytool") is existing


def test_recipe_registers_listed_tools_found_on_path():
    with patch("eosh.recipes.cobra.shutil.which", _on_path("kubectl", "helm")):
        cobra_recipe.register()
    assert command_registry.has("kubectl")
    assert command_registry.has("helm")
    assert not command_registry.has("gh")


# ---------------------------------------------------------------------------
# Shell dispatch — the #34 regression: nothing unlisted is ever run
# ---------------------------------------------------------------------------

def test_unlisted_command_is_never_run_in_completion_mode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "foo.txt").write_text("")
    shell = Shell()
    with patch("eosh.completion.subprocess.run") as run, \
         patch("eosh.completion.subprocess.Popen") as popen:
        completions, _, _ = shell._get_completions("touch fo")
    run.assert_not_called()
    popen.assert_not_called()
    assert [c.value for c in completions] == ["foo.txt"]


def test_listed_command_completes_through_cobra():
    with patch("eosh.recipes.cobra.shutil.which", _on_path("mytool")):
        enable_cobra("mytool")
    shell = Shell()
    with patch(
        "eosh.completion.subprocess.run",
        return_value=_completed("deploy\tship it\nstatus\n:4\n"),
    ) as run:
        completions, _, _ = shell._get_completions("mytool d")
    assert run.call_args[0][0] == ["mytool", "__complete", "d"]
    assert completions == [Completion(value="deploy", description="ship it")]


def test_listed_command_completes_flags_through_cobra():
    with patch("eosh.recipes.cobra.shutil.which", _on_path("mytool")):
        enable_cobra("mytool")
    shell = Shell()
    with patch(
        "eosh.completion.subprocess.run",
        return_value=_completed("--verbose\tbe loud\n:4\n"),
    ) as run:
        completions, _, _ = shell._get_completions("mytool deploy --v")
    assert run.call_args[0][0] == ["mytool", "__complete", "deploy", "--v"]
    assert [c.value for c in completions] == ["--verbose"]
