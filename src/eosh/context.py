"""Context management — named collection with a current pointer.

Contexts are kept in most-recently-used order (current first); that order is
what the Ctrl+] picker lists and what decides which context becomes current
when the current one is closed.

Each context stores:
- variables: set/restored on context switch
- cwd: saved/restored on switch
- process_slot: optional running subprocess (for multiplexing)
- history: per-context Up/Down command list (in-memory; global Ctrl-R
  history is stored separately on disk)
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .process import ProcessSlot

_SENTINEL = object()


class ContextState(Enum):
    IDLE = auto()
    RUNNING = auto()
    EXITED = auto()


@dataclass
class Context:
    name: str
    # Environment set in this context; ``None`` = unset here (a variable
    # from the environment eosh started with, removed in this context only).
    variables: dict[str, str | None] = field(default_factory=dict)
    cwd: str = field(default_factory=os.getcwd)
    process_slot: Any = field(default=None, repr=False)  # ProcessSlot | PythonCommandSlot
    history: list[str] = field(default_factory=list, repr=False)
    # PyVar values (name → get() result, None = unset), saved when leaving
    # the context and restored when coming back — like cwd.
    py_values: dict[str, str | None] = field(default_factory=dict, repr=False)

    @property
    def state(self) -> ContextState:
        if self.process_slot is None:
            return ContextState.IDLE
        if self.process_slot.is_alive():
            return ContextState.RUNNING
        return ContextState.EXITED


class ContextManager:
    def __init__(self):
        self.contexts: dict[str, Context] = {}
        self.current_name: str | None = None
        self._display_order: list[str] = []
        self._env_backup: dict[str, str | None] = {}
        self._initial_cwd: str = os.getcwd()
        #: Held while the current context changes and while a context's
        #: variables are read or written — a Python command running in the
        #: background reads and writes its own context's variables from its
        #: thread (``CommandContext``) while the main thread may be switching.
        self.lock = threading.RLock()

    def create(
        self,
        name: str,
        variables: dict[str, str] | None = None,
        history: list[str] | None = None,
    ) -> Context:
        ctx = Context(
            name=name,
            variables=dict(variables or {}),
            cwd=os.getcwd(),
            history=list(history or []),
        )
        self.contexts[name] = ctx
        self._display_order.append(name)
        if self.current_name is None:
            self._activate(name)
        return ctx

    def new(self, name: str) -> Context:
        """Create *name* inheriting the current context's variables and Up/Down
        history (it starts in the current cwd), without switching to it."""
        parent = self.current()
        ctx = self.create(
            name,
            variables=dict(parent.variables) if parent else {},
            history=list(parent.history) if parent else [],
        )
        ctx.py_values = _snapshot_py_vars()
        return ctx

    def _activate(self, name: str) -> None:
        self.current_name = name
        self._display_order = [name] + [n for n in self._display_order if n != name]
        self._restore(self.contexts[name])

    def switch(self, name: str) -> None:
        if name not in self.contexts:
            raise KeyError(f"No context named '{name}'")
        with self.lock:
            self._save_current()
            self._unapply_env()
            self._activate(name)

    def current(self) -> Context | None:
        if self.current_name is None:
            return None
        return self.contexts.get(self.current_name)

    def list_contexts(self) -> list[str]:
        return [n for n in self._display_order if n in self.contexts]

    def remove(self, name: str) -> None:
        """Delete *name*.  Removing the current context makes the most
        recently used remaining one current."""
        if name not in self.contexts:
            raise KeyError(f"No context named '{name}'")
        with self.lock:
            was_current = self.current_name == name
            del self.contexts[name]
            self._display_order = [n for n in self._display_order if n != name]
            if was_current:
                self._unapply_env()
                self.current_name = None
                if self._display_order:
                    self._activate(self._display_order[0])
                else:
                    os.chdir(self._initial_cwd)

    def rename(self, old: str, new: str) -> None:
        """Rename a context. Raises KeyError if *old* is missing or ValueError if *new* exists."""
        if old not in self.contexts:
            raise KeyError(f"No context named '{old}'")
        if new == old:
            return
        if new in self.contexts:
            raise ValueError(f"Context '{new}' already exists")
        ctx = self.contexts.pop(old)
        ctx.name = new
        self.contexts[new] = ctx
        self._display_order = [new if n == old else n for n in self._display_order]
        if self.current_name == old:
            self.current_name = new

    def set_variable(self, key: str, value: str) -> None:
        with self.lock:
            ctx = self.current()
            if ctx is None:
                raise RuntimeError("No active context")
            if key not in self._env_backup:
                self._env_backup[key] = os.environ.get(key)
            ctx.variables[key] = value
            os.environ[key] = value

    def unset_variable(self, key: str) -> None:
        """Unset *key* in the current context only: it comes back when
        another context is entered, and stays unset when this one is."""
        with self.lock:
            ctx = self.current()
            if ctx is not None:
                if key not in self._env_backup:
                    self._env_backup[key] = os.environ.get(key)
                ctx.variables[key] = None
            os.environ.pop(key, None)

    # A context other than the current one keeps its environment in
    # ``Context.variables`` only; ``os.environ`` holds the current one's.
    # Callers hold :attr:`lock` and check :meth:`current` themselves.

    def env_value_in(self, ctx: Context, key: str) -> str | None:
        """Environment variable *key* as *ctx* sees it, current or not."""
        if ctx is self.current():
            return os.environ.get(key)
        if key in ctx.variables:
            return ctx.variables[key]
        # Not set in ctx: the value from before any context set it.
        if key in self._env_backup:
            return self._env_backup[key]
        return os.environ.get(key)

    def environ_in(self, ctx: Context) -> dict[str, str]:
        """The whole environment as *ctx* sees it, current or not."""
        env = dict(os.environ)
        if ctx is self.current():
            return env
        for key, original in self._env_backup.items():   # undo the current one
            if original is None:
                env.pop(key, None)
            else:
                env[key] = original
        for key, value in ctx.variables.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def get_variable(self, key: str) -> str | None:
        ctx = self.current()
        if ctx is None:
            return None
        return ctx.variables.get(key)

    def _save_current(self) -> None:
        if self.current_name is None:
            return
        ctx = self.contexts.get(self.current_name)
        if ctx:
            ctx.cwd = os.getcwd()
            ctx.py_values = _snapshot_py_vars()

    def _restore(self, ctx: Context) -> None:
        os.chdir(ctx.cwd)
        self._apply_env(ctx)
        _restore_py_vars(ctx.py_values)

    def _apply_env(self, ctx: Context) -> None:
        self._env_backup = {}
        for key, value in ctx.variables.items():
            self._env_backup[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _unapply_env(self) -> None:
        for key, original in self._env_backup.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original
        self._env_backup = {}


# ── PyVar values: per-context, kept on the Python side ──────────────────────
#
# Looked up in the variable registry at switch time (imported lazily —
# variables imports completion, which imports this module).


def _py_vars():
    from .variables import PyVar, registry
    return [v for v in registry.all() if isinstance(v, PyVar)]


def _snapshot_py_vars() -> dict[str, str | None]:
    return {v.name: v.get() for v in _py_vars()}


def _restore_py_vars(values: dict[str, str | None]) -> None:
    """Put each PyVar back to *values*.  One the context never saw (it was
    registered later) keeps its current value.  A failing setter is
    reported, not allowed to break the switch."""
    import sys

    for v in _py_vars():
        if v.name not in values:
            continue
        try:
            value = values[v.name]
            if value is None:
                v.unset()
            else:
                v.set(value)
        except Exception as e:
            print(f"eosh: {v.name}: could not restore ({e})", file=sys.stderr)
