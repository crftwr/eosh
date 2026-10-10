# Desktop notifications for long-running commands

A command that runs for longer than ten seconds is one you have almost
certainly stopped watching — you switched to a browser, a chat window,
another terminal. eosh posts an OS desktop notification the moment such
a command finishes, so the shell tells you it is done instead of you
polling it.

```
~/projects/eosh 13:18:03> make -j8 release
...
                            ┌──────────────────────────────┐
                            │ ✓ Eolith Shell — 1m 23s      │
                            │ make -j8 release             │
                            └──────────────────────────────┘
```

Everything lives in [`src/eosh/notify.py`](../src/eosh/notify.py);
the shell contributes only the two hook sites described below and the
one-shot exit callback in [`process.py`](../src/eosh/process.py).

**Status:** shipped. Foreground lines and `Ctrl+]`-backgrounded processes
both report; macOS / Linux / Windows backends with a terminal-bell
fallback; two shell variables for runtime control.

## Zero dependencies

`notify-py` and friends were considered and rejected: eosh's
`pyproject.toml` declares `dependencies = []` and the whole notification
feature is one subprocess spawn per event. Every backend is a program the
platform already ships, probed once and cached in `_backend_cache`:

| Platform | Backend |
|---|---|
| macOS | `osascript -e 'display notification … with title …'` |
| Linux / BSD | `notify-send -a "Eolith Shell" …` (libnotify), when on `PATH` |
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

`notify()` hands the backend to a daemon thread named `eosh-notify`:
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

The `_backgrounded` flag is the interesting part. `Ctrl+]` makes `_execute`
return long before the work finishes, so the line's own duration says
nothing about it. The slot is handed over through one helper, `_park`, which
sets the flag along with `slot.parked`.

**2. A parked slot, via its exit handler.** Every slot is constructed with
`on_exit=Shell._slot_finished`, called once when its work ends (on the slot's
own thread):

```python
if not slot.parked:
    return                      # foreground: _execute timed the whole line
owner = next((n for n, c in ... if c.process_slot is slot), None)
out_of_sight = owner is not None and owner != current
notify.command_done(argv, slot.elapsed(), code,
                    context=owner if out_of_sight else None)
```

A slot that was never parked ran in the foreground and is already covered by
`_execute`. A parked one is always reported — `_execute` returned long ago —
with the context's name in the message when that context isn't the current
one (`[bg-1] make -j8`), the case where the user is *provably* elsewhere. One
that was resumed and watched to the end is reported without the prefix; its
duration then spans time the user spent watching.

### Why the handler is wired at construction

An earlier version armed the callback only once the shell had decided the
slot went to the background — by which time the work could already be over —
so `ExitCallbackMixin` needed a pending-exit record, a lock and a
fired-once flag to make exactly one of two paths deliver, plus a separate
`_notify_resumed_done` path in `run()`. Passing the handler to the
constructor, before anything runs, removes the race: the decision moves to
exit time, where `parked` already says everything.

It is a mixin because `PtySlot` and `PythonCommandSlot` share nothing
else; each calls `_init_exit_callback(on_exit)` from its own constructor.
The two call sites for `_fire_on_exit()` are `PtySlot._reader_loop`'s
`finally` (after the PTY is closed and the child reaped) and
`PythonCommandSlot._run` right after `self._finished.set()`.

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
eosh> var notify=off                # disable entirely
eosh> var notify_threshold=30       # only notify for commands ≥ 30s
eosh> var notify_threshold=<TAB>    # 5, 10, 30, 60, 300
eosh> var notify                    # → on
```

`notify` accepts `on/true/yes/1/enabled` and `off/false/no/0/disabled`;
anything else prints an error and leaves the setting alone, as does a
non-numeric or negative threshold. `var notify=` disables;
`var notify_threshold=` restores the 10-second default.

Both are `GlobalVar`s, so they are process-global rather than
per-context: a context switch neither saves nor restores them. "Tell me when
things finish" is a property of the person at the keyboard, not of the AWS
account they happen to be pointing at.

From `~/.eosh/config.py`:

```python
from eosh import notify

notify.configure(enabled=True, threshold=30)
notify.SKIP_COMMANDS.add("psql")        # add to the defaults
notify.configure(skip_commands={"vim"})  # or replace the set wholesale
```

`set_notifier(func)` replaces the platform backend entirely with
`func(title, message)` — for a Slack ping, a `tmux display-message`, an
`ntfy.sh` POST. It runs on the daemon thread and must not write to the
terminal. `set_notifier(None)` restores the built-in chain. The test suite
uses this hook to capture notifications instead of posting them.

`reload` undoes both before re-running the config (`notify.reset_config`):
the backend goes back to the built-in chain and `SKIP_COMMANDS` to its
defaults. The `notify` / `notify_threshold` values stay, since `var` sets
them at the prompt too.

## Message format

```
✓ Eolith Shell — 1m 23s      make -j8 release
✗ Eolith Shell — exit 2 (20.0s) make
✓ Eolith Shell — 1h 05m      [bg-1] terraform apply
```

The title carries the verdict and the duration (`fmt_duration`:
`12.3s` / `1m 23s` / `1h 05m`), the body carries the command with
whitespace collapsed, truncated at 160 characters with an ellipsis, and
prefixed with the context name when it is not `default`.
