"""""""""

Context Optimizer Plugin - Schema-Based Hook Plugin.

Context Optimizer Plugin - Schema-Based Hook Plugin.Context Optimizer Plugin - Reference Hook Plugin Implementation.

This plugin demonstrates schema-based hook implementation.

Hook definitions are loaded from schema.yaml, handlers from hooks.py.



**Plugin Type:** Hook-only (inherits from SchemaBasedPluginHook)This plugin demonstrates schema-based hook implementation.This plugin demonstrates how to implement a hooks-only plugin that modifies



**Hooks Defined in schema.yaml:**Hook definitions are loaded from schema.yaml, handlers from hooks.py.the conversation context before LLM calls to optimize token usage and improve

- context_optimizer: Main optimization hook (pre_llm_call)

- context_stats_logger: Optional stats logging hook (post_llm_call)response quality.

"""

**Plugin Type:** Hook-only (inherits from SchemaBasedPluginHook)

from .hooks import ContextOptimizerPlugin

**Plugin Type:** Hook-only (inherits only from PluginHook, NOT MCPServer)

# Plugin factory for discovery

def PLUGIN_FACTORY() -> ContextOptimizerPlugin:**Hooks Defined in schema.yaml:**

    """Factory function for plugin discovery."""

    return ContextOptimizerPlugin()- context_optimizer: Main optimization hook (pre_llm_call)**Hooks Implemented:**


- context_stats_logger: Optional stats logging hook (post_llm_call)- PRE_LLM_CALL: Optimizes message context before sending to LLM

"""

**Features:**

import logging- Removes duplicate consecutive messages

- Truncates overly long messages

from agent_system.hooks import SchemaBasedPluginHook- Ensures context stays within configurable token limits

- Preserves system messages and recent user messages

logger = logging.getLogger(__name__)- Adds optimization metadata to hook results

"""



class ContextOptimizerPlugin(SchemaBasedPluginHook):from typing import Any

    """

    Schema-based hook plugin that optimizes conversation context before LLM calls.from agent_system.hooks.plugin_hook import (

        HookContext,

    Hook definitions are loaded from schema.yaml.    HookResult,

    Handler methods are imported from hooks.py module.    PluginHook,

    """)

    

    def __init__(self):

        """Initialize the context optimizer plugin."""class ContextOptimizerPlugin(PluginHook):

        super().__init__()    """

        logger.info("ContextOptimizerPlugin initialized with schema-based hooks")    Hook plugin that optimizes conversation context before LLM calls.

    

    This is a **hook-only** plugin - it does NOT inherit from MCPServer

# Plugin factory for discovery    because it provides no tools, only lifecycle hooks.

def PLUGIN_FACTORY() -> ContextOptimizerPlugin:    

    """Factory function for plugin discovery."""    Configuration options:

    return ContextOptimizerPlugin()    - max_total_tokens: Maximum total tokens for context (default: 8000)

    - max_message_length: Maximum length for individual messages (default: 10000)
    - preserve_system_messages: Always preserve system messages (default: true)
    - preserve_recent_count: Number of recent messages to always preserve (default: 3)
    - remove_duplicates: Remove duplicate consecutive messages (default: true)
    """
    
    def __init__(self, name: str, config: dict[str, Any] | None = None):
        """
        Initialize the context optimizer plugin.
        
        Args:
            name: Plugin instance name
            config: Plugin configuration dictionary
        """
        super().__init__(name, config)
        
        # Configuration with defaults
        self.max_total_tokens = self.config.get('max_total_tokens', 8000)
        self.max_message_length = self.config.get('max_message_length', 10000)
        self.preserve_system_messages = self.config.get('preserve_system_messages', True)
        self.preserve_recent_count = self.config.get('preserve_recent_count', 3)
        self.remove_duplicates = self.config.get('remove_duplicates', True)
    
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """
        Optimize conversation context before LLM call.
        
        This hook:
        1. Removes duplicate consecutive messages
        2. Truncates overly long messages
        3. Estimates token usage and removes oldest non-system messages if needed
        4. Preserves system messages and recent messages
        
        Args:
            context: Hook context with messages and metadata
            
        Returns:
            HookResult with optimized messages and optimization stats
        """
        messages = context.messages or []
        
        if not messages:
            return HookResult(
                success=True,
                modified=False,
                context=context,
                metadata={'optimization': 'no_messages'}
            )
        
        # Statistics
        original_count = len(messages)
        original_length = sum(len(str(msg.get('content', ''))) for msg in messages)
        
        # Convert messages to dicts if they aren't already
        message_dicts = [msg if isinstance(msg, dict) else msg.model_dump() for msg in messages]
        
        # Step 1: Remove duplicates
        if self.remove_duplicates:
            message_dicts = self._remove_duplicates(message_dicts)
        
        # Step 2: Truncate long messages
        message_dicts = self._truncate_messages(message_dicts)
        
        # Step 3: Ensure token limits
        message_dicts = self._enforce_token_limits(message_dicts)
        
        # Calculate optimization stats
        optimized_count = len(message_dicts)
        optimized_length = sum(len(str(msg.get('content', ''))) for msg in message_dicts)
        
        # Create modified context
        modified_context = HookContext(
            hook_type=context.hook_type,
            request_id=context.request_id,
            session_id=context.session_id,
            agent=context.agent,
            agent_name=context.agent_name,
            messages=message_dicts,
            llm_response=context.llm_response,
            tool_call=context.tool_call,
            tool_result=context.tool_result,
            output=context.output,
            metadata=context.metadata,
            step=context.step,
            llm=context.llm
        )
        
        return HookResult(
            success=True,
            modified=True,
            context=modified_context,
            metadata={
                'optimization': {
                    'original_message_count': original_count,
                    'optimized_message_count': optimized_count,
                    'messages_removed': original_count - optimized_count,
                    'original_total_length': original_length,
                    'optimized_total_length': optimized_length,
                    'bytes_saved': original_length - optimized_length,
                    'reduction_percentage': round((1 - optimized_length / max(original_length, 1)) * 100, 2)
                }
            }
        )
    
    def _remove_duplicates(self, messages: list[dict]) -> list[dict]:
        """
        Remove duplicate consecutive messages.
        
        Args:
            messages: List of message dictionaries
            
        Returns:
            List with duplicates removed
        """
        if len(messages) <= 1:
            return messages
        
        result = [messages[0]]
        
        for msg in messages[1:]:
            prev_msg = result[-1]
            
            # Compare role and content
            if (msg.get('role') != prev_msg.get('role') or 
                msg.get('content') != prev_msg.get('content')):
                result.append(msg)
        
        return result
    
    def _truncate_messages(self, messages: list[dict]) -> list[dict]:
        """
        Truncate messages that exceed max_message_length.
        
        Args:
            messages: List of message dictionaries
            
        Returns:
            List with truncated messages
        """
        result = []
        
        for msg in messages:
            content = msg.get('content', '')
            
            # Skip if not a string (could be multimodal content)
            if not isinstance(content, str):
                result.append(msg)
                continue
            
            if len(content) > self.max_message_length:
                # Truncate and add indicator
                truncated_content = content[:self.max_message_length] + "\n\n[... content truncated by context optimizer ...]"
                result.append({**msg, 'content': truncated_content})
            else:
                result.append(msg)
        
        return result
    
    def _enforce_token_limits(self, messages: list[dict]) -> list[dict]:
        """
        Ensure total context stays within token limits.
        
        This uses a rough estimation: 1 token ≈ 4 characters.
        Removes oldest non-system messages first, but preserves
        recent messages based on preserve_recent_count.
        
        Args:
            messages: List of message dictionaries
            
        Returns:
            List with messages within token limits
        """
        # Rough token estimation (1 token ≈ 4 chars)
        def estimate_tokens(msg: dict) -> int:
            content = msg.get('content', '')
            if isinstance(content, str):
                return len(content) // 4
            return 0  # Multimodal content - conservative estimate
        
        total_tokens = sum(estimate_tokens(msg) for msg in messages)
        
        if total_tokens <= self.max_total_tokens:
            return messages
        
        # Separate system, recent, and old messages
        system_messages = []
        recent_messages = []
        old_messages = []
        
        for i, msg in enumerate(messages):
            is_recent = i >= len(messages) - self.preserve_recent_count
            is_system = msg.get('role') == 'system'
            
            if is_system and self.preserve_system_messages:
                system_messages.append(msg)
            elif is_recent:
                recent_messages.append(msg)
            else:
                old_messages.append(msg)
        
        # Always keep system and recent messages
        result = system_messages + recent_messages
        result_tokens = sum(estimate_tokens(msg) for msg in result)
        
        # Add old messages from newest to oldest until we hit limit
        for msg in reversed(old_messages):
            msg_tokens = estimate_tokens(msg)
            if result_tokens + msg_tokens <= self.max_total_tokens:
                # Insert after system messages but before recent messages
                result.insert(len(system_messages), msg)
                result_tokens += msg_tokens
            else:
                break
        
        return result
