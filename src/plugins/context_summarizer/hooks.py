"""Context Summarizer Plugin - Intelligent context reduction using LLM.

This plugin uses LLM to intelligently summarize older conversation messages,
reducing context size while preserving key information and decisions.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from agent_system.hooks import SchemaBasedPluginHook, HookContext, HookResult
from agent_system.llm.token_utils import estimate_token_count
from agent_system.llm.models import ChatMessage
from agent_system.mcp.status import publish_status, StatusPhase

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
                logger.warning("[ContextSummarizer] No LLM context window available, skipping summarization")
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={'reason': 'no_context_window'}
                )

            # Calculate trigger threshold from percentage
            trigger_tokens = int(context_window * self.trigger_percentage)

            # Estimate token count (rough: 1 token ≈ 4 chars)
            total_tokens = self._estimate_tokens(messages_as_dicts)

            if total_tokens < trigger_tokens:
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
                f"[ContextSummarizer] Context exceeds threshold: {total_tokens} > {trigger_tokens} tokens "
                f"({self.trigger_percentage:.0%} of {context_window}). Starting summarization for session {context.session_id}"
            )

            # Publish START status message
            await publish_status(
                server="context_summarizer",
                message=f"Starting context summarization: {total_tokens} tokens → target reduction {self.min_reduction:.0%}",
                request_id=context.request_id,
                phase=StatusPhase.START,
                level="info"
            )

            # Separate messages into categories
            system_msgs, recent_msgs, old_msgs = self._categorize_messages(messages_as_dicts)

            if len(old_msgs) < 2:
                # Not enough old messages to summarize
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={
                        'reason': 'insufficient_old_messages',
                        'old_message_count': len(old_msgs)
                    }
                )

            # Publish progress status
            await publish_status(
                server="context_summarizer",
                message=f"Summarizing {len(old_msgs)} older messages using LLM (preserving {len(recent_msgs)} recent messages)",
                request_id=context.request_id,
                phase=StatusPhase.PROGRESS,
                level="info"
            )

            # Summarize old messages in chunks
            summarized_msgs, summary_stats = await self._summarize_messages(
                old_msgs,
                context
            )

            # Reconstruct message list: system + summarized + recent
            new_messages_dicts = system_msgs + summarized_msgs + recent_msgs

            # Calculate reduction
            original_tokens = self._estimate_tokens(messages_as_dicts)
            new_tokens = self._estimate_tokens(new_messages_dicts)
            reduction_ratio = 1 - (new_tokens / max(original_tokens, 1))

            # Check if reduction meets minimum threshold
            if reduction_ratio < self.min_reduction:
                logger.warning(
                    f"[ContextSummarizer] Summarization reduction ({reduction_ratio:.2%}) "
                    f"below minimum ({self.min_reduction:.2%}). Keeping original messages."
                )
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

            # Publish END status message
            await publish_status(
                server="context_summarizer",
                message=f"Summarization complete: {len(messages)} → {len(new_messages)} messages, {original_tokens - new_tokens} tokens saved ({reduction_ratio:.1%} reduction)",
                request_id=context.request_id,
                phase=StatusPhase.END,
                level="info",
                meta={
                    'original_messages': len(messages),
                    'new_messages': len(new_messages),
                    'tokens_saved': original_tokens - new_tokens,
                    'reduction_ratio': reduction_ratio
                }
            )

            # Track summarization event for web UI
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
                    'before_messages': [self._serialize_message(m) for m in messages[-10:]],  # Last 10 for preview
                    'after_messages': [self._serialize_message(m) for m in new_messages[-10:]],
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

        Args:
            context: Hook context containing agent with config

        Returns:
            Context window size in tokens, or None if not available
        """
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

        Args:
            messages: List of all messages

        Returns:
            Tuple of (system_messages, recent_messages, old_messages)
        """
        system_msgs = []
        recent_msgs = []
        old_msgs = []

        for i, msg in enumerate(messages):
            is_system = msg.get('role') == 'system'
            is_recent = i >= len(messages) - self.preserve_recent

            if is_system and self.preserve_system:
                system_msgs.append(msg)
            elif is_recent:
                recent_msgs.append(msg)
            else:
                old_msgs.append(msg)

        return system_msgs, recent_msgs, old_msgs

    async def _summarize_messages(
        self,
        messages: List[Dict],
        context: HookContext
    ) -> tuple[List[Dict], Dict[str, Any]]:
        """Summarize a list of messages using LLM.

        Args:
            messages: Messages to summarize
            context: Original hook context for LLM access

        Returns:
            Tuple of (summarized_messages, statistics)
        """
        if not context.llm:
            logger.warning("[ContextSummarizer] No LLM available in context, skipping summarization")
            return messages, {'summary_count': 0, 'reason': 'no_llm'}

        summarized = []
        summary_count = 0
        total_chunks = (len(messages) + self.chunk_size - 1) // self.chunk_size

        # Process messages in chunks
        for chunk_idx in range(0, len(messages), self.chunk_size):
            chunk = messages[chunk_idx:chunk_idx + self.chunk_size]

            if len(chunk) < 2:
                # Too small to summarize, keep as-is
                summarized.extend(chunk)
                continue

            try:
                # Format messages for prompt
                formatted_msgs = self._format_messages_for_summary(chunk)

                # Create summarization prompt
                prompt = self.prompt_template.replace('{messages}', formatted_msgs)

                # Call LLM for summarization
                summary_response = await context.llm.generate(
                    messages=[{'role': 'user', 'content': prompt}],
                    profile=self.llm_profile
                )

                summary_content = summary_response.get('content', '') if isinstance(summary_response, dict) else str(summary_response)

                # Create summary marker
                start_time = chunk[0].get('timestamp', 'unknown')
                end_time = chunk[-1].get('timestamp', 'unknown')
                marker = self.marker_format.format(
                    count=len(chunk),
                    start_time=start_time,
                    end_time=end_time
                )

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
                    f"[ContextSummarizer] Summarized chunk {chunk_idx // self.chunk_size + 1}/{total_chunks}: "
                    f"{len(chunk)} messages → 1 summary"
                )

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
                'content': msg.content[:1000] if msg.content else '',  # Truncate for storage
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
                'content': str(content)[:1000],  # Truncate for storage
                'name': msg.get('name')
            }
