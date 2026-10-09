"""Completion recipes for external commands.

Recipes provide TAB completion for system commands. Enable them in
~/.eosh/config.py:

    from eosh.recipes import enable
    enable("*")              # every built-in recipe and bundled add-on
    enable("make", "git")    # or pick specific ones

Available recipes:
    aws       drives the AWS CLI v2 ``aws_completer`` binary; covers every
              service, operation, flag, and live AWS resource discovery
    chmod     mode operands (common octal + symbolic) and file completion
    chown     USER / USER:GROUP completion (system users + groups), files
    cobra     cobra-based CLIs (kubectl, helm, gh, docker, argocd, …) answered
              by their own ``__complete`` subcommand; add more with
              ``enable_cobra("mytool")``
    cp        copy flags (BSD/macOS vs GNU/Linux), file completion
    curl      flag dictionary and HTTP-method choices for -X / --request
    df        disk-free filesystem usage
    du        disk usage with size options
    find      filters, type, time, size, actions
    git       subcommands, branches, remotes, stash refs, per-subcommand flags
    grep      search flags (also egrep / fgrep / rgrep)
    kill      signal options, PID completion from running processes
              (also registers pkill with process-name completion)
    ls        listing flags
    lsof      flag dictionary, PID completion for -p
    make      Makefile target names and flags
    mv        move flags (BSD/macOS vs GNU/Linux), file completion
    ps        select-by-PID/USER/GROUP flag dictionary
    rm        remove flags (BSD/macOS vs GNU/Linux), file completion
    rsync     flag dictionary, [user@]host:-aware path completion
    scp       flag dictionary, [user@]host:-aware path completion
    ssh       host completion from ~/.ssh/config and known_hosts, options
    tail      follow options and file completion
    tar       create/extract/list flags, archive-aware file completion
    terraform subcommand tree, workspace completion, .tfvars file hints
    top       sort-key choices and PID completion (BSD/macOS vs Linux/procps)
    zip       compression flags, file completion
    unzip     listing/extraction flags, archive-aware file completion

Bundled add-ons — whole Python applications rather than completion
metadata, kept under ``addons/`` in the repository and installed as
``eosh_addons.<name>`` — are enabled the same way:

    awsut     AWS utility commands (whoami, console, ec2, logs,
              cloudformation, SageMaker, Bedrock AgentCore); needs the
              ``eosh[awsut]`` extra

A recipe's command is dropped again when its executable isn't on ``PATH`` —
``enable("tar")`` on a host without ``tar`` is a no-op rather than a hard
failure (see :func:`enable`; recipes don't check for themselves).

Your own recipes are plain Python: call ``registry.command(...)`` in
``config.py``, or in a module ``config.py`` imports.

Tools built on cobra are opted in by name, never detected — detecting one
would mean running an arbitrary command with ``__complete`` as its argument.
``enable("cobra")`` covers the well-known ones; name your own with
``enable_cobra``, from ``config.py`` or a module it imports::

    from eosh.recipes import enable, enable_cobra
    enable("cobra")
    enable_cobra("mytool")
"""

from __future__ import annotations

import shutil
import sys
from importlib import import_module
from pathlib import Path

from ..commands import registry as command_registry
from ..user_errors import config_warning
from .cobra import enable_cobra  # noqa: F401  (public API)


def enable(*recipe_names: str) -> None:
    """Enable built-in recipes and bundled add-ons by name.

    ``"*"`` enables every one.  Lookup: the built-in package
    (``eosh.recipes.<name>``), then the bundled add-ons (``eosh_addons.<name>``,
    found through the ``eosh.addons`` entry-point group).  An unknown name
    raises ``ImportError``.

    A recipe for an external tool doesn't check that the tool is installed:
    whatever completion-only command its ``register()`` adds is dropped
    again here when no executable of that name is on ``PATH``
    (:func:`_drop_absent_tools`).  So ``enable("tar")`` on a host without
    ``tar`` is a no-op, and one recipe registering several names (``grep``,
    ``egrep``, ``rgrep``) keeps exactly the ones that exist.

    An add-on whose *dependency* is missing (``awsut`` without boto3, i.e.
    installed without the ``eosh[awsut]`` extra) is skipped, recorded in
    :data:`skipped_recipes`, and stood in for by a placeholder command named
    after it that says what is missing when run (unless that name means
    something already).  Under ``"*"`` that is quiet — not installing an
    extra is a choice; naming the add-on explicitly also prints the install
    hint as a ``config warning:``.

    No name stops the others, or the rest of ``config.py``: an unknown name
    or a recipe whose ``register()`` raises is a ``config warning:`` (with
    the traceback, for the latter) and ``enable()`` moves on.

    Your own recipes aren't looked up here: define them in ``config.py``, or
    in a module ``config.py`` imports (``sys.path.append`` a team directory).
    """
    wildcard = "*" in recipe_names
    names = _discover_all_recipes() if wildcard else recipe_names
    for name in names:
        try:
            _enable_one(name, quiet_if_missing=wildcard)
        except _UnknownRecipe as e:
            config_warning(str(e))
        except Exception as e:
            config_warning(f"enable({name!r}) failed:", e)


def _enable_one(name: str, quiet_if_missing: bool) -> None:
    try:
        module = _load_recipe(name)
    except ModuleNotFoundError as e:
        if name not in _addons():
            raise
        missing = e.name or str(e)
        skipped_recipes[name] = missing
        _register_unavailable(name, missing)
        if not quiet_if_missing:
            config_warning(missing_message(name, missing))
        return
    skipped_recipes.pop(name, None)
    before = set(command_registry.list_commands())
    module.register()
    _drop_absent_tools(set(command_registry.list_commands()) - before)


def _drop_absent_tools(names: set[str]) -> None:
    """Remove the completion-only commands among *names* — recipes for an
    external tool — whose executable isn't on ``PATH``.  A command with a
    Python handler (an add-on's) is not a tool and always stays."""
    for name in names:
        cmd = command_registry.get(name)
        if cmd is not None and not cmd.has_any_handler() and shutil.which(name) is None:
            command_registry.remove(name)


def missing_message(name: str, module: str) -> str:
    """One line saying what add-on *name* needs.  Add-ons name their extra
    after themselves (see ``pyproject.toml``), so the hint needs no table."""
    return f"{name}: needs the Python module {module!r} — install eosh[{name}]"


def _addons() -> dict[str, str]:
    """Bundled add-ons: name → module, from the ``eosh.addons`` entry points.

    Entry points rather than a scan of ``eosh_addons.__path__``: in an
    editable install that namespace's path is a placeholder the import
    machinery resolves lazily, so listing it finds nothing.
    """
    from importlib.metadata import entry_points

    return {ep.name: ep.value for ep in entry_points(group="eosh.addons")}


# Add-ons ``enable()`` skipped because a dependency could not be imported:
# recipe name → missing module name (e.g. ``{"awsut": "boto3"}``).
skipped_recipes: dict[str, str] = {}


def _register_unavailable(name: str, missing: str) -> None:
    """Register a placeholder ``name`` command that explains the skip.

    Typing ``awsut`` would otherwise fall through to the system and end in a
    bare "command not found", with nothing pointing at the missing extra.

    The placeholder is only a guess at the command's name — a recipe file may
    register a differently-named command (``my_tool.py`` → ``my-tool``) — so
    it steps aside whenever the name means something already: a registered
    command, or an executable on ``PATH``.  A recipe that only adds completion
    to ``git`` must leave ``git`` itself runnable when its dependency is gone.
    """
    if command_registry.has(name) or shutil.which(name):
        return

    def unavailable(*_args: str) -> int:
        print(missing_message(name, missing), file=sys.stderr)
        return 127

    command_registry.command(name, help=f"(unavailable: needs {missing!r} — run it for details)")(unavailable)


def _discover_all_recipes() -> list[str]:
    """Every built-in recipe and bundled add-on, sorted.

    A leading underscore means "not a recipe": support modules
    (``_missing.py`` once, ``cobra``'s helpers) have no ``register()``.
    """
    found = set(_addons())
    found.update(p.stem for p in Path(__file__).parent.glob("*.py")
                 if not p.stem.startswith("_"))
    return sorted(found)


def _load_recipe(name: str):
    """The module for *name*: a built-in recipe, else a bundled add-on."""
    try:
        return import_module(f".{name}", package=__package__)
    except ImportError as e:
        # Only swallow the error if the recipe module itself is missing; a
        # missing dependency (boto3) propagates.
        if e.name != f"{__package__}.{name}":
            raise
    addon = _addons().get(name)
    if addon is not None:
        return import_module(addon)
    raise _UnknownRecipe(f"No recipe or add-on named {name!r}")


class _UnknownRecipe(ImportError):
    """``enable()`` was given a name that is neither a recipe nor an add-on."""
