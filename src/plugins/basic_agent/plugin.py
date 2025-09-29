"""Basic Agent plugin factory and configuration.

This module provides the plugin factory function that creates and configures
the basic agent plugin server. It demonstrates proper plugin initialization,
configuration handling, and dependency injection patterns for agent-based plugins.
"""

from __future__ import annotations
from .server import BasicAgent
from agent_system.plugins.factory_utils import make_agent_plugin_factory

# Einheitliches Pattern: generische Factory statt handgeschriebener Boilerplate.
PLUGIN_FACTORY = make_agent_plugin_factory(BasicAgent)

# Optional: Direkter Klassen-Export für seltene Sonderfälle / Tests.
BasicAgentServer = BasicAgent  # backward friendly alias

__all__ = ["PLUGIN_FACTORY", "BasicAgentServer", "BasicAgent"]