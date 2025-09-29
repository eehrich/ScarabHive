"""
MainAgent - Specialized agent for main API requests with dual status display.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from agent_system.servers.agent.server import Agent

logger = logging.getLogger(__name__)


class MainAgent(Agent):
    """Specialized Agent for main API requests with dual status display."""

    def __init__(self, name: str, config: Any, registry: Any, ssl_verify: bool = True):
        """Initialize MainAgent."""
        super().__init__(name, config, registry, None, ssl_verify)
