"""Command history storage."""

from __future__ import annotations

import os
from pathlib import Path

from .paths import config_dir


class History:
    def __init__(self, path: str | Path | None = None):
        if path is None:
            path = config_dir() / "history"
        self.path = Path(path)
        self.entries: list[str] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            self.entries = self.path.read_text().splitlines()

    def add(self, line: str) -> None:
        if not line.strip():
            return
        if self.entries and self.entries[-1] == line:
            return
        self.entries.append(line)
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("\n".join(self.entries) + "\n")

    def search(self, prefix: str) -> list[str]:
        return [e for e in reversed(self.entries) if prefix in e]
