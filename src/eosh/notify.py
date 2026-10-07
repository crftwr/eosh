"""OS desktop notifications when a long-running command finishes.

A command that takes longer than :data:`DEFAULT_THRESHOLD` seconds is one the
user has almost certainly stopped watching — they switched to a browser, a
chat window, another terminal.  This module posts a desktop notification at
the moment such a command finishes, so the shell can say "I'm done" without
the user polling it.

**Zero dependencies.**  Every backend is a program the platform already
ships, discovered once and cached:

===========  ==================================================================
macOS        ``osascript -e 'display notification …'``
Linux/BSD    ``notify-send`` (libnotify), when on ``PATH``
Windows      a PowerShell WinRT toast
fallback     the terminal bell (``\\a``) — audible, always available
===========  ==================================================================

Notifications are posted from a daemon thread: the backend is a subprocess
spawn (tens of milliseconds on macOS) and the shell must not stall on it.
Every failure is swallowed — a shell that dies because a notification
couldn't be delivered would be far worse than a missed notification.

**What triggers one** is decided by the shell (see ``Shell._execute`` and
``Shell._slot_finished``), in two cases — each piece of work is reported by
exactly one of them, and only if it ran at least :func:`get_threshold`
seconds:

1. A foreground command line that ran to completion.
2. A slot that was parked on a context (``Ctrl+]`` or ``@bg``), when it
   ends.  The message carries the context's name when that context isn't
   the current one — the user was looking elsewhere.

**Configuration** — at the prompt via two registered variables, or from
``~/.eosh/config.py`` via :func:`configure`::

    var notify=off              # disable entirely
    var notify_threshold=30     # only notify for commands ≥ 30s

    # ~/.eosh/config.py
    from eosh import notify
    notify.configure(threshold=30)
    notify.SKIP_COMMANDS.add("psql")
    notify.set_notifier(lambda title, msg: my_own_backend(title, msg))
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from typing import Callable

#: Seconds a command must run before its completion is worth a notification.
DEFAULT_THRESHOLD = 10.0

#: Commands whose long runtime says nothing about work finishing — the user
#: sat in an editor, a pager, or a remote session the whole time, and a
#: "done!" popup on quitting it is pure noise.  Matched against the basename
#: of the line's first word.  Mutate this set from ``config.py`` to taste.
SKIP_COMMANDS: set[str] = {
    # editors
    "vi", "vim", "nvim", "view", "emacs", "emacsclient", "nano", "pico", "micro",
    # pagers / viewers
    "less", "more", "most", "man", "bat", "info",
    # process monitors
    "top", "htop", "btop", "btm", "glances",
    # remote sessions & multiplexers
    "ssh", "mosh", "telnet", "tmux", "screen", "zellij",
    # shells (an interactive sub-shell is a session, not a job)
    "sh", "bash", "zsh", "fish", "dash", "csh", "tcsh", "eosh",
    # leaving the shell is not an event worth a popup
    "exit",
}

_enabled = True
_threshold = DEFAULT_THRESHOLD
_notifier: Callable[[str, str], None] | None = None
_backend_cache: Callable[[str, str], None] | None = None

_MAX_MESSAGE_CHARS = 160


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def configure(
    *,
    enabled: bool | None = None,
    threshold: float | None = None,
    skip_commands: set[str] | None = None,
) -> None:
    """Set any subset of the notification settings.

    Args:
        enabled:       master on/off switch.
        threshold:     minimum command duration, in seconds.
        skip_commands: replaces :data:`SKIP_COMMANDS` wholesale.  To *add* to
                       the defaults, mutate that set instead.
    """
    global _enabled, _threshold, SKIP_COMMANDS
    if enabled is not None:
        _enabled = bool(enabled)
    if threshold is not None:
        _threshold = max(0.0, float(threshold))
    if skip_commands is not None:
        SKIP_COMMANDS = set(skip_commands)


def set_notifier(func: Callable[[str, str], None] | None) -> None:
    """Replace the platform backend with *func*, called as ``func(title, message)``.

    Pass ``None`` to restore the built-in backend chain.  The function is
    invoked on a daemon thread and must not write to the terminal.
    """
    global _notifier
    _notifier = func


def is_enabled() -> bool:
    return _enabled


def set_enabled(value: bool) -> None:
    global _enabled
    _enabled = bool(value)


def get_threshold() -> float:
    return _threshold


def set_threshold(seconds: float) -> None:
    global _threshold
    _threshold = max(0.0, float(seconds))


# ---------------------------------------------------------------------------
# Decision + message construction
# ---------------------------------------------------------------------------

def is_skipped(command: str) -> bool:
    """True when *command*'s first word is in :data:`SKIP_COMMANDS`.

    The basename is compared, so ``/usr/bin/vim`` and ``vim`` behave the
    same, as do ``vim.exe`` and ``vim`` on Windows.
    """
    first = command.strip().split(maxsplit=1)
    if not first:
        return True
    word = os.path.basename(first[0].strip("\"'").replace("\\", "/"))
    if word.lower().endswith(".exe"):
        word = word[:-4]
    return word in SKIP_COMMANDS


def should_notify(command: str, elapsed: float) -> bool:
    """Whether a command that ran for *elapsed* seconds deserves a notification."""
    return _enabled and elapsed >= _threshold and not is_skipped(command)


def fmt_duration(seconds: float) -> str:
    """Format a duration the way a human would read it off a stopwatch."""
    if seconds < 0:
        seconds = 0.0
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def command_done(
    command: str,
    elapsed: float,
    exit_code: int,
    context: str | None = None,
) -> bool:
    """Notify that *command* finished, if it ran long enough to be worth it.

    Returns whether a notification was posted, so callers can be tested
    without a desktop.
    """
    command = " ".join(command.split())
    if not command or not should_notify(command, elapsed):
        return False

    duration = fmt_duration(elapsed)
    if exit_code == 0:
        title = f"✓ Eolith Shell — {duration}"
    else:
        title = f"✗ Eolith Shell — exit {exit_code} ({duration})"

    if len(command) > _MAX_MESSAGE_CHARS:
        command = command[: _MAX_MESSAGE_CHARS - 1] + "…"
    if context and context != "default":
        command = f"[{context}] {command}"

    notify(title, command)
    return True


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def notify(title: str, message: str) -> None:
    """Post a desktop notification, off the calling thread and never raising."""
    backend = _notifier or _get_backend()

    def _deliver() -> None:
        try:
            backend(title, message)
        except Exception:
            pass  # a failed notification must never disturb the shell

    threading.Thread(target=_deliver, name="eosh-notify", daemon=True).start()


def _get_backend() -> Callable[[str, str], None]:
    """Return the platform backend, probing for it only once."""
    global _backend_cache
    if _backend_cache is None:
        _backend_cache = _detect_backend()
    return _backend_cache


def _detect_backend() -> Callable[[str, str], None]:
    if sys.platform == "darwin" and shutil.which("osascript"):
        return _notify_macos
    if sys.platform == "win32":
        if shutil.which("powershell") or shutil.which("pwsh"):
            return _notify_windows
    elif shutil.which("notify-send"):
        return _notify_linux
    return _notify_bell


def _run_quiet(argv: list[str]) -> None:
    """Fire-and-wait on a notification helper with its output discarded."""
    kwargs: dict = {}
    if sys.platform == "win32":
        # Keep the helper from flashing a console window over the terminal.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
        **kwargs,
    )


def _applescript_str(s: str) -> str:
    """Quote *s* as an AppleScript string literal."""
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = "".join(c if c >= " " else " " for c in s)
    return f'"{s}"'


def _notify_macos(title: str, message: str) -> None:
    script = (
        f"display notification {_applescript_str(message)} "
        f"with title {_applescript_str(title)}"
    )
    _run_quiet(["osascript", "-e", script])


def _notify_linux(title: str, message: str) -> None:
    _run_quiet(["notify-send", "-a", "Eolith Shell", title, message])


def _notify_windows(title: str, message: str) -> None:
    exe = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
    # The toast is attributed to the PowerShell AppID: an unregistered AppID
    # is silently dropped by the Windows notification platform, and this one
    # is present on every Win10+ install.
    app_id = (
        "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
        "\\WindowsPowerShell\\v1.0\\powershell.exe"
    )
    ps = f"""
$ErrorActionPreference = 'Stop'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $xml.GetElementsByTagName('text')
[void]$texts.Item(0).AppendChild($xml.CreateTextNode({_powershell_str(title)}))
[void]$texts.Item(1).AppendChild($xml.CreateTextNode({_powershell_str(message)}))
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier(
    {_powershell_str(app_id)}).Show($toast)
"""
    _run_quiet([exe, "-NoProfile", "-NonInteractive", "-Command", ps])


def _powershell_str(s: str) -> str:
    """Quote *s* as a PowerShell single-quoted string literal."""
    return "'" + s.replace("'", "''") + "'"


def _notify_bell(title: str, message: str) -> None:
    """Last resort: ring the terminal bell.

    Invisible on screen, so it can't corrupt a picker or a running TUI, and
    every terminal has one.  ``title``/``message`` are deliberately unused —
    printing them would land in the middle of whatever is on screen.
    """
    try:
        sys.__stderr__.write("\a")
        sys.__stderr__.flush()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Shell variables — `var notify=off`, `var notify_threshold=30`
# ---------------------------------------------------------------------------

_TRUTHY = {"on", "true", "yes", "1", "enabled"}
_FALSEY = {"off", "false", "no", "0", "disabled"}


def register_vars() -> None:
    """Register ``notify`` and ``notify_threshold`` with the variable registry.

    Called from ``Shell._register_builtins``.  Both are :class:`GlobalVar` objects —
    process-global rather than per-context, so a context switch neither
    saves nor restores them: "tell me when things finish" is a property of
    the person at the keyboard, not of the AWS account they happen to be
    pointing at.
    """
    from .completion import ChoiceCompleter
    from .variables import GlobalVar, registry as var_registry

    class _NotifyEnabledVar(GlobalVar):
        @property
        def name(self) -> str:
            return "notify"

        @property
        def description(self) -> str:
            return "Desktop notification when a long command finishes (on/off)"

        def get(self) -> str | None:
            return "on" if is_enabled() else "off"

        def set(self, value: str) -> None:
            v = value.strip().lower()
            if v in _TRUTHY:
                set_enabled(True)
            elif v in _FALSEY:
                set_enabled(False)
            else:
                print(f"notify: expected on/off, got {value!r}", file=sys.stderr)

        def unset(self) -> None:
            set_enabled(False)

        @property
        def value_completer(self):
            return ChoiceCompleter(["on", "off"])

    class _NotifyThresholdVar(GlobalVar):
        @property
        def name(self) -> str:
            return "notify_threshold"

        @property
        def description(self) -> str:
            return "Seconds a command must run before it notifies"

        def get(self) -> str | None:
            t = get_threshold()
            return f"{t:g}"

        def set(self, value: str) -> None:
            try:
                seconds = float(value.strip())
            except ValueError:
                print(
                    f"notify_threshold: expected a number of seconds, got {value!r}",
                    file=sys.stderr,
                )
                return
            if seconds < 0:
                print("notify_threshold: must not be negative", file=sys.stderr)
                return
            set_threshold(seconds)

        def unset(self) -> None:
            set_threshold(DEFAULT_THRESHOLD)

        @property
        def value_completer(self):
            return ChoiceCompleter(["5", "10", "30", "60", "300"])

    var_registry.register(_NotifyEnabledVar())
    var_registry.register(_NotifyThresholdVar())
