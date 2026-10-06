"""Command registry, @command decorator, CmdParser, and arg() descriptor."""

from __future__ import annotations

import argparse
import inspect
import sys
from dataclasses import dataclass, field
from typing import Callable

from .completion import Completer, ChoiceCompleter, OptionsCompleter


class _HelpOrError(Exception):
    """Internal sentinel raised by CmdParser to avoid sys.exit()."""


class CmdParser(argparse.ArgumentParser):
    """ArgumentParser that is safe to use inside eosh commands.

    Standard ``ArgumentParser`` calls ``sys.exit()`` on ``--help`` and on
    parse errors, which would terminate the shell.  ``CmdParser`` intercepts
    both and returns ``None`` from :meth:`parse_args` instead — after printing
    the help or error message exactly as argparse normally would.

    Argparse handles combined short boolean flags automatically (``-nv`` is
    treated as ``-n -v``), so commands that expose eosh's multi-select
    option TUI work without any extra effort.

    Typical usage inside a command function::

        def deploy(*args):
            parser = CmdParser("deploy")
            parser.add_argument("environment", choices=["prod", "staging", "dev"])
            parser.add_argument("-n", "--dry-run", action="store_true")
            parser.add_argument("-t", "--timeout", type=int, default=60)
            ns = parser.parse_args(args)
            if ns is None:
                return          # error or --help already printed
            # use ns.environment, ns.dry_run, ns.timeout, ...
    """

    def exit(self, status: int = 0, message: str | None = None) -> None:
        # Called by --help and --version; don't let it kill the shell.
        if message:
            print(message, end="", file=sys.stderr)
        raise _HelpOrError()

    def error(self, message: str) -> None:
        # Called for parse errors (missing args, unknown flags, wrong types).
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise _HelpOrError()

    def parse_args(self, args=None, namespace=None):
        try:
            return super().parse_args(args, namespace)
        except _HelpOrError:
            return None


@dataclass
class Arg:
    """Descriptor for one positional argument or flag.

    Created with :func:`arg`.  ``kwargs`` are forwarded verbatim to
    ``argparse.ArgumentParser.add_argument()``; ``completer`` is a
    eosh-specific completion hint that is consumed by the registry and
    never passed to argparse.
    """
    names: tuple[str, ...]
    kwargs: dict
    completer: Completer | None = None


def arg(*names: str, completer: Completer | None = None, **kwargs) -> Arg:
    """Declare one positional argument or flag for a ``params=`` command.

    Keyword arguments mirror ``argparse.add_argument()`` exactly, plus one
    extra eosh-specific keyword:

    ``completer``
        A :class:`~eosh.completion.Completer` instance used for TAB
        completion of this argument's *value*.

        - For a **positional arg**, it completes the arg itself.  If omitted
          and ``choices=`` is set, a :class:`ChoiceCompleter` is derived
          automatically.
        - For a **value-taking flag** (``-t``, ``--timeout``, …), it
          completes the value the user types after the flag.
        - Ignored for **boolean flags** (``action="store_true"`` etc.).

    Examples::

        arg("environment", choices=["prod", "staging", "dev"])
        arg("instance",    completer=CallbackCompleter(fetch_instances))
        arg("-n", "--dry-run", action="store_true", help="skip execution")
        arg("-t", "--timeout", type=int, default=60, metavar="SECONDS",
            help="timeout in seconds",
            completer=ChoiceCompleter(["30", "60", "120", "300"]))

    The resulting :class:`Arg` is passed to ``@registry.command(params=[…])``.
    The registry derives both the argparse parser **and** the TAB-completion
    dict from the same list.
    """
    return Arg(names=names, kwargs=kwargs, completer=completer)


# argparse actions that consume a following value token
_VALUE_ACTIONS = {"store", "append", "extend"}

# Tokens with these prefixes are treated as flags by the completion engine.
# ``-`` covers short and long flags (-c, --verbose).  ``+`` is rare but used
# by tools like ``lsof`` for SysV-style enable/disable pairs (-c / +c).
_FLAG_PREFIXES = ("-", "+")


def _is_flag_name(name: str) -> bool:
    return name.startswith(_FLAG_PREFIXES)


# Sentinel key meaning "any positional index ≥ this slot".  Used for arg() with
# nargs="*" or nargs="+" — that completer becomes the source of truth for every
# positional slot from its declared position onward.
WILDCARD = ...


def _build_completers(params: list[Arg]) -> dict:
    """Derive a completers dict from a list of :class:`Arg` descriptors.

    Rules:

    * **Positional arg** — if ``completer=`` is set, use it; otherwise if
      ``choices=`` is set derive a :class:`ChoiceCompleter` automatically.
      Keyed by zero-based positional index.

    * **Wildcard positional** — when ``nargs="*"`` or ``nargs="+"`` is set,
      the completer is stored under the :data:`WILDCARD` (``...``) key instead
      of an integer.  It serves every positional slot from that point on, so
      a single ``arg("targets", nargs="*", ...)`` replaces the old ``0: …,
      1: …, 2: …`` boilerplate.

    * **Flag** — all flag names are collected into an
      :class:`OptionsCompleter` under the ``None`` key.  ``help=`` text
      becomes the description shown in the completion menu.  Value-taking
      flags (action="store" / "append" / "extend") are also registered in
      the ``args=`` dict of the ``OptionsCompleter`` with their ``metavar``
      and optional ``completer`` for the value.
    """
    all_options: dict[str, str] = {}
    args_hints: dict[str, str | tuple] = {}
    pos_completers: dict = {}
    pos_idx = 0

    for a in params:
        is_flag = _is_flag_name(a.names[0])

        if not is_flag:
            # ── positional arg ────────────────────────────────────────────
            comp = a.completer
            if comp is None and "choices" in a.kwargs:
                comp = ChoiceCompleter(list(a.kwargs["choices"]))
            if comp is not None:
                if a.kwargs.get("nargs") in ("*", "+"):
                    pos_completers[WILDCARD] = comp
                else:
                    pos_completers[pos_idx] = comp
            pos_idx += 1

        else:
            # ── flag ──────────────────────────────────────────────────────
            desc = a.kwargs.get("help", "")
            for name in a.names:
                all_options[name] = desc

            action = a.kwargs.get("action", "store")
            if action in _VALUE_ACTIONS:
                # Derive metavar from --long-name → LONG_NAME, or metavar=
                long_names = [n for n in a.names if n.startswith("--")]
                dest = (long_names[0].lstrip("-").replace("-", "_")
                        if long_names else a.names[0].lstrip("".join(_FLAG_PREFIXES)))
                metavar = a.kwargs.get("metavar", dest.upper())
                hint: str | tuple = (metavar, a.completer) if a.completer else metavar
                for name in a.names:
                    args_hints[name] = hint

    result: dict = {}
    if all_options:
        result[None] = OptionsCompleter(all_options, args=args_hints or None)
    result.update(pos_completers)
    return result


def get_positional_completer(completers: dict, pos_idx: int) -> Completer | None:
    """Look up a positional completer by index, falling back to the wildcard slot.

    External recipes (and Python commands using ``nargs="*"``) commonly attach
    a single completer at :data:`WILDCARD` to serve every positional slot.
    Callers should use this helper rather than ``completers.get(pos_idx)``
    directly so the wildcard fallback is honoured uniformly.
    """
    if pos_idx in completers:
        return completers[pos_idx]
    return completers.get(WILDCARD)


def _build_usage(cmd_name: str, params: list[Arg]) -> str:
    """Generate a compact usage line from a params list.

    Format mirrors conventional shell usage notation:

    * Required positional → ``<name>``
    * Optional positional (``nargs="?"`` or ``"*"``) → ``[name]``
    * Boolean flag → ``[-n]``  (shortest form)
    * Value-taking flag → ``[-n METAVAR]``  (shortest form + metavar)
    """
    parts = [cmd_name]
    for a in params:
        is_flag = _is_flag_name(a.names[0])
        if not is_flag:
            is_optional = a.kwargs.get("nargs") in ("?", "*")
            dest = a.names[0]
            parts.append(f"[{dest}]" if is_optional else f"<{dest}>")
        else:
            short = next((n for n in a.names if len(n) == 2), a.names[0])
            action = a.kwargs.get("action", "store")
            if action in _VALUE_ACTIONS:
                long_names = [n for n in a.names if n.startswith("--")]
                dest = (long_names[0].lstrip("-").replace("-", "_")
                        if long_names else a.names[0].lstrip("".join(_FLAG_PREFIXES)))
                metavar = a.kwargs.get("metavar", dest.upper())
                parts.append(f"[{short} {metavar}]")
            else:
                parts.append(f"[{short}]")
    return "Usage: " + " ".join(parts)


def _effective_description(help: str | None, func: Callable) -> str:
    """Return the description string: explicit *help* wins, then the docstring."""
    if help is not None:
        return help
    return inspect.getdoc(func) or ""


def _build_help_text(
    help: str | None,
    func: Callable,
    cmd_name: str,
    params: list[Arg] | None,
) -> str:
    """Assemble the full help text stored on a Command.

    Structure when both *help* and *params* are present::

        <description>

        Usage: cmd <pos> [opt] [-f] [-v VAL]

    The first line is always the short description (used in command listings).
    When neither *help* nor *params* is given, falls back to the function
    signature as a minimal hint (legacy behaviour).
    """
    desc = _effective_description(help, func)
    if params is not None:
        usage = _build_usage(cmd_name, params)
        return (desc + "\n\n" + usage).strip() if desc else usage
    return desc or _signature_help(func, cmd_name)


def _signature_help(func: Callable, cmd_name: str) -> str:
    """Generate a ``Usage: cmd_name [args]`` hint from the function signature.

    Shown as the fallback help text when a command has no docstring.
    Required parameters are shown as ``<name>``, optional ones as ``[name]``,
    and ``*args`` as ``[args...]``.  Returns an empty string when the signature
    cannot be determined.
    """
    try:
        sig = inspect.signature(func)
    except (ValueError, TypeError):
        return ""

    parts: list[str] = []
    for param_name, param in sig.parameters.items():
        if param.kind == param.VAR_POSITIONAL:
            parts.append(f"[{param_name}...]")
        elif param.kind == param.VAR_KEYWORD:
            pass  # **kwargs — not useful to surface in usage
        elif param.default is inspect.Parameter.empty:
            parts.append(f"<{param_name}>")
        else:
            parts.append(f"[{param_name}]")

    usage = " ".join([cmd_name] + parts)
    return f"Usage: {usage}"


@dataclass
class Command:
    """A node in the command tree.

    Roles are inferred from structure, not declared:

    * Has handler, no children → Python leaf (parses args, calls handler).
    * Has children, no handler → interior group (prints child list when run bare).
    * Tree contains no handler anywhere → external recipe (completion only;
      execution shells out via PTY).

    Both Python commands and external recipes are built through the same
    ``.command()`` builder method, so every node is just a ``Command``.
    """

    name: str
    func: Callable | None = None
    completers: dict[int | None, Completer] = field(default_factory=dict)
    help_text: str = ""
    params: list[Arg] | None = None
    description: str = ""  # raw description line (from help=), used as CmdParser description
    parent: "Command | None" = None
    children: dict[str, "Command"] = field(default_factory=dict)

    # ── Tree-shape predicates ────────────────────────────────────────────

    @property
    def is_leaf(self) -> bool:
        return not self.children

    @property
    def is_group(self) -> bool:
        return bool(self.children)

    def has_any_handler(self) -> bool:
        """True if this node or any descendant carries a Python handler."""
        if self.func is not None:
            return True
        return any(c.has_any_handler() for c in self.children.values())

    # ── Tree builder ─────────────────────────────────────────────────────

    def command(
        self,
        name: str | None = None,
        params: list[Arg] | None = None,
        help: str | None = None,
    ) -> "Command":
        """Create a child node — used bare for groups, as a decorator for leaves.

        Bare (returns the new child node)::

            s3 = aws.command("s3", help="Amazon S3")

        Decorator (still returns the node; ``__call__`` attaches the handler)::

            @s3.command("ls", params=[arg("path", nargs="?")])
            def s3_ls(path=None):
                ...

        When *name* is omitted the return value is a decorator that uses
        the wrapped function's ``__name__`` for the child name — mirrors
        the ``@registry.command`` flat-command syntax.
        """
        if name is not None:
            child = self._make_child(name, params=params, help=help)
            child._pending_help = help  # used when a handler is attached later
            return child

        def decorator(func: Callable) -> "Command":
            child = self._make_child(func.__name__, params=params, help=help)
            child._attach_handler(func, help)
            return child
        return decorator

    def __call__(self, func: Callable) -> "Command":
        """Decorator hook — ``@parent.command("ls", params=[...])`` flow.

        The first call ``parent.command("ls", ...)`` returns *this* node;
        Python then calls ``this_node(func)`` to apply the decorator.  We
        attach the handler and return self so callers can keep the
        reference (``my_leaf = @s3.command("ls", ...)`` works).
        """
        self._attach_handler(func, getattr(self, "_pending_help", None))
        return self

    def _attach_handler(self, func: Callable, help: str | None) -> None:
        self.func = func
        self.description = _effective_description(help, func)
        self.help_text = _build_help_text(help, func, self.name, self.params)

    def _make_child(
        self,
        name: str,
        params: list[Arg] | None,
        help: str | None,
    ) -> "Command":
        if name in self.children:
            # Re-declaration: update params/help on the existing node so a
            # later `.command("foo", ...)` call can populate a node first
            # created as a group.
            child = self.children[name]
            if params is not None:
                child.params = params
                child.completers = _build_completers(params)
            if help is not None:
                child.description = help
                child.help_text = _build_help_text(help, child.func or _noop_for_help,
                                                    child.name, child.params)
            return child

        completers = _build_completers(params) if params else {}
        child = Command(
            name=name,
            completers=completers,
            help_text=_build_help_text(help, _noop_for_help, name, params),
            params=params,
            description=help or "",
            parent=self,
        )
        self.children[name] = child
        return child

    # ── Resolution & dispatch ────────────────────────────────────────────

    def resolve(self, tokens: list[str]) -> tuple["Command", list[str]]:
        """Walk down the tree consuming sub-command names from *tokens*.

        Flags (and their values, if a value-taking flag is reachable) are
        skipped during the walk so they may appear at any position.

        Returns ``(node, remaining)`` where *remaining* are the tokens that
        were **not** consumed as sub-command names — flag tokens stay,
        positional args stay, only the matched sub-command name tokens are
        dropped.  The resolved leaf's argparse parses *remaining* directly.
        """
        node = self
        consumed_indices: set[int] = set()
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok.startswith("-"):
                if _flag_takes_value(node, tok):
                    i += 2
                else:
                    i += 1
                continue
            if tok in node.children:
                node = node.children[tok]
                consumed_indices.add(i)
                i += 1
                continue
            break
        remaining = [t for k, t in enumerate(tokens) if k not in consumed_indices]
        return node, remaining

    def invoke(self, args: list[str] | tuple[str, ...]) -> None:
        """Dispatch a command invocation.

        For tree-shaped commands (``self.children`` is non-empty) the method
        walks the tree and dispatches to the resolved node.  For flat
        commands it preserves the original ``func(**parsed_kwargs)`` or
        ``func(*args)`` behaviour.
        """
        args = list(args)
        if self.children:
            self._invoke_tree(args)
            return
        self._invoke_self(args)

    def _invoke_self(self, args: list[str]) -> None:
        if self.params is not None and self.func is not None:
            parser = CmdParser(self._full_name(), description=self.description or None)
            for a in self.params:
                parser.add_argument(*a.names, **a.kwargs)
            ns = parser.parse_args(args)
            if ns is None:
                return
            self.func(**vars(ns))
        elif self.func is not None:
            self.func(*args)
        else:
            # No handler — print group-style help (or signal no-op).
            print(self.help_text or f"{self.name}: no handler")

    def _invoke_tree(self, tokens: list[str]) -> None:
        node, remaining = self.resolve(tokens)
        if node.is_group and node.func is None:
            # Hit an interior group with no default handler — print its help.
            _print_group_help(node)
            return

        # node has a handler (leaf or group-with-default).  Combine its own
        # params with inherited ancestor flags so leaf handlers can declare
        # ancestor-flag kwargs (e.g. region=, profile=).
        merged_params = _collect_inherited_params(node)
        parser = CmdParser(node._full_name(), description=node.description or None)
        for a in merged_params:
            parser.add_argument(*a.names, **a.kwargs)
        ns = parser.parse_args(remaining)
        if ns is None:
            return
        kwargs = vars(ns)
        # Filter to kwargs the handler accepts (so leaves can ignore
        # unused ancestor flags by simply omitting them from their signature).
        accepted = _accepted_kwargs(node.func, kwargs)
        node.func(**accepted)

    def _full_name(self) -> str:
        """Dotted-or-spaced full path from root, used in error messages."""
        parts: list[str] = []
        n: Command | None = self
        while n is not None:
            parts.append(n.name)
            n = n.parent
        return " ".join(reversed(parts))

    # ── Completion helpers ───────────────────────────────────────────────

    def merged_options_completer(self) -> "OptionsCompleter | None":
        """Return an :class:`OptionsCompleter` merging this node's flags with
        every ancestor's flags.  Deepest definition wins on conflict.
        """
        merged_options: dict[str, str] = {}
        merged_args: dict[str, str | tuple] = {}
        merged_value_completers: dict[str, Completer] = {}
        chain: list[Command] = []
        n: Command | None = self
        while n is not None:
            chain.append(n)
            n = n.parent
        # Apply ancestors first so the deeper node overwrites (last-write-wins).
        for nd in reversed(chain):
            oc = nd.completers.get(None)
            if oc is None:
                continue
            for flag, desc in oc.options.items():
                merged_options[flag] = desc
            for flag, hint in oc.args.items():
                merged_args[flag] = hint
            for flag, vc in getattr(oc, "_value_completers", {}).items():
                merged_value_completers[flag] = vc
        if not merged_options and not merged_args:
            return None
        # Re-pack into OptionsCompleter.  Convert (hint, completer) tuples
        # to the form OptionsCompleter() expects when constructed.
        args_arg: dict[str, str | tuple] = {}
        for flag, hint in merged_args.items():
            vc = merged_value_completers.get(flag)
            args_arg[flag] = (hint, vc) if vc else hint
        return OptionsCompleter(merged_options, args=args_arg or None)


def _noop_for_help(*args, **kwargs):  # pragma: no cover — placeholder for builders
    pass


def _flag_takes_value(node: Command, flag: str) -> bool:
    """True if *flag* is a value-taking flag at *node* or any ancestor."""
    n: Command | None = node
    while n is not None:
        oc = n.completers.get(None)
        if oc is not None and hasattr(oc, "args") and flag in oc.args:
            return True
        n = n.parent
    return False


def _collect_inherited_params(node: Command) -> list[Arg]:
    """Combine ancestor flags with this node's own params.

    Positional args come from the resolved node only.  Flags are inherited
    from all ancestors; if the same flag name appears at multiple levels
    the deepest definition wins.
    """
    own_params = list(node.params or [])
    own_flag_names: set[str] = set()
    for a in own_params:
        if a.names and a.names[0].startswith("-"):
            own_flag_names.update(a.names)

    # Walk up collecting ancestor flags not already declared at the leaf.
    inherited_flags: list[Arg] = []
    n = node.parent
    while n is not None:
        for a in n.params or []:
            if not a.names or not a.names[0].startswith("-"):
                continue  # ancestor positionals are not inherited
            if any(name in own_flag_names for name in a.names):
                continue
            own_flag_names.update(a.names)
            inherited_flags.append(a)
        n = n.parent

    # Preserve order: own positionals + own flags first, then inherited flags.
    return own_params + inherited_flags


def _accepted_kwargs(func: Callable, kwargs: dict) -> dict:
    """Filter *kwargs* down to the names *func* accepts.

    Allows leaf handlers to declare only the inherited flags they care about
    by omitting the rest from their signature.
    """
    try:
        sig = inspect.signature(func)
    except (ValueError, TypeError):
        return kwargs
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return kwargs
    accepted_names = {p.name for p in sig.parameters.values()}
    return {k: v for k, v in kwargs.items() if k in accepted_names}


def _print_group_help(node: Command) -> None:
    """Print a sub-command list for a group with no default handler."""
    if node.description:
        print(node.description)
        print()
    print(f"Usage: {node._full_name()} <subcommand> [args...]")
    if node.children:
        print()
        print("Subcommands:")
        width = max(len(c) for c in node.children) + 2
        for child_name in sorted(node.children):
            child = node.children[child_name]
            desc = child.description or ""
            print(f"  {child_name:<{width}}{desc}")


class CommandRegistry:
    def __init__(self):
        self._commands: dict[str, Command] = {}
        self._builtin_names: set[str] = set()
        self._aliases: dict[str, str] = {}
        self._builtin_aliases: set[str] = set()

    def command(
        self,
        name: str | None = None,
        params: list[Arg] | None = None,
        help: str | None = None,
        delegate: Completer | None = None,
    ):
        """Register a top-level command, group, or external recipe.

        Three call forms:

        * **Bare** — ``aws = registry.command("aws", help="...", params=[...])``
          creates a :class:`Command` node and returns it; chain
          ``.command(...)`` on the result to add children.  When the entire
          tree has no handler anywhere it is treated as an external-command
          recipe (completion only).

        * **Decorator with name** — ``@registry.command("greet", params=[...])``
          wraps a function and registers it as a flat Python command.

        * **Decorator no name** — ``@registry.command(params=[...])`` (or
          plain ``@registry.command``) uses the function's ``__name__``.

        Decorator forms return the function (back-compat with the prior
        decorator behaviour); bare form returns the :class:`Command` node.

        ``delegate`` — install a single :class:`Completer` as the source of
        truth for **every** completion slot (flags, all positional indices).
        Used when an external tool ships its own completer protocol that
        decides per-call what to return (e.g. ``aws_completer``).  Cannot be
        combined with ``params``.

        """
        # Form: @registry.command  (no parens at all)
        if callable(name):
            func = name
            self._make_root(func.__name__, params=None, help=None, func=func)
            return func

        # Form: registry.command("name", ...) — bare or @decorator-with-name
        if isinstance(name, str):
            node = self._make_root(
                name, params=params, help=help, func=None,
                delegate=delegate,
            )
            node._pending_help = help
            return node

        # Form: @registry.command(params=[...])  (no positional name)
        def decorator(func: Callable) -> Callable:
            cmd_name = func.__name__
            self._make_root(
                cmd_name, params=params, help=help, func=func,
                delegate=delegate,
            )
            return func
        return decorator

    def _make_root(
        self,
        name: str,
        params: list[Arg] | None,
        help: str | None,
        func: Callable | None,
        delegate: Completer | None = None,
    ) -> Command:
        completers = _build_completers(params) if params else {}
        if delegate is not None:
            # Same completer at None (flags) and WILDCARD (all positional slots).
            completers[None] = delegate
            completers[WILDCARD] = delegate
        cmd = Command(
            name=name,
            func=func,
            completers=completers,
            help_text=_build_help_text(help, func or _noop_for_help, name, params),
            params=params,
            description=_effective_description(help, func or _noop_for_help) if (help or func) else "",
            parent=None,
        )
        self._commands[name] = cmd
        return cmd

    def get(self, name: str) -> Command | None:
        return self._commands.get(name)

    def list_commands(self) -> list[str]:
        return list(self._commands.keys())

    def has(self, name: str) -> bool:
        return name in self._commands

    def mark_builtins(self) -> None:
        """Snapshot current commands as builtins (won't be removed on reload)."""
        self._builtin_names = set(self._commands.keys())
        self._builtin_aliases = set(self._aliases.keys())

    def clear_user_commands(self) -> None:
        """Remove all non-builtin commands and aliases."""
        self._commands = {
            k: v for k, v in self._commands.items() if k in self._builtin_names
        }
        self._aliases = {
            k: v for k, v in self._aliases.items() if k in self._builtin_aliases
        }

    # ── Aliases ──────────────────────────────────────────────────────────────

    def alias(self, name: str, expansion: str) -> None:
        """Register *name* as a shorthand that expands to *expansion*.

        Expansion is applied to the first token of a command line (bash-style)
        so ``hp create`` becomes ``awsut sagemaker hyperpod create``.  The
        expansion text
        is tokenized at use time, so it may contain multiple words and flags.

        Aliases do not chain — the first token of the expansion is never itself
        re-expanded as an alias, which prevents infinite loops.
        """
        self._aliases[name] = expansion

    def unalias(self, name: str) -> bool:
        """Remove an alias.  Returns True if it existed."""
        return self._aliases.pop(name, None) is not None

    def get_alias(self, name: str) -> str | None:
        """Return the expansion for *name*, or None if not aliased."""
        return self._aliases.get(name)

    def list_aliases(self) -> dict[str, str]:
        """Return a copy of the alias table."""
        return dict(self._aliases)


registry = CommandRegistry()
