"""Sub-Agent Manager MCP Server implementation."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.hooks.plugin_hook import PluginHook, HookContext, HookResult
from agent_system.utils.id import short_id

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

from plugins.sub_agent_manager.manager import SubAgentManager

logger = logging.getLogger(__name__)


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

        # Agent filtering (multi-instance support - by instance name, not type)
        self.allowed_agents = list(getattr(mcp_config, 'allowed_agents', ['*']))
        self.blocked_agents = list(getattr(mcp_config, 'blocked_agents', []))

        # Track running sub-agent instances to prevent concurrent execution
        # Format: {sub_session_id: True}
        self._running_agents: set[str] = set()
        self._running_lock = __import__('asyncio').Lock()
        
        # Track async jobs by instance_id: {instance_id: {task, status, started_at, result, error}}
        self._async_jobs: dict[str, dict[str, Any]] = {}
        self._async_jobs_lock = __import__('asyncio').Lock()

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
            'allowed_agents': agent_names
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

        # NOTE: Registry and SessionService will be injected via params during tool/hook calls
        # by the ToolExecutionManager or via HookContext.agent

        logger.info(
            f"SubAgentManagerServer '{self.name}' initialized - "
            f"max_sub_agents={self.max_sub_agents}, max_history={self.max_history}, "
            f"max_nesting_depth={self.max_nesting_depth}, "
            f"allowed_agents={self.allowed_agents}, blocked_agents={self.blocked_agents}"
        )

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
            self.max_sub_agents_per_type
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

    async def manage_sub_agent(self, params: dict[str, Any]) -> dict[str, Any]:
        """Unified handler for all sub-agent operations.

        Dispatches to operation-specific handlers based on params["operation"].
        """
        operation = params.get("operation")
        status = params.get("_status")  # Get status early for error reporting

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
            # Extract parameters
            agent_name = params["agent_type"]  # This is actually the agent instance name
            task = params["task"]
            instance_label = params.get("instance_label")
            use_advanced_model = params.get("use_advanced_model", False)
            blocking = params.get("blocking", True)  # NEW: default to blocking behavior
            # Note: config_overrides would be used here when Agent.run_events supports them
            # For now, sub-agent uses its default configuration

            # Validate agent is allowed by this manager instance
            if not self._is_agent_allowed(agent_name):
                allowed_str = ', '.join(self.allowed_agents)
                raise ValueError(
                    f"Agent '{agent_name}' not allowed by this sub-agent manager. "
                    f"Allowed agents: {allowed_str}"
                )

            # Get parent session ID from injected context
            parent_session_id = params.get("_session_id")
            if not parent_session_id:
                raise ValueError("No session context available - this tool must be called from an agent")
            if status:
                await status.progress(f"Creating sub-agent: {agent_name}")

            # Get manager with injected dependencies
            registry = self._extract_registry(params)
            session_service = self._extract_session_service(params)
            manager = self._get_manager(session_service, registry)

            # Create sub-session (pass params for user_id extraction)
            sub_session_id = await manager.create_sub_session(
                parent_session_id=parent_session_id,
                agent_type=agent_name,
                initial_message=task,
                instance_label=instance_label,
                params=params  # Pass params for user_id extraction
            )

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
                            await manager.update_sub_agent_activity(
                                parent_session_id, sub_session_id, f"⚙️ {status_msg}"
                            )
                    except Exception as activity_err:
                        # Don't fail execution if activity tracking fails
                        logger.debug(f"Activity tracking failed: {activity_err}")
                    
                    # Collect final result (can be "final", "error", or "cancelled")
                    if event_type == "final":
                        result_text = event.get("summary", "")
                        # Clear activity on completion
                        await manager.update_sub_agent_activity(parent_session_id, sub_session_id, None)
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
                    await status.end(f"Created sub-agent {sub_session_id} (type: {agent_name})")

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
        status = params.get("_status")
        
        try:
            # Extract parameters
            instance_id = params["instance_id"]
            message = params["message"]
            use_advanced_model = params.get("use_advanced_model", False)

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
                raise ValueError(f"Sub-agent '{instance_id}' does not belong to current session")

            # Get agent type from session data
            agent_type = sub_session_data.get("agent_name")
            agent = registry.get(agent_type)
            if not agent:
                raise ValueError(f"Agent type '{agent_type}' not found")

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
                            await manager.update_sub_agent_activity(
                                parent_session_id, instance_id, f"⚙️ {status_msg}"
                            )
                    except Exception as activity_err:
                        # Don't fail execution if activity tracking fails
                        logger.debug(f"Activity tracking failed: {activity_err}")
                    
                    # Collect final result (can be "final", "error", or "cancelled")
                    if event_type == "final":
                        result_text = event.get("summary", "")
                        # Clear activity on completion
                        await manager.update_sub_agent_activity(parent_session_id, instance_id, None)
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

                # Update last_used timestamp and message_count in parent metadata
                await manager.update_sub_session_metadata(
                    parent_session_id=parent_session_id,
                    sub_session_id=instance_id,
                    last_used=datetime.now(UTC).isoformat(),
                    message_count=new_message_count
                )

                if status:
                    await status.end(f"Continued sub-agent {instance_id} (type: {agent_type})")

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
        """Handle 'list' operation - list all sub-agents."""
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

            # List sub-sessions
            sub_sessions = await manager.list_sub_sessions(
                parent_session_id=parent_session_id,
                include_completed=include_completed
            )

            # Format response - load actual message count from each sub-session
            session_manager = session_service.session_manager
            user_id = manager._extract_user_id(parent_session_id)
            
            instances = []
            for metadata in sub_sessions:
                instance_id = metadata["instance_id"]
                
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
                    "status": metadata["status"],
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
        """Handle 'delete' operation - archive sub-agent."""
        status = params.get("_status")
        try:
            parent_session_id = params.get("_session_id")
            if not parent_session_id:
                raise ValueError("No session context available")

            instance_id = params["instance_id"]

            # Get manager with injected dependencies (registry optional for delete)
            registry = params.get("_registry")  # Optional
            session_service = self._extract_session_service(params)
            manager = self._get_manager(session_service, registry)
            user_id = manager._extract_user_id(parent_session_id, params)
            session_manager = manager._session_service.session_manager

            # Verify ownership
            try:
                sub_session_data = await session_manager.load_session(user_id, instance_id)
            except FileNotFoundError:
                raise ValueError(f"Sub-agent '{instance_id}' not found")

            parent_link = sub_session_data.get("parent_session", {}).get("session_id")
            if parent_link != parent_session_id:
                raise ValueError(f"Sub-agent '{instance_id}' does not belong to current session")

            # Get agent type for status message
            agent_type = sub_session_data.get("agent_name", "unknown")

            # Update parent metadata (mark as archived)
            await manager.update_sub_session_metadata(
                parent_session_id=parent_session_id,
                sub_session_id=instance_id,
                status="archived",
                archived_at=datetime.now(UTC).isoformat()
            )

            # Note: We keep the session file (don't delete) for audit trail

            if status:
                await status.end(f"Archived sub-agent {instance_id} (type: {agent_type})")

            return {
                "instance_id": instance_id,
                "status": "archived",
                "message": f"Sub-agent '{instance_id}' archived successfully"
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
        """Handle 'info' operation - get detailed sub-agent info."""
        status = params.get("_status")
        try:
            parent_session_id = params.get("_session_id")
            if not parent_session_id:
                raise ValueError("No session context available")

            instance_id = params["instance_id"]

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
                raise ValueError(f"Sub-agent '{instance_id}' does not belong to current session")

            # Extract info
            messages = sub_session_data.get("messages", [])
            agent_type = sub_session_data.get("agent_name")

            # Get metadata from parent
            parent_data = await session_manager.load_session(user_id, parent_session_id)
            metadata = parent_data.get("metadata", {}).get("sub_agents", {}).get(instance_id, {})

            # Get recent activity (last 3 exchanges = 6 messages)
            recent_messages = []
            for msg in messages[-6:]:
                recent_messages.append({
                    "role": msg.get("role"),
                    "content": msg.get("content", "")[:200]  # Truncate long messages
                })

            if status:
                await status.end(f"Retrieved info for {instance_id} (type: {agent_type}, {len(messages)} messages)")

            return {
                "instance_id": instance_id,
                "agent_type": agent_type,
                "status": metadata.get("status", "unknown"),
                "created_at": metadata.get("created_at"),
                "last_used": metadata.get("last_used"),
                "message_count": len(messages),
                "task_summary": metadata.get("task_summary"),
                "recent_activity": recent_messages
            }

        except Exception as e:
            logger.exception(f"Error in get_sub_agent_info: {e}")
            if status:
                await status.error(f"Failed to get sub-agent info: {e}")
            return {
                "status": "error",
                "error": str(e)
            }

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

                # Execute (collect final result)
                result_text = ""
                parent_request_id = params.get("_request_id")
                sub_request_id = f"{parent_request_id}_async_{short_id(6)}" if parent_request_id else f"async_{short_id()}"

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
                            await manager.update_sub_agent_activity(
                                parent_session_id, instance_id, f"⚙️ {status_msg}"
                            )
                    except Exception:
                        pass  # Don't fail on activity tracking

                    # Collect result
                    if event_type == "final":
                        result_text = event.get("summary", "")
                        await manager.update_sub_agent_activity(parent_session_id, instance_id, None)
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

                # Update metadata
                await manager.update_sub_session_metadata(
                    parent_session_id=parent_session_id,
                    sub_session_id=instance_id,
                    last_used=datetime.now(UTC).isoformat()
                )

                # Mark job as completed
                async with self._async_jobs_lock:
                    if instance_id in self._async_jobs:
                        self._async_jobs[instance_id]["status"] = "completed"
                        self._async_jobs[instance_id]["result"] = result_text
                        self._async_jobs[instance_id]["completed_at"] = datetime.now(UTC).isoformat()

                logger.info(f"Async execution completed for {instance_id}")

            finally:
                # Release lock
                async with self._running_lock:
                    self._running_agents.discard(instance_id)

        except asyncio.CancelledError:
            # Job was cancelled
            async with self._async_jobs_lock:
                if instance_id in self._async_jobs:
                    self._async_jobs[instance_id]["status"] = "cancelled"
                    self._async_jobs[instance_id]["completed_at"] = datetime.now(UTC).isoformat()
            logger.info(f"Async execution cancelled for {instance_id}")
            raise

        except Exception as e:
            # Job failed
            logger.exception(f"Async execution failed for {instance_id}: {e}")
            async with self._async_jobs_lock:
                if instance_id in self._async_jobs:
                    self._async_jobs[instance_id]["status"] = "failed"
                    self._async_jobs[instance_id]["error"] = str(e)
                    self._async_jobs[instance_id]["completed_at"] = datetime.now(UTC).isoformat()

    async def _handle_poll(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle 'poll' - check status of running sub-agent without blocking."""
        status = params.get("_status")
        try:
            instance_id = params["instance_id"]

            # First check if async execution is tracked
            async with self._async_jobs_lock:
                if instance_id in self._async_jobs:
                    job = self._async_jobs[instance_id].copy()
                    # Remove task_handle from response (not serializable)
                    job.pop("task_handle", None)
                    if status:
                        await status.end(f"Poll: {instance_id} status={job.get('status', 'unknown')}")
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

                # Check if sub-agent exists
                sub_agents = await manager.list_sub_sessions(parent_session_id, include_completed=False)
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
        
        status_ctx = params.get("_status")
        try:
            instance_id = params["instance_id"]
            timeout = params.get("timeout", 3600)  # Default 1 hour

            if status_ctx:
                await status_ctx.progress(f"Waiting for {instance_id}...")

            # Check if async execution is tracked
            async with self._async_jobs_lock:
                is_async = instance_id in self._async_jobs

            if not is_async:
                # Not in async jobs - check DB immediately (may already be completed)
                poll_result = await self._handle_poll(params)
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
                        # Async tracking lost - check DB
                        poll_result = await self._handle_poll(params)
                        if poll_result.get("status") in ["completed", "error"]:
                            return poll_result
                        # Still not found - instance was deleted
                        if status_ctx:
                            await status_ctx.error(f"Instance {instance_id} disappeared during wait")
                        return {"status": "error", "error": f"Instance '{instance_id}' disappeared during wait"}

                    job = self._async_jobs[instance_id]
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
        
        status_ctx = params.get("_status")
        try:
            instance_ids = params["instance_ids"]
            timeout = params.get("timeout", 3600)

            if not isinstance(instance_ids, list):
                return {"status": "error", "error": "instance_ids must be a list"}

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
            formatted_results = []
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    formatted_results.append({
                        "instance_id": instance_ids[i],
                        "status": "error",
                        "error": str(result)
                    })
                else:
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
        status_ctx = params.get("_status")
        try:
            instance_id = params["instance_id"]

            async with self._async_jobs_lock:
                if instance_id not in self._async_jobs:
                    if status_ctx:
                        await status_ctx.error(f"Cancel: {instance_id} not found or not running async")
                    return {"status": "error", "error": f"Instance '{instance_id}' not found or not running async"}

                job = self._async_jobs[instance_id]
                task_handle = job.get("task_handle")

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
        """Check if agent is allowed by this manager instance.

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
            injector = SubAgentContextInjector(manager, hook_config)

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
