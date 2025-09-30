"""Weather plugin entrypoint (standardized)."""

from __future__ import annotations

from agent_system.plugins.factory_utils import make_agent_plugin_factory
from .server import WeatherServer


# MODERN: Use standardized plugin factory - automatically handles AgentConfig
PLUGIN_FACTORY = make_agent_plugin_factory(WeatherServer)

