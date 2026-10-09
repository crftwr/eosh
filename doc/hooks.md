# Event hooks

`eosh.hooks` lets `config.py` run its own functions when something happens
in the shell (discussion #28). Each event is a decorator:

```python
from eosh import hooks

@hooks.on_context_switched
def title(old, new):
    print(f"\x1b]2;[{new}] eosh\x07", end="", flush=True)   # terminal title

@hooks.on_command_not_found
def auto_cd(argv):
    if len(argv) == 1 and os.path.isdir(argv[0]):
        os.chdir(argv[0])
        return True
    return False
```

| Event | Called as | When |
|---|---|---|
| `on_startup` | `f()` | once, after `config.py` loads, before the first prompt |
| `on_exit` | `f()` | when the shell is about to exit (`exit`, Ctrl+D) |
| `on_directory_changed` | `f(old, new)` | the cwd changed, by any means |
| `on_context_switched` | `f(old, new)` | the current context changed |
| `on_command_starting` | `f(line)` | just before a line runs |
| `on_command_finished` | `f(line, status, elapsed)` | a line finished |
| `on_command_not_found` | `f(argv) -> bool` | a command is neither registered nor on `PATH` |

## Rules

- **Registration order.** Hooks run in the order they were registered.
- **A failing hook is contained.** Its exception is printed with a traceback
  through your files (`user_errors.format_user_exception`) and the next hook
  still runs.
- **Observe only**, except `on_command_not_found`: the first hook that
  returns `True` claims the command (the line succeeds with status 0) and no
  later hook is asked. `on_command_starting` can't cancel or rewrite the line.
  If the line that runs differed from the one typed, debugging would be
  miserable; aliases and decorators are the visible way to change it.
- **`reload` forgets every hook** (`Shell._clear_user_config` →
  `hooks.clear()`) before the config runs again. `on_startup` is not
  re-fired.

## Naming

The names are plain English with one `on_` prefix, so `hooks.` completes to
the whole list in an editor. A misspelt event is an `AttributeError` when
`config.py` loads, not a hook that silently never runs. `-ing` / `-ed` say
before / after (`on_command_starting`, `on_command_finished`), as in .NET's
`Closing` / `Closed`. Unlike there, an `-ing` event here can't cancel. The
zsh names (`chpwd`, `preexec`) were rejected: opaque to anyone who doesn't
already know zsh.

## Where the shell fires them

- **Directory and context changes are detected, not hooked.**
  `Shell._notice_state_change` compares `(current context, cwd)` with what it
  saw last and fires `on_context_switched`, then `on_directory_changed`. A
  switch usually changes the cwd too, so both fire, in that order. Comparing
  state catches every path: `cd`, a context switch restoring its cwd,
  `source-bash`, `os.chdir` in a Python command, or a hook. It runs at three
  points:
  1. after each command of a line, so `cd proj && make` reports the move
     before `make` runs;
  2. after a Ctrl+] switch (`_handle_switch`), between the separator and the
     new prompt. The line editor holds the terminal raw there, so the hooks
     run under `_cooked_output()` and can `print` normally;
  3. before each prompt, for whatever a resumed or finished slot changed.
- **`on_command_starting` / `on_command_finished`** are fired by
  `Shell._execute` for a foreground line, the same place that times the line
  for `notify`. A line backgrounded with Ctrl+] is reported by the slot's exit
  handler (`_slot_finished`) when its work actually ends. `_park` records the
  line on the slot (`slot.line`), so the hook still gets the line as typed.
- **`on_command_not_found`** is asked by `Shell._command_not_found`, from the
  paths that run one external command on its own (`_execute_external`, and
  the Windows variant after its `cmd /c` retry).

Known gaps are listed in [limitations.md](limitations.md#event-hooks--what-they-dont-see).

## Relation to `notify`

`notify` still has its own two reporting sites rather than being an
`on_command_finished` hook. Its messages need the owning context and the
skip list, which the hook signature doesn't carry. A custom backend is still
`notify.set_notifier`. Moving notify onto the hook would mean widening the
hook's arguments, which isn't worth it for one consumer.
