"""The pool of external MCP server connections.

This is the part that used to live in ``MCPIntegration`` as ``client_manager``
plus ``configured_external_servers``. It keeps the same observable behaviour --
including the two details that are easy to get wrong:

* Blocked tools are **not** dropped. They are handed on with ``blocked: True``
  and the consumer decides. Filtering them out here would silently change what
  the tool-permission layer above sees.
* A tool list is cached with a TTL, keyed by a hash over the servers' urls and
  their blocked lists, so a config change invalidates it by itself.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional

from .connection import MCPConnectionError, ServerConnection

logger = logging.getLogger(__name__)


class ExternalServerPool:
    """Owns every live connection to an external MCP server."""

    def __init__(self, *, ssl_verify: bool = True, timeout: float = 30.0, cache_ttl: float = 300.0) -> None:
        self.ssl_verify = ssl_verify
        self.timeout = timeout
        self.cache_ttl = cache_ttl

        #: name -> RemoteMCPConfig, only the ENABLED ones (same as before).
        self.configured_servers: Dict[str, Any] = {}
        self._connections: Dict[str, ServerConnection] = {}
        self._lock = asyncio.Lock()

        self._tools_cache: Optional[Dict[str, List[Dict[str, Any]]]] = None
        self._tools_cache_at: float = 0.0
        self._tools_cache_key: str = ""
        #: Last successful listing per server. Deliberately NOT the TTL cache:
        #: invalidating the cache must not also throw away what we know, or a
        #: refresh that happens to hit a slow server would report it tool-less.
        self._last_seen: Dict[str, List[Dict[str, Any]]] = {}

    # ------------------------------------------------------------------ config

    def configure(self, servers: Dict[str, Any]) -> None:
        """Take the enabled servers from ``external_servers.remote_servers``."""
        self.configured_servers = {
            name: cfg for name, cfg in (servers or {}).items() if getattr(cfg, "enabled", False)
        }
        skipped = len(servers or {}) - len(self.configured_servers)
        logger.info(
            "External MCP servers configured: %d enabled, %d disabled",
            len(self.configured_servers), skipped,
        )
        self.invalidate_cache()

    # -------------------------------------------------------------- connecting

    async def connect_all(self) -> Dict[str, Optional[str]]:
        """Connect every enabled server. Returns name -> error (None if fine).

        One unreachable server must not stop the others, and it must not stop
        startup either -- the result is reported, not raised.
        """
        results: Dict[str, Optional[str]] = {}
        for name in list(self.configured_servers):
            try:
                await self.connect(name)
                results[name] = None
            except Exception as e:
                results[name] = str(e)
                logger.warning("Could not connect external MCP server '%s': %s", name, e)
        return results

    async def connect(self, name: str) -> ServerConnection:
        """Connect (or reconnect) one configured server."""
        config = self.configured_servers.get(name)
        if config is None:
            raise MCPConnectionError(f"External MCP server '{name}' is not configured or not enabled")

        async with self._lock:
            existing = self._connections.get(name)
            if existing is not None and existing.connected:
                return existing
            if existing is not None:
                await existing.stop()

            connection = ServerConnection(
                name, config, ssl_verify=self.ssl_verify, timeout=self.timeout,
            )
            await connection.start()
            self._connections[name] = connection

        self.invalidate_cache()
        return connection

    async def disconnect(self, name: str) -> bool:
        """Close one server's connection. False if there was nothing to close."""
        async with self._lock:
            connection = self._connections.pop(name, None)
        if connection is None:
            return False
        await connection.stop()
        self._last_seen.pop(name, None)
        self.invalidate_cache()
        return True

    async def close_all(self) -> None:
        """Close everything. Never raises -- this runs during shutdown."""
        async with self._lock:
            connections = list(self._connections.values())
            self._connections.clear()
        for connection in connections:
            try:
                await connection.stop()
            except Exception as e:
                logger.debug("Error closing MCP server '%s': %s", connection.name, e)
        self._last_seen.clear()
        self.invalidate_cache()

    # ----------------------------------------------------------------- lookups

    def get(self, name: str) -> Optional[ServerConnection]:
        return self._connections.get(name)

    def list_connected(self) -> List[str]:
        return [name for name, c in self._connections.items() if c.connected]

    # ------------------------------------------------------------------- tools

    def _cache_key(self) -> str:
        """Hash url + blocked list per server, so config edits drop the cache."""
        material = {
            name: {
                "url": getattr(cfg, "url", None),
                "blocked": sorted((getattr(cfg, "tools", None).blocked or []) if getattr(cfg, "tools", None) else []),
            }
            for name, cfg in sorted(self.configured_servers.items())
        }
        material["_connected"] = sorted(self.list_connected())  # type: ignore[assignment]
        return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()[:16]

    def invalidate_cache(self) -> None:
        self._tools_cache = None
        self._tools_cache_at = 0.0
        self._tools_cache_key = ""

    async def list_tools_by_server(self, *, force_refresh: bool = False) -> Dict[str, List[Dict[str, Any]]]:
        """Tool lists per server, with ``blocked`` marked (never removed)."""
        key = self._cache_key()
        fresh = (
            not force_refresh
            and self._tools_cache is not None
            and self._tools_cache_key == key
            and (time.monotonic() - self._tools_cache_at) < self.cache_ttl
        )
        if fresh:
            return self._tools_cache  # type: ignore[return-value]

        result: Dict[str, List[Dict[str, Any]]] = {}
        complete = True
        for name, connection in list(self._connections.items()):
            if not connection.connected:
                continue
            try:
                tools = await connection.list_tools()
            except Exception as e:
                logger.warning("Could not list tools of MCP server '%s': %s", name, e)
                # Keep what we knew about this server rather than reporting it
                # as tool-less: a single slow answer would otherwise erase a
                # healthy server from the agents' catalogue.
                complete = False
                previous = self._last_seen.get(name)
                if previous is not None:
                    result[name] = previous
                continue

            config = self.configured_servers.get(name)
            tool_config = getattr(config, "tools", None) if config else None
            blocked = set(tool_config.blocked or []) if tool_config else set()

            result[name] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                    # Marked, NOT filtered: the permission layer above decides.
                    "blocked": tool.name in blocked,
                }
                for tool in tools
            ]
            self._last_seen[name] = result[name]

        # Only a complete answer is worth caching. Caching a partial one would
        # freeze the gap in for a whole TTL (an hour, with the shipped config)
        # even though the next attempt might well succeed.
        if complete:
            self._tools_cache = result
            self._tools_cache_at = time.monotonic()
            self._tools_cache_key = key
        return result

    async def call_tool(self, server_name: str, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Call a tool on one server, honouring that server's blocked list."""
        config = self.configured_servers.get(server_name)
        tool_config = getattr(config, "tools", None) if config else None
        if tool_config and tool_name in (tool_config.blocked or []):
            raise PermissionError(f"Tool '{tool_name}' is blocked on MCP server '{server_name}'")

        connection = self._connections.get(server_name)
        if connection is None or not connection.connected:
            raise MCPConnectionError(f"External MCP server '{server_name}' is not connected")
        return await connection.call_tool(tool_name, arguments)

    # ------------------------------------------------------------------ status

    def status(self) -> Dict[str, Dict[str, Any]]:
        """Per-server view for /mcp/status and the CLI."""
        out: Dict[str, Dict[str, Any]] = {}
        for name, config in self.configured_servers.items():
            connection = self._connections.get(name)
            out[name] = {
                "name": name,
                "description": getattr(config, "description", None) or name.replace("_", " ").title(),
                "url": getattr(config, "url", "") or "",
                "transport": getattr(config, "transport", None),
                "enabled": True,
                "connected": bool(connection and connection.connected),
                "server_info": connection.server_info if connection else {},
                "protocol_version": connection.protocol_version if connection else None,
            }
        return out
