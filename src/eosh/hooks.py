"""Event hooks — your own functions, called when something happens in the shell.

Each event is a decorator: it registers the function and returns it
unchanged, so a typo in the event name is an ``AttributeError`` while
``config.py`` loads rather than a hook that silently never runs::

    from eosh import hooks

    @hooks.on_directory_changed
    def per_project(old, new):
        ...

    @hooks.on_command_finished
    def ping(line, status, elapsed):
        ...

Rules shared by every event:

* Hooks run in **registration order** — the order ``config.py`` (and the
  modules it imports) registered them.
* A hook that raises is reported on stderr with its traceback and skipped;
  the hooks after it, and the shell, carry on.
* ``reload`` forgets every hook before it runs the config again, so a
  hook is never registered twice and one you deleted is gone.
* Hooks observe.  None of them can veto or rewrite what the shell does,
  except :func:`on_command_not_found`, whose whole job is to claim a line.
"""

from __future__ import annotations

import sys
from typing import Callable

from .user_errors import format_user_exception

_EVENTS = (
    "on_startup",
    "on_exit",
    "on_directory_changed",
    "on_context_switched",
    "on_command_starting",
    "on_command_finished",
    "on_command_not_found",
)

_handlers: dict[str, list[Callable]] = {event: [] for event in _EVENTS}


def _register(event: str, func: Callable) -> Callable:
    if not callable(func):
        raise TypeError(f"hooks.{event} decorates a function, got {func!r}")
    _handlers[event].append(func)
    return func


def on_startup(func: Callable[[], object]) -> Callable:
    """Call ``func()`` once, when the shell starts — after ``config.py`` has
    loaded, before the first prompt.  ``reload`` doesn't call it again."""
    return _register("on_startup", func)


def on_exit(func: Callable[[], object]) -> Callable:
    """Call ``func()`` when the shell is about to exit (``exit``, Ctrl+D)."""
    return _register("on_exit", func)


def on_directory_changed(func: Callable[[str | None, str], object]) -> Callable:
    """Call ``func(old, new)`` when the shell's working directory changes —
    by ``cd``, a context switch (each context has its own), ``source-bash``,
    or a Python command calling ``os.chdir``.

    Called after each command of a line (``cd proj && make`` reports the
    move before ``make`` runs) and after a context switch.  *old* is
    ``None`` if the previous directory had been removed.
    """
    return _register("on_directory_changed", func)


def on_context_switched(func: Callable[[str | None, str | None], object]) -> Callable:
    """Call ``func(old, new)`` with the context names when the current
    context changes — Ctrl+], ``context switch`` / ``new`` / ``close``.

    Reported before the :func:`on_directory_changed` the switch usually
    causes.  Right for a terminal title or a tmux status line: eosh pushes
    the value, so nothing has to poll it.
    """
    return _register("on_context_switched", func)


def on_command_starting(func: Callable[[str], object]) -> Callable:
    """Call ``func(line)`` with the line as typed, just before it runs.

    Observe only: a hook cannot cancel or rewrite the line (an alias or a
    decorator is how you change what runs, visibly).
    """
    return _register("on_command_starting", func)


def on_command_finished(func: Callable[[str, int, float], object]) -> Callable:
    """Call ``func(line, status, elapsed)`` when a line has finished:
    its exit status (the last command's, as for ``&&``) and the seconds it
    took.

    A line you background with Ctrl+] is reported when its work actually
    ends, not when it was parked — and that call comes from a background
    thread, while the prompt may be on screen: post a notification or write
    a log there, but don't print.
    """
    return _register("on_command_finished", func)


def on_command_not_found(func: Callable[[list[str]], bool]) -> Callable:
    """Call ``func(argv)`` when a command is neither registered nor on
    ``PATH``.  Return ``True`` to claim it — the line then succeeds (status
    0) and no later hook is asked; anything else passes it on, and if no
    hook claims it the shell reports ``command not found``.

    Asked for a command run on its own, not for a stage of a pipeline or a
    redirected command.
    """
    return _register("on_command_not_found", func)


# ── Shell side ──────────────────────────────────────────────────────────────

def fire(event: str, *args) -> None:
    """Call every *event* hook with *args*, in registration order."""
    for func in list(_handlers[event]):
        _call(event, func, args)


def fire_until_claimed(event: str, *args) -> bool:
    """Call *event* hooks in order until one returns ``True``; return
    whether one did."""
    return any(_call(event, func, args) is True for func in list(_handlers[event]))


def clear() -> None:
    """Forget every hook (``reload``, before the config runs again)."""
    for funcs in _handlers.values():
        funcs.clear()


def _call(event: str, func: Callable, args: tuple):
    try:
        return func(*args)
    except Exception as e:
        name = getattr(func, "__qualname__", repr(func))
        print(f"eosh: {event} hook {name} failed:", file=sys.stderr)
        print(format_user_exception(e), file=sys.stderr, end="")
        return None
