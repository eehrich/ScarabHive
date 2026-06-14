"""Debate Forum Plugin - Hook for injecting debate context into sub-agents.

Two-tier injection strategy:
- **Pinned messages + channel metadata** → ``role="system"`` (re-injected fresh
  on every LLM call). System role guarantees compaction-safety
  (context_engineer's ``keep_system_messages=True`` never archives system
  messages). Pinned-set changes rarely (only on explicit pin_message events),
  so prompt-cache stays warm between turns.
- **Unpinned forum posts** → ``role="user"`` (permanent, persisted in session).
  Only NEW messages since the last hook call are added (diff-based).
  Context optimiser plugins (context_engineer, context_summarizer) can compress
  older batches over time — no sliding-window limit needed.

Diff tracking:
  ``debate_last_injected_msg_id`` is stored in session template vars via the
  SessionTracker.  This survives context compression (the tracker lives outside
  the message list).

Flow:
1. Moderator sets context_var ``debate_channel_id`` (via task_switch/set_context)
2. Sub-agents inherit it when spawned
3. This hook injects pinned context (system) and new posts (user)
4. Old post batches stay in the conversation and get optimised automatically
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
INJECTION_MARKER_POSTS = "debate_forum_posts"


class DebateForumHooks(SchemaBasedPluginHook):
    """Hook plugin for debate forum context injection."""

    def __init__(self, plugin_dir: Path, db: "DebateForumDB", plugin_config: dict | None = None):
        super().__init__(plugin_dir)
        self.db = db
        # Merge config from plugins.yaml over schema defaults
        if plugin_config:
            self._config.update(plugin_config)

    async def inject_debate_context(self, context: HookContext) -> HookResult:
        """Inject debate forum context before LLM call.

        Two-tier injection:
        1. Pinned messages + channel metadata → system message
           (compaction-safe; cache stays warm between unchanged pin-sets)
        2. New forum posts since last call → user message (permanent)
        """
        logger.debug(
            "[DebateForumHook] inject_debate_context called for agent=%s session=%s",
            context.agent_name, context.session_id,
        )
        if not context.messages or not context.session_id:
            return HookResult(success=True, modified=False, context=context)

        try:
            channel_id = self._get_channel_id(context)
            if not channel_id:
                logger.debug("[DebateForumHook] No debate_channel_id in context_vars, skipping")
                return HookResult(success=True, modified=False, context=context)

            channel = self.db.get_channel(channel_id)
            pinned_messages = self.db.get_pinned_messages(channel_id)
            pinned_ids = {m["id"] for m in pinned_messages}

            from agent_system.llm.models import ChatMessage

            modified = False

            # ── 1. Pinned + metadata → system injection (compaction-safe) ─────
            # Remove previous injection (matched by injected_by marker)
            for i in range(len(context.messages) - 1, -1, -1):
                if getattr(context.messages[i], "injected_by", None) == INJECTION_MARKER:
                    context.messages.pop(i)

            has_metadata = channel and (channel.get("topic") or channel.get("context"))
            if pinned_messages or has_metadata:
                pinned_text = self._format_pinned_context(
                    pinned_messages, channel_id, channel
                )
                insert_pos = self._find_pinned_insert_position(context.messages)
                context.messages.insert(
                    insert_pos,
                    ChatMessage(
                        role="system",
                        content=pinned_text,
                        injected_by=INJECTION_MARKER,
                    ),
                )
                modified = True

            # ── 2. New posts → user injection (permanent, diff-based) ─────
            # The counter ``debate_last_injected_msg_id`` is a per-session
            # diff marker. Sub-agents inherit ``context_vars`` from their
            # parent at spawn time (sub_agent_manager.manager._create_sub_session),
            # which means the parent's counter leaks into fresh sub-sessions
            # and causes them to skip messages that were posted to the
            # channel BEFORE the sub-agent was spawned. Symptom observed:
            # Falk (Provocateur) sub-agent saw only the latest Autor-C
            # synopsis post because the moderator's counter was already
            # at Autor-B's msg_id when Falk was spawned.
            #
            # Fix: tag the counter with the owning session_id. If the
            # counter belongs to a different session (i.e. inherited from
            # parent), treat this as a fresh session and replay the full
            # channel history.
            session_vars = context.agent._session_tracker.get_session_template_vars(
                context.session_id
            )
            counter_owner = session_vars.get("debate_counter_owner")
            if counter_owner == context.session_id:
                last_injected_id = int(session_vars.get("debate_last_injected_msg_id", 0))
            else:
                # Inherited (or no) counter — replay full channel history
                last_injected_id = 0
                if counter_owner is not None:
                    logger.debug(
                        "[DebateForumHook] Counter inherited from session %s "
                        "into %s — resetting to replay channel #%d",
                        counter_owner, context.session_id, channel_id,
                    )

            all_messages = self.db.get_messages(channel_id, limit=0)
            new_messages = [
                m for m in all_messages
                if m["id"] not in pinned_ids and m["id"] > last_injected_id
            ]

            if new_messages:
                posts_text = self._format_new_posts(new_messages, channel_id)
                insert_pos = self._find_last_user_position(context.messages)
                context.messages.insert(
                    insert_pos,
                    ChatMessage(
                        role="user",
                        content=posts_text,
                        injected_by=INJECTION_MARKER_POSTS,
                    ),
                )
                max_id = max(m["id"] for m in new_messages)
                context.agent._session_tracker.set_session_template_vars(
                    context.session_id,
                    {
                        "debate_last_injected_msg_id": max_id,
                        "debate_counter_owner": context.session_id,
                    },
                )
                modified = True
                logger.info(
                    f"[DebateForumHook] Injected {len(new_messages)} new posts "
                    f"(msg_id {last_injected_id + 1}..{max_id}) from channel #{channel_id}"
                )
            elif counter_owner != context.session_id:
                # No new messages but we still need to claim ownership so
                # the next call doesn't see a stale inherited counter.
                context.agent._session_tracker.set_session_template_vars(
                    context.session_id,
                    {"debate_counter_owner": context.session_id},
                )

            if pinned_messages:
                logger.info(
                    f"[DebateForumHook] Injected {len(pinned_messages)} pinned messages "
                    f"from channel #{channel_id}"
                )

            return HookResult(success=True, modified=modified, context=context)

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
    def _format_pinned_context(
        pinned: list[dict[str, Any]],
        channel_id: int,
        channel: dict[str, Any] | None = None,
    ) -> str:
        """Format pinned messages and channel metadata for user injection."""
        ch_name = channel.get("name", "") if channel else ""
        header = f"## Debate Forum – Channel #{channel_id}"
        if ch_name:
            header += f" ({ch_name})"
        parts = [header]

        if channel:
            topic = channel.get("topic", "")
            context = channel.get("context", "")
            if topic:
                parts.append(f"**Topic:** {topic}")
            if context:
                parts.append(f"**Context:** {context}")

        if pinned:
            parts.append("\n📌 **Pinned messages (always visible):**\n")
            for msg in pinned:
                name = msg.get("agent_name", "?")
                role = msg.get("agent_role", "?")
                content = msg.get("content", "").strip()
                parts.append(
                    f'<post author="{name}" role="{role}" pinned="true">\n'
                    f"{content}\n"
                    f"</post>"
                )

        return "\n".join(parts)

    @staticmethod
    def _format_new_posts(
        messages: list[dict[str, Any]],
        channel_id: int,
    ) -> str:
        """Format new forum posts for permanent user injection."""
        parts = [f"[Debate-Forum Channel #{channel_id} – Neue Beiträge]\n"]

        current_round = None
        for msg in messages:
            r = msg.get("round", 0)
            if r != current_round:
                current_round = r
                parts.append(f"\n### Runde {r}\n")

            name = msg.get("agent_name", "?")
            role = msg.get("agent_role", "?")
            content = msg.get("content", "").strip()
            msg_id = msg.get("id", "?")
            parts.append(
                f'<post author="{name}" role="{role}" round="{r}" msg_id="{msg_id}">\n'
                f"{content}\n"
                f"</post>"
            )

        return "\n".join(parts)

    @staticmethod
    def _find_pinned_insert_position(messages: list) -> int:
        """Find position for pinned context: after system messages but before
        the first user task message, so the agent always sees pinned context
        before any debate posts."""
        after_system = 0
        for i, msg in enumerate(messages):
            role = msg.role if hasattr(msg, "role") else msg.get("role", "")
            if role != "system":
                after_system = i
                break
        else:
            after_system = len(messages)
        return after_system

    @staticmethod
    def _find_last_user_position(messages: list) -> int:
        """Find position of the last user message (to insert new posts before it)."""
        for i in range(len(messages) - 1, -1, -1):
            role = messages[i].role if hasattr(messages[i], "role") else messages[i].get("role", "")
            if role == "user":
                return i
        return len(messages)
