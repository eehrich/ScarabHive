"""Debate Forum Plugin - MCP Tool Server.

Provides MCP tools for creating/managing debate channels and posting messages.
Used by agent pipelines (e.g., V5a story design) to run structured LLM debates.
"""
from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from agent_system.mcp.schema_based import SchemaBasedMCPServer

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPServerConfig

from .database import DebateForumDB

logger = logging.getLogger(__name__)


class DebateForumServer(SchemaBasedMCPServer):
    """MCP server providing debate forum tools.

    Tools follow the schema.yaml definitions and are auto-routed
    by the SchemaBasedMCPServer dispatcher.
    """

    def __init__(
        self,
        name: str,
        system_config: "AgentSystemConfig",
        mcp_config: "MCPServerConfig",
        db: DebateForumDB,
    ):
        super().__init__(name, system_config, mcp_config)
        self.db = db

    # ── Tool: create_channel ──────────────────────────────────

    async def create_channel(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name", "")
        topic = params.get("topic", "")
        context = params.get("context", "")
        metadata = params.get("metadata")

        if not name or not topic:
            return {"error": "name and topic are required"}

        status = params.get("_status")
        if status:
            await status.progress(f"Creating channel: {name}")

        result = self.db.create_channel(
            name=name, topic=topic, context=context, metadata=metadata
        )

        if status:
            await status.end(f"Channel #{result['channel_id']} created")

        return {
            "status": "created",
            "channel_id": result["channel_id"],
            "name": result["name"],
        }

    # ── Tool: post_message ────────────────────────────────────

    async def post_message(self, params: dict[str, Any]) -> dict[str, Any]:
        channel_id = params.get("channel_id")
        agent_name = params.get("agent_name", "")
        agent_role = params.get("agent_role", "")
        round_num = params.get("round", 1)
        content = params.get("content", "")
        metadata = params.get("metadata")

        if not channel_id or not agent_name or not content:
            return {"error": "channel_id, agent_name, and content are required"}

        channel = self.db.get_channel(channel_id)
        if not channel:
            return {"error": f"Channel {channel_id} not found"}
        if channel["status"] != "active":
            return {"error": f"Channel {channel_id} is {channel['status']}, cannot post"}

        status = params.get("_status")
        if status:
            await status.progress(
                f"[{agent_role}] {agent_name} posting to #{channel_id} (round {round_num})"
            )

        result = self.db.post_message(
            channel_id=channel_id,
            agent_name=agent_name,
            agent_role=agent_role,
            round_num=round_num,
            content=content,
            metadata=metadata,
        )

        if status:
            await status.end(f"Message posted (id={result['message_id']})")

        return {
            "status": "posted",
            "message_id": result["message_id"],
            "channel_id": channel_id,
        }

    # ── Tool: get_thread ──────────────────────────────────────

    async def get_thread(self, params: dict[str, Any]) -> dict[str, Any]:
        channel_id = params.get("channel_id")
        if not channel_id:
            return {"error": "channel_id is required"}

        fmt = params.get("format", "text")
        max_messages = params.get("max_messages", 0)

        channel = self.db.get_channel(channel_id)
        if not channel:
            return {"error": f"Channel {channel_id} not found"}

        if fmt == "json":
            messages = self.db.get_messages(channel_id, limit=max_messages)
            return {
                "channel": channel,
                "messages": messages,
                "message_count": len(messages),
            }
        else:
            thread_text = self.db.format_thread(channel_id, max_messages=max_messages)
            return {
                "channel_id": channel_id,
                "thread": thread_text,
                "message_count": self.db.get_message_count(channel_id),
            }

    # ── Tool: conclude ────────────────────────────────────────

    async def conclude(self, params: dict[str, Any]) -> dict[str, Any]:
        channel_id = params.get("channel_id")
        verdict = params.get("verdict", {})
        summary = params.get("summary", "")

        # Extract summary from verdict object if not provided separately
        if not summary and isinstance(verdict, dict) and verdict.get("summary"):
            summary = verdict["summary"]

        if not channel_id or not verdict:
            return {"error": "channel_id and verdict are required"}

        channel = self.db.get_channel(channel_id)
        if not channel:
            return {"error": f"Channel {channel_id} not found"}

        status = params.get("_status")
        if status:
            await status.progress(f"Concluding channel #{channel_id}")

        ok = self.db.conclude_channel(channel_id, verdict=verdict, summary=summary)
        if not ok:
            return {"error": f"Failed to conclude channel {channel_id}"}

        if status:
            await status.end(f"Channel #{channel_id} concluded")

        return {
            "status": "concluded",
            "channel_id": channel_id,
            "summary": summary,
        }

    # ── Tool: reopen_channel ────────────────────────────────

    async def reopen_channel(self, params: dict[str, Any]) -> dict[str, Any]:
        channel_id = params.get("channel_id")
        if not channel_id:
            return {"error": "channel_id is required"}

        channel = self.db.get_channel(channel_id)
        if not channel:
            return {"error": f"Channel {channel_id} not found"}
        if channel["status"] == "active":
            return {"error": f"Channel {channel_id} is already active"}

        status = params.get("_status")
        if status:
            await status.progress(f"Reopening channel #{channel_id}")

        ok = self.db.reopen_channel(channel_id)
        if not ok:
            return {"error": f"Failed to reopen channel {channel_id}"}

        if status:
            await status.end(f"Channel #{channel_id} reopened")

        return {
            "status": "reopened",
            "channel_id": channel_id,
            "name": channel["name"],
        }

    # ── Tool: list_channels ───────────────────────────────────

    async def list_channels(self, params: dict[str, Any]) -> dict[str, Any]:
        status_filter = params.get("status", "") or None
        limit = params.get("limit", 50)
        search = params.get("search", "") or None

        channels = self.db.list_channels(
            status=status_filter, search=search, limit=limit
        )

        return {
            "channels": channels,
            "count": len(channels),
        }

    # ── Tool: pin_message ─────────────────────────────────────

    async def pin_message(self, params: dict[str, Any]) -> dict[str, Any]:
        message_id = params.get("message_id")
        pinned = params.get("pinned", True)

        if not message_id:
            return {"error": "message_id is required"}

        status = params.get("_status")

        if pinned:
            ok = self.db.pin_message(message_id)
            action = "pinned"
        else:
            ok = self.db.unpin_message(message_id)
            action = "unpinned"

        if not ok:
            return {"error": f"Message {message_id} not found"}

        if status:
            await status.end(f"Message #{message_id} {action}")

        return {"status": action, "message_id": message_id}
