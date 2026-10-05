"""Completion recipe for git, modelled as a sub-command tree."""

from __future__ import annotations

import subprocess

from ..commands import registry as command_registry, arg
from ..completion import Completer, Completion, CompletionContext, FileCompleter


def _run_git(args: list[str], timeout: float = 2.0) -> list[str]:
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    except (OSError, subprocess.TimeoutExpired):
        return []


# ─── Dynamic completers ─────────────────────────────────────────────────────

class GitBranchCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        lines = _run_git(["branch", "--all", "--format=%(refname:short)"])
        return [
            Completion(value=b, description="branch")
            for b in lines if b.startswith(ctx.prefix)
        ]


class GitTagCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        lines = _run_git(["tag"])
        return [Completion(value=t, description="tag") for t in lines if t.startswith(ctx.prefix)]


class GitModifiedFileCompleter(Completer):
    """Completes files that are modified, untracked, or staged."""

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        lines = _run_git(["status", "--short"])
        seen: set[str] = set()
        completions = []
        for line in lines:
            if len(line) < 4:
                continue
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ")[-1]
            if path not in seen and path.startswith(ctx.prefix):
                seen.add(path)
                completions.append(Completion(value=path))
        return completions


class GitRemoteCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        lines = _run_git(["remote"])
        return [Completion(value=r, description="remote") for r in lines if r.startswith(ctx.prefix)]


class GitStashRefCompleter(Completer):
    def complete(self, ctx: CompletionContext) -> list[Completion]:
        lines = _run_git(["stash", "list", "--format=%gd: %s"])
        completions = []
        for line in lines:
            parts = line.split(": ", 1)
            ref = parts[0]
            desc = parts[1] if len(parts) > 1 else ""
            if ref.startswith(ctx.prefix):
                completions.append(Completion(value=ref, description=desc))
        return completions


class GitRefCompleter(Completer):
    """Branches + tags merged (used for diff, show, blame, log)."""
    _branch = GitBranchCompleter()
    _tag = GitTagCompleter()

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        results = self._branch.complete(ctx) + self._tag.complete(ctx) + FileCompleter().complete(ctx)
        seen: set[str] = set()
        unique = []
        for c in results:
            if c.value not in seen:
                seen.add(c.value)
                unique.append(c)
        return unique


# ─── Tree definition ─────────────────────────────────────────────────────────

def register() -> None:
    git = command_registry.command("git", help="distributed version control")

    # ── add ──
    git.command(
        "add", help="add file contents to the index",
        params=[
            arg("path", nargs="*", completer=GitModifiedFileCompleter()),
            arg("-A", "--all", action="store_true", help="stage all changes"),
            arg("-p", "--patch", action="store_true", help="interactive patch"),
        ],
    )

    # ── branch ──
    git.command(
        "branch", help="list, create, or delete branches",
        params=[
            arg("name", nargs="?", completer=GitBranchCompleter()),
            arg("-d", "--delete", action="store_true", help="delete branch"),
            arg("-D", action="store_true", help="force delete"),
            arg("-a", "--all", action="store_true", help="show all branches"),
            arg("-r", "--remotes", action="store_true", help="show remote branches"),
        ],
    )

    # ── checkout / switch / merge / cherry-pick / restore ──
    git.command(
        "checkout", help="switch branches or restore working tree files",
        params=[
            arg("ref", nargs="?", completer=GitBranchCompleter()),
            arg("-b", metavar="NEW_BRANCH", help="create and switch to a new branch"),
            arg("-B", metavar="NEW_BRANCH", help="create/reset and switch to a branch"),
            arg("--detach", action="store_true", help="detach HEAD"),
            arg("--orphan", metavar="NEW_BRANCH", help="create a new orphan branch"),
            arg("--track", action="store_true", help="set up tracking mode"),
        ],
    )
    git.command(
        "switch", help="switch branches",
        params=[arg("branch", nargs="?", completer=GitBranchCompleter())],
    )
    git.command(
        "merge", help="join development histories",
        params=[arg("branch", nargs="?", completer=GitBranchCompleter())],
    )
    git.command(
        "cherry-pick", help="apply changes from existing commits",
        params=[arg("ref", nargs="?", completer=GitBranchCompleter())],
    )
    git.command(
        "restore", help="restore working tree files",
        params=[arg("path", nargs="*", completer=GitModifiedFileCompleter())],
    )

    # ── rm / mv ──
    git.command(
        "rm", help="remove files from the working tree and index",
        params=[arg("path", nargs="*", completer=GitModifiedFileCompleter())],
    )
    git.command(
        "mv", help="move or rename a file",
        params=[arg("path", nargs="*", completer=GitModifiedFileCompleter())],
    )

    # ── commit ──
    git.command(
        "commit", help="record changes to the repository",
        params=[
            arg("-a", "--all", action="store_true", help="stage all tracked"),
            arg("--amend", action="store_true", help="replace tip commit"),
            arg("--dry-run", action="store_true", help="show what would be committed"),
            arg("--no-edit", action="store_true", help="reuse current message"),
            arg("-v", "--verbose", action="store_true", help="show diff in editor"),
            arg("-m", "--message", metavar="MSG", help="commit message"),
        ],
    )

    # ── diff / show / blame / log ──
    diff_log_params = [
        arg("ref", nargs="*", completer=GitRefCompleter()),
        arg("--cached", action="store_true", help="view staged changes"),
        arg("--staged", action="store_true", help="view staged changes"),
        arg("--name-only", action="store_true", help="show only filenames"),
        arg("--name-status", action="store_true", help="show names and status"),
        arg("--stat", action="store_true", help="show diffstat"),
        arg("--word-diff", action="store_true", help="word-level diff"),
        arg("-w", action="store_true", help="ignore whitespace"),
    ]
    git.command("diff", help="show changes between commits or working tree", params=diff_log_params)
    git.command(
        "show", help="show various types of objects",
        params=[arg("ref", nargs="?", completer=GitRefCompleter())],
    )
    git.command(
        "blame", help="show last revision per line of a file",
        params=[arg("file", nargs="?", completer=FileCompleter())],
    )
    git.command(
        "log", help="show commit logs",
        params=[
            arg("ref", nargs="*", completer=GitRefCompleter()),
            arg("--all", action="store_true", help="all refs"),
            arg("--follow", action="store_true", help="follow renames"),
            arg("--graph", action="store_true", help="ascii graph"),
            arg("--no-merges", action="store_true", help="omit merges"),
            arg("--oneline", action="store_true", help="one line per commit"),
            arg("--patch", action="store_true", help="generate patch"),
            arg("-p", action="store_true", help="generate patch"),
            arg("--reverse", action="store_true", help="reverse order"),
            arg("--stat", action="store_true", help="show diffstat"),
            arg("-n", metavar="N", type=int, help="limit number of commits"),
        ],
    )

    # ── push / pull / fetch ──
    git.command(
        "push", help="update remote refs",
        params=[
            arg("remote", nargs="?", completer=GitRemoteCompleter()),
            arg("branch", nargs="?", completer=GitBranchCompleter()),
            arg("--all", action="store_true", help="push all branches"),
            arg("--delete", action="store_true", help="delete remote ref"),
            arg("--dry-run", action="store_true", help="dry run"),
            arg("--follow-tags", action="store_true", help="push local tags"),
            arg("-f", "--force", action="store_true", help="force push"),
            arg("--set-upstream", action="store_true", help="set upstream"),
            arg("-u", action="store_true", help="set upstream"),
            arg("--tags", action="store_true", help="push all tags"),
        ],
    )
    git.command(
        "pull", help="fetch and integrate",
        params=[
            arg("remote", nargs="?", completer=GitRemoteCompleter()),
            arg("branch", nargs="?", completer=GitBranchCompleter()),
        ],
    )
    git.command(
        "fetch", help="download objects and refs",
        params=[
            arg("remote", nargs="?", completer=GitRemoteCompleter()),
            arg("branch", nargs="?", completer=GitBranchCompleter()),
        ],
    )

    # ── reset / rebase ──
    git.command(
        "reset", help="reset HEAD to a specified state",
        params=[
            arg("ref", nargs="?", completer=GitRefCompleter()),
            arg("--soft", action="store_true", help="keep index and worktree"),
            arg("--mixed", action="store_true", help="default — reset index"),
            arg("--hard", action="store_true", help="reset everything"),
        ],
    )
    git.command(
        "rebase", help="reapply commits on top of another base",
        params=[
            arg("ref", nargs="?", completer=GitBranchCompleter()),
            arg("--abort", action="store_true", help="abort current rebase"),
            arg("--continue", action="store_true", help="continue after conflicts"),
            arg("-i", "--interactive", action="store_true", help="interactive list"),
            arg("--skip", action="store_true", help="skip current patch"),
        ],
    )

    # ── remote / tag ──
    git.command(
        "remote", help="manage tracked repositories",
        params=[arg("name", nargs="?", completer=GitRemoteCompleter())],
    )
    git.command(
        "tag", help="create, list, delete, or verify a tag",
        params=[arg("name", nargs="?", completer=GitTagCompleter())],
    )

    # ── stash (nested sub-commands) ──
    stash = git.command("stash", help="stash dirty working state")
    stash.command("apply", help="apply a stash without removing it",
                  params=[arg("ref", nargs="?", completer=GitStashRefCompleter())])
    stash.command("drop",  help="remove a single stash entry",
                  params=[arg("ref", nargs="?", completer=GitStashRefCompleter())])
    stash.command("show",  help="show the changes recorded in a stash",
                  params=[arg("ref", nargs="?", completer=GitStashRefCompleter())])
    stash.command("pop",   help="apply and remove the latest stash",
                  params=[arg("ref", nargs="?", completer=GitStashRefCompleter())])
    stash.command("push",  help="save the current state to the stash")
    stash.command("list",  help="list stash entries")
    stash.command("clear", help="remove all stash entries")

    # ── flat sub-commands without dynamic completion ──
    for sub, desc in [
        ("bisect", "binary search to find a bad commit"),
        ("clean",  "remove untracked files from the working tree"),
        ("clone",  "clone a repository into a new directory"),
        ("grep",   "print lines matching a pattern"),
        ("init",   "create an empty Git repository"),
        ("revert", "revert some existing commits"),
        ("status", "show the working tree status"),
    ]:
        git.command(sub, help=desc)
