# Add-ons

An **add-on** is a Python application that plugs into eosh: a command tree
with its own handlers, settings and output. A **recipe** is different: a few
dozen lines of completion metadata for an external command (see
[recipes.md](recipes.md)). `awsut` is the first add-on. It was moved out of
`src/eosh/recipes/` because at ~10,000 lines it was about 39% of the
core package (discussion #42).

Add-ons ship inside the `eosh` distribution, but they are kept apart from the
core. Each one uses only eosh's public API, so it could later move to its
own distribution without touching the core.

## Layout

```
addons/                  # namespace package eosh_addons (no __init__.py)
└── <name>/              # → import eosh_addons.<name>
    ├── __init__.py      # exposes register()
    ├── README.md        # what it is, how to enable it, its conventions
    └── …
tests/addons/
├── test_boundary.py     # public-API rule for every add-on
└── <name>/              # the add-on's own tests
```

The directory is `addons/<name>/` and the import name is
`eosh_addons.<name>`. The prefix keeps a short add-on name (`awsut`, `k8s`)
from colliding with anything else on `sys.path`. The mapping is the one thing
`pyproject.toml` can't express, so a small [`setup.py`](../setup.py) builds
the package list. `addons/` has no `__init__.py`: `eosh_addons` is a
namespace package, so an add-on that later becomes its own distribution keeps
its import name.

## Registering an add-on

Two lines in `pyproject.toml`:

```toml
[project.optional-dependencies]
<name> = ["third-party", "deps"]          # only if it has any

[project.entry-points."eosh.addons"]
<name> = "eosh_addons.<name>"
```

The entry point is how `enable("<name>")` and `enable("*")` find the add-on,
in a wheel and in an editable install alike. Scanning
`eosh_addons.__path__` doesn't work in editable mode: the path is a
placeholder there and lists nothing. Re-run `make install` (or
`pip install -e .`) after adding an entry point.

**Name the extra after the add-on.** When an import fails with
`ModuleNotFoundError`, the hint is built from the name alone:
`awsut: needs the Python module 'boto3' — install eosh[awsut]`. No table in
the core has to know about the add-on. Under `enable("*")` the add-on is then
skipped quietly and a placeholder command explains the gap. Naming it
explicitly raises with the same message.

Lookup order in `enable(name)`: built-in recipe → add-on → user recipe search
path.

## The public-API rule

An add-on may import from these modules only:

| Module | For |
|---|---|
| `eosh` | `arg`, `Var`, `EnvVar`, `var_registry`, `command_registry`, `passthrough_run` / `passthrough_input` / `passthrough_input_block`, … |
| `eosh.commands` | `registry`, `arg`, the command-tree API |
| `eosh.completion` | `Completer`, `Completion`, `CompletionContext`, the built-in completers |
| `eosh.completion_cache` | `get_or_fetch`, `aws_env_key` |
| `eosh.variables` | `Var`, `registry` |
| `eosh.recipes` | `enable`, `enable_cobra` |
| `eosh.recipes.aws` | the region list and profile completer awsut builds on |

[`tests/addons/test_boundary.py`](../tests/addons/test_boundary.py) enforces
the rule by parsing every add-on source file. It also checks that:

- relative imports stay inside the add-on's own package;
- an add-on doesn't import another add-on;
- every `addons/<name>/` has an entry point, and every entry point has a
  directory.

If an add-on needs something that isn't public yet, make it public in the
core (re-export it from `eosh`, or document the module) and add it to
`PUBLIC_MODULES` in the same change. Don't reach past the boundary.
