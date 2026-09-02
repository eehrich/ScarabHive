"""Sub-Agent Manager MCP Server implementation."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, List, Optional

from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.hooks.plugin_hook import PluginHook, HookContext, HookResult
from agent_system.utils.id import short_id
from agent_system.llm.token_utils import extract_text_from_content

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

from plugins.sub_agent_manager.manager import SubAgentManager

logger = logging.getLogger(__name__)


def _without_status(params: dict) -> dict:
    """Params for an INTERNAL sub-step, so it cannot close our status scope.

    All handlers share one StatusScope per tool call. A handler that calls a
    sibling with the original params hands it ``_status``; the sibling ends
    the scope, and everything the outer handler says afterwards is dropped
    (``StatusScope.ended``). The outer handler owns the line.
    """
    return {k: v for k, v in params.items() if k != "_status"}


def _register_request_user(request_id: str, user_id: str) -> None:
    """Register sub-agent request_id -> user_id mapping.

    This ensures sub-agent requests are properly associated with their user
    in the admin dashboard and other user-aware features.
    """
    from agent_system.core.request_context import register_request_user
    register_request_user(request_id, user_id)
    logger.debug(f"Registered sub-request {request_id} for user {user_id}")


class SubAgentManagerServer(SchemaBasedMCPServer, PluginHook):
    """MCP server for sub-agent management with hook support.

    Provides a unified tool `manage_sub_agent` with 5 operations:
    - create: Create and execute new sub-agent
    - continue: Continue existing sub-agent with new message
    - list: List active sub-agents
    - info: Get detailed status
    - delete: Archive sub-agent

    Also implements pre_llm_call hook to inject sub-agent context into system prompt.
    """

    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig) -> None:
        """Initialize SubAgentManagerServer.

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

        # Configuration
        self.max_sub_agents = int(getattr(mcp_config, 'max_sub_agents_per_session', 10))
        self.max_nesting_depth = int(getattr(mcp_config, 'max_nesting_depth', 5))
        self.max_history = int(getattr(mcp_config, 'max_message_history', 100))
        self.max_nesting_depth = int(getattr(mcp_config, 'max_nesting_depth', 5))
        self.max_sub_agents_per_type = int(getattr(mcp_config, 'max_sub_agents_per_type', 3))
        
        # Auto-archive oldest sub-agent when limit is reached
        self.auto_archive_on_limit = bool(getattr(mcp_config, 'auto_archive_on_limit', False))

        # Timeout configuration
        self.default_wait_timeout = int(getattr(mcp_config, 'default_wait_timeout', 3600))  # Default 1 hour

        # 'info' pagination (see _handle_info): the coordinator most often
        # wants the tail, but has to be able to page through the FULL
        # transcript too, the same way a file-reading tool offers offset+limit.
        # info_max_limit is a hard per-call cap, not a policy choice — it stops
        # one call from dumping the entire history back into the coordinator's
        # own context; page with 'offset' instead.
        self.info_default_limit = int(getattr(mcp_config, 'info_default_limit', 20))
        self.info_max_limit = int(getattr(mcp_config, 'info_max_limit', 200))
        self.info_default_max_chars = int(getattr(mcp_config, 'info_default_max_chars', 4000))

        # Agent filtering (multi-instance support - by instance name, not type)
        self.allowed_agents = list(getattr(mcp_config, 'allowed_agents', ['*']))
        self.blocked_agents = list(getattr(mcp_config, 'blocked_agents', []))

        # Kosten-Riegel: darf der AUFRUFER use_advanced_model=true setzen?
        # LLM-Caller setzen das Flag gern aus Eigeninitiative (Prod-Befund
        # 2026-07-20: der v6-Coordinator spawnte JEDES Panel mit
        # use_advanced_model=true, ohne dass sein Prompt es verlangt — der
        # komplette Moderator-Run lief still auf der advanced-Kette).
        # False = Flag wird ignoriert (mit Log); Default True = Bestand.
        self.allow_advanced_model = bool(getattr(mcp_config, 'allow_advanced_model', True))

        # Phase-based agent filtering (affects both tool schema and create validation)
        # Config is at top-level (same as allowed_agents), not inside hook_config
        phase_config = getattr(mcp_config, 'phase_filtering', {}) or {}
        self.phase_filtering_enabled = phase_config.get('enabled', False)
        self.phase_variable = phase_config.get('phase_variable', 'workflow_phase')
        self.phase_agents = phase_config.get('phase_agents', {})

        # Min result length per agent type: auto-retry if result is too short.
        # Config: {"v5b_synopsis_writer": 500, "v5b_beat_generator": 50}
        self._min_result_length_by_agent: dict[str, int] = dict(
            getattr(mcp_config, 'min_result_length_by_agent', {}) or {}
        )
        self._min_result_retries: int = int(
            getattr(mcp_config, 'min_result_retries', 2)
        )

        # Track running sub-agent instances to prevent concurrent execution
        # Format: {sub_session_id: True}
        self._running_agents: set[str] = set()
        self._running_lock = __import__('asyncio').Lock()
        
        # Track async jobs by instance_id: {instance_id: {task, status, started_at, result, error}}
        # Only running jobs are kept in memory - completed jobs are removed and loaded from DB on demand
        self._async_jobs: dict[str, dict[str, Any]] = {}
        self._async_jobs_lock = __import__('asyncio').Lock()

    def reload_config(self, mcp_config: Any) -> dict:
        """Hot-reload the mutable, config-derived fields from a freshly parsed
        MCPConfig — WITHOUT tearing down this instance or its running sub-agents.

        Called by the deliberate config-reload flow (POST /admin/reload-config,
        `agent-cli reload`). Only the plain filter/limit knobs are refreshed;
        running jobs, sessions and history are untouched. Adding a brand-new
        agent *definition* still needs a restart (the agent must be registered),
        but changing this manager's ``allowed_agents`` / limits / phase filtering
        now takes effect live.

        Returns a dict of the fields that actually changed ({} if nothing did),
        so the caller can report exactly what the reload updated.
        """
        changes: dict[str, dict] = {}

        def _upd(attr: str, new_value: Any) -> None:
            old_value = getattr(self, attr, None)
            if old_value != new_value:
                changes[attr] = {"old": old_value, "new": new_value}
                setattr(self, attr, new_value)

        _upd("allowed_agents", list(getattr(mcp_config, 'allowed_agents', ['*'])))
        _upd("blocked_agents", list(getattr(mcp_config, 'blocked_agents', [])))
        _upd("allow_advanced_model", bool(getattr(mcp_config, 'allow_advanced_model', True)))
        _upd("max_sub_agents", int(getattr(mcp_config, 'max_sub_agents_per_session', 10)))
        _upd("max_nesting_depth", int(getattr(mcp_config, 'max_nesting_depth', 5)))
        _upd("max_sub_agents_per_type", int(getattr(mcp_config, 'max_sub_agents_per_type', 3)))
        _upd("max_history", int(getattr(mcp_config, 'max_message_history', 100)))
        _upd("auto_archive_on_limit", bool(getattr(mcp_config, 'auto_archive_on_limit', False)))
        _upd("default_wait_timeout", int(getattr(mcp_config, 'default_wait_timeout', 3600)))
        _upd("info_default_limit", int(getattr(mcp_config, 'info_default_limit', 20)))
        _upd("info_max_limit", int(getattr(mcp_config, 'info_max_limit', 200)))
        _upd("info_default_max_chars", int(getattr(mcp_config, 'info_default_max_chars', 4000)))

        phase_config = getattr(mcp_config, 'phase_filtering', {}) or {}
        _upd("phase_filtering_enabled", phase_config.get('enabled', False))
        _upd("phase_variable", phase_config.get('phase_variable', 'workflow_phase'))
        _upd("phase_agents", phase_config.get('phase_agents', {}))

        _upd("_min_result_length_by_agent",
             dict(getattr(mcp_config, 'min_result_length_by_agent', {}) or {}))
        _upd("_min_result_retries", int(getattr(mcp_config, 'min_result_retries', 2)))

        if changes:
            logger.info(
                "SubAgentManager '%s' config reloaded: %s",
                getattr(self, 'name', '?'), ", ".join(sorted(changes.keys())),
            )
        return changes

    def _effective_use_advanced(self, params: dict[str, Any]) -> bool:
        """Vom Aufrufer angefordertes ``use_advanced_model`` gegen den
        Instanz-Riegel ``allow_advanced_model`` prüfen. Unterdrückung wird
        geloggt (kein stilles Umbiegen) — der Sub-Agent läuft dann auf
        seiner normalen Profil-Kette."""
        requested = bool(params.get("use_advanced_model", False))
        if requested and not self.allow_advanced_model:
            logger.info(
                "[%s] use_advanced_model angefordert, aber per Config "
                "unterdrückt (allow_advanced_model=false) — Standard-Profil.",
                self.name,
            )
            return False
        return requested

    def is_agent_running(self, instance_id: str) -> bool:
        """Check if sub-agent is actually running (has active async task OR synchronous execution).
        
        Args:
            instance_id: Sub-agent instance ID
            
        Returns:
            True if agent has active task in _async_jobs with running/pending status,
            OR if agent is in _running_agents (synchronous continue/spawn execution),
            False otherwise
        """
        # Check synchronous running agents first (continue operation uses this)
        if instance_id in self._running_agents:
            return True
        
        # Check async jobs (async spawn operation uses this)
        if instance_id not in self._async_jobs:
            return False
        
        job_status = self._async_jobs[instance_id].get("status", "unknown")
        return job_status in ("pending", "running")

    def get_template_vars(self) -> dict:
        """Return template variables for schema rendering.

        Uses same filtering logic as GET /agents endpoint:
        - Check if server is an Agent instance
        - Respect _mcp_public visibility flag

        This is called dynamically at runtime (not cached) to ensure
        the agent list is always up-to-date.
        """
        from agent_system.plugins.mcp_adapter import plugin_mcp_registry
        from agent_system.servers.agent.server import Agent

        agent_names = []
        for name in plugin_mcp_registry.list_servers():
            if name.startswith('_') or name in self.blocked_agents:
                continue

            try:
                # Access plugin_servers dict directly (PluginMCPRegistry has no .get() method)
                adapter = plugin_mcp_registry.plugin_servers.get(name)
                if not adapter:
                    continue

                # Get the actual plugin instance from the adapter
                srv = adapter.plugin_server  # PluginMCPAdapter.plugin_server is the actual server instance

                # Same logic as GET /agents endpoint
                if isinstance(srv, Agent):
                    # Check visibility flags: need either _mcp_public (UI) OR _mcp_tool_visible (tool)
                    # Skip only if BOTH are explicitly False (private agents)
                    is_ui_visible = getattr(srv, '_mcp_public', False)
                    is_tool_visible = getattr(srv, '_mcp_tool_visible', False)

                    if not is_ui_visible and not is_tool_visible:
                        continue  # Skip truly private agents

                    agent_names.append(name)
            except Exception as e:
                # Unexpected error - log and skip
                logger.debug(f"SubAgentManagerServer '{self.name}' skipping server '{name}': {type(e).__name__}: {e}")
                continue

        if self.allowed_agents and '*' not in self.allowed_agents:
            # Apply allowed_agents filter if specified
            agent_names = [name for name in agent_names if name in self.allowed_agents]

        return {
            'name': self.name,  # CRITICAL: Must include 'name' for {{ name }} template variable in schema.yaml
            'allowed_agents': agent_names,
            'phase_filtering_enabled': self.phase_filtering_enabled,
            'phase_variable': self.phase_variable
        }

    async def list_tools(self) -> list:
        """Override list_tools() to re-render schema dynamically.

        This ensures the agent list in the schema is always up-to-date,
        since agents are registered after this plugin is initialized.
        """
        # Invalidate schema cache to force re-rendering
        self._schema_cache = None

        # Call parent implementation (will re-render with fresh template vars)
        return await super().list_tools()

    def _extract_session_service(self, params: dict[str, Any]):
        """Extract session_service from params (_session_service or _agent._session_service).

        Args:
            params: Tool parameters with either _session_service or _agent

        Returns:
            SessionService instance

        Raises:
            RuntimeError: If session_service cannot be found
        """
        # Try direct injection first (from WebUI endpoints)
        session_service = params.get("_session_service")
        if session_service:
            return session_service

        # Try getting from agent instance (from ToolExecutionManager)
        agent = params.get("_agent")
        if agent and hasattr(agent, '_session_service'):
            return agent._session_service

        raise RuntimeError(
            "No session_service available - neither _session_service nor _agent._session_service found. "
            "This tool must be called from an agent or with explicit session_service injection."
        )

    def _get_manager(self, session_service, registry=None) -> SubAgentManager:
        """Get SubAgentManager with injected dependencies.

        Args:
            session_service: SessionService instance (injected from params["_session_service"])
            registry: MCPRegistry instance (injected from params["_registry"], optional for some ops)

        Returns:
            SubAgentManager instance

        Note:
            Uses injected session_service from Agent instead of creating a new one.
            This ensures all tools use the same session storage and avoids duplication.
        """
        # Use injected session_service (passed from Agent via ToolExecutionManager)
        if not session_service:
            raise RuntimeError("session_service is required but was not injected")

        # Create manager with injected dependencies
        return SubAgentManager(
            session_service, 
            registry, 
            self.max_nesting_depth,
            self.max_sub_agents_per_type,
            self.max_sub_agents,
            auto_archive_on_limit=self.auto_archive_on_limit
        )

    def _extract_registry(self, params: dict[str, Any]):
        """Extract and validate registry from params.

        Args:
            params: Tool parameters with injected _agent

        Returns:
            MCPRegistry instance

        Raises:
            RuntimeError: If registry not found
        """
        # Extract from agent instance (new pattern - same as session_service)
        agent = params.get("_agent")
        if agent and hasattr(agent, 'registry'):
            return agent.registry

        raise RuntimeError(
            "No registry available - neither _registry nor _agent.registry found. "
            "This tool must be called from an agent."
        )

    def _infer_operation(self, params: dict[str, Any]) -> Optional[str]:
        """Best-effort, SAFE inference when the model omits ``operation`` — a
        common, turn-wasting mistake (especially forgetting it on ``continue``).

        Only the two unambiguous argument shapes are inferred; anything else
        returns None so the caller still gets the explicit "Missing operation"
        error rather than a silently wrong action:
        - ``agent_type`` present, no ``instance_id`` -> ``create`` (agent_type
          exists ONLY to spawn a new agent).
        - ``instance_id`` + a prompt (``message``/``task``), no ``agent_type`` ->
          ``continue`` (a follow-up to an existing instance; the read-only ops
          poll/wait/info/cancel/delete never carry a prompt).

        Deliberately NOT inferred (ambiguous -> explicit error): ``instance_id``
        alone (poll vs info vs cancel vs delete is unknowable), or both
        ``agent_type`` and ``instance_id`` together.
        """
        has_type = bool(params.get("agent_type"))
        has_id = bool(params.get("instance_id"))
        has_prompt = bool(params.get("message") or params.get("task"))
        if has_type and not has_id:
            return "create"
        if has_id and has_prompt and not has_type:
            return "continue"
        return None

    async def manage_sub_agent(self, params: dict[str, Any]) -> dict[str, Any]:
        """Unified handler for all sub-agent operations.

        Dispatches to operation-specific handlers based on params["operation"].
        """
        operation = params.get("operation")
        status = params.get("_status")  # Get status early for error reporting

        if not operation:
            # Safety net: the model often forgets `operation` (esp. on continue).
            # Infer it from the argument shape when unambiguous — saves the retry
            # turn — but only for the two safe cases; otherwise error as before.
            operation = self._infer_operation(params)
            if operation:
                logger.info(
                    "SubAgentManager: no 'operation' given — inferred '%s' from the arguments",
                    operation,
                )

        if not operation:
            if status:
                await status.error("Missing 'operation' parameter")
            return {"status": "error", "error": "Missing 'operation' parameter"}

        try:
            if operation == "create":
                return await self._handle_create(params)
            elif operation == "continue":
                return await self._handle_continue(params)
            elif operation == "poll":
                return await self._handle_poll(params)
            elif operation == "wait":
                return await self._handle_wait(params)
            elif operation == "wait_all":
                return await self._handle_wait_all(params)
            elif operation == "cancel":
                return await self._handle_cancel(params)
            elif operation == "list":
                return await self._handle_list(params)
            elif operation == "info":
                return await self._handle_info(params)
            elif operation == "delete":
                return await self._handle_delete(params)
            else:
                if status:
                    await status.error(f"Unknown operation: {operation}")
                return {"status": "error", "error": f"Unknown operation: {operation}"}
        except Exception as e:
            logger.exception(f"Error in manage_sub_agent ({operation}): {e}")
            if status:
                await status.error(f"Sub-agent error ({operation}): {str(e)}")
            return {"status": "error", "error": str(e)}

    async def _handle_create(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle 'create' operation - create and execute new sub-agent."""
        # Get status context early (before try block) so it's available in except
        status = params.get("_status")
        
        try:
            # Extract & validate required parameters. LLMs (especially weak
            # local models) sometimes call 'create' without these — return an
            # actionable error so the agent can correct the call instead of
            # getting an opaque KeyError and blindly retrying the same broken
            # call (raw params["..."] surfaced just "'agent_type'" to the agent).
            agent_name = params.get("agent_type")  # This is actually the agent instance name
            task = params.get("task")
            missing = [
                key for key, val in (("agent_type", agent_name), ("task", task))
                if not (isinstance(val, str) and val.strip())
            ]
            if missing:
                phase_allowed = self._get_phase_allowed_agents(params)
                concrete = phase_allowed or [a for a in self.allowed_agents if a != "*"]
                agent_hint = (
                    f"one of: {', '.join(concrete)}" if concrete
                    else "the name of a registered agent"
                )
                error_msg = (
                    f"Missing required parameter(s) for 'create': {', '.join(missing)}. "
                    f"'create' needs 'agent_type' ({agent_hint}) and "
                    f"'task' (the instruction text for the sub-agent)."
                )
                logger.info(f"create rejected — {error_msg}")
                if status:
                    await status.error(error_msg)
                return {
                    "status": "error",
                    "error": error_msg,
                    "error_type": "missing_parameter",
                }

            instance_label = params.get("instance_label")
            use_advanced_model = self._effective_use_advanced(params)
            blocking = params.get("blocking", True)  # NEW: default to blocking behavior
            # Note: config_overrides would be used here when Agent.run_events supports them
            # For now, sub-agent uses its default configuration

            # Get parent session ID from injected context
            parent_session_id = params.get("_session_id")
            if not parent_session_id:
                raise ValueError("No session context available - this tool must be called from an agent")

            # Validate agent is allowed by this manager instance (with phase filtering)
            phase_allowed = self._get_phase_allowed_agents(params)
            if not self._is_agent_allowed_for_phase(agent_name, phase_allowed):
                # Build helpful error message
                if phase_allowed:
                    current_phase = self._get_current_phase(params)
                    error_msg = (
                        f"Agent '{agent_name}' not allowed in current phase '{current_phase}'. "
                        f"Allowed agents for this phase: {', '.join(phase_allowed)}"
                    )
                    logger.info(f"Phase filtering blocked agent: {error_msg}")
                    if status:
                        await status.error(error_msg)
                    return {
                        "status": "error",
                        "error": error_msg,
                        "error_type": "phase_blocked"
                    }
                else:
                    allowed_str = ', '.join(self.allowed_agents)
                    error_msg = (
                        f"Agent '{agent_name}' not allowed by this sub-agent manager. "
                        f"Allowed agents: {allowed_str}"
                    )
                    logger.info(f"Agent filtering blocked agent: {error_msg}")
                    if status:
                        await status.error(error_msg)
                    return {
                        "status": "error",
                        "error": error_msg,
                        "error_type": "agent_blocked"
                    }

            if status:
                await status.progress(f"Creating sub-agent: {agent_name}")

            # Get manager with injected dependencies
            registry = self._extract_registry(params)
            session_service = self._extract_session_service(params)
            manager = self._get_manager(session_service, registry)

            # Inject creator_plugin into params so manager knows which instance created this sub-agent
            params["_creator_plugin"] = self.name

            # Create sub-session (pass params for user_id extraction)
            try:
                sub_session_id = await manager.create_sub_session(
                    parent_session_id=parent_session_id,
                    agent_type=agent_name,
                    initial_message=task,
                    instance_label=instance_label,
                    params=params  # Pass params for user_id extraction and creator_plugin
                )
            except ValueError as e:
                # Handle max sub-agents limit gracefully
                if "Maximum number of active sub-agents" in str(e):
                    msg = str(e).split("Active sub-agents:")[0].strip()
                    logger.info(f"Sub-agent limit reached: {msg}")
                    if status:
                        await status.error(msg)
                    return {
                        "instance_id": None,
                        "status": "limit_reached",
                        "error": msg,
                        "agent_type": agent_name
                    }
                # Re-raise other ValueErrors (invalid agent, etc.)
                raise

            logger.info(f"Created sub-session {sub_session_id} for parent {parent_session_id}")

            # If non-blocking, start async execution and return immediately
            if not blocking:
                if status:
                    await status.progress(f"Starting async execution of {agent_name}")
                
                # Store job metadata
                async with self._async_jobs_lock:
                    self._async_jobs[sub_session_id] = {
                        "instance_id": sub_session_id,
                        "status": "pending",
                        "agent_type": agent_name,
                        "task": task,
                        "parent_session_id": parent_session_id,
                        "started_at": datetime.now(UTC).isoformat(),
                        "completed_at": None,
                        "result": None,
                        "error": None,
                        "task_handle": None
                    }

                # Start background execution
                import asyncio
                task_coro = self._execute_async_job(
                    instance_id=sub_session_id,
                    params=params,
                    agent_name=agent_name,
                    task=task,
                    use_advanced_model=use_advanced_model
                )
                task_handle = asyncio.create_task(task_coro)
                
                # Store task handle
                async with self._async_jobs_lock:
                    self._async_jobs[sub_session_id]["task_handle"] = task_handle
                    self._async_jobs[sub_session_id]["status"] = "running"

                if status:
                    await status.end(f"Async execution started: {sub_session_id}")

                return {
                    "instance_id": sub_session_id,
                    "status": "running",
                    "agent_type": agent_name,
                    "message": "Sub-agent execution started in background. Use poll or wait to check status."
                }

            # BLOCKING path: Check if sub-agent is already running (prevent concurrent execution)
            async with self._running_lock:
                if sub_session_id in self._running_agents:
                    raise ValueError(
                        f"Sub-agent '{sub_session_id}' is already running. "
                        "Cannot execute the same sub-agent instance concurrently. "
                        "Wait for current execution to complete or use a different instance."
                    )
                # Mark as running
                self._running_agents.add(sub_session_id)

            try:
                # Get agent from registry
                agent = registry.get(agent_name)
                if not agent:
                    raise ValueError(f"Agent '{agent_name}' not found in registry")

                # Inject session_service into agent (same pattern as app.py and agent_cli.py)
                # ALWAYS inject, even if already set, to ensure correct reference
                agent._session_service = session_service

                # CRITICAL: Set session metadata for sub-agent session
                # This ensures user_id is available during tool execution
                user_id = manager._extract_user_id(parent_session_id, params)
                agent._session_tracker.set_session_metadata(sub_session_id, {
                    "user_id": user_id,
                    "agent_name": agent_name,
                    "llm_profile": getattr(agent.agent_config, 'default_llm_profile', 'normal')
                })
                logger.debug(f"Set session metadata for sub-agent {sub_session_id}: user_id={user_id}")

                # CRITICAL: Restore context_vars from sub-session to agent's template_vars
                # This inherits book_id, workflow_phase, etc. from parent session
                try:
                    sub_session_data = await session_service.session_manager.load_session(
                        user_id, sub_session_id
                    )
                    context_vars = sub_session_data.get("context_vars", {})
                    if context_vars:
                        # Set session-scoped template vars (session-isolated, no global mutation)
                        agent._session_tracker.set_session_template_vars(sub_session_id, context_vars)
                        logger.debug(
                            f"Inherited context_vars to sub-agent template_vars: {list(context_vars.keys())}"
                        )
                except Exception as e:
                    logger.warning(f"Could not load context_vars for sub-agent: {e}")

                # Execute sub-agent with initial task (blocking)
                if status:
                    await status.progress(f"Executing {agent_name} with initial task...")

                result_text = ""

                # Generate hierarchical request ID: parent_request_id + "_sub_" + counter
                parent_request_id = params.get("_request_id")
                if parent_request_id:
                    # Use parent's request ID as base
                    sub_request_id = f"{parent_request_id}_sub_{short_id(6)}"
                else:
                    # Fallback to simple ID if no parent request_id
                    sub_request_id = f"sub_{short_id()}"

                # Register sub-request user mapping for admin dashboard
                _register_request_user(sub_request_id, user_id)

                async for event in agent.run_events(
                    task=task,
                    request_id=sub_request_id,
                    session_id=sub_session_id,
                    use_advanced_model=use_advanced_model
                    # Note: config_overrides would go here if Agent.run_events supported them
                    # For now, sub-agent uses its default configuration
                ):
                    event_type = event.get("type")
                    
                    # Track activity for live status display
                    try:
                        if event_type == "thinking_delta":
                            await manager.update_sub_agent_activity(
                                parent_session_id, sub_session_id, "💭 Thinking..."
                            )
                        elif event_type == "mcp_call":
                            tool_name = event.get("action", "tool")
                            await manager.update_sub_agent_activity(
                                parent_session_id, sub_session_id, f"🔧 Running tool: {tool_name}"
                            )
                        elif event_type == "status":
                            status_msg = event.get("message", "Processing...")
                            phase = event.get("phase", "progress")
                            # Use different icons based on status phase
                            if phase == "error":
                                icon = "❌"
                            elif phase == "end":
                                icon = "✅"
                            else:
                                icon = "⚙️"
                            await manager.update_sub_agent_activity(
                                parent_session_id, sub_session_id, f"{icon} {status_msg}"
                            )
                    except Exception as activity_err:
                        # Don't fail execution if activity tracking fails
                        logger.debug(f"Activity tracking failed: {activity_err}")
                    
                    # Collect final result (can be "final", "error", or "cancelled")
                    if event_type == "final":
                        result_text = event.get("summary", "")
                        # Clear activity on completion
                        await manager.update_sub_agent_activity(parent_session_id, sub_session_id, None)
                        # DON'T break here - continue iterating to get "end" event
                        # This ensures _finalize_request runs and messages are persisted
                    elif event_type == "end":
                        # Generator fully completed, messages are now in SessionTracker
                        break
                    elif event_type == "error":
                        result_text = f"Error: {event.get('message', 'Unknown error')}"
                        logger.warning(f"Sub-agent {sub_session_id} returned error: {result_text}")
                        await manager.update_sub_agent_activity(parent_session_id, sub_session_id, None)
                        break  # Stop waiting for more events
                    elif event_type == "cancelled":
                        result_text = f"Cancelled: {event.get('reason', 'Request was cancelled')}"
                        logger.info(f"Sub-agent {sub_session_id} was cancelled: {result_text}")
                        await manager.update_sub_agent_activity(parent_session_id, sub_session_id, None)
                        break  # Stop waiting for more events

                # -- Min result length guard: auto-retry with continue if too short --
                min_len = self._min_result_length_by_agent.get(agent_name, 0)
                if min_len > 0 and result_text and not result_text.startswith(("Error:", "Cancelled:")) and len(result_text) < min_len:
                    for retry_attempt in range(self._min_result_retries):
                        logger.warning(
                            "Sub-agent %s result too short (%d < %d chars), auto-retry %d/%d",
                            agent_name, len(result_text), min_len,
                            retry_attempt + 1, self._min_result_retries,
                        )
                        if status:
                            await status.progress(
                                f"⚠ {agent_name} result too short ({len(result_text)} chars), retrying..."
                            )
                        retry_result = ""
                        retry_req_id = f"{sub_request_id}_minlen_{retry_attempt}"
                        _register_request_user(retry_req_id, user_id)
                        async for event in agent.run_events(
                            task="Deine Antwort war unvollständig oder leer. Vervollständige deine Antwort.",
                            request_id=retry_req_id,
                            session_id=sub_session_id,
                        ):
                            event_type = event.get("type")
                            if event_type == "final":
                                retry_result = event.get("summary", "")
                                await manager.update_sub_agent_activity(
                                    parent_session_id, sub_session_id, None
                                )
                            elif event_type in ("end", "error", "cancelled"):
                                if event_type == "error":
                                    retry_result = f"Error: {event.get('message', '')}"
                                break
                        if retry_result and len(retry_result) >= min_len:
                            result_text = retry_result
                            logger.info(
                                "Sub-agent %s retry %d succeeded (%d chars)",
                                agent_name, retry_attempt + 1, len(result_text),
                            )
                            break
                        if retry_result:
                            result_text = retry_result  # Use latest even if still short
                    if len(result_text) < min_len:
                        logger.warning(
                            "Sub-agent %s still too short after %d retries (%d chars, min=%d)",
                            agent_name, self._min_result_retries, len(result_text), min_len,
                        )

                # Save session with messages after execution
                user_id = manager._extract_user_id(parent_session_id, params)
                # Get actual LLM profile from agent configuration
                llm_profile = agent.agent_config.default_llm_profile
                await session_service.save_session(
                    agent=agent,
                    user_id=user_id,
                    session_id=sub_session_id,
                    agent_name=agent_name,
                    llm_profile=llm_profile,
                    was_new_session=True
                )
                logger.debug(f"Saved sub-agent session {sub_session_id} with messages")

                # Update metadata after execution (including message_count)
                await manager.update_sub_session_metadata(
                    parent_session_id=parent_session_id,
                    sub_session_id=sub_session_id,
                    last_used=datetime.now(UTC).isoformat(),
                    message_count=2  # user + assistant for initial creation
                )

                if status:
                    # result_text carries the outcome ("Error: ..."/"Cancelled: ...")
                    # -- the same predicate the retry gate above uses. The end
                    # line reported "Created ..." either way, so an aborted run
                    # was the green line that stayed in the WebUI.
                    if result_text.startswith(("Error:", "Cancelled:")):
                        await status.error(
                            f"Sub-agent {sub_session_id} ({agent_name}): {result_text}")
                    else:
                        await status.end(
                            f"Created sub-agent {sub_session_id} (type: {agent_name}), "
                            f"{len(result_text)} chars")

                return {
                    "instance_id": sub_session_id,
                    "status": "completed",
                    "result": result_text,
                    "message_count": 2,  # user + assistant
                    "agent_type": agent_name
                }
            finally:
                # ALWAYS release lock, even on error (CRITICAL for preventing deadlock)
                async with self._running_lock:
                    self._running_agents.discard(sub_session_id)
                logger.debug(f"Released running lock for sub-agent {sub_session_id}")

        except Exception as e:
            logger.exception(f"Error in create_sub_agent: {e}")
            if status:
                await status.error(f"Failed to create sub-agent: {str(e)}")
            return {
                "status": "error",
                "error": str(e)
            }

    async def _handle_continue(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle 'continue' operation - continue existing sub-agent."""
        # Get status context early (before try block) so it's available in except
        status = params.get("_status") if params else None
        
        # Validate params
        if not params:
            if status:
                await status.error("Continue: params is None")
            return {"status": "error", "error": "Invalid parameters (None)"}
        
        try:
            # Extract and validate required parameters
            instance_id = params.get("instance_id")
            if not instance_id:
                if status:
                    await status.error("Continue: 'instance_id' is required")
                return {"status": "error", "error": "Missing required parameter: 'instance_id'"}
            
            # Accept `task` as a fallback for `message`: when the model omits
            # `operation` and this call is inferred as a continue, it often put
            # the follow-up prompt in `task` (the create field) by habit.
            message = params.get("message") or params.get("task")
            if not message:
                if status:
                    await status.error("Continue: 'message' is required")
                return {"status": "error", "error": "Missing required parameter: 'message'"}
            use_advanced_model = self._effective_use_advanced(params)

            # Get parent session ID from injected context
            parent_session_id = params.get("_session_id")
            if not parent_session_id:
                raise ValueError("No session context available")

            # Get manager with injected dependencies
            registry = self._extract_registry(params)
            session_service = self._extract_session_service(params)
            manager = self._get_manager(session_service, registry)
            user_id = manager._extract_user_id(parent_session_id, params)
            session_manager = manager._session_service.session_manager

            try:
                sub_session_data = await session_manager.load_session(user_id, instance_id)
            except FileNotFoundError:
                raise ValueError(f"Sub-agent instance '{instance_id}' not found")

            # Verify parent link
            parent_link = sub_session_data.get("parent_session", {}).get("session_id")
            if parent_link != parent_session_id:
                raise ValueError(
                    f"Sub-agent '{instance_id}' does not belong to current session "
                    f"(actual_parent={parent_link}, caller={parent_session_id})"
                )

            # Get agent type from session data
            agent_type = sub_session_data.get("agent_name")
            agent = registry.get(agent_type)
            if not agent:
                raise ValueError(f"Agent type '{agent_type}' not found")

            # Reactivate archived sub-agent BEFORE execution starts
            # This ensures the sub-agent shows as "active" during execution
            await manager.update_sub_session_metadata(
                parent_session_id=parent_session_id,
                sub_session_id=instance_id,
                status="active"
            )
            logger.debug(f"Reactivated sub-agent {instance_id} (was potentially archived)")

            # Check if sub-agent is already running (prevent concurrent execution)
            # CRITICAL: Use single try-finally to ensure _running_agents is ALWAYS cleaned up
            async with self._running_lock:
                if instance_id in self._running_agents:
                    raise ValueError(
                        f"Sub-agent '{instance_id}' is already running. "
                        "Cannot execute the same sub-agent instance concurrently. "
                        "Wait for current execution to complete."
                    )
                # Mark as running
                self._running_agents.add(instance_id)

            try:
                # Inject session_service into agent (same pattern as app.py and agent_cli.py)
                # ALWAYS inject, even if already set, to ensure correct reference
                agent._session_service = session_service

                # CRITICAL: Set session metadata for sub-agent session (for continued execution)
                # This ensures user_id is available during tool execution
                user_id = manager._extract_user_id(parent_session_id, params)
                agent._session_tracker.set_session_metadata(instance_id, {
                    "user_id": user_id,
                    "agent_name": agent_type,
                    "llm_profile": getattr(agent.agent_config, 'default_llm_profile', 'normal')
                })
                logger.debug(f"Set session metadata for continued sub-agent {instance_id}: user_id={user_id}")

                # CRITICAL: Refresh context_vars from the PARENT's live state, then
                # restore them into the sub-agent's template_vars.
                #
                # A sub-agent inherits context_vars only once, at create time.
                # Re-reading only its own frozen snapshot here would render the
                # prompt from stale state: after the coordinator calls
                # set_context(aufgabe=World) and continues this sub-agent,
                # {{ aufgabe }} would still say the previous task while the task
                # text says "World" — two contradicting instructions. Vars the
                # sub-agent set itself survive (see merge_parent_context_vars).
                try:
                    context_vars = await manager.refresh_sub_context_vars(
                        user_id=user_id,
                        sub_session_id=instance_id,
                        sub_session_data=sub_session_data,
                        parent_agent=params.get("_agent"),
                        parent_session_id=parent_session_id,
                    )
                    if context_vars:
                        # Set session-scoped template vars (session-isolated, no global mutation)
                        agent._session_tracker.set_session_template_vars(instance_id, context_vars)
                        logger.debug(
                            f"Restored context_vars for continued sub-agent: {list(context_vars.keys())}"
                        )
                except Exception as e:
                    logger.warning(f"Could not restore context_vars for continued sub-agent: {e}")

                if status:
                    await status.progress(f"Continuing {agent_type} with new message...")

                # Execute sub-agent with new message (continues existing session)
                result_text = ""

                # Generate hierarchical request ID for continue operation
                parent_request_id = params.get("_request_id")
                if parent_request_id:
                    sub_request_id = f"{parent_request_id}_sub_cont_{short_id(6)}"
                else:
                    sub_request_id = f"sub_cont_{short_id()}"

                # Register sub-request user mapping for admin dashboard
                _register_request_user(sub_request_id, user_id)

                async for event in agent.run_events(
                    task=message,
                    request_id=sub_request_id,
                    session_id=instance_id,  # Continue existing session
                    use_advanced_model=use_advanced_model
                ):
                    event_type = event.get("type")
                    
                    # Track activity for live status display
                    try:
                        if event_type == "thinking_delta":
                            await manager.update_sub_agent_activity(
                                parent_session_id, instance_id, "💭 Thinking..."
                            )
                        elif event_type == "mcp_call":
                            tool_name = event.get("action", "tool")
                            await manager.update_sub_agent_activity(
                                parent_session_id, instance_id, f"🔧 Running tool: {tool_name}"
                            )
                        elif event_type == "status":
                            status_msg = event.get("message", "Processing...")
                            phase = event.get("phase", "progress")
                            # Use different icons based on status phase
                            if phase == "error":
                                icon = "❌"
                            elif phase == "end":
                                icon = "✅"
                            else:
                                icon = "⚙️"
                            await manager.update_sub_agent_activity(
                                parent_session_id, instance_id, f"{icon} {status_msg}"
                            )
                    except Exception as activity_err:
                        # Don't fail execution if activity tracking fails
                        logger.debug(f"Activity tracking failed: {activity_err}")
                    
                    # Collect final result (can be "final", "error", or "cancelled")
                    if event_type == "final":
                        result_text = event.get("summary", "")
                        # Clear activity on completion
                        await manager.update_sub_agent_activity(parent_session_id, instance_id, None)
                        # DON'T break here - continue iterating to get "end" event
                        # This ensures _finalize_request runs and messages are persisted
                    elif event_type == "end":
                        # Generator fully completed, messages are now in SessionTracker
                        break
                    elif event_type == "error":
                        result_text = f"Error: {event.get('message', 'Unknown error')}"
                        logger.warning(f"Sub-agent {instance_id} returned error: {result_text}")
                        await manager.update_sub_agent_activity(parent_session_id, instance_id, None)
                        break  # Stop waiting for more events
                    elif event_type == "cancelled":
                        result_text = f"Cancelled: {event.get('reason', 'Request was cancelled')}"
                        logger.info(f"Sub-agent {instance_id} was cancelled: {result_text}")
                        await manager.update_sub_agent_activity(parent_session_id, instance_id, None)
                        break  # Stop waiting for more events

                # Save session with updated messages after execution
                user_id = manager._extract_user_id(parent_session_id, params)
                # Get actual LLM profile from agent configuration
                llm_profile = agent.agent_config.default_llm_profile
                await session_service.save_session(
                    agent=agent,
                    user_id=user_id,
                    session_id=instance_id,
                    agent_name=agent_type,
                    llm_profile=llm_profile,
                    was_new_session=False  # Updating existing session
                )
                logger.debug(f"Saved continued sub-agent session {instance_id} with messages")

                # Get current message count (after the new exchange)
                messages = sub_session_data.get("messages", [])
                new_message_count = len(messages) + 2  # existing + user + assistant

                # Update last_used timestamp, message_count, and reactivate status in parent metadata
                # IMPORTANT: Reactivate archived sub-agents by setting status back to "active"
                await manager.update_sub_session_metadata(
                    parent_session_id=parent_session_id,
                    sub_session_id=instance_id,
                    last_used=datetime.now(UTC).isoformat(),
                    message_count=new_message_count,
                    status="active"  # Reactivate archived sub-agents
                )

                if status:
                    # Same as the create path: an aborted continuation must
                    # not leave a green line behind.
                    if result_text.startswith(("Error:", "Cancelled:")):
                        await status.error(
                            f"Sub-agent {instance_id} ({agent_type}): {result_text}")
                    else:
                        await status.end(
                            f"Continued sub-agent {instance_id} (type: {agent_type}), "
                            f"{len(result_text)} chars")

                return {
                    "instance_id": instance_id,
                    "status": "completed",
                    "result": result_text,
                    "message_count": len(messages) + 2,  # existing + user + assistant
                    "agent_type": agent_type
                }
            finally:
                # ALWAYS release lock, even on error (CRITICAL for preventing deadlock)
                async with self._running_lock:
                    self._running_agents.discard(instance_id)
                logger.debug(f"Released running lock for sub-agent {instance_id}")

        except Exception as e:
            logger.exception(f"Error in continue_sub_agent: {e}")
            if status:
                await status.error(f"Failed to continue sub-agent: {str(e)}")
            return {
                "status": "error",
                "error": str(e)
            }

    async def _handle_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle 'list' operation - list all sub-agents.
        
        Only shows sub-agents created by this plugin instance (filtered by creator_plugin).
        """
        status = params.get("_status")
        try:
            parent_session_id = params.get("_session_id")
            if not parent_session_id:
                raise ValueError("No session context available")

            include_completed = params.get("include_completed", False)

            # Get session_service from params (supports both WebUI and tool calls)
            session_service = self._extract_session_service(params)

            # Get manager with injected dependencies (registry optional for list)
            registry = params.get("_registry")  # Optional - won't fail if missing
            manager = self._get_manager(session_service, registry)

            # List sub-sessions - filter by this plugin instance
            sub_sessions = await manager.list_sub_sessions(
                parent_session_id=parent_session_id,
                include_completed=include_completed,
                creator_plugin=self.name  # Only show sub-agents created by THIS instance
            )

            # Format response - load actual message count from each sub-session
            session_manager = session_service.session_manager
            user_id = manager._extract_user_id(parent_session_id)
            
            instances = []
            for metadata in sub_sessions:
                instance_id = metadata["instance_id"]
                sub_status = metadata["status"]

                activity_text = (metadata.get("current_activity") or "")
                activity_lower = activity_text.lower()
                looks_running = bool(activity_text) and (
                    "completed" not in activity_lower
                    and "cancelled" not in activity_lower
                    and "canceled" not in activity_lower
                    and "failed" not in activity_lower
                    and "error" not in activity_lower
                )
                
                # CRITICAL: If a sub-agent looks like it's running (status OR activity),
                # but we have no active async job (e.g., after server restart), mark interrupted.
                if sub_status in ("running", "pending") or (sub_status == "active" and looks_running):
                    if not self.is_agent_running(instance_id):
                        logger.warning(
                            f"Sub-agent {instance_id} has status='{sub_status}' in DB but no active task. "
                            f"Marking as 'interrupted' (likely server restart or crash)."
                        )
                        await manager.update_sub_session_metadata(
                            parent_session_id=parent_session_id,
                            sub_session_id=instance_id,
                            status="interrupted",
                            completed_at=datetime.now(UTC).isoformat(),
                            error="Server restarted or crashed while sub-agent was running"
                        )
                        # Clear stale activity to prevent WebUI from displaying RUNNING forever.
                        await manager.update_sub_agent_activity(
                            parent_session_id=parent_session_id,
                            sub_session_id=instance_id,
                            activity=None,
                        )
                        sub_status = "interrupted"
                
                # Get actual message count from sub-session (not from cached metadata)
                try:
                    sub_session_data = await session_manager.load_session(user_id, instance_id)
                    actual_message_count = len(sub_session_data.get("messages", []))
                except Exception:
                    # Fallback to metadata if sub-session can't be loaded
                    actual_message_count = metadata.get("message_count", 0)
                
                instances.append({
                    "instance_id": instance_id,
                    "agent_type": metadata["agent_type"],
                    "status": sub_status,
                    "created_at": metadata["created_at"],
                    "last_used": metadata["last_used"],
                    "task_summary": metadata["task_summary"],
                    "current_activity": metadata.get("current_activity"),
                    "activity_updated_at": metadata.get("activity_updated_at"),
                    "message_count": actual_message_count
                })

            # Emit descriptive status
            count = len(instances)
            if status:
                if count == 0:
                    await status.end("No sub-agents found")
                elif count == 1:
                    await status.end(f"Listed 1 sub-agent: {instances[0]['instance_id']}")
                else:
                    agent_ids = ", ".join(inst["instance_id"] for inst in instances[:3])
                    suffix = f", +{count-3} more" if count > 3 else ""
                    await status.end(f"Listed {count} sub-agents: {agent_ids}{suffix}")

            return {
                "instances": instances,
                "count": len(instances)
            }

        except Exception as e:
            logger.exception(f"Error in list_sub_agents: {e}")
            if status:
                await status.error(f"Failed to list sub-agents: {e}")
            return {
                "status": "error",
                "error": str(e)
            }

    async def _handle_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle 'delete' operation - archive sub-agent(s).
        
        Supports both single instance_id and multiple instance_ids (array).
        """
        status = params.get("_status") if params else None
        
        # Validate params
        if not params:
            if status:
                await status.error("Delete: params is None")
            return {"status": "error", "error": "Invalid parameters (None)"}
        
        try:
            # Support both single instance_id and array instance_ids
            instance_id = params.get("instance_id")
            instance_ids = params.get("instance_ids", [])
            
            # Build list of IDs to delete
            ids_to_delete: list[str] = []
            if instance_id:
                ids_to_delete.append(instance_id)
            if instance_ids:
                ids_to_delete.extend(instance_ids)
            
            # Deduplicate while preserving order
            seen: set[str] = set()
            unique_ids: list[str] = []
            for id_ in ids_to_delete:
                if id_ not in seen:
                    seen.add(id_)
                    unique_ids.append(id_)
            ids_to_delete = unique_ids
            
            if not ids_to_delete:
                if status:
                    await status.error("Delete: 'instance_id' or 'instance_ids' is required")
                return {"status": "error", "error": "Missing required parameter: 'instance_id' or 'instance_ids'"}
            
            parent_session_id = params.get("_session_id")
            if not parent_session_id:
                raise ValueError("No session context available")

            # Get manager with injected dependencies (registry optional for delete)
            registry = params.get("_registry")  # Optional
            session_service = self._extract_session_service(params)
            manager = self._get_manager(session_service, registry)
            user_id = manager._extract_user_id(parent_session_id, params)
            session_manager = manager._session_service.session_manager

            # Process each instance
            results: list[dict[str, Any]] = []
            archived_count = 0
            
            if status and len(ids_to_delete) > 1:
                await status.progress(f"Archiving {len(ids_to_delete)} sub-agents...")
            
            for sub_id in ids_to_delete:
                try:
                    # Verify ownership
                    try:
                        sub_session_data = await session_manager.load_session(user_id, sub_id)
                    except FileNotFoundError:
                        results.append({
                            "instance_id": sub_id,
                            "status": "error",
                            "error": f"Sub-agent '{sub_id}' not found"
                        })
                        continue

                    parent_link = sub_session_data.get("parent_session", {}).get("session_id")
                    if parent_link != parent_session_id:
                        logger.debug(
                            "Delete ownership mismatch for %s: parent_link=%s, expected=%s",
                            sub_id, parent_link, parent_session_id,
                        )
                        results.append({
                            "instance_id": sub_id,
                            "status": "error",
                            "error": (
                                f"Sub-agent '{sub_id}' does not belong to current session "
                                f"(actual_parent={parent_link}, caller={parent_session_id})"
                            ),
                        })
                        continue

                    # Get agent type for status message
                    agent_type = sub_session_data.get("agent_name", "unknown")

                    # Update parent metadata (mark as archived)
                    await manager.update_sub_session_metadata(
                        parent_session_id=parent_session_id,
                        sub_session_id=sub_id,
                        status="archived",
                        archived_at=datetime.now(UTC).isoformat()
                    )

                    results.append({
                        "instance_id": sub_id,
                        "status": "archived",
                        "agent_type": agent_type
                    })
                    archived_count += 1
                    
                except Exception as e:
                    results.append({
                        "instance_id": sub_id,
                        "status": "error",
                        "error": str(e)
                    })

            # Build response
            if len(ids_to_delete) == 1:
                # Single delete - return simple format for backwards compatibility
                result = results[0]
                if result["status"] == "archived":
                    if status:
                        await status.end(f"Archived sub-agent {result['instance_id']} (type: {result.get('agent_type', 'unknown')})")
                    return {
                        "instance_id": result["instance_id"],
                        "status": "archived",
                        "message": f"Sub-agent '{result['instance_id']}' archived successfully"
                    }
                else:
                    if status:
                        await status.error(result.get("error", "Unknown error"))
                    return result
            else:
                # Multiple deletes - return batch result
                failed_count = len(ids_to_delete) - archived_count
                if status:
                    if failed_count == 0:
                        await status.end(f"Archived {archived_count} sub-agents")
                    else:
                        await status.end(f"Archived {archived_count}/{len(ids_to_delete)} sub-agents ({failed_count} failed)")
                
                return {
                    "status": "completed",
                    "archived_count": archived_count,
                    "failed_count": failed_count,
                    "total": len(ids_to_delete),
                    "results": results
                }

        except Exception as e:
            logger.exception(f"Error in delete_sub_agent: {e}")
            if status:
                await status.error(f"Failed to delete sub-agent: {e}")
            return {
                "status": "error",
                "error": str(e)
            }

    async def _handle_info(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle 'info' operation - read a sub-agent's transcript, paged.

        Mirrors a file-reading tool's offset/limit model rather than a fixed
        snapshot: most calls just want the tail (the default -- omit
        'offset'), but a coordinator that needs to audit or resume precise
        context has to be able to page through the FULL transcript, not be
        stuck with a six-message, 200-character peephole. See 'window' in the
        response to know whether there is more to page through.
        """
        status = params.get("_status") if params else None

        # Validate params
        if not params:
            if status:
                await status.error("Info: params is None")
            return {"status": "error", "error": "Invalid parameters (None)"}

        try:
            parent_session_id = params.get("_session_id")
            if not parent_session_id:
                raise ValueError("No session context available")

            instance_id = params.get("instance_id")
            if not instance_id:
                if status:
                    await status.error("Info: 'instance_id' is required")
                return {"status": "error", "error": "Missing required parameter: 'instance_id'"}

            # Extract dependencies from injected params
            session_service = self._extract_session_service(params)

            # Get manager (registry is optional for info operation)
            registry = params.get("_registry")
            manager = self._get_manager(session_service, registry)
            user_id = manager._extract_user_id(parent_session_id, params)
            session_manager = manager._session_service.session_manager

            # Load sub-session data
            try:
                sub_session_data = await session_manager.load_session(user_id, instance_id)
            except FileNotFoundError:
                raise ValueError(f"Sub-agent '{instance_id}' not found")

            # Verify ownership
            parent_link = sub_session_data.get("parent_session", {}).get("session_id")
            if parent_link != parent_session_id:
                raise ValueError(
                    f"Sub-agent '{instance_id}' does not belong to current session "
                    f"(actual_parent={parent_link}, caller={parent_session_id})"
                )

            # Extract info
            messages = sub_session_data.get("messages", [])
            total = len(messages)
            agent_type = sub_session_data.get("agent_name")

            # Get metadata from parent
            parent_data = await session_manager.load_session(user_id, parent_session_id)
            metadata = parent_data.get("metadata", {}).get("sub_agents", {}).get(instance_id, {})

            window_messages, window_meta = self._paginate_messages(messages, params)

            if status:
                await status.end(
                    f"Retrieved info for {instance_id} (type: {agent_type}, "
                    f"showing {window_meta['returned']} of {total} messages)"
                )

            return {
                "instance_id": instance_id,
                "agent_type": agent_type,
                "status": metadata.get("status", "unknown"),
                "created_at": metadata.get("created_at"),
                "last_used": metadata.get("last_used"),
                "message_count": total,
                "task_summary": metadata.get("task_summary"),
                "messages": window_messages,
                "window": window_meta,
            }

        except Exception as e:
            logger.exception(f"Error in get_sub_agent_info: {e}")
            if status:
                await status.error(f"Failed to get sub-agent info: {e}")
            return {
                "status": "error",
                "error": str(e)
            }

    def _paginate_messages(
        self, messages: list[dict[str, Any]], params: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Slice *messages* per the caller's limit/offset/max_chars, formatted for an LLM.

        Two modes, chosen by whether 'offset' was given:
        - tail (default, no 'offset'): the most recent `limit` messages --
          what a coordinator almost always wants right after a run.
        - paging ('offset' >= 0): an absolute window from the start, so a
          caller can walk the ENTIRE transcript in order, `limit` at a time.
        """
        total = len(messages)

        limit = params.get("limit")
        if not isinstance(limit, int) or limit <= 0:
            limit = self.info_default_limit
        limit = min(limit, self.info_max_limit)

        offset = params.get("offset")
        if isinstance(offset, int) and offset >= 0:
            mode = "offset"
            start = min(offset, total)
        else:
            mode = "tail"
            start = max(0, total - limit)

        max_chars = params.get("max_chars")
        if not isinstance(max_chars, int):
            max_chars = self.info_default_max_chars

        window = messages[start:start + limit]
        formatted = [
            self._format_message(start + i, msg, max_chars)
            for i, msg in enumerate(window)
        ]

        return formatted, {
            "mode": mode,
            "start_index": start,
            "returned": len(formatted),
            "total": total,
            "has_more_before": start > 0,
            "has_more_after": (start + len(formatted)) < total,
        }

    @staticmethod
    def _clip_text(text: str, max_chars: int) -> str:
        """Truncate *text* to *max_chars*, saying so. `max_chars <= 0` means unlimited."""
        if max_chars > 0 and len(text) > max_chars:
            omitted = len(text) - max_chars
            return (
                f"{text[:max_chars]}\n"
                f"... [{omitted} more chars omitted -- raise 'max_chars' to see the rest]"
            )
        return text

    @classmethod
    def _format_message(cls, index: int, msg: dict[str, Any], max_chars: int) -> dict[str, Any]:
        """One transcript entry, readable on its own: text, and what a tool did.

        Assistant messages that only call a tool have empty `content` -- the
        tool name and arguments are the actually useful part, so they are
        surfaced explicitly rather than left for the caller to notice is
        missing. Multimodal content (text_file attachments, etc.) is
        flattened with the same helper token-counting uses elsewhere, so an
        image block does not come back as a raw Python repr.
        """
        entry: dict[str, Any] = {
            "index": index,
            "role": msg.get("role"),
            "content": cls._clip_text(extract_text_from_content(msg.get("content")), max_chars),
        }

        tool_calls = msg.get("tool_calls")
        if tool_calls:
            entry["tool_calls"] = [
                {
                    "name": (tc.get("function") or {}).get("name"),
                    "arguments": cls._clip_text(
                        str((tc.get("function") or {}).get("arguments", "")), max_chars
                    ),
                }
                for tc in tool_calls
                if isinstance(tc, dict)
            ]

        if msg.get("role") == "tool" and msg.get("name"):
            entry["tool_name"] = msg["name"]

        return entry

    # ========== Async Job Management Handlers ==========

    async def _execute_async_job(
        self,
        instance_id: str,
        params: dict[str, Any],
        agent_name: str,
        task: str,
        use_advanced_model: bool
    ) -> None:
        """Execute sub-agent in background and update job status.
        
        Args:
            instance_id: The sub_session_id (already created by caller)
            params: Full params dict with injected dependencies
            agent_name: Agent type name
            task: Task description
            use_advanced_model: Whether to use advanced model
        """
        import asyncio
        
        try:
            # Get dependencies
            registry = self._extract_registry(params)
            session_service = self._extract_session_service(params)
            manager = self._get_manager(session_service, registry)
            parent_session_id = params["_session_id"]

            logger.info(f"Async execution started for {instance_id}")

            # Mark as running (prevent concurrent execution)
            async with self._running_lock:
                if instance_id in self._running_agents:
                    raise ValueError(f"Sub-agent '{instance_id}' is already running")
                self._running_agents.add(instance_id)

            try:
                # Get agent and execute
                agent = registry.get(agent_name)
                if not agent:
                    raise ValueError(f"Agent '{agent_name}' not found in registry")

                agent._session_service = session_service
                user_id = manager._extract_user_id(parent_session_id, params)
                agent._session_tracker.set_session_metadata(instance_id, {
                    "user_id": user_id,
                    "agent_name": agent_name,
                    "llm_profile": getattr(agent.agent_config, 'default_llm_profile', 'normal')
                })

                # CRITICAL: Restore context_vars from sub-session to agent's template_vars
                # This inherits book_id, workflow_phase, etc. from parent session
                try:
                    sub_session_data = await session_service.session_manager.load_session(
                        user_id, instance_id
                    )
                    context_vars = sub_session_data.get("context_vars", {})
                    if context_vars:
                        # Set session-scoped template vars (session-isolated, no global mutation)
                        agent._session_tracker.set_session_template_vars(instance_id, context_vars)
                        logger.debug(
                            f"Inherited context_vars to async sub-agent template_vars: {list(context_vars.keys())}"
                        )
                except Exception as e:
                    logger.warning(f"Could not load context_vars for async sub-agent: {e}")

                # Execute (collect final result)
                result_text = ""
                parent_request_id = params.get("_request_id")
                sub_request_id = f"{parent_request_id}_async_{short_id(6)}" if parent_request_id else f"async_{short_id()}"

                # Register sub-request user mapping for admin dashboard
                _register_request_user(sub_request_id, user_id)

                async for event in agent.run_events(
                    task=task,
                    request_id=sub_request_id,
                    session_id=instance_id,
                    use_advanced_model=use_advanced_model
                ):
                    event_type = event.get("type")
                    
                    # Track activity
                    try:
                        if event_type == "thinking_delta":
                            await manager.update_sub_agent_activity(
                                parent_session_id, instance_id, "💭 Thinking..."
                            )
                        elif event_type == "mcp_call":
                            tool_name = event.get("action", "tool")
                            await manager.update_sub_agent_activity(
                                parent_session_id, instance_id, f"🔧 Running tool: {tool_name}"
                            )
                        elif event_type == "status":
                            status_msg = event.get("message", "Processing...")
                            phase = event.get("phase", "progress")
                            # Use different icons based on status phase
                            if phase == "error":
                                icon = "❌"
                            elif phase == "end":
                                icon = "✅"
                            else:
                                icon = "⚙️"
                            await manager.update_sub_agent_activity(
                                parent_session_id, instance_id, f"{icon} {status_msg}"
                            )
                    except Exception:
                        pass  # Don't fail on activity tracking

                    # Collect result
                    if event_type == "final":
                        result_text = event.get("summary", "")
                        await manager.update_sub_agent_activity(parent_session_id, instance_id, None)
                        # DON'T break here - continue iterating to get "end" event
                        # This ensures _finalize_request runs and messages are persisted
                    elif event_type == "end":
                        # Generator fully completed, messages are now in SessionTracker
                        break
                    elif event_type in ["error", "cancelled"]:
                        result_text = f"{event_type.capitalize()}: {event.get('message', event.get('reason', 'Unknown'))}"
                        await manager.update_sub_agent_activity(parent_session_id, instance_id, None)
                        break

                # Save session
                llm_profile = agent.agent_config.default_llm_profile
                await session_service.save_session(
                    agent=agent,
                    user_id=user_id,
                    session_id=instance_id,
                    agent_name=agent_name,
                    llm_profile=llm_profile,
                    was_new_session=True
                )

                # Update metadata (ensure status is active for async jobs)
                await manager.update_sub_session_metadata(
                    parent_session_id=parent_session_id,
                    sub_session_id=instance_id,
                    last_used=datetime.now(UTC).isoformat(),
                    status="active"  # Ensure active status for running async jobs
                )

                # Mark job as completed (keep in memory until polled once)
                async with self._async_jobs_lock:
                    if instance_id in self._async_jobs:
                        self._async_jobs[instance_id]["status"] = "completed"
                        self._async_jobs[instance_id]["result"] = result_text
                        self._async_jobs[instance_id]["completed_at"] = datetime.now(UTC).isoformat()
                        self._async_jobs[instance_id]["_awaiting_poll"] = True  # Will be removed after poll

                logger.info(f"Async execution completed for {instance_id}, awaiting result poll")

            finally:
                # Release lock
                async with self._running_lock:
                    self._running_agents.discard(instance_id)

        except asyncio.CancelledError:
            # Job was cancelled (e.g., parent agent interrupted)
            # Remove from memory tracking immediately
            async with self._async_jobs_lock:
                self._async_jobs.pop(instance_id, None)
            
            # CRITICAL: Persist cancelled status to DB to prevent polling loops on restart
            try:
                registry = self._extract_registry(params)
                session_service = self._extract_session_service(params)
                manager = self._get_manager(session_service, registry)
                parent_session_id = params.get("_session_id")
                
                if parent_session_id:
                    await manager.update_sub_session_metadata(
                        parent_session_id=parent_session_id,
                        sub_session_id=instance_id,
                        status="cancelled",
                        completed_at=datetime.now(UTC).isoformat()
                    )
                    logger.info(f"Persisted cancelled status for {instance_id} in DB after CancelledError")
            except Exception as persist_error:
                logger.warning(f"Failed to persist cancelled status for {instance_id}: {persist_error}")
            
            logger.info(f"Async execution cancelled for {instance_id}")
            raise

        except Exception as e:
            # Job failed - update status and remove from memory
            logger.exception(f"Async execution failed for {instance_id}: {e}")
            
            # CRITICAL: Update status to "failed" BEFORE removing from memory
            # This ensures wait() sees the correct status
            async with self._async_jobs_lock:
                if instance_id in self._async_jobs:
                    self._async_jobs[instance_id]["status"] = "failed"
                    self._async_jobs[instance_id]["error"] = str(e)
                    self._async_jobs[instance_id]["completed_at"] = datetime.now(UTC).isoformat()
                    # Keep in memory briefly so wait() can see the failed status
                    # It will be removed after poll
                    self._async_jobs[instance_id]["_awaiting_poll"] = True
            
            # Also persist failed status to DB
            try:
                registry = self._extract_registry(params)
                session_service = self._extract_session_service(params)
                manager = self._get_manager(session_service, registry)
                parent_session_id = params.get("_session_id")
                
                if parent_session_id:
                    await manager.update_sub_session_metadata(
                        parent_session_id=parent_session_id,
                        sub_session_id=instance_id,
                        status="failed",
                        completed_at=datetime.now(UTC).isoformat(),
                        error=str(e)
                    )
                    logger.info(f"Persisted failed status for {instance_id} in DB after exception")
            except Exception as persist_error:
                logger.warning(f"Failed to persist failed status for {instance_id}: {persist_error}")

    async def _handle_poll(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle 'poll' - check status of running sub-agent without blocking."""
        status = params.get("_status") if params else None
        
        # Validate params
        if not params:
            if status:
                await status.error("Poll: params is None")
            return {"status": "error", "error": "Invalid parameters (None)"}
        
        try:
            instance_id = params.get("instance_id")
            if not instance_id:
                if status:
                    await status.error("Poll: 'instance_id' is required")
                return {"status": "error", "error": "Missing required parameter: 'instance_id'"}

            # First check if async execution is tracked
            async with self._async_jobs_lock:
                if instance_id in self._async_jobs:
                    job = self._async_jobs[instance_id].copy()
                    # Ownership check: _async_jobs is a process-wide singleton dict
                    # keyed by guessable instance_id. Without this guard any
                    # session could poll another user's in-flight sub-agent and
                    # read its full result. Mirrors the DB-path check at the
                    # parent_link comparison used by continue/info/delete.
                    caller_session = params.get("_session_id")
                    job_parent = job.get("parent_session_id")
                    if caller_session and job_parent and job_parent != caller_session:
                        if status:
                            await status.error(f"Poll: {instance_id} not found")
                        return {"status": "error", "error": "Instance not found"}
                    job_status = job.get("status")

                    # If job is completed/failed/cancelled, remove from memory after returning
                    # Subsequent polls will load from DB (which is fine)
                    if job_status in ("completed", "failed", "cancelled"):
                        self._async_jobs.pop(instance_id, None)
                        logger.debug(f"Removed completed job {instance_id} from memory after poll")

                    # Remove task_handle from response (not serializable)
                    job.pop("task_handle", None)
                    job.pop("_awaiting_poll", None)
                    if status:
                        # A failed or cancelled job is not an END: that phase
                        # renders as a completed row.
                        if job_status in ("failed", "cancelled"):
                            await status.error(f"Poll: {instance_id} {job_status}")
                        else:
                            await status.end(f"Poll: {instance_id} status={job_status}")
                    return job

            # Not in async jobs - check if sub-agent exists in DB (may be completed or never ran async)
            # Only check DB if we have agent context (registry + session)
            try:
                registry = self._extract_registry(params)
                session_service = self._extract_session_service(params)
                manager = self._get_manager(session_service, registry)
                parent_session_id = params.get("_session_id")
                
                if not parent_session_id:
                    if status:
                        await status.error(f"Poll: {instance_id} not found (no session context)")
                    return {"status": "error", "error": "Instance not found in async tracking and no session context available"}

                # Check if sub-agent exists (only from this manager instance)
                sub_agents = await manager.list_sub_sessions(
                    parent_session_id, 
                    include_completed=False,
                    creator_plugin=self.name  # Only check sub-agents created by THIS instance
                )
                # Support both dict and Pydantic object access
                matching = [s for s in sub_agents if (s.get("instance_id") if isinstance(s, dict) else s.instance_id) == instance_id]
                
                if matching:
                    # Sub-agent exists but not in async tracking - it's completed
                    sub_agent = matching[0]
                    if status:
                        await status.end(f"Poll: {instance_id} completed")
                    return {
                        "instance_id": instance_id,
                        "status": "completed",
                        "agent_type": sub_agent.get("agent_type") if isinstance(sub_agent, dict) else sub_agent.agent_type,
                        "started_at": sub_agent.get("created_at") if isinstance(sub_agent, dict) else (sub_agent.created_at.isoformat() if sub_agent.created_at else None),
                        "completed_at": sub_agent.get("last_used") if isinstance(sub_agent, dict) else sub_agent.last_used,
                        "result": "Sub-agent execution completed (session persisted)",
                        "message": "Use 'info' operation to see conversation history"
                    }
                else:
                    if status:
                        await status.error(f"Poll: {instance_id} not found")
                    return {"status": "error", "error": f"Instance '{instance_id}' not found"}
            except RuntimeError:
                # No registry available - can only check async tracking (already done above)
                if status:
                    await status.error(f"Poll: {instance_id} not found in async tracking")
                return {"status": "error", "error": f"Instance '{instance_id}' not found in async tracking"}

        except KeyError as e:
            # Missing required parameter
            logger.error(f"Missing parameter in poll: {e}")
            if status:
                await status.error(f"Poll: Missing parameter {e}")
            return {"status": "error", "error": f"Missing required parameter: {e}"}
        except Exception as e:
            logger.exception(f"Unexpected error in poll: {e}")
            if status:
                await status.error(f"Poll error: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_wait(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle 'wait' - poll instance until completed or timeout."""
        import asyncio
        
        status_ctx = params.get("_status") if params else None
        
        # Validate params
        if not params:
            if status_ctx:
                await status_ctx.error("Wait: params is None")
            return {"status": "error", "error": "Invalid parameters (None)"}
        
        try:
            instance_id = params.get("instance_id")
            if not instance_id:
                if status_ctx:
                    await status_ctx.error("Wait: 'instance_id' is required")
                return {"status": "error", "error": "Missing required parameter: 'instance_id'"}
            
            timeout = self.default_wait_timeout  # Use configured timeout only

            if status_ctx:
                await status_ctx.progress(f"Waiting for {instance_id}...")

            # Check if async execution is tracked
            async with self._async_jobs_lock:
                is_async = instance_id in self._async_jobs

            if not is_async:
                # Not in async jobs - check DB immediately (may already be completed)
                # Silent poll: sharing the scope let the inner poll close it,
                # so wait's own verdict below never reached the status stream.
                poll_result = await self._handle_poll(_without_status(params))
                if poll_result.get("status") == "completed":
                    if status_ctx:
                        await status_ctx.end(f"Instance {instance_id} already completed")
                    return poll_result
                elif poll_result.get("status") == "error":
                    if status_ctx:
                        await status_ctx.error(f"Instance {instance_id} not found")
                    return poll_result

            # Async execution in progress - poll until done
            start_time = asyncio.get_event_loop().time()

            while True:
                # Check status
                async with self._async_jobs_lock:
                    if instance_id not in self._async_jobs:
                        # Async tracking lost - check DB (silent, see above)
                        poll_result = await self._handle_poll(_without_status(params))
                        if poll_result.get("status") in ["completed", "error"]:
                            # Say it ourselves: the inner poll is silent now,
                            # and the scope default would drop the instance id.
                            if status_ctx and poll_result.get("status") == "completed":
                                await status_ctx.end(f"Instance {instance_id} completed")
                            return poll_result
                        # Still not found - instance was deleted
                        if status_ctx:
                            await status_ctx.error(f"Instance {instance_id} disappeared during wait")
                        return {"status": "error", "error": f"Instance '{instance_id}' disappeared during wait"}

                    job = self._async_jobs[instance_id]
                    # Ownership check (see _handle_poll): the shared singleton
                    # _async_jobs is keyed by guessable instance_id; without this
                    # a session could wait on and read another user's sub-agent.
                    caller_session = params.get("_session_id")
                    job_parent = job.get("parent_session_id")
                    if caller_session and job_parent and job_parent != caller_session:
                        if status_ctx:
                            await status_ctx.error(f"Instance {instance_id} not found")
                        return {"status": "error", "error": "Instance not found"}
                    job_status = job["status"]

                    if job_status in ["completed", "failed", "cancelled"]:
                        result = job.copy()
                        result.pop("task_handle", None)

                        if status_ctx:
                            if job_status == "completed":
                                await status_ctx.end(f"Instance {instance_id} completed")
                            else:
                                await status_ctx.error(f"Instance {instance_id} {job_status}")

                        return result

                # Check timeout
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > timeout:
                    if status_ctx:
                        await status_ctx.error(f"Timeout waiting for {instance_id} ({elapsed:.1f}s)")
                    return {
                        "status": "error",
                        "error": f"Timeout waiting for instance '{instance_id}' (waited {elapsed:.1f}s)"
                    }

                # Wait before next poll
                await asyncio.sleep(0.5)

        except Exception as e:
            logger.exception(f"Error in wait: {e}")
            if status_ctx:
                await status_ctx.error(f"Failed to wait for instance: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_wait_all(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle 'wait_all' - wait for multiple sub-agents to complete."""
        import asyncio
        
        status_ctx = params.get("_status") if params else None
        
        # Validate params
        if not params:
            if status_ctx:
                await status_ctx.error("Wait_all: params is None")
            return {"status": "error", "error": "Invalid parameters (None)"}
        
        try:
            instance_ids = params.get("instance_ids")
            if not instance_ids:
                if status_ctx:
                    await status_ctx.error("Wait_all: 'instance_ids' is required")
                return {"status": "error", "error": "Missing required parameter: 'instance_ids'"}
            timeout = self.default_wait_timeout  # Use configured timeout only

            if not isinstance(instance_ids, list):
                # No status.error here: the returned error IS the message, and
                # call_with_status publishes it for any error result the
                # handler did not report itself. A second call would only
                # duplicate the text (mutation-checked: removing it keeps the
                # test green, so it carried nothing).
                return {"status": "error",
                        "error": f"Wait_all: 'instance_ids' must be a list "
                                 f"(got {type(instance_ids).__name__})"}

            if status_ctx:
                await status_ctx.progress(f"Waiting for {len(instance_ids)} instances...")

            # Wait for all instances
            wait_tasks = [
                self._handle_wait({
                    "instance_id": iid, 
                    "timeout": timeout,
                    "_registry": params.get("_registry"),
                    "_agent": params.get("_agent"),
                    "_session_id": params.get("_session_id"),
                    "_session_service": params.get("_session_service")
                })
                for iid in instance_ids
            ]

            results = await asyncio.gather(*wait_tasks, return_exceptions=True)

            # Format results
            formatted_results: list[dict[str, Any]] = []
            for i, result in enumerate(results):
                if isinstance(result, BaseException):
                    formatted_results.append({
                        "instance_id": instance_ids[i],
                        "status": "error",
                        "error": str(result)
                    })
                else:
                    # result is dict[str, Any] from wait_for_result
                    formatted_results.append(result)

            # Count statuses
            completed = sum(1 for r in formatted_results if r.get("status") == "completed")
            failed = sum(1 for r in formatted_results if r.get("status") in ["failed", "error"])

            if status_ctx:
                await status_ctx.end(f"Completed: {completed}, Failed: {failed} of {len(instance_ids)} instances")

            return {
                "results": formatted_results,
                "total": len(instance_ids),
                "completed": completed,
                "failed": failed
            }

        except Exception as e:
            logger.exception(f"Error in wait_all: {e}")
            if status_ctx:
                await status_ctx.error(f"Failed to wait for instances: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle 'cancel' - cancel running async sub-agent."""
        status_ctx = params.get("_status") if params else None
        
        # Validate params
        if not params:
            if status_ctx:
                await status_ctx.error("Cancel: params is None")
            return {"status": "error", "error": "Invalid parameters (None)"}
        
        try:
            instance_id = params.get("instance_id")
            if not instance_id:
                if status_ctx:
                    await status_ctx.error("Cancel: 'instance_id' is required")
                return {"status": "error", "error": "Missing required parameter: 'instance_id'"}

            async with self._async_jobs_lock:
                if instance_id not in self._async_jobs:
                    if status_ctx:
                        await status_ctx.error(f"Cancel: {instance_id} not found or not running async")
                    return {"status": "error", "error": f"Instance '{instance_id}' not found or not running async"}

                job = self._async_jobs[instance_id]
                task_handle = job.get("task_handle")
                parent_session_id = job.get("parent_session_id")

                # Ownership check (see _handle_poll): cancel is a control/write
                # operation on the shared singleton _async_jobs - without this a
                # session could kill (DoS) another user's in-flight sub-agent.
                caller_session = params.get("_session_id")
                if caller_session and parent_session_id and parent_session_id != caller_session:
                    if status_ctx:
                        await status_ctx.error(f"Cancel: {instance_id} not found")
                    return {"status": "error", "error": "Instance not found"}

                if job["status"] not in ["pending", "running"]:
                    if status_ctx:
                        await status_ctx.error(f"Cancel: {instance_id} has status '{job['status']}'")
                    return {
                        "status": "error",
                        "error": f"Cannot cancel instance with status '{job['status']}'"
                    }

                if task_handle and not task_handle.done():
                    task_handle.cancel()
                    logger.info(f"Cancelled async execution of {instance_id}")

                job["status"] = "cancelled"
                job["completed_at"] = datetime.now(UTC).isoformat()

            # CRITICAL: Update session metadata in database to persist cancelled status
            # This prevents polling loops when parent agent restarts
            if parent_session_id:
                try:
                    registry = self._extract_registry(params)
                    session_service = self._extract_session_service(params)
                    manager = self._get_manager(session_service, registry)
                    
                    await manager.update_sub_session_metadata(
                        parent_session_id=parent_session_id,
                        sub_session_id=instance_id,
                        status="cancelled",
                        completed_at=datetime.now(UTC).isoformat()
                    )
                    logger.info(f"Persisted cancelled status for {instance_id} in DB")
                except Exception as e:
                    logger.warning(f"Failed to persist cancelled status for {instance_id}: {e}")

            if status_ctx:
                await status_ctx.end(f"Cancelled {instance_id}")

            return {
                "instance_id": instance_id,
                "status": "cancelled",
                "message": "Sub-agent cancelled successfully"
            }

        except Exception as e:
            logger.exception(f"Error in cancel: {e}")
            if status_ctx:
                await status_ctx.error(f"Failed to cancel instance: {e}")
            return {"status": "error", "error": str(e)}

    # ========== End Async Job Management ==========

    def _is_agent_allowed(self, agent_name: str) -> bool:
        """Check if agent is allowed by this manager instance (ignoring phase filtering).

        Args:
            agent_name: Agent instance name (e.g., 'coding_agent', 'meta_agent')

        Returns:
            True if agent is allowed, False otherwise
        """
        # Check blacklist first
        if agent_name in self.blocked_agents:
            logger.debug(f"Agent '{agent_name}' blocked by blacklist")
            return False

        # Check whitelist
        if '*' in self.allowed_agents:
            return True

        # Exact match
        if agent_name in self.allowed_agents:
            return True

        # Glob pattern matching
        import fnmatch
        for pattern in self.allowed_agents:
            if fnmatch.fnmatch(agent_name, pattern):
                logger.debug(f"Agent '{agent_name}' matched pattern '{pattern}'")
                return True

        logger.debug(f"Agent '{agent_name}' not in allowed list: {self.allowed_agents}")
        return False

    def _get_current_phase(self, params: dict[str, Any]) -> Optional[str]:
        """Get current workflow phase from session template vars or agent config default.
        
        Args:
            params: Tool parameters with _agent reference
            
        Returns:
            Current phase value (from session vars or agent config default) or None
        """
        if not self.phase_filtering_enabled:
            return None
            
        try:
            agent = params.get("_agent")
            session_id = params.get("_session_id")
            if agent and session_id and hasattr(agent, '_session_tracker'):
                # First try session-scoped template vars (set via set_context)
                session_vars = agent._session_tracker.get_session_template_vars(session_id)
                phase = session_vars.get(self.phase_variable)
                if phase:
                    return phase
            
            # Fallback to agent config default (e.g., workflow_phase: "planning" in yaml)
            if agent and hasattr(agent, 'agent_config') and agent.agent_config:
                if hasattr(agent.agent_config, 'template_vars') and agent.agent_config.template_vars:
                    default_phase = agent.agent_config.template_vars.get(self.phase_variable)
                    if default_phase:
                        logger.debug(f"Using default phase from agent_config: {default_phase}")
                        return default_phase
        except Exception as e:
            logger.debug(f"Could not get phase from session vars: {e}")
        
        return None

    def _get_phase_allowed_agents(self, params: dict[str, Any]) -> Optional[List[str]]:
        """Get list of allowed agents for the current workflow phase.
        
        Args:
            params: Tool parameters with _agent reference
            
        Returns:
            List of allowed agents for current phase, or None if no phase filtering
        """
        if not self.phase_filtering_enabled:
            return None
            
        current_phase = self._get_current_phase(params)
        if not current_phase:
            return None
            
        phase_agents = self.phase_agents.get(current_phase)
        if phase_agents:
            logger.debug(f"Phase '{current_phase}' allows agents: {phase_agents}")
            return phase_agents
            
        # Check for default/fallback
        default_agents = self.phase_agents.get("_default")
        if default_agents is not None:
            if not default_agents:  # Empty list = all allowed_agents
                return None  # Return None to use all allowed_agents
            return default_agents
            
        return None

    def _is_agent_allowed_for_phase(self, agent_name: str, phase_allowed: Optional[List[str]]) -> bool:
        """Check if agent is allowed considering both base allowed_agents and phase filtering.
        
        Args:
            agent_name: Agent instance name
            phase_allowed: List of agents allowed for current phase, or None
            
        Returns:
            True if agent is allowed
        """
        # First check base allowed_agents
        if not self._is_agent_allowed(agent_name):
            return False
            
        # If no phase filtering active, base check is sufficient
        if phase_allowed is None:
            return True
            
        # Phase filtering active - check if agent in phase list
        return agent_name in phase_allowed

    # =========================================================================
    # Hook Implementation - Pre-LLM Call
    # =========================================================================

    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """Inject sub-agent context into system prompt before LLM call.

        This hook adds information about active sub-agents to the conversation
        context, allowing the coordinator agent to be aware of its sub-agents.

        Args:
            context: Hook context with session_id and messages

        Returns:
            HookResult with modified=True if context was injected
        """
        try:
            from plugins.sub_agent_manager.hooks import SubAgentContextInjector

            # Get session_service from agent in context
            # IMPORTANT: Don't cache the injector! Each agent has its own session_service
            if not context.agent or not hasattr(context.agent, '_session_service') or not context.agent._session_service:
                agent_info = f"name={context.agent.name}" if context.agent and hasattr(context.agent, 'name') else "unknown"
                has_attr = hasattr(context.agent, '_session_service') if context.agent else False
                is_none = context.agent._session_service is None if (context.agent and has_attr) else True
                logger.warning(
                    f"[SubAgentManager] No session_service available from agent ({agent_info}, "
                    f"has_attr={has_attr}, is_none={is_none}), skipping hook"
                )
                return HookResult(success=True, modified=False, context=context)

            session_service = context.agent._session_service
            manager = self._get_manager(session_service, registry=None)  # No registry needed for hooks

            # Get hook-specific configuration from plugin_config
            plugin_config = getattr(self.mcp_config, 'plugin_config', {})
            hooks_config = plugin_config.get("hooks", {})
            hook_config = hooks_config.get("inject_sub_agent_context", {})

            # Create fresh injector for this call (each agent has different session_service)
            # Pass self.name so injector only shows sub-agents from THIS manager instance
            # Pass allowed_agents for display in the hook message
            # Pass phase_filtering config from server (top-level config, not hook_config)
            phase_filtering_config = {
                'enabled': self.phase_filtering_enabled,
                'phase_variable': self.phase_variable,
                'phase_agents': self.phase_agents
            }
            injector = SubAgentContextInjector(
                manager, 
                self.name, 
                hook_config, 
                self.allowed_agents,
                phase_filtering_config
            )

            # Delegate to injector
            return await injector.inject_sub_agent_context(context)

        except Exception as e:
            logger.error(f"[SubAgentManager] Hook execution failed: {e}", exc_info=True)
            return HookResult(
                success=False,
                modified=False,
                context=context,
                metadata={"error": str(e)}
            )
