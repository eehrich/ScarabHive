"""Debate Forum Plugin - Hook for injecting debate context into sub-agents.

The inject_debate_context hook (pre_llm_call) reads debate messages from the forum
and injects them into the agent's message history. This eliminates the need for the
moderator to manually pass the thread to each sub-agent.

Flow:
1. Moderator sets context_var `debate_channel_id` (via task_switch/set_context)
2. Sub-agents inherit it when spawned
3. This hook reads all debate messages and injects them as system context
4. The sub-agent sees the full debate thread without manual passing
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING

from agent_system.hooks import SchemaBasedPluginHook, HookContext, HookResult

if TYPE_CHECKING:
    from .database import DebateForumDB

logger = logging.getLogger(__name__)

INJECTION_MARKER = "inject_debate_context"


class DebateForumHooks(SchemaBasedPluginHook):
    """Hook plugin for debate forum context injection."""

    def __init__(self, plugin_dir: Path, db: "DebateForumDB", plugin_config: dict | None = None):
        super().__init__(plugin_dir)
        self.db = db
        # Merge config from plugins.yaml over schema defaults
        if plugin_config:
            self._config.update(plugin_config)

    async def inject_debate_context(self, context: HookContext) -> HookResult:
        """Inject debate forum messages into agent context before LLM call.

        Reads `debate_channel_id` from session context_vars and injects
        all debate messages as a system message.
        """
        logger.debug(
            "[DebateForumHook] inject_debate_context called for agent=%s session=%s",
            context.agent_name, context.session_id,
        )
        if not context.messages or not context.session_id:
            return HookResult(success=True, modified=False, context=context)

        try:
            # Read context_vars from session
            channel_id = self._get_channel_id(context)
            if not channel_id:
                logger.debug("[DebateForumHook] No debate_channel_id in context_vars, skipping")
                return HookResult(success=True, modified=False, context=context)

            # Get config
            max_messages = self._config.get("max_messages_injected", 6)

            # Load channel metadata (name, topic, context)
            channel = self.db.get_channel(channel_id)

            # Load pinned messages (always included)
            pinned_messages = self.db.get_pinned_messages(channel_id)
            pinned_ids = {m["id"] for m in pinned_messages}

            # Load all messages from forum
            messages = self.db.get_messages(channel_id, limit=0)  # all
            if not messages and not pinned_messages:
                return HookResult(success=True, modified=False, context=context)

            # Take only the most recent N non-pinned messages
            unpinned_recent = [m for m in messages if m["id"] not in pinned_ids][-max_messages:]

            # Format for injection: pinned first, then recent
            debate_text = self._format_debate_context(
                pinned_messages, unpinned_recent, channel_id, channel
            )

            # Remove old injection
            from agent_system.llm.models import ChatMessage

            for i in range(len(context.messages) - 1, -1, -1):
                if getattr(context.messages[i], "injected_by", None) == INJECTION_MARKER:
                    context.messages.pop(i)

            # Insert after first system message
            insert_pos = self._find_insert_position(context.messages)
            context.messages.insert(
                insert_pos,
                ChatMessage(
                    role="system",
                    content=debate_text,
                    injected_by=INJECTION_MARKER,
                ),
            )

            logger.info(
                f"[DebateForumHook] Injected {len(pinned_messages)} pinned + "
                f"{len(unpinned_recent)} recent messages from channel #{channel_id}"
            )

            return HookResult(success=True, modified=True, context=context)

        except Exception as e:
            logger.error(f"[DebateForumHook] Failed: {e}", exc_info=True)
            return HookResult(success=True, modified=False, context=context)

    def _get_channel_id(self, context: HookContext) -> int | None:
        """Extract debate_channel_id from session context_vars."""
        try:
            if context.agent and hasattr(context.agent, "_session_tracker"):
                session_vars = context.agent._session_tracker.get_session_template_vars(
                    context.session_id
                )
                channel_id = session_vars.get("debate_channel_id")
                if channel_id is not None:
                    return int(channel_id)
        except Exception as e:
            logger.debug(f"[DebateForumHook] Could not read context_vars: {e}")
        return None

    @staticmethod
    def _format_debate_context(
        pinned: list[dict[str, Any]],
        recent: list[dict[str, Any]],
        channel_id: int,
        channel: dict[str, Any] | None = None,
    ) -> str:
        """Format debate messages for injection into agent context.

        Uses XML-style tags to clearly delimit each post, making it easy
        for LLMs to parse author, role, and content boundaries.
        Pinned messages are always included; all others go through the sliding window.
        """
        ch_name = channel.get("name", "") if channel else ""
        header = f"## Debate Forum – Channel #{channel_id}"
        if ch_name:
            header += f" ({ch_name})"
        parts = [header + "\n"]

        if channel:
            topic = channel.get("topic", "")
            context = channel.get("context", "")
            if topic:
                parts.append(f"**Topic:** {topic}")
            if context:
                parts.append(f"**Context:** {context}")
            if topic or context:
                parts.append("")

        if pinned:
            parts.append("📌 **Pinned messages (always visible):**\n")
            for msg in pinned:
                name = msg.get("agent_name", "?")
                role = msg.get("agent_role", "?")
                content = msg.get("content", "").strip()
                parts.append(
                    f'<post author="{name}" role="{role}" pinned="true">\n'
                    f"{content}\n"
                    f"</post>"
                )
            parts.append("\n---\n")

        if recent:
            parts.append("Recent debate messages:\n")
            current_round = None
            for msg in recent:
                r = msg.get("round", 0)
                if r != current_round:
                    current_round = r
                    parts.append(f"\n### Round {r}\n")

                name = msg.get("agent_name", "?")
                role = msg.get("agent_role", "?")
                content = msg.get("content", "").strip()
                parts.append(
                    f'<post author="{name}" role="{role}" round="{r}">\n'
                    f"{content}\n"
                    f"</post>"
                )

        return "\n".join(parts)

    @staticmethod
    def _find_insert_position(messages: list) -> int:
        """Find position after the first system message."""
        for i, msg in enumerate(messages):
            role = msg.role if hasattr(msg, "role") else msg.get("role", "")
            if role != "system":
                return i
        return len(messages)
