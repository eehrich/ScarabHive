"""Debate Forum Plugin - Hook for injecting debate context into sub-agents.

Two-tier injection strategy:
- **Pinned messages + channel metadata** → ``role="developer"``, appended
  when they change. They used to be re-inserted behind the system prompt on
  every call, where a provider hoists them into the prompt head: the pinned
  set changes rarely, but the block was rebuilt every call and every
  difference invalidated the cache for everything behind it. Appended, it
  leaves the whole history before it byte-identical. Its role no longer makes
  it compaction-safe, and it need not be: a block that is no longer in the
  history is simply appended again, the same branch as the first one.
- **Unpinned forum posts** → ``role="user"`` (permanent, persisted in session).
  Only NEW posts, and the new part of posts that grew by an appended chunk,
  are added (diff-based). Context optimiser plugins (context_engineer,
  context_summarizer) can compress older batches over time — no
  sliding-window limit needed.

Diff tracking:
  ``debate_counters`` (owner session, per channel each post given and its
  length) is stored in session template vars via the SessionTracker. This
  survives context compression (the tracker lives outside the message list).

Flow:
1. Moderator sets context_var ``debate_channel_id`` (via task_switch/set_context)
2. Sub-agents inherit it when spawned
3. This hook injects pinned context (developer) and new posts (user)
4. Old post batches stay in the conversation and get optimised automatically

Direct messages between sessions (deliver_direct_messages) need no channel
var; who runs where and waking idle sessions are core/session_presence/.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING

from agent_system.hooks import SchemaBasedPluginHook, HookContext, HookResult
from agent_system.llm.message_roles import DEVELOPER, is_input

if TYPE_CHECKING:
    from .database import DebateForumDB

logger = logging.getLogger(__name__)

INJECTION_MARKER = "inject_debate_context"
INJECTION_MARKER_POSTS = "debate_forum_posts"
INJECTION_MARKER_DIRECT = "debate_forum_direct"
# The session var noting what the session has been given (see inject_debate_context)
DEBATE_COUNTERS = "debate_counters"


class DebateForumHooks(SchemaBasedPluginHook):
    """Hook plugin for debate forum context injection."""

    def __init__(self, plugin_dir: Path, db: "DebateForumDB", plugin_config: dict | None = None,
                 tool_prefix: str = "debate_forum"):
        super().__init__(plugin_dir)
        self.db = db
        # The plugin instance name: its tools are <name>_send_message, and a
        # text that names a tool the agent does not have is worse than no hint.
        self.tool_prefix = tool_prefix
        # request_id -> the direct messages that request carries, until it ends
        # (mark_direct_messages_delivered)
        self._handed_over: dict[str, list[int]] = {}
        # Merge config from plugins.yaml over schema defaults
        if plugin_config:
            self._config.update(plugin_config)

    async def inject_debate_context(self, context: HookContext) -> HookResult:
        """Inject debate forum context before LLM call.

        Two-tier injection:
        1. Pinned messages + channel metadata: appended as a developer turn,
           and only when they changed. Not compaction-safe by its role any
           more, and it need not be -- a block that is gone is appended again.
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

            # 1. Pinned + metadata: appended when they change. Never
            # rewritten -- an earlier pinned block was true when it was
            # written, and deleting it would change the prefix the provider
            # has already cached.
            #
            # This DROPS an invariant the old code kept by searching for an
            # insert position: pinned context used to sit in front of the
            # debate posts, because the agent should know topic and rules
            # before it reads the debate. The posts still go before the last
            # user message (step 2), so pinned now comes AFTER them. The trade
            # is deliberate: an insert position that moves with the turn is
            # exactly what has to be rewritten on every call, and the pinned
            # block loses nothing by being last -- it is the closest thing to
            # the answer, and both are in the same prompt anyway.
            previous_pinned = next(
                (msg for msg in reversed(context.messages)
                 if getattr(msg, "injected_by", None) == INJECTION_MARKER), None)

            has_metadata = channel and (channel.get("topic") or channel.get("context"))
            if pinned_messages or has_metadata:
                pinned_text = self._format_pinned_context(
                    pinned_messages, channel_id, channel
                )
                if previous_pinned is None or previous_pinned.content != pinned_text:
                    context.messages.append(
                        ChatMessage(
                            role=DEVELOPER,
                            content=pinned_text,
                            injected_by=INJECTION_MARKER,
                        ),
                    )
                    modified = True

            # ── 2. New posts and new chunks → user injection (permanent, diff-based) ──
            # What the session has been given is noted in ONE session var,
            # DEBATE_COUNTERS = {"owner": session_id, "channels": {channel_id:
            # {msg_id: length handed over}}}:
            # - owner: sub-agents inherit context_vars from their parent at spawn,
            #   and a continued one gets the parent's live values merged in
            #   (sub_agent_manager merge_parent_context_vars) for every key it has
            #   not changed itself. A note owned by another session counts as none,
            #   so the channel is replayed. One composite var, not several: the
            #   merge takes a key as the child's own only as a whole.
            # - per channel: message ids are global, so an id reached in one
            #   channel says nothing about another.
            # - per post the length handed over: a chunk appended later
            #   (post_message append=true) is handed over as a continuation.
            tracker = context.agent._session_tracker
            session_vars = tracker.get_session_template_vars(context.session_id)
            given = self._given_posts(session_vars, context.session_id, channel_id)

            fresh, grown = [], []
            for m in self.db.get_messages(channel_id, limit=0):
                handed = given.get(str(m["id"]))
                if m["id"] in pinned_ids:
                    continue  # whole in the channel block; the rest follows if it is unpinned
                if handed is None:
                    fresh.append(m)
                elif len(m["content"]) > handed:
                    grown.append({**m, "content": m["content"][handed:], "continues": True})

            if fresh or grown:
                batch = sorted(fresh + grown, key=lambda m: m["id"])
                insert_pos = self._find_last_user_position(context.messages)
                if any(m.role in ("assistant", "tool") for m in context.messages[insert_pos + 1:]):
                    # inside a running turn: what follows the input was sent already and is cached --
                    # in front of it the posts would move all of it, on every new post
                    insert_pos = len(context.messages)
                context.messages.insert(
                    insert_pos,
                    ChatMessage(
                        role="user",
                        content=self._format_new_posts(batch, channel_id),
                        injected_by=INJECTION_MARKER_POSTS,
                    ),
                )
                note = session_vars.get(DEBATE_COUNTERS)
                channels = dict(note["channels"]) if self._owned(note, context.session_id) else {}
                channels[str(channel_id)] = {
                    **given, **{str(m["id"]): len(m["content"]) + given.get(str(m["id"]), 0)
                                for m in batch}}
                tracker.set_session_template_vars(
                    context.session_id,
                    {DEBATE_COUNTERS: {"owner": context.session_id, "channels": channels}},
                )
                modified = True
                logger.info(
                    "[DebateForumHook] Injected %d new post(s) and %d continuation(s) "
                    "from channel #%d", len(fresh), len(grown), channel_id)

            if pinned_messages:
                logger.info(
                    f"[DebateForumHook] Injected {len(pinned_messages)} pinned messages "
                    f"from channel #{channel_id}"
                )

            return HookResult(success=True, modified=modified, context=context)

        except Exception as e:
            logger.error(f"[DebateForumHook] Failed: {e}", exc_info=True)
            return HookResult(success=True, modified=False, context=context)

    async def deliver_direct_messages(self, context: HookContext) -> HookResult:
        """Hand the session the direct messages sent to it, appended to the request."""
        if not context.session_id or context.messages is None:
            return HookResult(success=True, modified=False, context=context)
        try:
            handed = self._handed_over.get(context.request_id, [])
            direct = [m for m in self.db.undelivered(context.session_id)
                      if m["id"] not in handed]
            if not direct:
                return HookResult(success=True, modified=False, context=context)

            from agent_system.llm.models import ChatMessage

            context.messages.append(ChatMessage(
                role="user", content=self._format_direct(direct, self._can_reply(context)),
                injected_by=INJECTION_MARKER_DIRECT,
            ))
            # Still undelivered in the store: only the end of this request says
            # they reached the session. Until then every further step of the
            # same request would read them again, hence the list.
            self._handed_over[context.request_id] = handed + [m["id"] for m in direct]
            logger.info("[DebateForumHook] Delivered %d direct message(s) to session %s",
                        len(direct), context.session_id)
            return HookResult(success=True, modified=True, context=context)
        except Exception as e:
            logger.error("[DebateForumHook] Direct messages failed: %s", e, exc_info=True)
            return HookResult(success=True, modified=False, context=context)

    async def mark_direct_messages_delivered(self, context: HookContext) -> HookResult:
        """The request is over and its conversation is saved, so what it carried
        counts as delivered. A run that died before this, or whose save never
        happened, hands its messages to the session's next run instead of losing
        them: what it was told is only in the answer it never wrote."""
        handed = self._handed_over.pop(context.request_id, None)
        if handed and not (context.metadata or {}).get("persisted"):
            logger.info("[DebateForumHook] Session %s was not saved; its %d direct "
                        "message(s) stay undelivered for its next run",
                        context.session_id, len(handed))
            return HookResult(success=True, modified=False, context=context)
        if handed:
            try:
                self.db.mark_delivered(handed)
            except Exception as e:
                logger.error("[DebateForumHook] Could not mark direct messages delivered: %s",
                             e, exc_info=True)
        return HookResult(success=True, modified=False, context=context)

    @staticmethod
    def _owned(note: Any, session_id: str) -> bool:
        return (isinstance(note, dict) and note.get("owner") == session_id
                and isinstance(note.get("channels"), dict))

    def _given_posts(self, session_vars: dict, session_id: str, channel_id: int) -> dict[str, int]:
        """{msg_id: length handed over} of the channel's posts this session was given."""
        note = session_vars.get(DEBATE_COUNTERS)
        if note is not None:
            return dict(note["channels"].get(str(channel_id), {})) if self._owned(note, session_id) else {}
        # Written before the note was one var: a counter of the last post given
        # and the session that owns it. The posts up to it count as given in
        # full -- how much of a post was given was not noted.
        if session_vars.get("debate_counter_owner") != session_id:
            return {}
        last = int(session_vars.get("debate_last_injected_msg_id") or 0)
        return {str(m["id"]): len(m["content"])
                for m in self.db.get_messages(channel_id, limit=0) if m["id"] <= last}

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
        """Format new forum posts, and the new part of posts given before, for permanent user injection."""
        parts = [f"[Debate-Forum Channel #{channel_id} – New posts]\n"]

        current_round = None
        for msg in messages:
            r = msg.get("round", 0)
            if r != current_round:
                current_round = r
                parts.append(f"\n### Round {r}\n")

            name = msg.get("agent_name", "?")
            role = msg.get("agent_role", "?")
            content = msg.get("content", "").strip()
            msg_id = msg.get("id", "?")
            continues = f' continuation_of="{msg_id}"' if msg.get("continues") else ""
            parts.append(
                f'<post author="{name}" role="{role}" round="{r}" msg_id="{msg_id}"{continues}>\n'
                f"{content}\n"
                f"</post>"
            )

        return "\n".join(parts)

    def _can_reply(self, context: HookContext) -> bool:
        """Does the agent have the tool that answers? Unknown (no tool list) counts as yes."""
        if context.tools_schema is None:
            return True
        name = f"{self.tool_prefix}_send_message"
        return any((tool.get("function") or {}).get("name") == name
                   for tool in context.tools_schema if isinstance(tool, dict))

    def _format_direct(self, messages: list[dict[str, Any]], can_reply: bool = True) -> str:
        """The messages as the session sees them; with the tool that answers only
        when the agent has it -- a hint naming a tool it lacks is worse than none."""
        parts = [f"[Direct messages -- reply with {self.tool_prefix}_send_message "
                 "to the from_session]" if can_reply else "[Direct messages]"]
        for msg in messages:
            parts.append(
                f'<message from_session="{msg["agent_role"]}" from_agent="{msg["agent_name"]}">\n'
                f'{msg["content"].strip()}\n'
                f"</message>"
            )
        return "\n".join(parts)

    @staticmethod
    def _find_last_user_position(messages: list) -> int:
        """Where new posts go: in front of the last thing being answered.

        `is_input`, not `role == "user"` and not `opens_a_turn`. New posts are
        delivered to the CURRENT tail, so the anchor has to find a woken run's
        `developer` wake as well as the marked user messages this plugin and
        the loop append inside a turn (a direct message, a continuation nudge,
        an earlier batch of posts). On the head of the turn they would land in
        front of the whole exchange that already answered it.
        """
        for i in range(len(messages) - 1, -1, -1):
            if is_input(messages[i]):
                return i
        return len(messages)


