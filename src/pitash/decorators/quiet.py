"""@quiet — discard the wrapped pipeline's stdout (and optionally stderr)."""

from __future__ import annotations

import os
from dataclasses import replace

from ..commands import arg
from ..pipeline import Pipeline, Redirect
from . import registry as decorator_registry


def _silenced(pipeline: Pipeline, *, also_stderr: bool) -> Pipeline:
    """Return a copy of *pipeline* whose last stage's stdout (and maybe
    stderr) is redirected to the platform null device.

    The AST is rebuilt rather than mutated so the user's original
    Pipeline is left intact (matches ``@watch``'s pattern).
    """
    new_stages = list(pipeline.stages)
    last = new_stages[-1]
    extras = [Redirect(kind=">", target=os.devnull)]
    if also_stderr:
        extras.append(Redirect(kind="2>&1", target="1"))
    new_stages[-1] = replace(last, redirects=list(last.redirects) + extras)
    return Pipeline(stages=new_stages)


def register() -> None:
    @decorator_registry.decorator(
        name="quiet",
        help="Discard pipeline stdout (and stderr with --stderr).",
        params=[
            arg("--stderr", action="store_true",
                help="also discard stderr"),
        ],
    )
    def quiet(pipeline: Pipeline, *, stderr: bool) -> int:
        return _silenced(pipeline, also_stderr=stderr).run()
