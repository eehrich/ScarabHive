"""stategraph_machine: a stategraph machine as an agent type.

The class lives next to the engine (``plugins.stategraph.facade``) so the coupling to
stategraph's internals stays inside one plugin; this folder is what ``type:`` names.
"""

from __future__ import annotations

from agent_system.plugins.factory_utils import make_agent_plugin_factory
from plugins.stategraph.facade import MachineAgent, offered_servers

PLUGIN_FACTORY = make_agent_plugin_factory(MachineAgent)
# a machine whose file has an agent: block is an agent without a config entry (Runtime.declare)
PLUGIN_FACTORY.offered_servers = offered_servers  # type: ignore[attr-defined]

__all__ = ["PLUGIN_FACTORY", "MachineAgent"]
