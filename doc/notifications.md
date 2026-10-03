# Desktop notifications for long-running commands

A command that runs for longer than ten seconds is one you have almost
certainly stopped watching — you switched to a browser, a chat window,
another terminal. pitash posts an OS desktop notification the moment such
a command finishes, so the shell tells you it is done instead of you
polling it.

```
~/projects/pitash 13:18:03> make -j8 release
...
                            ┌──────────────────────────────┐
                            │ ✓ pitash — 1m 23s           │
                            │ make -j8 release             │
                            └──────────────────────────────┘
```

Everything lives in [`src/pitash/notify.py`](../src/pitash/notify.py);
the shell contributes only the two hook sites described below and the
one-shot exit callback in [`process.py`](../src/pitash/process.py).

**Status:** shipped. Foreground lines, `Ctrl+]`-backgrounded processes and
`@bg` bodies all report; macOS / Linux / Windows backends with a terminal-bell
fallback; two shell variables for runtime control.

## Zero dependencies

`notify-py` and friends were considered and rejected: pitash's
`pyproject.toml` declares `dependencies = []` and the whole notification
feature is one subprocess spawn per event. Every backend is a program the
platform already ships, probed once and cached in `_backend_cache`:

| Platform | Backend |
|---|---|
| macOS | `osascript -e 'display notification … with title …'` |
| Linux / BSD | `notify-send -a pitash …` (libnotify), when on `PATH` |
| Windows | a PowerShell WinRT toast (`ToastTemplateType::ToastText02`) |
| fallback | the terminal bell (`\a`) — audible, always available |

Detection order is in `_detect_backend()`. The bell fallback writes to
`sys.__stderr__` and prints nothing: a visible line would land in the middle
of whatever is on screen — a picker, a full-screen TUI — and corrupt it.

Two details worth keeping:

- **The Windows toast is attributed to PowerShell's AppID.** An
  unregistered AppID is silently dropped by the Windows notification
  platform, so the toast uses
  `{1AC14E77-…}\WindowsPowerShell\v1.0\powershell.exe`, which is present on
  every Win10+ install. `CREATE_NO_WINDOW` keeps the helper from flashing a
  console window over the terminal.
- **Both backends interpolate the command line into a script**, so quoting
  is not cosmetic. `_applescript_str` escapes `\` and `"` and replaces
  control characters with spaces; `_powershell_str` doubles single quotes.
  A command containing `"` or `'` must not be able to break out of the
  literal — `make "all"` is an ordinary thing to type.

## Delivery is off the shell's thread, and never raises

`notify()` hands the backend to a daemon thread named `pitash-notify`:
`osascript` takes tens of milliseconds and PowerShell far longer, and the
prompt must not wait for either. The delivery body is wrapped in
`except Exception: pass`, as is the slot-side callback invocation. A shell
that died because a notification daemon was missing would be much worse
than a missed notification.

The callback runs on a reader/worker thread and therefore must not touch
the terminal — which is also why the bell fallback is the only backend that
writes to a stream at all.

## What triggers a notification

`notify.should_notify()` decides *whether* (enabled, long enough, not in the
skip list); the shell decides *when*. There are exactly two reporting sites,
arranged so that no completion is reported twice and none is missed:

**1. A foreground line, timed by `Shell._execute`.** The whole line is timed
— pipes, `&&` chains and all — and reported with the last stage's exit code:

```python
started = time.monotonic()
self._backgrounded = False
try:
    ...
finally:
    if not self._backgrounded:
        notify.command_done(line, time.monotonic() - started, last_exit)
```

The `_backgrounded` flag is the interesting part. `Ctrl+]` and `@bg` both
make `_execute` return long before the work finishes, so the line's own
duration says nothing about it. Those paths set the flag and arm the slot
instead (`_notify_when_backgrounded`).

**2. A backgrounded slot, via its exit callback.** `_notify_slot_done` looks
up which context owns the slot *at exit time* and stays silent unless it
finds one that is not the current context:

```python
owner = next((n for n, c in ... if c.process_slot is slot), None)
if owner is None or owner == self.context_manager.current_name:
    return
```

Both early returns are deliberate. No owner means the slot was never parked
on a context — a plain foreground command, already reported by `_execute`.
Owner == current means the user switched back and is watching it finish on
screen; a popup for something you are looking at is noise. The remaining
case — a context you are not looking at — is the one place where the user is
*provably* elsewhere, and it is reported with the context name in the
message: `[bg-1] make -j8`.

A slot that was backgrounded and then finishes while resumed is reported by
`run()`'s resume paths through `_notify_resumed_done`, without a context
prefix. That is the one case where the reported duration spans time the user
spent watching.

### The arming race

The shell can only arm a slot *after* it has decided the slot went to the
background — by which time the work may already be over.
`ExitCallbackMixin` closes that gap with `_exit_pending`: `_fire_on_exit()`
always records "I ended" even with nothing armed, and `arm_exit_callback()`
fires immediately if it finds that record. A lock plus a `_on_exit_fired`
flag guarantees exactly one of the two paths delivers.

It is a mixin rather than a base class because `PipelineSlot` deliberately
bypasses its parent's `__init__` and hand-mirrors attributes; every slot
type calls `_init_exit_callback()` from its own constructor. The three
call sites for `_fire_on_exit()` are `ProcessSlot._reader_loop`'s `finally`
(after the PTY is closed and the child reaped), and `PythonCommandSlot._run`
/ `PipelineSlot._run` right after `self._finished.set()`.

## The skip list

Quitting `vim` after an hour is not a job completing, and neither is closing
an `ssh` session or a `less`. `SKIP_COMMANDS` holds the basename of the
line's first word for editors, pagers, process monitors, remote sessions,
multiplexers, interactive sub-shells, and `exit`. Matching strips quotes,
normalizes `\` to `/`, takes the basename and drops a `.exe` suffix, so
`/usr/bin/vim`, `'vim'` and `vim.exe` all match `vim`.

This is a heuristic, not a solution — see
[limitations.md](limitations.md#desktop-notifications-can-still-fire-for-an-interactive-command).

## Configuration

At the prompt, through two registered `Var`s (`notify.register_vars()`,
called from `Shell._register_builtins`):

```
pitash> var notify=off                # disable entirely
pitash> var notify_threshold=30       # only notify for commands ≥ 30s
pitash> var notify_threshold=<TAB>    # 5, 10, 30, 60, 300
pitash> var notify                    # → on
```

`notify` accepts `on/true/yes/1/enabled` and `off/false/no/0/disabled`;
anything else prints an error and leaves the setting alone, as does a
non-numeric or negative threshold. `var notify=` disables;
`var notify_threshold=` restores the 10-second default.

Both variables declare no `env_keys`, so they are process-global rather than
per-context: a context switch neither saves nor restores them. "Tell me when
things finish" is a property of the person at the keyboard, not of the AWS
account they happen to be pointing at.

From `~/.pitash/config.py`:

```python
from pitash import notify

notify.configure(enabled=True, threshold=30)
notify.SKIP_COMMANDS.add("psql")        # add to the defaults
notify.configure(skip_commands={"vim"})  # or replace the set wholesale
```

`set_notifier(func)` replaces the platform backend entirely with
`func(title, message)` — for a Slack ping, a `tmux display-message`, an
`ntfy.sh` POST. It runs on the daemon thread and must not write to the
terminal. `set_notifier(None)` restores the built-in chain. The test suite
uses this hook to capture notifications instead of posting them.

## Message format

```
✓ pitash — 1m 23s              make -j8 release
✗ pitash — exit 2 (20.0s)      make
✓ pitash — 1h 05m              [bg-1] terraform apply
```

The title carries the verdict and the duration (`fmt_duration`:
`12.3s` / `1m 23s` / `1h 05m`), the body carries the command with
whitespace collapsed, truncated at 160 characters with an ellipsis, and
prefixed with the context name when it is not `default`.
