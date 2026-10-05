"""@bg — run a pipeline in a background context slot.

The wrapped pipeline is started on a dedicated worker slot that becomes
the new context's ``process_slot``.  Control returns to the prompt
immediately; the user can ``Ctrl+]`` into the new context to watch live
output, or ``context kill <name>`` to terminate it.

If ``--as NAME`` is omitted, an auto-generated name (``bg-1``, ``bg-2``,
…) is used.  A positional NAME isn't supported because the decorator
parser stops at the first non-flag token and treats it as the start of
the body — see ``doc/decorators.md`` ("Args syntax").
"""

from __future__ import annotations

import sys

from ..commands import arg
from ..pipeline import Pipeline
from . import registry as decorator_registry, run_in_background


def register() -> None:
    @decorator_registry.decorator(
        name="bg",
        help="Run pipeline in a background context slot.",
        params=[
            arg("--as", "-n", dest="ctx_name", metavar="NAME", default=None,
                help="name the context (auto-named if omitted)"),
        ],
    )
    def bg(pipeline: Pipeline, *, ctx_name: str | None) -> int:
        try:
            resolved = run_in_background(pipeline, name=ctx_name)
        except ValueError as e:
            sys.stderr.write(f"@bg: {e}\n")
            return 2
        sys.stderr.write(f"@bg: started in context '{resolved}' (Ctrl+] to switch in)\n")
        return 0
