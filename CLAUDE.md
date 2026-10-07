# Eolith Shell

A lightweight but powerful terminal shell environment implemented in Python.

## Before designing features or fixes — read these first

When designing a non-trivial new feature or working out a bug fix, always
consult these two documents *before* proposing a solution:

- [doc/limitations.md](doc/limitations.md) — known limitations of existing
  features. A "small" fix may already be a known gap with broader scope;
  one careful change can often resolve several entries at once.
- [doc/enhancements.md](doc/enhancements.md) — enhancement ideas and
  in-flight design drafts. A new feature may overlap with a planned one,
  or a bug fix may unlock part of an existing draft.

Cross-checking both lets you spot opportunities to solve multiple problems
together instead of stacking point fixes. When you ship something that
touches an entry in either doc, update or remove that entry in the same
change.

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                Eolith Shell (eosh)                  │
├─────────────────────────────────────────────────────┤
│  Shell Loop (shell.py)                              │
│  ├── Input handling (lineedit.py — DIY raw editor)  │
│  ├── Line parsing / pipeline execution              │
│  ├── Command dispatch                              │
│  ├── PTY process multiplexing (process.py)         │
│  └── Context switch TUI (tui.py)                   │
├─────────────────────────────────────────────────────┤
│  Command Registry (commands.py)                     │
│  ├── Built-in commands                             │
│  ├── Python function commands (from config)        │
│  ├── External completer registration               │
│  └── System command passthrough                    │
├─────────────────────────────────────────────────────┤
│  Variable Registry (variables.py)                   │
│  ├── Var ABC — Python-backed shell variables       │
│  ├── VarRegistry + var_registry singleton          │
│  ├── EnvVar convenience subclass                   │
│  └── VarCompleter — KEY=VALUE completion for var   │
├─────────────────────────────────────────────────────┤
│  Completion Engine (completion.py)                  │
│  ├── Command name completion                       │
│  ├── Argument completion (per-command completers)  │
│  ├── Options completion (flags as picker rows)     │
│  ├── HistoryCompleter — past lines, cwd-scoped     │
│  ├── CobraCompleter — <cmd> __complete (opt-in)    │
│  ├── ArgcompleteCompleter — drives argcomplete IPC │
│  └── Filesystem completion (fallback)              │
├─────────────────────────────────────────────────────┤
│  TUI Widgets (tui.py)                               │
│  ├── InlinePicker — single-select inline list      │
│  └── InlineArgPrompt — single-line text input      │
├─────────────────────────────────────────────────────┤
│  Line Editor (lineedit.py)                          │
│  ├── Raw-mode key dispatch                         │
│  ├── History (up/down, Ctrl+R search)              │
│  ├── TAB completion via InlinePicker               │
│  └── Ctrl+] context switch (inline picker)         │
├─────────────────────────────────────────────────────┤
│  Context Manager (context.py)                       │
│  ├── Contexts in most-recently-used order          │
│  ├── Context-aware variable resolution             │
│  ├── CWD save/restore on switch                    │
│  └── env var apply/unapply on switch               │
├─────────────────────────────────────────────────────┤
│  Prompt (prompt.py)                                 │
│  └── Default + user-overrideable prompt function   │
├─────────────────────────────────────────────────────┤
│  Notifications (notify.py)                          │
│  ├── OS notification when a slow command finishes  │
│  ├── Native backends only — osascript / notify-    │
│  │   send / PowerShell toast / terminal bell       │
│  └── var notify, var notify_threshold              │
├─────────────────────────────────────────────────────┤
│  Recipes (recipes/)                                 │
│  └── Completion recipes for external commands      │
├─────────────────────────────────────────────────────┤
│  Add-ons (addons/ → eosh_addons.*)                  │
│  ├── Python applications on the public API only    │
│  └── awsut — AWS utility commands                  │
├─────────────────────────────────────────────────────┤
│  Decorators (decorators/)                           │
│  ├── @name [flags] body — wrap pipeline at runtime │
│  └── Built-ins: @watch, @time, @retry, @quiet, @bg │
├─────────────────────────────────────────────────────┤
│  User Config (~/.eosh/config.py)                   │
│  ├── Custom command definitions                    │
│  └── Custom completer definitions                  │
└─────────────────────────────────────────────────────┘
```

## Module Design

### shell.py — Main Shell Loop

Entry point. Reads input, parses lines, dispatches commands.

- Uses a DIY raw-mode line editor (`lineedit.py`) — no external dependencies
- Supports `Ctrl+R` history search (via inline picker) — searches the
  **global** history (every command from every context)
- Supports `Ctrl+]` to open an inline context-switch picker
- Maintains a global command history in `~/.eosh/history` (every executed
  command, across all contexts). **Up/Down navigation is scoped per context**
  (each `Context` carries an in-memory `history` list); the global file backs
  `Ctrl+R` and seeds the `default` context's Up/Down list at startup. A newly
  created context snapshots its parent's Up/Down list, then they diverge.
  Per-context lists are in-memory only — not persisted across restarts. The
  per-context list also feeds **history TAB completion** (`HistoryCompleter`),
  so TAB recall and Up/Down recall share one scope — with one extra filter on
  the TAB side: candidates are narrowed to the lines that were run in the
  current directory (`~/.eosh/history.dirs`; see `HistoryCompleter`).
- Runs external commands in PTY-backed subprocess slots (`process.py`)
- Executes pipelines (`|`), sequences (`;`, `&&`, `||`), and redirections (`>`, `>>`, `<`, `2>`, `2>&1`)

**Built-in commands:** `cd`, `exit`, `reload`, `var`, `alias`, `unalias`, `source-bash`, `help`, `context`
(`var NAME=` unsets; there is no separate `unset`.)

**`source-bash` — run a bash script, import what it left behind.** The escape
hatch for anything bash can express and eosh can't (`$(…)`, `for`, heredocs,
`if`) and for the common case of *pasting* a block of `export KEY=VALUE` lines:

```
source-bash                 # paste lines, end with a blank line or Ctrl+D
source-bash setup.sh s3     # source a file with arguments
source-bash -c 'export A=1'
```

The body runs in a child `bash` via `passthrough_run` (so a script that prompts
for `sudo` or an MFA code still owns the terminal), and the child's **final
environment and cwd are imported back** — the way bash's own `source` leaves
them in the calling shell. The child writes a NUL-delimited
`cwd\0KEY=VALUE\0…` dump to a temp file from an **EXIT trap** (not a trailing
line), so a script ending in `exit 1` or dying under `set -e` still hands its
environment over; an empty dump means "not imported" rather than "unset
everything". Assignments are applied through `Shell._set_variable` /
`_unset_variable`, so an imported variable is indistinguishable from one set
with `var NAME=VALUE` (Var dispatch + per-context save/restore both apply).
Bash bookkeeping (`_`, `SHLVL`, `PWD`, `OLDPWD`, `BASH_FUNC_*`, …) is skipped,
removal is limited to plain-identifier keys, and the summary line prints
variable **names only** — a sourced script is exactly where an
`AWS_SESSION_TOKEN` comes from. `--no-cd` keeps the current directory, `-q`
suppresses the summary. Shell functions, aliases and shell options cannot come
back (eosh has no equivalent) — see `doc/limitations.md`.

**Ctrl+] context switching:** The user can press `Ctrl+]` at the shell prompt (or during a running process) to open a TUI picker listing all contexts. Selecting a context with a live process resumes it immediately. While the picker is open, the focused context's last few lines of buffered output are previewed below the list, and the following action keys mutate the context list in place: `Ctrl+N` creates a new context (inheriting the current context's variables), `Ctrl+D` deletes the focused context (including the current one — the manager picks the next current automatically), `Ctrl+R` renames the focused context. Action keys refuse to delete a context with a live process or to leave fewer than one context. The shell tracks processes across context switches via `ProcessSlot` (see `process.py`).

### commands.py — Command Registry

```python
from eosh.commands import registry, arg

@registry.command(
    name="hello",
    help="Greet someone by name.",
    params=[arg("name", completer=UsernameCompleter())],
)
def hello(name):
    print(f"Hello, {name}!")
```

Methods:
- `command(name, *, help=None, params=None, delegate=None) -> Command` — register a root. Two forms, both returning the `Command`. Use a **plain call** for a group or an external recipe (`git = registry.command("git", ...)`), and a **decorator** to attach a handler (`@registry.command("hello", ...)` or `name="hello"`). A name is always required. `params=[arg(...)]` declares positionals and flags. argparse parses with that list, and completion reads it on demand (`node.options_completer()`, `node.positional_completer(i)`, `node.takes_value(flag)`); there is no pre-built completer dict. `delegate=Completer` is a `Command` attribute that answers every completion slot, for a tool with its own completion protocol (`aws_completer`, cobra). It can't be combined with `params`.
- `node.command(name, ...)` — the same two forms one level down (see [doc/subcommands.md](doc/subcommands.md)). A node's flags are **its own** and are never inherited from ancestors; a flag shared by several commands is one `arg(...)` listed on each. A node never has both a handler and children: either order raises `ValueError`. A flat command is a root with no children, so completion, the status bar and dispatch all follow the same per-node rules, through `shell._resolve_slot`.
- `sync=True` (on `registry.command`) runs the command on the main thread instead of a backgroundable `PythonCommandSlot` — for commands that finish at once or change shell state; every built-in sets it.
- **A handler's return value is its exit status** — an `int` is the status, anything else (usually `None`) is 0, so `my_cmd && next` sees a failure the handler reports. A `SystemExit` is only a status too (it never ends the shell; `exit` sets `Shell._exit_requested` instead), `KeyboardInterrupt` is 130, an exception is 1 with the traceback on stderr, and an argparse usage error is 2. Every execution path — foreground slot, pipeline stage, `@bg` body, main-thread run — goes through one function, `shell.run_handler`.
- `mark_builtins()` — snapshot current commands as builtins (not removed on `reload`)
- `clear_user_commands()` — remove non-builtin commands and aliases

Dispatch order:
1. Built-in commands (cd, exit, reload, var, unset, help, context)
2. Registered Python function commands
3. System commands (via PTY subprocess)

### variables.py — Variable Registry

Python-backed shell variables that mirror the `CommandRegistry` pattern: register one with `var_registry` and `var NAME=VALUE`, bare `NAME=VALUE` and `$NAME` all go through it. There are exactly two kinds, and the registry refuses anything else (`TypeError`):

```python
class Var(ABC):                      # what `var` lists, reads and completes
    name: str                        # logical name ('aws_region')
    def get(self) -> str | None: ...
    value_completer: Completer | None = None
    description: str = ""

class EnvVar(Var):                   # declarative: a name over os.environ keys
    def __init__(self, name: str, keys: str | Sequence[str] | None = None,
                 completer: Completer | None = None, description: str = ""): ...
    keys: tuple[str, ...]            # defaults to (name,); get() reads keys[0]

class Setting(Var):                  # process-global value, no env key
    def set(self, value: str) -> None: ...   # abstract
    def unset(self) -> None: ...             # default: set("")
```

- **`EnvVar` has no writer of its own.** The shell does every write: `Shell._set_variable` sets each of `keys` through `ContextManager.set_variable`, so they are saved and restored per context like any variable set with `var` — one logical name can drive several keys (`aws_region` → `AWS_REGION` + `AWS_DEFAULT_REGION`).
- **`Setting`** is for values with no environment behind them — `notify`, `notify_threshold`, awsut's endpoint URLs. The shell calls `set()` / `unset()`, and a context switch leaves it alone: it belongs to the person at the keyboard, or to a tool's module state, not to the environment a context carries.

#### Registry

The module-level singleton is named `registry` inside `variables.py` (mirroring `commands.py`). Importers typically alias it as `var_registry` to disambiguate from the command registry:

```python
from eosh.variables import registry as var_registry
# or, equivalently, `from eosh import var_registry`

var_registry.register(var: EnvVar | Setting) -> None
var_registry.get(name: str) -> Var | None
var_registry.all() -> list[Var]
```

#### `var` Command Dispatch

```
var                          → list all env vars + registered Vars with their get() values
var aws_region               → print current value via get()
var aws_region=us-east-1     → EnvVar: the shell sets AWS_REGION and AWS_DEFAULT_REGION
var notify=off               → Setting: notify's set("off")
var AWS_SESSION_TOKEN=abc    → no Var registered: that one key, through the context manager
```

**`$NAME` / `${NAME}` expansion is symmetric with assignment.** `parsing.expand_vars` checks `var_registry` first, then `os.environ`, so a Python-backed variable can be read on the command line just like an env var:

```
var aws_region=us-west-2
aws ec2 describe-instances --region $aws_region   → expands via get()
echo $AWS_REGION                                   → also us-west-2 (both keys were written)
```

The same precedence applies to bare `NAME=VALUE` assignment (e.g. `aws_region=ap-south-1`).

#### `VarCompleter` — `=`-Aware Completion for `var`

Registered as the positional completer on the `var` command. It splits the current token at `=` to handle both phases:

- Typing `var aws_<TAB>` → list registered names (with `=` appended)
- Typing `var aws_region=<TAB>` → delegate to the variable's `value_completer`
- Typing `var aws_region=us-<TAB>` → narrow the value list by prefix

The split is local to `VarCompleter`; the global tokenizer is not changed.

#### Registering variables from `~/.eosh/config.py`

```python
# ~/.eosh/config.py
from eosh import EnvVar, Setting, var_registry
from eosh.completion import CallbackCompleter, ChoiceCompleter

var_registry.register(EnvVar(
    "aws_region", keys=["AWS_REGION", "AWS_DEFAULT_REGION"],
    completer=ChoiceCompleter(["us-east-1", "us-west-2", "eu-west-1"]),
    description="AWS region — sets AWS_REGION + AWS_DEFAULT_REGION",
))
var_registry.register(EnvVar("aws_profile", keys="AWS_PROFILE",
                             completer=CallbackCompleter(list_profiles)))

class Verbosity(Setting):            # a process-global knob, no env key
    name = "verbosity"
    level = "normal"
    def get(self):
        return self.level
    def set(self, value):
        self.level = value or "normal"

var_registry.register(Verbosity())
```

### completion.py — Completion Engine

The core of the TAB completion system. Designed for deep customization.

#### CompletionContext

Every completer receives a `CompletionContext` with full awareness of what's been typed:

```python
@dataclass
class CompletionContext:
    command: str | None        # command name (None if completing command itself)
    args: list[str]            # all preceding arguments (already completed)
    arg_index: int             # which argument position is being completed
    prefix: str               # partial text of current argument being completed
    line: str                 # full raw line
    shell_context: Context    # current shell context (account, region, etc.)
```

#### Completer Protocol

```python
class Completer(ABC):
    @abstractmethod
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        """Return completions for the current position."""
        ...

    def should_activate(self, ctx: CompletionContext) -> bool:
        """Optional guard — return False to skip this completer dynamically."""
        return True

@dataclass
class Completion:
    value: str              # the completion text (inserted into buffer)
    display: str = ""       # optional display label (shown in menu; defaults to value)
    description: str = ""   # optional description (shown beside completion)
    fields: tuple[str, ...] = ()  # description split into picker columns (aligned across rows)
    arg_hint: str = ""           # non-empty for a flag that takes a value ("N"): applying it
                                 # moves straight on to completing that value
    verbatim: bool = False       # True → value may span several tokens; inserted as-is (history)
```

**Metadata columns.** A completer with several facts to show per candidate
returns them as `fields` instead of joining them into one `description` string.
The picker pads each cell to the widest in its column (`Completion.meta` is what
it reads; `tui._meta_col_widths` / `tui._compose_meta` do the layout), so the
facts align down the list and no separator burns width on every row. A column
empty in every row is dropped, so an optional field is free when unused. Facts
that no leaf branches on don't belong here at all — see
[doc/completion.md](doc/completion.md#metadata-columns-fields).

#### Built-in Completers

```python
class FileCompleter(Completer): ...       # filesystem paths (files + dirs)
class DirCompleter(Completer): ...        # directory paths only
class CommandNameCompleter(Completer): ... # registered + system commands, plus cwd dirs / local executables at the command position
class ChoiceCompleter(Completer):          # static list of choices
    def __init__(self, choices: list[str]): ...
class CallbackCompleter(Completer):        # dynamic list from a function
    def __init__(self, func: Callable[[], list[str]]): ...
class HistoryCompleter(Completer):         # tails of past command lines (verbatim=True)
    def __init__(self, history_fn: Callable[[], list[str]], limit: int = 10,
                 ran_here_fn: Callable[[str], bool] | None = None): ...
class OptionsCompleter(Completer):         # flags, one picker row each; value-taking ones carry arg_hint
    def __init__(self, options: dict[str, str],
                 args: dict[str, str | tuple[str, Completer]] | None = None): ...
    def __init__(self, mapping: dict[tuple, Completer]): ...
```

#### Per-Argument Completer Binding

Python commands declare positionals and flags via a single `params=[arg(...)]` list. Each `arg()` configures argparse (validation, defaults, action) **and** TAB completion in one place — `completer=` on a positional drives completion at that position; `completer=` on a value-taking flag drives completion of the value typed after the flag. Completion reads the list on demand. There is no derived completer dict to keep in sync.

```python
@registry.command(
    name="ssh_instance",
    help="SSH into an EC2 instance.",
    params=[
        arg("account", choices=["account-A", "account-B"]),
        arg("region",  completer=RegionCompleter()),
        arg("instance_id", completer=EC2InstanceCompleter()),  # may inspect ctx.args[0]/[1]
        arg("-v", "--verbose", action="store_true", help="verbose"),
        arg("-p", "--port",    type=int, metavar="PORT",
                               help="port number"),
    ],
)
def ssh_instance(account, region, instance_id, verbose=False, port=22): ...
```

External recipes use the same `registry.command()` API as Python commands — they just omit the handler.  `nargs="*"` / `"+"` on a positional makes that completer serve every trailing slot, replacing the old per-index dict spam:

```python
registry.command(
    "git",
    help="distributed version control",
    params=[
        arg("subcommand", choices=["commit", "push", "pull", ...]),
        arg("path", nargs="*", help="repository path", completer=FileCompleter()),
        arg("-v", "--verbose", action="store_true", help="verbose"),
        arg("--no-pager", action="store_true", help="no pager"),
    ],
)
```

One escape hatch on `registry.command()` covers the case where flags and positional dispatch can't be expressed via `params` alone: `delegate=Completer` installs a single completer at **every** slot (flags + every positional index). It's used when an external tool ships its own completion protocol that decides per-call what to return (e.g. `aws_completer`, cobra's `__complete`).

#### OptionsCompleter — Flags as Picker Rows

Flags are ordinary rows in the same `InlinePicker` every other completion uses
— narrowed by typing, chosen with Down/Up + Enter, one flag per TAB. A
value-taking flag is shown as `-d <N>` and carries `arg_hint`. Choosing it
(or a single one auto-applying) inserts `-d ` and `_complete` loops straight
on to the value: the flag's value completer when one is registered (`args`
dict / `completer=` on the `arg`), otherwise nothing. With nothing to offer,
the status bar already reads `-d <N>: …` (`Shell._get_arg_info`), which
is all a separate hint line used to repeat. Combining short flags (`-al`) is
typed by hand; there is no checkbox picker (discussion #36).

#### `HistoryCompleter` — Multi-Argument Candidates from History

Past command lines are offered as TAB candidates at every position, so a
suggestion can span several arguments (`git commit <TAB>` → `-m "fix typo"`).
This is the one completer that *matches* in **line** space instead of token
space: an entry qualifies when it starts with `ctx.line` (everything before the
caret), not merely with `ctx.prefix`. What it returns is anchored like any other
candidate — the entry from `parsing.raw_token_start(ctx.line)` onwards. The values
carry `verbatim=True` because they can run past the current token, and
`lineedit._apply` splices them in at the anchor as-is: no shell-quoting, no
trailing space, text after the caret preserved. On the way to the picker,
`lineedit._align_verbatim_rows` trims each row's *display* (never its value) to
start at the column the picker opens in, so a history row never re-shows text the
user is already looking at — for `cat ~/.aws/<TAB>` a past
`cat ~/.aws/config ~/.aws/credentials` reads `config ~/.aws/credentials` beside
the directory entries, not the whole line over again.

`Shell._get_completions` is a thin wrapper that prepends these to the
completer-driven candidates from `Shell._get_base_completions` (history first —
"what I ran before" is the most likely intent). It draws on the **current
context's** Up/Down history list, so TAB recall matches arrow recall in scope
while `Ctrl+R` stays global.

A history row that would insert exactly what a completer already offers is
dropped in that merge (`shell._drop_history_duplicates`): two rows doing the same
thing is noise, and the completer's is the one carrying a description, so
`awsut sagemaker studio <TAB>` lists `spaces` once (with `List the spaces in a
domain`) instead of twice. The comparison unquotes, so `'My Documents/'` and
`My Documents/` count as the same single token; a tail spanning more than the
token (`spaces --max 5`) is never a duplicate, since spanning arguments is the
whole reason history is in the list.

Candidates are scoped to the **current directory** as well as the current
context. `ran_here_fn(entry)` — wired to `History.ran_here` — answers "was this
line run in the cwd?", and only entries that answer yes are offered, so another
checkout's `make deploy prod` stays out of the way. The scope is strict — there is
no fallback to entries from elsewhere, so a directory you've never run a matching
line in simply contributes no history rows; Up/Down and `Ctrl+R` stay unscoped for
when you do want to reach across directories. The directories come from a JSON
side table (`~/.eosh/history.dirs`, line → recent dirs, capped at
`lineedit.MAX_DIRS_PER_LINE`) that `History.add` maintains next to the plain
`~/.eosh/history` file; a re-run of the same line after a `cd` records the new
directory even though the line itself is a duplicate, and every write prunes
lines the history file no longer holds. Missing or corrupt side table → no
directory is known for any line, so history contributes no TAB candidates at all
(likewise for entries recorded before the side table existed, until they are run
again). See [doc/completion.md](doc/completion.md#historycompleter).

History is deliberately suppressed on an empty line, where bare TAB lists
commands. Flag rows and flag values are ordinary pickers, so history rows join
them (`du -d <TAB>` offers values run here before). And every "exactly one candidate" shortcut in
`lineedit._complete` counts single-token candidates only, so a unique token
completion still auto-applies on the first TAB and a lone history candidate is
always shown in a picker before it inserts several arguments.

TAB *inside* an open picker types the candidates' longest shared prefix. Because
history rows are measured from the raw anchor while token rows are measured from
the shlex-stripped prefix — and because narrowing can drop one kind entirely or
move the anchor across a space — `_complete` recomputes which space to measure in
on every press, via the `extend_fn(items, typed)` callback it hands the picker.
See [doc/completion.md](doc/completion.md) for the full rule table and the
picker-alignment rules.

#### Example: Context-Aware EC2 Completer

```python
class EC2InstanceCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        if len(ctx.args) < 2:
            return []
        account_id = ctx.args[0]
        region = ctx.args[1]
        instances = self._fetch_instances(account_id, region)
        return [
            Completion(
                value=inst["InstanceId"],
                display=inst["InstanceId"],
                description=inst.get("Name", ""),
            )
            for inst in instances
        ]

    def _fetch_instances(self, account_id, region):
        # Call AWS API, cache results
        ...
```

### context.py — Context Switch

Contexts represent an environment (e.g., AWS account + region, k8s cluster). Each context stores:
- `variables: dict[str, str]` — exported to `os.environ` on activation
- `cwd: str` — saved and restored on switch
- `process_slot: ProcessSlot | None` — optional running subprocess for multiplexing
- `state: ContextState` — `IDLE`, `RUNNING`, or `EXITED` (derived from `process_slot`)
- `history: list[str]` — per-context Up/Down command history (in-memory). Seeded
  from the global history file for `default`, or snapshotted from the parent at
  `create()` time; `Ctrl+R` ignores this and searches the global store instead

```python
class ContextManager:
    contexts: dict[str, Context]   # all known contexts by name
    current_name: str | None       # which context is active

    def create(self, name: str, variables: dict | None = None,
               history: list[str] | None = None) -> Context: ...
    def new(self, name: str) -> Context: ...   # create, inheriting current's vars + history
    def switch(self, name: str): ...           # set current to any existing context
    def current(self) -> Context | None: ...
    def list_contexts(self) -> list[str]: ...  # most-recently-used order (current first)
    def remove(self, name: str): ...           # current removed → MRU next becomes current
    def set_variable(self, key, value): ...    # set on current context + os.environ
    def unset_variable(self, key): ...         # remove from current context + os.environ
    def get_variable(self, key) -> str | None: ...
```

Context switching (shell commands):
```
eosh> context new prod
Created context 'prod'
[prod] eosh> var ACCOUNT=123456 REGION=us-east-1
[prod] eosh> context new staging
Created context 'staging'
[staging] eosh> var ACCOUNT=789012 REGION=us-west-2
[staging] eosh> context switch prod
[prod] eosh> context list             # MRU order: prod*, staging, default
[prod] eosh> context kill staging     # send SIGTERM to running process in 'staging'
[prod] eosh> context close staging    # remove it (refused while a process runs)
```

There is no push/pop stack. Its only effect was choosing the next current
context after a removal, and the MRU order answers that (discussion #41).

Completers can use `ctx.shell_context` to adapt:
```python
class EC2InstanceCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        account = ctx.args[0] if ctx.args else ctx.shell_context.get_variable("account")
        region = ctx.args[1] if len(ctx.args) > 1 else ctx.shell_context.get_variable("region")
        ...
```

### lineedit.py — Line Editor

DIY raw-mode line editor. No prompt_toolkit or readline.

- `LineEditor.prompt()` — read one line; returns the line string (not added to history — the shell joins continuation lines and records the result), `CONTEXT_CHANGED_SENTINEL` when a `Ctrl+]` switch needs the new context's process resumed, raises `EOFError` (Ctrl+D on empty) or `KeyboardInterrupt` (Ctrl+C)
- Key bindings: `Ctrl+A/E`, `Ctrl+B/F`, `Alt+B/F`, `Ctrl+W`, `Ctrl+K`, `Ctrl+U`, `Ctrl+L`, arrow keys, `Ctrl+P/N`, `Ctrl+R`
- TAB opens an `InlinePicker` (flags included — one row each) with **no candidate pre-selected**, so Enter dismisses the list instead of inserting the first item; only Down/Up make a selection; typing narrows the list; TAB inside the picker extends the common prefix and never moves the selection; Backspace can close the picker; narrowing to zero candidates closes it (a zero-row picker would be invisible but still eat keys). Characters typed inside a picker are committed to the buffer on every exit path.
- TAB candidates include **past command lines** matching everything typed so far (from the current context's history, scoped to the lines run in the cwd — see `HistoryCompleter`), listed first and tagged `history`. Only the tail from the completion anchor is offered, and applying one splices it in verbatim (`Completion.verbatim`); they are never auto-applied without being shown
- History search (`Ctrl+R`) opens a filterable picker over all history entries
- Multi-line wrapping is tracked so `_redraw()` correctly repositions the cursor after wraps
- VSCode integrated terminal detection: skips reflow-based repositioning, falls back to explicit clear+redraw on resize (`TERM_PROGRAM=vscode`)
- All raw-mode entry and key reading goes through `terminal.py` (not `termios`/`tty`/`select` directly), so the editor runs unchanged on POSIX and native Windows.

### terminal.py — Cross-Platform Terminal Layer

The single place that touches OS-specific terminal APIs. `lineedit.py`, `tui.py`, and the POSIX forwarding loops in `shell.py` drive the terminal through this module so the rendering/key-dispatch code stays platform-agnostic.

- `init()` — one-time setup. On Windows: enables VT output processing (so the ANSI escapes the renderer emits are honoured) and disables Python's `\n`→`\r\n` translation on the std streams. No-op on POSIX.
- `get_mode(fd)` / `set_raw(fd)` / `restore_mode(fd, saved)` — enter/leave raw mode. POSIX: `termios`/`tty` (TCSADRAIN). Windows: toggles `DISABLE_NEWLINE_AUTO_RETURN` so a bare `\n` is a pure line-feed while rendering and reverts to auto-CR for cooked output.
- `read_key(fd)` — block for one *complete logical key* as bytes: a control byte, a full UTF-8 char (all continuation bytes), or a complete escape sequence (`b"\x1b[A"`). POSIX reads via `os.read`+`select`; Windows reads via `msvcrt`, translating the `\x00`/`\xe0` scan-code prefixes into the same ANSI sequences POSIX produces. Callers compare against fixed byte patterns and never re-read the stream.
- `wait_readable(fd, timeout)` — POSIX `select`; Windows polls `msvcrt.kbhit`.
- `install_resize_handler` / `restore_resize_handler` / `HAS_SIGWINCH` — SIGWINCH wiring on POSIX; no-ops on Windows (where the TUI widgets detect resize by polling `terminal_size()` between key reads and cancel, same as they did on SIGWINCH).

**Path separators.** The shell uses `/` as the canonical separator on every platform (like Git Bash / MSYS) — Windows file APIs and executables accept it natively. This keeps `\` free for its POSIX meaning (escaping, `\`-line-continuation), so a path can never be mistaken for a continuation (`cd C:/Users/` not `cd C:\Users\`). `os.path` helpers emit native `\` on Windows, so completer output is normalized with `completion._to_slash` and the prompt renders `/` too. Users type `/` for paths; `\` still escapes as usual.

**Platform support.** The full interactive shell — line editing, completion, all TUI pickers, history, pipelines, redirects, built-ins, and `Ctrl+]` context switching at the prompt — runs natively on both POSIX and Windows. The one POSIX-only piece is **PTY-backed multiplexing of a live external process** (`process.py`'s `ProcessSlot`, the `passthrough_run` PTY, and the `_enter_forwarding_mode` loops): backgrounding a *running* native program via `Ctrl+]` and resuming it. On Windows, external commands run on the real console with inherited stdio (`_execute_external_windows`, with a `cmd /c` fallback for `cmd` builtins like `dir`/`echo`), and Python `@registry.command`s run synchronously on the main thread. Reviving that subsystem on Windows would mean a ConPTY (`CreatePseudoConsole`) backend for `ProcessSlot`.

**Setting up a Windows dev environment** (Python, `make`, POSIX tools, and the `Makefile`'s `2>nul` vs. `2>/dev/null` gotcha) is covered in [doc/windows-setup.md](doc/windows-setup.md).

### tui.py — Inline TUI Widgets

No alternate screen; all rendering anchored with DECSC/DECRC (`ESC 7` / `ESC 8`). On POSIX a resize arrives via SIGWINCH; on Windows it is detected by polling `terminal.terminal_size()` between key reads. Either way the picker cancels (redrawing without an alt-screen is unreliable — the user presses TAB again).

- **`InlinePicker`** — single-select list rendered inline below the current line. Supports narrowing by typing, TAB-extend common prefix (via `value_fn` + `completion_prefix`, or an `extend_fn(items, typed)` callback when the caller must recompute the value space per press), scrollbar, optional `meta_fn` for the labels beside each row (returning either one string or a sequence of cells, which the picker lays out as columns aligned across rows). `select_first=False` (used by the completion pickers) opens with no row highlighted, so Enter returns `None`; `closed_empty` signals "narrowing left zero candidates, I closed myself"; `typed` exposes the characters the picker echoed so the caller can commit them to its buffer.
- **`InlineArgPrompt`** — single-line text prompt (used by the context-switch picker to name or rename a context). Shows an optional description line above.

### process.py — PTY Process Slots (POSIX only)

`ProcessSlot` manages a single PTY-backed subprocess with output buffering, enabling context multiplexing. It depends on `pty`/`fcntl`/`termios` and is never instantiated on Windows (the module still imports cleanly there — the Unix-only imports are guarded — but `_execute_external` takes the inherited-stdio path instead). See the Platform support note under `terminal.py`.

- `start(argv, env, cwd)` — fork + exec in a new PTY; spawns a reader thread
- `activate() / deactivate()` — controls whether output is written to stdout
- `replay_buffer()` — flush buffered output when switching back to a context
- `write_stdin(data)` — forward raw bytes to the subprocess's PTY
- `resize(rows, cols)` — update PTY window size (sends SIGWINCH to child process group)
- `suspend_terminal_modes() / restore_terminal_modes()` — generate escape sequences to undo/redo DEC private modes (alt screen, mouse, app cursor keys) tracked across switches
- `kill()` — send SIGTERM

### Spawning interactive subprocesses from Python commands

A Python `@registry.command` runs in a background thread inside a `PythonCommandSlot` — unless it was registered with `sync=True`, which the built-ins (`cd`, `var`, `context`, `exit`, `source-bash`, `alias`, `help`, …) are: they finish at once or change the shell's own state, so they run on the main thread (no slot, no output proxy, not backgroundable with Ctrl+]), as every Python command does on Windows. While a slot runs, the main thread holds stdin in raw mode and forwards bytes to the slot via `write_stdin`. If the command body calls `subprocess.run([...])` directly, the child inherits the real terminal stdin — and now the main thread *and* the subprocess are both calling `read()` on fd 0. Whoever wins each keystroke gets it; the other sees nothing. Symptoms: dropped keys, garbled input, Ctrl+] sometimes reaches the subprocess.

External commands typed at the prompt (e.g. plain `aws ssm start-session`) don't have this problem because they're routed through `ProcessSlot`, which gives them a dedicated PTY pair. The main thread is the *only* reader of real stdin; it copies bytes into the PTY master.

The fix for Python commands is the same shape: spawn the subprocess against a slot-owned PTY and let the existing forwarding loop do its job. Use `eosh.passthrough_run`:

```python
from eosh import passthrough_run

@registry.command(name="my_ssm", ...)
def my_ssm():
    passthrough_run(["aws", "ssm", "start-session", "--target", target])
```

`passthrough_run` allocates a PTY on the enclosing `PythonCommandSlot`, starts the subprocess against the slave, and spawns a reader thread that copies output to stdout (or buffers it while the context is backgrounded). The main thread keeps reading real stdin in raw mode, intercepts Ctrl+] for context switching, and forwards every other byte to `slot.write_stdin` — which now writes to the PTY master. Ctrl+C is delivered to the subprocess (not the Python thread) while a passthrough subprocess is active. Window resizes propagate via `slot.resize()` → `TIOCSWINSZ` + `SIGWINCH` on the child's process group.

Outside a Python command thread (e.g. inside a synchronous handler that doesn't run on a slot), `passthrough_run` falls through to plain `subprocess.run`.

**Reading input from the user.** `input()` from a Python command body has the same race as `subprocess.run` — the main thread is also reading stdin in raw mode, so most keystrokes are lost and Enter arrives as `\r` with no echo. Use `eosh.passthrough_input(prompt)` for one line (the "Delete? [y/N]" answers, `exit`'s confirmation) and `eosh.passthrough_input_block(prompt)` for a pasted block — lines until a blank line or Ctrl+D, joined by `\n` (`awsut credentials set` takes `export AWS_ACCESS_KEY_ID=…` lines this way).

Both read the **raw key stream**: on a slot, the keys the forwarding loop already feeds it (`slot.poll_key`); on the main thread (a `sync` command), the terminal itself in raw-input / cooked-output mode. `shell._read_typed` does the echo, Backspace / Ctrl+U / Ctrl+W editing, CRLF folding and blank-line detection for both. Ctrl+C raises `KeyboardInterrupt` in the command (the forwarding loop sees `slot._reading_input` and hands Ctrl+C to the reader instead of interrupting), and Ctrl+D on an empty line `EOFError`. Nothing goes through cooked mode, whose canonical line buffer is capped at `MAX_CANON` — 1024 bytes on macOS, where an over-long line is **discarded whole**, which a pasted `AWS_SESSION_TOKEN` line exceeds on its own. A single-line read drops keys typed before the question was asked, so a stray `y` can't answer a delete prompt; a block keeps a paste that landed before its first poll. Without a terminal (and on Windows) both fall back to `input()`.

**When you don't need it.** Three cases that look like subprocesses but don't race for stdin:

1. **Non-interactive subprocesses** (`subprocess.run(..., capture_output=True)`, `$()` substitution, completer queries that shell out to `git`/`docker`/`aws`). The child doesn't read fd 0, so there's no race. Plain `subprocess.run` is fine.
2. **`subprocess.Popen` with explicit pipes/redirections** in pipeline stages. The shell already wires stdin/stdout to file descriptors that aren't the terminal, so the child never touches real stdin.
3. **`pexpect.popen_spawn.PopenSpawn`** (and any other library that drives the child via its own pipe). PopenSpawn passes `stdin=subprocess.PIPE` and writes via `sendline()`, so the child's stdin is owned entirely by the parent process — the user's keystrokes never reach it.

Rule of thumb: if a subprocess spawned from a Python command would, when run standalone in a terminal, read keystrokes from the user (SSH-like sessions, TUIs, MFA prompts, anything that calls `getpass`), wrap it with `passthrough_run`. Otherwise leave it as `subprocess.run`.

### prompt.py — Prompt Function

```python
def set_prompt(func: Callable[[ContextManager], str] | None) -> None: ...
def get_prompt_func() -> Callable[[ContextManager], str]: ...
```

Default prompt shows: `[context] path/cwd HH:MM:SS [bg:N]>` with ANSI colors. The `[context]` prefix is omitted when the context name is `"default"`. `[bg:N]` appears when N other contexts have live processes.

### notify.py — Desktop Notifications for Long Commands

Posts an OS notification when a command that ran for at least
`notify.get_threshold()` seconds (default **10**) finishes — by then the user
has almost certainly switched to another window, and the shell should say
"done" rather than be polled.

**Zero dependencies.** Every backend is a program the platform already ships,
probed once and cached: `osascript` on macOS, `notify-send` on Linux/BSD, a
PowerShell WinRT toast on Windows, and the terminal bell (`\a`) as a last
resort. Delivery happens on a daemon thread (`eosh-notify`) and every
failure is swallowed — a shell must not die because a notification couldn't
be posted, and the callback runs on a reader/worker thread so it never touches
the terminal.

**Two reporting sites, arranged so nothing is reported twice or missed:**

1. `Shell._execute` times the whole foreground line (pipes and `&&` chains
   included) and reports it with the last stage's exit code — unless
   `self._backgrounded` was set, which `Ctrl+]` and `@bg` do because they
   return long before the work finishes.
2. `Shell._slot_finished` — the exit handler every slot is **constructed
   with** — reports a slot that was parked on a context (`Shell._park`, the
   one place Ctrl+] and `@bg` hand a slot over; it sets `slot.parked` and
   `self._backgrounded`). The message carries the owning context's name
   (looked up at exit time) when that context isn't the current one:
   `[bg-1] make -j8`. A slot that was never parked ran in the foreground
   and stays quiet — `_execute` timed it.

`ExitCallbackMixin` (in `process.py`) is the slot-side hook: `mark_started()`
/ `elapsed()` for the duration, the `parked` flag, and `on_exit` — passed to
the constructor and called once at the end of the work. Wired before anything
runs, it can't race the slot's own end. It's a mixin rather than a base class
because `PipelineSlot` deliberately bypasses its parent's `__init__`; every
slot type calls `_init_exit_callback(on_exit)` from its own constructor.

`SKIP_COMMANDS` suppresses commands whose long runtime says nothing about work
finishing (editors, pagers, `top`, `ssh`, `tmux`, interactive sub-shells,
`exit`), matched on the basename of the line's first word.

**Configuration** — two `Var`s registered by `notify.register_vars()` from
`Shell._register_builtins`, plus `configure()` for `config.py`:

```
var notify=off                 # master switch (on/true/yes/1 | off/false/no/0)
var notify_threshold=30        # seconds; unset restores DEFAULT_THRESHOLD
```

Both are `Setting`s, so they are process-global rather than
per-context — "tell me when things finish" belongs to the person at the
keyboard, not to the AWS account they're pointing at.
`notify.set_notifier(func)` replaces the backend entirely (Slack, `ntfy.sh`,
`tmux display-message`); the test suite uses it to capture notifications.

See [doc/notifications.md](doc/notifications.md) for the full design, and
`doc/limitations.md` for the skip-list heuristic's known misses and the
best-effort nature of delivery.

### recipes/ — Completion Recipes for External Commands

Opt-in completion recipes for system commands. Enable in `~/.eosh/config.py`:

```python
from eosh.recipes import enable
enable("make", "git", "ssh", "kill", "tail", "ls", "grep", "find", "du", "df", "aws")
```

Available built-in recipes: `aws`, `chmod`, `chown`, `cobra`, `cp`, `curl`, `df`, `du`, `find`, `git`, `grep`, `kill`, `ls`, `lsof`, `make`, `mv`, `ps`, `rm`, `rsync`, `scp`, `ssh`, `tail`, `tar`, `terraform`, `top`, `unzip`, `zip` (see the `Available recipes:` block in `src/eosh/recipes/__init__.py` for descriptions). Bundled add-ons (`awsut`) are enabled by name the same way (see **addons/** below). Use `enable("*")` to load all built-ins, add-ons and user recipes.

**Cobra-based CLIs — opt-in by name.** `CobraCompleter` drives `<cmd> __complete` for cobra-based CLIs (`docker`, `kubectl`, `helm`, `gh`, `argocd`, …), but only for commands that have been named. The `cobra` recipe lists the well-known ones, and `enable_cobra("mytool")` adds more, from `config.py` or a user recipe's `register()`. Each name becomes a completion-only recipe with the completer as its `delegate`. It is skipped when the name isn't on `PATH` or is already registered. The tool's directive decides whether an empty answer falls back to files. Nothing is ever probed, because finding out whether a tool speaks the protocol means running it with `__complete` as an argument (`touch`, `./deploy.sh`). See `doc/cobra.md`.

**argcomplete fallback** — auto-activates where no completer is registered, no `enable()` required: **`ArgcompleteCompleter`** drives the argcomplete protocol (env vars + fd 8) for Python CLIs marked with `# PYTHON_ARGCOMPLETE_OK` (`pipx`, `conda`, `pre-commit`, `tox`, `pdm`, `httpie`, …). Detection reads the script and never runs it. See `doc/argcomplete-fallback.md`.

Every subprocess run at completion time gets `stdin=subprocess.DEVNULL`, so a misbehaving child can never take over the terminal.

Each recipe calls `registry.command(name, help=..., params=[...])` (with no handler attached) to register completion + flag metadata for an external command.  The shell's dispatch path (`shell.py:_execute`) treats handler-less Commands as external recipes and falls through to the system-command path.

#### User-Defined Recipes

`enable()` searches `recipe_search_path` (a `list[Path]`) when no built-in recipe matches. The default list contains only `~/.eosh/recipes/`; call `add_recipe_path()` to append more directories. The call site in `config.py` is unchanged.

Lookup order for every `enable()` call:

1. Built-in package (`eosh.recipes.<name>`) — always highest priority.
2. Each directory in `recipe_search_path` in order — first match wins.
3. `ImportError` with the searched directories listed if nothing is found.

A user recipe file must define a `register()` function with the same shape as built-in recipes:

```python
# ~/.eosh/recipes/my_tool.py
from eosh.commands import arg, registry
from eosh.completion import CallbackCompleter, ChoiceCompleter

def register():
    registry.command(
        "my-tool",
        help="my-tool — deploy/rollback/status helper",
        params=[
            arg("subcommand", choices=["deploy", "rollback", "status"]),
            arg("target", help="deploy target", completer=CallbackCompleter(_list_targets)),
            arg("-v", "--verbose", action="store_true", help="verbose"),
            arg("--dry-run", action="store_true", help="don't apply changes"),
        ],
    )

def _list_targets():
    return ["web", "worker", "scheduler"]
```

```python
# ~/.eosh/config.py
from eosh.recipes import add_recipe_path, enable

add_recipe_path("/team/shared/recipes")  # optional extra directory
enable("git")          # built-in
enable("my_tool")      # found in ~/.eosh/recipes/ or /team/shared/recipes/
```

`recipe_search_path` is a plain `list[Path]` and can be read or manipulated directly when finer control is needed.

**Missing dependencies.** Under `enable("*")`, a recipe or add-on whose
import fails with `ModuleNotFoundError` (`awsut` without the `eosh[awsut]`
extra, a user recipe importing `requests`) is skipped, recorded in
`recipes.skipped_recipes`, and replaced by a placeholder command of the same
name — unless that name is already registered or on `PATH`, so a
completion-only recipe never shadows the real executable. Running the
placeholder prints one line from `recipes.missing_message`. For an add-on it
names the extra, which by convention is named after the add-on
(`awsut: needs the Python module 'boto3' — install eosh[awsut]`), so the core
holds no table of which add-on needs what. Naming an add-on explicitly
(`enable("awsut")`) raises with the same message.

**User recipe errors are never silent.** Under `enable("*")`, *any* exception
from a user recipe (search-path, not built-in) — including a
`ModuleNotFoundError` raised by a helper it imports, which is as likely a typo
as a missing package — is printed to stderr with a traceback, and the loop
moves on to the next recipe. Only an add-on's missing dependency stays quiet.
Config-load failures print a traceback too. Both go through
`user_errors.format_user_exception`, which drops eosh-internal and
`<frozen importlib>` frames so the report shows the chain through the user's
own files.

### addons/ — Bundled Add-ons

An add-on is a Python *application* that plugs into eosh — a command tree
with handlers, settings and its own output contract — as opposed to a recipe
(completion metadata for an external command). They live at the top of the
repository, one directory each, and install as `eosh_addons.<name>`:

```
addons/awsut/      →  import eosh_addons.awsut      enable("awsut")
```

- **Packaging.** `addons/` has no `__init__.py`; `eosh_addons` is a
  namespace package so an add-on can later become its own distribution
  without renaming. `setup.py` maps `addons/` onto it (the one thing
  `pyproject.toml` can't express). Each add-on has an
  `[project.entry-points."eosh.addons"]` entry in `pyproject.toml`, which is
  how `enable()` finds it. Scanning the namespace finds nothing in an
  editable install. Its third-party dependencies go in an extra **named after
  the add-on**.
- **Public API only.** An add-on imports from `eosh`, `eosh.commands`,
  `eosh.completion`, `eosh.completion_cache`, `eosh.variables`,
  `eosh.recipes` and `eosh.recipes.aws`, and nothing else from the core.
  `tests/addons/test_boundary.py` parses every add-on source to enforce this.
  It also rejects relative imports that leave the add-on, imports of another
  add-on, and add-on directories without an entry point. Something an add-on
  needs that isn't public is made public in the core first.
- **awsut** (`addons/awsut/`) — `whoami`, `console`, `credentials`, `ec2`,
  `logs`, `cloudformation`, `sagemaker {jobs,hub,studio,hyperpod}`,
  `bedrock-agentcore {harness,memory}`. Layout and its output contract
  (`common.py`: `print_header` / `print_table` / `guard` …) are in
  `addons/awsut/README.md`; its tests are in `tests/addons/awsut/`.

See [doc/addons.md](doc/addons.md).

### decorators/ — Pipeline Decorators

A **decorator** is a token of the form `@name [flags]` at the start of a line that wraps the rest of the line as a pipeline and modifies how that pipeline is run. The leading `@` makes the syntax visually distinct from regular commands so parsing priority is unambiguous and the construct doesn't collide with POSIX command names.

```
@watch ls                              # bare single-command body
@watch -n 1 {df -h | grep abc}         # braced body required when operators appear
@watch --no-clear {tail -f log}
```

**Scope rule.** Any pipeline that contains `|`, `;`, `&&`, `||`, or a redirect must be enclosed in `{...}`. The single-command form is allowed without braces. Operators outside braces raise `DecoratorParseError` at parse time with a message pointing at the offending operator.

**Brace handling.** The brace-balancer in `pipeline.py::_find_matching_brace` shares a single scan with the existing quote/escape tracker and handles three things so braces inside the body don't terminate the scope:

1. Single-quoted regions: every char (including `}`) is literal.
2. Double-quoted regions: `"}"` is literal; `${...}` inside is still a balanced span.
3. `${name}` parameter expansion: matched as its own balanced `{...}` so the inner closing brace doesn't decrement the outer counter.
4. Backslash escapes: `\{` and `\}` are literal.

**Authoring a decorator:**

```python
from eosh.commands import arg
from eosh.decorators import registry as decorator_registry

@decorator_registry.decorator(
    name="watch",
    help="Repeatedly run a pipeline until interrupted.",
    params=[
        arg("-n", "--interval", type=float, default=2.0, metavar="SEC"),
        arg("--no-clear", action="store_true"),
    ],
)
def watch(pipeline, *, interval, no_clear):
    while True:
        if not no_clear and sys.stdout.isatty():
            sys.stdout.write("\x1b[2J\x1b[H")
        pipeline.run()
        time.sleep(interval)
```

The decorator function receives a `Pipeline` (the parsed AST of the wrapped body) and the parsed flag namespace as kwargs. `pipeline.run()` re-enters `Shell._execute_pipeline` so redirects, pipes, and Python-stage routing all work the same as at the top level.

**Built-in decorators:** `@watch`, `@time`, `@retry`, `@quiet`, `@bg` (each in its own `eosh/decorators/<name>.py`).

**Loading:** `Shell._register_builtins` calls `enable_decorators("watch", "time", "retry", "quiet", "bg")` on construction. The `enable("*")` helper, search-path mechanism, and `add_decorator_path()` mirror `eosh/recipes/`.

**`@bg` and slot infrastructure.** `@bg` runs its body on a `PipelineSlot` — a subclass of `PythonCommandSlot` whose work unit is a `Pipeline.run()` call instead of a single Python command. It registers itself as the new context's `process_slot`, so the run-loop's existing resume path (proxy buffering, `Ctrl+]` switching, `_compute_exit_code`) handles it without further wiring. The decorator-side hook is `set_background_runner()` in `eosh.decorators` (parallel to `set_pipeline_executor`); `Shell.__init__` registers `_run_in_background` against it.

**Caveats inherited from in-process Python pipelines.** A decorator body is a Python command in everything but syntax, so the constraints from `doc/limitations.md` ("Python commands in pipelines — caveats of the in-process model") apply: nested `subprocess.run` writes to the real terminal unless given `stdout=sys.stdout`, pure-CPU loops can't be `Ctrl+C`-interrupted in a piped context, and `passthrough_run`/`passthrough_input` raise `RuntimeError` from a piped decorator.

See [doc/decorators.md](doc/decorators.md) for the full design rationale, IPython-magic precedent, parser/executor walkthrough, and resolved UX questions; remaining follow-ups (stacking, more built-ins, …) are in [doc/enhancements.md](doc/enhancements.md).

### User Config (~/.eosh/config.py)

Users define custom commands and completers here. Loaded at shell startup; reloadable with the `reload` command.

```python
# ~/.eosh/config.py
from eosh.commands import registry, arg
from eosh.completion import Completer, Completion, ChoiceCompleter
from eosh.recipes import enable

# Enable recipes for system commands
enable("make", "git")

class MyInstanceCompleter(Completer):
    def complete(self, ctx):
        account = ctx.args[0] if ctx.args else ctx.shell_context.get_variable("account")
        # ... fetch and return completions
        return [Completion(value="i-abc123", description="web-server-1")]

@registry.command(
    name="connect",
    help="SSH into an EC2 instance.",
    params=[
        arg("account", choices=["prod", "staging"]),
        arg("region",  choices=["us-east-1", "us-west-2", "eu-west-1"]),
        arg("instance_id", completer=MyInstanceCompleter()),
    ],
)
def connect(account, region, instance_id):
    import os
    os.system(f"ssh {instance_id}")
```

#### Custom Decorators

Custom decorators register the same way commands do — import
`eosh.decorators.registry` and decorate a function. The function
receives the wrapped `Pipeline` as its first positional argument and the
parsed flag namespace as kwargs; call `pipeline.run()` to execute the
body. Return the int exit code (or let the return value of `pipeline.run()`
propagate).

```python
# ~/.eosh/config.py
import sys
import time
from eosh.commands import arg
from eosh.decorators import registry as decorator_registry
from eosh.pipeline import Pipeline

@decorator_registry.decorator(
    name="repeat",
    help="Run the pipeline N times, stopping early on the first failure.",
    params=[
        arg("-n", "--count", type=int, default=3, metavar="N",
            help="number of iterations (default 3)"),
        arg("--delay", type=float, default=0.0, metavar="SEC",
            help="seconds to sleep between iterations"),
    ],
)
def repeat(pipeline: Pipeline, *, count: int, delay: float) -> int:
    last = 0
    for i in range(1, count + 1):
        sys.stderr.write(f"@repeat: iteration {i}/{count}\n")
        last = pipeline.run()
        if last != 0:
            return last
        if delay > 0 and i < count:
            time.sleep(delay)
    return last
```

Usage at the prompt — the brace form is required when the body contains
pipeline operators:

```
eosh> @repeat -n 5 --delay 1 ls
eosh> @repeat -n 3 {make && ./run-tests}
```

For decorators shared across machines or teammates, drop a module under
`~/.eosh/decorators/<name>.py` that defines `register()` (same shape
as the built-ins) and call `enable()` from `config.py`:

```python
# ~/.eosh/config.py
from eosh.decorators import add_decorator_path, enable as enable_decorators

add_decorator_path("/team/shared/decorators")   # optional extra directory
enable_decorators("repeat")                     # found in ~/.eosh/decorators/
                                                # or /team/shared/decorators/
```

## File Layout

```
eosh/
├── CLAUDE.md
├── README.md
├── LICENSE                     # MIT
├── Makefile                    # install/test/run + build and release targets
├── pyproject.toml              # version + readme are dynamic (see Packaging & Release)
├── setup.py                    # package list only: maps addons/ → eosh_addons
├── scripts/
│   ├── install_launcher.py     # put a `eosh` launcher on PATH
│   ├── _version_source.py      # read/rewrite the single __version__ literal
│   ├── bump_version.py         # used by `make tag`
│   ├── release_preflight.py    # refuses a release from a dirty/stale checkout
│   ├── gen_pypi_readme.py      # README.md → README.pypi.md (absolute links)
│   ├── render_banner.py        # `make banner`: doc/images/banner.svg →
│   │                           # banner.jpg + banner-og.jpg (Pages / og:image)
│   └── demo/                   # `make demo`: setup.sh (throwaway HOME) +
│                               # demo.tape (VHS) → doc/images/demo.gif
├── src/
│   └── eosh/
│       ├── __init__.py         # public API exports + __version__ (single source)
│       ├── __main__.py         # entry point (`eosh`, `eosh --version`)
│       ├── paths.py            # config_dir() — the one place naming ~/.eosh
│       ├── user_errors.py      # traceback of a config/recipe error, eosh
│       │                       # frames stripped
│       ├── shell.py            # main loop, command dispatch, pipeline execution
│       ├── commands.py         # command registry, @command decorator
│       ├── variables.py        # Var ABC, VarRegistry, EnvVar, VarCompleter
│       ├── completion.py       # Completer ABC, CompletionContext, built-in completers
│       ├── completion_cache.py # TTL store for completer fetches; invalidated after every command
│       ├── context.py          # Context, ContextManager, ContextState
│       ├── lineedit.py         # DIY raw-mode line editor, History (+ directory side table), TAB completion glue
│       ├── notify.py           # OS notification when a slow command finishes;
│       │                       # native backends, skip list, `notify` +
│       │                       # `notify_threshold` Vars
│       ├── parsing.py          # line tokenization, quote handling, var expansion
│       ├── pipeline.py         # quote-aware operator parser: parse_line(), expand_globs(), decorator extraction, Pipeline.run()
│       ├── process.py          # PTY subprocess slots, output buffering, terminal-mode
│       │                       # tracking, ExitCallbackMixin (one-shot slot-done hook)
│       ├── prompt.py           # set_prompt / get_prompt_func / default_prompt
│       ├── tui.py              # InlinePicker, InlineArgPrompt
│       ├── recipes/
│       │   ├── __init__.py     # enable(*names) helper
│       │   ├── aws.py
│       │   ├── cobra.py       # opt-in list of cobra CLIs + enable_cobra()
│       │   ├── df.py
│       │   ├── du.py
│       │   ├── find.py
│       │   ├── git.py
│       │   ├── grep.py
│       │   ├── kill.py
│       │   ├── ls.py
│       │   ├── make.py
│       │   ├── ssh.py
│       │   └── tail.py
│       └── decorators/
│           ├── __init__.py     # Decorator/DecoratorRegistry/enable(*names)
│           ├── watch.py        # @watch built-in
│           ├── time.py         # @time built-in
│           ├── retry.py        # @retry built-in
│           ├── quiet.py        # @quiet built-in
│           └── bg.py           # @bg built-in
├── addons/                     # bundled add-ons → eosh_addons.<name>
│   │                           # (namespace package: no __init__.py)
│   └── awsut/                  # `awsut` — see addons/awsut/README.md
│       ├── __init__.py         # exposes register()
│       ├── cli.py              # the awsut root + its own leaves
│       ├── common.py           # output contract + AWS-shape helpers
│       ├── sagemaker/          # jobs | hub | studio | hyperpod
│       └── agentcore/          # harness | memory (control + data plane)
└── tests/
    ├── addons/
    │   ├── test_boundary.py    # add-ons use the public API only
    │   └── awsut/
    ├── test_commands.py
    ├── test_completion.py
    ├── test_completion_cache.py
    ├── test_context.py
    ├── test_decorators.py
    ├── test_notify.py
    ├── test_parsing.py
    ├── test_pipeline.py
    ├── test_process.py
    ├── test_recipe_missing.py
    ├── test_recipes.py
    ├── test_shell_continuation.py
    ├── test_user_config.py
    └── test_variables.py

~/.eosh/
├── config.py           # user configuration (commands, completers, recipes)
├── history             # persistent command history
├── history.dirs        # JSON: which directories each history line was run in
├── recipes/            # user-defined recipes (loaded by enable("<name>"))
│   └── <name>.py       # must define register()
└── decorators/         # user-defined decorators (loaded by enable("<name>"))
    └── <name>.py       # must define register()
```

## Shell Operator Support

### Implemented

**Tier 1 — Core (share a single pipeline parser):** ✅ all done
- Pipe `|` — `ls | grep py`
- Stdout redirect `>` `>>` — `make > build.log`
- Stdin redirect `<` — `sort < input.txt`
- Sequencing `;` `&&` `||` — `make && ./run`

**Tier 2 — High value, independent:**
- Glob expansion `*` `?` `**` ✅ — `expand_globs` with `recursive=True` for `**`
- Stderr redirect `2>` `2>>` `2>&1` ✅
- Backslash line continuation `\` ✅ — handled in `shell.py` before execution; continuation lines collected with `"> "` prompt; full joined command stored as one history entry
- Per-command env prefix `FOO=bar cmd args` ✅ — leading `KEY=VALUE` tokens apply only to that command's environment (`Shell._split_env_prefix`). External children get an explicit `env=`. A Python `@registry.command` **refuses** a prefix (status 2, `Shell._env_prefix_refused`): it runs in the shell's own process, whose only environment is the `os.environ` every thread shares, so a temporary change would leak into sibling pipeline stages and outlive a backgrounded run — set the variable with `var` instead. A line that is *only* assignments is still a permanent set; `make FOO=bar` keeps `FOO=bar` as an argument (scan stops at the command name).
- Command substitution `$(…)` ❌ — not yet implemented at the eosh prompt;
  `source-bash` runs a body containing it in a real bash and imports the
  resulting variables, which covers the pasted-snippet case

**Tier 3 — Nice to have:** ❌ none yet
- Background `&` (maps to auto context creation)
- Process substitution `<(cmd)`
- Here-documents `<<EOF`

### Implementation design

Parse order (all quote-aware, implemented in `pipeline.py` and `shell.py`):

```
raw input (one or more physical lines joined in shell.py)
 └─ backslash-continuation joining (shell.py — before any parsing)
     └─ split on ;          → list of statements
         └─ split on && ||  → conditional chain
             └─ split on |  → list of pipeline stages
                 └─ each stage: extract redirections (>, >>, <, 2>, 2>&1)
                     └─ remaining text: expand $VAR, tokenize, glob
```

Two execution modes in `shell.py`:

| Situation | Execution path |
|-----------|---------------|
| Standalone external command (no pipe, no redirect) | PTY via `ProcessSlot` |
| External command in a pipeline | `subprocess.Popen` with plain fds |
| External command with redirect (no pipe) | one-stage pipeline: `subprocess.Popen` with file fds |
| Python `@registry.command` in a pipeline | worker thread per stage; thread-local `sys.stdin`/`sys.stdout`/`sys.stderr` rebound to pipe ends |
| Python `@registry.command` with redirect (no pipe) | one-stage pipeline: worker thread, thread-local `sys.std*` rebound to the redirect files |

A redirected single stage goes through the same loop as a multi-stage pipeline (`_execute_pipeline`). There is no separate redirect path, so the process-global `sys.stdout` is never reassigned, and a background thread printing at the same time can't leak into the redirect target. `_execute_stage` only handles a lone stage with no redirects, which gets the terminal.

The thread-local routing (`_ThreadLocalStdin` / `_ThreadLocalStdout` / `_ThreadLocalStderr` in `shell.py`) is what lets multiple Python pipeline stages run concurrently without trampling each other or the main thread's terminal. Caveats — most importantly that nested `subprocess` from inside a piped Python command bypasses the thread-local rebinding because it reads the real fd 1 — are documented in `doc/limitations.md` under "Python commands in pipelines — caveats of the in-process model."

## Packaging & Release

Published to PyPI as **`eosh`**.
Conventions follow the author's other packages (puikit): setuptools ≥ 77,
`license = "MIT"`, and Makefile + twine with tokens from `~/.pypirc` — no CI.

- **Version** lives only in `src/eosh/__init__.py`'s `__version__`;
  `pyproject.toml` reads it via `dynamic = ["version"]`. Between releases it
  carries a `.devN` suffix (`0.1.0.dev0`), so `make tag VERSION=0.1.0` is
  "ahead" of it for `release_preflight.py`.
- **Extras**: the core is stdlib-only. Each add-on's dependencies are an
  extra named after it. `[awsut]` (boto3, pexpect) powers `awsut`; without it
  `enable("*")` skips `awsut` and records it in `recipes.skipped_recipes`.
  `[dev]` = pytest + `[awsut]`.
- **Add-ons are in the same wheel** (`eosh_addons.*`, via `setup.py`), so
  there is one release pipeline. Adding an add-on means adding its
  `eosh.addons` entry point to `pyproject.toml` and re-running
  `make install`.
- **PyPI readme** is `README.pypi.md`, generated by `make build` from
  `README.md` with relative links pinned to the version tag; gitignored.
- **Release**, in order: `make tag VERSION=x.y.z` (preflight, tests, bump,
  commit, annotated tag `vX.Y.Z`, build gate, push) → `make release-github`
  → `make release-whl` (twine upload of the exact tagged sdist + wheel, also
  attached to the GitHub Release) → `make release-status`.
  `make publish-testpypi` is the no-commitment rehearsal.

## Key Design Decisions

1. **DIY raw-mode line editor** — `lineedit.py` drives the terminal directly with `termios`/`tty`/`select`. This avoids external dependencies, keeps the codebase self-contained, and gives full control over the completion UI and resize handling.

2. **Completer receives full context** — the `CompletionContext` dataclass carries all parsed state so completers can make decisions based on command name, preceding args, and shell context without global state.

3. **`params` is the one source of truth for a command** — argparse parses with it, completion reads flag and positional completers off it on demand, and each node owns its flags (no inheritance down a tree). Flat commands and trees resolve the same way (`Command.resolve` + `shell._resolve_slot`), so TAB completion and the status bar can't drift apart. A completer at position N can inspect `ctx.args[:N]` to see what was already chosen.

4. **Contexts with env+cwd isolation, in most-recently-used order** — `context new` / `switch` / `close`; closing the current context returns to the one used before it. On every switch, context variables are unapplied from `os.environ` then the new context's variables are applied; CWD is saved and restored.

5. **PTY process multiplexing** — each context can hold a `ProcessSlot` with a live subprocess. `Ctrl+]` switches between contexts without killing the running process. The slot buffers output while inactive and replays it on return.

6. **Config as Python** — `config.py` is just Python that imports eosh APIs. No DSL to learn; full language power for defining completers with caching, API calls, etc. The `reload` command reloads the config without restarting the shell.

7. **System command fallback** — anything not registered as a Python command is passed to the system shell via PTY, so eosh is a drop-in replacement for daily use.

8. **Python-backed variables mirror the command registry pattern, with one writer for the environment** — an `EnvVar` only *declares* which `os.environ` keys a logical name stands for (e.g. `aws_region` → `AWS_REGION` + `AWS_DEFAULT_REGION`); the shell writes them, always through the `ContextManager`, so per-context save/restore can never be bypassed. A `Setting` is the separately named kind for process-global values with no env key. The `var` command and bare `NAME=VALUE` assignment dispatch through `VarRegistry` before falling back to plain env writes, and `$NAME` / `${NAME}` expansion does the same lookup in reverse — so registered Vars are read- and write-symmetric with `os.environ` and a Python-backed variable behaves transparently like an OS variable on the command line. `VarCompleter` handles `=`-split completion locally without touching the global tokenizer.

9. **One reader for real stdin** — when a Python command spawns an interactive subprocess, the child must not inherit fd 0 directly. The main forwarding thread is already reading stdin in raw mode; a second reader (the subprocess) splits keystrokes unpredictably between them. `passthrough_run` enforces the rule by allocating a slot-owned PTY for the child, so the chain stays `stdin → main → master → subprocess`. This is the same architecture `ProcessSlot` uses for external commands; `passthrough_run` extends it to subprocesses launched from inside a `PythonCommandSlot`.

10. **Decorators as a sigil-prefixed grammar, not a built-in command** — `@name [flags] body` is parsed *before* the normal pipeline grammar runs (`pipeline.py::_extract_decorator_prefix`), so the syntax is unambiguous to the parser and can never collide with a POSIX command name. Borrowed from IPython's magics (`%name args`); see [doc/decorators.md](doc/decorators.md). The `{...}` body delimiter is required when the wrapped pipeline contains operators, which makes the decorator's scope visible at a glance and side-steps the `watch -n 5 ls | grep abc` ambiguity that POSIX `watch` is famous for. `Pipeline.run()` lets a decorator body re-enter `Shell._execute_pipeline` so redirects/pipes/Python-stage routing all work the same as at the top level.

11. **TTL cache + command-boundary invalidation for completer fetches** — TAB completion runs the completer on every keystroke while the picker is open (see `lineedit.py::refresh_fn`). Completers that hit AWS APIs (e.g. `aws_completer`, `_HyperpodNodeIdCompleter`) would otherwise issue the same boto3 call four or five times for a single typed token. `completion_cache.py` provides `get_or_fetch(key, fn, ttl=60)` with a process-global store. Keys are tuples that include the active `(AWS_PROFILE, AWS_REGION)` via `aws_env_key()` so the cache doesn't bleed across profiles. `Shell._execute()` calls `completion_cache.invalidate_all()` after each pipeline finishes, so a freshly-mutated resource (e.g. after `awsut sagemaker hyperpod scale`) is re-fetched on the next TAB — TTL handles the within-session repeats, the invalidation hook handles correctness across commands.

12. **Notify from the place that knows the work ended, and only from one of them** — the shell has three ways a command can finish (a foreground line, a slot exiting in a context nobody is looking at, a backgrounded slot resumed and watched to completion), and a naive "notify on completion" hook either misses cases or double-reports them. The rule is that `Shell._execute` owns *foreground* timing and steps aside via `self._backgrounded` the moment a line parks its work on a context (`_park`), and the slot's exit handler — wired at construction — owns everything after that, reporting a slot only if it was parked. Backends stay dependency-free (native helper per platform, terminal bell as the floor), fire on a daemon thread so the prompt never waits on a subprocess spawn, and swallow every error — a missed notification is a nuisance, a shell that dies delivering one is a bug. See [doc/notifications.md](doc/notifications.md).
