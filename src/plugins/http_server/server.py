from __future__ import annotations

import os
from typing import TYPE_CHECKING

from fastapi import FastAPI

from agent_system.servers.http_server import check_bind_safety, create_app, serve_tool_server
from agent_system.tools.base import ToolServer
from agent_system.tools.schema_based import SchemaBasedToolServer

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig


class HTTPServer(SchemaBasedToolServer):
    """Serves one wrapped tool server over HTTP (/health, /call). No agent tools of its own.

    The HTTP interface and its guards live in agent_system.servers.http_server,
    shared with the plugins that serve themselves from their __main__.
    """

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig) -> None:
        super().__init__(name, system_config, server_config)
        self.host = getattr(server_config, 'host', None) or "127.0.0.1"
        port = getattr(server_config, 'port', None)  # 0: a free port
        self.port = int(port if port is not None else os.getenv("PORT", "9000"))
        # Optional API key guarding /call. When unset, /call is only allowed on
        # a loopback bind (enforced in serve()) and for loopback Host headers.
        self.auth_key = (
            getattr(server_config, 'auth_key', None)
            or os.getenv("HTTP_SERVER_AUTH_KEY")
            or None
        )
        self.wrapped_server = None

    def wrap_server(self, server: ToolServer) -> None:
        """Wrap a tool server to expose it via HTTP."""
        self.wrapped_server = server

    def create_fastapi_app(self) -> FastAPI:
        """Create and return a FastAPI application."""
        if not self.wrapped_server:
            raise ValueError("No server wrapped - use wrap_server() first")
        return create_app(self.wrapped_server, self.auth_key)

    def _check_bind_safety(self) -> None:
        """Refuse to expose /call on a network interface without authentication."""
        check_bind_safety(self.host, self.auth_key)

    async def serve(self) -> None:
        """Start the HTTP server; runs until stopped."""
        if not self.wrapped_server:
            raise ValueError("No server wrapped - use wrap_server() first")
        self._check_bind_safety()
        await serve_tool_server(self.wrapped_server, self.host, self.port, self.auth_key)
