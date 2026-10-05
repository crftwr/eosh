"""Python-backed shell variables — Var ABC, VarRegistry, convenience subclasses, VarCompleter.

Users subclass Var and register instances with the module-level ``registry``
to define variables that have custom get/set logic and optional value
completion.  The built-in ``var`` command dispatches through VarRegistry
before falling back to plain os.environ writes.

Example::

    from eosh import var_registry, Var, EnvVar
    from eosh.completion import ChoiceCompleter, CallbackCompleter

    REGIONS = ["us-east-1", "us-west-2", "eu-west-1", "ap-northeast-1"]

    class AwsRegionVar(Var):
        name = "aws_region"
        description = "AWS region — sets AWS_REGION + AWS_DEFAULT_REGION"

        def get(self):
            return os.environ.get("AWS_REGION")

        def set(self, value):
            os.environ["AWS_REGION"] = value
            os.environ["AWS_DEFAULT_REGION"] = value

        @property
        def env_keys(self):
            return ["AWS_REGION", "AWS_DEFAULT_REGION"]

        @property
        def value_completer(self):
            return ChoiceCompleter(REGIONS)

    var_registry.register(AwsRegionVar())
    var_registry.register(EnvVar(
        name="aws_profile",
        env_var="AWS_PROFILE",
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

from .completion import Completer, Completion, CompletionContext


class Var(ABC):
    """Base class for a Python-backed shell variable.

    Subclass and register with the module-level ``registry`` to define a variable with
    custom get/set logic and an optional value completer.  The env_keys
    property tells the shell which ``os.environ`` keys to save/restore on
    context switch.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Logical name as seen in the shell (e.g. ``'aws_region'``)."""
        ...

    @abstractmethod
    def get(self) -> str | None:
        """Return the current display value, or None if unset."""
        ...

    @abstractmethod
    def set(self, value: str) -> None:
        """Apply the new value (called by ``var NAME=VALUE``)."""
        ...

    def unset(self) -> None:
        """Remove the variable (called by ``var NAME=``).

        Default implementation removes every key returned by :attr:`env_keys`
        from ``os.environ``.  Override for custom teardown logic.
        """
        for k in self.env_keys:
            os.environ.pop(k, None)

    @property
    def env_keys(self) -> list[str]:
        """The actual ``os.environ`` keys this Var manages.

        The shell registers these with the context manager so their values are
        saved when leaving a context and restored when returning.  Override in
        subclasses to declare which environment variables your ``set()``
        implementation writes.
        """
        return []

    @property
    def value_completer(self) -> Completer | None:
        """Optional completer for the value side of ``KEY=VALUE``."""
        return None

    @property
    def description(self) -> str:
        """Short description shown next to the name in ``var`` listings."""
        return ""


class EnvVar(Var):
    """1-to-1 passthrough to a single ``os.environ`` key with an optional completer.

    Args:
        name:        Logical shell name (e.g. ``'aws_profile'``).
        env_var:     The actual ``os.environ`` key to read/write.
                     Defaults to *name* when omitted.
        completer:   Value completer shown when the user types ``NAME=<TAB>``.
        description: Short description shown in ``var`` listings.
    """

    def __init__(
        self,
        name: str,
        env_var: str | None = None,
        completer: Completer | None = None,
        description: str = "",
    ) -> None:
        self._name = name
        self._env_var = env_var or name
        self._completer = completer
        self._description = description

    @property
    def name(self) -> str:
        return self._name

    @property
    def env_keys(self) -> list[str]:
        return [self._env_var]

    def get(self) -> str | None:
        return os.environ.get(self._env_var)

    def set(self, value: str) -> None:
        os.environ[self._env_var] = value

    @property
    def value_completer(self) -> Completer | None:
        return self._completer

    @property
    def description(self) -> str:
        return self._description



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
        """Register a :class:`Var` instance under its logical name."""
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
    * Typing ``aws_region=<TAB>``     → delegate to ``AwsRegionVar.value_completer``.
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
