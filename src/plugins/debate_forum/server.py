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
        min_message_length: int = 50,
    ):
        super().__init__(name, system_config, mcp_config)
        self.db = db
        self.min_message_length = min_message_length

    # ── Tool: create_group ────────────────────────────────────

    async def create_group(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name", "")
        description = params.get("description", "")
        if not name:
            return {"error": "name is required"}
        result = self.db.create_group(name=name, description=description)
        status = params.get("_status")
        if status:
            await status.end(
                f"Group #{result['group_id']} '{str(result['name'])[:40]}' created")
        return {"status": "created", "group_id": result["group_id"], "name": result["name"]}

    # ── Tool: list_groups ─────────────────────────────────────

    async def list_groups(self, params: dict[str, Any]) -> dict[str, Any]:
        limit = params.get("limit", 100)
        groups = self.db.list_groups(limit=limit)
        status = params.get("_status")
        if status:
            await status.end(f"Listed {len(groups)} group(s)")
        return {"groups": groups, "count": len(groups)}

    # ── Tool: create_channel ──────────────────────────────────

    async def create_channel(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name", "")
        topic = params.get("topic", "")
        context = params.get("context", "")
        metadata = params.get("metadata")
        group_id: int | None = params.get("group_id") or None

        if not name or not topic:
            return {"error": "name and topic are required"}

        status = params.get("_status")
        if status:
            await status.progress(f"Creating channel: {name}")

        result = self.db.create_channel(
            name=name, topic=topic, context=context, metadata=metadata, group_id=group_id
        )

        if status:
            await status.end(
                f"Channel #{result['channel_id']} '{str(result['name'])[:40]}' created"
                + (f" in group {result['group_id']}" if result.get("group_id") else ""))

        return {
            "status": "created",
            "channel_id": result["channel_id"],
            "name": result["name"],
            "group_id": result.get("group_id"),
        }

    # ── Tool: post_message ────────────────────────────────────

    async def post_message(self, params: dict[str, Any]) -> dict[str, Any]:
        channel_id = params.get("channel_id")
        agent_name = params.get("agent_name", "")
        agent_role = params.get("agent_role", "")
        round_num = params.get("round", 1)
        content = params.get("content", "")
        metadata = params.get("metadata")

        # ── Append mode: continue an existing message with another chunk ──
        # For long outputs, post the first chunk normally, then append the rest
        # (append=true + message_id). The chunks are concatenated server-side
        # into ONE complete message, so nothing is truncated and the stored
        # JSON stays whole. Continuation chunks skip the min-length check.
        if params.get("append"):
            message_id = params.get("message_id")
            if not message_id or not content:
                return {"error": "append=true requires 'message_id' and 'content'"}
            msg = self.db.get_message(message_id)
            if not msg:
                return {"error": f"Message {message_id} not found (cannot append)"}
            channel = self.db.get_channel(msg["channel_id"])
            if not channel or channel["status"] != "active":
                return {"error": f"Channel {msg['channel_id']} is not active, cannot append"}
            result = self.db.append_message(message_id, content)
            status = params.get("_status")
            if status:
                await status.end(
                    f"Appended {len(content)} chars to message {message_id} "
                    f"(now {result['length']})"
                )
            return {
                "status": "appended",
                "message_id": message_id,
                "channel_id": msg["channel_id"],
                "length": result["length"],
            }

        if not channel_id or not agent_name or not content:
            return {"error": "channel_id, agent_name, and content are required"}

        channel = self.db.get_channel(channel_id)
        if not channel:
            return {"error": f"Channel {channel_id} not found"}
        if channel["status"] != "active":
            return {"error": f"Channel {channel_id} is {channel['status']}, cannot post"}

        # Filter truncated/retry artifacts (e.g. "Hallo Sven", "Sven")
        if self.min_message_length > 0 and len(content.strip()) < self.min_message_length:
            return {
                "error": f"Message too short ({len(content.strip())} chars, min {self.min_message_length}). "
                         f"Likely a truncated API response. Please provide full message."
            }

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
            await status.end(
                f"Posted to #{channel_id} (round {round_num}, "
                f"id={result['message_id']}, {len(content)} chars) "
                f"-- {str(agent_name)[:30]} [{str(agent_role)[:20]}]")

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

        status = params.get("_status")
        if fmt == "json":
            messages = self.db.get_messages(channel_id, limit=max_messages)
            if status:
                await status.end(f"Thread #{channel_id}: {len(messages)} message(s) as json")
            return {
                "channel": channel,
                "messages": messages,
                "message_count": len(messages),
            }
        else:
            thread_text = self.db.format_thread(channel_id, max_messages=max_messages)
            count = self.db.get_message_count(channel_id)
            if status:
                await status.end(
                    f"Thread #{channel_id}: {count} message(s), {len(thread_text)} chars")
            return {
                "channel_id": channel_id,
                "thread": thread_text,
                "message_count": count,
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

    # ── Tool: rename_channel ────────────────────────────────

    async def rename_channel(self, params: dict[str, Any]) -> dict[str, Any]:
        channel_id = params.get("channel_id")
        new_name = params.get("new_name")
        if not channel_id:
            return {"error": "channel_id is required"}
        if not new_name or not isinstance(new_name, str) or not new_name.strip():
            return {"error": "new_name (non-empty string) is required"}

        channel = self.db.get_channel(channel_id)
        if not channel:
            return {"error": f"Channel {channel_id} not found"}

        old_name = channel["name"]
        ok = self.db.rename_channel(channel_id, new_name)
        if not ok:
            return {"error": f"Failed to rename channel {channel_id}"}

        scope = params.get("_status")
        if scope:
            await scope.end(
                f"Channel #{channel_id} renamed: "
                f"'{str(old_name)[:40]}' -> '{new_name.strip()[:40]}'")
        return {
            "status": "renamed",
            "channel_id": channel_id,
            "old_name": old_name,
            "new_name": new_name.strip(),
        }

    # ── Tool: list_channels ───────────────────────────────────

    async def list_channels(self, params: dict[str, Any]) -> dict[str, Any]:
        status_filter = params.get("status", "") or None
        limit = params.get("limit", 50)
        search = params.get("search", "") or None
        group_id: int | None = params.get("group_id") or None

        channels = self.db.list_channels(
            status=status_filter, search=search, limit=limit, group_id=group_id
        )

        # NB: `status` is taken by the status FILTER parameter here, hence
        # `scope` -- the collision is why this handler never reported.
        scope = params.get("_status")
        if scope:
            filters = ", ".join(
                f"{k}={str(v)[:30]}" for k, v in
                (("status", status_filter), ("search", search), ("group", group_id))
                if v)
            await scope.end(
                f"Listed {len(channels)} channel(s)" + (f" [{filters}]" if filters else ""))

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
