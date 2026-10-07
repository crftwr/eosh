"""Python-backed shell variables — Var, EnvVar, Setting, VarRegistry, VarCompleter.

Register a variable with the module-level ``registry`` to give
``var NAME=VALUE`` (and bare ``NAME=VALUE``, and ``$NAME``) a logical name,
a description and value completion.  Two kinds:

* :class:`EnvVar` — **declarative**: a logical name over one or more
  ``os.environ`` keys.  The shell does every write, through the context
  manager, so each key is saved and restored per context like any variable
  set with ``var``.

* :class:`Setting` — a **process-global** value with no environment key
  (``notify``, ``notify_threshold``, an add-on's endpoint URL): subclass it
  and implement ``get`` / ``set`` / ``unset``.  Context switches leave it
  alone.

Example::

    from eosh import var_registry, EnvVar
    from eosh.completion import ChoiceCompleter, CallbackCompleter

    var_registry.register(EnvVar(
        "aws_region", keys=["AWS_REGION", "AWS_DEFAULT_REGION"],
        completer=ChoiceCompleter(["us-east-1", "us-west-2", "eu-west-1"]),
        description="AWS region — sets AWS_REGION + AWS_DEFAULT_REGION",
    ))
    var_registry.register(EnvVar(
        "aws_profile", keys="AWS_PROFILE",
        completer=CallbackCompleter(list_aws_profiles),
        description="AWS named profile",
    ))

The module-level singleton is named ``registry`` inside this module; importers
typically alias it as ``var_registry`` to distinguish it from the command
registry.
"""

from __future__ import annotations

import dataclasses
import os
from abc import ABC, abstractmethod
from typing import Sequence

from .completion import Completer, Completion, CompletionContext


class Var(ABC):
    """What ``var`` lists, reads and completes.  Register an :class:`EnvVar`
    or a :class:`Setting` — the shell needs to know which one it is writing."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Logical name as seen in the shell (e.g. ``'aws_region'``)."""
        ...

    @abstractmethod
    def get(self) -> str | None:
        """Return the current value, or None if unset."""
        ...

    @property
    def value_completer(self) -> Completer | None:
        """Optional completer for the value side of ``KEY=VALUE``."""
        return None

    @property
    def description(self) -> str:
        """Short description shown next to the name in ``var`` listings."""
        return ""


class EnvVar(Var):
    """A logical name over one or more ``os.environ`` keys.

    Declarative: the shell writes *keys* itself — every one gets the value —
    through the context manager, so they are saved and restored per context.
    Reading returns the first key.

    Args:
        name:        Logical shell name (e.g. ``'aws_region'``).
        keys:        The ``os.environ`` key, or keys, the name stands for.
                     Defaults to *name*.
        completer:   Value completer shown when the user types ``NAME=<TAB>``.
        description: Short description shown in ``var`` listings.
    """

    def __init__(
        self,
        name: str,
        keys: str | Sequence[str] | None = None,
        completer: Completer | None = None,
        description: str = "",
    ) -> None:
        self._name = name
        if keys is None:
            keys = [name]
        elif isinstance(keys, str):
            keys = [keys]
        if not keys:
            raise ValueError(f"EnvVar {name!r} needs at least one key")
        self.keys: tuple[str, ...] = tuple(keys)
        self._completer = completer
        self._description = description

    @property
    def name(self) -> str:
        return self._name

    def get(self) -> str | None:
        return os.environ.get(self.keys[0])

    @property
    def value_completer(self) -> Completer | None:
        return self._completer

    @property
    def description(self) -> str:
        return self._description


class Setting(Var):
    """A process-global value with no environment key — subclass and
    implement :meth:`get`, :meth:`set` and, if "unset" means more than
    ``set("")``, :meth:`unset`.

    Not saved or restored on a context switch: a setting belongs to the
    person at the keyboard (``notify``), or to a tool's own module state (an
    endpoint URL), not to the environment a context carries.
    """

    @abstractmethod
    def set(self, value: str) -> None:
        """Apply the new value (``var NAME=VALUE``)."""
        ...

    def unset(self) -> None:
        """Clear it (``var NAME=``)."""
        self.set("")


class VarRegistry:
    """Registry of Python-backed shell variables.

    A module-level singleton ``registry`` is provided; importers typically
    alias it as ``var_registry`` to distinguish it from the command registry.
    Use the singleton unless you need an isolated instance for testing.
    """

    def __init__(self) -> None:
        self._vars: dict[str, Var] = {}
        self._builtin_names: set[str] = set()

    def register(self, var: Var) -> None:
        """Register an :class:`EnvVar` or a :class:`Setting` under its name."""
        if not isinstance(var, (EnvVar, Setting)):
            raise TypeError(
                f"{type(var).__name__}: register an EnvVar (os.environ keys) "
                f"or a Setting subclass (a process-global value)"
            )
        self._vars[var.name] = var

    def get(self, name: str) -> Var | None:
        """Return the :class:`Var` for *name*, or ``None`` if not registered."""
        return self._vars.get(name)

    def all(self) -> list[Var]:
        """Return all registered :class:`Var` instances in registration order."""
        return list(self._vars.values())

    def mark_builtins(self) -> None:
        """Snapshot current vars as built-ins (preserved across ``reload``)."""
        self._builtin_names = set(self._vars.keys())

    def clear_user_vars(self) -> None:
        """Remove all non-builtin vars (called by ``reload``)."""
        self._vars = {k: v for k, v in self._vars.items() if k in self._builtin_names}


#: Module-level singleton — import and use this in config.py / recipes.
#: Typically aliased as ``var_registry`` by importers.
registry = VarRegistry()


class VarCompleter(Completer):
    """Completion for the ``var`` command's ``KEY=VALUE`` arguments.

    Three completion phases:

    * Typing ``aws_<TAB>``            → list registered Var names (with ``=`` appended).
    * Typing ``aws_region=<TAB>``     → delegate to the variable's ``value_completer``.
    * Typing ``aws_region=us-<TAB>``  → narrow the value list by prefix.

    The ``=``-split is local to this completer; the global tokeniser is not
    changed.
    """

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        prefix = ctx.prefix

        if "=" in prefix:
            key, _, val_prefix = prefix.partition("=")
            var = registry.get(key)
            if var is not None and var.value_completer is not None:
                sub_ctx = dataclasses.replace(ctx, prefix=val_prefix)
                return [
                    Completion(
                        value=f"{key}={c.value}",
                        display=c.display or c.value,
                        description=c.description,
                    )
                    for c in var.value_completer.complete(sub_ctx)
                ]
        else:
            results: list[Completion] = []
            seen: set[str] = set()

            # Registered Python-backed vars first (richer descriptions).
            for v in registry.all():
                if v.name.startswith(prefix):
                    results.append(Completion(
                        value=f"{v.name}=",
                        display=f"{v.name}=",
                        description=v.description,
                    ))
                    seen.add(v.name)

            # All os.environ keys, skipping names already covered above.
            for key in sorted(os.environ):
                if key.startswith(prefix) and key not in seen:
                    val = os.environ[key]
                    desc = val[:60] + "…" if len(val) > 60 else val
                    results.append(Completion(
                        value=f"{key}=",
                        display=f"{key}=",
                        description=desc,
                    ))

            return results

        return []
