"""
TODO Management Plugin Factory

Exports PLUGIN_FACTORY for AgentSystem plugin discovery.
"""

from __future__ import annotations

from .server import TodoManagementServer


PLUGIN_FACTORY = TodoManagementServer
