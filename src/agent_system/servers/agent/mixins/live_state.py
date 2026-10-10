"""A request while it runs: cancelling it, messages appended to it, and the live state readers see.

The agent is one instance shared by every request of the process, so what a run holds while it runs
is kept per session (Agent.__init__ says why): the message list and tool schemas of the current
turn, read by compaction tools, token-counting hooks and a viewer that joins a session mid-run.
Messages sent to a running request wait in the session tracker and are drained into the
conversation between steps (docs/mid_run_message_injection.md); a cancel goes to the request
manager (docs/cancellation_architecture.md).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ....llm.models import ChatMessage

if TYPE_CHECKING:
    from ..server import Agent


def _live_field(live_state: Dict[str, Dict[str, Any]], session_id: Optional[str], field: str) -> Any:
    """One field of a session's live state, or None: the session is not tracked,
    or the field is not set (get_live_messages, get_live_tools_schema). A
    function of the state alone, not a method: callers bind those two to
    objects that carry only the state."""
    entry = live_state.get(session_id) if session_id else None
    return entry.get(field) if entry else None


class LiveStateMixin:
    """The requests in flight and their live state (see the module docstring).

    Relies on Agent.__init__ for ``_request_manager``, ``_session_tracker``,
    ``_live_state_by_session``, ``_live_state_max_sessions`` and the deprecated shared fields
    ``_current_messages``, ``_current_tools_schema`` and ``_current_held_back_schemas``.
    """

    async def cancel_request(self: Agent, request_id: str) -> bool:
        """
        Cancel an active request.

        Uses dual cancellation: global CancellationManager for tools +
        per-agent events for request loop. See docs/cancellation_architecture.md
        for design details.

        Args:
            request_id: The unique ID of the request to cancel

        Returns:
            True if the request was found and cancelled, False otherwise
        """
        return await self._request_manager.cancel_request(request_id)

    def _is_cancelled(self: Agent, request_id: Optional[str]) -> bool:
        """
        Check if a request has been cancelled.

        Args:
            request_id: The unique ID of the request to check

        Returns:
            True if the request has been cancelled, False otherwise
        """
        return self._request_manager.is_cancelled(request_id)
    
    async def append_user_message(self: Agent, request_id: str, content: str) -> bool:
        """
        Append a user message to an active request's conversation.
        Returns True if appended, False if request not found.
        """
        return await self._session_tracker.append_user_message(request_id, content)

    async def append_to_session(self: Agent, session_id: str, content: str) -> bool:
        """
        Append a user message directly to a persisted session.
        Returns True if appended, False if session not found.
        """
        return await self._session_tracker.append_to_session(session_id, content)

    async def _drain_appended_messages(self: Agent, request_id: str, messages: List[ChatMessage]) -> List[ChatMessage]:
        """
        Drain any appended messages for a request and add them to the conversation.
        Returns the updated messages list.
        """
        return await self._session_tracker.drain_appended_messages(request_id, messages)

    async def _take_in_late_messages(self: Agent, request_id: str, session_id: Optional[str],
                                     messages: List[ChatMessage]) -> List[ChatMessage]:
        """Messages appended too late for the run to act on, into its conversation.

        The live copy is refreshed with them: it was taken at the run's final, and the
        session load serves it until the run's job has ended -- without them the chat
        showed the turn with the message missing that its note said was kept.
        """
        held = len(messages)
        messages = await self._session_tracker.drain_appended_messages(request_id, messages)
        if len(messages) > held:
            self._set_live_messages(session_id, messages.copy())
        return messages

    # --- Per-session live conversation state (see Agent.__init__ for rationale) ----

    def _set_live_messages(self: Agent, session_id: Optional[str], messages: List[ChatMessage]) -> None:
        """Record the live message list for a session (request-scoped)."""
        # Keep the deprecated shared attr in sync for any unmigrated reader.
        self._current_messages = messages
        if not session_id:
            return
        entry = self._live_state_by_session.setdefault(session_id, {})
        entry["messages"] = messages
        self._evict_live_state(session_id)

    def _set_live_tools_schema(self: Agent, session_id: Optional[str], tools_schema: List[Dict[str, Any]],
                               held_back: Optional[List[Dict[str, Any]]] = None) -> None:
        """Record the live tool schema for a session (request-scoped), and the
        schemas tools.deferred holds back from it (get_run_tool_schemas)."""
        self._current_tools_schema = tools_schema
        self._current_held_back_schemas = list(held_back or [])
        if not session_id:
            return
        entry = self._live_state_by_session.setdefault(session_id, {})
        entry["tools_schema"] = tools_schema
        entry["held_back_schemas"] = self._current_held_back_schemas
        self._evict_live_state(session_id)

    def get_run_tool_schemas(self: Agent, session_id: Optional[str]) -> List[Dict[str, Any]]:
        """Every schema the current run of *session_id* may call: the live list,
        then every deferred schema (a loaded one comes twice; a lookup by name
        finds it either way).

        For a caller that dispatches by name without the model having loaded
        the tool (tool_script): it needs the tool's parameters either way. The
        shared, racy fields answer only when the session is not tracked."""
        entry = self._live_state_by_session.get(session_id) if session_id else None
        if entry is None:
            return list(self._current_tools_schema) + list(self._current_held_back_schemas)
        return list(entry.get("tools_schema") or []) + list(entry.get("held_back_schemas") or [])

    def _evict_live_state(self: Agent, keep_session: str) -> None:
        """Bound the per-session live-state dict (simple FIFO eviction)."""
        if len(self._live_state_by_session) <= self._live_state_max_sessions:
            return
        for sid in list(self._live_state_by_session.keys()):
            if len(self._live_state_by_session) <= self._live_state_max_sessions:
                break
            if sid != keep_session:
                self._live_state_by_session.pop(sid, None)

    def get_live_messages(self: Agent, session_id: Optional[str]) -> Optional[List[ChatMessage]]:
        """Live (current-turn) messages for a session, or None if not tracked.

        Session-correct replacement for reading agent._current_messages. The
        caller should fall back to the persisted session tracker when this is
        None (e.g. a tool invoked outside an active step loop).
        """
        return _live_field(self._live_state_by_session, session_id, "messages")

    def get_live_conversation(self: Agent, session_id: Optional[str]) -> Optional[List[ChatMessage]]:
        """The in-flight messages of a session as a READER should see them.

        ``get_live_messages`` hands out the run's own working list: it opens
        with the rendered system prompt and carries the notes the run wrote for
        this one call. Persistence drops both -- the prompt is rebuilt per turn
        from config, a volatile note belongs to the call it was built for -- so
        a viewer joining a session mid-run must have them dropped too, or the
        chat shows the agent its own system prompt as a message.

        Both rules are the ones persistence uses, not copies of them: a system
        message that IS conversation (an archived_ref, a prune breadcrumb) stays
        by ``is_compaction_system_message``, and the volatile notes go by
        ``is_volatile_note``.
        """
        live = self.get_live_messages(session_id)
        if live is None:
            return None
        from ..components.hook_integration import is_compaction_system_message
        from ..components.session_tracking import is_volatile_note
        return [msg for msg in live
                if not is_volatile_note(msg)
                and (getattr(msg, "role", None) != "system"
                     or is_compaction_system_message(msg))]

    def get_live_tools_schema(self: Agent, session_id: Optional[str]) -> Optional[List[Dict[str, Any]]]:
        """Live tool schema for a session, or None if not tracked."""
        return _live_field(self._live_state_by_session, session_id, "tools_schema")
