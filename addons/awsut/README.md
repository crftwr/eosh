# awsut — AWS utility commands (bundled add-on)

`awsut` is a command tree with Python handlers: `whoami`, `console`,
`credentials`, `recent-cost`, `ec2`, `logs`, `cloudformation`,
`sagemaker {jobs,hub,studio,hyperpod}` and
`bedrock-agentcore {harness,memory}`. It ships inside the `eosh`
distribution as `eosh_addons.awsut` and needs the `awsut` extra:

```sh
pip install 'eosh[awsut]'          # or: uv tool install 'eosh[awsut]'
```

```python
# ~/.eosh/config.py
from eosh.recipes import enable
enable("awsut")                    # or enable("*")
```

Settings a config can override live on the `cli` module:

```python
from eosh_addons.awsut import cli as awsut
awsut.console_pages = {"home": "https://...", ...}
```

How add-ons in general are laid out, registered and kept on eosh's public
API is in [doc/addons.md](../../doc/addons.md).

## Layout

| Path | What |
|---|---|
| `cli.py` | The `awsut` root and its own leaves (whoami, console, credentials, ec2, logs, cloudformation, …); `register()` builds the tree and attaches the service groups. |
| `common.py` | The output contract below, plus what is about AWS *shapes* rather than one service: pagination, model introspection, the YAML-ish JSON renderer, the watch heartbeat, flag readers. Builds no AWS client. |
| `sagemaker/` | `awsut sagemaker` — `jobs`, `hub`, `studio`, `hyperpod`; `render.py` holds the SageMaker clients and re-exports `common`. |
| `agentcore/` | `awsut bedrock-agentcore` — `harness`, `memory`; `render.py` holds a client per *plane* (control / data) and the shared status vocabulary. |

Tests are in [tests/addons/awsut/](../../tests/addons/awsut/).

## Output conventions

Every awsut leaf prints to one contract, defined and documented in
[`common.py`](common.py). Read that module's docstring before adding a leaf;
the shape in brief:

| Element | Helper | Rule |
|---|---|---|
| Header line | `print_header(*parts)` | `N thing(s) · <scope> · region <r>`, joined with `·`, then a blank line. Empty parts drop out. It prints **whether or not there are rows** — the header is what says "asked, and there were zero". |
| Table | `print_table(header, rows, note=, colorize=)` | ALL-CAPS labels, a `-` rule, two spaces between columns, **widths from the data** so an identifier is never truncated and every cell can be pasted into the next command. No rows draws no table (the note still prints). |
| Note | `note=` on `print_table` | Only facts *about this output*: rows hidden by a filter, rows cut by `--max`, a query that was refused. **Never a legend and never advice** — a sentence that reads the same on every run belongs in the leaf's `help=` (or the owning flag's), where it costs nothing per listing. |
| Detail view | `print_labeled(pairs)` | Opens with the API's own field names, values unwrapped. `None` in place of a pair is a group separator. |
| Named block | `section(label)` | `--- label ---`, for the repeated blocks a fan-out emits (per node, per stream) and for anything non-tabular after a detail view's identifier block. |
| Failure | `guard` + `SmError` | `error: <message>` on stderr, lowercase, resource quoted. `@guard` goes **directly under** `@node.command(...)` with nothing between them. |
| Timestamp | `fmt_time` / `fmt_dur` / `fmt_bytes` | Local, second precision. A `watch` loop stamps its change lines `[HH:MM:SS]`. |

Three consequences worth stating, because each is easy to get wrong:

- **Don't write a bespoke sentence for the empty case.** `print(f"no spaces in
  domain {d}")` reads fine on its own and reads *inconsistent* next to
  `0 instance(s) · region us-west-2`. Let the header report the count and put
  anything situational in `note=`.
- **`common` builds no AWS clients**, on purpose: it sits above `cli.py` and
  above every per-service subpackage (`sagemaker/`, `agentcore/`) so any of
  them can import it without a cycle. Each subpackage's `render.py` re-exports
  its names, so a module in that subpackage reads every instrument off
  `render`. What stays in a `render.py` is whatever needs a client or knows
  that service's vocabulary.
