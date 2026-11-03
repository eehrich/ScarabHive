"""Web UI endpoints for context usage tracker plugin."""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from agent_system import app_state
from agent_system.auth.dependencies import get_optional_user
from agent_system.auth.models import User
from agent_system.auth.session_access import may_see_session, require_everything, viewer
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
    
    async def get_usage(self, request: Request, session_id: str | None = None,
                        current_user: Optional[User] = Depends(get_optional_user)) -> JSONResponse:
        """Get current usage statistics."""
        if not await may_see_session(request, current_user, session_id):
            return JSONResponse({"latest": {}, "agents": {}, "statistics": {}})
        tree = await self._tree(current_user, session_id)
        latest = self.tracker.get_latest(session_id=session_id)  # the session's own last call: its context
        agent_stats = self.tracker.get_agent_stats(session_id=session_id, tree=tree)
        statistics = self.tracker.get_statistics(session_id=session_id, tree=tree)

        return JSONResponse({
            "latest": latest or {},
            "agents": agent_stats,
            "statistics": statistics,
        })
    
    async def get_history(self, request: Request, last_n: int | None = None, session_id: str | None = None,
                          agent_id: str | None = None,
                          current_user: Optional[User] = Depends(get_optional_user)) -> JSONResponse:
        """Get usage history (optionally filtered by session and/or agent): the newest ``last_n`` calls of the
        tracker's window, the whole window without it -- the calls the statistics are computed over."""
        if not await may_see_session(request, current_user, session_id):
            return JSONResponse({"history": []})
        history = self.tracker.get_history(last_n=last_n, session_id=session_id, agent_id=agent_id,
                                           tree=await self._tree(current_user, session_id))
        return JSONResponse({"history": history})
    
    async def _tree(self, current_user: Optional[User], session_id: Optional[str]) -> list[str]:
        """The sessions below ``session_id`` (UsageTracker.session_tree), read under its owner: the viewer's
        own, or -- an admin looking at another user's session -- the user whose directory holds it."""
        sessions = getattr(app_state.session_service, "session_manager", None)
        if sessions is None or not session_id:
            return []
        user = viewer(current_user)
        if not sessions.belongs_to(user, session_id):
            try:
                sessions._validate_session_id(session_id)  # the owner scan joins the bare id onto every directory
            except ValueError:
                return []
            user = await sessions.find_session_owner_async(session_id) or user
        return await self.tracker.session_tree(sessions, user, session_id)

    async def clear_history(self, request: Request,
                            current_user: Optional[User] = Depends(get_optional_user)) -> JSONResponse:
        """Clear usage history -- every session's, so only an admin may."""
        require_everything(request, current_user, "Clearing the figures of every session")
        self.tracker.clear_history()
        return JSONResponse({"status": "cleared"})
