"""Tests for loading ``~/.eosh/config.py`` (``Shell._load_user_config``)."""

from __future__ import annotations

import sys

from eosh import shell as shell_mod
from eosh.shell import Shell

# Captured at import time, before conftest's autouse ``_no_user_config``
# replaces the method for every test.
_real_load_user_config = Shell.__dict__["_load_user_config"]


def test_first_launch_writes_and_loads_the_starter_config(tmp_path, monkeypatch):
    """A fresh install gets its recipes on the first launch, not the second."""
    starter = tmp_path / "starter.py"
    starter.write_text("import sys\nsys._eosh_test_config_loaded = True\n")
    home = tmp_path / "home"
    monkeypatch.setattr(shell_mod, "config_dir", lambda: home / ".eosh")
    monkeypatch.setattr(shell_mod, "_DEFAULT_CONFIG_PATH", starter)
    monkeypatch.setattr(sys, "_eosh_test_config_loaded", False, raising=False)

    _real_load_user_config(Shell.__new__(Shell))

    assert (home / ".eosh" / "config.py").read_text() == starter.read_text()
    assert sys._eosh_test_config_loaded is True


def test_config_imports_its_own_modules_and_reload_runs_them_again(tmp_path, monkeypatch):
    """~/.eosh is on sys.path while config.py runs, so your own recipes and
    decorators live in modules there — and `reload` re-runs them, since it
    just cleared what they registered."""
    home = tmp_path / ".eosh"
    home.mkdir()
    (home / "config.py").write_text("import eosh_t_my_tools\n")
    (home / "eosh_t_my_tools.py").write_text(
        "import sys\nsys._eosh_t_runs = getattr(sys, '_eosh_t_runs', 0) + 1\n")
    monkeypatch.setattr(shell_mod, "config_dir", lambda: home)
    monkeypatch.setattr(sys, "_eosh_t_runs", 0, raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    try:
        _real_load_user_config(Shell.__new__(Shell))
        _real_load_user_config(Shell.__new__(Shell))      # what `reload` does
        assert sys._eosh_t_runs == 2
    finally:
        sys.modules.pop("eosh_t_my_tools", None)


# ── reload: one sweep back to the built-ins ─────────────────────────────────

def _config(tmp_path, monkeypatch, text: str):
    home = tmp_path / ".eosh"
    home.mkdir(exist_ok=True)
    (home / "config.py").write_text(text)
    monkeypatch.setattr(shell_mod, "config_dir", lambda: home)
    monkeypatch.setattr(sys, "path", list(sys.path))


def test_reload_puts_back_a_builtin_the_config_stopped_overriding(tmp_path, monkeypatch):
    sh = Shell()
    builtin_cd = sh.registry.get("cd")
    _config(tmp_path, monkeypatch,
            "from eosh.commands import registry\n"
            "@registry.command('cd', override=True)\n"
            "def cd(path=''):\n"
            "    return 0\n")
    try:
        _real_load_user_config(sh)
        assert sh.registry.get("cd") is not builtin_cd

        _config(tmp_path, monkeypatch, "")
        sh._clear_user_config()
        _real_load_user_config(sh)
        assert sh.registry.get("cd") is builtin_cd
    finally:
        sh._clear_user_config()


def test_a_refused_override_does_not_stop_the_rest_of_the_config(tmp_path, monkeypatch, capsys):
    sh = Shell()
    _config(tmp_path, monkeypatch,
            "from eosh.commands import registry\n"
            "@registry.command('help')\n"
            "def my_help():\n"
            "    pass\n"
            "@registry.command('_t_after')\n"
            "def after():\n"
            "    pass\n")
    try:
        _real_load_user_config(sh)
        assert sh.registry.is_builtin("help")
        assert sh.registry.has("_t_after")
        assert "config warning: 'help' is a built-in command" in capsys.readouterr().err
    finally:
        sh._clear_user_config()


def test_reload_restores_the_notify_backend_and_skip_list(monkeypatch):
    from eosh import notify
    sh = Shell()
    notify.set_notifier(lambda title, msg: None)
    notify.SKIP_COMMANDS.add("_t_skip")
    sh._clear_user_config()
    assert notify._notifier is None
    assert "_t_skip" not in notify.SKIP_COMMANDS
    assert "vim" in notify.SKIP_COMMANDS


def test_help_lists_your_commands_apart_from_the_builtins(capsys):
    sh = Shell()

    @sh.registry.command("_t_mine", help="mine")
    def mine():
        pass

    try:
        sh.registry.get("help").invoke([])
        out = capsys.readouterr().out
        builtins, _, rest = out.partition("Commands from your config:")
        assert "  cd " in builtins and "_t_mine" not in builtins
        assert "_t_mine" in rest.partition("Decorators")[0]
    finally:
        sh._clear_user_config()


# ── config edit ─────────────────────────────────────────────────────────────

def _edit(monkeypatch, tmp_path, status=0, error=None):
    sh = Shell()
    calls = []

    def fake_run(argv):
        calls.append(argv)
        if error:
            raise error
        return status

    monkeypatch.setattr(shell_mod, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(shell_mod, "_run_interactive", fake_run)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", "myed -w")
    reloads = []
    monkeypatch.setattr(sh, "_reload_config", lambda: reloads.append(1))
    code = sh.registry.get("config").invoke(["edit"])
    return code, calls, reloads


def test_config_edit_runs_the_editor_then_reloads(monkeypatch, tmp_path):
    code, calls, reloads = _edit(monkeypatch, tmp_path)
    assert calls == [["myed", "-w", str(tmp_path / "config.py")]]
    assert reloads == [1] and code == 0


def test_config_edit_does_not_reload_when_the_editor_fails(monkeypatch, tmp_path, capsys):
    code, _, reloads = _edit(monkeypatch, tmp_path, status=3)
    assert reloads == [] and code == 3
    assert "not reloading" in capsys.readouterr().err


def test_config_edit_with_a_missing_editor(monkeypatch, tmp_path, capsys):
    code, _, reloads = _edit(monkeypatch, tmp_path, error=FileNotFoundError())
    assert reloads == [] and code == 127
    assert "editor not found: myed" in capsys.readouterr().err
