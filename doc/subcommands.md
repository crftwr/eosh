# Sub-Command Trees

## Goal

One uniform mechanism for nested commands such as
`awsut sagemaker hyperpod create`, `git stash pop`, or arbitrary-depth user
trees like `deploy ec2 instances list`.

1. **Arbitrary nesting depth.**
2. **TAB completion at every level**: sub-command names, positionals and
   flags.
3. **Flags at any token position**, before or between sub-command names.
4. **No dispatch boilerplate.** Handlers receive parsed kwargs and never
   switch on `args[0]`.
5. **One type and one builder** for Python commands and external-command
   recipes (`git`, `terraform`, …). A flat command is just a tree with no
   children, so it uses the same rules.

## Single Type: `Command`

Root nodes, groups, commands and external-command recipes are all the same
class. A node's role is **inferred from structure**:

| Structure                         | Role                                  |
|-----------------------------------|---------------------------------------|
| Has handler, no children          | Python command                        |
| Has children, no handler          | Group (prints its sub-commands)       |
| Tree contains no handler anywhere | External recipe (completion only)     |

A node never has **both** a handler and children. `node.command(...)` on a
node with a handler raises `ValueError`, and so does attaching a handler to a
node with children. No command ever needed both, and forbidding it keeps one
meaning per token: either it names a sub-command, or it is an argument.

```python
@dataclass
class Command:
    name: str
    func: Callable | None              # the handler
    params: list[Arg] | None           # this node's flags + positionals
    help: str | None
    delegate: Completer | None         # answers every completion slot
    parent: "Command | None"
    children: dict[str, "Command"]
```

Completion is derived from `params` when it is asked for:
`node.options_completer()`, `node.positional_completer(i)` and
`node.takes_value(flag)`. No second structure has to be kept in sync with
the list argparse parses.

## API

`registry.command(name, ...)` creates and registers a root. `node.command(name, ...)`
creates a child. Both have the same two forms, and both return the `Command`.

```python
from eosh.commands import registry, arg

# Plain call → a group (or an external-command recipe)
awsut = registry.command("awsut", help="AWS utility commands")
sagemaker = awsut.command("sagemaker", help="SageMaker resources")
cluster = sagemaker.command("hyperpod").command("cluster", help="cluster management")

# Decorator → a command; the function becomes the handler
@cluster.command("describe", params=[
    arg("name", completer=ClusterNameCompleter()),
    arg("--show-nodes", action="store_true", help="include node list"),
])
def cluster_describe(name, show_nodes):
    ...
```

A name is always required. There is no form that takes the function's
`__name__`.

### Flags belong to one node

A node's flags are its own; **nothing is inherited** from ancestors. When
several commands share a flag, declare it once and list it on each:

```python
CATEGORY = arg("--category", metavar="CATEGORY", completer=CategoryCompleter())

@jobs.command("list", params=[CATEGORY, ...])
def jobs_list(category, ...): ...

@jobs.command("stop", params=[CATEGORY, arg("job_name")])
def jobs_stop(job_name, category): ...
```

An earlier version merged ancestor flags into every descendant. It filtered
the kwargs against each handler's signature so a leaf could ignore the
flags it didn't want. That one feature had exactly one user
(`awsut sagemaker jobs --category`), so it was removed (discussion #41). A
handler now receives exactly the params it declares.

### External-Only Trees

When **no node** in a tree carries a handler, the tree is an
external-command recipe. Completion uses the tree; running it shells out to
the real binary via the PTY path.

```python
git = registry.command("git", help="distributed version control")
git.command("commit", params=[
    arg("-m", metavar="MSG"),
    arg("--amend", action="store_true"),
])
git.command("stash").command("pop", params=[
    arg("ref", nargs="?", completer=GitStashRefCompleter()),
])
```

A recipe's `params` describe the external tool's flags for completion only;
argparse never parses them. That is why its help text carries no generated
usage line: the tool's own `--help` is the authority.

### Delegates

`registry.command("aws", delegate=AwsCompleter())` hands **every** slot
(flags and every positional) to one completer. It is for a tool that ships
its own completion protocol: `aws_completer`, or cobra's `__complete` (see
`eosh.recipes.enable_cobra`). It can't be combined with `params`.

## Resolution

Given the tokens after the command name, `Command.resolve()` walks down:

```
node = root
for each token:
    flag            → skip it (and its value, if it is a value-taking flag *of node*)
    a child's name  → node = that child
    anything else   → stop: the rest are node's arguments
```

It returns the deepest node and the tokens that weren't sub-command names.
To run a command, `node`'s argparse parses those tokens and the handler is
called with the resulting kwargs. A group with no handler prints its
sub-command list. A tree with no handler anywhere goes to the external
binary instead.

## Completion and the status bar

TAB completion (`Shell._get_base_completions`) and the status bar
(`Shell._get_arg_info`) share one classifier, `shell._resolve_slot`. It
resolves the node, then decides what the token being typed (or under the
caret) is:

| Slot          | When                                                      | TAB offers                          |
|---------------|-----------------------------------------------------------|-------------------------------------|
| `delegate`    | the node has a `delegate`                                 | the delegate's candidates           |
| `flag`        | the token starts with `-`/`+` and the node has flags      | the node's flags                    |
| `value`       | the previous token is one of the node's value-taking flags | that flag's value completer, or nothing |
| `subcommand`  | the node has children and this is its first positional    | the children's names                |
| `positional`  | otherwise                                                 | the node's completer for that slot; with none, argcomplete / files |

Flat commands and trees take the same path. A flat command is the case where
`resolve()` returns the root itself.

A deeper node's flags are offered only once that node is reached.
`awsut sagemaker hyperpod --<TAB>` doesn't offer `--show-nodes`, which is
defined at `... cluster describe`. This matches runtime semantics: a flag
typed before its node would be rejected by argparse there too.

## Recipes

`recipes/git.py`, `recipes/terraform.py` and the `awsut` add-on
(`addons/awsut/cli.py`) build trees. Most recipes (`ls`, `du`, `tail`,
`kill`, `find`, `grep`, `make`, `ssh`, `df`, …) are flat: a single
`registry.command(name, params=[...])` call with no handler. Cobra-based
tools need no hand-written recipe: `enable_cobra("name")` (or the `cobra`
recipe) gives them a `CobraCompleter` delegate.

## Out of Scope

* **Auto-discovery** of sub-commands from filesystem layout.
* **Permissive flag aggregation**, i.e. offering descendants' flags at a
  group.
* **Type-driven dispatch.** Handlers remain plain functions taking parsed
  kwargs.
