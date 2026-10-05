# Sub-Command Trees

## Goal

Provide a single, uniform mechanism for declaring nested command structures
such as `awsut sagemaker hyperpod create`, `git stash pop`, or arbitrary-depth
user trees like `deploy ec2 instances list`.

The mechanism must:

1. Support **arbitrary nesting depth** (not just two levels).
2. Provide **TAB completion** at every level — sub-command names, positional
   arguments, and flags — with flags inherited down the tree from ancestors.
3. Allow **flags at any token position**, including before the sub-command
   that defines them is typed (with the documented limitation that a flag is
   only visible to completion once its defining node is reachable).
4. Eliminate **per-handler dispatch boilerplate** in user code: leaf
   handlers receive parsed kwargs, never a switch on `args[0]`.
5. Use the **same type and builder method** for Python commands and external
   command recipes (`aws`, `git`, etc.), so users learn one API.

## Single Type: `Command`

There is exactly one node type. Root nodes, interior groups, leaves, and
external-command shells are all instances of the same class. A node's role
is **inferred from structure**, not declared by a flag or a separate type:

| Structure                                            | Role               |
|------------------------------------------------------|--------------------|
| Has handler, no children                             | Python leaf        |
| Has children, no handler                             | Interior group     |
| Has children **and** handler                         | Group with default |
| Tree contains no handler anywhere                    | External recipe    |

```python
class Command:
    name: str
    help: str
    params: list[Arg]                  # flags + positionals at this node
    parent: "Command | None"
    children: dict[str, "Command"]
    handler: Callable | None           # set when used as a decorator
```

There is one builder method (`.command()`) and one vocabulary (`arg()`).
Roots are obtained from the registry; everything below comes from chaining
`.command()` on a parent node.

## API

### Creating a Root

```python
from eosh.commands import registry, arg

awsut = registry.command(
    "awsut",
    help="AWS utility commands",
    params=[
        arg("--region",  metavar="REGION",  completer=AwsRegionCompleter()),
        arg("--profile", metavar="PROFILE", completer=AwsProfileCompleter()),
        arg("--debug",   action="store_true"),
    ],
)
```

`registry.command(name, ...)` is the only special entry point. It creates a
root `Command` and registers it under `name` in the command table. The
returned object is a normal `Command` node — everything else uses
`Command.command()`.

### Creating Interior Groups

Same method, used **bare** (no decorator):

```python
sagemaker = awsut.command("sagemaker", help="SageMaker resources")
hyperpod = sagemaker.command("hyperpod", help="SageMaker HyperPod operations")
cluster = hyperpod.command("cluster", help="cluster management")
```

Each call returns the new child node, so callers can keep the reference and
add further children. Interior groups can carry their own `params=` for
flags valid at that depth and below.

### Creating Leaves

Same method, used as a **decorator**, with a Python handler:

```python
@cluster.command("describe", params=[
    arg("name", completer=ClusterNameCompleter()),
    arg("--show-nodes", action="store_true", help="include node list"),
])
def cluster_describe(name, show_nodes=False, region=None, profile=None):
    ...
```

The decorator attaches `cluster_describe` as the node's handler. Inherited
flags (`region`, `profile` from the root) are passed as kwargs alongside
the node's own parsed args.

### Arbitrary Depth

`.command()` on any node returns a node, which itself has `.command()`. So:

```python
deploy_ec2_instances = registry.command("deploy").command("ec2").command("instances")

@deploy_ec2_instances.command("list", params=[
    arg("--filter", action="append", metavar="KEY=VAL"),
])
def deploy_ec2_instances_list(filter=None, region=None, profile=None): ...

@deploy_ec2_instances.command("describe", params=[
    arg("instance_id", completer=InstanceIdCompleter()),
])
def deploy_ec2_instances_describe(instance_id, region=None, profile=None): ...
```

There is no built-in depth limit.

### External-Only Trees

When **no node** in a tree carries a handler, the framework treats the tree
as an external-command recipe. Completion uses the tree; execution shells
out to the real binary via PTY (existing path).

```python
git = registry.command("git")            # no handler at root
git.command("commit", params=[
    arg("-m", metavar="MSG"),
    arg("--amend",   action="store_true"),
    arg("--no-edit", action="store_true"),
])
git.command("push", params=[
    arg("--force",       action="store_true"),
    arg("--set-upstream", action="store_true"),
    arg("remote", nargs="?", completer=GitRemoteCompleter()),
    arg("branch", nargs="?", completer=GitBranchCompleter()),
])
git.command("stash").command("pop", params=[
    arg("ref", nargs="?", completer=GitStashRefCompleter()),
])
```

Identical builder API — the only difference is that no `@node.command(...)`
decorator is ever applied with a function. Existing recipes
(`recipes/git.py`, `recipes/aws.py`, etc.) are migrated to this form.

### Mixed Trees

A single tree can mix Python handlers and unhandled (passthrough) nodes,
though in practice this is rare. If the user invokes a leaf that has a
handler, the handler runs. If they invoke a path that ends at an interior
node, the framework prints that node's group help (sub-commands list).

## Resolution Algorithm

Given a parsed token list (after redirection / pipe splitting), the
framework walks the tree to find the executing node:

```
node = root
i = 0
while i < len(tokens):
    tok = tokens[i]
    if tok.startswith("-"):
        # Skip flag (and value, if value-taking at any reachable node)
        i += 2 if _is_value_taking_flag(node, tok) else 1
    elif tok in node.children:
        node = node.children[tok]
        i += 1
    else:
        break  # remaining tokens are positional args of `node`
remaining = tokens[i:]
```

Flags consume tokens during the walk so they can appear anywhere — before,
between, or after sub-command names. The remaining tokens are the
positional arguments of the resolved node and are parsed by that node's
argparse.

`_is_value_taking_flag(node, flag)` checks the current node and all
ancestors, since ancestor flags are always valid at deeper nodes.

### Execution

After resolution:

* If `node.handler` is set: parse `remaining` with `node`'s argparse, merge
  with parsed flag values from the walk (including inherited flags from
  ancestors), and call `node.handler(**kwargs)`.
* If `node.handler` is `None` and `node` has children: print group help and
  the list of sub-commands.
* If the entire tree has no handlers: pass the original (unmodified) token
  list to the external binary via the existing PTY path.

### Inherited Kwargs

When a leaf runs, parsed values for ancestor flags are passed as kwargs.
The convention is **lowest-defined-name wins**: a flag defined at the root
appears once in the leaf's signature; if the leaf re-declares the same
flag, the leaf's value takes precedence.

Leaf handlers declare ancestor flags they care about as ordinary keyword
parameters with defaults:

```python
def s3_ls(path=None, recursive=False, region=None, profile=None): ...
```

Handlers ignoring an inherited flag simply omit it from their signature;
the framework filters kwargs to those the handler accepts.

## Completion Behavior

### Walking the Tree for Completion

`_get_base_completions()` builds a `CompletionContext` and walks the tree the
same way as resolution, except:

* The walk stops at the **last fully-typed token**; the partial prefix
  (`ctx.prefix`) is not consumed.
* The "current node" after the walk determines what to offer:

  | What's being typed                               | Source of completions                          |
  |--------------------------------------------------|------------------------------------------------|
  | A `-`-prefixed token                             | All flags from current node + ancestors        |
  | A non-flag token, current node has children      | Sub-command names of current node              |
  | A non-flag token, current node has no children   | Positional completer at the right index of current node |
  | The value of a value-taking flag (last arg = flag) | Flag's value completer (if any)                |

### Inherited Flags

When listing flag completions, the framework collects flags from the
current node and walks up `parent` pointers, merging dictionaries. A flag
defined at the root is always offered, no matter how deep the user is.

A descendant's flag is **only** offered after its defining node is reached
in the walk. So `awsut sagemaker hyperpod --<TAB>` does not offer
`--show-nodes` (defined at `awsut sagemaker hyperpod cluster describe`);
the user must type
`cluster describe` first. This matches real CLI semantics — placing a
deep flag before its defining sub-command would fail at runtime — and
avoids dumping every flag from every leaf into the root's completion menu.

### Strict vs Permissive

Strict (the chosen default) is described above. A permissive mode
(aggregate descendants' flags at interior nodes) is **not** implemented in
the initial version; if needed later it can be added as an opt-in flag on
the root node.

## Compatibility With Flat Commands

Flat Python commands using `params=` continue to work unchanged:

```python
@registry.command(
    name="connect",
    params=[
        arg("environment", choices=["prod", "staging"]),
        arg("region", completer=RegionCompleter()),
        arg("--verbose", action="store_true"),
    ],
)
def connect(environment, region, verbose=False): ...
```

A flat command is just a tree with a single leaf node — the existing API
is the special case where the root **is** the leaf. Internally the
registry stores them as `Command` nodes too, so there is no separate code
path.

## Interaction With Existing Subsystems

* **Completion engine (`completion.py`)** — gains a tree-aware dispatch
  layer. The existing `Completer` ABC, `CompletionContext`,
  `OptionsCompleter`, etc. are reused unchanged for leaf-level completion;
  the tree walk simply selects which `OptionsCompleter` / positional
  completer to consult.
* **Command registry (`commands.py`)** — `Command` becomes a tree node.
  `_build_completers()` is generalized to merge ancestor flags into a node's
  effective options dict. The flat path remains a degenerate case.
* **Shell dispatch (`shell.py`)** — `_get_base_completions()` and the command
  invocation path call the resolution algorithm above. The
  `_positional_index()` helper is replaced by the walk.
* **External recipes (`recipes/*.py`)** — migrated incrementally. Each
  recipe's `register()` becomes a series of `.command()` calls instead of
  hand-rolled `OptionsCompleter` + dispatcher classes.

## Migration of Existing Recipes

`recipes/git.py` and `recipes/awsut.py` are the recipes that hand-roll
sub-command dispatch using the tree API. (`recipes/aws.py` no longer
defines its own subcommand tree — it now drives the AWS CLI v2
`aws_completer` binary directly, which knows every service, operation,
and flag.)

Other recipes (`ls`, `du`, `tail`, `kill`, `find`, `grep`, `make`, `ssh`,
`df`, …) are flat and stay on the simple form: a single
`registry.command(name, params=[...])` call with no handler attached.
Cobra-based tools like `docker`, `kubectl`, and `helm` don't need
recipes at all — `CobraCompleter` handles them automatically.

## What Is Out of Scope

* **Auto-discovery** of sub-commands from filesystem layout.
* **Permissive flag aggregation** at interior nodes.
* **Per-leaf custom completer hooks** beyond what `arg(completer=...)`
  already provides.
* **Type-driven dispatch** (e.g. routing by Pydantic model). Handlers
  remain plain Python functions taking parsed kwargs.

## Open Questions

1. **Help formatting at interior nodes.** Should `awsut sagemaker hyperpod`
   print a `git`-style sub-command list, or a `--help` block with usage?
   Initial plan: short list of children with one-line `help=` per child.
2. **Completing the value of an inherited value-taking flag** when the flag
   is typed before the defining node. Already works for ancestor-defined
   flags (root-level `--region` is always known); only matters if a
   value-taking flag is defined deep in the tree, which is uncommon.
3. **Whether `Command.handler` and `Command.children` may coexist.** The
   table above allows it; if no concrete use case appears, we can forbid it
   to simplify semantics.
