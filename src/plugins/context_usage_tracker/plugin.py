"""
Context Usage Tracker Plugin

Tracks LLM context usage and token consumption by implementing a post_llm_call hook.
Provides web UI for viewing usage statistics and history.
"""
import logging
from pathlib import Path
from typing import Any, Dict

from agent_system.hooks import SchemaBasedPluginHook, HookContext, HookResult
from agent_system.llm.factory import resolve_llm_config_for_agent
from agent_system.llm.pricing import normalize_usage, resolve_call_cost
from agent_system.llm.token_utils import estimate_tools_token_count
from agent_system.plugins.web_base import SchemaBasedPluginWebInterface
from .tracker import UsageTracker
from .web_endpoints import ContextUsageWebFactory


logger = logging.getLogger(__name__)


class ContextUsageTrackerHooks(SchemaBasedPluginHook):
    """Hook implementation for context usage tracking."""

    def __init__(self, plugin_dir: Path, tracker: UsageTracker):
        """
        Initialize the hooks plugin.

        Args:
            plugin_dir: Plugin directory
            tracker: Shared tracker instance
        """
        super().__init__(plugin_dir)
        self.tracker = tracker
        logger.info("ContextUsageTrackerHooks initialized")

    async def track_usage(self, context: HookContext) -> HookResult:
        """
        Track LLM usage after each call (post_llm_call hook).

        Args:
            context: Hook context with llm_response

        Returns:
            HookResult indicating success
        """
        try:
            llm_response = context.llm_response
            if not llm_response:
                logger.warning("No llm_response in context, skipping usage tracking")
                return HookResult(success=True, modified=False, context=context)

            # llm_response contains full response {"assistant": {...}, "usage": {...}}
            usage = llm_response.get("usage")

            if not usage:
                logger.warning(f"No usage data in llm_response (keys: {list(llm_response.keys())}), skipping tracking")
                return HookResult(success=True, modified=False, context=context)

            # One canonical shape for every provider dialect (OpenAI names,
            # Anthropic cache_read/cache_creation, Gemini camelCase, ...).
            # ALL four counts come from here: reading prompt/completion raw
            # while normalising the cache fields is what makes a rate divide
            # one dialect by another — for an Anthropic-shaped usage dict
            # 'prompt_tokens' is simply absent, so the denominator would be 0
            # while the numerator is not.
            call = normalize_usage(usage)
            prompt_tokens = call.prompt_tokens
            completion_tokens = call.completion_tokens
            cached_tokens = call.cached_tokens
            cache_write_tokens = call.cache_write_tokens
            total_tokens = usage.get("total_tokens") or (prompt_tokens + completion_tokens)

            # Model that actually served the call (respects llm_override)
            model = ""
            latency_ms = None
            if context.llm is not None:
                model = getattr(context.llm, 'model', None) or getattr(context.llm, 'model_name', '') or ""
                if not isinstance(model, str):  # mocks / exotic clients — keep it JSON-safe
                    model = ""
                # Wall-clock latency the client stashed for the served response
                # (LLMClient._notify_post_response). isinstance guard: mocks may
                # return a Mock for any attribute.
                _lat = getattr(context.llm, '_last_response_duration_ms', None)
                if isinstance(_lat, (int, float)):
                    latency_ms = float(_lat)

            # Billed figure first, central estimate second — the shared rule
            # lives in llm/pricing.resolve_call_cost so every consumer answers
            # the same for the same call. Batch clients
            # (BatchLLMClient.batch_provider = "openai"/...) bill at the
            # table's batch_discount; without it the estimate would be ~2x.
            # isinstance-str guard: plain mocks must not look batchy.
            bp = getattr(context.llm, 'batch_provider', None)
            is_batch = isinstance(bp, str) and bool(bp)
            cost, cost_is_estimate = resolve_call_cost(
                usage, model or None, is_batch=is_batch)

            # Get context_window from the actual LLM instance (respects llm_override)
            # instead of resolving from agent_config (which uses agent's default profile)
            context_window = 0
            if context.llm and hasattr(context.llm, 'context_window'):
                # Use the actual LLM's context_window (handles llm_override correctly)
                context_window = context.llm.context_window
                logger.debug(f"Using context_window from actual LLM: {context_window}")
            elif context.agent:
                # Fallback: resolve from agent_config (legacy behavior)
                try:
                    llm_config = resolve_llm_config_for_agent(
                        context.agent.system_config,
                        context.agent.agent_config
                    )
                    context_window = llm_config.get('context_window', 0)
                    logger.debug(f"Using context_window from agent_config: {context_window}")
                except Exception as e:
                    logger.debug(f"Could not resolve context_window: {e}")

            message_count = len(context.messages) if context.messages else 0

            agent_id = getattr(context.agent, 'agent_id', context.agent_name if context.agent else "unknown")
            agent_name = context.agent_name or "unknown"
            session_id = context.session_id or "unknown"

            # Estimate tool definition tokens. Prefer the per-request
            # context.tools_schema (session-correct) over the agent's shared attr.
            tool_definition_tokens = 0
            tools_schema = getattr(context, 'tools_schema', None)
            if tools_schema is None and context.agent:
                tools_schema = getattr(context.agent, '_current_tools_schema', None)
            if tools_schema and isinstance(tools_schema, list):
                tool_definition_tokens = estimate_tools_token_count(tools_schema)
                logger.debug(
                    f"Estimated tool definition tokens: {tool_definition_tokens} "
                    f"({len(tools_schema)} tools)"
                )

            # Record usage in tracker
            self.tracker.record_usage(
                agent_id=agent_id,
                agent_name=agent_name,
                session_id=session_id,
                total_tokens=total_tokens,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                message_count=message_count,
                context_window=context_window,
                cached_tokens=cached_tokens,
                tool_definition_tokens=tool_definition_tokens,
                cache_write_tokens=cache_write_tokens,
                cost=cost,
                cost_is_estimate=cost_is_estimate,
                model=model,
                request_id=getattr(context, 'request_id', None) or "",
                latency_ms=latency_ms,
            )

            return HookResult(success=True, modified=False, context=context)

        except Exception as e:
            logger.error(f"Error tracking LLM usage: {e}", exc_info=True)
            # Don't fail the request if tracking fails
            return HookResult(success=True, modified=False, context=context)


class ContextUsageTrackerPlugin(SchemaBasedPluginWebInterface):
    """Hybrid plugin combining hooks and web UI for context usage tracking."""

    def __init__(self, name: str, system_config: Dict[str, Any], mcp_config: Dict[str, Any]):
        """
        Initialize the context usage tracker plugin.

        Args:
            name: Plugin name
            system_config: System configuration
            mcp_config: MCP configuration
        """
        # Initialize base class (loads schema automatically)
        super().__init__(name, system_config, mcp_config)

        plugin_dir = Path(__file__).parent

        # Initialize shared tracker with plugin name for dynamic routing
        self.tracker = UsageTracker(max_history=1000, name=name)

        # Initialize hooks (schema-based)
        self.hooks_plugin = ContextUsageTrackerHooks(plugin_dir, self.tracker)

        # Initialize web factory
        self.web_factory = ContextUsageWebFactory(server=self)

        logger.info(f"Context Usage Tracker Plugin initialized: {name}")

    def get_hooks(self):
        """Get hooks from the hooks plugin."""
        return self.hooks_plugin.get_hooks()

    async def execute_hook(self, hook_type, context):
        """Execute hook via the hooks plugin."""
        return await self.hooks_plugin.execute_hook(hook_type, context)

    def get_web_router(self):
        """Get the web router for this plugin."""
        return self.web_factory.get_web_router()


# Plugin factory
PLUGIN_FACTORY = ContextUsageTrackerPlugin

