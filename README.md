# Eolith Shell

**Eolith Shell** (`eosh`) is a lightweight but powerful terminal shell environment with rich tab completion and context switching.

<!-- pypi-exclude-start -->
<p>
  <a href="https://pypi.org/project/eosh/">
    <img src="doc/images/install-pypi.svg" alt="Install Eolith Shell from PyPI with pip" />
  </a>
</p>
<!-- pypi-exclude-end -->

![Eolith Shell demo: TAB completion with descriptions, flag completion, and switching contexts while a build keeps running](doc/images/demo.gif)

## Features

- **Rich tab completion** — per-argument completers with descriptions, inline picker UI
- **Flag completion** — TAB on `-` lists every flag with its description; a flag that takes a value goes straight on to completing that value
- **Context switching** — named environments with variables and working directories, switched by name or live with `Ctrl+]`
- **PTY process multiplexing** — run processes in contexts and switch between them without killing them
- **Pipelines and redirections** — `|`, `>`, `>>`, `<`, `2>`, `2>&1`, `;`, `&&`, `||`, globs (`*`, `?`, `**`), and `\`-line-continuation
- **Pipeline decorators** — wrap any pipeline with `@watch`, `@time`, `@retry`, `@quiet`, or `@bg`; authoring your own is a few lines of Python
- **Custom commands** — define Python functions as shell commands with full completion support
- **Python-backed variables** — `var aws_region=us-east-1` can drive multiple `os.environ` keys via a `Var` subclass; `$NAME` expansion is symmetric
- **Completion recipes** — opt-in TAB completion for `git`, `make`, `ssh`, `aws`, and more
- **Protocol fallbacks** — automatic completion for cobra-based tools (`docker`, `kubectl`, `helm`, `gh`, …) and argcomplete-based Python CLIs (`pipx`, `conda`, `pre-commit`, `tox`, …) — no recipe needed
- **System command fallback** — anything not a registered command runs through the system shell
- **Cross-platform** — interactive shell, completion, pipelines, redirects, contexts, and history all work on POSIX and Windows; PTY-backed multiplexing of running native processes is POSIX-only
- **History** — persistent history with up/down navigation, `Ctrl+R` search, and past command lines offered as multi-argument TAB candidates, scoped to the context and directory you're in
- **Desktop notifications** — an OS notification when a command that took more than 10 seconds finishes, including ones running in a backgrounded context; native backends only, no extra dependencies

## Installation

Requires Python 3.12+. The core has no dependencies beyond the standard library.

```bash
pip install eosh               # or: pipx install eosh / uv tool install eosh
pip install "eosh[awsut]"      # + the `awsut` add-on (boto3, pexpect)
uv tool install "eosh[awsut]"  # same, as an isolated uv tool
```

On first launch `eosh` writes a starter `~/.eosh/config.py` that enables
every built-in recipe and bundled add-on. Without the `[awsut]` extra,
`awsut` is skipped and everything else loads normally; running `awsut` then
says which module is missing and which extra provides it.

`uv tool` and `pipx` install eosh into its own virtualenv, so a
`pip install boto3` in another shell does not reach it — add packages with
`uv tool install eosh --with <pkg>` or `pipx inject eosh <pkg>`. With
`uv tool`, `--with` *replaces* the previous list, so repeat any earlier
entries (`uv tool list --show-with` shows them).

### From source

```bash
git clone https://github.com/crftwr/eosh
cd eosh
make install      # .venv/ with `pip install -e ".[dev]"`
make test
make run
```

## Usage

```bash
eosh
```

### Built-in Commands

| Command | Description |
|---------|-------------|
| `cd [path]` | Change directory (default: home) |
| `help [command]` | Show help for a command or list all commands |
| `context` | Manage contexts (see below) |
| `var [NAME[=VALUE] ...]` | List variables, print one (`var NAME`), set (`var NAME=VALUE`), or unset (`var NAME=`) |
| `alias [NAME[=EXPANSION] ...]` | List aliases, show one, or define a shorthand for a command |
| `unalias NAME [NAME ...]` | Remove aliases |
| `source-bash [FILE [ARG ...]]` | Run a bash script (or a pasted block) and import its variables and cwd |
| `reload` | Reload `~/.eosh/config.py` without restarting |
| `exit` | Exit the shell |

Any command not listed above is passed through to the system shell (e.g., `ls`, `git`, `grep`).

### Aliases

An alias replaces the first word of a command line. Quote the expansion when it contains spaces:

```
eosh> alias hp='awsut sagemaker hyperpod'
eosh> hp list                  # runs: awsut sagemaker hyperpod list
eosh> alias                    # list all aliases
eosh> unalias hp
```

### Importing Variables from Bash

`source-bash` runs a script in a real `bash` — so `export`, `$(…)`, loops, heredocs and conditionals all work — and imports the environment and working directory it leaves behind, the way bash's own `source` would:

```
eosh> source-bash              # paste `export KEY=VALUE` lines, end with a blank line or Ctrl+D
eosh> source-bash setup.sh s3  # source a file with arguments
eosh> source-bash -c 'export A=$(date +%s)'
```

`--no-cd` keeps the current directory and `-q` suppresses the summary of imported variable names. Shell functions, aliases and shell options can't be imported.

### Contexts

Contexts let you define named environments with variables that are exported to `os.environ` and a remembered working directory.

```
eosh> context new prod
Created context 'prod'
[prod] eosh> var ACCOUNT=123456 REGION=us-east-1
[prod] eosh> context new staging
Created context 'staging'
[staging] eosh> var ACCOUNT=789012 REGION=us-west-2
[staging] eosh> context list
  * staging {'ACCOUNT': '789012', 'REGION': 'us-west-2'}
    prod {'ACCOUNT': '123456', 'REGION': 'us-east-1'}
    default
[staging] eosh> context close
Closed 'staging', now in 'prod'
```

Context subcommands:

| Subcommand | Description |
|------------|-------------|
| `context new <name>` | Create a context (inheriting the current one's variables and history) and switch to it |
| `context close [name]` | Remove a context (default: the current one); the most recently used remaining one becomes current |
| `context switch <name>` | Switch to an existing context |
| `context list` | Show all contexts with their state and variables |
| `context kill <name>` | Send SIGTERM to the running process in a context |

### Context Switching with Ctrl+]

Press `Ctrl+]` at the shell prompt (or while a process is running) to open a TUI context picker:

- Arrow keys or `Ctrl+P/N` to navigate
- **Enter** to switch to the selected context
- **Esc** / `Ctrl+C` to cancel
- Select `+ new context` to create a new one

If the target context has a running process, switching to it resumes that process immediately. The original process keeps running in the background — visible as `[bg:1]` in the prompt.

### Tab Completion

Press TAB to complete:
- Command names (registered commands + system PATH executables)
- File/directory paths (default fallback)
- Custom per-argument completions defined by commands
- Past command lines from history (see below)

**History completion** — TAB also offers past command lines that start with
what you've typed so far. Only the part that would be *added* is listed, like
any other candidate, tagged `history` and shown first:

```
eosh> git commit <TAB>
┌────────────────────────────────────────────────┐
│ -m "fix typo"                      history     │
│ --amend --no-edit                  history     │
│ doc/                                           │
│ src/                                           │
└────────────────────────────────────────────────┘
```

Accepting one inserts it at the cursor verbatim, so a single suggestion can fill
in several arguments at once. Matching is against the entire typed line —
including pipelines (`ls | grep fo<TAB>`) — and draws on the current context's
history, the same list `↑`/`↓` walks (`Ctrl+R` searches every context). A
history candidate is never inserted without being shown in the picker first, and
a unique ordinary completion still applies on the first TAB as before.

Candidates are also scoped to the **directory** you're in: eosh records where
each command was run (`~/.eosh/history.dirs`) and offers only the lines you ran
here, so another checkout's `make deploy` stays out of the way. Nothing matching
run here means no history rows — the picker just shows the ordinary candidates.
`↑`/`↓` and `Ctrl+R` are not directory-scoped, so lines from elsewhere are still
one key away.

**Flag completion** — TAB on `-` lists the command's flags as ordinary picker
rows, each with its description:
- Type to narrow, Down/Up and **Enter** to pick — one flag per TAB
- A flag that takes a value is shown as `-d <N>`; picking it inserts `-d ` and
  goes straight on to completing the value (a directory picker for `tar -C`,
  say). When the value has no completer, the status bar shows what to type.

### Pipelines, Redirects, and Sequencing

eosh supports the operators you'd expect from a POSIX shell:

| Operator | Meaning |
|----------|---------|
| `cmd1 \| cmd2` | Pipe stdout of `cmd1` into stdin of `cmd2` |
| `cmd > file` / `>> file` | Redirect stdout (truncate / append) |
| `cmd < file` | Redirect stdin from a file |
| `cmd 2> file` / `2>> file` | Redirect stderr |
| `cmd 2>&1` | Merge stderr into stdout |
| `cmd1 ; cmd2` | Sequence (run both regardless of exit code) |
| `cmd1 && cmd2` | Run `cmd2` only if `cmd1` succeeded |
| `cmd1 \|\| cmd2` | Run `cmd2` only if `cmd1` failed |
| `*`, `?`, `**` | Glob expansion (recursive `**` supported) |
| `\` at end of line | Continue command on the next line (one history entry) |

```
eosh> ls *.py | grep test | wc -l
eosh> make 2>&1 | tee build.log
eosh> echo hello > out.txt && cat out.txt
```

Both registered Python commands and external programs work seamlessly inside pipelines.

### Pipeline Decorators

A **decorator** is a token of the form `@name [flags]` at the start of a line that wraps the rest of the line as a pipeline and modifies how it runs. The leading `@` keeps the syntax visually distinct so it never collides with a regular command name.

```
@watch ls                              # bare single-command body
@watch -n 1 {df -h | grep abc}         # braced body required when operators appear
@time {make && ./run-tests}
@retry -n 5 --delay 2 curl https://flaky.example.com/
@quiet pytest -q
@bg {tail -f /var/log/system.log}      # run in a fresh background context
```

**Scope rule.** If the wrapped pipeline contains `|`, `;`, `&&`, `||`, or a redirect, it must be enclosed in `{...}`. Single-command bodies don't need braces. This makes the decorator's scope visible at a glance and side-steps the `watch -n 5 ls | grep abc` ambiguity that POSIX `watch` is famous for.

**Built-in decorators:**

| Decorator | Description |
|-----------|-------------|
| `@watch [-n SEC] [--no-clear]` | Repeatedly run a pipeline until interrupted |
| `@time` | Print elapsed wall/user/sys time after the pipeline finishes |
| `@retry [-n N] [--delay SEC]` | Re-run the pipeline on non-zero exit, up to `N` attempts |
| `@quiet [--stderr]` | Discard stdout (and stderr with `--stderr`); still propagates the exit code |
| `@bg [name]` | Run the pipeline in a fresh background context (resumable via `Ctrl+]`) |

See the [Custom Decorators](#custom-decorators) section below for authoring your own.

## Customization

Create `~/.eosh/config.py` to define custom commands and completers. This file is plain Python that imports from eosh. Use `reload` to apply changes without restarting.

```python
# ~/.eosh/config.py
from eosh.commands import registry, arg
from eosh.completion import Completer, Completion, ChoiceCompleter
from eosh.recipes import enable

# Enable TAB completion for system commands
enable("make", "git", "ssh")

class InstanceCompleter(Completer):
    def complete(self, ctx):
        account = ctx.args[0] if ctx.args else ctx.shell_context.get_variable("ACCOUNT")
        # fetch instances for account...
        return [Completion(value="i-abc123", description="web-server-1")]

@registry.command(
    name="connect",
    help="SSH into an EC2 instance.",
    params=[
        arg("account", choices=["prod", "staging"]),
        arg("region",  choices=["us-east-1", "us-west-2"]),
        arg("instance_id", completer=InstanceCompleter()),
    ],
)
def connect(account, region, instance_id):
    import os
    os.system(f"ssh {instance_id}")
```

### Python-Backed Variables

Register a variable with `var_registry`, and the built-in `var` command, bare `NAME=VALUE` assignment and `$NAME` / `${NAME}` expansion all go through it — a Python-backed variable is read- and write-symmetric with `os.environ`. An `EnvVar` names one or more environment keys (the shell writes all of them, per context). For a value that should stay out of child processes, subclass `PyVar` (still per-context) or `GlobalVar` (one value for the whole shell).

```python
# ~/.eosh/config.py
from eosh import EnvVar, var_registry
from eosh.completion import ChoiceCompleter, CallbackCompleter

var_registry.register(EnvVar(
    "aws_region", keys=["AWS_REGION", "AWS_DEFAULT_REGION"],
    completer=ChoiceCompleter(["us-east-1", "us-west-2", "eu-west-1"]),
    description="AWS region — sets AWS_REGION + AWS_DEFAULT_REGION",
))
var_registry.register(EnvVar(
    "aws_profile", keys="AWS_PROFILE",
    completer=CallbackCompleter(lambda: ["default", "prod", "staging"]),
))
```

With the variables above:

```
eosh> var aws_region=us-west-2
eosh> echo $AWS_REGION
us-west-2
eosh> aws ec2 describe-instances --region $aws_region
```

### Custom Decorators

To author your own decorator, decorate a function with `decorator_registry.decorator(...)`. The function receives the wrapped `Pipeline` as its first positional argument and the parsed flag namespace as kwargs; call `pipeline.run()` to execute the body and return the exit code.

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

Usage:

```
eosh> @repeat -n 5 --delay 1 ls
eosh> @repeat -n 3 {make && ./run-tests}
```

To share decorators across machines or teammates, drop a module under `~/.eosh/decorators/<name>.py` that defines `register()` (same shape as the built-ins) and call `enable()` from `config.py`:

```python
# ~/.eosh/config.py
from eosh.decorators import add_decorator_path, enable as enable_decorators

add_decorator_path("/team/shared/decorators")   # optional extra directory
enable_decorators("repeat")                     # found in ~/.eosh/decorators/
                                                # or /team/shared/decorators/
```

### Spawning Interactive Subprocesses

If a custom command needs to spawn a subprocess that reads from the user (SSH-like sessions, TUIs, MFA prompts, anything that calls `getpass`), wrap the call with `passthrough_run` — *not* `subprocess.run`:

```python
from eosh import passthrough_run

@registry.command(name="my_ssm", ...)
def my_ssm():
    passthrough_run(["aws", "ssm", "start-session", "--target", "i-abc123"])
```

Plain `subprocess.run` would have the main shell thread and the subprocess both reading from the real terminal, splitting keystrokes between them; `passthrough_run` allocates a slot-owned PTY for the child so input flows cleanly.

For reading a single line of input back from the user, use `passthrough_input(prompt)` instead of plain `input()`:

```python
from eosh import passthrough_input

answer = passthrough_input("Continue? [y/N] ")
```

Outside a Python command thread, both helpers fall back to the obvious thing (`subprocess.run` and `input`), so the same code is safe in either context. Non-interactive subprocesses (`capture_output=True`, pipeline stages with explicit pipes, `pexpect.popen_spawn`, …) don't need wrapping.

### Desktop Notifications

When a command runs for at least 10 seconds, eosh posts an OS notification as it finishes — by then you've probably switched to a browser:

```
✓ eosh — 1m 23s          make -j8 release
✗ eosh — exit 2 (20.0s)  make
✓ eosh — 1h 05m          [bg-1] terraform apply
```

Commands running in a context you backgrounded with `Ctrl+]` (or with `@bg`) notify too, tagged with the context name. Interactive programs — editors, pagers, `top`, `ssh`, `tmux`, sub-shells — are skipped, since sitting in them for an hour isn't work finishing.

Backends are whatever the platform already ships: `osascript` on macOS, `notify-send` on Linux, a PowerShell toast on Windows, and the terminal bell as a fallback. Nothing to install.

Control it at the prompt:

```
eosh> var notify=off              # disable for this session
eosh> var notify_threshold=30     # only notify for commands ≥ 30s
```

Or from your config:

```python
# ~/.eosh/config.py
from eosh import notify

notify.configure(threshold=30)
notify.SKIP_COMMANDS.add("psql")

# Or replace the backend entirely — Slack, ntfy.sh, tmux display-message, ...
notify.set_notifier(lambda title, message: post_to_slack(f"{title}\n{message}"))
```

See [doc/notifications.md](doc/notifications.md) for the design and the known limits.

### Prompt Customization

The prompt is generated by a Python function you can override with `set_prompt()`. The default prompt shows the context name (if not `"default"`), current directory (up to 2 levels), a timestamp, and `[bg:N]` when N other contexts have running processes:

```
[prod] projects/eosh 14:32:07>
```

To customize, define a function that takes a `ContextManager` and returns a string:

```python
# ~/.eosh/config.py
import os
from datetime import datetime
from eosh import set_prompt

def my_prompt(context_manager):
    ctx = context_manager.current()
    prefix = f"({ctx.name}) " if ctx else ""
    cwd = os.path.basename(os.getcwd()) or "/"
    time = datetime.now().strftime("%H:%M")
    return f"{prefix}{cwd} [{time}]$ "

set_prompt(my_prompt)
```

The function is called each time the prompt is displayed, so it reflects dynamic state like the current directory, time, or context variables.

### Available Completers

| Completer | Description |
|-----------|-------------|
| `ChoiceCompleter(items)` | Complete from a static list |
| `CallbackCompleter(func)` | Complete from a function's return value |
| `FileCompleter()` | Complete filesystem paths (files and directories) |
| `DirCompleter()` | Complete directory paths only |
| `OptionsCompleter(options, args)` | Complete flags, one picker row each; `args` declares value-taking flags |
| `HistoryCompleter(history_fn, limit, ran_here_fn)` | Continue the typed line from past command lines (may span several arguments), scoped to the ones run in the cwd |

### Completion Recipes

Built-in recipes add TAB completion for common system commands. Enable them in `~/.eosh/config.py`:

```python
from eosh.recipes import enable
enable("git", "make", "ssh", "kill", "ls", "grep", "find", "du", "df", "tail", "aws")
```

Each recipe registers flag completion (via `OptionsCompleter`) and positional completions (subcommands, files, branches, etc.) for the named command.

Two protocols cover whole families of tools without a per-tool recipe:

- **Cobra-based tools** (`docker`, `kubectl`, `helm`, `gh`, `argocd`, …) — `CobraCompleter` drives their `__complete` subcommand, including live resource enumeration (running containers, k8s resources, GitHub issues, …). Opt-in by name: `enable("cobra")` covers the well-known tools, and `enable_cobra("mytool")` adds your own. See [doc/cobra.md](doc/cobra.md).
- **argcomplete-based Python CLIs** (automatic) (`pipx`, `conda`, `pre-commit`, `tox`, `pdm`, `httpie`, …) — `ArgcompleteCompleter` detects the `# PYTHON_ARGCOMPLETE_OK` marker and drives the argcomplete protocol. See [doc/argcomplete-fallback.md](doc/argcomplete-fallback.md).

#### User-Defined Recipes

You can write your own recipes and place them in `~/.eosh/recipes/` (or any directory you add to the search path). `enable()` checks the search path automatically after the built-ins, so the call site in `config.py` is identical:

```python
from eosh.recipes import enable
enable("git")          # built-in
enable("my_tool")      # found in ~/.eosh/recipes/my_tool.py
```

A recipe file must define a `register()` function:

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
    # Return dynamic values (cached, fetched from an API, etc.)
    return ["web", "worker", "scheduler"]
```

#### Recipe Search Path

The default search path contains only `~/.eosh/recipes/`. Call `add_recipe_path()` to add more directories — useful for sharing recipes across a team:

```python
from eosh.recipes import add_recipe_path, enable

add_recipe_path("/team/shared/recipes")   # checked after ~/.eosh/recipes/
enable("my_tool")   # found in whichever directory contains my_tool.py first
```

Lookup order for every `enable()` call:

1. Built-in package (`eosh.recipes.<name>`) — always highest priority
2. `~/.eosh/recipes/<name>.py` — personal recipes
3. Additional paths in the order they were added via `add_recipe_path()`

You can also read or modify `recipe_search_path` directly (it is a plain `list[Path]`).

A recipe may import third-party packages, but they must be installed in
eosh's own environment (see [Installation](#installation) for `uv tool` /
`pipx`). When one is missing, `enable("*")` skips that recipe instead of
failing the whole config and registers a placeholder command under the
recipe's name (unless that name is already a command or an executable on
`PATH`); running it says which module is missing and how to install it.
Naming the recipe explicitly — `enable("my_tool")` — still raises.

Any error in one of *your* recipes — a typo in an import (even one inside a
helper module the recipe imports), an exception in `register()`, a syntax
error — is printed at startup with a traceback through your files, and under
`enable("*")` the remaining recipes still load.

### Writing a Custom Completer

Subclass `Completer` and implement `complete()`. The `CompletionContext` gives you:

- `command` — the command being completed for
- `args` — previously completed arguments
- `arg_index` — which argument position is being completed
- `prefix` — partial text typed so far
- `shell_context` — the active context (access variables with `.get_variable()`)

To add completion to a system command without wrapping it, register a handler-less command — execution falls through to the real binary:

```python
from eosh.commands import arg, registry
from eosh.completion import FileCompleter

registry.command(
    "mytools",
    help="my custom tool",
    params=[
        arg("file", nargs="*", completer=FileCompleter()),
        arg("-v", "--verbose", action="store_true", help="verbose"),
        arg("--output", metavar="FILE", help="output file"),
    ],
)
```

### Key Bindings

| Key | Action |
|-----|--------|
| `Tab` | Open completion picker |
| `Ctrl+R` | Search history |
| `Ctrl+]` | Open context switcher |
| `↑` / `↓` or `Ctrl+P/N` | Navigate history |
| `Ctrl+A` / `Ctrl+E` | Move to start / end of line |
| `Ctrl+B` / `Ctrl+F` | Move one character left / right |
| `Alt+B` / `Alt+F` | Move one word left / right |
| `Ctrl+W` | Delete word before cursor |
| `Ctrl+K` | Delete to end of line |
| `Ctrl+U` | Delete to beginning of line |
| `Ctrl+L` | Clear screen |
| `Ctrl+D` | Exit (on empty line) |

## File Locations

| Path | Purpose |
|------|---------|
| `~/.eosh/config.py` | User configuration |
| `~/.eosh/history` | Command history |
| `~/.eosh/history.dirs` | Directories each history line was run in (scopes history TAB candidates) |
| `~/.eosh/recipes/<name>.py` | User-defined completion recipes (loaded by `enable("<name>")`) |
| `~/.eosh/decorators/<name>.py` | User-defined pipeline decorators (loaded by `enable("<name>")`) |

## Platform Support

The interactive shell — line editing, completion, all TUI pickers, history, pipelines, redirects, built-ins, decorators, and `Ctrl+]` context switching at the prompt — runs natively on both POSIX and Windows. Path separators are normalized to `/` on every platform (Git-Bash style) so a path can never be mistaken for a `\`-line-continuation.

The one POSIX-only feature is **PTY-backed multiplexing of a live external process**: backgrounding a *running* native program with `Ctrl+]` and resuming it later. On Windows, external commands run on the real console with inherited stdio.

## License

MIT — see [LICENSE](LICENSE).
