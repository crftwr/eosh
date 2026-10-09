# eosh user configuration
# Define custom commands and completers here.
#
# `config edit` opens this file in $VISUAL / $EDITOR and reloads it when you
# quit; `reload` re-runs it from a clean slate.  Built-in names (cd, help,
# @watch, …) need `override=True` to be replaced:
#     @command_registry.command("cd", override=True)

# ── Simple example: one positional argument ───────────────────────────────────

from eosh.commands import registry as command_registry, arg
from eosh.completion import ChoiceCompleter

@command_registry.command(
    name="hello",
    help="Greet someone by name.",        # shell-facing description; no docstring needed
    params=[arg("name", nargs="?", default="world", help="name of the person to greet",
                completer=ChoiceCompleter(["world", "there"]))],
)
def hello(name):
    print(f"Hello, {name}!")


# ── Multi-level sub-command example ───────────────────────────────────────────
#
# Build a command tree by:
# 1. Calling `command_registry.command(name, ...)` — returns a `Command` node.
# 2. Chaining `.command(...)` on the node for each child group.
# 3. Using `@parent.command(name, ...)` as a decorator to attach a handler.
#
# A node's flags are its own — nothing is inherited down the tree.  A flag
# several sub-commands share is declared once and listed on each, and every
# handler receives exactly the params it declares.

# Shared flags: declared once, listed on each sub-command that takes them.
VERBOSE = arg("-v", "--verbose", action="store_true",
              help="print details for each step")
DRY_RUN = arg("-n", "--dry-run", action="store_true",
              help="show steps, skip execution")

deploy = command_registry.command("deploy", help="Deploy services to environments.")


@deploy.command(
    "app",
    help="Deploy a service to an environment.",
    params=[
        VERBOSE, DRY_RUN,
        # choices= drives argparse validation AND TAB completion simultaneously.
        arg("environment", choices=["prod", "staging", "dev"],
                           help="target environment"),
        arg("service",     nargs="?", default="all",
                           choices=["api", "web", "worker"],
                           help="service to deploy, or 'all'"),
        # Value-taking flags: completer= drives TAB completion for the value.
        arg("-t", "--timeout", type=int, default=60, metavar="SECONDS",
                               help="deployment timeout in seconds",
                               completer=ChoiceCompleter(["30", "60", "120", "300"])),
        arg("-b", "--branch",  default="main",       metavar="BRANCH",
                               help="git branch to deploy"),
    ],
)
def deploy_app(environment, service, dry_run, verbose, timeout, branch):
    import sys, time
    # While this runs, press Ctrl+] to switch context without killing the deploy.
    prefix = "[DRY RUN] " if dry_run else ""
    print(f"{prefix}Deploying '{service}' to '{environment}'  "
          f"branch={branch!r}  timeout={timeout}s")
    for step, secs in [("Build image",       2),
                       ("Push to registry",   3),
                       ("Update deployment",  2),
                       ("Wait for rollout",   4),
                       ("Health checks",      2)]:
        if verbose:
            print(f"  -> {step} ...", flush=True)
        if not dry_run:
            # Short sleeps with a flush touch sys.stdout each tick.  In a
            # pipeline, Ctrl-C closes the stage's stdout; the next flush()
            # then raises promptly instead of grinding through the rest of
            # the sleeps.  Standalone, flush() is just a no-op cost.
            for _ in range(secs * 10):
                time.sleep(0.1)
                sys.stdout.flush()
        print(f"  ok {step}")
    print(f"{prefix}Done.")


@deploy.command(
    "rollback",
    help="Roll back a deployment to the previous revision.",
    params=[
        VERBOSE, DRY_RUN,
        arg("environment", choices=["prod", "staging", "dev"],
                           help="target environment"),
        arg("service",     nargs="?", default="all",
                           choices=["api", "web", "worker"],
                           help="service to roll back, or 'all'"),
    ],
)
def deploy_rollback(environment, service, dry_run, verbose):
    prefix = "[DRY RUN] " if dry_run else ""
    print(f"{prefix}Rolling back '{service}' in '{environment}'")
    if verbose:
        print("  -> fetching previous revision ...")
        print("  -> swapping pointer ...")
    print(f"{prefix}Done.")


@deploy.command(
    "status",
    help="Show deployment status for an environment.",
    params=[
        VERBOSE,
        arg("environment", choices=["prod", "staging", "dev"],
                           help="environment to inspect"),
    ],
)
def deploy_status(environment, verbose):
    print(f"Status for '{environment}':")
    for name, ver in [("api", "v1.4.2"), ("web", "v1.4.2"), ("worker", "v1.4.1")]:
        print(f"  {name:8} {ver}")
        if verbose:
            print(f"    deployed: 2026-05-26 14:32 UTC")


# ── Filter command (reads stdin, writes stdout) ───────────────────────────────
#
# Python @registry.command handlers are first-class pipeline stages: they can
# sit anywhere in a pipe (`producer | py_cmd | consumer`).  Each stage runs
# on its own worker thread with `sys.stdin` / `sys.stdout` rebound to the
# pipe ends, so `print()` and iteration over `sys.stdin` "just work."
#
# Example: `cols` prints the requested whitespace-separated columns from
# each line — like `awk '{print $2, $5}'` but expressed in Python.
#
#     ls -la | cols 9 5             # filename and size
#     ps -A | cols 1 4              # PID and command
#     printf 'a b c\nd e f\n' | cols 1 3 | grep d
#
# Caveats inherent to the in-process pipeline model — see
# doc/limitations.md for the full list:
#
#   * If the consumer closes the pipe early (`cmd | head -1`), this stage
#     gets a BrokenPipeError on its next print(); catching it is the
#     well-behaved pattern for an unbounded producer.
#   * Calling subprocess.run() from inside a piped Python command writes
#     to the terminal, not the pipe, unless you pass stdout=sys.stdout.

import sys

@command_registry.command(
    name="cols",
    help="Print whitespace-separated columns by 1-based index from each stdin line.",
    params=[
        arg("indices", nargs="+", type=int, metavar="N",
                       help="1-based column number(s) to print"),
        arg("-d", "--delim", default=" ", metavar="STR",
                             help="output delimiter (default: single space)"),
    ],
)
def cols(indices, delim):
    try:
        for line in sys.stdin:
            fields = line.split()
            picked = [fields[i - 1] for i in indices if 0 < i <= len(fields)]
            print(delim.join(picked))
    except BrokenPipeError:
        pass  # downstream closed early (e.g. `... | head -1`) — normal


# ── Enable completion recipes for external commands ───────────────────────────

from eosh.recipes import enable
enable("*")


# ── Aliases ───────────────────────────────────────────────────────────────────
#
# Aliases expand the first token of a command line (bash-style):
#
#     hp create ...   →  awsut sagemaker hyperpod create ...
#     la /tmp         →  ls -la /tmp
#
# They participate in TAB completion: typing the alias name and pressing TAB
# completes the rest as if the expansion had been typed.

command_registry.alias("hp", "awsut sagemaker hyperpod")
command_registry.alias("la", "ls -la")


# ── Python-backed variables ───────────────────────────────────────────────────
#
# Register variables to give `var NAME=VALUE` a logical name, a description
# and TAB completion for the value side.  An EnvVar names one or more
# os.environ keys; the shell writes them all, and saves/restores them per
# context like any variable set with `var`.  (For a value kept on the Python
# side instead — out of child processes' environment — subclass
# eosh.variables.PyVar, per-context, or GlobalVar, process-global.)
#
# At the prompt:
#
#     eosh> var editor=<TAB>           → vim, emacs, nano, code
#     eosh> var editor=vim
#     eosh> var http_proxy=http://...  → sets HTTP_PROXY *and* HTTPS_PROXY

from eosh.variables import registry as var_registry, EnvVar

# One logical name → one env var, with completion.
var_registry.register(EnvVar(
    "editor", keys="EDITOR",
    completer=ChoiceCompleter(["vim", "emacs", "nano", "code"]),
    description="Default text editor",
))

# One logical name → two env keys.
var_registry.register(EnvVar(
    "http_proxy", keys=["HTTP_PROXY", "HTTPS_PROXY"],
    description="HTTP/HTTPS proxy — sets HTTP_PROXY + HTTPS_PROXY",
))


# ── Desktop notifications for long-running commands ───────────────────────────
#
# When a command runs longer than the threshold, eosh posts an OS
# notification as it finishes — you've probably switched to a browser by then.
# Backends are whatever the platform already ships (osascript / notify-send /
# a PowerShell toast), so there's nothing to install.
#
# At the prompt:
#
#     eosh> var notify=off              → disable for this session
#     eosh> var notify_threshold=30     → only notify for commands ≥ 30s
#
# Both are GlobalVars — process-global: unlike the EnvVars above, they are
# not saved/restored on context switch.

from eosh import notify

# Default is 10 seconds; raise it if your day is full of 15-second builds.
notify.configure(threshold=10)

# Commands whose long runtime means "I sat in it", not "work finished".
# The defaults already cover editors, pagers, top, ssh, tmux and sub-shells;
# add your own interactive tools here.
notify.SKIP_COMMANDS.update({"psql", "mysql", "sqlite3", "ipython"})

# Replace the platform backend entirely — post to Slack, ntfy.sh, tmux, ...
# Runs on a daemon thread and must not write to the terminal.
#
# def my_notifier(title, message):
#     import urllib.request
#     urllib.request.urlopen(
#         urllib.request.Request(
#             "https://ntfy.sh/my-private-topic",
#             data=f"{title}\n{message}".encode(),
#         ),
#         timeout=5,
#     )
#
# notify.set_notifier(my_notifier)


# ── Event hooks ──────────────────────────────────────────────────────────────
#
# Run your own function when something happens: on_startup, on_exit,
# on_directory_changed(old, new), on_context_switched(old, new),
# on_command_starting(line), on_command_finished(line, status, elapsed),
# on_command_not_found(argv) -> bool.
#
# import os
# from eosh import hooks
#
# @hooks.on_context_switched
# def terminal_title(old, new):
#     print(f"\x1b]2;[{new}] eosh\x07", end="", flush=True)
#
# @hooks.on_command_not_found
# def auto_cd(argv):
#     if len(argv) == 1 and os.path.isdir(argv[0]):
#         os.chdir(argv[0])
#         return True
#     return False


# ── Customize the prompt ──────────────────────────────────────────────────────

import os
from datetime import datetime
from eosh import set_prompt

def my_prompt(context_manager):
    """Replicates the built-in default prompt: [context] parent/cwd HH:MM:SS [bg:N]>."""
    CYAN_BOLD = "\033[1m\033[38;2;0;188;212m"
    BLUE_BOLD = "\033[1m\033[38;2;100;149;237m"
    GREEN = "\033[38;2;80;200;100m"
    YELLOW = "\033[38;2;229;192;123m"
    RESET = "\033[0m"

    parts = []

    ctx = context_manager.current()
    if ctx and ctx.name != "default":
        parts.append(f"{CYAN_BOLD}[{ctx.name}]{RESET}")

    cwd = os.getcwd()
    home = os.path.expanduser("~")
    if cwd == home:
        short_path = "~"
    elif cwd.startswith(home + os.sep):
        rel = cwd[len(home) + 1:]
        rel_parts = rel.split(os.sep)
        if len(rel_parts) <= 2:
            short_path = "~/" + rel
        else:
            short_path = os.sep.join(rel_parts[-2:])
    else:
        abs_parts = cwd.lstrip(os.sep).split(os.sep)
        if len(abs_parts) <= 2:
            short_path = "/" + os.sep.join(abs_parts)
        else:
            short_path = os.sep.join(abs_parts[-2:])

    timestamp = datetime.now().strftime("%H:%M:%S")
    parts.append(f"{BLUE_BOLD}{short_path}{RESET}")
    parts.append(f"{GREEN}{timestamp}{RESET}")

    bg_count = 0
    current_name = context_manager.current_name
    for name, c in context_manager.contexts.items():
        if name != current_name and c.process_slot and c.process_slot.is_alive():
            bg_count += 1
    if bg_count:
        parts.append(f"{YELLOW}[bg:{bg_count}]{RESET}")

    return " ".join(parts) + "> "

set_prompt(my_prompt)


# ── Color scheme ──────────────────────────────────────────────────────────────
#
# Choose a built-in scheme or define a fully custom one.
# Applies to TUI widgets — inline pickers and @watch's framing chrome
# (header / footer / scrollbar).  Prompt colors are deliberately not part
# of the scheme: customize the prompt by passing a function to set_prompt().
# Built-in schemes: "dark" (default), "light".
#
# Uncomment one of the examples below:

from eosh import set_color_scheme, ColorScheme

# Built-in schemes (for light- or dark-background terminals):
# set_color_scheme("dark")   # default
# set_color_scheme("light")

# Fully custom scheme — specify any subset of colors as (R, G, B) tuples:
# set_color_scheme(ColorScheme(
#     picker_row_bg=(50, 50, 60),          # non-selected picker row background
#     picker_row_fg=(220, 220, 220),       # non-selected picker row foreground
#     picker_sel_bg=(80, 40, 160),         # selected row background
#     picker_sel_fg=(255, 255, 255),       # selected row foreground
#     scroll_thumb=(120, 120, 120),        # scrollbar thumb (picker)
#     scroll_track=(40, 40, 50),           # scrollbar track (picker)
#     statusbar_bg=(30, 30, 30),           # picker bottom bar bg
#     statusbar_fg=(200, 200, 200),        # picker bottom bar fg
# ))
