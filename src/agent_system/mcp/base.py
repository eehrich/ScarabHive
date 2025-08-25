from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol


class MCPServer(ABC):
    name: str

    def __init__(self, name: str, config: dict | None = None, ssl_verify: bool = True) -> None:
        self.name = name
        self.config = config or {}
        self.ssl_verify = ssl_verify

    @abstractmethod
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        ...

    @abstractmethod
    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI function schema for this MCP server's tools."""
        ...

    @abstractmethod  
    def get_default_action(self) -> str:
        """Return the default action name for this MCP server."""
        ...


class MCPRegistry:
    def __init__(self) -> None:
        self._servers: dict[str, MCPServer] = {}

    def register(self, name: str, server: MCPServer) -> None:
        self._servers[name] = server

    def get(self, name: str) -> MCPServer:
        return self._servers[name]

    def list(self) -> list[str]:
        return list(self._servers.keys())
