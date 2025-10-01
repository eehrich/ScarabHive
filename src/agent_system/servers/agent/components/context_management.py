"""
Context Management for Agent Server
Handles token optimization and context management within the agent loop.
"""
from __future__ import annotations

import logging
import time
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from ..server import Agent
    from ....context import ContextManager, TokenOptimizer

from ....llm.models import ChatMessage
from ....context.agent_tracker import update_agent_context_usage
from ....llm.message_validator import validate_messages_before_llm

logger = logging.getLogger(__name__)


class ContextManagementHandler:
    """Handles context management operations within the agent loop.

    Requires an Agent instance to obtain globally-unique internal tool-call ids via
    `agent.next_internal_tool_request_id()`.
    """

    def __init__(self, context_manager: ContextManager, token_optimizer: TokenOptimizer, agent: Agent):
        """Initialize context management handler.
        
        Args:
            context_manager: Context manager instance
            token_optimizer: Token optimizer instance
            agent: Agent instance (required, no legacy string support)
        """
        self.context_manager = context_manager
        self.token_optimizer = token_optimizer
        self._agent = agent
        self.agent_name = agent.name

        # Token optimizer state
        self._last_optimizer_tokens_snapshot = 0
        self._last_optimizer_run_time = 0.0
        self._optimizer_cooldown_seconds = 10.0  # Increased from 1.0 to 10.0 seconds
        self._optimizer_min_increase_tokens = 200  # Minimum token increase required
        self._skip_optimizer_steps_after_context_mgmt = 0

    async def _get_next_internal_tool_request_id(self, base_request_id: str) -> str:
        """Return the next internal tool request id from the agent."""
        return await self._agent.next_internal_tool_request_id(base_request_id)

    async def handle_context_management(self, messages: List[ChatMessage], step: int,
                                      request_id: str) -> tuple[List[ChatMessage], int]:
        """Apply context management and token optimization to messages.
        
        Returns:
            Tuple of (updated_messages, estimated_tokens)
        """
        if not self.context_manager:
            # Fallback estimation for when no context manager
            estimated_tokens = sum(len(str(msg.content or "")) for msg in messages) // 4
            return messages, estimated_tokens

        # Apply token optimization with centralized guard
        if self.token_optimizer:
            messages = await self._apply_token_optimization(messages, request_id)

        # Check token count and issue appropriate warnings
        estimated_tokens, warning_level = self.context_manager.check_and_warn(messages, step)

        # Apply context management if needed
        if self.context_manager.should_manage_context(estimated_tokens, warning_level):
            logger.info("Applying context management at step %d", step + 1)
            # Generate unique request ID for this context manager call
            context_mgr_request_id = await self._get_next_internal_tool_request_id(request_id)
            messages = await self.context_manager.manage_context(messages, request_id=context_mgr_request_id)
            # Skip optimizer for next 2 steps after context management
            self._skip_optimizer_steps_after_context_mgmt = 2
            # Re-check after management
            estimated_tokens, _ = self.context_manager.check_and_warn(messages, step)

        return messages, estimated_tokens

    async def _apply_token_optimization(self, messages: List[ChatMessage], request_id: str) -> List[ChatMessage]:
        """Apply token optimization if conditions are met."""
        # Skip optimizer for a few steps after context management
        if getattr(self, '_skip_optimizer_steps_after_context_mgmt', 0) > 0:
            self._skip_optimizer_steps_after_context_mgmt -= 1
            logger.debug("Skipping token optimizer: %d steps remaining after context mgmt",
                       self._skip_optimizer_steps_after_context_mgmt)
            return messages

        try:
            now = time.time()
            estimated_tokens_now = self.context_manager.estimate_token_count(messages)

            tokens_growth = estimated_tokens_now - getattr(self, '_last_optimizer_tokens_snapshot', 0)
            time_since_last = now - getattr(self, '_last_optimizer_run_time', 0.0)

            should_run_optimizer = False
            if tokens_growth >= getattr(self, '_optimizer_min_increase_tokens', 200):
                should_run_optimizer = True
            elif time_since_last >= getattr(self, '_optimizer_cooldown_seconds', 10.0):
                should_run_optimizer = True

            if should_run_optimizer:
                # Generate unique request ID for this token optimizer call
                optimizer_request_id = await self._get_next_internal_tool_request_id(request_id)
                messages = await self.token_optimizer.optimize_messages(messages, request_id=optimizer_request_id)
                self._last_optimizer_tokens_snapshot = self.context_manager.estimate_token_count(messages)
                self._last_optimizer_run_time = now

                # Validate messages after token optimization
                messages = validate_messages_before_llm(messages, context="agent_server_token_optimization")
            else:
                logger.debug("Skipping token optimizer: growth=%d, time_since_last=%.2fs",
                           tokens_growth, time_since_last)
        except Exception as e:
            logger.debug("Token optimizer guard check failed: %s", e)

        return messages

    async def update_token_usage(self, llm_out: dict, estimated_tokens: int, message_count: int) -> None:
        """Update token usage tracking after LLM call."""
        if not self.context_manager or not isinstance(llm_out, dict) or 'usage' not in llm_out:
            return

        try:
            usage_data = llm_out['usage']
            self.context_manager.update_token_usage(usage_data)
            logger.debug("Updated token usage from LLM response: %s", usage_data)

            # Coerce token count to int safely and always update tracker
            raw_total = usage_data.get('total_tokens', 0)
            try:
                actual_tokens = int(raw_total or 0)
            except Exception:
                try:
                    actual_tokens = int(float(str(raw_total)))
                except Exception:
                    actual_tokens = 0

            try:
                update_agent_context_usage(
                    self.agent_name,
                    current_tokens=estimated_tokens,
                    predicted_tokens=estimated_tokens,
                    message_count=message_count,
                    actual_tokens=actual_tokens
                )
            except Exception as e:
                logger.debug("Failed to update agent context with LLM tokens: %s", e)
        except Exception as e:
            logger.debug("Failed to handle LLM usage: %s", e)