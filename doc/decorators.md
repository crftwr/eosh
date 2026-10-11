# Pipeline decorators

A **decorator** is a token of the form `@name` (with optional shell-style
flags) at the start of a line that wraps the rest of the line as a
pipeline and modifies how that pipeline is run. The leading `@` makes the
syntax visually distinct from regular commands, so parsing priority is
unambiguous and the construct doesn't collide with POSIX command names.

```
@watch ls
@watch -n 1 {df -h | grep abc}
@time make build
@retry -n 3 flaky-test
@quiet --stderr {noisy-cmd | filter}
```

**Status:** the feature is shipped. Parser, executor, registry, and
four built-in decorators (`@watch`, `@time`, `@retry`, `@quiet`)
are in place; `@deco {body} | next` composition runs the body's stdout
through the outer pipeline. Open follow-ups (decorator stacking,
outer sequencing, more built-ins) are tracked in
[discussion #67](https://github.com/crftwr/eosh/discussions/67).

**Shipped:**

- Parser support for `@name [flags] body` and `@name [flags] {body}`
  (`pipeline.py::_extract_decorator_prefix`, `_find_matching_brace`).
- Composition: `@deco {body} | next-stage` parses as a two-stage
  pipeline; the executor runs the decorator body in a worker thread
  whose stdio is wired to the outer pipe via the thread-local routers
  (`shell.py::_start_decorator_stage_thread`,
  `_run_pipeline_from_decorator`).
- Decorator registry mirroring `CommandRegistry` / `VarRegistry`
  (`eosh/decorators/__init__.py`).
- `Pipeline.run()` indirection so decorator bodies can re-enter
  execution (`pipeline.py::set_pipeline_executor`).
- Dispatch in `Shell._execute_decorator_stage`.
- Built-ins: `@watch`, `@time`, `@retry`, `@quiet`
  (`eosh/decorators/*.py`).
- `@<TAB>` completion: decorator-name list, decorator-flag picker, and
  body-command delegation through the existing recipe / argcomplete /
  cobra fallback chain.
- Tests in `tests/test_decorators.py`.

## Prior art: IPython magics

The closest battle-tested analog is **IPython's magic commands**, which
have shipped since ~2007 and are now familiar to most Python developers
via Jupyter:

| IPython | eosh decorator |
|---------|-------------------|
| `%time some_expr` | `@time some_pipeline` |
| `%timeit -n 100 -r 5 expr` | `@timeit -n 100 -r 5 pipeline` |
| `%%capture out` | `@capture out` (or `@quiet`) |
| `%%bash` | (eosh already runs shell) |
| `%lsmagic` | `@<TAB>` (decorator-name completion) |

What we borrow:

- **A distinct sigil that can never collide with a command name** (`%` for
  IPython, `@` for us). This is the core idea — same justification,
  different character.
- **Shell-style flags after the sigil** (`%timeit -n 100 expr`). IPython
  users are mostly Python developers and *still* chose flag syntax,
  because what comes after the magic is code/commands, not a function
  call. We follow that lead — see "Argument syntax" below.
- **`@<TAB>` discovery** mirrors `%lsmagic` — list everything available
  without needing docs.
- **User-defined magics via a Python decorator**
  (`@register_line_magic`) — our `@registry.command("@name", ...)`
  API has the same shape.

What we deliberately don't borrow:

- **Line vs. cell distinction (`%` vs `%%`).** IPython has both because
  notebook cells are multi-line. eosh lines are single statements
  (modulo `;` and `\`-continuation), so one prefix is enough.
- **Magics receive raw strings.** IPython magics get the rest of the
  line/cell as a string and parse it themselves, so every magic
  reinvents arg parsing. We hand decorators a *parsed Pipeline AST* —
  the shell tokenizes once, decorators stay simple.
- **Auto-magic** (the `%`-less form). IPython lets you write `time expr`
  if there's no name collision. We skip this — it reintroduces exactly
  the parsing-priority ambiguity the sigil exists to solve.
- **Implicit return-value capture.** IPython's `%time` interacts with the
  cell's last expression. There's no expression-vs-statement distinction
  in a shell, so this doesn't apply.

## Why decorators (vs. a regular built-in command)

The first sketch was to add `watch` as a built-in Python command whose
body re-parses its trailing arguments as an eosh pipeline. That works
for the pipe-with-watch ergonomic problem, but has two real costs:

1. **Syntactic confusion with POSIX `watch`.** Users typing
   `watch -n 2 ls` reasonably expect the system `watch(1)` semantics —
   `-d` for diff highlighting, curses-based redraw, no piping by
   default. A built-in named `watch` either has to mimic POSIX flags
   faithfully (a maintenance burden and a divergence trap) or silently
   behave differently from every other shell.
2. **Inconsistent parsing rules.** A built-in `watch` command would have
   to special-case its argument tokenization: `watch ls | grep py`
   needs to mean "watch the whole pipeline", but `grep py | watch ls`
   shouldn't. That makes the rule "where does `|` bind?" depend on
   which command is on the left, which is the kind of inconsistency
   that bites users later.

The `@` prefix borrows from Python's decorator syntax: visually obvious,
parsed before the normal pipeline grammar runs, no name collision.
`watch` (no `@`) still passes through to the system binary unchanged;
`@watch` is its own thing.

## Scope ambiguity: why `@watch` alone isn't enough

Even with the `@` sigil disambiguating *the parser*, there's a real
human-side concern. POSIX `watch` and `@watch` would have *opposite*
scopes for the same trailing text:

```
 watch -n 5 df -h | grep abc      # POSIX: pipes watch's output through grep
@watch -n 5 df -h | grep abc      # eosh: re-runs `df -h | grep abc` every 5s
```

The visual form is identical except for the leading `@`. Worse, this
inverts the behavior in exactly the case where users are most likely to
misread it: the famous POSIX gotcha (`watch` not piping the way you'd
expect, requiring `watch -n 5 'df -h | grep abc'` to work) trains users
that "the operators don't belong to `watch`." We'd be saying "good
news, no quotes!" while reusing the trap.

### Resolution: braces required when the wrapped pipeline contains operators

A `{...}` subexpression makes the decorator's scope literally visible.
The simple case stays terse; multi-stage pipelines force the user to
mark the scope.

```
@watch -n 5 df -h                   # OK — single command, scope is obvious
@watch -n 5 {df -h}                 # OK — explicit scope, also fine
@watch -n 5 {df -h | grep abc}      # OK — pipeline must be braced
@watch -n 5 df -h | grep abc        # parse error: ambiguous scope; use { ... }
```

The rule: **any pipeline that contains `|`, `;`, `&&`, `||`, or a
redirect (`>`, `<`, `2>`, …) and is wrapped by a decorator must be
enclosed in `{...}`**. A decorator wrapping a single bare command works
without braces.

This is enforced at parse time, not silently coerced — the error
message points at the operator and suggests the fix.

## Parsing model

Decorators are extracted *before* the pipeline parser runs:

```
raw line
 └─ while the next token starts with '@':
     ├─ peel off '@name' and any decorator-owned flags up to the next non-flag token
     └─ push onto decorator stack
 └─ if the next token is '{', parse a braced subexpression as the wrapped pipeline
 └─ else parse a single command (no operators allowed) as the wrapped pipeline
 └─ remainder (after the decorator's pipeline ends) → existing pipeline parser,
    with the decorator-call standing in as one stage
```

Everything inside the braces (or, for the un-braced single-command
form, the bare command) is a normal pipeline — `;`, `&&`, `||`, `|`,
`>`, `<`, `2>`, globbing, var expansion, backslash continuation all
work exactly as they do at the top level. The decorator never sees
raw text; it gets a parsed pipeline AST.

Multiple decorators stack from outside in (closest to the pipeline runs
innermost), matching Python's decorator semantics:

```
@time @retry -n 3 flaky-test
       │           └─ retry wraps `flaky-test`
       └─ time wraps `retry -n 3 flaky-test`
```

### Brace handling: `{` and `}` inside the wrapped pipeline

The brace-balancer that finds the closing `}` of a decorator scope is
just one more state on the same scan that already handles quotes and
escapes for `|`, `;`, `&&`, etc. It applies three rules so that braces
that are *part of the wrapped command* don't terminate the scope:

1. **Quoted braces are literal.** Inside `"..."` and `'...'`, every
   character (including `}`) is data. The brace counter doesn't move
   while inside a quoted region — exactly like `"|"` doesn't
   terminate a pipeline today.
   ```
   @watch {echo "}"}            # the "}" is literal; outer } closes scope
   @watch {grep '{' file}       # the '{' is literal; outer } closes scope
   @watch {grep "{}" file}      # both braces literal inside the string
   ```

2. **`${name}` parameter expansion is its own balanced span.** When
   `$` appears immediately before `{`, the tokenizer recognizes
   `${...}` as a single var-expansion token and tracks its own
   matching `}` separately from the decorator scope. This is the same
   rule `parsing.py` already applies for `${var}`; the brace counter
   just learns to peek for the `$` prefix.
   ```
   @watch {echo ${abc}}         # inner } closes the var; outer } closes scope
   @watch {echo "${abc}"}       # quoted ${...} also expands; same balance
   @watch {echo '${abc}'}       # single-quoted: literal text, no expansion
   ```

3. **Backslash escapes either brace.** `\{` and `\}` are literal,
   matching the existing escape rule for `\|`, `\;`.
   ```
   @watch {echo \}}             # literal }; the next } closes the scope
   ```

The brace counter and the existing quote/escape/var-expansion tracker
share one scan — no new tokenizer pass.

### Argument syntax

Decorators take **shell-style flags**, not Python kwargs. So
`@watch -n 1` rather than `@watch(n=1)`. Reasons:

- IPython's magics chose the same form (`%timeit -n 100 expr`) and it's
  been ergonomic for ~15 years.
- The token style stays consistent with the rest of the line — flags
  before the wrapped command, just like flags before any other
  command's positional args.
- We can reuse `OptionsCompleter` and `arg(...)` directly. No second
  parser to maintain, no second completion path.

Each decorator declares its flags via the same `arg(...)` helper used
for commands. The shell tokenizes the whole line into shell tokens once;
everything from `@name` up to the first non-flag token is consumed by
the decorator's argparse.

Examples:

```
@watch                        # bare, defaults
@watch -n 1 df -h             # one flag, then single-command pipeline
@watch -n 1 {df -h}           # one flag, braced pipeline (also fine)
@watch -n 1 --no-clear ls     # multiple flags
@retry -n 3 flaky-test        # positional-style count via -n
```

**Where do the decorator's flags end and the pipeline begin?** First
token that doesn't look like a flag (no leading `-`), isn't a value
bound to a preceding flag, and isn't another `@name`, or the opening
`{`. This is the same rule argparse already implements; we just stop
consuming at the first positional (or brace) and hand the rest off.

Edge case: a wrapped command whose first token *does* start with `-`.
The user can write `@watch -- ls -la` (POSIX-style `--` terminator) or
just brace it: `@watch {ls -la}`. Same convention as POSIX.

## Composing decorators inside larger pipelines

`@deco {body} | next` parses and runs as a two-stage pipeline whose
first stage is the decorator-call and second stage is whatever follows
the closing brace.

```
@watch {ls} | grep py
└──── stage 1 ──┘   └─ stage 2
```

Parser story (implemented in
[pipeline.py::_extract_decorator_prefix](../src/eosh/pipeline.py)):

```
@abc -n 5 {df -h} | grep xyz
       │   │   │  │
       │   │   │  └── outer pipeline parser resumes here
       │   │   └── decorator's scope ends at matching brace
       │   └── decorator's scope starts
       └── decorator's flags (consumed by argparse)
```

The remainder after the closing `}` is fed back through the same
`split_on_operators` path as a top-level pipeline, so any number of
`|`-stages can follow.  `;`/`&&`/`||` after the closing `}` are
explicitly rejected with a clear error — relaxing that needs the
outer-sequence parser to treat the decorator-stage as one statement,
which is a separate change.

Executor story
([shell.py::_execute_pipeline](../src/eosh/shell.py)):

* The decorator-call stage runs on a worker thread spawned by
  `_start_decorator_stage_thread` — analogous to
  `_start_python_stage_thread` for ordinary Python commands.  The
  thread's `sys.stdin`/`sys.stdout` are rebound (via the
  thread-local routers) to the boundary pipe ends so anything the
  decorator body writes via `print` flows downstream.
* When the decorator body does `pipeline.run()`, the registered
  executor (`_run_pipeline_from_decorator`) detects the in-pipe
  context, dups the thread-local stdio fds, and forces the multi-stage
  codepath in `_execute_pipeline` so the body's first/last stage
  read/write those fds directly.  Without this the body would route
  through the standalone-command path (its own `PipelineSlot`) and grab
  the real terminal — wrong from a
  worker thread.
* fds duped from thread-local overrides are owned by Popen / the
  Python-stage thread, so subsequent body runs (e.g. each
  `@watch` iteration) re-dup from the parent's wrappers — the
  parent's own stdio stays valid across iterations.

Three things this unlocks:

1. **Decorator output as a stream stage** —
   `@watch --no-clear {curl …} | jq '.status'` continuously
   processes results.
2. **Redirects bind to whichever scope is braced** —
   `@time {make && ./run} > build.log` times the whole chain and
   redirects together; `@time {make} > build.log` times only `make`
   but redirects only `make`'s output.  (`@time` itself isn't
   shipped yet, but the parsing/execution rule is in place.)
3. **Decorators on top of decorators on subexpressions** —
   `@retry -n 3 @time {flaky-test}` already works under the existing
   stacking rule; nothing new.

Two caveats worth flagging to decorator authors:

- **TTY-aware decorator behavior.** `@watch`'s clear-screen escape
  codes are wrong when its stdout is piped. Decorators that emit
  terminal control (or produce non-pipeable output) need to check
  `sys.stdout.isatty()` — same discipline as any well-behaved program.
  (`@watch` already does — `_ThreadLocalStream.isatty()` answers for the
  stage's own stdout.)
- **Infinite-output decorators in a pipeline.** `@watch {ls} | grep foo`
  runs forever; the user has to `Ctrl+C` (or `Ctrl+]` to background)
  just like `tail -f | grep`. Not a bug, but worth a sentence so it's
  not surprising.

## Why `{...}` (and the concerns)

`{...}` is the most natural choice visually but carries baggage from
other shells. The concerns, in order of weight:

1. **Brace expansion.** Bash, zsh, and fish all use `{...}` for
   list/sequence expansion: `echo {a,b,c}`, `mv img{,.bak}`,
   `seq {1..10}`. eosh doesn't implement this today, but it's a
   feature users routinely expect from a modern shell. Reserving
   `{...}` as a general grouping construct would lock us out (or force
   bash-style "depends on whitespace and position" disambiguation,
   which is exactly the kind of subtlety that bites users).
2. **Bash command groups (`{ cmd1; cmd2; }`).** Bash uses `{...}` for
   "run these in the current shell." The rules are famously fiddly:
   required space after `{`, required `;` (or newline) before `}`,
   only at statement start. Users who know this will assume our
   `{...}` follows the same rules.
3. **`${var}` parameter expansion.** Already used by eosh.
   Different context (always after `$`), so no actual parser
   collision, but it does mean `{` is overloaded across
   "subexpression," "parameter expansion," and (potentially) "brace
   expansion" in the future.

Not really a concern: PowerShell's `{...}` is a first-class
scriptblock object — different paradigm, but users coming from
PowerShell will at least find the visual familiar.

### Alternatives surveyed

| Syntax | Used by | Notes |
|--------|---------|-------|
| `{...}` | bash command groups, PowerShell scriptblocks, fish brace-expansion | Most "shell-natural" but conflicts with future brace expansion |
| `(...)` | bash subshells | Strong "this is its own scope" connotation; collides only if eosh ever adds subshells |
| `$(...)` | bash/zsh command substitution | Reads as "capture this," not "delimit this" — wrong semantics |
| `<(...)` `>(...)` | bash process substitution | Visually busy and the meaning ("file path that streams") doesn't fit |
| `[...]` | bash test, glob class | Heavily overloaded already |
| `((...))` | bash arithmetic | Niche but taken |
| `do ... end` | fish, ruby | Verbose; reads more like scripting than shell |
| Backticks | bash command substitution (legacy) | Single-char, visually distinct, but historically tied to capture |

### Recommendation

Use `{...}` with two explicit policies that keep it from spreading:

1. **`{` is a subexpression delimiter only after `@decorator [flags]`** —
   i.e., position-restricted, not a general grouping construct.
   Outside that position it stays free for brace expansion if we add
   it later.
2. **No bash-style whitespace/`;` requirements inside the braces** —
   `{df -h}` parses fine, no trailing `;` needed. We're reusing the
   visual, not the rules.

If preserving brace expansion as a future feature is high-priority and
we'd rather not depend on position-restriction, the next-best choice
is **`(...)`** — visually clean, reads as scope, only conflict is
with subshells (which a Python shell may never need).

## Completion UX

The big win: completion falls through to the existing machinery with
almost no new code.

- `@<TAB>` — list registered decorators (with descriptions). Same UI as
  command-name completion. Mirrors IPython's `%lsmagic`.
- `@watch -<TAB>` — complete the decorator's own flags via the same
  `OptionsCompleter` machinery commands use.
- `@watch -n <TAB>` — value completion for `-n`, if the decorator's
  `arg(...)` spec attaches a completer.
- `@watch <TAB>` — first non-flag token after the decorator's args;
  complete it as a command name. From here on, the existing recipe /
  argcomplete / cobra fallback chain takes over. `@watch git st<TAB>`
  works because we're literally asking the git recipe to complete its
  first arg.
- `@watch {<TAB>` — same as above, but inside a braced scope.
  Tokenizer reports the scope so completion knows the closing brace
  is owed.
- `@time @ret<TAB>` — second decorator name completes from the
  registry, same as the first.

No part of this requires the decorator to know about completion. The
shell handles `@`-token completion uniformly; once past the
decorator(s), it's plain pipeline completion.

## Impact of in-process Python pipelines (commit `047086b`)

eosh runs Python `@registry.command` stages in worker threads
that share the shell process, with thread-local
`sys.stdin`/`sys.stdout`/`sys.stderr` rebound to the pipe ends. That
landed before decorators were implemented, and it shapes the design
in three concrete ways — mostly in decorators' favor.

**1. The `Pipeline` AST is already a runnable handle.** The former
"factor pipeline-execution into a `Pipeline` object" implementation
step is much smaller than the original draft suggested.
`pipeline.Pipeline(stages=[...])` already exists as a dataclass
consumed by `Shell._execute_pipeline`. The decorator API just needs a
thin wrapper that calls `_execute_pipeline` (or a refactored helper
extracted from it) on the AST it was handed. No new execution model —
decorators reuse the path Python-stage pipelines already exercise.

**2. `pipeline.run()` from a decorator body inherits the thread-local
stdio routing.** A decorator runs as a Python command (essentially
`@registry.command` under the hood), so when the user pipes its output
— `@watch {ls} | grep foo` — `sys.stdout` inside the decorator body
is *already* rebound to the pipe end. `pipeline.run()` then runs its
own stages with their own thread-local rebinding, so the decorator's
direct writes (e.g. `@time`'s timing line) and the wrapped pipeline's
writes coexist without trampling. Decorator output goes through
`sys.stdout` like everything else, which means it follows the pipe in
a piped context and the terminal otherwise. No special-casing needed.

**3. The Python-pipeline caveats become decorator-author caveats.**
Anything documented in [limitations.md](limitations.md) under "Python
commands in pipelines — caveats of the in-process model" applies
verbatim to decorator bodies, because they *are* Python commands:

- **Nested `subprocess.run` writes to the real terminal**, not the
  decorator's rebound stdout. If `@time` shells out for some reason,
  it must pass `stdout=sys.stdout` (and `stderr=sys.stderr`) to land
  on the right destination when the decorator is itself piped.
- **Pure-CPU loops can't be `Ctrl+C`-interrupted** when the decorator
  is part of a pipeline. `@watch`'s `time.sleep(interval)` is fine
  (sleep is interruptible), but a decorator doing tight CPU work with
  no I/O won't unwind on `KeyboardInterrupt` until it next blocks.
- **`ctx.run_interactive` / `ctx.input` / `ctx.choose` are off-limits** when the
  decorator is in a pipeline — its stdio is wired to pipe fds, not
  the terminal. They raise `RuntimeError`. A decorator that wants to
  launch an interactive subprocess has to refuse, or fall back to
  non-interactive execution, when invoked from a piped context.
- **Stateful effects mutate the parent.** `@as NAME {...}` switching
  contexts persists, even if invoked inside a pipeline. Same as the
  built-in `cd | tee log` quirk; treat as the cost of in-process.

**4. Ctrl+C handling is mostly solved.** The pipeline driver already
catches `KeyboardInterrupt` and closes pipe ends to unblock I/O-bound
stages (commit `877f946`). A decorator's loop body that does I/O
(every iteration of `@watch`, every retry of `@retry`) gets the same
treatment automatically. The decorator's own `while True:` body still
needs to check for the broken-pipe signal between iterations — but
that's a one-line `try`/`except BrokenPipeError` around
`pipeline.run()`, not a new mechanism.

## Decorator API sketch

A decorator **is** a command — one named `@name` in the command registry,
whose handler receives the wrapped `Pipeline` ahead of its parsed flags
(discussion #40). It reuses `arg(...)`, argparse, completion and `reload`;
the `@` keeps it from colliding with a command (`@time` vs. `time`), and
command-name completion and `help` keep the two apart.

```python
from eosh.commands import arg, registry

@registry.command(
    "@watch",
    help="Repeatedly run a pipeline until interrupted.",
    params=[
        arg("-n", "--interval", type=float, default=2.0, help="seconds between runs"),
        arg("--no-clear", action="store_true", help="don't clear screen between runs"),
    ],
)
def watch(pipeline, *, interval: float, no_clear: bool):
    while True:
        if not no_clear and sys.stdout.isatty():
            sys.stdout.write("\x1b[2J\x1b[H")
        pipeline.run()              # runs the parsed pipeline; honours redirects, pipes, etc.
        time.sleep(interval)
```

The `pipeline` argument is the same kind of object the shell's normal
execution path runs. It exposes at least:

- `pipeline.run() -> int` — run synchronously, return exit status
- `pipeline.run(stdout=..., stderr=...)` — run with redirected stdio
  (so `@time` can capture)
- `pipeline.text` — original source text, for display

`Ctrl+C` during a decorator's body interrupts the body, and then the
decorator: when the body ends with status 130, `pipeline.run()` raises
`KeyboardInterrupt` (`Shell._run_pipeline_from_decorator`), so one rule
covers every decorator — `@watch` stops instead of ticking on, `@retry`
doesn't retry an interrupt, and `@time` still prints its timing from its
`finally`. On a `PipelineSlot` the decorator's own thread also gets a
`KeyboardInterrupt` (at its next bytecode), for a Ctrl+C that lands while it
sleeps between runs (discussion #76).

## Built-in decorators

Shipped:

| Decorator | Purpose |
|-----------|---------|
| `@watch [-n SEC] [--no-clear]` | re-run pipeline on a timer, showing the latest output |
| `@time` | print wall/user/sys time after the pipeline finishes |
| `@retry [-n N] [--delay SEC]` | re-run on non-zero exit, up to N times |
| `@quiet [--stderr]` | discard stdout (and optionally stderr) |

There is no `@bg`. It existed — running its body in a new background
context — but for a single command `Ctrl+]` already does that, and the rest
(a whole pipeline) needed a slot type of its own, `PipelineSlot`, plus a
late-bound runner hook and dispatch rules copied from `_execute_stage`.
Discussion #39 removed it: run the pipeline and press `Ctrl+]`, which
backgrounds the whole thing.

`@watch` is deliberately watch(1)-sized: an alternate screen with a
one-line header and the last run's output cut to the screen, `q` or
Ctrl+C to quit, and plain streaming off a terminal or with `--no-clear`.
It used to be a full pager — scrolling in both directions, pause, a
spinner, a scrollbar and a worker thread so keys stayed live mid-run —
about 660 lines for a marginal feature; discussion #39 cut it to the core.
For output longer than the screen, pipe the body through `tail` or `head`.
The run itself gets the cooked terminal, so Ctrl+C stops a slow body at
once (a run ending in 130 ends the loop, in either mode); `q` is read
between runs. See [limitations.md](limitations.md#watch-shows-one-screenful-and-q-waits-for-the-current-run).

`@quiet` is implemented by appending `> /dev/null` (and `2>&1` with
`--stderr`) to the body's last stage via the same `Redirect` AST the
shell already understands, so it composes naturally with everything
else.

`@retry`'s `--delay` is an extension over the original sketch — it
sleeps between attempts to avoid hammering a flaky service.

Future ideas:

| Decorator | Purpose |
|-----------|---------|
| `@confirm` | prompt before running (e.g. for destructive commands) |
| `@nice -n N` | run pipeline at a different process priority |

## Resolved UX questions

1. **Args syntax.** Shell-style flags via the existing `arg(...)`
   helper. Matches IPython precedent and reuses `OptionsCompleter`
   directly.
2. **Scope of the wrapped pipeline.** Braces required whenever the
   wrapped pipeline contains a shell operator; bare single-command
   form allowed for the simple case.
3. **Bracket choice.** `{...}`, position-restricted to "after
   `@decorator [flags]`" so it doesn't preempt future brace expansion.
4. **Where does decorator output go relative to pipeline output?**
   Decorator body output goes through `sys.stdout` like any other
   Python command (the in-process pipeline routes it through the
   thread-local stdio installed in `Shell.__init__`).  Convention
   only: diagnostic lines should go to `sys.stderr` so they don't
   poison piped output.
5. **Reload semantics.** Decorators are commands, so the one
   `defining_builtins()` / `clear_user_commands()` pair covers them:
   built-in decorators survive `reload` (and a config needs
   `override=True` to replace one), config-defined ones are cleared and
   re-registered by the re-run config.

Open questions are tracked in
[discussion #67](https://github.com/crftwr/eosh/discussions/67).

## Non-goals (for the first cut)

- **Not a general macro system.** Decorators wrap a parsed pipeline;
  they don't rewrite source text or define new syntax.
- **Not a replacement for shell functions or aliases.** Aliases are
  name-for-text substitution; decorators are runtime wrappers around a
  pipeline AST. They're complementary.
- **Not POSIX-compatible.** The `@` prefix and `{...}` scope marker
  are intentionally eosh-specific; scripts using decorators won't
  run in `bash`/`zsh`. That's fine — eosh is interactive-first.
- **`{...}` is not (yet) a general command-grouping construct.** It's
  defined only in the position immediately following a decorator. We
  may extend it later, but reserving it now would foreclose brace
  expansion.

## What's shipped

1. **Parser** in [pipeline.py](../src/eosh/pipeline.py):
   `_extract_decorator_prefix` peels `@name [flags] body` before the
   normal pipeline grammar runs; `_find_matching_brace` honours
   single quotes, double quotes (with `\\`-escapes), `${...}` var
   spans, and bare `\\{` / `\\}`. The flag-extraction loop respects
   the decorator's argparse spec via a late-bound callback so
   `@watch -n 5 ls` parses as `flags=["-n", "5"]`, `body="ls"`.
   Composition: any `|`-prefixed remainder after the closing `}` is
   handed back to `parse_line`, which prepends the decorator-stage
   to a regular multi-stage pipeline.  `;`/`&&`/`||` after the
   closing `}` are rejected with a focused error.
2. **Registry** — the command registry: a decorator is the `Command`
   `@name` (discussion #40 folded the former `DecoratorRegistry` into
   it). `Command.invoke(flag_tokens, pipeline)` parses the flags with
   argparse and passes the `Pipeline` first; the parser's
   "does this flag take a value?" question is `Command.takes_value`.
   Built-ins are registered by `eosh.decorators.register_builtins()`.
3. **`Pipeline.run()` indirection** in
   [pipeline.py](../src/eosh/pipeline.py): `Pipeline.run(stdin=,
   stdout=, stderr=) -> int` calls into a registered executor. The
   `Shell.__init__` flow registers
   `Shell._run_pipeline_from_decorator`, which currently ignores the
   stdio kwargs (decorator bodies inherit the decorator's stdio,
   which is already correctly routed by the thread-local stdio).
4. **Dispatch** in
   [shell.py::_execute_decorator_stage](../src/eosh/shell.py):
   resolves the decorator, runs its argparse, calls
   `deco.func(body_pipeline, **kwargs)`. `SystemExit` propagates;
   `KeyboardInterrupt` exits 130; other exceptions print and exit 1;
   unknown decorator name returns 127, naming the closest one (`@watche` →
   did you mean `@watch`?).  On a POSIX terminal a lone
   decorator stage doesn't come here: like any pipeline it runs on a
   `PipelineSlot` (one PTY, parkable with Ctrl+]), on a stage thread whose
   stdio is that PTY — so its body, and `ctx.input` / `ctx.choose` /
   `ctx.run_interactive` in a Python command inside it, still have a
   terminal (discussion #76).  In a multi-stage pipeline
   (`@deco {...} | next`) the decorator runs on a worker thread via
   `_start_decorator_stage_thread`; `_run_pipeline_from_decorator`
   detects the in-pipe context and forces the body onto the
   pipeline-execution path so its first/last stage read/write the
   thread's rebound stdio (the outer pipe ends).
5. **`@watch` built-in** in
   [eosh/decorators/watch.py](../src/eosh/decorators/watch.py)
   — alternate screen with a header off a TTY-aware check, streaming
   otherwise, `BrokenPipeError` graceful exit.
6. **`@<TAB>` completion** in
   [shell.py::_maybe_decorator_completion](../src/eosh/shell.py):
   three cases — decorator-name list, decorator-flag picker via
   `OptionsCompleter`, and body-command delegation through the
   normal command-completion path.
7. **Tests** in
   [tests/test_decorators.py](../tests/test_decorators.py): 50 tests
   covering registry, parser (all brace-handling cases from the
   "Brace handling" subsection plus error-path coverage),
   composition (parser + execution), executor indirection,
   dispatch, and completion.

## What's left for follow-up commits

Tracked in
[discussion #67](https://github.com/crftwr/eosh/discussions/67):
stacking, outer sequencing after a decorator scope, slot-aware
`@watch`, more built-ins, typo suggestions, and tests pinning history
and redirect binding.
