"""Report an exception raised by user code (config.py and the modules it imports).

The full traceback of a config-load failure is mostly eosh's own loader and
``<frozen importlib…>`` frames; what the user needs is the chain through
*their* files — which recipe, which helper module, which line.  A bare
``str(e)`` is worse still: ``No module named 'requets'`` names neither the
file nor the line holding the typo.
"""

from __future__ import annotations

import traceback
from pathlib import Path

_EOSH_DIR = str(Path(__file__).resolve().parent)


def _is_internal(filename: str) -> bool:
    if filename.startswith("<frozen"):
        return True
    try:
        return str(Path(filename).resolve()).startswith(_EOSH_DIR)
    except OSError:
        return False


def format_user_exception(exc: BaseException) -> str:
    """Return *exc* formatted as a traceback with eosh-internal frames removed.

    If every frame is internal (the failure is eosh's own), the traceback is
    kept whole rather than reduced to a bare message — except for a
    ``SyntaxError``, whose message already names the user's file and line.
    """
    frames = traceback.extract_tb(exc.__traceback__)
    user_frames = [f for f in frames if not _is_internal(f.filename)]
    if not user_frames and isinstance(exc, SyntaxError):
        return "".join(traceback.format_exception_only(exc))
    lines = ["Traceback (most recent call last):\n"]
    lines += traceback.format_list(user_frames or frames)
    lines += traceback.format_exception_only(exc)
    return "".join(lines)
