"""Per-context Up/Down history vs. global Ctrl-R / on-disk store.

Up/Down navigation is scoped to the current context's in-memory list; the
global History (on disk, and the Ctrl-R source) collects every command from
every context.
"""

from pathlib import Path

from eosh.lineedit import History, LineEditor, _norm_dir


def _make_editor(history, local_list):
    return LineEditor(
        history=history,
        get_completions=lambda line: ([], "", ""),
        get_prompt=lambda: "> ",
        local_history_fn=lambda: local_list,
    )


def test_add_to_history_updates_both_global_and_local(tmp_path):
    hist = History(tmp_path / "history")
    local: list[str] = []
    ed = _make_editor(hist, local)

    ed.add_to_history("cmd one")
    ed.add_to_history("cmd two")

    assert hist.entries == ["cmd one", "cmd two"]
    assert local == ["cmd one", "cmd two"]


def test_local_dedup_consecutive(tmp_path):
    hist = History(tmp_path / "history")
    local: list[str] = []
    ed = _make_editor(hist, local)

    ed.add_to_history("same")
    ed.add_to_history("same")
    assert local == ["same"]


def test_updown_reads_local_not_global(tmp_path):
    # Global store already has entries from other contexts...
    path = tmp_path / "history"
    path.write_text("global-a\nglobal-b\n")
    hist = History(path)
    # ...but this context's local list is separate.
    local = ["local-x", "local-y"]
    ed = _make_editor(hist, local)

    ed._buf = ""
    ed._hist_back()
    assert ed._buf == "local-y"
    ed._hist_back()
    assert ed._buf == "local-x"
    # No more local entries — does not fall through to the global store.
    ed._hist_back()
    assert ed._buf == "local-x"


def test_ctrl_r_source_is_global(tmp_path):
    # The Ctrl-R picker builds from self._history.entries (global), not local.
    path = tmp_path / "history"
    path.write_text("global-a\nglobal-b\n")
    hist = History(path)
    ed = _make_editor(hist, ["local-only"])

    assert ed._history.entries == ["global-a", "global-b"]


def test_no_local_fn_falls_back_to_global(tmp_path):
    path = tmp_path / "history"
    path.write_text("g1\ng2\n")
    hist = History(path)
    ed = LineEditor(
        history=hist,
        get_completions=lambda line: ([], "", ""),
        get_prompt=lambda: "> ",
    )
    ed._buf = ""
    ed._hist_back()
    assert ed._buf == "g2"


# ---------------------------------------------------------------------------
# Directory side table (history.dirs) — where each line was run
# ---------------------------------------------------------------------------

def test_add_records_the_directory_the_line_ran_in(tmp_path):
    hist = History(tmp_path / "history")

    hist.add("make test", cwd="/repo/a")

    assert hist.ran_here("make test", cwd="/repo/a")
    assert not hist.ran_here("make test", cwd="/repo/b")
    # A line nobody has run anywhere is not "from here" either.
    assert not hist.ran_here("make lint", cwd="/repo/a")


def test_directory_defaults_to_the_process_cwd(tmp_path, monkeypatch):
    hist = History(tmp_path / "history")
    monkeypatch.chdir(tmp_path)

    hist.add("make test")

    assert hist.ran_here("make test")


def test_directories_survive_a_restart(tmp_path):
    path = tmp_path / "history"
    History(path).add("make test", cwd="/repo/a")

    reloaded = History(path)

    assert reloaded.entries == ["make test"]
    assert reloaded.ran_here("make test", cwd="/repo/a")
    assert not reloaded.ran_here("make test", cwd="/repo/b")


def test_a_repeated_line_records_the_new_directory(tmp_path):
    """The line is a consecutive duplicate, the directory is not."""
    hist = History(tmp_path / "history")

    hist.add("make test", cwd="/repo/a")
    hist.add("make test", cwd="/repo/b")

    assert hist.entries == ["make test"]          # still deduplicated on disk
    assert hist.ran_here("make test", cwd="/repo/a")
    assert hist.ran_here("make test", cwd="/repo/b")
    assert History(path=tmp_path / "history").ran_here("make test", cwd="/repo/b")


def test_directory_list_per_line_is_capped(tmp_path):
    from eosh.lineedit import MAX_DIRS_PER_LINE

    hist = History(tmp_path / "history")
    for i in range(MAX_DIRS_PER_LINE + 5):
        hist.add("make test", cwd=f"/repo/d{i}")

    dirs = hist.dirs_for("make test")
    assert len(dirs) == MAX_DIRS_PER_LINE
    # The oldest directories are the ones dropped.
    assert not hist.ran_here("make test", cwd="/repo/d0")
    assert hist.ran_here("make test", cwd=f"/repo/d{MAX_DIRS_PER_LINE + 4}")


def test_repeating_a_directory_refreshes_rather_than_duplicates(tmp_path):
    hist = History(tmp_path / "history")

    hist.add("make test", cwd="/repo/a")
    hist.add("make lint", cwd="/repo/b")
    hist.add("make test", cwd="/repo/b")
    hist.add("make test", cwd="/repo/a")

    assert hist.dirs_for("make test") == [_norm_dir("/repo/b"), _norm_dir("/repo/a")]


def test_side_table_drops_lines_the_history_file_no_longer_has(tmp_path):
    """Hand-trimming ``history`` prunes ``history.dirs`` on the next write."""
    path = tmp_path / "history"
    hist = History(path)
    hist.add("make test", cwd="/repo/a")
    hist.add("make lint", cwd="/repo/a")

    path.write_text("make lint\n")                # user trimmed the file
    trimmed = History(path)
    assert trimmed.ran_here("make test", cwd="/repo/a")   # still in the side table
    trimmed.add("make docs", cwd="/repo/a")               # ... until the next save

    assert not History(path).ran_here("make test", cwd="/repo/a")
    assert History(path).ran_here("make lint", cwd="/repo/a")


def test_a_corrupt_side_table_is_ignored(tmp_path):
    path = tmp_path / "history"
    path.write_text("make test\n")
    (tmp_path / "history.dirs").write_text("{not json")

    hist = History(path)

    assert hist.entries == ["make test"]
    assert not hist.ran_here("make test", cwd="/repo/a")
