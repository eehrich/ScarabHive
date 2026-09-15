"""Web endpoints of the Sub-Agent Manager: the Sub-Agents panel and the calls it makes."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from agent_system.plugins.schema_router import create_schema_router
from agent_system.ui.resources import ui_templates

logger = logging.getLogger(__name__)

SESSION = Query(..., description="The parent session whose sub-agents")


def get_session_service():
    """The app's session service: sub-agents are sessions linked to their parent."""
    from agent_system.app import _session_service
    if not _session_service:
        raise HTTPException(status_code=503, detail="The session service is not running")
    return _session_service


def answered(result: dict[str, Any]) -> dict[str, Any]:
    """The server answers a failure like a result; here it becomes an error. A sub-agent that is not there or not the
    session's is the same to the panel: not found."""
    if result.get("status") != "error":
        return result
    error = result.get("error") or "Unknown error"
    if "not found" in error.lower() or "does not belong" in error:  # the tool's text names the other session
        raise HTTPException(status_code=404, detail="Sub-agent not found")
    raise HTTPException(status_code=500, detail=error)


class SubAgentManagerWebFactory:
    """The panel over the sub-agents a manager instance spawned in a session."""

    def __init__(self, server):
        self.server = server
        self.templates = ui_templates(Path(__file__).parent / "templates")

    def get_web_router(self) -> APIRouter:
        return create_schema_router(plugin_name=self.server.name, schema=self.server.get_schema_data(), handler_class=self)

    async def render_panel(self, request: Request):
        """Render the panel; its script and stylesheet are the plugin's static assets."""
        return self.templates.TemplateResponse(request, "panel.html", {"plugin": self.server.name})

    async def get_sub_agents(self, request: Request, session_id: str = SESSION) -> dict[str, Any]:
        """Every sub-agent of the session this instance spawned, archived ones included, and the workflow phase.

        Read-only, unlike the tool's ``list``: that one marks a sub-agent without a run in its process interrupted,
        which is right only in the process that runs them. Sub-agents run in other processes too (the writer worker),
        and a panel refreshing there would mark their running books interrupted -- so this shows the stored state."""
        session_service = get_session_service()
        manager = self.server._get_manager(session_service)
        try:
            stored = await manager.list_sub_sessions(parent_session_id=session_id, include_completed=True,
                                                     creator_plugin=self.server.name)
        except Exception as error:
            raise HTTPException(status_code=500, detail=str(error)) from error
        user_id = manager._extract_user_id(session_id)
        instances = []
        for metadata in stored:
            try:  # the transcript's own length; the stored count lags behind a run
                count = len((await session_service.session_manager.load_session(user_id, metadata["instance_id"])).get("messages", []))
            except Exception:
                count = metadata.get("message_count", 0)
            instances.append({key: metadata.get(key) for key in (
                "instance_id", "agent_type", "status", "created_at", "last_used", "task_summary", "current_activity",
                "activity_updated_at")} | {"message_count": count})
        return {"instances": instances, "phase": await self._phase(session_service, session_id)}

    async def _phase(self, session_service, session_id: str) -> dict[str, Any] | None:
        """The phase the session is in and the agents it lets this instance spawn -- the rule of
        ``_get_phase_allowed_agents``, read from the session's stored context vars. None without phase filtering."""
        server = self.server
        if not server.phase_filtering_enabled:
            return None
        current = None
        try:
            session_manager = session_service.session_manager
            owner = await session_manager._find_session_owner_async(session_id)
            if owner:
                stored = await session_manager.load_session(owner, session_id)
                current = (stored.get("context_vars") or {}).get(server.phase_variable)
        except Exception as error:  # a session not saved yet has no phase
            logger.debug("No phase for session %s: %s", session_id, error)
        phases = server.phase_agents
        spawnable = (phases.get(current) or phases.get("_default") or server.allowed_agents) if current else server.allowed_agents
        return {"variable": server.phase_variable, "current": current, "agents": list(spawnable),
                "allowed_agents": list(server.allowed_agents)}

    async def get_sub_agent(self, request: Request, agent_id: str, session_id: str = SESSION,
                            offset: int | None = Query(None, ge=0), limit: int | None = Query(None, ge=1)) -> dict[str, Any]:
        """A sub-agent's transcript, paged as the tool's ``info`` pages it: the tail without an offset."""
        return answered(await self.server._handle_info({
            "_session_id": session_id, "_session_service": get_session_service(), "instance_id": agent_id,
            "offset": offset, "limit": limit}))

    async def archive_sub_agent(self, request: Request, agent_id: str, session_id: str = SESSION) -> dict[str, Any]:
        """Archive a sub-agent, as the tool's ``delete`` does."""
        return answered(await self.server._handle_delete({
            "_session_id": session_id, "_session_service": get_session_service(), "instance_id": agent_id}))
