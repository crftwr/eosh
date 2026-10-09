"""Command registry, @command decorator, CmdParser, and arg() descriptor."""

from __future__ import annotations

import argparse
import inspect
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable

from .completion import Completer, ChoiceCompleter, OptionsCompleter
from .user_errors import config_warning


class _HelpOrError(Exception):
    """Internal sentinel raised by CmdParser to avoid sys.exit()."""


class CmdParser(argparse.ArgumentParser):
    """ArgumentParser that is safe to use inside eosh commands.

    Standard ``ArgumentParser`` calls ``sys.exit()`` on ``--help`` and on
    parse errors, which would terminate the shell.  ``CmdParser`` intercepts
    both and returns ``None`` from :meth:`parse_args` instead — after printing
    the help or error message exactly as argparse normally would.

    Argparse handles combined short boolean flags automatically (``-nv`` is
    treated as ``-n -v``), so a user who types flags combined needs no extra
    effort from the command.

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


def _metavar(a: Arg) -> str:
    """The placeholder a value-taking flag shows: ``metavar=``, else its dest."""
    long_names = [n for n in a.names if n.startswith("--")]
    dest = (long_names[0].lstrip("-").replace("-", "_")
            if long_names else a.names[0].lstrip("".join(_FLAG_PREFIXES)))
    return a.kwargs.get("metavar", dest.upper())


def _takes_value(a: Arg) -> bool:
    return a.kwargs.get("action", "store") in _VALUE_ACTIONS


# ── Completion derived from params ──────────────────────────────────────────
#
# Nothing is pre-computed: a command's flag and positional completers are read
# off its ``params`` list when completion asks for them.  That keeps one source
# of truth — the list argparse parses with — and lets a node's params be
# replaced (a re-declared group) without a second structure to keep in sync.


def options_completer(params: list[Arg]) -> OptionsCompleter | None:
    """An :class:`OptionsCompleter` over the flags in *params*, or ``None``.

    ``help=`` becomes each flag's description.  Value-taking flags (action
    ``store`` / ``append`` / ``extend``) are registered with their metavar
    and, when the ``arg`` has ``completer=``, the completer for the value.
    """
    options: dict[str, str] = {}
    value_flags: dict[str, str | tuple] = {}
    for a in params:
        if not _is_flag_name(a.names[0]):
            continue
        for name in a.names:
            options[name] = a.kwargs.get("help", "")
        if _takes_value(a):
            hint: str | tuple = (_metavar(a), a.completer) if a.completer else _metavar(a)
            for name in a.names:
                value_flags[name] = hint
    if not options:
        return None
    return OptionsCompleter(options, args=value_flags or None)


def positional_completer(params: list[Arg], pos_idx: int) -> Completer | None:
    """The completer for positional slot *pos_idx* (0-based), or ``None``.

    ``completer=`` wins; otherwise ``choices=`` yields a
    :class:`ChoiceCompleter`.  A positional with ``nargs="*"`` / ``"+"``
    serves every slot from its own position onward.
    """
    positionals = [a for a in params if not _is_flag_name(a.names[0])]
    for i, a in enumerate(positionals):
        if i == pos_idx or (i < pos_idx and a.kwargs.get("nargs") in ("*", "+")):
            if a.completer is not None:
                return a.completer
            if "choices" in a.kwargs:
                return ChoiceCompleter(list(a.kwargs["choices"]))
            return None
    return None


def flag_takes_value(params: list[Arg], flag: str) -> bool:
    """True if *flag* is declared in *params* and consumes the next token."""
    return any(flag in a.names and _takes_value(a) for a in params
               if _is_flag_name(a.names[0]))


def _build_parser(prog: str, params: list[Arg], description: str | None = None) -> CmdParser:
    parser = CmdParser(prog, description=description)
    for a in params:
        parser.add_argument(*a.names, **a.kwargs)
    return parser


def _build_usage(prog: str, params: list[Arg]) -> str:
    """The usage line argparse prints for *params* (what ``--help`` shows too)."""
    return _build_parser(prog, params).format_usage().strip()


def _effective_description(help: str | None, func: Callable) -> str:
    """Return the description string: explicit *help* wins, then the docstring."""
    if help is not None:
        return help
    return inspect.getdoc(func) or ""


def _build_help_text(
    help: str | None,
    func: Callable | None,
    cmd_name: str,
    params: list[Arg] | None,
) -> str:
    """Assemble the full help text stored on a Command.

    For a Python command (*func* given) with *params*::

        <description>

        usage: cmd [-h] [-f] [-v VAL] pos [opt]

    — the usage argparse itself prints.  Without params, the function
    signature stands in.  A node with no handler (a group, or a recipe for
    an external tool) gets its description only: a recipe's params describe
    the tool's flags for completion, and the tool's own ``--help`` is the
    authority on its usage.  The first line is always the short description
    (used in command listings).
    """
    if func is None:
        return help or ""
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

    * Has handler, no children → Python command (parses args, calls handler).
    * Has children, no handler → group (prints its sub-commands when run bare).
      A node never has both — :meth:`command` and :meth:`__call__` refuse.
    * Tree contains no handler anywhere → external recipe (completion only;
      execution shells out via PTY).

    A flat command is just a root with no children: completion, the status
    bar and dispatch all go through the same per-node rules.  A node's flags
    are its own — nothing is inherited from ancestors.
    """

    name: str
    func: Callable | None = None
    params: list[Arg] | None = None
    help: str | None = None   # as declared; the handler's docstring stands in when None
    description: str = ""     # first line of help, shown in listings and as CmdParser description
    help_text: str = ""
    delegate: Completer | None = None  # answers every completion slot (see registry.command)
    sync: bool = False        # run on the main thread, not a backgroundable slot
    pass_context: bool = False  # handler takes a CommandContext as its first argument
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

    # ── Completion, derived from params ──────────────────────────────────

    def options_completer(self) -> OptionsCompleter | None:
        return options_completer(self.params or [])

    def positional_completer(self, pos_idx: int) -> Completer | None:
        return positional_completer(self.params or [], pos_idx)

    def takes_value(self, flag: str) -> bool:
        return flag_takes_value(self.params or [], flag)

    # ── Tree builder ─────────────────────────────────────────────────────

    def command(
        self,
        name: str,
        params: list[Arg] | None = None,
        help: str | None = None,
        pass_context: bool = False,
    ) -> "Command":
        """Create (or re-declare) a child node and return it.

        Two forms — a plain call for a group, a decorator for a command::

            s3 = aws.command("s3", help="Amazon S3")

            @s3.command("ls", params=[arg("path", nargs="?")])
            def s3_ls(path=None):
                ...

        Either way the result is the child :class:`Command`; as a decorator,
        :meth:`__call__` attaches the handler and returns the node.
        """
        if self.func is not None:
            raise ValueError(
                f"{self._full_name()!r} has a handler, so it can't have "
                f"sub-command {name!r} too"
            )
        if name in self.children:
            # Re-declaration: update params/help on the existing node so a
            # later `.command("foo", ...)` call can populate a node first
            # created as a group.
            child = self.children[name]
            if params is not None:
                child.params = params
            if help is not None:
                child.help = help
                child.description = help
            if pass_context:
                child.pass_context = True
            child.help_text = _build_help_text(child.help, child.func,
                                               child._full_name(), child.params)
            return child

        # Children always carry a params list (empty when none were given),
        # so running one with stray arguments is an argparse error rather
        # than a TypeError from the handler.
        child = Command(
            name=name,
            params=params if params is not None else [],
            help=help,
            description=help or "",
            parent=self,
            pass_context=pass_context,
        )
        child.help_text = _build_help_text(help, None, child._full_name(), child.params)
        self.children[name] = child
        return child

    def __call__(self, func: Callable) -> "Command":
        """Decorator hook: ``@node.command("ls", ...)`` returns the node,
        and Python applies it to the function — attach the handler here."""
        if self.children:
            raise ValueError(
                f"{self._full_name()!r} has sub-commands, so it can't have a "
                f"handler too"
            )
        self.func = func
        self.description = _effective_description(self.help, func)
        self.help_text = _build_help_text(self.help, func, self._full_name(), self.params)
        return self

    # ── Resolution & dispatch ────────────────────────────────────────────

    def resolve(self, tokens: list[str]) -> tuple["Command", list[str]]:
        """Walk down the tree consuming sub-command names from *tokens*.

        Flags (and the value of a value-taking flag of the node reached so
        far) are skipped during the walk so they may appear at any position.

        Returns ``(node, remaining)`` where *remaining* are the tokens that
        were **not** consumed as sub-command names — flag tokens stay,
        positional args stay, only the matched sub-command name tokens are
        dropped.  The resolved node's argparse parses *remaining* directly.
        """
        node = self
        consumed_indices: set[int] = set()
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if _is_flag_name(tok):
                i += 2 if node.takes_value(tok) else 1
                continue
            if tok in node.children:
                node = node.children[tok]
                consumed_indices.add(i)
                i += 1
                continue
            break
        remaining = [t for k, t in enumerate(tokens) if k not in consumed_indices]
        return node, remaining

    def invoke(self, args: list[str] | tuple[str, ...], *lead, ctx=None):
        """Run this command (or the sub-command *args* resolve to).

        *lead* are positional values passed ahead of the parsed arguments —
        a decorator's handler gets the wrapped ``Pipeline`` this way.  *ctx*
        (a :class:`~eosh.command_context.CommandContext`) goes ahead of
        those when the resolved node was declared with ``pass_context=True``.

        Returns what the handler returned — the shell takes an ``int`` as
        the exit status — or ``2`` when argparse rejected the arguments
        (its own convention; the error is already printed).
        """
        node, remaining = self.resolve(list(args)) if self.children else (self, list(args))
        if node.pass_context:
            lead = (ctx, *lead)
        return node._invoke_self(remaining, lead)

    def _invoke_self(self, args: list[str], lead: tuple = ()):
        if self.func is None:
            if self.children:
                _print_group_help(self)
            else:
                print(self.help_text or f"{self.name}: no handler")
            return None
        if self.params is None:
            # A flat command declared without params: positional *args.
            return self.func(*lead, *args)
        ns = _build_parser(self._full_name(), self.params, self.description or None).parse_args(args)
        if ns is None:
            return 2   # usage error (or --help), already printed
        return self.func(*lead, **vars(ns))

    def _full_name(self) -> str:
        """Space-separated full path from root, used in usage and errors."""
        parts: list[str] = []
        n: Command | None = self
        while n is not None:
            parts.append(n.name)
            n = n.parent
        return " ".join(reversed(parts))


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
        self._aliases: dict[str, str] = {}
        # What `reload` restores: the built-in entries themselves, not just
        # their names, so a built-in a config replaced (override=True) comes
        # back once the config stops replacing it.
        self._builtins: dict[str, Command] = {}
        self._builtin_aliases: dict[str, str] = {}
        self._defining_builtins = False

    def command(
        self,
        name: str,
        params: list[Arg] | None = None,
        help: str | None = None,
        delegate: Completer | None = None,
        sync: bool = False,
        override: bool = False,
        pass_context: bool = False,
    ) -> Command:
        """Register a top-level command, group, or external recipe; return it.

        Two forms, both returning the :class:`Command`:

        * **Plain call** — ``git = registry.command("git", help=..., params=[...])``
          for a group or an external-command recipe; chain ``.command(...)``
          on the result to add sub-commands.  A tree with no handler
          anywhere is completion-only: running it shells out to the real
          executable.
        * **Decorator** — ``@registry.command("greet", params=[...])`` (or
          ``name="greet"``) attaches the decorated function as the handler.

        ``delegate`` — a single :class:`Completer` that answers **every**
        completion slot (flags and every positional), for an external tool
        that ships its own completion protocol (``aws_completer``, cobra's
        ``__complete``).  Cannot be combined with ``params``.

        ``sync`` — run on the main thread instead of a background-thread
        slot.  For commands that finish at once or change the shell's own
        state (``cd``, ``var``, ``context``): no slot, no raw-mode
        forwarding, no output proxy, and the state changes on the thread
        that owns it.  The cost is that Ctrl+] can't background it while it
        runs.  On Windows every Python command runs this way.

        ``pass_context`` — call the handler with a
        :class:`~eosh.command_context.CommandContext` as its first argument
        (ahead of a decorator's pipeline): ``ctx.input()``, ``ctx.choose()``,
        ``ctx.run_interactive()``, the command's own context's variables, ….

        ``override`` — replace a built-in (``cd``, ``help``, ``@watch``, …).
        Without it, a built-in name is refused with a ``config warning:``
        line and the built-in stays; the returned node is detached, so the
        rest of the config still runs.  ``reload`` restores the built-in.
        """
        if not isinstance(name, str):
            raise TypeError("registry.command() needs a name: "
                            "registry.command('name', ...)")
        if delegate is not None and params:
            raise ValueError(f"{name!r}: delegate= and params= are exclusive")
        cmd = Command(
            name=name,
            params=params,
            help=help,
            description=help or "",
            help_text=_build_help_text(help, None, name, params),
            delegate=delegate,
            sync=sync,
            pass_context=pass_context,
        )
        if self._defining_builtins:
            self._builtins[name] = cmd
        elif name in self._builtins and not override:
            config_warning(f"{name!r} is a built-in command — not replaced "
                           f"(pass override=True to replace it)")
            return cmd
        self._commands[name] = cmd
        return cmd

    def get(self, name: str) -> Command | None:
        return self._commands.get(name)

    def list_commands(self) -> list[str]:
        return list(self._commands.keys())

    def has(self, name: str) -> bool:
        return name in self._commands

    def remove(self, name: str) -> None:
        """Forget command *name* (no-op if it isn't registered)."""
        self._commands.pop(name, None)

    @contextmanager
    def defining_builtins(self):
        """Register the shell's own commands: what is registered inside the
        block — and only that — becomes the built-in set, which ``reload``
        keeps and a config needs ``override=True`` to replace."""
        self._builtins = {}
        self._builtin_aliases = {}
        self._defining_builtins = True
        try:
            yield self
        finally:
            self._defining_builtins = False

    def mark_builtins(self) -> None:
        """Snapshot every current command and alias as built-in (for tests
        that build a registry by hand; the shell uses
        :meth:`defining_builtins`)."""
        self._builtins = dict(self._commands)
        self._builtin_aliases = dict(self._aliases)

    def is_builtin(self, name: str) -> bool:
        """True if *name* is registered and is the built-in entry itself
        (not a config's override of it)."""
        cmd = self._commands.get(name)
        return cmd is not None and self._builtins.get(name) is cmd

    def clear_user_commands(self) -> None:
        """Back to the built-ins: drop every command and alias a config
        added, and put back any built-in it replaced."""
        self._commands = dict(self._builtins)
        self._aliases = dict(self._builtin_aliases)

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
        if self._defining_builtins:
            self._builtin_aliases[name] = expansion

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
