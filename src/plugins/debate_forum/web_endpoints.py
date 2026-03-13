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
        )

    async def serve_js(self, request: Request):
        return FileResponse(
            self.plugin_dir / "static" / "panel.js",
            media_type="application/javascript",
        )

    # ── API endpoints ─────────────────────────────────────────

    async def api_list_channels(
        self,
        request: Request,
        status: str | None = Query(default=None, description="Filter by status"),
        search: str | None = Query(default=None, description="Search name/topic"),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        channels = self.db.list_channels(
            status=status, search=search, limit=limit, offset=offset
        )
        # Add message_count to each channel
        for ch in channels:
            ch["message_count"] = self.db.get_message_count(ch["id"])
        total = self.db.count_channels(status=status)
        return {
            "channels": channels,
            "total": total,
            "count": len(channels),
        }

    async def api_get_channel(self, request: Request, channel_id: int):
        channel = self.db.get_channel(channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found")
        channel["message_count"] = self.db.get_message_count(channel_id)
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
        return {
            "channel_id": channel_id,
            "messages": messages,
            "count": len(messages),
        }

    async def api_get_stats(self, request: Request):
        return self.db.get_stats()

    async def api_archive_channel(self, request: Request, channel_id: int):
        channel = self.db.get_channel(channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found")
        ok = self.db.archive_channel(channel_id)
        if not ok:
            raise HTTPException(status_code=500, detail="Failed to archive channel")
        return {"status": "archived", "channel_id": channel_id}

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
