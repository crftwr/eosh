"""Color scheme support."""

from __future__ import annotations

from dataclasses import dataclass


def _fg(r: int, g: int, b: int) -> str:
    return f"\033[38;2;{r};{g};{b}m"


def _bg(r: int, g: int, b: int) -> str:
    return f"\033[48;2;{r};{g};{b}m"


@dataclass
class ColorScheme:
    # List rows — picker (and any future scrollable list widget)
    picker_row_bg: tuple[int, int, int] = (68, 68, 68)
    picker_row_fg: tuple[int, int, int] = (220, 220, 220)
    picker_sel_bg: tuple[int, int, int] = (0, 95, 135)
    picker_sel_fg: tuple[int, int, int] = (255, 255, 255)
    # Scroll bar — shared by picker and @watch's body scroll
    scroll_thumb: tuple[int, int, int] = (128, 128, 128)
    scroll_track: tuple[int, int, int] = (48, 48, 48)
    # Status bars — picker bottom hint bar + @watch header/footer
    statusbar_bg: tuple[int, int, int] = (30, 30, 30)
    statusbar_fg: tuple[int, int, int] = (200, 200, 200)


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
}

_active: ColorScheme = SCHEMES["dark"]


def set_color_scheme(scheme: str | ColorScheme) -> None:
    """Set the active color scheme by name or ColorScheme instance.

    Available built-in names: "dark", "light".
    """
    global _active
    if isinstance(scheme, str):
        if scheme not in SCHEMES:
            raise ValueError(f"Unknown color scheme {scheme!r}. Available: {sorted(SCHEMES)}")
        _active = SCHEMES[scheme]
    else:
        _active = scheme


def get_color_scheme() -> ColorScheme:
    return _active
