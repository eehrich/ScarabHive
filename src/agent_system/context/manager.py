"""Context window manager with enhanced warning system."""

import logging
import time
from typing import List, Optional, Tuple
from ..llm.clients import ChatMessage
from .config import ContextConfig, WarningLevel
from ..mcp.status import publish_status, PHASE_START, PHASE_END, PHASE_ERROR

logger = logging.getLogger(__name__)


class ContextManager:
    """Manages context window usage with enhanced warnings and automatic handling."""
    
    def __init__(self, config: ContextConfig):
        self.config = config
        self._last_warning_level: Optional[WarningLevel] = None
        self._summarizer = None  # Will be set when summarizer is available
    
    def set_summarizer(self, summarizer):
        """Set the conversation summarizer."""
        self._summarizer = summarizer
    
    def estimate_token_count(self, messages: List[ChatMessage]) -> int:
        """Enhanced token count estimation."""
        total_chars = 0
        
        for msg in messages:
            # Count content characters
            if msg.content:
                total_chars += len(str(msg.content))
            
            # Count tool calls with detailed breakdown
            if hasattr(msg, 'tool_calls') and msg.tool_calls:
                for tc in msg.tool_calls:
                    func = tc.get("function", {})
                    total_chars += len(str(func.get("name", "")))
                    
                    # Tool arguments can be very large
                    args_str = str(func.get("arguments", ""))
                    total_chars += len(args_str)
            
            # Count tool results (these can be the biggest consumers)
            if hasattr(msg, 'tool_call_id') and msg.tool_call_id:
                # This is a tool result message
                content_len = len(str(msg.content or ""))
                total_chars += content_len
            
            # Add role and structure overhead
            total_chars += 100  # More realistic overhead per message
        
        # Conservative token estimation: ~3.5 chars per token
        # This accounts for different languages and formatting
        estimated_tokens = int(total_chars / 3.5)
        return estimated_tokens
    
    def check_and_warn(self, messages: List[ChatMessage], step: int = 0) -> Tuple[int, Optional[WarningLevel]]:
        """Check token count and issue appropriate warnings."""
        estimated_tokens = self.estimate_token_count(messages)
        current_level = self.config.get_current_warning_level(estimated_tokens)
        
        # Log detailed token breakdown in debug mode
        self._log_token_breakdown(messages, estimated_tokens, step)
        
        # Issue warnings only when level changes or for RED level
        if current_level and (current_level != self._last_warning_level or current_level == WarningLevel.RED):
            self._issue_warning(current_level, estimated_tokens, len(messages))
            self._last_warning_level = current_level
        
        return estimated_tokens, current_level
    
    def should_manage_context(self, current_tokens: int, current_level: Optional[WarningLevel]) -> bool:
        """Determine if context management action should be taken."""
        # Auto-manage if we hit orange level or above
        return current_level in [WarningLevel.ORANGE, WarningLevel.RED] or self.config.should_summarize(current_tokens)
    
    async def manage_context(self, messages: List[ChatMessage]) -> List[ChatMessage]:
        """Apply context management strategy to reduce token count."""
        current_tokens = self.estimate_token_count(messages)
        
        if not self.config.should_summarize(current_tokens):
            return messages
        
        # Publish start status event
        await publish_status(
            server="context-manager",
            message="🔄 Starting context management",
            phase=PHASE_START,
            meta={"tokens": current_tokens, "strategy": self.config.strategy.value}
        )
        
        percentage = (current_tokens / self.config.context_window) * 100
        logger.debug("🔄 Context management triggered: %d tokens (%.1f%% of context window)", 
                   current_tokens, percentage)
        logger.debug("📋 Strategy: %s", self.config.strategy.value.replace('_', ' ').title())
        
        original_count = len(messages)
        start_time = time.time()
        
        try:
            if self.config.strategy == self.config.strategy.TRUNCATE_OLDEST:
                logger.debug("✂️  Truncating oldest messages to reduce context size...")
                result = self._truncate_oldest(messages)
            elif self.config.strategy == self.config.strategy.SUMMARIZE_OLDEST and self._summarizer:
                logger.debug("📝 Summarizing conversation history to preserve context...")
                result = await self._summarize_conversation(messages)
            elif self.config.strategy == self.config.strategy.SLIDING_WINDOW:
                logger.debug("🪟 Applying sliding window to keep most relevant messages...")
                result = self._apply_sliding_window(messages)
            else:
                # Fallback to truncation
                logger.debug("✂️  Applying fallback truncation strategy...")
                result = self._truncate_oldest(messages)
            
            # Report results
            end_time = time.time()
            new_tokens = self.estimate_token_count(result)
            new_count = len(result)
            saved_tokens = current_tokens - new_tokens
            processing_time = (end_time - start_time) * 1000  # Convert to milliseconds
            
            logger.debug("✅ Context management completed in %.1fms:", processing_time)
            logger.debug("   📊 Messages: %d → %d (removed %d)", 
                       original_count, new_count, original_count - new_count)
            logger.debug("   🪙 Tokens: %d → %d (saved %d tokens, %.1f%% reduction)", 
                       current_tokens, new_tokens, saved_tokens, (saved_tokens / current_tokens) * 100)
            logger.debug("   📈 New usage: %.1f%% of context window", 
                       (new_tokens / self.config.context_window) * 100)
            
            # Publish success status event
            await publish_status(
                server="context-manager",
                message=f"✅ Context management complete: {original_count}→{new_count} messages, saved {saved_tokens:,} tokens",
                phase=PHASE_END,
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
            # Publish error status event
            await publish_status(
                server="context-manager",
                message=f"❌ Context management failed: {str(e)}",
                phase=PHASE_ERROR,
                level="error",
                meta={"error": str(e), "strategy": self.config.strategy.value}
            )
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
    
    def _issue_warning(self, level: WarningLevel, tokens: int, message_count: int):
        """Issue appropriate warning based on level."""
        percentage = (tokens / self.config.context_window) * 100
        
        if level == WarningLevel.YELLOW:
            logger.warning(
                "🟡 Context usage at %.1f%% (%d/%d tokens, %d messages) - Consider summarization soon",
                percentage, tokens, self.config.context_window, message_count
            )
        elif level == WarningLevel.ORANGE:
            logger.warning(
                "🟠 Context usage at %.1f%% (%d/%d tokens, %d messages) - Automatic management will trigger soon",
                percentage, tokens, self.config.context_window, message_count
            )
        elif level == WarningLevel.RED:
            logger.error(
                "🔴 Context usage at %.1f%% (%d/%d tokens, %d messages) - CRITICAL: Immediate action required!",
                percentage, tokens, self.config.context_window, message_count
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
        target_tokens = int(self.config.context_window * 0.6)  # Aim for 60% usage
        
        # Start with recent messages and work backwards
        result = []
        current_tokens = 0
        
        for msg in reversed(messages):
            msg_tokens = self.estimate_token_count([msg])
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
                   len(result), (current_tokens / self.config.context_window) * 100, removed_count)
        
        return result
    
    async def _summarize_conversation(self, messages: List[ChatMessage]) -> List[ChatMessage]:
        """Summarize conversation using the summarizer."""
        if not self._summarizer:
            logger.warning("Summarizer not available, falling back to truncation")
            return self._truncate_oldest(messages)
        
        try:
            return await self._summarizer.summarize_conversation(messages, self.config)
        except Exception as e:
            logger.error("Summarization failed: %s, falling back to truncation", e)
            return self._truncate_oldest(messages)