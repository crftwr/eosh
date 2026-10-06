"""``awsut bedrock-agentcore`` — Bedrock AgentCore resources.

Attached from inside ``cli.register()`` for the same reason ``sagemaker``
is: ``CommandRegistry._make_root`` *overwrites* an existing root, so only the
module that built the ``awsut`` root can add to it.
"""

from __future__ import annotations


def register_agentcore(awsut_root) -> None:
    """Attach the ``bedrock-agentcore`` group to the ``awsut`` command tree."""
    # Imported here rather than at module scope: these modules do
    # ``from .. import cli``, and ``cli.register()`` is what calls this.
    from eosh.variables import registry as var_registry
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
