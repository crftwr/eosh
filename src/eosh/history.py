"""The command history, in SQLite (``~/.eosh/history.db``).

One row per command line run, with where and how it ran::

    id        INTEGER PRIMARY KEY   -- run order, across every eosh process
    cmd       TEXT                  -- the line as typed (continuations joined)
    cwd       TEXT                  -- directory it ran in (normcase'd)
    ctx       TEXT                  -- context name it ran in
    ts        REAL                  -- start time (epoch seconds)
    status    INTEGER               -- exit status, NULL until it finishes
    duration  REAL                  -- seconds, NULL until it finishes

Why SQLite: several eosh processes share one history.  Each command is one
``INSERT`` (and one ``UPDATE`` when it finishes), so concurrent shells never
overwrite each other's rows the way a rewritten file would, and a line run in
one window is searchable from another at once.  ``sqlite3`` is in the
standard library, so the core stays dependency-free.

The default rollback journal is used rather than WAL: WAL needs shared
memory, which a network-mounted home directory may not provide, and history
writes are far too small for WAL's concurrency to matter.

A history must never break the shell: every database error is swallowed (a
failed write is a lost history row, not a failed command), and a database
that can't be opened at all degrades to an in-memory one for the session.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id       INTEGER PRIMARY KEY,
    cmd      TEXT NOT NULL,
    cwd      TEXT,
    ctx      TEXT,
    ts       REAL NOT NULL,
    status   INTEGER,
    duration REAL
);
CREATE INDEX IF NOT EXISTS history_cwd_cmd ON history (cwd, cmd);
CREATE INDEX IF NOT EXISTS history_cmd ON history (cmd);
"""

# Larger than any code point, so ``prefix <= cmd < prefix + _MAX_CHAR`` is
# "starts with prefix" as an index range (SQLite compares TEXT as UTF-8
# bytes, which orders like code points).
_MAX_CHAR = "\U0010ffff"


def norm_dir(path: str) -> str:
    """The form a directory is stored and compared in.  ``normcase`` matters
    on Windows, where two spellings of one cwd can differ only in case."""
    return os.path.normcase(os.path.abspath(path))


@dataclass(frozen=True)
class HistoryEntry:
    id: int
    cmd: str
    cwd: str | None
    ctx: str | None
    ts: float
    status: int | None
    duration: float | None


class HistoryStore:
    """The shared history.  Thread-safe: a command that finishes in the
    background records its status from the slot's thread."""

    def __init__(self, path: Path | None) -> None:
        self._lock = threading.Lock()
        self._db = self._open(path)

    def _open(self, path: Path | None) -> sqlite3.Connection:
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                return self._connect(str(path))
            except (OSError, sqlite3.Error):
                pass        # unwritable / corrupt — keep a history for this session
        return self._connect(":memory:")

    @staticmethod
    def _connect(target: str) -> sqlite3.Connection:
        # isolation_level=None: autocommit, so each statement is its own
        # short transaction; timeout is how long to wait for another eosh
        # process's write lock.
        db = sqlite3.connect(target, timeout=2.0, isolation_level=None,
                             check_same_thread=False)
        db.executescript(_SCHEMA)
        return db

    def _run(self, sql: str, params=()) -> list[tuple]:
        with self._lock:
            try:
                return self._db.execute(sql, params).fetchall()
            except sqlite3.Error:
                return []

    # ── Writing ─────────────────────────────────────────────────────────

    def add(self, cmd: str, cwd: str | None = None, ctx: str | None = None) -> int | None:
        """Record *cmd* as starting now in *cwd* (default: the process's).
        Returns the row id for :meth:`finish`, or ``None`` if not recorded."""
        cmd = cmd.strip()
        if not cmd:
            return None
        if cwd is None:
            try:
                cwd = os.getcwd()
            except OSError:
                cwd = None
        with self._lock:
            try:
                cur = self._db.execute(
                    "INSERT INTO history (cmd, cwd, ctx, ts) VALUES (?, ?, ?, ?)",
                    (cmd, norm_dir(cwd) if cwd else None, ctx, time.time()))
                return cur.lastrowid
            except sqlite3.Error:
                return None

    def finish(self, entry_id: int | None, status: int, duration: float) -> None:
        """Record how the command added as *entry_id* ended."""
        if entry_id is None:
            return
        self._run("UPDATE history SET status = ?, duration = ? WHERE id = ?",
                  (status, duration, entry_id))

    # ── Reading ─────────────────────────────────────────────────────────

    def suggest(self, prefix: str, cwd: str) -> str | None:
        """The most recent line run in *cwd* that extends *prefix* — the
        line editor's ghost suggestion.  Single-line entries only."""
        if not prefix.strip():
            return None
        rows = self._run(
            "SELECT cmd FROM history"
            " WHERE cwd = ? AND cmd > ? AND cmd < ? AND instr(cmd, char(10)) = 0"
            " ORDER BY id DESC LIMIT 1",
            (norm_dir(cwd), prefix, prefix + _MAX_CHAR))
        return rows[0][0] if rows else None

    def recent_commands(self, limit: int = 1000) -> list[str]:
        """The last *limit* lines run anywhere, oldest first, with immediate
        repeats collapsed — what seeds a session's Up/Down list."""
        rows = self._run("SELECT cmd FROM history ORDER BY id DESC LIMIT ?", (limit,))
        out: list[str] = []
        for (cmd,) in reversed(rows):
            if not out or out[-1] != cmd:
                out.append(cmd)
        return out

    def distinct(self, limit: int = 5000) -> list[HistoryEntry]:
        """Each distinct line once, at its most recent run, newest first —
        what Ctrl+R searches."""
        rows = self._run(
            "SELECT h.id, h.cmd, h.cwd, h.ctx, h.ts, h.status, h.duration"
            " FROM history h JOIN (SELECT MAX(id) AS id FROM history GROUP BY cmd) last"
            " ON h.id = last.id ORDER BY h.id DESC LIMIT ?", (limit,))
        return [HistoryEntry(*r) for r in rows]

    def entries(self, limit: int = 25, *, keywords: list[str] | None = None,
                cwd: str | None = None) -> list[HistoryEntry]:
        """The last *limit* runs, oldest first, optionally only those run in
        *cwd* and containing every one of *keywords* (case-insensitively)."""
        where, params = [], []
        if cwd is not None:
            where.append("cwd = ?")
            params.append(norm_dir(cwd))
        for kw in keywords or []:
            where.append("instr(lower(cmd), ?) > 0")
            params.append(kw.lower())
        sql = "SELECT id, cmd, cwd, ctx, ts, status, duration FROM history"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        rows = self._run(sql, (*params, limit))
        return [HistoryEntry(*r) for r in reversed(rows)]

    def close(self) -> None:
        with self._lock:
            self._db.close()
