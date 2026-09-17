from __future__ import annotations

import hmac
import ipaddress
import os
from typing import Any, TYPE_CHECKING

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
import uvicorn

from agent_system.tools.base import ToolServer
from agent_system.tools.schema_based import SchemaBasedToolServer

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig


def _is_loopback_host(host: str) -> bool:
    """True if binding to ``host`` keeps the server on the local machine."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Not an IP literal (e.g. a hostname) — treat as non-loopback to be safe.
        return False


class CallRequest(BaseModel):
    tool: str
    params: dict[str, Any] = {}


class HTTPServer(SchemaBasedToolServer):
    """HTTP Server MCP adapter that wraps other tool servers with FastAPI REST endpoints."""

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig) -> None:
        super().__init__(name, system_config, server_config)
        # Extract config from ToolServerConfig object using getattr
        self.host = getattr(server_config, 'host', None) or os.getenv("HOST", "127.0.0.1")
        self.port = int(getattr(server_config, 'port', None) or os.getenv("PORT", "9000"))
        # Optional API key guarding the HTTP /call endpoint. When unset, /call is
        # only allowed to bind to a loopback host (enforced in serve()).
        self.auth_key = (
            getattr(server_config, 'auth_key', None)
            or os.getenv("HTTP_SERVER_AUTH_KEY")
            or None
        )
        self.wrapped_server = None

    def wrap_server(self, server: ToolServer) -> None:
        """Wrap a tool server to expose it via HTTP."""
        self.wrapped_server = server

    async def ops(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        HTTP server operations (health, call).
        
        Tool method - automatically called by generic dispatcher.
        Method name matches tool name in schema.yaml.
        """
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

    def create_fastapi_app(self) -> FastAPI:
        """Create and return a FastAPI application."""
        if not self.wrapped_server:
            raise ValueError("No server wrapped - use wrap_server() first")

        app = FastAPI(title=f"Tool server: {self.wrapped_server.name}")

        auth_key = self.auth_key

        async def _require_auth(
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
            authorization: str | None = Header(default=None),
        ) -> None:
            """Enforce the API key on /call when one is configured.

            Accepts either ``X-API-Key: <key>`` or ``Authorization: Bearer <key>``
            (both documented in the README). When no key is configured the
            endpoint is open — serve() guarantees that only happens on a loopback
            bind. Constant-time comparison avoids a timing oracle.
            """
            if not auth_key:
                return
            provided = x_api_key
            if not provided and authorization and authorization.lower().startswith("bearer "):
                provided = authorization[len("bearer "):].strip()
            if not provided or not hmac.compare_digest(
                provided.encode("utf-8"), auth_key.encode("utf-8")
            ):
                raise HTTPException(
                    status_code=401,
                    detail="Unauthorized: missing or invalid API key",
                )

        # /health stays open (liveness probe); /call requires auth when configured.
        @app.get("/health")
        def health():
            return {"status": "ok", "server": self.wrapped_server.name}

        @app.post("/call", dependencies=[Depends(_require_auth)])
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

    def _check_bind_safety(self) -> None:
        """Refuse to expose /call on a network interface without authentication.

        The /call endpoint proxies arbitrary tool calls to the wrapped server, so
        binding a non-loopback host without an API key would let any network
        client drive every wrapped tool unauthenticated.
        """
        if not _is_loopback_host(self.host) and not self.auth_key:
            raise RuntimeError(
                f"http_server refuses to bind non-loopback host '{self.host}' "
                "without authentication: the /call endpoint would let any network "
                "client invoke every wrapped tool. Set HTTP_SERVER_AUTH_KEY "
                "(or server_config.auth_key), or bind 127.0.0.1."
            )

    async def serve(self) -> None:
        """Start the HTTP server."""
        self._check_bind_safety()
        app = self.create_fastapi_app()
        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port
        )
        server_uvicorn = uvicorn.Server(config)
        await server_uvicorn.serve()
