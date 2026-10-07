"""Pipeline decorators — wrap a parsed pipeline to change how it runs.

A decorator is a token of the form ``@name [flags]`` at the start of a
line that wraps the rest of the line as a pipeline.  See
``doc/decorators.md`` for the full design.

A decorator **is a command**, registered in the command registry under its
``@``-prefixed name: it gets the same argparse parsing, completion and
``reload`` handling as any command, and its handler receives the wrapped
``Pipeline`` ahead of the parsed flags.  ``@name`` can never collide with
a command, since a token starting with ``@`` is a decorator to the parser::

    from eosh.commands import arg, registry

    @registry.command(
        "@twice",
        params=[arg("-q", "--quiet", action="store_true")],
    )
    def twice(pipeline, *, quiet):
        pipeline.run()
        return pipeline.run()

Built-in decorators are siblings of this module (e.g.
``eosh.decorators.watch``), registered by :func:`register_builtins`.
"""

from __future__ import annotations


def register_builtins() -> None:
    """Register the built-in decorators (``@watch``, ``@time``, ``@retry``,
    ``@quiet``).  Called once while the shell registers its own built-ins,
    so ``mark_builtins`` keeps them across ``reload``."""
    from . import quiet, retry, time, watch

    for module in (watch, time, retry, quiet):
        module.register()
