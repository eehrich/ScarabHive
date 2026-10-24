"""Web endpoints of the ComfyUI plugin: the ComfyUI panel and the calls it makes."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from agent_system import app_state
from agent_system.auth.dependencies import get_optional_user
from agent_system.auth.models import User
from agent_system.auth.session_access import may_see_session, sees_everything, viewer
from agent_system.plugins.schema_router import create_schema_router
from agent_system.ui.resources import ui_templates

from .job_tracker import ACTIVE_STATUSES

if TYPE_CHECKING:
    from .comfyui_client import ComfyUIClient
    from .server import ComfyUIServer

logger = logging.getLogger(__name__)

RECENT_SHOWN = 10


async def session_tree(current_user: Optional[User], session_id: str) -> list[str]:
    """``session_id`` and the sessions below it -- a sub-agent's jobs are the session's too. Read under the
    session's owner: the viewer's own, or (an admin looking at another user's session) the user who holds it.
    Without a session store, or when the tree cannot be read, the session alone."""
    sessions = getattr(app_state.session_service, "session_manager", None)
    if sessions is None:
        return [session_id]
    user = viewer(current_user)
    try:
        if not sessions.belongs_to(user, session_id):
            try:
                sessions._validate_session_id(session_id)  # the owner scan joins the bare id onto every directory
            except ValueError:  # no session can have it: asked for every tick, so not worth a warning
                return [session_id]
            user = await sessions._find_session_owner_async(session_id) or user
        return await sessions.subtree_ids(user, session_id) or [session_id]
    except Exception:  # the session's own jobs still show
        logger.warning("The sessions below %s could not be read", session_id, exc_info=True)
        return [session_id]


def since(stamp: str, now: datetime) -> float:
    started = datetime.fromisoformat(stamp)
    if started.tzinfo is None:  # rows written before the tracker stored UTC offsets
        started = started.replace(tzinfo=timezone.utc)
    return (now - started).total_seconds()


def job_row(job: dict[str, Any], now: datetime) -> dict[str, Any]:
    active = job["status"] in ACTIVE_STATUSES
    outputs = job.get("outputs")
    return {
        "prompt_id": job["prompt_id"],
        "workflow": job.get("workflow_name") or job["workflow_id"],
        "status": job["status"],
        "submitted_at": job["submitted_at"],
        "completed_at": job.get("completed_at"),
        # a job still to finish: time since it started running, or since it was submitted while it waits
        "seconds": since(job.get("started_at") or job["submitted_at"], now) if active else job.get("duration_seconds"),
        "error": job.get("error"),
        "outputs": {kind: len(files) for kind, files in outputs.items()} if isinstance(outputs, dict) else {},
    }


async def server_state(client: "ComfyUIClient") -> dict[str, Any]:
    ping = await client.ping()
    online = ping.get("status") == "online"
    return {
        "address": f"{client.host}:{client.port}",
        "online": online,
        "queue_pending": ping.get("queue_pending", 0),
        "queue_running": ping.get("queue_running", 0),
        "error": None if online else ping.get("error") or f"HTTP {ping.get('code')}",
    }


class ComfyUIWebEndpoints:
    """The panel works on the server's job tracker and asks every configured ComfyUI server for its queue."""

    def __init__(self, server: "ComfyUIServer") -> None:
        self.server = server
        self.templates = ui_templates(Path(__file__).parent / "templates")

    def get_web_router(self) -> APIRouter:
        return create_schema_router(plugin_name=self.server.name, schema=self.server.get_schema_data(), handler_class=self)

    async def render_panel(self, request: Request):
        return self.templates.TemplateResponse(request, "panel.html", {"plugin": self.server.name})

    async def list_jobs(self, request: Request, session_id: str | None = None,
                        current_user: Optional[User] = Depends(get_optional_user)) -> dict[str, Any]:
        """Each server's state, the jobs still to finish and the last ones finished, after the tracker has been
        brought up to date with what the servers report. With ``session_id`` the jobs of that session and the
        sessions below it; without, every job -- an admin's view, as in the other session panels."""
        if not await may_see_session(request, current_user, session_id):
            sessions = []  # another user's session: answered as one without jobs, as the other session panels do
        else:
            sessions = await session_tree(current_user, session_id) if session_id else None
        server = self.server
        tracker = server.job_tracker
        clients = [server.client, *(server._build_client(s["host"], s["port"], server.output_dir) for s in server._servers[1:])]
        states, queue = await asyncio.gather(
            asyncio.gather(*(server_state(client) for client in clients)),
            server._get_aggregated_queue(),
        )
        if queue is not None:
            running = {item[1] for item in queue.get("queue_running", []) if len(item) > 1}
            for job in tracker.get_active_jobs():
                if job["status"] != "running" and job["prompt_id"] in running:
                    tracker.update_status(job["prompt_id"], "running")
            await server._sync_stale_jobs_with_history(queue)
        now = datetime.now(timezone.utc)
        return {
            "servers": states,
            "active": [job_row(job, now) for job in tracker.get_active_jobs(sessions)],
            "recent": [job_row(job, now) for job in tracker.get_recent_completed(RECENT_SHOWN, sessions)],
            "stats": tracker.get_stats(sessions),
        }

    async def cancel_job(self, request: Request, prompt_id: str,
                         current_user: Optional[User] = Depends(get_optional_user)) -> dict[str, Any]:
        """Only a job still to finish, and only one the viewer may see: of their own session, or any for an admin
        (a job outside every session too). A refusal of the ComfyUI server is an error here."""
        job = self.server.job_tracker.get_job(prompt_id)
        if job is None or not (sees_everything(request, current_user) or (
                job.get("session_id") is not None and await may_see_session(request, current_user, job["session_id"]))):
            raise HTTPException(status_code=404, detail=f"Job {prompt_id} not found")
        if job["status"] not in ACTIVE_STATUSES:
            raise HTTPException(status_code=409, detail=f"Job {prompt_id} is already {job['status']}")
        result = await self.server._cancel_job(prompt_id)
        if result["status"] == "already_finished":
            raise HTTPException(status_code=409, detail=f"Job {prompt_id} has already {result['job_status']}")
        if result["status"] != "cancelled":
            raise HTTPException(status_code=502, detail=result.get("error") or f"Job {prompt_id} was not cancelled")
        return result
