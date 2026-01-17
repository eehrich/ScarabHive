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

import logging
from pathlib import Path
from typing import Any

from agent_system.hooks import (
    SchemaBasedPluginHook,
    HookContext,
    HookResult,
)
from agent_system.llm.token_utils import estimate_token_count, estimate_content_tokens

logger = logging.getLogger(__name__)

class ContextOptimizerPlugin(SchemaBasedPluginHook):
    """
    Schema-based hook plugin that optimizes conversation context before LLM calls.
    
    Hook definitions are loaded from schema.yaml. Configuration is also
    loaded from schema.yaml with default values.
    """
    
    def __init__(self, plugin_dir: Path | str, mcp_config: Any = None):
        """Initialize the context optimizer plugin.
        
        Args:
            plugin_dir: Directory containing schema.yaml
            mcp_config: MCP configuration (contains config from plugins.yaml)
        """
        super().__init__(plugin_dir)
        
        # Merge mcp_config.config if provided (overrides schema defaults)
        if mcp_config and hasattr(mcp_config, 'config') and mcp_config.config:
            self._config.update(mcp_config.config)
        
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
            # Token-based limits
            max_message_tokens = int(config.get('max_message_length_tokens', 8000))
            # Hard limit - force-truncate even tool responses if they exceed this
            hard_max_message_tokens = int(config.get('hard_max_message_length_tokens', 16000))
            preserve_system = bool(config.get('preserve_system_messages', True))
            preserve_last_n = int(config.get('preserve_last_n_messages', 5))
            remove_dupes = bool(config.get('remove_duplicates', True))
            
            # Smart JSON truncation config (still char-based for string truncation)
            max_json_string_length = int(config.get('max_json_string_length', 5000))
            keep_string_end = bool(config.get('keep_string_end', True))
            
            # Calculate absolute token limit from percentage
            max_total_tokens = int(context_window * max_context_pct)
            
            # Estimate current token usage
            estimated_current = estimate_token_count(messages)
            
            logger.info(
                f"[ContextOptimizer] Context window check: "
                f"estimated_tokens={estimated_current}, "
                f"max_tokens={max_total_tokens} ({max_context_pct:.0%} of {context_window}), "
                f"messages={len(messages)}"
            )
            
            # Statistics
            original_count = len(messages)
            
            # Apply optimizations
            optimized_messages = messages.copy()
            
            if remove_dupes:
                optimized_messages = self._remove_duplicates(optimized_messages)
            
            optimized_messages = self._truncate_messages(
                optimized_messages,
                max_message_tokens,
                max_json_string_length,
                keep_string_end,
                hard_max_message_tokens
            )
            
            optimized_messages = self._enforce_token_limits(
                optimized_messages,
                max_total_tokens,
                preserve_system,
                preserve_last_n
            )
            
            context.messages = optimized_messages
            
            # Check if modified: either count changed OR content changed
            # IMPORTANT: Truncation modifies content without changing count!
            count_changed = len(optimized_messages) != len(messages)
            content_changed = False
            if not count_changed:
                # Check if any message content was actually modified
                for orig, opt in zip(messages, optimized_messages):
                    if orig.content != opt.content:
                        content_changed = True
                        break
            
            modified = count_changed or content_changed
            
            if modified:
                if count_changed:
                    logger.info(
                        f"[ContextOptimizer] Context optimized: {len(messages)} -> {len(optimized_messages)} messages "
                        f"(max {max_total_tokens} tokens = {max_context_pct:.0%} of {context_window})"
                    )
                elif content_changed:
                    logger.info(
                        f"[ContextOptimizer] Messages truncated (count unchanged: {len(messages)} messages)"
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
        """Remove consecutive duplicate messages.
        
        IMPORTANT: Never remove tool responses or assistant messages with tool_calls as duplicates,
        even if content is identical. Each belongs to a specific interaction that must be preserved.
        """
        if len(messages) <= 1:
            return messages
        
        result = [messages[0]]
        removed_count = 0
        for i, msg in enumerate(messages[1:], start=1):
            prev_msg = result[-1]
            
            # Never treat tool-related messages as duplicates
            if msg.role == "tool":
                # Tool responses have unique tool_call_ids
                result.append(msg)
                continue
            
            if msg.role == "assistant" and getattr(msg, "tool_calls", None):
                # Assistant messages with tool_calls are unique even if content is same
                result.append(msg)
                continue
            
            if (msg.role != prev_msg.role or msg.content != prev_msg.content):
                result.append(msg)
            else:
                removed_count += 1
                logger.info(
                    f"[ContextOptimizer] Removed duplicate message at idx {i}: "
                    f"role={msg.role}, content_len={len(str(msg.content))}"
                )
        
        if removed_count > 0:
            logger.info(f"[ContextOptimizer] Removed {removed_count} duplicate message(s)")
        
        return result
    
    def _smart_truncate_json_strings(
        self,
        data: any,
        max_string_length: int = 5000,
        keep_end: bool = True
    ) -> any:
        """Recursively truncate long strings in JSON data while keeping structure valid.
        
        Args:
            data: JSON-serializable data (dict, list, str, etc.)
            max_string_length: Max length for string values
            keep_end: If True, keep end of string (newest data); if False, keep beginning
        
        Returns:
            Truncated data with same structure
        """
        if isinstance(data, dict):
            return {k: self._smart_truncate_json_strings(v, max_string_length, keep_end) for k, v in data.items()}
        elif isinstance(data, list):
            return [self._smart_truncate_json_strings(item, max_string_length, keep_end) for item in data]
        elif isinstance(data, str) and len(data) > max_string_length:
            if keep_end:
                # Keep the END (newest data usually more important)
                return f"...[{len(data) - max_string_length} chars removed]..." + data[-max_string_length:]
            else:
                # Keep the BEGINNING
                return data[:max_string_length] + f"...[{len(data) - max_string_length} chars removed]..."
        else:
            return data
    
    def _truncate_messages(
        self,
        messages: list,
        max_tokens: int,
        max_json_string_length: int = 5000,
        keep_string_end: bool = True,
        hard_max_tokens: int = 25000
    ) -> list:
        """Truncate overly long messages based on estimated token count.
        
        For tool responses with JSON: intelligently truncates long strings within the JSON
        while keeping structure valid. For other messages: simple truncation with warning.
        
        Handles multimodal content by extracting text for length checks while
        preserving original structure for images/audio.
        
        IMPORTANT: Messages exceeding hard_max_tokens are force-truncated even if
        JSON truncation fails. This prevents extremely large tool responses from
        breaking LLM calls.
        
        Args:
            messages: List of messages to truncate
            max_tokens: Max estimated tokens for entire message (soft limit, triggers optimization)
            max_json_string_length: Max length for strings within JSON (chars)
            keep_string_end: If True, keep end of strings; if False, keep beginning
            hard_max_tokens: Absolute max tokens - force-truncate if exceeded
        """
        import json
        from agent_system.llm.token_utils import extract_text_from_content
        
        result = []
        for msg in messages:
            # Handle multimodal content - extract text for length check
            raw_content = msg.content
            if isinstance(raw_content, list):
                # Multimodal content - don't truncate, preserve structure
                # Images/audio have their own size limits handled during upload
                result.append(msg)
                continue
            
            content = extract_text_from_content(raw_content)
            
            # Estimate tokens for this message content
            content_tokens = estimate_content_tokens(content)
            
            # Special handling for tool responses - try smart JSON truncation
            if msg.role == 'tool' and content_tokens > max_tokens:
                try:
                    # Try to parse as JSON
                    data = json.loads(content)
                    
                    # Smart truncate: reduce long strings within JSON, keep structure valid
                    truncated_data = self._smart_truncate_json_strings(
                        data,
                        max_string_length=max_json_string_length,
                        keep_end=keep_string_end
                    )
                    
                    truncated_content = json.dumps(truncated_data, ensure_ascii=False)
                    truncated_tokens = estimate_content_tokens(truncated_content)
                    
                    if truncated_tokens < content_tokens:
                        logger.info(
                            f"[ContextOptimizer] Smart-truncated tool response JSON: "
                            f"{content_tokens} -> {truncated_tokens} tokens"
                        )
                        # Check if still exceeds hard limit
                        if truncated_tokens > hard_max_tokens:
                            # Force truncate by chars (rough estimate: 4 chars per token)
                            max_chars = hard_max_tokens * 4
                            truncated_content = truncated_content[:max_chars] + '... [FORCE TRUNCATED - exceeded hard limit]'
                            logger.warning(
                                f"[ContextOptimizer] Force-truncated tool response to ~{hard_max_tokens} tokens (exceeded hard limit)"
                            )
                        truncated_msg = msg.model_copy(update={'content': truncated_content})
                        result.append(truncated_msg)
                    else:
                        # Truncation didn't help - check hard limit
                        if content_tokens > hard_max_tokens:
                            max_chars = hard_max_tokens * 4
                            truncated_content = content[:max_chars] + '... [FORCE TRUNCATED - exceeded hard limit]'
                            truncated_msg = msg.model_copy(update={'content': truncated_content})
                            result.append(truncated_msg)
                            logger.warning(
                                f"[ContextOptimizer] Force-truncated tool response from {content_tokens} to ~{hard_max_tokens} tokens (hard limit)"
                            )
                        else:
                            result.append(msg)
                            # Only log at DEBUG level - this is normal for binary/base64 content
                            logger.debug(
                                f"[ContextOptimizer] Tool response is {content_tokens} tokens (>{max_tokens}) "
                                f"and couldn't be reduced. This is normal for binary/base64 content."
                            )
                    continue
                    
                except (json.JSONDecodeError, Exception) as e:
                    # Not JSON or error - check hard limit
                    if content_tokens > hard_max_tokens:
                        max_chars = hard_max_tokens * 4
                        truncated_content = content[:max_chars] + '... [FORCE TRUNCATED - exceeded hard limit]'
                        truncated_msg = msg.model_copy(update={'content': truncated_content})
                        result.append(truncated_msg)
                        logger.warning(
                            f"[ContextOptimizer] Force-truncated non-JSON tool response from {content_tokens} to ~{hard_max_tokens} tokens"
                        )
                    else:
                        logger.debug(
                            f"[ContextOptimizer] Tool response is {content_tokens} tokens (>{max_tokens}) "
                            f"but NOT JSON (parse error: {e}). Keeping original."
                        )
                        result.append(msg)
                    continue
            
            # Tool response within limit - keep as-is
            if msg.role == 'tool':
                result.append(msg)
                continue
            
            # Non-tool messages: simple truncation based on tokens
            if content_tokens > max_tokens:
                # Check if content looks like JSON/structured data
                is_json_like = content.strip().startswith(('{', '['))
                
                if is_json_like:
                    logger.warning(
                        f"[ContextOptimizer] Truncating {msg.role} message with JSON-like content "
                        f"({content_tokens} tokens). This may break parsing. Consider removing instead."
                    )
                
                # Truncate by chars (rough estimate: 4 chars per token)
                max_chars = max_tokens * 4
                truncated_content = content[:max_chars] + '... [truncated]'
                # Create new ChatMessage with truncated content
                truncated_msg = msg.model_copy(update={'content': truncated_content})
                result.append(truncated_msg)
            else:
                result.append(msg)
        return result
    
    def _get_tool_call_ids(self, msg) -> set[str]:
        """Extract tool_call IDs from an assistant message."""
        if msg.role != 'assistant' or not msg.tool_calls:
            return set()
        return {tc.get('id') for tc in msg.tool_calls if tc.get('id')}
    
    def _get_tool_response_id(self, msg) -> str | None:
        """Extract tool_call_id from a tool response message."""
        if msg.role != 'tool':
            return None
        return getattr(msg, 'tool_call_id', None)
    
    def _find_removable_message_groups(
        self,
        messages: list,
        system_msgs: list,
        preserved: list
    ) -> list[list[int]]:
        """
        Find groups of messages that can be safely removed together.
        
        Groups tool_call assistant messages with their tool responses to ensure
        they are removed together (avoiding orphaned tool_calls/responses).
        
        Returns list of message index groups, oldest first.
        """
        groups = []
        i = 0
        
        while i < len(messages):
            msg = messages[i]
            
            # Skip system and preserved messages
            if msg in system_msgs or msg in preserved:
                i += 1
                continue
            
            # Check if this is an assistant with tool_calls
            tool_call_ids = self._get_tool_call_ids(msg)
            
            if tool_call_ids:
                # Find all following tool responses that match these tool_calls
                group = [i]
                j = i + 1
                remaining_ids = tool_call_ids.copy()
                
                while j < len(messages) and remaining_ids:
                    next_msg = messages[j]
                    response_id = self._get_tool_response_id(next_msg)
                    
                    if response_id and response_id in remaining_ids:
                        group.append(j)
                        remaining_ids.discard(response_id)
                        j += 1
                    elif next_msg.role == 'tool':
                        # Tool response but not matching - still part of sequence
                        j += 1
                    else:
                        # Non-tool message, stop looking
                        break
                
                # Only add as group if we found ALL matching responses (complete pair)
                # AND all messages in the group are removable
                all_responses_found = len(remaining_ids) == 0
                all_removable = all(
                    messages[idx] not in preserved and messages[idx] not in system_msgs
                    for idx in group
                )
                
                # Only allow removal if BOTH conditions are met:
                # 1. All tool responses were found (no missing responses)
                # 2. All messages in the group are removable (not preserved)
                if all_responses_found and all_removable:
                    groups.append(group)
                else:
                    # Don't remove incomplete groups - would create orphaned tool_calls
                    if not all_responses_found:
                        logger.debug(
                            f"[ContextOptimizer] Skipping incomplete tool_call group at idx {i}: "
                            f"missing {len(remaining_ids)} responses (IDs: {remaining_ids})"
                        )
                    if not all_removable:
                        logger.debug(
                            f"[ContextOptimizer] Skipping tool_call group at idx {i}: "
                            f"some messages are preserved"
                        )
                
                # Move past this group
                i = max(group) + 1 if group else i + 1
            else:
                # Check if this is an orphan tool response (no matching tool_call found earlier)
                response_id = self._get_tool_response_id(msg)
                if response_id:
                    # This is a tool response - check if its tool_call exists in messages
                    has_matching_call = False
                    for prev_msg in messages[:i]:
                        if response_id in self._get_tool_call_ids(prev_msg):
                            has_matching_call = True
                            break
                    
                    if not has_matching_call:
                        # No matching tool_call - this response would be orphaned if we remove anything
                        # Skip it to avoid creating orphaned tool_response
                        logger.debug(
                            f"[ContextOptimizer] Skipping tool response at idx {i} with id {response_id}: "
                            f"no matching tool_call found - would create orphaned response"
                        )
                        i += 1
                        continue
                
                # Regular message (user, assistant without tools) - can be removed alone
                groups.append([i])
                i += 1
        
        return groups
    
    def _enforce_token_limits(
        self,
        messages: list,
        max_tokens: int,
        preserve_system: bool,
        preserve_last_n: int
    ) -> list:
        """Ensure context stays within token limits.
        
        IMPORTANT: Removes tool_call/tool_response pairs together to avoid
        orphaned tool_calls or tool_responses.
        """
        # Messages are already ChatMessage objects
        estimated_tokens = estimate_token_count(messages)
        
        if estimated_tokens <= max_tokens:
            return messages
        
        # Separate system and non-system messages
        system_msgs = [msg for msg in messages if msg.role == 'system']
        other_msgs = [msg for msg in messages if msg.role != 'system']
        
        # Preserve recent messages
        preserved = other_msgs[-preserve_last_n:] if len(other_msgs) > preserve_last_n else other_msgs
        
        # Build result list
        result = messages.copy()
        
        # Find removable message groups (tool_call + responses grouped together)
        removable_groups = self._find_removable_message_groups(result, system_msgs, preserved)
        
        # Remove groups from oldest until under token limit
        for group in removable_groups:
            estimated_tokens = estimate_token_count(result)
            if estimated_tokens <= max_tokens:
                break
            
            # Remove all messages in this group (in reverse order to preserve indices)
            for idx in sorted(group, reverse=True):
                if idx < len(result):
                    removed_msg = result[idx]
                    if removed_msg not in system_msgs and removed_msg not in preserved:
                        logger.info(
                            f"[ContextOptimizer] Removing message at idx {idx}: "
                            f"role={removed_msg.role}, has_tool_calls={bool(getattr(removed_msg, 'tool_calls', None))}, "
                            f"tool_call_id={getattr(removed_msg, 'tool_call_id', None)}"
                        )
                        result.pop(idx)
        
        return result
