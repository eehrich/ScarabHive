"""Web endpoints of the ComfyUI plugin: the ComfyUI panel and the calls it makes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request

from agent_system.plugins.schema_router import create_schema_router
from agent_system.ui.resources import ui_templates

from .job_tracker import ACTIVE_STATUSES

if TYPE_CHECKING:
    from .comfyui_client import ComfyUIClient
    from .server import ComfyUIServer

RECENT_SHOWN = 10


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

    async def list_jobs(self, request: Request) -> dict[str, Any]:
        """Each server's state, the jobs still to finish and the last ones finished, after the tracker has been
        brought up to date with what the servers report."""
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
            "active": [job_row(job, now) for job in tracker.get_active_jobs()],
            "recent": [job_row(job, now) for job in tracker.get_recent_completed(limit=RECENT_SHOWN)],
            "stats": tracker.get_stats(),
        }

    async def cancel_job(self, request: Request, prompt_id: str) -> dict[str, Any]:
        """Only a job still to finish; a refusal of the ComfyUI server is an error here."""
        job = self.server.job_tracker.get_job(prompt_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {prompt_id} not found")
        if job["status"] not in ACTIVE_STATUSES:
            raise HTTPException(status_code=409, detail=f"Job {prompt_id} is already {job['status']}")
        result = await self.server._cancel_job(prompt_id)
        if result["status"] == "already_finished":
            raise HTTPException(status_code=409, detail=f"Job {prompt_id} has already {result['job_status']}")
        if result["status"] != "cancelled":
            raise HTTPException(status_code=502, detail=result.get("error") or f"Job {prompt_id} was not cancelled")
        return result
