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
    from .hooks import SubAgentContextInjector

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
        
        # Initialize manager (will be lazy-loaded when first tool is called)
        self._manager: SubAgentManager | None = None
        
        # Initialize hook injector (lazy-loaded)
        self._hook_injector: SubAgentContextInjector | None = None
        
        logger.info(
            f"SubAgentManagerServer '{name}' initialized - "
            f"max_sub_agents={self.max_sub_agents}, max_history={self.max_history}, "
            f"max_nesting_depth={self.max_nesting_depth}, "
            f"allowed_agents={self.allowed_agents}, blocked_agents={self.blocked_agents}"
        )
    
    def _get_manager(self) -> SubAgentManager:
        """Lazy-load SubAgentManager."""
        if self._manager is None:
            # Create session_service locally with default storage path
            from pathlib import Path
            from agent_system.services.session_manager import SessionManager
            from agent_system.services.session_service import SessionService
            from agent_system.mcp.base import MCPRegistry
            
            # Use default storage path (same as CLI)
            storage_path = Path(__file__).parents[3] / "data" / "sessions"
            session_manager = SessionManager(storage_path=str(storage_path))
            session_service = SessionService(session_manager)
            logger.debug(f"Created SessionService with storage_path={storage_path}")
            
            # Get or create registry
            registry = getattr(self.system_config, 'registry', None)
            if registry is None:
                # Create a new registry instance (will access shared plugin servers)
                registry = MCPRegistry()
                logger.debug("Created new MCPRegistry instance")
            
            self._manager = SubAgentManager(session_service, registry, self.max_nesting_depth)
            logger.debug("SubAgentManager lazy-loaded")
        
        return self._manager
    
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
            
            # Get manager
            manager = self._get_manager()
            
            # Create sub-session
            sub_session_id = await manager.create_sub_session(
                parent_session_id=parent_session_id,
                agent_type=agent_name,
                initial_message=task,
                instance_label=instance_label
            )
            
            logger.info(f"Created sub-session {sub_session_id} for parent {parent_session_id}")
            
            # Get agent from registry
            agent = self.registry.get(agent_name)
            if not agent:
                raise ValueError(f"Agent '{agent_name}' not found in registry")
            
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
                # Collect final result
                if event.get("type") == "result":
                    result_text = event.get("text", "")
            
            # Update metadata after execution
            await manager.update_sub_session_metadata(
                parent_session_id=parent_session_id,
                sub_session_id=sub_session_id,
                last_used=datetime.now(UTC).isoformat()
            )
            
            if status:
                await status.end(f"Sub-agent {sub_session_id} created and executed")
            
            return {
                "instance_id": sub_session_id,
                "status": "completed",
                "result": result_text,
                "message_count": 2,  # user + assistant
                "agent_type": agent_name
            }
            
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
            
            # Get manager and verify instance
            manager = self._get_manager()
            user_id = manager._extract_user_id(parent_session_id)
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
            agent = self.registry.get(agent_type)
            if not agent:
                raise ValueError(f"Agent type '{agent_type}' not found")
            
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
                if event.get("type") == "result":
                    result_text = event.get("text", "")
            
            # Update last_used timestamp
            await manager.update_sub_session_metadata(
                parent_session_id=parent_session_id,
                sub_session_id=instance_id,
                last_used=datetime.now(UTC).isoformat()
            )
            
            # Get current message count
            messages = sub_session_data.get("messages", [])
            
            if status:
                await status.end(f"Sub-agent {instance_id} continued")
            
            return {
                "instance_id": instance_id,
                "status": "completed",
                "result": result_text,
                "message_count": len(messages) + 2,  # existing + user + assistant
                "agent_type": agent_type
            }
            
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
        try:
            parent_session_id = params.get("_session_id")
            if not parent_session_id:
                raise ValueError("No session context available")
            
            include_completed = params.get("include_completed", False)
            
            # Get manager
            manager = self._get_manager()
            
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
            
            return {
                "instances": instances,
                "count": len(instances)
            }
            
        except Exception as e:
            logger.exception(f"Error in list_sub_agents: {e}")
            return {
                "status": "error",
                "error": str(e)
            }
    
    async def _handle_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle 'delete' operation - archive sub-agent."""
        try:
            parent_session_id = params.get("_session_id")
            if not parent_session_id:
                raise ValueError("No session context available")
            
            instance_id = params["instance_id"]
            
            # Get manager
            manager = self._get_manager()
            user_id = manager._extract_user_id(parent_session_id)
            session_manager = manager._session_service.session_manager
            
            # Verify ownership
            try:
                sub_session_data = await session_manager.load_session(user_id, instance_id)
            except FileNotFoundError:
                raise ValueError(f"Sub-agent '{instance_id}' not found")
            
            parent_link = sub_session_data.get("parent_session", {}).get("session_id")
            if parent_link != parent_session_id:
                raise ValueError(f"Sub-agent '{instance_id}' does not belong to current session")
            
            # Update parent metadata (mark as archived)
            await manager.update_sub_session_metadata(
                parent_session_id=parent_session_id,
                sub_session_id=instance_id,
                status="archived",
                archived_at=datetime.now(UTC).isoformat()
            )
            
            # Note: We keep the session file (don't delete) for audit trail
            
            return {
                "instance_id": instance_id,
                "status": "archived",
                "message": f"Sub-agent '{instance_id}' archived successfully"
            }
            
        except Exception as e:
            logger.exception(f"Error in delete_sub_agent: {e}")
            return {
                "status": "error",
                "error": str(e)
            }
    
    async def _handle_info(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle 'info' operation - get detailed sub-agent info."""
        try:
            parent_session_id = params.get("_session_id")
            if not parent_session_id:
                raise ValueError("No session context available")
            
            instance_id = params["instance_id"]
            
            # Get manager
            manager = self._get_manager()
            user_id = manager._extract_user_id(parent_session_id)
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
            # Lazy-load hook injector
            if self._hook_injector is None:
                from plugins.sub_agent_manager.hooks import SubAgentContextInjector
                manager = self._get_manager()
                # Get hook-specific configuration from plugin_config
                plugin_config = getattr(self.mcp_config, 'plugin_config', {})
                hooks_config = plugin_config.get("hooks", {})
                hook_config = hooks_config.get("inject_sub_agent_context", {})
                self._hook_injector = SubAgentContextInjector(manager, hook_config)
                logger.debug("SubAgentContextInjector lazy-loaded")
            
            # Delegate to injector
            return await self._hook_injector.inject_sub_agent_context(context)
            
        except Exception as e:
            logger.error(f"[SubAgentManager] Hook execution failed: {e}", exc_info=True)
            return HookResult(
                success=False,
                modified=False,
                context=context,
                metadata={"error": str(e)}
            )
