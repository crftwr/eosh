# Writing Completion Recipes

A **recipe** adds TAB completion to an external (system) command — one that runs as a subprocess rather than a Python function registered with `@registry.command`. Recipes live in `src/eosh/recipes/` and are activated in the user's config with `enable("name")`.

> **Before you write a recipe, check the two protocols.** They handle large families of tools without any per-command code:
>
> - **Cobra** — Go-based CLIs that expose a `__complete` subcommand (`docker`, `kubectl`, `helm`, `gh`, `argocd`, `doctl`, `linkerd`, `istioctl`, `hcloud`, `hugo`, `oras`, …). Opt-in by name, because detecting one would mean running it: the built-in `cobra` recipe lists the well-known tools, and one line covers yours. See [cobra.md](cobra.md).
>
>   ```python
>   # ~/.eosh/recipes/mytools.py  (or straight in config.py)
>   from eosh.recipes import enable_cobra
>
>   def register():
>       enable_cobra("mytool", "othertool")
>   ```
>
> - **argcomplete** — Python CLIs that ship completions via the [argcomplete](https://kislyuk.github.io/argcomplete/) library (`pipx`, `conda`, `pre-commit`, `tox`, `pdm`, `httpie`, `nox`, `virtualenv`, …). Automatic: eosh reads the script for the marker without running it. See [argcomplete-fallback.md](argcomplete-fallback.md).
>
> Write a recipe only when neither applies (most classic Unix tools), or when you want richer UX like multi-select flag pickers.

## Anatomy of a Recipe File

Every recipe file must expose a single `register()` function (no arguments). `enable("name")` imports the module and calls it; the recipe imports the module-level `registry` singleton directly and calls `registry.command(name, params=[...])` with **no handler** — the shell's dispatch path treats handler-less Commands as external recipes and falls through to the system-command path:

```python
# src/eosh/recipes/mytool.py

from ..commands import arg, registry as command_registry
from ..completion import FileCompleter

def register() -> None:
    command_registry.command(
        "mytool",
        help="my-tool — short description",
        params=[
            arg("path", nargs="*", completer=FileCompleter()),
            arg("-v", "--verbose", action="store_true", help="verbose"),
            arg("-o", metavar="FILE", help="output file", completer=FileCompleter()),
        ],
    )
```

`params=[arg(...)]` declares positionals and flags; the registry derives both an argparse parser and the per-position completer dict from the same list. Each `arg()` configures argparse (validation, defaults, action) and TAB completion in one place — `completer=` on a positional drives completion at that position; `completer=` on a value-taking flag drives completion of the value typed after the flag.

## Patterns

The recipes range from one-liners to complex multi-class files. Pick the pattern that fits the command.

---

### Pattern 1 — Flags only

Use this for simple commands whose only useful completions are flags. The flags collected from `arg(...)` entries with leading dashes feed the auto-built `OptionsCompleter`, which gives you multi-select TUI for free.

```python
# Example: ls recipe (abbreviated)
from ..commands import arg, registry as command_registry
from ..completion import FileCompleter

def register() -> None:
    command_registry.command(
        "ls",
        help="list directory contents",
        params=[
            arg("path", nargs="*", help="file or directory", completer=FileCompleter()),
            arg("-l", action="store_true", help="long listing format"),
            arg("-a", action="store_true", help="include hidden files"),
            arg("-h", action="store_true", help="human-readable sizes"),
            arg("-r", action="store_true", help="reverse sort order"),
            arg("-t", action="store_true", help="sort by modification time"),
        ],
    )
```

`nargs="*"` on the positional makes that completer serve every trailing slot — no need to repeat `FileCompleter()` per index. Use `nargs="+"` if at least one value is required, `nargs="?"` for an optional single value.

---

### Pattern 2 — Flags with value arguments

When a flag takes a value (e.g. `-j N`, `-C DIR`), declare it on the same `arg()` entry. The flag's `metavar=` becomes the inline hint shown after the flag is selected; if `completer=` is also provided, the engine opens that picker when the user starts typing the value:

```python
arg("-j", metavar="N", help="number of parallel jobs"),                       # hint only
arg("-C", metavar="DIR", help="change to directory", completer=DirCompleter()),
arg("-f", metavar="FILE", help="read FILE as the Makefile", completer=FileCompleter()),
```

Use a hint-only flag when the value is free-form (a number, a pattern); add a `completer=` when there's a finite or discoverable set of sensible values.

```python
# Example: make recipe (abbreviated)
from ..commands import arg, registry as command_registry
from ..completion import DirCompleter, FileCompleter

def register() -> None:
    command_registry.command(
        "make",
        help="build targets from a Makefile",
        params=[
            arg("target", nargs="*", help="target to build",
                completer=MakeTargetCompleter()),
            arg("-C", metavar="DIR", help="change to directory before doing anything",
                completer=DirCompleter()),
            arg("-f", metavar="FILE", help="read FILE as the Makefile",
                completer=FileCompleter()),
            arg("-j", metavar="N", help="number of parallel jobs"),
            arg("-n", action="store_true", help="dry run — print commands without executing"),
        ],
    )
```

`nargs="*"` on `target` means the same `MakeTargetCompleter()` serves every trailing positional slot, so flag+value pairs interleaved with target names all complete correctly without registering each index by hand.

---

### Pattern 3 — Dynamic completions from a subprocess

When completions depend on live system state, call the appropriate tool in a `Completer` subclass. Always use `timeout` and catch `OSError`/`TimeoutExpired` so a slow or missing tool doesn't hang the prompt.

```python
import subprocess
from ..completion import Completer, Completion, CompletionContext

class GitBranchCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        try:
            result = subprocess.run(
                ["git", "branch", "--all", "--format=%(refname:short)"],
                capture_output=True, text=True, timeout=2.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        prefix = ctx.prefix
        return [
            Completion(value=ln.strip(), description="branch")
            for ln in result.stdout.splitlines()
            if ln.strip().startswith(prefix)
        ]
```

A shared `_run_tool(args, timeout)` helper in the module keeps error handling in one place:

```python
def _run_git(args: list[str], timeout: float = 2.0) -> list[str]:
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    except (OSError, subprocess.TimeoutExpired):
        return []
```

---

### Pattern 4 — Subcommand tree (per-subcommand flags & completers)

Commands with subcommands (git, make's targets, awsut) are best modelled with the **command tree** API: `registry.command()` returns a `Command` whose `.command()` method registers a child sub-command. Each leaf has its own `params=[...]`, so per-subcommand flags and per-subcommand positional completers fall out for free — no hand-rolled dispatcher.

```python
# Example: trimmed-down git recipe
from ..commands import arg, registry as command_registry
from ..completion import Completer, Completion, CompletionContext, FileCompleter

class GitBranchCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]: ...

class GitRemoteCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]: ...

def register() -> None:
    git = command_registry.command("git", help="distributed version control")

    git.command(
        "commit", help="record changes to the repository",
        params=[
            arg("-a", "--all",   action="store_true", help="stage all tracked"),
            arg("--amend",       action="store_true", help="replace tip commit"),
            arg("-m", "--message", metavar="MSG",     help="commit message"),
        ],
    )

    git.command(
        "push", help="update remote refs",
        params=[
            arg("remote", nargs="?", completer=GitRemoteCompleter()),
            arg("branch", nargs="?", completer=GitBranchCompleter()),
            arg("-f", "--force", action="store_true", help="force push"),
            arg("--dry-run",     action="store_true", help="dry run"),
        ],
    )

    # Two-level nesting works the same way:
    stash = git.command("stash", help="stash dirty working state")
    stash.command("apply", help="apply a stash without removing it")
    stash.command("pop",   help="apply and remove the latest stash")

    # Flat sub-commands without their own params are one-liners:
    for sub, desc in [("status", "show the working tree status"),
                      ("init",   "create an empty Git repository")]:
        git.command(sub, help=desc)
```

Completion automatically walks the tree: typing `git push --` lists `push`'s flags; typing `git stash <TAB>` lists `apply` / `pop`. See [subcommands.md](subcommands.md) for the full design and resolution algorithm.

---

### Pattern 5 — Multiple commands in one recipe

A single recipe can register completers for several related commands:

```python
def register() -> None:
    command_registry.command(
        "kill",
        help="terminate processes by PID",
        params=[
            arg("pid", nargs="*", help="process id", completer=ProcessCompleter()),
            arg("-9", action="store_true", help="SIGKILL"),
            arg("-15", action="store_true", help="SIGTERM (default)"),
        ],
    )
    command_registry.command(
        "pkill",
        help="terminate processes by name",
        params=[
            arg("name", nargs="*", help="process name", completer=ProcessNameCompleter()),
        ],
    )
```

---

## Recipes that print — write an add-on instead

A recipe adds completion; the external tool does the printing. Something that
carries its own Python handlers and prints resource listings is an
application, not a recipe — make it an **add-on** under `addons/` (see
[addons.md](addons.md)). `awsut` is the bundled example, with its output
conventions in [addons/awsut/README.md](../addons/awsut/README.md).

Two rules from that world still apply to recipes:

- **Name a shared helper module with a leading underscore.** `enable("*")`
  discovers recipes by globbing `*.py` and calls `register()` on each hit, so
  a support module without one would break config loading for every
  `enable("*")` user. `_discover_all_recipes()` skips any stem starting with
  `_`, and that applies to your own `~/.eosh/recipes/_shared.py`.
- **A missing third-party import is reported, not fatal.** When a user
  recipe's import raises `ModuleNotFoundError`, `enable("*")` prints the
  traceback, records it in `recipes.skipped_recipes`, registers a placeholder
  command that names the missing module, and loads the rest of the config.
  Naming the recipe explicitly still raises.

## Checklist for a New Recipe

1. **Create `src/eosh/recipes/<name>.py`** with a `register()` function (no arguments — import `arg` and `registry` from `..commands`).
2. **Update the `Available recipes:` block** in `src/eosh/recipes/__init__.py` so `enable("*")` users see what they got.
3. **Cover common flags** with `arg(...)` entries. Include both long and short forms where both exist (`arg("-f", "--force", ...)`).
4. **Use `metavar=` and `completer=` for value-taking flags** — `metavar="N"` alone for numerics/free-text, `metavar="FILE", completer=FileCompleter()` when a picker is useful.
5. **Use `nargs="*"` (or `"+"`)** on a positional to make one completer serve every trailing slot — no need to register each index.
6. **Protect subprocess calls** with `timeout` and catch `OSError`/`TimeoutExpired`.
7. **Don't cache at module level** (module is imported once per `enable()` call). Cache inside completer instances or use a module-level dict keyed by the relevant state.
8. **Test it** by enabling it in `~/.eosh/config.py` and exercising TAB at each argument position.

## Completers Available for Recipes

| Completer | Import | Use for |
|-----------|--------|---------|
| `FileCompleter()` | `completion` | Any file argument |
| `DirCompleter()` | `completion` | Directory-only arguments (`-C DIR`) |
| `ChoiceCompleter(list)` | `completion` | Static value lists |
| `CallbackCompleter(fn)` | `completion` | Dynamic value list from a `() -> list[str]` |
| `OptionsCompleter(dict, args)` | `completion` | Custom flag completer (only when a recipe has unusual flag rules — the registry auto-builds this from `arg(...)` flag entries) |
| Custom `Completer` subclass | — | Live data from subprocesses or files |
