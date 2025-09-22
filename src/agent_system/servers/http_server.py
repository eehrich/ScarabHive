from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn

from ..mcp.base import MCPServer


class CallRequest(BaseModel):
    tool: str
    params: dict[str, Any] = {}


async def serve_mcp_server(server: MCPServer, host: str | None = None, port: int | None = None) -> None:
    app = FastAPI(title=f"MCP Server: {server.name}")

    @app.get("/health")
    def health():
        return {"status": "ok", "server": server.name}

    @app.post("/call")
    async def call(req: CallRequest):
        return await server.call_with_status(req.tool, req.params)

    config = uvicorn.Config(
        app,
        host=host or os.getenv("HOST", "127.0.0.1"),
        port=port or int(os.getenv("PORT", "9000"))
    )
    server_uvicorn = uvicorn.Server(config)
    await server_uvicorn.serve()
