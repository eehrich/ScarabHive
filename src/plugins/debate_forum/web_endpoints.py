"""Web endpoints of the debate forum: the Debate Forum panel and the calls it makes."""
from __future__ import annotations

import asyncio
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from agent_system.plugins.schema_router import create_schema_router
from agent_system.ui.resources import ui_templates
from agent_system.utils.markdown_render import markdown_to_html

if TYPE_CHECKING:
    from .database import DebateForumDB

CHANNELS_SHOWN = 50
GROUPS_SHOWN = 200


class NewChannel(BaseModel):
    name: str
    topic: str = ""
    context: str = ""


class NewPost(BaseModel):
    agent_name: str
    agent_role: str = ""
    content: str


class PinState(BaseModel):
    pinned: bool = True


def rendered(markdown: str) -> str:
    """Model-written Markdown as sanitised HTML; plain escaped text where the converter gives none."""
    return markdown_to_html(markdown) or escape(markdown)


class DebateForumWebFactory:
    """The panel works on the forum database the tools write."""

    def __init__(self, db: "DebateForumDB", name: str, server):
        self.db = db
        self.name = name
        self.server = server
        self.templates = ui_templates(Path(__file__).parent / "templates")

    def get_web_router(self) -> APIRouter:
        return create_schema_router(plugin_name=self.name, schema=self.server.get_schema_data(), handler_class=self)

    def get_static_assets(self) -> Path:
        return Path(__file__).parent / "static"

    async def render_panel(self, request: Request):
        return self.templates.TemplateResponse(request, "panel.html", {"plugin": self.name})

    def _channel(self, channel_id: int) -> dict:
        channel = self.db.get_channel(channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found")
        return channel

    async def api_list_channels(self, request: Request, status: str | None = None, search: str | None = None,
                                group_id: int | None = None) -> dict:
        """The most recently active channels that match, each with its message count, and how many match in all."""
        channels = self.db.list_channels(status=status, search=search, group_id=group_id, limit=CHANNELS_SHOWN)
        for channel in channels:
            channel["message_count"] = self.db.get_message_count(channel["id"])
        return {"channels": channels, "total": self.db.count_channels(status=status, group_id=group_id, search=search)}

    async def api_list_groups(self, request: Request) -> dict:
        return {"groups": self.db.list_groups(limit=GROUPS_SHOWN)}

    async def api_get_stats(self, request: Request) -> dict:
        return self.db.get_stats()

    async def api_get_channel(self, request: Request, channel_id: int) -> dict:
        """The channel with its message count and, once concluded, its verdict summary rendered."""
        channel = self._channel(channel_id)
        channel["message_count"] = self.db.get_message_count(channel_id)
        verdict = channel.get("verdict_json")
        summary = channel.get("verdict_summary") or (verdict.get("summary") if isinstance(verdict, dict) else None)
        # Rendering can take seconds (a paragraph of many lines): off the event loop.
        channel["verdict_summary_html"] = await asyncio.to_thread(rendered, summary) if summary else None
        return channel

    async def api_get_messages(self, request: Request, channel_id: int) -> dict:
        """Every post, oldest first, its Markdown rendered as ``content_html``; ``content`` stays raw for copying."""
        self._channel(channel_id)
        messages = self.db.get_messages(channel_id)
        def render_all() -> None:
            for message in messages:
                message["content_html"] = rendered(message["content"])

        await asyncio.to_thread(render_all)  # off the event loop, as above
        return {"messages": messages}

    async def api_post_message(self, request: Request, channel_id: int, post: NewPost) -> dict:
        """A post from the viewer into the channel's latest round; only an active channel takes one."""
        channel = self._channel(channel_id)
        if channel["status"] != "active":
            raise HTTPException(status_code=409, detail=f"Channel {channel_id} is {channel['status']}: it takes no posts")
        name, content = post.agent_name.strip(), post.content.strip()
        if not name or not content:
            raise HTTPException(status_code=422, detail="A post needs a name and a text")
        # an empty channel has not begun: its first round is 1, as the tool's
        latest = max((message["round"] for message in self.db.get_messages(channel_id)), default=1)
        result = self.db.post_message(channel_id=channel_id, agent_name=name, agent_role=post.agent_role.strip() or "user",
                                      round_num=latest, content=content)
        return {"message_id": result["message_id"], "channel_id": channel_id, "round": latest}

    async def api_archive_channel(self, request: Request, channel_id: int) -> dict:
        if self._channel(channel_id)["status"] == "archived":
            raise HTTPException(status_code=409, detail=f"Channel {channel_id} is archived already")
        self.db.archive_channel(channel_id)
        return {"status": "archived", "channel_id": channel_id}

    async def api_reopen_channel(self, request: Request, channel_id: int) -> dict:
        self._channel(channel_id)
        if not self.db.reopen_channel(channel_id):  # only a concluded or archived one
            raise HTTPException(status_code=409, detail=f"Channel {channel_id} is active already")
        return {"status": "active", "channel_id": channel_id}

    async def api_create_channel(self, request: Request, channel: NewChannel) -> dict:
        name = channel.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="A channel needs a name")
        return self.db.create_channel(name=name, topic=channel.topic.strip(), context=channel.context.strip())

    async def api_delete_channel(self, request: Request, channel_id: int) -> dict:
        """For good, with its messages."""
        if not self.db.delete_channel(channel_id):
            raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found")
        return {"status": "deleted", "channel_id": channel_id}

    async def api_toggle_pin(self, request: Request, message_id: int, state: PinState) -> dict:
        changed = self.db.pin_message(message_id) if state.pinned else self.db.unpin_message(message_id)
        if not changed:
            raise HTTPException(status_code=404, detail=f"Message {message_id} not found")
        return {"status": "pinned" if state.pinned else "unpinned", "message_id": message_id}
