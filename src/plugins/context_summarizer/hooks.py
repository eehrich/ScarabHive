"""Context Summarizer Plugin - Intelligent context reduction using LLM.

This plugin uses LLM to intelligently summarize older conversation messages,
reducing context size while preserving key information and decisions.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_system.hooks import SchemaBasedPluginHook, HookContext, HookResult
from agent_system.llm.token_utils import estimate_token_count
from agent_system.llm.message_roles import opens_a_turn
from agent_system.llm.models import ChatMessage
from agent_system.utils.reasoning_artifacts import invalidate_reasoning_artifacts
from agent_system.tools.status import status_bus, StatusScope

logger = logging.getLogger(__name__)


class ContextSummarizerPlugin(SchemaBasedPluginHook):
    """Schema-based plugin for intelligent context summarization.

    Uses LLM to create concise summaries of older messages when context
    exceeds configured token limits. Preserves recent messages and system
    messages while summarizing older conversation history.

    Configuration is loaded from schema.yaml.
    """

    def __init__(self, plugin_dir: Path | str, summarization_history: List[Dict[str, Any]] | None = None,
                 instance_name: str | None = None):
        """Initialize the context summarizer plugin.

        Args:
            plugin_dir: Directory containing schema.yaml
            summarization_history: Optional list to track summarization events for web UI
        """
        super().__init__(plugin_dir)

        # Web UI history tracking
        self.summarization_history = summarization_history

        # injected_by of the summary messages: written by this plugin instance, not by a person
        self.summary_mark = instance_name or self.name

        # Session tracking for rate limiting (with LRU eviction)
        self._last_summarization_time: Dict[str, float] = {}  # session_id -> timestamp

        self._event_ids = itertools.count(1)

        self.apply_config(None)

        # Store system_config for later LLM instantiation
        self._system_config = None
        self._summarizer_llm = None

        logger.info(
            f"ContextSummarizerPlugin initialized: trigger={self.trigger_percentage:.0%} of context window, "
            f"max_messages={self.max_messages or 'disabled'}, "
            f"chunk_size={self.chunk_size}, max_chunks={self.max_chunks}, preserve_recent={self.preserve_recent}, "
            f"llm_profile={self.llm_profile}, min_time_between={self.min_time_between}s"
        )

    def apply_config(self, values: Optional[Dict[str, Any]] = None) -> None:
        """Take the plugin's configuration as ONE mapping.

        ``values`` comes from config/plugins.yaml and wins over the defaults
        schema.yaml declares. One place reads a key, so there is no second copy
        that can drift.

        What this replaced: server.py kept its OWN default table and then
        copied every value onto this object. Two of its fallbacks were wrong
        and nothing said so.

        - ``llm_profile`` fell back to 'fast', a profile config/llm.yaml does
          not define at all.
        - ``summary_prompt_template`` fell back to '' — and since the prompt is
          built as ``template.replace('{messages}', …)``, an empty template
          yields an EMPTY prompt: the messages are never substituted in,
          because the replace runs on the template. Measured on the production
          path (server + the shipped plugins.yaml): len 0. The summarizer was
          calling the LLM with an empty user message and putting whatever came
          back in place of real conversation.

        Both went unnoticed because plugins.yaml sets neither key, so the
        fallback was always the effective value, and because the copy happened
        AFTER the schema had resolved the correct default — overwriting it.
        """
        config = {**(self.get_config() or {}), **(values or {})}

        self._max_tracked_sessions = int(config.get('max_tracked_sessions', 200))
        self.trigger_percentage = float(config.get('summarization_trigger_percentage', 0.60))
        self.chunk_size = int(config.get('summarization_chunk_size', 10))
        self.max_chunks = int(config.get('max_chunks', 10))  # Limit parallel LLM calls
        self.preserve_recent = int(config.get('preserve_recent_count', 10))
        self.preserve_system = bool(config.get('preserve_system_messages', True))
        self.llm_profile = str(config.get('llm_profile', 'turbo'))
        self.prompt_template = str(config.get('summary_prompt_template', ''))
        self.min_reduction = float(config.get('min_summary_reduction', 0.3))
        self.max_preview_length = int(config.get('max_message_preview_length', 5000))
        self.min_time_between = float(config.get('min_time_between_summarizations', 200.0))
        self.max_messages = int(config.get('max_messages', 0))  # 0 = disabled

        if not self.prompt_template.strip():
            # Never summarise with an empty instruction. Refusing loudly beats
            # sending an empty prompt and storing the answer as a summary.
            raise ValueError(
                "context_summarizer: summary_prompt_template is empty — the "
                "summarisation prompt would be empty and the result would "
                "replace real conversation. Check schema.yaml's default."
            )

    MAX_HISTORY = 1000

    def _record(self, event: Dict[str, Any]) -> None:
        """Add an event to the panel's history, newest last, bounded: skipped runs repeat on every call above the
        threshold."""
        event['id'] = next(self._event_ids)
        event['timestamp'] = datetime.now(timezone.utc).isoformat()
        self.summarization_history.append(event)
        del self.summarization_history[:-self.MAX_HISTORY]

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

            # Check rate limiting: prevent endless loop by enforcing minimum time between summarizations
            session_id = context.session_id or "unknown"
            current_time = time.monotonic()  # Use monotonic clock, not event loop time
            
            if not is_manual_trigger:
                last_summarization = self._last_summarization_time.get(session_id)
                if last_summarization:
                    time_since_last = current_time - last_summarization
                    if time_since_last < self.min_time_between:
                        logger.info(
                            f"[ContextSummarizer] Session {session_id}: Rate limited - "
                            f"only {time_since_last:.1f}s since last summarization "
                            f"(minimum: {self.min_time_between}s)"
                        )
                        return HookResult(
                            success=True,
                            modified=False,
                            context=context,
                            metadata={
                                'reason': 'rate_limited',
                                'time_since_last': time_since_last,
                                'min_time_between': self.min_time_between
                            }
                        )

            # Check per-agent max_messages override from hook_config
            agent_max_messages = self.max_messages
            if context.hook_config and 'max_messages' in context.hook_config:
                agent_max_messages = int(context.hook_config['max_messages'])

            # Determine if message count trigger fires
            message_count = len(messages_as_dicts)
            message_count_exceeded = (
                agent_max_messages > 0 and message_count > agent_max_messages
            )

            if not is_manual_trigger and total_tokens < trigger_tokens and not message_count_exceeded:
                logger.info(
                    f"[ContextSummarizer] Session {context.session_id}: Below threshold - "
                    f"total_tokens={total_tokens}, trigger_tokens={trigger_tokens} "
                    f"({self.trigger_percentage:.0%} of context_window={context_window})"
                    f"{f', messages={message_count}/{agent_max_messages}' if agent_max_messages > 0 else ''}"
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
                        'context_window': context_window,
                        'message_count': message_count,
                        'max_messages': agent_max_messages,
                    }
                )

            trigger_reason = 'manual' if is_manual_trigger else (
                'message_count' if message_count_exceeded and total_tokens < trigger_tokens else 'token_limit'
            )
            logger.info(
                f"[ContextSummarizer] Context exceeds threshold (reason={trigger_reason}): "
                f"{total_tokens} {'forced' if is_manual_trigger else '> ' + str(trigger_tokens)} tokens "
                f"({self.trigger_percentage:.0%} of {context_window})"
                f"{f', messages={message_count} > {agent_max_messages}' if message_count_exceeded else ''}"
                f". Starting summarization for session {context.session_id}"
            )

            # Clean up orphaned tool_calls BEFORE categorization
            # This prevents tool_calls without responses from causing issues during summarization
            messages_as_dicts = self._remove_orphaned_tool_calls(messages_as_dicts)

            # A manual run's overrides live in its own context, never on self: another session's hook running
            # meanwhile would otherwise summarize with them. Only a manual run's: an automatic run's metadata carries
            # what earlier hooks of the chain merged into it.
            metadata = context.metadata if is_manual_trigger else {}
            preserve_recent = int(metadata.get('preserve_recent', self.preserve_recent))
            chunk_size = int(metadata.get('chunk_size', self.chunk_size))

            # Generate unique request_id for summarizer status messages (like tool calls)
            # This must be done BEFORE creating StatusScope so all messages use the same unique ID
            summarizer_request_id = context.request_id
            if context.agent:
                summarizer_request_id = await context.agent.next_internal_tool_request_id(context.request_id)

            # Separate messages into categories first (needed for start message)
            system_msgs, recent_msgs, old_msgs = self._categorize_messages(messages_as_dicts, preserve_recent)

            # The heads of the running turn -- every message that opened a turn after the last final answer: the
            # task, and what a person typed into the run since -- stay in place when a long tool chain has pushed
            # them out of the recent messages. Summarized, the model lost its task, and with the summaries marked
            # as injected no message opened the turn any more: /undo, /retry and file_checkpoints found none.
            # Summaries come before, between and after them. An answer ends the turn only when no injected message
            # follows it: a hook's follow-up (agent_continuation's "Continue") keeps the turn going, and counted
            # from that answer the task in front of it was summarized -- also when a person wrote into the run
            # after the follow-up.
            answered = max((i for i, msg in enumerate(messages_as_dicts)
                            if msg.get('role') == 'assistant' and not msg.get('tool_calls')
                            and not (i + 1 < len(messages_as_dicts)
                                     and messages_as_dicts[i + 1].get('injected_by'))), default=-1)
            running = {id(msg) for msg in messages_as_dicts[answered + 1:] if opens_a_turn(msg)}
            heads: list[tuple[int, dict]] = []  # (position among the messages summarized, the head)
            rest = []
            for msg in old_msgs:
                if id(msg) in running:
                    heads.append((len(rest), msg))
                else:
                    rest.append(msg)
            old_msgs = rest

            if len(old_msgs) < 2:
                # Not enough old messages to summarize
                logger.info(
                    f"[ContextSummarizer] Session {context.session_id}: Insufficient old messages - "
                    f"old_msgs={len(old_msgs)}, recent_msgs={len(recent_msgs)}, system_msgs={len(system_msgs)}"
                )
                
                # Record in history even when not applied
                if self.summarization_history is not None:
                    event = {
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
                    self._record(event)
                
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={
                        'reason': 'insufficient_old_messages',
                        'old_message_count': len(old_msgs)
                    }
                )

            # Pre-check: estimate if summarization can even achieve min_reduction
            # Best case = old messages shrink to zero → max_possible_reduction = old_tokens / total_tokens
            # If that's already below min_reduction, no LLM call can help → skip early
            old_tokens = self._estimate_tokens(old_msgs)
            max_possible_reduction = old_tokens / max(total_tokens, 1)
            if max_possible_reduction < self.min_reduction:
                logger.info(
                    f"[ContextSummarizer] Session {context.session_id}: Skipping - even removing all "
                    f"{len(old_msgs)} old messages ({old_tokens} tokens) would only reduce by "
                    f"{max_possible_reduction:.1%}, below minimum {self.min_reduction:.0%}. "
                    f"Total: {total_tokens} tokens, recent: {len(recent_msgs)} msgs, system: {len(system_msgs)} msgs"
                )

                if self.summarization_history is not None:
                    event = {
                        'session_id': context.session_id,
                        'request_id': context.request_id,
                        'strategy': 'summarize',
                        'original_message_count': len(messages),
                        'summarized_message_count': len(messages),
                        'messages_summarized': 0,
                        'summary_count': 0,
                        'original_tokens': total_tokens,
                        'new_tokens': total_tokens,
                        'tokens_saved': 0,
                        'reduction_ratio': 0,
                        'status': 'skipped',
                        'reason': 'insufficient_potential_reduction',
                        'before_messages': [],
                        'after_messages': [],
                        'summary_stats': {
                            'summary_count': 0,
                            'old_tokens': old_tokens,
                            'max_possible_reduction': max_possible_reduction,
                        }
                    }
                    self._record(event)

                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={
                        'reason': 'insufficient_potential_reduction',
                        'old_tokens': old_tokens,
                        'total_tokens': total_tokens,
                        'max_possible_reduction': max_possible_reduction,
                        'min_reduction': self.min_reduction,
                        'old_message_count': len(old_msgs),
                    }
                )

            # The pause starts as soon as LLM calls are about to be spent: a
            # rejected or failed run would otherwise redo them on every call.
            # Stamped here, with no await since the check, so two calls of one
            # session cannot both pass; given back below if no call went out.
            previous_stamp = self._last_summarization_time.get(session_id)
            self._last_summarization_time[session_id] = current_time
            if len(self._last_summarization_time) > self._max_tracked_sessions:
                oldest = min(self._last_summarization_time, key=self._last_summarization_time.get)
                del self._last_summarization_time[oldest]

            # Use StatusScope to ensure START/END pairing even on errors
            result = None
            async with StatusScope(
                status_bus,
                "context_summarizer",
                summarizer_request_id,
                start_msg=f"Summarizing {len(old_msgs)} older messages using LLM (preserving {len(recent_msgs)} recent messages)",
            ) as scope:
                # Small sleep to allow START message to be delivered
                await asyncio.sleep(0.01)

                # Summarize old messages in chunks
                summarized_msgs, summary_stats = await self._summarize_messages(
                    old_msgs,
                    context,
                    scope,
                    chunk_size,
                    heads
                )
                # Only its own stamp: a manual run may have stamped this session meanwhile.
                if summary_stats['llm_calls'] == 0 and self._last_summarization_time.get(session_id) == current_time:
                    if previous_stamp is None:
                        self._last_summarization_time.pop(session_id, None)
                    else:
                        self._last_summarization_time[session_id] = previous_stamp

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
                        self._record(event)
                    
                    # Store result instead of returning directly
                    result = HookResult(
                        success=True,
                        modified=False,
                        context=context,
                        metadata={
                            'reason': 'insufficient_reduction',
                            'reduction_ratio': reduction_ratio,
                            'min_reduction': self.min_reduction
                        }
                    )
                    # A run that changed nothing must not end as 'completed'.
                    await scope.end(
                        f"Not applied: {reduction_ratio:.1%} reduction is "
                        f"below the {self.min_reduction:.1%} minimum"
                    )
                else:
                    # Convert dicts back to ChatMessage objects
                    new_messages = []
                    for msg_dict in new_messages_dicts:
                        if isinstance(msg_dict, dict):
                            new_messages.append(ChatMessage(**msg_dict))
                        else:
                            new_messages.append(msg_dict)

                    # THE INVARIANT (utils/reasoning_artifacts.py): summarization
                    # replaced a span of real messages with summaries — provider
                    # reasoning artifacts (OpenAI encrypted reasoning chains,
                    # Gemini thought signatures) over the removed span are now
                    # unverifiable and would 400 on a later turn. Invalidate them
                    # here so the chain resets deterministically.
                    invalidated = invalidate_reasoning_artifacts(new_messages)
                    if invalidated:
                        logger.info(
                            f"[ContextSummarizer] History mutated -> invalidated "
                            f"reasoning artifacts on {invalidated} message(s)"
                        )

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
                            'after_messages': [self._serialize_message(m) for m in summarized_msgs
                                               if all(m is not head for _, head in heads)],  # Summary messages created from old_msgs
                            'summary_stats': summary_stats
                        }
                        self._record(event)

                    # Session persistence is handled automatically by
                    # HookIntegrationManager when we return HookResult(modified=True)
                    # below (hook path), and by the summarize tool wrapper via its
                    # own set_compacted_messages() call (direct-tool path). No
                    # explicit persist here: it double-persisted the session (once
                    # explicitly, once automatically) — the tool path even wrote a
                    # different message set (non-system here vs full context there).

                    # Store result instead of returning directly
                    result = HookResult(
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
                    
                    # Invalidate usage tracker data for this session after successful summarization
                    # This prevents subsequent hooks from using stale token counts
                    self._invalidate_usage_tracker_session(context, session_id, "context_summarizer")

                    # End line survives alone in the WebUI -- carry the numbers.
                    await scope.end(
                        f"Summarized {len(old_msgs)} messages: {original_tokens} -> "
                        f"{new_tokens} tokens ({reduction_ratio:.1%} reduction)"
                    )
            
            # Return after StatusScope is properly closed
            return result

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

                resolved = resolve_llm_config_for_agent(
                    context.agent.system_config,
                    context.agent.agent_config
                )

                if resolved.spec.context_window:
                    return resolved.spec.context_window
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

        # Include tool definition tokens in estimation (they consume context window).
        # Prefer the per-request context.tools_schema (session-correct); fall back
        # to the agent's deprecated shared attr only if the context lacks it.
        tools_schema = getattr(context, 'tools_schema', None)
        if tools_schema is None and context.agent:
            tools_schema = getattr(context.agent, '_current_tools_schema', None)
        if tools_schema and isinstance(tools_schema, list):
            from agent_system.llm.token_utils import estimate_tools_token_count
            tool_tokens = estimate_tools_token_count(tools_schema)
            estimated_tokens += tool_tokens
            logger.debug(
                f"[ContextSummarizer] Added {tool_tokens} tool definition tokens "
                f"({len(tools_schema)} tools)"
            )

        actual_tokens = 0

        # Try to get actual tokens from context_usage_tracker's latest snapshot FOR THIS SESSION
        # This uses the previous LLM call's token count as baseline - if it was already high,
        # the next call will be at least as large (probably larger with new messages)
        try:
            # Access the plugin registry via agent's system_config
            if context.agent and hasattr(context.agent, 'system_config'):
                system_config = context.agent.system_config
                if hasattr(system_config, 'tool_registry') and system_config.tool_registry:
                    registry = system_config.tool_registry

                    # Get context_usage_tracker plugin
                    usage_tracker_plugin = registry.get_server('context_usage_tracker')
                    if usage_tracker_plugin and hasattr(usage_tracker_plugin, 'tracker'):
                        tracker = usage_tracker_plugin.tracker

                        # Get latest snapshot FOR THIS SESSION (not global _latest_snapshot!)
                        # This filters by session_id to avoid interference from sub-agents
                        latest = tracker.get_latest(session_id=context.session_id)
                        if latest:
                            # Check if data is stale (context was optimized since last LLM call)
                            # Stale data doesn't reflect current message list, so ignore it
                            if latest.get('is_stale'):
                                logger.debug(
                                    f"[ContextSummarizer] Ignoring stale usage_tracker data for session "
                                    f"{context.session_id} (context was already optimized)"
                                )
                            else:
                                actual_tokens = latest.get('prompt_tokens', 0)
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
    
    def _invalidate_usage_tracker_session(
        self, 
        context: HookContext, 
        session_id: str, 
        reason: str
    ) -> None:
        """Mark usage tracker data as stale after context optimization.
        
        This prevents subsequent hooks from using outdated token counts
        that don't reflect the optimized message list.
        
        Args:
            context: Hook context with agent reference
            session_id: Session to invalidate
            reason: Reason for invalidation (for logging)
        """
        try:
            if context.agent and hasattr(context.agent, 'system_config'):
                system_config = context.agent.system_config
                if hasattr(system_config, 'tool_registry') and system_config.tool_registry:
                    registry = system_config.tool_registry
                    usage_tracker_plugin = registry.get_server('context_usage_tracker')
                    if usage_tracker_plugin and hasattr(usage_tracker_plugin, 'tracker'):
                        usage_tracker_plugin.tracker.invalidate_session(session_id, reason)
        except Exception as e:
            logger.debug(f"[ContextSummarizer] Could not invalidate usage_tracker session: {e}")

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
        messages: List[Dict],
        preserve_recent: int | None = None
    ) -> tuple[List[Dict], List[Dict], List[Dict]]:
        """Categorize messages into system, recent, and old.

        Ensures tool_calls/tool response pairs stay together to prevent
        orphaned tool responses after summarization.

        Args:
            messages: List of all messages
            preserve_recent: How many of the last messages stay (default: the configured count)

        Returns:
            Tuple of (system_messages, recent_messages, old_messages)
        """
        if preserve_recent is None:
            preserve_recent = self.preserve_recent
        system_msgs = []
        recent_msgs = []
        old_msgs = []

        # Phase 1: Initial categorization
        for i, msg in enumerate(messages):
            is_system = msg.get('role') == 'system'
            is_recent = i >= len(messages) - preserve_recent

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

    def _remove_orphaned_tool_calls(self, messages: List[Dict]) -> List[Dict]:
        """Remove tool_calls that have no corresponding tool responses.
        
        This prevents orphaned tool_calls from causing validation issues
        after summarization (e.g., after cancellation or context_summarizer runs).
        
        Args:
            messages: List of messages
            
        Returns:
            Cleaned list with orphaned tool_calls removed
        """
        # Build set of tool_call_ids that have responses
        responded_tool_call_ids: set[str] = set()
        for msg in messages:
            if msg.get('role') == 'tool' and msg.get('tool_call_id'):
                responded_tool_call_ids.add(msg['tool_call_id'])
        
        # Clean assistant messages: remove tool_calls without responses. Not those of the LAST message: they are
        # still running -- a manual run is one of them -- and their results are appended after it; stripped, the
        # results would answer calls that are no longer in the history.
        cleaned_messages = []
        last = len(messages) - 1
        for index, msg in enumerate(messages):
            if index != last and msg.get('role') == 'assistant' and msg.get('tool_calls'):
                # Filter tool_calls to only those with responses
                original_tool_calls = msg.get('tool_calls', [])
                kept_tool_calls = []
                removed_count = 0
                
                for tc in original_tool_calls:
                    if isinstance(tc, dict) and tc.get('id'):
                        if tc['id'] in responded_tool_call_ids:
                            kept_tool_calls.append(tc)
                        else:
                            removed_count += 1
                            logger.debug(
                                f"[ContextSummarizer] Removing orphaned tool_call: {tc.get('id')} "
                                f"(function: {tc.get('function', {}).get('name', 'unknown')})"
                            )
                
                # Create cleaned message
                if removed_count > 0:
                    cleaned_msg = msg.copy()
                    if kept_tool_calls:
                        cleaned_msg['tool_calls'] = kept_tool_calls
                    else:
                        # No tool_calls left, remove the field entirely
                        cleaned_msg.pop('tool_calls', None)
                    cleaned_messages.append(cleaned_msg)
                    
                    if removed_count > 0:
                        logger.info(
                            f"[ContextSummarizer] Cleaned assistant message: "
                            f"removed {removed_count} orphaned tool_call(s), "
                            f"kept {len(kept_tool_calls)} with responses"
                        )
                else:
                    # No changes needed
                    cleaned_messages.append(msg)
            else:
                # Non-assistant or no tool_calls, keep as-is
                cleaned_messages.append(msg)
        
        return cleaned_messages

    def _create_smart_chunks(self, messages: List[dict], chunk_size: int) -> List[List[dict]]:
        """Create chunks that keep tool_calls and tool responses together.

        This ensures that if an assistant message with tool_calls is in a chunk,
        all its corresponding tool responses are also in the same chunk.

        Args:
            messages: Messages to chunk
            chunk_size: Target chunk size (may be exceeded to keep pairs together)

        Returns:
            List of message chunks
        """
        if not messages:
            return []

        # Build mapping: tool_call_id -> assistant message index
        tool_call_map: Dict[str, int] = {}
        for i, msg in enumerate(messages):
            if msg.get('role') == 'assistant' and msg.get('tool_calls'):
                for tc in msg.get('tool_calls', []):
                    if isinstance(tc, dict) and 'id' in tc:
                        tool_call_map[tc['id']] = i

        # Build groups: each group is a list of message indices that must stay together
        # Start with each message in its own group
        groups: List[set[int]] = [{i} for i in range(len(messages))]

        # Merge groups: if a tool response belongs to an assistant, merge their groups
        for i, msg in enumerate(messages):
            if msg.get('role') == 'tool':
                tool_call_id = msg.get('tool_call_id')
                if tool_call_id and tool_call_id in tool_call_map:
                    assistant_idx = tool_call_map[tool_call_id]
                    # Merge groups: add tool response index to assistant's group
                    groups[assistant_idx].add(i)
                    groups[i] = groups[assistant_idx]  # Point to same group object

        # Deduplicate groups (multiple indices may point to same set object)
        unique_groups = []
        seen = set()
        for group in groups:
            group_id = id(group)
            if group_id not in seen:
                seen.add(group_id)
                unique_groups.append(sorted(list(group)))

        # Now create chunks by greedily packing groups
        chunks: List[List[dict]] = []
        current_chunk: List[dict] = []
        current_size = 0

        for group_indices in unique_groups:
            group_msgs = [messages[i] for i in group_indices]
            group_size = len(group_msgs)

            # If adding this group exceeds chunk_size and current_chunk is not empty, start new chunk
            if current_size > 0 and current_size + group_size > chunk_size:
                chunks.append(current_chunk)
                current_chunk = []
                current_size = 0

            # Add group to current chunk
            current_chunk.extend(group_msgs)
            current_size += group_size

        # Add final chunk if not empty
        if current_chunk:
            chunks.append(current_chunk)

        return chunks

    def _get_summarizer_llm(self, context: HookContext):
        """Get or create LLM instance with configured llm_profile.
        
        Args:
            context: Hook context with agent and system_config
            
        Returns:
            LLM instance configured with self.llm_profile
        """
        # Cache LLM instance for reuse
        if self._summarizer_llm is not None:
            return self._summarizer_llm
            
        # Get system_config from context
        if not context.agent or not hasattr(context.agent, 'system_config'):
            logger.warning("[ContextSummarizer] No system_config available, falling back to context.llm")
            return context.llm
            
        system_config = context.agent.system_config
        
        # Create LLM instance with configured profile
        try:
            # create_llm_from_profile forwards EVERY resolved field. Listing the
            # factory arguments by hand (pre-registry make_llm) dropped thinking_level, max_tokens,
            # safety_settings, service_tier and provider_routing - harmless for
            # the profile configured today, silently wrong the moment this points
            # at an OpenRouter profile. It also gets batch wrapping right, which
            # the hand-rolled call never did.
            from agent_system.llm.factory import create_llm_from_profile

            self._summarizer_llm = create_llm_from_profile(system_config, self.llm_profile)

            logger.info(
                f"[ContextSummarizer] Created LLM instance with profile '{self.llm_profile}' "
                f"(model: {self._summarizer_llm.model_name if hasattr(self._summarizer_llm, 'model_name') else 'unknown'})"
            )
            
            return self._summarizer_llm
        except Exception as e:
            logger.error(f"[ContextSummarizer] Failed to create LLM with profile '{self.llm_profile}': {e}")
            logger.warning("[ContextSummarizer] Falling back to context.llm")
            return context.llm

    async def _summarize_messages(
        self,
        messages: List[dict],
        context: HookContext,
        scope: StatusScope,
        chunk_size: int | None = None,
        heads: list[tuple[int, dict]] | None = None
    ) -> tuple[List[dict], Dict[str, Any]]:
        """Summarize messages in chunks using LLM.
        
        Uses parallel processing to submit all chunks to the LLM simultaneously,
        which enables efficient batch API usage when configured.
        
        If chunk count exceeds max_chunks, automatically increases chunk_size
        to reduce the number of parallel LLM calls.

        Args:
            messages: Messages to summarize
            context: Hook context with LLM access
            scope: StatusScope for progress updates
            chunk_size: Messages per chunk (default: the configured size)
            heads: (position in messages, message) of each head of the running turn, in order: kept as it is
                between the chunks before and after it -- never inside a chunk

        Returns:
            Tuple of (summarized_messages, statistics)
        """
        heads = heads or []
        # the parts between the heads, each chunked on its own
        cuts = [0, *(at for at, _ in heads), len(messages)]
        parts = [messages[start:end] for start, end in zip(cuts, cuts[1:])]
        unchanged = list(parts[0])
        for (_, head), part in zip(heads, parts[1:]):
            unchanged += [head, *part]

        # Get LLM with configured profile (not agent's LLM!)
        summarizer_llm = self._get_summarizer_llm(context)
        
        if not summarizer_llm:
            logger.warning("[ContextSummarizer] No LLM available, skipping summarization")
            return unchanged, {'summary_count': 0, 'reason': 'no_llm', 'llm_calls': 0}

        # Calculate effective chunk_size to respect max_chunks limit
        effective_chunk_size = self.chunk_size if chunk_size is None else chunk_size
        if self.max_chunks > 0 and len(messages) > 0:
            # The smallest chunk size whose chunks, counted over every part, stay within max_chunks -- the parts
            # are chunked apart, so a size taken from the total alone gave one chunk more per head. Counted as
            # _create_smart_chunks cuts them: it keeps a call with its results whole, so more chunks than
            # len / size. At the longest part every part is one chunk. Searched by halving: the greedy packing
            # never needs more chunks at a larger size, and counting up one by one re-chunked the whole history
            # hundreds of times inside the hook (seconds for a few thousand messages).
            configured = effective_chunk_size
            low, high = configured, max(configured, max(len(part) for part in parts))
            while low < high:
                size = (low + high) // 2
                if sum(len(self._create_smart_chunks(part, size)) for part in parts) > self.max_chunks:
                    low = size + 1
                else:
                    high = size
            effective_chunk_size = low
            if effective_chunk_size > configured:
                logger.info(
                    f"[ContextSummarizer] Increasing chunk_size from {configured} to "
                    f"{effective_chunk_size} to respect max_chunks={self.max_chunks} "
                    f"(messages={len(messages)}, parts={len(parts)})"
                )

        # CRITICAL: Create chunks that keep tool_calls/tool response pairs together
        chunks = []
        placed = []  # (index of the first chunk after it, head)
        for index, part in enumerate(parts):
            if index:
                placed.append((len(chunks), heads[index - 1][1]))
            chunks += self._create_smart_chunks(part, effective_chunk_size)

        total_chunks = len(chunks)
        
        # Log if we had to limit chunks
        if total_chunks > self.max_chunks and self.max_chunks > 0:
            logger.warning(
                f"[ContextSummarizer] Chunk count ({total_chunks}) still exceeds max_chunks "
                f"({self.max_chunks}) due to tool_call grouping. Proceeding anyway."
            )

        # Get cancellation token from context (if available)
        cancellation_token = getattr(context, 'cancellation_token', None)

        # Check for cancellation before starting
        if cancellation_token and cancellation_token.is_cancelled:
            logger.info("[ContextSummarizer] Cancellation requested before starting")
            return unchanged, {'summary_count': 0, 'cancelled': True, 'cancelled_at_chunk': 0, 'llm_calls': 0}

        # Send progress update
        await scope.progress(
            f"Summarizing {total_chunks} chunks in parallel ({len(messages)} messages total)..."
        )
        await asyncio.sleep(0.01)

        # Process ALL chunks in parallel for batch API efficiency
        summarized, stats = await self._summarize_chunks_parallel(
            chunks, summarizer_llm, cancellation_token, scope, total_chunks, placed
        )

        return summarized, stats

    async def _summarize_single_chunk(
        self,
        chunk: List[dict],
        chunk_idx: int,
        total_chunks: int,
        summarizer_llm,
        cancellation_token
    ) -> dict:
        """Summarize a single chunk of messages.
        
        Args:
            chunk: Messages to summarize
            chunk_idx: Index of this chunk (0-based)
            total_chunks: Total number of chunks
            summarizer_llm: LLM instance to use
            cancellation_token: Optional cancellation token
            
        Returns:
            Dict with 'success', 'result' (summary message or original chunk), and 'error' if failed
        """
        chunk_num = chunk_idx + 1
        
        if len(chunk) < 2:
            # Too small to summarize, keep as-is
            return {
                'success': True,
                'result': chunk,
                'is_summary': False,
                'chunk_idx': chunk_idx,
                'llm_called': False
            }

        llm_called = False
        try:
            # Format messages for prompt
            formatted_msgs = self._format_messages_for_summary(chunk)

            # Create summarization prompt
            prompt = self.prompt_template.replace('{messages}', formatted_msgs)

            # Call LLM for summarization using chat() method with cancellation support
            from agent_system.llm.models import ChatMessage
            llm_called = True
            summary_response = await summarizer_llm.chat(
                messages=[ChatMessage(role='user', content=prompt, timestamp=datetime.now())],
                cancellation_token=cancellation_token
            )

            summary_content = summary_response if isinstance(summary_response, str) else str(summary_response)

            # Create summary marker with message count
            marker = f"[Summary of {len(chunk)} older messages (chunk {chunk_num}/{total_chunks})]"

            # Create summary message as 'user' role so it gets persisted in sessions. injected_by: nobody typed it --
            # unmarked, every summary counted as a turn a person took (/undo, /retry, turn ages, "the last thing the
            # user wrote").
            summary_msg = {
                'role': 'user',
                'name': '__context_summary__',  # Special marker for UI styling
                'injected_by': self.summary_mark,
                'content': f"{marker}\n\n{summary_content}",
                'metadata': {
                    'is_summary': True,
                    'summarized_count': len(chunk),
                    'chunk_index': chunk_idx,
                    'total_chunks': total_chunks
                }
            }

            logger.debug(
                f"[ContextSummarizer] Summarized chunk {chunk_num}/{total_chunks}: "
                f"{len(chunk)} messages → 1 summary"
            )

            return {
                'success': True,
                'result': [summary_msg],
                'is_summary': True,
                'chunk_idx': chunk_idx,
                'llm_called': True
            }

        except asyncio.CancelledError:
            logger.info(f"[ContextSummarizer] Chunk {chunk_num}/{total_chunks} cancelled")
            return {
                'success': False,
                'result': chunk,
                'is_summary': False,
                'chunk_idx': chunk_idx,
                'error': 'cancelled',
                'llm_called': llm_called
            }
        except Exception as e:
            logger.error(f"[ContextSummarizer] Error summarizing chunk {chunk_num}: {e}", exc_info=True)
            # Fallback: keep original messages
            return {
                'success': False,
                'result': chunk,
                'is_summary': False,
                'chunk_idx': chunk_idx,
                'error': str(e),
                'llm_called': llm_called
            }

    async def _summarize_chunks_parallel(
        self,
        chunks: List[List[dict]],
        summarizer_llm,
        cancellation_token,
        scope: StatusScope,
        total_chunks: int,
        heads: list[tuple[int, dict]] | None = None
    ) -> tuple[List[dict], Dict[str, Any]]:
        """Process all chunks in parallel for batch API efficiency.
        
        Args:
            chunks: List of message chunks to summarize
            summarizer_llm: LLM instance to use
            cancellation_token: Optional cancellation token
            scope: StatusScope for progress updates
            total_chunks: Total number of chunks
            heads: (index of the first chunk after it, message) of each head of the running turn, in order: put
                back in front of that chunk

        Returns:
            Tuple of (summarized_messages, statistics)
        """
        # Create tasks for all chunks
        tasks = [
            self._summarize_single_chunk(
                chunk, idx, total_chunks, summarizer_llm, cancellation_token
            )
            for idx, chunk in enumerate(chunks)
        ]

        logger.info(f"[ContextSummarizer] Starting parallel summarization of {len(tasks)} chunks")

        # Execute all tasks in parallel
        # Using return_exceptions=True to handle individual failures gracefully
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results maintaining chunk order
        summarized = []
        summary_count = 0
        failed_chunks = 0
        llm_calls = 0
        cancelled = False

        for idx, result in enumerate(results):
            # the turn's heads, between the chunks before and after them
            summarized += [head for at, head in heads or [] if at == idx]
            if isinstance(result, Exception):
                # Task raised an exception
                logger.error(f"[ContextSummarizer] Chunk {idx + 1} raised exception: {result}")
                summarized.extend(chunks[idx])  # Keep original
                failed_chunks += 1
                llm_calls += 1  # unknown whether the call went out; count it so the pause holds
            elif isinstance(result, dict):
                llm_calls += result['llm_called']
                if result.get('error') == 'cancelled':
                    cancelled = True
                
                # Append result messages (either summary or original)
                summarized.extend(result['result'])
                
                if result.get('is_summary'):
                    summary_count += 1
                elif not result.get('success'):
                    failed_chunks += 1
            else:
                # Unexpected result type
                logger.warning(f"[ContextSummarizer] Unexpected result type for chunk {idx + 1}: {type(result)}")
                summarized.extend(chunks[idx])
                failed_chunks += 1

        summarized += [head for at, head in heads or [] if at >= len(results)]

        # Send completion progress
        await scope.progress(
            f"Completed {summary_count}/{total_chunks} chunks successfully"
        )

        stats = {
            'summary_count': summary_count,
            'total_chunks': total_chunks,
            'successful_chunks': summary_count,
            'failed_chunks': failed_chunks,
            'llm_calls': llm_calls,
            'parallel': True,
            'cancelled': cancelled
        }

        logger.info(
            f"[ContextSummarizer] Parallel summarization complete: "
            f"{summary_count}/{total_chunks} successful, {failed_chunks} failed"
        )

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

            # A call's name and arguments are only in tool_calls: without them an assistant turn that only calls a
            # tool reached the summarizer as an empty line, and the results below it answered nothing it could see.
            for call in msg.get('tool_calls') or []:
                function = (call.get('function') if isinstance(call, dict) else None) or {}
                content = f"{content or ''}\n[calls {function.get('name', 'a tool')}({function.get('arguments', '')})]".strip()

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
