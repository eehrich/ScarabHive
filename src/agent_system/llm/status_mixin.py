"""LLM Status Mixin - Provides status reporting for LLM clients.

This mixin adds status reporting capabilities to LLM clients,
allowing them to report progress to the agent's status system.
"""

from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..tools.status import StatusScope

logger = logging.getLogger(__name__)


class LLMStatusMixin:
    """Mixin that provides status reporting for LLM clients.
    
    Usage:
        class MyLLMClient(LLMClient, LLMStatusMixin):
            async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
                await self.report_status(status_scope, f"Calling {self.model}")
                # ... do work ...
                await self.report_status(status_scope, f"Response from {self.model}")
    """
    
    # Track last status message to avoid duplicate updates
    _last_status_message: Optional[str] = None
    
    async def report_status(
        self,
        status_scope: Optional["StatusScope"],
        message: str,
        force: bool = False
    ) -> None:
        """Report a status update if scope is available and message changed.
        
        Args:
            status_scope: Optional StatusScope for reporting progress
            message: Status message to report
            force: If True, send even if message hasn't changed
        """
        if status_scope is None:
            return
            
        # Skip duplicate messages unless forced
        if not force and message == self._last_status_message:
            return
            
        self._last_status_message = message
        
        try:
            await status_scope.progress(message)
        except Exception as e:
            # Never let status reporting break the LLM call
            logger.debug(f"Failed to report LLM status: {e}")
    
    def reset_status_tracking(self) -> None:
        """Reset status tracking for a new request."""
        self._last_status_message = None
