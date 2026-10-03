"""Sub-cell vertical scrollbar rendering.

A scrollbar thumb drawn by snapping to whole rows jumps a full row at a time
as the user scrolls, so a long list's thumb lurches in coarse steps and often
looks a row short of its track at the extremes. The fix — borrowed from
puikit's ``draw_scrollbar`` — renders the thumb at *eighth-cell* resolution:

* The thumb **body** is a background fill (a plain space with the thumb bg).
  A background covers the whole cell including the terminal's inter-line
  spacing, so a stacked body reads as one continuous bar with no gaps — which
  a stacked ``█`` glyph would not, leaving thin gaps between rows.
* The thumb's two **end caps** are drawn from the lower-block ladder
  (``▁▂▃▄▅▆▇``), so the thumb starts and stops on 1/8-cell boundaries instead
  of a whole row at a time. The bottom cap inverts fg/bg because Unicode has
  no matching *upper*-block ladder.

The geometry (:func:`vbar_cells`) is factored out from the ANSI emission
(:func:`cell_ansi` / :func:`render_column`) so it can be unit-tested without a
terminal, exactly as puikit tests its backends.
"""

from __future__ import annotations

from typing import Iterator

from .colors import _bg, _fg

_RESET = "\033[0m"

#: Sub-cell resolution, in fractions of a cell — eight, the reach of the
#: lower-block ladder.
_SUBCELL = 8

#: LOWER {0..8}/8 BLOCK: index k fills the bottom k eighths of the cell
#: (0 = a plain space, 8 = the full block). Used for the thumb's end caps.
_LOWER_BLOCKS = " ▁▂▃▄▅▆▇█"


def vbar_cells(
    h: int, pos: float, ratio: float, subcell: bool = True
) -> Iterator[tuple[int, str, int]]:
    """Decompose a vertical scrollbar of ``h`` rows into per-row cell kinds.

    Yields ``(row, kind, eighths)`` top to bottom. ``kind`` is ``"track"`` or
    ``"thumb"`` for a whole cell of either, or ``"top"``/``"bottom"`` for a
    partially covered *end cap*, where ``eighths`` is the thumb's share of that
    cell — ``"top"`` means the thumb starts inside the cell and covers its
    lower part, ``"bottom"`` that the thumb ends inside it and covers its upper
    part.

    ``pos`` is the thumb position in ``0..1`` and ``ratio`` the visible
    fraction of the content in ``0..1``. Thumb length and offset are computed
    in eighth-cell units, so the thumb slides in 1/8-row steps instead of
    snapping a whole row at a time. ``subcell=False`` falls back to whole-cell
    rounding and yields no caps (for a terminal without truecolor, where a cap
    would have no way to say which half is the thumb).

    The one-cell minimum length is what keeps both caps out of the *same* cell:
    a cell covered only in its middle has no glyph to draw it with.
    """
    unit = _SUBCELL if subcell else 1
    total = h * unit
    length = max(unit, round(total * ratio))
    start = round((total - length) * pos)
    end = start + length
    for row in range(h):
        top = row * unit
        covered = min(end, top + unit) - max(start, top)
        if covered <= 0:
            yield row, "track", 0
        elif covered >= unit:
            yield row, "thumb", unit
        elif start <= top:
            yield row, "bottom", covered
        else:
            yield row, "top", covered


def cell_ansi(
    kind: str,
    eighths: int,
    thumb: tuple[int, int, int],
    track: tuple[int, int, int],
) -> str:
    """Return the ANSI-styled single-character cell for one scrollbar row."""
    if kind == "thumb":
        return _bg(*thumb) + " " + _RESET
    if kind == "track":
        return _bg(*track) + " " + _RESET
    if kind == "top":
        # Thumb in the cell's lower part: a lower block of exactly that many
        # eighths, thumb-colored (fg), over the track (bg).
        return _fg(*thumb) + _bg(*track) + _LOWER_BLOCKS[eighths] + _RESET
    # "bottom": thumb in the cell's *upper* part. Unicode has no upper-block
    # ladder, so the colors invert — a lower block of the track's remainder,
    # track-colored, over a thumb-colored cell.
    return _fg(*track) + _bg(*thumb) + _LOWER_BLOCKS[_SUBCELL - eighths] + _RESET


def render_column(
    *,
    height: int,
    offset: int,
    visible: int,
    total: int,
    thumb: tuple[int, int, int],
    track: tuple[int, int, int],
) -> list[str]:
    """Return ``height`` ANSI cell strings — one scrollbar column, top to bottom.

    ``visible`` rows of ``total`` items are shown starting at ``offset``. When
    everything fits (``total <= visible``) the whole track is drawn.
    """
    if height <= 0:
        return []
    if total <= visible:
        return [cell_ansi("track", 0, thumb, track)] * height
    ratio = visible / total
    travel = total - visible
    pos = offset / travel if travel > 0 else 0.0
    pos = min(1.0, max(0.0, pos))
    return [cell_ansi(kind, e, thumb, track) for _, kind, e in vbar_cells(height, pos, ratio)]
