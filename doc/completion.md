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
    shell_context: Context | None  # current shell context
```

The `shell_context` field gives completers access to the active context's variables (e.g., account, region), enabling completions that adapt to the current environment without explicit arguments.

### Completion

```python
@dataclass
class Completion:
    value: str              # the text inserted on selection
    display: str = ""       # label shown in completion menu (defaults to value)
    description: str = ""   # metadata shown beside the completion
    fields: tuple[str, ...] = ()  # description split into columns (aligned across rows)
    multi_select: bool = False   # True → opens InlineMultiPicker instead of InlinePicker
    combinable: bool = False     # True for single-char flags that can be merged (-a -l → -al)
    arg_hint: str = ""           # non-empty when flag requires a following argument (e.g. "N")
    is_arg_hint: bool = False    # True when this IS the hint for a preceding flag's value
    verbatim: bool = False       # True → value may span tokens; inserted as-is
```

Every candidate's `value` starts at the **completion anchor** — the position the
current token starts at (`parsing.raw_token_start`), which is where the editor
splices it in. `verbatim` marks one whose value may run past the end of that
token (today: history suggestions — see
[History candidates](#history-candidates)); because such a value is already
shell syntax, the editor inserts it without shell-quoting it and without
appending a trailing space.

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

Completes command-line flags. Registered under the `None` key in a completers dict so it activates at any argument position when the user types a `-`-prefixed token.

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

When all completions returned are `multi_select=True` (which `OptionsCompleter` always sets), the line editor opens `InlineMultiPicker` instead of `InlinePicker`. The user:
- Navigates with arrows / `Ctrl+P/N` — the list opens with **no** row highlighted, so the first Down/Up moves onto the first/last flag
- **Space** to toggle the highlighted flag's checked state
- **Enter** to confirm (checked items, or the highlighted item if nothing is checked; nothing at all if neither — the picker just closes)
- Types a letter to jump to the next flag starting with that letter

Boolean short flags are automatically merged: selecting `-a` and `-l` inserts `-al`. Flags with `arg_hint` are inserted individually followed by a space, then either a value picker or an inline hint line.

`OptionsCompleter` also handles:
- **Flag deduplication** — flags already present in `ctx.args` are excluded
- **Short-flag cluster parsing** — `-hs` in `ctx.args` is treated as both `-h` and `-s` already used
- **Preceding-flag hint** — when the last completed arg is a value-taking flag and the user presses TAB without typing `-`, the engine shows a hint instead of opening a picker

### ConditionalCompleter

Selects a sub-completer based on the preceding arguments. Useful when argument N's valid values depend on what was chosen for arguments 0..N-1:

```python
ConditionalCompleter({
    ("prod",): ChoiceCompleter(["us-east-1", "us-west-2"]),
    ("staging",): ChoiceCompleter(["us-west-2"]),
})
```

Performs longest-prefix matching on `ctx.args` against the mapping keys: tries the full `args` tuple first, then progressively shorter prefixes.

### HistoryCompleter

Completes the typed line from past command lines. It is the one completer that
*matches* in **line space**: an entry qualifies when it starts with `ctx.line`
(everything before the caret), not merely with `ctx.prefix`. What it returns is
an ordinary anchored candidate — the entry from `raw_token_start(ctx.line)`
onwards — flagged `verbatim=True` because that tail can span several tokens.

```python
HistoryCompleter(history_fn, limit=10, ran_here_fn=None)
```

`history_fn` returns the entries to search — the shell passes the **current
context's** in-memory Up/Down list, so TAB recall and arrow recall agree on
scope (`Ctrl+R` is the one that searches the global store). It is called on
every keystroke while a picker is open, so it must stay cheap.

Candidates are most-recent-first, deduplicated, and capped at `limit`. Entries
are skipped when they add nothing (an exact match, or one differing only by
trailing whitespace) and when they contain a newline — the value is inserted
verbatim, so a newline would submit the line on insert.

**Directory scoping.** `ran_here_fn(entry)` answers "was this line run in the
current directory?" — the shell passes `History.ran_here`, backed by the
`~/.eosh/history.dirs` side table (see below). Only entries that answer True
are offered, so a `make deploy prod` from another checkout stays out of the way in
this one. There is **no fallback**: a directory you have never run a matching line
in contributes no history rows, and the picker shows only the ordinary candidates.
Up/Down and `Ctrl+R` stay unscoped, so reaching across directories is still one
key away.

Two consequences worth knowing:

- Entries recorded before the side table existed (or copied from another
  machine without it) have no directory at all, so they are never offered as TAB
  candidates until they are run again.
- The scope is the directory the command was *typed* in, matched exactly. `cd`
  into a subdirectory of a repo and the root's entries drop out.

**Where the directories come from.** `lineedit.History` appends each executed
line to `~/.eosh/history` as before, and records the cwd it was typed in
against that line in `~/.eosh/history.dirs` — a JSON map of line → recent
directories (most recent last, capped at `MAX_DIRS_PER_LINE`). It is a *side*
table so the main history file's format, and every reader of `History.entries`
(`Ctrl+R`, the `default` context's seed), stay untouched. A consecutive duplicate
line still records its directory (re-running a command after a `cd` is new
information even when the line is not), and each write prunes lines the history
file no longer holds. Missing, corrupt, or unreadable: no directory is known for
any line, and every candidate arrives via the fallback.

Anchoring means the picker shows what a candidate would *add*, so a history row
reads like the token rows next to it: `git com<TAB>` offers `commit` (the
sub-command) and `commit -m "fix typo"` (from history) side by side, both
starting from the `com` the user typed.

## History candidates

Because a history entry spans several arguments, it can suggest things no
per-argument completer could produce:

```
eosh> git commit <TAB>
┌────────────────────────────────────────────────┐
│ -m "fix typo"                      history     │
│ --amend --no-edit                  history     │
│ doc/                                           │
│ src/                                           │
└────────────────────────────────────────────────┘
```

`Shell._get_completions` is a thin wrapper that merges these on top of the
completer-driven candidates from `_get_base_completions`, so history reaches
*every* position — command name, sub-command, argument, and the stages of a
pipeline (`ls | grep fo<TAB>` matches past lines starting with `ls | grep fo`).

The rules that keep the merge from degrading the existing UX:

| Rule | Why |
|------|-----|
| History rows are listed **first** | "What I ran before" is the most likely intent |
| Dropped when a completer already offers the same single token | Both rows insert the same text, and the completer's is the one carrying the description. `awsut sagemaker studio <TAB>` listed `spaces` twice — once tagged `history`, once as `List the spaces in a domain`. Compared after unquoting, so `'My Documents/'` and `My Documents/` count as one token; anything spanning more (`spaces --max 5`) survives, since no per-argument completer can produce it |
| Scoped to the current directory | The lines you ran *here* are the relevant ones; another checkout's `make deploy prod` is noise. Up/Down and `Ctrl+R` remain unscoped for the rest |
| Suppressed when nothing is typed | A bare TAB should list available commands; Up/Down and `Ctrl+R` already cover recall with an empty line |
| Suppressed on the flag picker (all candidates `multi_select`) | One history candidate would demote the Space-to-toggle checkbox picker to a plain list |
| Suppressed on the arg-hint (a lone `is_arg_hint`) | The editor renders that lone candidate as a hint line; a second candidate turns it into a picker |
| Excluded from flag-value pickers (`_prompt_for_arg`) | A history tail is not a value for the flag being filled in |
| Never auto-applied | Every "exactly one candidate" shortcut in `lineedit._complete` counts only single-token candidates, so a unique token completion still applies on the first TAB, and a lone history candidate is always *shown* before it inserts several arguments |

The last rule has a visible consequence worth knowing: when a position has
exactly one token candidate, the first TAB auto-applies it and the history rows
are not shown. They are one TAB away — press it again on the now-longer line.
The alternative (opening a picker whenever history matches) would cost a
keystroke on completions that used to be instant.

**TAB-extend and the raw anchor.** `TAB` inside a picker types the longest shared
prefix of the candidate *values*, which requires every value to live in the same
space as the prefix it is measured against. Anchored history values nearly share
the token's space, with one exception: they start at the *raw* anchor, while
`ctx.prefix` is what shlex left after stripping quotes — so for `cat 'My Do<TAB>`
the history value begins with `'`. `lineedit._complete` therefore picks one space
per TAB press: a history-only list measures against the raw token text
(`line[raw_token_start(line):]`), while a mixed list measures the token prefix
(`ctx.prefix` as recomputed for the current line) and drops the verbatim rows
from the measurement.

The choice is re-made on every TAB, not fixed when the picker opened, because
narrowing moves both halves of the subtraction:

- A **mixed list can narrow to history rows only** — `make job <TAB>` opens with
  file candidates alongside the history tails, and typing `J` drops the files.
- **Typing a space moves the anchor.** History candidates match in line space, so
  they keep matching across a token boundary and the picker stays open; the
  values are then anchored one token further right.

Both cases used to leave a stale, too-long prefix on the measuring side, so the
extension came out empty and `TAB` appeared to do nothing. The picker delegates
the whole computation to the `extend_fn(items, typed)` callback `_complete`
supplies (`InlinePicker`'s simpler `value_fn` + `completion_prefix` pair still
serves the flag-value picker, where the value space is fixed).

**Picker column alignment.** `_picker_col_offset` measures history rows *with*
everything else, since anchored values normally do start with the typed token —
that is what opens a history-only picker under the token instead of at the caret.
Only when including them shares nothing (again, the quoted token) does it fall
back to measuring the token rows alone.

Whatever column it lands on, `_align_verbatim_rows` then trims the verbatim rows'
`display` so every row on screen *starts* at that column. A history display
begins at the raw anchor, but a token row's begins wherever its completer chose:
`FileCompleter` shows only the last path segment, so `cat ~/.aws/<TAB>` lists
`config`, and the picker has to open at the caret. Left untrimmed, a history row
for `cat ~/.aws/config ~/.aws/credentials` would render there as
`~/.aws/config ~/.aws/credentials` — leading with the same `~/.aws/` the user is
looking at one line above, apparently duplicated; trimmed, it reads
`config ~/.aws/credentials`. (A history row that stopped at `~/.aws/config`
never reaches the picker at all — the file candidate offers that token already,
so the merge step drops it.) Trimming is purely cosmetic:
the `value` still starts at the anchor, because that is where `_apply` splices it
in. The partial-overlap case falls out of the same rule — with `cat doc/co<TAB>`
aligned under the `co`, a history row shows `completion.md --dry-run`.

## How TAB Completion Works

The line editor (`lineedit.py`) calls `_get_completions(line_before_cursor)` on every TAB press. The shell implements this as:

```
_get_completions(line_before_cursor)
  → _get_base_completions(line_before_cursor)   ← the dispatch chain below
  → HistoryCompleter, unless the base result is a flag picker or an arg-hint
      → drop the tails the base result already offers as a single token
      → prepend what is left, anchored (verbatim=True)

_get_base_completions(line_before_cursor)
  → _split_on_operators() → isolate current pipeline stage
  → split_for_completion(stage) → (tokens, prefix)
  → No tokens?
      → CommandNameCompleter
  → Has tokens?
      → Look up command in registry (or external completers)
      → completers[None] present AND prefix starts with "-"?
          → Check if last arg is a value-taking flag (preceding-flag hint)
              → Yes, has value_completer → return value_completer.complete(ctx)
              → Yes, hint only → return [is_arg_hint=True Completion]
          → options_completer.complete(ctx) if should_activate()
      → No options matches yet, completers[arg_index] present?
          → positional_completer.complete(ctx) if should_activate()
      → No completer registered at this slot?
          → Try ArgcompleteCompleter (if command is an argcomplete-marked Python script)
      → Still no matches and no completer registered? → FileCompleter fallback
```

**The argcomplete fallback** runs only where no completer is registered. A registered completer that returned `[]` meant "nothing here". It finds argcomplete tools by reading the script, never by running it, and caches that per command. **Cobra tools are not a fallback.** Running an unknown command with `__complete` to find out would execute it, so they are opted in by name and get a `CobraCompleter` as their `delegate`, like any other recipe. See [cobra.md](cobra.md) and [argcomplete-fallback.md](argcomplete-fallback.md).

Once completions are returned to the line editor:

| Situation | Behaviour |
|-----------|-----------|
| Zero completions | Do nothing |
| Single `is_arg_hint` completion | Show inline hint below buffer; cleared on next keypress |
| Single `multi_select` + `arg_hint` completion | Auto-apply the flag (insert `flag `), then loop again to handle the value |
| Single non-hint completion | Apply immediately; if it has `arg_hint`, then prompt for the value |
| All `multi_select` | Open `InlineMultiPicker` |
| Mixed | Open `InlinePicker` (narrows as user types more characters) |

Every "single completion" row above counts **single-token** candidates only;
`verbatim` (history) candidates are excluded, so they never auto-apply and never
turn a unique token completion into a picker. See
[History candidates](#history-candidates).

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
2. `lineedit._complete` / `_prompt_for_arg` commit `picker.typed` into the
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

Python commands declare arguments via a single `params=[arg(...)]` list. Each `arg()` configures argparse (validation, type coercion, defaults, action) **and** TAB completion in one place — `completer=` on a positional drives completion of the value at that position; `completer=` on a value-taking flag drives completion of the value typed after the flag. The registry derives the underlying `{arg_index: Completer, None: OptionsCompleter}` dict automatically.

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
        account = ctx.args[0] if ctx.args else ctx.shell_context.get_variable("account")
        region = ctx.args[1] if len(ctx.args) > 1 else ctx.shell_context.get_variable("region")
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
