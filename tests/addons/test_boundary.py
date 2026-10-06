"""Add-ons under ``addons/`` use eosh's public API and nothing else.

Each add-on is meant to be liftable into its own distribution later without
touching the core, so the boundary is checked rather than trusted:

* ``eosh`` imports are limited to :data:`PUBLIC_MODULES`;
* relative imports stay inside the add-on's own package (no reaching into a
  sibling add-on);
* every ``addons/<name>/`` directory is registered as an ``eosh.addons``
  entry point in ``pyproject.toml`` — and every entry point has a directory
  — since that is how ``enable()`` finds an add-on.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADDONS = ROOT / "addons"

# The modules an add-on may import from.  ``eosh`` itself re-exports the
# passthrough helpers, Var, arg, the registries, …; the rest are the
# documented extension surfaces.  Anything else (``eosh.shell``,
# ``eosh.lineedit``, ``eosh.tui``, a ``_private`` module) is internal.
PUBLIC_MODULES = {
    "eosh",
    "eosh.commands",
    "eosh.completion",
    "eosh.completion_cache",
    "eosh.variables",
    "eosh.recipes",
    "eosh.recipes.aws",
}


def _addon_dirs() -> list[Path]:
    return sorted(
        p for p in ADDONS.iterdir()
        if p.is_dir() and (p / "__init__.py").exists()
    )


def _sources() -> list[tuple[str, Path]]:
    return [
        (addon.name, path)
        for addon in _addon_dirs()
        for path in sorted(addon.rglob("*.py"))
    ]


def _module_name(addon_dir: Path, path: Path) -> str:
    rel = path.relative_to(ADDONS).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(["eosh_addons", *parts])


def _violations(addon: str, path: Path) -> list[str]:
    module = _module_name(ADDONS / addon, path)
    # A package's own __init__ resolves relative imports against itself.
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    own_root = f"eosh_addons.{addon}"
    found = []
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.Import):
            targets = [(alias.name, node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                target = ".".join(base + ([node.module] if node.module else []))
                if not (target == own_root or target.startswith(own_root + ".")):
                    found.append(f"{path.name}:{node.lineno} relative import leaves "
                                 f"the add-on: {target}")
                continue
            targets = [(node.module or "", node.lineno)]
        else:
            continue
        for name, line in targets:
            if name == "eosh" or name.startswith("eosh."):
                if name not in PUBLIC_MODULES:
                    found.append(f"{path.name}:{line} imports eosh internals: {name}")
            elif name.startswith("eosh_addons.") and not (
                name == own_root or name.startswith(own_root + ".")
            ):
                found.append(f"{path.name}:{line} imports another add-on: {name}")
    return found


def test_there_is_something_to_check():
    assert _addon_dirs(), "no add-ons found — has addons/ moved?"


@pytest.mark.parametrize(
    "addon,path", _sources(), ids=lambda v: v if isinstance(v, str) else v.name
)
def test_addon_uses_only_public_api(addon, path):
    assert _violations(addon, path) == []


def test_every_addon_has_an_entry_point():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    entry_points = meta["project"]["entry-points"]["eosh.addons"]
    dirs = {p.name for p in _addon_dirs()}
    assert set(entry_points) == dirs
    for name, target in entry_points.items():
        assert target == f"eosh_addons.{name}"


def test_addons_dir_is_a_namespace():
    """No ``addons/__init__.py``: ``eosh_addons`` is a namespace package, so
    an add-on can later move to its own distribution under the same name."""
    assert not (ADDONS / "__init__.py").exists()
