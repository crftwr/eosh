"""Completion for cobra-based CLIs (kubectl, helm, gh, docker, …).

Tools built on spf13/cobra answer ``<cmd> __complete <words>`` with their own
candidates — subcommands, flags, and live resources (pods, containers, PRs).
:class:`~eosh.completion.CobraCompleter` drives that protocol; this recipe
decides *which* commands get it.

It has to be a list.  The only way to find out whether a command speaks the
protocol is to run it with ``__complete`` as an argument, and a command that
doesn't takes it literally: ``touch __complete`` makes a file, and
``./deploy.sh __complete`` deploys.  So nothing is probed — a command is
driven in completion mode only after it is named here or in
:func:`enable_cobra`.

    # ~/.eosh/config.py
    from eosh.recipes import enable, enable_cobra
    enable("cobra")             # the well-known tools below
    enable_cobra("mytool")      # any other cobra-based CLI you use

A module your ``config.py`` imports can call :func:`enable_cobra` just
the same.
"""

from __future__ import annotations

import shutil

from ..commands import registry as command_registry
from ..completion import CobraCompleter

# Widely used CLIs known to be built on cobra with completion support.  A
# name not on PATH is skipped, so listing a tool costs nothing on a host
# that doesn't have it.
COMMANDS: tuple[str, ...] = (
    "argocd",
    "buf",
    "cilium",
    "cosign",
    "docker",
    "doctl",
    "eksctl",
    "etcdctl",
    "flux",
    "gh",
    "glab",
    "golangci-lint",
    "goreleaser",
    "hcloud",
    "helm",
    "hugo",
    "istioctl",
    "k3d",
    "kind",
    "kubectl",
    "kustomize",
    "linkerd",
    "minikube",
    "oras",
    "podman",
    "rclone",
    "skaffold",
    "velero",
)

_completer = CobraCompleter()


def enable_cobra(*names: str) -> None:
    """Complete each of *names* through its cobra ``__complete`` subcommand.

    Each name becomes a completion-only recipe whose every slot (flags and
    positionals) is answered by the tool itself.  Skipped when the name is
    not on ``PATH`` — like every other recipe — or when a command of that
    name is already registered, so a hand-written recipe always wins.
    """
    for name in names:
        if shutil.which(name) is None or command_registry.has(name):
            continue
        command_registry.command(
            name,
            help=f"{name} (completion via `{name} __complete`)",
            delegate=_completer,
        )


def register() -> None:
    enable_cobra(*COMMANDS)
