# argcomplete Protocol Completion Fallback

## Status

Implemented. See [src/eosh/completion.py](../src/eosh/completion.py) (`ArgcompleteCompleter`) and [tests/test_argcomplete_fallback.py](../tests/test_argcomplete_fallback.py).

## Motivation

[argcomplete](https://kislyuk.github.io/argcomplete/) is the de-facto completion library for Python CLIs that use `argparse`. Tools that opt in include:

- `pipx`, `conda`, `pre-commit`, `tox`, `pdm`, `httpie`, `nox`, `virtualenv`, …
- Many internal Amazon Python tools that wire `argcomplete.autocomplete(parser)` into their entry point.

`ArgcompleteCompleter` drives the protocol directly so eosh can complete these tools without any per-command recipe and without depending on the bash-completion package.

Combined with [cobra-fallback.md](cobra-fallback.md) (Go CLIs) and [`recipes/aws.py`](../src/eosh/recipes/aws.py) (AWS CLI v2's `aws_completer`), three protocol fallbacks cover the vast majority of modern CLI tools out of the box.

## Goals

- Provide TAB completions for any argcomplete-marked tool with zero per-tool config.
- Activate **only when no recipe / `@registry.command` completer produced candidates** — recipes always win.
- **Never invoke a tool that isn't argcomplete-marked.** Detection happens by inspecting the executable, not by trial-running it. (Some tools have dangerous side effects when run.)
- Bound the cost: a TAB press should never block the prompt for more than a small, configurable timeout.
- Fail silently when the tool isn't installed or isn't argcomplete-aware — fall back to `FileCompleter`.

## Non-goals

- Honoring argcomplete's per-completion display strings or "no-space" directives. argcomplete supports them via a separate, less stable wire format; we surface plain values only.
- Caching across shell restarts. In-memory only.

## How argcomplete works

A Python script registers completion via:

```python
#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
import argparse, argcomplete
parser = argparse.ArgumentParser()
# ... add arguments ...
argcomplete.autocomplete(parser)
parser.parse_args()
```

The `# PYTHON_ARGCOMPLETE_OK` comment is the marker that completion-driving code looks for. When `argcomplete.autocomplete()` runs and sees `_ARGCOMPLETE=1` in the environment, it computes candidates from the parser, writes them to **fd 8** joined by the value of `_ARGCOMPLETE_IFS` (default `\v`), and exits without running the user's code.

### Protocol summary

| Variable / fd | Meaning |
|----------|---------|
| `_ARGCOMPLETE=1` | enable completion mode (value is also a 1-based "leading words to skip" count; we always pass `1`) |
| `_ARGCOMPLETE_IFS` | candidate separator (default `\v`) |
| `_ARGCOMPLETE_SHELL` | `bash`/`zsh`/`fish` — affects formatting; we use `bash` |
| `_ARGCOMPLETE_SUPPRESS_SPACE` | `1` to suppress trailing space |
| `COMP_LINE` | the entire command line up to cursor |
| `COMP_POINT` | byte offset of the cursor within `COMP_LINE` |
| fd 8 (output) | candidates joined by `_ARGCOMPLETE_IFS` |
| fd 9 (debug) | argcomplete's debug stream (we discard it) |

## Detection

Detection inspects the executable file (without running it), caching the result per command per shell session. Three cases:

1. **Plain Python script with marker.** The script begins with `#!/usr/bin/env python3` (or similar) and contains `# PYTHON_ARGCOMPLETE_OK` in its first 2 KiB. Cheapest case — no extra subprocess.

2. **Setuptools `console_scripts` shim.** The shim is a small Python file generated at install time:

   ```python
   #!/path/to/python
   import sys
   from MODULE.submodule import FUNC
   if __name__ == '__main__':
       sys.exit(FUNC())
   ```

   The shim itself never has the marker. We parse the `from … import …` line, then run a tiny probe with the shim's *own* Python interpreter to locate the imported module via `importlib.util.find_spec` and read its first 1 KiB for the marker. Using the script's interpreter ensures the right `sys.path` for the venv.

3. **Anything else.** Compiled binaries, shell scripts, missing files — return `False` without invoking anything.

The probe never executes user code; it only parses the shim and reads bytes.

## Invocation

After detection succeeds, completion is driven by:

```python
env = os.environ + {
    "_ARGCOMPLETE": "1",
    "_ARGCOMPLETE_IFS": "\v",
    "_ARGCOMPLETE_SHELL": "bash",
    "_ARGCOMPLETE_SUPPRESS_SPACE": "1",
    "COMP_LINE": <full line>,
    "COMP_POINT": <byte offset of cursor>,
    "COMP_TYPE": "9",  # 9 = TAB
}
# child gets a pipe write end on fd 8
subprocess.Popen([command], env=env, pass_fds=(8,),
                 stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL)
# read candidates from the pipe read end, split on \v
```

The fd-8 plumbing has a subtle constraint: `pass_fds` only works for fds that are open in the parent at the time of `Popen`. We `dup2` the pipe's write end onto parent fd 8, then list `8` in `pass_fds`, then restore parent fd 8 after. This ensures the child inherits fd 8 pointing to our pipe.

## Dispatch chain

```
1. Recipe / @registry.command completer for this position?  → use it
2. None-key OptionsCompleter and prefix starts with "-"?     → use it
3. CobraCompleter (cobra-marked command on PATH)?            → try it
4. ArgcompleteCompleter (argcomplete-marked Python script)?  → try it
   - non-empty result            → use it
   - empty result or unavailable → fall through
5. FileCompleter fallback                                    → use it
```

Cobra runs before argcomplete because its probe is cheaper (single `__complete --help` subprocess) and the two protocols don't overlap on a single tool.

## API

```python
from eosh.completion import (
    enable_argcomplete_fallback,
    disable_argcomplete_fallback,
    get_argcomplete_fallback,
    ArgcompleteCompleter,
)

# Default: enabled. To override the timeout (default 2.0s):
enable_argcomplete_fallback(timeout=5.0)

# To turn it off entirely (e.g. in ~/.eosh/config.py):
disable_argcomplete_fallback()
```

## Cost and tradeoffs

| Aspect | Assessment |
|--------|------------|
| Latency (probe) | First TAB on a command runs a small subprocess (the probe), typically 30–80 ms. Cached per command for the rest of the session. |
| Latency (invoke) | One subprocess per TAB; argcomplete-instrumented Python tools usually return in 50–300 ms. Capped by `timeout` (default 2.0 s). |
| Caching | Per-command for detection; per-line for results. Same-line repeated TABs reuse cached candidates. |
| Correctness | argcomplete parses `COMP_LINE`/`COMP_POINT` itself — quoting/escaping match user expectation. |
| UX gap vs. recipes | Plain string candidates (no description, no multi-select, no arg-hint prompt). Recipes remain the path for richer UX. |
| Failure modes | Tool missing, isn't argcomplete-aware, errors, or times out → empty result, fall through to `FileCompleter`. Never crashes the prompt. Never invokes a non-argcomplete tool blindly. |
| Security | Detection only reads bytes from disk; never executes user code. Invocation only runs tools already marked as argcomplete-aware. |

## Why detection has to be careful

Naively running `<command>` with `_ARGCOMPLETE=1` would be unsafe. Tools that don't read the env var simply ignore it and run normally. So `_ARGCOMPLETE=1 rm -rf /tmp/foo` would actually delete files. That's why detection inspects the file's bytes and only invokes once the marker is confirmed.

## Future work

- **Description support.** argcomplete v2+ optionally returns `name<tab>description` pairs via `_ARGCOMPLETE_DFS`. Plumbing those through to `Completion(description=...)` would match the cobra fallback's UX.
- **Honor `_ARGCOMPLETE_SUPPRESS_SPACE` results.** Currently we always insert a trailing space; some completions (file paths with `/`) want to suppress it.
- **Concurrent probe.** First TAB on a command blocks on the probe subprocess. Could fire it eagerly the first time the user types a known-PATH command.
