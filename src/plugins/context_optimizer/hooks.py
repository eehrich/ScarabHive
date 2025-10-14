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
            
            # Get config values
            config = self.get_config()
            max_total_tokens = config.get('max_total_tokens', 100000)
            max_message_length = config.get('max_message_length', 50000)
            preserve_system = config.get('preserve_system_messages', True)
            preserve_last_n = config.get('preserve_last_n_messages', 5)
            remove_dupes = config.get('remove_duplicates', True)
            
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
                    f"Context optimized: {len(messages)} -> {len(optimized_messages)} messages"
                )
            
            return HookResult(
                success=True,
                modified=modified,
                context=modified_context,
                metadata={
                    'original_count': original_count,
                    'optimized_count': len(optimized_messages),
                    'removed_count': original_count - len(optimized_messages)
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
        # Simple char-based approximation (1 token ≈ 4 chars)
        estimated_tokens = sum(len(str(msg.get('content', ''))) for msg in messages) // 4
        
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
            estimated_tokens = sum(len(str(msg.get('content', ''))) for msg in result) // 4
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
