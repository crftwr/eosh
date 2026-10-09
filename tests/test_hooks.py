"""Tests for event hooks (``eosh.hooks``) and where the shell fires them."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from eosh import hooks
from eosh.commands import registry as command_registry
from eosh.shell import Shell


@pytest.fixture(autouse=True)
def _no_hooks():
    hooks.clear()
    yield
    hooks.clear()


@pytest.fixture
def events():
    """Register a recorder on every event; returns the list it appends to."""
    seen: list[tuple] = []
    hooks.on_startup(lambda: seen.append(("startup",)))
    hooks.on_exit(lambda: seen.append(("exit",)))
    hooks.on_directory_changed(lambda old, new: seen.append(("dir", old, new)))
    hooks.on_context_switched(lambda old, new: seen.append(("ctx", old, new)))
    hooks.on_command_starting(lambda line: seen.append(("starting", line)))
    hooks.on_command_finished(
        lambda line, status, elapsed: seen.append(("finished", line, status)))
    return seen


@pytest.fixture
def sh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shell = Shell()
    yield shell
    command_registry.clear_user_commands()


# ── The registry ────────────────────────────────────────────────────────────

def test_hooks_run_in_registration_order():
    calls = []

    @hooks.on_command_starting
    def first(line):
        calls.append("first")

    @hooks.on_command_starting
    def second(line):
        calls.append("second")

    hooks.fire("on_command_starting", "ls")
    assert calls == ["first", "second"]
    assert first.__name__ == "first"     # the decorator returns the function


def test_a_failing_hook_is_reported_and_the_next_one_still_runs(capsys):
    calls = []

    @hooks.on_command_starting
    def broken(line):
        raise ValueError("oops")

    hooks.on_command_starting(lambda line: calls.append(line))

    hooks.fire("on_command_starting", "ls")
    assert calls == ["ls"]
    err = capsys.readouterr().err
    assert "on_command_starting hook" in err and "broken" in err
    assert "ValueError: oops" in err


def test_the_first_hook_to_claim_wins():
    asked = []

    @hooks.on_command_not_found
    def passes(argv):
        asked.append("passes")
        return False

    @hooks.on_command_not_found
    def claims(argv):
        asked.append("claims")
        return True

    @hooks.on_command_not_found
    def never(argv):
        asked.append("never")
        return True

    assert hooks.fire_until_claimed("on_command_not_found", ["x"]) is True
    assert asked == ["passes", "claims"]


def test_only_true_claims():
    hooks.on_command_not_found(lambda argv: "yes")
    assert hooks.fire_until_claimed("on_command_not_found", ["x"]) is False


def test_an_unknown_event_is_an_attribute_error():
    with pytest.raises(AttributeError):
        hooks.on_chpwd


def test_decorating_a_non_callable_is_a_type_error():
    with pytest.raises(TypeError):
        hooks.on_exit("not a function")


# ── Where the shell fires them ──────────────────────────────────────────────

def test_a_line_reports_starting_and_finished_with_its_status(sh, events):
    @command_registry.command("_t_fail", sync=True)
    def fail():
        return 3

    sh._execute("_t_fail")
    assert events == [("starting", "_t_fail"), ("finished", "_t_fail", 3)]


def test_cd_reports_the_move_before_the_next_command_runs(sh, events, tmp_path):
    (tmp_path / "proj").mkdir()
    old = os.getcwd()

    @command_registry.command("_t_mark", sync=True)
    def mark():
        events.append(("ran",))

    sh._execute("cd proj && _t_mark")
    new = str((tmp_path / "proj").resolve())
    assert [e for e in events if e[0] in ("dir", "ran")] == [
        ("dir", old, os.path.realpath(new)), ("ran",)]


def test_a_context_switch_reports_the_context_then_its_directory(sh, events, tmp_path):
    sh._execute("context new other")
    (tmp_path / "sub").mkdir()
    sh._execute("cd sub")
    events.clear()
    sh._execute("context switch default")
    assert [e[0] for e in events if e[0] in ("ctx", "dir")] == ["ctx", "dir"]
    assert ("ctx", "other", "default") in events


def test_nothing_is_reported_when_nothing_changed(sh, events):
    sh._execute("var _T_HOOKS=1")
    sh._execute("var _T_HOOKS=")
    assert [e for e in events if e[0] in ("ctx", "dir")] == []


def test_an_unclaimed_missing_command_is_reported(sh, capsys):
    assert sh._command_not_found(["eosh-t-nope", "-x"]) == 127
    assert "command not found: eosh-t-nope" in capsys.readouterr().out


def test_a_claimed_missing_command_succeeds(sh, capsys):
    seen = []

    @hooks.on_command_not_found
    def claim(argv):
        seen.append(argv)
        return True

    assert sh._command_not_found(["eosh-t-nope", "-x"]) == 0
    assert seen == [["eosh-t-nope", "-x"]]
    assert "command not found" not in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY path")
def test_a_missing_external_command_goes_through_the_hook(sh):
    seen = []
    hooks.on_command_not_found(lambda argv: seen.append(argv) or True)
    assert sh._execute_external("eosh-t-no-such-command", ["a"]) == 0
    assert seen == [["eosh-t-no-such-command", "a"]]


def test_a_backgrounded_line_is_reported_when_its_slot_ends(sh, events):
    slot = SimpleNamespace(parked=True, line="make -j8", history_id=None, argv=["make", "-j8"],
                           exit_code=2, elapsed=lambda: 1.5)
    sh._slot_finished(slot)
    assert events == [("finished", "make -j8", 2)]


def test_a_foreground_slot_is_left_to_the_line(sh, events):
    slot = SimpleNamespace(parked=False, line=None, history_id=None, argv=["ls"],
                           exit_code=0, elapsed=lambda: 0.1)
    sh._slot_finished(slot)
    assert events == []


def test_startup_and_exit_bracket_the_session(sh, events, monkeypatch):
    def eof(*a, **k):
        raise EOFError
    monkeypatch.setattr(sh._line_editor, "prompt", eof)
    monkeypatch.setattr(sh, "_install_sigwinch_handler", lambda: None)
    sh.run()
    assert events[0] == ("startup",) and events[-1] == ("exit",)


def test_reload_forgets_every_hook(sh, events):
    sh._clear_user_config()
    sh._execute("var _T_HOOKS=")
    assert events == []
