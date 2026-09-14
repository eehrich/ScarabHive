"""Web UI endpoints for context usage tracker plugin."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from agent_system.plugins.schema_router import create_schema_router
from agent_system.ui.resources import ui_templates


class ContextUsageWebFactory:
    """Web UI factory for context usage tracker."""

    def __init__(self, server):
        """
        Initialize web factory.

        Args:
            server: ContextUsageTrackerPlugin instance
        """
        self.server = server
        self.tracker = server.tracker
        self.templates = ui_templates(Path(__file__).parent / "templates")

    def get_web_router(self) -> APIRouter:
        """Get the FastAPI router for this plugin's web endpoints."""
        # Get schema from server (already loaded with Jinja2 templates rendered)
        schema = self.server.get_schema_data() if hasattr(self.server, 'get_schema_data') else {}
        
        # Generate router from schema
        return create_schema_router(
            plugin_name=self.server.name,
            schema=schema,
            handler_class=self
        )
    
    # Handler methods (called by schema router)
    
    async def get_panel(self, request: Request):
        """Render the panel; its script and stylesheet are the plugin's static assets."""
        return self.templates.TemplateResponse(request, "panel.html", {"plugin": self.server.name})
    
    async def get_usage(self, request: Request, session_id: str | None = None) -> JSONResponse:
        """Get current usage statistics."""
        latest = self.tracker.get_latest(session_id=session_id)
        agent_stats = self.tracker.get_agent_stats(session_id=session_id)
        statistics = self.tracker.get_statistics(session_id=session_id)

        return JSONResponse({
            "latest": latest or {},
            "agents": agent_stats,
            "statistics": statistics,
        })
    
    async def get_history(self, request: Request, last_n: int | None = None, session_id: str | None = None,
                          agent_id: str | None = None) -> JSONResponse:
        """Get usage history (optionally filtered by session and/or agent): the newest ``last_n`` calls of the
        tracker's window, the whole window without it -- the calls the statistics are computed over."""
        history = self.tracker.get_history(last_n=last_n, session_id=session_id, agent_id=agent_id)
        return JSONResponse({"history": history})
    
    async def clear_history(self, request: Request) -> JSONResponse:
        """Clear usage history."""
        self.tracker.clear_history()
        return JSONResponse({"status": "cleared"})
