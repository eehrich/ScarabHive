"""Context Summarizer MCP Server - Manual context summarization tool with hook support.

Unified implementation combining MCP tools and hook functionality.
Allows LLMs to manually trigger context summarization and automatically
summarizes when context exceeds configured thresholds.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING, Dict, List

from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.hooks.plugin_hook import PluginHook, HookContext, HookResult
from agent_system.llm.token_utils import estimate_token_count, estimate_tools_token_count

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


class ContextSummarizerServer(SchemaBasedMCPServer, PluginHook):
    """Unified MCP server and hook for context summarization.

    Provides MCP tools:
    - summarize: Manually trigger summarization of current conversation
    - check_stats: Check current context statistics (token count, message count)

    Implements pre_llm_call hook for automatic summarization when context
    exceeds configured thresholds.
    """

    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig) -> None:
        """Initialize ContextSummarizerServer.

        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            mcp_config: Plugin-specific configuration
        """
        # Initialize MCP server
        SchemaBasedMCPServer.__init__(self, name, system_config, mcp_config)

        # Initialize hook
        hook_config = getattr(mcp_config, 'hook_config', {})
        PluginHook.__init__(self, name, config=hook_config)

        # Load configuration. ONE mapping, handed over whole — the hook merges
        # it over the schema defaults and owns every key from there.
        #
        # This used to be a second default table here plus a line-per-key copy
        # onto the hook, which ran AFTER the schema had already resolved the
        # right values and overwrote them. Two of those fallbacks were wrong:
        # llm_profile fell back to 'fast' (not a profile config/llm.yaml
        # defines) and summary_prompt_template to '' — and an empty template
        # makes `template.replace('{messages}', …)` an EMPTY prompt. Measured
        # on this exact path: the summarizer called the LLM with an empty user
        # message. Neither key is set in plugins.yaml, so the wrong fallback
        # was always the effective value.
        config_dict = dict(mcp_config.config) if getattr(mcp_config, 'config', None) else {}

        # Web UI history tracking
        self.summarization_history: List[Dict[str, Any]] = []

        # Import and instantiate the actual hook implementation
        from plugins.context_summarizer.hooks import ContextSummarizerPlugin

        plugin_dir = Path(__file__).parent
        self._hooks_impl = ContextSummarizerPlugin(
            plugin_dir,
            summarization_history=self.summarization_history
        )
        self._hooks_impl.apply_config(config_dict)

        # The MCP tool below reports the threshold; read it off the hook so
        # there is one source rather than a copy that can disagree.
        self.trigger_percentage = self._hooks_impl.trigger_percentage

        logger.info(
            f"ContextSummarizerServer initialized: trigger={self.trigger_percentage:.0%} of context window, "
            f"chunk_size={self._hooks_impl.chunk_size}, "
            f"preserve_recent={self._hooks_impl.preserve_recent}, "
            f"llm_profile={self._hooks_impl.llm_profile}, "
            f"max_messages={self._hooks_impl.max_messages or 'disabled'}, "
            f"min_time_between={self._hooks_impl.min_time_between}s"
        )

    # =========================================================================
    # MCP Tools Interface
    # =========================================================================

    async def list_tools(self) -> list:
        """List available MCP tools from schema.

        Returns tools defined in schema.yaml for this plugin.
        """
        return await super().list_tools()

    # =========================================================================
    # Hook Interface - delegate to hooks implementation
    # =========================================================================

    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """Hook for automatic context summarization before LLM calls.

        Delegates to ContextSummarizerPlugin for actual implementation.
        This allows the server to implement both MCP and Hook interfaces
        while keeping the complex hook logic in a separate file.

        Args:
            context: Hook context with messages and metadata

        Returns:
            HookResult with summarized messages or original context
        """
        return await self._hooks_impl.summarize_context(context)

    async def summarize(self, params: dict[str, Any]) -> dict[str, Any]:
        """Manually trigger context summarization.

        Tool name: {{ name }}_summarize → e.g., 'context_summarizer_summarize'

        Args:
            params: {
                "reason": Optional reason for summarization (for logging),
                "chunk_size": Optional override for chunk size,
                "preserve_recent": Optional override for recent message count
            }

        Returns:
            {
                "status": "success" | "error",
                "original_count": int,
                "summarized_count": int,
                "tokens_saved": int,
                "summary_preview": str
            }
        """
        status = params.get("_status")

        try:
            # Get session info from params (injected by ToolExecutionManager)
            session_id = params.get("_session_id")
            agent = params.get("_agent")

            if not session_id or not agent:
                error_msg = "Session context not available (session_id or agent missing)"
                if status:
                    await status.error(error_msg)
                return {"status": "error", "error": error_msg}

            # Get current messages from the agent's LIVE messages list, not persisted session
            # This ensures we include the current assistant message (with tool_calls) that
            # triggered this summarize() call. Without this, orphaned tool responses occur.
            # Session-correct live messages (keyed by session_id), falling back
            # to the persisted session tracker. Replaces the shared-singleton
            # agent._current_messages read which could return another session's
            # messages under concurrency.
            messages = None
            if hasattr(agent, 'get_live_messages'):
                live = agent.get_live_messages(session_id)
                if isinstance(live, list):
                    messages = live.copy()
            elif hasattr(agent, '_current_messages') and isinstance(agent._current_messages, list):
                messages = agent._current_messages.copy()

            # Fallback to session tracker if live messages not available
            if not messages:
                messages = agent._session_tracker.get_session_messages(session_id)
            
            # Note: We do NOT filter system messages here - the hook implementation
            # preserves them according to preserve_system_messages config
            if not messages:
                if status:
                    await status.end("No messages to summarize")
                return {
                    "status": "success",
                    "original_count": 0,
                    "summarized_count": 0,
                    "tokens_saved": 0,
                    "message": "No messages in conversation"
                }

            original_count = len(messages)
            reason = params.get("reason", "manual_trigger")

            if status:
                await status.progress(f"Summarizing {original_count} messages...")

            # Create hook context
            from agent_system.hooks import HookContext, HookType

            # Apply optional overrides
            old_chunk = None
            old_preserve = None

            if "chunk_size" in params:
                old_chunk = self._hooks_impl.chunk_size
                self._hooks_impl.chunk_size = int(params["chunk_size"])

            if "preserve_recent" in params:
                old_preserve = self._hooks_impl.preserve_recent
                self._hooks_impl.preserve_recent = int(params["preserve_recent"])

            hook_context = HookContext(
                hook_type=HookType.PRE_LLM_CALL,
                request_id=session_id,
                session_id=session_id,
                messages=messages,
                agent=agent,
                # The model answering the running step, not the configured one:
                # the trigger is a share of ITS window.
                llm=(agent.llm_for_session(session_id) if hasattr(agent, 'llm_for_session')
                     else getattr(agent, 'llm', None)),
                metadata={"manual_trigger": True, "reason": reason}
            )

            # Execute summarization via hook implementation
            result = await self._hooks_impl.summarize_context(hook_context)

            # Restore overrides
            if old_chunk is not None:
                self._hooks_impl.chunk_size = old_chunk
            if old_preserve is not None:
                self._hooks_impl.preserve_recent = old_preserve

            if not result.success:
                error_msg = f"Summarization failed: {result.metadata.get('error', 'unknown')}"
                if status:
                    await status.error(error_msg)
                return {"status": "error", "error": error_msg}

            # Store summarized messages for end-of-request persistence
            # We can't modify the request's local messages list directly, so we store
            # the summarized messages in the session tracker. The agent will use these
            # when persisting the session at end of request.
            if result.modified and result.context and result.context.messages:
                agent._session_tracker.set_compacted_messages(session_id, result.context.messages)

            summarized_count = len(result.context.messages) if result.context else original_count

            # Calculate token savings
            original_tokens = estimate_token_count(messages)
            new_tokens = estimate_token_count(result.context.messages) if result.context else original_tokens
            tokens_saved = original_tokens - new_tokens

            # Get summary preview (first summary message)
            summary_preview = ""
            if result.context and result.context.messages:
                for msg in result.context.messages:
                    msg_dict = msg.model_dump() if hasattr(msg, 'model_dump') else msg
                    if msg_dict.get("role") == "user" and "[Summary of" in str(msg_dict.get("content", "")):
                        summary_preview = str(msg_dict.get("content", ""))[:200]
                        break

            if status:
                await status.end(
                    f"Summarized {original_count} → {summarized_count} messages "
                    f"(saved ~{tokens_saved} tokens)"
                )

            return {
                "status": "success",
                "original_count": original_count,
                "summarized_count": summarized_count,
                "tokens_saved": tokens_saved,
                "summary_preview": summary_preview,
                "reason": reason,
                "modified": result.modified
            }

        except Exception as e:
            logger.exception(f"Error in manual summarization: {e}")
            if status:
                await status.error(f"Summarization error: {str(e)}")
            return {"status": "error", "error": str(e)}

    async def check_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """Check current conversation context statistics.

        Tool name: {{ name }}_check_stats → e.g., 'context_summarizer_check_stats'

        Returns:
            {
                "status": "success",
                "message_count": int,
                "total_tokens": int,
                "context_window": int,
                "utilization_percentage": float,
                "recommendation": "summarize" | "ok"
            }
        """
        status = params.get("_status")

        try:
            # Get session info
            session_id = params.get("_session_id")
            agent = params.get("_agent")

            if not session_id or not agent:
                error_msg = "Session context not available"
                if status:
                    await status.error(error_msg)
                return {"status": "error", "error": error_msg}

            # Get current messages from the agent's LIVE messages list
            # This ensures we include the current assistant message (with tool_calls)
            # Session-correct live messages (keyed by session_id), falling back
            # to the persisted session tracker. Replaces the shared-singleton
            # agent._current_messages read which could return another session's
            # messages under concurrency.
            messages = None
            if hasattr(agent, 'get_live_messages'):
                live = agent.get_live_messages(session_id)
                if isinstance(live, list):
                    messages = live.copy()
            elif hasattr(agent, '_current_messages') and isinstance(agent._current_messages, list):
                messages = agent._current_messages.copy()

            # Fallback to session tracker if live messages not available
            if not messages:
                messages = agent._session_tracker.get_session_messages(session_id)
            
            # Note: We include system messages in stats for accurate context window calculation
            message_count = len(messages) if messages else 0

            if message_count == 0:
                if status:
                    await status.end("No messages in conversation")
                return {
                    "status": "success",
                    "message_count": 0,
                    "total_tokens": 0,
                    "context_window": 0,
                    "utilization_percentage": 0.0,
                    "recommendation": "ok"
                }

            # Calculate tokens - try to get actual tokens from context_usage_tracker first
            estimated_tokens = estimate_token_count(messages)

            # Include tool definition tokens in estimation (session-correct).
            tool_definition_tokens = 0
            tools_schema = None
            if hasattr(agent, 'get_live_tools_schema'):
                tools_schema = agent.get_live_tools_schema(session_id)
            if tools_schema is None:
                tools_schema = getattr(agent, '_current_tools_schema', None)
            if isinstance(tools_schema, list) and tools_schema:
                tool_definition_tokens = estimate_tools_token_count(tools_schema)
                estimated_tokens += tool_definition_tokens
                logger.debug(
                    f"[check_stats] Added {tool_definition_tokens} tool definition tokens "
                    f"({len(tools_schema)} tools)"
                )

            actual_tokens = 0

            # Try to get actual tokens from context_usage_tracker (more accurate)
            try:
                if hasattr(agent, 'system_config') and hasattr(agent.system_config, 'mcp_registry'):
                    registry = agent.system_config.mcp_registry
                    usage_tracker = registry.get_server('context_usage_tracker')
                    if usage_tracker and hasattr(usage_tracker, 'tracker'):
                        # get_latest(session_id=...) instead of the tracker's
                        # private _latest_snapshot: that attribute was the
                        # process's own last call and is gone since the tracker
                        # moved to a shared store. Reaching into it kept
                        # "working" — the AttributeError landed in the except
                        # below and this silently fell back to the estimate.
                        latest = usage_tracker.tracker.get_latest(session_id=session_id)
                        if latest:
                            actual_tokens = latest.get('prompt_tokens', 0) or 0
                            logger.debug(
                                f"[check_stats] Got actual tokens from usage_tracker: {actual_tokens} "
                                f"(estimated: {estimated_tokens})"
                            )
            except Exception as e:
                logger.debug(f"[check_stats] Could not get actual tokens from usage_tracker: {e}")

            # Use the MAXIMUM of actual vs estimated (ensures we don't underestimate)
            total_tokens = max(actual_tokens, estimated_tokens)

            # Get context window
            context_window = 0
            answering = (agent.llm_for_session(session_id) if hasattr(agent, 'llm_for_session')
                         else getattr(agent, 'llm', None))
            if answering:
                context_window = getattr(answering, 'context_window', 0)

            # Calculate utilization
            utilization = (total_tokens / context_window * 100) if context_window > 0 else 0

            # Recommendation based on trigger threshold
            trigger_threshold = self.trigger_percentage * 100
            recommendation = "summarize" if utilization >= trigger_threshold else "ok"

            if status:
                status_msg = f"Context: {message_count} messages, ~{total_tokens} tokens "
                if tool_definition_tokens > 0:
                    status_msg += f"(incl. {tool_definition_tokens} tool def tokens) "
                if actual_tokens > 0:
                    status_msg += f"(actual: {actual_tokens}, estimated: {estimated_tokens}) "
                status_msg += f"({utilization:.1f}% of {context_window})"
                await status.end(status_msg)

            return {
                "status": "success",
                "message_count": message_count,
                "total_tokens": total_tokens,
                "estimated_tokens": estimated_tokens,
                "actual_tokens": actual_tokens,
                "tool_definition_tokens": tool_definition_tokens,
                "context_window": context_window,
                "utilization_percentage": round(utilization, 1),
                "trigger_threshold": round(trigger_threshold, 1),
                "recommendation": recommendation
            }

        except Exception as e:
            logger.exception(f"Error checking context stats: {e}")
            if status:
                await status.error(f"Stats error: {str(e)}")
            return {"status": "error", "error": str(e)}
