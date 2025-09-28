from __future__ import annotations

import os
from typing import Any


from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn

from agent_system.mcp.base import MCPServer
from agent_system.mcp.schema_based import SchemaBasedMCPServer


class CallRequest(BaseModel):
    tool: str
    params: dict[str, Any] = {}


class HTTPServer(SchemaBasedMCPServer):
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
        if tool == "http_server_ops":
            operation = params.get("operation")
            
            if operation == "health":
                # Check for cancellation before health check
                cancellation_token = params.get("_cancellation_token")
                if cancellation_token and cancellation_token.is_cancelled:
                    return {"error": "HTTP health check cancelled by user", "cancelled": True}

                if not self.wrapped_server:
                    return {"error": "No server wrapped - use wrap_server() first"}
                return {"status": "ok", "server": self.name}
            
            elif operation == "call":
                if not self.wrapped_server:
                    return {"error": "No server wrapped - use wrap_server() first"}
                    
                target_tool = params.get("tool")
                if not target_tool:
                    return {"error": "Tool name required for 'call' operation"}
                
                tool_params = params.get("params", {})
                return await self.wrapped_server.call(target_tool, tool_params)
            
            else:
                return {"error": f"Invalid operation: {operation}"}
        
        else:
            return {"error": f"Unknown tool: {tool}"}



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
            try:
                return await self.wrapped_server.call(req.tool, req.params)
            except Exception as e:
                return {
                    "error": f"Call failed: {str(e)}",
                    "tool": req.tool,
                    "params": req.params
                }

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
