# Architecture Overview

## System Diagram

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
│  └── Context switch TUI (tui.py)                   │
├─────────────────────────────────────────────────────┤
│  Command Registry (commands.py)                     │
│  ├── Built-in commands                             │
│  ├── Python function commands (from config)        │
│  ├── External recipes (handler-less Commands)      │
│  ├── Aliases                                       │
│  └── System command passthrough                    │
├─────────────────────────────────────────────────────┤
│  Variable Registry (variables.py)                   │
│  ├── Var ABC — Python-backed shell variables       │
│  ├── EnvVar — single-key passthrough               │
│  └── VarCompleter — KEY=VALUE TAB completion       │
├─────────────────────────────────────────────────────┤
│  Completion Engine (completion.py)                  │
│  ├── Command name completion                       │
│  ├── Argument completion (per-command completers)  │
│  ├── Options completion (flags as picker rows)     │
│  ├── CobraCompleter (opt-in) / Argcomplete fallback│
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
│  Cross-Platform Terminal Layer (terminal.py)        │
│  ├── init / get_mode / set_raw / restore_mode      │
│  ├── read_key — full logical key as bytes          │
│  └── SIGWINCH (POSIX) / kbhit polling (Windows)    │
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
│  Colors (colors.py)                                 │
│  └── ColorScheme: dark / light / mono (NO_COLOR)   │
├─────────────────────────────────────────────────────┤
│  Key Bindings (keys.py)                             │
│  └── keys.bind(), @keys.action() from config       │
├─────────────────────────────────────────────────────┤
│  Recipes (recipes/)                                 │
│  └── Completion recipes for external commands      │
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

## Module Responsibilities

### shell.py — Main Shell Loop

Entry point and orchestrator. Owns the REPL cycle: read input, parse, dispatch, repeat.

- Uses a DIY raw-mode line editor (`lineedit.py`) — no external dependencies
- Registers built-in commands: `cd`, `exit`, `reload`, `config`, `var`, `alias`, `unalias`, `source-bash`, `help`, `context`
- Loads user configuration at startup; `reload` re-loads without restarting the shell
- Falls back to PTY subprocess (`process.py`) for unrecognized commands
- Executes pipelines (`|`), sequences (`;`, `&&`, `||`), and redirections (`>`, `>>`, `<`, `2>`, `2>&1`)
- Handles `Ctrl+]` context-switch picker (shows all contexts + running process names)

### commands.py — Command Registry

Provides the `CommandRegistry` class and a global `registry` singleton.

- `registry.command(name, *, help=None, params=None, delegate=None) -> Command` — plain call for a group or external recipe, decorator to attach a handler; a name is always required. `params=[arg(...)]` declares positionals and flags; argparse parses with it and completion reads it on demand. `delegate=Completer` (a `Command` attribute) answers every completion slot, for a tool with its own completion protocol.
- `arg(*names, completer=None, **argparse_kwargs)` builder used inside `params=`. `metavar=` becomes the inline hint for value-taking flags; `choices=` auto-populates a `ChoiceCompleter` when `completer=` is omitted.
- Sub-command tree: `Command.command(name, ...)` registers a child sub-command. Used by `git`, `awsut`, and any nested CLI; see [subcommands.md](subcommands.md). A node's flags are its own (no inheritance), and a node has either a handler or children, never both.
- Aliases: `registry.alias(name, value)`, `registry.unalias(name)`, `get_alias`, `list_aliases`.
- Built-ins: `with registry.defining_builtins():` marks what the shell registers as built-in; a config replacing one needs `override=True` (otherwise a `config warning:` and the built-in stays), and `registry.clear_user_commands()` — part of `Shell._clear_user_config`, the one sweep `reload` runs — puts back exactly the built-in set. `registry.is_builtin(name)` splits `help`'s listing.
- Each `Command` holds: name, optional callable, `params: list[Arg] | None`, `help`, optional `delegate`, help text, description, parent and children.
- Description comes from the explicit `help=` kwarg, falling back to the function's docstring.

A handler-less `Command` (no callable attached) is treated by the dispatch path as an external recipe — `Shell._execute` falls through to the system-command path so the registered completion + flag metadata drive TAB completion while the actual program runs as an OS process. There is no separate `register_external_completers()` API.

### variables.py — Variable Registry

Python-backed shell variables, registered with `var_registry`: an `EnvVar(name, keys=..., completer=...)` names one or more `os.environ` keys that the shell writes (per context, through the `ContextManager`), a `PyVar` subclass holds a per-context value on the Python side (saved and restored with the context, never in `os.environ`), and a `GlobalVar` subclass a process-global one. The `var` built-in command and `$NAME` / `${NAME}` expansion both check the registry first, then fall back to `os.environ`. `VarCompleter` handles `KEY=VALUE` TAB completion locally without changing the global tokenizer. See the Variable Registry section in CLAUDE.md.

### completion.py — Completion Engine

Defines the `Completer` protocol, `CompletionContext`, and built-in completers.

- `CompletionContext` carries full parse state to every completer
- `Completer` ABC with `complete()` and optional `should_activate()` guard
- Built-in completers: `FileCompleter`, `DirCompleter`, `CommandNameCompleter`, `ChoiceCompleter`, `CallbackCompleter`, `OptionsCompleter`
- `OptionsCompleter` offers flags as ordinary picker rows (`-d <N>` for a value-taking flag, which then leads straight on to its value completer), and deduplicates flags already typed
- **Protocol completers**:
  - `CobraCompleter` drives `<cmd> __complete <words>` for cobra-based CLIs (docker, kubectl, helm, gh, argocd, …). It is opt-in per command, installed as a `delegate` by the `cobra` recipe or `enable_cobra(...)`. See `doc/cobra.md`.
  - `ArgcompleteCompleter` (automatic, where no completer is registered) drives the argcomplete protocol (env vars + fd 8) for Python CLIs (pipx, conda, pre-commit, tox, pdm, httpie, …) — see `doc/argcomplete-fallback.md`

### context.py — Context Manager

Manages named environments with variables, working directories, and optional running processes.

- `Context` dataclass: name, variables dict, saved cwd, optional parked slot, `state` property (`IDLE`/`RUNNING`/`EXITED`)
- `ContextManager`: named collection with a current pointer, kept in most-recently-used order; `new(name)` creates a context inheriting the current one's variables and history, and removing the current context makes the MRU next one current
- On switch: saves current cwd, restores target cwd, swaps environment variables
- Environment variable backup/restore to avoid leaking between contexts
- `set_variable` / `unset_variable` update both the current context and `os.environ`

### lineedit.py — Line Editor

DIY raw-mode line editor. No prompt_toolkit or readline.

- `LineEditor.prompt()` — reads one line in raw terminal mode
- Full key binding suite: `Ctrl+A/E/B/F/W/K/U/L`, `Alt+B/F`, arrows, `Ctrl+P/N`, `Ctrl+R`, `Ctrl+]`
- TAB opens `InlinePicker` (flags included, one row each) with **no candidate pre-selected** — Enter dismisses, only Down/Up select (TAB extends the common prefix, never selects); supports narrowing by typing, TAB-extend, backspace-to-close, and self-closes when narrowing leaves zero candidates
- `Ctrl+R` opens a filterable picker over the shared history, starting from what is typed
- A fish-style ghost suggestion (the latest line run in the cwd that extends the buffer) is drawn dim after the caret; `→` / `Ctrl+E` accept it, `Alt+F` one word — see [history.md](history.md)
- Multi-line wrap tracking for correct cursor repositioning
- VSCode integrated terminal detection for resize handling (see `doc/terminal-resize.md`)

### tui.py — Inline TUI Widgets

Inline-rendered widgets anchored with DECSC/DECRC (no alternate screen). Cancel on SIGWINCH.

- `InlinePicker` — single-select list; supports narrowing by typing, scrollbar, `meta_fn` labels (one string, or cells laid out as columns aligned across rows). `select_first=False` opens with no row highlighted (Enter → None); `closed_empty` reports "narrowed to zero candidates"; `empty_placeholder` keeps the picker open on zero candidates, rendering that text instead (used by `Ctrl+R`, whose query lives only in the picker); `typed` exposes the chars the picker echoed for the caller to commit
- `InlineArgPrompt` — single-line text prompt (naming a context in the switch picker)

### process.py — PTY Process Slots

`PtySlot` is work on a PTY eosh owns, with output buffering for context multiplexing.

- a reader thread streams the master's output to `OutputBuffer`
- `activate()` / `deactivate()` route buffered output to stdout or hold it
- `replay_buffer()` flushes held output when switching back to a context
- `resize()` updates PTY window size and delivers SIGWINCH to the child process group
- `suspend_terminal_modes()` / `restore_terminal_modes()` generate escape sequences to undo/redo DEC private modes (alt screen, mouse, app cursor keys) across switches

`job.PipelineSlot` is the one subclass: every external command runs on it.

### job.py — A Pipeline on One PTY

`PipelineSlot(PtySlot)` runs a whole pipeline (or a redirected command) on one PTY, so Ctrl+] parks it like a single command. Terminal-facing ends of every stage are the PTY slave. External stages are started by a job leader (`_job_leader.py`, `python -I -S`): the session leader with the PTY as its controlling terminal, so the stages share its session and process group — `/dev/tty`, Ctrl+C and SIGWINCH behave as in a POSIX shell's job. Requests go over a socketpair (length-prefixed JSON, stdio fds as `SCM_RIGHTS`). Python stages stay on eosh's threads.

### prompt.py — Prompt Function

Provides `set_prompt()` / `get_prompt_func()` for customizable prompt generation.

Default prompt: `[context] path/cwd HH:MM:SS [bg:N]>` (ANSI colors). The `[context]` prefix is omitted when the context name is `"default"`. `[bg:N]` appears when N other contexts have live processes.

### keys.py — Key Bindings

One action→keys table for the line editor (`prompt.*`), every inline picker (`picker.*`) and the context switcher (`switcher.*`), dotted names as in XeFM's `KEY_BINDINGS`. `config.py` calls `keys.bind(action, keys)` (replaces an action's keys, `[]` unbinds; a bare name binds every surface that has it, a dotted one wins on its own surface) and `@keys.action(name)` (a user `prompt.*` action, `func(ctx)` with an `EditorContext`, bound with `keys.bind` like any other — before or after it is defined; names are checked after the config runs). Key names (`Ctrl-R`, `Alt-.`, `Shift-Tab`) parse to the byte sequences `terminal.read_key` returns. Bad names, unknown actions and printable keys are `config warning:`s, and `reload` resets everything. `help keys` lists the table. See [keys.md](keys.md).

### hooks.py — Event Hooks

One decorator per event (`on_startup`, `on_exit`, `on_directory_changed`, `on_context_switched`, `on_command_starting`, `on_command_finished`, `on_command_not_found`) for `config.py` to react to the shell. Hooks run in registration order, a failing one is reported and skipped, and `reload` clears them. The shell detects directory and context changes by comparing state (`Shell._notice_state_change`) rather than hooking each mutation. See [hooks.md](hooks.md).

### colors.py — Color Schemes

`ColorScheme` dataclass holding the colours of the TUI widgets (picker rows, status bar, scrollbar), with `dark`, `light` and `mono` schemes shipped. A colour is an RGB triple or `TERM_FG` / `TERM_BG` (the terminal's own colours); `paint(fg, bg)` turns a pair into SGR, drawing text in `TERM_BG` on `TERM_FG` as reverse video — which is all `mono` uses, so it emits no colour. `set_color_scheme(scheme)` picks a scheme from `~/.eosh/config.py` (`reload` resets it); with none picked, `get_color_scheme()` returns `mono` when `NO_COLOR` is set ([no-color.org](https://no-color.org)) and `dark` otherwise. There is no separate no-colour mode. A command's own output (awsut's tables, the starter config's prompt) asks `color_enabled(stream)`: no `NO_COLOR`, and a terminal.

### terminal.py — Cross-Platform Terminal Layer

The single place that touches OS-specific terminal APIs. `lineedit.py`, `tui.py`, and the POSIX forwarding loop in `shell.py` go through `terminal.py` so the rest of the codebase is platform-agnostic. Provides `init()`, `get_mode/set_raw/restore_mode`, `read_key(fd)` (returns one logical key as bytes — control byte, full UTF-8 char, or full escape sequence), `wait_readable(fd, timeout)`, and SIGWINCH wiring on POSIX (`HAS_SIGWINCH`, `install_resize_handler`). On Windows it translates `msvcrt` scan-codes into the same ANSI sequences POSIX produces, so the keypress logic in `lineedit.py` is identical on both platforms.

### recipes/ — Completion Recipes

Opt-in completion recipes for system commands. Each recipe calls `registry.command(name, params=[...])` with **no handler attached** — the dispatch path treats handler-less Commands as external recipes. Enable in config:

```python
from eosh.recipes import enable
enable("*")                                     # every built-in recipe + bundled add-on
enable("git", "make", "ssh", "kill", "aws")     # or pick specific ones
```

Cobra-based tools (`docker`, `kubectl`, `helm`, `gh`, …) need no hand-written recipe. `enable("cobra")` or `enable_cobra("mytool")` hands them to `CobraCompleter`. Argcomplete-based Python CLIs (`pipx`, `conda`, …) are detected automatically by `ArgcompleteCompleter`.

### decorators/ — Pipeline Decorators

A decorator is a token of the form `@name [flags]` at the start of a line that wraps the rest of the line as a pipeline and modifies how it runs. A decorator is a command named `@name` in the command registry: authors register a function with `@registry.command("@name", params=[...])` that receives a parsed `Pipeline` AST and the parsed flag namespace. Built-ins: `@watch`, `@time`, `@retry`, `@quiet`, registered by `eosh.decorators.register_builtins()`. Pipelines that contain `|`, `;`, `&&`, `||`, or a redirect must be enclosed in `{...}`. See [decorators.md](decorators.md).

### parsing.py — Line Tokenization

Splits raw input into tokens respecting quoting rules.

- `split_for_completion(line)` returns `(tokens, prefix)` for the completion engine
- `expand_vars(line)` expands `$VAR` and `${VAR}`, leaving single-quoted regions unexpanded.  Lookup checks `var_registry` first (so `$aws_region` returns whatever the registered Var's `get()` reports), then falls back to `os.environ` — mirroring the set-side precedence in `shell._set_variable`.
- `tokenize(line)` wraps `shlex.split` with graceful recovery for unclosed quotes

### pipeline.py — Shell Operator Parser

Quote-aware parser for the full operator set.

- `parse_line(line)` returns a `Sequence` of `Pipeline` objects, each containing `Stage` objects with extracted `Redirect` lists
- `expand_globs(tokens)` expands `*`, `?`, `[`, and `**` patterns

## Data Flow

### Command Execution

```
User input → expand_vars() → parse_line() → Sequence of Pipelines
  → On a POSIX terminal, the whole Sequence on one PipelineSlot: a driver
    thread runs the pipelines (&&, ||, ;) — OS pipes between stages, the
    slot's PTY at the terminal-facing ends, external stages started by its
    job leader — in the context the line started in; the main thread relays
    the terminal.  Exceptions on the main thread: a line of assignments, and
    a line with a lone sync built-in (pipeline by pipeline)
  → Without a terminal / on Windows: pipeline by pipeline, plain Popen
  → Python stages get thread-local sys.stdin/stdout/stderr
```

### Tab Completion

```
User presses TAB
  → LineEditor._complete()
    → _get_completions(line_before_cursor)
        → split_on_operators() → isolate current pipeline stage
        → split_for_completion(stage) → (tokens, prefix)
        → No tokens? → CommandNameCompleter
        → Has tokens?
            → Look up command; Command.resolve() to the deepest sub-command
            → _resolve_slot(): delegate | flag | value | subcommand | positional
              (one classifier, shared with the status bar's _get_arg_info)
            → No completer for the slot? → argcomplete, then FileCompleter
    → Single token completion → _apply() directly (a value-taking flag
      loops on to complete its value)
    → Otherwise → InlinePicker (narrows as user types)
```

### Context Switch

```
Ctrl+] pressed (or "context switch <name>")
  → _save_current(): snapshot cwd into current context
  → _unapply_env(): restore os.environ from backup
  → _activate(name):
      → os.chdir(target.cwd)
      → _apply_env(target): export target.variables, backup originals
  → If target context has a live slot: resume forwarding mode
```

## File Layout

```
eosh/
├── CLAUDE.md               # Development instructions
├── README.md               # End-user documentation
├── pyproject.toml          # Package metadata, dependencies
├── doc/                    # Technical design documents
│   ├── architecture.md
│   ├── completion.md
│   ├── context.md
│   ├── decorators.md
│   ├── recipes.md
│   ├── addons.md
│   ├── subcommands.md
│   ├── cobra.md
│   ├── argcomplete-fallback.md
│   ├── terminal-resize.md
│   └── limitations.md
├── src/
│   └── eosh/
│       ├── __init__.py         # public exports
│       ├── __main__.py         # entry point (calls Shell().run())
│       ├── _config.py          # bundled default ~/.eosh/config.py template
│       ├── shell.py            # main loop, command dispatch, pipeline execution
│       ├── slots.py            # Python-command slots, thread-local stdio, run_handler
│       ├── commands.py         # command registry, @command decorator, arg() builder, CmdParser, sub-command tree
│       ├── variables.py        # Var ABC, VarRegistry, EnvVar, VarCompleter
│       ├── completion.py       # Completer ABC, CompletionContext, built-in completers, cobra/argcomplete fallbacks
│       ├── context.py          # Context, ContextManager, ContextState
│       ├── history.py          # HistoryStore: shared SQLite history (~/.eosh/history.db)
│       ├── lineedit.py         # DIY raw-mode line editor, ghost suggestion, TAB completion glue
│       ├── parsing.py          # line tokenization, quote handling, var expansion
│       ├── pipeline.py         # quote-aware operator parser: parse_line(), expand_globs(), decorator extraction, Pipeline.run()
│       ├── job.py              # PipelineSlot: a line's pipeline on one PTY; _job_leader.py starts its stages
│       ├── process.py          # PTY subprocess slots, output buffering, terminal-mode tracking
│       ├── prompt.py           # set_prompt / get_prompt_func / default_prompt
│       ├── colors.py           # ColorScheme + set_color_scheme (dark/light/mono), NO_COLOR
│       ├── keys.py             # key bindings: action table, keys.bind, @keys.action
│       ├── terminal.py         # cross-platform raw-mode + key reading
│       ├── tui.py              # InlinePicker, InlineArgPrompt
│       ├── recipes/            # external-command completion recipes (28+ files)
│       │   ├── __init__.py     # enable(*names): built-in recipes + add-ons
│       │   └── <name>.py       # see Available recipes block in __init__.py
│       └── decorators/
│           ├── __init__.py     # register_builtins()
│           ├── watch.py        # @watch built-in
│           ├── time.py         # @time built-in
│           ├── retry.py        # @retry built-in
│           ├── quiet.py        # @quiet built-in
└── tests/
    ├── test_alias_expansion.py
    ├── test_argcomplete_fallback.py
    ├── test_aws_recipe.py
    ├── test_cobra_completion.py
    ├── test_commands.py
    ├── test_completion.py
    ├── test_context.py
    ├── test_decorators.py
    ├── test_parsing.py
    ├── test_pipeline.py
    ├── test_piped_python_commands.py
    ├── test_process.py
    ├── test_recipes.py
    ├── test_shell_continuation.py
    ├── test_subcommands.py
    ├── test_tar_recipe.py
    └── test_variables.py
```

## Design Decisions

1. **DIY raw-mode line editor** — `lineedit.py` drives the terminal directly with `termios`/`tty`/`select`. This avoids external dependencies, keeps the codebase self-contained, and gives full control over the completion UI and resize handling.

2. **Completer receives full context** — `CompletionContext` carries all parsed state so completers make decisions based on command name, preceding args, and shell context without global state.

3. **`params` is the one source of truth for a command** — argparse parses with it and completion reads flag/positional completers off it on demand; each node owns its flags. Flat commands and trees resolve the same way, so completion and the status bar share one classifier. A completer at position N inspects `ctx.args[:N]` to see prior selections.

4. **Config as Python** — `~/.eosh/config.py` is plain Python importing eosh APIs. No DSL to learn; full language power for defining completers with caching, API calls, conditional logic. `reload` applies changes without restarting.

5. **PTY process multiplexing** — each context can hold a slot with live work. `Ctrl+]` switches between contexts without killing the running process; the slot buffers output while inactive and replays it on return.

6. **Context variables as env vars** — switching contexts exports variables to `os.environ` and backs up originals. This means subprocesses (system commands) automatically inherit context variables without eosh-specific wiring.

7. **System command fallback** — unregistered commands pass through to a PTY subprocess, making eosh a drop-in replacement shell for daily use.

## Known structural smells

These are not bugs and they are not blocking work. They are the architectural rough edges that have accumulated as features (Python pipelines, decorators, passthrough subprocesses) layered on top of the original PTY-multiplexing core. Each is tracked in more detail in [discussion #70](https://github.com/crftwr/eosh/discussions/70).

- **`shell.py` is ~2600 lines** and still hosts three concerns that are conceptually separate: the per-stage pipeline executor (`_execute_pipeline`, `_execute_stage`, redirect plumbing); the raw-mode forwarding loop (`_forward`) and the Ctrl+] switcher; and the actual REPL + built-ins + completion glue. (The Python-command slot family moved to `slots.py`.) Everything else in the package is right-sized.
- **Module-global callback registration is the hidden contract between layers.** `pipeline.set_pipeline_executor` and the `_in_pipeline` / `_job_local` thread-locals consumed by `CommandContext`'s methods (via `slots._run_interactive` / `_read_from_user` / `_choose`) are independent global setters wired from `Shell.__init__`. Works, but: two `Shell` instances cannot coexist in one process, tests must reset the globals, and the real interface between `Pipeline.run` and `Shell._run_pipeline_from_decorator` is implicit.
- **Redirect-open code is duplicated** inside `_execute_pipeline` and `_execute_stage` with subtly different sentinels (`subprocess.STDOUT` vs the string `"stdout"` for `2>&1`). A single `_open_redirects(stage)` helper would unify both call sites.

The shape of the relief is sketched in [discussion #70](https://github.com/crftwr/eosh/discussions/70): extract `dispatch.py` (the pipeline executor), and replace the global setters with a single `ExecutionEnvironment` interface that `Shell` constructs and passes down. None of this is a one-shot refactor — it is a sequence of medium-risk moves, each independently valuable.
