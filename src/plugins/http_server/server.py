from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn

from agent_system.mcp.base import MCPServer


class CallRequest(BaseModel):
    tool: str
    params: dict[str, Any] = {}


class HTTPServer(MCPServer):
    """HTTP Server MCP adapter that wraps other MCP servers with FastAPI REST endpoints."""

    def __init__(self, name: str, config: dict | None = None, ssl_verify: bool = True) -> None:
        super().__init__(name, config, ssl_verify=ssl_verify)
        self.config = config or {}
        self.host = self.config.get("host", os.getenv("HOST", "127.0.0.1"))
        self.port = self.config.get("port", int(os.getenv("PORT", "9000")))
        self.wrapped_server = None

    def wrap_server(self, server: MCPServer) -> None:
        """Wrap an MCP server to expose it via HTTP."""
        self.wrapped_server = server

    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        """Route calls to the wrapped MCP server."""
        if not self.wrapped_server:
            return {"error": "No server wrapped - use wrap_server() first"}

        return await self.wrapped_server.call(tool, params)

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAPI function schema for HTTP server."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "HTTP adapter for MCP servers - exposes wrapped MCP servers via REST API endpoints",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["health", "call"], "description": "HTTP server action"},
                        "tool": {"type": "string", "description": "MCP tool to call (when action is 'call')"},
                        "params": {"type": "object", "description": "Parameters for the MCP tool call"}
                    },
                    "required": ["action"],
                    "additionalProperties": True,
                },
            },
        }

    def get_default_action(self) -> str:
        """Return the default action for HTTP server."""
        return "health"

    def create_fastapi_app(self) -> FastAPI:
        """Create and return a FastAPI application."""
        if not self.wrapped_server:
            raise ValueError("No server wrapped - use wrap_server() first")

        app = FastAPI(title=f"MCP Server: {self.wrapped_server.name}")

        @app.get("/health")
        def health():
            return {"status": "ok", "server": self.wrapped_server.name}

        @app.post("/call")
        async def call(req: CallRequest):
            return await self.wrapped_server.call(req.tool, req.params)

        return app

    async def serve(self) -> None:
        """Start the HTTP server."""
        app = self.create_fastapi_app()
        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port
        )
        server_uvicorn = uvicorn.Server(config)
        await server_uvicorn.serve()
