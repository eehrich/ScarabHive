"""The external MCP client, as a plugin.

Talking to foreign MCP servers used to be welded into ``ToolServerIntegration`` in the
core, next to the plugin bootstrap that has nothing to do with it. This plugin
takes over that half: it owns the connections, speaks the current protocol
through the official SDK, and offers the foreign tools back to the agent core
through the ``external_tools`` capability.

Two roles in one object:

* an ordinary plugin, with management tools (list / connect / disconnect /
  tools), so the connections can be inspected and steered like anything else;
* an :class:`~agent_system.plugins.capabilities.ExternalToolProvider`, which is
  how the agent core gets at the foreign tools without knowing this plugin
  exists.

The agent core keeps its ``server.tool`` naming for foreign tools. That dot is
not cosmetic -- it is what the tool-permission layer matches on, and what the
runtime uses to decide where a call goes. Federating these tools under flat
names would silently widen what an agent may call, so the plugin hands out the
same shape the core has always seen.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, TYPE_CHECKING

from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.plugins import capabilities

from .connection import MCPConnectionError
from .elicitation import Elicitation, ElicitBroker
from .manager import ExternalServerPool
from .oauth import AUTHORIZE_WAIT_S, OAuthBroker

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)


def _positive_int(value: Any, default: int) -> int:
    """*value* as a positive int; anything else is *default*."""
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if number > 0 and not isinstance(value, bool) else default



#: network.host values that mean "every interface", not an address -> the loopback address the app is
#: reached on then (a socket on :: listens on IPv6 only: 127.0.0.1 does not reach it).
_ANY_ADDRESS = {"0.0.0.0": "127.0.0.1", "::": "[::1]", "[::]": "[::1]"}


def _positive_float(value: Any, default: float) -> float:
    """*value* as a positive number of seconds; anything else is *default*."""
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if number > 0 and number != float("inf") else default


class MCPClientServer(SchemaBasedToolServer):
    """Connects to external MCP servers and federates their tools."""

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig) -> None:
        super().__init__(name, system_config, server_config)

        network = getattr(system_config, "network", None)
        external = getattr(system_config, "external_servers", None)
        connection_cfg = getattr(external, "connection", None) if external else None
        cache_cfg = getattr(external, "cache", None) if external else None

        self.oauth = OAuthBroker(authorize_tool=f"{name}_authorize")
        host = str(getattr(network, "host", None) or "127.0.0.1")
        # Listening on every interface is no address to send a browser to -- an authorization server
        # refuses http://0.0.0.0 (only https or a loopback address), and no browser reaches it.
        host = _ANY_ADDRESS.get(host, host)
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"          # an IPv6 address goes in brackets in a URL
        port = getattr(network, "port", None) or 8000
        #: Where an authorization server sends the browser back (oauth.py); a server's own
        #: auth.redirect_uri wins -- the address the browser reaches this app on.
        self.default_redirect_uri = f"http://{host}:{port}{self.callback_path}"
        #: Authorizations running in the background, kept until they end (a task nobody holds is collected).
        self._authorizations: Dict[str, asyncio.Task] = {}
        #: A server's questions to the person while its tool runs (elicitation.py); off: not offered.
        self.questions = ElicitBroker()
        elicit = None
        if getattr(server_config, "elicitation", True) is not False:
            elicit = Elicitation(name, self.questions, self.answer_url,
                                 timeout=_positive_float(getattr(server_config, "elicitation_timeout", None), 300.0)).ask

        self.pool = ExternalServerPool(
            ssl_verify=getattr(network, "ssl_verify", True) if network else True,
            timeout=getattr(connection_cfg, "timeout", 30.0) if connection_cfg else 30.0,
            cache_ttl=getattr(cache_cfg, "tool_list_ttl", 300.0) if cache_cfg else 300.0,
            max_result_chars=_positive_int(getattr(server_config, "max_result_chars", None), 50000),
            max_description_chars=_positive_int(getattr(server_config, "max_description_chars", None), 1024),
            oauth=self.oauth, redirect_uri=self.default_redirect_uri, elicit=elicit,
        )
        self.pool.on_catalog_changed = self._catalog_changed
        if external is not None:
            self.pool.configure(getattr(external, "remote_servers", None) or {})

    @property
    def answer_url(self) -> str:
        return f"/plugins/{self.name}/answer"

    @property
    def callback_path(self) -> str:
        return f"/plugins/{self.name}/oauth/callback"

    def _catalog_changed(self) -> None:
        """A server changed its tools: the agent core lists them afresh (scheduled -- called from the
        connection's notification handler, which must not wait)."""
        capabilities.notify_tool_catalog_changed()   # schedules its async listeners and keeps them

    def get_web_router(self):
        from .web import build_router
        return build_router(self)

    # --------------------------------------------------------------- lifecycle

    async def start_plugin(self) -> None:
        """Announce the capability, then connect the enabled servers.

        Registration comes first on purpose: the core must be able to find the
        provider even when every server is unreachable, otherwise an outage
        would look like "no external tools are configured".
        """
        capabilities.register_provider(capabilities.EXTERNAL_TOOLS, self)

        if not self.pool.configured_servers:
            logger.info("No external MCP servers enabled")
            return

        results = await self.pool.connect_all()
        failed = {name: error for name, error in results.items() if error}
        logger.info(
            "External MCP servers: %d connected, %d failed, %d on demand",
            len(results) - len(failed), len(failed), len(self.pool.on_demand_servers()),
        )
        # Await the invalidation: the caller's next tool listing must see
        # the new catalog, not a cache that a scheduled task has not
        # cleared yet.
        await capabilities.anotify_tool_catalog_changed()

    async def stop_plugin(self) -> None:
        """Close every connection and withdraw the capability."""
        capabilities.unregister_provider(capabilities.EXTERNAL_TOOLS, self)
        for task in self._authorizations.values():
            task.cancel()                       # an authorization waiting for a browser ends with us
        await asyncio.gather(*self._authorizations.values(), return_exceptions=True)
        self._authorizations.clear()
        await self.pool.close_all()
        # Await the invalidation: the caller's next tool listing must see
        # the new catalog, not a cache that a scheduled task has not
        # cleared yet.
        await capabilities.anotify_tool_catalog_changed()

    # ------------------------------------------------- ExternalToolProvider role

    async def list_external_tools(self, *, force_refresh: bool = False) -> Dict[str, List[Dict[str, Any]]]:
        """``{server: [{name, description, input_schema, blocked}]}`` for the core.

        Blocked tools are left out: the core offers the model whatever it gets
        here and ignores the flag. The management tool still shows them.
        """
        by_server = await self.pool.list_tools_by_server(force_refresh=force_refresh)
        return {server: [t for t in tools if not t["blocked"]] for server, tools in by_server.items()}

    async def call_external_tool(self, server: str, tool: str, arguments: Dict[str, Any]) -> Any:
        """Route one call to one external server."""
        return await self.pool.call_tool(server, tool, arguments)

    async def connect_for_patterns(self, patterns: List[str]) -> None:
        """Connect the on_demand servers an agent's ``tools.allowed`` names.

        Named means ``<server>.*`` or ``<server>.<tool>`` -- the dot form that
        selects external tools, with the server spelled out. ``*`` or a glob
        over the server part does not count: an allow-all agent would
        otherwise start every on_demand server there is.
        """
        names = {pattern.split(".", 1)[0] for pattern in patterns if "." in pattern}
        if names and await self.pool.connect_on_demand(sorted(names)):
            await capabilities.anotify_tool_catalog_changed()

    # -------------------------------------------------------- management tools

    async def list_servers(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Tool: show the configured external MCP servers and their state."""
        pool_status = self.pool.status()
        connected = self.pool.list_connected()
        # NB: `status` here is the scope, `pool_status` the servers -- the two
        # used to share the name, which is why nothing was ever reported.
        scope = params.get("_status")
        if scope:
            await scope.end(
                f"{len(pool_status)} server(s) configured, {len(connected)} connected")
        return {
            "servers": list(pool_status.values()),
            "connected": connected,
            "total": len(pool_status),
        }

    async def connect(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Tool: connect (or reconnect) one configured server."""
        name = params.get("server")
        if not name:
            return {"success": False, "error": "Parameter 'server' is required"}
        try:
            connection = await self.pool.connect(name)
        except (MCPConnectionError, Exception) as e:  # noqa: B014 - report, never raise
            return {"success": False, "server": name, "error": str(e)}
        # Await the invalidation: the caller's next tool listing must see
        # the new catalog, not a cache that a scheduled task has not
        # cleared yet.
        await capabilities.anotify_tool_catalog_changed()
        scope = params.get("_status")
        if scope:
            await scope.end(
                f"Connected {name} (protocol {connection.protocol_version})")
        return {
            "success": True,
            "server": name,
            "server_info": connection.server_info,
            "protocol_version": connection.protocol_version,
        }

    async def authorize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Tool: start the OAuth authorization of one server and hand out the link a person opens."""
        name = params.get("server")
        scope = params.get("_status")
        if not name:
            return {"success": False, "error": "Parameter 'server' is required"}
        if name not in self.pool.configured_servers:
            return {"success": False, "server": name,
                    "error": f"External MCP server '{name}' is not configured or not enabled"}
        if not self.pool.uses_oauth(name):
            return {"success": False, "server": name,
                    "error": f"External MCP server '{name}' does not use auth type oauth"}
        connection = self.pool.get(name)
        if connection is not None and connection.connected:
            if scope:
                await scope.end(f"{name}: connected already")
            return {"success": True, "server": name, "message": "connected already; nothing to authorize"}
        running = self._authorizations.get(name)
        event = self.oauth.url_event(name)
        if running is None or running.done():
            event.clear()
            self.oauth.urls.pop(name, None)
            running = asyncio.create_task(self._authorize(name), name=f"mcp-authorize:{name}")
            # Its failure is logged in _authorize; read here, it is no "never retrieved" either.
            running.add_done_callback(lambda task: task.cancelled() or task.exception())
            self._authorizations[name] = running
        waiter = asyncio.create_task(event.wait())
        try:
            await asyncio.wait({running, waiter}, timeout=60, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        url = self.oauth.urls.get(name)
        if url and not running.done():
            if scope:
                await scope.end(f"{name}: waiting for authorization in a browser")
            return {"success": True, "server": name, "authorize_url": url,
                    "message": (f"Open authorize_url in a browser and allow access within "
                                f"{AUTHORIZE_WAIT_S / 60:.0f} minutes; the server connects then. The browser "
                                f"is sent back to {self.pool.redirect_uri_for(name)} -- it has to reach this app there, "
                                f"signed in under that very address.")}
        if running.done():
            error = running.exception()
            if error is None:
                if scope:
                    await scope.end(f"{name}: connected, the stored authorization still works")
                return {"success": True, "server": name, "message": "connected with the stored authorization"}
            return {"success": False, "server": name, "error": str(error)}
        return {"success": False, "server": name,
                "error": "the authorization server did not hand out a link within 60s; try again"}

    async def _authorize(self, name: str) -> None:
        try:
            await self.pool.connect(name, interactive=True)
        except Exception as e:
            logger.warning("MCP server '%s': authorization failed: %s", name, e)
            raise
        await capabilities.anotify_tool_catalog_changed()

    async def disconnect(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Tool: close one server's connection."""
        name = params.get("server")
        if not name:
            return {"success": False, "error": "Parameter 'server' is required"}
        closed = await self.pool.disconnect(name)
        # Await the invalidation: the caller's next tool listing must see
        # the new catalog, not a cache that a scheduled task has not
        # cleared yet.
        await capabilities.anotify_tool_catalog_changed()
        scope = params.get("_status")
        if scope:
            # Not connected is a legitimate ANSWER, not a failure -- end, not
            # error, and the central net in call_with_status leaves it alone
            # because the result carries no 'error' key.
            await scope.end(f"{name}: {'disconnected' if closed else 'was not connected'}")
        return {"success": closed, "server": name,
                "message": "disconnected" if closed else "was not connected"}

    async def tools(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Tool: list the tools of the connected servers."""
        all_by_server = await self.pool.list_tools_by_server(
            force_refresh=bool(params.get("force_refresh"))
        )
        server = params.get("server")
        by_server = {server: all_by_server.get(server, [])} if server else all_by_server
        total = sum(len(tools) for tools in by_server.values())
        scope = params.get("_status")
        if scope:
            if not server:
                await scope.end(f"{total} tool(s) on {len(by_server)} server(s)")
            elif server in all_by_server:
                # With a name given the dict is always size 1, so the old
                # "on 1 server(s)" said nothing -- and an unknown or
                # disconnected name read as a server that has no tools.
                await scope.end(f"{total} tool(s) on {server}")
            else:
                await scope.end(f"{server}: not connected")
        return {
            "servers": by_server,
            "total": total,
        }
