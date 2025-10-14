"""Web UI endpoints for context usage tracker plugin."""

import logging
from pathlib import Path
from typing import Dict, Any, List

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)


class ContextUsageWebFactory:
    """Web UI factory for context usage tracker."""
    
    def __init__(self, tracker):
        """
        Initialize web factory.
        
        Args:
            tracker: UsageTracker instance
        """
        self.tracker = tracker
        self.plugin_dir = Path(__file__).parent
        self.templates_dir = self.plugin_dir / "templates"
        self.templates = Jinja2Templates(directory=str(self.templates_dir))
    
    def get_web_router(self) -> APIRouter:
        """Get the FastAPI router for this plugin's web endpoints."""
        router = APIRouter(prefix="/plugins/context_usage_tracker")
        
        @router.get("/panel", response_class=HTMLResponse)
        async def get_panel(request: Request):
            """Render the context usage debug panel."""
            return self.render_panel(request)
        
        @router.get("/usage")
        async def get_usage():
            """Get current usage statistics."""
            latest = self.tracker.get_latest()
            agent_stats = self.tracker.get_agent_stats()
            statistics = self.tracker.get_statistics()
            
            return JSONResponse({
                "latest": latest or {},
                "agents": agent_stats,
                "statistics": statistics,
            })
        
        @router.get("/history")
        async def get_history(last_n: int = 100):
            """Get usage history."""
            history = self.tracker.get_history(last_n=last_n)
            return JSONResponse({"history": history})
        
        @router.post("/clear")
        async def clear_history():
            """Clear usage history."""
            self.tracker.clear_history()
            return JSONResponse({"status": "cleared"})
        
        return router
    
    def get_panels(self) -> List[Dict[str, Any]]:
        """Get panel definitions for this plugin."""
        return [
            {
                "id": "context_usage_tracker",
                "title": "Context Usage Debug",
                "icon": "📊",
                "endpoint": "/plugins/context_usage_tracker/panel",
                "type": "iframe",
                "default_height": 600,
            }
        ]
    
    def get_static_assets(self) -> Dict[str, Path]:
        """Get static assets for this plugin."""
        return {}
    
    def render_panel(self, request: Request) -> HTMLResponse:
        """Render the context usage debug panel."""
        return self.templates.TemplateResponse(
            "panel.html",
            {"request": request}
        )
