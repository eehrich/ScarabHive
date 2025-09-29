"""Web research agent plugin entrypoint (unified minimal pattern)."""

from __future__ import annotations

from .server import WebResearchAgent
from agent_system.plugins.factory_utils import make_agent_plugin_factory

# Generic factory (expects full AgentConfig via bootstrap like basic_agent now)
PLUGIN_FACTORY = make_agent_plugin_factory(WebResearchAgent)

# Legacy alias exports
WebResearchAgentServer = WebResearchAgent
__all__ = ["PLUGIN_FACTORY", "WebResearchAgentServer", "WebResearchAgent"]


