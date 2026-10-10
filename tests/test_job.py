"""Tests for ``PipelineSlot`` — a pipeline on one PTY, with its external
stages started by a job leader that owns the PTY as controlling terminal
(discussion #76)."""

import os
import sys
import time

import pytest

pytest.importorskip("pty")

from eosh.commands import registry as command_registry
from eosh.job import PipelineSlot
from eosh.shell import Shell


def _env():
    return dict(os.environ)


def _finish(slot, timeout=10.0):
    deadline = time.monotonic() + timeout
    while slot.is_alive():
        assert time.monotonic() < deadline, "slot never finished"
        time.sleep(0.01)
    return slot.exit_code


def _output(slot) -> str:
    return slot.buffer.peek().decode(errors="replace").replace("\r\n", "\n")


def _run(*argvs):
    """*argvs* as a pipeline on a fresh slot; the finished slot."""
    slot = PipelineSlot("test")
    procs, prev_r = [], None
    for i, argv in enumerate(argvs):
        last = i == len(argvs) - 1
        r, w = (None, None) if last else os.pipe()
        procs.append(slot.spawn(argv, stdin=prev_r, stdout=w, stderr=None,
                                env=_env(), cwd=os.getcwd()))
        for fd in (prev_r, w):
            if fd is not None:
                os.close(fd)
        prev_r = r
    slot.start(procs)
    _finish(slot)
    return slot


# ── the slot ────────────────────────────────────────────────────────────────

def test_output_goes_to_the_pty():
    slot = _run(["echo", "hello"])
    assert slot.exit_code == 0
    assert "hello" in _output(slot)


def test_status_is_the_last_stages():
    assert _run(["false"], ["true"]).exit_code == 0
    assert _run(["true"], ["false"]).exit_code == 1


def test_stages_are_connected():
    slot = _run(["echo", "piped"], ["tr", "a-z", "A-Z"])
    assert "PIPED" in _output(slot)


def test_a_stage_has_the_pty_as_its_controlling_terminal():
    # /dev/tty is what `| less`, `| sudo`, `| fzf` open for keys.
    slot = _run(["sh", "-c", "test -t 1 && echo via-dev-tty > /dev/tty"])
    assert slot.exit_code == 0
    assert "via-dev-tty" in _output(slot)


def test_keys_reach_a_stage_reading_the_terminal():
    slot = PipelineSlot("test")
    proc = slot.spawn(["cat"], stdin=None, stdout=None, stderr=None,
                      env=_env(), cwd=os.getcwd())
    slot.start([proc])
    slot.write_stdin(b"typed-in\n\x04")
    assert _finish(slot) == 0
    assert _output(slot).count("typed-in") == 2      # the echo, and cat's copy


def test_a_missing_command_raises_like_popen():
    slot = PipelineSlot("test")
    with pytest.raises(FileNotFoundError):
        slot.spawn(["no-such-command-eosh"], stdin=None, stdout=None, stderr=None,
                   env=_env(), cwd=os.getcwd())
    slot.discard()
    _finish(slot)


def test_keys_nobody_read_come_back_for_the_prompt():
    slot = PipelineSlot("test")
    proc = slot.spawn(["sleep", "0.3"], stdin=None, stdout=None, stderr=None,
                      env=_env(), cwd=os.getcwd())
    slot.start([proc])
    time.sleep(0.1)
    slot.write_stdin(b"ls -l\r")           # typed while sleep ran
    _finish(slot)
    assert slot.take_unread() == b"ls -l\n"
    assert slot.take_unread() == b""


def test_kill_stops_the_stages():
    slot = PipelineSlot("test")
    proc = slot.spawn(["sleep", "30"], stdin=None, stdout=None, stderr=None,
                      env=_env(), cwd=os.getcwd())
    slot.start([proc])
    time.sleep(0.1)
    slot.kill()
    assert _finish(slot, timeout=5) != 0


def test_ctrl_c_on_the_pty_interrupts_the_stages():
    slot = PipelineSlot("test")
    proc = slot.spawn(["sleep", "30"], stdin=None, stdout=None, stderr=None,
                      env=_env(), cwd=os.getcwd())
    slot.start([proc])
    time.sleep(0.2)
    assert slot.ctrl_c_interrupts()
    slot.write_stdin(b"\x03")              # the line discipline sends SIGINT
    assert _finish(slot, timeout=5) == 130


# ── the shell runs a line's pipeline on one ────────────────────────────────

@pytest.fixture
def sh(monkeypatch):
    """A Shell that runs top-level pipelines on a PipelineSlot, as on a
    terminal, with the forwarding loop replaced by waiting for the slot."""
    monkeypatch.setattr("eosh.shell._stdin_is_tty", lambda: True)
    shell = Shell()
    slots = []

    def forward(slot, force_redraw=False):
        slots.append(slot)
        _finish(slot)
        return "exited"

    monkeypatch.setattr(shell, "_forward", forward)
    shell.slots = slots
    yield shell
    command_registry.clear_user_commands()


def test_a_pipeline_runs_on_a_slot(sh):
    assert sh._execute("echo hi | tr a-z A-Z") == 0
    (slot,) = sh.slots
    assert isinstance(slot, PipelineSlot)
    assert "HI" in _output(slot)


def test_a_redirected_command_runs_on_a_slot(sh, tmp_path):
    out = tmp_path / "out.txt"
    assert sh._execute(f"echo filed > {out}") == 0
    assert len(sh.slots) == 1
    assert out.read_text() == "filed\n"


@pytest.mark.requires_real_stdio
def test_a_python_stage_writes_to_the_slot(sh):
    @command_registry.command("eosh-t-say")
    def say():
        print("from python")
        print("to stderr", file=sys.stderr)

    assert sh._execute("eosh-t-say | cat") == 0
    out = _output(sh.slots[0])
    assert "from python" in out
    assert "to stderr" in out


@pytest.mark.requires_real_stdio
def test_a_decorator_body_joins_the_slot(sh):
    # @time's body runs on the decorator's stage thread: its `echo` goes
    # through the same job leader, its timing line to the same PTY.
    assert sh._execute("@time {echo body} | cat") == 0
    out = _output(sh.slots[0])
    assert "body" in out
    assert "real" in out


def test_a_lone_command_runs_on_a_slot_too(sh):
    assert sh._execute("echo alone") == 0
    (slot,) = sh.slots
    assert "alone" in _output(slot)


def test_a_lone_command_not_found_goes_to_the_hooks(sh, capsys):
    assert sh._execute("no-such-command-eosh x") == 127
    assert "command not found: no-such-command-eosh" in capsys.readouterr().out
    assert sh.slots == []


def test_a_lone_python_command_is_not_on_a_pipeline_slot(sh):
    assert sh._execute("var EOSH_T_LONE=1") == 0
    assert not any(isinstance(s, PipelineSlot) for s in sh.slots)
    os.environ.pop("EOSH_T_LONE", None)


# ── decorators on the slot ──────────────────────────────────────────────────

@pytest.mark.requires_real_stdio
def test_a_lone_decorator_runs_on_a_slot(sh):
    assert sh._execute("@time echo deco") == 0
    (slot,) = sh.slots
    assert slot.argv == ["@time {echo deco}"]
    out = _output(slot)
    assert "deco" in out and "real" in out


@pytest.mark.requires_real_stdio
def test_a_lone_decorators_body_has_the_terminal(sh):
    assert sh._execute("@time sh -c 'test -t 0 && test -t 1 && echo both-tty'") == 0
    assert "both-tty" in _output(sh.slots[0])


def test_ctrl_c_raises_keyboard_interrupt_in_a_decorator_stage():
    from eosh.slots import _PyStageHandle

    slot = PipelineSlot("test")
    calls = []

    class _Deco(_PyStageHandle):
        def raise_keyboard_interrupt(self):
            calls.append("deco")

    class _Plain(_PyStageHandle):
        def interrupt(self):
            calls.append("plain")

    slot._workers = [_Deco("@time", decorator=True), _Plain("cmd")]
    slot.interrupt_python_stages()
    assert calls == ["deco", "plain"]
    slot.discard()
    _finish(slot)
