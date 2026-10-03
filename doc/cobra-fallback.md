# Cobra-Protocol Completion Fallback

## Status

Implemented. See [src/pitash/completion.py](../src/pitash/completion.py) (`CobraCompleter`) and [tests/test_cobra_completion.py](../tests/test_cobra_completion.py).

## Motivation

pitash provides TAB completion through two paths:

1. Python commands registered with `@registry.command(params=[arg(...)])`.
2. External completer recipes in [src/pitash/recipes/](../src/pitash/recipes/).

Anything outside those falls through to `FileCompleter` only — fine for many cases but a long tail of useful tools (kubectl, helm, gh, argocd, k9s, doctl, linkerd, …) get no smart completions until someone writes a recipe.

A huge fraction of these tools are built on the [spf13/cobra](https://github.com/spf13/cobra) framework, which exposes a hidden `__complete` subcommand. That subcommand is the same thing cobra's bash/zsh completion scripts call internally. Driving it directly skips bash entirely, returns richer data than bash-completion (descriptions per candidate), and works on any host that has the tool installed — **zero install dependency on pitash's side.**

## Goals

- Provide useful TAB completions for any cobra-based command, with zero per-tool config and no system-level dependencies.
- Activate **only when no recipe / @registry.command completer produced candidates** — recipes always win.
- Stay within the existing `Completer` protocol; no changes to `lineedit.py` or `tui.py`.
- Bound the cost: a TAB press should never block the prompt for more than a small, configurable timeout.
- Fail silently when the tool isn't installed, isn't cobra-shaped, or the subprocess errors — fall back to `FileCompleter`.

## Non-goals

- Universal protocol detection. We target cobra only. argcomplete (Python tools) is handled by a sibling fallback — see [argcomplete-fallback.md](argcomplete-fallback.md). Ad-hoc protocols (`npm completion`, etc.) remain out of scope.
- Honoring cobra's directive byte (`:N` trailer — nospace, nofiles, etc.). We strip it for now; hooking directives into `InlinePicker` is a follow-up.
- Caching across shell restarts. In-memory only.

## How cobra completion works

When a cobra-based CLI is invoked as `<cmd> __complete <words> <prefix>`, it prints candidates one per line on stdout, optionally with a tab-separated description, terminated by a directive byte:

```
$ kubectl __complete get po ""
pod         retrieve pods
pods        (alias)
poddisruptionbudget
poddisruptionbudgets
:4
Completion ended with directive: ShellCompDirectiveNoFileComp
```

| Element | Meaning |
|---------|---------|
| `name<TAB>description` | one candidate per line; description optional |
| `:N` | trailing directive byte (4 = nofile, 2 = nospace, …) |
| `Completion ended …` | trace line emitted by some cobra builds |

pitash's parser keeps the candidate lines, drops the directive and trace.

## Detection

Cobra is detected per-command by running:

```sh
<cmd> __complete --help
```

once per command per shell session. The output is checked for either of:

- The phrase `shell completion` (case-insensitive)
- The token `ShellCompDirective`
- A heuristic: exit code 0 + the literal `__complete` mentioned in stdout

If any matches, the command is marked cobra-capable and cached. Subsequent TABs on that command go straight to the invocation path.

Non-cobra tools (e.g. `git`, `ls`) emit a different error and are cached as not-cobra — they never spawn a subprocess on TAB.

## Dispatch chain

```
1. Recipe / @registry.command completer for this position?  → use it
2. None-key OptionsCompleter and prefix starts with "-"?     → use it
3. Cobra fallback enabled and command is on PATH?            → probe + try
   - non-empty result            → use it
   - empty result or unavailable → fall through
4. FileCompleter fallback                                    → use it
```

## API

```python
from pitash.completion import (
    enable_cobra_fallback,
    disable_cobra_fallback,
    get_cobra_fallback,
    CobraCompleter,
)

# Default: enabled. To override the timeout (default 1.5s):
enable_cobra_fallback(timeout=3.0)

# To turn it off entirely (e.g. in ~/.pitash/config.py):
disable_cobra_fallback()
```

## Cost and tradeoffs

| Aspect | Assessment |
|--------|------------|
| Latency (probe) | One `<cmd> __complete --help` per command per session — typically 20–80 ms once. Cached for the rest of the session. |
| Latency (invoke) | One `<cmd> __complete <words>` per TAB press — depends on the tool. kubectl can take 100–500 ms when it queries the API server; gh similar. helm is fast. Capped by `timeout`. |
| Caching | Keyed on the full line. Same-line repeated TABs reuse cached candidates; only the prefix filter changes. |
| Correctness | Cobra parses the words itself — we just pass them through. Quoting/escaping issues are the tool's concern. |
| UX gap vs. recipes | We surface descriptions but not multi-select flag pickers or arg-hint prompts. Recipes remain the path for richer UX. |
| Failure modes | If the tool is missing, isn't cobra, returns non-zero, or times out → empty result, fall through to `FileCompleter`. Never crashes the prompt. |
| Security | We only invoke a tool the user can already invoke as a system command. No new attack surface. |

## Why cobra and not bash-completion

bash-completion was the obvious-looking choice but adds a system dependency (the bash-completion package, plus bash 4+ on macOS). Cobra:

- Needs nothing installed beyond the tool itself.
- Returns descriptions; bash-completion only returns plain words.
- Has a stable, documented protocol — `<cmd> __complete <words>`.
- Covers most of what users actually reach for in 2026 (kubectl, helm, gh, argocd, k3d, k9s, fluxctl, oras, doctl, linkerd, istioctl, hugo, hcloud, op, gitleaks, …).

The remaining long tail is covered by:

- The `aws` recipe drives `aws_completer` (AWS CLI v2's protocol — different from cobra; see [recipes/aws.py](../src/pitash/recipes/aws.py))
- [argcomplete-fallback.md](argcomplete-fallback.md) for Python CLIs (pipx, conda, pre-commit, tox, …)
- pitash's own recipes for shell-only completion (`git`, `ssh`, `ls`, `make`, …)

## Future work

- **Honor cobra directives** — surface `nospace` and `nofiles` to the line editor so the right thing happens when a candidate is selected.
- **Concurrent probe** — first TAB on a command currently blocks while the probe runs. Could fire it eagerly the first time the user types a known-PATH command.
