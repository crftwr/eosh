# Context System Design

## Overview

The context system provides named environments that bundle variables and a working directory. Switching contexts restores the associated state, making it easy to work across multiple environments (e.g., AWS accounts, projects, clusters) within a single shell session.

Contexts also serve as the unit of **process multiplexing**: each context can hold a live PTY subprocess. Pressing `Ctrl+]` opens a picker to switch between contexts; if the target context has a running process, it is resumed immediately without being killed.

## Data Model

### ContextState

```python
class ContextState(Enum):
    IDLE    = auto()   # no process attached
    RUNNING = auto()   # process_slot.is_alive() is True
    EXITED  = auto()   # process finished but slot not yet cleaned up
```

### Context

```python
@dataclass
class Context:
    name: str                           # unique identifier
    variables: dict[str, str]           # key-value pairs exported to os.environ
    cwd: str                            # saved working directory
    process_slot: ProcessSlot | None    # optional running subprocess

    @property
    def state(self) -> ContextState: ...  # derived from process_slot
```

A context captures:
- **Variables**: exported to `os.environ` on activation, restored on deactivation. Subprocesses inherit these automatically.
- **Working directory**: saved when leaving, restored when entering. Each context remembers where you were.
- **Process slot**: an optional PTY-backed subprocess. When a context has a live process and is switched away from, the process keeps running and its output is buffered until you switch back.

### ContextManager

```python
class ContextManager:
    contexts: dict[str, Context]     # all known contexts by name
    current_name: str | None         # which context is active
```

The manager maintains:
- A **named collection** of all contexts (addressable by name)
- A **current pointer** indicating the active context
- A **most-recently-used order** (current first). The `Ctrl+]` picker lists
  contexts in this order, and it decides which context becomes current when
  the current one is closed.

There is no push/pop stack. Its only effect was choosing the next current
context after a removal, and the MRU order already answers that (discussion
#41).

## Operations

### New (create + switch)

```
context new prod
```

Creates a context named `prod` and switches to it. It inherits the current
context's variables and its Up/Down history, and starts in the current
directory (`ContextManager.new`). `Ctrl+N` in the picker creates contexts the
same way.

> **Note:** set the new context's own variables with `var` afterwards:
> ```
> eosh> context new prod
> [prod] eosh> var ACCOUNT=123456 REGION=us-east-1
> ```

### Switch

```
context switch staging
```

Makes any existing context current. Nothing is removed.

### Close

```
context close [name]
```

Deletes the named context, or the current one when no name is given. When it
was current, the most recently used remaining context becomes current. It
follows the same rules as `Ctrl+D` in the picker: it refuses the last
remaining context, and a context with a live process (`context kill` it
first).

### Kill

```
context kill <name>
```

Sends SIGTERM to the running process in the named context. The context itself is not removed.

### Variables

```
var KEY=VALUE [KEY=VALUE ...]    # set one or more context variables
var KEY                          # print the current value of KEY
var KEY= [KEY= ...]              # remove variables from context and os.environ
var                              # list all current environment variables
```

`var` sets variables on the **current context** and immediately exports them to `os.environ`. They will be re-applied whenever this context is switched to.

### Ctrl+] — Live Context Switch

Pressing `Ctrl+]` at the shell prompt (or during a running process) opens an inline picker listing all contexts. Each entry shows:
- A `*` marker for the current context
- The name of the running command (if any) as a right-aligned label

Selecting a context:
- If it has a live process: the shell enters forwarding mode immediately, resuming that process
- If idle: the shell switches to that context and shows its prompt

The picker also shows a small **preview pane** below the list — the last few buffered output lines from the focused context's process slot. This makes it possible to recognise contexts at a glance when they have similar names but are running different commands.

While the picker is open, three action keys mutate the context list in place and re-open the picker:

- `Ctrl+N` — create a new context. Opens an inline prompt for the name and creates the context inheriting the current context's variables (same effect as the old `+ new context` row).
- `Ctrl+D` — delete the highlighted context (including the current one — when the current context is deleted, the most recently used remaining one becomes current). Refused when the highlighted context has a live process or is the only remaining context.
- `Ctrl+R` — rename the highlighted context. Opens an inline prompt pre-filled with the current name.

`Ctrl+N` overrides the picker's default Ctrl+N → "down" alias for the duration of the context-switch picker; the down arrow still works for navigation.

After any action the picker reopens with the updated list. `Esc` cancels and (if `Ctrl+D` removed the current context) the shell falls through to its normal resume path for the new current context.

## Environment Variable Management

### Activation

When a context becomes active:
1. Back up the current value of each variable key in `os.environ` (or `None` if unset)
2. Set each context variable in `os.environ`

### Deactivation

When a context is deactivated:
1. For each backed-up key, restore the original value (or remove if it was `None`)

This ensures:
- Context variables don't leak between contexts
- Pre-existing environment variables aren't permanently lost
- System commands spawned via subprocess see the correct variables

### Working Directory

On switch:
1. Save `os.getcwd()` into the departing context's `cwd`
2. `os.chdir()` to the arriving context's `cwd`

This means you can `cd` around within a context, switch away, and return to find yourself back where you left off.

## Integration with Completion

Completers receive the active context via `CompletionContext.shell_context`. This enables context-aware completions:

```python
class InstanceCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        # Use explicit arg if given, otherwise inherit from context
        account = ctx.args[0] if ctx.args else (
            ctx.shell_context.get_variable("ACCOUNT") if ctx.shell_context else None
        )
        # ... fetch completions for account
```

This pattern lets commands work both ways:
- Explicitly: `connect prod us-east-1` (args override)
- Implicitly: `connect` (inherits from active context)

## State Diagram

```
Shell starts → "default" context created (IDLE, current)
                    │
               context new prod
                    │
                    ▼
         "prod" (IDLE, current), "default" (IDLE)
                    │
               run vim  (vim launches in PTY)
                    │
                    ▼
         "prod" (RUNNING, current), "default" (IDLE)
                    │
               Ctrl+] → switch to "default"
                    │
                    ▼
         "prod" (RUNNING, background)
         "default" (IDLE, current)   ← [bg:1] shown in prompt
                    │
               Ctrl+] → switch back to "prod"
                    │
                    ▼
         "prod" (RUNNING, current) ← vim resumes in foreground
                    │
               quit vim, then: context close
                    │
                    ▼
         "default" (IDLE, current), "prod" deleted
         (close refuses while vim is still running)
```

## Prompt Integration

The shell prompt reflects the active context and any background activity:

- Default context (`"default"`): no context prefix shown
- Named context: `[contextname] path/cwd HH:MM:SS>`
- Background processes: `[contextname] path/cwd HH:MM:SS [bg:N]>`

The `[bg:N]` indicator shows how many other contexts (not the current one) have live running processes.

## Design Rationale

**Why named collection + stack instead of just a stack?**

A pure stack forces linear navigation — you must pop through intermediates to reach a distant context. Named contexts allow direct `switch` to any context at any time. The stack remains available as a convenience for the common "briefly enter context X, then return" pattern.

**Why export to os.environ?**

Subprocess commands (the system fallback) need to see context variables without eosh-specific wiring. Exporting to `os.environ` means `aws`, `kubectl`, `ssh`, and other tools pick up the right account/region/cluster automatically.

**Why save cwd per context?**

Different environments often correspond to different directories (project roots, deployment repos). Saving cwd per context eliminates repetitive `cd` after every switch.

**Why PTY-per-context rather than job control?**

Traditional job control (`bg`/`fg`/`&`) is complex and exposes Unix process groups to the user. Per-context PTY slots are simpler to reason about: each context has at most one foreground process, and `Ctrl+]` is the only way to switch. The tradeoff is that you cannot have multiple background jobs within a single context — use multiple contexts instead.
