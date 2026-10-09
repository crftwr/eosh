"""Tests for ``CommandContext`` / ``ShellView`` — what user code sees of the shell."""

from __future__ import annotations

import os

import pytest

from eosh.command_context import CommandContext, ShellView
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
    monkeypatch.setattr("eosh.shell._read_from_user",
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
