"""Context window manager with enhanced warning system."""

import logging
from typing import List, Optional, Tuple
from ..llm.clients import ChatMessage
from .config import ContextConfig, WarningLevel

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
    
    def manage_context(self, messages: List[ChatMessage]) -> List[ChatMessage]:
        """Apply context management strategy to reduce token count."""
        current_tokens = self.estimate_token_count(messages)
        
        if not self.config.should_summarize(current_tokens):
            return messages
        
        logger.info("Applying context management strategy: %s", self.config.strategy.value)
        
        if self.config.strategy == self.config.strategy.TRUNCATE_OLDEST:
            return self._truncate_oldest(messages)
        elif self.config.strategy == self.config.strategy.SUMMARIZE_OLDEST and self._summarizer:
            return self._summarize_conversation(messages)
        elif self.config.strategy == self.config.strategy.SLIDING_WINDOW:
            return self._apply_sliding_window(messages)
        else:
            # Fallback to truncation
            return self._truncate_oldest(messages)
    
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
        logger.info("Truncated %d oldest messages, kept %d messages", removed_count, len(result))
        
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
        logger.info("Applied sliding window: kept %d messages (%.1f%% of context), removed %d", 
                   len(result), (current_tokens / self.config.context_window) * 100, removed_count)
        
        return result
    
    def _summarize_conversation(self, messages: List[ChatMessage]) -> List[ChatMessage]:
        """Summarize conversation using the summarizer."""
        if not self._summarizer:
            logger.warning("Summarizer not available, falling back to truncation")
            return self._truncate_oldest(messages)
        
        try:
            return self._summarizer.summarize_conversation(messages, self.config)
        except Exception as e:
            logger.error("Summarization failed: %s, falling back to truncation", e)
            return self._truncate_oldest(messages)