"""
MainAgent - Specialized agent for main API requests with dual status display.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from agent_system.servers.agent.server import Agent

logger = logging.getLogger(__name__)


class MainAgent(Agent):
    """Specialized Agent for main API requests with dual status display."""

    def __init__(self, name: str, config: Any, registry: Any, ssl_verify: bool = True):
        """Initialize MainAgent."""
        super().__init__(name, config, registry, None, ssl_verify)

    async def run_events(self, task: str, request_id: Optional[str] = None, session_id: Optional[str] = None):
        """
        Override run_events to publish operation status at the right time.
        This is called by the CLI instead of run().
        """
        
        # Now call the parent run_events method
        async for event in super().run_events(task, request_id=request_id, session_id=session_id):
            yield event

    async def run(self, task: str, request_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Run the agent with dual status display.
        This is called by the API and raw CLI mode.
        """
        # Run the actual agent task (this will publish LLM status with self.name)
        return await super().run(task, request_id=request_id)