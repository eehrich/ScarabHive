"""Sub-Agent Manager MCP Server implementation."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.utils.id import short_id

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

from plugins.sub_agent_manager.manager import SubAgentManager

logger = logging.getLogger(__name__)


class SubAgentManagerServer(SchemaBasedMCPServer):
    """MCP server for sub-agent management tools.
    
    Provides 5 tools for coordinator agents:
    - create_sub_agent: Create and execute initial task
    - continue_sub_agent: Continue with new message
    - list_sub_agents: List active instances
    - delete_sub_agent: Archive instance
    - get_sub_agent_info: Query detailed status
    """
    
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig) -> None:
        """Initialize SubAgentManagerServer.
        
        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            mcp_config: Plugin-specific configuration
        """
        super().__init__(name, system_config, mcp_config)
        
        # Configuration
        self.max_sub_agents = int(getattr(mcp_config, 'max_sub_agents_per_session', 10))
        self.max_history = int(getattr(mcp_config, 'max_message_history', 100))
        self.max_nesting_depth = int(getattr(mcp_config, 'max_nesting_depth', 5))
        
        # Initialize manager (will be lazy-loaded when first tool is called)
        self._manager: SubAgentManager | None = None
        
        logger.info(
            f"SubAgentManagerServer '{name}' initialized - "
            f"max_sub_agents={self.max_sub_agents}, max_history={self.max_history}, "
            f"max_nesting_depth={self.max_nesting_depth}"
        )
    
    def _get_manager(self) -> SubAgentManager:
        """Lazy-load SubAgentManager."""
        if self._manager is None:
            # Get session_service from system_config
            session_service = getattr(self.system_config, 'session_service', None)
            if session_service is None:
                raise RuntimeError("session_service not available in system_config")
            
            # Get registry
            registry = getattr(self.system_config, 'registry', None)
            if registry is None:
                raise RuntimeError("registry not available in system_config")
            
            self._manager = SubAgentManager(session_service, registry, self.max_nesting_depth)
            logger.debug("SubAgentManager lazy-loaded")
        
        return self._manager
    
    async def create_sub_agent(self, params: dict[str, Any]) -> dict[str, Any]:
        """Create new sub-agent instance and execute initial task.
        
        Handler for create_sub_agent tool.
        """
        try:
            # Extract parameters
            agent_type = params["agent_type"]
            task = params["task"]
            instance_label = params.get("instance_label")
            # Note: config_overrides would be used here when Agent.run_events supports them
            # For now, sub-agent uses its default configuration
            
            # Get parent session ID from injected context
            parent_session_id = params.get("_session_id")
            if not parent_session_id:
                raise ValueError("No session context available - this tool must be called from an agent")
            
            # Get status context
            status = params.get("_status")
            if status:
                await status.progress(f"Creating sub-agent: {agent_type}")
            
            # Get manager
            manager = self._get_manager()
            
            # Create sub-session
            sub_session_id = await manager.create_sub_session(
                parent_session_id=parent_session_id,
                agent_type=agent_type,
                initial_message=task,
                instance_label=instance_label
            )
            
            logger.info(f"Created sub-session {sub_session_id} for parent {parent_session_id}")
            
            # Get agent from registry
            agent = self.registry.get(agent_type)
            if not agent:
                raise ValueError(f"Agent type '{agent_type}' not found in registry")
            
            # Execute sub-agent with initial task (blocking)
            if status:
                await status.progress(f"Executing {agent_type} with initial task...")
            
            result_text = ""
            request_id = f"sub_{short_id()}"
            
            async for event in agent.run_events(
                task=task,
                request_id=request_id,
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
                "agent_type": agent_type
            }
            
        except Exception as e:
            logger.exception(f"Error in create_sub_agent: {e}")
            if status:
                await status.error(f"Failed to create sub-agent: {str(e)}")
            return {
                "status": "error",
                "error": str(e)
            }
    
    async def continue_sub_agent(self, params: dict[str, Any]) -> dict[str, Any]:
        """Continue existing sub-agent with new message.
        
        Handler for continue_sub_agent tool.
        """
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
            request_id = f"sub_cont_{short_id()}"
            
            async for event in agent.run_events(
                task=message,
                request_id=request_id,
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
    
    async def list_sub_agents(self, params: dict[str, Any]) -> dict[str, Any]:
        """List all sub-agents for current session.
        
        Handler for list_sub_agents tool.
        """
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
    
    async def delete_sub_agent(self, params: dict[str, Any]) -> dict[str, Any]:
        """Archive sub-agent instance.
        
        Handler for delete_sub_agent tool.
        """
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
    
    async def get_sub_agent_info(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get detailed info about a sub-agent.
        
        Handler for get_sub_agent_info tool.
        """
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
