"""What user code sees of the shell: one object instead of internals.

* :class:`ShellView` — read-only: the context's name, its cwd, its
  variables.  Completers get one as ``CompletionContext.shell_context``.
* :class:`CommandContext` — a :class:`ShellView` that can also talk to the
  user and change its context's variables and directory.  A Python command declared with
  ``pass_context=True`` receives one as its first argument::

      @registry.command("deploy", params=[arg("env")], pass_context=True)
      def deploy(ctx, env):
          target = ctx.choose(list_targets(env), title="Deploy which target?")
          if target is None or not ctx.confirm(f"Deploy {target} to {env}?"):
              return 1
          return ctx.run_interactive(["make", "deploy", f"TARGET={target}"])

* :class:`SubshellContext` — what a stage of a multi-command pipeline gets
  instead: its changes stay in the stage (``cd x | cat`` changes nothing).

All three are bound to the context the command **started in**, not to whichever
context is current when they are asked: a command left running in the
background (Ctrl+]) still reads and writes its own context's variables and
directory, so a script started in ``prod`` can never change ``staging``.

Talking to the user goes through the shell's single reader of the terminal
(see the "One reader for real stdin" design note): a Python command runs on a
background thread while the main thread reads every key, so ``input()`` or a
``subprocess.run`` of an interactive program would race it for keystrokes.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Sequence

from . import slots
from .variables import EnvVar, PyVar, registry as var_registry

if TYPE_CHECKING:
    from .context import Context, ContextManager


class ShellView:
    """Read-only view of one shell context."""

    def __init__(self, manager: "ContextManager", context: "Context") -> None:
        self._manager = manager
        self._context = context

    def _is_current(self) -> bool:
        return self._context is self._manager.current()

    @property
    def context_name(self) -> str:
        """The context's name (follows a rename)."""
        return self._context.name

    @property
    def cwd(self) -> str:
        """The context's working directory.  While the context is current
        that is the process's; otherwise the one it will return to."""
        with self._manager.lock:
            return os.getcwd() if self._is_current() else self._context.cwd

    def environ(self) -> dict[str, str]:
        """The context's whole environment (a copy) — ``os.environ`` while
        it is current, what it will return to otherwise."""
        with self._manager.lock:
            return self._manager.environ_in(self._context)

    def get_var(self, name: str) -> str | None:
        """*name* as ``$name`` would expand in this context: a registered
        variable (``aws_region``) or an environment variable; ``None`` when
        unset."""
        with self._manager.lock:
            var = var_registry.get(name)
            if var is None:
                return self._manager.env_value_in(self._context, name)
            if isinstance(var, EnvVar):
                return self._manager.env_value_in(self._context, var.keys[0])
            if isinstance(var, PyVar) and not self._is_current():
                if name in self._context.py_values:
                    return self._context.py_values[name]
            return var.get()


class CommandContext(ShellView):
    """A :class:`ShellView` for a running Python command: it can also ask
    the user, run interactive programs, and set the context's variables.

    Talking to the user isn't possible from a stage of a pipeline (its
    stdin/stdout are pipes): those methods raise :class:`RuntimeError`.
    """

    def __init__(self, manager: "ContextManager", context: "Context", shell) -> None:
        super().__init__(manager, context)
        self._shell = shell

    # ── Talking to the user ─────────────────────────────────────────────

    def input(self, prompt: str = "") -> str:
        """Read one line, with echo and Backspace / Ctrl+U / Ctrl+W editing.

        Ctrl+C raises ``KeyboardInterrupt`` and Ctrl+D on an empty line
        ``EOFError``, as :func:`input` would.  Keys typed before the question
        was asked are dropped, so a stray ``y`` can't answer it.
        """
        return slots._read_from_user(prompt, block=False, what="ctx.input")

    def input_block(self, prompt: str = "") -> str:
        """Read lines until a blank line or Ctrl+D, joined by ``\\n`` — for
        a pasted block (``export KEY=…`` lines, a policy document).  No line
        length limit (cooked mode would cap a line at 1024 bytes on macOS)."""
        return slots._read_from_user(prompt, block=True, what="ctx.input_block")

    def confirm(self, prompt: str, *, default: bool = False) -> bool:
        """Ask a yes/no question.  ``[y/N]`` (or ``[Y/n]`` when *default*)
        is appended; an empty answer is *default*."""
        suffix = " [Y/n] " if default else " [y/N] "
        answer = self.input(prompt.rstrip() + suffix).strip().lower()
        if not answer:
            return default
        return answer in ("y", "yes")

    def choose(self, items: Sequence[str], *, title: str = "") -> str | None:
        """Let the user pick one of *items* in an inline picker (type to
        narrow, Up/Down, Enter).  Returns the chosen item, or ``None`` when
        the picker is dismissed with Esc; Ctrl+C raises
        ``KeyboardInterrupt``.  *title* labels the picker and, once chosen,
        the line that records the choice."""
        return slots._choose(list(items), title)

    def run_interactive(self, argv: Sequence[str], **popen_kwargs) -> int:
        """Run a program that talks to the user (``ssh``, an MFA prompt, a
        TUI) on a terminal of its own; return its exit status.

        ``subprocess.run`` would let the child read the real terminal while
        the shell is reading it too, splitting keystrokes between them.
        Here the child gets a PTY the shell feeds, so Ctrl+] still switches
        contexts and Ctrl+C reaches the child.  *popen_kwargs* go to
        :class:`subprocess.Popen`.
        """
        return slots._run_interactive(list(argv), **popen_kwargs)

    # ── Variables ───────────────────────────────────────────────────────

    def set_var(self, name: str, value: str) -> None:
        """Set *name* in this command's context, as ``var name=value``
        would there.  When the context isn't current (the command was sent
        to the background), it takes effect when the context is next
        entered."""
        with self._manager.lock:
            if self._is_current():
                self._shell._set_variable(name, value)
                return
            keys = self._shell._env_keys_for(name)
            if keys is not None:
                for key in keys:
                    self._context.variables[key] = value
            elif isinstance(var_registry.get(name), PyVar):
                self._context.py_values[name] = value
            else:                       # a GlobalVar: one value everywhere
                var_registry.get(name).set(value)

    def unset_var(self, name: str) -> None:
        """Unset *name* in this command's context (``var name=``)."""
        with self._manager.lock:
            if self._is_current():
                self._shell._unset_variable(name)
                return
            keys = self._shell._env_keys_for(name)
            if keys is not None:
                for key in keys:
                    self._context.variables[key] = None
            elif isinstance(var_registry.get(name), PyVar):
                self._context.py_values[name] = None
            else:
                var_registry.get(name).unset()

    # ── The working directory ───────────────────────────────────────────

    def chdir(self, path: str) -> str:
        """Change this command's context's directory, like ``cd``; return
        the new one.  *path* is relative to :attr:`cwd`, ``~`` expands.

        While the context is current that is ``os.chdir`` (and ``$PWD``);
        once it isn't (the command went to the background) it is the
        directory the context returns to.  Raises :class:`OSError` as
        ``os.chdir`` would.
        """
        path = os.path.expanduser(path)
        with self._manager.lock:
            if self._is_current():
                os.chdir(path)
                os.environ["PWD"] = os.getcwd()
                return os.getcwd()
            target = _resolve_dir(self._context.cwd, path)
            self._context.cwd = target
            return target


class SubshellContext(CommandContext):
    """The ``ctx`` of one stage of a multi-command pipeline: a subshell.

    POSIX shells run each stage of ``a | b`` in a subshell, so ``cd x | cat``
    and ``var X=1 | cat`` change nothing once the line is over.  This is
    that: it reads as its *parent* does until it writes, and its writes
    (``set_var``, ``unset_var``, ``chdir``) stay in it — later reads and the
    stage's own children see them, the context never does.  Every stage
    gets one of its own, as bash does (zsh and ksh run the last stage in
    the shell itself).
    """

    def __init__(self, parent: CommandContext) -> None:
        super().__init__(parent._manager, parent._context, parent._shell)
        self._parent = parent
        self._cwd: str | None = None
        self._env: dict[str, str | None] = {}   # environment keys written here
        self._py: dict[str, str | None] = {}    # PyVar / GlobalVar values written here

    @property
    def cwd(self) -> str:
        return self._cwd if self._cwd is not None else self._parent.cwd

    def environ(self) -> dict[str, str]:
        env = self._parent.environ()
        for key, value in self._env.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def get_var(self, name: str) -> str | None:
        keys = self._shell._env_keys_for(name)
        if keys is None:
            if name in self._py:
                return self._py[name]
        elif keys[0] in self._env:
            return self._env[keys[0]]
        return self._parent.get_var(name)

    def set_var(self, name: str, value: str | None) -> None:
        keys = self._shell._env_keys_for(name)
        if keys is None:
            self._py[name] = value
        else:
            for key in keys:
                self._env[key] = value

    def unset_var(self, name: str) -> None:
        self.set_var(name, None)

    def chdir(self, path: str) -> str:
        self._cwd = _resolve_dir(self.cwd, os.path.expanduser(path))
        return self._cwd


def _resolve_dir(base: str, path: str) -> str:
    """*path* relative to *base*, refused as ``os.chdir`` would refuse it."""
    target = os.path.normpath(os.path.join(base, path))
    if not os.path.exists(target):
        raise FileNotFoundError(2, "No such file or directory", path)
    if not os.path.isdir(target):
        raise NotADirectoryError(20, "Not a directory", path)
    if not os.access(target, os.X_OK):
        raise PermissionError(13, "Permission denied", path)
    return target
