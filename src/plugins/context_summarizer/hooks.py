"""Context Summarizer Plugin - Intelligent context reduction using LLM.

This plugin uses LLM to intelligently summarize older conversation messages,
reducing context size while preserving key information and decisions.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from agent_system.hooks import SchemaBasedPluginHook, HookContext, HookResult
from agent_system.llm.token_utils import estimate_token_count
from agent_system.llm.models import ChatMessage
from agent_system.mcp.status import status_bus, StatusScope

logger = logging.getLogger(__name__)


class ContextSummarizerPlugin(SchemaBasedPluginHook):
    """Schema-based plugin for intelligent context summarization.

    Uses LLM to create concise summaries of older messages when context
    exceeds configured token limits. Preserves recent messages and system
    messages while summarizing older conversation history.

    Configuration is loaded from schema.yaml.
    """

    def __init__(self, plugin_dir: Path | str, summarization_history: List[Dict[str, Any]] | None = None):
        """Initialize the context summarizer plugin.

        Args:
            plugin_dir: Directory containing schema.yaml
            summarization_history: Optional list to track summarization events for web UI
        """
        super().__init__(plugin_dir)

        # Web UI history tracking
        self.summarization_history = summarization_history

        # Load config - for hooks, config is a raw dict from YAML
        config = self.get_config()
        self.trigger_percentage = float(config.get('summarization_trigger_percentage', 0.60))
        self.chunk_size = int(config.get('summarization_chunk_size', 10))
        self.preserve_recent = int(config.get('preserve_recent_count', 10))
        self.preserve_system = bool(config.get('preserve_system_messages', True))
        self.llm_profile = str(config.get('llm_profile', 'fast'))
        self.prompt_template = str(config.get('summary_prompt_template', ''))
        self.min_reduction = float(config.get('min_summary_reduction', 0.3))
        self.store_metadata = bool(config.get('store_original_metadata', True))
        self.marker_format = str(config.get('summary_marker_format',
                                        '[Summary of {count} messages from {start_time} to {end_time}]'))
        self.max_preview_length = int(config.get('max_message_preview_length', 5000))

        logger.info(
            f"ContextSummarizerPlugin initialized: trigger={self.trigger_percentage:.0%} of context window, "
            f"chunk_size={self.chunk_size}, preserve_recent={self.preserve_recent}, "
            f"llm_profile={self.llm_profile}"
        )

    async def summarize_context(self, context: HookContext) -> HookResult:
        """Summarize older messages when context exceeds token limit.

        Handler for 'summarize_context' hook defined in schema.yaml.
        Uses percentage-based threshold relative to LLM context window.

        Args:
            context: Hook context with messages and metadata

        Returns:
            HookResult with summarized messages or original context
        """
        try:
            messages = context.messages or []

            if not messages:
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={'reason': 'no_messages'}
                )

            # Convert ChatMessage objects to dicts for internal processing
            from agent_system.llm.models import ChatMessage
            messages_as_dicts = []
            for msg in messages:
                if isinstance(msg, ChatMessage):
                    messages_as_dicts.append(msg.model_dump(exclude_none=True))
                else:
                    messages_as_dicts.append(msg)

            # Get LLM context window size
            context_window = self._get_context_window(context)
            if not context_window:
                logger.warning(
                    f"[ContextSummarizer] No LLM context window available for session {context.session_id}. "
                    f"Skipping summarization. context.llm={context.llm}, "
                    f"has_context_window={hasattr(context.llm, 'context_window') if context.llm else False}"
                )
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={'reason': 'no_context_window'}
                )

            # Calculate trigger threshold from percentage
            trigger_tokens = int(context_window * self.trigger_percentage)

            # Try to get actual prompt tokens from context_usage_tracker (more accurate)
            # If not available, fall back to estimation
            total_tokens = self._get_actual_or_estimated_tokens(context, messages_as_dicts)

            # Skip threshold check if this is a manual trigger
            is_manual_trigger = context.metadata.get('manual_trigger', False) if context.metadata else False

            if not is_manual_trigger and total_tokens < trigger_tokens:
                logger.info(
                    f"[ContextSummarizer] Session {context.session_id}: Below threshold - "
                    f"total_tokens={total_tokens}, trigger_tokens={trigger_tokens} "
                    f"({self.trigger_percentage:.0%} of context_window={context_window})"
                )
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={
                        'reason': 'below_threshold',
                        'total_tokens': total_tokens,
                        'threshold': trigger_tokens,
                        'threshold_percentage': self.trigger_percentage,
                        'context_window': context_window
                    }
                )

            logger.info(
                f"[ContextSummarizer] Context {'(manual trigger) ' if is_manual_trigger else ''}exceeds threshold: {total_tokens} {'forced' if is_manual_trigger else '> ' + str(trigger_tokens)} tokens "
                f"({self.trigger_percentage:.0%} of {context_window}). Starting summarization for session {context.session_id}"
            )

            # Generate unique request_id for summarizer status messages (like tool calls)
            # This must be done BEFORE creating StatusScope so all messages use the same unique ID
            summarizer_request_id = context.request_id
            if context.agent:
                summarizer_request_id = await context.agent.next_internal_tool_request_id(context.request_id)

            # Separate messages into categories first (needed for start message)
            system_msgs, recent_msgs, old_msgs = self._categorize_messages(messages_as_dicts)

            if len(old_msgs) < 2:
                # Not enough old messages to summarize
                logger.info(
                    f"[ContextSummarizer] Session {context.session_id}: Insufficient old messages - "
                    f"old_msgs={len(old_msgs)}, recent_msgs={len(recent_msgs)}, system_msgs={len(system_msgs)}"
                )
                
                # Record in history even when not applied
                if self.summarization_history is not None:
                    event = {
                        'timestamp': datetime.now().isoformat(),
                        'session_id': context.session_id,
                        'request_id': context.request_id,
                        'strategy': 'summarize',
                        'original_message_count': len(messages),
                        'summarized_message_count': len(messages),
                        'messages_summarized': 0,
                        'summary_count': 0,
                        'original_tokens': 0,
                        'new_tokens': 0,
                        'tokens_saved': 0,
                        'reduction_ratio': 0,
                        'status': 'skipped',
                        'reason': 'insufficient_old_messages',
                        'before_messages': [],
                        'after_messages': [],
                        'summary_stats': {'summary_count': 0}
                    }
                    self.summarization_history.append(event)
                
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={
                        'reason': 'insufficient_old_messages',
                        'old_message_count': len(old_msgs)
                    }
                )

            # Use StatusScope to ensure START/END pairing even on errors
            async with StatusScope(
                status_bus,
                "context_summarizer",
                summarizer_request_id,
                start_msg=f"Summarizing {len(old_msgs)} older messages using LLM (preserving {len(recent_msgs)} recent messages)",
                end_msg="Context summarization completed"
            ) as scope:
                # Small sleep to allow START message to be delivered
                await asyncio.sleep(0.01)

                # Summarize old messages in chunks
                summarized_msgs, summary_stats = await self._summarize_messages(
                    old_msgs,
                    context,
                    scope
                )

                # Reconstruct message list: system + summarized + recent
                new_messages_dicts = system_msgs + summarized_msgs + recent_msgs

                # Calculate reduction
                original_tokens = self._estimate_tokens(messages_as_dicts)
                new_tokens = self._estimate_tokens(new_messages_dicts)
                reduction_ratio = 1 - (new_tokens / max(original_tokens, 1))

                # Check if reduction meets minimum threshold
                if reduction_ratio < self.min_reduction:
                    logger.debug(
                        f"[ContextSummarizer] Summarization reduction ({reduction_ratio:.2%}) "
                        f"below minimum ({self.min_reduction:.2%}). Keeping original messages."
                    )
                    
                    # Record in history even when not applied
                    if self.summarization_history is not None:
                        event = {
                            'timestamp': datetime.now().isoformat(),
                            'session_id': context.session_id,
                            'request_id': context.request_id,
                            'strategy': 'summarize',
                            'original_message_count': len(messages),
                            'summarized_message_count': len(messages),  # No change
                            'messages_summarized': len(old_msgs),
                            'summary_count': 0,
                            'original_tokens': original_tokens,
                            'new_tokens': original_tokens,  # No change
                            'tokens_saved': 0,
                            'reduction_ratio': reduction_ratio,
                            'status': 'rejected',
                            'reason': 'insufficient_reduction',
                            'before_messages': [],
                            'after_messages': [],
                            'summary_stats': {'summary_count': 0}
                        }
                        self.summarization_history.append(event)
                    
                    return HookResult(
                        success=True,
                        modified=False,
                        context=context,
                        metadata={
                            'reason': 'insufficient_reduction',
                            'reduction_ratio': reduction_ratio,
                            'min_reduction': self.min_reduction
                        }
                    )

                # Convert dicts back to ChatMessage objects
                new_messages = []
                for msg_dict in new_messages_dicts:
                    if isinstance(msg_dict, dict):
                        new_messages.append(ChatMessage(**msg_dict))
                    else:
                        new_messages.append(msg_dict)

                # Create modified context
                modified_context = HookContext(
                    hook_type=context.hook_type,
                    request_id=context.request_id,
                    session_id=context.session_id,
                    agent=context.agent,
                    agent_name=context.agent_name,
                    messages=new_messages,
                    llm_response=context.llm_response,
                    tool_call=context.tool_call,
                    tool_result=context.tool_result,
                    output=context.output,
                    metadata=context.metadata,
                    step=context.step,
                    llm=context.llm
                )

                logger.info(
                    f"[ContextSummarizer] Summarization complete: "
                    f"{len(messages)} → {len(new_messages)} messages, "
                    f"{original_tokens} → {new_tokens} tokens ({reduction_ratio:.1%} reduction)"
                )

                # Record summarization event in history
                if self.summarization_history is not None:
                    event = {
                        'timestamp': datetime.now().isoformat(),
                        'session_id': context.session_id,
                        'request_id': context.request_id,
                        'strategy': 'summarize',  # context_summarizer uses LLM summarization
                        'original_message_count': len(messages),
                        'summarized_message_count': len(new_messages),
                        'messages_summarized': len(old_msgs),
                        'summary_count': summary_stats['summary_count'],
                        'original_tokens': original_tokens,
                        'new_tokens': new_tokens,
                        'tokens_saved': original_tokens - new_tokens,
                        'reduction_ratio': reduction_ratio,
                        'status': 'success',  # Mark successful summarizations
                        'before_messages': [self._serialize_message(m) for m in old_msgs],  # ALL messages that were removed (summarized)
                        'after_messages': [self._serialize_message(m) for m in summarized_msgs],  # Summary messages created from old_msgs
                        'summary_stats': summary_stats
                    }
                    self.summarization_history.append(event)

                    # Keep only last 1000 events
                    if len(self.summarization_history) > 1000:
                        self.summarization_history.pop(0)

                return HookResult(
                    success=True,
                    modified=True,
                    context=modified_context,
                    metadata={
                        'summarization': {
                            'original_message_count': len(messages),
                            'summarized_message_count': len(new_messages),
                            'messages_summarized': len(old_msgs),
                            'summary_count': summary_stats['summary_count'],
                            'original_tokens': original_tokens,
                            'new_tokens': new_tokens,
                            'tokens_saved': original_tokens - new_tokens,
                            'reduction_ratio': reduction_ratio,
                            **summary_stats
                        }
                    }
                )

        except Exception as e:
            logger.error(f"[ContextSummarizer] Error during summarization: {e}", exc_info=True)
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
                logger.warning(f"[ContextSummarizer] Error resolving LLM config: {e}")

        logger.warning("[ContextSummarizer] Could not determine context window size")
        return None

    def _get_actual_or_estimated_tokens(self, context: HookContext, messages: List[Dict]) -> int:
        """Get actual token count from last LLM response or estimate from messages.

        Uses the MAXIMUM of:
        1. Actual prompt_tokens from last LLM response (via context_usage_tracker)
        2. Estimated tokens from current messages

        This ensures we trigger summarization if either metric exceeds threshold,
        preventing context overflow.

        Args:
            context: Hook context with session_id
            messages: Current message list

        Returns:
            Maximum of actual or estimated token count
        """
        estimated_tokens = self._estimate_tokens(messages)
        actual_tokens = 0

        # Try to get actual tokens from context_usage_tracker's latest snapshot
        try:
            # Access the plugin registry via agent's system_config
            if context.agent and hasattr(context.agent, 'system_config'):
                system_config = context.agent.system_config
                if hasattr(system_config, 'mcp_registry') and system_config.mcp_registry:
                    registry = system_config.mcp_registry

                    # Get context_usage_tracker plugin
                    usage_tracker_plugin = registry.get_server('context_usage_tracker')
                    if usage_tracker_plugin and hasattr(usage_tracker_plugin, 'tracker'):
                        tracker = usage_tracker_plugin.tracker

                        # Get latest snapshot for this session
                        if tracker._latest_snapshot and tracker._latest_snapshot.session_id == context.session_id:
                            actual_tokens = tracker._latest_snapshot.prompt_tokens
                            logger.debug(
                                f"[ContextSummarizer] Got actual tokens from usage_tracker: {actual_tokens} "
                                f"(estimated: {estimated_tokens})"
                            )
        except Exception as e:
            logger.debug(f"[ContextSummarizer] Could not get actual tokens from usage_tracker: {e}")

        # Return the MAXIMUM to ensure we trigger on either metric
        max_tokens = max(actual_tokens, estimated_tokens)

        if actual_tokens > 0 and estimated_tokens > 0:
            logger.info(
                f"[ContextSummarizer] Session {context.session_id}: Using max tokens - "
                f"actual={actual_tokens}, estimated={estimated_tokens}, using={max_tokens}"
            )

        return max_tokens

    def _estimate_tokens(self, messages: List[Dict]) -> int:
        """Estimate token count for messages using agent system's token estimation.

        Args:
            messages: List of message dictionaries

        Returns:
            Estimated token count
        """
        # Convert dict messages to ChatMessage for proper token estimation
        chat_messages = []
        for msg in messages:
            try:
                chat_messages.append(ChatMessage(**msg) if isinstance(msg, dict) else msg)
            except Exception as e:
                logger.warning(f"[ContextSummarizer] Could not convert message to ChatMessage: {e}")
                # Fallback: skip this message in estimation
                continue

        return estimate_token_count(chat_messages)

    def _categorize_messages(
        self,
        messages: List[Dict]
    ) -> tuple[List[Dict], List[Dict], List[Dict]]:
        """Categorize messages into system, recent, and old.

        Ensures tool_calls/tool response pairs stay together to prevent
        orphaned tool responses after summarization.

        Args:
            messages: List of all messages

        Returns:
            Tuple of (system_messages, recent_messages, old_messages)
        """
        system_msgs = []
        recent_msgs = []
        old_msgs = []

        # Phase 1: Initial categorization
        for i, msg in enumerate(messages):
            is_system = msg.get('role') == 'system'
            is_recent = i >= len(messages) - self.preserve_recent

            if is_system and self.preserve_system:
                system_msgs.append((i, msg))
            elif is_recent:
                recent_msgs.append((i, msg))
            else:
                old_msgs.append((i, msg))

        # Phase 2: Keep tool_calls/tool response pairs together
        # Build mapping: tool_call_id -> message index with tool_calls
        tool_call_map: Dict[str, int] = {}
        for i, msg in enumerate(messages):
            if msg.get('role') == 'assistant' and msg.get('tool_calls'):
                for tc in msg.get('tool_calls', []):
                    if isinstance(tc, dict) and 'id' in tc:
                        tool_call_map[tc['id']] = i

        # Find tool responses and ensure they're in the same category as their tool_calls
        indices_to_move_to_old: set[int] = set()
        indices_to_move_to_recent: set[int] = set()

        for i, msg in enumerate(messages):
            if msg.get('role') == 'tool':
                tool_call_id = msg.get('tool_call_id')
                if tool_call_id and tool_call_id in tool_call_map:
                    assistant_idx = tool_call_map[tool_call_id]

                    # Check where assistant and tool are categorized
                    assistant_in_old = any(idx == assistant_idx for idx, _ in old_msgs)
                    assistant_in_recent = any(idx == assistant_idx for idx, _ in recent_msgs)
                    tool_in_old = any(idx == i for idx, _ in old_msgs)
                    tool_in_recent = any(idx == i for idx, _ in recent_msgs)

                    # If assistant is old but tool is recent, move tool to old
                    if assistant_in_old and tool_in_recent:
                        indices_to_move_to_old.add(i)
                    # If assistant is recent but tool is old, move both to recent
                    elif assistant_in_recent and tool_in_old:
                        indices_to_move_to_recent.add(i)
                        indices_to_move_to_recent.add(assistant_idx)

        # Apply moves
        if indices_to_move_to_old or indices_to_move_to_recent:
            # Rebuild categories with moves applied
            recent_msgs_filtered = [(i, m) for i, m in recent_msgs if i not in indices_to_move_to_old]
            old_msgs_filtered = [(i, m) for i, m in old_msgs if i not in indices_to_move_to_recent]

            # Add moved messages
            for i in indices_to_move_to_old:
                msg = messages[i]
                old_msgs_filtered.append((i, msg))

            for i in indices_to_move_to_recent:
                msg = messages[i]
                if not any(idx == i for idx, _ in recent_msgs_filtered):
                    recent_msgs_filtered.append((i, msg))

            # Sort by original index to maintain order
            recent_msgs = sorted(recent_msgs_filtered, key=lambda x: x[0])
            old_msgs = sorted(old_msgs_filtered, key=lambda x: x[0])

        # Remove indices, return just messages
        return (
            [msg for _, msg in system_msgs],
            [msg for _, msg in recent_msgs],
            [msg for _, msg in old_msgs]
        )

    async def _summarize_messages(
        self,
        messages: List[dict],
        context: HookContext,
        scope: StatusScope
    ) -> tuple[List[dict], Dict[str, Any]]:
        """Summarize messages in chunks using LLM.

        Args:
            messages: Messages to summarize
            context: Hook context with LLM access
            scope: StatusScope for progress updates

        Returns:
            Tuple of (summarized_messages, statistics)
        """
        if not context.llm:
            logger.warning("[ContextSummarizer] No LLM available in context, skipping summarization")
            return messages, {'summary_count': 0, 'reason': 'no_llm'}

        summarized = []
        summary_count = 0
        total_chunks = (len(messages) + self.chunk_size - 1) // self.chunk_size

        # Get cancellation token from context (if available)
        cancellation_token = getattr(context, 'cancellation_token', None)

        # Process messages in chunks
        for chunk_idx in range(0, len(messages), self.chunk_size):
            chunk = messages[chunk_idx:chunk_idx + self.chunk_size]

            if len(chunk) < 2:
                # Too small to summarize, keep as-is
                summarized.extend(chunk)
                continue

            # Check for cancellation before each chunk
            if cancellation_token and cancellation_token.is_cancelled:
                logger.info(f"[ContextSummarizer] Cancellation requested, stopping at chunk {chunk_idx // self.chunk_size + 1}/{total_chunks}")
                # Send error message via scope before returning
                await scope.error(
                    f"cancelled at chunk {chunk_idx // self.chunk_size + 1}/{total_chunks}",
                    meta={'cancelled_at_chunk': chunk_idx // self.chunk_size + 1}
                )
                # Return what we have so far + remaining unsummarized messages
                summarized.extend(messages[chunk_idx:])
                stats = {
                    'summary_count': summary_count,
                    'total_chunks': total_chunks,
                    'successful_chunks': summary_count,
                    'failed_chunks': total_chunks - summary_count,
                    'cancelled': True,
                    'cancelled_at_chunk': chunk_idx // self.chunk_size + 1
                }
                return summarized, stats

            chunk_num = chunk_idx // self.chunk_size + 1

            # Send progress update via the StatusScope
            await scope.progress(
                f"Summarizing chunk {chunk_num}/{total_chunks} ({len(chunk)} messages)..."
            )

            # Small sleep to allow progress message to be sent
            await asyncio.sleep(0.01)

            try:
                # Format messages for prompt
                formatted_msgs = self._format_messages_for_summary(chunk)

                # Create summarization prompt
                prompt = self.prompt_template.replace('{messages}', formatted_msgs)

                # Call LLM for summarization using chat() method with cancellation support
                from agent_system.llm.models import ChatMessage
                summary_response = await context.llm.chat(
                    messages=[ChatMessage(role='user', content=prompt)],
                    cancellation_token=cancellation_token
                )

                summary_content = summary_response if isinstance(summary_response, str) else str(summary_response)

                # Create summary marker with message count
                # Note: Messages typically don't have timestamps, so we just show count
                marker = f"[Summary of {len(chunk)} older messages (chunk {chunk_num}/{total_chunks})]"

                # Create summary message
                summary_msg = {
                    'role': 'system',
                    'content': f"{marker}\n\n{summary_content}",
                    'metadata': {
                        'is_summary': True,
                        'summarized_count': len(chunk),
                        'chunk_index': chunk_idx // self.chunk_size,
                        'total_chunks': total_chunks
                    }
                }

                # Store original messages in metadata if configured
                if self.store_metadata:
                    summary_msg['metadata']['original_messages'] = chunk

                summarized.append(summary_msg)
                summary_count += 1

                logger.debug(
                    f"[ContextSummarizer] Summarized chunk {chunk_num}/{total_chunks}: "
                    f"{len(chunk)} messages → 1 summary"
                )

            except asyncio.CancelledError:
                logger.info(f"[ContextSummarizer] Summarization cancelled at chunk {chunk_num}/{total_chunks}")
                # Send error message via scope before returning
                await scope.error(
                    f"cancelled at chunk {chunk_num}/{total_chunks}",
                    meta={'cancelled_at_chunk': chunk_num}
                )
                # Return what we have + remaining unsummarized messages
                summarized.extend(messages[chunk_idx:])
                stats = {
                    'summary_count': summary_count,
                    'total_chunks': total_chunks,
                    'successful_chunks': summary_count,
                    'failed_chunks': total_chunks - summary_count,
                    'cancelled': True,
                    'cancelled_at_chunk': chunk_num
                }
                return summarized, stats
            except Exception as e:
                logger.error(f"[ContextSummarizer] Error summarizing chunk: {e}", exc_info=True)
                # Fallback: keep original messages
                summarized.extend(chunk)

        stats = {
            'summary_count': summary_count,
            'total_chunks': total_chunks,
            'successful_chunks': summary_count,
            'failed_chunks': total_chunks - summary_count
        }

        return summarized, stats

    def _format_messages_for_summary(self, messages: List[Dict]) -> str:
        """Format messages for inclusion in summarization prompt.

        Args:
            messages: Messages to format

        Returns:
            Formatted string representation of messages
        """
        formatted = []
        for i, msg in enumerate(messages, 1):
            role = msg.get('role', 'unknown')
            content = msg.get('content', '')

            # Handle multimodal content
            if isinstance(content, list):
                text_parts = []
                for item in content:
                    if isinstance(item, dict) and item.get('type') == 'text':
                        text_parts.append(item.get('text', ''))
                content = ' '.join(text_parts)

            timestamp = msg.get('timestamp', '')
            ts_str = f" ({timestamp})" if timestamp else ""

            formatted.append(f"{i}. {role.upper()}{ts_str}: {content}")

        return '\n\n'.join(formatted)

    def _serialize_message(self, msg: dict[str, Any] | ChatMessage) -> dict[str, Any]:
        """Serialize a message for JSON storage in history.

        Args:
            msg: Message as dict or ChatMessage

        Returns:
            Serialized message dict
        """
        if isinstance(msg, ChatMessage):
            return {
                'role': msg.role,
                'content': msg.content[:self.max_preview_length] if msg.content else '',
                'name': msg.name
            }
        else:
            content = msg.get('content', '')
            # Handle multimodal content
            if isinstance(content, list):
                text_parts = []
                for item in content:
                    if isinstance(item, dict) and item.get('type') == 'text':
                        text_parts.append(item.get('text', ''))
                content = ' '.join(text_parts)

            return {
                'role': msg.get('role', 'unknown'),
                'content': str(content)[:self.max_preview_length],
                'name': msg.get('name')
            }
