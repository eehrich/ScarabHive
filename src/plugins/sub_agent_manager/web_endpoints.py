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

# What one map may cost. The panel asks for it every ten seconds, so the expensive part is bounded on its own:
# a node with sub-agents is READ, and ``load_session`` reads the whole file, transcript included. MAP_READS bounds
# those, MAP_NODES the answer. MAP_DEPTH is only a guard against a link that leads back into itself -- a real tree
# ends at the manager's ``max_nesting_depth`` (5 by default) long before. Whichever is reached, the answer says
# ``truncated`` rather than growing.
MAP_NODES = 300
MAP_READS = 60
MAP_DEPTH = 20


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

        Read-only, unlike the tool's ``list``: that one heals what a crash left behind, marking a sub-agent nobody
        has in hand interrupted. A panel is a viewer -- it refreshes every ten seconds, of its own accord, in
        whichever process happens to serve it -- so it writes nothing and shows the stored state."""
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

    async def get_agent_map(self, request: Request, session_id: str = SESSION) -> dict[str, Any]:
        """The session and everything below it, nested: each sub-agent carries the sub-agents it spawned itself.

        Two things it does differently from the list, both on purpose. It shows what ANY manager instance spawned --
        below the first level the spawning instance is the sub-agent's own, so leaving a branch out for its name
        would be a map that lies -- and it carries no message count: the stored one lags a run behind (the list
        reads the transcript's own length instead), and reading every node to correct it is what this walk avoids.
        """
        session_service = get_session_service()
        manager = self.server._get_manager(session_service)
        sessions = session_service.session_manager
        user_id = manager._extract_user_id(session_id)
        remaining = MAP_NODES
        reads = MAP_READS
        truncated = False

        async def branch(parent_id: str, depth: int,
                         stored: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            """The sub-agents of one node, oldest first, each with its own below it.

            A node is read only when the stat behind ``_has_children`` says a sub-index exists for it: a session
            with fifty leaves must not read fifty session files to learn that they are leaves. The one thing that
            stat answers differently from the metadata read here is a node whose last sub-agent was DELETED --
            deleting it unlinks the sub-index while the entry stays in the parent's metadata, and the node is then
            shown as the leaf it has become, rather than as the parent of something that is gone.

            Whatever is NOT shown is said, once, as ``truncated`` -- a node cut off looks exactly like a leaf
            otherwise. ``stored`` is a session already read: the root's, which the answer needs for its title
            anyway, and which is charged to the reads all the same.
            """
            nonlocal remaining, reads, truncated
            if not sessions._has_children(user_id, parent_id):
                return []
            if depth <= 0 or remaining <= 0 or reads <= 0:  # something IS below here, and it is not being shown
                truncated = True
                return []
            reads -= 1
            if stored is None:
                try:
                    stored = await sessions.load_session(user_id, parent_id)
                except Exception as error:  # the stat says there are sub-agents and the file does not answer
                    logger.debug("No sub-agents under %s: %s", parent_id, error)
                    truncated = True
                    return []
            listed = (stored.get("metadata") or {}).get("sub_agents") or {}
            nodes = []
            for instance_id, metadata in sorted(listed.items(), key=lambda pair: pair[1].get("created_at") or ""):
                if remaining <= 0:  # the siblings after this one are not shown either
                    truncated = True
                    break
                remaining -= 1
                nodes.append({key: metadata.get(key) for key in (
                    "agent_type", "status", "created_at", "last_used", "task_summary", "current_activity")}
                    | {"instance_id": instance_id, "parent_session_id": parent_id,
                       "children": await branch(instance_id, depth - 1)})
            return nodes

        try:
            root = await sessions.load_session(user_id, session_id)
        except Exception as error:  # a session not saved yet is a map of one node
            logger.debug("No session %s to map: %s", session_id, error)
            root = {}
        children = await branch(session_id, MAP_DEPTH, stored=root or None)
        return {"root": {"instance_id": session_id, "title": root.get("title"),
                         "agent_type": root.get("agent_name"), "children": children},
                "truncated": truncated}

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
