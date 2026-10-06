"""Tests for the sub-command tree mechanism."""

from eosh.commands import CommandRegistry, arg


# ── Tree construction ────────────────────────────────────────────────────────

def test_root_creates_node_with_no_handler():
    reg = CommandRegistry()
    aws = reg.command("aws", help="AWS CLI")
    assert aws.name == "aws"
    assert aws.func is None
    assert aws.children == {}


def test_subgroup_returns_node():
    reg = CommandRegistry()
    aws = reg.command("aws")
    s3 = aws.command("s3", help="Amazon S3")
    assert s3.name == "s3"
    assert s3.parent is aws
    assert "s3" in aws.children
    assert aws.children["s3"] is s3


def test_arbitrary_depth():
    reg = CommandRegistry()
    aws = reg.command("aws")
    instances = aws.command("ec2").command("instances")

    handler_called = []

    @instances.command("list", params=[arg("--filter", action="append")])
    def list_instances(filter=None):
        handler_called.append(filter)

    leaf = aws.children["ec2"].children["instances"].children["list"]
    assert callable(leaf.func)
    assert leaf.parent.name == "instances"
    assert leaf.parent.parent.name == "ec2"
    assert leaf.parent.parent.parent.name == "aws"
    leaf.func()
    assert handler_called == [None]


def test_decorator_attaches_handler():
    reg = CommandRegistry()
    aws = reg.command("aws")
    s3 = aws.command("s3")

    @s3.command("ls", params=[arg("path", nargs="?")])
    def s3_ls(path=None):
        return path

    assert s3.children["ls"].func is not None
    assert callable(s3.children["ls"].func)
    assert s3.children["ls"].func("hello") == "hello"


def test_redeclaration_updates_params():
    """First .command('foo') creates a group node, later .command('foo', params=...)
    populates that node — common pattern when sub-commands declared later."""
    reg = CommandRegistry()
    aws = reg.command("aws")
    s3 = aws.command("s3")
    s3.command("ls")              # group-only
    s3.command("ls", params=[arg("path")])  # later: add params
    leaf = s3.children["ls"]
    assert leaf.params is not None
    assert leaf.params[0].names == ("path",)


def test_external_tree_has_no_handlers():
    reg = CommandRegistry()
    aws = reg.command("aws")
    aws.command("s3").command("ls")
    assert aws.has_any_handler() is False


def test_python_tree_detected_via_handler():
    reg = CommandRegistry()
    aws = reg.command("aws")
    instances = aws.command("ec2").command("instances")

    @instances.command("list")
    def list_inst():
        return "ok"

    assert aws.has_any_handler() is True


# ── Resolution ───────────────────────────────────────────────────────────────

def test_resolve_walks_tree():
    reg = CommandRegistry()
    aws = reg.command("aws")
    aws.command("s3").command("ls")

    node, remaining = aws.resolve(["s3", "ls", "s3://bkt"])
    assert node.name == "ls"
    assert remaining == ["s3://bkt"]


def test_resolve_stops_at_unknown_token():
    reg = CommandRegistry()
    aws = reg.command("aws")
    aws.command("s3").command("ls")

    node, remaining = aws.resolve(["s3", "unknown", "tail"])
    assert node.name == "s3"
    assert remaining == ["unknown", "tail"]


def test_resolve_skips_boolean_flags():
    """Flags between/before subcommand names must not block the walk."""
    reg = CommandRegistry()
    aws = reg.command("aws", params=[arg("--debug", action="store_true")])
    aws.command("s3").command(
        "ls", params=[arg("--recursive", action="store_true")]
    )

    # Flag at the end
    node, _ = aws.resolve(["s3", "ls", "--recursive"])
    assert node.name == "ls"
    # Flag between
    node, _ = aws.resolve(["s3", "--debug", "ls"])
    assert node.name == "ls"
    # Flag at the front
    node, _ = aws.resolve(["--debug", "s3", "ls"])
    assert node.name == "ls"


def test_resolve_consumes_value_taking_flag_value():
    reg = CommandRegistry()
    aws = reg.command("aws", params=[arg("--region", metavar="R")])
    aws.command("s3").command("ls")

    node, remaining = aws.resolve(["--region", "us-east-1", "s3", "ls", "s3://bkt"])
    assert node.name == "ls"
    assert remaining == ["--region", "us-east-1", "s3://bkt"]


# ── Dispatch ─────────────────────────────────────────────────────────────────

def test_invoke_dispatches_to_leaf_handler():
    reg = CommandRegistry()
    captured = {}

    aws = reg.command("aws")
    s3 = aws.command("s3")

    @s3.command("ls", params=[arg("path", nargs="?")])
    def s3_ls(path=None):
        captured["path"] = path

    reg.get("aws").invoke(["s3", "ls", "s3://bkt"])
    assert captured == {"path": "s3://bkt"}


def test_a_leaf_receives_exactly_its_own_params():
    """Flags are never inherited: a shared flag is declared on each leaf."""
    reg = CommandRegistry()
    captured = {}
    region = arg("--region", metavar="R")

    s3 = reg.command("aws").command("s3")

    @s3.command("ls", params=[region, arg("path", nargs="?")])
    def s3_ls(path, region):
        captured.update(path=path, region=region)

    reg.get("aws").invoke(["s3", "ls", "--region", "us-east-1", "s3://bkt"])
    assert captured == {"path": "s3://bkt", "region": "us-east-1"}


def test_a_flag_declared_elsewhere_is_rejected(capsys):
    reg = CommandRegistry()
    called = []

    @reg.command("aws").command("s3").command("ls")
    def s3_ls():
        called.append(True)

    reg.get("aws").invoke(["s3", "ls", "--region", "x"])
    assert called == []
    assert "unrecognized arguments: --region" in capsys.readouterr().err


def test_a_node_cannot_have_both_a_handler_and_children():
    import pytest

    reg = CommandRegistry()

    @reg.command("tool")
    def tool():
        pass

    with pytest.raises(ValueError, match="has a handler"):
        reg.get("tool").command("sub")

    group = reg.command("grp")
    group.command("sub")
    with pytest.raises(ValueError, match="has sub-commands"):
        group(lambda: None)


def test_invoke_group_node_prints_help(capsys):
    reg = CommandRegistry()
    aws = reg.command("aws", help="AWS CLI")
    aws.command("s3", help="Amazon S3")
    aws.command("ec2", help="Amazon EC2")

    reg.get("aws").invoke([])
    out = capsys.readouterr().out
    assert "AWS CLI" in out
    assert "Subcommands" in out
    assert "s3" in out and "ec2" in out


def test_invoke_partial_path_prints_subgroup_help(capsys):
    reg = CommandRegistry()
    aws = reg.command("aws")
    s3 = aws.command("s3", help="S3 storage")
    s3.command("ls", help="list")
    s3.command("cp", help="copy")

    reg.get("aws").invoke(["s3"])
    out = capsys.readouterr().out
    assert "S3 storage" in out
    assert "ls" in out and "cp" in out


def test_external_tree_invoke_falls_through(capsys):
    """Trees with no handlers anywhere should not be invokable as Python.
    Shell wires this via has_any_handler() — direct invoke just prints help."""
    reg = CommandRegistry()
    aws = reg.command("aws", help="AWS CLI")
    aws.command("s3").command("ls")
    # has_any_handler is False, so shell.py routes to PTY; here we just verify
    # the predicate.
    assert reg.get("aws").has_any_handler() is False


# ── Completion: merged options ───────────────────────────────────────────────

def test_a_node_offers_only_its_own_flags():
    reg = CommandRegistry()
    aws = reg.command("aws", params=[arg("--region", metavar="R")])
    s3 = aws.command("s3")
    s3.command("ls", params=[arg("--recursive", action="store_true")])

    assert set(aws.children["s3"].children["ls"].options_completer().options) == {"--recursive"}
    assert aws.children["s3"].options_completer() is None
    assert set(aws.options_completer().options) == {"--region"}


def test_value_taking_flag_known_at_descendant():
    reg = CommandRegistry()
    aws = reg.command("aws", params=[arg("--region", metavar="R")])
    s3 = aws.command("s3")
    leaf = s3.command("ls", params=[arg("--page-size", metavar="N")])

    # When walking aws s3 ls --region <value>, the resolver must treat
    # --region as value-taking (defined at root).
    node, _ = aws.resolve(["s3", "ls", "--region", "us-east-1"])
    # The walk shouldn't have peeled off "us-east-1" as a positional.
    assert node.name == "ls"
