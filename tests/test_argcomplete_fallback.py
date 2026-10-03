"""Tests for ArgcompleteCompleter — the argcomplete-protocol fallback."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from unittest.mock import patch

import pytest

from pitash import completion as completion_mod
from pitash.completion import (
    ArgcompleteCompleter,
    Completion,
    CompletionContext,
    disable_argcomplete_fallback,
    enable_argcomplete_fallback,
    get_argcomplete_fallback,
)


def make_ctx(line: str, prefix: str, command: str, args=None):
    return CompletionContext(
        command=command,
        args=args or [],
        arg_index=len(args) if args else 0,
        prefix=prefix,
        line=line,
        shell_context=None,
    )


# ---------------------------------------------------------------------------
# Detection: marker in the script itself (plain Python script)
# ---------------------------------------------------------------------------

def test_probe_finds_marker_in_plain_script(tmp_path):
    script = tmp_path / "mytool"
    script.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        # PYTHON_ARGCOMPLETE_OK
        import argparse, argcomplete
        parser = argparse.ArgumentParser()
        argcomplete.autocomplete(parser)
        parser.parse_args()
    """))
    script.chmod(0o755)
    ac = ArgcompleteCompleter()
    with patch("pitash.completion.shutil.which", return_value=str(script)):
        assert ac._probe("mytool") is True


def test_probe_rejects_script_without_marker(tmp_path):
    script = tmp_path / "plain"
    script.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
    script.chmod(0o755)
    ac = ArgcompleteCompleter()
    with patch("pitash.completion.shutil.which", return_value=str(script)):
        assert ac._probe("plain") is False


def test_probe_rejects_binary_file(tmp_path):
    """Compiled binaries can't be argcomplete-aware; bytes-level scanning copes."""
    binary = tmp_path / "bin"
    binary.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 200)  # mach-o-ish header
    binary.chmod(0o755)
    ac = ArgcompleteCompleter()
    with patch("pitash.completion.shutil.which", return_value=str(binary)):
        assert ac._probe("bin") is False


def test_probe_rejects_missing_executable():
    ac = ArgcompleteCompleter()
    with patch("pitash.completion.shutil.which", return_value=None):
        assert ac._probe("nonexistent") is False


def test_probe_rejects_unreadable_executable(tmp_path):
    ac = ArgcompleteCompleter()
    bogus_path = str(tmp_path / "doesnotexist")
    with patch("pitash.completion.shutil.which", return_value=bogus_path):
        assert ac._probe("ghost") is False


# ---------------------------------------------------------------------------
# Detection: setuptools console_script shim → check imported module
# ---------------------------------------------------------------------------

def test_probe_recognises_setuptools_shim_with_marked_module(tmp_path, monkeypatch):
    # 1. Write a fake module with the marker.
    pkg = tmp_path / "fakepkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    main_py = pkg / "main.py"
    main_py.write_text("# PYTHON_ARGCOMPLETE_OK\ndef cli(): pass\n")

    # 2. Write a setuptools-style shim that imports it.
    shim = tmp_path / "fakeshim"
    shim.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import sys
        from fakepkg.main import cli
        if __name__ == '__main__':
            sys.exit(cli())
    """))
    shim.chmod(0o755)

    # 3. Make the package importable from the shim's interpreter.
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))

    ac = ArgcompleteCompleter()
    with patch("pitash.completion.shutil.which", return_value=str(shim)):
        assert ac._probe("fakeshim") is True


def test_probe_rejects_setuptools_shim_with_unmarked_module(tmp_path, monkeypatch):
    pkg = tmp_path / "plainpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "main.py").write_text("def cli(): pass\n")

    shim = tmp_path / "plainshim"
    shim.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        from plainpkg.main import cli
        cli()
    """))
    shim.chmod(0o755)

    monkeypatch.setenv("PYTHONPATH", str(tmp_path))

    ac = ArgcompleteCompleter()
    with patch("pitash.completion.shutil.which", return_value=str(shim)):
        assert ac._probe("plainshim") is False


def test_probe_cached_per_command(tmp_path):
    script = tmp_path / "cached"
    script.write_text("#!/usr/bin/env python3\n# PYTHON_ARGCOMPLETE_OK\n")
    script.chmod(0o755)
    ac = ArgcompleteCompleter()
    with patch("pitash.completion.shutil.which", return_value=str(script)) as which:
        assert ac._is_argcomplete_command("cached") is True
        assert ac._is_argcomplete_command("cached") is True
    # which() is called once for the probe; the second call short-circuits
    # via the cache.  (It's also called inside should_activate but we don't
    # invoke that here.)
    assert which.call_count == 1


# ---------------------------------------------------------------------------
# should_activate
# ---------------------------------------------------------------------------

def test_should_activate_skips_no_command():
    ac = ArgcompleteCompleter()
    ctx = CompletionContext(command=None, args=[], arg_index=0, prefix="", line="", shell_context=None)
    assert ac.should_activate(ctx) is False


def test_should_activate_skips_unknown_command():
    ac = ArgcompleteCompleter()
    with patch("pitash.completion.shutil.which", return_value=None):
        assert ac.should_activate(make_ctx("ghost ", "", "ghost")) is False


def test_should_activate_uses_probe_result(tmp_path):
    script = tmp_path / "marked"
    script.write_text("#!/usr/bin/env python3\n# PYTHON_ARGCOMPLETE_OK\n")
    script.chmod(0o755)
    ac = ArgcompleteCompleter()
    with patch("pitash.completion.shutil.which", return_value=str(script)):
        assert ac.should_activate(make_ctx("marked ", "", "marked")) is True


# ---------------------------------------------------------------------------
# Output parsing — the protocol joins candidates with _IFS=\v
# ---------------------------------------------------------------------------

def test_complete_filters_by_prefix(monkeypatch):
    ac = ArgcompleteCompleter()
    # Bypass detection
    ac._is_argcomplete["mytool"] = True
    monkeypatch.setattr(ac, "_invoke", lambda cmd, line: ["install", "install-all", "inject"])
    results = ac.complete(make_ctx("mytool ins", "ins", "mytool"))
    assert [c.value for c in results] == ["install", "install-all"]


def test_complete_returns_plain_completions(monkeypatch):
    ac = ArgcompleteCompleter()
    ac._is_argcomplete["x"] = True
    monkeypatch.setattr(ac, "_invoke", lambda cmd, line: ["foo", "bar"])
    results = ac.complete(make_ctx("x ", "", "x"))
    assert all(isinstance(c, Completion) for c in results)
    assert all(c.description == "" for c in results)


def test_complete_skips_non_argcomplete_command():
    ac = ArgcompleteCompleter()
    ac._is_argcomplete["ls"] = False
    # No mocking of _invoke — if the dispatch is correct, it never runs.
    assert ac.complete(make_ctx("ls -", "-", "ls")) == []


def test_results_cached_per_line(monkeypatch):
    ac = ArgcompleteCompleter()
    ac._is_argcomplete["x"] = True
    calls = []
    def fake_invoke(cmd, line):
        calls.append(line)
        return ["a", "b"]
    monkeypatch.setattr(ac, "_invoke", fake_invoke)
    ac.complete(make_ctx("x ", "", "x"))
    ac.complete(make_ctx("x ", "", "x"))
    assert calls == ["x "]


def test_results_recomputed_on_line_change(monkeypatch):
    ac = ArgcompleteCompleter()
    ac._is_argcomplete["x"] = True
    calls = []
    monkeypatch.setattr(ac, "_invoke", lambda cmd, line: calls.append(line) or ["a"])
    ac.complete(make_ctx("x a", "a", "x"))
    ac.complete(make_ctx("x b", "b", "x"))
    assert calls == ["x a", "x b"]


# ---------------------------------------------------------------------------
# Module-level enable/disable API
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_state():
    saved = completion_mod._argcomplete_fallback
    saved_enabled = completion_mod._argcomplete_enabled
    completion_mod._argcomplete_fallback = None
    completion_mod._argcomplete_enabled = True
    try:
        yield
    finally:
        completion_mod._argcomplete_fallback = saved
        completion_mod._argcomplete_enabled = saved_enabled


def test_disable_returns_none():
    enable_argcomplete_fallback()
    disable_argcomplete_fallback()
    assert get_argcomplete_fallback() is None


def test_enable_after_disable_restores():
    disable_argcomplete_fallback()
    assert get_argcomplete_fallback() is None
    ac = enable_argcomplete_fallback(timeout=3.0)
    assert ac is not None
    assert ac._timeout == 3.0
    assert get_argcomplete_fallback() is ac


def test_get_lazy_initialises():
    assert completion_mod._argcomplete_fallback is None
    ac = get_argcomplete_fallback()
    assert ac is not None
    # Reuses singleton.
    assert get_argcomplete_fallback() is ac
