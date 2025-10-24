"""Sequential Thinking MCP Server implementation.

This module provides step-by-step reasoning tools for LLMs to break down
complex problems dynamically with support for branching and revision.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from agent_system.mcp.schema_based import SchemaBasedMCPServer

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


@dataclass
class Thought:
    """Single thought in reasoning chain."""
    
    number: int
    content: str
    timestamp: datetime
    branch_id: str
    is_revision: bool = False
    revises_thought: int | None = None
    revision_history: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Branch:
    """Branch in thought tree for exploring alternatives."""
    
    branch_id: str
    parent_branch: str | None
    branched_from_thought: int
    created_at: datetime
    thoughts: list[Thought] = field(default_factory=list)
    active: bool = True


@dataclass
class SessionState:
    """State for a single sequential thinking session."""
    
    session_id: str
    created_at: datetime
    last_accessed: datetime
    thoughts: list[Thought] = field(default_factory=list)
    current_branch: str = "main"
    branches: dict[str, Branch] = field(default_factory=dict)
    total_thoughts_estimate: int = 1
    actual_thoughts: int = 0
    max_history_size: int = 100
    
    def __post_init__(self) -> None:
        """Initialize main branch if not exists."""
        if "main" not in self.branches:
            self.branches["main"] = Branch(
                branch_id="main",
                parent_branch=None,
                branched_from_thought=0,
                created_at=self.created_at
            )


class SequentialThinkingServer(SchemaBasedMCPServer):
    """Sequential Thinking MCP server for step-by-step reasoning.

    This server provides:
    - sequentialthinking: Main reasoning tool with branching/revision support
    - clear_history: Clear session thought history
    - get_thought_summary: Get condensed summary of thought chain
    
    Features:
    - Dynamic problem breakdown with adaptive complexity
    - Branching to explore alternative approaches
    - Revision to refine previous thoughts
    - Session management with TTL cleanup
    - Memory limit enforcement
    """

    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig) -> None:
        """
        Initialize Sequential Thinking server.
        
        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            mcp_config: Plugin-specific configuration
        """
        super().__init__(name, system_config, mcp_config)
        
        # Configuration
        self.max_history_size = int(getattr(mcp_config, 'max_history_size', 100))
        self.session_ttl_seconds = int(getattr(mcp_config, 'session_ttl_seconds', 3600))
        self.enable_branching = bool(getattr(mcp_config, 'enable_branching', True))
        self.enable_revisions = bool(getattr(mcp_config, 'enable_revisions', True))
        self.max_summary_thoughts = int(getattr(mcp_config, 'max_summary_thoughts', 10))
        
        # Session storage (in-memory)
        self._sessions: dict[str, SessionState] = {}
        
        logger.info(
            f"Sequential Thinking server '{name}' initialized - "
            f"max_history={self.max_history_size}, ttl={self.session_ttl_seconds}s, "
            f"branching={self.enable_branching}, revisions={self.enable_revisions}"
        )

    def get_template_vars(self) -> dict[str, Any]:
        """Provide custom template variables for schema rendering."""
        return {
            "name": self.name,
            "max_history_size": self.max_history_size,
            "session_ttl_seconds": self.session_ttl_seconds,
            "enable_branching": self.enable_branching,
            "enable_revisions": self.enable_revisions
        }

    def _get_or_create_session(self, session_id: str | None) -> SessionState:
        """Get existing session or create new one."""
        if session_id and session_id in self._sessions:
            session = self._sessions[session_id]
            session.last_accessed = datetime.now()
            return session
        
        # Create new session
        new_id = session_id or str(uuid.uuid4())
        session = SessionState(
            session_id=new_id,
            created_at=datetime.now(),
            last_accessed=datetime.now(),
            max_history_size=self.max_history_size
        )
        self._sessions[new_id] = session
        logger.info(f"Created new session: {new_id}")
        return session

    def _cleanup_old_sessions(self) -> None:
        """Remove expired sessions based on TTL."""
        now = datetime.now()
        expired = [
            sid for sid, session in self._sessions.items()
            if (now - session.last_accessed) > timedelta(seconds=self.session_ttl_seconds)
        ]
        for sid in expired:
            del self._sessions[sid]
            logger.info(f"Cleaned up expired session: {sid}")

    def _create_branch(
        self, 
        session: SessionState, 
        branch_id: str, 
        branch_from_thought: int
    ) -> Branch:
        """Create new branch from existing thought."""
        if branch_id in session.branches:
            raise ValueError(f"Branch '{branch_id}' already exists")
        
        branch = Branch(
            branch_id=branch_id,
            parent_branch=session.current_branch,
            branched_from_thought=branch_from_thought,
            created_at=datetime.now()
        )
        session.branches[branch_id] = branch
        session.current_branch = branch_id
        logger.info(f"Created branch '{branch_id}' from thought {branch_from_thought}")
        return branch

    def _add_thought(
        self,
        session: SessionState,
        thought_content: str,
        thought_number: int,
        is_revision: bool = False,
        revises_thought: int | None = None
    ) -> Thought:
        """Add thought to current branch."""
        current_branch = session.branches[session.current_branch]
        
        thought = Thought(
            number=thought_number,
            content=thought_content,
            timestamp=datetime.now(),
            branch_id=session.current_branch,
            is_revision=is_revision,
            revises_thought=revises_thought
        )
        
        # Handle revision
        if is_revision and revises_thought is not None:
            # Find most recent version of the thought being revised
            latest_version = None
            for t in reversed(session.thoughts):
                if t.number == revises_thought and t.branch_id == session.current_branch:
                    latest_version = t
                    break
            
            if latest_version:
                # Copy existing revision history and add this version
                thought.revision_history = latest_version.revision_history.copy()
                thought.revision_history.append({
                    "timestamp": latest_version.timestamp.isoformat(),
                    "content": latest_version.content
                })
        
        current_branch.thoughts.append(thought)
        session.thoughts.append(thought)
        session.actual_thoughts += 1
        
        return thought

    def _enforce_memory_limit(self, session: SessionState) -> None:
        """Enforce memory limit by removing oldest thoughts if needed."""
        if len(session.thoughts) > session.max_history_size:
            excess = len(session.thoughts) - session.max_history_size
            # Remove oldest thoughts (keep recent ones)
            session.thoughts = session.thoughts[excess:]
            logger.warning(
                f"Memory limit reached for session {session.session_id}, "
                f"removed {excess} oldest thoughts"
            )

    def _get_branch_tree(self, session: SessionState) -> dict[str, Any]:
        """Get branch tree visualization."""
        tree = {}
        for branch_id, branch in session.branches.items():
            tree[branch_id] = {
                "parent": branch.parent_branch,
                "branched_from": branch.branched_from_thought,
                "thoughts_count": len(branch.thoughts),
                "active": branch.active,
                "created_at": branch.created_at.isoformat()
            }
        return tree

    async def sequentialthinking(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Main reasoning tool for step-by-step problem-solving.
        
        Tool method - automatically called by generic dispatcher.
        Supports branching, revision, and adaptive complexity.
        """
        try:
            # Cleanup old sessions first
            self._cleanup_old_sessions()
            
            # Extract parameters
            thought_content = params["thought"]
            next_thought_needed = params["next_thought_needed"]
            thought_number = int(params["thought_number"])
            total_thoughts = int(params["total_thoughts"])
            session_id = params.get("session_id")
            is_revision = params.get("is_revision", False)
            revises_thought = params.get("revises_thought")
            branch_from_thought = params.get("branch_from_thought")
            branch_id = params.get("branch_id")
            needs_more_thoughts = params.get("needs_more_thoughts", False)
            
            # Get status context
            status = params["_status"]
            
            # Get or create session first (for session_id in status messages)
            session = self._get_or_create_session(session_id)
            
            # Validate parameters
            if thought_number < 1:
                await status.error("thought_number must be >= 1")
                return {"status": "error", "error": "thought_number must be >= 1"}
            if total_thoughts < 1:
                await status.error("total_thoughts must be >= 1")
                return {"status": "error", "error": "total_thoughts must be >= 1"}
            if not thought_content.strip():
                await status.error("thought content cannot be empty")
                return {"status": "error", "error": "thought content cannot be empty"}
            
            # Check feature flags
            if is_revision and not self.enable_revisions:
                await status.error("Revisions are disabled")
                return {"status": "error", "error": "Revisions are disabled"}
            if branch_id and not self.enable_branching:
                await status.error("Branching is disabled")
                return {"status": "error", "error": "Branching is disabled"}
            
            # Handle branching
            if branch_from_thought is not None and branch_id:
                # Creating new branch - must not exist
                if branch_id in session.branches:
                    await status.error(f"Branch '{branch_id}' already exists")
                    return {"status": "error", "error": f"Branch '{branch_id}' already exists"}
                
                await status.progress(
                    f"Creating branch '{branch_id}' from thought {branch_from_thought}"
                )
                self._create_branch(session, branch_id, branch_from_thought)
            elif branch_id:
                # Switching to existing branch (no branch_from_thought specified)
                if branch_id not in session.branches:
                    await status.error(f"Branch '{branch_id}' not found")
                    return {"status": "error", "error": f"Branch '{branch_id}' not found"}
                session.current_branch = branch_id
            
            # Handle revision
            if is_revision and revises_thought is not None:
                await status.progress(f"Revising thought {revises_thought} with new insights")
            
            # Add thought
            self._add_thought(
                session,
                thought_content,
                thought_number,
                is_revision,
                revises_thought
            )
            
            # Update estimate if needed
            if needs_more_thoughts or total_thoughts != session.total_thoughts_estimate:
                old_estimate = session.total_thoughts_estimate
                session.total_thoughts_estimate = total_thoughts
                if needs_more_thoughts:
                    await status.progress(
                        f"Adjusting complexity: {old_estimate} → {total_thoughts} thoughts"
                    )
            
            # Enforce memory limit
            self._enforce_memory_limit(session)
            
            # Check memory usage
            usage_pct = (len(session.thoughts) / session.max_history_size) * 100
            if usage_pct > 80:
                await status.progress(
                    f"Memory usage: {len(session.thoughts)}/{session.max_history_size} "
                    f"thoughts ({usage_pct:.0f}%)",
                    level="warning"
                )
            
            # Prepare result
            result = {
                "status": "success",
                "session_id": session.session_id,
                "current_thought_number": thought_number,
                "total_thoughts_estimate": session.total_thoughts_estimate,
                "next_thought_needed": next_thought_needed,
                "progress": f"Thought {session.actual_thoughts}/{session.total_thoughts_estimate}",
                "branch": session.current_branch,
                "thought_history": [
                    {
                        "number": t.number,
                        "content": t.content[:200] + "..." if len(t.content) > 200 else t.content,
                        "branch": t.branch_id,
                        "is_revision": t.is_revision
                    }
                    for t in session.thoughts[-self.max_summary_thoughts:]
                ],
                "branch_summary": self._get_branch_tree(session) if self.enable_branching else None,
                "error": None,
                "warning": None
            }
            
            # Add warning if approaching limit
            if usage_pct > 90:
                result["warning"] = f"Session approaching memory limit ({usage_pct:.0f}% used)"
            
            # END status
            complete_msg = "✓ Complete" if not next_thought_needed else "Continue reasoning..."
            await status.end(
                f"Thought {thought_number} added ({session.actual_thoughts}/{session.total_thoughts_estimate}). "
                f"{complete_msg}"
            )
            
            return result
            
        except Exception as e:
            logger.exception(f"Error in sequentialthinking: {e}")
            await status.error(f"Failed to add thought: {str(e)}")
            return {"status": "error", "error": str(e)}

    async def clear_history(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Clear thought history for a session or all sessions.
        
        Tool method - automatically called by generic dispatcher.
        """
        try:
            session_id = params.get("session_id")
            status = params["_status"]
            
            if session_id:
                # Clear specific session
                if session_id not in self._sessions:
                    await status.error(f"Session {session_id} not found")
                    return {
                        "status": "error",
                        "error": f"Session {session_id} not found"
                    }
                
                session = self._sessions[session_id]
                thought_count = len(session.thoughts)
                
                await status.progress(
                    f"Clearing session {session_id[:8]} ({thought_count} thoughts)"
                )
                
                del self._sessions[session_id]
                
                await status.end("Cleared 1 session")
                
                return {
                    "status": "success",
                    "cleared_sessions": 1,
                    "message": f"Cleared session {session_id} ({thought_count} thoughts)"
                }
            else:
                # Clear all sessions
                session_count = len(self._sessions)
                
                await status.progress(f"Clearing all sessions ({session_count} total)")
                
                self._sessions.clear()
                
                await status.end(f"Cleared {session_count} session(s)")
                
                return {
                    "status": "success",
                    "cleared_sessions": session_count,
                    "message": f"Cleared all {session_count} sessions"
                }
                
        except Exception as e:
            logger.exception(f"Error in clear_history: {e}")
            await status.error(f"Failed to clear history: {str(e)}")
            return {"status": "error", "error": str(e)}

    async def get_thought_summary(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Get condensed summary of thought chain.
        
        Tool method - automatically called by generic dispatcher.
        """
        try:
            session_id = params["session_id"]
            max_thoughts = params.get("max_thoughts", self.max_summary_thoughts)
            include_branches = params.get("include_branches", True)
            status = params["_status"]
            
            if session_id not in self._sessions:
                await status.error(f"Session {session_id} not found")
                return {
                    "status": "error",
                    "error": f"Session {session_id} not found"
                }
            
            session = self._sessions[session_id]
            
            await status.progress(
                f"Generating summary for session {session_id[:8]} "
                f"({len(session.thoughts)} thoughts, {len(session.branches)} branches)"
            )
            
            # Get recent thoughts
            recent_thoughts = session.thoughts[-max_thoughts:] if max_thoughts > 0 else session.thoughts
            
            summary = {
                "status": "success",
                "session_id": session.session_id,
                "total_thoughts": len(session.thoughts),
                "current_branch": session.current_branch,
                "thoughts": [
                    {
                        "number": t.number,
                        "content": t.content,
                        "timestamp": t.timestamp.isoformat(),
                        "branch": t.branch_id,
                        "is_revision": t.is_revision,
                        "revises_thought": t.revises_thought,
                        "revision_count": len(t.revision_history)
                    }
                    for t in recent_thoughts
                ],
                "branches": self._get_branch_tree(session) if include_branches else None,
                "created_at": session.created_at.isoformat(),
                "last_accessed": session.last_accessed.isoformat()
            }
            
            await status.end(
                f"Summary generated: {len(recent_thoughts)} thoughts, "
                f"{len(session.branches)} branches"
            )
            
            return summary
            
        except Exception as e:
            logger.exception(f"Error in get_thought_summary: {e}")
            await status.error(f"Failed to generate summary: {str(e)}")
            return {"status": "error", "error": str(e)}
