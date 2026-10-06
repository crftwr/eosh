import os
import tempfile

from eosh.completion import (
    ChoiceCompleter,
    CommandNameCompleter,
    CompletionContext,
    FileCompleter,
    OptionsCompleter,
)
from eosh.context import Context
from eosh.shell import _positional_index


def make_ctx(prefix="", args=None, command="test"):
    return CompletionContext(
        command=command,
        args=args or [],
        arg_index=len(args) if args else 0,
        prefix=prefix,
        line="",
        shell_context=None,
    )


def test_choice_completer():
    c = ChoiceCompleter(["alpha", "beta", "gamma"])
    results = c.complete(make_ctx(prefix="a"))
    assert len(results) == 1
    assert results[0].value == "alpha"


def test_choice_completer_empty_prefix():
    c = ChoiceCompleter(["alpha", "beta"])
    results = c.complete(make_ctx(prefix=""))
    assert len(results) == 2


def test_file_completer():
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "foo.txt"), "w").close()
        open(os.path.join(tmpdir, "bar.py"), "w").close()
        os.mkdir(os.path.join(tmpdir, "subdir"))

        old_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            c = FileCompleter()
            results = c.complete(make_ctx(prefix="f"))
            assert any(r.value == "foo.txt" for r in results)

            results = c.complete(make_ctx(prefix=""))
            values = [r.value for r in results]
            assert "foo.txt" in values
            assert "bar.py" in values
            assert "subdir/" in values
        finally:
            os.chdir(old_cwd)


def test_options_completer_shows_value_taking_flags():
    """Flags registered only in `args` (not in `options`) must still appear."""
    c = OptionsCompleter(
        {"-n": "dry run", "-v": "verbose"},
        args={
            "-t": ("SECONDS", ChoiceCompleter(["30", "60"])),
            "-b": "BRANCH",
        },
    )
    results = c.complete(make_ctx(prefix="-"))
    values = [r.value for r in results]
    # Boolean flags
    assert "-n" in values
    assert "-v" in values
    # Value-taking flags must also be present
    assert "-t" in values, "-t (args-only flag) missing from completions"
    assert "-b" in values, "-b (args-only flag) missing from completions"
    # arg_hints are set correctly
    t = next(r for r in results if r.value == "-t")
    assert t.arg_hint == "SECONDS"
    b = next(r for r in results if r.value == "-b")
    assert b.arg_hint == "BRANCH"
    # ... and shown with their value placeholder, as ordinary rows
    assert t.display == "-t <SECONDS>"
    n = next(r for r in results if r.value == "-n")
    assert n.display == "-n" and not n.arg_hint


def _make_options_completer():
    return OptionsCompleter(
        {"-n": "dry run", "-v": "verbose"},
        args={
            "-t": ("SECONDS", ChoiceCompleter(["30", "60"])),
            "-b": "BRANCH",
        },
    )


def test_positional_index_no_flags():
    assert _positional_index([], None) == 0
    assert _positional_index(["prod"], None) == 1
    assert _positional_index(["prod", "api"], None) == 2


def test_positional_index_boolean_flags_skipped():
    oc = _make_options_completer()
    # "deploy -n <TAB>" → first positional not yet given
    assert _positional_index(["-n"], oc) == 0
    # "deploy -n -v <TAB>" → still 0
    assert _positional_index(["-n", "-v"], oc) == 0
    # "deploy prod -n <TAB>" → one positional seen
    assert _positional_index(["prod", "-n"], oc) == 1
    # "deploy prod -n api <TAB>" → two positionals seen
    assert _positional_index(["prod", "-n", "api"], oc) == 2


def test_positional_index_value_taking_flags_consume_value_token():
    oc = _make_options_completer()
    # "deploy -t 60 <TAB>" → -t + 60 skipped, positional = 0
    assert _positional_index(["-t", "60"], oc) == 0
    # "deploy prod -t 60 <TAB>" → positional = 1
    assert _positional_index(["prod", "-t", "60"], oc) == 1
    # "deploy -b main prod <TAB>" → -b + main skipped, prod counted
    assert _positional_index(["-b", "main", "prod"], oc) == 1
    # mixed: "deploy -n prod -t 60 api <TAB>" → positional = 2
    assert _positional_index(["-n", "prod", "-t", "60", "api"], oc) == 2


class _EmptyRegistry:
    """Minimal registry stub for CommandNameCompleter — no registered commands."""

    def list_commands(self):
        return []

    def get(self, name):
        return None

    def list_aliases(self):
        return {}


def test_command_name_completer_offers_local_directories():
    with tempfile.TemporaryDirectory() as tmpdir:
        os.mkdir(os.path.join(tmpdir, "scripts"))
        open(os.path.join(tmpdir, "runme.sh"), "w").close()
        old_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            c = CommandNameCompleter(_EmptyRegistry())
            results = c.complete(make_ctx(prefix="scr", command=None))
            values = [r.value for r in results]
            assert "scripts/" in values
        finally:
            os.chdir(old_cwd)


def test_command_name_completer_offers_local_executable_with_path_prefix():
    with tempfile.TemporaryDirectory() as tmpdir:
        exe = os.path.join(tmpdir, "deploy.sh")
        open(exe, "w").close()
        os.chmod(exe, 0o755)
        open(os.path.join(tmpdir, "notes.txt"), "w").close()
        old_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            c = CommandNameCompleter(_EmptyRegistry())
            # Path-shaped prefix ("./") → local executables are offered.
            results = c.complete(make_ctx(prefix="./dep", command=None))
            values = [r.value for r in results]
            assert "./deploy.sh" in values
            # Non-executable files are never command candidates.
            results = c.complete(make_ctx(prefix="./not", command=None))
            assert not any(r.value.endswith("notes.txt") for r in results)
        finally:
            os.chdir(old_cwd)


def test_command_name_completer_hides_bare_local_executables():
    """A bare (non-path) prefix must not offer cwd executables — they'd
    insert a value that resolves via PATH, not the local file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        exe = os.path.join(tmpdir, "deploy.sh")
        open(exe, "w").close()
        os.chmod(exe, 0o755)
        old_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            c = CommandNameCompleter(_EmptyRegistry())
            results = c.complete(make_ctx(prefix="dep", command=None))
            assert not any(r.value == "deploy.sh" for r in results)
        finally:
            os.chdir(old_cwd)


def test_command_name_completer_empty_prefix_no_local_flood():
    with tempfile.TemporaryDirectory() as tmpdir:
        os.mkdir(os.path.join(tmpdir, "adir"))
        old_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            c = CommandNameCompleter(_EmptyRegistry())
            results = c.complete(make_ctx(prefix="", command=None))
            assert not any(r.value == "adir/" for r in results)
        finally:
            os.chdir(old_cwd)


def test_completer_receives_shell_context():
    ctx = CompletionContext(
        command="deploy",
        args=[],
        arg_index=0,
        prefix="",
        line="deploy ",
        shell_context=Context(name="prod"),
    )
    assert ctx.shell_context.name == "prod"
