"""Color scheme support."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import IO, Union

#: The terminal's own foreground / background color.  A scheme may use them
#: in place of an RGB triple; a cell painted TERM_BG on TERM_FG is drawn in
#: reverse video, which is how the "mono" scheme marks a selection without
#: emitting any color.
TERM_FG = "terminal-fg"
TERM_BG = "terminal-bg"

Color = Union[tuple[int, int, int], str]


def _fg(r: int, g: int, b: int) -> str:
    return f"\033[38;2;{r};{g};{b}m"


def _bg(r: int, g: int, b: int) -> str:
    return f"\033[48;2;{r};{g};{b}m"


def paint(fg: Color, bg: Color) -> str:
    """The SGR prefix for text in *fg* over *bg*.

    TERM_FG / TERM_BG emit nothing (the terminal's colors are already in
    effect after a reset).  When the text should be in the terminal's
    background color, or the cell in its foreground color, the pair is
    drawn in reverse video, with any RGB side swapped to match.
    """
    if fg == TERM_BG or bg == TERM_FG:
        fg, bg = bg, fg
        prefix = "\033[7m"
    else:
        prefix = ""
    if not isinstance(fg, str):
        prefix += _fg(*fg)
    if not isinstance(bg, str):
        prefix += _bg(*bg)
    return prefix


def color_enabled(stream: IO | None = None) -> bool:
    """Whether output may carry ANSI colors: False when ``NO_COLOR`` is set
    to a non-empty value (https://no-color.org), and — given *stream* — when
    that stream isn't a terminal.  For a command's own output; the pickers
    follow the color scheme instead (see :func:`get_color_scheme`)."""
    if os.environ.get("NO_COLOR"):
        return False
    if stream is None:
        return True
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


@dataclass
class ColorScheme:
    # List rows — picker (and any future scrollable list widget)
    picker_row_bg: Color = (68, 68, 68)
    picker_row_fg: Color = (220, 220, 220)
    picker_sel_bg: Color = (0, 95, 135)
    picker_sel_fg: Color = (255, 255, 255)
    # Scroll bar — the picker's
    scroll_thumb: Color = (128, 128, 128)
    scroll_track: Color = (48, 48, 48)
    # Status bar — the picker's bottom hint bar
    statusbar_bg: Color = (30, 30, 30)
    statusbar_fg: Color = (200, 200, 200)


SCHEMES: dict[str, ColorScheme] = {
    "dark": ColorScheme(),
    "light": ColorScheme(
        picker_row_bg=(220, 220, 220),
        picker_row_fg=(30, 30, 30),
        picker_sel_bg=(0, 100, 180),
        picker_sel_fg=(255, 255, 255),
        scroll_thumb=(160, 160, 160),
        scroll_track=(200, 200, 200),
        statusbar_bg=(195, 210, 225),
        statusbar_fg=(60, 60, 80),
    ),
    # No color at all: the terminal's own colors, reverse video where the
    # others fill a background (selection, status bar, scrollbar thumb).
    "mono": ColorScheme(
        picker_row_bg=TERM_BG,
        picker_row_fg=TERM_FG,
        picker_sel_bg=TERM_FG,
        picker_sel_fg=TERM_BG,
        scroll_thumb=TERM_FG,
        scroll_track=TERM_BG,
        statusbar_bg=TERM_FG,
        statusbar_fg=TERM_BG,
    ),
}

#: Set by set_color_scheme(); None picks automatically.
_chosen: ColorScheme | None = None


def set_color_scheme(scheme: str | ColorScheme | None) -> None:
    """Set the active color scheme by name or ColorScheme instance.

    Available built-in names: "dark", "light", "mono".  None goes back to
    the automatic choice (see :func:`get_color_scheme`).
    """
    global _chosen
    if isinstance(scheme, str):
        if scheme not in SCHEMES:
            raise ValueError(f"Unknown color scheme {scheme!r}. Available: {sorted(SCHEMES)}")
        _chosen = SCHEMES[scheme]
    else:
        _chosen = scheme


def get_color_scheme() -> ColorScheme:
    """The scheme set with set_color_scheme(); otherwise "mono" when
    ``NO_COLOR`` is set (read on every call, so ``var NO_COLOR=1`` applies
    at once) and "dark" when it isn't.  An explicit choice wins over
    NO_COLOR, as https://no-color.org allows for user configuration."""
    if _chosen is not None:
        return _chosen
    return SCHEMES["mono" if os.environ.get("NO_COLOR") else "dark"]
