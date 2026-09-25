"""Web endpoints of the Sub-Agent Manager: the Sub-Agents panel and the calls it makes."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from agent_system.auth.dependencies import get_optional_user
from agent_system.auth.models import User
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


def viewer(current_user: Optional[User]) -> str:
    """Whose sessions the request may see -- the rule of ``/sessions``. Every lookup below goes by it: a session of
    another user is simply not found. The panel used to ask the session directories whose session an id is, and
    answered anyone who named one -- its sub-agents, their transcripts, and an archive of them."""
    return current_user.username if current_user else "anonymous"


def hers(sessions, user_id: str, session_id: str) -> bool:
    """Whether ``session_id`` is one of the viewer's sessions -- a stat, before anything is looked up about it. An id
    no session can have is not (``belongs_to`` says so)."""
    return sessions.belongs_to(user_id, session_id)


def own_sub_sessions(sessions, user_id: str, instance_ids) -> set[str]:
    """The ids among ``instance_ids`` that are the viewer's own sessions -- the ones whose figures and runs the panel
    looks up. A session's entries are its user's to write (PATCH /sessions): one naming another user's sub-agent had
    that one's tokens and whether it runs looked up by the bare id. It is shown as its entry says, as one whose
    sub-session was deleted behind it is -- the two look alike, so neither says whether an id exists elsewhere."""
    return {instance_id for instance_id in instance_ids if hers(sessions, user_id, instance_id)}


def get_session_service():
    """The app's session service: sub-agents are sessions linked to their parent."""
    from agent_system.app import _session_service
    if not _session_service:
        raise HTTPException(status_code=503, detail="The session service is not running")
    return _session_service


def _usage_tracker():
    """The context_usage_tracker's UsageTracker, or None where it is not loaded."""
    from agent_system.plugins.tool_adapter import plugin_tool_registry
    plugin = getattr(plugin_tool_registry.get_server("context_usage_tracker"), "plugin_server", None)
    tracker = getattr(plugin, "tracker", None)
    return tracker if hasattr(tracker, "get_latest") else None


async def context_of(session_ids: list[str]) -> dict[str, dict[str, Any]]:
    """What the last LLM call of each session carried, as the provider counted it: ``context_tokens`` (its prompt
    tokens), the ``context_window`` it ran against, and ``context_stale`` once the context was compacted since.

    From context_usage_tracker, whose database holds the calls of every process that runs it, a sub-agent's included
    -- the number the chat shows for a session (chat_actions.measured_context). Nothing where it is not loaded or a
    session made no call yet: an estimate of our own would be a second number beside that one."""
    tracker = _usage_tracker()
    if tracker is None or not session_ids:
        return {}

    def read() -> dict[str, dict[str, Any]]:
        found = {}
        for session_id in session_ids:
            try:
                latest = tracker.get_latest(session_id=session_id)
            except Exception as error:  # a figure must not fail the listing it stands in
                logger.debug("No usage for %s: %s", session_id, error)
                continue
            if latest and latest.get("prompt_tokens"):
                found[session_id] = {"context_tokens": int(latest["prompt_tokens"]),
                                     "context_window": int(latest.get("context_window") or 0),
                                     "context_stale": bool(latest.get("is_stale"))}
        return found

    return await asyncio.to_thread(read)  # sqlite, two queries per sub-agent


async def message_counts(sessions, user_id: str, parent_id: str) -> dict[str, int]:
    """The message count of each sub-session of ``parent_id``, from the parent's sub-index: one small file, rewritten
    at every save of a sub-session with the transcript's length as of that save -- no transcript read for it. The
    count in the parent's entry is no substitute: it lags a run behind and was measured wrong besides (0, 2 or 9 where
    the transcripts held 4 to 27)."""
    try:
        rows = await sessions.list_child_sessions(user_id, parent_id, annotate_children=False)
    except Exception as error:  # a figure must not fail the listing it stands in
        logger.debug("No sub-index for %s: %s", parent_id, error)
        return {}
    return {row["session_id"]: int(row.get("message_count") or 0) for row in rows if row.get("session_id")}


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
    """The panel over a session's sub-agents, whichever manager instance spawned them."""

    def __init__(self, server):
        self.server = server
        self.templates = ui_templates(Path(__file__).parent / "templates")

    def get_web_router(self) -> APIRouter:
        return create_schema_router(plugin_name=self.server.name, schema=self.server.get_schema_data(), handler_class=self)

    async def render_panel(self, request: Request):
        """Render the panel; its script and stylesheet are the plugin's static assets."""
        return self.templates.TemplateResponse(request, "panel.html", {"plugin": self.server.name})

    def _spawned_by(self, metadata: dict[str, Any]):
        """The manager instance that spawned a sub-agent, as its entry names it (``creator_plugin``) -- this one where
        that instance is not loaded in this process, or the entry names none.

        The panel shows every instance's sub-agents, while a run, a background job and the mark an archive leaves on
        it live in the instance that spawned it. Asked through another one, a sub-agent running in this process read
        as idle wherever session presence is off, and an archive left its job unmarked: at its ending the job kept
        its result for a poll nobody makes, and woke the caller for a sub-agent the panel had put away."""
        from plugins.sub_agent_manager.server import SubAgentManagerServer

        name = metadata.get("creator_plugin")
        if not name or name == self.server.name:
            return self.server
        from agent_system.plugins.tool_adapter import plugin_tool_registry
        plugin = getattr(plugin_tool_registry.get_server(name), "plugin_server", None)
        server = getattr(plugin, "server", plugin)  # the hybrid plugin holds the tool server
        return server if isinstance(server, SubAgentManagerServer) else self.server

    @staticmethod
    def _require_hers(user_id: str, *session_ids: str) -> None:
        """Not found, unless every one is the viewer's session: the tool's lookups behind these ask by the id alone."""
        sessions = get_session_service().session_manager
        if not all(hers(sessions, user_id, session_id) for session_id in session_ids):
            raise HTTPException(status_code=404, detail="Sub-agent not found")

    async def _owner_of(self, session_id: str, agent_id: str, user_id: str):
        """``_spawned_by`` for a sub-agent named by id, read from the entry its parent ``session_id`` holds -- under the
        viewer, as the list reads: a directory scan could meet another user's copy of an id two directories hold."""
        try:
            parent = await get_session_service().session_manager.load_session(user_id, session_id)
        except Exception as error:  # the handler asked next reads the same session, and says what is wrong
            logger.debug("No entry for %s under %s: %s", agent_id, session_id, error)
            return self.server
        return self._spawned_by(((parent.get("metadata") or {}).get("sub_agents") or {}).get(agent_id) or {})

    async def get_sub_agents(self, request: Request, session_id: str = SESSION,
                             current_user: Optional[User] = Depends(get_optional_user)) -> dict[str, Any]:
        """Every sub-agent of the session, archived ones included, and the workflow phase.

        Whichever manager instance spawned it, as the map shows them: the panel is one per instance, and a list of this
        instance's own was empty on every session whose agent spawns through another -- beside a map full of them.

        Each carries ``state``: what it is doing, in the words and by the rule of the tool's ``list`` (running,
        idle, interrupted, failed, cancelled, archived) -- the stored ``status`` says "active" for running and idle
        alike. And the figures the map carries too: ``message_count`` (``message_counts``) and the context of its last
        call (``context_of``), each absent where it is not known. Read-only, unlike ``list``: that one heals what a
        crash left behind, marking a sub-agent nobody has in hand interrupted. A panel is a viewer -- it refreshes
        every ten seconds, of its own accord, in whichever process happens to serve it -- so it writes nothing."""
        session_service = get_session_service()
        sessions = session_service.session_manager
        user_id = viewer(current_user)
        if not hers(sessions, user_id, session_id):  # not hers, or not saved yet
            return {"instances": [], "phase": await self._phase(session_service, user_id, session_id)}
        try:
            # By the viewer, as the map reads it: `list_sub_sessions` finds the owner by scanning the directories,
            # and took whichever copy of an id it met first.
            parent = await sessions.load_session(user_id, session_id)
        except Exception as error:
            raise HTTPException(status_code=500, detail=str(error)) from error
        stored = sorted(({**metadata, "instance_id": instance_id} for instance_id, metadata
                         in ((parent.get("metadata") or {}).get("sub_agents") or {}).items()),
                        key=lambda metadata: metadata.get("last_used") or "", reverse=True)
        mine = own_sub_sessions(sessions, user_id, [metadata["instance_id"] for metadata in stored])
        counts = await message_counts(sessions, user_id, session_id)
        context = await context_of(sorted(mine))
        instances = []
        for metadata in stored:
            instance_id = metadata["instance_id"]
            instances.append({key: metadata.get(key) for key in (
                "instance_id", "agent_type", "status", "created_at", "last_used", "task_summary", "current_activity",
                "activity_updated_at")} | {"message_count": counts.get(instance_id), **context.get(instance_id, {}),
                                           "state": await self._spawned_by(metadata)._shown_status(
                                               metadata, lambda: user_id, ask_runs=instance_id in mine)})
        return {"instances": instances, "phase": await self._phase(session_service, user_id, session_id)}

    async def _phase(self, session_service, user_id: str, session_id: str) -> dict[str, Any] | None:
        """The phase the session is in and the agents it lets this instance spawn -- the rule of
        ``_get_phase_allowed_agents``, read from the session's stored context vars. None without phase filtering."""
        server = self.server
        if not server.phase_filtering_enabled:
            return None
        current = None
        try:
            stored = await session_service.session_manager.load_session(user_id, session_id)
            current = (stored.get("context_vars") or {}).get(server.phase_variable)
        except Exception as error:  # a session not saved yet, or not the viewer's, has no phase
            logger.debug("No phase for session %s: %s", session_id, error)
        phases = server.phase_agents
        spawnable = (phases.get(current) or phases.get("_default") or server.allowed_agents) if current else server.allowed_agents
        return {"variable": server.phase_variable, "current": current, "agents": list(spawnable),
                "allowed_agents": list(server.allowed_agents)}

    async def get_agent_map(self, request: Request, session_id: str = SESSION,
                            current_user: Optional[User] = Depends(get_optional_user)) -> dict[str, Any]:
        """The session and everything below it, nested: each sub-agent carries the sub-agents it spawned itself.

        It shows what ANY manager instance spawned, as the list does: below the first level the spawning instance is
        the sub-agent's own, so leaving a branch out for its name would be a map that lies. Each node carries the
        list's figures, from the same two places and without reading a node for them: ``message_count`` from the
        sub-index of the node above it, and the context of its last call (``context_of``).
        """
        session_service = get_session_service()
        sessions = session_service.session_manager
        user_id = viewer(current_user)
        remaining = MAP_NODES
        reads = MAP_READS
        truncated = False
        shown: list[dict[str, Any]] = []

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
            counts = await message_counts(sessions, user_id, parent_id)  # the sub-index the stat above found
            nodes = []
            for instance_id, metadata in sorted(listed.items(), key=lambda pair: pair[1].get("created_at") or ""):
                if remaining <= 0:  # the siblings after this one are not shown either
                    truncated = True
                    break
                remaining -= 1
                node = {key: metadata.get(key) for key in (
                    "agent_type", "status", "created_at", "last_used", "task_summary", "current_activity")} | {
                    "instance_id": instance_id, "parent_session_id": parent_id,
                    "message_count": counts.get(instance_id),
                    "state": await self._spawned_by(metadata)._shown_status(
                        {**metadata, "instance_id": instance_id}, lambda: user_id,
                        ask_runs=instance_id in own_sub_sessions(sessions, user_id, [instance_id])),
                    "children": await branch(instance_id, depth - 1)}
                shown.append(node)
                nodes.append(node)
            return nodes

        try:
            root = await sessions.load_session(user_id, session_id)
        except Exception as error:  # a session not saved yet is a map of one node
            logger.debug("No session %s to map: %s", session_id, error)
            root = {}
        children = await branch(session_id, MAP_DEPTH, stored=root or None)
        # in one go, off the loop, and of her own only (`own_sub_sessions`)
        context = await context_of(sorted(own_sub_sessions(sessions, user_id, [node["instance_id"] for node in shown])))
        for node in shown:
            node.update(context.get(node["instance_id"], {}))
        return {"root": {"instance_id": session_id, "title": root.get("title"),
                         "agent_type": root.get("agent_name"), "children": children},
                "truncated": truncated}

    async def get_sub_agent(self, request: Request, agent_id: str, session_id: str = SESSION,
                            offset: int | None = Query(None, ge=0), limit: int | None = Query(None, ge=1),
                            current_user: Optional[User] = Depends(get_optional_user)) -> dict[str, Any]:
        """A sub-agent's transcript, paged as the tool's ``info`` pages it: the tail without an offset. Answered by the
        instance that spawned it (``_spawned_by``)."""
        user_id = viewer(current_user)
        self._require_hers(user_id, session_id, agent_id)
        owner = await self._owner_of(session_id, agent_id, user_id)
        return answered(await owner._handle_info({
            "_session_id": session_id, "_session_service": get_session_service(), "_user_id": user_id,
            "instance_id": agent_id, "offset": offset, "limit": limit}))

    async def archive_sub_agent(self, request: Request, agent_id: str, session_id: str = SESSION,
                                current_user: Optional[User] = Depends(get_optional_user)) -> dict[str, Any]:
        """Archive a sub-agent, as the tool's ``delete`` does -- the delete of the instance that spawned it, which holds
        its job (``_spawned_by``)."""
        user_id = viewer(current_user)
        self._require_hers(user_id, session_id, agent_id)
        owner = await self._owner_of(session_id, agent_id, user_id)
        return answered(await owner._handle_delete({
            "_session_id": session_id, "_session_service": get_session_service(), "_user_id": user_id,
            "instance_id": agent_id}))
