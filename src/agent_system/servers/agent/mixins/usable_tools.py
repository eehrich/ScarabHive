"""The tools this agent can use: which ones, their schemas, and calling one by name.

The INTERNAL tool interface of the Agent class docstring: discovery filtered by the agent's allowed
and blocked patterns (list_usable_tools), the LLM schemas a run sends (build_llm_tool_schemas,
_schemas_for, the deferred ones held back), the one server resolution, and the programmatic dispatch
(dispatch_tool_call) that gives in-process callers -- tool_script, slash commands -- the same
resolution and the same allow/block answer as the schema build. One module because the two halves
must not drift: what the LLM cannot see cannot be dispatched, and the other way round.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Dict, Optional

from ....tools.base import ToolServer
from ..components.server_resolution import resolve_registry_server, resolve_longest_prefix
from ..tool_discovery import ToolDiscoveryService
from ..tool_schema_builder import ToolSchemaBuilder, server_matches_patterns
from ..deferred_tools import DeferredTools

if TYPE_CHECKING:
    from ..server import Agent

logger = logging.getLogger(__name__)


class UsableToolsMixin:
    """What this agent CAN USE, and calling it (see the module docstring).

    Relies on Agent.__init__ for ``name``, ``agent_config``, ``registry``,
    ``_tool_integration_manager``, ``_hook_manager`` and ``_session_tracker``.
    """

    # ------------------------------------------------------------------
    # Tool filtering helpers
    # ------------------------------------------------------------------
    def _is_tool_allowed(self: Agent, tool_name: str, patterns: list[str]) -> bool:
        """Return True if the discovery-stage name matches any allowed pattern.

        Thin delegate to the SINGLE shared discovery-stage matcher
        ``tool_schema_builder.server_matches_patterns`` (see its docstring for
        pattern semantics). Kept as a method for existing callers/tests.
        NOTE: empty patterns now mean deny-all (security by default) — all
        production callers guard for non-empty patterns before calling.
        """
        return server_matches_patterns(tool_name, patterns)

    async def list_usable_tools(self: Agent) -> tuple[list[str], list[str] | None, list[str] | None]:
        """Return list of tool names this agent CAN USE (filtered by agent config).

        This is the INTERNAL interface - tools available for this agent's execution.
        Filtered by agent_config.tools.allowed patterns.

        Contrast with list_tools() which returns what this agent OFFERS to others.

        Returns:
            Tuple of:
            - List of tool server names this agent is allowed to use
            - List of allowed patterns (for fine-grained filtering after tool expansion)
            - List of blocked patterns (to be applied after tool expansion)
        """
        # Initialize tool integration (idempotent)
        await self._tool_integration_manager.setup_tool_integration()

        # Use ToolDiscoveryService for clean tool filtering
        discovery_service = ToolDiscoveryService(
            agent_name=self.name,
            agent_config=self.agent_config,
            tool_integration_manager=self._tool_integration_manager,
            registry=self.registry if hasattr(self, 'registry') else None
        )

        return await discovery_service.discover_allowed_tools()

    async def describe_context_inputs(
        self: Agent, session_id: Optional[str] = None, messages: Optional[list] = None
    ) -> tuple[str, list[Dict[str, Any]]]:
        """(system prompt, tool schemas) as they go into a call of *session_id*.

        What sits in the context window before the conversation does, and what
        `/context` counts. Two things matter about it:

        * The prompt is rendered WITH the session's template vars, because
          that is the prompt the session really sends -- rendered without
          them it is short by the whole var payload, on the one line the
          command exists to show.
        * One discovery, not two. Both halves need the list of usable tools,
          and asking for it twice means awaiting list_tools() on every
          registered server a second time.

        *messages* is the session's history as stored: the deferred tools it
        loaded are sent again, so they count. Without it this process's
        tracker is asked, which knows only the sessions that ran here.
        """
        if messages is None:
            messages = (self._session_tracker.get_session_messages(session_id) or []
                        if session_id and self._session_tracker else [])
        tools_schema, usable_tools = await self._schemas_for(
            *await self.list_usable_tools(), history=messages)
        max_steps = max(1, int(getattr(self.agent_config, "max_steps", 6)))
        system_msg, _ = self._render_prompts(
            usable_tools, max_steps, current_step=0, session_id=session_id)
        return system_msg, tools_schema

    async def _schemas_for(self: Agent, usable_tools: list[str],
                           allowed_patterns: Optional[list[str]],
                           blocked_patterns: Optional[list[str]],
                           history: Optional[list] = None,
                           ) -> tuple[list[Dict[str, Any]], list[str]]:
        """The LLM schemas for an ALREADY discovered set of tools, and the
        tool names after expansion and filtering -- what a run renders as
        ``tools``.

        Every schema, unless a session's *history* is given (empty for a new
        one): then the schemas a run of it would send -- the deferred ones held
        back except those the history already loaded."""
        schema_builder = ToolSchemaBuilder(
            agent_name=self.name,
            tool_integration_manager=self._tool_integration_manager,
            server_getter_func=self._get_server_from_any_registry,
        )
        tools_schema, mapping, usable, _display = await schema_builder.build_schemas(
            usable_tools,
            allowed_patterns=allowed_patterns,
            blocked_patterns=blocked_patterns,
        )
        tools_schema = list(tools_schema)
        if history is not None:
            deferred = DeferredTools.split(tools_schema, mapping, self._deferred_patterns(), self.name)
            if deferred is not None:
                deferred.restore(history, tools_schema)
        return tools_schema, usable

    async def build_llm_tool_schemas(self: Agent) -> list[Dict[str, Any]]:
        """The tool schemas of every tool this agent may call. A run sends
        the deferred ones (tools.deferred) only once loaded; what it sends is
        describe_context_inputs'.

        EXACTLY the pipeline that builds the LLM schema -- discovery (deny-all
        on empty allowed, _tool_visible, externals) plus ToolSchemaBuilder
        (tool-level allow/block, both tool interfaces, every schema dialect).
        A caller that re-implements half of it diverges on every point it
        skips: hybrid plugins (list_tools-only) went missing entirely, an
        empty allowlist meant allow-all in one place and deny-all in the
        other, and blocked patterns were never applied -- an agent asking what
        it could do got a different answer than the schema it ran with.

        Whole schemas, parameters and all: what a listing needs is the name,
        what a token count needs is the rest, and the parameters are usually
        the larger half of both.
        """
        schemas, _usable = await self._schemas_for(*await self.list_usable_tools())
        return schemas

    async def _list_usable_tools_with_details(self: Agent, params: Dict[str, Any]) -> list[Dict[str, Any]]:
        """Return detailed info about tools this agent CAN USE (name + description).

        Internal utility for agent subclasses (e.g., BasicAgent's list_available_tools).
        Like list_usable_tools() but includes descriptions for user-facing output.

        Args:
            params: Parameters including optional '_status' for progress reporting

        Returns:
            List of dicts with 'name' and 'description' keys

        Raises whatever the listing raises. It used to answer every failure
        with an empty list, which every caller reads as "this agent has no
        tools" -- the chat even named the cause: an empty allowlist.
        """
        status = params.get("_status")
        tools_schema = await self.build_llm_tool_schemas()

        all_tools: list[Dict[str, Any]] = []
        for entry in tools_schema:
            function = entry.get("function", {}) if isinstance(entry, dict) else {}
            if not isinstance(function, dict):
                continue
            all_tools.append({
                "name": function.get("name", "unknown"),
                "description": function.get("description", "") or "",
            })

        if status:
            await status.end(f"Listed available tools ({len(all_tools)} tools)")

        return all_tools

    def _deferred_patterns(self: Agent) -> list[str]:
        tools_config = getattr(self.agent_config, "tools", None)
        return list(getattr(tools_config, "deferred", None) or [])

    # ------------------------------------------------------------------
    # Unified Server Resolution (Central Method)
    # ------------------------------------------------------------------
    def _get_server_from_any_registry(self: Agent, server_name: str) -> Optional[ToolServer]:
        """Get a server from either plugin_registry or self.registry.

        This is the CENTRAL method for resolving servers. All code that needs to
        find a server should use this method instead of accessing registries directly.

        Search order:
        1. self.registry (contains ALL servers: plugins + config agents)
        2. plugin_registry (fallback for plugin adapters)

        Args:
            server_name: Name of the server to find (e.g., 'basic_operations', 'research_agent')

        Returns:
            The server instance or None if not found
        """
        registry = self.registry if hasattr(self, 'registry') else None
        return resolve_registry_server(registry, self._tool_integration_manager, server_name)

    # ------------------------------------------------------------------
    # Programmatic tool dispatch (used by tool_script and other in-process
    # callers that execute tools on the agent's behalf)
    # ------------------------------------------------------------------

    def _resolve_flat_tool_name(self: Agent, tool_name: str):
        """Resolve a flat tool name (e.g. 'v6_json_manage_json') to
        (server, server_name). Returns (None, None) if nothing matches.

        Order: exact server-name match (config agents / single-name servers),
        then longest-prefix match over '_'-joined segments (plugin tools), then
        the agent's own-tool prefix — mirroring ToolExecutionManager's paths.
        """
        server = self._get_server_from_any_registry(tool_name)
        if server:
            return server, tool_name
        server, candidate = resolve_longest_prefix(
            self._get_server_from_any_registry, tool_name)
        if server:
            return server, candidate
        if tool_name.startswith(f"{self.name}_"):
            return self, self.name
        return None, None

    def tool_dispatch_denial(self: Agent, tool_name: str, server_name: str) -> Optional[str]:
        """Why this agent may not dispatch *tool_name*, or None if it may.

        ONE definition of "may call", asked by two callers: ``dispatch_tool_call``
        raises it, and the chat's plugin commands ask it before LISTING a
        command, so /help never offers something that can only answer with a
        refusal. A second, drifting copy of these patterns is how a UI starts
        promising more than the agent has.

        Full fidelity to schema build: allowed first, then blocked, both matched
        on the same server/tool path by the SAME matcher schema build uses. No
        allowlist configured -> deny (schema build shows zero tools in that case
        too).
        """
        from ..tool_schema_builder import tool_matches_patterns

        tools_config = getattr(self.agent_config, "tools", None) if getattr(
            self, "agent_config", None) else None
        allowed_patterns = list(getattr(tools_config, "allowed", None) or [])
        blocked_patterns = list(getattr(tools_config, "blocked", None) or [])
        if not allowed_patterns or not tool_matches_patterns(
                tool_name, server_name, allowed_patterns):
            return f"Tool '{tool_name}' is not in this agent's allowed tools."
        if blocked_patterns and tool_matches_patterns(
                tool_name, server_name, blocked_patterns):
            return f"Tool '{tool_name}' is blocked for this agent."
        return None

    async def dispatch_tool_call(self: Agent, tool_name: str, params: Dict[str, Any], *,
                                 session_id: Optional[str] = None,
                                 user_id: Optional[str] = None,
                                 request_id: Optional[str] = None,
                                 hook_source: Optional[str] = None,
                                 cancellation_token: Optional[Any] = None,
                                 injected_params: Optional[Dict[str, Any]] = None) -> Any:
        """Execute one tool call programmatically with THIS agent's authorization.

        The in-process counterpart of the LLM tool path: same server resolution,
        same allowed/blocked pattern semantics as schema build (shared matcher
        ``tool_matches_patterns`` — what the LLM cannot see cannot be dispatched,
        in both directions), same runtime-param injection (shared
        ``inject_runtime_params``), same call_with_status/call dispatch.

        Used by the tool_script plugin ("scripted tool chains"); any future
        in-process caller (hooks, schedulers) should go through here as well.

        ``hook_source`` names a caller that acts for the model -- tool_script
        runs a script the model wrote. Given, the pre_tool_call and
        post_tool_call hooks fire as for the model's own calls, with
        ``tool_call["source"] = hook_source``: a call the hooks would stop
        must not get past them inside a script. None (the default) fires
        none: the caller is the framework or a person (slash commands,
        preloads, state machines), not the model. ``cancellation_token`` is
        the caller's; the hooks get it, so one that waits stops on a cancel.
        With a hook_source, a tool that raises is handed on the way the model's
        own loop hands a failure on: as an error result
        (``{"status": "error", ...}``), which passes the post_tool_call hooks
        -- a redacting hook sees a failure's text too.

        ``injected_params`` are values the caller's configuration adds
        (tool_script's ``inject_params``: secrets the model never wrote). They
        are merged after the pre_tool_call hooks, over what those left, and no
        hook sees them -- a hook that logs a call or shows it to a person must
        not expose them.

        Raises ToolDispatchError with an agent-actionable message for unknown
        tools, unsupported tool types, authorization failures and calls a
        pre_tool_call hook blocked. Tool-level errors are returned as the tool's
        normal result (callers interpret the status convention themselves).
        """
        from ....hooks.plugin_hook import HookType
        from ..components.tool_execution import (
            ToolDispatchError, drop_runtime_params, inject_runtime_params)

        # External tools (dotted names) take a different execution branch
        # (MCP client sessions) that programmatic dispatch does not replicate.
        if "." in tool_name:
            raise ToolDispatchError(
                f"Tool '{tool_name}' is an external MCP tool — not supported in "
                f"programmatic dispatch (v1). Call it directly instead.")

        server, server_name = self._resolve_flat_tool_name(tool_name)
        if server is None:
            raise ToolDispatchError(
                f"Unknown tool: '{tool_name}'. Use the exact tool name from your "
                f"tool list.")

        denial = self.tool_dispatch_denial(tool_name, server_name)
        if denial is not None:
            raise ToolDispatchError(denial)

        # SECURITY: strip caller-supplied runtime params BEFORE injecting the
        # real ones — same guarantee the LLM tool path gives. This path is
        # driven by tool_script, whose call_tool forwards script-authored params
        # verbatim; a script could otherwise pass _session_id to impersonate
        # another agent and defeat json_store's owner-based write protection.
        # request_id/requestId likewise: status and cancellation route by them.
        params, forged = drop_runtime_params(params)
        if forged:
            logger.warning(
                "Dropping caller-supplied runtime param(s) %s from programmatic "
                "dispatch of %s", forged, tool_name)

        hooks = getattr(self, "_hook_manager", None) if hook_source else None
        tool_call = {"id": None, "name": tool_name, "server": server_name,
                     "arguments": params, "source": hook_source}
        if hooks is not None and hooks.wants_hooks(HookType.PRE_TOOL_CALL):
            params, block = await hooks.execute_pre_tool_hooks(
                tool_call, step=0, request_id=request_id or "", session_id=session_id or "",
                cancellation_token=cancellation_token)
            # The loop's own calls check the run's token right before they
            # start; a script call must too -- a hook that waited for a person
            # returns (or fails) on the cancel, and the call must not run then.
            if cancellation_token is not None and getattr(cancellation_token, "is_cancelled", False):
                raise ToolDispatchError("Request cancelled — the call did not run.")
            if block is not None:
                raise ToolDispatchError(block)
            tool_call = {**tool_call, "arguments": params}
        if injected_params:
            extra, dropped = drop_runtime_params(dict(injected_params))
            if dropped:
                logger.warning("Dropping runtime param(s) %s injected into programmatic "
                               "dispatch of %s", dropped, tool_name)
            params = {**params, **extra}

        params = inject_runtime_params(
            params, session_id=session_id, user_id=user_id,
            request_id=request_id, agent=self)
        if request_id:
            params["request_id"] = params["requestId"] = request_id

        logger.info("Invoking tool %s via programmatic dispatch (agent=%s)",
                    tool_name, self.name)
        started_at = time.time()
        try:
            if hasattr(server, 'call_with_status'):
                result = await server.call_with_status(tool_name, params)
            else:
                result = await server.call(tool_name, params)
        except Exception as exc:
            if hook_source is None:
                raise
            logger.exception("Tool %s failed via programmatic dispatch", tool_name)
            result = {"status": "error", "error": f"Tool '{tool_name}' execution failed: {exc}",
                      "type": type(exc).__name__}
        finished_at = time.time()
        logger.info("Tool %s returned (programmatic dispatch): %s",
                    tool_name, str(result)[:500])
        if hooks is not None and hooks.wants_hooks(HookType.POST_TOOL_CALL):
            try:
                result = await hooks.execute_post_tool_hooks(
                    tool_call, result, step=0, request_id=request_id or "", session_id=session_id or "",
                    cancellation_token=cancellation_token, started_at=started_at, finished_at=finished_at)
            except Exception:
                # The call ran; raising now would report a done job as failed.
                logger.exception("post_tool_call hooks failed for %s; the result stays as "
                                 "the tool returned it", tool_name)

        # A tool may stage a rewritten history via set_compacted_messages
        # (the summarizer's manual path does). During a run the request's own
        # machinery consumes that staging (the rebuild after tool execution /
        # _finalize_request) -- but a direct dispatch with no active request
        # (chat slash commands, web buttons) has no finalize: the staging sat
        # stale, /stats kept showing the old history, and the NEXT turn's
        # selection either discarded it or rebuilt the history around it.
        # Apply it here instead. "A request owns the session" == session lock
        # held; in-run dispatch (tool_script) therefore never takes this
        # branch. The live entry is refreshed too -- it outlives the previous
        # turn and is what stats-style readers see first.
        tracker = getattr(self, "_session_tracker", None)
        if session_id and tracker is not None:
            compacted = tracker.get_compacted_messages(session_id)
            if compacted is not None and not tracker.check_session_locked(session_id)[0]:
                tracker.set_session_messages(session_id, compacted)
                tracker.clear_compacted_messages(session_id)
                self._set_live_messages(session_id, compacted)
                try:
                    await self._save_session_to_disk(session_id)
                except Exception:
                    logger.warning(
                        "Compacted history for %s applied in memory but the "
                        "disk save failed", session_id, exc_info=True)
        return result
