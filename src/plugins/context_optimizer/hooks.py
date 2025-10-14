"""
Context Optimizer Plugin - Schema-Based Hook Plugin.

This plugin demonstrates schema-based hook implementation.
Hook definitions are loaded from schema.yaml.

**Plugin Type:** Hook-only (inherits from SchemaBasedPluginHook)

**Hooks Defined in schema.yaml:**
- context_optimizer: Main optimization hook (pre_llm_call)
- context_stats_logger: Optional stats logging hook (post_llm_call)

**Features:**
- Removes duplicate consecutive messages
- Truncates overly long messages
- Ensures context stays within configurable token limits
- Preserves system messages and recent user messages
- Adds optimization metadata to hook results
"""

from pathlib import Path
from typing import Any
import logging

from agent_system.hooks import (
    SchemaBasedPluginHook,
    HookContext,
    HookResult,
)
from agent_system.llm.token_utils import estimate_token_count
from agent_system.llm.models import ChatMessage

logger = logging.getLogger(__name__)


class ContextOptimizerPlugin(SchemaBasedPluginHook):
    """
    Schema-based hook plugin that optimizes conversation context before LLM calls.
    
    Hook definitions are loaded from schema.yaml. Configuration is also
    loaded from schema.yaml with default values.
    """
    
    def __init__(self, plugin_dir: Path | str):
        """Initialize the context optimizer plugin.
        
        Args:
            plugin_dir: Directory containing schema.yaml
        """
        super().__init__(plugin_dir)
        logger.info("ContextOptimizerPlugin initialized with schema-based hooks")
    
    # Handler for 'context_optimizer' hook (referenced in schema.yaml as 'optimize_context')
    async def optimize_context(self, context: HookContext) -> HookResult:
        """
        Optimize conversation context before LLM call.
        
        This hook:
        1. Removes duplicate consecutive messages
        2. Truncates overly long messages
        3. Estimates token usage and removes oldest non-system messages if needed
        4. Preserves system messages and recent messages
        
        Uses percentage-based thresholds relative to LLM context window.
        """
        try:
            messages = context.messages or []
            
            if not messages:
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={'optimization': 'no_messages'}
                )
            
            # Get LLM context window size
            context_window = self._get_context_window(context)
            if not context_window:
                logger.warning("[ContextOptimizer] No LLM context window available, skipping optimization")
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={'reason': 'no_context_window'}
                )
            
            # Get config values
            config = self.get_config()
            max_context_pct = config.get('max_context_percentage', {}).get('default', 0.80)
            max_message_length = config.get('max_message_length_chars', {}).get('default', 50000)
            preserve_system = config.get('preserve_system_messages', {}).get('default', True)
            preserve_last_n = config.get('preserve_last_n_messages', {}).get('default', 5)
            remove_dupes = config.get('remove_duplicates', {}).get('default', True)
            
            # Calculate absolute token limit from percentage
            max_total_tokens = int(context_window * max_context_pct)
            
            logger.debug(
                f"[ContextOptimizer] Using {max_context_pct:.0%} of {context_window} tokens = {max_total_tokens} max tokens"
            )
            
            # Statistics
            original_count = len(messages)
            
            # Apply optimizations
            optimized_messages = messages.copy()
            
            if remove_dupes:
                optimized_messages = self._remove_duplicates(optimized_messages)
            
            optimized_messages = self._truncate_messages(
                optimized_messages,
                max_message_length
            )
            
            optimized_messages = self._enforce_token_limits(
                optimized_messages,
                max_total_tokens,
                preserve_system,
                preserve_last_n
            )
            
            # Create modified context
            modified_context = context.model_copy(deep=True)
            modified_context.messages = optimized_messages
            
            modified = len(optimized_messages) != len(messages)
            
            if modified:
                logger.info(
                    f"[ContextOptimizer] Context optimized: {len(messages)} -> {len(optimized_messages)} messages "
                    f"(max {max_total_tokens} tokens = {max_context_pct:.0%} of {context_window})"
                )
            
            return HookResult(
                success=True,
                modified=modified,
                context=modified_context,
                metadata={
                    'original_count': original_count,
                    'optimized_count': len(optimized_messages),
                    'removed_count': original_count - len(optimized_messages),
                    'context_window': context_window,
                    'max_tokens': max_total_tokens,
                    'max_percentage': max_context_pct
                }
            )
            
        except Exception as e:
            logger.exception(f"Error in context optimization: {e}")
            return HookResult(
                success=False,
                modified=False,
                context=context,
                error=str(e)
            )
    
    def _get_context_window(self, context: HookContext) -> int | None:
        """Extract context window size from LLM in HookContext.
        
        Args:
            context: Hook context containing LLM instance
            
        Returns:
            Context window size in tokens, or None if not available
        """
        if not context.llm:
            return None
        
        # Try to get context_window from LLM instance
        if hasattr(context.llm, 'context_window'):
            return context.llm.context_window
        
        # Fallback: check if agent has config
        if context.agent and hasattr(context.agent, 'agent_config'):
            if hasattr(context.agent.agent_config, 'llm'):
                if hasattr(context.agent.agent_config.llm, 'context_window'):
                    return context.agent.agent_config.llm.context_window
        
        logger.warning("[ContextOptimizer] Could not determine context window size")
        return None
    
    # Handler for optional stats logging hook
    async def log_context_stats(self, context: HookContext) -> HookResult:
        """Log context statistics after LLM call (if enabled in schema.yaml)."""
        try:
            messages = context.messages or []
            total_length = sum(len(str(msg.get('content', ''))) for msg in messages)
            
            logger.debug(
                f"Context stats: {len(messages)} messages, "
                f"~{total_length} chars"
            )
            
            return HookResult(
                success=True,
                modified=False,
                context=context,
                metadata={
                    'message_count': len(messages),
                    'total_length': total_length
                }
            )
        except Exception as e:
            logger.exception(f"Error logging context stats: {e}")
            return HookResult(success=False, modified=False, context=context, error=str(e))
    
    def _remove_duplicates(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove consecutive duplicate messages."""
        if len(messages) <= 1:
            return messages
        
        result = [messages[0]]
        for msg in messages[1:]:
            prev_msg = result[-1]
            if (msg.get('role') != prev_msg.get('role') or
                msg.get('content') != prev_msg.get('content')):
                result.append(msg)
        
        return result
    
    def _truncate_messages(
        self,
        messages: list[dict[str, Any]],
        max_length: int
    ) -> list[dict[str, Any]]:
        """Truncate overly long messages."""
        result = []
        for msg in messages:
            content = str(msg.get('content', ''))
            if len(content) > max_length:
                truncated_content = content[:max_length] + '... [truncated]'
                msg = {**msg, 'content': truncated_content}
            result.append(msg)
        return result
    
    def _enforce_token_limits(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int,
        preserve_system: bool,
        preserve_last_n: int
    ) -> list[dict[str, Any]]:
        """Ensure context stays within token limits."""
        # Convert dict messages to ChatMessage for proper token estimation
        chat_messages = [ChatMessage(**msg) if isinstance(msg, dict) else msg for msg in messages]
        estimated_tokens = estimate_token_count(chat_messages)
        
        if estimated_tokens <= max_tokens:
            return messages
        
        # Separate system and non-system messages
        system_msgs = [msg for msg in messages if msg.get('role') == 'system']
        other_msgs = [msg for msg in messages if msg.get('role') != 'system']
        
        # Preserve recent messages
        preserved = other_msgs[-preserve_last_n:] if len(other_msgs) > preserve_last_n else other_msgs
        removable = other_msgs[:-preserve_last_n] if len(other_msgs) > preserve_last_n else []
        
        # Remove oldest messages until under limit
        result = (system_msgs if preserve_system else []) + removable + preserved
        
        while len(result) > preserve_last_n:
            chat_result = [ChatMessage(**msg) if isinstance(msg, dict) else msg for msg in result]
            estimated_tokens = estimate_token_count(chat_result)
            if estimated_tokens <= max_tokens:
                break
            
            # Remove oldest non-system, non-preserved message
            for i, msg in enumerate(result):
                if msg not in system_msgs and msg not in preserved:
                    result.pop(i)
                    break
            else:
                break  # No more removable messages
        
        return result
