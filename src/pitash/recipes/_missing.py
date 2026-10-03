"""What to tell the user when a recipe's dependency is not installed.

A recipe that imports a third-party module (``awsut`` → boto3, a user recipe
→ ``requests``) fails with ``ModuleNotFoundError`` when pitash's environment
lacks it.  That environment is wherever pitash was installed — a ``uv tool``
venv, a ``pipx`` venv, a plain interpreter — and the fix is a different
command in each, so :func:`install_command` works out which one applies
instead of always saying ``pip install``.  (Running ``pip install boto3`` in
another shell installs into the *wrong* interpreter for the first two, which
is exactly the confusion this is here to prevent.)

Leading underscore: a support module, not a recipe — see
``_discover_all_recipes``.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

# Built-in recipes whose dependencies are packaged as an extra of pitash
# itself.  For these the fix is "install pitash[<extra>]", not "install the
# missing module" — the extra pins every dependency the recipe needs, and the
# module name alone may not even be the package name.
RECIPE_EXTRAS: dict[str, str] = {"awsut": "aws"}

_UV_RECEIPT = "uv-receipt.toml"
_PIPX_METADATA = "pipx_metadata.json"


def install_command(*, extra: str | None = None, module: str | None = None,
                    prefix: str | Path | None = None) -> str:
    """Return a shell command that installs the fix into pitash's environment.

    Pass *extra* (``"aws"``) for a pitash extra, or *module* (``"yaml"``) for
    a bare missing module — in which case the module's top-level name stands
    in for the package name, which is right far more often than not but is
    only ever a guess (``yaml`` is published as ``pyyaml``).

    *prefix* defaults to ``sys.prefix``; tests pass a fake venv.
    """
    venv = Path(prefix if prefix is not None else sys.prefix)
    package = module.split(".")[0] if module else None

    if (venv / _UV_RECEIPT).is_file():
        return _uv_command(venv / _UV_RECEIPT, extra=extra, package=package)
    if (venv / _PIPX_METADATA).is_file():
        return _pipx_command(venv / _PIPX_METADATA, extra=extra, package=package)
    target = f"pitash[{extra}]" if extra else package
    return f"{shlex.quote(sys.executable)} -m pip install {shlex.quote(target)}"


def missing_message(recipe: str, module: str, *,
                    prefix: str | Path | None = None) -> str:
    """The multi-line explanation printed when a skipped recipe is used."""
    extra = RECIPE_EXTRAS.get(recipe)
    if extra:
        what = f"the [{extra}] extra (Python module {module!r} is not installed)"
    else:
        what = f"Python module {module!r}, which is not installed"
    cmd = install_command(extra=extra, module=None if extra else module,
                          prefix=prefix)
    lead = "Install it with:" if extra else "Install the package that provides it, e.g.:"
    return (
        f"{recipe}: unavailable — needs {what} in pitash's environment.\n"
        f"{lead}\n"
        f"  {cmd}\n"
        f"then restart pitash."
    )


# ── uv tool ──────────────────────────────────────────────────────────────────

def _uv_command(receipt: Path, *, extra: str | None, package: str | None) -> str:
    """``uv tool install …`` that *keeps* the tool's current requirements.

    ``uv tool install`` replaces the ``--with`` list rather than adding to it
    (an earlier ``--with requests`` is uninstalled by a later ``--with
    pyyaml``), so the command is rebuilt from the receipt uv wrote when the
    tool was installed — the same file ``uv tool upgrade`` reads.  A
    requirement the receipt describes in a way we don't reconstruct (a URL, a
    local directory, an editable) gives up on exactness and says so.
    """
    reqs = _read_uv_requirements(receipt)
    fallback = (
        "uv tool install pitash"
        + (f"[{extra}]" if extra else "")
        + (f" --with {shlex.quote(package)}" if package else "")
        + "   # keep the --with entries from `uv tool list --show-with`"
    )
    if reqs is None:
        return fallback

    rendered: list[str] = []
    for req in reqs:
        name = req.get("name")
        if not isinstance(name, str) or set(req) - {"name", "extras", "specifier"}:
            return fallback
        extras = [e for e in req.get("extras", []) if isinstance(e, str)]
        if name == "pitash" and extra and extra not in extras:
            extras.append(extra)
        text = name + (f"[{','.join(extras)}]" if extras else "")
        text += req.get("specifier", "") or ""
        rendered.append(text)

    if not rendered or reqs[0].get("name") != "pitash":
        return fallback
    names = {r["name"].lower() for r in reqs}
    if package and package.lower() not in names:
        rendered.append(package)

    head, *withs = rendered
    parts = ["uv", "tool", "install", shlex.quote(head)]
    for w in withs:
        parts += ["--with", shlex.quote(w)]
    return " ".join(parts)


def _read_uv_requirements(receipt: Path) -> list[dict] | None:
    try:
        import tomllib
        data = tomllib.loads(receipt.read_text())
    except (OSError, ValueError):
        return None
    reqs = data.get("tool", {}).get("requirements")
    if not isinstance(reqs, list) or not all(isinstance(r, dict) for r in reqs):
        return None
    return reqs


# ── pipx ─────────────────────────────────────────────────────────────────────

def _pipx_command(metadata: Path, *, extra: str | None, package: str | None) -> str:
    """``pipx inject`` — adds to the venv, unlike ``pipx install --force``.

    For an extra, inject the extra's own requirements (read from pitash's
    installed metadata) so nothing else in the venv is disturbed.
    """
    venv_name = "pitash"
    try:
        data = json.loads(metadata.read_text())
        venv_name = data.get("main_package", {}).get("package") or venv_name
    except (OSError, ValueError, AttributeError):
        pass

    targets = _extra_requirements(extra) if extra else [package]
    if not targets:
        return f"pipx install --force {shlex.quote(f'pitash[{extra}]')}"
    return " ".join(["pipx", "inject", shlex.quote(venv_name),
                     *(shlex.quote(t) for t in targets)])


def _extra_requirements(extra: str) -> list[str]:
    """Requirement strings of pitash's *extra*, from the installed metadata."""
    from importlib import metadata
    try:
        requires = metadata.requires("pitash") or []
    except metadata.PackageNotFoundError:
        return []
    out: list[str] = []
    for req in requires:
        spec, _, marker = req.partition(";")
        compact = marker.replace(" ", "").replace("'", '"')
        if f'extra=="{extra}"' in compact:
            out.append(spec.strip())
    return out
