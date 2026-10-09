"""The pages the server serves outside the API, and what it says about itself.

GET /health (version, uptime, package versions), the web UI's pages (``/``,
``/login``, ``/status``), the status bus metrics (``/status/meta``) and the
favicon.

Three routers, so that build_app registers each where it always was among the
app's routes: /health first of them, the favicon last, the pages in between.
None of these paths overlaps another route, but the order is also the order of
the generated /docs page.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse

from agent_system import app_state
from agent_system.api.app_context import AppContext, app_context
from agent_system.tools.status import get_status_metrics
from agent_system.ui.resources import ui_templates
from agent_system.utils import yaml_io

logger = logging.getLogger(__name__)

templates = ui_templates()

#: GET /health -- the first of the app's own routes.
health_router = APIRouter()
#: The web UI's pages and the status bus metrics.
router = APIRouter()
#: GET /favicon.ico -- the last of the app's own routes.
favicon_router = APIRouter()


# Health check endpoint
@health_router.get("/health")
def health(ctx: AppContext = Depends(app_context)):
    uptime_seconds = time.time() - app_state.app_start_time if app_state.app_start_time else 0

    agent_config: dict = {}
    try:
        with open(ctx.config_path, 'r', encoding='utf-8') as f:  # the config this app was built from
            agent_config = yaml_io.safe_load(f) or {}
    except Exception as e:
        logger.debug(f"Failed to load config for health check: {e}")

    # Get Python version
    import sys
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    # Get key package versions
    packages = {}
    try:
        import fastapi
        import anthropic
        import openai
        import uvicorn
        import pydantic

        packages["fastapi"] = getattr(fastapi, "__version__", "unknown")
        packages["anthropic"] = getattr(anthropic, "__version__", "unknown")
        packages["openai"] = getattr(openai, "__version__", "unknown")
        packages["uvicorn"] = getattr(uvicorn, "__version__", "unknown")
        packages["pydantic"] = getattr(pydantic, "__version__", "unknown")

        # Try to get Google Gemini version
        try:
            from google import genai
            packages["google-genai"] = getattr(genai, "__version__", "unknown")
        except ImportError:
            pass

        # Try to get httpx version
        try:
            import httpx
            packages["httpx"] = getattr(httpx, "__version__", "unknown")
        except ImportError:
            pass

        # Try to get chromadb version
        try:
            import chromadb
            packages["chromadb"] = getattr(chromadb, "__version__", "unknown")
        except ImportError:
            pass

    except Exception as e:
        logger.debug(f"Failed to get package versions: {e}")

    return {
        "status": "ok",
        "version": agent_config.get("version", "unknown"),
        "name": agent_config.get("name", "AgentSystem"),
        "uptime_seconds": round(uptime_seconds, 2),
        "timestamp": datetime.now().isoformat(),
        "python_version": python_version,
        "packages": packages
    }


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, ctx: AppContext = Depends(app_context)):
    response = templates.TemplateResponse(request, "index.html")

    # Disable caching if configured
    if ctx.config.network.disable_cache:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"

    return response


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Login page for multi-user authentication"""
    response = templates.TemplateResponse(request, "login.html")

    # Disable caching for login page
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"

    return response


@router.get("/status", response_class=HTMLResponse)
async def status_page(request: Request):
    # Redirect to main page with integrated status
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/", status_code=302)


@router.get("/status/meta")
async def status_meta(request: Request):
    if os.getenv("AGENT_STATUS_REQUIRE_AUTH") == "1":
        expected = os.getenv("AGENT_STATUS_TOKEN", "")
        provided = request.headers.get("X-Status-Token", "")
        if not expected or provided != expected:
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Unauthorized")
    return get_status_metrics()


@favicon_router.get("/favicon.ico")
async def favicon():
    favicon_path = Path(__file__).parents[3] / "static" / "favicon.ico"
    if favicon_path.exists():
        return FileResponse(favicon_path)
    else:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Favicon not found")
