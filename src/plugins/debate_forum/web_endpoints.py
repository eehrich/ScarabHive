"""Debate Forum Plugin - Web Endpoints.

Provides REST API and HTML panel for the Discord-style debate forum UI.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Query, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse

from agent_system.plugins.schema_router import create_schema_router
from agent_system.utils.markdown_render import markdown_to_html

if TYPE_CHECKING:
    from .database import DebateForumDB

logger = logging.getLogger(__name__)


class DebateForumWebFactory:
    """Web factory for debate forum plugin.

    Provides REST API endpoints for channel/message data
    and serves the HTML/CSS/JS panel.
    """

    def __init__(self, db: "DebateForumDB", name: str = "debate_forum", server=None):
        self.db = db
        self.name = name
        self.server = server
        self.plugin_dir = Path(__file__).parent

    # ── Panel rendering ───────────────────────────────────────

    async def render_panel(self, request: Request) -> HTMLResponse:
        template_path = self.plugin_dir / "templates" / "panel.html"
        if not template_path.exists():
            return HTMLResponse(
                content="<html><body><h1>Template not found</h1></body></html>",
                status_code=500,
            )
        html_content = template_path.read_text(encoding="utf-8")
        return HTMLResponse(content=html_content, media_type="text/html")

    async def serve_css(self, request: Request):
        return FileResponse(
            self.plugin_dir / "static" / "panel.css",
            media_type="text/css",
            headers={"Cache-Control": "no-cache"},
        )

    async def serve_js(self, request: Request):
        return FileResponse(
            self.plugin_dir / "static" / "panel.js",
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    # ── API endpoints ─────────────────────────────────────────

    async def api_list_channels(
        self,
        request: Request,
        status: str | None = Query(default=None, description="Filter by status"),
        search: str | None = Query(default=None, description="Search name/topic"),
        group_id: int | None = Query(default=None, description="Filter by group ID"),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        channels = self.db.list_channels(
            status=status, search=search, limit=limit, offset=offset, group_id=group_id
        )
        # Add message_count to each channel
        for ch in channels:
            ch["message_count"] = self.db.get_message_count(ch["id"])
        total = self.db.count_channels(status=status, group_id=group_id)
        return {
            "channels": channels,
            "total": total,
            "count": len(channels),
        }

    async def api_list_groups(self, request: Request):
        groups = self.db.list_groups(limit=200)
        return {"groups": groups, "count": len(groups)}

    async def api_get_channel(self, request: Request, channel_id: int):
        channel = self.db.get_channel(channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found")
        channel["message_count"] = self.db.get_message_count(channel_id)
        # Render the verdict summary Markdown to HTML for the panel (display-only;
        # verdict_summary / verdict_json stay raw in the DB). Mirror the panel's
        # own resolution: prefer verdict_summary, fall back to verdict_json.summary.
        summary = channel.get("verdict_summary")
        if not summary and isinstance(channel.get("verdict_json"), dict):
            summary = channel["verdict_json"].get("summary")
        if summary:
            channel["verdict_summary_html"] = markdown_to_html(summary)
        return channel

    async def api_get_messages(
        self,
        request: Request,
        channel_id: int,
        limit: int = Query(default=0, ge=0, le=10000, description="0=all"),
    ):
        channel = self.db.get_channel(channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found")
        messages = self.db.get_messages(channel_id, limit=limit)
        # Render each post's Markdown to HTML for the panel (same central
        # renderer the main chat panel uses). The DB keeps the raw Markdown —
        # this is display-only; ``content`` stays untouched for copy/export.
        for m in messages:
            m["content_html"] = markdown_to_html(m.get("content", ""))
        return {
            "channel_id": channel_id,
            "messages": messages,
            "count": len(messages),
        }

    async def api_get_stats(self, request: Request):
        return self.db.get_stats()

    async def api_post_message(self, request: Request, channel_id: int):
        channel = self.db.get_channel(channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found")
        if channel["status"] != "active":
            raise HTTPException(status_code=400, detail=f"Channel {channel_id} is {channel['status']}, cannot post")
        body = await request.json()
        agent_name = (body.get("agent_name") or "").strip()
        content = (body.get("content") or "").strip()
        if not agent_name or not content:
            raise HTTPException(status_code=400, detail="agent_name and content are required")
        agent_role = (body.get("agent_role") or "user").strip()
        round_num = body.get("round", 0)
        result = self.db.post_message(
            channel_id=channel_id,
            agent_name=agent_name,
            agent_role=agent_role,
            round_num=round_num,
            content=content,
        )
        return {"status": "posted", "message_id": result["message_id"], "channel_id": channel_id}

    async def api_archive_channel(self, request: Request, channel_id: int):
        channel = self.db.get_channel(channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found")
        ok = self.db.archive_channel(channel_id)
        if not ok:
            raise HTTPException(status_code=500, detail="Failed to archive channel")
        return {"status": "archived", "channel_id": channel_id}

    async def api_reopen_channel(self, request: Request, channel_id: int):
        channel = self.db.get_channel(channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found")
        if channel["status"] == "active":
            raise HTTPException(status_code=400, detail="Channel is already active")
        ok = self.db.reopen_channel(channel_id)
        if not ok:
            raise HTTPException(status_code=500, detail="Failed to reopen channel")
        return {"status": "active", "channel_id": channel_id}

    async def api_create_channel(self, request: Request):
        body = await request.json()
        name = (body.get("name") or "").strip()
        topic = (body.get("topic") or "").strip()
        context = (body.get("context") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        result = self.db.create_channel(name=name, topic=topic, context=context)
        return result

    async def api_delete_channel(self, request: Request, channel_id: int):
        channel = self.db.get_channel(channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found")
        ok = self.db.delete_channel(channel_id)
        if not ok:
            raise HTTPException(status_code=500, detail="Failed to delete channel")
        return {"status": "deleted", "channel_id": channel_id}

    async def api_toggle_pin(self, request: Request, message_id: int):
        body = await request.json()
        pinned = body.get("pinned", True)
        if pinned:
            ok = self.db.pin_message(message_id)
        else:
            ok = self.db.unpin_message(message_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Message {message_id} not found")
        return {"status": "pinned" if pinned else "unpinned", "message_id": message_id}

    # ── Router ────────────────────────────────────────────────

    def get_web_router(self) -> APIRouter:
        schema = (
            self.server.get_schema_data()
            if self.server and hasattr(self.server, "get_schema_data")
            else {}
        )
        return create_schema_router(
            plugin_name=self.name,
            schema=schema,
            handler_class=self,
        )
