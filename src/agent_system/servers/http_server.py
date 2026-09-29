from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn

from ..tools.base import ToolServer


class CallRequest(BaseModel):
    tool: str
    params: dict[str, Any] = {}


async def serve_tool_server(server: ToolServer, host: str | None = None, port: int | None = None) -> None:
    app = FastAPI(title=f"Tool server: {server.name}")

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
