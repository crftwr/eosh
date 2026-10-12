"""What the running code is part of: one record instead of scattered globals.

A Python command, a decorator's body or a line's driver runs on a thread of
its own, and what it may do depends on where it runs: on a line's
:class:`~eosh.job.PipelineSlot` or not, in which context, as a stage of a
pipeline whose stdio are pipes or on a terminal.  That used to be spread
over a module global (the executor ``Pipeline.run`` called) and two
``threading.local`` objects (``_job_local``, ``_in_pipeline``) set and
cleared field by field.  It is now one frozen :class:`Execution`, held in a
:class:`~contextvars.ContextVar`:

* :func:`current` reads it;
* :func:`entered` changes it for the duration of a ``with`` block;
* :func:`thread` makes a thread that starts from its parent's record, so a
  stage inherits the shell that runs it, and states what it is on top.

A ``ContextVar`` rather than a thread-local because the record follows the
work, not the thread — and so two :class:`~eosh.shell.Shell` instances in
one process (tests) each run their own bodies.
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Callable, Iterator

if TYPE_CHECKING:
    from .command_context import CommandContext
    from .job import PipelineSlot


@dataclass(frozen=True)
class Execution:
    """Where the current code runs."""

    #: Runs a decorator's body (``Pipeline.run`` / ``Sequence.run``) — the
    #: shell's ``_run_body``.  None outside a shell.
    runner: Callable[..., int] | None = None
    #: The slot the line runs on; None on the main thread, or without a
    #: terminal (no slots at all).
    job: PipelineSlot | None = None
    #: The context the work belongs to — the line's, or a pipeline stage's
    #: subshell — when it isn't simply the current one.
    ctx: CommandContext | None = None
    #: This thread drives the line: it runs the pipelines one after another
    #: and waits for their stages (the ones Ctrl+C interrupts).
    driver: bool = False
    #: A stage of a pipeline, on a thread of its own with its stdio rebound.
    stage: bool = False
    #: … whose stdin and stdout are both a terminal (a slot's PTY): it may
    #: still talk to the user.
    on_terminal: bool = False

    @property
    def piped(self) -> bool:
        """A stage whose stdin or stdout is a pipe: no one to ask."""
        return self.stage and not self.on_terminal


_current: contextvars.ContextVar[Execution] = contextvars.ContextVar(
    "eosh_execution", default=Execution())


def current() -> Execution:
    """The record of the code running now."""
    return _current.get()


def set_runner(runner: Callable[..., int] | None) -> None:
    """Make *runner* this context's body runner from now on (``Shell()``
    does, so ``body.run()`` works outside a line, too)."""
    _current.set(replace(current(), runner=runner))


@contextlib.contextmanager
def entered(**changes) -> Iterator[Execution]:
    """The current record with *changes*, until the block ends."""
    token = _current.set(replace(current(), **changes))
    try:
        yield _current.get()
    finally:
        _current.reset(token)


def thread(target: Callable[[], object], *, name: str, **changes) -> threading.Thread:
    """A daemon thread (not started) that runs *target* with the caller's
    record plus *changes* — a stage keeps the shell and slot it was started
    from, and says it is a stage."""
    record = replace(current(), **changes)

    def run() -> None:
        _current.set(record)
        target()

    return threading.Thread(target=contextvars.copy_context().run, args=(run,),
                            daemon=True, name=name)
