from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class Executor:
    """Executes MCP tool calls against a registry and returns results.

    This keeps validation and MCP invocation separate from planning.
    """

    def __init__(self, registry):
        self.registry = registry

    async def invoke(self, tool_name: str, params: Dict[str, Any]):
        if tool_name not in self.registry.list():
            raise RuntimeError(f"Unknown tool: {tool_name}")
        server = self.registry.get(tool_name)
        action_name = params.get("action") or server.get_default_action()
        try:
            result = await server.call(action_name, params)
            return result
        except Exception as e:
            logger.exception("Tool %s invocation failed: %s", tool_name, e)
            raise
