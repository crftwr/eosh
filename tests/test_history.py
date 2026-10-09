"""The shared SQLite history, the ghost suggestion, Ctrl+R and ``history``."""

from __future__ import annotations

import multiprocessing
import os

import pytest

from eosh.commands import registry as command_registry
from eosh.history import HistoryStore, norm_dir
from eosh.lineedit import LineEditor
from eosh.shell import Shell


@pytest.fixture
def store(tmp_path):
    s = HistoryStore(tmp_path / "history.db")
    yield s
    s.close()


# ── The store ───────────────────────────────────────────────────────────────

def test_add_records_where_and_finish_records_how(store, tmp_path):
    hid = store.add("make test", cwd=str(tmp_path), ctx="prod")
    store.finish(hid, 2, 1.5)
    [e] = store.entries()
    assert (e.cmd, e.cwd, e.ctx, e.status, e.duration) == (
        "make test", norm_dir(str(tmp_path)), "prod", 2, 1.5)


def test_blank_lines_are_not_recorded(store):
    assert store.add("   ") is None
    assert store.entries() == []


def test_suggest_is_the_latest_line_here_that_extends_the_prefix(store):
    store.add("git commit -m one", cwd="/repo/a")
    store.add("git commit -m two", cwd="/repo/a")
    store.add("git commit -m elsewhere", cwd="/repo/b")
    assert store.suggest("git co", "/repo/a") == "git commit -m two"
    assert store.suggest("git co", "/repo/b") == "git commit -m elsewhere"
    assert store.suggest("git co", "/repo/c") is None      # strictly this directory


def test_suggest_needs_something_longer_than_what_is_typed(store):
    store.add("ls", cwd="/r")
    assert store.suggest("ls", "/r") is None
    assert store.suggest("", "/r") is None
    assert store.suggest("  ", "/r") is None


def test_suggest_skips_multi_line_entries(store):
    store.add("echo a\necho b", cwd="/r")
    assert store.suggest("echo", "/r") is None


def test_suggest_handles_like_metacharacters_literally(store):
    store.add("grep 100%_done x", cwd="/r")
    store.add("grep 100xydone y", cwd="/r")
    assert store.suggest("grep 100%_", "/r") == "grep 100%_done x"


def test_recent_commands_collapse_immediate_repeats(store):
    for cmd in ["a", "b", "b", "c", "b"]:
        store.add(cmd, cwd="/r")
    assert store.recent_commands() == ["a", "b", "c", "b"]


def test_distinct_lists_each_line_once_at_its_latest_run(store):
    store.add("a", cwd="/one")
    store.add("b", cwd="/one")
    store.add("a", cwd="/two")
    assert [(e.cmd, e.cwd) for e in store.distinct()] == [
        ("a", norm_dir("/two")), ("b", norm_dir("/one"))]


def test_entries_filter_by_keywords_and_directory(store):
    store.add("docker run web", cwd="/a")
    store.add("docker ps", cwd="/a")
    store.add("Docker run db", cwd="/b")
    assert [e.cmd for e in store.entries(keywords=["docker", "RUN"])] == [
        "docker run web", "Docker run db"]
    assert [e.cmd for e in store.entries(cwd="/a")] == ["docker run web", "docker ps"]
    assert [e.cmd for e in store.entries(limit=1)] == ["Docker run db"]


def test_an_unopenable_database_degrades_to_memory(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    s = HistoryStore(blocker / "history.db")      # parent is a file
    s.add("still works", cwd="/r")
    assert [e.cmd for e in s.entries()] == ["still works"]


def _write_many(path: str, tag: str, n: int) -> None:
    s = HistoryStore(__import__("pathlib").Path(path))
    for i in range(n):
        hid = s.add(f"{tag} {i}", cwd="/r")
        s.finish(hid, 0, 0.0)
    s.close()


def test_concurrent_processes_keep_every_row(tmp_path):
    """Two eosh processes writing at once lose nothing — the reason the
    history moved from rewritten files to SQLite."""
    path = str(tmp_path / "history.db")
    HistoryStore(tmp_path / "history.db").close()        # create the schema first
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_write_many, args=(path, tag, 200)) for tag in "ab"]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    rows = HistoryStore(tmp_path / "history.db").entries(limit=1000)
    assert len(rows) == 400
    assert all(r.status == 0 for r in rows)


def test_a_line_from_another_process_is_visible_at_once(tmp_path):
    a = HistoryStore(tmp_path / "history.db")
    b = HistoryStore(tmp_path / "history.db")
    a.add("make deploy prod", cwd="/r")
    assert b.suggest("make d", "/r") == "make deploy prod"
    assert [e.cmd for e in b.distinct()] == ["make deploy prod"]


# ── The ghost suggestion in the line editor ────────────────────────────────

def _editor(suggest=None, history=None):
    ed = LineEditor(
        history=history or HistoryStore(None),
        get_completions=lambda line: ([], "", ""),
        get_prompt=lambda: "> ",
        suggest_fn=suggest,
    )
    ed._prompt_str, ed._prompt_len, ed._cols = "> ", 2, 80
    return ed


def test_the_ghost_is_drawn_dim_after_the_caret(capsys):
    ed = _editor(lambda buf: "git commit -m fix" if "git commit -m fix".startswith(buf) else None)
    ed._buf, ed._cursor = "git co", 6
    ed._redraw()
    out = capsys.readouterr().out
    assert "git co\x1b[2mmmit -m fix\x1b[22m" in out


def test_right_arrow_and_ctrl_e_accept_the_whole_line():
    for key in (b"\x1b[C", b"\x06", b"\x05", b"\x1b[F"):
        ed = _editor(lambda buf: "git commit -m fix")
        ed._buf, ed._cursor = "git co", 6
        ed._ghost_suffix(8)
        ed._handle_key(key, 0)
        assert (ed._buf, ed._cursor) == ("git commit -m fix", 17), key


def test_alt_f_accepts_one_word():
    ed = _editor(lambda buf: "git commit -m fix")
    ed._buf, ed._cursor = "git", 3
    ed._ghost_suffix(5)
    ed._handle_key(b"\x1bf", 0)
    assert ed._buf == "git commit"


def test_right_arrow_inside_the_line_just_moves():
    ed = _editor(lambda buf: "git commit")
    ed._buf, ed._cursor = "git co", 2
    ed._ghost_suffix(8)
    ed._handle_key(b"\x1b[C", 0)
    assert (ed._buf, ed._cursor) == ("git co", 3)


def test_no_ghost_away_from_the_end_or_on_a_continuation_line():
    ed = _editor(lambda buf: "git commit")
    ed._buf, ed._cursor = "git co", 3
    assert ed._ghost_suffix(8) == ""
    ed._cursor = 6
    ed._ghost_enabled = False
    assert ed._ghost_suffix(8) == ""


def test_the_ghost_is_cut_to_the_row_it_starts_on():
    ed = _editor(lambda buf: "x" + "y" * 200)
    ed._cols = 20
    ed._buf, ed._cursor = "x", 1
    assert ed._ghost_suffix(3) == "y" * 16          # 20 cols - 3 used - 1 spare
    ed._handle_key(b"\x1b[C", 0)
    assert ed._buf == "x" + "y" * 200               # accepting takes all of it


# ── Ctrl+R starts from what is typed ───────────────────────────────────────

def test_ctrl_r_opens_filtered_by_the_buffer(monkeypatch, capsys):
    import eosh.tui as tui

    store = HistoryStore(None)
    for cmd in ["git status", "git commit -m a", "make test"]:
        store.add(cmd, cwd="/r")
    seen = {}

    class Picker:
        def __init__(self, items, **kw):
            seen["items"] = [e.cmd for e in items]
            seen["typed"] = kw.get("typed")

        def run(self):
            return None

    monkeypatch.setattr(tui, "InlinePicker", Picker)
    ed = _editor(history=store)
    ed._buf, ed._cursor = "git", 3
    ed._history_search()
    assert seen == {"items": ["git commit -m a", "git status"], "typed": "git"}
    assert ed._buf == "git"                         # Esc restores the line
    capsys.readouterr()


# ── The shell: what gets recorded, and `history` ───────────────────────────

@pytest.fixture
def sh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shell = Shell()
    yield shell
    command_registry.clear_user_commands()


def test_a_line_is_recorded_with_its_context_and_status(sh):
    @command_registry.command("_t_fail", sync=True)
    def fail():
        return 3

    hid = sh._record_history("_t_fail")
    sh._execute("_t_fail", history_id=hid)
    [e] = sh._history.entries()
    assert (e.cmd, e.ctx, e.status) == ("_t_fail", "default", 3)
    assert e.cwd == norm_dir(os.getcwd())
    assert sh._current_context_history()[-1] == "_t_fail"


def test_the_suggestion_follows_the_directory(sh, tmp_path):
    sh._history.add("make deploy", cwd=str(tmp_path))
    assert sh._suggest("make d") == "make deploy"
    (tmp_path / "sub").mkdir()
    os.chdir(tmp_path / "sub")
    assert sh._suggest("make d") is None


def test_history_command_lists_and_filters(sh, tmp_path, capsys):
    sh._history.add("docker ps", cwd=str(tmp_path))
    hid = sh._history.add("docker run web", cwd="/elsewhere")
    sh._history.finish(hid, 1, 0.1)
    sh.registry.get("history").invoke(["run"])
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1 and out[0].endswith("  1  /elsewhere  docker run web")
    sh.registry.get("history").invoke(["--here"])
    assert capsys.readouterr().out.strip().endswith("docker ps")
