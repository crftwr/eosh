"""@retry — re-run a pipeline on non-zero exit, up to N times."""

from __future__ import annotations

import sys
import time

from ..commands import arg
from ..pipeline import Pipeline
from . import registry as decorator_registry


def register() -> None:
    @decorator_registry.decorator(
        name="retry",
        help="Re-run pipeline until exit-status 0, up to -n attempts.",
        params=[
            arg("-n", "--attempts", type=int, default=3, metavar="N",
                help="maximum attempts (default 3)"),
            arg("--delay", type=float, default=0.0, metavar="SEC",
                help="seconds to sleep between attempts (default 0)"),
        ],
    )
    def retry(pipeline: Pipeline, *, attempts: int, delay: float) -> int:
        if attempts < 1:
            sys.stderr.write("@retry: --attempts must be >= 1\n")
            return 2
        last = 1
        for i in range(1, attempts + 1):
            try:
                last = pipeline.run()
            except KeyboardInterrupt:
                return 130
            if last == 0:
                return 0
            if i < attempts:
                sys.stderr.write(
                    f"@retry: attempt {i}/{attempts} exited {last}, retrying...\n"
                )
                if delay > 0:
                    try:
                        time.sleep(delay)
                    except KeyboardInterrupt:
                        return 130
        sys.stderr.write(f"@retry: gave up after {attempts} attempts (last exit {last})\n")
        return last
