from __future__ import annotations

import asyncio
from typing import Any

from agent_system.mcp.base import MCPServer


class DummyServer(MCPServer):
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        return {"tool": tool, "params": params}


def test_http_server_imports():
    # Ensure FastAPI wrapper is importable
    from agent_system.servers.http_server import serve_mcp_server  # noqa: F401

    # Ensure Dummy is usable with the wrapper type-wise
    s = DummyServer("dummy")
    assert s.name == "dummy"
