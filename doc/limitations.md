# Known Limitations & Future Improvements

A living document for eosh limitations worth knowing about, and ideas for
future improvements. Add new entries as they come up; once an item is fixed,
either delete it or move it under a "Resolved" subsection with the commit
that addressed it.

## Python commands in pipelines — caveats of the in-process model

Python `@registry.command` handlers participate in pipelines as worker
threads sharing the shell process — `_execute_pipeline` looks up each
stage in the registry, runs registered commands in a thread that rebinds
`sys.stdin` / `sys.stdout` / `sys.stderr` to the pipe ends via the
thread-local routers in `shell.py`, and waits on a mixed list of
`subprocess.Popen` and Python-stage handles. No `fork()`; works the
same on POSIX and Windows.

The remaining caveats below are inherent to the "stay in-process" choice;
fixing them would require a separate process per Python stage.

**Nested `subprocess` writes to the terminal, not the pipe.**

```python
@registry.command(name="my_cmd")
def my_cmd():
    print("hello")              # → goes through the pipe ✓
    subprocess.run(["echo", "x"])  # → writes to the terminal ✗
```

`subprocess` reads the *real* fd 1, not the Python `sys.stdout` object
the thread-local router rebinds. Workaround: pass `stdout=sys.stdout`
(and `stdin=sys.stdin`, `stderr=sys.stderr` as needed) explicitly when
shelling out from a piped Python command. The same caveat applies to
a redirected single stage, which runs as a one-stage pipeline:
`my_cmd > out.txt` redirects `print` but not nested `subprocess` output.

**Stateful built-ins mutate the parent in pipelines.**

`cd | tee log` actually changes the shell's CWD; `var X=1 | …` actually
sets the variable; `context new x | …` actually creates a context. POSIX
shells run each stage in a subshell, so these mutations are normally
discarded — eosh does not. Treat this as the cost of the in-process
model: the change is visible.

**Pure-CPU loops in a Python command can't be Ctrl+C'd in a pipeline.**

The pipeline driver catches `KeyboardInterrupt` and closes pipe ends to
unblock I/O-bound stages, which is enough for the common case (the
worker's next read/write raises `BrokenPipeError`/`OSError` and the
thread unwinds). A stage that's running a tight Python loop with no
I/O won't notice — Python doesn't support cancelling a thread. If a
command wants to be interruptible without I/O, it needs to check for
some flag or use `signal.set_wakeup_fd`-style coordination itself.

**`ctx.run_interactive` / `ctx.input` / `ctx.input_block` / `ctx.choose` are
not usable in piped Python commands** — stdin/stdout are wired to pipes, not the terminal, so
those helpers can't do their job. They raise `RuntimeError` if called
from inside a pipeline thread. Use plain `subprocess.run` (with the
`stdout=sys.stdout` workaround above) for non-interactive children.

## `@watch` shows one screenful, and `q` waits for the current run

`@watch` was cut to watch(1)'s core in discussion #39: output longer than
the screen is cut off (no scrolling — pipe the body through `head` /
`tail`), and `q` is only read between runs. Ctrl+C works at any time — it
reaches the running body, and a run that ends with status 130 (or a
signal) stops the loop — with one gap: a Ctrl+C that lands in the last
moment of a lone external body (`@watch --no-clear date`) goes to that
command's PTY as it exits and is lost, so the loop goes on until the next
press.

## `awsut sagemaker jobs` — a category the loaded model doesn't declare costs a round-trip

`JobCategory` is required by ListJobs and DescribeJob, so a job cannot be
looked up by name alone: with no `--category`, the commands query every
category they know about. That set is the loaded botocore model's
`ListJobs` enum plus anything in `jobs.EXTRA_JOB_CATEGORIES` (empty by
default — see the docstring there for how to add one from
`~/.eosh/config.py`).

botocore treats an enum as documentation and does not reject a value
absent from it, so an undeclared category reaches the service and the
service decides. `jobs.category_not_offered` classifies the resulting
`ValidationException` as "this endpoint does not have that category" and
reports the set once as a compact note, rather than one stderr line per
category on every `jobs list`. What remains is the wasted call: an
undeclared category is still *tried*, so `list` / `describe` / `watch`
spend one extra round-trip per such category per pass, and the job-name
completer one per typed token. Caching keeps the completer cost off the
keystroke path but does not remove it. A category that graduates into the
model stops costing anything, with no code change.

## `awsut sagemaker studio watch` sees spaces and apps, but not the domain

The watch resolves `--domain` once and then polls two listings —
ListSpaces and ListApps — so the two resources that actually move during a
start or a stop are covered, and the cost per tick is O(1) in the size of
the domain rather than one DescribeApp per app. Three consequences:

- **The domain's own status is not tracked.** A domain going `Updating` or
  `Deleting` shows up only indirectly, as its spaces and apps changing or
  as a poll starting to fail. Watching a domain teardown would need a
  third call per tick (ListDomains) for a status that changes about twice
  in a domain's life.
- **`--max` bounds each listing, and the window can slide.** Both calls
  page to `--max` items of a newest-first listing, so in a domain holding
  more spaces or apps than the cap, a newly created one pushes the oldest
  out of the window — and the watch reports that as `no longer listed`
  when nothing was deleted. Raising `--max` or scoping with `--space`
  avoids it.
- **A space's deletion is inferred, not observed.** An app reaches
  `Deleted` and says so, because ListApps keeps returning it; a space just
  stops being listed, which is reported as a departure carrying its last
  known status.

## `awsut bedrock-agentcore memory` derives the name from the id, and resolves a name by listing

`ListMemories` returns `id` / `status` / `createdAt` / `updatedAt` /
`managedByResourceArn` and no name, so `memory list`'s NAME column is
computed by `memory.name_of()`: strip a trailing `-` plus ten
alphanumerics. That is exact for today's ids — a memory id is
`<name>-<10 chars>` and the name pattern
(`[a-zA-Z][a-zA-Z0-9_]{0,47}`) forbids `-` — but it is a *format*
assumption, not a documented contract. Two consequences:

- **A future id format silently changes the column.** An id that doesn't
  end in that shape is printed whole (so nothing is mangled), but a longer
  suffix would leave part of it in the NAME cell. The `help` text marks
  the column as derived; `describe` calls GetMemory and prints the real
  `name`, which is the way to check.
- **A name selector costs a full listing.** There is no lookup-by-name
  API, so `resolve_memory_id` pages through up to
  `memory.RESOLVE_MAX` (500) memories and matches on the derived name.
  Pasting an id — or an ARN — skips it entirely. In a region with more
  than 500 memories, a name that exists past the cap reports as
  not-found; the error names what it did see, which is the hint that the
  cap was hit.

Neither applies to the data-plane leaves, which only ever take an id.

## Desktop notifications can still fire for an interactive command

`notify.SKIP_COMMANDS` suppresses the obvious cases — editors, pagers,
process monitors, `ssh`, multiplexers, interactive sub-shells — by
matching the basename of the line's first word. It is a heuristic and it
misses in both directions:

- **A wrapper hides the interactive program.** `make menuconfig`,
  `git rebase -i`, `docker run -it ubuntu bash`, `kubectl exec -it`,
  `aws ssm start-session` all sit at a prompt for as long as the user
  wants and then "finish", earning a meaningless popup. The first word is
  the only thing examined, so no amount of tuning the set catches these.
- **The first word isn't the program.** `env FOO=1 vim` and `nohup vim`
  are not skipped; `sudo make` *is* skipped only if `sudo` were listed
  (it isn't, deliberately — `sudo make install` is worth reporting).
- **A skipped name can be a batch job.** `ssh host 'make release'` is a
  real long job and is silently skipped because it starts with `ssh`.

Deciding this properly means asking whether the process actually read from
the terminal — e.g. tracking whether the PTY slot ever received forwarded
stdin bytes, which `ProcessSlot` is in a position to know. That would
subsume the skip list for external commands (a `vim` session that took
keystrokes is self-evidently interactive) but not for Python commands. Until then: add to `notify.SKIP_COMMANDS` from
`~/.eosh/config.py`, or `var notify=off` for a session spent in
interactive tools.

## Notification delivery is best-effort and mostly unverifiable from the shell

Every backend in `notify.py` is spawned as a subprocess with its output
discarded and every exception swallowed, so `command_done()` returning
`True` means "a notification was dispatched", never "the user saw it".
Specifically:

- **macOS attributes the notification to the terminal app**, because
  `osascript` posts on behalf of whatever host is running it. If
  notifications are denied for Terminal/iTerm2/VS Code in System
  Settings → Notifications, nothing appears and there is no error to
  detect. There is no way to ask for permission from a CLI, and no
  API to query the current grant.
- **The Windows toast path is untested on real hardware.** It is written
  against the documented WinRT `ToastText02` template and the PowerShell
  AppID, but the whole feature was developed and verified on macOS.
  Failure mode is a silent no-op (an unregistered AppID is dropped by the
  notification platform without an error).
- **Linux needs `notify-send` and a running notification daemon.** The
  binary being on `PATH` is what gets probed; a session with no daemon
  listening on the D-Bus name accepts the call and drops it.
- **The bell fallback is easy to miss** — many terminals have the audible
  bell disabled, in which case a fallback notification is nothing at all.

`set_notifier()` is the escape hatch: a custom backend (Slack, `ntfy.sh`,
`tmux display-message`) can be verified end-to-end by the user in a way the
built-in chain can't be.

## A backgrounded command that finishes while you are watching it is reported anyway

`Shell._slot_finished` reports every slot that was parked on a context,
dropping only the `[context]` prefix when that context is the current one —
so this sequence still produces a notification:

1. `make -j8`, `Ctrl+]` to background it,
2. work in another context for two minutes,
3. `Ctrl+]` back to it *before* it finishes and watch the last of the build
   scroll past.

The reported duration correctly covers the whole run, but part of it was
spent in front of the user. Distinguishing "resumed and then finished" from
"finished unobserved" needs the slot to record when it was last activated,
which is more bookkeeping than the noise warrants today.

## A question left open by Ctrl+] is not redrawn on return

If you press Ctrl+] while a command is waiting in `ctx.input` or `ctx.choose`,
the command keeps waiting in the background. When you switch back, its
buffered output is replayed, but the picker or the half-typed line is not
redrawn until the next key arrives. That key is still delivered to it, so
pressing an arrow key or typing continues the answer. Redrawing on resume
would need the slot to tell the forwarding loop "I'm showing a prompt" and to
re-render it on `activate()`.

## History

- **A network-mounted `~/.eosh` may not lock reliably.** The history is a
  SQLite database. Its locking depends on the filesystem, and NFS / SMB
  locking can be unreliable. A failed write loses that one history row, never
  the command.
- **The ghost suggestion is cut at the end of the row.** Only as much as fits
  after the buffer on its row is drawn (a wrapping ghost would complicate
  every redraw's row bookkeeping). Accepting it still inserts the whole line.
- **Ctrl+R starts empty when the line is too long to fit on the prompt row**
  next to the prompt.

## Key bindings

- **Native Windows delivers fewer chords.** `msvcrt` reports only the
  arrows, Home/End, Insert/Delete and PageUp/PageDown as special keys. Ctrl- or
  Alt-modified special keys, F-keys and Alt-letters never arrive, so a binding
  to them never fires there. Ctrl-letters work everywhere.
- **Some chords share bytes.** A terminal sends the same byte for Ctrl-I and
  Tab, Ctrl-M and Enter, Ctrl-H and Backspace, and Ctrl-[ and Esc. Binding one
  binds the other. Ctrl-Shift-letter, Ctrl-Tab and Ctrl-Enter have no code at
  all and are refused.
- **Only `prompt.*` actions can be user-defined.** A user action can't yet
  run on a picker (for example a key in the TAB picker that inserts every
  candidate).
- **The context-name prompt is not configurable.** The `InlineArgPrompt` that
  names or renames a context keeps Enter, Esc, Ctrl-C and Backspace.
- **Binding `prompt.switch_context` to an escape-sequence key** (Alt-x, an
  arrow) works at the prompt, but while a command runs the shell finds it by
  scanning raw input chunks. Bound to Esc itself, it would fire on every
  escape sequence the program receives.

## Event hooks — what they don't see

- **A backgrounded line's `on_command_finished` runs on a background
  thread.** It is called from the slot's exit handler when the work ends,
  which may be while you are typing at the prompt. Output from the hook
  lands in the middle of the line editor's display. Post a notification or
  write a file there, but don't print. Handing the call to the main thread
  would need a way to wake the line editor, which it doesn't have.
- **`on_command_not_found` is asked only for a command run on its own.** A
  missing command inside a pipeline or with a redirect (`nope | cat`,
  `nope > out`) is reported by the pipeline path as before.
- **`on_exit` runs on `exit` and Ctrl+D only.** A shell killed by a signal
  (the terminal window closed, `SIGHUP`) ends without it.
- **A directory change made by a still-running Python command** (a
  backgrounded one calling `os.chdir`) is reported at the next check (after a
  command, a switch, or before a prompt), not at the moment it happens.

## `source-bash` imports variables and the cwd — nothing else

The dump the child bash writes is `env -0` plus `$PWD`, so what comes
back is exactly what the *environment* can carry. Everything else a
`source`d script can establish stays in the child and is lost when it
exits:

- **Shell functions and aliases.** Bash exports functions as
  `BASH_FUNC_name%%` env entries in an encoding only bash understands;
  they are skipped deliberately (`_BASH_ENV_SKIP_PREFIXES`) rather than
  imported as nonsense variables. eosh has no shell-function concept
  to import them *into*; `alias` exists but bash aliases are not exported
  at all, so `source-bash` cannot see them. Net effect: sourcing a
  `~/.bashrc`-style file gives you its variables and none of its
  helpers.
- **Shell options.** `set -o`/`shopt` state, `umask`, `ulimit`, and
  non-exported (`local`/plain) variables are invisible to `env`.
- **A script that installs its own EXIT trap** replaces the one that
  writes the dump, so nothing is imported. This is reported
  (`environment not imported`) rather than silently applied — the
  alternative reading of an empty dump is "the script unset every
  variable", which would wipe the shell's environment.

One smaller edge: a key that bash cannot bind to a variable (`not-an-identifier=1`, put in
the environment by some other program) is never *removed* on import,
because its absence from the dump doesn't prove the script unset it.

Windows needs a `bash` on `PATH` (Git Bash's, typically); without one the
command reports `no 'bash' on PATH` and does nothing.

## argcomplete completion borrows the shell's fd 8

argcomplete hard-codes fd 8 as the channel for its candidates. To hand the
child a pipe there, `ArgcompleteCompleter._invoke` `dup2`s the pipe's write
end onto fd 8 *in the shell process*, spawns the child with `pass_fds=(8,)`,
then restores or closes fd 8. For that window, fd 8 means something else
process-wide. A background slot thread that happens to open a file, socket
or pipe and get fd 8 back can have it clobbered, or leak into the child.
The window is a single `Popen`, so this is unlikely, but it isn't
impossible.

`subprocess` has no "map this fd to child fd N" option. The race-free fixes
are a tiny exec wrapper that does the `dup2` in the child (e.g.
`sh -c 'exec "$@" 8>&3' -- tool` with the pipe on fd 3), or newer
argcomplete's `_ARGCOMPLETE_STDOUT_FILENAME`, which writes to a path
instead of fd 8 (version-dependent). Not done yet: the fallback's real-world
use hasn't been confirmed (see discussion #34).
