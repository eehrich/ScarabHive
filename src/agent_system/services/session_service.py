"""
Session Service - Centralized session management logic.

This service handles loading and saving sessions for both API and CLI contexts.
It provides a clean interface for session restoration and persistence.
"""

import asyncio
import logging
from typing import List, Dict, Any, Optional

from agent_system.services.session_manager import SessionPermissionError, SessionNotFoundError

logger = logging.getLogger(__name__)


# Default checkpoint interval. Tunable via SessionService init.
DEFAULT_CHECKPOINT_INTERVAL_SECONDS = 30


def _trim_to_safe_boundary(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Trim a message list to the last consistent tool_call/tool_result boundary.

    Walks the messages forward, tracking which assistant tool_call IDs have
    matching tool result messages. The "last safe boundary" is the position
    after the most recent message where every tool_call seen so far has been
    answered. Anything after that boundary (typically a pending assistant
    tool_call whose tool is still running) is dropped.

    The result is reload-safe: every assistant tool_call has a matching tool
    message, so the LLM-loop can pick the conversation back up cleanly.
    """
    open_ids: set[str] = set()
    last_safe_end = 0
    for i, msg in enumerate(messages):
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                tc_id = tc.get("id") if isinstance(tc, dict) else None
                if tc_id:
                    open_ids.add(tc_id)
        elif role == "tool":
            tcid = msg.get("tool_call_id")
            if tcid:
                open_ids.discard(tcid)
        if not open_ids:
            last_safe_end = i + 1
    return messages[:last_safe_end]


def _msg_to_dict(msg: Any) -> Dict[str, Any]:
    """Convert a ChatMessage (or compatible) into a JSON-ready dict."""
    if isinstance(msg, dict):
        return msg
    if hasattr(msg, "model_dump"):
        return msg.model_dump(mode="json")
    if hasattr(msg, "dict"):
        return msg.dict()
    return dict(msg)


def _estimate_message_tokens(msg_dict: Dict[str, Any]) -> int:
    """Estimate token count for a message.
    
    Uses ~4 chars per token approximation for text content.
    Tool calls and multimodal content are handled separately.
    
    Args:
        msg_dict: Message as dictionary
        
    Returns:
        Estimated token count
    """
    tokens = 0
    
    # Content tokens
    content = msg_dict.get('content') or ''
    if isinstance(content, str):
        tokens += len(content) // 4
    elif isinstance(content, list):
        # Multimodal content
        for part in content:
            if isinstance(part, dict):
                if part.get('type') == 'text':
                    tokens += len(part.get('text', '')) // 4
                elif part.get('type') in ('image_url', 'image'):
                    # Images cost ~1000 tokens (approximate)
                    tokens += 1000
            elif isinstance(part, str):
                tokens += len(part) // 4
    else:
        # Fallback: serialize to JSON
        import json
        tokens += len(json.dumps(content)) // 4
    
    # Tool calls tokens (assistant messages with function calls)
    tool_calls = msg_dict.get('tool_calls')
    if tool_calls:
        import json
        for tc in tool_calls:
            tc_str = json.dumps(tc) if isinstance(tc, dict) else str(tc)
            tokens += len(tc_str) // 4
    
    # Reasoning content (if present). It sits either on the message or inside
    # the reasoning artifacts — one home at a time, so ask the shared reader
    # rather than one field name.
    from agent_system.utils.reasoning_artifacts import thinking_text
    reasoning = thinking_text(msg_dict)
    if reasoning:
        tokens += len(reasoning) // 4
    
    return max(1, tokens)  # At least 1 token per message


class SessionService:
    """Service for managing session loading, restoration, and saving."""

    def __init__(self, session_manager, checkpoint_interval_seconds: int = DEFAULT_CHECKPOINT_INTERVAL_SECONDS):
        """
        Initialize the session service.

        Args:
            session_manager: SessionManager instance for persistence operations
            checkpoint_interval_seconds: Period of background checkpoint saves while
                a request is active. Set to 0 to disable.
        """
        self.session_manager = session_manager
        self.checkpoint_interval_seconds = checkpoint_interval_seconds
        # session_id -> asyncio.Task running the checkpoint loop
        self._checkpoint_tasks: Dict[str, asyncio.Task] = {}

    async def load_and_restore_session(
        self,
        agent,
        user_id: str,
        session_id: str
    ) -> tuple[bool, int]:
        """
        Load a session from storage and restore it to the agent.

        Args:
            agent: The agent instance to restore the session to
            user_id: User ID who owns the session
            session_id: Session ID to load

        Returns:
            Tuple of (session_exists: bool, message_count: int)
            - session_exists: True if session was found and loaded
            - message_count: Number of messages restored to agent
        """
        if not self.session_manager:
            logger.warning("No session manager available, cannot load session")
            return False, 0

        try:
            session_data = await self.session_manager.load_session(user_id, session_id)

            if not session_data.get("messages"):
                logger.debug(f"[SESSION] Session {session_id} found but has no messages")
                return True, 0

            # Convert dict messages to ChatMessage objects
            from agent_system.llm.models import ChatMessage
            messages_objects = []

            from agent_system.utils.json_utils import history_safe_tool_calls

            for msg_dict in session_data["messages"]:
                try:
                    # Sanitize on restore: invalid arguments JSON in persisted
                    # tool calls poisons every later request of the session.
                    if isinstance(msg_dict, dict) and msg_dict.get("tool_calls"):
                        msg_dict = {**msg_dict, "tool_calls":
                                    history_safe_tool_calls(msg_dict["tool_calls"])}
                    # ChatMessage can be constructed from dict
                    chat_msg = ChatMessage(**msg_dict)
                    messages_objects.append(chat_msg)
                except Exception as e:
                    logger.warning(f"Failed to convert message to ChatMessage: {e}, skipping")

            # Restore conversation history to agent using component API
            agent._session_tracker.set_session_messages(session_id, messages_objects)

            # Store session metadata for later use (e.g., user_id for tool execution context)
            agent._session_tracker.set_session_metadata(session_id, {
                "user_id": user_id,
                "agent_name": session_data.get("agent_name"),
                "llm_profile": session_data.get("llm_profile")
            })

            # Restore context_vars to SESSION-SCOPED template_vars (NOT agent.agent_config!)
            # CRITICAL: This ensures session isolation - multiple sessions using the same
            # agent singleton won't contaminate each other's template vars (e.g., workflow_phase)
            context_vars = session_data.get("context_vars", {})
            if context_vars:
                agent._session_tracker.set_session_template_vars(session_id, context_vars)
                logger.debug(f"[SESSION] Restored context_vars to session template_vars: {list(context_vars.keys())}")
            else:
                # No context_vars in session - initialize from agent config defaults
                # This ensures defaults (e.g., workflow_phase: "planning") are available
                if hasattr(agent, 'agent_config') and agent.agent_config:
                    if hasattr(agent.agent_config, 'template_vars') and agent.agent_config.template_vars:
                        default_vars = agent.agent_config.template_vars.copy()
                        if default_vars:
                            agent._session_tracker.set_session_template_vars(session_id, default_vars)
                            logger.debug(f"[SESSION] Initialized session template_vars from agent config defaults: {list(default_vars.keys())}")

            logger.debug(f"[SESSION] Loaded session {session_id} with {len(messages_objects)} messages")
            return True, len(messages_objects)

        except SessionPermissionError:
            # Re-raise permission errors - user trying to access session they don't own
            raise
        except SessionNotFoundError:
            # Session doesn't exist - return False so caller can create new one
            logger.debug(f"[SESSION] Session {session_id} not found")
            return False, 0
        except Exception as e:
            # Other errors (e.g., corrupt session file) - log and return False
            logger.error(f"[SESSION] Failed to load session {session_id}: {e}", exc_info=True)
            return False, 0

    async def save_session(
        self,
        agent,
        user_id: str,
        session_id: str,
        agent_name: str,
        llm_profile: str,
        was_new_session: bool,
        title: Optional[str] = None
    ) -> bool:
        """
        Save or update a session to storage.

        Args:
            agent: The agent instance with session messages
            user_id: User ID who owns the session
            session_id: Session ID to save
            agent_name: Name of the agent used
            llm_profile: LLM profile used
            was_new_session: True if this is a new session (create), False for update
            title: Explicit session title (overrides the auto-extracted one)

        Returns:
            True if save was successful, False otherwise
        """
        if not self.session_manager:
            logger.warning("No session manager available, cannot save session")
            return False

        try:
            # Get messages from agent using component API
            messages_list = agent._session_tracker.get_session_messages(session_id)
            if not messages_list:
                logger.debug(f"[SESSION] No messages in session {session_id}, skipping save")
                return False

            logger.debug(f"[SESSION] Saving session {session_id}, messages count: {len(messages_list)}")

            # Convert ChatMessage objects to dicts with token estimation
            messages_dicts = []
            for msg in messages_list:
                if hasattr(msg, 'model_dump'):
                    msg_dict = msg.model_dump(mode='json')
                elif hasattr(msg, 'dict'):
                    msg_dict = msg.dict()
                else:
                    msg_dict = dict(msg)
                
                # Add estimated token count if not already present
                if 'estimated_tokens' not in msg_dict:
                    msg_dict['estimated_tokens'] = _estimate_message_tokens(msg_dict)
                
                messages_dicts.append(msg_dict)

            # Explicit title wins; otherwise derive it from the first user message
            explicit_title = title
            title = explicit_title or self._extract_session_title(messages_dicts)

            # Check if session already exists and find its owner
            # This is critical for sub-agent sessions which may have different user_ids
            session_owner = await self.session_manager._find_session_owner_async(session_id)
            session_exists = session_owner is not None

            # Use the actual owner's user_id for existing sessions
            actual_user_id = session_owner if session_exists else user_id

            # Save or update session
            if not session_exists:
                logger.debug(f"[SESSION] Creating new session {session_id} for user {actual_user_id}")
                try:
                    session_data = await self.session_manager.create_session(
                        session_id=session_id,
                        user_id=actual_user_id,
                        title=title,
                        agent_name=agent_name,
                        llm_profile=llm_profile
                    )
                except ValueError as create_err:
                    if "already exists" in str(create_err):
                        # Race condition: _find_session_owner_async missed the file
                        # but create_session found it. Fall through to update path.
                        logger.debug(
                            f"[SESSION] Session {session_id} exists after all (race), "
                            f"falling back to update path"
                        )
                        actual_user_id = (
                            await self.session_manager._find_session_owner_async(session_id)
                            or user_id
                        )
                        session_exists = True
                    else:
                        raise

            if session_exists:
                # Load existing session, preserving parent_session, depth, context_vars, etc.
                session_data = await self.session_manager.load_session(actual_user_id, session_id)
                session_data["messages"] = messages_dicts
                if explicit_title:
                    session_data["title"] = explicit_title
                else:
                    # Only update title if it's still the default auto-generated title
                    extracted_title = self._extract_session_title(messages_dicts)
                    if session_data.get("title") == extracted_title or not session_data.get("title"):
                        session_data["title"] = extracted_title
                # CRITICAL: Always update agent_name and llm_profile from current request
                session_data["agent_name"] = agent_name
                session_data["llm_profile"] = llm_profile
            else:
                # Truly new session (create_session succeeded above)
                session_data["messages"] = messages_dicts

                # Initialize context_vars from agent config defaults if not already set
                if "context_vars" not in session_data or not session_data["context_vars"]:
                    if hasattr(agent, 'agent_config') and agent.agent_config:
                        if hasattr(agent.agent_config, 'template_vars') and agent.agent_config.template_vars:
                            session_data["context_vars"] = agent.agent_config.template_vars.copy()
                            logger.debug(f"[SESSION] Initialized context_vars from agent config: {list(session_data['context_vars'].keys())}")

            # Sync runtime template_vars back into persisted context_vars.
            # Plugins may update session-scoped template_vars during the run via
            # agent._session_tracker.set_session_template_vars(...) without writing
            # session_data["context_vars"] directly. Without this sync those updates
            # would never reach the persisted session and the Session Info panel would
            # show stale or empty values for non-pipeline sessions.
            session_tracker = getattr(agent, "_session_tracker", None)
            if session_tracker is not None:
                try:
                    runtime_vars = session_tracker.get_session_template_vars(session_id)
                except Exception as tracker_err:
                    logger.debug(f"[SESSION] Could not read runtime template_vars: {tracker_err}")
                    runtime_vars = None
                if runtime_vars:
                    existing = session_data.get("context_vars")
                    if not isinstance(existing, dict):
                        existing = {}
                    existing.update(runtime_vars)
                    session_data["context_vars"] = existing

            # Save back
            await self.session_manager.save_session(session_data)

            logger.debug(f"[SESSION] Session {session_id} saved with {len(messages_dicts)} messages")
            return True

        except Exception as save_err:
            logger.error(f"[SESSION] Failed to save session {session_id}: {save_err}", exc_info=True)
            return False

    def _extract_session_title(self, messages_dicts: List[Dict[str, Any]]) -> str:
        """
        Extract a session title from the first user message.

        Args:
            messages_dicts: List of message dictionaries

        Returns:
            Title string (max 50 chars), or default title if no user message found
        """
        for msg_dict in messages_dicts:
            if msg_dict.get("role") == "user":
                content = msg_dict.get("content", "")

                if isinstance(content, str):
                    return content[:50]
                elif isinstance(content, list) and len(content) > 0:
                    # Multimodal message - find first text part
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            return part.get("text", "")[:50]

        return "New conversation"

    # ------------------------------------------------------------------
    # Periodic checkpointing (orphan-safe save during long-running tools)
    # ------------------------------------------------------------------

    async def checkpoint_session(self, agent, user_id: str, session_id: str) -> bool:
        """Persist the current in-memory session state up to the last consistent
        tool_call/tool_result boundary.

        Background-safe alternative to save_session for use while a long-running
        tool is executing: the trim guarantees no orphan tool_calls land in the
        file. Skips silently if there is nothing reload-safe to write yet
        (e.g. the very first assistant turn is still pending).
        """
        if not self.session_manager:
            return False
        tracker = getattr(agent, "_session_tracker", None)
        if tracker is None:
            return False

        try:
            messages_list = tracker.get_session_messages(session_id)
            messages_dicts = [_msg_to_dict(m) for m in messages_list]
            for md in messages_dicts:
                if "estimated_tokens" not in md:
                    md["estimated_tokens"] = _estimate_message_tokens(md)

            safe_messages = _trim_to_safe_boundary(messages_dicts)
            runtime_vars = tracker.get_session_template_vars(session_id) if hasattr(tracker, "get_session_template_vars") else {}

            if not safe_messages and not runtime_vars:
                # Nothing useful to checkpoint yet
                return False

            # Find the actual session owner if the session is already on disk
            session_owner = await self.session_manager._find_session_owner_async(session_id)
            actual_user_id = session_owner if session_owner else user_id

            if session_owner is None:
                # Session not on disk yet — only create one if we have at least
                # something meaningful to write. We skip pure context_vars-only
                # checkpoints for non-existent sessions to avoid littering disk
                # with empty session files for transient sub-agents.
                if not safe_messages:
                    return False
                title = self._extract_session_title(safe_messages)
                meta = tracker.get_session_metadata(session_id) if hasattr(tracker, "get_session_metadata") else {}
                agent_name = (meta or {}).get("agent_name") or getattr(agent, "name", "agent")
                llm_profile = (meta or {}).get("llm_profile") or "normal"
                try:
                    session_data = await self.session_manager.create_session(
                        session_id=session_id,
                        user_id=actual_user_id,
                        title=title,
                        agent_name=agent_name,
                        llm_profile=llm_profile,
                    )
                except ValueError:
                    # Race: session was created in the meantime — fall back to load
                    session_owner = await self.session_manager._find_session_owner_async(session_id)
                    if not session_owner:
                        return False
                    actual_user_id = session_owner
                    session_data = await self.session_manager.load_session(actual_user_id, session_id)
            else:
                session_data = await self.session_manager.load_session(actual_user_id, session_id)

            session_data["messages"] = safe_messages
            if runtime_vars:
                existing = session_data.get("context_vars")
                if not isinstance(existing, dict):
                    existing = {}
                existing.update(runtime_vars)
                session_data["context_vars"] = existing

            await self.session_manager.save_session(session_data)
            logger.debug(
                f"[CHECKPOINT] Session {session_id}: persisted {len(safe_messages)} safe messages "
                f"(trimmed from {len(messages_dicts)})"
            )
            return True
        except Exception as e:
            logger.debug(f"[CHECKPOINT] Failed for session {session_id}: {e}")
            return False

    def start_checkpoint_loop(self, agent, user_id: str, session_id: str) -> None:
        """Start a background task that periodically checkpoints this session.

        Idempotent: a second call for the same session_id is a no-op as long as
        the previous loop is still running.
        """
        if self.checkpoint_interval_seconds <= 0:
            return
        existing = self._checkpoint_tasks.get(session_id)
        if existing is not None and not existing.done():
            return

        interval = self.checkpoint_interval_seconds

        async def _loop() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    await self.checkpoint_session(agent, user_id, session_id)
            except asyncio.CancelledError:
                return

        try:
            task = asyncio.create_task(_loop(), name=f"session-checkpoint-{session_id}")
        except RuntimeError:
            # No running event loop (e.g. unit test in sync context)
            return
        self._checkpoint_tasks[session_id] = task

    async def stop_checkpoint_loop(self, session_id: str, final_checkpoint_agent=None, final_checkpoint_user_id: Optional[str] = None) -> None:
        """Stop the background checkpoint loop for this session.

        If ``final_checkpoint_agent`` is provided, runs one last checkpoint
        synchronously after cancelling the loop — useful to flush the final
        pre-save state when a request is winding down.
        """
        task = self._checkpoint_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        if final_checkpoint_agent is not None and final_checkpoint_user_id is not None:
            try:
                await self.checkpoint_session(final_checkpoint_agent, final_checkpoint_user_id, session_id)
            except Exception as e:
                logger.debug(f"[CHECKPOINT] Final checkpoint failed for {session_id}: {e}")
