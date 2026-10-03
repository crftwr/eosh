"""``awsut bedrock-agentcore`` — Bedrock AgentCore resources.

A private subpackage rather than a second recipe module, for the same two
reasons ``_awsut_sagemaker`` is one:

* ``recipes/_discover_all_recipes()`` globs ``recipes/*.py`` and calls
  ``register()`` on every match, so a sibling module would be picked up by
  ``enable("*")`` as a recipe in its own right.  Directories are not globbed.
* ``CommandRegistry._make_root`` *overwrites* an existing root, so a second
  module cannot re-open the ``awsut`` root without discarding the tree
  ``awsut.py`` already built.  The group is therefore registered from inside
  ``awsut.register()``, which owns that root.

The entry point is deliberately **not** named ``register`` — see the first
point above.

The group name is the service's own (``bedrock-agentcore``), not a shortened
``agentcore``: the two boto3 services behind it are ``bedrock-agentcore`` and
``bedrock-agentcore-control``, and a command tree that renames what it wraps
makes the mapping back to the API something to remember rather than to read.
"""

from __future__ import annotations


def register_agentcore(awsut_root) -> None:
    """Attach the ``bedrock-agentcore`` group to the ``awsut`` command tree."""
    # Imported here rather than at module scope: these modules do
    # ``from .. import awsut``, and ``awsut.register()`` is what calls this.
    from ...variables import registry as var_registry
    from .harness import register_harness
    from .memory import register_memory
    from .render import ControlEndpointVar, DataEndpointVar

    agentcore = awsut_root.command(
        "bedrock-agentcore",
        help="Bedrock AgentCore resources — harnesses (with their versions and "
             "endpoints) and memories (with the actors, sessions, events and "
             "extracted records they hold)",
    )
    register_harness(agentcore)
    register_memory(agentcore)

    # One Var per plane, since a memory is reached through both.
    var_registry.register(ControlEndpointVar())
    var_registry.register(DataEndpointVar())
