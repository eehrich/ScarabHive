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
from datetime import datetime, timezone
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

        # Session-level locks: session_id -> asyncio.Lock
        # Prevents multiple parallel requests from modifying the same session simultaneously
        self._session_locks: Dict[str, asyncio.Lock] = {}

        # Track which request owns which session lock: session_id -> request_id
        self._session_lock_owners: Dict[str, str] = {}

        # Compacted messages pending to be applied: session_id -> List[ChatMessage]
        # When a compaction tool runs mid-request, it stores the compacted messages here.
        # The agent will use these instead of the request's local messages when persisting.
        self._compacted_messages: Dict[str, List[ChatMessage]] = {}

        # Appended messages for append-mode requests: request_id -> List[ChatMessage]
        self._appended_messages: Dict[str, List[ChatMessage]] = {}

        # Lock for thread-safe access to internal data structures
        self._lock = asyncio.Lock()

    async def is_request_active(self, request_id: str) -> bool:
        """Check if a request is currently active (still running).
        
        Used by the status endpoint to determine if a request is still processing.
        
        Args:
            request_id: The request ID to check
            
        Returns:
            True if the request is active, False otherwise
        """
        async with self._lock:
            return request_id in self._active_requests

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
                        msg = ChatMessage(
                            role="user",
                            content=sanitize_for_llm(content),
                            timestamp=datetime.now(timezone.utc)
                        )
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

    async def acquire_session_lock(self, session_id: str, request_id: str, timeout: float = 5.0) -> bool:
        """Acquire exclusive lock for a session.
        
        Args:
            session_id: The session ID to lock
            request_id: The request ID acquiring the lock
            timeout: Maximum time to wait for lock (seconds)
            
        Returns:
            True if lock acquired, False if timeout or already locked by another request
        """
        async with self._lock:
            # Check if session is already locked by a different request
            if session_id in self._session_lock_owners:
                owner = self._session_lock_owners[session_id]
                if owner != request_id:
                    logger.warning("Session %s is already locked by request %s (request %s waiting)",
                                 session_id, owner, request_id)
                    return False
                else:
                    # Same request already owns the lock (re-entrant)
                    logger.debug("Request %s already owns lock for session %s", request_id, session_id)
                    return True
            
            # Create lock if it doesn't exist
            if session_id not in self._session_locks:
                self._session_locks[session_id] = asyncio.Lock()
        
        # Try to acquire the lock with timeout
        lock = self._session_locks[session_id]
        try:
            await asyncio.wait_for(lock.acquire(), timeout=timeout)
            async with self._lock:
                self._session_lock_owners[session_id] = request_id
            logger.info("Request %s acquired lock for session %s", request_id, session_id)
            return True
        except asyncio.TimeoutError:
            logger.warning("Request %s timed out waiting for lock on session %s", request_id, session_id)
            return False
    
    async def release_session_lock(self, session_id: str, request_id: str) -> None:
        """Release exclusive lock for a session.
        
        Args:
            session_id: The session ID to unlock
            request_id: The request ID releasing the lock
        """
        async with self._lock:
            if session_id not in self._session_lock_owners:
                logger.debug("No lock owner for session %s (request %s trying to release)",
                           session_id, request_id)
                return
            
            owner = self._session_lock_owners[session_id]
            if owner != request_id:
                logger.warning("Request %s tried to release lock owned by %s for session %s",
                             request_id, owner, session_id)
                return
            
            # Remove ownership
            del self._session_lock_owners[session_id]
        
        # Release the actual lock
        if session_id in self._session_locks:
            lock = self._session_locks[session_id]
            if lock.locked():
                lock.release()
                logger.info("Request %s released lock for session %s", request_id, session_id)
    
    def check_session_locked(self, session_id: str) -> tuple[bool, Optional[str]]:
        """Check if a session is currently locked.
        
        Args:
            session_id: The session ID to check
            
        Returns:
            Tuple of (is_locked, owner_request_id)
        """
        if session_id in self._session_lock_owners:
            return True, self._session_lock_owners[session_id]
        return False, None

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
                    msg = ChatMessage(
                        role="user",
                        content=sanitize_for_llm(content),
                        timestamp=datetime.now(timezone.utc)
                    )
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
        Unregister an active request and release session lock if held.

        Args:
            request_id: The request ID to remove
        """
        # Release session lock if this request owns it
        session_id = self._request_to_session.get(request_id)
        if session_id:
            # Check if this request owns the lock and release it synchronously
            # (called during cleanup, async not needed here)
            if session_id in self._session_lock_owners and self._session_lock_owners[session_id] == request_id:
                del self._session_lock_owners[session_id]
                if session_id in self._session_locks:
                    lock = self._session_locks[session_id]
                    if lock.locked():
                        lock.release()
                        logger.info("Released session lock for %s during unregister of request %s", 
                                  session_id, request_id)
        
        # Remove request tracking
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

    def set_compacted_messages(self, session_id: str, messages: List[ChatMessage]) -> None:
        """
        Set compacted messages to be used instead of request messages when persisting.
        
        When a compaction/summarization tool runs mid-request, the local request
        messages list cannot be directly modified. This stores the compacted messages
        so the agent can use them when persisting the session at end of request.

        Args:
            session_id: The session ID
            messages: The compacted messages to use
        """
        self._compacted_messages[session_id] = messages
        logger.debug(f"Set {len(messages)} compacted messages for session {session_id}")

    def get_compacted_messages(self, session_id: str) -> Optional[List[ChatMessage]]:
        """
        Get pending compacted messages for a session.

        Args:
            session_id: The session ID

        Returns:
            The compacted messages if any, None otherwise
        """
        return self._compacted_messages.get(session_id)

    def clear_compacted_messages(self, session_id: str) -> None:
        """
        Clear pending compacted messages for a session.
        Called after the compacted messages have been applied.

        Args:
            session_id: The session ID
        """
        self._compacted_messages.pop(session_id, None)

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
        deleted = False
        if session_id in self._sessions:
            del self._sessions[session_id]
            deleted = True
        
        # Also clear any pending compacted messages
        self._compacted_messages.pop(session_id, None)
        
        # Clear metadata to prevent memory leak
        self._session_metadata.pop(session_id, None)
        
        # Clear session locks to prevent memory leak
        if session_id in self._session_lock_owners:
            del self._session_lock_owners[session_id]
        if session_id in self._session_locks:
            lock = self._session_locks.pop(session_id)
            # Release lock if still held (defensive)
            if lock.locked():
                try:
                    lock.release()
                except RuntimeError:
                    pass  # Already released
        
        return deleted

    def clear(self) -> None:
        """
        Clear all sessions and request mappings.
        Used during shutdown or reset operations.
        """
        self._sessions.clear()
        self._request_to_session.clear()
        self._appended_messages.clear()
        self._compacted_messages.clear()
        self._session_metadata.clear()
        self._session_locks.clear()
        self._session_lock_owners.clear()
