# Eolith Shell

A lightweight but powerful terminal shell environment implemented in Python.

## Before designing features or fixes — read these first

When designing a non-trivial new feature or working out a bug fix, always
consult these two sources *before* proposing a solution:

- [doc/limitations.md](doc/limitations.md) — known limitations of existing
  features. A "small" fix may already be a known gap with broader scope;
  one careful change can often resolve several entries at once.
- [GitHub Discussions → Ideas](https://github.com/crftwr/eosh/discussions/categories/ideas) —
  enhancement ideas and in-flight design drafts, one discussion per topic
  (`gh api graphql` to read them; open ones are the live backlog). A new
  feature may overlap with a planned one, or a bug fix may unlock part of
  an existing draft.

Cross-checking both lets you spot opportunities to solve multiple problems
together instead of stacking point fixes. When you ship something that
touches an entry, update or remove the limitations.md entry in the same
change, and tick off or close the discussion.

**Current focus:** optimising conventional terminal work over text
streams. Structured data in pipelines (Nushell / PowerShell-style objects)
is a deliberate non-goal — see issue #13.

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
│  ├── Python-command slots (slots.py)               │
│  ├── Pipeline slots + job leader (job.py)          │
│  └── Context switch TUI (switcher.py, tui.py)      │
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
│  ├── History (up/down, Ctrl+R, ghost suggestion)   │
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
│  Shell Integration (shell_integration.py)           │
│  └── OSC 633 / 133 marks around prompts + commands │
├─────────────────────────────────────────────────────┤
│  Event Hooks (hooks.py)                             │
│  └── @hooks.on_directory_changed, … from config    │
├─────────────────────────────────────────────────────┤
│  Key Bindings (keys.py)                             │
│  ├── prompt.* / picker.* / switcher.* action table │
│  └── keys.bind(), @keys.action() from config       │
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
│  └── Built-ins: @watch, @time, @retry, @quiet      │
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
- Supports `Ctrl+]` to open an inline context-switch picker
- Records every command line in the **shared history** (`history.py`,
  `~/.eosh/history.db`, SQLite — one store for every eosh process; see
  **history.py** below). `Shell._record_history` inserts the row when the line
  starts, and `HistoryStore.finish` adds the exit status and duration when it
  ends (from `_execute`, or from `_slot_finished` for a line parked with Ctrl+],
  via `slot.history_id`). The store feeds the ghost suggestion
  (`Shell._suggest`: the latest line run in the cwd that extends the buffer),
  `Ctrl+R` (every distinct line, starting filtered by the buffer) and the
  `history` built-in. **Up/Down is scoped per context** (each `Context`
  carries an in-memory `history` list): `default`'s is seeded from the store's
  recent lines at startup, and a new context snapshots its parent's.
- Runs external commands in PTY-backed subprocess slots (`process.py`)
- Executes pipelines (`|`), sequences (`;`, `&&`, `||`), and redirections (`>`, `>>`, `<`, `2>`, `2>&1`)

**Built-in commands:** `cd`, `exit`, `reload`, `config` (`config edit`), `var`, `alias`, `unalias`, `source-bash`, `help` (`help keys` lists the key bindings), `context`, `history`
(`var NAME=` unsets; there is no separate `unset`.)

**`reload` is one sweep.** `Shell._clear_user_config` puts every registry a
config writes to back to its built-in state — commands, decorators and
aliases, Vars, `recipes.skipped_recipes`, the prompt, `notify`'s backend and
skip list, `shell_integration.TERMINALS` — and the config runs again. A new kind of config registration
(hooks, key bindings, …) adds its reset there. `config edit` runs
`$VISUAL` / `$EDITOR` (`vi`, `notepad` on Windows) on `config.py` through
`_run_interactive` and reloads when the editor exits 0.

**`source-bash` — run a bash script, import what it left behind.** The escape
hatch for anything bash can express and eosh can't (`$(…)`, `for`, heredocs,
`if`) and for the common case of *pasting* a block of `export KEY=VALUE` lines:

```
source-bash                 # paste lines, end with a blank line or Ctrl+D
source-bash setup.sh s3     # source a file with arguments
source-bash -c 'export A=1'
```

The body runs in a child `bash` via `_run_interactive` (so a script that prompts
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

**Ctrl+] context switching:** The user can press `Ctrl+]` at the shell prompt (or during a running process) to open a TUI picker listing all contexts. Selecting a context with a live process resumes it immediately. While the picker is open, the focused context's last few lines of buffered output are previewed below the list, and the following action keys mutate the context list in place: `Ctrl+N` creates a new context (inheriting the current context's variables), `Ctrl+D` deletes the focused context (including the current one — the manager picks the next current automatically), `Ctrl+R` renames the focused context. Action keys refuse to delete a context with a live process or to leave fewer than one context. The shell tracks running work across context switches with slots (`PipelineSlot` in `job.py`).

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
- `command(name, *, help=None, params=None, delegate=None, sync=False, override=False, pass_context=False) -> Command` — register a root. Two forms, both returning the `Command`. Use a **plain call** for a group or an external recipe (`git = registry.command("git", ...)`), and a **decorator** to attach a handler (`@registry.command("hello", ...)` or `name="hello"`). A name is always required. `params=[arg(...)]` declares positionals and flags. argparse parses with that list, and completion reads it on demand (`node.options_completer()`, `node.positional_completer(i)`, `node.takes_value(flag)`); there is no pre-built completer dict. `delegate=Completer` is a `Command` attribute that answers every completion slot, for a tool with its own completion protocol (`aws_completer`, cobra). It can't be combined with `params`.
- `node.command(name, ...)` — the same two forms one level down (see [doc/subcommands.md](doc/subcommands.md)). A node's flags are **its own** and are never inherited from ancestors; a flag shared by several commands is one `arg(...)` listed on each. A node never has both a handler and children: either order raises `ValueError`. A flat command is a root with no children, so completion, the status bar and dispatch all follow the same per-node rules, through `shell._resolve_slot`.
- `pass_context=True` (on `registry.command` or `node.command`) makes the handler receive a `CommandContext` as its first argument — see **command_context.py** below.
- `sync=True` (on `registry.command`) runs the command on the main thread instead of on its line's backgroundable `PipelineSlot` — for commands that change the whole shell (`context`, `alias` / `unalias`, `reload`, `config`, `exit`) or finish at once (`help`, `history`). `cd`, `var` and `source-bash` don't need it: they change only their own context, through `ctx` (`chdir`, `set_var`, `unset_var`), so one still running after Ctrl+] changes the context it started in (discussion #76). Without a terminal on stdin every Python command runs on the main thread.
- **A handler's return value is its exit status** — an `int` is the status, anything else (usually `None`) is 0, so `my_cmd && next` sees a failure the handler reports. A `SystemExit` is only a status too (it never ends the shell; `exit` sets `Shell._exit_requested` instead), `KeyboardInterrupt` is 130, an exception is 1 with the traceback on stderr, and an argparse usage error is 2. Every execution path — foreground slot, pipeline stage, decorator, main-thread run — goes through one function, `slots.run_handler`.
- `defining_builtins()` — context manager the shell registers its own commands in; exactly what is registered inside it is the built-in set. Afterwards, registering a built-in name without `override=True` prints a `config warning:` and keeps the built-in (the decorator form gets a detached node, so the config runs on). `is_builtin(name)` is false for an override, which `help` lists under "Commands from your config". `mark_builtins()` snapshots everything (for hand-built test registries).
- `clear_user_commands()` — back to the built-in set: drops config commands and aliases and restores any built-in a config overrode

Dispatch order:
1. Built-in commands (cd, exit, reload, var, unset, help, context)
2. Registered Python function commands
3. System commands (via PTY subprocess)

### variables.py — Variable Registry

Python-backed shell variables that mirror the `CommandRegistry` pattern: register one with `var_registry` and `var NAME=VALUE`, bare `NAME=VALUE` and `$NAME` all go through it. There are exactly three kinds — by where the value lives and whether it follows the context — and the registry refuses anything else (`TypeError`):

| Kind | Value lives in | Per context | Used by |
|---|---|---|---|
| `EnvVar` | `os.environ` (child processes see it) | yes | `aws_region`, `aws_profile` |
| `PyVar` | Python (module state; children don't see it) | yes | awsut's endpoint URLs |
| `GlobalVar` | Python | no — process-global | `notify`, `notify_threshold` |

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

class PyVar(Var):                    # per-context value on the Python side
    def set(self, value: str) -> None: ...   # abstract
    def unset(self) -> None: ...             # default: set("")

class GlobalVar(Var):                # process-global value on the Python side
    def set(self, value: str) -> None: ...   # (same surface as PyVar)
    def unset(self) -> None: ...
```

- **`EnvVar` has no writer of its own.** The shell does every write: `Shell._set_variable` sets each of `keys` through `ContextManager.set_variable`, so they are saved and restored per context like any variable set with `var` — one logical name can drive several keys (`aws_region` → `AWS_REGION` + `AWS_DEFAULT_REGION`).
- **`PyVar`** keeps its value on the Python side — out of `os.environ`, so child processes never see it — yet follows the context like an `EnvVar`: `ContextManager` saves `get()` into `Context.py_values` when a context is left and calls `set()` (or `unset()` for `None`) when it is entered again, the way it handles cwd; `context new` / Ctrl+N start from the current value. The shell's `var NAME=VALUE` just calls `set()`. awsut's endpoint URLs are PyVars, so a prod and a staging context can point at different endpoints.
- **`GlobalVar`** has the same surface but ignores context switches — for things that belong to the person at the keyboard, not to the environment a context carries (`notify`, `notify_threshold`).

#### Registry

The module-level singleton is named `registry` inside `variables.py` (mirroring `commands.py`). Importers typically alias it as `var_registry` to disambiguate from the command registry:

```python
from eosh.variables import registry as var_registry
# or, equivalently, `from eosh import var_registry`

var_registry.register(var: EnvVar | PyVar | GlobalVar, *, override=False) -> None   # built-ins need override=True
var_registry.get(name: str) -> Var | None
var_registry.all() -> list[Var]
```

#### `var` Command Dispatch

```
var                          → list all env vars + registered Vars with their get() values
var aws_region               → print current value via get()
var aws_region=us-east-1     → EnvVar: the shell sets AWS_REGION and AWS_DEFAULT_REGION
var notify=off               → GlobalVar: notify's set("off")
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
from eosh import EnvVar, GlobalVar, var_registry
from eosh.completion import CallbackCompleter, ChoiceCompleter

var_registry.register(EnvVar(
    "aws_region", keys=["AWS_REGION", "AWS_DEFAULT_REGION"],
    completer=ChoiceCompleter(["us-east-1", "us-west-2", "eu-west-1"]),
    description="AWS region — sets AWS_REGION + AWS_DEFAULT_REGION",
))
var_registry.register(EnvVar("aws_profile", keys="AWS_PROFILE",
                             completer=CallbackCompleter(list_profiles)))

class Verbosity(GlobalVar):          # a process-global knob, no env key
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
    shell_context: ShellView  # current context, read-only: context_name, cwd, get_var()
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
class OptionsCompleter(Completer):         # flags, one picker row each; value-taking ones carry arg_hint
    def __init__(self, options: dict[str, str],
                 args: dict[str, str | tuple[str, Completer]] | None = None): ...
    def __init__(self, mapping: dict[tuple, Completer]): ...
class OverlayCompleter(Completer):         # base's candidates + each active extra's (deduped)
    def __init__(self, base: Completer, *extras: Completer): ...
```

`OverlayCompleter` is how the `aws` recipe adds what `aws_completer` doesn't
answer — path arguments — without replacing it: `eosh.recipes.aws.S3PathCompleter`
(`s3://bucket/key`, one level per listing through the `aws` CLI, cached per
account) and the filesystem, each gated to the path arguments of
`aws s3 <op>`. `S3PathCompleter` is public for your own commands' arguments;
it is never applied by scheme to every command (discussion #33).

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
- `variables: dict[str, str | None]` — exported to `os.environ` on activation;
  `None` unsets an inherited variable in this context only
- `cwd: str` — saved and restored on switch
- `process_slot` — the line's `PipelineSlot` when it was parked here, or None
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
    def env_value_in(self, ctx, key) -> str | None: ...  # ctx's view, current or not
    lock: threading.RLock      # held by switch/remove/set — a background command
                               # reads and writes its own context's variables
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
        account = ctx.args[0] if ctx.args else ctx.shell_context.get_var("account")
        region = ctx.args[1] if len(ctx.args) > 1 else ctx.shell_context.get_var("region")
        ...
```

### lineedit.py — Line Editor

DIY raw-mode line editor. No prompt_toolkit or readline.

- `LineEditor.prompt()` — read one line; returns the line string (not added to history — the shell joins continuation lines and records the result), `CONTEXT_CHANGED_SENTINEL` when a `Ctrl+]` switch needs the new context's process resumed, raises `EOFError` (Ctrl+D on empty) or `KeyboardInterrupt` (Ctrl+C)
- Key bindings come from `keys.py`: `_handle_key` looks the key up as a `prompt.*` action (`keys.lookup`) and `run_action` runs a user action first, then the built-in from `_builtin_actions` (defaults: `Ctrl+A/E`, `Ctrl+B/F`, `Alt+B/F`, `Ctrl+W`, `Ctrl+K`, `Ctrl+U`, `Ctrl+L`, `Delete`, arrow keys, `Ctrl+P/N`, `Ctrl+R`). Printable keys are never bindable — typing is not an action
- TAB opens an `InlinePicker` (flags included — one row each) with **no candidate pre-selected**, so Enter dismisses the list instead of inserting the first item; only Down/Up make a selection; typing narrows the list; TAB inside the picker extends the common prefix and never moves the selection; Backspace can close the picker; narrowing to zero candidates closes it (a zero-row picker would be invisible but still eat keys). Characters typed inside a picker are committed to the buffer on every exit path.
- **Ghost suggestion** (fish-style): with the caret at the end of a non-empty line, `suggest_fn(buffer)` (the shell's `_suggest`) names a past line that extends it, and `_redraw` draws the rest dimmed after the caret — cut to what fits on the buffer's last row, so it never wraps and the row bookkeeping stays about the buffer. `→` / `Ctrl+F` / `Ctrl+E` / End at the end of the line accept the whole line, `Alt+F` one word. It is off on continuation (`> `) prompts and on the final redraw of a submitted line, and `_erase_ghost` wipes it before a picker opens or on Ctrl+C. TAB never offers past lines.
- History search (`Ctrl+R`) opens a filterable picker over every distinct line in the shared store (`HistoryStore.distinct`), with where / how long ago / failed-status columns. It starts filtered by the buffer (`InlinePicker(typed=...)`) when that fits on the prompt row.
- Multi-line wrapping is tracked so `_redraw()` correctly repositions the cursor after wraps
- VSCode integrated terminal detection: skips reflow-based repositioning, falls back to explicit clear+redraw on resize (`TERM_PROGRAM=vscode`)
- All raw-mode entry and key reading goes through `terminal.py` (not `termios`/`tty`/`select` directly), so the editor runs unchanged on POSIX and native Windows.

### terminal.py — Cross-Platform Terminal Layer

The single place that touches OS-specific terminal APIs. `lineedit.py`, `tui.py`, and the POSIX forwarding loop in `shell.py` drive the terminal through this module so the rendering/key-dispatch code stays platform-agnostic.

- `init()` — one-time setup. On Windows: enables VT output processing (so the ANSI escapes the renderer emits are honoured) and disables Python's `\n`→`\r\n` translation on the std streams. No-op on POSIX.
- `get_mode(fd)` / `set_raw(fd)` / `restore_mode(fd, saved)` — enter/leave raw mode. POSIX: `termios`/`tty` (TCSADRAIN). Windows: toggles `DISABLE_NEWLINE_AUTO_RETURN` so a bare `\n` is a pure line-feed while rendering and reverts to auto-CR for cooked output.
- `read_key(fd)` — block for one *complete logical key* as bytes: a control byte, a full UTF-8 char (all continuation bytes), or a complete escape sequence (`b"\x1b[A"`). POSIX reads via `os.read`+`select`; Windows reads via `msvcrt`, translating the `\x00`/`\xe0` scan-code prefixes into the same ANSI sequences POSIX produces. Callers compare against fixed byte patterns and never re-read the stream.
- `wait_readable(fd, timeout)` — POSIX `select`; Windows polls `msvcrt.kbhit`.
- `install_resize_handler` / `restore_resize_handler` / `HAS_SIGWINCH` — SIGWINCH wiring on POSIX; no-ops on Windows (where the TUI widgets detect resize by polling `terminal_size()` between key reads and cancel, same as they did on SIGWINCH).

**Path separators.** The shell uses `/` as the canonical separator on every platform (like Git Bash / MSYS) — Windows file APIs and executables accept it natively. This keeps `\` free for its POSIX meaning (escaping, `\`-line-continuation), so a path can never be mistaken for a continuation (`cd C:/Users/` not `cd C:\Users\`). `os.path` helpers emit native `\` on Windows, so completer output is normalized with `completion._to_slash` and the prompt renders `/` too. Users type `/` for paths; `\` still escapes as usual.

**Platform support.** The full interactive shell — line editing, completion, all TUI pickers, history, pipelines, redirects, built-ins, and `Ctrl+]` context switching at the prompt — runs natively on both POSIX and Windows. The one POSIX-only piece is **PTY-backed multiplexing of a live external process** (`process.py`'s `PtySlot` and `job.py`'s `PipelineSlot`, the `ctx.run_interactive` PTY, and the `Shell._forward` loop): backgrounding a *running* native program via `Ctrl+]` and resuming it. On Windows, external commands run on the real console with inherited stdio (`_execute_external_windows`, with a `cmd /c` fallback for `cmd` builtins like `dir`/`echo`), and Python `@registry.command`s run synchronously on the main thread. Reviving that subsystem on Windows would mean a ConPTY (`CreatePseudoConsole`) backend for `PtySlot`.

**Setting up a Windows dev environment** (Python, `make`, POSIX tools, and the `Makefile`'s `2>nul` vs. `2>/dev/null` gotcha) is covered in [doc/windows-setup.md](doc/windows-setup.md).

### tui.py — Inline TUI Widgets

No alternate screen; all rendering anchored with DECSC/DECRC (`ESC 7` / `ESC 8`). On POSIX a resize arrives via SIGWINCH; on Windows it is detected by polling `terminal.terminal_size()` between key reads. Either way the picker cancels (redrawing without an alt-screen is unreliable — the user presses TAB again).

- **`InlinePicker`** — single-select list rendered inline below the current line. Supports narrowing by typing, TAB-extend common prefix (via `value_fn` + `completion_prefix`), an initial `typed` query (Ctrl+R starts from the buffer), a `key_source` replacing terminal reads (a command's slot), scrollbar, optional `meta_fn` for the labels beside each row (returning either one string or a sequence of cells, which the picker lays out as columns aligned across rows). `select_first=False` (used by the completion pickers) opens with no row highlighted, so Enter returns `None`; `closed_empty` signals "narrowing left zero candidates, I closed myself"; `typed` exposes the characters the picker echoed so the caller can commit them to its buffer.
- **`InlineArgPrompt`** — single-line text prompt (used by the context-switch picker to name or rename a context). Shows an optional description line above.
- **Colors** come from `colors.get_color_scheme()` (`dark` / `light` / `mono`), painted with `colors.paint(fg, bg)`. A color is RGB or `TERM_FG` / `TERM_BG`; `TERM_BG` text on `TERM_FG` is reverse video. `NO_COLOR` is not a mode — with no `set_color_scheme()` in the config it just selects `mono`. Output outside the widgets checks `eosh.color_enabled(stream)`.

### process.py — PTY Process Slots (POSIX only)

`PtySlot` is work on a PTY eosh owns — the reader thread, output buffering and replay, DEC-mode tracking, `write_stdin` / `resize`; a subclass supplies `_reap`, `_pgid` and `kill`. `job.PipelineSlot` is the subclass every external command runs on (alone or in a pipeline). It depends on `pty`/`fcntl`/`termios` and is never instantiated on Windows (the module still imports cleanly there — the Unix-only imports are guarded — but `_execute_external` takes the inherited-stdio path instead). See the Platform support note under `terminal.py`.

- `activate() / deactivate()` — controls whether output is written to stdout
- `replay_buffer()` — flush buffered output when switching back to a context
- `write_stdin(data)` — forward raw bytes to the subprocess's PTY
- `resize(rows, cols)` — update PTY window size (sends SIGWINCH to child process group)
- `suspend_terminal_modes() / restore_terminal_modes()` — generate escape sequences to undo/redo DEC private modes (alt screen, mouse, app cursor keys) tracked across switches
- `kill()` — send SIGTERM (subclass)

### slots.py — Python Commands on Threads

A Python command runs on a thread of the shell's own process, yet as a stage
of its line's `PipelineSlot` like any external command: its `sys.std*` are the
slot's PTY (or pipes / files). This module is that plumbing, kept out of
`shell.py` so it can be tested on its own; it imports nothing of the shell.

- `_ThreadLocalStream` (installed over `sys.std*` by `install_stdio_routers`)
  — per-thread stdio; `fileno()` / `isatty()` / `buffer` answer for the
  thread's override (a pipe end, a file, a slot's PTY).
- `_PyStageHandle` — a Python stage on its thread. A *graceful* one (a
  decorator, or a command whose stdin and stdout are the terminal) is
  interrupted with `KeyboardInterrupt`; one in a pipe by closing its stdio.
- `run_handler` — a handler's end as an exit status, for every path.
- `_run_interactive` / `_read_from_user` / `_choose` — the bodies of
  `CommandContext`'s methods (below), reaching the user through the stage's
  PTY (`_job_local.job`) or, on the main thread, the terminal.

### command_context.py — What user code sees of the shell

Two objects, so user code never holds a live internal (`Context`, a slot, the
`Shell`):

- **`ShellView`** — read-only: `context_name`, `cwd`, `get_var(name)` (resolved
  like `$name`: registered `Var` first, then the environment). Completers get
  one as `CompletionContext.shell_context`.
- **`CommandContext(ShellView)`** — what a Python command declared with
  `pass_context=True` receives as its **first argument** (ahead of a
  decorator's `Pipeline`; `Command.invoke(args, *lead, ctx=...)` prepends it
  only for a node that opted in). Adds `input(prompt)`, `input_block(prompt)`,
  `confirm(prompt, default=False)`, `choose(items, title="")`,
  `run_interactive(argv, **popen_kwargs)`, `set_var(name, value)`,
  `unset_var(name)`, `chdir(path)`, and `environ()` (the context's whole
  environment, on `ShellView`).
- **`SubshellContext(CommandContext)`** — a pipeline stage's `ctx`: writes
  stay in it (see below).

```python
@registry.command("deploy", params=[arg("env")], pass_context=True)
def deploy(ctx, env):
    target = ctx.choose(list_targets(env), title="Deploy which target?")
    if target is None or not ctx.confirm(f"Deploy {target} to {env}?"):
        return 1
    return ctx.run_interactive(["make", "deploy", f"TARGET={target}"])
```

There are no free functions for this: `passthrough_run` / `passthrough_input`
/ `passthrough_input_block` were replaced by the methods (discussion #30). The
implementations stay private in `slots.py` (`_run_interactive`,
`_read_from_user`, `_choose`), which built-ins such as `source-bash` and
`config edit` call directly.

**Bound to the context the command started in.** The shell builds the
`CommandContext` at dispatch (`Shell._command_context`), on the main thread,
so a command sent to the background with Ctrl+] keeps reading and writing
*its own* context: `cwd` is that context's saved directory once it isn't
current, `get_var` reads its saved values (`ContextManager.env_value_in`,
`Context.py_values`), and `set_var` / `unset_var` / `chdir` write them there —
taking effect when the context is entered again — instead of touching
whatever context is current now. `cd`, `var` and `source-bash` are written
on these, which is why they need no `sync`. A `GlobalVar` is one value everywhere. Reads and writes hold
`ContextManager.lock`, which `switch` / `remove` / `set_variable` also take,
since they happen on the command's thread while the main thread may be
switching.

**A pipeline stage is a subshell.** Each stage of a multi-command pipeline
gets a `SubshellContext` of its own (`command_context.py`, built in
`Shell._execute_pipeline`), as bash runs every stage in a subshell: it reads
as its parent until it writes, and its writes stay in it — the stage and the
programs it starts see them, the context never does. So `cd x | cat` and
`var X=1 | cat` change nothing (discussion #85). A lone stage — redirected
(`cd x > log`) or a decorator body — runs in the shell itself. A decorator
that is a stage gets the subshell too, and its body runs in it
(`_job_local.ctx`, read by `Shell._thread_ctx`).

#### Why the user-facing methods exist: one reader for real stdin

A Python `@registry.command` runs on a thread, as a stage of its line's `PipelineSlot` with the slot's PTY as its `sys.std*` — unless it was registered with `sync=True`, which most built-ins (`context`, `exit`, `alias`, `reload`, `help`, …) are: they change the whole shell or finish at once, so they run on the main thread (no slot, not backgroundable with Ctrl+]), as every Python command does on Windows and without a terminal. While a slot runs, the main thread holds stdin in raw mode and forwards bytes to the PTY master via `write_stdin`. If the command body calls `subprocess.run([...])` or `input()` directly, it reads the real terminal stdin (fd 0 is still the shell's) — and now the main thread *and* the command are both calling `read()` on fd 0. Whoever wins each keystroke gets it; the other sees nothing. Symptoms: dropped keys, garbled input, Ctrl+] sometimes reaches the subprocess.

External commands typed at the prompt (e.g. plain `aws ssm start-session`) don't have this problem because they run on a `PipelineSlot`, which gives them a dedicated PTY pair. The main thread is the *only* reader of real stdin; it copies bytes into the PTY master.

- **`ctx.run_interactive(argv)`** runs the program on the command's slot: the job leader starts it on the slot's PTY, as its controlling terminal, exactly like an external stage. The main thread keeps reading real stdin in raw mode, intercepts Ctrl+] for context switching, and forwards every other byte to the PTY master. Ctrl+C is the program's alone while it runs (`PipelineSlot.interactive()` keeps the Python thread from being interrupted). Window resizes reach it through the slot. On the main thread (a `sync` command) there is no slot and no competing reader, so it is plain `subprocess.run`.
- **`ctx.input` / `ctx.input_block`** read the **raw key stream** of the command's terminal — the slot's PTY, or on the main thread the real terminal — in raw-input / cooked-output mode. `slots._read_typed` does the echo, Backspace / Ctrl+U / Ctrl+W editing, CRLF folding and blank-line detection for both. Ctrl+C raises `KeyboardInterrupt` in the command (with ISIG off the PTY passes it as a key, and the forwarding loop leaves the thread alone), and Ctrl+D on an empty line `EOFError`. Nothing goes through cooked mode, whose canonical line buffer is capped at `MAX_CANON` — 1024 bytes on macOS, where an over-long line is **discarded whole**, which a pasted `AWS_SESSION_TOKEN` line exceeds on its own. On a slot, a single-line read drops keys typed before the question was asked (`tcflush`), so a stray `y` can't answer a delete prompt; a block keeps a paste that landed before it. Without a terminal (and on Windows) both fall back to `input()`.
- **`ctx.choose`** runs an `InlinePicker` on the command's terminal (the slot's PTY: its `sys.stdin` / `sys.stdout`). Ctrl+C sets `picker.interrupted` and becomes `KeyboardInterrupt`. Esc returns `None`. Without a terminal it falls back to a numbered list.
- All of them raise `RuntimeError` in a pipeline stage whose stdin/stdout are pipes (`_in_pipeline.on_terminal` is false).

**When you don't need `run_interactive`.** Three cases that look like subprocesses but don't race for stdin:

1. **Non-interactive subprocesses** (`subprocess.run(..., capture_output=True)`, `$()` substitution, completer queries that shell out to `git`/`docker`/`aws`). The child doesn't read fd 0, so there's no race. Plain `subprocess.run` is fine.
2. **`subprocess.Popen` with explicit pipes/redirections** in pipeline stages. The shell already wires stdin/stdout to file descriptors that aren't the terminal, so the child never touches real stdin.
3. **`pexpect.popen_spawn.PopenSpawn`** (and any other library that drives the child via its own pipe). PopenSpawn passes `stdin=subprocess.PIPE` and writes via `sendline()`, so the child's stdin is owned entirely by the parent process — the user's keystrokes never reach it.

Rule of thumb: if a subprocess spawned from a Python command would, when run standalone in a terminal, read keystrokes from the user (SSH-like sessions, TUIs, MFA prompts, anything that calls `getpass`), run it with `ctx.run_interactive`. Otherwise leave it as `subprocess.run`.

### history.py — Shared Command History

`HistoryStore` over `~/.eosh/history.db` (SQLite, stdlib `sqlite3`): one row per
line run — `cmd`, `cwd` (normcase'd), `ctx`, `ts`, and `status` / `duration`
filled in when it finishes. Every eosh process shares it. Each command is one
`INSERT` plus one `UPDATE`, so concurrent shells never overwrite each other (the
old `history` + rewritten-whole `history.dirs` JSON did), and a line run in one
window is suggested in another at once (discussion #37).

- `add(cmd, cwd, ctx) -> id` / `finish(id, status, duration)` — written by the
  shell (`_record_history`, `_execute`, `_slot_finished`); thread-safe, since a
  parked slot finishes on its own thread.
- `suggest(prefix, cwd)` — the ghost: the most recent single-line entry run in
  *cwd* that strictly extends *prefix*, as an index range on `(cwd, cmd)`
  (`cmd > p AND cmd < p || U+10FFFF`), so `%` / `_` are literal. Not filtered by
  context: names aren't unique across processes (`default` is everywhere).
  Failed lines stay candidates — fixing and re-running is the common case.
- `distinct()` — Ctrl+R's list; `recent_commands()` — seeds `default`'s
  Up/Down; `entries(limit, keywords, cwd)` — the `history` built-in.
- Rollback journal, not WAL (WAL needs shared memory a network home dir may
  lack); 2 s busy timeout; every `sqlite3.Error` swallowed, and an unopenable
  file degrades to `:memory:` — a history must never fail a command. Tests get
  an in-memory store from an autouse fixture in `tests/conftest.py`.

See [doc/history.md](doc/history.md).

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
   `self._backgrounded` was set, which `Ctrl+]` does because it
   returns long before the work finishes.
2. `Shell._slot_finished` — the exit handler every slot is **constructed
   with** — reports a slot that was parked on a context (`Shell._park`, the
   one place Ctrl+] hands a slot over; it sets `slot.parked` and
   `self._backgrounded`). The message carries the owning context's name
   (looked up at exit time) when that context isn't the current one:
   `[bg-1] make -j8`. A slot that was never parked ran in the foreground
   and stays quiet — `_execute` timed it.

`ExitCallbackMixin` (in `process.py`) is the slot-side hook: `mark_started()`
/ `elapsed()` for the duration, the `parked` flag, and `on_exit` — passed to
the constructor and called once at the end of the work. Wired before anything
runs, it can't race the slot's own end. `PtySlot` calls
`_init_exit_callback(on_exit)` from its constructor.

`SKIP_COMMANDS` suppresses commands whose long runtime says nothing about work
finishing (editors, pagers, `top`, `ssh`, `tmux`, interactive sub-shells,
`exit`), matched on the basename of the line's first word.

**Configuration** — two `Var`s registered by `notify.register_vars()` from
`Shell._register_builtins`, plus `configure()` for `config.py`:

```
var notify=off                 # master switch (on/true/yes/1 | off/false/no/0)
var notify_threshold=30        # seconds; unset restores DEFAULT_THRESHOLD
```

Both are `GlobalVar`s, so they are process-global rather than
per-context — "tell me when things finish" belongs to the person at the
keyboard, not to the AWS account they're pointing at.
`notify.set_notifier(func)` replaces the backend entirely (Slack, `ntfy.sh`,
`tmux display-message`); the test suite uses it to capture notifications.

See [doc/notifications.md](doc/notifications.md) for the full design, and
`doc/limitations.md` for the skip-list heuristic's known misses and the
best-effort nature of delivery.

### keys.py — Key Bindings

One action→keys table for every surface, in XeFM's shape: dotted names
(`prompt.history_search`, `picker.next`, `switcher.new`), one namespace,
`[]` to unbind, user actions bound by name like built-ins (discussion #29).

```python
from eosh import keys

keys.bind("prompt.history_search", "Ctrl-S")   # replaces its keys; [] unbinds
keys.bind("insert_last_arg", "Alt-.")         # before or after the definition

@keys.action("insert_last_arg")
def insert_last_arg(ctx):                     # ctx: lineedit.EditorContext
    ctx.insert(ctx.history[-1].split()[-1])
```

- **Where the behaviour lives.** `keys.py` holds names, descriptions and default
  keys only. Behaviour stays on each surface: `LineEditor._builtin_actions`,
  `InlinePicker._ACTIONS`, and `Shell._show_switch_menu` (its `key_actions`
  come from `keys.sequences("switcher.*")` and are checked before `picker.*`).
  The forwarding loop (`Shell._forward`) finds `prompt.switch_context` in raw input
  with `shell._find_switch_key`.
- **Names in `bind`.** A bare name (`"accept"`) binds that action on every
  surface that has one (`prompt.accept` and `picker.accept`). A dotted name
  binds one surface and wins there over the bare entry, whatever the call
  order, as in XeFM. `@keys.action` and `ctx.invoke` read a bare name as
  `prompt.`.
- **Defining and binding are separate** (XeFM's `ACTIONS` / `KEY_BINDINGS`).
  `@keys.action` takes no keys; `bind` only records the name, so it may
  precede the definition. `_load_user_config` calls `keys.check_bindings()`
  after the config runs, which warns about and drops names that don't exist.
- **Which binding wins.** Defaults first, then every `bind()` in call order;
  each later one takes the key. An `override=True` action keeps the
  built-in's keys.
- **Errors.** Unknown action names, unparseable key names, chords the terminal
  can't send and printable keys are each a `config warning:`. An unknown
  modifier is refused, never dropped. `reload` → `keys.reset()`.
- **`EditorContext`** has `buffer`, `cursor` (both read-write), `history`,
  `insert`, `replace`, `invoke(name)` and `choose(items, title)`. `invoke` skips
  a user action that is already running (XeFM's re-entry guard), so an
  `override=True` action reaches its built-in by its own name.
  `invoke("accept")` finishes the line. A raising action is reported below
  the line and editing goes on. Only `prompt.*` actions can be user-defined.
- **Key names** (`Ctrl-R`, `Alt-.`, `Alt-Shift-B`, `Shift-Tab`, `Ctrl-Left`,
  `F5`) parse to the bytes `terminal.read_key` returns. There are no
  multi-stroke keys. `help keys` lists the table (`Shell._print_key_bindings`).

See [doc/keys.md](doc/keys.md); gaps are in `doc/limitations.md`.

### shell_integration.py — Prompt and Command Marks

Tells the terminal where each prompt, command line and output starts —
VS Code's `OSC 633` or the FinalTerm `OSC 133` that iTerm2, WezTerm,
Ghostty, kitty, foot and Windows Terminal read — so sticky scroll,
jump-to-prompt and the exit-status gutter see each eosh line as a command.
Without it, VS Code (which injects its own marks into the bash / zsh it
starts) sees `zsh% eosh` as one command whose output is the whole session
(issue #26).

```
osc633:  A <prompt> B <line> [F "> " G …]  E;<line> C <output> D;<status> P;Cwd=…
osc133:  A <prompt> B <line>                        C <output> D;<status> OSC 7 file://host/cwd
```

- `prompt_marks(continuation)` — `LineEditor.prompt` takes them once and
  `_redraw` writes them around the prompt on every redraw, outside
  `_prompt_str` so the width math never sees them.
- `command_started(line)` / `command_finished(status)` — from
  `Shell._run_loop` around `_execute`, which returns the line's status (or
  `None` when Ctrl+] parked it). A line that ran nothing (empty, Ctrl+C, a
  switch) is closed at the top of the next iteration, as zsh does: `C` and a
  `D` without a status. Marks are closed in the dialect they were opened in.
- **The cwd** follows every `D`, and `startup()` sends it before the first
  prompt, so a new tab or split opens in eosh's directory: `P;Cwd` in VS
  Code, `OSC 7` (`file_url`: host + percent-encoded path) for the OSC 133
  terminals, plus Windows Terminal's `OSC 9;9` on native Windows.
- **Allowlist, not opt-out.** Nushell and fish send `OSC 133` everywhere;
  fish printed garbage in Termux / Guacamole / noVNC that way (fish#11749),
  and here a stray mark would also break the caret math. `TERMINALS` is a
  list of `(env var, value or None, dialect)` rules, first match wins, VS
  Code first. A config appends to it (`reload` restores the defaults, as
  `notify.SKIP_COMMANDS`). Nothing unless stdout is a tty.
- `var shell_integration=auto|osc133|osc633|off` (a `GlobalVar`; unset →
  `auto`) forces a dialect or turns marks off. Gaps are in
  `doc/limitations.md`, follow-ups in discussion #69.

### hooks.py — Event Hooks

`config.py` reacts to shell events with one decorator per event:
`on_startup`, `on_exit`, `on_directory_changed(old, new)`,
`on_context_switched(old, new)`, `on_command_starting(line)`,
`on_command_finished(line, status, elapsed)`, `on_command_not_found(argv) -> bool`.
Plain-English names with a single `on_` prefix; `-ing` / `-ed` mean
before / after. A misspelt event is an `AttributeError` at load time.

- **Rules:** registration order; a raising hook is reported
  (`format_user_exception`) and the rest still run; hooks only observe,
  except `on_command_not_found`, where the first `True` claims the command
  (status 0) and stops the chain; `reload` → `hooks.clear()` (in
  `Shell._clear_user_config`), and `on_startup` isn't re-fired.
- **Directory / context are detected, not hooked:**
  `Shell._notice_state_change` diffs `(current context, cwd)` against the
  last look and fires context first, then directory. It's called after each
  command of a line (`cd x && make` reports before `make`), after a Ctrl+]
  switch in `_handle_switch` (under `_cooked_output()`, since the line editor
  holds the terminal raw), and before each prompt.
- **Starting / finished** come from `Shell._execute` for a foreground line.
  A parked slot is reported by `_slot_finished` when its work ends, on the
  slot's thread, with the line `_park` stored in `slot.line`. Same split as
  `notify`, which still has its own reporting sites.
- **Not found** goes through `Shell._command_not_found`, from the
  single-external-command paths only (not pipeline stages).

See [doc/hooks.md](doc/hooks.md); gaps are in `doc/limitations.md`.

### recipes/ — Completion Recipes for External Commands

Opt-in completion recipes for system commands. Enable in `~/.eosh/config.py`:

```python
from eosh.recipes import enable
enable("make", "git", "ssh", "kill", "tail", "ls", "grep", "find", "du", "df", "aws")
```

Available built-in recipes: `aws`, `chmod`, `chown`, `cobra`, `cp`, `curl`, `df`, `du`, `find`, `git`, `grep`, `kill`, `ls`, `lsof`, `make`, `mv`, `ps`, `rm`, `rsync`, `scp`, `ssh`, `tail`, `tar`, `terraform`, `top`, `unzip`, `zip` (see the `Available recipes:` block in `src/eosh/recipes/__init__.py` for descriptions). Bundled add-ons (`awsut`) are enabled by name the same way (see **addons/** below). Use `enable("*")` to load every built-in recipe and bundled add-on.

**Cobra-based CLIs — opt-in by name.** `CobraCompleter` drives `<cmd> __complete` for cobra-based CLIs (`docker`, `kubectl`, `helm`, `gh`, `argocd`, …), but only for commands that have been named. The `cobra` recipe lists the well-known ones, and `enable_cobra("mytool")` adds more, from `config.py` or a module it imports. Each name becomes a completion-only recipe with the completer as its `delegate`. It is skipped when the name isn't on `PATH` or is already registered. The tool's directive decides whether an empty answer falls back to files. Nothing is ever probed, because finding out whether a tool speaks the protocol means running it with `__complete` as an argument (`touch`, `./deploy.sh`). See `doc/cobra.md`.

**argcomplete fallback** — auto-activates where no completer is registered, no `enable()` required: **`ArgcompleteCompleter`** drives the argcomplete protocol (env vars + fd 8) for Python CLIs marked with `# PYTHON_ARGCOMPLETE_OK` (`pipx`, `conda`, `pre-commit`, `tox`, `pdm`, `httpie`, …). Detection reads the script and never runs it. See `doc/argcomplete-fallback.md`.

Every subprocess run at completion time gets `stdin=subprocess.DEVNULL`, so a misbehaving child can never take over the terminal.

Each recipe calls `registry.command(name, help=..., params=[...])` (with no handler attached) to register completion + flag metadata for an external command.  The shell's dispatch path (`shell.py:_execute`) treats handler-less Commands as external recipes and falls through to the system-command path.

**Recipes don't check `PATH` themselves.** After a recipe's `register()`, `enable()` drops every *completion-only* command it added whose name isn't on `PATH` (`recipes._drop_absent_tools`) — so `enable("tar")` without `tar` is a no-op, and the `grep` recipe keeps `egrep` but not a missing `rgrep`. A command with a Python handler (an add-on's) always stays.

#### Your Own Recipes

There is no recipe search path: `enable()` covers the built-in recipes and
the bundled add-ons only. A recipe of your own is plain Python — call
`registry.command(...)` in `config.py`, or in a module it imports.
`~/.eosh` is on `sys.path` while `config.py` runs (as a script's own
directory is), so `~/.eosh/my_tools.py` is `import my_tools`; a team
directory is a `sys.path.append(...)` away. On `reload`, modules imported
from `~/.eosh` are forgotten first so they run — and register — again. An
error in one is a config-load error, printed with a traceback through your
own files (`user_errors.format_user_exception` drops eosh-internal and
`<frozen importlib>` frames).

```python
# ~/.eosh/my_tools.py
from eosh.commands import arg, registry
from eosh.completion import CallbackCompleter

registry.command(
    "my-tool",
    help="my-tool — deploy/rollback/status helper",
    params=[
        arg("subcommand", choices=["deploy", "rollback", "status"]),
        arg("target", help="deploy target",
            completer=CallbackCompleter(lambda: ["web", "worker", "scheduler"])),
        arg("--dry-run", action="store_true", help="don't apply changes"),
    ],
)
```

```python
# ~/.eosh/config.py
import sys
sys.path.append("/team/shared/eosh")    # optional: a shared directory
from eosh.recipes import enable

enable("git")       # built-in
import my_tools     # your own, from ~/.eosh
```

**Missing dependencies.** Under `enable("*")`, an add-on whose
import fails with `ModuleNotFoundError` (`awsut` without the `eosh[awsut]`
extra) is skipped, recorded in
`recipes.skipped_recipes`, and replaced by a placeholder command of the same
name — unless that name is already registered or on `PATH`, so a
completion-only recipe never shadows the real executable. Running the
placeholder prints one line from `recipes.missing_message`. For an add-on it
names the extra, which by convention is named after the add-on
(`awsut: needs the Python module 'boto3' — install eosh[awsut]`), so the core
holds no table of which add-on needs what. Naming an add-on explicitly
(`enable("awsut")`) does the same and prints that message as a
`config warning:`. No name stops `enable()` or the config: an unknown name
or a `register()` that raises is a `config warning:` (via
`user_errors.config_warning`, with the traceback for the latter) and the
remaining names still load.

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
  `eosh.completion`, `eosh.completion_cache`, `eosh.hooks`, `eosh.variables`,
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

**Scope rule.** Any pipeline that contains `|`, `;`, `&&`, `||`, or a redirect must be enclosed in `{...}`. The single-command form is allowed without braces (a redirect there binds to the body: `@time make > log`). Operators outside braces raise `DecoratorParseError` at parse time with a message pointing at the offending operator. After a braced scope the line goes on: `@deco {…} | next`, `@deco {…} && other`, and a decorator may start any item of a sequence (`make; @time {…}`) — `parse_line` takes one item at a time. Decorators stack outside in: `@time @retry -n 3 cmd` is @time around @retry.

**Brace handling.** The brace-balancer in `pipeline.py::_find_matching_brace` shares a single scan with the existing quote/escape tracker and handles three things so braces inside the body don't terminate the scope:

1. Single-quoted regions: every char (including `}`) is literal.
2. Double-quoted regions: `"}"` is literal; `${...}` inside is still a balanced span.
3. `${name}` parameter expansion: matched as its own balanced `{...}` so the inner closing brace doesn't decrement the outer counter.
4. Backslash escapes: `\{` and `\}` are literal.

**A decorator is a command** named `@name` in the one command registry
(discussion #40): argparse parses its flags, completion treats them like any
command's (`_resolve_slot`), `reload` clears config-defined ones with the other
user commands, and `help` lists them under their own heading. The `@` keeps
them apart from commands — `@time` and the system `time` never collide, and
command-name completion leaves `@` names out. The handler gets the wrapped
`Pipeline` ahead of the parsed flags (`Command.invoke(args, pipeline)`).

**Authoring a decorator:**

```python
from eosh.commands import arg, registry

@registry.command(
    "@watch",
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

**Built-in decorators:** `@watch`, `@time`, `@retry`, `@quiet` (each in its own `eosh/decorators/<name>.py`). There is no `@bg` (removed in discussion #39): to background something, run it and press `Ctrl+]` — a whole pipeline goes with it (`PipelineSlot`), and so does a lone decorator, which runs on one too. When a body ends with 130 (Ctrl+C), `pipeline.run()` raises `KeyboardInterrupt`: `@watch` stops, `@retry` doesn't retry, `@time` still reports (discussion #76).

**Loading:** `Shell._register_builtins` calls `eosh.decorators.register_builtins()` inside `defining_builtins()`, so the built-ins survive `reload` and a config needs `override=True` to replace one. There is no decorator search path; your own are defined in `config.py` (or a module it imports), like a recipe.

**Caveats inherited from in-process Python pipelines.** A decorator body is a Python command in everything but syntax, so the constraints from `doc/limitations.md` ("Python commands in pipelines — caveats of the in-process model") apply: nested `subprocess.run` writes to the real terminal unless given `stdout=sys.stdout`, pure-CPU loops can't be `Ctrl+C`-interrupted in a piped context, and `ctx.run_interactive` / `ctx.input` / `ctx.choose` raise `RuntimeError` from a piped decorator. A lone decorator on a `PipelineSlot` is not "piped": its stdin and stdout are the slot's PTY, so they work (`_in_pipeline.on_terminal`).

See [doc/decorators.md](doc/decorators.md) for the full design rationale, IPython-magic precedent, parser/executor walkthrough, and resolved UX questions; remaining follow-ups (more built-ins) are in [discussion #67](https://github.com/crftwr/eosh/discussions/67).

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
        account = ctx.args[0] if ctx.args else ctx.shell_context.get_var("account")
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

A decorator is a command named `@name` — register it with
`registry.command("@name", ...)` and decorate a function. The function
receives the wrapped `Pipeline` as its first positional argument and the
parsed flag namespace as kwargs; call `pipeline.run()` to execute the
body. Return the int exit code (or let the return value of `pipeline.run()`
propagate).

```python
# ~/.eosh/config.py
import sys
import time
from eosh.commands import arg, registry
from eosh.pipeline import Pipeline

@registry.command(
    "@repeat",
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

To share decorators across machines or teammates, put them in a module on
`sys.path` and import it from `config.py` — the same as your own recipes.

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
│       ├── switcher.py         # Ctrl+] context switcher (a Shell mixin)
│       ├── slots.py            # Python stages: thread-local stdio, run_handler,
│       │                       # the CommandContext I/O implementations
│       ├── commands.py         # command registry, @command decorator
│       ├── variables.py        # Var ABC, VarRegistry, EnvVar, VarCompleter
│       ├── completion.py       # Completer ABC, CompletionContext, built-in completers
│       ├── completion_cache.py # TTL store for completer fetches; invalidated after every command
│       ├── context.py          # Context, ContextManager, ContextState
│       ├── history.py          # HistoryStore: shared SQLite history (~/.eosh/history.db)
│       ├── lineedit.py         # DIY raw-mode line editor, ghost suggestion, TAB completion glue
│       ├── command_context.py  # CommandContext / ShellView: what user code sees
│       ├── hooks.py            # event hooks: @hooks.on_directory_changed, …
│       ├── shell_integration.py # OSC 633 / 133 prompt and command marks
│       ├── keys.py             # key bindings: action table, keys.bind, @keys.action
│       ├── notify.py           # OS notification when a slow command finishes;
│       │                       # native backends, skip list, `notify` +
│       │                       # `notify_threshold` Vars
│       ├── parsing.py          # line tokenization, quote handling, var expansion
│       ├── pipeline.py         # quote-aware operator parser: parse_line(), expand_globs(), decorator extraction, Pipeline.run()
│       ├── job.py              # PipelineSlot: a line's pipeline on one PTY + job leader client
│       ├── _job_leader.py      # the job leader: session leader that starts external stages
│       ├── process.py          # PTY subprocess slots, output buffering, terminal-mode
│       │                       # tracking, ExitCallbackMixin (one-shot slot-done hook)
│       ├── prompt.py           # set_prompt / get_prompt_func / default_prompt
│       ├── tui.py              # InlinePicker, InlineArgPrompt
│       ├── colors.py           # ColorScheme (dark/light/mono), paint(), NO_COLOR
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
│           ├── __init__.py     # register_builtins()
│           ├── watch.py        # @watch built-in
│           ├── time.py         # @time built-in
│           ├── retry.py        # @retry built-in
│           └── quiet.py        # @quiet built-in
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
    ├── test_command_context.py
    ├── test_command_input.py
    ├── test_commands.py
    ├── test_completion.py
    ├── test_completion_cache.py
    ├── test_context.py
    ├── test_decorators.py
    ├── test_history.py
    ├── test_hooks.py
    ├── test_keys.py
    ├── test_notify.py
    ├── test_parsing.py
    ├── test_pipeline.py
    ├── test_process.py
    ├── test_recipe_missing.py
    ├── test_recipes.py
    ├── test_shell_continuation.py
    ├── test_shell_integration.py
    ├── test_user_config.py
    └── test_variables.py

~/.eosh/
├── config.py           # user configuration (commands, recipes, decorators, vars)
└── history.db          # SQLite: the history every eosh process shares (history.py)
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
| Standalone external command (no pipe, no redirect) | a one-stage `PipelineSlot` (inherited terminal when stdin isn't one, and on Windows) |
| External command in a pipeline | started by the line's job leader on its `PipelineSlot` PTY (`subprocess.Popen` with plain fds when stdin isn't a terminal, and on Windows) |
| External command with redirect (no pipe) | one-stage pipeline, as above, with file fds |
| Python `@registry.command` in a pipeline | worker thread per stage; thread-local `sys.stdin`/`sys.stdout`/`sys.stderr` rebound to pipe ends (and the slot's PTY at the terminal-facing ends) |
| Python `@registry.command` with redirect (no pipe) | one-stage pipeline: worker thread, thread-local `sys.std*` rebound to the redirect files |

**A line is one slot** (`job.py`, discussion #76). On a POSIX terminal,
`Shell._run_on_slot` runs the whole line — every command, external or Python,
every pipeline, `&&` / `||` / `;` — on one `PipelineSlot`: one PTY whose slave
is every terminal-facing end, read and buffered by `PtySlot`. A driver thread
(`PipelineSlot.run`) runs the sequence; the main thread only relays the
terminal (`_forward`). So Ctrl+] parks the rest of the line with it, and
Ctrl+C (a pipeline ending in 130) ends the line, as in bash. Everything the
line starts uses the context it started in (`slot.ctx`): its cwd for spawns,
globs and redirects, its environment, its variables — a parked line keeps
running in its own context, whatever is current. Two exceptions run on the
main thread: a line of nothing but assignments, and a line with a lone `sync`
built-in (`context`, `alias`, `reload`, `exit`, …), which goes pipeline by
pipeline, each of the others on a slot of its own (so parking one drops the
rest of that line). The external stages are started by a **job leader**
(`_job_leader.py`, run as `python -I -S`; one spare is always started ahead
of time, and the PTY is handed to it with `TIOCSCTTY`): the
session leader with the PTY as its controlling terminal, so the stages share
its session and process group — `/dev/tty` works for `| less` / `| sudo` /
`| fzf`, Ctrl+C reaches them through the line discipline, SIGWINCH goes to the
group. eosh can't be that leader: its children are in its own session. A line costs ~20 ms over a plain `Popen` (the warm leader saves the ~30 ms
interpreter start). Ctrl+C also
interrupts the Python stages (`interrupt_python_stages`) unless a stage turned
ISIG off. A decorator body on a stage thread joins the slot (`_job_local`).
Keys typed ahead that no stage read are drained from the PTY before the leader
exits and handed back to the prompt (`take_unread` → `terminal.unread`).

A redirected single stage goes through the same loop as a multi-stage pipeline (`_execute_pipeline`). There is no separate redirect path, so the process-global `sys.stdout` is never reassigned, and a background thread printing at the same time can't leak into the redirect target. `_execute_stage` only handles a lone stage with no redirects, which gets the terminal.

The thread-local routing (one `_ThreadLocalStream` per `sys.std*`, in `slots.py`, installed by `install_stdio_routers` from `Shell.__init__`) is what lets multiple Python pipeline stages run concurrently without trampling each other or the main thread's terminal. Caveats — most importantly that nested `subprocess` from inside a piped Python command bypasses the thread-local rebinding because it reads the real fd 1 — are documented in `doc/limitations.md` under "Python commands in pipelines — caveats of the in-process model."

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

5. **PTY process multiplexing** — each context can hold a slot (`PipelineSlot`) with live work — external commands and Python commands alike. `Ctrl+]` switches between contexts without killing the running process. The slot buffers output while inactive and replays it on return.

6. **Config as Python** — `config.py` is just Python that imports eosh APIs. No DSL to learn; full language power for defining completers with caching, API calls, etc. The `reload` command reloads the config without restarting the shell.

7. **System command fallback** — anything not registered as a Python command is passed to the system shell via PTY, so eosh is a drop-in replacement for daily use.

8. **Python-backed variables mirror the command registry pattern, with one writer for the environment** — an `EnvVar` only *declares* which `os.environ` keys a logical name stands for (e.g. `aws_region` → `AWS_REGION` + `AWS_DEFAULT_REGION`); the shell writes them, always through the `ContextManager`, so per-context save/restore can never be bypassed. A value that must stay out of child processes is a `PyVar` (still per-context — the context manager saves and restores it like cwd) or, if it belongs to the whole shell, a `GlobalVar`. The `var` command and bare `NAME=VALUE` assignment dispatch through `VarRegistry` before falling back to plain env writes, and `$NAME` / `${NAME}` expansion does the same lookup in reverse — so registered Vars are read- and write-symmetric with `os.environ` and a Python-backed variable behaves transparently like an OS variable on the command line. `VarCompleter` handles `=`-split completion locally without touching the global tokenizer.

9. **One reader for real stdin** — when a Python command spawns an interactive subprocess, the child must not inherit fd 0 directly. The main forwarding thread is already reading stdin in raw mode; a second reader (the subprocess) splits keystrokes unpredictably between them. `ctx.run_interactive` enforces the rule by starting the child on the command's slot PTY, so the chain stays `stdin → main → master → subprocess` — the same path every external command takes — and `ctx.input` / `ctx.choose` read that PTY too.

10. **Decorators as a sigil-prefixed grammar, not a built-in command** — `@name [flags] body` is parsed *before* the normal pipeline grammar runs (`pipeline.py::_extract_decorator_prefix`), so the syntax is unambiguous to the parser and can never collide with a POSIX command name. Borrowed from IPython's magics (`%name args`); see [doc/decorators.md](doc/decorators.md). The `{...}` body delimiter is required when the wrapped pipeline contains operators, which makes the decorator's scope visible at a glance and side-steps the `watch -n 5 ls | grep abc` ambiguity that POSIX `watch` is famous for. `Pipeline.run()` lets a decorator body re-enter `Shell._execute_pipeline` so redirects/pipes/Python-stage routing all work the same as at the top level.

11. **TTL cache + command-boundary invalidation for completer fetches** — TAB completion runs the completer on every keystroke while the picker is open (see `lineedit.py::refresh_fn`). Completers that hit AWS APIs (e.g. `aws_completer`, `_HyperpodNodeIdCompleter`) would otherwise issue the same boto3 call four or five times for a single typed token. `completion_cache.py` provides `get_or_fetch(key, fn, ttl=60)` with a process-global store. Keys are tuples that include the active `(AWS_PROFILE, AWS_REGION)` via `aws_env_key()` so the cache doesn't bleed across profiles. `Shell._execute()` calls `completion_cache.invalidate_all()` after each pipeline finishes, so a freshly-mutated resource (e.g. after `awsut sagemaker hyperpod scale`) is re-fetched on the next TAB — TTL handles the within-session repeats, the invalidation hook handles correctness across commands.

12. **Notify from the place that knows the work ended, and only from one of them** — the shell has three ways a command can finish (a foreground line, a slot exiting in a context nobody is looking at, a backgrounded slot resumed and watched to completion), and a naive "notify on completion" hook either misses cases or double-reports them. The rule is that `Shell._execute` owns *foreground* timing and steps aside via `self._backgrounded` the moment a line parks its work on a context (`_park`), and the slot's exit handler — wired at construction — owns everything after that, reporting a slot only if it was parked. Backends stay dependency-free (native helper per platform, terminal bell as the floor), fire on a daemon thread so the prompt never waits on a subprocess spawn, and swallow every error — a missed notification is a nuisance, a shell that dies delivering one is a bug. See [doc/notifications.md](doc/notifications.md).
