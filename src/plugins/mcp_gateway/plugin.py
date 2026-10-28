"""mcp_gateway: tools of this installation for an outside CLI (Claude Code, Antigravity), over MCP.

``/plugins/<instance>/mcp`` speaks MCP's Streamable HTTP through the mcp SDK's own server (stateless, JSON
answers). The route has no login -- config/security.yaml opens it in both layers -- and lets nothing reach the SDK
without the bearer token of a live grant (grants.py). No grant is configured here: a caller in this process issues
one per run.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from agent_system.plugins.web_base import SchemaBasedPluginWebInterface

from . import grants
from .gateway import Gateway

logger = logging.getLogger(__name__)


def _bearer(header: Optional[str]) -> Optional[str]:
    scheme, _, value = (header or "").partition(" ")
    return (value.strip() or None) if scheme.lower() == "bearer" else None


class McpGatewayPlugin(SchemaBasedPluginWebInterface):
    def __init__(self, name, system_config, server_config):
        super().__init__(name, system_config, server_config)
        self.gateway = Gateway(name, str(getattr(server_config, "runner_agent", None) or "mcp_gateway_runner"),
                               max(1000, int(getattr(server_config, "max_result_chars", None) or 60000)))
        self._manager: Any = None
        self._ready: Optional[asyncio.Future] = None
        self._stop: Optional[asyncio.Event] = None
        self._holder: Optional[asyncio.Task] = None
        # Built in every process (agent-cli, a worker), served only by the app's: until the app has mounted the
        # route, a grant is refused -- a CLI handed one here would reach no gateway, or one that knows no token.
        self._mounted = False

    def router_mounted(self) -> None:
        """The app serves this instance's route (PluginWebRegistry.apply_to_app): grants may be issued."""
        self._mounted = True
        grants.serve(self.name, True)

    async def call(self, tool: Optional[str] = None, params: Optional[dict] = None, *args: Any, **kwargs: Any):
        """No tools: the gateway is for outside clients."""
        return {"status": "ok", "name": self.name, "type": "web_ui_only", "active": True}

    def _server(self) -> Any:
        from mcp.server.lowlevel import Server

        def grant_of(ctx: Any) -> grants.Grant:
            request = getattr(ctx, "request", None)
            grant = grants.lookup(self.name, _bearer(request.headers.get("authorization") if request else None))
            if grant is None:  # revoked or expired since the route let the request in
                logger.info("mcp_gateway %s: %s refused, the token was revoked or expired", self.name, ctx.method)
                raise PermissionError("the token of this run is no longer valid")
            return grant

        async def on_list_tools(ctx: Any, params: Any) -> Any:
            return await self.gateway.list_tools(grant_of(ctx))

        async def on_call_tool(ctx: Any, params: Any) -> Any:
            return await self.gateway.call_tool(grant_of(ctx), params.name, params.arguments)

        return Server(self.name, version="0.1.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool)

    async def _running(self) -> Any:
        """The SDK's session manager, its task group started on the first request and held until stop_plugin."""
        if self._ready is None:
            from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
            from mcp.server.transport_security import TransportSecuritySettings

            # No DNS-rebinding check: it guards a server a browser page could reach without a secret, and
            # nothing gets past the route here without the run's token.
            self._manager = StreamableHTTPSessionManager(
                app=self._server(), json_response=True, stateless=True,
                security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False))
            loop = asyncio.get_running_loop()
            self._ready, self._stop = loop.create_future(), asyncio.Event()

            async def hold() -> None:
                try:
                    async with self._manager.run():
                        self._ready.set_result(None)
                        await self._stop.wait()
                except BaseException as exc:
                    if not self._ready.done():
                        self._ready.set_exception(exc)
                    raise

            self._holder = loop.create_task(hold())
        manager = self._manager
        await asyncio.shield(self._ready)
        return manager if manager is self._manager else None  # None: stopped meanwhile

    def get_web_router(self) -> Any:
        plugin = self

        class Handover(Response):
            """Hands the request, body unread, to the SDK: a Response is an ASGI app, and FastAPI drops a route
            that is one."""

            def __init__(self, manager: Any):
                self.manager = manager
                self.background = None

            async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
                await self.manager.handle_request(scope, receive, send)

        router = APIRouter(prefix=f"/plugins/{self.name}", tags=[self.name])

        @router.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
        async def mcp(request: Request) -> Any:
            if grants.lookup(plugin.name, _bearer(request.headers.get("authorization"))) is None:
                return JSONResponse({"error": "a valid run token is required (Authorization: Bearer ...)"},
                                    status_code=401, headers={"WWW-Authenticate": "Bearer"})
            manager = await plugin._running()
            if manager is None:
                return JSONResponse({"error": "the gateway is stopping"}, status_code=503)
            return Handover(manager)

        return router

    async def start_plugin(self) -> None:
        if self._mounted:
            grants.serve(self.name, True)

    async def stop_plugin(self) -> None:
        """Stop serving; a start_plugin afterwards serves again with a new session manager (the SDK runs one
        only once)."""
        grants.serve(self.name, False)
        holder, stop = self._holder, self._stop
        self._manager = self._ready = self._stop = self._holder = None
        if stop is not None:
            stop.set()
        if holder is not None:
            try:
                await holder
            except Exception:  # noqa: BLE001 -- stopping: the manager's failure was logged when it happened
                logger.debug("mcp_gateway %s: session manager ended with an error", self.name, exc_info=True)


PLUGIN_FACTORY = McpGatewayPlugin
