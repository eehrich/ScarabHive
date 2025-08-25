from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
import json
import os
import logging
import uvicorn

from ..config.loader import load_config
from ..mcp.base import MCPRegistry
from ..agent.core import Agent
from ..servers.bootstrap import bootstrap_servers
from ..utils.logging import setup_logging


app = FastAPI(title="Agent System (MCP)")
templates = Jinja2Templates(directory=str(Path(__file__).parents[3] / "templates"))

# Mount static directory for CSS/JS
static_path = str(Path(__file__).parents[3] / "static")
app.mount("/static", StaticFiles(directory=static_path), name="static")


def build_app(config_path: Optional[str] = None) -> FastAPI:
    cfg_path = config_path or str(Path(__file__).parents[3] / "config" / "agent.yaml")
    config = load_config(cfg_path)

    # Logging: truncate file each start; console INFO+, file per config
    log_file = setup_logging(config.logging.enabled, config.logging.level, config.logging.file)
    if log_file:
        logging.getLogger(__name__).info("Logging initialized, file=%s", log_file)

    # SSL verify off if configured
    if not config.network.ssl_verify:
        os.environ["PYTHONHTTPSVERIFY"] = "0"
        os.environ.setdefault("SSL_CERT_FILE", "")
        os.environ.setdefault("CURL_CA_BUNDLE", "")
        os.environ.setdefault("REQUESTS_CA_BUNDLE", "")

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
        logging.getLogger(__name__).info("/run invoked, task=%s", task)
        return await agent.run(task)

    @app.get("/events")
    async def events(task: str):
        logger = logging.getLogger(__name__)
        logger.info("SSE /events connected, task=%s", task)

        async def event_stream():
            # Initial keep-alive line
            yield ":ok\n\n"
            async for ev in agent.run_events(task):
                logger.debug("SSE event: %s", ev.get("type"))
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse("index.html", {"request": request})

    return app


def run() -> None:
    # Load config first to get logging and network settings
    from ..config.loader import load_config
    from pathlib import Path
    cfg_path = str(Path(__file__).parents[3] / "config" / "agent.yaml")
    config = load_config(cfg_path)
    
    # Build app (this sets up logging based on config)
    app_obj = build_app(cfg_path)
    
    # Try environment variables first, then config values, then hard defaults
    host = os.getenv("HOST") or config.network.host or "127.0.0.1"
    port_env = os.getenv("PORT")
    port = int(port_env) if port_env else int(getattr(config.network, "port", 8000))
    
    # Use config log level for uvicorn, convert to lowercase as uvicorn expects
    uvicorn_log_level = config.logging.level.lower() if config.logging.enabled else "info"
    
    # Create a complete log config based on uvicorn's default but adding our file handler
    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(levelprefix)s %(message)s",
                "use_colors": None,
            },
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            },
            "file": {
                "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
            },
            "access": {
                "formatter": "access",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
            "file": {
                "formatter": "file", 
                "class": "logging.FileHandler",
                "filename": config.logging.file,
                "mode": "a",  # Append mode since we already truncated in setup_logging
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default", "file"] if config.logging.enabled else ["default"], "level": uvicorn_log_level.upper()},
            "uvicorn.error": {"level": uvicorn_log_level.upper()},
            "uvicorn.access": {"handlers": ["access", "file"] if config.logging.enabled else ["access"], "level": uvicorn_log_level.upper(), "propagate": True},
        },
        "root": {
            "level": uvicorn_log_level.upper(),
            "handlers": ["default", "file"] if config.logging.enabled else ["default"],
        },
    }
    
    uvicorn.run(
        app, 
        host=host, 
        port=port, 
        log_level=uvicorn_log_level,
        log_config=log_config,
        access_log=config.logging.enabled,
        use_colors=False
    )


if __name__ == "__main__":
    run()
