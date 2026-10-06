# Cobra-Protocol Completion

## Status

Implemented. See `CobraCompleter` in [src/eosh/completion.py](../src/eosh/completion.py), the opt-in list in [src/eosh/recipes/cobra.py](../src/eosh/recipes/cobra.py), and [tests/test_cobra_completion.py](../tests/test_cobra_completion.py).

## Motivation

Many modern CLIs (kubectl, helm, gh, docker, argocd, …) are built on the [spf13/cobra](https://github.com/spf13/cobra) framework, which exposes a hidden `__complete` subcommand. That is the same entry point cobra's bash/zsh completion scripts call. Driving it directly skips bash, returns richer data than bash-completion (a description per candidate, live resources like pods, containers and PRs), and needs nothing installed beyond the tool itself.

## Opt-in, never detected

A command is driven in completion mode **only after it has been named**. eosh doesn't try to work out whether a command speaks the protocol.

Detecting it would mean running the command. An earlier version did that, probing every PATH command with `<cmd> __complete --help` on its first TAB. A tool that doesn't know `__complete` takes it as an ordinary argument:

- `touch foo<TAB>` / `mkdir foo<TAB>` could create entries named `__complete` and `--help`. BSD getopt stops at the first non-option.
- `./deploy.sh <TAB>` ran the user's script, because `which` resolves it.
- An editor or pager could take over the tty until the timeout.

So the list of cobra commands is explicit:

```python
# ~/.eosh/config.py
from eosh.recipes import enable, enable_cobra

enable("cobra")             # well-known tools (kubectl, helm, gh, docker, …)
enable_cobra("mytool")      # any other cobra-based CLI you use
```

`enable("*")` includes the `cobra` recipe. A user recipe can call `enable_cobra(...)` from its own `register()`.

`enable_cobra(*names)` registers each name as a completion-only recipe whose `delegate` is a shared `CobraCompleter`, so every slot (flags and positionals alike) is answered by the tool. Two rules match the other recipes:

- A name that isn't on `PATH` is skipped. That makes the built-in list free on hosts without those tools. A tool installed mid-session needs a `reload`.
- A name that is already registered is left alone, so a hand-written recipe always wins over the generic protocol.

## How cobra completion works

`<cmd> __complete <words> <prefix>` prints one candidate per line on stdout, optionally with a tab-separated description. A directive line ends the output:

```
$ kubectl __complete get po ""
pod         retrieve pods
pods        (alias)
poddisruptionbudget
poddisruptionbudgets
:4
Completion ended with directive: ShellCompDirectiveNoFileComp
```

`_parse_cobra_output` returns `(candidates, directive)`. The trace line is dropped.

### Directives honoured

| Bit | Name | eosh behaviour |
|-----|------|----------------|
| 1 | `Error` | no candidates |
| 4 | `NoFileComp` | an empty answer stays empty |
| 8 | `FilterFileExt` | the candidates are extensions; offers files with those extensions, plus directories |
| 16 | `FilterDirs` | offers directories only |
| — | (none of the above, no candidates) | file completion, e.g. `kubectl apply -f <TAB>` |

Bits 2 (`NoSpace`) and 32 (`KeepOrder`) are not wired to the line editor yet.

The command is a registered recipe, so the shell's own "no completer registered → files" fallback doesn't apply. File completion for cobra tools comes only from the directive.

## Caching and safety

- Results go through `completion_cache` keyed on `(cwd, command, args, prefix)`. Repeated keystrokes in an open picker reuse them, and the shell clears the cache after every command.
- The child gets `stdin=subprocess.DEVNULL`, and runs are capped by `timeout` (default 1.5 s). A non-zero exit, a timeout or an `OSError` all read as "no candidates".

## Why cobra and not bash-completion

bash-completion would add a system dependency: the bash-completion package, plus bash 4+ on macOS. Cobra needs only the tool, returns descriptions, and has a stable protocol. The rest of the long tail is covered by:

- the `aws` recipe, which drives `aws_completer` (see [recipes/aws.py](../src/eosh/recipes/aws.py))
- [argcomplete-fallback.md](argcomplete-fallback.md) for Python CLIs. That one *can* be auto-detected safely, by reading the script for a marker without running it.
- eosh's own recipes (`git`, `ssh`, `ls`, `make`, …)

## Future work

- **`NoSpace` / `KeepOrder` directives.** These need a `Completion` field the line editor honours.
