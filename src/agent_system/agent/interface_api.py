from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import uvicorn

from ..config.loader import load_config
from ..mcp.base import MCPRegistry
from ..agent.core import Agent
from ..servers.bootstrap import bootstrap_servers


app = FastAPI(title="Agent System (MCP)")


def build_app(config_path: Optional[str] = None) -> FastAPI:
    cfg_path = config_path or str(Path(__file__).parents[3] / "config" / "agent.yaml")
    config = load_config(cfg_path)
    registry = MCPRegistry()
    bootstrap_servers(config, registry)
    agent = Agent(config, registry)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/config")
    def get_config():
        return config.model_dump()

    @app.post("/run")
    async def run(task: str):
        return await agent.run(task)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return """
                <html>
                <head><title>Agent System</title>
                <style>body{font-family:system-ui;margin:2rem;} input,button{font-size:1rem;padding:.4rem;}</style>
                </head>
                <body>
                <h2>Agent System (MCP)</h2>
                <form id="f" onsubmit="run(); return false;">
                    <input id="task" size="80" placeholder="Ask the agent..." />
                    <button>Run</button>
                </form>
                <pre id="out"></pre>
                <script>
                async function run(){
                    const task = document.getElementById('task').value;
                    const r = await fetch('/run?task=' + encodeURIComponent(task), {method:'POST'});
                    const j = await r.json();
                    document.getElementById('out').textContent = JSON.stringify(j, null, 2);
                }
                </script>
    </body></html>
    """

    return app


def run() -> None:
    build_app()
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    run()
