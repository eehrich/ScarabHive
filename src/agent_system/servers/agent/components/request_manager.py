"""
Active request management and cancellation for agent execution.

This module handles:
- Tracking active agent requests
- Request cancellation (dual system: CancellationManager + agent events)
- Request registration/unregistration
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional, Any

from ....core.cancellation import get_cancellation_manager


logger = logging.getLogger(__name__)


class AgentRequestManager:
    """Manages active agent requests and cancellation.
    
    Responsibilities:
    - Register/unregister active requests
    - Handle request cancellation via dual system:
      1. Global CancellationManager (for tool-level cancellation)
      2. Per-agent cancel events (for request loop control)
    - Check cancellation status
    
    Thread-safety: Uses asyncio.Lock for concurrent access.
    """

    def __init__(self, agent_name: str):
        """Initialize the request manager.
        
        Args:
            agent_name: The name of the agent (for logging)
        """
        self._agent_name = agent_name
        
        # Active requests: request_id -> {'cancel': Event(), 'message_event': Event(), 'appended': List}
        self._active_requests: Dict[str, Dict[str, Any]] = {}
        
        # Lock for thread-safe access
        self._lock = asyncio.Lock()

    async def cancel_request(self, request_id: str) -> bool:
        """
        Cancel an active request.
        
        Uses dual cancellation system:
        1. Global CancellationManager for tools (with prefix matching for sub-tasks)
        2. Per-agent cancel events for request loop control
        
        See docs/cancellation_architecture.md for design details.

        Args:
            request_id: The unique ID of the request to cancel

        Returns:
            True if the request was found and cancelled, False otherwise
        """
        logger.info("Cancelling request %s for agent %s", request_id, self._agent_name)
        
        # Use CancellationManager for tool-level cancellation with prefix matching
        cancellation_manager = get_cancellation_manager()
        tool_cancelled = cancellation_manager.cancel_request(request_id)
        
        # Don't force-cancel tasks immediately - let timeout monitor handle it
        # Only check if we have matching tasks for logging
        task_cancelled_count = 0
        for task_id in cancellation_manager._tasks.keys():
            if task_id == request_id or task_id.startswith(request_id + "_"):
                task_cancelled_count += 1
        
        # Also cancel in the agent's internal tracking
        async with self._lock:
            agent_cancelled = False
            if request_id in self._active_requests:
                try:
                    self._active_requests[request_id]["cancel"].set()
                    agent_cancelled = True
                except Exception as e:
                    # Defensive: if structure unexpected, try old-style event
                    logger.debug(f"Failed to cancel via cancel event, trying old-style: {e}")
                    if isinstance(self._active_requests[request_id], asyncio.Event):
                        self._active_requests[request_id].set()
                        agent_cancelled = True
            
            if not agent_cancelled:
                logger.debug("Request %s not found in active requests (already completed or cleaned up)", request_id)
            
            # Return True if any system found and cancelled something
            return tool_cancelled or agent_cancelled or (task_cancelled_count > 0)

    def is_cancelled(self, request_id: Optional[str]) -> bool:
        """
        Check if a request has been cancelled.

        Args:
            request_id: The unique ID of the request to check (None returns False)

        Returns:
            True if the request has been cancelled, False otherwise
        """
        if request_id:
            # Check the cancellation token first (global system)
            cancellation_manager = get_cancellation_manager()
            token = cancellation_manager.get_token(request_id)
            if token and token.is_cancelled:
                logger.debug("Request %s is cancelled (cancellation token)", request_id)
                return True
            elif token:
                logger.debug("Request %s has token but not cancelled", request_id)
            else:
                logger.debug("Request %s has no cancellation token", request_id)
                
            # Also check agent's internal cancellation event
            if request_id in self._active_requests:
                entry = self._active_requests[request_id]
                if isinstance(entry, dict) and 'cancel' in entry:
                    return bool(entry['cancel'].is_set())
                if isinstance(entry, asyncio.Event):
                    return entry.is_set()
        return False

    def register_active_request(self, request_id: str, request_entry: Dict[str, Any]) -> None:
        """
        Register a new active request.
        
        Args:
            request_id: The request ID
            request_entry: The request entry dict with 'cancel', 'message_event', 'appended' keys
        """
        # Note: Called synchronously during initialization, no lock needed
        self._active_requests[request_id] = request_entry

    def unregister_active_request(self, request_id: str) -> None:
        """
        Unregister an active request.
        
        Args:
            request_id: The request ID to remove
        """
        # Note: Called during finalization, may need lock if concurrent
        self._active_requests.pop(request_id, None)

    def get_active_requests(self) -> List[str]:
        """
        Get list of currently active request IDs.
        
        Returns:
            List of request IDs
        """
        return list(self._active_requests.keys())

    def get_request_entry(self, request_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the request entry for a given request ID.
        
        Args:
            request_id: The request ID
            
        Returns:
            The request entry dict, or None if not found
        """
        return self._active_requests.get(request_id)
