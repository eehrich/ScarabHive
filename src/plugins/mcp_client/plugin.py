"""mcp_client plugin entrypoint."""

from __future__ import annotations

from .server import MCPClientServer

PLUGIN_FACTORY = MCPClientServer
