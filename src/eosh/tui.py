"""DIY inline TUI widgets — no alternate screen, no scroll region."""

from __future__ import annotations

import os
import sys
import unicodedata
from typing import Callable, Generic, Sequence, TypeVar

from . import terminal
from .colors import _bg, _fg, get_color_scheme
from .scrollbar import render_column as _scrollbar_column

T = TypeVar("T")


def _csi(code: str) -> str:
    return f"\033[{code}"


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


def _wcs_clip(s: str, max_cols: int) -> str:
    """Return the longest prefix of s that fits within max_cols terminal columns."""
    cols = 0
    for i, ch in enumerate(s):
        if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            w = 0
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            w = 2
        else:
            w = 1
        if cols + w > max_cols:
            return s[:i]
        cols += w
    return s


def _wcs_ljust(s: str, width: int) -> str:
    """Left-justify s in a field of width terminal columns, padding with spaces."""
    return s + " " * max(0, width - _wcswidth(s))


# ─── metadata columns ───────────────────────────────────────────────────────
#
# ``meta_fn`` may return one string (rendered as-is) or a sequence of cells,
# in which case the cells are padded so they line up across every row of the
# picker.  Columns are what a separator inside the string cannot be: aligned,
# and free — a run of two spaces reads as a column break, where a " · " glued
# between fields costs three columns on every row and still leaves the eye to
# do the aligning.

_META_GAP = "  "


def _meta_cells(meta: str | Sequence[str]) -> tuple[str, ...]:
    """Normalize a ``meta_fn`` return value into a tuple of column cells."""
    if isinstance(meta, str):
        return (meta,) if meta else ()
    return tuple(meta)


def _meta_col_widths(items: list[T], meta_fn: Callable[[T], str | Sequence[str]] | None) -> list[int]:
    """Widest cell per column across *items* (0 for a column no row fills).

    A width of 0 means the column is dropped by ``_compose_meta``, so an
    optional field (a status shown only when it is interesting) costs nothing
    on the rows — and in the runs — that leave it empty.
    """
    if meta_fn is None:
        return []
    widths: list[int] = []
    for item in items:
        for i, cell in enumerate(_meta_cells(meta_fn(item))):
            if i >= len(widths):
                widths.extend([0] * (i + 1 - len(widths)))
            widths[i] = max(widths[i], _wcswidth(cell))
    return widths


def _meta_total(widths: list[int]) -> int:
    """Display width of a fully populated meta area laid out in *widths*."""
    live = [w for w in widths if w]
    return sum(live) + len(_META_GAP) * (len(live) - 1) if live else 0


def _compose_meta(meta: str | Sequence[str], widths: list[int]) -> str:
    """Render one row's cells padded into *widths* so columns align."""
    cells = _meta_cells(meta)
    parts = [
        _wcs_ljust(cells[i] if i < len(cells) else "", w)
        for i, w in enumerate(widths)
        if w
    ]
    return _META_GAP.join(parts).rstrip() if parts else ""


def _statusbar(label: str, hints: str, cols: int) -> str:
    """Render the status-bar string for the bottom line of the terminal.

    Sized to fit the text plus a small margin, NOT the full row width.
    A full-width bar (any row whose cells are all "filled" up to ``cols``)
    forces some terminals (Terminal.app, xterm.js / VSCode integrated) to
    reflow that row when the user shrinks the width — which scrolls the
    entire screen up. During a drag-resize the terminal can scroll
    multiple times between SIGWINCH deliveries, leaving stranded copies
    of the prompt above our resize-redraw that we have no way to detect
    after the fact. iTerm2 trims trailing styled cells before reflow and
    doesn't show this symptom, but the narrow-bar form is correct on all
    terminals we target.

    By keeping the bar's footprint to its text width and clearing the
    rest of the row with ``\\033[K``, the row's content is short and the
    terminal won't reflow it on a width shrink (unless the shrink goes
    *below* the text width — visually obvious and rare).
    """
    s = get_color_scheme()
    # Status bar is a single row — collapse any embedded newlines (a help
    # text's first line is the summary; the rest is detail meant for `help`).
    label = label.split("\n", 1)[0].rstrip() if label else label
    hints = hints.split("\n", 1)[0].rstrip() if hints else hints
    parts = [p for p in (label, hints) if p]
    text = "   ".join(parts)
    if not text:
        # No content to show — emit just an erase-line so any leftover
        # bar from a prior render disappears.
        return "\033[2K"
    inner = f"  {text}  "
    # Cap to cols-1 so even pathologically long labels don't fill the row.
    inner = _wcs_clip(inner, max(1, cols - 1))
    # Bar = colored text block + ``\033[K`` (erase to end of row) so the
    # cells past the bar are default/empty — terminals don't reflow empty
    # cells on width shrink.
    return (
        f"{_bg(*s.statusbar_bg)}{_fg(*s.statusbar_fg)}{inner}\033[0m\033[K"
    )


def _common_prefix(strings: list[str]) -> str:
    if not strings:
        return ""
    prefix = strings[0]
    for s in strings[1:]:
        while not s.startswith(prefix):
            prefix = prefix[:-1]
            if not prefix:
                return ""
    return prefix


class InlinePicker(Generic[T]):
    """
    Selectable list rendered inline below the current cursor position.

    Position is anchored with DECSC/DECRC (ESC 7 / ESC 8) at reserve time.
    On SIGWINCH the picker cancels immediately — redrawing after a resize is
    unreliable without an alternate screen, so the user just presses TAB again.

    ``select_first=False`` opens the list with *no* row highlighted, so Enter
    dismisses instead of silently accepting a candidate the user never chose
    (see the completion pickers in ``lineedit.py``). The first Down/Up moves
    onto the first/last row. When ``refresh_fn`` narrows the list to nothing
    the picker closes itself and sets ``closed_empty`` — an open-but-empty
    picker renders no rows, so it would swallow keystrokes invisibly.
    ``empty_placeholder`` opts out of that: the picker stays open and renders
    the placeholder text in place of the rows, so the text typed so far
    survives and Backspace can widen the filter again (Ctrl+R history search).
    """

    def __init__(
        self,
        items: list[T],
        display_fn: Callable[[T], str] = str,
        meta_fn: Callable[[T], str | Sequence[str]] | None = None,
        max_height: int = 10,
        col: int = 0,
        initial_offset: int = 0,
        rows_above: int = 1,
        refresh_fn: Callable[[str], tuple[list[T], int]] | None = None,
        value_fn: Callable[[T], str | None] | None = None,
        completion_prefix: str = "",
        extend_fn: Callable[[list[T], str], str] | None = None,
        reopen_when: Callable[[list[T]], bool] | None = None,
        min_width: int = 0,
        hide_cursor: bool = False,
        status_label: str = "",
        status_hints: str = "",
        transient_status: bool = False,
        preview_fn: Callable[[T], list[str]] | None = None,
        preview_height: int = 0,
        key_actions: dict[bytes, str] | None = None,
        select_first: bool = True,
        empty_placeholder: str = "",
    ):
        self._items = items
        self._display_fn = display_fn
        self._meta_fn = meta_fn
        self._max_height = max_height
        self._col = col
        self._initial_offset = initial_offset
        self._rows_above = rows_above
        self._refresh_fn = refresh_fn
        self._value_fn = value_fn
        self._completion_prefix = completion_prefix
        self._extend_fn = extend_fn
        self._reopen_when = reopen_when
        self._min_width = min_width
        self._hide_cursor = hide_cursor
        self._status_label = status_label
        self._status_hints = status_hints
        self._transient_status = transient_status
        self._preview_fn = preview_fn
        self._preview_height = max(0, preview_height) if preview_fn is not None else 0
        self._key_actions = dict(key_actions or {})
        self._empty_placeholder = empty_placeholder
        # -1 means "no row highlighted"; it is also the state the selection
        # resets to whenever the list is re-filtered by typing.
        self._no_selection = 0 if select_first else -1
        self._typed = ""
        self.reopen = False          # set True when tab-complete typed chars; caller should reopen
        self.apply_backspace = False  # set True when backspace pressed with no typed chars
        self.closed_empty = False     # set True when narrowing left no candidates
        self.action: str | None = None  # set when run() exits via a key in key_actions

        self._selected = self._no_selection
        self._offset = 0
        self._cols = 80
        self._lines = 24
        self._height = min(max_height, len(items))
        self._cancelled = False

    @property
    def typed(self) -> str:
        """Characters the user typed while the picker was open.

        The picker echoes them to the prompt line itself but never touches the
        caller's buffer, so the caller must commit them on every exit path.
        """
        return self._typed

    def _current(self) -> T | None:
        """The highlighted item, or None when nothing is highlighted."""
        if not self._items or self._selected < 0:
            return None
        return self._items[self._selected]

    def run(self) -> T | None:
        if not self._items:
            return None

        self._fd = sys.stdin.fileno()
        old_attrs = terminal.get_mode(self._fd)

        result: T | None = None
        if self._hide_cursor:
            sys.stdout.write("\x1b[?25l")
            sys.stdout.flush()
        try:
            self._update_size()
            self._last_size = terminal.terminal_size()
            self._reserve()
            terminal.set_raw(self._fd)
            self._render()

            while True:
                key_bytes = self._read_key()
                # A transient status message (e.g. a one-shot warning from the
                # caller) is cleared on the next user input so it doesn't
                # linger past the action that produced it.
                if self._transient_status and self._status_label:
                    self._status_label = ""
                if key_bytes in self._key_actions:
                    self.action = self._key_actions[key_bytes]
                    result = self._current()
                    break
                action = self._dispatch(key_bytes)
                if action == "accept":
                    # With no row highlighted this returns None — Enter then
                    # just dismisses the list instead of picking a candidate
                    # the user never selected.
                    result = self._current()
                    break
                if action == "cancel":
                    break
                if action == "up":
                    self._move(-1)
                    self._render()
                elif action == "down":
                    self._move(1)
                    self._render()
                elif action == "tab_complete":
                    # TAB on a single remaining candidate accepts it — the
                    # caller's _apply() then replaces the whole token with
                    # the candidate's canonical value, so any case mismatch
                    # in what the user typed (e.g. "p" vs "Pictures/") is
                    # corrected. Common-prefix extension only writes the
                    # suffix past what was typed, which preserves the wrong
                    # case in the prefix region.
                    if len(self._items) == 1:
                        result = self._items[0]
                        break
                    if self._handle_tab_complete():
                        break
                    # Nothing left to extend: TAB is a no-op. It must NOT fall
                    # back to highlighting a row — TAB means "type the shared
                    # prefix for me", and moving the selection is the user's
                    # job (Down/Up). Anything else re-introduces the surprise
                    # that select_first=False exists to remove.
                elif action == "backspace":
                    if self._handle_backspace():
                        break
                elif len(action) == 1:  # printable char
                    if self._handle_char(action):
                        break
        finally:
            terminal.restore_mode(self._fd, old_attrs)
            self._cleanup()
            if self._hide_cursor:
                sys.stdout.write("\x1b[?25h")
                sys.stdout.flush()

        return result

    # ── size ────────────────────────────────────────────────────────────────

    def _update_size(self) -> None:
        sz = os.get_terminal_size()
        self._cols = sz.columns
        self._lines = sz.lines
        # Preview area is rendered below the picker rows; reserve space for it
        # so the list stays visible alongside the focused item's preview.
        reserve = 4 + self._preview_height
        self._height = min(self._max_height, len(self._items), max(1, sz.lines - reserve))

    # ── drawing ─────────────────────────────────────────────────────────────

    def _reserve(self) -> None:
        """Create blank lines below cursor and save the top position with DECSC."""
        total = self._height + self._preview_height
        sys.stdout.write("\n" * total)
        # CHA jumps to the target column atomically; \r + cursor-forward
        # would briefly render the caret at col 0 before moving right.
        sys.stdout.write(_csi(f"{total}A") + f"\033[{self._col + 1}G\0337")
        sys.stdout.flush()

    def _render(self) -> None:
        """Restore to DECSC anchor, clear, draw rows, then place cursor at prompt caret."""
        visible = self._items[self._offset : self._offset + self._height]
        out: list[str] = ["\0338\r\033[J"]  # restore to anchor, clear to end

        label_col, meta_col, panel_w, meta_widths = self._compute_layout()
        has_scrollbar = len(self._items) > self._height
        sb_cells = self._scrollbar_cells() if has_scrollbar else []
        if not visible and self._empty_placeholder:
            out.append(self._format_placeholder())
        for i, item in enumerate(visible):
            out.append(
                self._format_row(
                    item,
                    selected=(i + self._offset == self._selected),
                    scrollbar_cell=sb_cells[i] if has_scrollbar else "",
                    label_col=label_col,
                    meta_col=meta_col,
                    panel_w=panel_w,
                    meta_widths=meta_widths,
                )
            )
            if i < len(visible) - 1:
                out.append("\n")

        # Preview pane below the list, if configured.
        if self._preview_height > 0:
            out.append(self._format_preview(panel_w))

        # Re-save anchor, then move cursor to prompt caret position.
        # CHA (\033[{N}G) sets the column atomically — `\r` + cursor-forward
        # flickers because the terminal briefly renders the caret at col 0.
        out.append("\0338\0337")
        if self._rows_above > 0:
            out.append(f"\033[{self._rows_above}A")
        caret_col_now = self._col + self._initial_offset + len(self._typed)
        out.append(f"\033[{caret_col_now + 1}G")

        # Draw status bar at the bottom line, then return to caret via anchor.
        # When a status label is set it takes the whole bar — hints are
        # secondary and reading two strings simultaneously fights for the
        # user's attention.
        out.append(f"\033[{self._lines};1H")
        hints = "" if self._status_label else self._status_hints
        out.append(_statusbar(self._status_label, hints, self._cols))
        out.append("\0338")
        if self._rows_above > 0:
            out.append(f"\033[{self._rows_above}A")
        out.append(f"\033[{caret_col_now + 1}G")

        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def _format_placeholder(self) -> str:
        """Render the ``empty_placeholder`` text as a single dim row.

        Drawn instead of the rows when the filter matched nothing, so the
        picker keeps a visible footprint (and thus stays credibly open) while
        the user edits the query.
        """
        avail = max(1, self._cols - self._col)
        text = _wcs_clip(self._empty_placeholder, avail)
        s = get_color_scheme()
        bg = _bg(*s.picker_row_bg) + _fg(*s.picker_row_fg)
        col_move = f"\033[{self._col}C" if self._col > 0 else ""
        width = max(self._min_width, _wcswidth(text))
        return f"\r{col_move}{bg}\033[2m{_wcs_ljust(text, min(width, avail))}\033[22m\033[0m"

    def _format_preview(self, panel_w: int) -> str:
        """Render the preview pane: ``preview_height`` lines below the list."""
        s = get_color_scheme()
        lines: list[str] = []
        focused = self._current()
        if focused is not None and self._preview_fn is not None:
            try:
                raw = self._preview_fn(focused)
            except Exception:
                raw = []
            lines = [str(line) for line in raw][-self._preview_height:]
        # Pad to preview_height so the area's footprint stays stable.
        lines = lines + [""] * max(0, self._preview_height - len(lines))
        avail = max(1, self._cols - self._col)
        col_move = f"\033[{self._col}C" if self._col > 0 else ""
        out = []
        # The picker rows leave the cursor at the end of the last row;
        # advance to the line below before drawing preview.
        for line in lines:
            text = _wcs_clip(line, avail)
            text_disp = _wcs_ljust(text, avail)
            # Dim/grey body so the preview reads as secondary info.
            out.append(
                f"\n\r{col_move}\033[2m{text_disp}\033[22m"
            )
        return "".join(out)

    def _scrollbar_cells(self) -> list[str]:
        s = get_color_scheme()
        return _scrollbar_column(
            height=self._height,
            offset=self._offset,
            visible=self._height,
            total=len(self._items),
            thumb=s.scroll_thumb,
            track=s.scroll_track,
        )

    def _compute_layout(self) -> tuple[int, int, int, list[int]]:
        """Return (label_col, meta_col, panel_w, meta_widths).

        ``label_col`` is the width every label is padded to so meta columns
        align across rows. ``meta_col`` is the cap applied to descriptions —
        what fits after labels and the 2-space gap inside ``avail``.
        ``meta_widths`` sizes the columns *within* the meta area (see
        ``_meta_col_widths``); a single-string ``meta_fn`` yields one column.
        """
        has_scrollbar = len(self._items) > self._height
        avail = max(1, self._cols - self._col - (1 if has_scrollbar else 0))

        max_label = 0
        meta_widths = _meta_col_widths(self._items, self._meta_fn)
        max_meta = _meta_total(meta_widths)
        for item in self._items:
            max_label = max(max_label, _wcswidth(self._display_fn(item)))

        if max_meta:
            # Reserve at least 2 cols for the gap; squeeze label first if tight.
            label_col = min(max_label, max(1, avail - 2))
            meta_col = max(0, min(max_meta, avail - label_col - 2))
        else:
            label_col = min(max_label, avail)
            meta_col = 0

        panel_w = label_col + (2 + meta_col if meta_col else 0)
        panel_w = max(panel_w, self._min_width)
        return label_col, meta_col, min(panel_w, avail), meta_widths

    def _format_row(
        self,
        item: T,
        *,
        selected: bool,
        scrollbar_cell: str = "",
        label_col: int = 0,
        meta_col: int = 0,
        panel_w: int = 0,
        meta_widths: list[int] | None = None,
    ) -> str:
        label = self._display_fn(item)
        meta = (
            _compose_meta(self._meta_fn(item), meta_widths or [])
            if self._meta_fn else ""
        )

        label = _wcs_clip(label, label_col)
        # Pad the label to label_col only when there's a meta column to align to.
        label_disp = _wcs_ljust(label, label_col) if meta_col else label
        meta = _wcs_clip(meta, meta_col) if meta_col else ""

        content_w = _wcswidth(label_disp) + (2 + _wcswidth(meta) if meta else 0)
        pad = " " * max(0, panel_w - content_w)

        s = get_color_scheme()
        bg = _bg(*s.picker_row_bg) + _fg(*s.picker_row_fg)
        sel = _bg(*s.picker_sel_bg) + _fg(*s.picker_sel_fg)
        col_move = f"\033[{self._col}C" if self._col > 0 else ""
        if selected:
            inner = label_disp + (f"  {meta}" if meta else "")
            row = f"\r{col_move}{sel}{inner}{pad}\033[0m"
        else:
            inner = label_disp + (f"  \033[2m{meta}\033[22m" if meta else "")
            row = f"\r{col_move}{bg}{inner}{pad}\033[0m"

        if scrollbar_cell:
            sb_col = self._col + panel_w + 1  # 1-indexed terminal column
            row += f"\033[{sb_col}G{scrollbar_cell}"
        return row

    def _cleanup(self) -> None:
        """Restore to anchor and erase the picker area.

        Skip the flush so the bytes batch with whatever the caller writes next
        (typically a ``\\033[{rows_above}A`` to return to the prompt caret, then
        a redraw or the next picker's ``_reserve``). Flushing here would render
        an intermediate cursor position below the prompt, producing flicker.

        Skip the leading ``\\r`` for the same reason: the picker only ever
        wrote from ``self._col`` onwards (each row uses ``\\r{col_move}{...}``
        within the same buffered render), so the area to the left of the
        anchor on the anchor row was never touched and doesn't need clearing.

        On a resize-induced cancel (``self._cancelled``), hide the cursor
        first: the DECSC anchor was saved in the *old* geometry, so
        ``\\0338`` lands at an unexpected post-reflow position and the
        caret would visibly flash there before the caller's redraw moves
        it back. The next ``_redraw()`` re-shows the cursor at the prompt
        caret.

        On a non-resize close (Esc, selection, or a narrow-into-reopen),
        the anchor is still correct, so leave the cursor visible —
        otherwise a picker reopen-flow would close one picker (hiding
        the cursor) and open the next without the line editor's
        ``_redraw()`` running between them, leaving the caret invisible
        until the picker chain finally exits.
        """
        prefix = "\x1b[?25l" if self._cancelled else ""
        sys.stdout.write(f"{prefix}\0338\033[J")

    # ── char input ──────────────────────────────────────────────────────────

    def _handle_tab_complete(self) -> bool:
        """Type the common prefix extension. Returns True (sets reopen) if chars were typed."""
        if not self._items:
            return False
        if self._extend_fn is not None:
            # The caller owns the arithmetic: it sees the *current* items and
            # typed text, so it can re-decide which value space the remaining
            # rows live in and where the text already typed starts (narrowing
            # can change both — see ``lineedit._complete``).
            extension = self._extend_fn(self._items, self._typed)
        elif self._value_fn is not None:
            # Simple case: every item's value extends ``completion_prefix``,
            # which grows by exactly the characters typed since the picker
            # opened.  ``value_fn`` may return None to exclude an item from the
            # common-prefix measurement.
            values = [v for v in map(self._value_fn, self._items) if v is not None]
            if not values:
                return False
            effective_len = len(self._completion_prefix) + len(self._typed)
            extension = _common_prefix(values)[effective_len:]
        else:
            return False
        if not extension:
            return False
        sys.stdout.write(extension)
        sys.stdout.flush()
        self._typed += extension
        self.reopen = True
        return True

    def _handle_char(self, ch: str) -> bool:
        """Write ch at prompt caret and refresh. Returns True when the picker should close."""
        sys.stdout.write(ch)
        sys.stdout.flush()
        self._typed += ch
        if self._refresh_fn is not None:
            new_items, new_col = self._refresh_fn(self._typed)
            self._items = new_items
            if not new_items and not self._empty_placeholder:
                # Nothing matches what's now typed. Keeping the picker open
                # would leave it rendering zero rows while still eating keys —
                # invisible but active. Close instead; the caller commits the
                # typed chars and the user can press TAB again.
                self.closed_empty = True
                return True
            if new_col != self._col or (self._reopen_when is not None and self._reopen_when(new_items)):
                self.reopen = True
                return True
            self._selected = self._no_selection
            self._offset = 0
        self._render()
        return False

    def _handle_backspace(self) -> bool:
        """Erase one char. Returns True when the picker should close (sets reopen or apply_backspace)."""
        if self._typed:
            # Erase last picker-typed char from the prompt line.
            sys.stdout.write("\033[D \033[D")
            sys.stdout.flush()
            self._typed = self._typed[:-1]
            if self._refresh_fn is not None:
                new_items, new_col = self._refresh_fn(self._typed)
                self._items = new_items
                if not new_items and not self._empty_placeholder:
                    self.closed_empty = True   # same rule as _handle_char
                    return True
                if new_col != self._col:
                    self.reopen = True
                    return True
                self._selected = self._no_selection
                self._offset = 0
            self._render()
            return False
        else:
            # No picker-typed chars remain; erase the last buffer char visually
            # and signal the caller to apply the deletion and close.
            caret_col = self._col + self._initial_offset
            if caret_col > 0:
                sys.stdout.write("\033[D \033[D")
                sys.stdout.flush()
            self.apply_backspace = True
            return True

    # ── input ───────────────────────────────────────────────────────────────

    def _read_key(self) -> bytes:
        while True:
            if self._cancelled:
                return b"\x1b"  # triggers "cancel" in _dispatch
            if terminal.wait_readable(self._fd, 0.1):
                return terminal.read_key(self._fd)
            # No key within the poll window — check for a resize so the picker
            # can cancel. Redrawing after a resize without an alt-screen is
            # unreliable, so the user just presses TAB again.
            if terminal.terminal_size() != self._last_size:
                self._cancelled = True

    def _dispatch(self, key: bytes) -> str:
        if key in (b"\r", b"\n"):
            return "accept"
        if key in (b"\x1b", b"\x03"):
            return "cancel"
        if key in (b"\x1b[A", b"\x1bOA", b"\x10"):   # up arrow (normal/app mode), Ctrl+P
            return "up"
        if key in (b"\x1b[B", b"\x1bOB", b"\x0e"):  # down arrow (normal/app mode), Ctrl+N
            return "down"
        if key == b"\t":
            return "tab_complete"
        if key in (b"\x7f", b"\x08"):            # Backspace, Ctrl+H
            return "backspace"
        if len(key) == 1 and 0x20 <= key[0] < 0x7F:
            return key.decode()
        return "noop"

    # ── scroll ───────────────────────────────────────────────────────────────

    def _move(self, delta: int) -> None:
        n = len(self._items)
        if not n:
            return
        if self._selected < 0:
            # Nothing highlighted yet: Down enters at the top, Up at the bottom.
            self._selected = 0 if delta > 0 else n - 1
        else:
            self._selected = max(0, min(n - 1, self._selected + delta))
        if self._selected < self._offset:
            self._offset = self._selected
        elif self._selected >= self._offset + self._height:
            self._offset = self._selected - self._height + 1


class InlineArgPrompt:
    """Single-line inline prompt for a flag argument, rendered below the cursor.

    Shows a dim label (e.g. ``--max-depth <N>:``) followed by an editable
    input area. Enter confirms; Esc / Ctrl+C cancels (returns None).
    The caller is responsible for positioning (writing \\n to move below the
    command line) before calling run() and moving the cursor back afterward.
    """

    def __init__(self, label: str, description: str = "", status_label: str = "", initial: str = ""):
        self._label = label
        self._description = description
        self._status_label = status_label
        self._buf = initial
        self._cancelled = False
        try:
            sz = os.get_terminal_size()
            self._cols = sz.columns
            self._lines = sz.lines
        except OSError:
            self._cols = 80
            self._lines = 24

    def run(self) -> str | None:
        self._fd = sys.stdin.fileno()
        old_attrs = terminal.get_mode(self._fd)
        self._last_size = terminal.terminal_size()

        result: str | None = None
        try:
            self._reserve()
            terminal.set_raw(self._fd)
            self._render()

            while True:
                key = self._read_key()
                if self._cancelled or key in (b"\x1b", b"\x03"):
                    break
                if key in (b"\r", b"\n"):
                    result = self._buf
                    break
                if key in (b"\x7f", b"\x08"):
                    if self._buf:
                        self._buf = self._buf[:-1]
                        self._render()
                elif len(key) == 1 and 0x20 <= key[0] < 0x7F:
                    self._buf += key.decode()
                    self._render()
        finally:
            terminal.restore_mode(self._fd, old_attrs)
            self._cleanup()

        return result

    def _reserve(self) -> None:
        """Save anchor at col 0 of the current line (caller moved us here).
        When a description is shown, an extra line is reserved below."""
        if self._description:
            sys.stdout.write("\n")   # push a second line into existence
            sys.stdout.write("\033[1A")  # come back up to the anchor line
        sys.stdout.write("\r\0337")
        sys.stdout.flush()

    def _render(self) -> None:
        """Restore to anchor, redraw description (if any) + label + typed text."""
        out = ["\0338\r\033[J"]
        if self._description:
            desc = self._description[: self._cols - 2]
            out.append(f"  \033[2m{desc}\033[0m\n")
        # \r ensures col 0 — raw-mode \n is a bare line-feed and leaves the
        # cursor at the column where the description text ended.
        out.append(f"\r\033[2m{self._label}:\033[0m {self._buf}")
        # Draw status bar at the bottom line, then return to end-of-input via anchor.
        cursor_col = _wcswidth(self._label) + 2 + _wcswidth(self._buf)
        out.append(f"\033[{self._lines};1H")
        out.append(_statusbar(self._status_label, "", self._cols))
        out.append("\0338")  # restore to anchor
        if self._description:
            out.append("\033[B\r")  # down to label row, col 0
        out.append(f"\033[{cursor_col + 1}G")
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def _cleanup(self) -> None:
        # On resize-cancel, hide the cursor: the DECSC anchor was saved in
        # the old geometry, so \0338 lands at an unexpected post-reflow
        # position and the caret would flash there before the next redraw
        # moves it. The caller's _redraw() restores visibility. Non-resize
        # closes leave the cursor visible — the anchor is still correct.
        prefix = "\x1b[?25l" if self._cancelled else ""
        sys.stdout.write(f"{prefix}\0338\r\033[J")
        sys.stdout.flush()

    def _read_key(self) -> bytes:
        while True:
            if self._cancelled:
                return b"\x1b"
            if terminal.wait_readable(self._fd, 0.1):
                return terminal.read_key(self._fd)
            if terminal.terminal_size() != self._last_size:
                self._cancelled = True


class InlineMultiPicker(Generic[T]):
    """
    Multi-select inline picker rendered below the cursor.

    Space toggles the highlighted item's checked state. Enter confirms and
    returns all checked items (or just the highlighted item if nothing is
    checked). Esc / Ctrl+C cancels and returns None.

    With ``select_first=False`` no row is highlighted until the user moves or
    jumps, so Enter on a freshly opened picker returns None (dismiss) rather
    than the first flag in the list.

    On SIGWINCH the picker cancels, same as InlinePicker.
    """

    def __init__(
        self,
        items: list[T],
        display_fn: Callable[[T], str] = str,
        meta_fn: Callable[[T], str | Sequence[str]] | None = None,
        max_height: int = 12,
        rows_above: int = 1,
        caret_col: int = 0,
        status_label: str = "",
        select_first: bool = True,
    ):
        self._items = items
        self._display_fn = display_fn
        self._meta_fn = meta_fn
        self._max_height = max_height
        self._rows_above = rows_above
        self._caret_col = caret_col
        self._status_label = status_label

        self._selected = 0 if select_first else -1  # -1 = no row highlighted
        self._offset = 0
        self._checked: set[int] = set()
        self._cols = 80
        self._lines = 24
        self._height = min(max_height, len(items))
        self._cancelled = False
        self._label_col_w = max((_wcswidth(display_fn(item)) for item in items), default=4)
        self._meta_widths = _meta_col_widths(items, meta_fn)

    def run(self) -> list[T] | None:
        """Return checked items (or [highlighted] if none), or None on cancel."""
        if not self._items:
            return None

        self._fd = sys.stdin.fileno()
        old_attrs = terminal.get_mode(self._fd)

        result: list[T] | None = None
        try:
            self._update_size()
            self._last_size = terminal.terminal_size()
            self._reserve()
            terminal.set_raw(self._fd)
            self._render()

            while True:
                action = self._dispatch(self._read_key())
                if action == "accept":
                    if self._checked:
                        result = [self._items[i] for i in sorted(self._checked)]
                    elif self._selected >= 0:
                        result = [self._items[self._selected]]
                    else:
                        result = None  # nothing checked, nothing highlighted
                    break
                if action == "cancel":
                    break
                if action == "up":
                    self._move(-1)
                    self._render()
                elif action == "down":
                    self._move(1)
                    self._render()
                elif action == "toggle":
                    idx = self._selected
                    if idx < 0:
                        continue  # nothing highlighted yet — nothing to toggle
                    if idx in self._checked:
                        self._checked.discard(idx)
                    else:
                        self._checked.add(idx)
                    self._render()
                elif action.startswith("jump:"):
                    self._jump_to(action[5:])
                    self._render()
        finally:
            terminal.restore_mode(self._fd, old_attrs)
            self._cleanup()

        return result

    # ── size ────────────────────────────────────────────────────────────────

    def _update_size(self) -> None:
        sz = os.get_terminal_size()
        self._cols = sz.columns
        self._lines = sz.lines
        self._height = min(self._max_height, len(self._items), max(1, sz.lines - 4))

    # ── drawing ─────────────────────────────────────────────────────────────

    def _reserve(self) -> None:
        sys.stdout.write("\n" * self._height)
        sys.stdout.write(_csi(f"{self._height}A") + "\r\0337")
        sys.stdout.flush()

    def _compute_panel_w(self) -> int:
        """Minimum width that fits all rows, bounded by available columns."""
        has_scrollbar = len(self._items) > self._height
        avail = max(1, self._cols - (1 if has_scrollbar else 0))
        check_w = len(self._CHECK_ON)
        content_avail = max(0, avail - check_w)
        # label_col_w is already in display columns (computed via _wcswidth in __init__)
        label_col = min(self._label_col_w, content_avail)
        max_meta = _meta_total(self._meta_widths)
        meta_cap = min(max_meta, max(0, content_avail - label_col - 2))
        panel_w = check_w + label_col + (2 + meta_cap if meta_cap else 0)
        return min(panel_w, avail)

    def _render(self) -> None:
        visible = self._items[self._offset : self._offset + self._height]
        out: list[str] = ["\0338\r\033[J"]

        panel_w = self._compute_panel_w()
        has_scrollbar = len(self._items) > self._height
        sb_cells = self._scrollbar_cells() if has_scrollbar else []
        for i, item in enumerate(visible):
            abs_i = self._offset + i
            out.append(self._format_row(item, checked=(abs_i in self._checked), selected=(abs_i == self._selected), scrollbar_cell=sb_cells[i] if has_scrollbar else "", panel_w=panel_w))
            if i < len(visible) - 1:
                out.append("\n")

        # Re-save anchor, then move cursor back to the prompt caret.
        # CHA (\033[{N}G) sets the column atomically — `\r` + cursor-forward
        # flickers because the terminal briefly renders the caret at col 0.
        out.append("\0338\0337")
        if self._rows_above > 0:
            out.append(f"\033[{self._rows_above}A")
        out.append(f"\033[{self._caret_col + 1}G")

        # Draw status bar at the bottom line, then return to caret via anchor.
        out.append(f"\033[{self._lines};1H")
        out.append(_statusbar(self._status_label, "", self._cols))
        out.append("\0338")
        if self._rows_above > 0:
            out.append(f"\033[{self._rows_above}A")
        out.append(f"\033[{self._caret_col + 1}G")

        sys.stdout.write("".join(out))
        sys.stdout.flush()

    _CHECK_ON = "[x] "
    _CHECK_OFF = "[ ] "

    def _scrollbar_cells(self) -> list[str]:
        s = get_color_scheme()
        return _scrollbar_column(
            height=self._height,
            offset=self._offset,
            visible=self._height,
            total=len(self._items),
            thumb=s.scroll_thumb,
            track=s.scroll_track,
        )

    def _format_row(self, item: T, *, checked: bool, selected: bool, scrollbar_cell: str = "", panel_w: int = 0) -> str:
        label = self._display_fn(item)
        meta = (
            _compose_meta(self._meta_fn(item), self._meta_widths)
            if self._meta_fn else ""
        )

        check = self._CHECK_ON if checked else self._CHECK_OFF
        content_avail = max(1, panel_w - len(check))
        # Align all labels to the widest one (in display columns); give the rest to the description.
        label_col = min(self._label_col_w, content_avail)
        meta_w = max(0, content_avail - label_col - 2)
        label_padded = _wcs_ljust(_wcs_clip(label, label_col), label_col)
        meta = _wcs_clip(meta, meta_w)

        content_w = len(check) + label_col + (2 + _wcswidth(meta) if meta else 0)
        pad = " " * max(0, panel_w - content_w)

        s = get_color_scheme()
        bg = _bg(*s.picker_row_bg) + _fg(*s.picker_row_fg)
        sel = _bg(*s.picker_sel_bg) + _fg(*s.picker_sel_fg)
        if selected:
            inner = check + label_padded + (f"  {meta}" if meta else "")
            row = f"\r{sel}{inner}{pad}\033[0m"
        else:
            inner = check + label_padded + (f"  \033[2m{meta}\033[22m" if meta else "")
            row = f"\r{bg}{inner}{pad}\033[0m"

        if scrollbar_cell:
            sb_col = panel_w + 1  # 1-indexed terminal column
            row += f"\033[{sb_col}G{scrollbar_cell}"

        return row

    def _cleanup(self) -> None:
        # On resize-cancel, hide the cursor: the DECSC anchor was saved in
        # the old geometry, so \0338 lands at an unexpected post-reflow
        # position and the caret would flash there before the next redraw
        # moves it. The caller's _redraw() restores visibility. Non-resize
        # closes leave the cursor visible — the anchor is still correct.
        prefix = "\x1b[?25l" if self._cancelled else ""
        sys.stdout.write(f"{prefix}\0338\r\033[J")
        sys.stdout.flush()

    # ── input ───────────────────────────────────────────────────────────────

    def _read_key(self) -> bytes:
        while True:
            if self._cancelled:
                return b"\x1b"
            if terminal.wait_readable(self._fd, 0.1):
                return terminal.read_key(self._fd)
            if terminal.terminal_size() != self._last_size:
                self._cancelled = True

    def _dispatch(self, key: bytes) -> str:
        if key in (b"\r", b"\n"):
            return "accept"
        if key in (b"\x1b", b"\x03"):
            return "cancel"
        if key in (b"\x1b[A", b"\x1bOA", b"\x10"):
            return "up"
        if key in (b"\x1b[B", b"\x1bOB", b"\x0e"):
            return "down"
        if key == b" ":
            return "toggle"
        if len(key) == 1 and chr(key[0]).isalnum():
            return f"jump:{chr(key[0])}"
        return "noop"

    # ── scroll / jump ────────────────────────────────────────────────────────

    def _move(self, delta: int) -> None:
        n = len(self._items)
        if not n:
            return
        if self._selected < 0:
            # Nothing highlighted yet: Down enters at the top, Up at the bottom.
            self._selected = 0 if delta > 0 else n - 1
        else:
            self._selected = max(0, min(n - 1, self._selected + delta))
        self._scroll_to_selected()

    def _jump_to(self, ch: str) -> None:
        """Move selection to the next item whose label starts with ch (case-insensitive), rotating."""
        ch_lower = ch.lower()
        candidates = [
            i for i, item in enumerate(self._items)
            if self._display_fn(item).lstrip("-")[:1].lower() == ch_lower
        ]
        if not candidates:
            return
        # Advance to the first candidate strictly after the current position; wrap on exhaustion.
        for idx in candidates:
            if idx > self._selected:
                self._selected = idx
                self._scroll_to_selected()
                return
        self._selected = candidates[0]
        self._scroll_to_selected()

    def _scroll_to_selected(self) -> None:
        if self._selected < self._offset:
            self._offset = self._selected
        elif self._selected >= self._offset + self._height:
            self._offset = self._selected - self._height + 1
