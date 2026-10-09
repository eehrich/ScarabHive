"""Saving the session a run has: into the tracker, to disk, and by checkpoints while it runs.

THE one sequence that writes a run's conversation (_persist_conversation: drop what is rebuilt each
turn, set the tracker's messages, save the file) and the disk save behind it, used at three points
of a run and by the programmatic tool dispatch; and the checkpoint loop that keeps a long run's
session file reload-safe. One module, so that every place that writes a session goes by the same
rules.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, List, Optional

from ....llm.models import ChatMessage

if TYPE_CHECKING:
    from ..server import Agent

logger = logging.getLogger(__name__)


class PersistenceMixin:
    """The session's saves (see the module docstring).

    Relies on Agent.__init__ for ``name``, ``agent_config``, ``_session_tracker`` and
    ``_session_service``.
    """

    async def _save_session_to_disk(self: Agent, session_id: str) -> bool:
        """Persist a session to disk via SessionService (no-op without service
        or metadata). Shared by the turn-persistence helper and the
        compacted-messages branch in _finalize_request. Returns whether the
        session file was written: what a request carried only counts as
        delivered once it is (see _finalize_request)."""
        if not self._session_service:
            logger.debug("No session_service available, skipping disk save")
            return False
        session_meta = self._session_tracker.get_session_metadata(session_id)
        if not session_meta:
            logger.warning(f"No session metadata found for {session_id}, skipping disk save")
            return False
        written = await self._session_service.save_session(
            agent=self,
            user_id=session_meta.get("user_id", "anonymous"),
            session_id=session_id,
            agent_name=session_meta.get("agent_name", self.name),
            llm_profile=session_meta.get("llm_profile", self.agent_config.default_llm_profile),
            was_new_session=False,  # Always update for intermediate/final saves
            # whoever opened the run named its choice (the API, the chat); one who did not leaves the record's
            llm_choice=session_meta.get("llm_choice"),
        )
        logger.debug(f"Saved session {session_id} to disk")
        return bool(written)

    async def _persist_conversation(self: Agent, session_id: str, messages: List[ChatMessage],
                                    *, to_disk: bool, note: str) -> bool:
        """Persist the conversation (non-system messages) to the in-memory
        tracker and optionally to disk — THE single implementation of the
        'filter system → set_session_messages → save_session' sequence that was
        copied at three points of the request lifecycle (after LLM response,
        after a completed tool turn, at request finalization). Never raises:
        persistence failures must not kill a running request -- it returns
        whether the session file was written instead, for callers that must not
        promise what the disk did not take."""
        from ..components.hook_integration import is_compaction_system_message
        try:
            # System messages are rebuilt from config each turn and must not be
            # persisted — EXCEPT the ones that are compacted conversation
            # (archive pointers, the prune breadcrumb). Dropping those loses
            # conversation state for good: the content is in a store, but
            # nothing left in the session says it exists.
            # Volatile developer notes are dropped one level down, by
            # set_session_messages: five places write session messages and this
            # is only one of them.
            conversation_msgs = [
                msg for msg in messages
                if msg.role != "system" or is_compaction_system_message(msg)
            ]
            self._session_tracker.set_session_messages(session_id, conversation_msgs.copy())
            logger.debug(f"Persisted session {session_id} ({note}) with {len(conversation_msgs)} messages")
            if to_disk:
                return await self._save_session_to_disk(session_id)
        except Exception as e:
            logger.warning(f"Failed to persist session {session_id} ({note}): {e}", exc_info=True)
        return False

    def _start_checkpoint_loop(self: Agent, session_id: str) -> Optional[asyncio.Task]:
        """Start a background checkpoint loop so long-running tool calls don't
        leave the session unsaved on disk. The loop persists messages up to
        the last consistent tool_call/tool_result boundary, so the file is
        always reload-safe (orphan-free).

        The loop this run started, or None when one runs for the session already
        (another run of this process on the same session, a nested one say):
        _finalize_request stops this one and no other."""
        if not (self._session_service and session_id and self._session_tracker is not None):
            return None
        try:
            meta = self._session_tracker.get_session_metadata(session_id) or {}
            return self._session_service.start_checkpoint_loop(
                self, meta.get("user_id", "anonymous"), session_id)
        except Exception as e:
            logger.debug(f"Could not start checkpoint loop for session {session_id}: {e}")
            return None
