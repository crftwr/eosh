"""Tests for Python @registry.command handlers participating in pipelines.

These exercise the threaded pipeline path in shell.py: each registered
Python stage is run on a worker thread that rebinds sys.stdin/stdout/stderr
to the pipe ends, while external stages go through subprocess.Popen as
before.
"""

import os
import sys
import tempfile

import pytest

from eosh.commands import registry
from eosh.command_context import CommandContext
from eosh.shell import Shell
from eosh.slots import _in_pipeline


# All tests in this module exercise the in-process pipeline whose worker
# threads route ``print()`` through a thread-local override on
# ``sys.stdout``.  Pytest's per-test stdio capture replaces ``sys.stdout``
# during the call phase, breaking that routing.  The marker (defined in
# tests/conftest.py) keeps capture suspended for the duration of each call.
pytestmark = pytest.mark.requires_real_stdio


@pytest.fixture(autouse=True)
def _cleanup_test_commands():
    """Remove any commands registered during a test from the registry."""
    before = {c.name for c in registry._commands.values()}
    yield
    to_remove = [name for name in list(registry._commands)
                 if name not in before]
    for name in to_remove:
        del registry._commands[name]


@pytest.fixture
def shell():
    """A Shell instance with thread-local stdio installed."""
    return Shell()


def _read_to_file(shell_obj, line: str) -> str:
    """Append ``> tmp`` to *line*, run it, return the file contents."""
    fd, path = tempfile.mkstemp()
    os.close(fd)
    try:
        shell_obj._execute(f"{line} > {path}")
        with open(path) as f:
            return f.read()
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Producer side: Python command emits, external command consumes
# ---------------------------------------------------------------------------

def test_python_producer_to_external(shell):
    @registry.command(name="_t_emit_lines")
    def _t_emit_lines():
        print("apple")
        print("banana")
        print("cherry")

    out = _read_to_file(shell, "_t_emit_lines | grep an")
    assert out == "banana\n"


def test_python_producer_multiline(shell):
    @registry.command(name="_t_emit_many")
    def _t_emit_many():
        for i in range(50):
            print(f"line-{i:02d}")

    out = _read_to_file(shell, "_t_emit_many | grep line-25")
    assert out == "line-25\n"


# ---------------------------------------------------------------------------
# Consumer side: external command produces, Python command consumes
# ---------------------------------------------------------------------------

def test_external_to_python_consumer(shell):
    @registry.command(name="_t_upcase")
    def _t_upcase():
        for line in sys.stdin:
            print(line.rstrip("\n").upper())

    out = _read_to_file(shell, "printf 'foo\\nbar\\n' | _t_upcase")
    assert out == "FOO\nBAR\n"


def test_python_consumer_sees_eof(shell):
    @registry.command(name="_t_count_lines")
    def _t_count_lines():
        n = sum(1 for _ in sys.stdin)
        print(n)

    out = _read_to_file(shell, "printf 'a\\nb\\nc\\n' | _t_count_lines")
    assert out == "3\n"


# ---------------------------------------------------------------------------
# All-Python pipelines
# ---------------------------------------------------------------------------

def test_python_to_python(shell):
    @registry.command(name="_t_p1")
    def _t_p1():
        print("hello")
        print("world")

    @registry.command(name="_t_p2")
    def _t_p2():
        for line in sys.stdin:
            print(f"<{line.rstrip()}>")

    out = _read_to_file(shell, "_t_p1 | _t_p2")
    assert out == "<hello>\n<world>\n"


def test_three_stage_pipeline_with_python_in_middle(shell):
    @registry.command(name="_t_double")
    def _t_double():
        for line in sys.stdin:
            text = line.rstrip("\n")
            print(text + text)

    out = _read_to_file(shell, "printf 'ab\\ncd\\n' | _t_double | grep cdcd")
    assert out == "cdcd\n"


def test_three_stage_all_python(shell):
    @registry.command(name="_t_src")
    def _t_src():
        print("alpha")
        print("beta")
        print("gamma")

    @registry.command(name="_t_filter")
    def _t_filter():
        for line in sys.stdin:
            if "a" in line:
                sys.stdout.write(line)

    @registry.command(name="_t_count")
    def _t_count():
        print(sum(1 for _ in sys.stdin))

    out = _read_to_file(shell, "_t_src | _t_filter | _t_count")
    assert out == "3\n"  # alpha, beta(no), gamma — "a" matches alpha and gamma


def test_three_stage_all_python_actual_filter(shell):
    @registry.command(name="_t_src2")
    def _t_src2():
        print("apple")
        print("orange")
        print("apricot")

    @registry.command(name="_t_filter2")
    def _t_filter2():
        for line in sys.stdin:
            if line.startswith("a"):
                sys.stdout.write(line)

    @registry.command(name="_t_count2")
    def _t_count2():
        print(sum(1 for _ in sys.stdin))

    out = _read_to_file(shell, "_t_src2 | _t_filter2 | _t_count2")
    assert out == "2\n"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

def test_python_command_exception_does_not_kill_shell(shell):
    @registry.command(name="_t_boom")
    def _t_boom():
        raise RuntimeError("kaboom")

    # Should run to completion without raising into the test.
    shell._execute("_t_boom | grep anything")


def test_systemexit_in_python_stage_does_not_kill_shell(shell):
    """`exit | cat` would kill the shell pre-fix; pipeline path absorbs it."""

    @registry.command(name="_t_quitter")
    def _t_quitter():
        sys.exit(7)

    # Must not raise SystemExit out of the pipeline.
    shell._execute("_t_quitter | cat")


def test_talking_to_the_user_refuses_inside_pipeline_thread():
    """ctx.run_interactive / ctx.input / ctx.choose must error out when stdin/stdout
    are wired to pipes."""
    _in_pipeline.flag = True
    try:
        ctx = CommandContext(None, None, None)
        with pytest.raises(RuntimeError, match="ctx.run_interactive"):
            ctx.run_interactive(["true"])
        with pytest.raises(RuntimeError, match="ctx.input"):
            ctx.input("> ")
        with pytest.raises(RuntimeError, match="ctx.choose"):
            ctx.choose(["a"])
    finally:
        _in_pipeline.flag = False


# ---------------------------------------------------------------------------
# Stateful built-ins: each stage of a pipeline is a subshell, as in bash
# (discussion #85) — its changes are discarded.
# ---------------------------------------------------------------------------

def test_var_in_pipeline_does_not_mutate_parent(shell):
    """`var FOO=bar | cat` sets FOO in the stage's subshell only."""
    os.environ.pop("_T_PIPED_VAR", None)
    shell._execute("var _T_PIPED_VAR=hello | cat")
    assert "_T_PIPED_VAR" not in os.environ


# ---------------------------------------------------------------------------
# Thread-local stdio routers — basic isolation
# ---------------------------------------------------------------------------

def test_main_thread_stdout_unaffected_by_pipeline(shell):
    """A Python pipeline stage's print() must not leak to the main thread's
    sys.stdout (the test's own stdout) — it goes to the pipe and only the
    pipe.  Verified by checking the redirected file matches exactly the
    bytes the producer printed, with nothing missing or extra."""
    @registry.command(name="_t_silent_in_main")
    def _t_silent_in_main():
        for _ in range(5):
            print("PIPE_OUTPUT")

    out = _read_to_file(shell, "_t_silent_in_main | cat")
    assert out == "PIPE_OUTPUT\n" * 5


# ---------------------------------------------------------------------------
# Single-stage redirect (``pycmd > file``) — runs as a one-stage pipeline
# ---------------------------------------------------------------------------

def test_redirect_does_not_swap_global_stdout(shell):
    """The redirect is bound on the command's own thread only.

    Before, ``pycmd > file`` assigned the process-global ``sys.stdout``, so
    any other thread printing meanwhile (a backgrounded command) wrote into
    the redirect target.
    """
    import threading

    seen = {}

    @registry.command(name="_t_redir_global")
    def _t_redir_global():
        seen["stdout"] = sys.modules["sys"].stdout
        other = threading.Thread(target=lambda: print("LEAK", file=sys.stdout))
        other.start()
        other.join()
        print("ok")

    router = sys.stdout
    out = _read_to_file(shell, "_t_redir_global")
    assert out == "ok\n"
    assert seen["stdout"] is router
    assert sys.stdout is router


def test_redirect_stdin_and_stderr_to_stdout(shell, tmp_path):
    src = tmp_path / "in.txt"
    src.write_text("one\ntwo\n")

    @registry.command(name="_t_redir_io")
    def _t_redir_io():
        for line in sys.stdin:
            print(line.strip().upper())
        print("warn", file=sys.stderr)

    out = _read_to_file(shell, f"_t_redir_io < {src} 2>&1")
    assert out == "ONE\nTWO\nwarn\n"


def test_redirect_append(shell, tmp_path):
    @registry.command(name="_t_redir_append")
    def _t_redir_append():
        print("line")

    target = tmp_path / "log.txt"
    shell._execute(f"_t_redir_append >> {target}")
    shell._execute(f"_t_redir_append >> {target}")
    assert target.read_text() == "line\nline\n"


def test_system_exit_in_redirected_command_does_not_exit_shell(shell, tmp_path):
    @registry.command(name="_t_redir_exit")
    def _t_redir_exit():
        print("bye")
        raise SystemExit(3)

    target = tmp_path / "out.txt"
    shell._execute(f"_t_redir_exit > {target}")  # must not raise
    assert target.read_text() == "bye\n"


def test_redirected_external_command(shell, tmp_path):
    target = tmp_path / "out.txt"
    shell._execute(f"echo hello > {target}")
    assert target.read_text() == "hello\n"


def test_assignment_with_redirect_still_assigns(shell, tmp_path, monkeypatch):
    monkeypatch.delenv("_T_REDIR_VAR", raising=False)
    shell._execute(f"_T_REDIR_VAR=set > {tmp_path / 'x'}")
    assert os.environ.get("_T_REDIR_VAR") == "set"


def test_an_interrupted_stage_does_not_fall_back_to_the_terminal(shell):
    """Ctrl+C closes a stage's pipe ends; a decorator body re-run after
    that (``@watch {…} | cat``) must fail, not write to the real terminal."""
    from eosh.slots import _dup_threadlocal_override_fd

    r, w = os.pipe()
    os.close(r)
    out = os.fdopen(w, "w")
    out.close()
    sys.stdout.set_override(out)
    try:
        with pytest.raises(BrokenPipeError):
            _dup_threadlocal_override_fd(sys.stdout)
    finally:
        sys.stdout.clear_override()
