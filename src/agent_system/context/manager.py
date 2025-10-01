"""Context window manager with enhanced warning system."""

from __future__ import annotations

import logging
import time
from typing import List, Optional, Tuple, TYPE_CHECKING
from ..llm.models import ChatMessage
from ..llm.token_utils import estimate_token_count
from ..config.models import ContextManagementConfig
from .tracker import record_context_usage
from .agent_tracker import record_agent_summarization
from ..mcp.status import (
    status_bus,
    StatusScope,
)

if TYPE_CHECKING:
    from ..servers.agent.server import Agent

logger = logging.getLogger(__name__)


class ContextManager:
    """Manages context window usage with warnings and automatic handling."""

    def __init__(self, agent: Agent):
        """Initialize ContextManager with Agent object.
        
        Args:
            agent: Agent instance containing all necessary config and context window info
        """
        self.agent = agent
        self.agent_id = agent.name
        
        # Get config from agent's mcp_config.agent_config
        if not hasattr(agent.mcp_config, 'agent_config') or agent.mcp_config.agent_config is None:
            raise ValueError(f"Agent {agent.name} has no agent_config in mcp_config")
        
        self.config: ContextManagementConfig = agent.mcp_config.agent_config.context_management
        
        # Get context_window from LLM configuration (via agent's system_config)
        self.context_window = self._get_context_window_from_agent()
        
        self._last_warning_level: Optional[str] = None  # "yellow", "orange", "red"
        self._summarizer = None  # Will be set when summarizer is available
        self._summarization_in_progress = False  # Prevent recursive summarization loops
        self._context_managed_this_step = False  # Prevent duplicate context management in same step
        
        # Track actual token usage from LLM responses
        self._actual_usage_stats = {
            'total_tokens': 0,
            'prompt_tokens': 0,
            'completion_tokens': 0,
            'last_call_tokens': 0
        }
        
        # Precompute warning levels in absolute tokens for UI consumption
        self.warning_levels = {
            level: int(self.context_window * threshold)
            for level, threshold in self.config.warning_levels.items()
        }
    
    def _get_context_window_from_agent(self) -> int:
        """Extract context_window from agent's LLM configuration."""
        try:
            # Try to get from LLM client if available
            if hasattr(self.agent, 'llm') and self.agent.llm:
                if hasattr(self.agent.llm, 'context_window'):
                    return self.agent.llm.context_window
                if hasattr(self.agent.llm, 'model_config') and hasattr(self.agent.llm.model_config, 'context_window'):
                    return self.agent.llm.model_config.context_window
            
            # Try to get from system_config via LLM factory resolution
            if hasattr(self.agent, 'system_config') and self.agent.system_config:
                from ..llm.factory import resolve_llm_config_for_agent
                llm_kwargs = resolve_llm_config_for_agent(self.agent.system_config, self.agent.name)
                context_window = llm_kwargs.get("context_window")
                if context_window:
                    return context_window
        except Exception as e:
            logger.warning("Failed to get context_window from agent, using default fallback: %s", e)
        
        # Fallback to default with warning
        logger.warning("⚠️  Could not determine context_window from agent '%s', falling back to default: 100000 tokens", self.agent.name)
        return 100000

    def get_summarization_threshold_tokens(self) -> int:
        """Get the summarization threshold in absolute tokens."""
        if self.config.summarization_threshold <= 1:
            # Percentage-based threshold
            return int(self.context_window * self.config.summarization_threshold)
        else:
            # Absolute token threshold (backwards compatibility)
            return int(self.config.summarization_threshold)

    def get_current_warning_level(self, current_tokens: int) -> Optional[str]:
        """Get the current warning level based on token count."""
        usage_ratio = current_tokens / self.context_window
        
        # Check in descending order: red, orange, yellow
        for level in ["red", "orange", "yellow"]:
            if usage_ratio >= self.config.warning_levels[level]:
                return level
        
        return None

    def should_manage_context_prediction(self, predicted_tokens: int) -> bool:
        """Check if context management should trigger based on token prediction."""
        threshold_tokens = int(self.context_window * self.config.prediction_threshold)
        return predicted_tokens >= threshold_tokens

    def should_manage_context_actual(self, actual_tokens: int) -> bool:
        """Check if context management should trigger based on actual token usage."""
        threshold_tokens = self.get_summarization_threshold_tokens()
        return actual_tokens >= threshold_tokens

    def set_summarizer(self, summarizer):
        """Set the conversation summarizer."""
        self._summarizer = summarizer

    def reset_step_state(self):
        """Reset per-step state tracking. Should be called at the start of each agent step."""
        self._context_managed_this_step = False

    def update_token_usage(self, usage_data: dict) -> None:
        """Update current conversation token usage from LLM response.
        
        Note: This tracks the current conversation state, not session-wide accumulation.
        Context management decisions should be based on the current conversation size,
        not the total tokens used across all conversations in the session.
        """
        if usage_data:
            # Update with latest LLM call usage (represents current conversation state)
            total = usage_data.get('total_tokens', 0) or 0
            prompt = usage_data.get('prompt_tokens', 0) or 0
            completion = usage_data.get('completion_tokens', 0) or 0

            # Store current conversation token usage (not accumulated)
            self._actual_usage_stats['total_tokens'] = total
            self._actual_usage_stats['prompt_tokens'] = prompt
            self._actual_usage_stats['completion_tokens'] = completion
            self._actual_usage_stats['last_call_tokens'] = total
            logger.debug("Current conversation token usage: %d tokens", total)
            logger.debug("Token usage breakdown: %s", self._actual_usage_stats)

    def get_usage_stats(self) -> dict:
        """Get current usage statistics for debugging."""
        return {
            'actual_usage': self._actual_usage_stats.copy(),
            'context_window': self.context_window,
            'prediction_threshold': self.config.prediction_threshold,
            # Report summarization threshold as absolute tokens for the UI
            'summarization_threshold': self.get_summarization_threshold_tokens(),
            'warning_levels': self.config.warning_levels  # Already a dict of str: float
        }

    def check_and_warn(self, messages: List[ChatMessage], step: int = 0) -> Tuple[int, Optional[str]]:
        """Check token count and issue appropriate warnings."""
        estimated_tokens = estimate_token_count(messages)
        current_level = self.get_current_warning_level(estimated_tokens)

        # Log detailed token breakdown in debug mode
        self._log_token_breakdown(messages, estimated_tokens, step)

        # Issue warnings only when level changes or for RED level
        if current_level and (current_level != self._last_warning_level or current_level == "red"):
            self._issue_warning(current_level, estimated_tokens, len(messages))
            self._last_warning_level = current_level

        return estimated_tokens, current_level

    def should_manage_context(self, current_tokens: int, current_level: Optional[str]) -> bool:
        """Determine if context management action should be taken.

        Primary trigger strategy: Use estimated token count of current messages.
        Context management should trigger when the conversation (messages) gets too large,
        not when session-wide accumulated tokens exceed thresholds.
        
        Triggers:
        1. Current conversation tokens >= summarization threshold (primary trigger)
        2. Prediction-based: estimated next tokens would exceed prediction threshold
        3. Recent actual usage trigger: latest LLM call was expensive (proactive management)
        4. Warning level escalation (ORANGE/RED levels)
        """
        # Prevent duplicate context management in the same step
        if self._context_managed_this_step:
            logger.debug("✅ Context management already applied this step, skipping to prevent loops")
            return False

        # Primary trigger: Current conversation size exceeds summarization threshold
        summarization_threshold = self.get_summarization_threshold_tokens()
        primary_trigger = current_tokens >= summarization_threshold

        # Secondary trigger: Prediction-based (90% of context window)
        prediction_trigger = self.should_manage_context_prediction(current_tokens)

        # Tertiary trigger: Recent actual usage exceeds threshold (proactive management)
        # This handles cases where the last LLM call was expensive, indicating we should
        # proactively manage context even if current estimated tokens are moderate
        recent_usage = self._actual_usage_stats.get('last_call_tokens', 0)
        actual_usage_trigger = self.should_manage_context_actual(recent_usage)

        # Quaternary trigger: Warning level escalation (for UI responsiveness)
        level_trigger = current_level in ("orange", "red")

        # Log trigger reasons for debugging
        triggers_fired = []
        if primary_trigger:
            threshold_percent = (summarization_threshold / self.context_window) * 100
            current_percent = (current_tokens / self.context_window) * 100
            triggers_fired.append(f"CONVERSATION: {current_tokens:,} tokens ({current_percent:.1f}%) >= {summarization_threshold:,} threshold ({threshold_percent:.1f}%)")

        if prediction_trigger:
            pred_percent = (current_tokens / self.context_window) * 100
            pred_threshold_percent = self.config.prediction_threshold * 100
            triggers_fired.append(f"PREDICTION: {current_tokens:,} tokens ({pred_percent:.1f}%) >= {pred_threshold_percent:.1f}% threshold")

        if actual_usage_trigger:
            triggers_fired.append(f"RECENT_USAGE: Last call used {recent_usage:,} tokens >= {summarization_threshold:,} threshold")

        if level_trigger:
            triggers_fired.append(f"WARNING_LEVEL: {current_level} level reached")

        if triggers_fired:
            logger.info("🔥 Context management triggered by: %s", " | ".join(triggers_fired))
        else:
            logger.debug("✅ No context management triggers fired (current: %d tokens, threshold: %d tokens)",
                        current_tokens, summarization_threshold)

        return primary_trigger or prediction_trigger or actual_usage_trigger or level_trigger

    async def manage_context(self, messages: List[ChatMessage], request_id: Optional[str] = None) -> List[ChatMessage]:
        """Apply context management strategy to reduce token count."""
        current_tokens = estimate_token_count(messages)

        # Determine current warning level for more informed trigger decisions
        current_level = self.get_current_warning_level(current_tokens)

        # Use the improved trigger logic (no forced triggers)
        should_trigger = self.should_manage_context(current_tokens, current_level)

        if not should_trigger:
            return messages

        # Mark that context management is being applied this step
        self._context_managed_this_step = True

        # Check if we already have summaries to prevent re-summarizing summaries
        has_existing_summary = any(
            getattr(msg, 'role', None) == 'system' and 
            msg.content and '[CONVERSATION SUMMARY]' in str(msg.content)
            for msg in messages
        )

        if has_existing_summary and self.config.strategy == "SUMMARIZE_OLDEST":
            logger.debug("🔄 Existing summary detected, skipping re-summarization to prevent loops")
            # Use truncation as fallback to avoid infinite summarization loops
            return self._truncate_oldest(messages)

        # Use StatusScope for automatic START/END status management
        async with StatusScope(status_bus, "context-manager", request_id) as status:
            percentage = (current_tokens / self.context_window) * 100
            logger.debug("🔄 Context management triggered: %d tokens (%.1f%% of context window)",
                       current_tokens, percentage)
            logger.debug("📋 Strategy: %s", self.config.strategy.replace('_', ' ').title())

            # Record context management trigger
            record_context_usage(
                total_tokens=current_tokens,
                message_count=len(messages),
                context_window=self.context_window,
                management_triggered=True,
                management_strategy=self.config.strategy
            )

            original_count = len(messages)
            start_time = time.time()

            try:
                if self.config.strategy == "TRUNCATE_OLDEST":
                    logger.debug("✂️  Truncating oldest messages to reduce context size...")
                    result = self._truncate_oldest(messages)
                elif self.config.strategy == "SUMMARIZE_OLDEST" and self._summarizer:
                    logger.debug("📝 Summarizing conversation history to preserve context...")
                    result = await self._summarize_conversation(messages)
                    # Track summarization for UI display
                    try:
                        agent_name = self.agent_id if self.agent_id else "unknown_agent"
                        record_agent_summarization(agent_name)
                    except Exception as track_e:
                        logger.debug("Failed to track summarization for %s: %s", agent_name, track_e)
                elif self.config.strategy == "SLIDING_WINDOW":
                    logger.debug("🪟 Applying sliding window to keep most relevant messages...")
                    result = self._apply_sliding_window(messages)
                else:
                    # Fallback to truncation
                    logger.debug("✂️  Applying fallback truncation strategy...")
                    result = self._truncate_oldest(messages)

                # Report results
                end_time = time.time()
                new_tokens = estimate_token_count(result)
                new_count = len(result)
                saved_tokens = current_tokens - new_tokens
                processing_time = (end_time - start_time) * 1000  # Convert to milliseconds

                logger.debug("✅ Context management completed in %.1fms:", processing_time)
                logger.debug("   📊 Messages: %d → %d (removed %d)",
                           original_count, new_count, original_count - new_count)
                logger.debug("   🪙 Tokens: %d → %d (saved %d tokens, %.1f%% reduction)",
                           current_tokens, new_tokens, saved_tokens, (saved_tokens / current_tokens) * 100)
                logger.debug("   📈 New usage: %.1f%% of context window",
                           (new_tokens / self.context_window) * 100)

                # Add final progress update with results
                await status.end(
                    f"complete: {original_count}→{new_count} messages, saved {saved_tokens:,} tokens",
                    meta={
                        "original_messages": original_count,
                        "final_messages": new_count,
                        "original_tokens": current_tokens,
                        "final_tokens": new_tokens,
                        "tokens_saved": saved_tokens,
                        "processing_time_ms": processing_time
                    }
                )

                return result

            except Exception as e:
                # StatusScope will automatically publish ERROR event
                await status.error(f"❌ failed: {str(e)}",
                                 meta={"error": str(e), "strategy": self.config.strategy})
                logger.error("Context management failed: %s", e)
                # Return original messages as fallback
                return messages

    def _log_token_breakdown(self, messages: List[ChatMessage], total_tokens: int, step: int):
        """Log detailed token usage breakdown."""
        if not logger.isEnabledFor(logging.DEBUG):
            return

        breakdown = {
            "user_messages": 0,
            "assistant_messages": 0,
            "tool_calls": 0,
            "tool_results": 0,
            "system_messages": 0
        }

        for msg in messages:
            role = getattr(msg, 'role', 'unknown')
            content_tokens = len(str(msg.content or "")) // 4

            if role == "user":
                breakdown["user_messages"] += content_tokens
            elif role == "assistant":
                breakdown["assistant_messages"] += content_tokens
                if hasattr(msg, 'tool_calls') and msg.tool_calls:
                    for tc in msg.tool_calls:
                        func = tc.get("function", {})
                        breakdown["tool_calls"] += len(str(func)) // 4
            elif role == "tool":
                breakdown["tool_results"] += content_tokens
            elif role == "system":
                breakdown["system_messages"] += content_tokens

        logger.debug(
            "Token breakdown (step %d): Total=%d, User=%d, Assistant=%d, ToolCalls=%d, ToolResults=%d, System=%d",
            step, total_tokens, breakdown["user_messages"], breakdown["assistant_messages"],
            breakdown["tool_calls"], breakdown["tool_results"], breakdown["system_messages"]
        )

        # Record context usage for tracking and monitoring
        record_context_usage(
            total_tokens=total_tokens,
            user_tokens=breakdown["user_messages"],
            assistant_tokens=breakdown["assistant_messages"],
            tool_call_tokens=breakdown["tool_calls"],
            tool_result_tokens=breakdown["tool_results"],
            system_tokens=breakdown["system_messages"],
            message_count=len(messages),
            context_window=self.context_window
        )

    def _issue_warning(self, level: str, tokens: int, message_count: int):
        """Issue appropriate warning based on level. Warnings are for UI/logging only."""
        percentage = (tokens / self.context_window) * 100
        threshold_tokens = int(self.context_window * self.config.warning_levels.get(level, 0.70))

        if level == "yellow":
            logger.warning(
                "🟡 Context usage WARNING (yellow): %d/%d tokens (%.1f%%, threshold: %d tokens, %d messages) - Consider optimization soon",
                tokens, self.context_window, percentage, threshold_tokens, message_count
            )
        elif level == "orange":
            logger.warning(
                "🟠 Context usage WARNING (orange): %d/%d tokens (%.1f%%, threshold: %d tokens, %d messages) - Optimization recommended",
                tokens, self.context_window, percentage, threshold_tokens, message_count
            )
        elif level == "red":
            logger.error(
                "🔴 Context usage WARNING (red): %d/%d tokens (%.1f%%, threshold: %d tokens, %d messages) - Critical level reached!",
                tokens, self.context_window, percentage, threshold_tokens, message_count
            )

        # Record warning in usage tracker
        record_context_usage(
            total_tokens=tokens,
            message_count=message_count,
            context_window=self.context_window,
            warning_level=level
        )

    def _truncate_oldest(self, messages: List[ChatMessage]) -> List[ChatMessage]:
        """Truncate oldest messages while preserving recent ones."""
        preserve_count = max(self.config.preserve_recent_messages, 3)

        if len(messages) <= preserve_count:
            return messages

        # Always keep system message (if first) and recent messages
        result = []
        if messages and getattr(messages[0], 'role', None) == 'system':
            result.append(messages[0])

        # Add recent messages
        recent_messages = messages[-preserve_count:]
        result.extend(recent_messages)

        removed_count = len(messages) - len(result)
        logger.debug("Truncated %d oldest messages, kept %d messages", removed_count, len(result))

        return result

    def _apply_sliding_window(self, messages: List[ChatMessage]) -> List[ChatMessage]:
        """Apply sliding window to keep most relevant messages."""
        target_tokens = int(self.context_window * 0.6)  # Aim for 60% usage

        # Start with recent messages and work backwards
        result = []
        current_tokens = 0

        for msg in reversed(messages):
            msg_tokens = estimate_token_count([msg])
            if current_tokens + msg_tokens <= target_tokens:
                result.insert(0, msg)
                current_tokens += msg_tokens
            else:
                break

        # Always include system message if it exists
        if messages and getattr(messages[0], 'role', None) == 'system' and messages[0] not in result:
            if result:
                result[0] = messages[0]
            else:
                result = [messages[0]]

        removed_count = len(messages) - len(result)
        logger.debug("Applied sliding window: kept %d messages (%.1f%% of context), removed %d",
                   len(result), (current_tokens / self.context_window) * 100, removed_count)

        return result

    async def _summarize_conversation(self, messages: List[ChatMessage]) -> List[ChatMessage]:
        """Summarize conversation using the summarizer."""
        if not self._summarizer:
            logger.warning("Summarizer not available, falling back to truncation")
            return self._truncate_oldest(messages)

        # Check if summarization is already in progress to prevent infinite loops
        if self._summarization_in_progress:
            logger.warning("Summarization already in progress, falling back to truncation to prevent loop")
            return self._truncate_oldest(messages)

        try:
            self._summarization_in_progress = True
            return await self._summarizer.summarize_conversation(messages, self.config)
        except Exception as e:
            logger.error("Summarization failed: %s, falling back to truncation", e)
            return self._truncate_oldest(messages)
        finally:
            self._summarization_in_progress = False