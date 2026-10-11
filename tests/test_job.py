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


@pytest.mark.requires_real_stdio
def test_a_lone_command_not_found_goes_to_the_hooks(sh):
    assert sh._execute("no-such-command-eosh x") == 127
    assert "command not found: no-such-command-eosh" in _output(sh.slots[0])


def test_a_sync_command_stays_on_the_main_thread(sh):
    assert sh._execute("alias eosh_t_al=ls") == 0
    assert sh.slots == []


# ── decorators on the slot ──────────────────────────────────────────────────

@pytest.mark.requires_real_stdio
def test_a_lone_decorator_runs_on_a_slot(sh):
    assert sh._execute("@time echo deco") == 0
    (slot,) = sh.slots
    assert slot.argv == ["@time echo deco"]          # the line as typed
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

    slot._workers = [_Deco("@time", graceful=True), _Plain("cmd")]
    slot.interrupt_python_stages()
    assert calls == ["deco", "plain"]
    slot.discard()
    _finish(slot)


# ── a Python command on the slot's PTY ─────────────────────────────────────

def _wait_for(slot, text, timeout=5.0):
    deadline = time.monotonic() + timeout
    while text not in _output(slot):
        assert time.monotonic() < deadline, f"never saw {text!r}: {_output(slot)!r}"
        time.sleep(0.01)


@pytest.fixture
def driven(monkeypatch):
    """A Shell whose forwarding loop is a script: ``driven.keys`` is a list
    of ``(wait_for_text, keys)`` sent to the slot in turn."""
    monkeypatch.setattr("eosh.shell._stdin_is_tty", lambda: True)
    shell = Shell()
    shell.keys = []
    shell.slots = []

    def forward(slot, force_redraw=False):
        shell.slots.append(slot)
        for text, keys in shell.keys:
            if text:
                _wait_for(slot, text)
            slot.write_stdin(keys)
            if keys == b"\x03" and slot.ctrl_c_interrupts():
                slot.interrupt_python_stages()       # what _forward does
        _finish(slot)
        return "exited"

    monkeypatch.setattr(shell, "_forward", forward)
    yield shell
    command_registry.clear_user_commands()


@pytest.mark.requires_real_stdio
def test_a_python_command_runs_on_a_slot_and_asks_on_its_pty(driven):
    @command_registry.command("eosh-t-ask", pass_context=True)
    def ask(ctx):
        print(f"hello {ctx.input('name? ')}")

    driven.keys = [("name? ", b"bob\r")]
    assert driven._execute("eosh-t-ask") == 0
    (slot,) = driven.slots
    assert isinstance(slot, PipelineSlot)
    assert "hello bob" in _output(slot)


@pytest.mark.requires_real_stdio
def test_keys_typed_before_the_question_are_not_the_answer(driven):
    @command_registry.command("eosh-t-ask2", pass_context=True)
    def ask(ctx):
        time.sleep(0.2)
        print(f"got {ctx.input('sure? ')}")

    driven.keys = [(None, b"y\r"), ("sure? ", b"n\r")]
    driven._execute("eosh-t-ask2")
    assert "got n" in _output(driven.slots[0])


@pytest.mark.requires_real_stdio
def test_run_interactive_gets_the_pty(driven):
    @command_registry.command("eosh-t-run", pass_context=True)
    def run(ctx):
        return ctx.run_interactive(["sh", "-c", "test -t 0 && test -t 1 && echo on-a-tty"])

    assert driven._execute("eosh-t-run") == 0
    assert "on-a-tty" in _output(driven.slots[0])


@pytest.mark.requires_real_stdio
def test_ctrl_c_interrupts_a_python_command(driven):
    @command_registry.command("eosh-t-sleep")
    def nap():
        print("napping")
        for _ in range(50):
            time.sleep(0.1)

    driven.keys = [("napping", b"\x03")]
    started = time.monotonic()
    assert driven._execute("eosh-t-sleep") == 130
    assert time.monotonic() - started < 3


@pytest.mark.requires_real_stdio
def test_ctrl_c_during_run_interactive_is_the_programs(driven):
    @command_registry.command("eosh-t-wrap", pass_context=True)
    def wrap(ctx):
        status = ctx.run_interactive(["sh", "-c", "echo child-up; sleep 5"])
        print(f"after {status}")

    driven.keys = [("child-up", b"\x03")]
    assert driven._execute("eosh-t-wrap") == 0
    assert "after 130" in _output(driven.slots[0])


# ── one slot per line ───────────────────────────────────────────────────────

@pytest.mark.requires_real_stdio
def test_a_whole_line_runs_on_one_slot(sh):
    assert sh._execute("echo one && echo two; false || echo three") == 0
    (slot,) = sh.slots
    out = _output(slot)
    assert ("one" in out) and ("two" in out) and ("three" in out)


@pytest.mark.requires_real_stdio
def test_ctrl_c_ends_the_line(driven):
    driven.keys = [("started", b"\x03")]
    status = driven._execute("sh -c 'echo started; sleep 5'; echo not-reached")
    assert status == 130
    assert "not-reached" not in _output(driven.slots[0]).replace("echo not-reached", "")


@pytest.mark.requires_real_stdio
def test_a_parked_line_runs_on_in_its_own_context(monkeypatch, tmp_path):
    """Ctrl+] mid-line: the rest runs later, in the background, with the
    cwd, environment and globs of the context the line started in."""
    monkeypatch.setattr("eosh.shell._stdin_is_tty", lambda: True)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("")
    other = tmp_path / "elsewhere"
    other.mkdir()
    sh = Shell()
    slots = []

    def forward(slot, force_redraw=False):
        slots.append(slot)
        _wait_for(slot, "first")
        return "switched"                       # Ctrl+]

    def switch_away():
        sh.context_manager.new("other")
        sh.context_manager.switch("other")
        os.chdir(other)                          # the other context's cwd
        sh.context_manager.set_variable("EOSH_T_WHERE", "other")

    monkeypatch.setattr(sh, "_forward", forward)
    monkeypatch.setattr(sh, "_handle_switch", switch_away)
    monkeypatch.setenv("EOSH_T_WHERE", "line")
    try:
        status = sh._execute(
            "echo first; sleep 0.3 && cd sub && "
            "sh -c 'echo $EOSH_T_WHERE \"$@\"' x *.txt > out.txt")
        assert status is None                     # parked
        _finish(slots[0])
        assert slots[0].exit_code == 0
        assert (tmp_path / "sub" / "out.txt").read_text() == "line a.txt\n"
        assert os.getcwd() == str(other)          # the current context didn't move
        assert sh.context_manager.contexts["default"].cwd == str(tmp_path / "sub")
    finally:
        os.environ.pop("EOSH_T_WHERE", None)
        command_registry.clear_user_commands()


@pytest.mark.requires_real_stdio
def test_a_line_with_a_shell_wide_builtin_goes_pipeline_by_pipeline(sh):
    assert sh._execute("echo before; alias eosh_t_x=ls") == 0
    assert len(sh.slots) == 1                   # echo's own; alias on the main thread
    assert sh.registry.get_alias("eosh_t_x") == "ls"


def test_a_line_of_assignments_stays_on_the_main_thread(sh):
    assert sh._execute("EOSH_T_A=1; EOSH_T_B=2") == 0
    assert sh.slots == []
    assert os.environ.pop("EOSH_T_A") == "1"
    assert os.environ.pop("EOSH_T_B") == "2"
