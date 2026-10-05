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
from eosh.shell import Shell, _in_pipeline, passthrough_input, passthrough_run


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


def test_passthrough_run_refuses_inside_pipeline_thread():
    """passthrough_run / passthrough_input must error out when stdin/stdout
    are wired to pipes."""
    _in_pipeline.flag = True
    try:
        with pytest.raises(RuntimeError, match="passthrough_run"):
            passthrough_run(["true"])
        with pytest.raises(RuntimeError, match="passthrough_input"):
            passthrough_input("> ")
    finally:
        _in_pipeline.flag = False


# ---------------------------------------------------------------------------
# Stateful built-ins: the in-process model lets these mutate the parent
# (POSIX shells discard them; eosh does not — documented in
# doc/limitations.md as accepted behaviour).
# ---------------------------------------------------------------------------

def test_var_in_pipeline_mutates_parent(shell):
    """`var FOO=bar | cat` actually sets FOO in the parent process.

    The Python `var` builtin runs in the pipeline thread and writes to
    os.environ, which is shared.  This is the documented divergence
    from POSIX shells.
    """
    if "_T_PIPED_VAR" in os.environ:
        del os.environ["_T_PIPED_VAR"]
    shell._execute("var _T_PIPED_VAR=hello | cat")
    try:
        assert os.environ.get("_T_PIPED_VAR") == "hello"
    finally:
        os.environ.pop("_T_PIPED_VAR", None)


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
