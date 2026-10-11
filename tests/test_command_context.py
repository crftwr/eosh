"""Tests for ``CommandContext`` / ``ShellView`` — what user code sees of the shell."""

from __future__ import annotations

import os

import pytest

from eosh.command_context import CommandContext, ShellView, SubshellContext
from eosh.commands import CommandRegistry, registry as command_registry
from eosh.context import ContextManager
from eosh.shell import Shell
from eosh.variables import EnvVar, GlobalVar, PyVar, registry as var_registry


@pytest.fixture
def sh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shell = Shell()
    yield shell
    command_registry.clear_user_commands()
    var_registry.clear_user_vars()
    for key in ("EOSH_T_A", "EOSH_T_REGION", "EOSH_T_REGION2"):
        os.environ.pop(key, None)


# ── pass_context ────────────────────────────────────────────────────────────

def test_a_pass_context_handler_gets_the_context_first():
    reg = CommandRegistry()
    got = {}

    @reg.command("greet", pass_context=True)
    def greet(ctx, name="x"):
        got.update(ctx=ctx, name=name)

    reg.get("greet").invoke(["bob"], ctx="CTX")
    assert got == {"ctx": "CTX", "name": "bob"}


def test_a_handler_without_it_gets_no_context():
    reg = CommandRegistry()

    @reg.command("plain")
    def plain(name):
        return name

    assert reg.get("plain").invoke(["bob"], ctx="CTX") == "bob"


def test_a_sub_command_opts_in_on_its_own():
    reg = CommandRegistry()
    tool = reg.command("tool")
    got = []

    @tool.command("ask", pass_context=True)
    def ask(ctx):
        got.append(ctx)

    reg.get("tool").invoke(["ask"], ctx="CTX")
    assert got == ["CTX"]


def test_a_decorator_gets_the_context_before_the_pipeline():
    reg = CommandRegistry()
    got = []

    @reg.command("@t", pass_context=True)
    def deco(ctx, pipeline):
        got.append((ctx, pipeline))

    reg.get("@t").invoke([], "PIPE", ctx="CTX")
    assert got == [("CTX", "PIPE")]


def test_the_shell_passes_a_context_bound_to_the_current_one(sh):
    got = []

    @command_registry.command("_t_ctx", sync=True, pass_context=True)
    def handler(ctx):
        got.append(ctx)

    sh._execute("_t_ctx")
    assert isinstance(got[0], CommandContext)
    assert got[0].context_name == "default"


# ── ShellView: the command's own context, current or not ───────────────────

def _manager_with_two(tmp_path):
    cm = ContextManager()
    cm.create("a")
    cm.set_variable("EOSH_T_A", "in-a")
    a = cm.current()
    (tmp_path / "b").mkdir()
    cm.new("b")
    cm.switch("b")
    os.chdir(tmp_path / "b")
    cm.set_variable("EOSH_T_A", "in-b")
    return cm, a


def test_a_view_of_a_background_context_reads_its_own_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cm, a = _manager_with_two(tmp_path)
    try:
        view = ShellView(cm, a)
        assert view.context_name == "a"
        assert view.cwd == os.path.realpath(tmp_path) or view.cwd == str(tmp_path)
        assert view.get_var("EOSH_T_A") == "in-a"
        assert ShellView(cm, cm.current()).get_var("EOSH_T_A") == "in-b"
    finally:
        os.environ.pop("EOSH_T_A", None)


def test_a_view_reads_registered_vars(sh):
    var_registry.register(EnvVar("eosh_t_region", keys=["EOSH_T_REGION", "EOSH_T_REGION2"]))
    sh._set_variable("eosh_t_region", "us-west-2")
    assert sh._shell_view().get_var("eosh_t_region") == "us-west-2"


# ── CommandContext: variables go to the command's own context ──────────────

def test_set_var_in_the_current_context_is_like_var(sh):
    ctx = sh._command_context()
    ctx.set_var("EOSH_T_A", "1")
    assert os.environ["EOSH_T_A"] == "1"
    ctx.unset_var("EOSH_T_A")
    assert "EOSH_T_A" not in os.environ


def test_set_var_after_a_switch_lands_in_the_commands_context(sh):
    ctx = sh._command_context()            # started in "default"
    sh._execute("context new other")       # …then the user moved on
    ctx.set_var("EOSH_T_A", "from-background")
    assert "EOSH_T_A" not in os.environ    # "other" is untouched
    assert ctx.get_var("EOSH_T_A") == "from-background"
    sh._execute("context switch default")
    assert os.environ["EOSH_T_A"] == "from-background"


def test_a_pyvar_set_in_the_background_comes_back_with_its_context(sh):
    class Endpoint(PyVar):
        name = "eosh_t_endpoint"
        value = None

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    var = Endpoint()
    var_registry.register(var)
    ctx = sh._command_context()
    sh._execute("context new other")
    ctx.set_var("eosh_t_endpoint", "https://staging")
    assert var.get() != "https://staging"  # not the current context's value
    assert ctx.get_var("eosh_t_endpoint") == "https://staging"
    sh._execute("context switch default")
    assert var.get() == "https://staging"


def test_unset_var_in_the_background_stays_unset_there(sh, monkeypatch):
    monkeypatch.setenv("EOSH_T_A", "base")      # from the environment eosh started with
    ctx = sh._command_context()
    sh._execute("context new other")
    ctx.unset_var("EOSH_T_A")
    assert os.environ["EOSH_T_A"] == "base"     # "other" keeps it
    assert ctx.get_var("EOSH_T_A") is None
    assert "EOSH_T_A" not in ctx.environ()
    sh._execute("context switch default")
    assert "EOSH_T_A" not in os.environ


def test_environ_is_the_contexts_whole_environment(sh):
    ctx = sh._command_context()
    ctx.set_var("EOSH_T_A", "here")
    sh._execute("context new other")
    sh._execute("var EOSH_T_A=there")
    assert ctx.environ()["EOSH_T_A"] == "here"
    assert sh._command_context().environ()["EOSH_T_A"] == "there"


# ── chdir ───────────────────────────────────────────────────────────────────

def test_chdir_in_the_current_context_is_cd(sh, tmp_path):
    (tmp_path / "sub").mkdir()
    ctx = sh._command_context()
    assert ctx.chdir("sub") == os.getcwd()
    assert os.path.realpath(os.getcwd()) == os.path.realpath(tmp_path / "sub")
    assert os.environ["PWD"] == os.getcwd()


def test_chdir_after_a_switch_lands_in_the_commands_context(sh, tmp_path):
    (tmp_path / "sub").mkdir()
    ctx = sh._command_context()            # started in "default", in tmp_path
    sh._execute("context new other")
    here = os.getcwd()
    ctx.chdir("sub")                       # relative to the context's own cwd
    assert os.getcwd() == here             # "other" didn't move
    assert ctx.cwd == os.path.join(str(tmp_path), "sub")
    sh._execute("context switch default")
    assert os.path.realpath(os.getcwd()) == os.path.realpath(tmp_path / "sub")


@pytest.mark.parametrize("make, error", [
    (lambda p: None, FileNotFoundError),
    (lambda p: p.write_text(""), NotADirectoryError),
])
def test_chdir_in_the_background_refuses_what_cd_would(sh, tmp_path, make, error):
    make(tmp_path / "target")
    ctx = sh._command_context()
    sh._execute("context new other")
    with pytest.raises(error):
        ctx.chdir("target")
    assert ctx.cwd == str(tmp_path)


# ── the built-ins that change one context ───────────────────────────────────

@pytest.mark.parametrize("name", ["cd", "var", "source-bash"])
def test_per_context_builtins_are_not_sync(sh, name):
    assert not sh.registry.get(name).sync


@pytest.mark.parametrize("name", ["context", "alias", "unalias", "reload", "exit", "config"])
def test_shell_wide_builtins_stay_sync(sh, name):
    assert sh.registry.get(name).sync


def test_cd_in_the_background_moves_its_own_context(sh, tmp_path):
    (tmp_path / "sub").mkdir()
    ctx = sh._command_context()
    sh._execute("context new other")
    sh.registry.get("cd").invoke(["sub"], ctx=ctx)
    assert ctx.cwd == os.path.join(str(tmp_path), "sub")
    assert os.path.realpath(os.getcwd()) == os.path.realpath(tmp_path)


def test_source_bash_in_the_background_imports_into_its_own_context(sh, tmp_path):
    (tmp_path / "sub").mkdir()
    ctx = sh._command_context()
    sh._execute("context new other")
    sh.registry.get("source-bash").invoke(
        ["-q", "-c", "export EOSH_T_A=from-bash; cd sub"], ctx=ctx)
    assert "EOSH_T_A" not in os.environ
    assert os.path.realpath(os.getcwd()) == os.path.realpath(tmp_path)
    sh._execute("context switch default")
    assert os.environ["EOSH_T_A"] == "from-bash"
    assert os.path.realpath(os.getcwd()) == os.path.realpath(tmp_path / "sub")


def test_a_globalvar_is_one_value_everywhere(sh):
    class Knob(GlobalVar):
        name = "eosh_t_knob"
        value = "a"

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    var = Knob()
    var_registry.register(var)
    ctx = sh._command_context()
    sh._execute("context new other")
    ctx.set_var("eosh_t_knob", "b")
    assert var.get() == "b"


# ── Talking to the user ─────────────────────────────────────────────────────

@pytest.mark.parametrize("answer,default,expected", [
    ("y", False, True), ("yes", False, True), ("n", False, False),
    ("", False, False), ("", True, True), ("no", True, False),
])
def test_confirm(monkeypatch, answer, default, expected):
    asked = []
    monkeypatch.setattr("eosh.slots._read_from_user",
                        lambda prompt, **kw: asked.append(prompt) or answer)
    ctx = CommandContext(None, None, None)
    assert ctx.confirm("Delete it?", default=default) is expected
    assert asked == ["Delete it? [Y/n] " if default else "Delete it? [y/N] "]


def test_choose_without_a_terminal_asks_for_a_number(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": "2")
    ctx = CommandContext(None, None, None)
    assert ctx.choose(["dev", "prod"], title="Where?") == "prod"
    out = capsys.readouterr().out
    assert "Where?" in out and "  2) prod" in out


def test_choose_nothing_is_none():
    assert CommandContext(None, None, None).choose([]) is None


# ── a pipeline stage is a subshell (discussion #85) ─────────────────────────

def test_a_subshell_reads_its_parent_until_it_writes(sh, tmp_path):
    (tmp_path / "sub").mkdir()
    parent = sh._command_context()
    parent.set_var("EOSH_T_A", "outer")
    sub = SubshellContext(parent)
    assert sub.get_var("EOSH_T_A") == "outer"
    assert sub.cwd == parent.cwd

    sub.set_var("EOSH_T_A", "inner")
    assert sub.chdir("sub") == os.path.join(parent.cwd, "sub")
    assert sub.get_var("EOSH_T_A") == "inner"
    assert sub.environ()["EOSH_T_A"] == "inner"
    assert sub.cwd == os.path.join(parent.cwd, "sub")

    assert os.environ["EOSH_T_A"] == "outer"
    assert os.path.realpath(os.getcwd()) == os.path.realpath(tmp_path)


def test_a_subshell_unset_hides_the_parents_value(sh):
    parent = sh._command_context()
    parent.set_var("EOSH_T_A", "outer")
    sub = SubshellContext(parent)
    sub.unset_var("EOSH_T_A")
    assert sub.get_var("EOSH_T_A") is None
    assert "EOSH_T_A" not in sub.environ()
    assert os.environ["EOSH_T_A"] == "outer"


def test_a_subshell_keeps_python_side_vars_to_itself(sh):
    class Knob(GlobalVar):
        name = "eosh_t_knob"
        value = "a"

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    knob = Knob()
    var_registry.register(knob)
    sub = SubshellContext(sh._command_context())
    sub.set_var("eosh_t_knob", "b")
    assert sub.get_var("eosh_t_knob") == "b"
    assert knob.get() == "a"


def test_a_subshell_writes_each_key_of_an_envvar(sh):
    var_registry.register(EnvVar("eosh_t_region", keys=["EOSH_T_REGION", "EOSH_T_REGION2"]))
    sub = SubshellContext(sh._command_context())
    sub.set_var("eosh_t_region", "us-west-2")
    assert sub.get_var("eosh_t_region") == "us-west-2"
    env = sub.environ()
    assert env["EOSH_T_REGION"] == env["EOSH_T_REGION2"] == "us-west-2"
    assert "EOSH_T_REGION" not in os.environ


def test_a_subshell_chdir_refuses_what_cd_would(sh, tmp_path):
    (tmp_path / "file").write_text("")
    sub = SubshellContext(sh._command_context())
    with pytest.raises(NotADirectoryError):
        sub.chdir("file")
    assert sub.cwd == sh._command_context().cwd


@pytest.mark.requires_real_stdio
@pytest.mark.parametrize("line", ["cd sub | cat", "echo | cd sub"])
def test_cd_in_a_pipeline_changes_nothing(sh, tmp_path, line):
    (tmp_path / "sub").mkdir()
    sh._execute(line)
    assert os.path.realpath(os.getcwd()) == os.path.realpath(tmp_path)


@pytest.mark.requires_real_stdio
@pytest.mark.parametrize("line", ["var EOSH_T_A=1 | cat", "echo | var EOSH_T_A=1"])
def test_var_in_a_pipeline_changes_nothing(sh, line):
    sh._execute(line)
    assert "EOSH_T_A" not in os.environ


@pytest.mark.requires_real_stdio
def test_cd_with_a_redirect_still_changes_directory(sh, tmp_path):
    # Not a pipeline: bash runs `cd x > log` in the shell itself.
    (tmp_path / "sub").mkdir()
    sh._execute(f"cd sub > {tmp_path / 'log'}")
    assert os.path.realpath(os.getcwd()) == os.path.realpath(tmp_path / "sub")


@pytest.mark.requires_real_stdio
def test_a_stage_sees_its_own_changes(sh, tmp_path):
    seen = []

    @command_registry.command("_t_cd_then_look", pass_context=True)
    def look(ctx):
        ctx.chdir("sub")
        ctx.set_var("EOSH_T_A", "x")
        seen.append((ctx.cwd, ctx.get_var("EOSH_T_A")))

    (tmp_path / "sub").mkdir()
    sh._execute("_t_cd_then_look | cat")
    assert seen == [(os.path.join(str(tmp_path), "sub"), "x")]
    assert os.path.realpath(os.getcwd()) == os.path.realpath(tmp_path)
    assert "EOSH_T_A" not in os.environ


@pytest.mark.requires_real_stdio
def test_a_decorator_body_in_a_pipeline_is_a_subshell(sh, tmp_path):
    (tmp_path / "sub").mkdir()
    sh._execute(f"@quiet {{cd sub}} | cat > {tmp_path / 'out'}")
    assert os.path.realpath(os.getcwd()) == os.path.realpath(tmp_path)


@pytest.mark.requires_real_stdio
def test_a_decorator_in_a_pipeline_gets_the_stages_subshell(sh, tmp_path):
    got = []

    @command_registry.command("@_t_rec", pass_context=True)
    def rec(ctx, pipeline):
        got.append(ctx)
        return pipeline.run()

    sh._execute(f"@_t_rec {{true}} | cat > {tmp_path / 'out'}")
    sh._execute("@_t_rec true")
    assert isinstance(got[0], SubshellContext)
    assert not isinstance(got[1], SubshellContext)
