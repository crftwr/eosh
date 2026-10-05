"""Completion recipe for chown."""

from __future__ import annotations

import shutil
import subprocess

from ..commands import arg, registry as command_registry
from ..completion import (
    Completer,
    Completion,
    CompletionContext,
    FileCompleter,
)


class OwnerCompleter(Completer):
    """Completes USER or USER:GROUP for chown.

    Splits the prefix at ':' and completes the user before, the group after.
    Falls back to silent empty list when the system enumeration commands are
    unavailable.
    """

    def complete(self, ctx: CompletionContext) -> list[Completion]:
        prefix = ctx.prefix
        if ":" in prefix:
            user_part, group_part = prefix.split(":", 1)
            groups = self._list_groups()
            return [
                Completion(value=f"{user_part}:{g}", display=g)
                for g in groups
                if g.startswith(group_part)
            ]
        users = self._list_users()
        return [Completion(value=u) for u in users if u.startswith(prefix)]

    @staticmethod
    def _run(cmd: list[str]) -> list[str]:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        names: list[str] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line and not line.startswith("_"):
                names.append(line)
        return sorted(set(names))

    def _list_users(self) -> list[str]:
        # macOS: dscl; Linux: getent passwd; fallback to /etc/passwd.
        if shutil.which("dscl"):
            return self._run(["dscl", ".", "-list", "/Users"])
        if shutil.which("getent"):
            out = self._run_pairs(["getent", "passwd"])
            return out
        return self._read_passwd_file("/etc/passwd")

    def _list_groups(self) -> list[str]:
        if shutil.which("dscl"):
            return self._run(["dscl", ".", "-list", "/Groups"])
        if shutil.which("getent"):
            return self._run_pairs(["getent", "group"])
        return self._read_passwd_file("/etc/group")

    @staticmethod
    def _run_pairs(cmd: list[str]) -> list[str]:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        names = []
        for line in result.stdout.splitlines():
            name = line.split(":", 1)[0].strip()
            if name and not name.startswith("_"):
                names.append(name)
        return sorted(set(names))

    @staticmethod
    def _read_passwd_file(path: str) -> list[str]:
        try:
            with open(path) as f:
                lines = f.readlines()
        except OSError:
            return []
        names = []
        for line in lines:
            name = line.split(":", 1)[0].strip()
            if name and not name.startswith("_"):
                names.append(name)
        return sorted(set(names))


def register() -> None:
    if shutil.which("chown") is None:
        return
    command_registry.command(
        "chown",
        help="change file owner and group",
        params=[
            arg("owner", help="user or user:group", completer=OwnerCompleter()),
            arg("file", nargs="*", help="file or directory", completer=FileCompleter()),
            arg("-f", action="store_true", help="do not display diagnostic messages on failure"),
            arg("-h", action="store_true", help="change owner of symlink itself, not target"),
            arg("-H", action="store_true", help="with -R, follow symlinks on the command line only"),
            arg("-L", action="store_true", help="with -R, follow all symlinks"),
            arg("-P", action="store_true", help="with -R, do not follow any symlinks (default)"),
            arg("-R", action="store_true", help="recurse into directories"),
            arg("-v", action="store_true", help="verbose — show files as ownership is changed"),
            arg("-x", action="store_true", help="do not cross filesystem boundaries with -R"),
        ],
    )
