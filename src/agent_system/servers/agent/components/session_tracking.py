"""
Session and request tracking for agent execution.

This module handles:
- Request-to-session mapping
- Message appending to active requests
- Message appending to persisted sessions  
- Draining appended messages during execution
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional, Any

from ....llm.models import ChatMessage
from ....llm.text_sanitizer import sanitize_for_llm


logger = logging.getLogger(__name__)


class SessionTracker:
    """Manages request/session lifecycle and message appending.
    
    Responsibilities:
    - Track active requests and their associated sessions
    - Allow appending messages to active requests (for multi-turn conversations)
    - Allow appending messages directly to sessions
    - Drain pending appended messages during execution loops
    
    Thread-safety: All public methods use asyncio.Lock for concurrent access.
    
    Note: The _active_requests dict is shared with AgentRequestManager to ensure
    both components work with the same request entries.
    """

    def __init__(self, active_requests: Optional[Dict[str, Dict[str, Any]]] = None):
        """Initialize the session tracker.
        
        Args:
            active_requests: Shared active requests dict (from AgentRequestManager).
                           If None, creates its own dict (for testing).
        """
        # Active requests: request_id -> {'cancel': Event(), 'message_event': Event(), 'appended': List[ChatMessage]}
        # Note: We only manage the 'appended' list and 'message_event' here
        # The 'cancel' event is managed by AgentRequestManager
        # This dict is SHARED with AgentRequestManager for coordination
        self._active_requests: Dict[str, Dict[str, Any]] = active_requests if active_requests is not None else {}
        
        # Persisted sessions: session_id -> List[ChatMessage]
        self._sessions: Dict[str, List[ChatMessage]] = {}
        
        # Session metadata: session_id -> Dict[str, Any] (user_id, etc.)
        self._session_metadata: Dict[str, Dict[str, Any]] = {}
        
        # Request-to-session mapping: request_id -> session_id
        self._request_to_session: Dict[str, str] = {}
        
        # Lock for thread-safe access
        self._lock = asyncio.Lock()

    async def append_user_message(self, request_id: str, content: str) -> bool:
        """
        Append a user message to an active request's conversation.
        
        Args:
            request_id: The request ID to append to
            content: The message content
            
        Returns:
            True if appended successfully, False if request not found
        """
        logger.debug("Append request received for request_id=%s: %s", request_id, content[:50])
        async with self._lock:
            if request_id in self._active_requests:
                entry = self._active_requests[request_id]
                if isinstance(entry, dict):
                    try:
                        msg = ChatMessage(role="user", content=sanitize_for_llm(content))
                        entry.setdefault('appended', []).append(msg)
                        # Notify run_events if it's waiting
                        try:
                            entry['message_event'].set()
                        except Exception as e:
                            logger.debug(f"Failed to set message event: {e}")
                        logger.debug("Message appended to active request %s", request_id)
                        return True
                    except Exception as e:
                        logger.debug("Failed to append message to request %s: %s", request_id, e)
                        return False
        logger.debug("Request %s not found for append", request_id)
        return False

    async def append_to_session(self, session_id: str, content: str) -> bool:
        """
        Append a user message directly to a persisted session.
        
        Args:
            session_id: The session ID to append to
            content: The message content
            
        Returns:
            True if appended successfully, False if session not found
        """
        logger.debug("Session append request for session_id=%s: %s", session_id, content[:50])
        async with self._lock:
            if session_id in self._sessions:
                try:
                    msg = ChatMessage(role="user", content=sanitize_for_llm(content))
                    self._sessions[session_id].append(msg)
                    logger.debug("Message appended to session %s", session_id)
                    return True
                except Exception as e:
                    logger.debug("Failed to append message to session %s: %s", session_id, e)
                    return False
        logger.debug("Session %s not found for append", session_id)
        return False

    async def drain_appended_messages(self, request_id: str, messages: List[ChatMessage]) -> List[ChatMessage]:
        """
        Drain any appended messages for a request and add them to the conversation.
        
        Args:
            request_id: The request ID to drain messages from
            messages: The current message list to extend
            
        Returns:
            The updated messages list with appended messages added
        """
        async with self._lock:
            entry = self._active_requests.get(request_id)
            if isinstance(entry, dict):
                appended = entry.get('appended', [])
                if appended:
                    messages.extend(appended)
                    entry['appended'] = []
                    logger.debug("Drained %d appended messages for request %s", len(appended), request_id)
                    # Clear message_event
                    try:
                        entry['message_event'].clear()
                    except Exception as e:
                        logger.debug(f"Failed to clear message event: {e}")
        return messages

    def register_request(self, request_id: str, session_id: str, request_entry: Dict[str, Any]) -> None:
        """
        Register a new active request's session mapping.
        
        Args:
            request_id: The request ID
            session_id: The associated session ID
            request_entry: The request entry dict (should already be in shared _active_requests)
        
        Note: The request_entry should already be registered in the shared _active_requests
        dict by AgentRequestManager. This method only sets up the session mapping.
        """
        # Verify the entry exists in shared dict (defensive check)
        if request_id not in self._active_requests:
            # If not already there, add it (shouldn't happen in normal flow)
            self._active_requests[request_id] = request_entry
            logger.debug("Request entry added to shared dict for %s (unexpected)", request_id)
        
        # Ensure session exists
        self._sessions.setdefault(session_id, [])
        # Map request to session
        self._request_to_session[request_id] = session_id

    def unregister_request(self, request_id: str) -> None:
        """
        Unregister an active request.
        
        Args:
            request_id: The request ID to remove
        """
        # Note: This is called during finalization, may need lock if concurrent
        self._active_requests.pop(request_id, None)
        self._request_to_session.pop(request_id, None)

    def get_session_for_request(self, request_id: str) -> Optional[str]:
        """
        Get the session ID associated with a request.
        
        Args:
            request_id: The request ID
            
        Returns:
            The session ID, or None if not found
        """
        return self._request_to_session.get(request_id)

    def get_session_messages(self, session_id: str) -> List[ChatMessage]:
        """
        Get the persisted messages for a session.
        
        Args:
            session_id: The session ID
            
        Returns:
            The session's message history (empty list if session not found)
        """
        return self._sessions.get(session_id, [])

    def set_session_messages(self, session_id: str, messages: List[ChatMessage]) -> None:
        """
        Set the persisted messages for a session.
        
        Args:
            session_id: The session ID
            messages: The messages to persist
        """
        self._sessions[session_id] = messages
    
    def set_session_metadata(self, session_id: str, metadata: Dict[str, Any]) -> None:
        """
        Set metadata for a session (e.g., user_id).
        
        Args:
            session_id: The session ID
            metadata: Metadata dict (user_id, agent_name, etc.)
        """
        self._session_metadata[session_id] = metadata
    
    def get_session_metadata(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Get metadata for a session.
        
        Args:
            session_id: The session ID
            
        Returns:
            Metadata dict or None if not found
        """
        return self._session_metadata.get(session_id)

    def has_session(self, session_id: str) -> bool:
        """
        Check if a session exists.
        
        Args:
            session_id: The session ID
            
        Returns:
            True if session exists, False otherwise
        """
        return session_id in self._sessions

    def get_all_session_ids(self) -> List[str]:
        """
        Get all session IDs.
        
        Returns:
            List of session IDs
        """
        return list(self._sessions.keys())

    def delete_session(self, session_id: str) -> bool:
        """
        Delete a session completely.
        
        Args:
            session_id: The session ID to delete
            
        Returns:
            True if session was deleted, False if it didn't exist
        """
        if session_id in self._sessions:
            del self._sessions[session_id]
            return True
        return False

    def clear(self) -> None:
        """
        Clear all sessions and request mappings.
        Used during shutdown or reset operations.
        """
        self._sessions.clear()
        self._request_to_session.clear()
        self._appended_messages.clear()
