"""Configurable key bindings and user line-editor actions (discussion #29)."""

import pytest

from eosh import keys
from eosh.history import HistoryStore
from eosh.lineedit import LineEditor
from eosh.shell import Shell
from eosh.tui import InlinePicker


# Captured before the autouse fixture stubs it out for every Shell().
_real_load_user_config = Shell.__dict__["_load_user_config"]


def _editor(local=None):
    local = local if local is not None else []
    ed = LineEditor(
        history=HistoryStore(None),
        get_completions=lambda line: ([], "", ""),
        get_prompt=lambda: "> ",
        local_history_fn=lambda: local,
    )
    ed._prompt_str, ed._prompt_len, ed._cols = "> ", 2, 80
    return ed


def _type(ed, text):
    for ch in text:
        ed._handle_key(ch.encode())


# ── key names ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("spec, seqs", [
    ("Ctrl-R", (b"\x12",)),
    ("ctrl-r", (b"\x12",)),
    ("Ctrl-]", (b"\x1d",)),
    ("Alt-.", (b"\x1b.",)),
    ("Alt-B", (b"\x1bb",)),            # a bare letter is lowercase
    ("Alt-Shift-B", (b"\x1bB",)),
    ("Ctrl-Alt-H", (b"\x1b\x08",)),
    ("Up", (b"\x1b[A", b"\x1bOA")),
    ("Ctrl-Left", (b"\x1b[1;5D",)),
    ("Alt-Right", (b"\x1b[1;3C",)),
    ("Shift-Tab", (b"\x1b[Z",)),
    ("Delete", (b"\x1b[3~",)),
    ("Ctrl-Delete", (b"\x1b[3;5~",)),
    ("F5", (b"\x1b[15~",)),
    ("Ctrl-Space", (b"\x00",)),
    ("Alt--", (b"\x1b-",)),
])
def test_parse_key(spec, seqs):
    assert keys.parse_key(spec) == seqs


@pytest.mark.parametrize("spec, problem", [
    ("Hyper-X", "unknown modifier"),        # never widened to a bare X
    ("Ctrl-Frob", "unknown key"),
    ("Ctrl-Shift-A", "no distinct code"),
    ("Ctrl-", "no key"),
    ("Ctrl-Tab", "no distinct code"),
])
def test_bad_key_names_are_rejected(spec, problem):
    with pytest.raises(ValueError, match=problem):
        keys.parse_key(spec)


# ── the table ────────────────────────────────────────────────────────────────

def test_defaults():
    assert keys.lookup("prompt", b"\x12") == "prompt.history_search"
    assert keys.lookup("prompt", b"\x1bOA") == "prompt.previous_history"
    assert keys.lookup("picker", b"\x0e") == "picker.next"
    assert keys.lookup("switcher", b"\x0e") == "switcher.new"


def test_bind_replaces_the_defaults():
    keys.bind("prompt.history_search", "Ctrl-S")
    assert keys.lookup("prompt", b"\x13") == "prompt.history_search"
    assert keys.lookup("prompt", b"\x12") is None


def test_a_bound_key_moves_from_its_default_owner():
    keys.bind("history_search", "Ctrl-N")             # "prompt." is assumed
    assert keys.lookup("prompt", b"\x0e") == "prompt.history_search"
    assert keys.key_names("prompt.next_history") == ["Down"]


def test_a_bare_name_binds_every_surface_that_has_it():
    keys.bind("accept", "Ctrl-J")
    for context in ("prompt", "picker"):
        assert keys.lookup(context, b"\n") == f"{context}.accept"
        assert keys.lookup(context, b"\r") is None


def test_a_bare_name_unique_to_one_surface():
    keys.bind("next", "Ctrl-J")
    keys.bind("delete", "Alt-D")
    assert keys.lookup("picker", b"\n") == "picker.next"
    assert keys.lookup("switcher", b"\x1bd") == "switcher.delete"


@pytest.mark.parametrize("order", ["dotted first", "bare first"])
def test_a_dotted_name_wins_over_a_bare_one_on_its_surface(order):
    calls = [("picker.accept", "Ctrl-O"), ("accept", "Ctrl-J")]
    for name, key in (calls if order == "dotted first" else calls[::-1]):
        keys.bind(name, key)
    assert keys.key_names("picker.accept") == ["Ctrl-O"]
    assert keys.key_names("prompt.accept") == ["Ctrl-J"]


def test_empty_list_unbinds():
    keys.bind("prompt.clear_screen", [])
    assert keys.lookup("prompt", b"\x0c") is None
    assert keys.key_names("prompt.clear_screen") == []


def test_reset_restores_the_defaults():
    keys.bind("prompt.history_search", "Ctrl-S")
    keys.reset()
    assert keys.lookup("prompt", b"\x12") == "prompt.history_search"


def test_unknown_action_is_a_warning(capsys):
    keys.bind("prompt.no_such_thing", "Ctrl-X")
    keys.bind("no_such_thing", "Ctrl-X")
    keys.bind("prompt.next", "Ctrl-X")            # next is picker's, not prompt's
    keys.check_bindings()                          # names are checked after the config
    err = capsys.readouterr().err
    assert "unknown action 'prompt.no_such_thing'" in err
    assert "unknown action 'no_such_thing'" in err
    assert "unknown action 'prompt.next'" in err


def test_bad_key_is_a_warning_and_the_rest_still_bind(capsys):
    keys.bind("prompt.history_search", ["Hyper-S", "Ctrl-S"])
    assert "unknown modifier" in capsys.readouterr().err
    assert keys.key_names("prompt.history_search") == ["Ctrl-S"]


def test_printable_key_is_refused(capsys):
    keys.bind("prompt.history_search", ["x", "Space"])
    err = capsys.readouterr().err
    assert "'x' is a printable key" in err and "'Space' is a printable key" in err
    assert keys.key_names("prompt.history_search") == []


def test_hint():
    assert keys.hint("switcher.new", "new") == "^N new"
    keys.bind("switcher.new", "Alt-N")
    assert keys.hint("switcher.new", "new") == "Alt-N new"
    keys.bind("switcher.new", [])
    assert keys.hint("switcher.new", "new") == ""


def test_listing_covers_every_surface():
    contexts = {a.context for a, _ in keys.listing()}
    assert contexts == set(keys.CONTEXTS)


# ── the prompt ───────────────────────────────────────────────────────────────

def test_rebound_key_drives_the_editor():
    keys.bind("prompt.backward_kill_line", "Alt-U")
    ed = _editor()
    _type(ed, "echo hi")
    ed._handle_key(b"\x15")                  # old Ctrl+U: now unbound
    assert ed._buf == "echo hi"
    ed._handle_key(b"\x1bu")
    assert ed._buf == ""


def test_delete_char():
    ed = _editor()
    _type(ed, "abc")
    ed._handle_key(b"\x1b[D")
    ed._handle_key(b"\x1b[D")
    ed._handle_key(b"\x1b[3~")
    assert (ed._buf, ed._cursor) == ("ac", 1)


def test_user_action_edits_the_buffer():
    keys.bind("insert_last_arg", "Alt-.")

    @keys.action("insert_last_arg")
    def insert_last_arg(ctx):
        ctx.insert(ctx.history[-1].split()[-1])

    ed = _editor(local=["cp a.txt backup/"])
    _type(ed, "ls ")
    ed._handle_key(b"\x1b.")
    assert (ed._buf, ed._cursor) == ("ls backup/", 10)


def test_context_buffer_cursor_and_replace():
    keys.bind("upcase_word", "Alt-U")

    @keys.action("upcase_word")
    def upcase_word(ctx):
        start = ctx.buffer.rfind(" ", 0, ctx.cursor) + 1
        ctx.replace(start, ctx.cursor, ctx.buffer[start:ctx.cursor].upper())

    ed = _editor()
    _type(ed, "git push")
    ed._handle_key(b"\x1bu")
    assert (ed._buf, ed._cursor) == ("git PUSH", 8)


def test_invoke_accept_finishes_the_line():
    keys.bind("sudo_accept", "Alt-S")

    @keys.action("sudo_accept")
    def sudo_accept(ctx):
        ctx.buffer = "sudo " + ctx.buffer
        ctx.invoke("accept")

    ed = _editor()
    _type(ed, "make install")
    assert ed._handle_key(b"\x1bs") == "sudo make install"


def test_override_wraps_the_builtin_through_invoke():
    calls = []

    @keys.action("kill_line", override=True)
    def kill_line(ctx):
        calls.append(ctx.buffer[ctx.cursor:])
        ctx.invoke("kill_line")                  # the built-in, not itself

    ed = _editor()
    _type(ed, "abc")
    ed._handle_key(b"\x01")                      # Ctrl+A
    ed._handle_key(b"\x0b")                      # Ctrl+K keeps its default key
    assert calls == ["abc"] and ed._buf == ""


def test_overriding_a_builtin_needs_override(capsys):
    keys.bind("kill_line", "Alt-K")

    @keys.action("kill_line")
    def kill_line(ctx):
        pass

    assert "pass override=True" in capsys.readouterr().err
    assert not keys.get_action("kill_line").is_user


def test_a_raising_action_is_reported_and_editing_goes_on(capsys):
    keys.bind("boom", "Alt-X")

    @keys.action("boom")
    def boom(ctx):
        raise RuntimeError("kaboom")

    ed = _editor()
    _type(ed, "ab")
    assert ed._handle_key(b"\x1bx") is None
    out = capsys.readouterr().out
    assert "key action 'boom' failed" in out and "kaboom" in out
    _type(ed, "c")
    assert ed._buf == "abc"


def test_invoke_unknown_action_raises():
    seen = []

    keys.bind("bad", "Alt-X")

    @keys.action("bad")
    def bad(ctx):
        try:
            ctx.invoke("nope")
        except ValueError as e:
            seen.append(str(e))

    _editor()._handle_key(b"\x1bx")
    assert seen == ["unknown key action 'nope'"]


def test_only_prompt_actions_can_be_defined(capsys):
    @keys.action("picker.mine")
    def mine(ctx):
        pass

    assert "only prompt.* actions" in capsys.readouterr().err


def test_choose_picks_from_a_list(monkeypatch):
    monkeypatch.setattr(InlinePicker, "run", lambda self: self._items[1])

    keys.bind("pick", "Alt-P")

    @keys.action("pick")
    def pick(ctx):
        choice = ctx.choose(["main", "dev"], title="branch")
        ctx.insert(choice)

    ed = _editor()
    _type(ed, "git checkout ")
    ed._handle_key(b"\x1bp")
    assert ed._buf == "git checkout dev"


# ── pickers and the forwarding loops ─────────────────────────────────────────

def test_picker_keys_follow_bindings():
    keys.bind("picker.next", ["Down", "Ctrl-J"])
    picker = InlinePicker(["a", "b"])
    assert picker._dispatch(b"\n") == "down"      # Ctrl-J moved off accept
    assert picker._dispatch(b"\r") == "accept"
    assert picker._dispatch(b"\x03") == "interrupt"


def test_switch_key_in_raw_input():
    from eosh.shell import _find_switch_key

    assert _find_switch_key(b"ab\x1dcd") == 2
    keys.bind("prompt.switch_context", "Alt-W")
    assert _find_switch_key(b"ab\x1dcd") == -1
    assert _find_switch_key(b"xyz\x1bw") == 3


def test_help_keys_lists_the_table(capsys):
    keys.bind("shout", "Alt-S")

    @keys.action("shout", help="upper-case the line")
    def shout(ctx):
        pass

    keys.bind("prompt.history_search", "Ctrl-S")
    Shell().registry.get("help").invoke(["keys"])
    out = capsys.readouterr().out
    assert "switcher:" in out
    assert "history_search           Ctrl-S" in out
    assert "shout *                  Alt-S" in out and "* from your config" in out


def test_a_binding_may_come_before_its_action():
    keys.bind("shout", "Alt-Z")                    # not defined yet: just recorded

    @keys.action("shout")
    def shout(ctx):
        pass

    keys.check_bindings()
    assert keys.key_names("prompt.shout") == ["Alt-Z"]


def test_a_user_action_has_no_keys_until_bound():
    @keys.action("shout")
    def shout(ctx):
        pass

    assert keys.key_names("prompt.shout") == []


def test_an_override_keeps_the_builtin_keys():
    @keys.action("kill_line", override=True)
    def kill_line(ctx):
        pass

    assert keys.key_names("prompt.kill_line") == ["Ctrl-K"]


def test_config_load_reports_a_binding_to_nothing(tmp_path, monkeypatch, capsys):
    import sys
    from eosh import shell as shell_mod

    home = tmp_path / ".eosh"
    home.mkdir()
    (home / "config.py").write_text(
        "from eosh import keys\n"
        "keys.bind('shout', 'Alt-Z')\n"
        "keys.bind('typo_action', 'Alt-Q')\n"
        "@keys.action('shout')\n"
        "def shout(ctx):\n"
        "    pass\n"
    )
    monkeypatch.setattr(shell_mod, "config_dir", lambda: home)
    monkeypatch.setattr(sys, "path", list(sys.path))
    _real_load_user_config(Shell.__new__(Shell))
    err = capsys.readouterr().err
    assert "unknown action 'typo_action'" in err and "shout" not in err
    assert keys.key_names("prompt.shout") == ["Alt-Z"]
