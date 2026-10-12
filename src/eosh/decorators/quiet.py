"""@quiet — discard the wrapped body's stdout (and optionally stderr)."""

from __future__ import annotations

import os
from dataclasses import replace

from ..commands import arg
from ..pipeline import Pipeline, Redirect, Sequence
from ..commands import registry as command_registry


def _silenced(body: Sequence, *, also_stderr: bool) -> Sequence:
    """Return a copy of *body* whose pipelines each send their last stage's
    stdout (and maybe stderr) to the platform null device.

    The AST is rebuilt rather than mutated so the user's original body is
    left intact (matches ``@watch``'s pattern).
    """
    def silence(pipeline: Pipeline) -> Pipeline:
        new_stages = list(pipeline.stages)
        last = new_stages[-1]
        extras = [Redirect(kind=">", target=os.devnull)]
        if also_stderr:
            extras.append(Redirect(kind="2>&1", target="1"))
        new_stages[-1] = replace(last, redirects=list(last.redirects) + extras)
        return Pipeline(stages=new_stages)

    return body.map(silence)


def register() -> None:
    @command_registry.command(
        "@quiet",
        help="Discard pipeline stdout (and stderr with --stderr).",
        params=[
            arg("--stderr", action="store_true",
                help="also discard stderr"),
        ],
    )
    def quiet(body: Sequence, *, stderr: bool) -> int:
        return _silenced(body, also_stderr=stderr).run()
