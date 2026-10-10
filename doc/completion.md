# Completion Engine Design

## Overview

The completion engine provides context-aware tab completion for all shell input. It is designed to be deeply customizable — each command declares completers for each argument position, and each completer receives full parse state to make intelligent suggestions.

## Core Types

### CompletionContext

Every completer receives a `CompletionContext` with full awareness of what's been typed:

```python
@dataclass
class CompletionContext:
    command: str | None        # command name (None if completing command itself)
    args: list[str]            # all preceding arguments (already completed)
    arg_index: int             # which argument position is being completed
    prefix: str               # partial text of current argument being completed
    line: str                 # full raw line
    shell_context: ShellView | None  # current context, read-only
```

The `shell_context` field is a read-only `ShellView` of the active context — `get_var(name)` (resolved like `$name`), `context_name`, `cwd` — so completions can adapt to the current environment (account, region) without explicit arguments.

### Completion

```python
@dataclass
class Completion:
    value: str              # the text inserted on selection
    display: str = ""       # label shown in completion menu (defaults to value)
    description: str = ""   # metadata shown beside the completion
    fields: tuple[str, ...] = ()  # description split into columns (aligned across rows)
    arg_hint: str = ""           # non-empty for a flag that takes a value ("N"): applying
                                 # it moves straight on to completing that value
```

Every candidate's `value` starts at the **completion anchor** — the position the
current token starts at (`parsing.raw_token_start`), which is where the editor
splices it in.

#### Metadata columns (`fields`)

A completer whose metadata is several facts rather than one sentence hands them
over as `fields` instead of gluing them into `description`. The picker pads each
cell to the widest one in that column (2-space gap, via
`tui._meta_col_widths` / `tui._compose_meta`), so the facts line up down the
list; `Completion.meta` is what the picker reads — `fields` when set, the plain
`description` otherwise, which counts as a single column.

```python
Completion(value="data-prep-space", fields=("Private", "JupyterLab", "no app"))
```

```
data-prep-space         Private  JupyterLab  app InService     ← aligned columns
data-prep-space-shared  Shared   JupyterLab  no app
```

A column that is empty in **every** row is dropped entirely, so an optional
field (a status worth showing only when it is unusual) costs nothing on the
lists that don't use it. A row that leaves it empty while another row fills it
pads through, keeping the later columns aligned.

Two properties come out of this that a separator inside `description` cannot
give: the eye can scan one fact down the list, and the separator's own width
(`" · "` = 3 columns on every row, on a line already sharing space with the
command being typed) is gone. Alongside it, the rule for *what* goes in the
metadata at all: a fact no leaf branches on is a column of noise — the space
completer drops the owner profile for that reason, and a description that would
read the same on every row (`"log stream"` under `--stream`) is left out
entirely.

### Completer Protocol

```python
class Completer(ABC):
    @abstractmethod
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        """Return completions for the current position."""
        ...

    def should_activate(self, ctx: CompletionContext) -> bool:
        """Optional guard — return False to skip this completer dynamically."""
        return True
```

The `should_activate` guard allows a completer to be registered at a position but only engage under certain conditions (e.g., only complete context names after `context switch`, not after `context list`).

## Built-in Completers

### FileCompleter

Completes filesystem paths relative to the current directory. Handles:
- Directory prefix expansion (`src/` lists contents of `src/`)
- Hidden file filtering (only shown when prefix starts with `.`)
- Directory suffix (`/` appended to directory completions)
- Case-insensitive matching

### DirCompleter

Like `FileCompleter` but only returns directories. Used for flags that take a directory path (e.g. `du -C DIR`).

### CommandNameCompleter

Completes command names from two sources:
1. Registered commands in the `CommandRegistry`
2. Executable files on `$PATH` (only searched when prefix is non-empty, to avoid flooding)

Results are labeled `"command"` or `"system"` in the description field.

### ChoiceCompleter

Completes from a static list of strings. Simple but covers many cases (subcommands, enum-like arguments, known account names).

```python
ChoiceCompleter(["us-east-1", "us-west-2", "eu-west-1"])
```

### CallbackCompleter

Completes from a function's return value. The function is called on each completion attempt, enabling dynamic lists:

```python
CallbackCompleter(lambda: get_current_branches())
```

### OptionsCompleter

Completes command-line flags. Built on demand from a command's `params` (`Command.options_completer()`); it answers at any argument position where the user types a `-`-prefixed token.

```python
OptionsCompleter(
    options={
        "-l": "long format",
        "-a": "show hidden",
        "--color": "colorize output",
        "-d": "max depth",
    },
    args={
        "-d": "N",               # hint only — user types the value
        "--color": ("WHEN", ChoiceCompleter(["always", "auto", "never"])),
        # tuple form: (hint, value_completer) → opens a picker for the value
    },
)
```

Flags are ordinary rows in the same `InlinePicker` as every other candidate: typing narrows, Down/Up + Enter picks, one flag per TAB. A value-taking flag is displayed as `-d <N>` and carries `arg_hint`; picking it (or a lone one auto-applying) inserts `-d ` and `lineedit._complete` loops straight on to the value — the flag's value completer if one is registered, otherwise nothing, in which case the status bar already reads `-d <N>: …` (`Shell._get_arg_info`). Combined short flags (`-al`) are typed by hand.

This replaced a separate multi-select checkbox picker and an after-TAB "arg hint" line (discussion #36): a second copy of the picker's key handling and layout, three extra `Completion` fields every consumer had to respect, and a hint that repeated the status bar word for word.

`OptionsCompleter` also handles:
- **Flag deduplication** — flags already present in `ctx.args` are excluded
- **Short-flag cluster parsing** — `-hs` in `ctx.args` is treated as both `-h` and `-s` already used
- **Preceding-flag hint** — when the last completed arg is a value-taking flag and the user presses TAB without typing `-`, the engine shows a hint instead of opening a picker

### OverlayCompleter

Adds candidates to a completer instead of replacing it. Use it for a tool with
its own completion protocol (a `delegate`) that leaves some slots unanswered:

```python
delegate=OverlayCompleter(
    AwsCompleter(),                                    # flags, services, operations
    _AwsS3PathArg(S3PathCompleter(), _S3_REMOTE_OPS),  # s3:// on `aws s3 <op>` paths
    _AwsS3PathArg(FileCompleter(), _S3_LOCAL_OPS),     # local paths on cp / mv / sync
)
```

- **When an extra runs.** Each extra's `should_activate` decides where it
  applies.
- **Duplicates.** A value already offered keeps its first entry.
- **Future-proof.** Because the extras add rather than replace, they keep
  working if the base tool later starts answering in the same slot too.

### S3PathCompleter

`eosh.recipes.aws.S3PathCompleter` completes `s3://bucket/key` (discussion #33).
Put it on any argument that takes an S3 URI:
`arg("src", completer=S3PathCompleter())`.

- **What it lists.** First the buckets, then one "directory" level of keys at a
  time, using `/` as the delimiter. Size and date are shown as `fields`.
- **How it lists.** Through the `aws` CLI (`s3api list-buckets` /
  `list-objects-v2`), so the core needs no boto3. It honours a `--profile` or
  `--region` already typed on the line.
- **Caching.** Results are cached per account. One listing serves every
  keystroke that narrows within the same level.
- **Failures.** An error or a timeout is reported once and cached as empty
  until the next command runs.
- **Before `s3://` is typed.** On an empty word, or one that could still become
  `s3://` (`s`, `s3:`), it offers `s3://` itself and doesn't call AWS.

The `aws` recipe uses it only on the path arguments of `aws s3 ls|cp|mv|rm|sync|rb|presign`.
It is never applied by scheme to every command, because most commands don't
accept an S3 URI.

Past command lines are not completion candidates: they come back as the line
editor's ghost suggestion, through Ctrl+R, and through `history` — see
[history.md](history.md).

## How TAB Completion Works

The line editor (`lineedit.py`) calls `_get_completions(line_before_cursor)` on every TAB press. The shell implements this as:

```
_get_completions(line_before_cursor)
  → _split_on_operators() → isolate current pipeline stage
  → split_for_completion(stage) → (tokens, prefix)
  → No tokens?
      → CommandNameCompleter
  → Has tokens?
      → Look up command; Command.resolve(args) → deepest sub-command + its args
        (a flat command resolves to itself)
      → _resolve_slot() classifies the token — the same call the status bar makes:
          delegate   → node.delegate.complete(ctx)        (aws_completer, cobra)
          flag       → node.options_completer().complete(ctx)
          value      → the flag's value completer, or [] (the status bar says what to type)
          subcommand → node's children names
          positional → node.positional_completer(i).complete(ctx)
      → No completer at this slot (positional without one, or unknown command)?
          → Try ArgcompleteCompleter (if command is an argcomplete-marked Python script)
          → Still nothing → FileCompleter
```

**The argcomplete fallback** runs only where no completer is registered. A registered completer that returned `[]` meant "nothing here". It finds argcomplete tools by reading the script, never by running it, and caches that per command. **Cobra tools are not a fallback.** Running an unknown command with `__complete` to find out would execute it, so they are opted in by name and get a `CobraCompleter` as their `delegate`, like any other recipe. See [cobra.md](cobra.md) and [argcomplete-fallback.md](argcomplete-fallback.md).

Once completions are returned to the line editor:

| Situation | Behaviour |
|-----------|-----------|
| Zero completions | Do nothing |
| Single completion | Apply immediately; if it has `arg_hint`, loop again to complete the flag's value |
| Several | Open `InlinePicker` (narrows as user types more characters); picking a row with `arg_hint` also loops on to its value |

Auto-apply on a single completion only fires on the **initial** TAB press.
If the user is narrowing inside an open picker and the candidate count
drops to one, the picker stays open on that lone item — the user can't
see the count cross the threshold mid-typing, so a sudden close + insert
would feel like the shell is finishing the word for them out of nowhere.
Press Enter to apply, or TAB to extend the common prefix explicitly.

### Nothing is selected until the user selects it

Both completion pickers open with **no row highlighted** (`select_first=False`
on the widget). Enter on a freshly opened list therefore inserts nothing — it
just dismisses the list, leaving the line exactly as typed. To accept a
candidate the user makes the choice explicit:

- `Down` / `Ctrl+N` (or `Up` / `Ctrl+P` to enter at the bottom), then `Enter`
- `TAB` on a list narrowed to a single candidate accepts it outright

The alternative — highlighting the first candidate on open — means a reflexive
Enter silently rewrites the argument the user just typed.

`TAB` inside an open picker *only* types the longest shared prefix of the
remaining candidates. When there is nothing left to extend it does nothing:
moving the selection is the user's job, and a TAB that quietly highlighted a
row would re-introduce the same surprise from the other direction.

### An empty candidate list never stays open

While a picker is open, typed characters are echoed by the *picker*; they are
committed to the line buffer when it closes. Two rules keep that from losing
input:

1. If typing (or backspacing) narrows the list to zero candidates, the picker
   closes itself (`InlinePicker.closed_empty`). Previously it stayed open
   rendering zero rows — invisible, but still consuming keystrokes, and Enter
   then discarded everything typed since TAB.
2. `lineedit._complete` commits `picker.typed` into the
   buffer on **every** exit path — accept, dismiss, Esc, or empty-close — so
   the redraw on return can never erase characters the user saw echoed.

The invisible-picker problem is really "nothing left to render", so a caller
that *can* render something opts out of rule 1 with `empty_placeholder=`. The
`Ctrl+R` history search does: its query isn't a token in the buffer — it lives
only inside the picker — so closing on the first non-matching keystroke would
throw the whole query away, and the next keystroke after a query that matches
nothing is almost always the Backspace that fixes it. With the placeholder set,
the picker keeps a visible row reading `(no matches)`, the query survives, and
Backspace widens the filter again.

The **fallback to `FileCompleter`** only triggers when **no completer** is registered for that position. If a completer is registered but returns empty results, no fallback occurs — commands can explicitly declare "no completions here" by registering a completer that returns `[]`.

## Per-Argument Binding

Python commands declare arguments via a single `params=[arg(...)]` list. Each `arg()` configures argparse (validation, type coercion, defaults, action) **and** TAB completion in one place — `completer=` on a positional drives completion of the value at that position; `completer=` on a value-taking flag drives completion of the value typed after the flag. Completion reads the list on demand (`Command.options_completer()`, `Command.positional_completer(i)`); nothing is pre-derived.

```python
from eosh.commands import registry, arg
from eosh.completion import ChoiceCompleter

@registry.command(
    name="deploy",
    help="Deploy a service to an environment.",
    params=[
        # choices= drives both argparse validation AND TAB completion.
        arg("environment", choices=["prod", "staging", "dev"]),
        arg("region",      completer=RegionCompleter()),
        arg("service",     completer=ServiceCompleter()),  # may inspect ctx.args
        # Boolean flags
        arg("-v", "--verbose",  action="store_true",   help="verbose output"),
        arg("-n", "--dry-run",  action="store_true",   help="skip execution"),
        # Value-taking flag with a value completer
        arg("-t", "--timeout",  type=int, metavar="SECONDS",
                                completer=ChoiceCompleter(["30", "60", "120"])),
    ],
)
def deploy(environment, region, service, verbose, dry_run, timeout):
    ...
```

This design means:
- Each `arg()` is independent — positionals declared in order, flags can appear anywhere in the list
- Positionals without a `completer=` (and no `choices=`) fall back to file completion at that index
- Later completers see earlier args via `ctx.args`
- All flags collected into a single `OptionsCompleter` under `None` — activated whenever the user types a `-`-prefixed token

External recipes use the same `params=[arg(...)]` form, registered without a handler. The dispatch path treats handler-less Commands as external recipes and falls through to the system-command path:

```python
from eosh.commands import arg, registry as command_registry
from eosh.completion import FileCompleter

command_registry.command(
    "rsync",
    help="fast incremental file transfer",
    params=[
        arg("paths", nargs="*", completer=FileCompleter()),
        arg("-a", action="store_true", help="archive"),
        arg("-v", action="store_true", help="verbose"),
        arg("-n", action="store_true", help="dry run"),
        arg("--exclude", metavar="PATTERN", help="exclude pattern"),
    ],
)
```

## Writing Custom Completers

### Basic Pattern

```python
class MyCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        return [
            Completion(value=item, description=desc)
            for item, desc in self._get_items()
            if item.startswith(ctx.prefix)
        ]
```

### Context-Aware Pattern

```python
class EC2InstanceCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        # Use preceding args or fall back to shell context
        account = ctx.args[0] if ctx.args else ctx.shell_context.get_var("account")
        region = ctx.args[1] if len(ctx.args) > 1 else ctx.shell_context.get_var("region")
        instances = fetch_instances(account, region)
        return [
            Completion(value=i["id"], description=i["name"])
            for i in instances
            if i["id"].startswith(ctx.prefix)
        ]
```

### Caching Pattern

For completers that call expensive APIs, cache results keyed on the relevant arguments:

```python
class CachedCompleter(Completer):
    def __init__(self):
        self._cache: dict[tuple, list[Completion]] = {}

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        key = tuple(ctx.args[:2])
        if key not in self._cache:
            self._cache[key] = self._fetch(ctx.args[0], ctx.args[1])
        return [c for c in self._cache[key] if c.value.startswith(ctx.prefix)]
```

## Parsing for Completion

`split_for_completion(line)` splits the input line into tokens and a trailing prefix:

- `"git commit "` → `(["git", "commit"], "")`
- `"git commit -m hel"` → `(["git", "commit", "-m"], "hel")`
- `"git "` → `(["git"], "")`
- `"gi"` → `([], "gi")`

The distinction between completed tokens (in `args`) and the in-progress token (in `prefix`) is critical for routing completions correctly.

Completion is always scoped to the **current pipeline stage**: for `ls | grep -`, the completion context uses `grep` as the command, not `ls`.

For a practical guide to adding completions for external commands, see [`doc/recipes.md`](recipes.md).
