"""Where one tool call goes and how its answer becomes a tool message.

A call of the model runs either on an external MCP server (a dotted name,
``<server>.<tool>``) or on a plugin / config agent / the agent's own tools.
The two paths find their server differently and treat the arguments
differently (external servers get no runtime params), but report a call the
same way: a ``tool_call`` event, then the result entry, the ``tool_result``
event and the tool message (``_CallReport``). ``invoke_tool`` is the bare
call behind Agent.call_tool: no events, no message, a failure raises.

ToolExecutionManager decides when a call runs (hooks, cancellation, parallel
tasks); this module only runs it. A component, not a mixin: it needs nothing
of the manager but the registry and the agent it was built with.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from ....llm.models import ChatMessage
from ....llm.text_sanitizer import sanitize_for_llm, sanitize_json_content
from ....tools.integration import get_tool_integration
from ....utils.redact import redact_secrets
from .server_resolution import resolve_longest_prefix
from .tool_call_contract import (
    call_id_or,
    inject_runtime_params,
    make_params_serializable,
    pop_multimodal_content,
    tool_error_message,
)

if TYPE_CHECKING:
    from ..server import Agent
    from ....tools.base import ToolServerRegistry

logger = logging.getLogger(__name__)

#: What one call hands back: the tool message, its events, its result entries.
CallOutcome = Tuple[ChatMessage, List[Dict], List[Dict]]


async def call_server(server: Any, tool_name: str, params: Dict[str, Any]) -> Any:
    """Call ``tool_name`` on ``server``: through call_with_status (the status
    scope, which hands the tool its ``_status``) when it has one, else call()."""
    # Check if server has call_with_status (tool server interface)
    if hasattr(server, 'call_with_status'):
        return await server.call_with_status(tool_name, params)
    # Fallback to regular call method
    return await server.call(tool_name, params)


class _CallReport:
    """What one call of the model reports besides running: the ``tool_call``
    event when it starts, then the result entry, the ``tool_result`` event and
    the tool message -- or the error message in place of the result.

    Shared by the external and the plugin path; ``action`` is what they name
    the call in events and results (the external server's own tool name, or
    the name the model called).
    """

    def __init__(self, tc: Dict, tool_name: str, action: str, params: Dict[str, Any],
                 step: int, request_id: Optional[str]):
        self.tc = tc
        self.tool_name = tool_name
        self.action = action
        self.step = step
        # Create serializable params for events (exclude non-JSON-serializable objects like StatusScope)
        self.params = make_params_serializable(params)
        # Emit tool call event (include request_id for correlation)
        # Prefer tool-specific request_id from params over the general request_id
        self.request_id = self.params.get('request_id') or request_id
        call_event = {"type": "tool_call", "step": step + 1, "server": tool_name, "action": action, "params": self.params, "request_id": self.request_id}
        self.events: List[Dict] = [call_event]
        self.results: List[Dict] = []

    def answered(self, tool_result: Any, message_name: str, json_default: Any = None) -> CallOutcome:
        """The call returned ``tool_result``: record it and answer with it.

        ``_multimodal_content`` leaves the result for the message's own parts
        first (pop_multimodal_content). A result that does not encode as JSON
        raises here, after it was recorded -- the caller's error path answers.
        """
        multimodal_content = pop_multimodal_content(tool_result, self.tool_name)

        self.results.append({
            "server": self.tool_name,
            "action": self.action,
            "params": self.params,
            "result": tool_result
        })

        # Emit the result event (include request_id for correlation)
        result_event = {"type": "tool_result", "step": self.step + 1, "server": self.tool_name, "action": self.action, "result": tool_result, "request_id": self.request_id}
        self.events.append(result_event)

        # Create tool result message with optional multimodal content
        call_id = call_id_or(self.tc, f"{self.tool_name}-call")
        # Sanitize tool result content before adding to messages
        tool_msg_content = sanitize_json_content(json.dumps(tool_result, ensure_ascii=False, default=json_default))
        message = ChatMessage(
            role="tool",
            tool_call_id=call_id,
            name=message_name,
            content=tool_msg_content,
            timestamp=datetime.now(timezone.utc),
            multimodal_content=multimodal_content  # Attach multimodal content
        )
        return message, self.events, self.results

    def failed(self, call_id: str, openai_tool_name: str, error_content: str) -> CallOutcome:
        """The call failed: answer with ``error_content`` instead of a result."""
        return tool_error_message(call_id, openai_tool_name, error_content), self.events, self.results

    def closed(self, openai_tool_name: str, request_id: Optional[str]) -> CallOutcome:
        """The tool was closed with GeneratorExit (an async generator tool):
        treated as a cancellation."""
        logger.warning("Tool %s closed with GeneratorExit (request_id: %s)", self.tool_name, request_id)
        call_id = call_id_or(self.tc, f"{self.tool_name}-cancelled")
        error_content = json.dumps({"error": "Tool execution was cancelled (GeneratorExit)"})
        return self.failed(call_id, openai_tool_name, error_content)


class ToolInvoker:
    """Runs a single tool call on the server it belongs to."""

    def __init__(self, registry: ToolServerRegistry, agent: Optional[Agent] = None):
        self.registry = registry  # Legacy fallback (ToolExecutionManager.registry)
        self._agent = agent

    def _initialized_tool_integration(self) -> Any:
        """The agent's plugin tool integration once it is initialized, else None."""
        if self._agent and hasattr(self._agent, '_tool_integration_manager'):
            tool_integration = self._agent._tool_integration_manager.tool_integration
            if tool_integration is not None:
                if tool_integration.initialized:  # type: ignore[unreachable]
                    return tool_integration
        return None

    async def invoke_tool(self, tool_name: str, params: Dict[str, Any]):
        """Execute a tool call against the registry and return results.

        Modern interface: tool_name IS the function/method to call.
        No separate action_name needed - the tool name identifies the exact operation.
        """

        # First, try to get plugin adapter (important for status forwarding)
        plugin_adapter = None
        tool_integration = self._initialized_tool_integration()
        if tool_integration is not None:
            # First try exact match (legacy behavior)
            plugin_adapter = tool_integration.plugin_registry.get_server(tool_name)

            # If not found, resolve the server name embedded in the flat
            # tool name (servername_toolname) via the shared prefix walk.
            if not plugin_adapter:
                plugin_adapter, adapter_server_name = resolve_longest_prefix(
                    tool_integration.plugin_registry.get_server, tool_name)
                if plugin_adapter:
                    logger.debug(f"Found plugin adapter for {tool_name} via server name {adapter_server_name}")

        if plugin_adapter:
            # Use the PluginToolAdapter which handles tool routing and status forwarding correctly
            try:
                # Call through the PluginToolAdapter with the tool name directly
                result = await plugin_adapter.call_tool(tool_name, params)
                return result
            except Exception as e:
                logger.exception("Plugin tool %s invocation failed: %s", tool_name, e)
                raise

        # If no plugin adapter, try to get server directly (for config agents)
        server = None
        if self._agent and hasattr(self._agent, '_get_server_from_any_registry'):
            server = self._agent._get_server_from_any_registry(tool_name)

        # Final fallback to legacy registry (though it's usually empty)
        if not server:
            server = self.registry.get(tool_name) if tool_name in self.registry.list() else None

        if not server:
            raise RuntimeError(f"Unknown tool: {tool_name}")

        try:
            return await call_server(server, tool_name, params)
        except Exception as e:
            logger.exception("Tool %s invocation failed: %s", tool_name, e)
            raise

    async def execute_tool(self, tc: Dict, tool_name: str, openai_tool_name: str,
                           params: Dict[str, Any], step: int, request_id: str | None = None,
                           session_id: str | None = None, user_id: str | None = None) -> CallOutcome:
        """Run one call of the model: on its external server when the name is
        dotted (``<server>.<tool>``), else as a plugin tool."""
        if "." in tool_name:
            return await self.execute_external_tool(tc, tool_name, openai_tool_name, params, step, request_id, session_id, user_id)
        return await self.execute_plugin_tool(tc, tool_name, openai_tool_name, params, step, request_id, session_id, user_id)

    async def execute_external_tool(self, tc: Dict, tool_name: str, openai_tool_name: str,
                                    params: Dict[str, Any], step: int, request_id: str | None = None,
                                    session_id: str | None = None, user_id: str | None = None) -> CallOutcome:
        """Execute an external tool."""
        server_name, actual_tool_name = tool_name.split(".", 1)

        # NOTE: no session-context injection here. External servers are
        # foreign processes -- internal runtime keys (_session_id/_user_id)
        # must not leave the process. The old injection block was dead code
        # anyway: make_params_serializable strips every "_"-prefixed key
        # before the call, so the values never reached the server.

        report = _CallReport(tc, tool_name, actual_tool_name, params, step, request_id)

        try:
            logger.info("Invoking external tool %s on server %s with params %s", actual_tool_name, server_name,
                        redact_secrets(params))  # an add_machine password must not reach the log
            # Use the integration the agent already set up.
            #
            # This used to call get_tool_integration(config=agent_config) --
            # an AgentConfig where an AgentSystemConfig is expected. It only
            # ever worked because the lookup returns the existing global
            # instance before it looks at config at all; the moment it had to
            # build one, it died with AttributeError on external_servers.
            manager = getattr(self._agent, "_tool_integration_manager", None) if self._agent else None
            tool_integration = getattr(manager, "tool_integration", None) if manager else None
            if tool_integration is None:
                system_config = getattr(self._agent, "system_config", None) if self._agent else None
                if system_config is None:
                    raise RuntimeError("Cannot access tool integration without system config")
                tool_integration = get_tool_integration(config=system_config)
            # Use serializable_params (report.params) to avoid passing non-JSON-serializable objects (like CancellationToken) to external servers
            tool_result = await tool_integration.call_tool(server_name, actual_tool_name, report.params, "external")
            logger.info("External tool %s returned: %s", tool_name, str(tool_result)[:500])
            return report.answered(tool_result, sanitize_for_llm(openai_tool_name))
        except (Exception, GeneratorExit) as e:
            # Handle both normal exceptions and GeneratorExit (when async generator tools are closed)
            if isinstance(e, GeneratorExit):
                return report.closed(openai_tool_name, request_id)
            logger.exception("External tool %s invocation failed: %s", tool_name, e)
            return report.failed(call_id_or(tc, f"{tool_name}-error"), openai_tool_name,
                                 json.dumps({"error": f"Tool invocation failed: {str(e)}"}))

    def _plugin_server(self, tool_name: str) -> Any:
        """The server a plugin tool (or config agent tool) runs on, or None."""
        # CRITICAL FIX: Check if tool_name exists as a registered server FIRST
        # This prevents prefix-based false positives where "sysadmin_agent_manager"
        # incorrectly matches "sysadmin_agent_" prefix check
        server = None

        # Try to get server from registries first
        if self._agent and hasattr(self._agent, '_get_server_from_any_registry'):
            server = self._agent._get_server_from_any_registry(tool_name)

        # If not found in registry, check if it's an own tool using prefix check
        if not server and self._agent and tool_name.startswith(f"{self._agent.name}_"):
            # This is likely an own tool - use the agent itself as the server
            server = self._agent
            logger.debug(f"Tool '{tool_name}' is agent's own tool (prefix match), using self as server")

        # Legacy fallback paths (for systems not using _get_server_from_any_registry)
        if not server:
            # Get plugin server from tool integration plugin registry
            tool_integration = self._initialized_tool_integration()
            if tool_integration is not None:
                plugin_adapter = tool_integration.plugin_registry.get_server(tool_name)
                if plugin_adapter and hasattr(plugin_adapter, 'plugin_server'):
                    server = plugin_adapter.plugin_server

            if not server:
                # Fallback to agent's registry (for config agents and other servers)
                if self._agent and hasattr(self._agent, 'registry'):
                    agent_registry = self._agent.registry
                    if agent_registry and tool_name in agent_registry.list():
                        server = agent_registry.get(tool_name)

                # Final fallback to legacy registry (though it may be empty)
                if not server:
                    server = self.registry.get(tool_name) if tool_name in self.registry.list() else None

        return server

    async def execute_plugin_tool(self, tc: Dict, tool_name: str, openai_tool_name: str,
                                  params: Dict[str, Any], step: int, request_id: str | None = None,
                                  session_id: str | None = None, user_id: str | None = None) -> CallOutcome:
        """Execute a plugin tool (or config agent tool)."""
        server = self._plugin_server(tool_name)

        if not server:
            # Return error response instead of raising - allows agent to recover from hallucinated tool names
            logger.warning("Server not found for tool: %s (hallucinated tool call?)", tool_name)
            call_id = call_id_or(tc, "error-call")
            error_content = json.dumps({
                "error": f"Unknown tool: '{tool_name}'. The tool does not exist. Please check available tools and try again.",
                "type": "ToolNotFoundError"
            })
            message = tool_error_message(call_id, openai_tool_name, error_content)
            return message, [{"type": "tool_error", "tool": tool_name, "error": f"Unknown tool: {tool_name}"}], []

        report = _CallReport(tc, tool_name, openai_tool_name, params, step, request_id)

        try:
            logger.info("Invoking tool %s with params %s", openai_tool_name,
                        redact_secrets(params))  # an add_machine password must not reach the log

            # Inject session context (shared with Agent.dispatch_tool_call — see
            # inject_runtime_params; passed through the call chain to avoid races)
            params = inject_runtime_params(
                params, session_id=session_id, user_id=user_id,
                request_id=request_id, agent=self._agent)

            tool_result = await call_server(server, openai_tool_name, params)

            logger.info("Tool %s returned: %s", tool_name, str(tool_result)[:500])

            # default=str: a plugin may hand back a set or a date; the model gets
            # it as text instead of "invocation failed: not JSON serializable".
            return report.answered(tool_result, openai_tool_name, json_default=str)

        except (Exception, GeneratorExit) as e:
            if isinstance(e, GeneratorExit):
                return report.closed(openai_tool_name, request_id)
            logger.exception("Tool %s invocation failed: %s", tool_name, e)
            return report.failed(call_id_or(tc, "error-call"), openai_tool_name,
                                 json.dumps({"error": sanitize_for_llm(str(e))}))
