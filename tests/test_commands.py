import pytest
from eosh.commands import (
    Command, CommandRegistry, CmdParser, arg,
    _build_usage, _build_help_text, options_completer, positional_completer,
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
    assert cmd.positional_completer(0) is completer
    assert cmd.positional_completer(1) is None


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

def test_build_usage_is_what_argparse_prints():
    """The usage line is argparse's own — the same text ``--help`` shows."""
    assert _build_usage("cmd", [arg("name")]) == "usage: cmd [-h] name"
    assert _build_usage("cmd", [arg("x", nargs="?")]) == "usage: cmd [-h] [x]"


def test_build_usage_flags_use_their_first_name_and_metavar():
    usage = _build_usage("cmd", [arg("-n", "--dry-run", action="store_true"),
                                 arg("-t", "--timeout", type=int, metavar="SECONDS"),
                                 arg("-o", "--output")])
    assert "[-n]" in usage and "--dry-run" not in usage
    assert "[-t SECONDS]" in usage
    assert "[-o OUTPUT]" in usage   # no metavar= → derived from --output


def test_build_usage_full_deploy():
    assert _build_usage("deploy", DEPLOY_PARAMS) == (
        "usage: deploy [-h] [-n] [-t SECONDS] {prod,dev} [service]")


# ── _build_help_text ──────────────────────────────────────────────────────────

def _noop(): pass


def test_build_help_text_help_only():
    ht = _build_help_text("Do something.", _noop, "cmd", None)
    assert ht == "Do something."


def test_build_help_text_params_only():
    ht = _build_help_text(None, _noop, "cmd", [arg("name")])
    assert ht == "usage: cmd [-h] name"


def test_build_help_text_without_a_handler_is_the_description_only():
    """A group or an external-tool recipe: the tool's own --help owns usage."""
    assert _build_help_text("list files", None, "ls", [arg("-l", action="store_true")]) == "list files"
    assert _build_help_text(None, None, "ls", [arg("path")]) == ""


def test_build_help_text_help_and_params():
    ht = _build_help_text("Do something.", _noop, "cmd", [arg("name")])
    lines = ht.splitlines()
    assert lines[0] == "Do something."
    assert any("usage:" in l for l in lines)


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
    assert "usage: demo [-h] x" in ht


def test_registry_description_field_matches_help():
    reg = CommandRegistry()

    @reg.command(name="thing", help="Does a thing.")
    def thing(*args): pass

    assert reg.get("thing").description == "Does a thing."


def test_positional_completer_from_choices():
    comp = positional_completer([arg("env", choices=["prod", "staging"])], 0)
    assert isinstance(comp, ChoiceCompleter)
    assert set(comp.choices) == {"prod", "staging"}
    assert options_completer([arg("env", choices=["prod"])]) is None   # no flags


def test_positional_completer_explicit_wins_over_choices():
    explicit = ChoiceCompleter(["x"])
    assert positional_completer([arg("x", choices=["a", "b"], completer=explicit)], 0) is explicit


def test_positional_completer_wildcard_serves_every_later_slot():
    rest = ChoiceCompleter(["f"])
    params = [arg("first", choices=["a"]), arg("rest", nargs="*", completer=rest)]
    assert isinstance(positional_completer(params, 0), ChoiceCompleter)
    assert positional_completer(params, 1) is rest
    assert positional_completer(params, 7) is rest
    assert positional_completer([arg("only")], 1) is None


def test_options_completer_boolean_flags():
    oc = options_completer([
        arg("-n", "--dry-run", action="store_true", help="dry run"),
        arg("-v", "--verbose", action="store_true", help="verbose"),
    ])
    assert isinstance(oc, OptionsCompleter)
    assert {"-n", "--dry-run", "-v", "--verbose"} <= set(oc.options)
    # Boolean flags must NOT appear in args (they don't take a value)
    assert "-n" not in oc.args and "--dry-run" not in oc.args


def test_options_completer_value_taking_flags():
    val_compl = ChoiceCompleter(["30", "60"])
    oc = options_completer([
        arg("-t", "--timeout", type=int, default=60, metavar="SECONDS",
            completer=val_compl),
        arg("-b", "--branch", default="main", metavar="BRANCH"),
        arg("-o", "--output", default="-"),
    ])
    assert oc.args["-t"] == oc.args["--timeout"] == "SECONDS"
    assert oc._value_completers["-t"] is val_compl
    assert oc._value_completers["--timeout"] is val_compl
    assert oc.args["-b"] == "BRANCH"       # plain string when no completer
    assert "-b" not in oc._value_completers
    assert oc.args["-o"] == "OUTPUT"       # metavar derived from --output


def test_registry_command_derives_completion_from_params():
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
    assert isinstance(cmd.positional_completer(0), ChoiceCompleter)
    assert "-v" in cmd.options_completer().options


def test_registry_command_needs_a_name():
    reg = CommandRegistry()
    with pytest.raises(TypeError):
        @reg.command
        def nameless():
            pass


def test_delegate_and_params_are_exclusive():
    reg = CommandRegistry()
    with pytest.raises(ValueError):
        reg.command("x", params=[arg("-v", action="store_true")],
                    delegate=ChoiceCompleter(["a"]))


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
