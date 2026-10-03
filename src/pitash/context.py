"""Context management — named collection with current pointer and push/pop stack.

Each context stores:
- variables: set/restored on context switch
- cwd: saved/restored on switch
- process_slot: optional running subprocess (for multiplexing)
- history: per-context Up/Down command list (in-memory; global Ctrl-R
  history is stored separately on disk)
"""

from __future__ import annotations

import os
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
    variables: dict[str, str] = field(default_factory=dict)
    cwd: str = field(default_factory=os.getcwd)
    process_slot: Any = field(default=None, repr=False)  # ProcessSlot | PythonCommandSlot
    history: list[str] = field(default_factory=list, repr=False)

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
        self.stack: list[str] = []
        self._display_order: list[str] = []
        self._env_backup: dict[str, str | None] = {}
        self._initial_cwd: str = os.getcwd()

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

    def _activate(self, name: str) -> None:
        self.current_name = name
        self._display_order = [name] + [n for n in self._display_order if n != name]
        self._restore(self.contexts[name])

    def switch(self, name: str) -> None:
        if name not in self.contexts:
            raise KeyError(f"No context named '{name}'")
        self._save_current()
        self._unapply_env()
        self._activate(name)

    def push(self, name: str) -> None:
        if self.current_name is not None:
            self.stack.append(self.current_name)
        if name not in self.contexts:
            raise KeyError(f"No context named '{name}'")
        self._save_current()
        self._unapply_env()
        self._activate(name)

    def pop(self) -> Context | None:
        self._save_current()
        self._unapply_env()
        if not self.stack:
            self.current_name = None
            os.chdir(self._initial_cwd)
            return None
        prev_name = self.stack.pop()
        self._activate(prev_name)
        return self.contexts.get(prev_name)

    def current(self) -> Context | None:
        if self.current_name is None:
            return None
        return self.contexts.get(self.current_name)

    def list_contexts(self) -> list[str]:
        return [n for n in self._display_order if n in self.contexts]

    def remove(self, name: str) -> None:
        if name not in self.contexts:
            raise KeyError(f"No context named '{name}'")
        was_current = self.current_name == name
        del self.contexts[name]
        self._display_order = [n for n in self._display_order if n != name]
        self.stack = [n for n in self.stack if n != name]
        if was_current:
            self._unapply_env()
            self.current_name = self.stack[-1] if self.stack else None
            if self.current_name:
                self._restore(self.contexts[self.current_name])
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
        self.stack = [new if n == old else n for n in self.stack]
        if self.current_name == old:
            self.current_name = new

    def set_variable(self, key: str, value: str) -> None:
        ctx = self.current()
        if ctx is None:
            raise RuntimeError("No active context")
        if key not in self._env_backup:
            self._env_backup[key] = os.environ.get(key)
        ctx.variables[key] = value
        os.environ[key] = value

    def unset_variable(self, key: str) -> None:
        ctx = self.current()
        if ctx is not None:
            ctx.variables.pop(key, None)
        os.environ.pop(key, None)

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

    def _restore(self, ctx: Context) -> None:
        os.chdir(ctx.cwd)
        self._apply_env(ctx)

    def _apply_env(self, ctx: Context) -> None:
        self._env_backup = {}
        for key, value in ctx.variables.items():
            self._env_backup[key] = os.environ.get(key)
            os.environ[key] = value

    def _unapply_env(self) -> None:
        for key, original in self._env_backup.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original
        self._env_backup = {}
