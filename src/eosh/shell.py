"""Main shell loop — input handling, command dispatch, completion integration."""

from __future__ import annotations

import contextlib
import difflib
import io
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# PTY multiplexing and raw-mode forwarding are POSIX-only.  On Windows these
# modules are absent and the code paths that use them are never reached (the
# shell runs external commands on the real console — see _execute_external).
IS_WINDOWS = os.name == "nt"
if not IS_WINDOWS:
    import termios

from . import terminal
from .commands import (
    _FLAG_PREFIXES, arg, Command,
    registry as command_registry,
)
from .completion import (
    CommandNameCompleter,
    CompletionContext,
    FileCompleter,
    Completion,
    get_argcomplete_fallback,
)
from .variables import EnvVar, registry as var_registry, VarCompleter
from .command_context import CommandContext, ShellView
from .context import ContextManager
from .history import HistoryStore, norm_dir
from .lineedit import CONTEXT_CHANGED_SENTINEL, LineEditor
from .parsing import expand_vars, split_for_completion, tokenize
from .paths import config_dir
from .pipeline import (
    DecoratorParseError,
    Sequence,
    Stage,
    Pipeline,
    expand_globs,
    parse_line,
    set_pipeline_executor,
    split_on_operators,
)
from . import hooks, notify, shell_integration
from . import keys as keymap
from . import job as job_mod
from .job import PipelineSlot
from .colors import set_color_scheme
from .prompt import get_prompt_func, set_prompt
from .switcher import ContextSwitcher
from .slots import (
    _PyStageHandle,
    _dup_threadlocal_override_fd,
    _in_pipeline,
    _job_local,
    _read_from_user,
    _run_interactive,
    _stdin_is_tty,
    install_stdio_routers,
    run_handler,
)

def _find_switch_key(data: bytes) -> int:
    """Where the first context-switch key (``prompt.switch_context``) starts
    in a chunk of raw input, or -1."""
    hits = [i for i in (data.find(seq) for seq in keymap.sequences("prompt.switch_context"))
            if i >= 0]
    return min(hits) if hits else -1







_DEFAULT_CONFIG_PATH = Path(__file__).parent / "_config.py"


def _isatty(f) -> bool:
    try:
        return f is not None and os.isatty(f.fileno())
    except (OSError, ValueError, AttributeError):
        return False


def _stage_label_of(pipeline: Pipeline) -> str:
    return " | ".join(_stage_label(st) for st in pipeline.stages)


def _stage_label(stage: Stage) -> str:
    """How a stage reads in the switcher and a notification."""
    call = stage.decorator
    if call is None:
        return stage.text
    body = " | ".join(_stage_label(st) for st in call.body.stages)
    return " ".join([f"@{call.name}", *call.flag_tokens, f"{{{body}}}"])


def _is_continuation(line: str) -> bool:
    """Return True if *line* ends with an unescaped backslash (line continuation).

    An even number of trailing backslashes means the last one is escaped (e.g.
    ``echo \\\\`` has two backslashes, none of which continue the line).
    Trailing spaces/tabs after the backslash are ignored so that accidental
    trailing whitespace does not prevent continuation from being recognised.
    """
    s = line.rstrip(" \t")
    count = 0
    for ch in reversed(s):
        if ch == "\\":
            count += 1
        else:
            break
    return count % 2 == 1


def _strip_continuation(line: str) -> str:
    """Remove the trailing continuation backslash (and any trailing whitespace before it).

    The result is ready to be concatenated with the next continuation line.
    Leading whitespace in the next line is preserved so indented continuations
    (the common style) work naturally::

        docker run --rm \\
          -v /foo:/bar      →  joined as  "docker run --rm   -v /foo:/bar"
    """
    return line.rstrip(" \t")[:-1]


def _positional_index(args: list[str], node: Command) -> int:
    """Return the number of positional (non-flag) arguments in *args*.

    Flags are skipped without counting: boolean flags advance by 1 token;
    *node*'s value-taking flags advance by 2 because they consume the
    following token as their value.
    """
    pos = 0
    i = 0
    while i < len(args):
        token = args[i]
        if token.startswith(_FLAG_PREFIXES):
            i += 2 if node.takes_value(token) else 1
        else:
            pos += 1
            i += 1
    return pos


@dataclass
class _Slot:
    """What one token of a command line is — the single answer that TAB
    completion and the status bar both act on.

    *kind* is one of:

    * ``"delegate"`` — the command's ``delegate`` completer answers every slot;
    * ``"flag"`` — a flag being typed, and the node has flags;
    * ``"value"`` — the value of *flag*, the value-taking flag just before it;
    * ``"subcommand"`` — the first positional of a group: a child's name;
    * ``"positional"`` — positional slot *pos_idx* of *node*;
    * ``"unknown"`` — no registered command (*node* is ``None``).

    *args* are the tokens after the resolved node, sub-command names removed.
    """
    node: Command | None
    args: list[str]
    kind: str
    flag: str = ""
    pos_idx: int = 0


def _resolve_slot(cmd: Command | None, args: list[str], token: str) -> _Slot:
    """Classify *token*, typed after *args*, against command *cmd*.

    A flat command is a node with no children, so the same walk covers both:
    resolve to the deepest sub-command, then look at that node's own params.
    """
    if cmd is None:
        return _Slot(None, args, "unknown", pos_idx=len(args))
    node, rest = cmd.resolve(args)
    if node.delegate is not None:
        return _Slot(node, rest, "delegate")
    if token.startswith(_FLAG_PREFIXES) and node.options_completer() is not None:
        return _Slot(node, rest, "flag")
    if rest and rest[-1].startswith(_FLAG_PREFIXES) and node.takes_value(rest[-1]):
        return _Slot(node, rest, "value", flag=rest[-1])
    pos_idx = _positional_index(rest, node)
    if node.children and pos_idx == 0:
        return _Slot(node, rest, "subcommand")
    return _Slot(node, rest, "positional", pos_idx=pos_idx)


def _flag_label(flag: str, arg_hint: str, description: str) -> str:
    """Consistent status-bar label for a flag, used across all call sites."""
    if arg_hint and description:
        return f"{flag} <{arg_hint}>: {description}"
    if description:
        return f"{flag}: {description}"
    if arg_hint:
        return f"{flag} <{arg_hint}>"
    return flag


def _label_from_arg(param) -> str:
    """Format an Arg descriptor as a status-bar label."""
    name = param.names[0]
    help_text = param.kwargs.get("help", "")
    if help_text:
        return f"{name}: {help_text}"
    return name


def _positional_label(cmd, pos_idx: int, command_name: str, args: list[str]) -> str:
    """Return a status-bar label for the positional argument at *pos_idx*.

    First consults the slot's completer via ``describe_slot(args, pos_idx)``
    so completers whose role depends on preceding args (e.g. tar's first
    positional, which flips between archive and member when ``-f`` is used)
    can override the static label.  Falls back to the ``help=`` text on the
    matching ``arg()`` descriptor — wildcard positionals reuse their label
    for every slot from their declared position onward.
    """
    if cmd is None or cmd.params is None:
        return f"arg {pos_idx + 1}"
    completer = cmd.positional_completer(pos_idx)
    if completer is not None:
        dynamic = completer.describe_slot(args, pos_idx)
        if dynamic is not None:
            return dynamic
    from .commands import _is_flag_name
    positionals = [a for a in cmd.params if a.names and not _is_flag_name(a.names[0])]
    if not positionals:
        return f"arg {pos_idx + 1}"
    if pos_idx < len(positionals):
        return _label_from_arg(positionals[pos_idx])
    # Beyond the declared list: if the last positional is a wildcard, reuse
    # its label for every subsequent slot.
    last = positionals[-1]
    if last.kwargs.get("nargs") in ("*", "+"):
        return _label_from_arg(last)
    return f"arg {pos_idx + 1}"


# ── source-bash: run a bash script, then import its environment ────────────
#
# The whole point of `source-bash` is the *import*: a child bash exits and
# takes its `export`s with it, so the wrapper below arranges for the child to
# dump its final environment and cwd where the parent can read them back.
#
# The dump is NUL-delimited (a value may contain newlines, but never a NUL)
# with the cwd as the first record, and it is written from an EXIT trap rather
# than a trailing line so a script that ends in `exit 1` — or dies under
# `set -e` — still hands its environment back.
_BASH_ENV_DUMP_WRAPPER = """\
__eosh_dump() {{ {{ printf '%s\\0' "$PWD"; env -0; }} > {dump} 2>/dev/null; }}
trap __eosh_dump EXIT
{body}
"""

# Bash bookkeeping that says nothing about the script's intent.  `_` holds the
# last argument of the last command, `SHLVL` counts nesting, `PWD`/`OLDPWD` are
# handled as the cwd instead, and `BASH_FUNC_*` are exported shell functions in
# an encoding only bash understands.
_BASH_ENV_SKIP = frozenset({
    "_", "PWD", "OLDPWD", "SHLVL", "BASHOPTS", "SHELLOPTS",
    "BASH_EXECUTION_STRING",
})
_BASH_ENV_SKIP_PREFIXES = ("BASH_FUNC_",)

# Only plain identifiers are considered for *removal*.  A key bash cannot bind
# to a variable (`foo-bar=1`, put in the environment by some other program) may
# be missing from the dump without the script having unset anything.
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _bash_env_ignored(key: str) -> bool:
    """True when *key* must not be imported back from a bash environment dump."""
    return key in _BASH_ENV_SKIP or key.startswith(_BASH_ENV_SKIP_PREFIXES)


def _parse_bash_env_dump(data: str) -> tuple[str | None, dict[str, str]]:
    """Split a NUL-delimited ``cwd\\0KEY=VALUE\\0…`` dump into (cwd, env).

    Returns ``(None, {})`` for an empty dump — which is how the caller learns
    the child never reached its EXIT trap (killed, or the script installed a
    trap of its own).
    """
    records = data.split("\0")
    if records and records[-1] == "":
        records.pop()
    if not records:
        return None, {}
    env: dict[str, str] = {}
    for record in records[1:]:
        key, sep, value = record.partition("=")
        if sep and key:
            env[key] = value
    return records[0], env


class Shell(ContextSwitcher):
    def __init__(self):
        # Enable VT output / disable newline translation (Windows) before any
        # rendering or stdout wrapping happens.
        terminal.init()
        os.environ.setdefault("PWD", os.getcwd())
        self.registry = command_registry
        self.context_manager = ContextManager()
        self.context_manager.create("default")
        # True while the line currently being executed handed its work to a
        # background context — see _execute / _park.
        self._backgrounded = False
        # Set by the `exit` built-in; run() ends after the current line.
        self._exit_requested = False
        # Everything registered here is built-in: `reload` keeps it, and a
        # config needs override=True to replace it.
        with self.registry.defining_builtins(), var_registry.defining_builtins():
            self._register_builtins()
        self._load_user_config()
        # (context, cwd) as the hooks last saw it — see _notice_state_change.
        self._seen_state = self._observed_state()
        # The line _execute is running, for _park to hand to the slot.
        self._current_line: str | None = None
        install_stdio_routers()
        if not IS_WINDOWS and _stdin_is_tty():
            job_mod.prewarm()           # a line never waits for its job leader

        # The history every eosh process shares (SQLite — see history.py).
        self._history = HistoryStore(config_dir() / "history.db")
        # The history row of the line _execute is running, for _park.
        self._current_history_id: int | None = None
        # Seed the default context's per-session Up/Down list from it. New
        # contexts snapshot their parent's list at create time; Ctrl+R and
        # the ghost suggestion always query the shared store.
        default_ctx = self.context_manager.current()
        if default_ctx is not None:
            default_ctx.history = self._history.recent_commands()
        self._line_editor = LineEditor(
            history=self._history,
            get_completions=self._get_completions,
            get_prompt=lambda: get_prompt_func()(self.context_manager),
            switch_fn=self._handle_switch,
            get_arg_info=self._get_arg_info,
            local_history_fn=self._current_context_history,
            suggest_fn=self._suggest,
        )

        self._command_completer = CommandNameCompleter(self.registry)
        self._file_completer = FileCompleter()
        self._var_completer = VarCompleter()

        # Wire Pipeline.run() so decorator bodies can re-enter execution.
        set_pipeline_executor(self._run_pipeline_from_decorator)

    def _suggest(self, buf: str) -> str | None:
        """The ghost suggestion for *buf*: the latest line run in this
        directory — by any context, in any eosh process — that extends it."""
        try:
            cwd = os.getcwd()
        except OSError:
            return None
        return self._history.suggest(buf, cwd)

    def _record_history(self, line: str) -> int | None:
        """Add *line* to the shared history and the context's Up/Down list."""
        self._line_editor.add_to_history(line)
        return self._history.add(line, ctx=self.context_manager.current_name)

    def _current_context_history(self) -> list[str]:
        """Return the current context's per-session Up/Down history list.

        This is the actual mutable list on the Context, so the line editor
        both reads (Up/Down) and appends (on Enter) through it. Falls back to
        an ephemeral list if somehow there is no active context.
        """
        ctx = self.context_manager.current()
        if ctx is None:
            return []
        return ctx.history

    def _maybe_decorator_completion(
        self,
        tokens: list[str],
        prefix: str,
        line_before_cursor: str,
    ):
        """Handle TAB completion for ``@name [flags] <body>`` lines.

        Returns one of three things:

        * ``None`` — the line is not decorator-prefixed.
        * ``("return", (completions, prefix, label))`` — decorator-specific
          completions; caller returns this directly.
        * ``("fallthrough", (stripped_tokens, prefix, stripped_line))`` —
          cursor is in the body; caller continues with normal completion
          using the rewritten locals.
        """
        # Case A: ``@<partial>`` — completing the decorator name itself.
        if not tokens and prefix.startswith("@"):
            completions = [
                Completion(value=name, description=self.registry.get(name).description)
                for name in sorted(self.registry.list_commands())
                if name.startswith("@") and name.startswith(prefix)
            ]
            return "return", (completions, prefix, "decorator")

        # Case B/C: a decorator name has already been typed.
        if not tokens or not tokens[0].startswith("@"):
            return None
        deco = self.registry.get(tokens[0])
        if deco is None:
            return None  # unknown decorator; fall through (treats it like a command)

        # Walk past the decorator's flags to find where the body starts.
        body_start = len(tokens)
        i = 1
        while i < len(tokens):
            tok = tokens[i]
            if tok == "--":
                body_start = i + 1
                break
            if not tok.startswith(("-", "+")):
                body_start = i
                break
            i += 2 if ("=" not in tok and deco.takes_value(tok)) else 1

        # Case B: still in the decorator's flags (``@watch -<TAB>``,
        # ``@watch -n <TAB>``) — the decorator is a command, so its flags and
        # their values complete exactly as a command's do.
        if body_start >= len(tokens):
            slot = _resolve_slot(deco, tokens[1:], prefix)
            if slot.kind in ("flag", "value"):
                ctx = CompletionContext(
                    command=deco.name,
                    args=slot.args,
                    arg_index=len(slot.args),
                    prefix=prefix,
                    line=line_before_cursor,
                    shell_context=self._shell_view(),
                )
                flags = deco.options_completer()
                if slot.kind == "flag":
                    found = flags.complete(ctx) if flags.should_activate(ctx) else []
                    return "return", (found, prefix, f"{deco.name} option")
                flag, arg_hint, description, value_completer = flags.get_preceding_flag_hint(ctx)
                found = value_completer.complete(ctx) if value_completer else []
                return "return", (found, prefix, _flag_label(flag, arg_hint, description))

        # Case C: cursor is in the body.  Strip the decorator portion and
        # let normal completion run on what's left.  We rebuild the line
        # so downstream completers see the body as if typed at top level.
        stripped_tokens = tokens[body_start:]
        stripped_line = self._strip_leading_tokens(
            line_before_cursor, len(tokens) - len(stripped_tokens)
        )
        return "fallthrough", (stripped_tokens, prefix, stripped_line)

    @staticmethod
    def _strip_leading_tokens(line: str, n: int) -> str:
        """Drop the first *n* whitespace-separated tokens from *line*.

        Tokens that contain quotes are tracked correctly so a quoted
        decorator-flag value isn't double-counted.
        """
        i = 0
        ln = len(line)
        dropped = 0
        while dropped < n and i < ln:
            # Skip leading whitespace
            while i < ln and line[i] in (" ", "\t"):
                i += 1
            if i >= ln:
                break
            # Read one token
            while i < ln and line[i] not in (" ", "\t"):
                ch = line[i]
                if ch in ('"', "'"):
                    quote = ch
                    i += 1
                    while i < ln and line[i] != quote:
                        if line[i] == "\\" and quote == '"' and i + 1 < ln:
                            i += 2
                        else:
                            i += 1
                    i += 1
                    continue
                if ch == "\\" and i + 1 < ln:
                    i += 2
                    continue
                i += 1
            dropped += 1
        # Skip whitespace after the dropped tokens — preserve a single
        # space so split_for_completion still sees an "empty prefix" if the
        # cursor is mid-whitespace.
        while i < ln and line[i] in (" ", "\t"):
            i += 1
        return line[i:]

    def _get_completions(self, line_before_cursor: str) -> tuple[list[Completion], str, str]:
        # Isolate the current pipeline stage so completions for `ls | grep -`
        # are computed against `grep`, not `ls`.
        stage_line = split_on_operators(line_before_cursor, [";", "&&", "||", "|"])[-1][1]
        tokens, prefix = split_for_completion(stage_line)

        # Decorator-prefixed stage (e.g. ``@watch -n 1 ls -l <TAB>``): handle
        # the ``@``-name and decorator-flag completions here, then strip the
        # decorator's tokens from the line and fall through so the wrapped
        # command's completer sees ``ls -l <TAB>``.
        deco_result = self._maybe_decorator_completion(tokens, prefix, line_before_cursor)
        if deco_result is not None:
            kind, payload = deco_result
            if kind == "return":
                return payload  # (completions, prefix, label)
            # kind == "fallthrough"
            tokens, prefix, line_before_cursor = payload

        # Expand the first token if it is an alias, so completions for
        # `hp <TAB>` come from the expansion's resolved command.
        if tokens:
            expansion = self.registry.get_alias(tokens[0])
            if expansion is not None:
                expansion_tokens = tokenize(expansion)
                if expansion_tokens:
                    tokens = expansion_tokens + tokens[1:]

        if not tokens:
            # Bare KEY=VALUE assignment (e.g. "aws_region=us-<TAB>"): delegate
            # to VarCompleter for value-side completion.
            if "=" in prefix:
                ctx = CompletionContext(
                    command=None,
                    args=[],
                    arg_index=0,
                    prefix=prefix,
                    line=line_before_cursor,
                    shell_context=self._shell_view(),
                )
                return self._var_completer.complete(ctx), prefix, "variable"

            ctx = CompletionContext(
                command=None,
                args=[],
                arg_index=0,
                prefix=prefix,
                line=line_before_cursor,
                shell_context=self._shell_view(),
            )
            return self._command_completer.complete(ctx), prefix, "command"

        command_name = tokens[0]
        args = tokens[1:]
        arg_index = len(args)

        ctx = CompletionContext(
            command=command_name,
            args=args,
            arg_index=arg_index,
            prefix=prefix,
            line=line_before_cursor,
            shell_context=self._shell_view(),
        )

        slot = _resolve_slot(self.registry.get(command_name), args, prefix)
        node = slot.node
        # Completers see the tokens after the resolved node — for a flat
        # command that is every argument.
        ctx = CompletionContext(
            command=command_name,
            args=slot.args,
            arg_index=len(slot.args),
            prefix=prefix,
            line=line_before_cursor,
            shell_context=self._shell_view(),
        )

        # A registered completer that returns [] means "nothing here"; only a
        # slot with *no* completer falls back to argcomplete and then files.
        has_completer = slot.kind != "positional" and slot.kind != "unknown"
        completions: list[Completion] = []
        label = command_name

        if slot.kind == "delegate":
            if node.delegate.should_activate(ctx):
                completions = node.delegate.complete(ctx)
        elif slot.kind == "flag":
            flags = node.options_completer()
            if flags.should_activate(ctx):
                completions = flags.complete(ctx)
            label = f"{command_name} option"
        elif slot.kind == "value":
            # The slot belongs to the flag's value: its value completer, or
            # nothing — never positional/file candidates.  With nothing to
            # offer, the status bar (``_get_arg_info``) says what to type.
            flag, arg_hint, description, value_completer = (
                node.options_completer().get_preceding_flag_hint(ctx))
            if value_completer is not None:
                completions = value_completer.complete(ctx)
            label = _flag_label(flag, arg_hint, description)
        elif slot.kind == "subcommand":
            completions = [
                Completion(value=name, description=node.children[name].description)
                for name in sorted(node.children) if name.startswith(prefix)
            ]
            label = f"{command_name} subcommand"
        elif slot.kind == "positional":
            positional = node.positional_completer(slot.pos_idx)
            if positional is not None:
                has_completer = True
                if positional.should_activate(ctx):
                    completions = positional.complete(ctx)
                label = _positional_label(node, slot.pos_idx, command_name, slot.args)

        # argcomplete fallback: the de-facto Python CLI completion library
        # (pipx, conda, pre-commit, tox, pdm, httpie, …).  Detection is done
        # by inspecting the script for the ``PYTHON_ARGCOMPLETE_OK`` marker,
        # so it never invokes side-effecting tools blindly.  (Cobra tools
        # have no fallback — they are opted in per command as a
        # ``delegate``; see ``eosh.recipes.enable_cobra``.)
        if not has_completer:
            argc = get_argcomplete_fallback()
            if argc is not None and argc.should_activate(ctx):
                completions = argc.complete(ctx)

        if not completions and not has_completer:
            completions = self._file_completer.complete(ctx)

        return completions, prefix, label

    def _get_arg_info(self, buf: str, cursor: int) -> str | None:
        """Return a status-bar description for the token the caret sits on.

        Handles three cases:
        - Flag token (``--flag``): returns ``"--flag: description"``
        - Flag value (token immediately after a value-taking flag): returns
          the flag's own description so the context stays visible
        - Positional arg: returns the param name (and help text when available)
        """
        # Extract the word surrounding cursor (scan left and right past non-space).
        start, end = cursor, cursor
        while start > 0 and buf[start - 1] not in (" ", "\t"):
            start -= 1
        while end < len(buf) and buf[end] not in (" ", "\t"):
            end += 1
        token = buf[start:end]

        # Parse everything before the token to get the command/args context.
        pre = buf[:start].rstrip()
        stage_pre = split_on_operators(pre, [";", "&&", "||", "|"])[-1][1]
        tokens_before, _ = split_for_completion(stage_pre + " ")

        if not tokens_before:
            if not token:
                return None
            # Caret is on the command name itself — show its help text.
            cmd = self.registry.get(token)
            if cmd is not None and cmd.description:
                return f"{token}: {cmd.description}"
            # If the token is an alias, fall back to the alias's expansion
            # (and the description of whatever it resolves to).
            expansion = self.registry.get_alias(token)
            if expansion is not None:
                expansion_tokens = tokenize(expansion)
                if expansion_tokens:
                    target = self.registry.get(expansion_tokens[0])
                    if target is not None and target.description:
                        return f"{token} → {expansion}: {target.description}"
                return f"{token} → {expansion}"
            return None

        command_name = tokens_before[0]
        preceding_args = tokens_before[1:]

        # Expand the leading token if it is an alias, so the status bar for
        # `hp <args>` resolves against the alias's expansion (e.g.
        # `awsut sagemaker hyperpod`).  Mirrors the alias handling in
        # _get_completions.
        expansion = self.registry.get_alias(command_name)
        if expansion is not None:
            expansion_tokens = tokenize(expansion)
            if expansion_tokens:
                command_name = expansion_tokens[0]
                preceding_args = expansion_tokens[1:] + preceding_args

        slot = _resolve_slot(self.registry.get(command_name), preceding_args, token)
        node = slot.node
        if slot.kind in ("unknown", "delegate"):
            return None   # nothing declared to describe

        if token.startswith(_FLAG_PREFIXES) or slot.kind == "value":
            # The flag itself, or the value of the flag just before the caret.
            flags = node.options_completer()
            flag = slot.flag or token
            if flags is None:
                return None
            label = _flag_label(flag, flags.args.get(flag, ""), flags.options.get(flag, ""))
            return label if label != flag else None

        if slot.kind == "subcommand":
            # The child's description — or, for a partial name that doesn't
            # match a child yet, just say what the slot is.
            child = node.children.get(token)
            if child is None:
                return f"{command_name} subcommand"
            if child.description:
                return f"{command_name} {token}: {child.description}"
            return f"{command_name} {token}"

        return _positional_label(node, slot.pos_idx, command_name, slot.args)

    def _register_builtins(self) -> None:
        from .completion import (
            CallbackCompleter, ChoiceCompleter, Completer, Completion, DirCompleter,
        )

        # cd, var and source-bash change only the context they run in, and
        # do it through ctx — so, unlike the commands that change the whole
        # shell (context, alias, reload, exit), they need not be `sync`:
        # one that is still running after Ctrl+] changes its own context.

        @self.registry.command(
            name="cd",
            help="Change directory.",
            params=[arg("path", nargs="?", default="~", completer=DirCompleter())],
            pass_context=True,
        )
        def cd(ctx, path):
            try:
                ctx.chdir(path)
            except OSError as e:
                print(f"cd: {e}", file=sys.stderr)
                return 1

        @self.registry.command(name="exit", help="Exit the shell.", sync=True)
        def exit_shell():
            # A pipeline stage is a subshell in POSIX terms: `exit | cat`
            # does not end the shell.
            if getattr(_in_pipeline, "flag", False):
                return
            running = self._running_contexts()
            if running and not self._confirm_exit(running):
                return 1
            # A request, not a SystemExit: a handler's SystemExit is just an
            # exit status (see run_handler).  run() ends after this line.
            self._exit_requested = True

        @self.registry.command(name="reload", help="Reload ~/.eosh/config.py.", sync=True)
        def reload_config():
            self._reload_config()

        config = self.registry.command(
            "config", help="Work with ~/.eosh/config.py.", sync=True)

        @config.command("edit", help="Open config.py in $VISUAL / $EDITOR, "
                                     "then reload it when the editor exits.")
        def config_edit():
            return self._edit_config()

        @self.registry.command(
            name="var",
            help=(
                "Set, unset, or list context variables.\n\n"
                "  var              list all registered vars and env vars\n"
                "  var NAME         print current value of NAME\n"
                "  var NAME=VALUE   set NAME to VALUE\n"
                "  var NAME=        unset NAME (remove from env)\n\n"
                "NAME may be a registered Python-backed variable (e.g. 'aws_region')\n"
                "or a plain environment variable.  Registered variables handle their\n"
                "own set logic (e.g. writing multiple env keys at once)."
            ),
            params=[arg("assignments", nargs="*", metavar="NAME[=VALUE]",
                        completer=VarCompleter())],
            pass_context=True,
        )
        def var_cmd(ctx, assignments):
            if not assignments:
                # List registered Python-backed vars first, then plain env.
                py_vars = var_registry.all()
                if py_vars:
                    print("[vars]")
                    for v in py_vars:
                        val = ctx.get_var(v.name)
                        val_str = val if val is not None else "(unset)"
                        desc = f"  # {v.description}" if v.description else ""
                        print(f"  {v.name}={val_str}{desc}")
                    print("[env]")
                for key, value in sorted(ctx.environ().items()):
                    print(f"  {key}={value}")
                return
            for assignment in assignments:
                if "=" in assignment:
                    key, _, value = assignment.partition("=")
                    if value == "":
                        ctx.unset_var(key)
                    else:
                        ctx.set_var(key, value)
                elif var_registry.get(assignment) is not None:
                    # 'var NAME' with no '=' → print current value of Python-backed var
                    val = ctx.get_var(assignment)
                    print(f"{assignment}={val}" if val is not None else f"{assignment}=(unset)")
                elif ctx.get_var(assignment) is not None:
                    # 'var NAME' for a plain env var → print its value
                    print(f"{assignment}={ctx.get_var(assignment)}")
                else:
                    print(f"var: invalid argument '{assignment}' (expected NAME=VALUE or NAME= to unset)")

        @self.registry.command(
            name="source-bash",
            help=(
                "Run a bash script and import its environment into this shell.\n\n"
                "  source-bash                 paste lines, end with a blank line or Ctrl+D\n"
                "  source-bash FILE [ARG…]     source FILE with the given arguments\n"
                "  source-bash -c 'TEXT'       run TEXT\n\n"
                "The script runs in a real bash — so `export`, `$(…)`, loops,\n"
                "heredocs and conditionals all behave as they do in bash — and its\n"
                "final environment and working directory are imported back here,\n"
                "the way bash's own `source` leaves them in the calling shell.\n\n"
                "Shell functions, aliases and shell options cannot be imported\n"
                "(eosh has no equivalent); only variables and the cwd come back."
            ),
            params=[
                arg("script", nargs="*", metavar="FILE|ARG", completer=FileCompleter()),
                arg("-c", "--command", metavar="TEXT",
                    help="run TEXT instead of a file"),
                arg("--no-cd", action="store_true",
                    help="keep the current directory, whatever the script left"),
                arg("-q", "--quiet", action="store_true",
                    help="don't print the summary of imported variables"),
            ],
            pass_context=True,
        )
        def source_bash_cmd(ctx, script, command, no_cd, quiet):
            if command is not None and script:
                print("source-bash: -c takes the whole script; don't pass a FILE too")
                return 2
            if command is not None:
                body = command
            elif script:
                path = os.path.expanduser(script[0])
                if not os.path.isfile(path):
                    print(f"source-bash: {script[0]}: no such file")
                    return 1
                body = " ".join(shlex.quote(a) for a in ["source", path, *script[1:]])
            else:
                body = _read_from_user(
                    "Paste bash lines; end with a blank line or Ctrl+D:\n",
                    block=True, what="source-bash",
                )
                if not body.strip():
                    print("source-bash: nothing to run")
                    return

            code, cwd, env = self._run_bash_script(body)
            if cwd is None:
                # No dump: the script replaced our EXIT trap, or bash died
                # before running it.  Importing an empty environment here
                # would unset everything the shell has.
                print("source-bash: environment not imported (script did not exit normally)")
                if code != 0:
                    print(f"source-bash: exit status {code}")
                return code or 1

            changed, removed, new_cwd = self._apply_bash_env(
                ctx, cwd, env, import_cwd=not no_cd
            )
            if code != 0:
                print(f"source-bash: exit status {code}")
            if quiet:
                return code
            # Names only, never values — a sourced script is exactly where an
            # AWS_SESSION_TOKEN comes from, and this line lands in the scrollback.
            parts = []
            if changed:
                parts.append(f"set: {', '.join(changed)}")
            if removed:
                parts.append(f"unset: {', '.join(removed)}")
            if new_cwd:
                parts.append(f"cwd: {new_cwd}")
            print(f"source-bash: {'; '.join(parts)}" if parts else "source-bash: no changes")
            # The script's own status is the command's — `source-bash x && …`.
            return code

        @self.registry.command(
            name="alias",
            sync=True,
            help=(
                "Define or list command aliases.\n\n"
                "  alias                  list all aliases\n"
                "  alias NAME             show the expansion of NAME\n"
                "  alias NAME=EXPANSION   define NAME as a shorthand for EXPANSION\n\n"
                "Aliases expand the first token of a command line.  Quote the\n"
                "expansion if it contains spaces:\n"
                "  alias hp='awsut sagemaker hyperpod'"
            ),
            params=[arg("assignments", nargs="*", metavar="NAME[=EXPANSION]")],
        )
        def alias_cmd(assignments):
            if not assignments:
                aliases = self.registry.list_aliases()
                if not aliases:
                    return
                for name in sorted(aliases):
                    print(f"alias {name}={aliases[name]!r}")
                return
            for assignment in assignments:
                if "=" in assignment:
                    name, _, expansion = assignment.partition("=")
                    if not name:
                        print(f"alias: invalid name in '{assignment}'")
                        continue
                    self.registry.alias(name, expansion)
                else:
                    expansion = self.registry.get_alias(assignment)
                    if expansion is None:
                        print(f"alias: {assignment}: not found")
                    else:
                        print(f"alias {assignment}={expansion!r}")

        @self.registry.command(
            name="unalias",
            sync=True,
            help="Remove one or more aliases.",
            params=[arg("names", nargs="+", metavar="NAME",
                        completer=CallbackCompleter(
                            lambda: sorted(self.registry.list_aliases())))],
        )
        def unalias_cmd(names):
            for name in names:
                if not self.registry.unalias(name):
                    print(f"unalias: {name}: not found")

        @self.registry.command(
            name="help",
            sync=True,
            help=(
                "Show help for a command, or list all commands.\n\n"
                "  help          every command\n"
                "  help NAME     one command\n"
                "  help keys     the key bindings: every action, its keys, what it does"
            ),
            params=[arg("command_name", nargs="?", default="",
                        completer=CallbackCompleter(
                            lambda: sorted({*self.registry.list_commands(), "keys"})))],
        )
        def help_cmd(command_name: str = ""):
            if command_name == "keys":
                # A topic, not a command: it wins over a command named keys.
                self._print_key_bindings()
            elif command_name:
                cmd = self.registry.get(command_name)
                if cmd:
                    print(f"{cmd.name}: {cmd.help_text or 'No help available.'}")
                else:
                    print(f"Unknown command: {command_name}")
            else:
                names = sorted(self.registry.list_commands())
                builtin = [n for n in names if self.registry.is_builtin(n)]
                user = [n for n in names if not self.registry.is_builtin(n)]
                for title, group in (
                    ("Built-in commands:", [n for n in builtin if not n.startswith("@")]),
                    ("Commands from your config:", [n for n in user if not n.startswith("@")]),
                    ("Decorators (@name [flags] {pipeline}):", [n for n in builtin if n.startswith("@")]),
                    ("Decorators from your config:", [n for n in user if n.startswith("@")]),
                ):
                    if not group:
                        continue
                    print(title)
                    for name in group:
                        cmd = self.registry.get(name)
                        desc = cmd.help_text.split("\n")[0] if cmd.help_text else ""
                        print(f"  {name:20s} {desc}")
                print("\n`help keys` lists the key bindings.")

        @self.registry.command(
            name="history",
            sync=True,
            help=(
                "List past command lines (every context and eosh process).\n\n"
                "  history              the last 25\n"
                "  history docker run   the last 25 containing every keyword\n"
                "  history -n 100 --here"
            ),
            params=[
                arg("keywords", nargs="*", metavar="KEYWORD",
                    help="only lines containing all of these (case-insensitive)"),
                arg("-n", type=int, default=25, metavar="N", dest="limit",
                    help="how many (default 25)"),
                arg("--here", action="store_true",
                    help="only lines run in this directory"),
            ],
        )
        def history_cmd(keywords, limit, here):
            entries = self._history.entries(
                limit, keywords=keywords, cwd=os.getcwd() if here else None)
            home = norm_dir(os.path.expanduser("~"))
            for e in entries:
                when = time.strftime("%m-%d %H:%M", time.localtime(e.ts))
                status = "" if not e.status else str(e.status)
                where = e.cwd or ""
                if where == home or where.startswith(home + os.sep):
                    where = "~" + where[len(home):]
                print(f"{when}  {status:>3}  {where.replace(os.sep, '/')}  {e.cmd}")

        _names_after_subcommands = {"switch", "kill"}

        class ContextNameCompleter(Completer):
            def __init__(self, cm):
                self._cm = cm

            def should_activate(self, ctx: CompletionContext) -> bool:
                return bool(ctx.args) and ctx.args[0] in _names_after_subcommands

            def complete(self, ctx: CompletionContext) -> list[Completion]:
                subcmd = ctx.args[0] if ctx.args else ""
                names = self._cm.list_contexts()
                if subcmd == "kill":
                    names = [
                        n for n in names
                        if self._cm.contexts[n].process_slot
                        and self._cm.contexts[n].process_slot.is_alive()
                    ]
                return [
                    Completion(value=n)
                    for n in names
                    if n.startswith(ctx.prefix)
                ]

        @self.registry.command(
            name="context",
            sync=True,
            help="Manage shell contexts: new, close, switch, list, kill.",
            params=[
                arg("subcommand", nargs="?", default="",
                    completer=ChoiceCompleter(["new", "close", "switch", "list", "kill"])),
                arg("name", nargs="?", default="",
                    completer=ContextNameCompleter(self.context_manager)),
            ],
        )
        def context_cmd(subcommand: str = "", name: str = ""):
            if not subcommand:
                ctx = self.context_manager.current()
                if ctx:
                    vars_str = f" {ctx.variables}" if ctx.variables else ""
                    print(f"Current: {ctx.name}{vars_str}")
                else:
                    print("No active context.")
                return

            if subcommand == "new":
                if not name:
                    print("Usage: context new <name>")
                    return
                if name in self.context_manager.contexts:
                    print(f"Context '{name}' already exists.")
                    return
                self.context_manager.new(name)
                self.context_manager.switch(name)
                print(f"Created context '{name}'")

            elif subcommand == "close":
                # The current context unless one is named; the same rules as
                # Ctrl+D in the Ctrl+] picker.
                target = name or self.context_manager.current_name
                if target not in self.context_manager.contexts:
                    print(f"No context named '{target}'")
                    return
                if len(self.context_manager.list_contexts()) <= 1:
                    print("Cannot close the last context.")
                    return
                slot = self.context_manager.contexts[target].process_slot
                if slot is not None and slot.is_alive():
                    print(f"Context '{target}' has a running process "
                          f"(context kill {target} first).")
                    return
                self.context_manager.remove(target)
                print(f"Closed '{target}', now in '{self.context_manager.current_name}'")

            elif subcommand == "switch":
                if not name:
                    print("Usage: context switch <name>")
                    return
                try:
                    self.context_manager.switch(name)
                except KeyError as e:
                    print(e)

            elif subcommand == "list":
                names = self.context_manager.list_contexts()
                if not names:
                    print("No contexts defined.")
                else:
                    current = self.context_manager.current_name
                    ordered = ([current] if current else []) + [n for n in names if n != current]
                    for n in ordered:
                        marker = "*" if n == current else " "
                        ctx = self.context_manager.contexts[n]
                        state = ctx.state.name.lower()
                        if state == "idle":
                            state_str = ""
                        elif state == "running" and ctx.process_slot and ctx.process_slot.argv:
                            cmd = " ".join(ctx.process_slot.argv)
                            state_str = f" (running: {cmd})"
                        else:
                            state_str = f" ({state})"
                        vars_str = f" {ctx.variables}" if ctx.variables else ""
                        print(f"  {marker} {n}{state_str}{vars_str}")

            elif subcommand == "kill":
                if not name:
                    print("Usage: context kill <name>")
                    return
                if name not in self.context_manager.contexts:
                    print(f"No context named '{name}'")
                    return
                target_ctx = self.context_manager.contexts[name]
                if target_ctx.process_slot and target_ctx.process_slot.is_alive():
                    target_ctx.process_slot.kill()
                    print(f"Sent SIGTERM to process in context '{name}'")
                else:
                    print(f"Context '{name}' has no running process.")

            else:
                print(f"Unknown subcommand: {subcommand}")

        # Built-in pipeline decorators — commands named `@watch`, `@time`, …
        from .decorators import register_builtins as register_builtin_decorators
        register_builtin_decorators()

        # `var notify=off` / `var notify_threshold=30`.
        notify.register_vars()
        # `var shell_integration=off`.
        shell_integration.register_vars()

    def _reload_config(self) -> None:
        """``reload``: undo everything the config registered, then run it again."""
        self._clear_user_config()
        self._load_user_config()
        print("Config reloaded.")

    def _print_key_bindings(self) -> None:
        """``help keys``: every action by surface, its keys and what it does;
        actions from the config are marked ``*``."""
        shown = None
        rows = keymap.listing()
        for action, names in rows:
            if action.context != shown:
                if shown is not None:
                    print()
                print(f"{action.context}:")
                shown = action.context
            label = action.short_name + (" *" if action.is_user else "")
            print(f"  {label:24s} {', '.join(names) or '-':18s} {action.description}")
        if any(a.is_user for a, _ in rows):
            print("\n* from your config")

    def _clear_user_config(self) -> None:
        """Put every registry a config writes to back to its built-in state.

        The one sweep ``reload`` runs, so nothing a config registered
        survives once the config stops registering it — including a
        built-in it replaced with ``override=True``, which comes back.
        """
        from . import recipes
        self.registry.clear_user_commands()   # commands, decorators, aliases
        var_registry.clear_user_vars()
        recipes.skipped_recipes.clear()
        set_prompt(None)
        set_color_scheme(None)
        keymap.reset()
        notify.reset_config()
        shell_integration.reset_config()
        hooks.clear()

    def _edit_config(self) -> int:
        """``config edit``: run the user's editor on config.py, then reload."""
        path = config_dir() / "config.py"
        editor = (os.environ.get("VISUAL") or os.environ.get("EDITOR")
                  or ("notepad" if IS_WINDOWS else "vi"))
        argv = shlex.split(editor) + [str(path)]
        try:
            code = _run_interactive(argv)
        except FileNotFoundError:
            print(f"config edit: editor not found: {argv[0]}", file=sys.stderr)
            return 127
        if code != 0:
            print(f"config edit: {argv[0]} exited with status {code}; "
                  f"not reloading", file=sys.stderr)
            return code
        self._reload_config()
        return 0

    def _load_user_config(self) -> None:
        config_path = config_dir() / "config.py"
        if not config_path.exists():
            # First launch: write the starter config, then load it like any
            # other — a fresh install should not need a restart to get recipes.
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(_DEFAULT_CONFIG_PATH.read_text())

        import importlib
        import importlib.util

        # ~/.eosh is on sys.path while config.py runs, as a script's own
        # directory is: your own recipes and decorators live in modules there
        # (`import my_tools`), or anywhere config.py adds to sys.path.
        home = config_path.parent.resolve()
        if str(home) not in sys.path:
            sys.path.insert(0, str(home))
        # A module imported from there on an earlier load is forgotten, so
        # `reload` runs it again — `reload` just cleared what it registered.
        for name, mod in list(sys.modules.items()):
            origin = getattr(mod, "__file__", None)
            if origin and Path(origin).resolve().is_relative_to(home):
                del sys.modules[name]
        importlib.invalidate_caches()

        sys.modules.pop("eosh_user_config", None)
        spec = importlib.util.spec_from_file_location("eosh_user_config", config_path)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            sys.modules["eosh_user_config"] = module
            try:
                spec.loader.exec_module(module)
            except KeyboardInterrupt:
                # VSCode and similar terminal integrations may inject a Ctrl+C
                # right after opening a terminal (to clear any in-progress input
                # before auto-activating a venv). If that lands during config
                # load — typically inside a slow import like boto3 — exit
                # cleanly so the user can re-run eosh once the terminal has
                # finished its startup dance, instead of crashing with a
                # traceback or starting up half-configured.
                print("Config load interrupted by Ctrl+C; exiting.", file=sys.stderr)
                sys.exit(130)
            except Exception as e:
                from .user_errors import format_user_exception
                print(f"Error loading config ({config_path}):", file=sys.stderr)
                print(format_user_exception(e), file=sys.stderr, end="")
            # A keys.bind() may precede the action it names; check the
            # names now that the whole config has run.
            keymap.check_bindings()

    _ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)")

    def _split_env_prefix(self, tokens: list[str]) -> tuple[dict[str, str], list[str]]:
        """Split leading ``KEY=VALUE`` tokens into a per-command env prefix.

        Supports the POSIX idiom ``FOO=bar BAZ=qux cmd args`` — the assignments
        apply only to *cmd*'s environment, not the shell's.  Scanning stops at
        the first non-assignment token (the command name); everything after it
        is left untouched, so ``make FOO=bar`` keeps ``FOO=bar`` as an argument
        to ``make`` (a Makefile override) rather than an env prefix.

        Returns ``(env_prefix, rest)``.  When there is no command after the
        assignments (a pure-assignment line) ``rest`` is empty and the caller
        keeps the existing permanent-assignment behaviour.
        """
        env_prefix: dict[str, str] = {}
        idx = 0
        for token in tokens:
            m = self._ASSIGNMENT_RE.match(token)
            if m is None:
                break
            env_prefix[m.group(1)] = m.group(2)
            idx += 1
        rest = tokens[idx:]
        # No trailing command → not a prefix; let pure-assignment handling run.
        if not rest:
            return {}, tokens
        return env_prefix, rest

    @staticmethod
    def _merged_env(env_prefix: dict[str, str]) -> dict[str, str]:
        """Return a copy of ``os.environ`` overlaid with *env_prefix*."""
        env = dict(os.environ)
        env.update(env_prefix)
        return env

    @staticmethod
    def _env_prefix_refused(command_name: str, env_prefix: dict[str, str]) -> int:
        """A Python command can't take ``FOO=bar cmd``: it runs in the shell's
        own process, where the only environment is ``os.environ`` — shared by
        every thread, so a temporary change would leak into the other stages
        of a pipeline and outlive a backgrounded run.  Say so (status 2)."""
        names = " ".join(f"{k}=…" for k in env_prefix)
        print(f"eosh: {names} {command_name}: an environment prefix only applies to "
              f"external commands; set it with `var` for a Python command",
              file=sys.stderr)
        return 2

    def _env_keys_for(self, key: str) -> tuple[str, ...] | None:
        """The ``os.environ`` keys an assignment to *key* writes — an
        :class:`EnvVar`'s keys, *key* itself for a plain name — or ``None``
        for a PyVar / GlobalVar, whose value lives on the Python side."""
        var = var_registry.get(key)
        if var is None:
            return (key,)
        return var.keys if isinstance(var, EnvVar) else None

    def _set_variable(self, key: str, value: str) -> None:
        """``KEY=VALUE`` — every environment write goes through the context
        manager (so it is saved and restored per context); a PyVar or
        GlobalVar sets itself (and the context manager saves a PyVar's value
        when the context is left)."""
        keys = self._env_keys_for(key)
        if keys is None:
            var_registry.get(key).set(value)
            return
        for env_key in keys:
            self.context_manager.set_variable(env_key, value)

    def _unset_variable(self, key: str) -> None:
        """``KEY=`` — the mirror of :meth:`_set_variable`."""
        keys = self._env_keys_for(key)
        if keys is None:
            var_registry.get(key).unset()
            return
        for env_key in keys:
            self.context_manager.unset_variable(env_key)

    def _run_bash_script(self, body: str) -> tuple[int, str | None, dict[str, str]]:
        """Run *body* in a child bash and read back its final cwd + environment.

        Returns ``(exit_code, cwd, env)``.  ``cwd`` is None (and ``env`` empty)
        when the child never reached the dump — the caller reports that rather
        than importing an empty environment over the live one.

        The child runs through :func:`_run_interactive` so a script that prompts
        (``sudo``, an MFA code, ``read -p``) still owns the terminal, and the
        dump goes to a temp *file* rather than an extra fd so nothing has to be
        multiplexed alongside the script's own stdout.
        """
        bash = shutil.which("bash")
        if bash is None:
            print("source-bash: no 'bash' on PATH")
            return 127, None, {}

        fd, dump_path = tempfile.mkstemp(prefix="eosh-env-")
        os.close(fd)
        try:
            script = _BASH_ENV_DUMP_WRAPPER.format(
                dump=shlex.quote(dump_path), body=body
            )
            code = _run_interactive([bash, "-c", script])
            try:
                data = Path(dump_path).read_text(errors="replace")
            except OSError:
                data = ""
        finally:
            with contextlib.suppress(OSError):
                os.unlink(dump_path)

        cwd, env = _parse_bash_env_dump(data)
        return code, cwd, env

    @staticmethod
    def _apply_bash_env(
        ctx: CommandContext, cwd: str | None, env: dict[str, str], *,
        import_cwd: bool = True,
    ) -> tuple[list[str], list[str], str | None]:
        """Import a bash dump into *ctx*'s context: set, unset, and chdir.

        Everything goes through *ctx* — an imported variable is
        indistinguishable from one set with ``var NAME=VALUE``, and a
        ``source-bash`` sent to the background with Ctrl+] (still at an MFA
        prompt, say) lands in the context it was started in.

        Returns ``(set_names, unset_names, new_cwd)`` for the caller's summary;
        ``new_cwd`` is None when the directory did not change.
        """
        before = ctx.environ()
        changed: list[str] = []
        for key, value in env.items():
            if _bash_env_ignored(key):
                continue
            if before.get(key) != value:
                ctx.set_var(key, value)
                changed.append(key)

        removed: list[str] = []
        for key in before:
            if key in env or _bash_env_ignored(key) or not _ENV_NAME_RE.match(key):
                continue
            ctx.unset_var(key)
            removed.append(key)

        new_cwd = None
        if import_cwd and cwd and os.path.realpath(cwd) != os.path.realpath(ctx.cwd):
            try:
                new_cwd = ctx.chdir(cwd)
            except OSError as e:
                print(f"source-bash: cannot enter {cwd}: {e}")

        return sorted(changed), sorted(removed), new_cwd

    def _execute(self, line: str, history_id: int | None = None) -> int | None:
        """Run one line; its exit status, or ``None`` when Ctrl+] sent it to
        the background before it finished."""
        try:
            seq = parse_line(expand_vars(line))
        except DecoratorParseError as e:
            print(f"eosh: {e}", file=sys.stderr)
            return 2
        last_exit = 0
        started = time.monotonic()
        # Cleared per line; set by whichever path hands the work to a
        # background context, so the notification comes from the slot's own
        # exit callback instead of from this (premature) return.
        self._backgrounded = False
        self._current_line = line
        self._current_history_id = history_id
        hooks.fire("on_command_starting", line)
        try:
            if self._runs_on_one_slot(seq):
                last_exit = self._run_on_slot(seq, line)
                self._notice_state_change()
                return None if self._backgrounded else last_exit
            for op, pipeline in seq.items:
                if self._backgrounded:
                    # Ctrl+] parked an earlier part.  Its status isn't known
                    # yet, and the rest would run now, in the context just
                    # switched to — so it doesn't run at all.
                    print("eosh: the rest of the line was not run: "
                          "its first part went to the background", file=sys.stderr)
                    break
                if op == "&&" and last_exit != 0:
                    continue
                if op == "||" and last_exit == 0:
                    continue
                if self._can_park() and not self._on_main_thread(pipeline):
                    # A line with a shell-wide built-in runs pipeline by
                    # pipeline, each of the others on a slot of its own.
                    last_exit = self._run_on_slot(Sequence(items=[(None, pipeline)]),
                                                  _stage_label_of(pipeline))
                else:
                    last_exit = self._execute_pipeline(pipeline)
                # `cd proj && make`: the move is reported before make runs.
                self._notice_state_change()
        finally:
            self._current_line = None
            self._current_history_id = None
            if not self._backgrounded:
                elapsed = time.monotonic() - started
                self._history.finish(history_id, last_exit, elapsed)
                notify.command_done(line, elapsed, last_exit)
                hooks.fire("on_command_finished", line, last_exit, elapsed)
            # User-run commands may have mutated remote state (e.g.
            # ``awsut sagemaker hyperpod scale``); drop cached completer
            # fetches so the next TAB session re-queries.
            from . import completion_cache
            completion_cache.invalidate_all()
        return None if self._backgrounded else last_exit

    # --- long-command notifications ------------------------------------------

    def _park(self, slot, ctx) -> None:
        """Hand *slot* to *ctx* to keep running in the background (Ctrl+]).  From here on the slot's exit handler reports it, so the
        line that started it doesn't (see :meth:`_execute`)."""
        ctx.process_slot = slot
        slot.parked = True
        slot.line = self._current_line
        slot.history_id = self._current_history_id
        self._backgrounded = True

    def _slot_finished(self, slot) -> None:
        """Exit handler every slot is constructed with; runs on the slot's
        own thread, so it must not touch the terminal —
        :func:`notify.command_done` only spawns a notification helper.

        A slot that was never parked ran in the foreground, and
        :meth:`_execute` timed the whole line.  A parked one is reported
        here, with its context's name when that context isn't the current
        one — the user was looking elsewhere when it ended.  The owner is
        looked up, not remembered: a slot can move between contexts.
        """
        if not slot.parked:
            return
        owner = next(
            (name for name, ctx in self.context_manager.contexts.items()
             if ctx.process_slot is slot),
            None,
        )
        out_of_sight = owner is not None and owner != self.context_manager.current_name
        notify.command_done(" ".join(slot.argv), slot.elapsed(), slot.exit_code or 0,
                            context=owner if out_of_sight else None)
        self._history.finish(slot.history_id, slot.exit_code or 0, slot.elapsed())
        hooks.fire("on_command_finished", slot.line or " ".join(slot.argv),
                   slot.exit_code or 0, slot.elapsed())

    # --- what user code sees ---------------------------------------------------

    def _shell_view(self) -> ShellView:
        """The current context, read-only — ``CompletionContext.shell_context``."""
        return ShellView(self.context_manager, self.context_manager.current())

    def _command_context(self) -> CommandContext:
        """For a ``pass_context`` handler: bound to the context the command
        starts in, which it keeps if it is sent to the background."""
        return CommandContext(self.context_manager, self.context_manager.current(), self)

    # --- hooks ----------------------------------------------------------------

    def _observed_state(self) -> tuple[str | None, str | None]:
        try:
            cwd = os.getcwd()
        except OSError:          # the directory was removed under us
            cwd = None
        return self.context_manager.current_name, cwd

    def _notice_state_change(self) -> None:
        """Fire on_context_switched / on_directory_changed for whatever
        changed since the last look.

        Comparing state rather than hooking each mutation catches every way
        it can change — ``cd``, a context switch restoring its cwd,
        ``source-bash``, ``os.chdir`` in a Python command — from a handful of
        call sites: after each command of a line, after a context switch,
        and before each prompt.
        """
        old_name, old_cwd = self._seen_state
        name, cwd = self._seen_state = self._observed_state()
        if name != old_name:
            hooks.fire("on_context_switched", old_name, name)
        if cwd != old_cwd and cwd is not None:
            hooks.fire("on_directory_changed", old_cwd, cwd)

    def _tokenize_stage(self, stage: Stage, root_dir: str | None = None) -> list[str]:
        """Expand variables, tokenize, alias-expand, and glob-expand a stage's
        text — globs relative to *root_dir* (default: the process's cwd)."""
        tokens = tokenize(stage.text + " ")
        tokens = [os.path.expanduser(t) for t in tokens]
        tokens = self._expand_alias(tokens)
        return expand_globs(tokens, root_dir=root_dir)

    def _expand_alias(self, tokens: list[str]) -> list[str]:
        """Replace the first token with its alias expansion, if any.

        Aliases never chain — the expansion's own first token is not
        re-expanded — so cycles are impossible.
        """
        if not tokens:
            return tokens
        expansion = self.registry.get_alias(tokens[0])
        if expansion is None:
            return tokens
        expansion_tokens = tokenize(expansion)
        if not expansion_tokens:
            return tokens
        return expansion_tokens + tokens[1:]

    def _run_pipeline_from_decorator(
        self, pipeline: Pipeline, *, stdin=None, stdout=None, stderr=None
    ) -> int:
        """Entry point used by ``Pipeline.run()`` from a decorator body.

        The MVP rejects explicit ``stdin``/``stdout``/``stderr`` overrides;
        the body inherits stdio from the decorator's caller via the
        thread-local routers.

        When the decorator itself is a stage of an outer pipeline
        (composition: ``@deco {body} | next``), the body's stdout must
        feed into the outer pipe — not the real terminal.  We detect that
        case via ``_in_pipeline.flag`` and pipe the body's last stage to
        the thread's rebound ``sys.stdout`` (which is the outer pipe's
        write end).  Without this, a body like ``@watch {ls}`` running
        under ``@watch {ls} | grep py`` would route through the
        standalone-command path (its own ``PipelineSlot``)
        and grab the real terminal — wrong from a worker thread.
        """
        if any(x is not None for x in (stdin, stdout, stderr)):
            raise NotImplementedError(
                "Pipeline.run(stdin=, stdout=, stderr=) is not supported yet; "
                "the decorator body inherits stdio from the decorator's caller."
            )
        in_outer_pipe = getattr(_in_pipeline, "flag", False)
        status = self._execute_pipeline(pipeline, _in_outer_pipe=in_outer_pipe)
        if status == 130:
            # The body was interrupted (Ctrl+C): so is the decorator — @watch
            # stops, @retry doesn't retry, @time still prints on the way out.
            raise KeyboardInterrupt
        return status

    def _execute_pipeline(
        self,
        pipeline: Pipeline,
        *,
        _in_outer_pipe: bool = False,
    ) -> int:
        """Execute a pipeline; return exit code of last stage.

        On a slot — a line's driver thread, or a decorator body on one of
        its stage threads (:data:`_job_local`) — the pipeline joins it:
        external stages go through its job leader, terminal-facing ends to
        its PTY, and the cwd, environment and variables are those of the
        context the line started in (``job.ctx``), whichever is current now.

        ``_in_outer_pipe`` is set when this call is the body of a
        decorator that is itself a stage of an outer pipeline
        (``@deco {body} | next``).  In that case the body's last stage
        must write into the thread's rebound ``sys.stdout`` (the outer
        pipe's write end) rather than the real terminal — so we force
        every stage onto the worker-thread / Popen path even when the
        body has only one stage (where ``_execute_stage`` would
        otherwise grab a real PTY).
        """
        stages = pipeline.stages
        single = stages[0] if len(stages) == 1 else None
        # A lone stage gets the terminal (its own PipelineSlot) unless it
        # has redirects.  A redirected stage is a one-stage pipeline: the
        # loop below already binds a Python stage's stdio through the
        # thread-local routers and an external one through Popen, so there
        # is no second redirect path that swaps the process-global
        # ``sys.stdout`` under every other thread.  Decorator stages keep
        # the direct path — their redirects live inside the braced body.
        # On a slot nothing takes the terminal for itself: the slot has it.
        job: PipelineSlot | None = getattr(_job_local, "job", None)
        if (
            single is not None
            and job is None
            and not _in_outer_pipe
            and (not single.redirects or single.decorator is not None)
        ):
            return self._execute_stage(single)

        # Multi-stage pipeline, redirected single stage, or single-stage
        # decorator body running under an outer pipe: connect with OS
        # pipes.  External stages run via subprocess.Popen; registered
        # Python commands run in worker threads that rebind
        # sys.stdin/stdout/stderr to the pipe ends (or redirect files) via
        # the thread-local routers installed in __init__.

        # Whose cwd and environment the stages get: the line's context on a
        # slot (it may not be the current one by now), else the process's.
        line_ctx = job.ctx if job is not None else None
        cwd = line_ctx.cwd if line_ctx is not None else os.getcwd()

        def stage_env(prefix: dict[str, str]) -> dict[str, str]:
            if line_ctx is None:
                return self._merged_env(prefix)
            env = line_ctx.environ()
            env.update(prefix)
            return env

        n = len(stages)
        # The status when nothing could be started (a lone command not found).
        unstarted_status = 0
        pipe_fds: list[tuple[int, int]] = []
        for _ in range(n - 1):
            pipe_fds.append(os.pipe())

        # When running under an outer pipe, dup the boundary fds from the
        # thread-local stdio overrides so the body's first stage reads
        # from whatever feeds the decorator and the last stage writes to
        # whatever follows it.  We dup so that the worker (Popen or
        # Python-stage thread) takes ownership of its own fd copy and the
        # parent thread's wrappers stay open for subsequent iterations
        # (e.g. ``@watch {ls} | grep py`` re-runs the body each tick).
        outer_in_fd = _dup_threadlocal_override_fd(sys.stdin) if _in_outer_pipe else None
        outer_out_fd = _dup_threadlocal_override_fd(sys.stdout) if _in_outer_pipe else None

        # Workers list contains either subprocess.Popen instances or
        # _PyStageHandle objects (see _start_stage_thread).
        workers: list = []
        for idx, stage in enumerate(stages):
            stdin_fd_pipe = pipe_fds[idx - 1][0] if idx > 0 else outer_in_fd
            stdout_fd_pipe = pipe_fds[idx][1] if idx < n - 1 else outer_out_fd

            # Decorator stage (e.g. ``@watch {ls} | grep foo``).  The
            # decorator body inherits the thread's rebound stdio via the
            # thread-local routers, so its writes flow through the pipe
            # to the next stage just like a Python command would.
            if stage.decorator is not None:
                # Decorator stages don't currently honour explicit redirects
                # on the stage itself — redirects belong inside the braced
                # body where the user can scope them precisely.  An MVP-level
                # warning would be noise; just ignore them silently.
                call = stage.decorator
                worker = self._start_stage_thread(
                    label=f"@{call.name}",
                    fn=lambda call=call: self._invoke_decorator(call),
                    stdin_fd=stdin_fd_pipe,
                    stdout_fd=stdout_fd_pipe,
                    job=job,
                    decorator=True,
                )
                workers.append(worker)
                continue

            tokens = self._tokenize_stage(stage, root_dir=cwd)
            if not tokens:
                continue

            # ``FOO=bar > x`` at top level is still an assignment, as it is
            # without the redirect.  (Inside a real pipeline it is not —
            # POSIX would run it in a subshell, so it is left to fail as a
            # command, as before.)
            if n == 1 and not _in_outer_pipe and all(
                self._ASSIGNMENT_RE.match(t) for t in tokens
            ):
                for token in tokens:
                    m = self._ASSIGNMENT_RE.match(token)
                    if line_ctx is not None:
                        line_ctx.set_var(m.group(1), m.group(2))
                    else:
                        self._set_variable(m.group(1), m.group(2))
                continue

            # Per-command env prefix (``FOO=bar cmd``) applies to this stage only.
            env_prefix, tokens = self._split_env_prefix(tokens)
            if not tokens:
                continue

            cmd = self.registry.get(tokens[0])
            if cmd is not None and not cmd.has_any_handler():
                cmd = None

            # Resolve explicit redirects (override the pipe ends).
            stdin_file = stdout_file = None
            stderr_dst: object | None = None
            redirect_error = False
            for redir in stage.redirects:
                # Relative to the line's cwd, which may not be the process's.
                target = os.path.join(cwd, os.path.expanduser(redir.target))
                try:
                    if redir.kind == "<":
                        stdin_file = open(target, "rb")
                    elif redir.kind == ">":
                        stdout_file = open(target, "wb")
                    elif redir.kind == ">>":
                        stdout_file = open(target, "ab")
                    elif redir.kind == "2>":
                        stderr_dst = open(target, "wb")
                    elif redir.kind == "2>>":
                        stderr_dst = open(target, "ab")
                    elif redir.kind == "2>&1":
                        stderr_dst = subprocess.STDOUT
                except OSError as e:
                    print(
                        f"eosh: {redir.target}: {e.strerror or e}",
                        file=sys.stderr,
                    )
                    redirect_error = True
                    break

            stdin_pipe_used = stdin_fd_pipe is not None and stdin_file is None
            stdout_pipe_used = stdout_fd_pipe is not None and stdout_file is None
            is_py_stage = cmd is not None

            worker = None
            if redirect_error:
                pass  # leave worker=None; cleanup below closes any open files/pipes
            elif is_py_stage:
                if env_prefix:
                    # Refused (see _env_prefix_refused) — as the stage's own
                    # work, so its pipe ends close and its status is 2.
                    fn = lambda name=cmd.name, env=env_prefix: self._env_prefix_refused(name, env)
                else:
                    fn = (lambda cmd=cmd, args=tokens[1:],
                          ctx=line_ctx or self._command_context():
                          cmd.invoke(args, ctx=ctx))
                worker = self._start_stage_thread(
                    label=cmd.name,
                    fn=fn,
                    stdin_fd=stdin_fd_pipe if stdin_pipe_used else None,
                    stdout_fd=stdout_fd_pipe if stdout_pipe_used else None,
                    stdin_file=stdin_file,
                    stdout_file=stdout_file,
                    stderr_dst=stderr_dst,
                    job=job,
                )
            else:
                stdin_arg = stdin_file if stdin_file else stdin_fd_pipe
                stdout_arg = stdout_file if stdout_file else stdout_fd_pipe
                try:
                    if job is not None:
                        # On the slot's PTY, through its job leader: None is
                        # the PTY, and 2>&1 follows wherever stdout goes.
                        worker = job.spawn(
                            tokens,
                            stdin=stdin_arg,
                            stdout=stdout_arg,
                            stderr=stdout_arg if stderr_dst is subprocess.STDOUT else stderr_dst,
                            env=stage_env(env_prefix),
                            cwd=cwd,
                        )
                    else:
                        worker = subprocess.Popen(
                            tokens,
                            stdin=stdin_arg,
                            stdout=stdout_arg,
                            stderr=stderr_dst,
                            env=self._merged_env(env_prefix),
                            cwd=os.getcwd(),
                        )
                except FileNotFoundError:
                    if n == 1 and not _in_outer_pipe:
                        unstarted_status = self._command_not_found(tokens)
                    else:
                        print(f"eosh: command not found: {tokens[0]}")
                except OSError as e:
                    print(f"eosh: {e}")
                    unstarted_status = 1

            if worker is not None:
                workers.append(worker)

            # Drop the parent's reference to each pipe end the stage used.
            # For Popen the OS-level dup has already happened, so closing here
            # is correct.  For a Python-thread stage the worker thread owns
            # the fd via the TextIOWrapper passed to it and will close it
            # itself — so close here only if the stage *didn't* use that end
            # (because of an explicit redirect, or because the stage failed
            # to start at all).
            #
            # Outer-pipe boundary fds (idx=0 stdin, idx=n-1 stdout when
            # ``_in_outer_pipe`` is set) were duped from the thread-local
            # overrides; they need to be closed by the parent under exactly
            # the same rules — Popen handles its own dup, Python-thread
            # owns the fd, otherwise close here.
            stdin_fd_is_owned = idx > 0 or (
                outer_in_fd is not None and stdin_fd_pipe == outer_in_fd
            )
            stdout_fd_is_owned = idx < n - 1 or (
                outer_out_fd is not None and stdout_fd_pipe == outer_out_fd
            )
            close_stdin_pipe = (
                stdin_fd_is_owned
                and stdin_fd_pipe is not None
                and (worker is None or not is_py_stage or not stdin_pipe_used)
            )
            close_stdout_pipe = (
                stdout_fd_is_owned
                and stdout_fd_pipe is not None
                and (worker is None or not is_py_stage or not stdout_pipe_used)
            )
            if close_stdin_pipe:
                os.close(stdin_fd_pipe)
            if close_stdout_pipe:
                os.close(stdout_fd_pipe)

            # Close redirect file objects we opened.  Popen has already dup'd
            # them; the Python-thread stage took ownership of them, so in
            # both cases the parent's copy is no longer needed — except when
            # the stage failed to start, in which case we must close them
            # ourselves to release the fd.
            if not is_py_stage or worker is None:
                if stdin_file:
                    stdin_file.close()
                if stdout_file:
                    stdout_file.close()
                if (
                    stderr_dst is not None
                    and stderr_dst is not subprocess.STDOUT
                    and hasattr(stderr_dst, "close")
                ):
                    try:
                        stderr_dst.close()
                    except Exception:
                        pass

        if not workers:
            return unstarted_status
        if job is not None and getattr(_job_local, "driver", False):
            return job.wait_for(workers)    # the ones Ctrl+C interrupts
        return self._wait_for_stages(workers)

    # --- one slot per line ---------------------------------------------------

    @staticmethod
    def _can_park() -> bool:
        """Whether lines run on slots: a POSIX terminal."""
        return not IS_WINDOWS and _stdin_is_tty()

    def _stage_command(self, stage: Stage):
        """A lone stage's registered command, ``"assign"`` for a line of
        assignments, or None (an external command, a decorator)."""
        if stage.decorator is not None:
            return None
        tokens = self._tokenize_stage(stage)
        if not tokens or all(self._ASSIGNMENT_RE.match(t) for t in tokens):
            return "assign"
        _, tokens = self._split_env_prefix(tokens)
        cmd = self.registry.get(tokens[0]) if tokens else None
        return cmd if cmd is not None and cmd.has_any_handler() else None

    def _on_main_thread(self, pipeline: Pipeline) -> bool:
        """A pipeline that runs on the main thread, not on a slot: a lone
        `sync` command (it changes the whole shell), or a lone line of
        assignments."""
        if len(pipeline.stages) != 1:
            return False
        cmd = self._stage_command(pipeline.stages[0])
        return cmd == "assign" or (cmd is not None and cmd.sync)

    def _runs_on_one_slot(self, seq: Sequence) -> bool:
        """Whether the whole line runs on one slot (discussion #76): on a
        terminal, unless it has a lone shell-wide built-in — that runs on the
        main thread, so the line goes pipeline by pipeline — or is nothing
        but main-thread work."""
        if not self._can_park():
            return False
        lone = [self._stage_command(p.stages[0]) if len(p.stages) == 1 else None
                for _, p in seq.items]
        if any(cmd not in (None, "assign") and cmd.sync for cmd in lone):
            return False
        return not all(cmd == "assign" for cmd in lone)

    def _run_on_slot(self, seq: Sequence, label: str) -> int:
        """Run *seq* on one :class:`~eosh.job.PipelineSlot`: its pipelines,
        `&&` / `||` / `;` included, on a driver thread, while this thread
        gives the slot the terminal.  Ctrl+] parks the rest of the line with
        it; Ctrl+C (a pipeline ending in 130) ends the line, as in bash."""
        slot = PipelineSlot(label, on_exit=self._slot_finished)
        # Bound to the context the line started in, whatever is current when
        # its later parts run: their cwd, environment, variables.
        slot.ctx = self._command_context()
        lone_command = (len(seq.items) == 1 and len(seq.items[0][1].stages) == 1
                        and not seq.items[0][1].stages[0].redirects
                        and seq.items[0][1].stages[0].decorator is None)

        def drive() -> int:
            last = 0
            for op, pipeline in seq.items:
                if op == "&&" and last != 0:
                    continue
                if op == "||" and last == 0:
                    continue
                last = self._execute_pipeline(pipeline)
                if last == 130:
                    break                   # Ctrl+C ends the line
                if not slot.parked:
                    # `cd proj && make`: the move is reported before make runs.
                    self._notice_state_change()
            return last

        slot.run(drive)
        ctx = self.context_manager.current()
        slot.activate(replay_missed=True)
        result = self._forward(slot)
        if result == "switched":
            self._park(slot, ctx or self.context_manager.current())
            slot.deactivate()
            self._handle_switch()
            return 0
        slot.deactivate()
        if ctx is not None and ctx.process_slot is slot:
            ctx.process_slot = None
        exit_code = slot.exit_code or 0
        if lone_command and exit_code not in (0, 127, 130):
            print(f"\n[Process exited with code {exit_code}]")
        return exit_code

    @staticmethod
    def _wait_for_stages(workers: list) -> int:
        """Wait for a pipeline's stages on this thread; the last one's status.
        Ctrl+C (KeyboardInterrupt) stops them all: 130."""
        exit_code = 0
        try:
            for w in workers:
                if isinstance(w, _PyStageHandle):
                    w.wait()
                    exit_code = w.exit_code or 0
                else:
                    w.wait()
                    exit_code = w.returncode or 0
        except KeyboardInterrupt:
            for w in workers:
                if isinstance(w, _PyStageHandle):
                    w.interrupt()
                else:
                    try:
                        w.terminate()
                    except Exception:
                        pass
            for w in workers:
                if isinstance(w, _PyStageHandle):
                    w.wait()
                else:
                    try:
                        w.wait()
                    except Exception:
                        pass
            exit_code = 130
        return exit_code

    def _start_stage_thread(
        self,
        *,
        label: str,
        fn: Callable[[], object],
        stdin_fd: int | None,
        stdout_fd: int | None,
        stdin_file=None,
        stdout_file=None,
        stderr_dst=None,
        job: PipelineSlot | None = None,
        decorator: bool = False,
    ) -> "_PyStageHandle":
        """Run *fn* — a Python command or a decorator — as one pipeline stage.

        The thread takes ownership of *stdin_fd* / *stdout_fd* (raw OS pipe
        ends) or, when an explicit redirect is in play, the corresponding
        opened file object, and binds them to its thread-local
        ``sys.stdin`` / ``sys.stdout`` / ``sys.stderr`` for the duration of
        *fn*.  A decorator body that re-enters ``_execute_pipeline`` through
        ``Pipeline.run()`` inherits that binding, so its output flows on to
        the next stage.  The exit status comes from :func:`run_handler`.
        """
        # Exactly one of (stdin_fd, stdin_file) is set when this stage has
        # any stdin source, and similarly for stdout.  Neither means the
        # terminal: the real one (no override), or a slot's PTY.
        if job is not None:
            if stdin_fd is None and stdin_file is None:
                stdin_fd = job.terminal_fd()
            if stdout_fd is None and stdout_file is None:
                stdout_fd = job.terminal_fd()
            if stderr_dst is None:
                stderr_dst = os.fdopen(job.terminal_fd(), "wb", buffering=0)
        in_obj = None
        if stdin_file is not None:
            in_obj = stdin_file
        elif stdin_fd is not None:
            in_obj = os.fdopen(stdin_fd, "rb", buffering=0, closefd=True)

        out_obj = None
        if stdout_file is not None:
            out_obj = stdout_file
        elif stdout_fd is not None:
            out_obj = os.fdopen(stdout_fd, "wb", buffering=0, closefd=True)

        err_obj = stderr_dst  # a file, subprocess.STDOUT (2>&1), or None

        # Both ends a terminal (a slot's PTY): the stage may talk to the user.
        on_terminal = _isatty(in_obj) and _isatty(out_obj)
        handle = _PyStageHandle(cmd_name=label, graceful=decorator or on_terminal)
        in_wrapper = io.TextIOWrapper(in_obj, encoding="utf-8", errors="replace") if in_obj is not None else None
        out_wrapper = io.TextIOWrapper(out_obj, encoding="utf-8", errors="replace", write_through=True) if out_obj is not None else None
        err_wrapper = None
        if err_obj is not None and err_obj is not subprocess.STDOUT:
            err_wrapper = io.TextIOWrapper(err_obj, encoding="utf-8", errors="replace", write_through=True)
        # Hand wrappers to the handle so interrupt() can close them.
        for w in (in_wrapper, out_wrapper, err_wrapper):
            if w is not None:
                handle._io_objs.append(w)

        def _target():
            _in_pipeline.flag = True
            _in_pipeline.on_terminal = on_terminal
            _job_local.job = job        # a decorator body joins the slot
            try:
                if in_wrapper is not None:
                    sys.stdin.set_override(in_wrapper)
                if out_wrapper is not None:
                    sys.stdout.set_override(out_wrapper)
                if err_obj is subprocess.STDOUT:
                    sys.stderr.set_override(out_wrapper if out_wrapper is not None else sys.stdout)
                elif err_wrapper is not None:
                    sys.stderr.set_override(err_wrapper)
                # After interrupt() closed our wrappers, an I/O error is the
                # expected way out, not one to report.
                handle.exit_code = run_handler(
                    fn, label, interrupted=lambda: handle.interrupted)
            finally:
                sys.stdin.clear_override()
                sys.stdout.clear_override()
                sys.stderr.clear_override()
                # Flush, then close (which closes the underlying fds/files) —
                # output first, so a reader pipe sees all of it and then EOF.
                for w in (out_wrapper, err_wrapper):
                    if w is not None:
                        try:
                            w.flush()
                        except Exception:
                            pass
                for w in (out_wrapper, err_wrapper, in_wrapper):
                    if w is not None:
                        try:
                            w.close()
                        except Exception:
                            pass
                _in_pipeline.flag = False
                _in_pipeline.on_terminal = False
                _job_local.job = None
                handle.done.set()

        t = threading.Thread(target=_target, name=f"pipe-{label}", daemon=True)
        handle.thread = t
        t.start()
        return handle

    def _invoke_decorator(self, decorator_call):
        """Run a ``@name`` decorator over its body; returns its result.

        A decorator is the command ``@name``: its flags are parsed like any
        command's (a flag error is status 2, already printed) and its handler
        gets the body ``Pipeline`` first.  An unknown decorator is 127, as an
        unknown command would be.
        """
        deco = self.registry.get(f"@{decorator_call.name}")
        if deco is None:
            known = [n for n in self.registry.list_commands() if n.startswith("@")]
            close = difflib.get_close_matches(f"@{decorator_call.name}", known, n=1)
            hint = f" (did you mean {close[0]}?)" if close else ""
            print(f"eosh: unknown decorator: @{decorator_call.name}{hint}", file=sys.stderr)
            return 127
        return deco.invoke(decorator_call.flag_tokens, decorator_call.body,
                           ctx=self._command_context())

    def _execute_decorator_stage(self, stage: Stage) -> int:
        """Run a lone ``@name`` stage on the main thread."""
        call = stage.decorator
        return run_handler(lambda: self._invoke_decorator(call), f"@{call.name}",
                           announce_interrupt=True)

    def _execute_stage(self, stage: Stage) -> int:
        """Execute a lone stage with no redirects on the terminal.

        On a POSIX terminal only a ``sync`` command and a line of
        assignments reach here — :meth:`_execute_pipeline` runs everything
        else on a ``PipelineSlot``.  Without one (Windows, no tty) a Python
        command runs on the main thread and an external one inherits the
        terminal.  Returns the exit code.
        """
        if stage.decorator is not None:
            return self._execute_decorator_stage(stage)

        tokens = self._tokenize_stage(stage)
        if not tokens:
            return 0

        # Pure-assignment line
        if all(self._ASSIGNMENT_RE.match(t) for t in tokens):
            for token in tokens:
                m = self._ASSIGNMENT_RE.match(token)
                self._set_variable(m.group(1), m.group(2))
            return 0

        # Per-command env prefix (``FOO=bar cmd args``): the assignments apply
        # only to this command, not the shell.
        env_prefix, tokens = self._split_env_prefix(tokens)

        command_name = tokens[0]
        args = tokens[1:]

        cmd = self.registry.get(command_name)
        # External recipes are stored as Command nodes too (for unified
        # completion), but have no Python handler anywhere — fall through to
        # the system command path so the real binary runs.
        if cmd is not None and not cmd.has_any_handler():
            cmd = None
        if cmd and env_prefix:
            return self._env_prefix_refused(command_name, env_prefix)
        if cmd:
            # On the main thread: a `sync` command (the built-ins that change
            # the whole shell), and every command on Windows or when stdin
            # isn't a terminal — on a POSIX terminal any other command runs
            # on a PipelineSlot (see _execute_pipeline).  ctx.run_interactive
            # falls back to subprocess.run and ctx.input reads the terminal
            # directly.
            return run_handler(lambda: cmd.invoke(args, ctx=self._command_context()),
                               command_name,
                               announce_interrupt=True)

        return self._execute_external(command_name, args, env_prefix=env_prefix)

    def _execute_external_windows(
        self, command_name: str, args: list[str], env_prefix: dict[str, str] | None = None
    ) -> int:
        """Run an external command on the real console (Windows path).

        Without ConPTY-based multiplexing, the child simply inherits the
        terminal's stdio. Commands that are cmd.exe builtins (dir, echo, cls,
        …) rather than real executables are retried via ``cmd /c``.
        """
        argv = [command_name] + args
        env = self._merged_env(env_prefix or {})
        cwd = os.getcwd()
        try:
            return subprocess.run(argv, env=env, cwd=cwd).returncode
        except FileNotFoundError:
            try:
                return subprocess.run(["cmd", "/c", *argv], env=env, cwd=cwd).returncode
            except FileNotFoundError:
                return self._command_not_found(argv)
            except OSError as e:
                print(f"eosh: {e}")
                return 1
        except OSError as e:
            print(f"eosh: {e}")
            return 1

    def _command_not_found(self, argv: list[str]) -> int:
        """Offer *argv* to the on_command_not_found hooks; report it if none
        claims it."""
        if hooks.fire_until_claimed("on_command_not_found", argv):
            return 0
        print(f"eosh: command not found: {argv[0]}")
        return 127

    def _execute_external(
        self, command_name: str, args: list[str], env_prefix: dict[str, str] | None = None
    ) -> int:
        """Run a lone external command on the inherited terminal — Windows,
        or no terminal on stdin.  On a POSIX terminal it runs on a
        :class:`~eosh.job.PipelineSlot` instead (see :meth:`_execute_pipeline`)."""
        if IS_WINDOWS:
            return self._execute_external_windows(command_name, args, env_prefix)
        argv = [command_name] + args
        try:
            return subprocess.run(argv, env=self._merged_env(env_prefix or {}),
                                  cwd=os.getcwd()).returncode
        except FileNotFoundError:
            return self._command_not_found(argv)
        except OSError as e:
            print(f"eosh: {e}")
            return 1

    def _forward(self, slot, force_redraw: bool = False) -> str:
        """Forward the terminal to a running *slot* until it ends or is left.

        Every slot is a PTY (a :class:`~eosh.job.PipelineSlot`), so this is
        a plain relay: stdin in raw mode, SIGINT ignored (Ctrl+C reaches the
        slot as a byte, and its line discipline makes it a signal), window
        resizes passed on through ``slot.resize``, every key forwarded with
        ``slot.write_stdin``.  Two keys are special:

          • the switch key (prompt.switch_context, Ctrl+] by default) —
            anything typed before it is still forwarded; returns 'switched'
            so the caller can park the slot
          • Ctrl+C, while the PTY treats it as an interrupt — the Python
            stages are threads of ours, not processes, so they are told
            separately (``interrupt_python_stages``)

        *force_redraw* sends the current size to a resumed slot at once.
        Returns 'exited' when the slot finishes, handing the keys nothing
        read back to the line editor.
        """
        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        old_sigint = signal.getsignal(signal.SIGINT)
        old_sigwinch = signal.getsignal(signal.SIGWINCH)
        result = "exited"

        def on_resize(signum, frame):
            try:
                size = os.get_terminal_size(fd)
                slot.resize(size.lines, size.columns)
            except OSError:
                pass

        try:
            terminal.set_raw(fd)            # TCSADRAIN: keep keys typed ahead
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGWINCH, on_resize)
            if force_redraw:
                on_resize(None, None)

            while slot.is_alive():
                rlist, _, _ = select.select([fd], [], [], 0.1)
                if fd not in rlist:
                    continue
                data = os.read(fd, 1024)
                if not data:
                    break
                idx = _find_switch_key(data)
                if idx >= 0:
                    if idx > 0:
                        slot.write_stdin(data[:idx])
                    result = "switched"
                    break
                slot.write_stdin(data)
                if b"\x03" in data and slot.ctrl_c_interrupts():
                    slot.interrupt_python_stages()
            if result == "exited":
                # Typed ahead of the next prompt (a pasted `cd x` + `ls`):
                # nothing read it, so the line editor gets it.
                terminal.unread(slot.take_unread())
            return result
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
            signal.signal(signal.SIGINT, old_sigint)
            signal.signal(signal.SIGWINCH, old_sigwinch)
            if result == "switched":
                suspend_seq = slot.suspend_terminal_modes()
                if suspend_seq:
                    sys.stdout.write(suspend_seq)
                    sys.stdout.flush()

    def run(self) -> None:
        self._install_sigwinch_handler()
        print("Eolith Shell — type 'help' for available commands, 'exit' to quit.")
        hooks.fire("on_startup")
        shell_integration.startup()
        try:
            self._run_loop()
        finally:
            shell_integration.command_finished(None)
            hooks.fire("on_exit")

    def _run_loop(self) -> None:
        while True:
            try:
                # A line that ran nothing (empty, Ctrl+C, a Ctrl+] switch)
                # or was interrupted is closed here.
                shell_integration.command_finished(None)
                ctx = self.context_manager.current()

                if ctx and ctx.process_slot and ctx.process_slot.is_alive():
                    # Resume the slot parked here.
                    slot = ctx.process_slot
                    self._resume_pty_slot(slot)
                    result = self._forward(slot, force_redraw=True)
                    slot.deactivate()
                    if result == "switched":
                        self._handle_switch()
                        continue
                    exit_code = slot.exit_code
                    ctx.process_slot = None
                    if exit_code and exit_code != 0:
                        print(f"\n[Process exited with code {exit_code}]")
                    continue

                if ctx and ctx.process_slot and not ctx.process_slot.is_alive():
                    slot = ctx.process_slot
                    slot.replay_buffer()
                    exit_code = slot.exit_code
                    ctx.process_slot = None
                    if exit_code and exit_code != 0:
                        print(f"\n[Process exited with code {exit_code}]")

                # Anything a resumed or finished slot changed.
                self._notice_state_change()

                # Collect the primary line (history managed here, not inside the editor).
                text = self._line_editor.prompt()
                if text == CONTEXT_CHANGED_SENTINEL:
                    continue

                # Handle backslash line continuation: keep prompting with "> "
                # until a line that does NOT end with an unescaped backslash.
                # Ctrl+C propagates as KeyboardInterrupt and abandons the command.
                # A context-switch (Ctrl+]) during continuation also abandons it.
                full_text = text
                while _is_continuation(full_text):
                    partial = _strip_continuation(full_text)
                    cont = self._line_editor.prompt(prompt_str="> ")
                    if cont == CONTEXT_CHANGED_SENTINEL:
                        full_text = ""
                        break
                    full_text = partial + cont

                if full_text.strip():
                    history_id = self._record_history(full_text.strip())
                    shell_integration.command_started(full_text.strip())
                    status = self._execute(full_text.strip(), history_id=history_id)
                    shell_integration.command_finished(status)
                    if self._exit_requested:
                        break
            except KeyboardInterrupt:
                continue
            except EOFError:
                print("\nexit")
                running = self._running_contexts()
                if running and not self._confirm_exit(running):
                    continue
                break
            except SystemExit:
                break

    def _install_sigwinch_handler(self) -> None:
        # No SIGWINCH on Windows; live-process resize forwarding is part of the
        # PTY multiplexing path, which is POSIX-only.
        if not terminal.HAS_SIGWINCH:
            return

        def on_resize(signum, frame):
            ctx = self.context_manager.current()
            if ctx and ctx.process_slot and ctx.process_slot.is_alive():
                try:
                    rows, cols = os.get_terminal_size()
                    ctx.process_slot.resize(rows, cols)
                except OSError:
                    pass

        signal.signal(signal.SIGWINCH, on_resize)
