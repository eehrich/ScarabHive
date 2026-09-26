"""stategraph_machine: a stategraph machine as an agent type.

The class lives next to the engine (``plugins.stategraph.facade``) so the coupling to
stategraph's internals stays inside one plugin; this folder is what ``type:`` names.
"""

from __future__ import annotations

from agent_system.plugins.factory_utils import make_agent_plugin_factory
from plugins.stategraph.facade import MachineAgent

PLUGIN_FACTORY = make_agent_plugin_factory(MachineAgent)

__all__ = ["PLUGIN_FACTORY", "MachineAgent"]
