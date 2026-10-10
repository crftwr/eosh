"""Color schemes, TERM_FG / TERM_BG, and NO_COLOR (https://no-color.org).

NO_COLOR is not a mode: it only changes which scheme is picked when the
config chose none, and the "mono" scheme draws with reverse video.
"""

import io

import pytest

from eosh import colors
from eosh.colors import (
    SCHEMES,
    TERM_BG,
    TERM_FG,
    color_enabled,
    get_color_scheme,
    paint,
    set_color_scheme,
)
from eosh.scrollbar import cell_ansi
from eosh.tui import _statusbar


@pytest.fixture(autouse=True)
def _automatic_scheme(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    set_color_scheme(None)
    yield
    set_color_scheme(None)


def _has_color(s: str) -> bool:
    return "\033[38;" in s or "\033[48;" in s


def test_scheme_defaults_to_dark():
    assert get_color_scheme() is SCHEMES["dark"]


def test_no_color_picks_mono(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert get_color_scheme() is SCHEMES["mono"]


def test_empty_no_color_is_ignored(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "")
    assert get_color_scheme() is SCHEMES["dark"]
    assert color_enabled()


def test_explicit_scheme_wins_over_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    set_color_scheme("light")
    assert get_color_scheme() is SCHEMES["light"]


def test_paint_rgb():
    assert paint((1, 2, 3), (4, 5, 6)) == colors._fg(1, 2, 3) + colors._bg(4, 5, 6)


def test_paint_terminal_colors():
    assert paint(TERM_FG, TERM_BG) == ""
    assert paint(TERM_BG, TERM_FG) == "\033[7m"


def test_paint_reverse_swaps_an_rgb_side():
    # Text in the terminal's background color on a red cell: reverse video
    # with red as the (swapped) foreground.
    assert paint(TERM_BG, (255, 0, 0)) == "\033[7m" + colors._fg(255, 0, 0)


def test_mono_status_bar_is_reverse_video(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    bar = _statusbar("label", "hints", 80)
    assert bar.startswith("\033[7m") and not _has_color(bar)


def test_mono_scrollbar_has_no_color():
    mono = SCHEMES["mono"]
    cells = {kind: cell_ansi(kind, 4, mono.scroll_thumb, mono.scroll_track)
             for kind in ("thumb", "track", "top", "bottom")}
    assert not any(_has_color(c) for c in cells.values())
    assert "\033[7m" in cells["thumb"] and "\033[7m" in cells["bottom"]
    assert "\033[7m" not in cells["track"] and "\033[7m" not in cells["top"]


def test_color_enabled_checks_the_stream(monkeypatch):
    assert not color_enabled(io.StringIO())
    monkeypatch.setenv("NO_COLOR", "1")
    assert not color_enabled()
