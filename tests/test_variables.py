"""Tests for variables.py — Var ABC, VarRegistry, EnvVar, VarCompleter."""

import os
from unittest.mock import patch

from eosh.completion import ChoiceCompleter, CompletionContext
import pytest

from eosh.variables import (
    EnvVar,
    Setting,
    Var,
    VarCompleter,
    VarRegistry,
    registry as var_registry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_ctx(prefix: str = "", args: list[str] | None = None) -> CompletionContext:
    return CompletionContext(
        command="var",
        args=args or [],
        arg_index=len(args) if args else 0,
        prefix=prefix,
        line=f"var {prefix}",
        shell_context=None,
    )


# ---------------------------------------------------------------------------
# EnvVar
# ---------------------------------------------------------------------------

class TestEnvVar:
    """Declarative: names the keys; the shell does the writing."""

    def test_get_reads_the_first_key(self):
        v = EnvVar("aws_region", keys=["AWS_REGION", "AWS_DEFAULT_REGION"])
        os.environ.pop("AWS_REGION", None)
        assert v.get() is None
        os.environ["AWS_REGION"] = "us-west-2"
        try:
            assert v.get() == "us-west-2"
        finally:
            os.environ.pop("AWS_REGION", None)

    def test_keys_accept_one_or_many_and_default_to_the_name(self):
        assert EnvVar("aws_profile", keys="AWS_PROFILE").keys == ("AWS_PROFILE",)
        assert EnvVar("r", keys=["A", "B"]).keys == ("A", "B")
        assert EnvVar("MY_VAR").keys == ("MY_VAR",)

    def test_no_keys_is_an_error(self):
        with pytest.raises(ValueError):
            EnvVar("x", keys=[])

    def test_it_has_no_writer_of_its_own(self):
        v = EnvVar("aws_profile", keys="AWS_PROFILE")
        assert not hasattr(v, "set") and not hasattr(v, "unset")

    def test_value_completer(self):
        c = ChoiceCompleter(["prod", "dev"])
        v = EnvVar("aws_profile", keys="AWS_PROFILE", completer=c)
        assert v.value_completer is c

    def test_description(self):
        v = EnvVar("x", description="my desc")
        assert v.description == "my desc"


class TestShellWrites:
    """Every environment write goes through the context manager, so an
    EnvVar is saved and restored per context like any `var` assignment."""

    @pytest.fixture
    def shell(self, monkeypatch):
        from eosh.shell import Shell
        monkeypatch.delenv("EOSH_T_A", raising=False)
        monkeypatch.delenv("EOSH_T_B", raising=False)
        reg_before = dict(var_registry._vars)
        var_registry.register(EnvVar("pair", keys=["EOSH_T_A", "EOSH_T_B"]))
        sh = Shell()
        yield sh
        var_registry._vars = reg_before
        for k in ("EOSH_T_A", "EOSH_T_B"):
            os.environ.pop(k, None)

    def test_every_key_is_written_and_restored_per_context(self, shell):
        cm = shell.context_manager
        shell._set_variable("pair", "one")
        assert os.environ["EOSH_T_A"] == os.environ["EOSH_T_B"] == "one"
        cm.new("other")
        cm.switch("other")
        shell._set_variable("pair", "two")
        cm.switch("default")
        assert os.environ["EOSH_T_A"] == os.environ["EOSH_T_B"] == "one"
        cm.switch("other")
        assert os.environ["EOSH_T_B"] == "two"

    def test_unset_removes_every_key(self, shell):
        shell._set_variable("pair", "one")
        shell._unset_variable("pair")
        assert "EOSH_T_A" not in os.environ and "EOSH_T_B" not in os.environ

    def test_a_plain_name_is_its_own_key(self, shell):
        shell._set_variable("EOSH_T_A", "plain")
        assert os.environ["EOSH_T_A"] == "plain"
        assert shell.context_manager.current().variables["EOSH_T_A"] == "plain"


# ---------------------------------------------------------------------------
# VarRegistry
# ---------------------------------------------------------------------------

class TestVarRegistry:
    def _make_registry(self) -> VarRegistry:
        return VarRegistry()

    def test_register_and_get(self):
        reg = self._make_registry()
        v = EnvVar("aws_profile", "AWS_PROFILE")
        reg.register(v)
        assert reg.get("aws_profile") is v

    def test_get_missing_returns_none(self):
        reg = self._make_registry()
        assert reg.get("nonexistent") is None

    def test_all_returns_in_registration_order(self):
        reg = self._make_registry()
        v1 = EnvVar("first", "FIRST")
        v2 = EnvVar("second", "SECOND")
        reg.register(v1)
        reg.register(v2)
        assert reg.all() == [v1, v2]

    def test_clear_user_vars_removes_non_builtins(self):
        reg = self._make_registry()
        builtin = EnvVar("builtin_var", "BUILTIN_VAR")
        reg.register(builtin)
        reg.mark_builtins()
        user = EnvVar("user_var", "USER_VAR")
        reg.register(user)
        reg.clear_user_vars()
        assert reg.get("builtin_var") is builtin
        assert reg.get("user_var") is None

    def test_register_overwrites_existing(self):
        reg = self._make_registry()
        v1 = EnvVar("aws_profile", "AWS_PROFILE")
        v2 = EnvVar("aws_profile", "AWS_PROFILE_NEW")
        reg.register(v1)
        reg.register(v2)
        assert reg.get("aws_profile") is v2


# ---------------------------------------------------------------------------
# VarCompleter
# ---------------------------------------------------------------------------

class TestVarCompleter:
    def _make_registry_and_completer(self):
        """Return a (VarRegistry, VarCompleter) pair sharing an isolated registry."""
        reg = VarRegistry()
        completer = VarCompleter()
        # Monkey-patch the module-level registry used inside VarCompleter.
        import eosh.variables as _vmod
        self._original_registry = _vmod.registry
        _vmod.registry = reg
        return reg, completer

    def teardown_method(self):
        import eosh.variables as _vmod
        if hasattr(self, "_original_registry"):
            _vmod.registry = self._original_registry

    def test_complete_name_prefix(self):
        reg, c = self._make_registry_and_completer()
        reg.register(EnvVar("aws_profile", "AWS_PROFILE", description="profile"))
        reg.register(EnvVar("aws_region", "AWS_REGION", description="region"))
        results = c.complete(make_ctx(prefix="aws_"))
        values = [r.value for r in results]
        assert "aws_profile=" in values
        assert "aws_region=" in values

    def test_complete_name_empty_prefix(self):
        reg, c = self._make_registry_and_completer()
        reg.register(EnvVar("aws_profile", "AWS_PROFILE"))
        results = c.complete(make_ctx(prefix=""))
        assert any(r.value == "aws_profile=" for r in results)

    def test_complete_name_no_match(self):
        reg, c = self._make_registry_and_completer()
        reg.register(EnvVar("aws_profile", "AWS_PROFILE"))
        # Use an isolated env so no system variable accidentally starts with "zz_unique_"
        with patch.dict(os.environ, {}, clear=True):
            results = c.complete(make_ctx(prefix="zz_unique_"))
        assert results == []

    def test_complete_name_includes_env_vars(self):
        reg, c = self._make_registry_and_completer()
        with patch.dict(os.environ, {"MY_TOKEN": "secret", "MY_OTHER": "val"}, clear=True):
            results = c.complete(make_ctx(prefix="MY_"))
        values = [r.value for r in results]
        assert "MY_TOKEN=" in values
        assert "MY_OTHER=" in values

    def test_complete_name_env_var_description_is_current_value(self):
        reg, c = self._make_registry_and_completer()
        with patch.dict(os.environ, {"MY_VAR": "hello"}, clear=True):
            results = c.complete(make_ctx(prefix="MY_"))
        match = next(r for r in results if r.value == "MY_VAR=")
        assert match.description == "hello"

    def test_complete_name_env_var_long_value_truncated(self):
        reg, c = self._make_registry_and_completer()
        long_val = "x" * 80
        with patch.dict(os.environ, {"MY_LONG": long_val}, clear=True):
            results = c.complete(make_ctx(prefix="MY_"))
        match = next(r for r in results if r.value == "MY_LONG=")
        assert match.description.endswith("…")
        assert len(match.description) < len(long_val)

    def test_complete_name_py_var_comes_before_env_var(self):
        reg, c = self._make_registry_and_completer()
        reg.register(EnvVar("aws_region", "AWS_REGION", description="region"))
        with patch.dict(os.environ, {"AWS_REGION": "us-east-1", "AWS_PROFILE": "prod"}, clear=True):
            results = c.complete(make_ctx(prefix=""))
        values = [r.value for r in results]
        # Python-backed var (lowercase) appears before env vars (uppercase)
        assert values.index("aws_region=") < values.index("AWS_PROFILE=")

    def test_complete_name_py_var_not_duplicated_by_env(self):
        # If a Python-backed var's name happens to match an env key, it should
        # appear only once (as the Python-backed entry, not again as env).
        reg, c = self._make_registry_and_completer()
        reg.register(EnvVar("MY_VAR", "MY_VAR", description="managed"))
        with patch.dict(os.environ, {"MY_VAR": "current"}, clear=True):
            results = c.complete(make_ctx(prefix="MY_"))
        assert sum(1 for r in results if r.value == "MY_VAR=") == 1
        match = next(r for r in results if r.value == "MY_VAR=")
        assert match.description == "managed"  # description from Var, not from env value

    def test_complete_value_delegates_to_value_completer(self):
        reg, c = self._make_registry_and_completer()
        regions = ["us-east-1", "us-west-2", "eu-west-1"]
        reg.register(EnvVar("aws_region", "AWS_REGION", completer=ChoiceCompleter(regions)))
        results = c.complete(make_ctx(prefix="aws_region=us-"))
        values = [r.value for r in results]
        assert "aws_region=us-east-1" in values
        assert "aws_region=us-west-2" in values
        assert "aws_region=eu-west-1" not in values  # doesn't match "us-" prefix

    def test_complete_value_prefixes_key(self):
        reg, c = self._make_registry_and_completer()
        reg.register(EnvVar("aws_region", "AWS_REGION", completer=ChoiceCompleter(["us-east-1"])))
        results = c.complete(make_ctx(prefix="aws_region="))
        assert results[0].value == "aws_region=us-east-1"
        assert results[0].display == "us-east-1"

    def test_complete_value_no_completer(self):
        reg, c = self._make_registry_and_completer()
        reg.register(EnvVar("aws_profile", "AWS_PROFILE"))  # no completer
        results = c.complete(make_ctx(prefix="aws_profile="))
        assert results == []

    def test_complete_value_unknown_key(self):
        reg, c = self._make_registry_and_completer()
        results = c.complete(make_ctx(prefix="UNKNOWN_VAR=val"))
        assert results == []

    def test_complete_empty_registry_returns_env_vars(self):
        _, c = self._make_registry_and_completer()
        with patch.dict(os.environ, {"SOME_VAR": "val"}, clear=True):
            results = c.complete(make_ctx(prefix=""))
        assert any(r.value == "SOME_VAR=" for r in results)

    def test_complete_empty_registry_and_empty_env(self):
        _, c = self._make_registry_and_completer()
        with patch.dict(os.environ, {}, clear=True):
            results = c.complete(make_ctx(prefix=""))
        assert results == []


# ---------------------------------------------------------------------------
# Custom Var subclass (integration)
# ---------------------------------------------------------------------------

class TestSetting:
    def _setting(self):
        class Mode(Setting):
            value: str = "auto"
            name = "mode"

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        return Mode()

    def test_the_shell_calls_set_and_unset_and_writes_no_environment(self, monkeypatch):
        from eosh.shell import Shell

        reg_before = dict(var_registry._vars)
        s = self._setting()
        var_registry.register(s)
        try:
            sh = Shell()
            env_before = dict(os.environ)
            sh._set_variable("mode", "fast")
            assert s.get() == "fast"
            sh._unset_variable("mode")
            assert s.get() == ""            # default unset() is set("")
            assert dict(os.environ) == env_before
            assert "mode" not in sh.context_manager.current().variables
        finally:
            var_registry._vars = reg_before

    def test_defaults(self):
        s = self._setting()
        assert s.value_completer is None
        assert s.description == ""


class TestRegistryAcceptsOnlyTheTwoKinds:
    def test_a_bare_var_subclass_is_refused(self):
        class Bare(Var):
            name = "bare"

            def get(self):
                return None

        with pytest.raises(TypeError, match="EnvVar .* or a Setting"):
            VarRegistry().register(Bare())
