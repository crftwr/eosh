"""Customizable prompt generation."""

from __future__ import annotations

import os
from datetime import datetime
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .context import ContextManager

PromptFunc = Callable[["ContextManager"], str]

_prompt_func: PromptFunc | None = None


def set_prompt(func: PromptFunc | None) -> None:
    """Set a custom prompt function, or None to reset to default.

    The function receives the ContextManager and returns the prompt string.
    """
    global _prompt_func
    _prompt_func = func


def get_prompt_func() -> PromptFunc:
    """Return the active prompt function (custom or default)."""
    return _prompt_func or default_prompt


def default_prompt(context_manager: "ContextManager") -> str:
    """Default prompt: [context] parent/cwd HH:MM:SS [bg:N]> in plain text.

    Customize colors and layout by passing your own function to set_prompt().
    """
    parts = []

    ctx = context_manager.current()
    if ctx and ctx.name != "default":
        parts.append(f"[{ctx.name}]")

    cwd = os.getcwd()
    home = os.path.expanduser("~")
    if cwd == home:
        short_path = "~"
    elif cwd.startswith(home + os.sep):
        rel = cwd[len(home) + 1:]
        rel_parts = rel.split(os.sep)
        if len(rel_parts) <= 2:
            short_path = "~/" + rel
        else:
            short_path = "/".join(rel_parts[-2:])
    else:
        abs_parts = cwd.lstrip(os.sep).split(os.sep)
        if len(abs_parts) <= 2:
            # POSIX absolute paths need their leading separator restored;
            # Windows paths carry their drive letter (C:\...) already.
            prefix = "" if os.name == "nt" else "/"
            short_path = prefix + "/".join(abs_parts)
        else:
            short_path = "/".join(abs_parts[-2:])
    # The shell uses '/' as the canonical separator on every platform.
    short_path = short_path.replace(os.sep, "/") if os.altsep else short_path

    timestamp = datetime.now().strftime("%H:%M:%S")
    parts.append(short_path)
    parts.append(timestamp)

    bg_count = 0
    current_name = context_manager.current_name
    for name, c in context_manager.contexts.items():
        if name != current_name and c.process_slot and c.process_slot.is_alive():
            bg_count += 1
    if bg_count:
        parts.append(f"[bg:{bg_count}]")

    return " ".join(parts) + "> "
