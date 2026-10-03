"""Pipeline decorators — wrap a parsed pipeline to change how it runs.

A decorator is a token of the form ``@name [flags]`` at the start of a
line that wraps the rest of the line as a pipeline.  See
``doc/decorators.md`` for the full design.

Built-in decorators are siblings of this module (e.g.
``pitash.decorators.watch``).  Each module exposes ``register()`` which
calls ``decorator_registry.decorator(...)``.

Public API::

    from pitash.decorators import registry as decorator_registry
    from pitash.commands import arg

    @decorator_registry.decorator(
        name="watch",
        params=[arg("-n", "--interval", type=float, default=2.0)],
    )
    def watch(pipeline, *, interval):
        while True:
            pipeline.run()
            time.sleep(interval)
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Callable

from ..commands import (
    Arg,
    CmdParser,
    _VALUE_ACTIONS,
    _build_completers,
    _build_help_text,
    _effective_description,
)
from ..completion import Completer
from ..paths import config_dir
from ..pipeline import Pipeline, set_decorator_value_flag_lookup


# Late-bound hook so ``@bg`` can ask the running Shell to spawn a background
# slot.  Wired from ``Shell.__init__`` to keep the decorator package free of
# any Shell import (decorators are loaded *during* Shell construction).  The
# callable's contract: ``run_in_background(pipeline, name=None) -> str`` —
# returns the resolved context name on success, raises ValueError on
# collision/invalid input.
_background_runner: Callable[..., str] | None = None


def set_background_runner(fn: Callable[..., str] | None) -> None:
    global _background_runner
    _background_runner = fn


def run_in_background(pipeline: Pipeline, *, name: str | None = None) -> str:
    if _background_runner is None:
        raise RuntimeError(
            "@bg requires a running Shell; no background runner is registered"
        )
    return _background_runner(pipeline, name=name)


@dataclass
class Decorator:
    """A registered pipeline decorator.

    ``func`` receives the wrapped ``Pipeline`` as its first positional
    argument and the parsed flag namespace as keyword arguments.
    """
    name: str
    func: Callable
    params: list[Arg] | None = None
    completers: dict[int | None, Completer] = field(default_factory=dict)
    help_text: str = ""
    description: str = ""


class DecoratorRegistry:
    """Registry of pipeline decorators.

    Mirrors :class:`pitash.commands.CommandRegistry` but flat — decorators
    don't have subcommands or aliases.
    """

    def __init__(self) -> None:
        self._decorators: dict[str, Decorator] = {}
        self._builtin_names: set[str] = set()

    def decorator(
        self,
        name: str | None = None,
        params: list[Arg] | None = None,
        help: str | None = None,
    ):
        """Register a decorator function.

        Three call forms, identical in shape to :meth:`CommandRegistry.command`:

        * ``@registry.decorator`` — uses the function's ``__name__``.
        * ``@registry.decorator(name="watch", params=[...])`` — explicit name.
        * ``@registry.decorator(params=[...])`` — uses the function's
          ``__name__`` with params.
        """
        # Form: @registry.decorator  (no parens at all)
        if callable(name):
            func = name
            self._register(func.__name__, func, params=None, help=None)
            return func

        # Form: registry.decorator("name", ...) or @registry.decorator(name=...)
        def wrap(func: Callable) -> Callable:
            cmd_name = name if isinstance(name, str) else func.__name__
            self._register(cmd_name, func, params=params, help=help)
            return func
        return wrap

    def _register(
        self,
        name: str,
        func: Callable,
        params: list[Arg] | None,
        help: str | None,
    ) -> Decorator:
        completers = _build_completers(params) if params else {}
        deco = Decorator(
            name=name,
            func=func,
            params=params,
            completers=completers,
            help_text=_build_help_text(help, func, f"@{name}", params),
            description=_effective_description(help, func),
        )
        self._decorators[name] = deco
        return deco

    def get(self, name: str) -> Decorator | None:
        return self._decorators.get(name)

    def has(self, name: str) -> bool:
        return name in self._decorators

    def list_decorators(self) -> list[str]:
        return list(self._decorators.keys())

    def flag_takes_value(self, decorator_name: str, flag: str) -> bool:
        """Whether *flag* on *decorator_name* consumes the next token.

        Used by the parser to decide whether ``@watch -n 5 ls`` is
        ``flags=["-n", "5"], body="ls"`` (yes, ``-n`` takes a value)
        vs ``flags=["-n"], body="5 ls"`` (no, ``-n`` is a boolean).
        """
        deco = self._decorators.get(decorator_name)
        if deco is None or not deco.params:
            return False
        for a in deco.params:
            if flag not in a.names:
                continue
            action = a.kwargs.get("action", "store")
            return action in _VALUE_ACTIONS
        return False

    def mark_builtins(self) -> None:
        """Snapshot current decorators as builtins (won't be removed on reload)."""
        self._builtin_names = set(self._decorators.keys())

    def clear_user_decorators(self) -> None:
        """Remove all non-builtin decorators."""
        self._decorators = {
            k: v for k, v in self._decorators.items() if k in self._builtin_names
        }


registry = DecoratorRegistry()

# Wire the parser's late-bound lookup so it can ask "does this decorator's
# flag take a value?" without importing this module.
set_decorator_value_flag_lookup(registry.flag_takes_value)


def parse_decorator_args(deco: Decorator, tokens: list[str]) -> dict | None:
    """Parse *tokens* against *deco*'s argparse spec.

    Returns the keyword-arg dict on success, or ``None`` if argparse
    rejected the input (error or ``--help``; the parser already printed
    the message).
    """
    parser = CmdParser(f"@{deco.name}", description=deco.description or None)
    for a in deco.params or []:
        parser.add_argument(*a.names, **a.kwargs)
    ns = parser.parse_args(tokens)
    if ns is None:
        return None
    return vars(ns)


# ---------------------------------------------------------------------------
# Built-in / user decorator loading (mirrors pitash.recipes.enable)
# ---------------------------------------------------------------------------

decorator_search_path: list[Path] = [config_dir() / "decorators"]


def add_decorator_path(path: str | Path) -> None:
    """Append *path* to the decorator search path."""
    decorator_search_path.append(Path(path))


def enable(*decorator_names: str) -> None:
    """Enable one or more built-in or user decorators by name.

    Pass ``"*"`` to enable all discoverable decorators.
    Lookup order: built-in package, then each directory in
    :data:`decorator_search_path`.
    """
    names = decorator_names
    if "*" in names:
        names = _discover_all_decorators()
    for name in names:
        module = _load_decorator(name)
        module.register()


def _discover_all_decorators() -> list[str]:
    found: set[str] = set()
    builtin_dir = Path(__file__).parent
    for p in builtin_dir.glob("*.py"):
        if p.stem != "__init__":
            found.add(p.stem)
    for directory in decorator_search_path:
        if directory.is_dir():
            for p in directory.glob("*.py"):
                if p.stem != "__init__":
                    found.add(p.stem)
    return sorted(found)


def _load_decorator(name: str):
    try:
        return import_module(f".{name}", package=__package__)
    except ImportError as e:
        if e.name != f"{__package__}.{name}":
            raise

    for directory in decorator_search_path:
        candidate = Path(directory) / f"{name}.py"
        if candidate.exists():
            spec = importlib.util.spec_from_file_location(
                f"pitash_user_decorator_{name}", candidate
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

    searched = ", ".join(str(d) for d in decorator_search_path)
    raise ImportError(
        f"Decorator {name!r} not found in built-in decorators or search path: [{searched}]"
    )
