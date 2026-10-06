"""Unit tests for desktop notifications on long-running commands."""

from __future__ import annotations

import pytest

from eosh import notify
from eosh.process import ExitCallbackMixin


@pytest.fixture(autouse=True)
def _isolated_notify_settings():
    """Notification settings are process-global; restore them per test."""
    saved = (notify.is_enabled(), notify.get_threshold(), set(notify.SKIP_COMMANDS))
    yield
    notify.set_enabled(saved[0])
    notify.set_threshold(saved[1])
    notify.SKIP_COMMANDS = saved[2]
    notify.set_notifier(None)


@pytest.fixture
def posted():
    """Capture notifications instead of posting them to the desktop."""
    seen: list[tuple[str, str]] = []
    notify.set_notifier(lambda title, message: seen.append((title, message)))
    return seen


def _wait_for(seen: list, count: int = 1, timeout: float = 2.0) -> None:
    """notify() delivers on a daemon thread; give it a moment to land."""
    import time
    deadline = time.monotonic() + timeout
    while len(seen) < count and time.monotonic() < deadline:
        time.sleep(0.005)


# ---------------------------------------------------------------------------
# Duration formatting
# ---------------------------------------------------------------------------

class TestFmtDuration:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "0.0s"),
            (-5, "0.0s"),
            (0.42, "0.4s"),
            (12.34, "12.3s"),
            (59.9, "59.9s"),
            (60, "1m 00s"),
            (83, "1m 23s"),
            (3599, "59m 59s"),
            (3600, "1h 00m"),
            (3900, "1h 05m"),
        ],
    )
    def test_formats(self, seconds, expected):
        assert notify.fmt_duration(seconds) == expected


# ---------------------------------------------------------------------------
# The decision: threshold, master switch, skip list
# ---------------------------------------------------------------------------

class TestShouldNotify:
    def test_below_threshold_is_silent(self):
        notify.set_threshold(10)
        assert notify.should_notify("make", 9.9) is False

    def test_at_threshold_notifies(self):
        notify.set_threshold(10)
        assert notify.should_notify("make", 10.0) is True

    def test_disabled_is_silent_however_long(self):
        notify.set_enabled(False)
        assert notify.should_notify("make", 3600) is False

    def test_threshold_of_zero_notifies_for_anything(self):
        notify.set_threshold(0)
        assert notify.should_notify("make", 0.0) is True


class TestSkipList:
    @pytest.mark.parametrize(
        "line", ["vim notes.md", "less log.txt", "ssh host", "man ls", "exit"]
    )
    def test_interactive_commands_are_skipped(self, line):
        assert notify.is_skipped(line) is True

    @pytest.mark.parametrize("line", ["make -j8", "cargo build", "pytest -x"])
    def test_batch_commands_are_not_skipped(self, line):
        assert notify.is_skipped(line) is False

    def test_absolute_path_matches_on_basename(self):
        assert notify.is_skipped("/usr/local/bin/vim file") is True

    def test_windows_exe_suffix_matches(self):
        assert notify.is_skipped("vim.exe file") is True

    def test_quoted_command_matches(self):
        assert notify.is_skipped("'vim' file") is True

    def test_empty_line_is_skipped(self):
        assert notify.is_skipped("   ") is True

    def test_configure_replaces_the_set(self):
        notify.configure(skip_commands={"make"})
        assert notify.is_skipped("make") is True
        assert notify.is_skipped("vim") is False


# ---------------------------------------------------------------------------
# Message construction
# ---------------------------------------------------------------------------

class TestCommandDone:
    def test_short_command_posts_nothing(self, posted):
        notify.set_threshold(10)
        assert notify.command_done("make", 1.0, 0) is False
        assert posted == []

    def test_success_title_carries_the_duration(self, posted):
        notify.set_threshold(1)
        assert notify.command_done("make -j8", 83.0, 0) is True
        _wait_for(posted)
        title, message = posted[0]
        assert title == "✓ Eolith Shell — 1m 23s"
        assert message == "make -j8"

    def test_failure_title_carries_the_exit_code(self, posted):
        notify.set_threshold(1)
        notify.command_done("make", 20.0, 2)
        _wait_for(posted)
        title, _ = posted[0]
        assert title.startswith("✗ Eolith Shell — exit 2")
        assert "20.0s" in title

    def test_context_prefixes_the_message(self, posted):
        notify.set_threshold(1)
        notify.command_done("make", 20.0, 0, context="bg-1")
        _wait_for(posted)
        assert posted[0][1] == "[bg-1] make"

    def test_default_context_is_not_shown(self, posted):
        notify.set_threshold(1)
        notify.command_done("make", 20.0, 0, context="default")
        _wait_for(posted)
        assert posted[0][1] == "make"

    def test_long_command_is_truncated(self, posted):
        notify.set_threshold(1)
        notify.command_done("echo " + "x" * 500, 20.0, 0)
        _wait_for(posted)
        message = posted[0][1]
        assert len(message) <= 160
        assert message.endswith("…")

    def test_whitespace_is_collapsed(self, posted):
        notify.set_threshold(1)
        notify.command_done("make   -j8\n  install", 20.0, 0)
        _wait_for(posted)
        assert posted[0][1] == "make -j8 install"

    def test_empty_command_posts_nothing(self, posted):
        notify.set_threshold(0)
        assert notify.command_done("   ", 20.0, 0) is False

    def test_a_raising_backend_does_not_propagate(self):
        def boom(title, message):
            raise RuntimeError("no notification daemon")

        notify.set_notifier(boom)
        notify.set_threshold(1)
        notify.command_done("make", 20.0, 0)  # must not raise


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

class TestBackendSelection:
    def test_macos_uses_osascript(self, monkeypatch):
        monkeypatch.setattr(notify.sys, "platform", "darwin")
        monkeypatch.setattr(notify.shutil, "which", lambda name: "/usr/bin/osascript")
        assert notify._detect_backend() is notify._notify_macos

    def test_linux_uses_notify_send(self, monkeypatch):
        monkeypatch.setattr(notify.sys, "platform", "linux")
        monkeypatch.setattr(
            notify.shutil, "which", lambda name: "/usr/bin/notify-send" if name == "notify-send" else None
        )
        assert notify._detect_backend() is notify._notify_linux

    def test_windows_uses_powershell(self, monkeypatch):
        monkeypatch.setattr(notify.sys, "platform", "win32")
        monkeypatch.setattr(
            notify.shutil, "which", lambda name: "powershell.exe" if name == "powershell" else None
        )
        assert notify._detect_backend() is notify._notify_windows

    def test_no_helper_falls_back_to_the_bell(self, monkeypatch):
        monkeypatch.setattr(notify.sys, "platform", "linux")
        monkeypatch.setattr(notify.shutil, "which", lambda name: None)
        assert notify._detect_backend() is notify._notify_bell

    def test_macos_without_osascript_falls_back(self, monkeypatch):
        monkeypatch.setattr(notify.sys, "platform", "darwin")
        monkeypatch.setattr(notify.shutil, "which", lambda name: None)
        assert notify._detect_backend() is notify._notify_bell

    def test_backend_receives_the_built_command(self, monkeypatch):
        """The chosen backend, not just an override, gets title and message."""
        calls: list[list[str]] = []
        monkeypatch.setattr(notify, "_run_quiet", lambda argv: calls.append(argv))
        notify._notify_macos("✓ Eolith Shell — 1m 23s", 'make "all"')
        assert calls[0][0] == "osascript"
        script = calls[0][2]
        # Quotes in the command must not break out of the AppleScript literal.
        assert r'\"all\"' in script
        assert "with title" in script


class TestQuoting:
    def test_applescript_escapes_quotes_and_backslashes(self):
        assert notify._applescript_str(r'a"b\c') == r'"a\"b\\c"'

    def test_applescript_strips_control_characters(self):
        assert notify._applescript_str("a\nb") == '"a b"'

    def test_powershell_doubles_single_quotes(self):
        assert notify._powershell_str("it's") == "'it''s'"


# ---------------------------------------------------------------------------
# Shell variables
# ---------------------------------------------------------------------------

class TestVars:
    @pytest.fixture(autouse=True)
    def _registered(self):
        notify.register_vars()

    def _var(self, name):
        from eosh.variables import registry as var_registry
        var = var_registry.get(name)
        assert var is not None, f"{name} not registered"
        return var

    def test_notify_reports_and_flips_the_switch(self):
        var = self._var("notify")
        notify.set_enabled(True)
        assert var.get() == "on"
        var.set("off")
        assert notify.is_enabled() is False
        assert var.get() == "off"
        var.set("on")
        assert notify.is_enabled() is True

    @pytest.mark.parametrize("value", ["OFF", "false", "no", "0", "disabled"])
    def test_notify_accepts_falsey_spellings(self, value):
        var = self._var("notify")
        notify.set_enabled(True)
        var.set(value)
        assert notify.is_enabled() is False

    def test_notify_rejects_garbage_without_changing_state(self, capsys):
        var = self._var("notify")
        notify.set_enabled(True)
        var.set("maybe")
        assert notify.is_enabled() is True
        assert "expected on/off" in capsys.readouterr().err

    def test_unset_notify_disables(self):
        var = self._var("notify")
        notify.set_enabled(True)
        var.unset()
        assert notify.is_enabled() is False

    def test_threshold_round_trips_without_a_decimal_tail(self):
        var = self._var("notify_threshold")
        var.set("30")
        assert notify.get_threshold() == 30.0
        assert var.get() == "30"

    def test_threshold_rejects_non_numbers(self, capsys):
        var = self._var("notify_threshold")
        notify.set_threshold(10)
        var.set("soon")
        assert notify.get_threshold() == 10.0
        assert "expected a number" in capsys.readouterr().err

    def test_threshold_rejects_negatives(self, capsys):
        var = self._var("notify_threshold")
        notify.set_threshold(10)
        var.set("-5")
        assert notify.get_threshold() == 10.0
        assert "negative" in capsys.readouterr().err

    def test_unset_threshold_restores_the_default(self):
        var = self._var("notify_threshold")
        var.set("99")
        var.unset()
        assert notify.get_threshold() == notify.DEFAULT_THRESHOLD

    def test_neither_var_touches_the_environment(self):
        # No env_keys → process-global, not saved/restored on context switch.
        assert self._var("notify").env_keys == []
        assert self._var("notify_threshold").env_keys == []

    def test_values_are_completable(self):
        assert self._var("notify").value_completer is not None
        assert self._var("notify_threshold").value_completer is not None


# ---------------------------------------------------------------------------
# The slot-side exit hook the background notification rides on
# ---------------------------------------------------------------------------

class _FakeSlot(ExitCallbackMixin):
    def __init__(self, argv: list[str] | None = None, exit_code: int = 0, on_exit=None):
        self._init_exit_callback(on_exit)
        self.argv = argv or ["sleep", "60"]
        self.exit_code = exit_code


class TestExitCallbackMixin:
    def test_the_handler_given_at_construction_fires_with_the_slot(self):
        fired: list = []
        slot = _FakeSlot(on_exit=fired.append)
        slot._fire_on_exit()
        assert fired == [slot]

    def test_fires_at_most_once(self):
        fired: list = []
        slot = _FakeSlot(on_exit=fired.append)
        slot._fire_on_exit()
        slot._fire_on_exit()
        assert fired == [slot]

    def test_no_handler_is_not_an_error(self):
        _FakeSlot()._fire_on_exit()

    def test_a_raising_handler_is_swallowed(self):
        def boom(slot):
            raise RuntimeError("notification backend exploded")

        _FakeSlot(on_exit=boom)._fire_on_exit()  # must not raise into teardown

    def test_a_slot_starts_unparked(self):
        assert _FakeSlot().parked is False

    def test_elapsed_is_zero_before_start(self):
        assert _FakeSlot().elapsed() == 0.0

    def test_elapsed_measures_from_mark_started(self):
        slot = _FakeSlot()
        slot.mark_started()
        assert slot.start_time is not None
        assert slot.elapsed() >= 0.0


# ---------------------------------------------------------------------------
# Shell wiring — which paths report, which stay quiet
# ---------------------------------------------------------------------------

class TestShellHooks:
    @pytest.fixture
    def shell(self):
        from eosh.shell import Shell
        notify.set_threshold(0)
        return Shell()

    def test_a_finished_line_notifies(self, shell, monkeypatch, posted):
        monkeypatch.setattr(shell, "_execute_pipeline", lambda p, **kw: 0)
        shell._execute("make -j8")
        _wait_for(posted)
        assert posted[0][0].startswith("✓ Eolith Shell")
        assert posted[0][1] == "make -j8"

    def test_the_lines_exit_code_reaches_the_title(self, shell, monkeypatch, posted):
        monkeypatch.setattr(shell, "_execute_pipeline", lambda p, **kw: 2)
        shell._execute("make")
        _wait_for(posted)
        assert posted[0][0].startswith("✗ Eolith Shell — exit 2")

    def test_a_backgrounded_line_stays_quiet(self, shell, monkeypatch, posted):
        """Ctrl+] / @bg return early, so the line's own duration is meaningless —
        the slot's exit callback is what reports it."""
        def _background(pipeline, **kw):
            shell._backgrounded = True
            return 0

        monkeypatch.setattr(shell, "_execute_pipeline", _background)
        shell._execute("sleep 600")
        _wait_for(posted, timeout=0.2)
        assert posted == []

    def test_a_skipped_command_stays_quiet(self, shell, monkeypatch, posted):
        monkeypatch.setattr(shell, "_execute_pipeline", lambda p, **kw: 0)
        shell._execute("vim notes.md")
        _wait_for(posted, timeout=0.2)
        assert posted == []

    def test_backgrounded_flag_resets_between_lines(self, shell, monkeypatch, posted):
        def _background(pipeline, **kw):
            shell._backgrounded = True
            return 0

        monkeypatch.setattr(shell, "_execute_pipeline", _background)
        shell._execute("sleep 600")
        monkeypatch.setattr(shell, "_execute_pipeline", lambda p, **kw: 0)
        shell._execute("make")
        _wait_for(posted)
        assert [m for _, m in posted] == ["make"]


class TestSlotDoneNotification:
    """``Shell._slot_finished`` — the exit handler every slot is built with."""

    @pytest.fixture
    def shell(self):
        from eosh.shell import Shell
        notify.set_threshold(0)
        return Shell()

    def _parked(self, shell, name, argv=None, exit_code=0):
        slot = _FakeSlot(argv, exit_code=exit_code, on_exit=shell._slot_finished)
        slot.mark_started()
        ctx = shell.context_manager.contexts.get(name) or shell.context_manager.create(name)
        shell._park(slot, ctx)
        return slot

    def test_parking_marks_the_slot_and_the_line(self, shell):
        slot = self._parked(shell, "bg-test")
        assert slot.parked is True
        assert shell._backgrounded is True
        assert shell.context_manager.contexts["bg-test"].process_slot is slot

    def test_a_slot_ending_out_of_sight_carries_its_context(self, shell, posted):
        slot = self._parked(shell, "bg-test", ["sleep", "600"])
        slot._fire_on_exit()
        _wait_for(posted)
        assert posted[0][1] == "[bg-test] sleep 600"

    def test_a_slot_ending_in_the_current_context_has_no_prefix(self, shell, posted):
        """Resumed and watched to the end: still reported (the line that
        started it returned long ago), just without the context name."""
        slot = self._parked(shell, "watched", ["make"])
        shell.context_manager.switch("watched")
        slot._fire_on_exit()
        _wait_for(posted)
        assert posted[0][1] == "make"

    def test_a_slot_that_was_never_parked_stays_quiet(self, shell, posted):
        """It ran in the foreground, and ``_execute`` timed the whole line."""
        slot = _FakeSlot(on_exit=shell._slot_finished)
        slot.mark_started()
        slot._fire_on_exit()
        _wait_for(posted, timeout=0.2)
        assert posted == []

    def test_failure_exit_code_is_reported(self, shell, posted):
        slot = self._parked(shell, "bg-test", ["make"], exit_code=1)
        slot._fire_on_exit()
        _wait_for(posted)
        assert posted[0][0].startswith("✗ Eolith Shell — exit 1")
