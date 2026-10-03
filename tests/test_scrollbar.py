"""Sub-cell vertical scrollbar geometry and rendering.

The thumb's *body* is a background fill (seamless across the terminal's line
spacing) while its two end caps come from the lower-block ladder — so the thumb
slides in 1/8-row steps instead of jumping a whole row at a time. The bottom cap
inverts fg/bg, Unicode having no upper-block ladder to match. Ported from
puikit's test_scrollbar_subcell.py.
"""

from pitash.colors import _bg, _fg
from pitash.scrollbar import (
    _LOWER_BLOCKS,
    _SUBCELL,
    cell_ansi,
    render_column,
    vbar_cells,
)

THUMB = (200, 200, 200)
TRACK = (40, 40, 40)


# h=8, ratio=0.5 -> a 32-eighth thumb in a 64-eighth track; pos=0.625 starts it
# at eighth 20 — halfway into row 2 — and ends it halfway into row 6.
_HALF_BAR = dict(h=8, pos=0.625, ratio=0.5)


def _kinds(h, pos, ratio, subcell=True):
    return list(vbar_cells(h, pos, ratio, subcell))


def test_thumb_body_is_a_background_fill():
    # A thumb covering whole cells is a plain space with the thumb bg: the fill
    # covers the inter-line spacing a stacked block glyph would leave gapped.
    cells = _kinds(10, 0.0, 0.5)
    assert [k for _, k, _ in cells] == ["thumb"] * 5 + ["track"] * 5


def test_top_cap_is_a_lower_block_in_the_thumb_color():
    # Row 2 is the top cap: the thumb covers its lower half.
    row, kind, eighths = _kinds(**_HALF_BAR)[2]
    assert (kind, eighths) == ("top", 4)
    ansi = cell_ansi(kind, eighths, THUMB, TRACK)
    assert _LOWER_BLOCKS[4] == "▄" and "▄" in ansi
    # thumb below (fg), track above (bg)
    assert _fg(*THUMB) in ansi and _bg(*TRACK) in ansi


def test_bottom_cap_inverts_foreground_and_background():
    # The thumb ends halfway into row 6, whose *upper* half it covers. No
    # upper-block ladder exists, so the cap is the track's remaining lower 4/8
    # painted in the track color over a thumb-colored cell.
    row, kind, eighths = _kinds(**_HALF_BAR)[6]
    assert (kind, eighths) == ("bottom", 4)
    ansi = cell_ansi(kind, eighths, THUMB, TRACK)
    assert "▄" in ansi  # _LOWER_BLOCKS[8-4]
    assert _fg(*TRACK) in ansi and _bg(*THUMB) in ansi  # inverted


def test_thumb_moves_in_sub_row_steps():
    # The whole point: two positions a fraction of a row apart render
    # differently. With whole-cell rounding both of these snapped to row 1.
    assert _kinds(8, 0.20, 0.25) != _kinds(8, 0.25, 0.25)


def test_ends_are_flush_at_the_extremes():
    # pos=0 starts flush with the top edge, pos=1 ends flush with the bottom —
    # whole thumb cells at either end, no partial cap glyph.
    top = _kinds(12, 0.0, 0.25)
    assert [k for _, k, _ in top][:3] == ["thumb"] * 3
    assert all(k == "track" for _, k, _ in top[3:])
    bottom = _kinds(12, 1.0, 0.25)
    assert [k for _, k, _ in bottom][-3:] == ["thumb"] * 3
    assert all(k == "track" for _, k, _ in bottom[:-3])


def test_short_thumb_keeps_its_one_cell_minimum():
    # A tiny ratio still yields a full cell of coverage, which is what keeps
    # both caps out of the same cell — a cell covered only in its middle has no
    # glyph to draw it.
    for pos in (0.0, 0.37, 0.5, 1.0):
        cells = _kinds(20, pos, 0.001)
        painted = sum(n for _, kind, n in cells if kind != "track")
        assert painted == _SUBCELL


def test_full_ratio_fills_the_track():
    assert all(kind == "thumb" for _, kind, _ in _kinds(6, 0.0, 1.0))


def test_no_color_falls_back_to_whole_cells():
    # Without sub-cell there is no cap to draw with two colors in one cell.
    for pos in (0.0, 0.33, 0.5, 0.9, 1.0):
        kinds = {kind for _, kind, _ in _kinds(10, pos, 0.37, subcell=False)}
        assert kinds <= {"thumb", "track"}


def test_geometry_is_total_and_contiguous():
    for h in (1, 2, 5, 10, 37):
        for pos in (0.0, 0.1, 0.5, 0.83, 1.0):
            for ratio in (0.01, 0.2, 0.5, 0.99, 1.0):
                cells = _kinds(h, pos, ratio)
                assert [row for row, _, _ in cells] == list(range(h))
                covered = [i for i, (_, k, _) in enumerate(cells) if k != "track"]
                assert covered == list(range(covered[0], covered[-1] + 1))
                for i, (_, kind, _) in enumerate(cells):
                    if kind == "top":
                        assert i == covered[0]
                    elif kind == "bottom":
                        assert i == covered[-1]


def test_render_column_all_visible_is_full_track():
    col = render_column(height=5, offset=0, visible=5, total=5, thumb=THUMB, track=TRACK)
    assert col == [_bg(*TRACK) + " " + "\033[0m"] * 5


def test_render_column_thumb_at_top_and_bottom():
    top = render_column(height=10, offset=0, visible=10, total=100, thumb=THUMB, track=TRACK)
    assert _bg(*THUMB) in top[0]
    bottom = render_column(height=10, offset=90, visible=10, total=100, thumb=THUMB, track=TRACK)
    assert _bg(*THUMB) in bottom[-1]


def test_render_column_length_matches_height():
    for h in (1, 3, 8, 25):
        col = render_column(height=h, offset=3, visible=h, total=h * 4, thumb=THUMB, track=TRACK)
        assert len(col) == h
