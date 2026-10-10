import os
import tempfile

import pytest
from eosh.context import ContextManager


def test_create_and_current():
    cm = ContextManager()
    cm.create("prod")
    ctx = cm.current()
    assert ctx.name == "prod"


def test_switch():
    cm = ContextManager()
    cm.create("prod")
    cm.create("staging")
    cm.switch("staging")
    assert cm.current().name == "staging"
    cm.switch("prod")
    assert cm.current().name == "prod"


def test_switch_nonexistent():
    cm = ContextManager()
    with pytest.raises(KeyError):
        cm.switch("nope")


def test_closing_the_current_context_returns_to_the_last_one_used():
    cm = ContextManager()
    cm.create("prod")
    cm.create("staging")
    cm.create("dev")
    cm.switch("staging")
    cm.switch("prod")
    cm.new("tmp")
    cm.switch("tmp")
    cm.remove("tmp")
    assert cm.current_name == "prod"      # most recently used, not creation order
    cm.remove("prod")
    assert cm.current_name == "staging"


def test_removing_the_last_context_leaves_none():
    cm = ContextManager()
    cm.create("prod")
    cm.remove("prod")
    assert cm.current_name is None


def test_list_contexts():
    cm = ContextManager()
    cm.create("a")
    cm.create("b")
    cm.create("c")
    assert set(cm.list_contexts()) == {"a", "b", "c"}


def test_remove():
    cm = ContextManager()
    cm.create("prod")
    cm.create("staging")
    cm.switch("prod")
    cm.remove("staging")
    assert "staging" not in cm.list_contexts()


def test_remove_current():
    cm = ContextManager()
    cm.create("prod")
    cm.create("staging")
    cm.switch("prod")
    cm.switch("staging")
    cm.remove("staging")
    assert cm.current_name == "prod"


def test_rename():
    cm = ContextManager()
    cm.create("prod")
    cm.create("staging")
    cm.rename("staging", "stg")
    assert "stg" in cm.list_contexts()
    assert "staging" not in cm.list_contexts()
    assert cm.contexts["stg"].name == "stg"


def test_rename_current_updates_pointer():
    cm = ContextManager()
    cm.create("prod")
    cm.rename("prod", "production")
    assert cm.current_name == "production"
    assert cm.current().name == "production"


def test_rename_keeps_the_usage_order():
    cm = ContextManager()
    cm.create("a")
    cm.create("b")
    cm.create("c")
    cm.switch("b")
    cm.switch("c")
    cm.rename("b", "B")
    assert cm.list_contexts() == ["c", "B", "a"]
    cm.remove("c")
    assert cm.current_name == "B"


def test_rename_to_existing_raises():
    cm = ContextManager()
    cm.create("a")
    cm.create("b")
    with pytest.raises(ValueError):
        cm.rename("a", "b")


def test_rename_missing_raises():
    cm = ContextManager()
    cm.create("a")
    with pytest.raises(KeyError):
        cm.rename("nope", "x")


def test_rename_to_same_is_noop():
    cm = ContextManager()
    cm.create("a")
    cm.rename("a", "a")
    assert cm.list_contexts() == ["a"]


def test_set_get_variable():
    cm = ContextManager()
    cm.create("prod")
    cm.set_variable("REGION", "us-west-2")
    assert cm.get_variable("REGION") == "us-west-2"
    assert cm.get_variable("nonexistent") is None


def test_set_variable_updates_env():
    cm = ContextManager()
    os.environ.pop("EOSH_TEST_VAR", None)
    cm.create("prod")
    cm.set_variable("EOSH_TEST_VAR", "hello")
    assert os.environ.get("EOSH_TEST_VAR") == "hello"
    cm.remove("prod")
    os.environ.pop("EOSH_TEST_VAR", None)


def test_unset_variable():
    cm = ContextManager()
    os.environ.pop("EOSH_TEST_VAR", None)
    cm.create("prod")
    cm.set_variable("EOSH_TEST_VAR", "hello")
    cm.unset_variable("EOSH_TEST_VAR")
    assert cm.get_variable("EOSH_TEST_VAR") is None
    assert os.environ.get("EOSH_TEST_VAR") is None


def test_variables_saved_and_restored_on_switch():
    cm = ContextManager()
    os.environ.pop("EOSH_TEST_VAR", None)
    cm.create("prod")
    cm.set_variable("EOSH_TEST_VAR", "prod_val")
    cm.create("staging")
    cm.switch("staging")
    assert os.environ.get("EOSH_TEST_VAR") is None
    cm.set_variable("EOSH_TEST_VAR", "staging_val")
    cm.switch("prod")
    assert os.environ.get("EOSH_TEST_VAR") == "prod_val"
    cm.switch("staging")
    assert os.environ.get("EOSH_TEST_VAR") == "staging_val"
    os.environ.pop("EOSH_TEST_VAR", None)


def test_variables_restored_when_the_current_context_is_closed():
    cm = ContextManager()
    os.environ.pop("EOSH_TEST_VAR", None)
    cm.create("base")
    cm.set_variable("EOSH_TEST_VAR", "base_val")
    cm.create("child")
    cm.switch("child")
    assert os.environ.get("EOSH_TEST_VAR") is None
    cm.remove("child")
    assert os.environ.get("EOSH_TEST_VAR") == "base_val"
    os.environ.pop("EOSH_TEST_VAR", None)


def test_new_inherits_variables_and_history():
    cm = ContextManager()
    os.environ.pop("EOSH_TEST_VAR", None)
    cm.create("base", history=["make test"])
    cm.set_variable("EOSH_TEST_VAR", "base_val")
    child = cm.new("child")
    assert cm.current_name == "base"      # new() does not switch
    assert child.history == ["make test"]
    cm.switch("child")
    assert cm.current().variables.get("EOSH_TEST_VAR") == "base_val"
    assert os.environ.get("EOSH_TEST_VAR") == "base_val"
    os.environ.pop("EOSH_TEST_VAR", None)


def test_cwd_saved_and_restored_on_switch():
    original_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as dir_a, tempfile.TemporaryDirectory() as dir_b:
        real_a = os.path.realpath(dir_a)
        real_b = os.path.realpath(dir_b)

        os.chdir(real_a)
        cm = ContextManager()
        cm.create("ctx_a")  # current=ctx_a, cwd=real_a

        cm.create("ctx_b")  # ctx_b created with cwd=real_a
        cm.switch("ctx_b")  # saves ctx_a's cwd as real_a, switches to ctx_b
        os.chdir(real_b)    # now in ctx_b, move to real_b

        cm.switch("ctx_a")  # saves ctx_b's cwd as real_b, restores ctx_a -> real_a
        assert os.getcwd() == real_a

        cm.switch("ctx_b")  # saves ctx_a's cwd as real_a, restores ctx_b -> real_b
        assert os.getcwd() == real_b

        # Leave the temp dirs before the context manager cleans them up:
        # Windows refuses to remove a directory that is the process cwd.
        os.chdir(original_cwd)

    os.chdir(original_cwd)


def test_cwd_restored_when_the_current_context_is_closed():
    original_cwd = os.getcwd()
    try:
        with tempfile.TemporaryDirectory() as dir_a, tempfile.TemporaryDirectory() as dir_b:
            real_a = os.path.realpath(dir_a)
            real_b = os.path.realpath(dir_b)

            os.chdir(real_a)
            cm = ContextManager()
            cm.create("ctx_a")  # current=ctx_a, cwd=real_a

            cm.create("ctx_b")
            cm.switch("ctx_b")
            os.chdir(real_b)    # in ctx_b, move to real_b

            cm.remove("ctx_b")  # closing ctx_b restores ctx_a -> real_a
            assert os.getcwd() == real_a

            # Leave the temp dirs before the context manager cleans them up:
            # Windows refuses to remove a directory that is the process cwd.
            os.chdir(original_cwd)
    finally:
        os.chdir(original_cwd)


def test_create_with_history_snapshot():
    cm = ContextManager()
    cm.create("prod", history=["a", "b"])
    ctx = cm.contexts["prod"]
    assert ctx.history == ["a", "b"]


def test_history_snapshot_is_copied_not_shared():
    cm = ContextManager()
    seed = ["a", "b"]
    cm.create("prod", history=seed)
    # Mutating the source list must not leak into the context.
    seed.append("c")
    assert cm.contexts["prod"].history == ["a", "b"]
    # And the context's own list is independent of the source.
    cm.contexts["prod"].history.append("d")
    assert seed == ["a", "b", "c"]


def test_history_defaults_to_empty():
    cm = ContextManager()
    cm.create("prod")
    assert cm.contexts["prod"].history == []


def test_child_history_diverges_from_parent():
    cm = ContextManager()
    cm.create("parent", history=["shared"])
    parent = cm.contexts["parent"]
    cm.create("child", history=list(parent.history))
    child = cm.contexts["child"]
    parent.history.append("parent-only")
    child.history.append("child-only")
    assert parent.history == ["shared", "parent-only"]
    assert child.history == ["shared", "child-only"]


# ── `context` command: new / close ───────────────────────────────────────────

def test_context_new_and_close_commands(capsys):
    from eosh.shell import Shell

    original_cwd = os.getcwd()
    try:
        sh = Shell()
        cm = sh.context_manager
        sh.registry.get("context").invoke(["new", "review"])
        assert cm.current_name == "review"
        sh.registry.get("context").invoke(["close"])
        assert cm.current_name == "default"
        assert "review" not in cm.contexts
        out = capsys.readouterr().out
        assert "Created context 'review'" in out
        assert "Closed 'review', now in 'default'" in out
    finally:
        os.chdir(original_cwd)


def test_context_close_refuses_the_last_context(capsys):
    from eosh.shell import Shell

    sh = Shell()
    sh.registry.get("context").invoke(["close"])
    assert sh.context_manager.current_name == "default"
    assert "Cannot close the last context." in capsys.readouterr().out


def test_unsetting_an_inherited_variable_is_per_context(monkeypatch):
    monkeypatch.setenv("EOSH_TEST_BASE", "orig")
    cm = ContextManager()
    cm.create("a")
    cm.create("b")
    cm.switch("a")
    cm.unset_variable("EOSH_TEST_BASE")
    assert "EOSH_TEST_BASE" not in os.environ
    cm.switch("b")
    assert os.environ["EOSH_TEST_BASE"] == "orig"      # only "a" unset it
    assert cm.env_value_in(cm.contexts["a"], "EOSH_TEST_BASE") is None
    cm.switch("a")
    assert "EOSH_TEST_BASE" not in os.environ          # and it stays unset there


def test_environ_in_a_context_that_is_not_current(monkeypatch):
    monkeypatch.setenv("EOSH_TEST_BASE", "orig")
    cm = ContextManager()
    cm.create("a")
    cm.create("b")
    cm.switch("a")
    cm.set_variable("EOSH_TEST_VAR", "in-a")
    cm.unset_variable("EOSH_TEST_BASE")
    cm.switch("b")
    cm.set_variable("EOSH_TEST_VAR", "in-b")
    env_a = cm.environ_in(cm.contexts["a"])
    assert env_a["EOSH_TEST_VAR"] == "in-a"
    assert "EOSH_TEST_BASE" not in env_a
    assert cm.environ_in(cm.contexts["b"])["EOSH_TEST_BASE"] == "orig"
    os.environ.pop("EOSH_TEST_VAR", None)
