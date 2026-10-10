"""DIY line editor with history and TAB completion — no prompt_toolkit."""

from __future__ import annotations

import contextlib
import os
import re
import sys
import time
import unicodedata
from typing import Callable

from . import keys, shell_integration, terminal
from .completion import Completion
from .history import HistoryEntry, HistoryStore
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


def _history_meta(entry: HistoryEntry, now: float) -> tuple[str, ...]:
    """Ctrl+R's columns for *entry*: where it last ran, how long ago, and
    its exit status when that was a failure."""
    home = os.path.normcase(os.path.expanduser("~"))
    where = entry.cwd or ""
    if where == home or where.startswith(home + os.sep):
        where = "~" + where[len(home):]
    age = max(0, now - entry.ts)
    if age < 3600:
        when = f"{int(age // 60)}m"
    elif age < 86400:
        when = f"{int(age // 3600)}h"
    else:
        when = f"{int(age // 86400)}d"
    failed = f"exit {entry.status}" if entry.status else ""
    return (where.replace(os.sep, "/"), when, failed)


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
        history: HistoryStore,
        get_completions: GetCompletionsFn,
        get_prompt: Callable[[], str],
        switch_fn: Callable[[], tuple[bool, str | None]] | None = None,
        get_arg_info: GetArgInfoFn | None = None,
        local_history_fn: Callable[[], list[str]] | None = None,
        suggest_fn: Callable[[str], str | None] | None = None,
    ):
        # The shared store: what Ctrl+R searches.
        self._history = history
        # Returns the current context's per-session Up/Down history list
        # (the actual mutable list); only Up/Down navigation is scoped per
        # context.
        self._local_history_fn = local_history_fn
        # Ghost suggestion: given the buffer, a past line that extends it.
        self._suggest_fn = suggest_fn
        self._ghost: str | None = None     # the full suggested line, when one is shown
        self._ghost_enabled = True         # off for continuation prompts and on submit
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
        self._prompt_marks = ("", "")  # shell_integration's, around the prompt
        self._cursor_row = 0  # rows below render-top where cursor sits
        # VSCode integrated terminal does not reflow content on resize;
        # cursor stays at the same row (clamped column). Detect it so we
        # re-render explicitly instead of relying on terminal reflow.
        self._terminal_reflows = os.environ.get("TERM_PROGRAM", "") != "vscode"
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
        # User actions running right now (the ctx.invoke re-entry guard), and
        # a line an action asked to finish with (ctx.invoke("accept")).
        self._invoking: set[str] = set()
        self._pending_result: str | None = None

    def add_to_history(self, line: str) -> None:
        """Append *line* to the current context's Up/Down list (the caller
        records it in the shared store, with where and how it ran)."""
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
        # A continuation line ("> ") is the middle of a command, which no
        # history entry starts with.
        self._ghost_enabled = prompt_str is None
        self._ghost = None
        self._prompt_marks = shell_integration.prompt_marks(
            continuation=prompt_str is not None)

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
                    self._ghost_enabled = False   # the line as run, no suggestion
                    self._redraw()
                    self._clear_status_bar()
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    return result
                self._redraw()
        except (EOFError, KeyboardInterrupt):
            self._erase_ghost()
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
        mark_start, mark_end = self._prompt_marks
        sys.stdout.write(mark_start + self._prompt_str + mark_end + self._buf)
        ghost = self._ghost_suffix(total_char)
        if ghost:
            sys.stdout.write(f"\033[2m{ghost}\033[22m")

        # Decide whether the status bar needs to render. ``_suppress_statusbar``
        # is set by ``_on_resize`` so a drag-resize doesn't repaint the bar
        # on every SIGWINCH; the flag is cleared on the next keypress.
        from .tui import _statusbar
        statusbar_str: str | None = None
        if self._suppress_statusbar:
            pass
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

    def _handle_key(self, key: bytes, fd: int = 0) -> str | None:
        """Return a result string to finish, or None to keep editing."""
        self._suppress_statusbar = False  # bring the bar back on user activity

        name = keys.lookup("prompt", key)
        if name is not None:
            return self.run_action(name)

        # Printable ASCII, or a UTF-8 character (already fully assembled by
        # terminal.read_key).  Printable keys are never bindable.
        if (len(key) == 1 and 0x20 <= key[0] < 0x7F) or key[:1] >= b"\x80":
            ch = key.decode("utf-8", errors="replace")
            if ch.isprintable():
                self._insert(ch)
        return None

    def run_action(self, name: str) -> str | None:
        """Run the action *name* (a user action first, then the built-in) and
        return what :meth:`_handle_key` should: a line to finish with, or None.

        A user action already running is skipped in favour of the built-in of
        the same name, so ``ctx.invoke("history_search")`` inside an action
        that overrides it reaches the original (XeFM's re-entry guard).
        """
        action = keys.get_action(name)
        if action is None:
            return None
        name = action.name
        if action.is_user and name not in self._invoking:
            self._invoking.add(name)
            self._pending_result = None
            try:
                action.func(EditorContext(self))
            except (EOFError, KeyboardInterrupt):
                raise
            except Exception as exc:
                self._report_action_error(action.short_name, exc)
            finally:
                self._invoking.discard(name)
            result, self._pending_result = self._pending_result, None
            return result
        handler = self._builtin_actions().get(name)
        return handler() if handler is not None else None

    def _report_action_error(self, name: str, exc: BaseException) -> None:
        """A raising user action prints its traceback below the line; the
        prompt is drawn afresh under it and editing goes on."""
        from .user_errors import format_user_exception
        self._erase_ghost()
        text = f"key action {name!r} failed:\n" + format_user_exception(exc)
        sys.stdout.write("\r\n" + text.rstrip("\n").replace("\n", "\r\n") + "\r\n")
        sys.stdout.flush()
        self._cursor_row = 0

    def _builtin_actions(self) -> dict[str, Callable[[], str | None]]:
        return {
            "prompt.accept": lambda: self._buf,
            "prompt.complete": self._act_complete,
            "prompt.history_search": self._act_history_search,
            "prompt.previous_history": self._act(self._hist_back),
            "prompt.next_history": self._act(self._hist_fwd),
            "prompt.switch_context": self._act_switch_context,
            "prompt.beginning_of_line": self._act(lambda: self._move_to(0)),
            "prompt.end_of_line": self._act(self._end_of_line),
            "prompt.backward_char": self._act(lambda: self._move_to(self._cursor - 1)),
            "prompt.forward_char": self._act(self._forward_char),
            "prompt.backward_word": self._act(lambda: self._move_to(self._word_start())),
            "prompt.forward_word": self._act(self._forward_word),
            "prompt.backward_delete_char": self._act(
                lambda: self._delete(self._cursor - 1, self._cursor)),
            "prompt.delete_char": self._act(
                lambda: self._delete(self._cursor, self._cursor + 1)),
            "prompt.backward_kill_word": self._act(
                lambda: self._delete(self._word_start(), self._cursor)),
            "prompt.kill_line": self._act(lambda: self._delete(self._cursor, len(self._buf))),
            "prompt.backward_kill_line": self._act(lambda: self._delete(0, self._cursor)),
            "prompt.clear_screen": self._act(lambda: sys.stdout.write("\033[2J\033[H")),
            "prompt.eof": self._act_eof,
            "prompt.interrupt": self._act_interrupt,
        }

    @staticmethod
    def _act(func: Callable[[], object]) -> Callable[[], None]:
        """An editing action: runs *func*, never finishes the line."""
        def run() -> None:
            func()
        return run

    # ── editing primitives (the built-in actions and EditorContext use these)

    def _insert(self, text: str) -> None:
        self._buf = self._buf[: self._cursor] + text + self._buf[self._cursor :]
        self._cursor += len(text)

    def _delete(self, start: int, end: int) -> None:
        start, end = max(0, start), min(len(self._buf), end)
        if start >= end:
            return
        self._buf = self._buf[:start] + self._buf[end:]
        if self._cursor > end:
            self._cursor -= end - start
        elif self._cursor > start:
            self._cursor = start

    def _move_to(self, pos: int) -> None:
        self._cursor = max(0, min(len(self._buf), pos))

    def _word_start(self) -> int:
        i = self._cursor
        while i > 0 and self._buf[i - 1] == " ":
            i -= 1
        while i > 0 and self._buf[i - 1] != " ":
            i -= 1
        return i

    def _end_of_line(self) -> None:
        # At the end of the line, accept the ghost suggestion.
        if not self._accept_ghost():
            self._cursor = len(self._buf)

    def _forward_char(self) -> None:
        # At the end of the line, accept the ghost suggestion.
        if self._cursor < len(self._buf):
            self._cursor += 1
        else:
            self._accept_ghost()

    def _forward_word(self) -> None:
        # At the end of the line, accept one word of the ghost suggestion.
        if self._cursor == len(self._buf) and self._accept_ghost(word=True):
            return
        i, n = self._cursor, len(self._buf)
        while i < n and self._buf[i] == " ":
            i += 1
        while i < n and self._buf[i] != " ":
            i += 1
        self._cursor = i

    def _act_complete(self) -> None:
        self._erase_ghost()
        self._complete()

    def _act_history_search(self) -> None:
        self._erase_ghost()
        self._history_search()

    def _act_switch_context(self) -> str | None:
        # Inert without a switch_fn (e.g. in tests).
        if self._switch_fn is None:
            return None
        self._erase_ghost()
        return CONTEXT_CHANGED_SENTINEL if self._do_inline_switch() else None

    def _act_eof(self) -> None:
        if not self._buf:
            raise EOFError

    def _act_interrupt(self) -> None:
        self._buf = ""
        self._cursor = 0
        raise KeyboardInterrupt

    def _choose(self, items: list[str], title: str = "") -> str | None:
        """:meth:`EditorContext.choose`: a picker below the line, filtered by
        keywords typed while it is open.  What is typed is a query, not text,
        so it never reaches the buffer."""
        from .tui import InlinePicker

        items = [str(i) for i in items]
        if not items:
            return None
        self._erase_ghost()
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

        def matching(typed: str) -> list[str]:
            words = typed.lower().split()
            return [i for i in items if all(w in i.lower() for w in words)]

        picker = InlinePicker(
            items,
            max_height=10,
            col=0,
            initial_offset=caret_col,
            rows_above=rows_above,
            refresh_fn=lambda typed: (matching(typed), 0),
            status_label=title,
            empty_placeholder="(no matches)",
        )
        with self._picker_session():
            choice = picker.run()
        sys.stdout.write(f"\033[{rows_above}A")
        return choice

    # ── history ──────────────────────────────────────────────────────────────

    def _local_entries(self) -> list[str]:
        """Per-context Up/Down history."""
        if self._local_history_fn is not None:
            return self._local_history_fn()
        return []

    # ── ghost suggestion ─────────────────────────────────────────────────────

    def _ghost_suffix(self, total_char: int) -> str:
        """Look up the suggestion for the buffer and return the part to draw
        after it (dimmed), or ``""``.  Sets :attr:`_ghost` to the full line.

        Shown only with the caret at the end of the line, and only as much of
        it as fits on the row the buffer ends on: a ghost never wraps, so
        the row bookkeeping in :meth:`_redraw` stays about the buffer alone.
        Accepting it inserts the whole line regardless.
        """
        self._ghost = None
        if (not self._ghost_enabled or self._suggest_fn is None
                or self._cursor != len(self._buf) or not self._buf.strip()):
            return ""
        line = self._suggest_fn(self._buf)
        if not line or not line.startswith(self._buf) or len(line) <= len(self._buf):
            return ""
        self._ghost = line
        used = total_char % self._cols
        room = self._cols - used - 1 if used or total_char == 0 else 0
        out, width = "", 0
        for ch in line[len(self._buf):]:
            w = _wcswidth(ch)
            if width + w > room:
                break
            out += ch
            width += w
        return out

    def _accept_ghost(self, word: bool = False) -> bool:
        """Insert the ghost suggestion (or its next word); False if none."""
        if self._ghost is None or self._cursor != len(self._buf):
            return False
        rest = self._ghost[len(self._buf):]
        if word:
            i = len(rest) - len(rest.lstrip(" "))
            while i < len(rest) and rest[i] != " ":
                i += 1
            rest = rest[:i]
        self._buf += rest
        self._cursor = len(self._buf)
        return True

    def _erase_ghost(self) -> None:
        """Wipe a ghost suggestion off the screen before something else is
        drawn below the line (a picker) or the line is left behind (Ctrl+C).
        The caret sits where the ghost starts, so erasing to end of line
        is enough."""
        if self._ghost is not None:
            sys.stdout.write("\033[K")
            sys.stdout.flush()
            self._ghost = None

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
        from .tui import InlinePicker

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

            if len(completions) == 1 and not from_reopen:
                self._apply(completions[0])
                if completions[0].arg_hint:
                    # A value-taking flag: go straight on to its value.
                    buf_changed = True
                    continue
                return

            # Move to the end of the visible content, then go one line down.
            # The prompt line stays visible above the picker during interaction.
            caret_char = self._prompt_len + _wcswidth(self._buf[:self._cursor])
            caret_col = _pending_wrap_col(caret_char, self._cols)
            caret_row = _pending_wrap_row(caret_char, self._cols)
            end_row = _pending_wrap_row(self._prompt_len + _wcswidth(self._buf), self._cols)
            rows_above = end_row - caret_row + 1
            display_offset = _display_col_offset(prefix, completions)
            col = caret_col - display_offset

            cols_from_end = _wcswidth(self._buf[self._cursor:])
            if cols_from_end > 0:
                sys.stdout.write(f"\033[{cols_from_end}C")
            sys.stdout.write("\n")
            sys.stdout.flush()

            buf_at_tab = self._buf[: self._cursor]
            caret_char_at_tab = caret_char

            def refresh(typed: str) -> tuple[list[Completion], int]:
                new_completions, new_prefix, _ = self._get_completions(buf_at_tab + typed)
                new_caret_col = _pending_wrap_col(
                    caret_char_at_tab + len(typed), self._cols  # typed is always ASCII
                )
                return new_completions, new_caret_col - _display_col_offset(
                    new_prefix, new_completions)

            picker = InlinePicker(
                completions,
                display_fn=lambda c: c.display or c.value,
                meta_fn=lambda c: c.meta,
                max_height=10,
                col=col,
                initial_offset=display_offset,
                rows_above=rows_above,
                refresh_fn=refresh,
                # TAB inside the picker types the candidates' shared prefix.
                value_fn=lambda c: c.value,
                completion_prefix=prefix,
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
            if selected is None:
                return
            self._apply(selected)
            if not selected.arg_hint:
                return
            # A value-taking flag (``-d <N>``): the next round offers the
            # flag's value completer, or returns nothing and leaves the
            # status bar to say what to type.
            buf_changed = True
            from_reopen = False

    def _history_search(self) -> None:
        """Ctrl+R: every distinct line from the shared history (all
        contexts, all directories, every eosh process), newest first,
        filtered by keywords.  What is already typed is the first filter."""
        from .tui import InlinePicker

        entries = self._history.distinct()
        if not entries:
            return

        saved_buf = self._buf
        saved_cursor = self._cursor

        # Start from the buffer when it fits on the prompt row (the picker
        # echoes the query on that row); a longer one starts empty.
        seed = self._buf if self._prompt_len + _wcswidth(self._buf) < self._cols - 1 else ""
        self._buf = seed
        self._cursor = len(seed)
        self._ghost_enabled = False
        self._redraw()
        self._ghost_enabled = True

        # Move below the prompt line
        sys.stdout.write("\n")
        sys.stdout.flush()

        caret_col = _pending_wrap_col(self._prompt_len, self._cols)

        def matching(typed: str) -> list[HistoryEntry]:
            keywords = typed.lower().split()
            return [e for e in entries if all(k in e.cmd.lower() for k in keywords)]

        def refresh(typed: str) -> tuple[list[HistoryEntry], int]:
            return matching(typed), caret_col

        now = time.time()
        picker = InlinePicker(
            matching(seed),
            display_fn=lambda e: e.cmd,
            meta_fn=lambda e: _history_meta(e, now),
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
            typed=seed,
        )
        with self._picker_session():
            selected = picker.run()

        # No flush — let the up-move batch with _redraw on return.
        sys.stdout.write("\033[1A")

        if selected is not None:
            self._buf = selected.cmd
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
        if not completion.value.endswith(("/", "=")) and not post[:1].isspace():
            value = value + " "
        self._buf = pre + value + post
        self._cursor = len(pre) + len(value)


class EditorContext:
    """What a ``@keys.action`` function receives: the line being edited.

    ``buffer`` and ``cursor`` are read-write (the cursor is clamped to the
    line); the methods edit at the caret, run another action by name, or ask
    the user to pick from a list.
    """

    def __init__(self, editor: LineEditor):
        self._editor = editor

    @property
    def buffer(self) -> str:
        return self._editor._buf

    @buffer.setter
    def buffer(self, text: str) -> None:
        self._editor._buf = str(text)
        self._editor._move_to(self._editor._cursor)

    @property
    def cursor(self) -> int:
        return self._editor._cursor

    @cursor.setter
    def cursor(self, pos: int) -> None:
        self._editor._move_to(int(pos))

    @property
    def history(self) -> list[str]:
        """This context's Up/Down history, oldest first (a copy)."""
        return list(self._editor._local_entries())

    def insert(self, text: str) -> None:
        """Insert *text* at the caret and move past it."""
        self._editor._insert(str(text))

    def replace(self, start: int, end: int, text: str) -> None:
        """Replace ``buffer[start:end]`` with *text*; the caret ends after it."""
        ed = self._editor
        start = max(0, min(len(ed._buf), start))
        end = max(start, min(len(ed._buf), end))
        ed._buf = ed._buf[:start] + str(text) + ed._buf[end:]
        ed._cursor = start + len(str(text))

    def invoke(self, name: str) -> None:
        """Run the action *name* (``"complete"``, ``"accept"``, another user
        action …).  Inside an action that overrides a built-in, its own name
        runs the built-in.  ``"accept"`` finishes the line once this action
        returns."""
        if keys.get_action(name) is None:
            raise ValueError(f"unknown key action {name!r}")
        result = self._editor.run_action(name)
        if result is not None:
            self._editor._pending_result = result

    def choose(self, items: list[str], title: str = "") -> str | None:
        """Pick one of *items* in a picker below the line; None on Esc."""
        return self._editor._choose(items, title)
