import pytest
from eosh.commands import (
    Command, CommandRegistry, CmdParser, arg,
    _build_completers, _build_usage, _build_help_text,
)
from eosh.completion import ChoiceCompleter, OptionsCompleter


def test_register_and_get():
    reg = CommandRegistry()

    @reg.command(name="greet")
    def greet(name):
        return f"Hello, {name}"

    cmd = reg.get("greet")
    assert cmd is not None
    assert cmd.name == "greet"
    assert cmd.func("world") == "Hello, world"


def test_list_commands():
    reg = CommandRegistry()

    @reg.command(name="foo")
    def foo():
        pass

    @reg.command(name="bar")
    def bar():
        pass

    assert set(reg.list_commands()) == {"foo", "bar"}


def test_command_with_completers():
    reg = CommandRegistry()
    completer = ChoiceCompleter(["a", "b"])

    @reg.command(name="test", params=[arg("x", completer=completer)])
    def test_cmd(x):
        pass

    cmd = reg.get("test")
    assert 0 in cmd.completers
    assert cmd.completers[0] is completer


def _make_deploy_parser():
    p = CmdParser("deploy")
    p.add_argument("environment", choices=["prod", "staging", "dev"])
    p.add_argument("service", nargs="?", default="all")
    p.add_argument("-n", "--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-t", "--timeout", type=int, default=60)
    p.add_argument("-b", "--branch", default="main")
    return p


def test_cmd_parser_normal_parse():
    ns = _make_deploy_parser().parse_args(("prod", "api", "-v", "-t", "120"))
    assert ns is not None
    assert ns.environment == "prod"
    assert ns.service == "api"
    assert ns.verbose is True
    assert ns.dry_run is False
    assert ns.timeout == 120
    assert ns.branch == "main"


def test_cmd_parser_combined_short_flags():
    """Argparse must expand -nv into -n -v (matches eosh TUI output)."""
    ns = _make_deploy_parser().parse_args(("staging", "-nv"))
    assert ns is not None
    assert ns.dry_run is True
    assert ns.verbose is True


def test_cmd_parser_returns_none_on_error(capsys):
    ns = _make_deploy_parser().parse_args(("--unknown-flag",))
    assert ns is None
    err = capsys.readouterr().err
    assert "error" in err.lower()


def test_cmd_parser_returns_none_on_help(capsys):
    ns = _make_deploy_parser().parse_args(("--help",))
    assert ns is None
    out = capsys.readouterr().out
    assert "deploy" in out   # help text was printed


def test_cmd_parser_does_not_raise_system_exit():
    """Neither --help nor a parse error may propagate SystemExit."""
    try:
        _make_deploy_parser().parse_args(("--help",))
        _make_deploy_parser().parse_args(("--bad",))
    except SystemExit:
        pytest.fail("CmdParser raised SystemExit")


def test_params_dispatch_receives_parsed_kwargs():
    """Function must be called with typed keyword args, not raw *args."""
    reg = CommandRegistry()
    received = {}

    @reg.command(
        name="greet",
        params=[
            arg("name"),
            arg("-u", "--upper", action="store_true"),
            arg("-n", "--count", type=int, default=1),
        ],
    )
    def greet(name, upper, count):
        received.update(name=name, upper=upper, count=count)

    reg.get("greet").invoke(["world", "-u", "--count", "3"])
    assert received == {"name": "world", "upper": True, "count": 3}


def test_params_dispatch_combined_short_flags():
    reg = CommandRegistry()
    received = {}

    @reg.command(
        name="flags",
        params=[arg("-a", action="store_true"), arg("-b", action="store_true")],
    )
    def flags(a, b):
        received.update(a=a, b=b)

    reg.get("flags").invoke(["-ab"])
    assert received == {"a": True, "b": True}


def test_params_dispatch_returns_none_on_error(capsys):
    reg = CommandRegistry()
    called = []

    @reg.command(name="strict", params=[arg("required_arg")])
    def strict(required_arg):
        called.append(required_arg)

    reg.get("strict").invoke([])   # missing required arg
    assert not called              # function must NOT have been called
    assert "error" in capsys.readouterr().err.lower()


DEPLOY_PARAMS = [
    arg("environment", choices=["prod", "dev"]),
    arg("service", nargs="?", default="all"),
    arg("-n", "--dry-run", action="store_true", help="dry run"),
    arg("-t", "--timeout", type=int, default=60, metavar="SECONDS",
        help="timeout"),
]


# ── _build_usage ──────────────────────────────────────────────────────────────

def test_build_usage_required_positional():
    assert _build_usage("cmd", [arg("name")]) == "Usage: cmd <name>"


def test_build_usage_optional_positional():
    assert _build_usage("cmd", [arg("x", nargs="?")]) == "Usage: cmd [x]"


def test_build_usage_boolean_flag_short_form():
    usage = _build_usage("cmd", [arg("-n", "--dry-run", action="store_true")])
    assert "[-n]" in usage
    assert "--dry-run" not in usage   # compact: only short form


def test_build_usage_value_taking_flag_with_metavar():
    usage = _build_usage("cmd", [arg("-t", "--timeout", type=int, metavar="SECONDS")])
    assert "[-t SECONDS]" in usage


def test_build_usage_value_taking_flag_metavar_derived():
    # No metavar= → derived from --long-name → uppercased
    usage = _build_usage("cmd", [arg("-o", "--output")])
    assert "[-o OUTPUT]" in usage


def test_build_usage_full_deploy():
    usage = _build_usage("deploy", DEPLOY_PARAMS)
    assert usage == "Usage: deploy <environment> [service] [-n] [-t SECONDS]"


# ── _build_help_text ──────────────────────────────────────────────────────────

def _noop(): pass


def test_build_help_text_help_only():
    ht = _build_help_text("Do something.", _noop, "cmd", None)
    assert ht == "Do something."


def test_build_help_text_params_only():
    ht = _build_help_text(None, _noop, "cmd", [arg("name")])
    assert ht == "Usage: cmd <name>"


def test_build_help_text_help_and_params():
    ht = _build_help_text("Do something.", _noop, "cmd", [arg("name")])
    lines = ht.splitlines()
    assert lines[0] == "Do something."
    assert any("Usage:" in l for l in lines)


def test_build_help_text_first_line_is_description():
    """Command listing uses only the first line — it must be the description."""
    ht = _build_help_text("Short desc.", _noop, "deploy", DEPLOY_PARAMS)
    assert ht.split("\n")[0] == "Short desc."


def test_build_help_text_docstring_fallback():
    def func_with_doc():
        """Docstring description."""
    ht = _build_help_text(None, func_with_doc, "cmd", None)
    assert ht == "Docstring description."


def test_build_help_text_explicit_help_wins_over_docstring():
    def func_with_doc():
        """Should be ignored."""
    ht = _build_help_text("Explicit wins.", func_with_doc, "cmd", None)
    assert ht == "Explicit wins."


# ── registry.command(help=) integration ──────────────────────────────────────

def test_registry_help_param_stored():
    reg = CommandRegistry()

    @reg.command(name="greet", help="Say hello.")
    def greet(*args): pass

    assert reg.get("greet").help_text == "Say hello."


def test_registry_help_and_params_combined():
    reg = CommandRegistry()

    @reg.command(name="demo", help="Run demo.", params=[arg("x")])
    def demo(x): pass

    ht = reg.get("demo").help_text
    assert ht.startswith("Run demo.")
    assert "Usage: demo <x>" in ht


def test_registry_description_field_matches_help():
    reg = CommandRegistry()

    @reg.command(name="thing", help="Does a thing.")
    def thing(*args): pass

    assert reg.get("thing").description == "Does a thing."


def test_build_completers_positional_choices():
    comps = _build_completers([arg("env", choices=["prod", "staging"])])
    assert isinstance(comps[0], ChoiceCompleter)
    assert set(comps[0].choices) == {"prod", "staging"}
    assert None not in comps  # no flags → no OptionsCompleter


def test_build_completers_positional_explicit_completer():
    explicit = ChoiceCompleter(["a", "b"])
    comps = _build_completers([arg("x", completer=explicit)])
    assert comps[0] is explicit


def test_build_completers_positional_choices_overridden_by_completer():
    override = ChoiceCompleter(["x"])
    comps = _build_completers([arg("x", choices=["a", "b"], completer=override)])
    # explicit completer= wins over auto-derived ChoiceCompleter(choices)
    assert comps[0] is override


def test_build_completers_boolean_flags_in_options():
    comps = _build_completers([
        arg("-n", "--dry-run", action="store_true", help="dry run"),
        arg("-v", "--verbose", action="store_true", help="verbose"),
    ])
    oc = comps[None]
    assert isinstance(oc, OptionsCompleter)
    assert "-n" in oc.options and "--dry-run" in oc.options
    assert "-v" in oc.options and "--verbose" in oc.options
    # Boolean flags must NOT appear in args (they don't take a value)
    assert "-n" not in oc.args and "--dry-run" not in oc.args


def test_build_completers_value_taking_flags():
    val_compl = ChoiceCompleter(["30", "60"])
    comps = _build_completers([
        arg("-t", "--timeout", type=int, default=60, metavar="SECONDS",
            completer=val_compl),
        arg("-b", "--branch", default="main", metavar="BRANCH"),
    ])
    oc = comps[None]
    # Both flags in options dict
    assert "-t" in oc.options and "--timeout" in oc.options
    assert "-b" in oc.options and "--branch" in oc.options
    # Value-taking flags in args dict (OptionsCompleter unpacks tuple internally)
    assert oc.args["-t"] == "SECONDS"
    assert oc._value_completers["-t"] is val_compl
    assert oc.args["--timeout"] == "SECONDS"
    assert oc._value_completers.get("--timeout") is val_compl
    assert oc.args["-b"] == "BRANCH"       # plain string when no completer
    assert "-b" not in oc._value_completers


def test_build_completers_metavar_derived_from_long_name():
    # --output → dest = "output" → metavar = "OUTPUT" when metavar= not given
    comps = _build_completers([arg("-o", "--output", default="-")])
    oc = comps[None]
    assert oc.args["-o"] == "OUTPUT"


def test_registry_auto_derives_completers_from_params():
    reg = CommandRegistry()

    @reg.command(
        name="demo",
        params=[
            arg("env", choices=["prod", "dev"]),
            arg("-v", "--verbose", action="store_true", help="be loud"),
        ],
    )
    def demo(env, verbose):
        pass

    cmd = reg.get("demo")
    # Positional completer at index 0
    assert isinstance(cmd.completers[0], ChoiceCompleter)
    # OptionsCompleter under None key
    assert isinstance(cmd.completers[None], OptionsCompleter)
    assert "-v" in cmd.completers[None].options


def test_registry_explicit_completer_on_arg_overrides_choices():
    """arg(completer=) wins over the ChoiceCompleter auto-derived from choices=."""
    reg = CommandRegistry()
    override = ChoiceCompleter(["override"])

    @reg.command(
        name="over",
        params=[arg("x", choices=["auto"], completer=override)],
    )
    def over(x):
        pass

    assert reg.get("over").completers[0] is override


def test_no_params_backward_compat():
    """Commands without params= must still receive raw *args."""
    reg = CommandRegistry()
    received = []

    @reg.command(name="raw")
    def raw(*args):
        received.extend(args)

    reg.get("raw").invoke(["a", "b", "c"])
    assert received == ["a", "b", "c"]


def test_has():
    reg = CommandRegistry()

    @reg.command(name="exists")
    def exists():
        pass

    assert reg.has("exists")
    assert not reg.has("nope")


# ── Aliases ──────────────────────────────────────────────────────────────────

def test_alias_register_and_lookup():
    reg = CommandRegistry()
    reg.alias("hp", "awsut hyperpod")
    assert reg.get_alias("hp") == "awsut hyperpod"
    assert reg.list_aliases() == {"hp": "awsut hyperpod"}


def test_alias_unalias():
    reg = CommandRegistry()
    reg.alias("hp", "awsut hyperpod")
    assert reg.unalias("hp") is True
    assert reg.get_alias("hp") is None
    assert reg.unalias("missing") is False


def test_alias_overwrite():
    reg = CommandRegistry()
    reg.alias("hp", "awsut hyperpod")
    reg.alias("hp", "echo hello")
    assert reg.get_alias("hp") == "echo hello"


def test_alias_cleared_by_clear_user_commands_unless_builtin():
    reg = CommandRegistry()
    reg.alias("builtin_alias", "echo b")
    reg.mark_builtins()
    reg.alias("user_alias", "echo u")

    reg.clear_user_commands()
    assert reg.get_alias("builtin_alias") == "echo b"
    assert reg.get_alias("user_alias") is None


def test_list_aliases_returns_copy():
    reg = CommandRegistry()
    reg.alias("hp", "awsut hyperpod")
    snapshot = reg.list_aliases()
    snapshot["hp"] = "tampered"
    assert reg.get_alias("hp") == "awsut hyperpod"
