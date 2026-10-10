"""One way to run a Python handler and read its exit status (discussion #38).

``run_handler`` is what every execution path — foreground slot, pipeline
stage, decorator, main-thread run — goes through, so these pin the
contract once.
"""

from __future__ import annotations

import pytest

from eosh.commands import CommandRegistry, arg
from eosh.pipeline import parse_line
from eosh.shell import Shell
from eosh.slots import run_handler


def test_a_returned_int_is_the_exit_status():
    assert run_handler(lambda: 3, "t") == 3
    assert run_handler(lambda: 0, "t") == 0


@pytest.mark.parametrize("value", [None, "text", [1], True, False])
def test_anything_else_returned_is_success(value):
    assert run_handler(lambda: value, "t") == 0


def test_system_exit_is_a_status_not_a_shell_exit(capsys):
    def boom(code):
        raise SystemExit(code)
    assert run_handler(lambda: boom(4), "t") == 4
    assert run_handler(lambda: boom(None), "t") == 0
    assert run_handler(lambda: boom("bad input"), "t") == 1
    assert "bad input" in capsys.readouterr().err


def test_interrupt_and_broken_pipe(capsys):
    def interrupt():
        raise KeyboardInterrupt
    def broken():
        raise BrokenPipeError
    assert run_handler(interrupt, "t") == 130
    assert capsys.readouterr().out == ""
    assert run_handler(interrupt, "t", announce_interrupt=True) == 130
    assert "t: interrupted" in capsys.readouterr().out
    assert run_handler(broken, "t") == 0


def test_an_error_is_reported_unless_the_stage_was_torn_down(capsys):
    def fail():
        raise ValueError("nope")
    assert run_handler(fail, "mycmd") == 1
    err = capsys.readouterr().err
    assert "mycmd: error: nope" in err and "Traceback" in err
    assert run_handler(fail, "mycmd", interrupted=lambda: True) == 130
    assert capsys.readouterr().err == ""


def test_invoke_returns_the_handler_result_and_2_on_a_usage_error(capsys):
    reg = CommandRegistry()

    @reg.command("check", params=[arg("n", type=int)])
    def check(n):
        return n

    assert reg.get("check").invoke(["7"]) == 7
    assert reg.get("check").invoke(["x"]) == 2
    assert "invalid int value" in capsys.readouterr().err


@pytest.mark.requires_real_stdio
def test_the_status_drives_and_or(tmp_path):
    reg_names = []
    from eosh.commands import registry

    @registry.command("_t_status")
    def _t_status(code):
        return int(code)
    reg_names.append("_t_status")
    try:
        sh = Shell()
        out = tmp_path / "out"
        line = f"_t_status 3 > {out} || echo fallback > {out}.2"
        sh._execute(line)
        assert (tmp_path / "out.2").read_text() == "fallback\n"
        seq = parse_line(f"_t_status 5 > {out}")
        assert sh._execute_pipeline(seq.items[0][1]) == 5
    finally:
        for name in reg_names:
            registry._commands.pop(name, None)


def test_exit_requests_the_shell_end_instead_of_raising():
    sh = Shell()
    assert sh.registry.get("exit").invoke([]) is None
    assert sh._exit_requested is True


def test_exit_in_a_pipeline_stage_is_a_subshell_exit():
    from eosh.slots import _in_pipeline

    sh = Shell()
    _in_pipeline.flag = True
    try:
        sh.registry.get("exit").invoke([])
    finally:
        _in_pipeline.flag = False
    assert sh._exit_requested is False
