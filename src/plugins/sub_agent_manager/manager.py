"""Sub-Agent Manager - Core lifecycle management."""

from __future__ import annotations

import asyncio
import logging
from agent_system.services.session_manager import SessionNotFoundError
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from agent_system.mcp.base import MCPRegistry
    from agent_system.services.session_service import SessionService

logger = logging.getLogger(__name__)


class SubAgentManager:
    """Manages lifecycle of sub-agent instances.
    
    This manager handles:
    - Creating sub-sessions via SessionManager
    - Linking sub-sessions to parent sessions
    - Listing and querying sub-sessions
    - Generating unique instance IDs
    
    CRITICAL: Does NOT extend SessionTracker. Uses SessionManager directly
    for all persistence operations.
    """
    
    def __init__(self, session_service: SessionService, registry: MCPRegistry, max_nesting_depth: int = 5):
        """Initialize SubAgentManager.
        
        Args:
            session_service: Session service for persistence
            registry: MCP registry for agent lookup
            max_nesting_depth: Maximum recursion depth for nested sub-agents
        """
        self._session_service = session_service
        self._registry = registry
        self._global_counter = 0  # Global counter for all sub-agents
        self._lock = asyncio.Lock()
        self.max_nesting_depth = max_nesting_depth
        
        logger.info(f"SubAgentManager initialized (max_nesting_depth={max_nesting_depth})")
    
    async def create_sub_session(
        self,
        parent_session_id: str,
        agent_type: str,
        initial_message: str,
        instance_label: Optional[str] = None,
        params: Optional[dict] = None
    ) -> str:
        """Create sub-session file via SessionManager.
        
        Args:
            parent_session_id: Parent coordinator session ID
            agent_type: Type of agent to create (e.g., "web_research_agent")
            initial_message: Initial task/message for sub-agent
            instance_label: Optional human-readable label
            params: Optional tool call params with injected _user_id
            
        Returns:
            Sub-session ID (format: sub_{agent_type}_{global_counter})
            
        Raises:
            ValueError: If parent session not found, agent type invalid, or max depth exceeded
        """
        # Extract user_id from params or session file
        user_id = self._extract_user_id(parent_session_id, params)
        session_manager = self._session_service.session_manager
        
        # Load parent session to check depth
        # If parent session doesn't exist yet (e.g., CLI ephemeral session),
        # create it automatically with minimal metadata
        try:
            parent_data = await session_manager.load_session(user_id, parent_session_id)
        except SessionNotFoundError:
            logger.info(
                f"Parent session {parent_session_id} not found in storage, creating it now"
            )
            # Create parent session with minimal metadata
            # This handles CLI/ephemeral sessions that haven't been saved yet
            await session_manager.create_session(
                user_id=user_id,
                session_id=parent_session_id,
                title="Coordinator Session",
                agent_name="unknown",  # Will be updated when parent saves
                llm_profile="default"
            )
            parent_data = await session_manager.load_session(user_id, parent_session_id)
        
        # Check nesting depth
        parent_depth = parent_data.get("depth", 1)
        child_depth = parent_depth + 1
        
        if child_depth > self.max_nesting_depth:
            raise ValueError(
                f"Maximum nesting depth ({self.max_nesting_depth}) exceeded. "
                f"Parent depth={parent_depth}, attempted child depth={child_depth}"
            )
        
        # Generate unique instance ID (short format)
        sub_session_id = await self._generate_instance_id(agent_type, instance_label)
        
        # Create session via existing SessionManager
        await session_manager.create_session(
            user_id=user_id,
            session_id=sub_session_id,
            title=f"{initial_message[:50]}..." if len(initial_message) > 50 else initial_message,
            agent_name=agent_type,
            llm_profile="default"  # Will be overridden by agent config
        )
        
        logger.info(
            f"Created sub-session: {sub_session_id} (parent: {parent_session_id}, depth: {child_depth})"
        )
        
        # Add parent link and depth to sub-session metadata
        session_data = await session_manager.load_session(user_id, sub_session_id)
        
        session_data["depth"] = child_depth
        
        if "parent_session" not in session_data:
            session_data["parent_session"] = {}
        
        session_data["parent_session"]["session_id"] = parent_session_id
        session_data["parent_session"]["created_at"] = datetime.now(UTC).isoformat()
        
        # Initialize sub_agents dict for potential children
        if "metadata" not in session_data:
            session_data["metadata"] = {}
        if "sub_agents" not in session_data["metadata"]:
            session_data["metadata"]["sub_agents"] = {}
        
        await session_manager.save_session(session_data)
        
        logger.debug(f"Linked sub-session {sub_session_id} to parent {parent_session_id}, depth={child_depth}")
        
        # Update parent session with sub-agent metadata
        await self._link_sub_to_parent(
            user_id, parent_session_id, sub_session_id, agent_type, initial_message, child_depth
        )
        
        return sub_session_id
    
    async def _link_sub_to_parent(
        self,
        user_id: str,
        parent_session_id: str,
        sub_session_id: str,
        agent_type: str,
        task_summary: str,
        child_depth: int
    ) -> None:
        """Add sub-agent metadata to parent session.
        
        Args:
            user_id: User ID
            parent_session_id: Parent session ID
            sub_session_id: Sub-session ID
            agent_type: Agent type
            task_summary: Task summary (truncated to 100 chars)
            child_depth: Nesting depth of child
        """
        session_manager = self._session_service.session_manager
        parent_data = await session_manager.load_session(user_id, parent_session_id)
        
        if "metadata" not in parent_data:
            parent_data["metadata"] = {}
        if "sub_agents" not in parent_data["metadata"]:
            parent_data["metadata"]["sub_agents"] = {}
        
        parent_data["metadata"]["sub_agents"][sub_session_id] = {
            "instance_id": sub_session_id,
            "agent_type": agent_type,
            "created_at": datetime.now(UTC).isoformat(),
            "last_used": datetime.now(UTC).isoformat(),
            "status": "active",
            "task_summary": task_summary[:100],
            "depth": child_depth
        }
        
        await session_manager.save_session(parent_data)
        
        logger.debug(f"Updated parent {parent_session_id} with sub-agent {sub_session_id} (depth={child_depth})")
    
    async def list_sub_sessions(
        self,
        parent_session_id: str,
        include_completed: bool = False
    ) -> list[dict[str, Any]]:
        """List sub-sessions by loading parent metadata.
        
        Args:
            parent_session_id: Parent session ID
            include_completed: Include archived/completed sessions
            
        Returns:
            List of sub-agent metadata dicts
        """
        user_id = self._extract_user_id(parent_session_id)
        session_manager = self._session_service.session_manager
        
        try:
            parent_data = await session_manager.load_session(user_id, parent_session_id)
        except FileNotFoundError:
            # Parent session doesn't exist yet (new session) - normal during initialization
            logger.debug(f"Parent session {parent_session_id} not found (likely new session)")
            return []
        
        sub_agents = parent_data.get("metadata", {}).get("sub_agents", {})
        
        result = []
        for instance_id, metadata in sub_agents.items():
            if not include_completed and metadata.get("status") != "active":
                continue
            result.append(metadata)
        
        logger.debug(f"Listed {len(result)} sub-sessions for parent {parent_session_id}")
        
        return result
    
    async def update_sub_session_metadata(
        self,
        parent_session_id: str,
        sub_session_id: str,
        **updates: Any
    ) -> None:
        """Update sub-session metadata in parent session.
        
        Args:
            parent_session_id: Parent session ID
            sub_session_id: Sub-session ID
            **updates: Metadata fields to update (e.g., last_used, status)
        """
        user_id = self._extract_user_id(parent_session_id)
        session_manager = self._session_service.session_manager
        
        parent_data = await session_manager.load_session(user_id, parent_session_id)
        
        if "metadata" not in parent_data:
            parent_data["metadata"] = {}
        if "sub_agents" not in parent_data["metadata"]:
            parent_data["metadata"]["sub_agents"] = {}
        
        if sub_session_id not in parent_data["metadata"]["sub_agents"]:
            logger.warning(f"Sub-session {sub_session_id} not found in parent metadata")
            return
        
        # Update metadata fields
        for key, value in updates.items():
            parent_data["metadata"]["sub_agents"][sub_session_id][key] = value
        
        await session_manager.save_session(parent_data)
        
        logger.debug(f"Updated metadata for sub-session {sub_session_id}: {updates}")
    
    async def _generate_instance_id(
        self,
        agent_type: str,
        instance_label: Optional[str]
    ) -> str:
        """Generate unique instance ID: sub_{type}_{global_counter}.
        
        Args:
            agent_type: Agent type
            instance_label: Optional custom label
            
        Returns:
            Unique instance ID (short format, no parent ID)
        """
        async with self._lock:
            # Increment global counter
            self._global_counter += 1
            counter = self._global_counter
            
            # Use custom label or auto-generate
            if instance_label:
                # Sanitize label (alphanumeric, underscore, hyphen only)
                import re
                sanitized = re.sub(r'[^a-zA-Z0-9_-]', '_', instance_label)
                instance_id = f"sub_{sanitized}_{counter:03d}"
            else:
                # Auto-generate: sub_{agent_type}_{counter}
                instance_id = f"sub_{agent_type}_{counter:03d}"
            
            logger.debug(f"Generated instance ID: {instance_id} (global counter: {counter})")
            
            return instance_id
    
    def _extract_user_id(self, session_id: str, params: Optional[dict] = None) -> str:
        """Extract user_id from injected params or session file location.
        
        Priority:
        1. Use _user_id from injected params (from ToolExecutionManager)
        2. Search session file in user directories
        3. Use 'anonymous' as fallback (NOT 'admin' for security)
        
        Args:
            session_id: Session ID to search for
            params: Optional tool call params with injected _user_id
            
        Returns:
            User ID (from params, directory name, or 'anonymous')
        """
        # Priority 1: Use injected user_id from tool params
        if params and "_user_id" in params:
            user_id = params["_user_id"]
            logger.debug(f"Using user_id '{user_id}' from injected params")
            return user_id
        
        # Priority 2: Search all user directories for this session_id
        storage_path = self._session_service.session_manager.storage_path
        
        for user_dir in storage_path.iterdir():
            if user_dir.is_dir() and not user_dir.name.startswith('.'):
                session_file = user_dir / f"{session_id}.json"
                if session_file.exists():
                    logger.debug(
                        f"Found session {session_id} in user directory: {user_dir.name}"
                    )
                    return user_dir.name
        
        # Priority 3: Use 'anonymous' as safe fallback (NOT 'admin' for security)
        # This happens when session doesn't exist yet (e.g., CLI ephemeral sessions)
        logger.info(
            f"No user_id found for session {session_id}, using 'anonymous'"
        )
        return "anonymous"
