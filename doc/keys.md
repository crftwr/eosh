# Key bindings

`eosh.keys` lets `config.py` change the keys of the line editor, the
pickers and the context switcher, and bind keys to its own Python
functions that edit the line, the way zsh widgets do (discussion #29).

```python
import shlex
from eosh import keys

keys.bind("prompt.history_search", "Ctrl-S")      # replace an action's keys
keys.bind("picker.next", ["Down", "Ctrl-J"])      # several keys
keys.bind("prompt.clear_screen", [])              # unbind

@keys.action("insert_last_arg", keys="Alt-.")
def insert_last_arg(ctx):
    """Insert the last word of the previous line."""
    if ctx.history:
        ctx.insert(shlex.split(ctx.history[-1])[-1])

@keys.action("pick_branch", keys="Alt-G")
def pick_branch(ctx):
    import subprocess
    out = subprocess.run(["git", "branch", "--format=%(refname:short)"],
                         capture_output=True, text=True).stdout
    choice = ctx.choose(out.split(), title="branch")
    if choice:
        ctx.insert(choice)
```

`help keys` lists every action, its current keys and what it does. Actions
from your config are marked `*`.

## One table, dotted names

Every action has a name made of a surface and a short name. All surfaces share
one namespace, the shape XeFM's `KEY_BINDINGS` uses:

| Surface | What | Examples |
|---|---|---|
| `prompt.*` | the line editor | `prompt.history_search`, `prompt.kill_line` |
| `picker.*` | every inline picker (TAB, Ctrl+R, `ctx.choose`, the switcher) | `picker.next`, `picker.cancel` |
| `switcher.*` | the Ctrl+] context switcher, checked before `picker.*` | `switcher.new` |

The built-in names follow readline's where one exists.

**A name without a surface**, as in XeFM, means every surface that has an
action of that name. A dotted name narrows it to one:

| `keys.bind(…)` | binds |
|---|---|
| `"history_search"` | `prompt.history_search`, the only one |
| `"accept"` | `prompt.accept` and `picker.accept` |
| `"next"` | `picker.next` |
| `"picker.accept"` | `picker.accept` only. It wins there over an `"accept"` entry, whichever was bound first |

`@keys.action(name)` and `ctx.invoke(name)` read a bare name as `prompt.`
(the only surface with user actions, and the one `invoke` runs on). Bind a
user action after defining it: before that, its name is unknown.

`prompt.switch_context` is also the key the shell watches for while a command
runs in the foreground, so rebinding it moves Ctrl+] everywhere.

## Rules

- **`bind` replaces.** `keys.bind(action, keys)` makes *keys* the action's
  keys. Its defaults are gone, and `[]` leaves it with none.
- **A dotted entry beats a bare one** for its surface, whatever the call
  order. This is XeFM's "qualified entry first".
- **The last claim on a key wins.** A key goes to built-in defaults first,
  then to a user action's own `keys=`, then to every `bind` in call order. Each
  later source takes the key from an earlier one. `keys.bind("history_search",
  "Ctrl-N")` takes Ctrl-N away from `next_history`, which keeps Down.
- **Nothing fails silently.** Each of these prints a `config warning:` and the
  rest of the config still runs:
  - an unknown action name (XeFM ignores it)
  - a key name that doesn't parse, such as an unknown modifier (XeFM drops the
    modifier, so `Hyper-X` becomes a bare `X`)
  - a chord the terminal can't send, such as `Ctrl-Shift-A` or `Ctrl-Tab`
  - a printable key, which would stop typing that character. Typing is not an
    action, so it can't be rebound.
- **`reload` resets** every binding and action (`keys.reset()` in
  `Shell._clear_user_config`).
- **No multi-stroke keys** (`Ctrl-G b`), as in XeFM. Readline and zsh users
  rarely use them, and they need key-sequence state in every reader.

## Key names

Modifiers and the key are joined with `-`: `Ctrl-R`, `Alt-.`, `Ctrl-Alt-H`,
`Shift-Tab`, `Ctrl-Left`, `F5`. A trailing `-` is the minus key (`Alt--`).

- **Modifiers:** `Ctrl` (`Control`), `Alt` (`Meta`, `Option`), `Shift`.
  Modifier names and key names are case-insensitive.
- **Letters:** a bare letter is lowercase, so `Alt-B` is ESC `b`. Write
  `Alt-Shift-B` for ESC `B`. Ctrl-letters have no case.
- **Named keys:** `Enter` (`Return`), `Tab`, `Esc`, `Backspace`, `Space`, `Up`,
  `Down`, `Left`, `Right`, `Home`, `End`, `Insert`, `Delete` (`Del`), `PageUp`,
  `PageDown`, `F1`–`F12`.
- **Bytes:** each name becomes the byte sequences `terminal.read_key` returns.
  `Up` covers both the CSI and SS3 forms. A modified arrow or tilde key uses
  xterm's `CSI 1;m X` / `CSI n;m ~` form. `Enter` is CR only (what the key
  sends in raw mode). LF is `Ctrl-J`, which `accept` also has by default, so
  binding `Enter` elsewhere leaves Ctrl-J alone. `Backspace` is DEL and
  Ctrl-H, because terminals send either.

## User actions

`@keys.action(name, keys=…, help=…, override=False)` registers `func(ctx)` as
a `prompt.*` action. `help` defaults to the docstring's first line, which is
what `help keys` shows. Only `prompt.*` actions can be defined for now. XeFM also
starts with one surface (`filer`).

`ctx` is an `EditorContext`:

| | |
|---|---|
| `ctx.buffer` | the line (read-write) |
| `ctx.cursor` | the caret index (read-write, clamped to the line) |
| `ctx.history` | this context's Up/Down history, oldest first (a copy) |
| `ctx.insert(text)` | insert at the caret and move past it |
| `ctx.replace(start, end, text)` | replace `buffer[start:end]`; the caret ends after it |
| `ctx.invoke(name)` | run another action, built-in or user. `"accept"` runs the line once the action returns |
| `ctx.choose(items, title="")` | a picker below the line, filtered by typed keywords; `None` on Esc |

**Wrapping a built-in.** Pass `override=True` to take a built-in's name. Its
default keys carry over unless you give `keys=`. Inside the action,
`ctx.invoke(<its own name>)` runs the built-in, because invoke skips a user
action that is already running and falls through to the built-in, as XeFM's
guard does. The same guard stops mutual recursion between two user actions.

```python
@keys.action("accept", override=True)
def accept_with_sudo_fix(ctx):
    if ctx.buffer.startswith("apt "):
        ctx.buffer = "sudo " + ctx.buffer
    ctx.invoke("accept")
```

**Failures.** An action that raises prints `key action 'name' failed:` with
its traceback (your frames only), the prompt is drawn again below it, and
editing goes on. Ctrl+C and Ctrl+D inside `invoke("interrupt")` /
`invoke("eof")` still do what they do at the prompt.

## Design notes

- **Function calls, not a dict.** XeFM's config is declarative
  (`KEY_BINDINGS = {...}` inside `class Config`). eosh's config already
  registers everything by calling the API (`registry.command`,
  `hooks.on_*`, `var_registry.register`), so keys work the same way. The data
  model underneath is XeFM's: one action→keys table, dotted surfaces, `[]` to
  unbind, user actions bound by name like built-ins.
- **Text input stays outside the table.** Typing a character and the
  `InlineArgPrompt` used to name a context are not actions. XeFM's
  search bar works the same way: printable keys go straight to the text field.
- **Built-ins live with their surface.** `keys.py` holds names, descriptions
  and default keys. The behaviour stays in `LineEditor._builtin_actions`
  and `InlinePicker._ACTIONS`, as XeFM keeps handlers on each surface.

Known gaps are in [limitations.md](limitations.md#key-bindings).
