"""Completion recipe for terraform.

Terraform uses HashiCorp's mitchellh/cli + posener/complete framework, not
cobra, and its bash completion is just a thin ``COMP_LINE``/``COMP_POINT``
shim — neither is picked up by eosh's cobra or argcomplete fallbacks.
This recipe models the surface area as a static subcommand tree (mirroring
``git.py``), with dynamic completion for workspaces and ``.tfvars`` files.
"""

from __future__ import annotations

import shutil
import subprocess

from ..commands import registry as command_registry, arg
from ..completion import (
    Completer,
    Completion,
    CompletionContext,
    DirCompleter,
    FileCompleter,
)


def _run_terraform(args: list[str], timeout: float = 2.0) -> list[str]:
    try:
        result = subprocess.run(
            ["terraform"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    except (OSError, subprocess.TimeoutExpired):
        return []


# ─── Dynamic completers ─────────────────────────────────────────────────────

class TerraformWorkspaceCompleter(Completer):
    """Lists existing workspaces via ``terraform workspace list``."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        names: list[str] = []
        for line in _run_terraform(["workspace", "list"]):
            # `terraform workspace list` prefixes the active workspace with
            # "* "; strip that and any leading whitespace.
            if line.startswith("*"):
                line = line[1:].strip()
            if line:
                names.append(line)
        return [
            Completion(value=n, description="workspace")
            for n in names if n.startswith(ctx.prefix)
        ]


class _SuffixFileCompleter(Completer):
    """File completer that surfaces files with one of the given suffixes first."""

    def __init__(self, suffixes: tuple[str, ...]) -> None:
        self._suffixes = tuple(s.lower() for s in suffixes)
        self._inner = FileCompleter()

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        all_files = self._inner.complete(ctx)
        preferred: list[Completion] = []
        others: list[Completion] = []
        for c in all_files:
            if c.value.endswith("/") or c.value.lower().endswith(self._suffixes):
                preferred.append(c)
            else:
                others.append(c)
        return preferred + others


def _tfvars_completer() -> Completer:
    return _SuffixFileCompleter((".tfvars", ".tfvars.json"))


def _plan_file_completer() -> Completer:
    # Plan files have no canonical suffix — convention is `tfplan` / `*.tfplan`.
    return _SuffixFileCompleter((".tfplan",))


# ─── Tree definition ─────────────────────────────────────────────────────────

def register() -> None:
    if shutil.which("terraform") is None:
        return

    tf = command_registry.command("terraform", help="infrastructure as code")

    # ── init ──
    tf.command(
        "init", help="prepare working directory",
        params=[
            arg("-backend-config", metavar="PATH_OR_KV",
                help="backend config file or KEY=VALUE",
                completer=_SuffixFileCompleter((".tfvars", ".hcl"))),
            arg("-reconfigure", action="store_true", help="reconfigure backend"),
            arg("-migrate-state", action="store_true", help="migrate state to new backend"),
            arg("-upgrade", action="store_true", help="upgrade modules and providers"),
            arg("-get", metavar="BOOL", help="download modules (true/false)"),
            arg("-input", metavar="BOOL", help="prompt for input (true/false)"),
            arg("-no-color", action="store_true", help="disable color output"),
        ],
    )

    # ── plan / apply / destroy share most flags ──
    plan_apply_flags = [
        arg("-destroy", action="store_true", help="planning mode: destroy"),
        arg("-refresh-only", action="store_true", help="planning mode: refresh only"),
        arg("-refresh", metavar="BOOL", help="refresh remote state (true/false)"),
        arg("-replace", metavar="ADDR", help="force replacement of resource"),
        arg("-target", metavar="ADDR", help="limit to a resource address"),
        arg("-var", metavar="KEY=VALUE", help="set an input variable"),
        arg("-var-file", metavar="FILE", help="load variables from file",
            completer=_tfvars_completer()),
        arg("-compact-warnings", action="store_true", help="compact warnings"),
        arg("-detailed-exitcode", action="store_true", help="detailed exit codes"),
        arg("-input", metavar="BOOL", help="prompt for input (true/false)"),
        arg("-lock", metavar="BOOL", help="hold state lock (true/false)"),
        arg("-lock-timeout", metavar="DURATION", help="lock retry duration"),
        arg("-no-color", action="store_true", help="disable color output"),
        arg("-parallelism", metavar="N", help="resource ops in parallel"),
    ]

    tf.command(
        "plan", help="show changes the configuration would make",
        params=plan_apply_flags + [
            arg("-out", metavar="PATH", help="write plan file to PATH",
                completer=FileCompleter()),
            arg("-generate-config-out", metavar="PATH",
                help="write generated HCL for imports", completer=FileCompleter()),
        ],
    )

    tf.command(
        "apply", help="create or update infrastructure",
        params=plan_apply_flags + [
            arg("plan", nargs="?", completer=_plan_file_completer()),
            arg("-auto-approve", action="store_true", help="skip approval prompt"),
            arg("-backup", metavar="PATH", help="state backup path",
                completer=FileCompleter()),
        ],
    )

    tf.command(
        "destroy", help="destroy previously-created infrastructure",
        params=plan_apply_flags + [
            arg("-auto-approve", action="store_true", help="skip approval prompt"),
        ],
    )

    # ── validate / fmt ──
    tf.command(
        "validate", help="check whether configuration is valid",
        params=[
            arg("-json", action="store_true", help="machine-readable output"),
            arg("-no-color", action="store_true", help="disable color output"),
            arg("-no-tests", action="store_true", help="skip test files"),
        ],
    )

    tf.command(
        "fmt", help="reformat configuration in standard style",
        params=[
            arg("path", nargs="?", completer=DirCompleter()),
            arg("-list", metavar="BOOL", help="list files whose formatting differs"),
            arg("-write", metavar="BOOL", help="overwrite source files"),
            arg("-diff", action="store_true", help="display diffs of changes"),
            arg("-check", action="store_true",
                help="exit non-zero if any files need formatting"),
            arg("-recursive", action="store_true", help="process subdirectories"),
        ],
    )

    # ── refresh / get / graph / show / output ──
    tf.command(
        "refresh", help="update state to match remote systems",
        params=[
            arg("-target", metavar="ADDR", help="limit to a resource address"),
            arg("-var", metavar="KEY=VALUE", help="set an input variable"),
            arg("-var-file", metavar="FILE", help="load variables from file",
                completer=_tfvars_completer()),
            arg("-no-color", action="store_true", help="disable color output"),
        ],
    )

    tf.command(
        "get", help="install or upgrade remote modules",
        params=[
            arg("-update", action="store_true", help="check for module updates"),
            arg("-no-color", action="store_true", help="disable color output"),
        ],
    )

    tf.command(
        "graph", help="generate Graphviz graph",
        params=[
            arg("-type", metavar="TYPE", help="graph type"),
            arg("-plan", metavar="PATH", help="render an existing plan file",
                completer=_plan_file_completer()),
            arg("-draw-cycles", action="store_true", help="highlight cycles"),
        ],
    )

    tf.command(
        "show", help="show current state or a saved plan",
        params=[
            arg("path", nargs="?", completer=_plan_file_completer()),
            arg("-json", action="store_true", help="machine-readable output"),
            arg("-no-color", action="store_true", help="disable color output"),
        ],
    )

    tf.command(
        "output", help="show output values from root module",
        params=[
            arg("name", nargs="?"),
            arg("-json", action="store_true", help="machine-readable output"),
            arg("-raw", action="store_true", help="raw string output"),
            arg("-no-color", action="store_true", help="disable color output"),
        ],
    )

    # ── import / taint / untaint ──
    tf.command(
        "import", help="associate existing infrastructure with a resource",
        params=[
            arg("address"),
            arg("id", nargs="?"),
            arg("-var", metavar="KEY=VALUE", help="set an input variable"),
            arg("-var-file", metavar="FILE", help="load variables from file",
                completer=_tfvars_completer()),
            arg("-allow-missing-config", action="store_true",
                help="allow import without configuration"),
            arg("-no-color", action="store_true", help="disable color output"),
        ],
    )

    tf.command(
        "taint", help="mark a resource instance as not fully functional",
        params=[
            arg("address"),
            arg("-allow-missing", action="store_true",
                help="don't error if resource is missing"),
        ],
    )

    tf.command(
        "untaint", help="remove the tainted state from a resource instance",
        params=[
            arg("address"),
            arg("-allow-missing", action="store_true",
                help="don't error if resource is missing"),
        ],
    )

    # ── workspace (nested) ──
    workspace = tf.command("workspace", help="manage workspaces")
    workspace.command("list",   help="list workspaces")
    workspace.command("show",   help="show the current workspace")
    workspace.command(
        "new", help="create a new workspace",
        params=[arg("name")],
    )
    workspace.command(
        "select", help="select a workspace",
        params=[arg("name", completer=TerraformWorkspaceCompleter())],
    )
    workspace.command(
        "delete", help="delete a workspace",
        params=[
            arg("name", completer=TerraformWorkspaceCompleter()),
            arg("-force", action="store_true", help="force delete"),
        ],
    )

    # ── state (nested) ──
    state = tf.command("state", help="advanced state management")
    state.command(
        "list", help="list resources in the state",
        params=[arg("address", nargs="*")],
    )
    state.command(
        "show", help="show a resource in the state",
        params=[arg("address")],
    )
    state.command(
        "mv", help="move an item in the state",
        params=[arg("source"), arg("destination")],
    )
    state.command(
        "rm", help="remove instances from the state",
        params=[arg("address", nargs="+")],
    )
    state.command(
        "pull", help="pull current state and output to stdout",
    )
    state.command(
        "push", help="update remote state from a local state file",
        params=[arg("path", completer=FileCompleter())],
    )
    state.command(
        "replace-provider", help="replace provider in the state",
        params=[arg("from_provider"), arg("to_provider")],
    )
    state.command(
        "identities", help="list resource identities in the state",
    )

    # ── providers (nested) ──
    providers = tf.command("providers", help="show required providers")
    providers.command(
        "lock", help="write provider dependency lock entries",
    )
    providers.command(
        "mirror", help="mirror provider plugins to a directory",
        params=[arg("target_dir", completer=DirCompleter())],
    )
    providers.command(
        "schema", help="show schemas for the configuration's providers",
        params=[arg("-json", action="store_true", help="machine-readable output")],
    )

    # ── login / logout ──
    tf.command(
        "login", help="obtain and save credentials for a remote host",
        params=[arg("hostname", nargs="?")],
    )
    tf.command(
        "logout", help="remove locally-stored credentials for a remote host",
        params=[arg("hostname", nargs="?")],
    )

    # ── force-unlock / console / test / query / modules / metadata / version ──
    tf.command(
        "force-unlock", help="release a stuck lock on the current workspace",
        params=[
            arg("lock_id"),
            arg("-force", action="store_true", help="don't ask for confirmation"),
        ],
    )

    tf.command(
        "console", help="interactive expression prompt",
        params=[
            arg("-var", metavar="KEY=VALUE", help="set an input variable"),
            arg("-var-file", metavar="FILE", help="load variables from file",
                completer=_tfvars_completer()),
        ],
    )

    tf.command(
        "test", help="execute integration tests for modules",
        params=[
            arg("-filter", metavar="PATH", help="run only the given test files",
                completer=FileCompleter()),
            arg("-test-directory", metavar="DIR", help="test files directory",
                completer=DirCompleter()),
            arg("-verbose", action="store_true", help="verbose output"),
            arg("-json", action="store_true", help="machine-readable output"),
        ],
    )

    # ── flat sub-commands without dynamic completion ──
    for sub, desc in [
        ("modules", "show declared modules in a working directory"),
        ("metadata", "metadata related commands"),
        ("query", "search and list remote infrastructure"),
        ("stacks", "manage HCP Terraform stack operations"),
        ("version", "show the current Terraform version"),
    ]:
        tf.command(sub, help=desc)
