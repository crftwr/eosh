# History

eosh keeps one command history, shared by every eosh process, in
`~/.eosh/history.db` (SQLite). Three things read it: the **ghost
suggestion**, **Ctrl+R**, and the **`history`** command. Up/Down walks a
per-context list instead (discussion #37).

## What the user sees

| Recall | Scope | How |
|---|---|---|
| Ghost suggestion | lines run **in this directory**, by any context, in any eosh process | dim text after the caret; → / Ctrl+F / Ctrl+E / End accepts it, Alt+F one word |
| Ctrl+R | every line, everywhere | picker filtered by keywords, **starting from what is typed** |
| `history [-n N] [--here] [KEYWORD...]` | every line (or this directory) | plain listing: time, failed exit status, directory, line |
| Up / Down | this context's own session list | seeded from the store at startup; a new context copies its parent's |

```
~/proj 10:42:07> git commit -m "fix typo"
                       ^^^^^^^^^^^^^^^^^^ typed "git co"; the rest is the dim ghost
```

The ghost is the **most recent** line run in the current directory that
starts with the whole buffer, and is shown only while the caret is at the end
of the line. It is never inserted without a key: → at the end of the line
(where it used to do nothing), Ctrl+E / End (likewise), or Alt+F for one word.
Only as much as fits on the row the buffer ends on is drawn. Accepting it
inserts the whole line.

TAB is ordinary completion again. Past lines are no longer mixed into the
picker.

## Storage

```sql
CREATE TABLE history (
    id INTEGER PRIMARY KEY,  -- run order across processes
    cmd TEXT NOT NULL,       -- the line as typed (continuations joined)
    cwd TEXT,                -- normcase'd directory it ran in
    ctx TEXT,                -- context name
    ts REAL NOT NULL,        -- start time
    status INTEGER,          -- exit status, NULL until it finishes
    duration REAL            -- seconds, NULL until it finishes
);
CREATE INDEX history_cwd_cmd ON history (cwd, cmd);   -- the ghost lookup
CREATE INDEX history_cmd ON history (cmd);            -- Ctrl+R's distinct lines
```

**Why SQLite.** The previous storage was a plain `history` file plus a JSON
side table (`history.dirs`). Each process rewrote the side table whole after
every command, so two shells kept erasing each other's directory records. Now
each command is one `INSERT` when it starts (`Shell._record_history`) and one
`UPDATE` when it finishes (`HistoryStore.finish`, from `_execute`, or from the
slot's exit handler for a line sent to the background). Concurrent shells
never overwrite each other, and a line run in one window is a ghost or a
Ctrl+R hit in another at once. `sqlite3` is in the standard library.

- **Rollback journal, not WAL.** WAL needs shared memory, which a
  network-mounted home directory may not provide. History writes are tiny,
  so WAL's concurrency buys nothing.
- **A history never breaks the shell.** Every database error is swallowed (a
  lost row, not a failed command). A database that can't be opened at all
  becomes an in-memory one for the session. A writer waits up to 2 s for
  another process's lock.
- **Prefix lookup is an index range.** `cmd > prefix AND cmd < prefix ||
  U+10FFFF` on the `(cwd, cmd)` index (SQLite compares TEXT as UTF-8 bytes,
  which orders like code points), so `%` and `_` in what is typed are
  literal. Multi-line entries are never suggested.
- **Exit status is recorded, not used for filtering.** A failed line stays
  a ghost candidate, since fixing and re-running it is common. Ctrl+R and
  `history` show the status.

The old `history` / `history.dirs` files are not migrated.

## Context names

The ghost doesn't filter by context. The store is shared by every eosh
process, and a context name isn't unique across them (`default` exists in all
of them, and two shells' `prod` may differ). The name is recorded and shown,
nothing more.

Known gaps are in [limitations.md](limitations.md#history).
