# Enhancement Ideas

A living document for eosh enhancement ideas — features that would be
nice to have but aren't yet implemented. Each entry should be enough for a
future implementer (or design discussion) to pick up cold; flesh out
sections as the idea matures. Once an idea ships, either delete it or move
it under a "Shipped" subsection with the commit that landed it.

For known limitations of existing features, see [limitations.md](limitations.md).

## Pipeline decorators — follow-up items

The decorator feature itself is shipped and documented in
[decorators.md](decorators.md). The open follow-up work below stays
here in enhancements.md until each item lands.

- **Stacking** (`@time @watch {ls}`) — the parser currently peels one
  decorator. Loop in `_extract_decorator_prefix` and chain calls in
  dispatch.
- **Outer sequencing after a decorator scope** (`@deco {...} ; pwd`,
  `@deco {...} && other`) — currently rejected with a clear error.
  Allowing it means letting the outer-sequence parser treat the
  decorator-stage as one statement; the parser already isolates the
  decorator scope so the additional change is small.
- **More built-ins** — `@time`, `@retry` and `@quiet` are shipped
  (alongside `@watch`; `@bg` was removed in #39 — `Ctrl+]` covers it).  Future candidates: `@confirm`
  (prompt before running) and `@nice -n N` (process-priority wrapper).
- **Slot-aware `@watch`** — route long-running decorator bodies
  through `PythonCommandSlot` so `Ctrl+]` backgrounding works the
  same as for regular Python commands.
- **`@watche ls` typo suggestions** — we own the lookup so suggesting
  `@watch` is a small, low-risk follow-up.
- **History** — `@watch ls` is stored in history as written, not
  per-iteration. Almost certainly the right default; just hasn't
  been pinned with a test.
- **Redirects bound to the decorator vs. the body** —
  `@time {make} > build.log` currently binds the redirect to the
  body's only stage, so `@time`'s own timing line would print to
  the terminal rather than land in the file. Likely correct; codify
  with a test once `@time` ships.

## `awsut bedrock-agentcore` — the rest of the service

Two groups are shipped read-only plus `delete`:
`harness` (`addons/awsut/agentcore/harness.py`: `list`, `describe`, `versions`,
`endpoints`, `watch`, `delete`) and `memory`
(`addons/awsut/agentcore/memory.py`: `list`, `describe`, `strategies`, `watch`,
`delete` on the control plane; `actors`, `sessions`, `events`, `event`,
`records`, `record`, `search`, `jobs` on the data plane). What is
deliberately not in those cuts, roughly in the order it becomes worth
having:

- **`harness invoke`.** `InvokeHarness` answers with an **event stream**,
  not a response dict, so it needs a renderer that prints events as they
  arrive and flushes per event (the same reason `read_log_stream` flushes
  per pass — in a pipeline, `| grep` must see output before the stream
  ends). The plumbing it used to also need is now in place: the data-plane
  client and `var agentcore_data_endpoint=` landed with the `memory`
  group. It is the leaf that makes `harness` useful for more than
  inspection.
- **`harness create` / `update`, and endpoint lifecycle**
  (`Create/Update/DeleteHarnessEndpoint`). A harness is defined by the
  `model` / `systemPrompt` / `tools` / `skills` / `memory` structures
  that `describe` prints — a file's worth of JSON, not a line of flags.
  So the shape to design is almost certainly
  `create --config FILE` (`-` for stdin) with a handful of flags for the
  scalars (`--execution-role`, `--max-iterations`, …), which makes
  `describe --raw` the way you get a starting point to edit. Deciding
  that is the whole design; the calls themselves are one-liners.
- **`memory create` / `update`.** `CreateMemory` takes the
  `memoryStrategies` list that `describe` prints and `strategies` tables,
  plus `eventExpiryDuration` and the KMS/role scalars — the same file's
  worth of JSON as `harness create`, so the same `--config FILE` design,
  decided once for both. `UpdateMemory` is harder than it looks: its
  strategy input is a three-way add/modify/delete diff rather than a
  replacement list, which is a flag surface worth designing rather than
  transcribing.
- **Writing into a memory, and pruning one.** `CreateEvent` (append a
  turn), `DeleteEvent`, `DeleteMemoryRecord`, the `BatchCreate` /
  `BatchUpdate` / `BatchDeleteMemoryRecords` trio, and
  `StartMemoryExtractionJob`. Left out for two different reasons.
  `CreateEvent` and the batch record calls take a payload, so they belong
  with the `--config FILE` design above. `DeleteEvent` /
  `DeleteMemoryRecord` are one-liners, but they destroy *part* of a live
  conversation, which is a worse failure mode than `memory delete`'s: there
  is no way to tell from the shell whether an event is one the agent is
  mid-way through, and no undo. If they land they should take `memory
  delete`'s `-y` confirmation and print the payload before asking — which
  `memory event` already knows how to render.
- **The other AgentCore resource types** — gateways, browsers, code
  interpreters, runtimes. Each is a sibling group under
  `bedrock-agentcore`, and the shared plumbing they need (a client per
  plane, status vocabulary, `cache_key`, `mark_for`, `render_detail`) is
  already in `addons/awsut/agentcore/render.py` — `statuses(operation, list_key)`
  reads a resource's status enum out of whatever list operation it has, so
  a new group inherits the marks without hard-coding words. Note that
  **browser and code-interpreter sessions are a different thing from
  `memory sessions`**: `Start/List/StopBrowserSession` and the
  code-interpreter equivalents are live compute sessions, not stored
  conversations, so they belong to their own group's leaves — which is why
  `memory`'s docstring says up front which sense of "session" it means.
- **`list` server-side filters.** `ListHarnesses` and `ListMemories` both
  take only `maxResults` / `nextToken`, so `--status` / `--contains` filter
  in the shell after the fetch, which interacts with `--max` (the note
  under the table says so when it does). If the API ever grows filters,
  they should move server-side and that note should go away.
- **`memory sessions` with no ACTOR costs one call per actor.**
  `ListSessions` requires an `actorId`, so the "every actor" default fans
  out over `ListActors` and reports how many actors it scanned. Fine for a
  handful, linear in a memory holding thousands. There is no cross-actor
  session listing to use instead, so the options are concurrency (a thread
  pool over actors) or requiring ACTOR once the count is large.

## Architectural follow-ups

The features have shipped (Python pipelines, decorators,
passthrough subprocesses, sub-command tree, cross-platform terminal),
but the layering hasn't fully caught up to them.  None of the items
below is breaking anything; each improves cohesion, makes the slot
subsystem testable in isolation, and makes the parser/executor
boundary explicit. They can land independently and in any order.

For background, see the "Known structural smells" section at the end
of [architecture.md](architecture.md).

- **Extract `slots.py` (or `slots/` package).** Move
  `_StdoutProxy`, `_NullBuffer`, `_PyStageHandle`,
  `PythonCommandSlot`, the three `_ThreadLocal*`
  stdio routers, `_dup_threadlocal_override_fd`, the
  `_current_slot` / `_in_pipeline` thread-locals, and the
  `passthrough_run` / `passthrough_input` /
  `passthrough_input_block` free functions out of `shell.py` into one
  module. Roughly halves `shell.py`. The free functions become
  natural top-level exports of the slot module rather than
  reaching into module-private thread-locals from `shell.py`.
  *Risk:* moderate — the slot needs a callback into the shell to
  re-execute a pipeline (for `Pipeline.run` re-entry); a
  small `slots.set_pipeline_runner(callable)` hook (or
  `ExecutionEnvironment` — see below) handles that cleanly.

- **Extract a `dispatch.py` (pipeline executor) from `shell.py`.**
  A `PipelineExecutor` class owning `_execute`, `_execute_pipeline`,
  `_execute_stage`, `_start_python_stage_thread`,
  `_start_decorator_stage_thread`, `_execute_decorator_stage`,
  `_run_python_command_sync`, `_execute_external*`,
  `_tokenize_stage`, `_expand_alias`, plus the redirect-resolution
  code.
  This is also the module that should *own* the `Pipeline.run`
  executor — `Pipeline.run` becomes `executor.run(pipeline)`
  injected via constructor. Big payoff: the redirect path becomes
  testable without spinning up a full `Shell`. *Risk:* high — most
  of the runtime state lives here; needs a clear value object
  (`ShellEnvironment`) carrying the registries and the context
  manager.

- **Replace the module-global setters with one
  `ExecutionEnvironment` interface.** Today `Shell.__init__`
  calls parallel registration hooks:
  `pipeline.set_pipeline_executor`, plus the implicit
  `_current_slot` / `_in_pipeline` thread-locals consumed by the
  free `passthrough_*` functions. Replace with a small Protocol
  carrying `run_pipeline(pipeline)`,
  `current_slot()`,
  `in_pipeline()`. Carry it
  via `contextvars` so two `Shell` instances can coexist.
  *Risk:* low–moderate — the wiring exists; this is renaming and
  consolidation.

- **Unify the two raw-mode forwarding loops.**
  `_enter_forwarding_mode` (PTY-backed `ProcessSlot`) and
  `_enter_python_forwarding_mode` (`PythonCommandSlot`) duplicate
  ~80% of their logic — termios snapshot/restore, SIGWINCH+SIGINT
  install, `\x1d` interception, byte forwarding. Factor into one
  `ForwardingLoop` taking a slot interface
  (`is_alive` / `write_stdin` / `kill` / `resize`); input reads no
  longer need a hook — `passthrough_input` reads the same key stream
  the loop forwards (#38). *Risk:* low — surface
  is small; cuts ~100 lines and removes a bug class (any fix
  today must be applied twice).

- **Move the Ctrl+] context-switch UI into a `switcher.py`** (or
  back into `context.py`). `_show_switch_menu`, `_resume_pty_slot`,
  `_handle_switch`, `_NEW_CTX_SENTINEL`, `_running_contexts`,
  `_confirm_exit` are UI-over-`ContextManager`. *Risk:* low —
  almost a pure move; `_handle_switch` already returns a sentinel
  to `lineedit`, which is a clean boundary. Today
  `_resume_pty_slot` reaches into `slot.terminal_modes`
  (process.py internals) — make this a method on the slot.

- **Promote `pipeline._split_on_operators` to a public name.**
  It is imported from `shell.py` (cross-module use of a leading
  underscore) for both completion-stage isolation and decorator-
  prefix remainder validation. Drop the underscore or move to
  `parsing.py`. *Risk:* trivial (rename).

- **Move `lineedit.History` into its own `history.py`.** The
  class owns the on-disk `~/.eosh/history` file and the
  `history.dirs` directory side table — storage, which doesn't
  belong in the key-dispatch module. (An unused duplicate
  `history.py` was deleted in the #43 sweep.) *Risk:* trivial;
  re-export `History` from `lineedit` for the tests that import it
  from there.

These are sequenced from highest payoff (shrinks `shell.py` the
most, exposes the cleanest public interface) to lowest. Doing the
first two together — `slots.py` + `dispatch.py` — collapses
`shell.py` to roughly the REPL-and-built-ins module its name
implies (~600 lines), which is also what the architecture diagrams
in CLAUDE.md and architecture.md already promise.

## UX brainstorming — closing the CUI/GUI gap

Exploratory ideas for narrowing the gap between CUI and GUI interaction
models. None of these are designed yet — the entries below capture the
problem framing and brainstormed directions so a future design pass can
pick them up without re-deriving the motivation.

- **Preview before execution / pipeline dry-run.** Pipelines are opaque
  until run — `find . -name "*.log" | xargs rm` gives no chance to see
  what would be deleted; `grep ERROR app.log | wc -l` hides the
  intermediate `grep` output. Idea: a live per-stage preview while
  typing, executing for real on Enter — a two-phase interaction. Open
  question: refresh cadence (every keystroke vs. after a pause).
  Overlaps with the pipe-learning-curve idea below.

- **Mixing filter-based and manual selection — and folding "object-first"
  into it.** GUIs let you narrow a list by filter and then individually
  check/uncheck items; CUI tools (`find`, `grep`, `ps | grep python`,
  `git add -p`) don't compose this way. Idea: give `InlinePicker` a
  "type to filter, Space to toggle individual items" mode, with the
  non-trivial question of whether toggled selections survive a filter
  change. (The old flag-only checkbox picker, `InlineMultiPicker`, was
  removed in #36 — it was a second copy of the picker built for one
  caller. If this comes back it belongs in `InlinePicker` itself, for
  every completer.)

  This turns out to be the key that unlocks "object-first interaction"
  (verb→object vs. object→verb) too — previously framed as its own
  entry needing a dedicated target-selection entry point and a dynamic
  action menu. The simpler framing that emerged from a brainstorming
  session: CUI forces "decide the command, then name the target"
  (`rm file.txt`) only because TAB completion assumes the verb is typed
  first and inserts one value at a time. If TAB completion *is* a good
  enough picker — search/filter/sort/multi-select, anchored at any
  argument position via the existing per-position completer binding
  (`CompletionContext.arg_index`, `FileCompleter` vs. `DirCompleter`
  etc.) — and the caret can move freely (`Ctrl+A/E`, already supported),
  then object-first selection falls out of verb-first typing for free:
  type `mv `, TAB to multi-select source files, jump to end-of-line,
  TAB again to pick the destination directory. Source and destination
  end up using the *exact same* picker UX (just different completers),
  which also resolves the "two different UX in one command" inconsistency
  that a `$sel`/clipboard-variable approach would otherwise have. No new
  commands, no implicit selection variables, no dual-pane mode-shift —
  "object-first" becomes "fill in arguments out of order using a picker
  good enough to trust." The one genuinely new piece this requires:
  **multi-select insertion** — TAB on a multi-select picker must insert
  N items as a single properly-quoted, space-separated token group at
  the cursor, rather than one completion replacing one prefix (today's
  `Completion`/`InlinePicker` path assumes the latter). Get filter+manual
  selection and multi-select insertion right here, and the standalone
  "object-first" entry point mostly stops being necessary.

- **Viewing two places at once.** `cp`/`mv`/`git diff` and side-by-side
  context comparison all want simultaneous views of two locations or
  states; eosh's contexts support *switching* but not *viewing
  side by side*, and tmux-style splits are disconnected from the shell.
  Open question: should the shell own a split-view mode, or is
  "remember one side" good enough for most cases?

- **Structured-data filtering and sorting.** `ps aux`, `ls -la`,
  `git log` are tabular but treated as plain text — sorting/filtering by
  column means reaching for `awk`/`sort`/`grep`, and header rows cause
  off-by-one bugs in `wc -l`. Idea: a built-in viewer that recognizes
  tabular output and supports column sort/filter, opt-in via something
  like a `--view` flag or `@table` decorator (heuristic parsing of known
  command output, à la a lightweight Nushell — fragile but maybe
  practical without sacrificing compatibility).

- **Lowering the pipe learning curve.** The "data flowing through a
  pipeline" mental model is hard to build without visual feedback;
  stderr silently not flowing through pipes, and the `xargs`-or-not
  distinction, are common beginner traps. Idea: build-and-verify pipeline
  UI (overlaps with the preview idea above), more visible/explicit stderr
  handling, and detecting when `xargs` is needed and suggesting it.

Cross-cutting: these are fundamentally TUI design problems bounded by
fixed-width text and cursor control; richer UI trades off against the
"simple shell" ideal, and serving both experienced Bash users and
beginners well may need different modes or progressive disclosure.
Keybinding layout will make or break any of these.
