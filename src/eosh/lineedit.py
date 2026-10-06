"""DIY line editor with history and TAB completion — no prompt_toolkit."""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import unicodedata
from dataclasses import replace
from pathlib import Path
from typing import Callable

from . import terminal
from .completion import Completion
from .parsing import raw_token_start

CONTEXT_CHANGED_SENTINEL = "\x1d__CHANGED__"

_NEEDS_QUOTING = re.compile(r"[^\w@%+=:,./~-]")


def _shell_quote(s: str) -> str:
    """Like shlex.quote but treats ~ as safe (common in home-dir paths like ~/foo)."""
    if not s:
        return "''"
    if not _NEEDS_QUOTING.search(s):
        return s
    return "'" + s.replace("'", "'\"'\"'") + "'"

_ANSI_RE = re.compile(r"\033\[[0-9;]*[A-Za-z]")


def _wcswidth(s: str) -> int:
    """Terminal display width of s (wide/fullwidth chars count as 2 columns).

    Combining/format characters (Unicode category Mn, Me, Cf) are zero-width
    and checked BEFORE east_asian_width so that NFD-decomposed characters like
    voiced katakana (e.g. ガ → カ + U+3099 combining dakuten) are not
    double-counted.  U+3099 has east_asian_width='W' in Python's unicodedata,
    but it is a combining mark and must be treated as zero-width.
    """
    w = 0
    for ch in s:
        if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue  # zero-width combining / format char
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            w += 2
        else:
            w += 1
    return w


def _visible_len(s: str) -> int:
    """Display width of s after stripping ANSI escape codes (wide chars count as 2)."""
    return _wcswidth(_ANSI_RE.sub("", s))


def _display_col_offset(prefix: str, completions: list[Completion]) -> int:
    """Return how many terminal columns of prefix appear at the start of every display value.

    The picker should open this many columns to the LEFT of the caret so that
    the candidate text aligns with the already-typed partial token.
    E.g. prefix="doc/co", displays=["completion.md","context.md"] → 2 ("co").
    Wide chars in the prefix count as 2 columns each.

    Match case-insensitively so completers that fold case (e.g. ``FileCompleter``
    treating ``cd p`` as matching ``Pictures/``) still align the picker under
    the typed ``p`` instead of falling back to the caret position.
    """
    for start in range(len(prefix) + 1):
        suffix = prefix[start:]
        suffix_lower = suffix.lower()
        if all(c.display.lower().startswith(suffix_lower) for c in completions):
            return _wcswidth(suffix)
    return 0


def _picker_col_offset(prefix: str, completions: list[Completion]) -> int:
    """Like :func:`_display_col_offset`, tolerating verbatim (history) candidates.

    Verbatim candidates are anchored like any other, so they normally align with
    the typed token too and are measured with everything else — that is what
    puts a history-only picker under the token instead of at the caret.

    The exception is a quoted token: a verbatim value starts at the *raw* anchor,
    so its display begins with the quote character while ``prefix`` is what shlex
    left after removing it, and nothing is shared.  Rather than un-align the
    token candidates the user is completing against, fall back to measuring those
    alone.
    """
    offset = _display_col_offset(prefix, completions)
    if offset:
        return offset
    aligned = [c for c in completions if not c.verbatim]
    if not aligned or len(aligned) == len(completions):
        return offset
    return _display_col_offset(prefix, aligned)


def _align_verbatim_rows(line: str, completions: list[Completion],
                         display_offset: int) -> list[Completion]:
    """Trim verbatim displays so every picker row starts at the same column.

    A verbatim (history) *value* starts at the raw anchor, so its display
    repeats the whole partial token; a token row's display starts wherever its
    completer chose to put it — ``FileCompleter`` shows only the last path
    segment, for instance, so ``cat ~/.aws/<TAB>`` lists ``config``, not
    ``~/.aws/config``.  The picker opens ``display_offset`` columns left of the
    caret, which is the column those rows agree on; a verbatim row must
    therefore show only its last ``display_offset`` columns of already-typed
    text or it renders shifted right, reading as though it were duplicating
    what the user has already typed.

    Only the display changes.  The value still starts at the anchor, because
    that is where :meth:`LineEditor._apply` splices it in.
    """
    typed = line[raw_token_start(line):]
    drop = 0
    while drop < len(typed) and _wcswidth(typed[drop:]) > display_offset:
        drop += 1
    if not drop:
        return completions
    return [
        replace(c, display=c.display[drop:])
        if c.verbatim and c.display.startswith(typed) else c
        for c in completions
    ]


def _pending_wrap_row(char_count: int, cols: int) -> int:
    """Row offset below render-top where cursor sits after writing char_count visible chars.

    Writing exactly N*cols chars leaves the cursor in pending-wrap state on the
    last filled row (row N-1), not on the next row. N//cols would be off by one.
    """
    if char_count <= 0:
        return 0
    return (char_count - 1) // cols


def _pending_wrap_col(char_count: int, cols: int) -> int:
    """Column offset (from col 0) for the cursor after writing char_count visible chars.

    When the content exactly fills a row, the cursor sits at the rightmost column
    in pending-wrap state. cursor_char % cols would give 0 (wrong).
    """
    if char_count <= 0:
        return 0
    rem = char_count % cols
    return rem if rem != 0 else cols - 1


# ── Debug logging (opt-in via EOSH_RESIZE_DEBUG=/path/to/log) ──────────────


_RESIZE_DEBUG_PATH = os.environ.get("EOSH_RESIZE_DEBUG")


def _resize_debug(msg: str) -> None:
    """Append *msg* to ``$EOSH_RESIZE_DEBUG`` if set; otherwise no-op.

    Used to instrument SIGWINCH handling in the wild without polluting
    stdout (which would corrupt the line editor's render).
    """
    if not _RESIZE_DEBUG_PATH:
        return
    try:
        with open(_RESIZE_DEBUG_PATH, "a") as f:
            f.write(msg + "\n")
    except OSError:
        pass


# ── History ──────────────────────────────────────────────────────────────────


# Enough to cover the handful of places a given command actually gets run,
# without letting one much-repeated line grow an unbounded directory list.
MAX_DIRS_PER_LINE = 8


def _norm_dir(path: str) -> str:
    """Canonical form used to compare directories.

    ``normcase`` matters on Windows, where the shell's cwd and a previously
    recorded one can differ only by drive-letter or separator case.
    """
    return os.path.normcase(os.path.abspath(path))


class History:
    """The global on-disk command history, plus where each line was run.

    Two files, both under ``~/.eosh/``:

    ``history``
        One command line per entry, in the order they were run — appended to as
        the shell runs.  This is what ``Ctrl+R`` searches and what seeds the
        ``default`` context's Up/Down list at startup.

    ``history.dirs``
        A JSON side table mapping each line to the directories it was run in
        (most recent last, capped at :data:`MAX_DIRS_PER_LINE`).  It scopes TAB
        completion's history candidates to the current directory (see
        :class:`completion.HistoryCompleter`).  A *side* table rather than an
        extra field in ``history`` so that file's format — and every reader of
        :attr:`entries` — stays exactly as it was.  A missing, unreadable or
        stale side table degrades to "no directory is known for this line",
        which the completer handles as "not from here".
    """

    def __init__(self, path: Path):
        self._path = path
        self._dirs_path = path.with_name(path.name + ".dirs")
        self._entries: list[str] = []
        self._dirs: dict[str, list[str]] = {}
        self._load()

    def _load(self) -> None:
        try:
            self._entries = [
                ln for ln in self._path.read_text().splitlines() if ln.strip()
            ]
        except FileNotFoundError:
            pass
        self._load_dirs()

    def _load_dirs(self) -> None:
        try:
            raw = json.loads(self._dirs_path.read_text())
        except (OSError, ValueError):
            return  # absent, unreadable or corrupt — no directories are known
        if not isinstance(raw, dict):
            return
        for line, dirs in raw.items():
            if isinstance(line, str) and isinstance(dirs, list):
                self._dirs[line] = [d for d in dirs if isinstance(d, str)]

    def add(self, line: str, cwd: str | None = None) -> None:
        """Record *line* as run in *cwd* (the process's cwd by default)."""
        line = line.rstrip()
        if not line:
            return
        dirs_changed = self._note_dir(line, os.getcwd() if cwd is None else cwd)
        if self._entries and self._entries[-1] == line:
            # The line itself is already stored, but re-running it after a `cd`
            # is new information, so the directory still has to be recorded.
            if dirs_changed:
                self._save_dirs()
            return
        self._entries.append(line)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a") as f:
                f.write(line + "\n")
        except OSError:
            pass
        self._save_dirs()

    def _note_dir(self, line: str, cwd: str) -> bool:
        """Record *cwd* against *line*. Returns True when anything changed."""
        norm = _norm_dir(cwd)
        dirs = self._dirs.setdefault(line, [])
        if dirs and dirs[-1] == norm:
            return False
        if norm in dirs:
            dirs.remove(norm)          # keep it, but as the most recent
        dirs.append(norm)
        del dirs[:-MAX_DIRS_PER_LINE]
        return True

    def _save_dirs(self) -> None:
        # Drop lines the history file no longer holds, so hand-trimming
        # ``history`` prunes the side table on the next write as well.
        known = set(self._entries)
        self._dirs = {ln: dirs for ln, dirs in self._dirs.items() if ln in known}
        try:
            self._dirs_path.parent.mkdir(parents=True, exist_ok=True)
            self._dirs_path.write_text(json.dumps(self._dirs, ensure_ascii=False))
        except OSError:
            pass   # same policy as the history file: never fail a command over it

    def ran_here(self, line: str, cwd: str | None = None) -> bool:
        """True when *line* was recorded as run in *cwd* (default: the cwd).

        Entries stored before the side table existed have no directory at all,
        so they answer False everywhere and never become TAB candidates (until
        they are run again).  Up/Down and Ctrl+R still reach them.
        """
        cwd = os.getcwd() if cwd is None else cwd
        return _norm_dir(cwd) in self._dirs.get(line.rstrip(), ())

    @property
    def entries(self) -> list[str]:
        return self._entries


# ── Line editor ───────────────────────────────────────────────────────────────

GetCompletionsFn = Callable[[str], tuple[list[Completion], str, str]]
GetArgInfoFn = Callable[[str, int], str | None]


class LineEditor:
    """
    Raw-mode line editor. Handles its own key dispatch, history, and TAB
    completion via InlinePicker. No prompt_toolkit involved.

    prompt() returns the entered line, CONTEXT_CHANGED_SENTINEL when a Ctrl+]
    switch needs the caller to resume the new context's process, or raises
    EOFError (Ctrl+D on empty line) or KeyboardInterrupt (Ctrl+C).
    """

    def __init__(
        self,
        history: History,
        get_completions: GetCompletionsFn,
        get_prompt: Callable[[], str],
        switch_fn: Callable[[], tuple[bool, str | None]] | None = None,
        get_arg_info: GetArgInfoFn | None = None,
        local_history_fn: Callable[[], list[str]] | None = None,
    ):
        self._history = history
        # Returns the current context's per-session Up/Down history list
        # (the actual mutable list). Ctrl-R and the on-disk store stay global
        # (self._history); only Up/Down navigation is scoped per context.
        self._local_history_fn = local_history_fn
        self._get_completions = get_completions
        self._get_prompt = get_prompt
        self._switch_fn = switch_fn
        self._get_arg_info = get_arg_info

        self._buf = ""
        self._cursor = 0
        self._hist_idx = 0
        self._saved_buf = ""
        self._cols = 80
        self._lines = 24
        self._prompt_str = ""
        self._prompt_len = 0
        self._cursor_row = 0  # rows below render-top where cursor sits
        # VSCode integrated terminal does not reflow content on resize;
        # cursor stays at the same row (clamped column). Detect it so we
        # re-render explicitly instead of relying on terminal reflow.
        self._terminal_reflows = os.environ.get("TERM_PROGRAM", "") != "vscode"
        self._hint: str = ""  # transient hint shown after TAB; cleared on next keypress
        self._status_bar_visible: bool = False  # whether the status bar is currently showing something
        self._suppress_statusbar: bool = False  # set by _on_resize, cleared on next keypress
        # While a TUI picker is up the cursor isn't where the line editor
        # thinks it is. SIGWINCH must NOT trigger our _redraw in that case
        # (it would clobber the picker's panel and confuse the picker's
        # cleanup). Pickers poll terminal_size() between key reads and
        # cancel themselves on resize; the next _redraw after _picker_session
        # exits then runs cleanly with the new geometry.
        self._picker_active: bool = False
        # SIGWINCH reentrancy guard. _on_resize does I/O on stdout (DSR
        # query + _redraw); both call sys.stdout.flush(), which holds the
        # BufferedWriter lock. Drag-resize fires many SIGWINCHes — when a
        # second one is delivered while we're already inside _redraw, the
        # nested handler's flush() re-enters that lock and Python raises
        # ``RuntimeError: reentrant call inside <_io.BufferedWriter>``.
        # The flag below lets nested calls bail out (after refreshing the
        # tracked size), and ``_resize_pending`` makes the outer call
        # replay once so the final geometry isn't lost.
        self._in_resize: bool = False
        self._resize_pending: bool = False

    def add_to_history(self, line: str) -> None:
        """Add *line* to history from outside the editor (e.g. after joining continuation lines).

        Appends to the global on-disk store (used by Ctrl-R and to seed the
        default context) and to the current context's per-session Up/Down list.
        """
        self._history.add(line)
        if self._local_history_fn is not None:
            stripped = line.rstrip()
            if stripped:
                local = self._local_history_fn()
                if not local or local[-1] != stripped:
                    local.append(stripped)

    @contextlib.contextmanager
    def _picker_session(self):
        """Suppress the line editor's SIGWINCH redraw while a TUI picker is active.

        TUI widgets paint their own status bar on the bottom row and run
        their own resize-detection (poll ``terminal_size()`` between key
        reads, cancel on change). After the picker returns we mark the
        status bar visible so the next ``_redraw`` explicitly wipes the
        bottom row, since the picker may have left a bar there.

        If the terminal was resized while the picker was up, run our own
        resize-recovery path on exit (DSR + clear) so the line editor
        recovers cleanly rather than relying on stale row tracking.
        """
        prev = self._picker_active
        self._picker_active = True
        cols_before, lines_before = self._cols, self._lines
        try:
            yield
        finally:
            self._picker_active = prev
            self._update_cols()
            self._status_bar_visible = True
            if cols_before != self._cols or lines_before != self._lines:
                # Synthesise the SIGWINCH path: rewind _cols/_lines so
                # _on_resize sees the change, then call it directly. The
                # _picker_active guard is already cleared at this point.
                self._cols, self._lines = cols_before, lines_before
                self._on_resize(None, None)

    def prompt(self, prompt_str: str | None = None) -> str:
        """Read one line.

        The line is *not* added to history: the caller joins continuation
        lines first and records the combined command with
        :meth:`add_to_history`.

        Args:
            prompt_str: If given, display this string instead of calling _get_prompt().
                        Useful for continuation prompts (e.g. ``"> "``).
        """
        self._buf = ""
        self._cursor = 0
        self._hist_idx = 0
        self._saved_buf = ""
        self._prompt_str = prompt_str if prompt_str is not None else self._get_prompt()
        self._prompt_len = _visible_len(self._prompt_str)

        fd = sys.stdin.fileno()
        old_attrs = terminal.get_mode(fd)
        old_sigwinch = terminal.install_resize_handler(self._on_resize)

        try:
            self._update_cols()
            self._cursor_row = 0
            terminal.set_raw(fd)
            self._redraw()

            while True:
                key = terminal.read_key(fd)
                result = self._handle_key(key, fd)
                if result is not None:
                    self._cursor = len(self._buf)
                    self._redraw()
                    self._clear_status_bar()
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    return result
                self._redraw()
        except (EOFError, KeyboardInterrupt):
            self._clear_status_bar()
            sys.stdout.write("\r\n")
            sys.stdout.flush()
            raise
        finally:
            terminal.restore_mode(fd, old_attrs)
            terminal.restore_resize_handler(old_sigwinch)

    # ── terminal size ────────────────────────────────────────────────────────

    def _update_cols(self) -> None:
        try:
            sz = os.get_terminal_size()
            self._cols = sz.columns
            self._lines = sz.lines
        except OSError:
            self._cols = 80
            self._lines = 24

    def _on_resize(self, _sig, _frame) -> None:
        # While a TUI picker is up the cursor isn't where the line editor
        # thinks. Redrawing now would paint over the picker's panel and
        # break its cleanup. The picker polls terminal_size() between key
        # reads and cancels itself on resize; the next _redraw (after
        # _picker_session exits) handles the new geometry cleanly.
        if self._picker_active:
            self._update_cols()
            return
        # SIGWINCH can be delivered between bytecodes while we're already
        # inside this handler — typically during _redraw's stdout writes,
        # which hold the BufferedWriter lock. A nested handler that calls
        # query_cursor_position or _redraw would re-enter that lock and
        # crash with ``RuntimeError: reentrant call inside ...``. Bail out
        # of the nested call but record that another resize happened so
        # the outer call replays once with the latest geometry.
        if self._in_resize:
            self._resize_pending = True
            self._update_cols()
            return
        self._in_resize = True
        try:
            self._handle_resize()
            while self._resize_pending:
                self._resize_pending = False
                self._handle_resize()
        finally:
            self._in_resize = False

    def _handle_resize(self) -> None:
        old_cursor_row = self._cursor_row
        old_cols = self._cols
        self._update_cols()

        # Suppress status-bar drawing for this resize-driven redraw. Drag-
        # resize delivers many SIGWINCHes and repainting the bar on each one
        # is jarring (and on terminals that reflow at width-change boundaries
        # the old bar's row would not be cleared by the new draw, leaving
        # ghosts). The next keypress clears the flag and the bar comes
        # back immediately.
        self._suppress_statusbar = True

        # Ask the terminal where the caret is now. Whether the terminal
        # reflowed (Terminal.app, iTerm2, VSCode at width changes) or not
        # (some VSCode resize cases, certain emulators), DSR reports the
        # caret's *current* absolute row, which is all we need to compute
        # the prompt's render-top and clear from there.
        fd = sys.stdin.fileno()
        pos = terminal.query_cursor_position(fd)
        _resize_debug(
            f"DSR={pos} old_cursor_row={old_cursor_row} "
            f"cols={old_cols}->{self._cols} lines={self._lines}"
        )
        if pos is not None:
            caret_row, _ = pos
            # Render-top = caret_row - cursor_row_offset_within_prompt.
            #
            # On width changes, every terminal we target (Terminal.app,
            # iTerm2, VSCode integrated) reflows: the cursor moves with
            # the logical content to its new wrapped row, so the cursor's
            # row offset under the NEW geometry (``new_cursor_row``) is
            # the right value.
            #
            # On height-only changes (no width change), content layout is
            # unchanged, so the cursor's row offset matches what we
            # tracked before the resize (``old_cursor_row``). Picking
            # ``min`` of both estimates would over-clear by one row when
            # reflow shrank the prompt back to a single row, erasing
            # prior output above the prompt — so we pick deliberately.
            cursor_char = self._prompt_len + _wcswidth(self._buf[:self._cursor])
            new_cursor_row = _pending_wrap_row(cursor_char, self._cols)
            if old_cols == self._cols:
                cursor_row_offset = old_cursor_row
            else:
                cursor_row_offset = new_cursor_row
            top = max(1, caret_row - cursor_row_offset)
            top = min(top, self._lines)
            _resize_debug(
                f"  → top={top} offset={cursor_row_offset} "
                f"(no_reflow={caret_row - old_cursor_row}, "
                f"reflow={caret_row - new_cursor_row}, caret={caret_row})"
            )
            sys.stdout.write(f"\033[{top};1H\033[J")
            self._cursor_row = 0
            self._redraw()
        else:
            # DSR unsupported: fall back to the legacy reflow / relative
            # clear paths. Status-bar suppression still applies, so a
            # stranded bar is at least one-shot rather than refreshed each
            # SIGWINCH.
            _resize_debug("  → DSR unsupported, using legacy fallback")
            cursor_char = self._prompt_len + _wcswidth(self._buf[:self._cursor])
            if self._terminal_reflows:
                self._cursor_row = _pending_wrap_row(cursor_char, self._cols)
            else:
                if old_cursor_row > 0:
                    sys.stdout.write(f"\033[{old_cursor_row}A")
                sys.stdout.write("\r\033[J")
                self._cursor_row = 0
                self._redraw()

    # ── rendering ────────────────────────────────────────────────────────────

    def _redraw(self) -> None:
        """Rewrite the prompt and buffer, handling multi-line wrapping."""
        cols = self._cols
        cursor_char = self._prompt_len + _wcswidth(self._buf[:self._cursor])
        total_char = self._prompt_len + _wcswidth(self._buf)

        # Synchronized output (DECSET 2026): the terminal buffers everything
        # written between BSU (\x1b[?2026h) and ESU (\x1b[?2026l) and presents
        # it as a single atomic frame. iTerm2 supports it; terminals that
        # don't simply ignore the private modes. Without this, iTerm2 paints
        # the intermediate state of \r\033[J — a blank line — before the
        # rewritten prompt+buffer arrives, which reads as flicker.
        # DECTCEM cursor-hide on top suppresses caret flashes at col 0.
        sys.stdout.write("\x1b[?2026h\x1b[?25l")

        # Go up to the render top, then clear to end of screen.
        if self._cursor_row > 0:
            sys.stdout.write(f"\033[{self._cursor_row}A")
        sys.stdout.write("\r\033[J")
        sys.stdout.write(self._prompt_str + self._buf)

        # Decide whether the status bar needs to render. ``_suppress_statusbar``
        # is set by ``_on_resize`` so a drag-resize doesn't repaint the bar
        # on every SIGWINCH; the flag is cleared on the next keypress.
        from .tui import _statusbar
        statusbar_str: str | None = None
        if self._suppress_statusbar:
            pass
        elif self._hint:
            statusbar_str = _statusbar(self._hint, "", self._cols)
        elif self._get_arg_info is not None:
            info = self._get_arg_info(self._buf, self._cursor)
            if info:
                statusbar_str = _statusbar(info, "", self._cols)

        # When the status bar will be drawn, ensure the bottom row is free. If
        # end-of-content sits on the terminal's last row, \n triggers a scroll
        # (raw mode keeps the column); the matching \033[A returns to the same
        # visual position. When not at the bottom, the pair is a visual no-op.
        if statusbar_str is not None:
            sys.stdout.write("\n\033[A")

        # Compute cursor position within the render for next resize.
        self._cursor_row = _pending_wrap_row(cursor_char, cols)

        # After writing the buffer the cursor is at the end of content.
        end_row = _pending_wrap_row(total_char, cols)

        # Navigate from end of content back to where the cursor belongs.
        rows_up = end_row - self._cursor_row
        if rows_up > 0:
            sys.stdout.write(f"\033[{rows_up}A")

        # Use CHA (absolute column) so the caret jumps to its final position in
        # one atomic move — `\r` followed by `\033[{N}C` flickers through col 0.
        cursor_col = _pending_wrap_col(cursor_char, cols)
        sys.stdout.write(f"\033[{cursor_col + 1}G")

        if statusbar_str is not None:
            sys.stdout.write("\0337")
            sys.stdout.write(f"\033[{self._lines};1H")
            sys.stdout.write(statusbar_str)
            sys.stdout.write("\0338")
            self._status_bar_visible = True
        elif self._status_bar_visible:
            sys.stdout.write("\0337")
            sys.stdout.write(f"\033[{self._lines};1H\033[2K")
            sys.stdout.write("\0338")
            self._status_bar_visible = False

        sys.stdout.write("\x1b[?25h\x1b[?2026l")
        sys.stdout.flush()

    def _clear_status_bar(self) -> None:
        """Wipe the status bar from the bottom row before yielding the terminal.

        Called when prompt() is about to return so that command output (or the
        next prompt) doesn't have to fight with leftover status-bar pixels.
        """
        if not self._status_bar_visible:
            return
        sys.stdout.write("\0337")
        sys.stdout.write(f"\033[{self._lines};1H\033[2K")
        sys.stdout.write("\0338")
        sys.stdout.flush()
        self._status_bar_visible = False

    # ── input ────────────────────────────────────────────────────────────────

    def _handle_key(self, key: bytes, fd: int) -> str | None:
        """Return a result string to finish, or None to keep editing."""
        self._hint = ""  # any keypress dismisses the hint; TAB may re-set it
        self._suppress_statusbar = False  # bring the bar back on user activity

        # Enter
        if key in (b"\r", b"\n"):
            return self._buf

        # Ctrl+D — EOF if buffer empty
        if key == b"\x04":
            if not self._buf:
                raise EOFError
            return None

        # Ctrl+C
        if key == b"\x03":
            self._buf = ""
            self._cursor = 0
            raise KeyboardInterrupt

        # Ctrl+] — context switch (inert without a switch_fn, e.g. in tests)
        if key == b"\x1d":
            if self._switch_fn is None:
                return None
            needs_forward = self._do_inline_switch()
            return CONTEXT_CHANGED_SENTINEL if needs_forward else None

        # TAB — completion
        if key == b"\x09":
            self._complete()
            return None

        # Backspace
        if key in (b"\x7f", b"\x08"):
            if self._cursor > 0:
                self._buf = self._buf[: self._cursor - 1] + self._buf[self._cursor :]
                self._cursor -= 1
            return None

        # Ctrl+W — delete word before cursor
        if key == b"\x17":
            i = self._cursor
            while i > 0 and self._buf[i - 1] == " ":
                i -= 1
            while i > 0 and self._buf[i - 1] != " ":
                i -= 1
            self._buf = self._buf[:i] + self._buf[self._cursor :]
            self._cursor = i
            return None

        # Ctrl+K — delete to end of line
        if key == b"\x0b":
            self._buf = self._buf[: self._cursor]
            return None

        # Ctrl+U — delete to beginning
        if key == b"\x15":
            self._buf = self._buf[self._cursor :]
            self._cursor = 0
            return None

        # Ctrl+A / Home (CSI and SS3 forms — Terminal.app sends SS3 in app cursor mode)
        if key in (b"\x01", b"\x1b[H", b"\x1b[1~", b"\x1bOH"):
            self._cursor = 0
            return None

        # Ctrl+E / End
        if key in (b"\x05", b"\x1b[F", b"\x1b[4~", b"\x1bOF"):
            self._cursor = len(self._buf)
            return None

        # Ctrl+L — clear screen
        if key == b"\x0c":
            sys.stdout.write("\033[2J\033[H")
            return None

        # Ctrl+B / Left arrow
        if key in (b"\x02", b"\x1b[D", b"\x1bOD"):
            if self._cursor > 0:
                self._cursor -= 1
            return None

        # Ctrl+F / Right arrow
        if key in (b"\x06", b"\x1b[C", b"\x1bOC"):
            if self._cursor < len(self._buf):
                self._cursor += 1
            return None

        # Alt+B — move word left
        if key == b"\x1bb":
            i = self._cursor
            while i > 0 and self._buf[i - 1] == " ":
                i -= 1
            while i > 0 and self._buf[i - 1] != " ":
                i -= 1
            self._cursor = i
            return None

        # Alt+F — move word right
        if key == b"\x1bf":
            i = self._cursor
            n = len(self._buf)
            while i < n and self._buf[i] == " ":
                i += 1
            while i < n and self._buf[i] != " ":
                i += 1
            self._cursor = i
            return None

        # Ctrl+R — history search
        if key == b"\x12":
            self._history_search()
            return None

        # Up arrow — history back (CSI and SS3 forms; SS3 is what Terminal.app
        # sends when the keypad/cursor is in application mode — DECCKM)
        if key in (b"\x1b[A", b"\x1bOA", b"\x10"):
            self._hist_back()
            return None

        # Down arrow — history forward
        if key in (b"\x1b[B", b"\x1bOB", b"\x0e"):
            self._hist_fwd()
            return None

        # Printable ASCII
        if len(key) == 1 and 0x20 <= key[0] < 0x7F:
            ch = key.decode()
            self._buf = self._buf[: self._cursor] + ch + self._buf[self._cursor :]
            self._cursor += 1
            return None

        # UTF-8 multi-byte char (already fully assembled by terminal.read_key)
        if key[:1] >= b"\x80":
            ch = key.decode("utf-8", errors="replace")
            if ch.isprintable():
                self._buf = self._buf[: self._cursor] + ch + self._buf[self._cursor :]
                self._cursor += 1
            return None

        return None

    # ── history ──────────────────────────────────────────────────────────────

    def _local_entries(self) -> list[str]:
        """Per-context Up/Down history (falls back to the global store)."""
        if self._local_history_fn is not None:
            return self._local_history_fn()
        return self._history.entries

    def _hist_back(self) -> None:
        entries = self._local_entries()
        if not entries:
            return
        if self._hist_idx == 0:
            self._saved_buf = self._buf
        if self._hist_idx < len(entries):
            self._hist_idx += 1
            self._buf = entries[-self._hist_idx]
            self._cursor = len(self._buf)

    def _hist_fwd(self) -> None:
        if self._hist_idx == 0:
            return
        self._hist_idx -= 1
        if self._hist_idx == 0:
            self._buf = self._saved_buf
        else:
            self._buf = self._local_entries()[-self._hist_idx]
        self._cursor = len(self._buf)

    # ── context switch ───────────────────────────────────────────────────────

    def _do_inline_switch(self) -> bool:
        """Run the context-switch picker inline, preserving the current buffer.

        Returns True if the new context has a running process (caller should
        exit prompt so the run loop can enter forwarding mode).
        """
        caret_char = self._prompt_len + _wcswidth(self._buf[:self._cursor])
        caret_row = _pending_wrap_row(caret_char, self._cols)
        end_row = _pending_wrap_row(self._prompt_len + _wcswidth(self._buf), self._cols)
        rows_above = end_row - caret_row + 1

        cols_from_end = _wcswidth(self._buf[self._cursor:])
        if cols_from_end > 0:
            sys.stdout.write(f"\033[{cols_from_end}C")
        sys.stdout.write("\n")
        sys.stdout.flush()

        assert self._switch_fn is not None
        with self._picker_session():
            needs_forward, new_context_name = self._switch_fn()

        # Picker cleanup left cursor at the anchor (col 0 of the blank line).
        if new_context_name is not None:
            # The previous context's prompt + any output above it should stay
            # on screen.  ``_switch_fn`` already drew a dim separator labelled
            # with the new context name; we just need to let the next _redraw()
            # paint the new prompt fresh on the row below.  Resetting
            # _cursor_row to 0 prevents _redraw() from walking back up over
            # the old prompt.
            self._cursor_row = 0
        else:
            # Move back up to the caret row so _redraw() can take over from
            # there.  Skip the flush so the up-move batches with the redraw
            # the caller performs next — otherwise the terminal briefly
            # renders the caret at col 0 of the prompt row.
            sys.stdout.write(f"\033[{rows_above}A")

        # Prompt text may have changed after a context switch.
        self._prompt_str = self._get_prompt()
        self._prompt_len = _visible_len(self._prompt_str)

        return bool(needs_forward)

    # ── completion ───────────────────────────────────────────────────────────

    def _complete(self) -> None:
        from .tui import InlinePicker, _common_prefix

        buf_changed = False
        # True when the current loop iteration is a re-entry triggered by the
        # picker reopening (TAB-extend or display-col shift while the user was
        # narrowing). On a re-entry, narrowing to a single completion must NOT
        # auto-apply — the user would have no way to know the candidate count
        # crossed the threshold. Keep the picker open on the lone item so they
        # press Enter (apply) or TAB (extend common prefix) explicitly.
        from_reopen = False
        while True:
            # Redraw if a previous iteration modified the buffer (e.g. auto-applied a
            # flag), so the prompt reflects the new content before the next picker opens.
            if buf_changed:
                self._redraw()
                buf_changed = False

            completions, prefix, status_label = self._get_completions(self._buf[: self._cursor])

            if not completions:
                return

            # Single-token candidates only.  Verbatim (history) candidates are
            # excluded from every "there is exactly one candidate" shortcut
            # below: auto-applying a token is a small, predictable edit, while
            # silently appending several arguments is not — so a history entry is
            # only ever inserted from a picker the user can see.  This also
            # keeps the pre-history behaviour of the shortcuts intact: a unique
            # token completion still applies on the first TAB even when past
            # command lines also match.
            token_completions = [c for c in completions if not c.verbatim]

            # Arg-hint: the preceding flag needs a typed value (e.g. "-d N").
            # Show an informational hint below the buffer without opening a
            # picker or modifying the buffer — cleared by the next _redraw().
            if len(token_completions) == 1 and token_completions[0].is_arg_hint:
                hint = token_completions[0]
                if hint.description:
                    self._hint = f"{hint.value} <{hint.arg_hint}>: {hint.description}"
                else:
                    self._hint = f"{hint.value} <{hint.arg_hint}>"
                return

            # Single value-taking option: auto-apply then re-run the loop.
            # The next iteration will either open a value picker (when a
            # value_completer is registered) or set self._hint (is_arg_hint).
            if (len(token_completions) == 1
                    and token_completions[0].multi_select
                    and token_completions[0].arg_hint
                    and not from_reopen):
                self._apply(token_completions[0])
                buf_changed = True
                continue

            if len(token_completions) == 1 and not from_reopen:
                self._apply(token_completions[0])
                return

            # Multi-select options picker.
            if all(c.multi_select for c in completions):
                self._complete_multi(completions, prefix, status_label)
                return

            # Move to the end of the visible content, then go one line down.
            # The prompt line stays visible above the picker during interaction.
            caret_char = self._prompt_len + _wcswidth(self._buf[:self._cursor])
            caret_col = _pending_wrap_col(caret_char, self._cols)
            caret_row = _pending_wrap_row(caret_char, self._cols)
            end_row = _pending_wrap_row(self._prompt_len + _wcswidth(self._buf), self._cols)
            rows_above = end_row - caret_row + 1
            display_offset = _picker_col_offset(prefix, completions)
            col = caret_col - display_offset
            rows = _align_verbatim_rows(self._buf[: self._cursor], completions, display_offset)

            cols_from_end = _wcswidth(self._buf[self._cursor:])
            if cols_from_end > 0:
                sys.stdout.write(f"\033[{cols_from_end}C")
            sys.stdout.write("\n")
            sys.stdout.flush()

            buf_at_tab = self._buf[: self._cursor]
            caret_char_at_tab = caret_char

            # The token prefix of the line as it stands, kept current by
            # ``refresh`` so ``extend`` measures against the live anchor.
            live = {"prefix": prefix}

            def refresh(typed: str) -> tuple[list[Completion], int]:
                line = buf_at_tab + typed
                new_completions, new_prefix, _ = self._get_completions(line)
                live["prefix"] = new_prefix
                new_caret_col = _pending_wrap_col(
                    caret_char_at_tab + len(typed), self._cols  # typed is always ASCII
                )
                new_offset = _picker_col_offset(new_prefix, new_completions)
                new_rows = _align_verbatim_rows(line, new_completions, new_offset)
                return new_rows, new_caret_col - new_offset

            def extend(items: list[Completion], typed: str) -> str:
                """Common prefix of *items*, minus the text already typed.

                TAB-extend measures candidate *values*, so every measured value
                must live in the same space as the text it is measured against.
                Both kinds of candidate start at the completion anchor, but a
                verbatim (history) value starts at the *raw* anchor while a
                token value starts where the shlex-unquoted ``prefix`` does, so
                the two spaces differ whenever the token is quoted.
                Verbatim-only lists therefore measure against the raw token
                text; mixed lists measure the token candidates and drop the
                verbatim rows, which keeps TAB-extend behaving exactly as it did
                before history candidates existed.

                Both the value space and the already-typed length are decided
                per TAB press rather than when the picker opened, because
                narrowing moves both: a mixed list can narrow down to history
                rows only, and typing a space moves the anchor (a history
                candidate keeps matching across token boundaries, so the picker
                stays open).  Measuring a fresh common prefix against a stale
                anchor always over-counts, which silently made TAB inert.
                """
                if all(c.verbatim for c in items):
                    values = [c.value for c in items]
                    line = buf_at_tab + typed
                    typed_len = len(line) - raw_token_start(line)
                else:
                    values = [c.value for c in items if not c.verbatim]
                    typed_len = len(live["prefix"])
                if not values:
                    return ""
                return _common_prefix(values)[typed_len:]

            picker = InlinePicker(
                rows,
                display_fn=lambda c: c.display or c.value,
                meta_fn=lambda c: c.meta,
                max_height=10,
                col=col,
                initial_offset=display_offset,
                rows_above=rows_above,
                refresh_fn=refresh,
                extend_fn=extend,
                reopen_when=lambda items: bool(items) and all(c.multi_select for c in items),
                status_label=status_label,
                # Open with nothing highlighted: Enter must not insert a
                # candidate the user never picked. Only Down/Up select.
                select_first=False,
            )
            with self._picker_session():
                selected = picker.run()

            # Picker cleanup leaves cursor at the anchor (col `picker._col` of
            # the first blank row) WITHOUT flushing. Move up to the caret row
            # and skip the flush so these bytes batch with the next render
            # (either the reopened picker's _reserve or _redraw on return) —
            # otherwise the terminal briefly shows the caret at the start of
            # the token being completed.
            sys.stdout.write(f"\033[{rows_above}A")

            # Characters typed while the picker was open were echoed to the
            # screen by the picker but live only in ``picker.typed`` — commit
            # them to the buffer on *every* exit path. Skipping this on the
            # dismiss/cancel paths made the redraw on return silently erase
            # what the user had typed since pressing TAB.
            if picker.typed:
                self._buf = self._buf[: self._cursor] + picker.typed + self._buf[self._cursor :]
                self._cursor += len(picker.typed)

            if picker.reopen:
                # TAB-complete typed chars: reopen the picker at the new position.
                from_reopen = True
                continue

            if picker.apply_backspace:
                # Backspace with no picker-typed chars: delete one buffer char and close.
                if self._cursor > 0:
                    self._buf = self._buf[: self._cursor - 1] + self._buf[self._cursor :]
                    self._cursor -= 1
                return

            # ``selected is None`` covers Esc, Enter with nothing highlighted,
            # and ``picker.closed_empty`` (typing narrowed the list to zero):
            # in all three the typed chars stay in the buffer and the user is
            # handed back a plain prompt.
            if selected is not None:
                self._apply(selected)
            return

    def _complete_multi(self, completions: list[Completion], prefix: str, status_label: str = "") -> None:
        """Run the multi-select options picker."""
        from .tui import InlineMultiPicker

        caret_char = self._prompt_len + _wcswidth(self._buf[:self._cursor])
        caret_col = _pending_wrap_col(caret_char, self._cols)
        caret_row = _pending_wrap_row(caret_char, self._cols)
        end_row = _pending_wrap_row(self._prompt_len + _wcswidth(self._buf), self._cols)
        rows_above = end_row - caret_row + 1

        cols_from_end = _wcswidth(self._buf[self._cursor:])
        if cols_from_end > 0:
            sys.stdout.write(f"\033[{cols_from_end}C")
        sys.stdout.write("\n")
        sys.stdout.flush()

        picker = InlineMultiPicker(
            completions,
            display_fn=lambda c: f"{c.display or c.value} <{c.arg_hint}>" if c.arg_hint else (c.display or c.value),
            meta_fn=lambda c: c.meta,
            max_height=12,
            rows_above=rows_above,
            caret_col=caret_col,
            status_label=status_label,
            # Nothing highlighted on open — Enter must not insert the first
            # flag in the list. Space checks items; Down/Up highlight one.
            select_first=False,
        )
        with self._picker_session():
            selected = picker.run()

        # No flush — let these bytes batch with the next _redraw so the caret
        # doesn't briefly land at the picker's anchor column on the prompt row.
        sys.stdout.write(f"\033[{rows_above}A")

        if not selected:
            return

        bool_sel = [c for c in selected if not c.arg_hint]
        arg_sel = [c for c in selected if c.arg_hint]

        # Replace the prefix and insert combined boolean flags.
        pre = self._buf[: self._cursor - len(prefix)]
        post = self._buf[self._cursor :]
        short = [c for c in bool_sel if c.combinable]
        long_bool = [c for c in bool_sel if not c.combinable]
        parts: list[str] = []
        if short:
            parts.append("-" + "".join(c.value[1:] for c in short))
        parts.extend(c.value for c in long_bool)
        bool_str = " ".join(parts)
        self._buf = pre + bool_str + post
        self._cursor = len(pre) + len(bool_str)

        # For each arg-taking flag, insert it then handle its value:
        #   • flags with a value completer (e.g. -C DIR) → picker via _prompt_for_arg
        #   • hint-only flags (e.g. -j N)               → hint line, return to user
        for opt in arg_sel:
            sep = " " if self._cursor > 0 and self._buf[self._cursor - 1] != " " else ""
            ins = f"{sep}{opt.value} "
            self._buf = self._buf[: self._cursor] + ins + self._buf[self._cursor :]
            self._cursor += len(ins)

            value_comps, _, _ = self._get_completions(self._buf[: self._cursor])
            # ``verbatim`` (history) candidates are not values for this flag —
            # they'd make every flag look like it had a value completer.
            has_value_picker = any(
                not c.multi_select and not c.is_arg_hint and not c.verbatim
                for c in value_comps
            )

            if has_value_picker:
                # Value completer available: open picker, then continue to next flag.
                if not self._prompt_for_arg(opt):
                    break
            else:
                # Hint-only: show hint below and hand control back to the user.
                # They type the value directly; any remaining flags wait for next TAB.
                hint_comp = next((c for c in value_comps if c.is_arg_hint), None)
                if hint_comp:
                    if hint_comp.description:
                        self._hint = f"{hint_comp.value} <{hint_comp.arg_hint}>: {hint_comp.description}"
                    else:
                        self._hint = f"{hint_comp.value} <{hint_comp.arg_hint}>"
                break

    def _history_search(self) -> None:
        from .tui import InlinePicker

        entries = self._history.entries
        if not entries:
            return

        # Deduplicate, most recent first
        seen: set[str] = set()
        unique: list[str] = []
        for e in reversed(entries):
            if e not in seen:
                seen.add(e)
                unique.append(e)

        saved_buf = self._buf
        saved_cursor = self._cursor

        self._buf = ""
        self._cursor = 0
        self._redraw()

        # Move below the (now empty) prompt line
        sys.stdout.write("\n")
        sys.stdout.flush()

        caret_col = _pending_wrap_col(self._prompt_len, self._cols)

        def refresh(typed: str) -> tuple[list[str], int]:
            if not typed:
                return unique, caret_col
            keywords = typed.lower().split()
            filtered = [e for e in unique if all(k in e.lower() for k in keywords)]
            return filtered, caret_col

        picker = InlinePicker(
            unique,
            display_fn=str,
            max_height=10,
            col=caret_col,
            initial_offset=0,
            rows_above=1,
            refresh_fn=refresh,
            value_fn=None,  # disable tab-complete inside the search picker
            status_label="history search",
            # A keyword that matches nothing must not tear the search down —
            # the next keystroke is usually a Backspace fixing a typo, and
            # closing here would throw the whole query away.
            empty_placeholder="(no matches)",
        )
        with self._picker_session():
            selected = picker.run()

        # No flush — let the up-move batch with _redraw on return.
        sys.stdout.write("\033[1A")

        if selected is not None:
            self._buf = selected
            self._cursor = len(self._buf)
            self._hist_idx = 0
        else:
            self._buf = saved_buf
            self._cursor = saved_cursor

    def _raw_token_start(self) -> int:
        """Return the index in self._buf where the current raw token starts.

        Shares :func:`parsing.raw_token_start` with the completers, so a
        candidate is measured from exactly the position it is inserted at.
        """
        return raw_token_start(self._buf[: self._cursor])

    def _apply(self, completion: Completion) -> None:
        # Verbatim candidate (history): the value may span several tokens and is
        # already shell syntax, so it replaces the raw token as-is — no shell
        # quoting and no trailing space.  It starts at the same anchor an
        # ordinary token candidate would, so what the user saw in the picker is
        # what lands under the caret.  Text after the caret is kept: applying a
        # suggestion must not silently discard part of the buffer.
        if completion.verbatim:
            raw_start = self._raw_token_start()
            post = self._buf[self._cursor :]
            self._buf = self._buf[:raw_start] + completion.value + post
            self._cursor = raw_start + len(completion.value)
            return
        # Find where the raw token starts in the buffer.  We cannot use
        # len(prefix) here because shlex.split returns the *unquoted* length,
        # which differs from the raw length when the token is surrounded by
        # quotes (e.g. `'My Documents/'` is 16 raw chars but 14 unquoted).
        raw_start = self._raw_token_start()
        pre = self._buf[:raw_start]
        post = self._buf[self._cursor :]
        # Shell-quote the value if it contains whitespace or other characters
        # that shlex would split on (e.g. spaces in S3 keys or local filenames).
        # _shell_quote only adds quotes when necessary and treats ~ as safe so
        # that home-dir paths like ~/Desktop/ are not needlessly quoted.
        value = _shell_quote(completion.value)
        # Append a trailing space so the next argument can be typed immediately.
        # Skip when: (a) the value ends with "/" — a directory, where the user
        # may continue typing the path; (b) the value ends with "=" — a KEY=
        # completion where the user will continue typing the value; (c) post
        # already starts with whitespace.
        # arg_hint flags always get a space — _prompt_for_arg uses it as a separator.
        if completion.arg_hint:
            value = value + " "
        elif not completion.value.endswith(("/", "=")) and not post[:1].isspace():
            value = value + " "
        self._buf = pre + value + post
        self._cursor = len(pre) + len(value)

    def _prompt_for_arg(self, opt: Completion) -> bool:
        """Pick a value for opt's argument from its completer; False if cancelled.

        Called only for a flag whose value has a registered completer — a flag
        without one gets the arg hint instead (see :meth:`_complete_multi`).
        """
        from .tui import InlinePicker

        self._redraw()

        # Ask the completion engine what's available for this argument position.
        # Filter out multi_select entries (flag pickers), is_arg_hint entries
        # (hint-only flags with no value completer), and verbatim entries
        # (history suggestions, which are not values for this flag) —
        # only real value completions remain.
        raw_completions, prefix, _ = self._get_completions(self._buf[: self._cursor])
        completions = [
            c for c in raw_completions
            if not c.multi_select and not c.is_arg_hint and not c.verbatim
        ]

        end_char = self._prompt_len + _wcswidth(self._buf)
        end_row = _pending_wrap_row(end_char, self._cols)
        end_col = _pending_wrap_col(end_char, self._cols)
        caret_row = self._cursor_row  # updated by _redraw()

        if opt.arg_hint and opt.description:
            _flag_label = f"{opt.value} <{opt.arg_hint}>: {opt.description}"
        elif opt.description:
            _flag_label = f"{opt.value}: {opt.description}"
        elif opt.arg_hint:
            _flag_label = f"{opt.value} <{opt.arg_hint}>"
        else:
            _flag_label = opt.value

        if not completions:
            # Only reached from _complete_multi, which checks for value
            # candidates first; nothing to pick means nothing to insert.
            return False

        # ── picker path: completions are available for the flag value ──────────
        # Loop mirrors _complete()'s while-True structure to handle tab-extend
        # (picker.reopen) and backspace (picker.apply_backspace).
        while True:
            caret_char = self._prompt_len + _wcswidth(self._buf[:self._cursor])
            caret_col = _pending_wrap_col(caret_char, self._cols)
            caret_row = _pending_wrap_row(caret_char, self._cols)
            end_char = self._prompt_len + _wcswidth(self._buf)
            end_row = _pending_wrap_row(end_char, self._cols)
            rows_above = end_row - caret_row + 1
            display_offset = _display_col_offset(prefix, completions)
            col = caret_col - display_offset

            # Move cursor to the end of the buffer content, then one line below.
            cols_from_end = _wcswidth(self._buf[self._cursor:])
            if cols_from_end > 0:
                sys.stdout.write(f"\033[{cols_from_end}C")
            sys.stdout.write("\n")
            sys.stdout.flush()

            buf_at_open = self._buf[: self._cursor]
            caret_char_at_open = caret_char

            def refresh(typed: str) -> tuple[list[Completion], int]:
                new_raw, new_prefix, _ = self._get_completions(buf_at_open + typed)
                new_completions = [
                    c for c in new_raw if not c.multi_select and not c.verbatim
                ]
                new_caret_col = _pending_wrap_col(
                    caret_char_at_open + len(typed), self._cols  # typed is always ASCII
                )
                new_col = new_caret_col - _display_col_offset(new_prefix, new_completions)
                return new_completions, new_col

            picker = InlinePicker(
                completions,
                display_fn=lambda c: c.display or c.value,
                meta_fn=lambda c: c.meta,
                max_height=10,
                col=col,
                initial_offset=display_offset,
                rows_above=rows_above,
                refresh_fn=refresh,
                value_fn=lambda c: c.value,
                completion_prefix=prefix,
                status_label=_flag_label,
                select_first=False,   # Enter must not pick an unselected value
            )
            with self._picker_session():
                selected = picker.run()

            # Picker cleanup left cursor at col `picker._col` of (end_row + 1).
            # Go back up to the caret row, but don't flush — let these bytes
            # batch with the next render so we don't briefly show the caret
            # at the picker's anchor column on the prompt row.
            sys.stdout.write(f"\033[{rows_above}A")

            # Commit what the user typed inside the picker on every exit path —
            # the picker echoed those chars but never wrote them to the buffer.
            if picker.typed:
                self._buf = self._buf[: self._cursor] + picker.typed + self._buf[self._cursor :]
                self._cursor += len(picker.typed)

            if picker.reopen:
                # TAB was pressed inside the picker (or the display column
                # shifted while narrowing): reopen with a refreshed completion
                # list. Even if narrowing leaves only one completion, do NOT
                # auto-apply — the user can't see the count cross the threshold
                # mid-typing, so a sudden close + insert would be surprising.
                # Keep the picker open on the lone item; the user presses Enter
                # to apply or TAB to extend the common prefix explicitly.
                completions, prefix, _ = self._get_completions(self._buf[: self._cursor])
                completions = [
                    c for c in completions if not c.multi_select and not c.verbatim
                ]
                if not completions:
                    return True  # typed chars committed; no further completions
                continue

            if picker.apply_backspace:
                # Backspace with nothing typed: remove the trailing space the flag
                # inserted and let the user continue editing freely.
                if self._cursor > 0:
                    self._buf = self._buf[: self._cursor - 1] + self._buf[self._cursor :]
                    self._cursor -= 1
                return True

            # Esc, Enter with nothing highlighted, or the list narrowing to zero
            # (``picker.closed_empty``): leave the typed value in the buffer and
            # report "no value chosen" so the caller stops filling in flags.
            if selected is None:
                return False

            self._apply(selected)
            return True
