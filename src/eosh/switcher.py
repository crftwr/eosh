"""The Ctrl+] context switcher: the inline picker over the contexts, and
what happens around a switch — parking and resuming slots, the separator
line, the exit confirmation when contexts still run something.

A mixin of :class:`~eosh.shell.Shell` (discussion #70): UI over the
:class:`~eosh.context.ContextManager`, kept out of the REPL module.  It uses
``self.context_manager``, ``self._notice_state_change`` and
``self._line_editor`` of the shell it is mixed into.
"""

from __future__ import annotations

import contextlib
import os
import sys

from . import terminal
from . import keys as keymap
from .process import PtySlot
from .slots import _read_from_user


@contextlib.contextmanager
def _cooked_output():
    """Keep ``\n`` → ``\r\n`` on output for the duration, whatever mode the
    terminal is in (the line editor holds it raw while a picker runs)."""
    if not sys.stdin.isatty():
        yield
        return
    fd = sys.stdin.fileno()
    saved = terminal.get_mode(fd)
    terminal.set_raw_input_cooked_output(fd)
    try:
        yield
    finally:
        terminal.restore_mode(fd, saved)


class ContextSwitcher:
    """Shell methods for Ctrl+] — see the module docstring."""

    _PREVIEW_HEIGHT = 3
    # switcher.* actions → the hint shown for each in the status bar.
    _SWITCH_ACTIONS: tuple[tuple[str, str], ...] = (
        ("new", "new"),
        ("delete", "delete"),
        ("rename", "rename"),
    )

    def _show_switch_menu(self) -> tuple[str, bool] | None:
        """Show TUI context picker.

        Returns ``(name, is_new)`` when the user picks a context (or creates one),
        ``None`` on cancel.  Action keys (new / delete / rename) mutate the
        context manager in place and re-open the picker until the user picks a
        context or cancels.
        """
        from .tui import InlineArgPrompt, InlinePicker

        def display_fn(name: str) -> str:
            cur = self.context_manager.current_name
            return ("* " if name == cur else "  ") + name

        def meta_fn(name: str) -> str:
            ctx = self.context_manager.contexts.get(name)
            if ctx is None:
                return ""
            slot = ctx.process_slot
            if slot and slot.is_alive() and slot.argv:
                parts = [os.path.basename(slot.argv[0])] + slot.argv[1:2]
                return " ".join(parts)
            return ""

        def preview_fn(name: str) -> list[str]:
            ctx = self.context_manager.contexts.get(name)
            if ctx is None or ctx.process_slot is None:
                return []
            slot = ctx.process_slot
            if hasattr(slot, "tail_lines"):
                try:
                    return slot.tail_lines(self._PREVIEW_HEIGHT)
                except Exception:
                    return []
            return []

        # switcher.* keys are checked before picker.* ones, so its default
        # Ctrl+N means "new" here, not "down" (Down / Ctrl+P still move).
        key_actions = {seq: action
                       for action, _ in self._SWITCH_ACTIONS
                       for seq in keymap.sequences(f"switcher.{action}")}
        hints = "  ".join(filter(None, (keymap.hint(f"switcher.{action}", label)
                                        for action, label in self._SWITCH_ACTIONS)))

        # Selection persists across re-openings (after delete/rename).
        selected_name: str | None = self.context_manager.current_name
        # One-shot status message shown in the next picker iteration — used to
        # explain why an action was refused (e.g. running process, last context).
        warning: str = ""

        while True:
            contexts = self.context_manager.list_contexts()

            picker = InlinePicker(
                contexts,
                display_fn=display_fn,
                meta_fn=meta_fn,
                max_height=10,
                min_width=32,
                hide_cursor=True,
                status_label=warning,
                status_hints=hints,
                transient_status=True,
                preview_fn=preview_fn,
                preview_height=self._PREVIEW_HEIGHT,
                key_actions=key_actions,
            )
            warning = ""  # one-shot — clear after attaching to the picker
            if selected_name in contexts:
                picker._selected = contexts.index(selected_name)

            selected = picker.run()

            if selected is None:
                return None

            action = picker.action
            if action == "new":
                sys.stdout.write("\n")
                sys.stdout.flush()
                arg_prompt = InlineArgPrompt(label="new context name")
                name = arg_prompt.run()
                sys.stdout.write("\033[1A")
                sys.stdout.flush()
                if not name:
                    selected_name = self.context_manager.current_name
                    continue
                if name in self.context_manager.contexts:
                    warning = f"context '{name}' already exists"
                    selected_name = self.context_manager.current_name
                    continue
                return (name, True)

            if action == "delete":
                if len(self.context_manager.list_contexts()) <= 1:
                    warning = "cannot delete: only one context remaining"
                    continue
                ctx = self.context_manager.contexts.get(selected)
                if ctx is None:
                    continue
                if ctx.process_slot and ctx.process_slot.is_alive():
                    warning = (
                        f"cannot delete '{selected}': process still running "
                        f"(use 'context kill {selected}' first)"
                    )
                    continue
                # Pick a sensible next selection before removing.
                remaining = [n for n in contexts if n != selected]
                idx = contexts.index(selected)
                next_sel = remaining[min(idx, len(remaining) - 1)] if remaining else None
                self.context_manager.remove(selected)
                # If we deleted the current context, remove() switched the
                # pointer for us; align selection with the new current so the
                # picker reopens with that highlighted.
                selected_name = self.context_manager.current_name or next_sel
                continue

            if action == "rename":
                old = selected
                sys.stdout.write("\n")
                sys.stdout.flush()
                arg_prompt = InlineArgPrompt(
                    label=f"rename '{old}' to",
                    initial=old,
                )
                new = arg_prompt.run()
                sys.stdout.write("\033[1A")
                sys.stdout.flush()
                if new and new != old:
                    if new in self.context_manager.contexts:
                        warning = f"context '{new}' already exists"
                        selected_name = old
                    else:
                        try:
                            self.context_manager.rename(old, new)
                            selected_name = new
                        except (KeyError, ValueError) as e:
                            warning = str(e)
                            selected_name = old
                else:
                    selected_name = old
                continue

            # No action key — user pressed Enter to accept the selection.
            if selected == self.context_manager.current_name:
                return None
            return (selected, False)

    def _resume_pty_slot(self, slot: PtySlot) -> None:
        """Restore terminal modes and re-activate a backgrounded PTY slot.

        Used both when resuming after a context switch and when the user
        cancels the switch picker.  Two strategies, picked by alt-screen
        state:
          • Alt-screen TUIs (vi, tfm, less): rely on the app's own
            redraw.  We force a SIGWINCH-driven repaint by wiggling the
            PTY size: (1, 1) then the real (rows, cols).  This guarantees
            ncurses sees a real resize event (not a no-op short-circuit)
            and triggers KEY_RESIZE → full clear+redraw.  Snapshot replay
            of the buffer is unsafe — paint commands are size-dependent
            and the deque-bounded history can be partial, leaving stale
            cells (selection markers, mis-positioned separators) that
            ncurses' shadow won't consider dirty.
          • Streaming output (logs, build): activate(replay_missed=True)
            atomically prints bytes that arrived while inactive and
            clears the missed-buffer under the buffer lock.
        """
        restore_seq = slot.restore_terminal_modes()
        if restore_seq:
            sys.stdout.write(restore_seq)
            sys.stdout.flush()
        if slot.terminal_modes.get("alt_screen", False):
            slot.activate()
            # Force the app to do a full clear+redraw.  The freshly-
            # restored alt-screen is blank, but the app's internal shadow
            # still matches what was on screen pre-suspend, so a passive
            # resume leaves the screen empty until the app repaints.
            #
            # Send Ctrl+L (\x0c, FF) — the universal TUI convention for
            # "force full redraw".  Bound by default in vim, nvim, nano,
            # less, mc, emacs, htop, etc.  Apps that don't handle it
            # (and aren't simply ignoring it) should — it's the standard
            # contract.  Wiggling the PTY size to fire SIGWINCH/KEY_RESIZE
            # works around non-conforming apps but introduces its own
            # artifacts (vim's per-column diff redraw confusion when the
            # file exceeds viewport height), so we no longer do that.
            slot.write_stdin(b"\x0c")
        else:
            slot.activate(replay_missed=True)

    def _emit_context_separator(self, new_name: str) -> None:
        """Print a dim full-width separator labelled with the new context name.

        Called when the active context changes (by Ctrl+] in either the line
        editor or a running process) so the new prompt is visually distinct
        from the previous context's output.  Cursor is assumed to be at column
        0 of an otherwise blank line; the separator ends with ``\\r\\n`` so the
        cursor lands on a fresh line below.
        """
        try:
            cols = os.get_terminal_size().columns
        except OSError:
            cols = 80
        from .lineedit import _wcswidth
        label = f"─ {new_name} "
        label_w = _wcswidth(label)
        fill = "─" * max(1, cols - label_w)
        sys.stdout.write(f"\r\033[2m{label}{fill}\033[22m\r\n")
        sys.stdout.flush()

    def _handle_switch(self) -> tuple[bool, str | None]:
        """Handle Ctrl+] switch request.

        Returns ``(needs_forward, new_context_name)``.  ``needs_forward`` is
        True when lineedit should exit (CONTEXT_CHANGED_SENTINEL) so the run()
        loop can take over — either to replay buffered output or to enter
        forwarding mode.  ``new_context_name`` is the name of the now-active
        context when it differs from the one we entered with (lineedit uses it
        to decide whether to preserve the old prompt); ``None`` when the
        context did not change (plain picker cancel).

        When the context changes, a dim separator labelled with the new
        context's name is printed before returning, so all call sites
        (line-editor Ctrl+] and forwarding-mode Ctrl+] from vi/etc.) get a
        consistent visual break between contexts.
        """
        ctx = self.context_manager.current()
        original_name = ctx.name if ctx else None
        if ctx and ctx.process_slot:
            slot = ctx.process_slot
            slot_alive = slot.is_alive()
            # Snapshot the cursor-column hint *before* deactivating — once
            # deactivated, new output is buffered (not displayed), but
            # ``cursor_at_col0()`` reflects what is already on screen, so
            # the ordering is not strictly required.  Done first anyway for
            # symmetry with how the line-editor path measures its caret.
            at_col0 = slot.cursor_at_col0() if slot_alive else True
            slot.deactivate()
            if slot_alive and not at_col0:
                # The picker is anchored at the current cursor position and
                # its first render does ``\r\033[J`` — clearing the anchor
                # row from column 0 onwards. If the subprocess's last write
                # did not end with ``\n`` / ``\r``, the cursor sits mid-line
                # and the picker would erase that last output line. Move
                # down to a fresh row first.  When the cursor is already at
                # column 0 (output ended with ``\n`` — the common case for
                # streaming tools like ``ping``), skip this so we don't add
                # a blank row.  The line-editor path (``_do_inline_switch``)
                # has its own pre-picker positioning and never reaches here
                # for a live slot.
                sys.stdout.write("\r\n")
                sys.stdout.flush()

        result = self._show_switch_menu()

        def _finish(needs_forward: bool) -> tuple[bool, str | None]:
            new_ctx = self.context_manager.current()
            new_name = new_ctx.name if new_ctx else None
            if new_name != original_name:
                if new_name is not None:
                    self._emit_context_separator(new_name)
                # Between the separator and the new prompt.  The line editor
                # may hold the terminal raw here; a hook prints normally.
                with _cooked_output():
                    self._notice_state_change()
                return (needs_forward, new_name)
            return (needs_forward, None)

        if result is None:
            # User cancelled.  If a menu action (pop) changed the current
            # context, behave as if the user had switched: let run()'s resume
            # path do the right thing for the new context's slot.  Otherwise
            # re-activate the slot so its reader streams output again.
            new_ctx = self.context_manager.current()
            if new_ctx is None or (new_ctx.name != original_name):
                needs_forward = bool(new_ctx and new_ctx.process_slot)
                return _finish(needs_forward)
            if ctx and ctx.process_slot and ctx.process_slot.is_alive():
                self._resume_pty_slot(ctx.process_slot)
            return (False, None)

        target_name, is_new = result

        if is_new:
            self.context_manager.new(target_name)
        self.context_manager.switch(target_name)

        new_ctx = self.context_manager.current()
        # Don't activate a new-context slot here — leave that to run()'s resume
        # path so it can choose between snapshot replay (TUI) and missed-buffer
        # flush (streaming) based on alt-screen state.  Returning True makes
        # lineedit exit so run() takes over immediately rather than waiting for
        # the user to press Enter.
        needs_forward = bool(new_ctx and new_ctx.process_slot)
        return _finish(needs_forward)

    def _running_contexts(self) -> list[tuple[str, list[str]]]:
        return [
            (name, ctx.process_slot.argv)
            for name, ctx in self.context_manager.contexts.items()
            if ctx.process_slot and ctx.process_slot.is_alive()
        ]

    def _confirm_exit(self, running: list[tuple[str, list[str]]]) -> bool:
        print(f"There {'is' if len(running) == 1 else 'are'} {len(running)} context(s) with running processes:")
        for name, argv in running:
            print(f"  {name}: {' '.join(argv)}")
        try:
            answer = _read_from_user("Exit anyway? [y/N] ", block=False).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return answer in ("y", "yes")
