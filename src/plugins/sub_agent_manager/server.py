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
        
        # Agent filtering (multi-instance support - by instance name, not type)
        self.allowed_agents = list(getattr(mcp_config, 'allowed_agents', ['*']))
        self.blocked_agents = list(getattr(mcp_config, 'blocked_agents', []))
        
        # Track running sub-agent instances to prevent concurrent execution
        # Format: {sub_session_id: True}
        self._running_agents: set[str] = set()
        self._running_lock = __import__('asyncio').Lock()
    
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
                    # Check _mcp_public flag (visibility control)
                    if hasattr(srv, '_mcp_public') and not srv._mcp_public:
                        continue  # Skip visibility='tool' or 'private'
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
        return SubAgentManager(session_service, registry, self.max_nesting_depth)
    
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
        
        if not operation:
            return {"status": "error", "error": "Missing 'operation' parameter"}
        
        try:
            if operation == "create":
                return await self._handle_create(params)
            elif operation == "continue":
                return await self._handle_continue(params)
            elif operation == "list":
                return await self._handle_list(params)
            elif operation == "info":
                return await self._handle_info(params)
            elif operation == "delete":
                return await self._handle_delete(params)
            else:
                return {"status": "error", "error": f"Unknown operation: {operation}"}
        except Exception as e:
            logger.exception(f"Error in manage_sub_agent ({operation}): {e}")
            return {"status": "error", "error": str(e)}
    
    async def _handle_create(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle 'create' operation - create and execute new sub-agent."""
        try:
            # Extract parameters
            agent_name = params["agent_type"]  # This is actually the agent instance name
            task = params["task"]
            instance_label = params.get("instance_label")
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
            
            # Get status context
            status = params.get("_status")
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
            
            # Check if sub-agent is already running (prevent concurrent execution)
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
                    session_id=sub_session_id
                    # Note: config_overrides would go here if Agent.run_events supported them
                    # For now, sub-agent uses its default configuration
                ):
                    # Collect final result (event type is "final" not "result")
                    if event.get("type") == "final":
                        result_text = event.get("summary", "")
                
                # Update metadata after execution
                await manager.update_sub_session_metadata(
                    parent_session_id=parent_session_id,
                    sub_session_id=sub_session_id,
                    last_used=datetime.now(UTC).isoformat()
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
                # ALWAYS release lock, even on error
                async with self._running_lock:
                    self._running_agents.discard(sub_session_id)
            
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
        try:
            # Extract parameters
            instance_id = params["instance_id"]
            message = params["message"]
            
            # Get parent session ID from injected context
            parent_session_id = params.get("_session_id")
            if not parent_session_id:
                raise ValueError("No session context available")
            
            # Get status context
            status = params.get("_status")
            
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
                    session_id=instance_id  # Continue existing session
                ):
                    # Collect final result (event type is "final" not "result")
                    if event.get("type") == "final":
                        result_text = event.get("summary", "")
                
                # Update last_used timestamp
                await manager.update_sub_session_metadata(
                    parent_session_id=parent_session_id,
                    sub_session_id=instance_id,
                    last_used=datetime.now(UTC).isoformat()
                )
                
                # Get current message count
                messages = sub_session_data.get("messages", [])
                
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
                # ALWAYS release lock, even on error
                async with self._running_lock:
                    self._running_agents.discard(instance_id)
            
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
            
            # Format response
            instances = []
            for metadata in sub_sessions:
                instances.append({
                    "instance_id": metadata["instance_id"],
                    "agent_type": metadata["agent_type"],
                    "status": metadata["status"],
                    "created_at": metadata["created_at"],
                    "last_used": metadata["last_used"],
                    "task_summary": metadata["task_summary"]
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
                logger.warning("[SubAgentManager] No session_service available from agent, skipping hook")
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
