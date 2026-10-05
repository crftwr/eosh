"""Tests for the tar recipe — positional dispatch (archive vs member files)."""

from __future__ import annotations

import os
import tempfile

import pytest

from eosh.completion import CompletionContext
from eosh.recipes.tar import (
    TarArchiveCompleter,
    _TarPositionalCompleter,
)


def make_ctx(args=None, prefix="", command="tar"):
    return CompletionContext(
        command=command,
        args=args or [],
        arg_index=len(args) if args else 0,
        prefix=prefix,
        line="",
        shell_context=None,
    )


# ---------------------------------------------------------------------------
# _TarPositionalCompleter._is_archive_slot
# ---------------------------------------------------------------------------

class TestArchiveSlotDetection:
    def setup_method(self):
        self.p = _TarPositionalCompleter()

    @pytest.mark.parametrize("args, expected", [
        ([], True),                                 # bare `tar <TAB>`
        (["-cvzf"], True),                          # cluster (treated as one flag) → archive next
        (["-cvzf", "a.tgz"], False),                # archive given → members next
        (["-xvf"], True),                           # extract bundle
        (["-xvf", "a.tgz", "member1"], False),      # member already started
        (["-f", "a.tgz"], False),                   # archive already given via -f
        (["-f", "a.tgz", "member"], False),
        (["-cvzf", "-C", "/tmp"], True),            # -C consumes /tmp; still need archive
        (["-cvzf", "-C", "/tmp", "a.tgz"], False),
        (["./doc/"], False),                        # path counted as rank 1, no archive slot
    ])
    def test_archive_slot(self, args, expected):
        assert self.p._is_archive_slot(args) is expected


# ---------------------------------------------------------------------------
# _TarPositionalCompleter.complete
# ---------------------------------------------------------------------------

class TestPositionalCompleter:
    def setup_method(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self.tmp.name
        # Layout: a.tgz (archive), b.txt (regular), doc/ (directory)
        open(os.path.join(self.tmpdir, "a.tgz"), "w").close()
        open(os.path.join(self.tmpdir, "b.txt"), "w").close()
        os.mkdir(os.path.join(self.tmpdir, "doc"))
        self._old_cwd = os.getcwd()
        os.chdir(self.tmpdir)

    def teardown_method(self):
        os.chdir(self._old_cwd)
        self.tmp.cleanup()

    def test_archive_slot_lists_archives_first(self):
        """`tar -cvzf <TAB>` → archives appear before regular files."""
        p = _TarPositionalCompleter()
        results = p.complete(make_ctx(args=["-cvzf"], prefix=""))
        non_dir = [r.value for r in results if not r.value.endswith("/")]
        assert non_dir.index("a.tgz") < non_dir.index("b.txt")

    def test_member_slot_uses_plain_file_completer(self):
        """`tar -cvzf a.tgz <TAB>` → member files; no archive prioritisation."""
        p = _TarPositionalCompleter()
        results = p.complete(make_ctx(args=["-cvzf", "a.tgz"], prefix=""))
        values = [r.value for r in results]
        assert values.index("a.tgz") < values.index("b.txt")

    def test_dispatches_archive_completer_on_archive_slot(self):
        p = _TarPositionalCompleter()
        results = p.complete(make_ctx(args=["-xvf"], prefix=""))
        non_dir = [r.value for r in results if not r.value.endswith("/")]
        assert non_dir.index("a.tgz") < non_dir.index("b.txt")


# ---------------------------------------------------------------------------
# _TarPositionalCompleter.describe_slot — drives the status-bar label
# ---------------------------------------------------------------------------

class TestDescribeSlot:
    def setup_method(self):
        self.p = _TarPositionalCompleter()

    @pytest.mark.parametrize("args, expected", [
        ([], "archive: tar archive"),
        (["-cvzf"], "archive: tar archive"),
        (["-cvzf", "out.tgz"], "file: file to add or extract"),
        (["-f", "out.tar"], "file: file to add or extract"),
        (["-f", "out.tar", "member"], "file: file to add or extract"),
        (["./doc/"], "file: file to add or extract"),
    ])
    def test_describe_slot(self, args, expected):
        # pos_idx is unused by _TarPositionalCompleter — it dispatches off args.
        assert self.p.describe_slot(args, len(args)) == expected


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_register_wires_smart_completer(self):
        """The recipe registers a wildcard positional + flag arg() declarations."""
        from eosh.commands import registry as command_registry, WILDCARD, get_positional_completer
        from eosh.recipes import tar as tar_recipe

        if not _which("tar"):
            pytest.skip("tar not on PATH")

        tar_recipe.register()
        cmd = command_registry.get("tar")
        assert cmd is not None
        # The wildcard positional serves any integer slot.
        c0 = get_positional_completer(cmd.completers, 0)
        c5 = get_positional_completer(cmd.completers, 5)
        assert c0 is c5
        assert isinstance(c0, _TarPositionalCompleter)
        assert isinstance(cmd.completers.get(WILDCARD), _TarPositionalCompleter)
        # Flag completer is the standard OptionsCompleter built from arg() declarations.
        assert "-c" in cmd.completers.get(None).options
        assert "-f" in cmd.completers.get(None).args


def _which(cmd: str) -> bool:
    import shutil
    return shutil.which(cmd) is not None
