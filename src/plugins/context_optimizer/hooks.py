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
import logging

from agent_system.hooks import (
    SchemaBasedPluginHook,
    HookContext,
    HookResult,
)
from agent_system.llm.token_utils import estimate_token_count

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
            
            # Get config values - for hooks, config is a raw dict from YAML
            config = self.get_config()
            max_context_pct = float(config.get('max_context_percentage', 0.80))
            max_message_length = int(config.get('max_message_length_chars', 50000))
            preserve_system = bool(config.get('preserve_system_messages', True))
            preserve_last_n = int(config.get('preserve_last_n_messages', 5))
            remove_dupes = bool(config.get('remove_duplicates', True))
            
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
            
            context.messages = optimized_messages
            
            modified = len(optimized_messages) != len(messages)
            
            if modified:
                logger.info(
                    f"[ContextOptimizer] Context optimized: {len(messages)} -> {len(optimized_messages)} messages "
                    f"(max {max_total_tokens} tokens = {max_context_pct:.0%} of {context_window})"
                )
            
            return HookResult(
                success=True,
                modified=modified,
                context=context,
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
        """Extract context window size from agent's LLM config.

        Respects WebUI llm_profile overrides by checking context.llm first.

        Args:
            context: Hook context containing agent with config

        Returns:
            Context window size in tokens, or None if not available
        """
        # Check if context has LLM instance with context_window (respects override)
        if context.llm and hasattr(context.llm, 'context_window') and context.llm.context_window:
            return context.llm.context_window

        # Fallback: resolve from agent's default config
        if context.agent and hasattr(context.agent, 'agent_config') and hasattr(context.agent, 'system_config'):
            try:
                from agent_system.llm.factory import resolve_llm_config_for_agent

                llm_config = resolve_llm_config_for_agent(
                    context.agent.system_config,
                    context.agent.agent_config
                )

                if 'context_window' in llm_config and llm_config['context_window']:
                    return llm_config['context_window']
            except Exception as e:
                logger.warning(f"[ContextOptimizer] Error resolving LLM config: {e}")

        logger.warning("[ContextOptimizer] Could not determine context window size")
        return None    # Handler for optional stats logging hook
    async def log_context_stats(self, context: HookContext) -> HookResult:
        """Log context statistics after LLM call (if enabled in schema.yaml)."""
        try:
            messages = context.messages or []
            # Messages are ChatMessage objects
            total_length = sum(len(str(msg.content)) for msg in messages)
            
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
    
    def _remove_duplicates(self, messages: list) -> list:
        """Remove consecutive duplicate messages."""
        if len(messages) <= 1:
            return messages
        
        result = [messages[0]]
        for msg in messages[1:]:
            prev_msg = result[-1]
            if (msg.role != prev_msg.role or msg.content != prev_msg.content):
                result.append(msg)
        
        return result
    
    def _truncate_messages(
        self,
        messages: list,
        max_length: int
    ) -> list:
        """Truncate overly long messages."""
        result = []
        for msg in messages:
            content = str(msg.content)
            if len(content) > max_length:
                truncated_content = content[:max_length] + '... [truncated]'
                # Create new ChatMessage with truncated content
                truncated_msg = msg.model_copy(update={'content': truncated_content})
                result.append(truncated_msg)
            else:
                result.append(msg)
        return result
    
    def _enforce_token_limits(
        self,
        messages: list,
        max_tokens: int,
        preserve_system: bool,
        preserve_last_n: int
    ) -> list:
        """Ensure context stays within token limits."""
        # Messages are already ChatMessage objects
        estimated_tokens = estimate_token_count(messages)
        
        if estimated_tokens <= max_tokens:
            return messages
        
        # Separate system and non-system messages
        system_msgs = [msg for msg in messages if msg.role == 'system']
        other_msgs = [msg for msg in messages if msg.role != 'system']
        
        # Preserve recent messages
        preserved = other_msgs[-preserve_last_n:] if len(other_msgs) > preserve_last_n else other_msgs
        removable = other_msgs[:-preserve_last_n] if len(other_msgs) > preserve_last_n else []
        
        # Remove oldest messages until under limit
        result = (system_msgs if preserve_system else []) + removable + preserved
        
        while len(result) > preserve_last_n:
            estimated_tokens = estimate_token_count(result)
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
