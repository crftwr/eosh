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
│  ├── Python-command slots & passthrough_run        │
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
│  ├── Options completion (flags, multi-select TUI)  │
│  ├── CobraCompleter (opt-in) / Argcomplete fallback│
│  └── Filesystem completion (fallback)              │
├─────────────────────────────────────────────────────┤
│  TUI Widgets (tui.py)                               │
│  ├── InlinePicker — single-select inline list      │
│  ├── InlineMultiPicker — multi-select with Space   │
│  └── InlineArgPrompt — flag-argument text input    │
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
│  ├── Context stack                                 │
│  ├── Context-aware variable resolution             │
│  ├── CWD save/restore on switch                    │
│  └── env var apply/unapply on switch               │
├─────────────────────────────────────────────────────┤
│  Prompt (prompt.py)                                 │
│  └── Default + user-overrideable prompt function   │
├─────────────────────────────────────────────────────┤
│  Colors (colors.py)                                 │
│  └── ColorScheme + set_color_scheme (dark/light)   │
├─────────────────────────────────────────────────────┤
│  Recipes (recipes/)                                 │
│  └── Completion recipes for external commands      │
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

## Module Responsibilities

### shell.py — Main Shell Loop

Entry point and orchestrator. Owns the REPL cycle: read input, parse, dispatch, repeat.

- Uses a DIY raw-mode line editor (`lineedit.py`) — no external dependencies
- Registers built-in commands: `cd`, `exit`, `reload`, `var`, `alias`, `unalias`, `source-bash`, `help`, `context`
- Loads user configuration at startup; `reload` re-loads without restarting the shell
- Falls back to PTY subprocess (`process.py`) for unrecognized commands
- Executes pipelines (`|`), sequences (`;`, `&&`, `||`), and redirections (`>`, `>>`, `<`, `2>`, `2>&1`)
- Handles `Ctrl+]` context-switch picker (shows all contexts + running process names)

### commands.py — Command Registry

Provides the `CommandRegistry` class and a global `registry` singleton.

- `@registry.command(name, *, help=None, params=None, delegate=None, options_completer=None)` — register a Python function (with handler) or an external recipe (no handler). `params=[arg(...)]` declares positionals and flags; the registry derives both an argparse parser and the per-position completer dict from the same list. `delegate=Completer` installs a single completer at every slot (used when an external tool drives its own completion protocol). `options_completer=OptionsCompleter` overrides the auto-built flag completer when a custom subclass is needed.
- `arg(*names, completer=None, **argparse_kwargs)` builder used inside `params=`. `metavar=` becomes the inline hint for value-taking flags; `choices=` auto-populates a `ChoiceCompleter` when `completer=` is omitted.
- Sub-command tree: `Command.command(name, ...)` registers a child sub-command. Used by `git`, `awsut`, and any nested CLI; see [subcommands.md](subcommands.md).
- Aliases: `registry.alias(name, value)`, `registry.unalias(name)`, `get_alias`, `list_aliases`.
- `registry.register(cmd)` for imperative registration of a pre-built `Command`.
- `registry.mark_builtins()` / `registry.clear_user_commands()` for hot-reload support.
- Each `Command` holds: name, optional callable, `params: list[Arg] | None`, derived per-argument completers dict, help text, description.
- Description comes from the explicit `help=` kwarg, falling back to the function's docstring.

A handler-less `Command` (no callable attached) is treated by the dispatch path as an external recipe — `Shell._execute` falls through to the system-command path so the registered completion + flag metadata drive TAB completion while the actual program runs as an OS process. There is no separate `register_external_completers()` API.

### variables.py — Variable Registry

Python-backed shell variables. `Var` is the ABC; subclass and register instances with `var_registry`. The `var` built-in command and `$NAME` / `${NAME}` expansion both check the registry first, then fall back to `os.environ`. `EnvVar(name, env_var, completer=...)` is the convenience subclass for a 1-to-1 passthrough to a single env key. `VarCompleter` handles `KEY=VALUE` TAB completion locally without changing the global tokenizer. See the Variable Registry section in CLAUDE.md.

### completion.py — Completion Engine

Defines the `Completer` protocol, `CompletionContext`, and built-in completers.

- `CompletionContext` carries full parse state to every completer
- `Completer` ABC with `complete()` and optional `should_activate()` guard
- Built-in completers: `FileCompleter`, `DirCompleter`, `CommandNameCompleter`, `ChoiceCompleter`, `CallbackCompleter`, `OptionsCompleter`, `ConditionalCompleter`
- `OptionsCompleter` supports multi-select flag TUI, flag arg-hints, value completers, and flag deduplication
- **Protocol completers**:
  - `CobraCompleter` drives `<cmd> __complete <words>` for cobra-based CLIs (docker, kubectl, helm, gh, argocd, …). It is opt-in per command, installed as a `delegate` by the `cobra` recipe or `enable_cobra(...)`. See `doc/cobra.md`.
  - `ArgcompleteCompleter` (automatic, where no completer is registered) drives the argcomplete protocol (env vars + fd 8) for Python CLIs (pipx, conda, pre-commit, tox, pdm, httpie, …) — see `doc/argcomplete-fallback.md`

### context.py — Context Manager

Manages named environments with variables, working directories, and optional running processes.

- `Context` dataclass: name, variables dict, saved cwd, optional `ProcessSlot`, `state` property (`IDLE`/`RUNNING`/`EXITED`)
- `ContextManager`: named collection with current pointer and push/pop stack
- On switch: saves current cwd, restores target cwd, swaps environment variables
- Environment variable backup/restore to avoid leaking between contexts
- `set_variable` / `unset_variable` update both the current context and `os.environ`

### lineedit.py — Line Editor

DIY raw-mode line editor. No prompt_toolkit or readline.

- `LineEditor.prompt()` — reads one line in raw terminal mode
- Full key binding suite: `Ctrl+A/E/B/F/W/K/U/L`, `Alt+B/F`, arrows, `Ctrl+P/N`, `Ctrl+R`, `Ctrl+]`
- TAB opens `InlinePicker` (or `InlineMultiPicker` for flags) with **no candidate pre-selected** — Enter dismisses, only Down/Up select (TAB extends the common prefix, never selects); supports narrowing by typing, TAB-extend, backspace-to-close, and self-closes when narrowing leaves zero candidates
- `Ctrl+R` opens a filterable history picker
- Multi-line wrap tracking for correct cursor repositioning
- VSCode integrated terminal detection for resize handling (see `doc/terminal-resize.md`)

### tui.py — Inline TUI Widgets

Inline-rendered widgets anchored with DECSC/DECRC (no alternate screen). Cancel on SIGWINCH.

- `InlinePicker` — single-select list; supports narrowing by typing, scrollbar, `meta_fn` labels (one string, or cells laid out as columns aligned across rows). `select_first=False` opens with no row highlighted (Enter → None); `closed_empty` reports "narrowed to zero candidates"; `empty_placeholder` keeps the picker open on zero candidates, rendering that text instead (used by `Ctrl+R`, whose query lives only in the picker); `typed` exposes the chars the picker echoed for the caller to commit
- `InlineMultiPicker` — multi-select with Space; jump-to by letter; returns checked items (or the highlighted one, or None when neither); `select_first=False` as above
- `InlineArgPrompt` — single-line text prompt for a flag's argument value

### process.py — PTY Process Slots

`ProcessSlot` manages a PTY-backed subprocess with output buffering for context multiplexing.

- `start()` forks a child in a new PTY; a reader thread streams output to `OutputBuffer`
- `activate()` / `deactivate()` route buffered output to stdout or hold it
- `replay_buffer()` flushes held output when switching back to a context
- `resize()` updates PTY window size and delivers SIGWINCH to the child process group
- `suspend_terminal_modes()` / `restore_terminal_modes()` generate escape sequences to undo/redo DEC private modes (alt screen, mouse, app cursor keys) across switches

### prompt.py — Prompt Function

Provides `set_prompt()` / `get_prompt_func()` for customizable prompt generation.

Default prompt: `[context] path/cwd HH:MM:SS [bg:N]>` (ANSI colors). The `[context]` prefix is omitted when the context name is `"default"`. `[bg:N]` appears when N other contexts have live processes.

### colors.py — Color Schemes

`ColorScheme` dataclass holding ANSI colour codes used by the prompt and TUI widgets, with `dark` and `light` schemes shipped. `set_color_scheme(scheme)` swaps the active scheme; users override colours from `~/.eosh/config.py`.

### terminal.py — Cross-Platform Terminal Layer

The single place that touches OS-specific terminal APIs. `lineedit.py`, `tui.py`, and the POSIX forwarding loops in `shell.py` go through `terminal.py` so the rest of the codebase is platform-agnostic. Provides `init()`, `get_mode/set_raw/restore_mode`, `read_key(fd)` (returns one logical key as bytes — control byte, full UTF-8 char, or full escape sequence), `wait_readable(fd, timeout)`, and SIGWINCH wiring on POSIX (`HAS_SIGWINCH`, `install_resize_handler`). On Windows it translates `msvcrt` scan-codes into the same ANSI sequences POSIX produces, so the keypress logic in `lineedit.py` is identical on both platforms.

### recipes/ — Completion Recipes

Opt-in completion recipes for system commands. Each recipe calls `registry.command(name, params=[...])` with **no handler attached** — the dispatch path treats handler-less Commands as external recipes. Enable in config:

```python
from eosh.recipes import enable
enable("*")                                     # all built-in + user recipes
enable("git", "make", "ssh", "kill", "aws")     # or pick specific ones
```

Cobra-based tools (`docker`, `kubectl`, `helm`, `gh`, …) need no hand-written recipe. `enable("cobra")` or `enable_cobra("mytool")` hands them to `CobraCompleter`. Argcomplete-based Python CLIs (`pipx`, `conda`, …) are detected automatically by `ArgcompleteCompleter`.

### decorators/ — Pipeline Decorators

A decorator is a token of the form `@name [flags]` at the start of a line that wraps the rest of the line as a pipeline and modifies how it runs. Authors register a function with `@decorator_registry.decorator(name, params=[...])` that receives a parsed `Pipeline` AST and the parsed flag namespace. Built-ins: `@watch`, `@time`, `@retry`, `@quiet`, `@bg`. Loaded with `enable_decorators(...)`. Pipelines that contain `|`, `;`, `&&`, `||`, or a redirect must be enclosed in `{...}`. See [decorators.md](decorators.md).

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
  → For each Pipeline:
      → Single stage, no redirect: PTY via ProcessSlot
      → Multiple stages (pipe): subprocess.Popen with OS pipes
      → Single stage with redirect: subprocess.run with file fds
  → Python commands have sys.stdout/stdin/stderr temporarily replaced
```

### Tab Completion

```
User presses TAB
  → LineEditor._complete()
    → _get_completions(line_before_cursor)
      → _get_base_completions(line_before_cursor)
          → _split_on_operators() → isolate current pipeline stage
          → split_for_completion(stage) → (tokens, prefix)
          → No tokens? → CommandNameCompleter
          → Has tokens?
              → Look up command (registered or external completers)
              → Check completers[None] for OptionsCompleter (if prefix starts with "-")
              → Check completers[arg_index] for positional completer
              → No completer registered? → FileCompleter fallback
      → HistoryCompleter (current context's history, cwd-scoped, tail from the anchor)
          → prepended, unless the base result is a flag picker or an arg-hint
          → minus the tails the base result already offers as a single token
    → Single token completion → _apply() directly
    → All multi_select completions → InlineMultiPicker
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
  → If target context has live ProcessSlot: resume forwarding mode
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
│   ├── subcommands.md
│   ├── cobra.md
│   ├── argcomplete-fallback.md
│   ├── terminal-resize.md
│   ├── enhancements.md
│   └── limitations.md
├── src/
│   └── eosh/
│       ├── __init__.py         # public exports
│       ├── __main__.py         # entry point (calls Shell().run())
│       ├── _config.py          # bundled default ~/.eosh/config.py template
│       ├── shell.py            # main loop, command dispatch, pipeline execution, Python-command slots
│       ├── commands.py         # command registry, @command decorator, arg() builder, CmdParser, sub-command tree
│       ├── variables.py        # Var ABC, VarRegistry, EnvVar, VarCompleter
│       ├── completion.py       # Completer ABC, CompletionContext, built-in completers, cobra/argcomplete fallbacks
│       ├── context.py          # Context, ContextManager, ContextState
│       ├── history.py          # history storage and search
│       ├── lineedit.py         # DIY raw-mode line editor, History (+ history.dirs side table), TAB completion glue
│       ├── parsing.py          # line tokenization, quote handling, var expansion
│       ├── pipeline.py         # quote-aware operator parser: parse_line(), expand_globs(), decorator extraction, Pipeline.run()
│       ├── process.py          # PTY subprocess slots, output buffering, terminal-mode tracking
│       ├── prompt.py           # set_prompt / get_prompt_func / default_prompt
│       ├── colors.py           # ColorScheme + set_color_scheme (dark/light)
│       ├── terminal.py         # cross-platform raw-mode + key reading
│       ├── tui.py              # InlinePicker, InlineMultiPicker, InlineArgPrompt
│       ├── recipes/            # external-command completion recipes (28+ files)
│       │   ├── __init__.py     # enable(*names) helper, recipe_search_path, add_recipe_path
│       │   └── <name>.py       # see Available recipes block in __init__.py
│       └── decorators/
│           ├── __init__.py     # DecoratorRegistry, enable_decorators, add_decorator_path
│           ├── watch.py        # @watch built-in
│           ├── time.py         # @time built-in
│           ├── retry.py        # @retry built-in
│           ├── quiet.py        # @quiet built-in
│           └── bg.py           # @bg built-in
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

3. **Dict-based positional completers with `None` key for options** — `{arg_index: Completer}` for positional args; `{None: OptionsCompleter(...)}` for flags at any position. A completer at position N inspects `ctx.args[:N]` to see prior selections.

4. **Config as Python** — `~/.eosh/config.py` is plain Python importing eosh APIs. No DSL to learn; full language power for defining completers with caching, API calls, conditional logic. `reload` applies changes without restarting.

5. **PTY process multiplexing** — each context can hold a `ProcessSlot` with a live subprocess. `Ctrl+]` switches between contexts without killing the running process; the slot buffers output while inactive and replays it on return.

6. **Context variables as env vars** — switching contexts exports variables to `os.environ` and backs up originals. This means subprocesses (system commands) automatically inherit context variables without eosh-specific wiring.

7. **System command fallback** — unregistered commands pass through to a PTY subprocess, making eosh a drop-in replacement shell for daily use.

## Known structural smells

These are not bugs and they are not blocking work. They are the architectural rough edges that have accumulated as features (Python pipelines, decorators, `@bg`, passthrough subprocesses) layered on top of the original PTY-multiplexing core. Each is described in more detail in [enhancements.md](enhancements.md) under "Architectural follow-ups."

- **`shell.py` is ~3300 lines** and hosts at least four concerns that are conceptually separate: thread-local stdio routing + `_StdoutProxy` + `PythonCommandSlot` + `PipelineSlot` (peer to `process.py`); the per-stage pipeline executor (`_execute_pipeline`, `_execute_stage`, redirect plumbing); the two raw-mode forwarding loops; and the actual REPL + built-ins + completion glue. Everything else in the package is right-sized.
- **Module-global callback registration is the hidden contract between layers.** `pipeline.set_pipeline_executor`, `decorators.set_background_runner`, `pipeline._decorator_value_flag_lookup`, and the `_current_slot` / `_in_pipeline` thread-locals consumed by free `passthrough_*` functions are five independent global setters wired from `Shell.__init__`. Works, but: two `Shell` instances cannot coexist in one process, tests must reset the globals, and the real interface between `Pipeline.run` and `Shell._run_pipeline_from_decorator` is implicit.
- **`shell.py` imports private names from `pipeline.py`** — `_split_on_operators` is used both for completion-stage isolation and for decorator-prefix remainder validation. It is part of `pipeline.py`'s effective public surface; the leading underscore is a leftover.
- **Two near-identical raw-mode forwarding loops** (`_enter_forwarding_mode` for PTY-backed `ProcessSlot`, `_enter_python_forwarding_mode` for `PythonCommandSlot`) duplicate ~80% of their logic — termios snapshot/restore, SIGWINCH/SIGINT install, `\x1d` interception, byte forwarding. Any fix has to be applied twice today.
- **Redirect-open code is duplicated** inside `_execute_pipeline` and `_execute_stage` with subtly different sentinels (`subprocess.STDOUT` vs the string `"stdout"` for `2>&1`). A single `_open_redirects(stage)` helper would unify both call sites.
- **`PipelineSlot` reaches into `PythonCommandSlot` privates** (`_input_request`, `_pty_lock`, `_proxy`, ...) by mirroring `__init__` rather than calling `super().__init__()`. The base class is not subclass-friendly — the inheritance is more "happens to share fields" than "is-a."

The shape of the relief is sketched in `enhancements.md`: extract `slots.py` (Python-command slot family + thread-local routers + passthrough helpers), extract `dispatch.py` (the pipeline executor), unify the two forwarding loops behind a small slot interface, and replace the global setters with a single `ExecutionEnvironment` interface that `Shell` constructs and passes down. None of this is a one-shot refactor — it is a sequence of medium-risk moves, each independently valuable.
