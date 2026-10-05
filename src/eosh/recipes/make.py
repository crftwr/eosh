"""Completion recipe for make — completes Makefile target names, variables, and flags.

Variable completion covers two phases:

* ``make build CON<TAB>`` → list ``CONFIG=`` along with target names.  Variables
  are gathered from both top-level assignments (``REGION = us-west-2``) and
  ``$(VAR)`` / ``${VAR}`` references inside recipes (so command-line-only
  parameters like ``CONFIG`` show up too).
* ``make build CONFIG=./<TAB>`` → delegate the value side to
  :class:`FileCompleter` when the value prefix looks path-like.  "Looks
  path-like" means it starts with ``./``, ``../``, ``/``, ``~``, *or* it
  contains a ``/`` and the directory portion exists on disk
  (``CONFIG=src/<TAB>`` works when ``src/`` is a real directory).  Bare
  words like ``CONFIG=foo`` get no completion — we don't guess.
"""

from __future__ import annotations

import dataclasses
import os
import re

from ..commands import arg, registry as command_registry
from ..completion import (
    Completer,
    Completion,
    CompletionContext,
    DirCompleter,
    FileCompleter,
)


_ASSIGN_RE = re.compile(
    r"^\s*(?:override\s+|export\s+)*([A-Za-z_][A-Za-z0-9_]*)\s*[:?+]?="
)
_REF_RE = re.compile(r"\$[\(\{]([A-Za-z_][A-Za-z0-9_]*)[\)\}]")
_TARGET_RE = re.compile(r"^([a-zA-Z0-9_][a-zA-Z0-9_./-]*)\s*:(?!=)")

# Variables make sets automatically — never user-overridable on the command line.
_AUTO_VARS = frozenset({
    "MAKE", "MAKEFLAGS", "MAKEFILE_LIST", "MAKELEVEL", "MAKECMDGOALS",
    "MAKESHELL", "MAKE_VERSION", "MAKE_HOST", "MAKE_TERMOUT", "MAKE_TERMERR",
    "CURDIR", "SHELL", ".DEFAULT_GOAL", ".RECIPEPREFIX", ".VARIABLES",
    ".FEATURES", ".INCLUDE_DIRS",
})


def _find_dash_c_dir(args: list[str]) -> str | None:
    """Return the directory passed via -C in args, or None."""
    for i, tok in enumerate(args):
        if tok == "-C" and i + 1 < len(args):
            return args[i + 1]
    return None


def _find_dash_f_file(args: list[str]) -> str | None:
    for i, tok in enumerate(args):
        if tok == "-f" and i + 1 < len(args):
            return args[i + 1]
    return None


def _read_makefile(directory: str | None, explicit_file: str | None) -> str | None:
    if explicit_file:
        try:
            with open(explicit_file) as f:
                return f.read()
        except OSError:
            return None
    base = directory or "."
    for name in ("Makefile", "makefile", "GNUmakefile"):
        path = os.path.join(base, name)
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    return f.read()
            except OSError:
                return None
    return None


def _parse_targets(content: str) -> list[str]:
    targets = []
    for line in content.splitlines():
        m = _TARGET_RE.match(line)
        if m:
            targets.append(m.group(1))
    return sorted(set(targets))


def _parse_variables(content: str) -> list[str]:
    """Collect overridable Makefile variable names.

    Union of top-level assignments and ``$(VAR)`` / ``${VAR}`` references,
    minus make's automatic variables.  Recipe references are included so
    that command-line-only parameters (declared nowhere but used everywhere,
    e.g. ``CONFIG``) appear in completion.
    """
    found: set[str] = set()
    for line in content.splitlines():
        m = _ASSIGN_RE.match(line)
        if m:
            found.add(m.group(1))
    for m in _REF_RE.finditer(content):
        found.add(m.group(1))
    return sorted(found - _AUTO_VARS)


def _looks_like_path(prefix: str) -> bool:
    """True when *prefix* should trigger filesystem completion.

    Triggers on: explicit path roots (``./``, ``../``, ``/``, ``~``); or any
    prefix containing ``/`` whose directory portion exists on disk (so
    ``src/<TAB>`` works when ``src`` exists in cwd).
    """
    if not prefix:
        return False
    if prefix.startswith(("./", "../", "/", "~")):
        return True
    if "/" not in prefix:
        return False
    head = prefix.rsplit("/", 1)[0]
    if not head:
        return False
    return os.path.isdir(os.path.expanduser(head))


class MakeTargetCompleter(Completer):
    """Completes both Makefile targets and ``VAR=`` / ``VAR=path`` arguments.

    The completer detects ``=`` in the current token and switches between
    three phases:

    * No ``=``: union of target names and ``VAR=`` suggestions.
    * ``VAR=`` with a path-like value prefix: delegate to :class:`FileCompleter`.
    * ``VAR=`` with a bare-word value prefix: no completion (we don't guess).
    """

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        prefix = ctx.prefix

        if "=" in prefix:
            return self._complete_value(ctx, prefix)

        return self._complete_name(ctx, prefix)

    def _complete_name(self, ctx: CompletionContext, prefix: str) -> list[Completion]:
        content = self._load(ctx.args)
        targets: list[str] = []
        variables: list[str] = []
        if content is not None:
            targets = _parse_targets(content)
            variables = _parse_variables(content)

        results: list[Completion] = []
        for t in targets:
            if t.startswith(prefix):
                results.append(Completion(value=t, description="target"))
        for v in variables:
            if v.startswith(prefix):
                results.append(Completion(
                    value=f"{v}=",
                    display=f"{v}=",
                    description="variable",
                ))
        return results

    def _complete_value(self, ctx: CompletionContext, prefix: str) -> list[Completion]:
        key, _, val_prefix = prefix.partition("=")
        if not _looks_like_path(val_prefix):
            return []
        sub_ctx = dataclasses.replace(ctx, prefix=val_prefix)
        return [
            Completion(
                value=f"{key}={c.value}",
                display=c.display or c.value,
                description=c.description,
            )
            for c in FileCompleter().complete(sub_ctx)
        ]

    def _load(self, args: list[str]) -> str | None:
        return _read_makefile(_find_dash_c_dir(args), _find_dash_f_file(args))


def register() -> None:
    command_registry.command(
        "make",
        help="build targets from a Makefile",
        params=[
            arg("target", nargs="*", help="target to build",
                completer=MakeTargetCompleter()),
            arg("-B", action="store_true", help="unconditionally make all targets"),
            arg("-C", metavar="DIR", help="change to directory before doing anything",
                completer=DirCompleter()),
            arg("-d", action="store_true", help="print lots of debugging information"),
            arg("-e", action="store_true", help="give environment variables precedence over Makefile variables"),
            arg("-f", metavar="FILE", help="read FILE as the Makefile", completer=FileCompleter()),
            arg("-i", action="store_true", help="ignore errors from recipes"),
            arg("-j", metavar="N", help="number of parallel jobs (omit for unlimited)"),
            arg("-k", action="store_true", help="keep going after errors as much as possible"),
            arg("-l", metavar="N", help="don't start new jobs if load average is above N"),
            arg("-n", action="store_true", help="print commands without executing them (dry run)"),
            arg("-o", metavar="FILE", help="do not remake FILE even if it is older than its dependencies",
                completer=FileCompleter()),
            arg("-p", action="store_true", help="print make's internal database"),
            arg("-q", action="store_true", help="exit 0 if all targets are up to date, 1 otherwise"),
            arg("-r", action="store_true", help="disable built-in implicit rules"),
            arg("-R", action="store_true", help="disable built-in variable settings"),
            arg("-s", action="store_true", help="silent mode — do not echo recipes"),
            arg("-S", action="store_true", help="cancel the effect of -k"),
            arg("-t", action="store_true", help="touch targets instead of running their recipes"),
            arg("-v", action="store_true", help="print version information"),
            arg("-w", action="store_true", help="print working directory before and after processing"),
            arg("-W", metavar="FILE", help="pretend FILE was just modified", completer=FileCompleter()),
            arg("--warn-undefined-variables", action="store_true",
                help="warn when an undefined variable is referenced"),
        ],
    )
