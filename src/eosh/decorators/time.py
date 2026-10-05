"""@time — print elapsed wall/user/sys time after the pipeline finishes."""

from __future__ import annotations

import os
import sys
import time as _time

from ..pipeline import Pipeline
from . import registry as decorator_registry


def _format(seconds: float) -> str:
    """Human-friendly mm:ss.ms — matches the bash ``time`` builtin."""
    minutes, secs = divmod(seconds, 60)
    return f"{int(minutes)}m{secs:.3f}s"


def register() -> None:
    @decorator_registry.decorator(
        name="time",
        help="Print elapsed wall/user/sys time after the pipeline finishes.",
    )
    def time_(pipeline: Pipeline) -> int:
        # ``os.times()`` reports cumulative CPU time for this process and
        # any waited-for children.  We diff before/after so any other
        # subprocess work happening in the same shell doesn't pollute the
        # numbers.
        wall_start = _time.monotonic()
        cpu_start = os.times()
        try:
            return pipeline.run()
        finally:
            wall_end = _time.monotonic()
            cpu_end = os.times()
            real = wall_end - wall_start
            user = (cpu_end.user - cpu_start.user) + (cpu_end.children_user - cpu_start.children_user)
            sysn = (cpu_end.system - cpu_start.system) + (cpu_end.children_system - cpu_start.children_system)
            sys.stderr.write(
                f"\nreal\t{_format(real)}\n"
                f"user\t{_format(user)}\n"
                f"sys\t{_format(sysn)}\n"
            )
            sys.stderr.flush()
