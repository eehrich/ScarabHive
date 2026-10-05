"""
Context Usage Tracker Plugin

Tracks LLM context usage and token consumption: a post_llm_call hook for what
an agent spends, and a post_llm_response hook for the clients that run without
one and report a token usage -- the decisions client, and a synthesis from a
provider that measures one (see the second hook).
Provides web UI for viewing usage statistics and history.
"""
import asyncio
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
            # the same for the same call. An answer from a batch
            # (BatchLLMClient.last_was_batch) bills at the table's
            # batch_discount; its sync fallback does not. `is True`: plain
            # mocks must not look batchy.
            is_batch = getattr(context.llm, 'last_was_batch', None) is True
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
                    resolved = resolve_llm_config_for_agent(
                        context.agent.system_config,
                        context.agent.agent_config
                    )
                    context_window = resolved.spec.context_window or 0
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

            # Off the event loop. The write is a SQLite transaction with an
            # fsync, and on a cross-process conflict it waits on busy_timeout —
            # blocking C that `asyncio.wait_for` cannot cancel, so the hook's
            # declared timeout would be inert exactly when it mattered. The old
            # file-based tracker was off-loop too (run_in_executor); this keeps
            # that property.
            await asyncio.to_thread(
                self.tracker.record_usage,
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


    async def track_non_agent_usage(self, context: HookContext) -> HookResult:
        """Record an LLM call that belongs to no agent (post_llm_response).

        The decisions client talks to a provider with no agent around it, so
        the agent-level hook above never sees it: its spend appeared in no live
        total, only in the message debugger -- whose rows a retention setting
        prunes and a config switch can turn off entirely. For chat there were
        always two ledgers; for that call there was one.

        The guard is the whole trick. ``post_llm_response`` fires for chat
        calls too (servers/agent/components/hook_integration.py wires it with
        the agent attached), so counting those here would book every single
        chat call TWICE. An agent-less context is exactly what the other hook
        cannot see, and nothing else.

        **A synthesis lands here too, and only when it was measured.** What
        decides is the usage, not the kind of call: Gemini answers a synthesis
        with the same ``usage_metadata`` its chat calls carry, so those tokens
        are real and get booked; OpenAI's ``/audio/speech`` returns audio and
        nothing else, is billed per character, and arrives with ``llm_usage``
        empty -- skipped two lines below, because a row that invents a number
        is worse than no row. Its spend stays where it was (the message
        debugger and writer_audio's own accounting).

        What such a row is NOT is a context: it is recorded with a window of
        0, and that zero is what keeps it off every context reading --
        ``get_statistics`` leaves it out of the series, ``get_latest`` never
        answers "how full is it now" with it. Output tokens from a synthesis
        are audio tokens, so they are spend on the cost cards and nothing on
        the "Context tokens" line.
        """
        try:
            if context.agent is not None:
                return HookResult(success=True, modified=False, context=context)
            # No usage, no row. That is also what keeps a retry or a failed call
            # out: the clients report those through the same hook WITHOUT a
            # usage, a failure that never billed must not appear as spend, and a
            # retry that LATER succeeds is reported again, and counted then. A
            # failure WITH a usage is an answer the client refused after it was
            # billed (llm_decisions) -- spend all the same, so it is booked.
            usage = context.llm_usage or {}
            if not usage:
                return HookResult(success=True, modified=False, context=context)

            call = normalize_usage(usage)
            model = context.llm_model or ""
            # Same rule as the agent path: the figure the provider billed wins
            # over any estimate, which is what makes a decisions call honest --
            # it has no per-token price and reports its cost itself.
            # A batch request (BatchLLMClient reports as "batch_<provider>")
            # bills at the table's batch_discount, as on the agent path.
            is_batch = (context.llm_provider or "").startswith("batch_")
            cost, cost_is_estimate = resolve_call_cost(usage, model or None, is_batch=is_batch)

            # Who spent it: the provider, because there is no agent. The panel
            # shows it next to the agents, with a context window of 0 -- a call
            # that carries no conversation cannot fill one, and a made-up
            # window would put it on a percentage scale it does not live on.
            who = context.agent_name or context.llm_provider or "llm"

            await asyncio.to_thread(
                self.tracker.record_usage,
                agent_id=who,
                agent_name=who,
                session_id=context.session_id or "",
                total_tokens=usage.get("total_tokens")
                or (call.prompt_tokens + call.completion_tokens),
                prompt_tokens=call.prompt_tokens,
                completion_tokens=call.completion_tokens,
                context_window=0,
                cached_tokens=call.cached_tokens,
                cache_write_tokens=call.cache_write_tokens,
                cost=cost,
                cost_is_estimate=cost_is_estimate,
                model=model,
                request_id=context.request_id or "",
                latency_ms=context.llm_duration_ms,
            )
            return HookResult(success=True, modified=False, context=context)

        except Exception as e:
            logger.error(f"Error tracking non-agent LLM usage: {e}", exc_info=True)
            # Never fail the call over bookkeeping, same as the hook above.
            return HookResult(success=True, modified=False, context=context)


class ContextUsageTrackerPlugin(SchemaBasedPluginWebInterface):
    """Hybrid plugin combining hooks and web UI for context usage tracking."""

    def __init__(self, name: str, system_config: Dict[str, Any], server_config: Dict[str, Any]):
        """
        Initialize the context usage tracker plugin.

        Args:
            name: Plugin name
            system_config: System configuration
            server_config: tool server configuration
        """
        # Initialize base class (loads schema automatically)
        super().__init__(name, system_config, server_config)

        plugin_dir = Path(__file__).parent

        # Initialize shared tracker with plugin name for dynamic routing.
        # The path is configurable so a test (or a second deployment on the
        # same machine) does not open the production store: constructing this
        # plugin now creates a database and migrates a legacy file, which is
        # not something an unrelated test should do to data/.
        # getattr, not dict access: server_config is a ToolServerConfig pydantic model in
        # production (extra="allow", so config keys arrive as attributes) and a
        # plain dict only in tests. A dict-only read left both knobs inert.
        def _cfg(key: str, default: Any) -> Any:
            if isinstance(server_config, dict):
                return server_config.get(key, default)
            return getattr(server_config, key, default)

        storage_path = _cfg("storage_path", None)
        self.tracker = UsageTracker(
            max_history=int(_cfg("max_history", 1000) or 1000),
            storage_path=Path(storage_path) if storage_path else None,
            name=name,
        )

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

    def get_static_assets(self) -> Path:
        """The panel's script and stylesheet, served under /plugins/<name>/static/."""
        return Path(__file__).parent / "static"


# Plugin factory
PLUGIN_FACTORY = ContextUsageTrackerPlugin

