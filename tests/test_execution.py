"""Tests for ``execution``: the record of what running code is part of."""

from __future__ import annotations

from eosh import execution
from eosh.commands import registry as command_registry
from eosh.pipeline import parse_line
from eosh.shell import Shell


def test_outside_anything_it_is_empty():
    rec = execution.Execution()
    assert (rec.job, rec.ctx, rec.driver, rec.stage, rec.on_terminal) == (
        None, None, False, False, False)
    assert not rec.piped


def test_entered_changes_it_for_the_block_only():
    before = execution.current()
    with execution.entered(stage=True) as rec:
        assert rec.stage and execution.current().stage
        with execution.entered(on_terminal=True):
            assert execution.current().stage and execution.current().on_terminal
        assert not execution.current().on_terminal
    assert execution.current() == before


def test_piped_is_a_stage_off_the_terminal():
    assert execution.Execution(stage=True).piped
    assert not execution.Execution(stage=True, on_terminal=True).piped
    assert not execution.Execution().piped


def test_a_thread_starts_from_its_parents_record():
    seen = []
    runner = object()
    with execution.entered(runner=runner, driver=True):
        t = execution.thread(lambda: seen.append(execution.current()),
                             name="t", driver=False, stage=True)
    t.start()
    t.join()
    (rec,) = seen
    assert rec.runner is runner          # inherited
    assert rec.stage and not rec.driver  # stated on top
    assert not execution.current().stage  # the parent is untouched


def test_a_plain_thread_starts_from_nothing():
    import threading
    seen = []
    with execution.entered(stage=True):
        t = threading.Thread(target=lambda: seen.append(execution.current()))
        t.start()
        t.join()
    assert seen == [execution.Execution()]


def test_each_shell_runs_its_own_bodies(tmp_path):
    """Two shells in one process: a decorator's body runs on the shell that
    runs the line, not on whichever was constructed last."""
    first = Shell()
    second = Shell()
    ran_on = []
    for sh in (first, second):
        original = sh._run_body
        sh._run_body = (lambda body, sh=sh, original=original, **kw:
                        (ran_on.append(sh), original(body, **kw))[1])

    @command_registry.command("@_t_body")
    def _body(body):
        return body.run()

    try:
        first._execute(f"@_t_body {{true > {tmp_path / 'x'}}}")
        assert ran_on == [first]
    finally:
        command_registry._commands.pop("@_t_body", None)


def test_a_body_outside_any_shell_has_no_runner():
    import pytest
    with execution.entered(runner=None):
        with pytest.raises(RuntimeError, match="no executor"):
            parse_line("true").items[0][1].run()
