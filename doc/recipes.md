# Writing Completion Recipes

A **recipe** adds TAB completion to an external (system) command — one that runs as a subprocess rather than a Python function registered with `@registry.command`. Recipes live in `src/eosh/recipes/` and are activated in the user's config with `enable("name")`.

> **Before you write a recipe, check the protocol fallbacks.** eosh ships two automatic fallbacks that handle large families of tools without any per-command code:
>
> - **Cobra** — covers Go-based CLIs that expose a `__complete` subcommand: `docker`, `kubectl`, `helm`, `gh`, `argocd`, `k9s`, `doctl`, `linkerd`, `istioctl`, `hcloud`, `op`, `hugo`, `oras`, `gitleaks`, … See [cobra-fallback.md](cobra-fallback.md).
> - **argcomplete** — covers Python CLIs that ship completions via the [argcomplete](https://kislyuk.github.io/argcomplete/) library: `pipx`, `conda`, `pre-commit`, `tox`, `pdm`, `httpie`, `nox`, `virtualenv`, … See [argcomplete-fallback.md](argcomplete-fallback.md).
>
> Both activate automatically — no recipe needed. Write a recipe only when neither fallback applies (most classic Unix tools, or when you want richer UX like multi-select flag pickers).

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

## Output Conventions (recipes that *print*)

Most recipes only add completion — the external tool does the printing. A few
carry Python handlers instead and print resource listings themselves (`awsut`
and its `sagemaker` / `bedrock-agentcore` subtrees are the built-in examples).
Those share one output
contract, defined and documented in
[`src/eosh/recipes/_awsut_common.py`](../src/eosh/recipes/_awsut_common.py).
Read that module's docstring before adding a leaf; the shape in brief:

| Element | Helper | Rule |
|---|---|---|
| Header line | `print_header(*parts)` | `N thing(s) · <scope> · region <r>`, joined with `·`, then a blank line. Empty parts drop out. It prints **whether or not there are rows** — the header is what says "asked, and there were zero". |
| Table | `print_table(header, rows, note=, colorize=)` | ALL-CAPS labels, a `-` rule, two spaces between columns, **widths from the data** so an identifier is never truncated and every cell can be pasted into the next command. No rows draws no table (the note still prints). |
| Note | `note=` on `print_table` | Only facts *about this output*: rows hidden by a filter, rows cut by `--max`, a query that was refused. **Never a legend and never advice** — a sentence that reads the same on every run belongs in the leaf's `help=` (or the owning flag's), where it costs nothing per listing. |
| Detail view | `print_labeled(pairs)` | Opens with the API's own field names, values unwrapped. `None` in place of a pair is a group separator. |
| Named block | `section(label)` | `--- label ---`, for the repeated blocks a fan-out emits (per node, per stream) and for anything non-tabular after a detail view's identifier block. |
| Failure | `guard` + `SmError` | `error: <message>` on stderr, lowercase, resource quoted. `@guard` goes **directly under** `@node.command(...)` with nothing between them. |
| Timestamp | `fmt_time` / `fmt_dur` / `fmt_bytes` | Local, second precision. A `watch` loop stamps its change lines `[HH:MM:SS]`. |

Three consequences worth stating, because each is easy to get wrong:

- **Don't write a bespoke sentence for the empty case.** `print(f"no spaces in
  domain {d}")` reads fine on its own and reads *inconsistent* next to
  `0 instance(s) · region us-west-2`. Let the header report the count and put
  anything situational in `note=`.
- **`_awsut_common` builds no AWS clients**, on purpose: it sits above
  `awsut.py` and above every per-service subpackage (`_awsut_sagemaker/`,
  `_awsut_agentcore/`) so any of them can import it without a cycle. Each
  subpackage's `render.py` re-exports its names, so a module in that subpackage
  reads every instrument off `render`. It also holds what is about *AWS shapes*
  rather than one service — pagination, model introspection, the YAML-ish JSON
  renderer, the watch heartbeat, the completion-context flag readers — since a
  second service group would otherwise copy them; what stays in a `render.py` is
  whatever needs a client or knows that service's vocabulary.
- **Name a shared helper module with a leading underscore.** `enable("*")`
  discovers recipes by globbing `*.py` and calls `register()` on each hit, so a
  support module without one would break config loading for every `enable("*")`
  user. `_discover_all_recipes()` skips any stem starting with `_` — that is the
  whole convention, and it applies to your own `~/.eosh/recipes/_shared.py`
  just as much as to `_awsut_common.py`.
- **Import third-party packages at top level and declare them in an extra.**
  When a recipe's import raises `ModuleNotFoundError` for a dependency,
  `enable("*")` skips that recipe and records it in `recipes.skipped_recipes`
  (`{"awsut": "boto3"}`) instead of aborting the rest of the config; naming the
  recipe explicitly still raises. `awsut` re-raises its boto3 failure with the
  `pip install 'eosh[aws]'` hint, keeping `name=` so the skip still applies.

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
| `ConditionalCompleter(mapping)` | `completion` | Values that depend on a preceding arg |
| `OptionsCompleter(dict, args)` | `completion` | Custom flag completer (only when a recipe has unusual flag rules — the registry auto-builds this from `arg(...)` flag entries) |
| Custom `Completer` subclass | — | Live data from subprocesses or files |
