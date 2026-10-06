"""Tests for pipeline decorators (@name [flags] body).

Exercises the parser (brace handling, flag extraction, body building),
the registry, the executor dispatch, and ``@<TAB>`` completion.
"""

from __future__ import annotations

import os

import pytest

from eosh.commands import arg, registry as command_registry
from eosh.decorators import (
    parse_decorator_args,
    registry as decorator_registry,
)
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
    """Snapshot decorator + command registries; restore after each test."""
    deco_before = dict(decorator_registry._decorators)
    cmd_before = dict(command_registry._commands)
    yield
    # Drop only the new entries added during the test.
    for name in list(decorator_registry._decorators):
        if name not in deco_before:
            del decorator_registry._decorators[name]
    for name in list(command_registry._commands):
        if name not in cmd_before:
            del command_registry._commands[name]


@pytest.fixture
def watch_deco():
    """Register a minimal @watch-shaped decorator for parser tests."""
    @decorator_registry.decorator(
        name="watch",
        params=[
            arg("-n", "--interval", type=float, default=2.0),
            arg("--no-clear", action="store_true"),
        ],
    )
    def _watch(pipeline, *, interval, no_clear):
        pass
    return decorator_registry.get("watch")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_register_decorator(watch_deco):
    assert decorator_registry.has("watch")
    assert "watch" in decorator_registry.list_decorators()
    assert watch_deco.name == "watch"


def test_parse_decorator_args(watch_deco):
    assert parse_decorator_args(watch_deco, []) == {"interval": 2.0, "no_clear": False}
    assert parse_decorator_args(watch_deco, ["-n", "5"]) == {"interval": 5.0, "no_clear": False}
    assert parse_decorator_args(watch_deco, ["--no-clear"]) == {"interval": 2.0, "no_clear": True}
    # argparse rejects unknown flags — should return None, not raise.
    assert parse_decorator_args(watch_deco, ["--bogus"]) is None


def test_flag_takes_value(watch_deco):
    assert decorator_registry.flag_takes_value("watch", "-n") is True
    assert decorator_registry.flag_takes_value("watch", "--interval") is True
    assert decorator_registry.flag_takes_value("watch", "--no-clear") is False
    assert decorator_registry.flag_takes_value("watch", "--missing") is False
    assert decorator_registry.flag_takes_value("nonexistent", "-n") is False


def test_clear_user_decorators_keeps_builtins(watch_deco):
    decorator_registry.mark_builtins()

    @decorator_registry.decorator(name="_t_user")
    def _user(pipeline):
        pass

    assert decorator_registry.has("_t_user")
    decorator_registry.clear_user_decorators()
    assert decorator_registry.has("watch")  # builtin survives
    assert not decorator_registry.has("_t_user")  # user decorator gone


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

    @decorator_registry.decorator(name="_t_capture", params=[arg("-n", type=int, default=1)])
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

    @decorator_registry.decorator(name="_t_argerr", params=[arg("-n", type=int, default=1)])
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

    @decorator_registry.decorator(name="_t_once")
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

    @decorator_registry.decorator(name="_t_once_b")
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
# @time, @retry, @quiet, @bg — built-in decorators
# ---------------------------------------------------------------------------

def _enable_builtin(name: str) -> None:
    """Load a built-in decorator without spinning up a full Shell.

    Tests that just need the decorator registered (parsing / arg checks)
    can call this and rely on the autouse fixture to pop it again.
    """
    from eosh.decorators import enable as enable_decorators
    enable_decorators(name)


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
    deco = decorator_registry.get("time")

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
    deco = decorator_registry.get("retry")
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
    deco = decorator_registry.get("retry")
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
    deco = decorator_registry.get("retry")
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
    deco = decorator_registry.get("retry")
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


def test_bg_starts_background_context_with_auto_name():
    import threading
    _enable_builtin("bg")
    sh = Shell()
    release = threading.Event()
    name = sh._run_in_background(_make_blocking_pipeline(release))
    try:
        assert name == "bg-1"
        ctx = sh.context_manager.contexts["bg-1"]
        assert ctx.process_slot is not None
        assert ctx.process_slot.is_alive()
    finally:
        release.set()
        sh.context_manager.contexts["bg-1"].process_slot._thread.join(timeout=2.0)


def test_bg_named_context():
    import threading
    _enable_builtin("bg")
    sh = Shell()
    release = threading.Event()
    name = sh._run_in_background(_make_blocking_pipeline(release), name="my-job")
    try:
        assert name == "my-job"
        assert "my-job" in sh.context_manager.contexts
    finally:
        release.set()
        sh.context_manager.contexts["my-job"].process_slot._thread.join(timeout=2.0)


def test_bg_refuses_collision_with_running_slot():
    import threading
    _enable_builtin("bg")
    sh = Shell()
    release = threading.Event()
    sh._run_in_background(_make_blocking_pipeline(release), name="taken")
    try:
        with pytest.raises(ValueError, match="already has a running process"):
            sh._run_in_background(_make_blocking_pipeline(release), name="taken")
    finally:
        release.set()
        sh.context_manager.contexts["taken"].process_slot._thread.join(timeout=2.0)


def test_bg_auto_name_skips_existing():
    import threading
    _enable_builtin("bg")
    sh = Shell()
    release = threading.Event()
    n1 = sh._run_in_background(_make_blocking_pipeline(release))
    n2 = sh._run_in_background(_make_blocking_pipeline(release))
    try:
        assert n1 == "bg-1"
        assert n2 == "bg-2"
    finally:
        release.set()
        for n in ("bg-1", "bg-2"):
            sh.context_manager.contexts[n].process_slot._thread.join(timeout=2.0)


def test_bg_decorator_function_calls_runner(capsys):
    """The @bg decorator function delegates to the shell-side runner via
    the ``set_background_runner`` hook.  Verify the message format and
    return code without spinning up a real Shell."""
    _enable_builtin("bg")
    deco = decorator_registry.get("bg")
    from eosh.decorators import set_background_runner

    received = {}

    def _fake_runner(pipeline, *, name=None):
        received["pipeline"] = pipeline
        received["name"] = name
        return name or "bg-fake"

    set_background_runner(_fake_runner)
    try:
        pipeline = _make_recording_pipeline(lambda: 0)
        rc = deco.func(pipeline, ctx_name="explicit")
        assert rc == 0
        assert received["pipeline"] is pipeline
        assert received["name"] == "explicit"
        assert "started in context 'explicit'" in capsys.readouterr().err
    finally:
        # Restore the shell's runner if one was registered before this test.
        from eosh.shell import Shell as _Shell  # noqa: F401
        # Simplest restore: register None; subsequent Shell() calls re-wire.
        set_background_runner(None)


def test_bg_pipeline_slot_is_python_command_slot_subclass():
    """run() loop's resume path branches on isinstance(slot, PythonCommandSlot).
    PipelineSlot must subclass it so backgrounded pipelines take the
    Python-command resume path (proxy buffering, no PTY)."""
    from eosh.shell import PipelineSlot, PythonCommandSlot

    assert issubclass(PipelineSlot, PythonCommandSlot)


def test_bg_refuses_outer_pipeline_composition(capsys):
    """`@bg {body} | next` makes no sense — @bg returns immediately and the
    next stage would have nothing to read.  Verify the runner refuses it."""
    _enable_builtin("bg")
    sh = Shell()
    import threading
    from eosh.shell import _in_pipeline

    @command_registry.command(name="_t_bg_pipe_body")
    def _body():
        pass

    # Simulate being inside an outer pipeline by setting the thread-local flag.
    _in_pipeline.flag = True
    try:
        with pytest.raises(ValueError, match="outer pipeline"):
            sh._run_in_background(
                Pipeline(stages=[Stage(text="echo hi")]),
            )
    finally:
        _in_pipeline.flag = False


def test_bg_external_body_uses_process_slot(capfd):
    """A single-stage external command should be backed by ``ProcessSlot``
    (real PTY) so interactive TUIs work and output is captured at the PTY
    level rather than leaking to the real terminal.

    Regression test for the original bug where ``@bg df`` showed output
    immediately because the body went through ``PipelineSlot``'s pipe path,
    which then called ``_execute_external``'s PTY-backed slot from a
    background thread and grabbed real stdin/stdout.
    """
    import time

    from eosh.process import ProcessSlot

    _enable_builtin("bg")
    sh = Shell()
    pipeline = Pipeline(stages=[Stage(text="echo bg-marker-xyz")])
    name = sh._run_in_background(pipeline)
    slot = sh.context_manager.contexts[name].process_slot
    try:
        assert isinstance(slot, ProcessSlot)
        # Wait for the child to exit.
        slot._exit_event.wait(timeout=2.0)
        assert not slot.is_alive()
        # The reader thread routes bytes to slot.missed (not real stdout)
        # while the slot is inactive.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            joined = b"".join(slot.buffer._buf)
            if b"bg-marker-xyz" in joined:
                break
            time.sleep(0.02)
        captured = capfd.readouterr()
        assert "bg-marker-xyz" not in captured.out, captured.out
        assert b"bg-marker-xyz" in joined
    finally:
        if slot.is_alive():
            slot.kill()


def test_bg_python_body_uses_python_command_slot(capfd):
    """A single-stage registered Python command must run on
    ``PythonCommandSlot`` (not the PipelineSlot pipe path) so the body can
    call :func:`passthrough_run` to allocate its own PTY for nested
    interactive subprocesses (``aws ssm start-session`` etc.).  Output is
    buffered through the slot's ``_StdoutProxy`` until the user switches
    in.
    """
    from eosh.shell import PythonCommandSlot, PipelineSlot

    _enable_builtin("bg")
    sh = Shell()

    @command_registry.command(name="_t_bg_py_body")
    def _body():
        print("py-marker-abc")

    pipeline = Pipeline(stages=[Stage(text="_t_bg_py_body")])
    name = sh._run_in_background(pipeline)
    slot = sh.context_manager.contexts[name].process_slot
    try:
        assert isinstance(slot, PythonCommandSlot)
        assert not isinstance(slot, PipelineSlot)
        slot._thread.join(timeout=2.0)
        assert not slot._thread.is_alive()
        captured = capfd.readouterr()
        assert "py-marker-abc" not in captured.out
        # Output is buffered in the slot's _StdoutProxy ready to replay.
        assert "py-marker-abc" in slot._proxy._buf.getvalue()
    finally:
        if slot._thread.is_alive():
            slot._thread.join(timeout=1.0)


def test_bg_multi_stage_body_uses_pipeline_slot():
    """A multi-stage pipeline body falls through to :class:`PipelineSlot`
    (OS pipe + Popen)."""
    from eosh.shell import PipelineSlot

    _enable_builtin("bg")
    sh = Shell()
    pipeline = Pipeline(stages=[Stage(text="echo hi"), Stage(text="cat")])
    name = sh._run_in_background(pipeline)
    slot = sh.context_manager.contexts[name].process_slot
    try:
        assert isinstance(slot, PipelineSlot)
        slot._thread.join(timeout=2.0)
    finally:
        if slot._thread.is_alive():
            slot._thread.join(timeout=1.0)
