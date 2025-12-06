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

            total_tokens = usage.get("total_tokens", 0)
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            
            # Extract cached_tokens from OpenAI's prompt_tokens_details
            # Format: {"prompt_tokens_details": {"cached_tokens": 1920}}
            cached_tokens = 0
            prompt_tokens_details = usage.get("prompt_tokens_details")
            if prompt_tokens_details and isinstance(prompt_tokens_details, dict):
                cached_tokens = prompt_tokens_details.get("cached_tokens", 0)

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

