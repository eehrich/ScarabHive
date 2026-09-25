"""Web UI endpoints for context usage tracker plugin."""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from agent_system.auth.dependencies import get_optional_user
from agent_system.auth.models import User, UserRole
from agent_system.plugins.schema_router import create_schema_router
from agent_system.ui.resources import ui_templates


def sees_everything(request: Request, current_user: Optional[User]) -> bool:
    """An admin sees every session's figures, and so does everyone while
    authentication is off (one person uses the instance). Everyone else sees
    their own sessions'."""
    auth = getattr(getattr(request.app.state, "config", None), "auth", None)
    if auth is not None and not getattr(auth, "enabled", True):
        return True
    return getattr(current_user, "role", None) == UserRole.ADMIN


async def shown_session(request: Request, current_user: Optional[User], session_id: Optional[str]) -> bool:
    """Whether the figures of ``session_id`` (None: every session) may be shown.

    The tracker records calls by session id and keeps no owner, and these
    endpoints answered anyone signed in about any session -- or all of them,
    with their ids. So the session is held against its owner as the app knows
    it: a run of this process that has it names its user (its first turn is
    not on disk yet); otherwise the session store, under the viewer (the
    signed-in user, else "anonymous" -- the rule of /sessions). Another user's
    session shows nothing. Every session at once is for an admin.

    Known gap, left for the per-user separation of plugin data
    (docs/multiuser_datentrennung_konzept.md): the rows carry no owner, so an
    id another user's DELETED session had, taken for a new session of one's
    own, brings that user's old calls along.
    """
    if sees_everything(request, current_user):
        return True
    if session_id is None:
        raise HTTPException(status_code=403,
                            detail="The figures of every session are for admins -- pick one of your sessions.")
    user_id = current_user.username if current_user else "anonymous"
    from agent_system.services.background_job_manager import get_background_job_manager
    running = (await get_background_job_manager().active_sessions()).get(session_id)
    if running is not None and running.get("user_id") is not None:
        return running["user_id"] == user_id
    from agent_system.app import _session_service
    sessions = getattr(_session_service, "session_manager", None)
    if sessions is None:
        raise HTTPException(status_code=503, detail="The session store is not available in this process.")
    return sessions.belongs_to(user_id, session_id)


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
        if not await shown_session(request, current_user, session_id):
            return JSONResponse({"latest": {}, "agents": {}, "statistics": {}})
        latest = self.tracker.get_latest(session_id=session_id)
        agent_stats = self.tracker.get_agent_stats(session_id=session_id)
        statistics = self.tracker.get_statistics(session_id=session_id)

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
        if not await shown_session(request, current_user, session_id):
            return JSONResponse({"history": []})
        history = self.tracker.get_history(last_n=last_n, session_id=session_id, agent_id=agent_id)
        return JSONResponse({"history": history})
    
    async def clear_history(self, request: Request,
                            current_user: Optional[User] = Depends(get_optional_user)) -> JSONResponse:
        """Clear usage history -- every session's, so only an admin may."""
        if not sees_everything(request, current_user):
            raise HTTPException(status_code=403, detail="Clearing the figures of every session is for admins.")
        self.tracker.clear_history()
        return JSONResponse({"status": "cleared"})
