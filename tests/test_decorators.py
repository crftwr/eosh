"""Tests for pipeline decorators (@name [flags] body).

Exercises the parser (brace handling, flag extraction, body building),
the registry, the executor dispatch, and ``@<TAB>`` completion.
"""

from __future__ import annotations

import os

import pytest

from eosh.commands import arg, registry as command_registry
from eosh.pipeline import (
    DecoratorParseError,
    Pipeline,
    Stage,
    parse_line,
    set_pipeline_executor,
)
from eosh.shell import Shell


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_test_decorators():
    """Snapshot the command registry (decorators live there as ``@name``)."""
    cmd_before = dict(command_registry._commands)
    builtins_before = set(command_registry._builtin_names)
    yield
    command_registry._commands = cmd_before
    command_registry._builtin_names = builtins_before


@pytest.fixture
def watch_deco():
    """Register a minimal @watch-shaped decorator for parser tests."""
    @command_registry.command(
        "@watch",
        params=[
            arg("-n", "--interval", type=float, default=2.0),
            arg("--no-clear", action="store_true"),
        ],
    )
    def _watch(pipeline, *, interval, no_clear):
        pass
    return command_registry.get("@watch")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_a_decorator_is_a_command_named_with_an_at(watch_deco):
    assert command_registry.has("@watch")
    assert watch_deco.name == "@watch"
    assert not command_registry.has("watch")   # no collision with a `watch` command


def test_flags_parse_like_a_command_and_the_pipeline_comes_first(watch_deco):
    received = {}

    @command_registry.command("@_t_args", params=[
        arg("-n", "--interval", type=float, default=2.0),
        arg("--no-clear", action="store_true"),
    ])
    def _args(pipeline, *, interval, no_clear):
        received.update(pipeline=pipeline, interval=interval, no_clear=no_clear)

    deco = command_registry.get("@_t_args")
    deco.invoke(["-n", "5"], "PIPE")
    assert received == {"pipeline": "PIPE", "interval": 5.0, "no_clear": False}
    # argparse rejects unknown flags — a usage error, status 2, not an exception.
    assert deco.invoke(["--bogus"], "PIPE") == 2


def test_the_parser_asks_the_registry_which_flags_take_a_value(watch_deco):
    from eosh.pipeline import _flag_takes_value
    assert _flag_takes_value("watch", "-n") is True
    assert _flag_takes_value("watch", "--interval") is True
    assert _flag_takes_value("watch", "--no-clear") is False
    assert _flag_takes_value("watch", "--missing") is False
    assert _flag_takes_value("nonexistent", "-n") is False


def test_reload_clears_user_decorators_and_keeps_builtins(watch_deco):
    """One registry, one sweep — `reload` used to leave config decorators."""
    command_registry.mark_builtins()

    @command_registry.command("@_t_user")
    def _user(pipeline):
        pass

    assert command_registry.has("@_t_user")
    command_registry.clear_user_commands()
    assert command_registry.has("@watch")        # builtin survives
    assert not command_registry.has("@_t_user")  # user decorator gone


# ---------------------------------------------------------------------------
# Parser — single-command form
# ---------------------------------------------------------------------------

def _decorator_stage(line: str) -> Stage:
    """Parse *line* and return the single decorator-stage in it."""
    seq = parse_line(line)
    assert len(seq.items) == 1
    pipeline = seq.items[0][1]
    assert len(pipeline.stages) == 1
    stage = pipeline.stages[0]
    assert stage.decorator is not None
    return stage


def test_parse_bare_decorator_no_flags(watch_deco):
    stage = _decorator_stage("@watch ls")
    assert stage.decorator.name == "watch"
    assert stage.decorator.flag_tokens == []
    assert [s.text for s in stage.decorator.body.stages] == ["ls"]


def test_parse_decorator_with_value_flag(watch_deco):
    """`-n 1` consumes both tokens; `df -h` is the body."""
    stage = _decorator_stage("@watch -n 1 df -h")
    assert stage.decorator.flag_tokens == ["-n", "1"]
    assert [s.text for s in stage.decorator.body.stages] == ["df -h"]


def test_parse_decorator_with_boolean_flag(watch_deco):
    stage = _decorator_stage("@watch --no-clear ls")
    assert stage.decorator.flag_tokens == ["--no-clear"]
    assert [s.text for s in stage.decorator.body.stages] == ["ls"]


def test_parse_decorator_with_flag_value_then_boolean(watch_deco):
    stage = _decorator_stage("@watch -n 1 --no-clear ls")
    assert stage.decorator.flag_tokens == ["-n", "1", "--no-clear"]
    assert [s.text for s in stage.decorator.body.stages] == ["ls"]


def test_parse_decorator_eq_form(watch_deco):
    """`--interval=5` is a single token; argparse already understands it."""
    stage = _decorator_stage("@watch --interval=5 ls")
    assert stage.decorator.flag_tokens == ["--interval=5"]
    assert [s.text for s in stage.decorator.body.stages] == ["ls"]


def test_parse_decorator_double_dash(watch_deco):
    """`--` terminates flag parsing; -la is part of ls's args."""
    stage = _decorator_stage("@watch -- ls -la")
    assert stage.decorator.flag_tokens == []
    assert [s.text for s in stage.decorator.body.stages] == ["ls -la"]


def test_parse_body_with_dash_flags(watch_deco):
    """`@watch -n 5 ls -la` — `-la` belongs to ls, not @watch."""
    stage = _decorator_stage("@watch -n 5 ls -la")
    assert stage.decorator.flag_tokens == ["-n", "5"]
    assert [s.text for s in stage.decorator.body.stages] == ["ls -la"]


# ---------------------------------------------------------------------------
# Parser — braced form
# ---------------------------------------------------------------------------

def test_parse_braced_single_command(watch_deco):
    stage = _decorator_stage("@watch {ls}")
    assert [s.text for s in stage.decorator.body.stages] == ["ls"]


def test_parse_braced_multistage_pipeline(watch_deco):
    stage = _decorator_stage("@watch {df -h | grep abc}")
    assert [s.text for s in stage.decorator.body.stages] == ["df -h", "grep abc"]


def test_parse_braced_with_flag(watch_deco):
    stage = _decorator_stage("@watch -n 1 {df -h | grep abc}")
    assert stage.decorator.flag_tokens == ["-n", "1"]
    assert [s.text for s in stage.decorator.body.stages] == ["df -h", "grep abc"]


# ---------------------------------------------------------------------------
# Parser — brace handling: quotes, escapes, ${var}
# ---------------------------------------------------------------------------

def test_brace_handling_double_quoted_brace(watch_deco):
    stage = _decorator_stage('@watch {echo "}"}')
    assert [s.text for s in stage.decorator.body.stages] == ['echo "}"']


def test_brace_handling_single_quoted_brace(watch_deco):
    stage = _decorator_stage("@watch {grep '{' file}")
    assert [s.text for s in stage.decorator.body.stages] == ["grep '{' file"]


def test_brace_handling_escaped_brace(watch_deco):
    stage = _decorator_stage(r"@watch {echo \}}")
    assert [s.text for s in stage.decorator.body.stages] == [r"echo \}"]


def test_brace_handling_var_expansion(watch_deco):
    """``${VAR}`` inside a decorator body should not terminate the scope.

    ``parse_line`` runs after ``expand_vars``; for tokens that survive
    expansion (single-quoted), the parser's ``${...}`` brace-balancing
    keeps the outer scope intact.
    """
    stage = _decorator_stage("@watch {echo '${X}'}")
    assert [s.text for s in stage.decorator.body.stages] == ["echo '${X}'"]


# ---------------------------------------------------------------------------
# Parser — composition (`@deco {body} | next`)
# ---------------------------------------------------------------------------

def test_parse_compose_decorator_with_pipe(watch_deco):
    """``@watch {ls} | grep py`` parses as a two-stage pipeline whose
    first stage is the decorator-call and second stage is ``grep py``."""
    seq = parse_line("@watch {ls} | grep py")
    assert len(seq.items) == 1
    pipeline = seq.items[0][1]
    assert len(pipeline.stages) == 2
    deco_stage, next_stage = pipeline.stages
    assert deco_stage.decorator is not None
    assert deco_stage.decorator.name == "watch"
    assert [s.text for s in deco_stage.decorator.body.stages] == ["ls"]
    assert next_stage.decorator is None
    assert next_stage.text == "grep py"


def test_parse_compose_decorator_with_multistage_body(watch_deco):
    stage_seq = parse_line("@watch {df -h | grep abc} | wc -l")
    pipeline = stage_seq.items[0][1]
    assert len(pipeline.stages) == 2
    assert pipeline.stages[0].decorator is not None
    assert [s.text for s in pipeline.stages[0].decorator.body.stages] == [
        "df -h", "grep abc"
    ]
    assert pipeline.stages[1].text == "wc -l"


def test_parse_compose_decorator_with_flags_and_pipe(watch_deco):
    seq = parse_line("@watch -n 1 {ls} | grep py")
    pipeline = seq.items[0][1]
    deco_stage, next_stage = pipeline.stages
    assert deco_stage.decorator.flag_tokens == ["-n", "1"]
    assert [s.text for s in deco_stage.decorator.body.stages] == ["ls"]
    assert next_stage.text == "grep py"


def test_parse_compose_decorator_with_multiple_pipe_stages(watch_deco):
    seq = parse_line("@watch {ls} | grep py | wc -l")
    pipeline = seq.items[0][1]
    assert len(pipeline.stages) == 3
    assert pipeline.stages[0].decorator is not None
    assert pipeline.stages[1].text == "grep py"
    assert pipeline.stages[2].text == "wc -l"


def test_parse_compose_decorator_redirect_on_following_stage(watch_deco):
    """A redirect on a stage *after* the decorator binds to that stage,
    not the decorator's body."""
    seq = parse_line("@watch {ls} | tee out.log")
    pipeline = seq.items[0][1]
    assert pipeline.stages[0].decorator is not None
    assert pipeline.stages[1].text == "tee out.log"


# ---------------------------------------------------------------------------
# Parser — error cases
# ---------------------------------------------------------------------------

def test_unbraced_pipe_is_rejected(watch_deco):
    with pytest.raises(DecoratorParseError, match="\\|"):
        parse_line("@watch ls | grep foo")


def test_unbraced_seq_is_rejected(watch_deco):
    with pytest.raises(DecoratorParseError, match="&&"):
        parse_line("@watch ls && pwd")


def test_unmatched_brace_is_rejected(watch_deco):
    with pytest.raises(DecoratorParseError, match="unmatched"):
        parse_line("@watch {ls")


def test_seq_after_close_brace_is_rejected(watch_deco):
    """``@deco {body} ; pwd`` mixes a decorator scope with the outer
    sequence grammar — only ``|`` composition is supported in the MVP."""
    with pytest.raises(DecoratorParseError, match="not supported"):
        parse_line("@watch {ls} ; pwd")
    with pytest.raises(DecoratorParseError, match="not supported"):
        parse_line("@watch {ls} && pwd")


def test_text_after_close_brace_without_pipe_is_rejected(watch_deco):
    """Plain trailing text after `}` (not a pipe) is still rejected."""
    with pytest.raises(DecoratorParseError, match="must start with"):
        parse_line("@watch {ls} grep foo")


def test_empty_body_is_rejected(watch_deco):
    with pytest.raises(DecoratorParseError, match="empty"):
        parse_line("@watch { }")


# ---------------------------------------------------------------------------
# Pipeline.run executor indirection
# ---------------------------------------------------------------------------

def test_pipeline_run_without_shell_raises():
    set_pipeline_executor(None)
    try:
        with pytest.raises(RuntimeError, match="no executor"):
            Pipeline(stages=[Stage(text="echo hi")]).run()
    finally:
        # leave the slot clean for the next test (any test instantiating
        # Shell will re-register).
        pass


def test_pipeline_run_with_shell_routes_to_executor(tmp_path):
    """Shell registers the executor on construction.  Pipeline.run() then
    delegates without raising RuntimeError.

    Use a redirect so the single-stage path runs synchronously rather than
    going through the PTY-forwarding loop (which can't open a controlling
    terminal under pytest's stdin capture).
    """
    Shell()
    captured = []

    @command_registry.command(name="_t_capture_arg")
    def _capture():
        captured.append("ran")
        print("ok")

    out = tmp_path / "out"
    from eosh.pipeline import Redirect
    Pipeline(
        stages=[Stage(text="_t_capture_arg", redirects=[Redirect(kind=">", target=str(out))])],
    ).run()
    assert captured == ["ran"]
    assert out.read_text().strip() == "ok"


# ---------------------------------------------------------------------------
# Decorator dispatch
# ---------------------------------------------------------------------------

def test_decorator_dispatch_passes_pipeline_and_args():
    """When ``@_t_capture -n 5 echo hi`` is executed, the decorator
    function receives the parsed pipeline body and the parsed flag
    value as a kwarg.  We invoke ``_execute_decorator_stage`` directly
    so we don't pull in the PTY-forwarding path (which fails under
    pytest's stdin capture)."""
    sh = Shell()
    received = {}

    @command_registry.command("@_t_capture", params=[arg("-n", type=int, default=1)])
    def _capture(pipeline, *, n):
        received["n"] = n
        received["body_texts"] = [s.text for s in pipeline.stages]

    seq = parse_line("@_t_capture -n 5 echo hi")
    sh._execute_decorator_stage(seq.items[0][1].stages[0])
    assert received == {"n": 5, "body_texts": ["echo hi"]}


def test_unknown_decorator_returns_127(capsys):
    """Parsing is registry-agnostic: any ``@<ident>`` produces a
    decorator stage.  Dispatch is what reports an unknown name."""
    sh = Shell()
    seq = parse_line("@_t_no_such_decorator ls")
    deco_stage = seq.items[0][1].stages[0]
    rc = sh._execute_decorator_stage(deco_stage)
    assert rc == 127
    captured = capsys.readouterr()
    assert "unknown decorator" in captured.err


def test_decorator_argparse_error_returns_2():
    sh = Shell()

    @command_registry.command("@_t_argerr", params=[arg("-n", type=int, default=1)])
    def _h(pipeline, *, n):
        pass

    seq = parse_line("@_t_argerr --bogus ls")
    rc = sh._execute_decorator_stage(seq.items[0][1].stages[0])
    assert rc == 2


# ---------------------------------------------------------------------------
# Decorator composition (`@deco {body} | next`) — execution path
# ---------------------------------------------------------------------------

def test_compose_decorator_pipes_body_output_through_next_stage(tmp_path):
    """``@_t_emit {body} | grep`` should run the decorator's body with
    its stdout wired to the next stage's stdin.  The decorator runs the
    body once (no looping) so we get a deterministic single capture."""
    from eosh.commands import registry as command_registry

    sh = Shell()

    @command_registry.command(name="_t_emit_lines2")
    def _emit():
        print("apple")
        print("banana")
        print("cherry")

    @command_registry.command("@_t_once")
    def _once(pipeline):
        # Run the body once and return — exercises the composition path
        # without the timing complexity @watch brings.
        pipeline.run()

    out_path = tmp_path / "out"
    sh._execute(f"@_t_once {{_t_emit_lines2}} | grep an > {out_path}")
    assert out_path.read_text() == "banana\n"


def test_compose_decorator_runs_body_inside_outer_pipe(tmp_path):
    """A bare-body decorator (no braces) under composition still runs
    the wrapped command and feeds the next stage."""
    from eosh.commands import registry as command_registry

    sh = Shell()

    @command_registry.command(name="_t_emit_lines3")
    def _emit():
        print("xx")
        print("yy")
        print("xz")

    @command_registry.command("@_t_once_b")
    def _once(pipeline):
        pipeline.run()

    out_path = tmp_path / "out"
    sh._execute(f"@_t_once_b {{_t_emit_lines3}} | grep x > {out_path}")
    text = out_path.read_text()
    assert "xx" in text and "xz" in text and "yy" not in text


# ---------------------------------------------------------------------------
# @<TAB> completion
# ---------------------------------------------------------------------------

def test_completion_decorator_name(watch_deco):
    shell = Shell()
    completions, prefix, label = shell._get_completions("@wa")
    assert label == "decorator"
    assert any(c.value == "@watch" for c in completions)


def test_completion_decorator_flags(watch_deco):
    shell = Shell()
    completions, prefix, label = shell._get_completions("@watch -")
    assert label == "@watch option"
    flag_values = {c.value for c in completions}
    assert "-n" in flag_values
    assert "--interval" in flag_values
    assert "--no-clear" in flag_values


def test_completion_after_decorator_delegates_to_command_completion(watch_deco):
    shell = Shell()
    completions, prefix, label = shell._get_completions("@watch e")
    # ``@watch e<TAB>`` should fall through to command-name completion.
    assert label == "command"
    values = {c.value for c in completions}
    assert "exit" in values  # built-in command starts with 'e'


def test_completion_after_decorator_with_flags_delegates(watch_deco):
    shell = Shell()
    completions, prefix, label = shell._get_completions("@watch -n 1 e")
    assert label == "command"
    values = {c.value for c in completions}
    assert "exit" in values


# ---------------------------------------------------------------------------
# Shell-level error handling for malformed decorator lines
# ---------------------------------------------------------------------------

def test_shell_execute_swallows_decorator_parse_error(capsys, watch_deco):
    """A malformed decorator line should print an error and return to the
    prompt, not crash the shell with an uncaught exception."""
    sh = Shell()
    sh._execute("@watch {ls")  # unmatched brace
    err = capsys.readouterr().err
    assert "unmatched" in err


def test_shell_execute_swallows_decorator_composition_seq_error(capsys, watch_deco):
    """``@deco {body} ; pwd`` is rejected — only ``|`` composition is
    supported.  The parse error must not crash the shell."""
    sh = Shell()
    sh._execute("@watch {ls} ; pwd")
    err = capsys.readouterr().err
    assert "not supported" in err


# ---------------------------------------------------------------------------
# sys.stdout.isatty() inside a decorator body
# ---------------------------------------------------------------------------

def test_stdout_isatty_visible_to_decorator_body():
    """``@watch`` checks ``sys.stdout.isatty()`` to decide whether to emit
    the screen-clear ANSI escape.  The ``_StdoutProxy`` installed for a
    Python-command thread must report the real stdout's tty status, not
    the io.TextIOBase default of ``False`` — otherwise ``--no-clear``
    has no observable effect (the clear path is skipped either way).

    Verified directly against the proxy: under pytest's stdin capture
    the real stdout isn't a tty, so the proxy must report False; what
    matters is that it asks the real stream rather than returning
    False unconditionally.
    """
    import sys as _sys
    from eosh.shell import _StdoutProxy

    proxy = _StdoutProxy(_sys.__stdout__)
    assert proxy.isatty() == _sys.__stdout__.isatty()


def test_watch_split_to_lines_strips_ansi_and_normalises_endings():
    """Real CLI output often includes ANSI colour / cursor-control codes
    even when stdout is redirected (TTY-autodetection is not reliable).
    ``_split_to_lines`` must strip them so footer line counts are real
    and lines never render in a leftover SGR state."""
    from eosh.decorators.watch import _split_to_lines

    # SGR colour around content — both ends should be removed, and the
    # visible width should be only the content itself.
    out = _split_to_lines("\x1b[31mred\x1b[0m\nplain\n")
    assert out == ["red", "plain"]

    # CRLF and bare CR are both treated as line terminators.
    assert _split_to_lines("a\r\nb\rc\n") == ["a", "b", "c"]

    # OSC sequences (xterm title etc.) are stripped — and don't inflate
    # the visible line length.
    out = _split_to_lines("\x1b]0;title\x07hello\n")
    assert out == ["hello"]


def test_watch_apply_scroll_key_clamps_to_range():
    """Scroll deltas must clamp to ``[0, max]`` so the user can't navigate
    past the start or end of the buffered output."""
    from eosh.decorators.watch import (
        _KEY_DOWN, _KEY_END, _KEY_HOME, _KEY_PAGE_DOWN, _KEY_PAGE_UP, _KEY_UP,
        _apply_scroll_key,
    )

    common = dict(body_rows=10, total_lines=100, max_line_len=80, body_cols=80)

    # PageDown from 0 → +10.  Up from 0 → still 0.
    assert _apply_scroll_key(_KEY_PAGE_DOWN, scroll_y=0, scroll_x=0, **common)[0] == 10
    assert _apply_scroll_key(_KEY_UP, scroll_y=0, scroll_x=0, **common)[0] == 0
    # G jumps to the bottom; further Down stays clamped.
    end_y = _apply_scroll_key(_KEY_END, scroll_y=0, scroll_x=0, **common)[0]
    assert end_y == 90  # total - body_rows
    assert _apply_scroll_key(_KEY_DOWN, scroll_y=end_y, scroll_x=0, **common)[0] == end_y
    # Home returns to the top.
    assert _apply_scroll_key(_KEY_HOME, scroll_y=end_y, scroll_x=0, **common)[0] == 0
    # PageUp from middle clamps to 0 when the chunk overshoots.
    assert _apply_scroll_key(_KEY_PAGE_UP, scroll_y=5, scroll_x=0, **common)[0] == 0


def test_watch_render_scrollbar_thumb_size_and_position():
    """Scrollbar thumb is proportional to visible/total and slides as we scroll."""
    from eosh.colors import _bg, get_color_scheme
    from eosh.decorators.watch import _render_scrollbar

    s = get_color_scheme()
    thumb_sgr = _bg(*s.scroll_thumb)
    track_sgr = _bg(*s.scroll_track)

    def is_thumb(cell: str) -> bool:
        return thumb_sgr in cell

    # No scrollbar when content fits.
    bar = _render_scrollbar(body_rows=10, scroll_y=0, total_lines=10)
    assert bar == [" "] * 10

    # 100 lines into a 10-row body → thumb is 1 row at the very top.
    bar = _render_scrollbar(body_rows=10, scroll_y=0, total_lines=100)
    assert is_thumb(bar[0])
    assert sum(1 for c in bar if is_thumb(c)) >= 1
    assert all(is_thumb(c) or track_sgr in c for c in bar)

    # Scrolled to bottom → thumb is at the last row.
    bar = _render_scrollbar(body_rows=10, scroll_y=90, total_lines=100)
    assert is_thumb(bar[-1])


def test_watch_slice_for_render_pads_and_trims():
    """The visible window is body_rows tall, body_cols wide, padded with
    blanks when the buffered output is shorter than the body."""
    from eosh.decorators.watch import _slice_for_render

    lines = ["aaa", "bbbb", "cc"]
    out = _slice_for_render(lines, scroll_y=0, scroll_x=0, body_rows=5, body_cols=3)
    assert out == ["aaa", "bbb", "cc", "", ""]

    # Vertical scroll skips the head.
    out = _slice_for_render(lines, scroll_y=1, scroll_x=0, body_rows=2, body_cols=10)
    assert out == ["bbbb", "cc"]

    # Horizontal scroll skips columns.
    out = _slice_for_render(lines, scroll_y=0, scroll_x=2, body_rows=3, body_cols=10)
    assert out == ["a", "bb", ""]


def test_watch_pipeline_redirected_to_helper():
    """`@watch`'s alt-screen path appends a `> /tmp/...` redirect to the
    body's last stage so output can be captured before drawing.  The
    helper that builds the redirected Pipeline must leave the original
    AST untouched (subsequent iterations re-run the user's pipeline as
    written) and override prior stdout redirects on the last stage."""
    from eosh.decorators.watch import _pipeline_redirected_to
    from eosh.pipeline import Pipeline, Redirect, Stage

    original = Pipeline(stages=[
        Stage(text="echo hi"),
        Stage(text="grep h"),
    ])
    redirected = _pipeline_redirected_to(original, "/tmp/out")
    # Original is untouched.
    assert [s.redirects for s in original.stages] == [[], []]
    # First stage unchanged; last stage gets stdout + stderr redirected
    # so command errors land in the watch frame rather than bleeding onto
    # the alt-screen UI.
    assert redirected.stages[0].redirects == []
    assert redirected.stages[-1].redirects == [
        Redirect(kind=">", target="/tmp/out"),
        Redirect(kind="2>&1", target="1"),
    ]
    # Stage text is preserved.
    assert [s.text for s in redirected.stages] == ["echo hi", "grep h"]


def test_python_command_slot_poll_key_returns_buffered_bytes():
    """The slot's stdin keybuf collects bytes the main forwarding loop
    received while no PTY subprocess is active, so a Python command body
    can poll for keystrokes (e.g. ``q`` to quit ``@watch``)."""
    from eosh.shell import PythonCommandSlot

    class _DummyCmd:
        name = "_dummy"

        def invoke(self, args):
            pass

    slot = PythonCommandSlot(_DummyCmd(), [])

    # No data and a zero timeout → empty bytes immediately.
    assert slot.poll_key(timeout=0) == b""

    # write_stdin with no PTY active: bytes should land in the keybuf.
    slot.write_stdin(b"q")
    assert slot.poll_key(timeout=0) == b"q"
    # Subsequent poll with the buffer drained returns empty.
    assert slot.poll_key(timeout=0) == b""


def test_thread_local_stdout_isatty_falls_through(monkeypatch):
    """``_ThreadLocalStream`` must forward ``isatty()`` to either the
    real stream (no override) or the thread-local override — not return
    the io.TextIOBase default of ``False``."""
    import sys as _sys
    from eosh.shell import _ThreadLocalStream, _StdoutProxy

    tls = _ThreadLocalStream(_sys.__stdout__)
    # No override: reflect the real stream.
    assert tls.isatty() == _sys.__stdout__.isatty()

    # With a _StdoutProxy override (the path a PythonCommandSlot uses),
    # isatty() should reflect the proxy, which itself reflects the real
    # stream.
    proxy = _StdoutProxy(_sys.__stdout__)
    tls.set_override(proxy)
    try:
        assert tls.isatty() == _sys.__stdout__.isatty()
    finally:
        tls.clear_override()


# ---------------------------------------------------------------------------
# @time, @retry, @quiet — built-in decorators
# ---------------------------------------------------------------------------

def _enable_builtin(name: str) -> None:
    """Load a built-in decorator without spinning up a full Shell.

    Tests that just need the decorator registered (parsing / arg checks)
    can call this and rely on the autouse fixture to pop it again.
    """
    from eosh.decorators import register_builtins
    register_builtins()


def _make_recording_pipeline(callback):
    """Return a Pipeline whose run() invokes *callback()* and returns its int.

    Tests use this to drive decorator functions directly without going
    through ``_execute`` (which under pytest needs a real terminal for the
    Python-command forwarding path).
    """
    from eosh.pipeline import Pipeline as _P, Stage as _S

    class _RecordingPipeline(_P):
        def run(self, stdin=None, stdout=None, stderr=None):
            return callback()

    return _RecordingPipeline(stages=[_S(text="<recorded>")])


def test_time_decorator_runs_body_and_emits_summary(capsys):
    _enable_builtin("time")
    deco = command_registry.get("@time")

    calls = {"n": 0}

    def _body():
        calls["n"] += 1
        return 0

    pipeline = _make_recording_pipeline(_body)
    rc = deco.func(pipeline)
    assert rc == 0
    assert calls["n"] == 1
    err = capsys.readouterr().err
    for label in ("real\t", "user\t", "sys\t"):
        assert label in err


def test_retry_succeeds_first_attempt(capsys):
    _enable_builtin("retry")
    deco = command_registry.get("@retry")
    calls = {"n": 0}

    def _ok():
        calls["n"] += 1
        return 0

    pipeline = _make_recording_pipeline(_ok)
    rc = deco.func(pipeline, attempts=3, delay=0.0)
    assert rc == 0
    assert calls["n"] == 1


def test_retry_eventual_success(capsys):
    _enable_builtin("retry")
    deco = command_registry.get("@retry")
    calls = {"n": 0}

    def _flaky():
        calls["n"] += 1
        return 0 if calls["n"] >= 3 else 1

    pipeline = _make_recording_pipeline(_flaky)
    rc = deco.func(pipeline, attempts=5, delay=0.0)
    assert rc == 0
    assert calls["n"] == 3
    err = capsys.readouterr().err
    assert err.count("retrying") == 2


def test_retry_gives_up(capsys):
    _enable_builtin("retry")
    deco = command_registry.get("@retry")
    calls = {"n": 0}

    def _bad():
        calls["n"] += 1
        return 7

    pipeline = _make_recording_pipeline(_bad)
    rc = deco.func(pipeline, attempts=2, delay=0.0)
    assert rc == 7
    assert calls["n"] == 2
    err = capsys.readouterr().err
    assert "gave up" in err


def test_retry_invalid_attempts(capsys):
    _enable_builtin("retry")
    deco = command_registry.get("@retry")
    pipeline = _make_recording_pipeline(lambda: 0)
    rc = deco.func(pipeline, attempts=0, delay=0.0)
    assert rc == 2
    assert "must be >= 1" in capsys.readouterr().err


def test_quiet_silenced_helper_appends_redirect():
    _enable_builtin("quiet")
    from eosh.decorators.quiet import _silenced
    from eosh.pipeline import Pipeline, Redirect, Stage

    original = Pipeline(stages=[Stage(text="echo hi"), Stage(text="grep h")])
    silenced = _silenced(original, also_stderr=False)
    # Original untouched.
    assert [s.redirects for s in original.stages] == [[], []]
    # Last stage now writes to /dev/null.
    assert silenced.stages[-1].redirects[0].kind == ">"
    assert silenced.stages[-1].redirects[0].target == os.devnull
    assert len(silenced.stages[-1].redirects) == 1

    silenced2 = _silenced(original, also_stderr=True)
    # With --stderr, an additional 2>&1 is appended so stderr follows stdout.
    assert silenced2.stages[-1].redirects[1] == Redirect(kind="2>&1", target="1")


def test_quiet_discards_stdout(tmp_path):
    _enable_builtin("quiet")
    sh = Shell()

    out_marker = tmp_path / "should_be_empty"
    out_marker.write_text("placeholder\n")  # ensure file gets overwritten

    @command_registry.command(name="_t_quiet_body")
    def _body():
        print("hidden output")
        import sys
        print("err output", file=sys.stderr)

    # @quiet's body's stdout goes to /dev/null; our outer redirect captures
    # whatever stdout reaches the decorator (which is nothing now).
    sh._execute(f"@quiet _t_quiet_body > {out_marker}")
    # The outer ``>`` redirect creates an empty file (the decorator
    # function returned 0 having printed nothing to its own stdout).
    assert out_marker.read_text() == ""


def _make_blocking_pipeline(release_event):
    """Pipeline whose run() blocks until *release_event* is set, then returns 0."""
    def _body():
        release_event.wait(timeout=2.0)
        return 0
    return _make_recording_pipeline(_body)


# ---------------------------------------------------------------------------
# Decorators share the command registry — kept apart where it shows
# ---------------------------------------------------------------------------

def test_command_name_completion_leaves_decorators_out(watch_deco):
    shell = Shell()
    completions, _, _ = shell._get_base_completions("w")
    assert "@watch" not in [c.value for c in completions]


def test_help_lists_decorators_under_their_own_heading(watch_deco, capsys):
    shell = Shell()
    shell.registry.get("help").invoke([])
    out = capsys.readouterr().out
    commands, _, decorators = out.partition("Decorators")
    assert "@watch" in decorators and "@watch" not in commands
    shell.registry.get("help").invoke(["@watch"])
    assert "@watch" in capsys.readouterr().out


def test_a_decorator_flag_value_completes_like_a_commands():
    from eosh.completion import ChoiceCompleter

    @command_registry.command("@_t_val", params=[
        arg("-m", "--mode", metavar="MODE", completer=ChoiceCompleter(["fast", "slow"])),
    ])
    def _val(pipeline, *, mode):
        pass

    shell = Shell()
    completions, _, label = shell._get_base_completions("@_t_val -m ")
    assert [c.value for c in completions] == ["fast", "slow"]
    assert label.startswith("-m <MODE>")
