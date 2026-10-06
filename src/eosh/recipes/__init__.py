"""Completion recipes for external commands.

Recipes provide TAB completion for system commands. Enable them in
~/.eosh/config.py:

    from eosh.recipes import enable
    enable("*")              # all built-in + user recipes
    enable("make", "git")    # or pick specific ones

Available recipes:
    aws       drives the AWS CLI v2 ``aws_completer`` binary; covers every
              service, operation, flag, and live AWS resource discovery
    awsut     AWS utility commands — console URL opening, recent cost report,
              ec2 / cloudwatch logs / cloudformation; under
              `awsut sagemaker ...`, jobs + hub content (with ARN lineage
              tracing), Studio domains/spaces/apps, and HyperPod clusters; and
              under `awsut bedrock-agentcore ...`, Harness resources with their
              versions and endpoints, and Memory resources with the actors,
              sessions, events and extracted records they hold
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

Recipes for external commands silently skip registration when the
underlying command is not available on ``PATH`` — ``enable("tar")`` on a
host without ``tar`` is a no-op rather than a hard failure.

Tools built on cobra are opted in by name, never detected — detecting one
would mean running an arbitrary command with ``__complete`` as its argument.
``enable("cobra")`` covers the well-known ones; name your own with
``enable_cobra``, from ``config.py`` or a user recipe's ``register()``::

    from eosh.recipes import enable, enable_cobra
    enable("cobra")
    enable_cobra("mytool")
"""

from __future__ import annotations

import importlib.util
import sys
from importlib import import_module
from pathlib import Path

from ..paths import config_dir
from .cobra import enable_cobra  # noqa: F401  (public API)

# Directories searched in order when a recipe is not found in the built-in
# package.  The default entry covers the conventional user recipe location;
# call add_recipe_path() to register additional directories.
recipe_search_path: list[Path] = [config_dir() / "recipes"]


def add_recipe_path(path: str | Path) -> None:
    """Append *path* to the recipe search path.

    Recipes in directories added earlier in the list take priority over those
    added later.  The built-in package always has the highest priority.

    Example (in ~/.eosh/config.py)::

        from eosh.recipes import add_recipe_path, enable
        add_recipe_path("/team/shared/recipes")
        enable("my_tool")   # found in ~/.eosh/recipes/ or /team/shared/recipes/
    """
    recipe_search_path.append(Path(path))


def enable(*recipe_names: str) -> None:
    """Enable one or more completion recipes by name.

    Pass ``"*"`` to enable all discoverable recipes (built-in + search path).

    Lookup order for each name:

    1. Built-in package (``eosh.recipes.<name>``).
    2. Each directory in :data:`recipe_search_path` in order
       (default: ``~/.eosh/recipes/``).

    Raises ``ImportError`` if the recipe is not found anywhere.

    With ``"*"``, a recipe whose *dependency* is missing (``awsut`` without
    boto3, i.e. installed without the ``[aws]`` extra) is skipped and recorded
    in :data:`skipped_recipes` instead of aborting the whole call — the same
    "absent tool is a no-op" rule as a recipe whose command is not on
    ``PATH``.  Naming such a recipe explicitly still raises.

    Skipping is not silent at the point of use, though: unless it would
    shadow a real executable, a placeholder command named after the recipe is
    registered, and running it explains what is missing and prints the
    install command for *this* environment (``uv tool``, ``pipx`` or pip —
    see :mod:`eosh.recipes._missing`).  It also shows up in ``help``.

    A *user* recipe that fails under ``"*"`` — for any reason, including an
    import failing inside a helper module it imports — is reported on stderr
    with a traceback through the user's files, and the remaining recipes
    still load.  Only a built-in recipe's missing dependency is skipped
    quietly: that is an optional extra not installed, while a user recipe's
    ``ModuleNotFoundError`` is as likely a typo as a missing package.
    """
    wildcard = "*" in recipe_names
    names = _discover_all_recipes() if wildcard else recipe_names
    for name in names:
        try:
            module = _load_recipe(name)
            skipped_recipes.pop(name, None)
            module.register()
        except Exception as e:
            if not wildcard:
                raise
            builtin = _is_builtin(name)
            if not builtin:
                _report_user_recipe_error(name, e)
            if isinstance(e, ModuleNotFoundError):
                missing = e.name or str(e)
                skipped_recipes[name] = missing
                _register_unavailable(name, missing)
            elif builtin:
                raise  # a bug in eosh itself — don't hide it


def _is_builtin(name: str) -> bool:
    here = Path(__file__).parent
    return (here / f"{name}.py").exists() or (here / name / "__init__.py").exists()


def _report_user_recipe_error(name: str, exc: Exception) -> None:
    from ..user_errors import format_user_exception

    print(f"eosh: recipe {name!r} failed to load and was skipped:", file=sys.stderr)
    print(format_user_exception(exc), file=sys.stderr, end="")


# Recipes ``enable("*")`` skipped because a dependency could not be imported:
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
    import shutil
    from ..commands import registry

    if registry.has(name) or shutil.which(name):
        return

    from ._missing import missing_message

    def unavailable(*_args: str) -> None:
        print(missing_message(name, missing), file=sys.stderr)
        raise SystemExit(127)

    registry.command(name, help=f"(unavailable: needs {missing!r} — run it for how to install)")(unavailable)


def _discover_all_recipes() -> list[str]:
    """Return sorted list of all available recipe names (built-in + search path).

    A leading underscore means "not a recipe": a recipe is a module with a
    ``register()`` function, and support modules a recipe imports
    (``_awsut_common.py``, a user's own ``_helpers.py``) have none, so globbing
    them in would make ``enable("*")`` fail on an ``AttributeError``.  The
    ``_awsut_sagemaker`` / ``_awsut_agentcore`` subpackages are skipped for the
    same reason and already escaped by being directories rather than ``.py``
    files.
    """
    found: set[str] = set()

    for directory in [Path(__file__).parent, *recipe_search_path]:
        if not directory.is_dir():
            continue
        for p in directory.glob("*.py"):
            if not p.stem.startswith("_"):
                found.add(p.stem)

    return sorted(found)


def _load_recipe(name: str):
    """Return the module for *name*, searching built-ins then recipe_search_path."""
    # 1. Try built-in package first.
    try:
        return import_module(f".{name}", package=__package__)
    except ImportError as e:
        # Only swallow the error if the recipe module itself is missing.
        # If a dependency (e.g. boto3) is missing, propagate the error.
        if e.name != f"{__package__}.{name}":
            raise

    # 2. Walk recipe_search_path; return the first match.
    for directory in recipe_search_path:
        candidate = Path(directory) / f"{name}.py"
        if candidate.exists():
            spec = importlib.util.spec_from_file_location(
                f"eosh_user_recipe_{name}", candidate
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

    # 3. Not found anywhere.
    searched = ", ".join(str(d) for d in recipe_search_path)
    raise ImportError(
        f"Recipe {name!r} not found in built-in recipes or search path: [{searched}]"
    )
