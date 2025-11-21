"""
Session Service - Centralized session management logic.

This service handles loading and saving sessions for both API and CLI contexts.
It provides a clean interface for session restoration and persistence.
"""

import logging
from typing import List, Dict, Any

from agent_system.services.session_manager import SessionPermissionError, SessionNotFoundError

logger = logging.getLogger(__name__)


class SessionService:
    """Service for managing session loading, restoration, and saving."""

    def __init__(self, session_manager):
        """
        Initialize the session service.

        Args:
            session_manager: SessionManager instance for persistence operations
        """
        self.session_manager = session_manager

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
                logger.info(f"[SESSION] Session {session_id} found but has no messages")
                return True, 0

            # Convert dict messages to ChatMessage objects
            from agent_system.llm.models import ChatMessage
            messages_objects = []

            for msg_dict in session_data["messages"]:
                try:
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

            logger.info(f"[SESSION] Loaded session {session_id} with {len(messages_objects)} messages")
            return True, len(messages_objects)

        except SessionPermissionError:
            # Re-raise permission errors - user trying to access session they don't own
            raise
        except SessionNotFoundError:
            # Session doesn't exist - return False so caller can create new one
            logger.info(f"[SESSION] Session {session_id} not found")
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
        was_new_session: bool
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

            # Convert ChatMessage objects to dicts
            messages_dicts = []
            for msg in messages_list:
                if hasattr(msg, 'model_dump'):
                    messages_dicts.append(msg.model_dump(mode='json'))
                elif hasattr(msg, 'dict'):
                    messages_dicts.append(msg.dict())
                else:
                    messages_dicts.append(dict(msg))

            # Determine title from first user message
            title = self._extract_session_title(messages_dicts)

            # Check if session already exists and find its owner
            # This is critical for sub-agent sessions which may have different user_ids
            session_owner = self.session_manager._find_session_owner(session_id)
            session_exists = session_owner is not None

            # Use the actual owner's user_id for existing sessions
            actual_user_id = session_owner if session_exists else user_id

            # Save or update session
            if not session_exists:
                logger.debug(f"[SESSION] Creating new session {session_id} for user {actual_user_id}")
                # Step 1: Create empty session
                session_data = await self.session_manager.create_session(
                    session_id=session_id,
                    user_id=actual_user_id,
                    title=title,
                    agent_name=agent_name,
                    llm_profile=llm_profile
                )
                # Step 2: Add messages
                session_data["messages"] = messages_dicts
                # Step 3: Save back
                await self.session_manager.save_session(session_data)
            else:
                logger.debug(f"[SESSION] Updating existing session {session_id} (owner: {actual_user_id})")
                # Load existing session with correct owner
                session_data = await self.session_manager.load_session(actual_user_id, session_id)
                # Update messages
                session_data["messages"] = messages_dicts
                # Only update title if it's still the default auto-generated title
                # This preserves user-renamed session titles
                extracted_title = self._extract_session_title(messages_dicts)
                if session_data.get("title") == extracted_title or not session_data.get("title"):
                    session_data["title"] = extracted_title
                # Update llm_profile
                session_data["llm_profile"] = llm_profile
                # Save back
                await self.session_manager.save_session(session_data)

            logger.info(f"[SESSION] Session {session_id} saved with {len(messages_dicts)} messages")
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
