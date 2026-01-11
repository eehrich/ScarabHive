"""Sequential Thinking MCP Server implementation.

This module provides step-by-step reasoning tools for LLMs to break down
complex problems dynamically with support for branching and revision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.hooks.plugin_hook import HookContext, HookResult
from agent_system.utils.id import short_id

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


@dataclass
class Thought:
    """Single thought in reasoning chain."""

    event_id: str  # Unique UUID for this entry
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

        # Mapping: agent_session_id → list of sequential_thinking_session_ids
        # This allows the hook to find active thinking sessions for a given conversation
        self._agent_session_mapping: dict[str, list[str]] = {}

        # Idempotency: idempotency_key → event_id (prevents duplicate entries on retry)
        self._idempotency_cache: dict[str, str] = {}

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

    def _get_or_create_session(self, session_id: str | None, agent_session_id: str | None = None) -> SessionState:
        """Get existing session or create new one.

        Args:
            session_id: Sequential thinking session ID (10-char hex)
            agent_session_id: Agent conversation session ID (for hook lookup)

        Returns:
            SessionState: Existing or new session

        Behavior:
            - If session_id provided → use that specific session
            - If no session_id but agent_session_id → reuse most recent session for that agent
            - If neither provided → create completely new session
        """
        # If explicit session_id provided, use/get that specific session
        if session_id:
            if session_id in self._sessions:
                session = self._sessions[session_id]
                session.last_accessed = datetime.now()

                # Update agent session mapping if provided
                if agent_session_id and session_id not in self._agent_session_mapping.get(agent_session_id, []):
                    if agent_session_id not in self._agent_session_mapping:
                        self._agent_session_mapping[agent_session_id] = []
                    self._agent_session_mapping[agent_session_id].append(session_id)

                return session
            else:
                # Requested session doesn't exist - create new session with this explicit ID
                # Don't fall through to agent_session_id lookup
                new_id = session_id
                session = SessionState(
                    session_id=new_id,
                    created_at=datetime.now(),
                    last_accessed=datetime.now(),
                    max_history_size=self.max_history_size
                )
                self._sessions[new_id] = session

                # Add to agent session mapping if provided
                if agent_session_id:
                    if agent_session_id not in self._agent_session_mapping:
                        self._agent_session_mapping[agent_session_id] = []
                    if new_id not in self._agent_session_mapping[agent_session_id]:
                        self._agent_session_mapping[agent_session_id].append(new_id)
                    logger.debug(f"Mapped agent session {agent_session_id} → thinking session {new_id}")

                logger.info(f"Created new thinking session with explicit ID: {new_id}")
                return session

        # No explicit session_id - check for existing session via agent_session_id
        if agent_session_id and agent_session_id in self._agent_session_mapping:
            # Get most recent thinking session for this agent
            thinking_session_ids = self._agent_session_mapping[agent_session_id]
            valid_sessions = [
                self._sessions[sid] for sid in thinking_session_ids
                if sid in self._sessions
            ]
            if valid_sessions:
                # Return most recently accessed session
                session = max(valid_sessions, key=lambda s: s.last_accessed)
                session.last_accessed = datetime.now()
                logger.debug(f"Reusing thinking session {session.session_id} for agent session {agent_session_id}")
                return session

        # Create new session
        new_id = session_id or short_id(10)
        session = SessionState(
            session_id=new_id,
            created_at=datetime.now(),
            last_accessed=datetime.now(),
            max_history_size=self.max_history_size
        )
        self._sessions[new_id] = session

        # Add to agent session mapping if provided
        if agent_session_id:
            if agent_session_id not in self._agent_session_mapping:
                self._agent_session_mapping[agent_session_id] = []
            if new_id not in self._agent_session_mapping[agent_session_id]:
                self._agent_session_mapping[agent_session_id].append(new_id)
            logger.debug(f"Mapped agent session {agent_session_id} → thinking session {new_id}")

        logger.info(f"Created new thinking session: {new_id}")
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

            # Clean up agent session mapping
            for agent_sid, thinking_sids in list(self._agent_session_mapping.items()):
                if sid in thinking_sids:
                    thinking_sids.remove(sid)
                # Remove empty mappings
                if not thinking_sids:
                    del self._agent_session_mapping[agent_sid]

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

        # Find the thought we're branching from to determine parent branch
        source_thought = None
        for t in session.thoughts:
            if t.number == branch_from_thought:
                source_thought = t
                break

        if source_thought is None:
            raise ValueError(f"Thought #{branch_from_thought} not found")

        # Parent branch is the branch of the thought we're branching from
        parent_branch_id = source_thought.branch_id

        branch = Branch(
            branch_id=branch_id,
            parent_branch=parent_branch_id,
            branched_from_thought=branch_from_thought,
            created_at=datetime.now()
        )
        session.branches[branch_id] = branch
        session.current_branch = branch_id
        logger.info(
            f"Created branch '{branch_id}' from thought {branch_from_thought} "
            f"(parent: '{parent_branch_id}')"
        )
        return branch

    def _add_thought(
        self,
        session: SessionState,
        thought_content: str,
        thought_number: int,
        is_revision: bool = False,
        revises_thought: int | None = None
    ) -> Thought:
        """Add thought to current branch (or to original branch if revision)."""
        # Generate unique event ID
        event_id = short_id()

        # Determine target branch for this thought
        target_branch_id = session.current_branch

        # Handle revision - find original thought and use its branch
        if is_revision and revises_thought is not None:
            # Find most recent version of the thought being revised (across all branches)
            latest_version = None
            for t in reversed(session.thoughts):
                if t.number == revises_thought:
                    latest_version = t
                    break

            if latest_version:
                # Revision goes to the same branch as the original thought
                target_branch_id = latest_version.branch_id
                logger.debug(
                    f"Revision of thought #{revises_thought} assigned to original branch '{target_branch_id}'"
                )

        target_branch = session.branches[target_branch_id]

        thought = Thought(
            event_id=event_id,
            number=thought_number,
            content=thought_content,
            timestamp=datetime.now(),
            branch_id=target_branch_id,
            is_revision=is_revision,
            revises_thought=revises_thought
        )

        # Build revision history if revising
        if is_revision and revises_thought is not None:
            latest_version = None
            for t in reversed(session.thoughts):
                if t.number == revises_thought:
                    latest_version = t
                    break

            if latest_version:
                # Copy existing revision history and add this version
                thought.revision_history = latest_version.revision_history.copy()
                thought.revision_history.append({
                    "event_id": latest_version.event_id,
                    "timestamp": latest_version.timestamp.isoformat(),
                    "content": latest_version.content,
                    "branch_id": latest_version.branch_id
                })

        target_branch.thoughts.append(thought)
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

    def _relative_time(self, dt: datetime) -> str:
        """Format datetime as relative time (e.g., '2m ago', '1h ago')."""
        now = datetime.now()
        delta = now - dt

        seconds = int(delta.total_seconds())
        if seconds < 60:
            return f"{seconds}s ago"
        elif seconds < 3600:
            return f"{seconds // 60}m ago"
        elif seconds < 86400:
            return f"{seconds // 3600}h ago"
        else:
            return f"{seconds // 86400}d ago"

    def _get_branch_tree(self, session: SessionState) -> dict[str, Any]:
        """Get branch tree visualization."""
        tree = {}
        for branch_id, branch in session.branches.items():
            branch_info = {
                "branched_from": branch.branched_from_thought,
                "thoughts_count": len(branch.thoughts),
                "active": branch.active,
                "created_at": branch.created_at.isoformat()
            }
            # Only include parent if it's not None (avoid null values for Gemini compatibility)
            if branch.parent_branch is not None:
                branch_info["parent"] = branch.parent_branch
            
            tree[branch_id] = branch_info
        return tree

    async def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Main tool entry point - called when tool name matches server name ({{name}}).

        Main tool for sequential thinking - generates chain of thought.

        Tool name: {{ name }} → e.g., 'sequential_thinking'
        Method is called directly (no prefix to strip since tool name = plugin name).
        """
        # Initialize status early to avoid UnboundLocalError in exception handler
        status = None
        try:
            # Cleanup old sessions first
            self._cleanup_old_sessions()

            # Extract parameters - use .get() with defaults for robustness
            thought_content = params.get("thought", "")
            # Support both snake_case and camelCase for next_thought_needed
            next_thought_needed = params.get("next_thought_needed", params.get("nextThoughtNeeded", True))
            thought_number = int(params.get("thought_number", params.get("thoughtNumber", 1)))
            total_thoughts = int(params.get("total_thoughts", params.get("totalThoughts", 5)))
            session_id = params.get("session_id", params.get("sessionId"))
            is_revision = params.get("is_revision", params.get("isRevision", False))
            revises_thought = params.get("revises_thought", params.get("revisesThought"))
            branch_from_thought = params.get("branch_from_thought", params.get("branchFromThought"))
            branch_id = params.get("branch_id", params.get("branchId"))
            needs_more_thoughts = params.get("needs_more_thoughts", params.get("needsMoreThoughts", False))
            idempotency_key = params.get("idempotency_key", params.get("idempotencyKey"))

            # Get status context (optional for direct test calls)
            status = params.get("_status")

            # Helper to safely call status methods when status is available
            async def safe_status_call(method_name: str, *args, **kwargs):
                if status:
                    method = getattr(status, method_name)
                    await method(*args, **kwargs)

            # Get agent session ID if available (for hook mapping)
            agent_session_id = params.get("_session_id")

            # Check idempotency: return cached result if key exists
            if idempotency_key and idempotency_key in self._idempotency_cache:
                cached_event_id = self._idempotency_cache[idempotency_key]
                await safe_status_call("end", f"Idempotent request (key={idempotency_key[:8]}..., event_id={cached_event_id[:8]}...)")
                # Find the cached thought and return its response
                for s in self._sessions.values():
                    for t in s.thoughts:
                        if t.event_id == cached_event_id:
                            return {
                                "status": "success",
                                "session_id": s.session_id,
                                "event_id": t.event_id,
                                "current_thought_number": t.number,
                                "client_thought_number": thought_number,
                                "recorded_thoughts_count": len(s.thoughts),
                                "total_thoughts_estimate": s.total_thoughts_estimate,
                                "next_thought_needed": next_thought_needed,
                                "progress": f"Thought {s.actual_thoughts}/{max(s.actual_thoughts, s.total_thoughts_estimate)}",
                                "branch": s.current_branch,
                                "thought_history": [],  # Minimal response for cached
                                "warnings": ["Idempotent request - returned cached result"]
                            }

            # Get or create session first (for session_id in status messages)
            session = self._get_or_create_session(session_id, agent_session_id)

            # Server-assigned thought number (auto-increment for consistency)
            # For revisions, use the thought being revised; for new thoughts, auto-increment
            if is_revision and revises_thought is not None:
                server_thought_number = revises_thought  # Keep same number for revisions
            else:
                server_thought_number = session.actual_thoughts + 1  # New thought gets next number

            # Validate parameters
            if thought_number < 1:
                await safe_status_call("error", "thought_number must be >= 1")
                return {"status": "error", "error": "thought_number must be >= 1"}
            if total_thoughts < 1:
                await safe_status_call("error", "total_thoughts must be >= 1")
                return {"status": "error", "error": "total_thoughts must be >= 1"}
            if not thought_content.strip():
                await safe_status_call("error", "thought content cannot be empty")
                return {"status": "error", "error": "thought content cannot be empty"}

            # Validate revision parameters
            if is_revision:
                if revises_thought is None:
                    await safe_status_call("error", "revises_thought is required when is_revision=true")
                    return {"status": "error", "error": "revises_thought is required when is_revision=true"}
                if revises_thought < 1:
                    await safe_status_call("error", "revises_thought must be >= 1")
                    return {"status": "error", "error": "revises_thought must be >= 1"}
                # Check if thought to revise exists
                thought_exists = any(t.number == revises_thought for t in session.thoughts)
                if not thought_exists:
                    await safe_status_call("error", f"Thought #{revises_thought} not found in session")
                    return {"status": "error", "error": f"Thought #{revises_thought} not found in session"}

            # Check feature flags
            if is_revision and not self.enable_revisions:
                await safe_status_call("error", "Revisions are disabled")
                return {"status": "error", "error": "Revisions are disabled"}
            if branch_id and not self.enable_branching:
                await safe_status_call("error", "Branching is disabled")
                return {"status": "error", "error": "Branching is disabled"}

            # Handle branching
            if branch_from_thought is not None and branch_id:
                # Creating new branch - must not exist
                if branch_id in session.branches:
                    await safe_status_call("error", f"Branch '{branch_id}' already exists")
                    return {"status": "error", "error": f"Branch '{branch_id}' already exists"}

                await safe_status_call(
                    "progress",
                    f"Creating branch '{branch_id}' from thought {branch_from_thought}"
                )
                self._create_branch(session, branch_id, branch_from_thought)
            elif branch_id:
                # Switching to existing branch (no branch_from_thought specified)
                if branch_id not in session.branches:
                    await safe_status_call("error", f"Branch '{branch_id}' not found")
                    return {"status": "error", "error": f"Branch '{branch_id}' not found"}
                session.current_branch = branch_id

            # Handle revision
            if is_revision and revises_thought is not None:
                await safe_status_call("progress", f"Revising thought {revises_thought} with new insights")

            # Add thought with server-assigned number (ensures no gaps)
            self._add_thought(
                session,
                thought_content,
                server_thought_number,  # Use server number, not client number
                is_revision,
                revises_thought
            )

            # Collect warnings for validation issues
            warnings = []

            # Update estimate if needed with auto-clamp and validation
            if needs_more_thoughts or total_thoughts != session.total_thoughts_estimate:
                old_estimate = session.total_thoughts_estimate

                # Auto-clamp: estimate must be >= actual thoughts
                if total_thoughts < session.actual_thoughts:
                    warnings.append(
                        f"total_thoughts ({total_thoughts}) < recorded thoughts "
                        f"({session.actual_thoughts}), adjusted to {session.actual_thoughts}"
                    )
                    total_thoughts = session.actual_thoughts
                    logger.warning(
                        f"Auto-clamped total_thoughts from {params['total_thoughts']} "
                        f"to {total_thoughts} for session {session.session_id}"
                    )

                # Warn if needs_more_thoughts but estimate not increased
                if needs_more_thoughts and total_thoughts <= old_estimate:
                    warnings.append(
                        f"needs_more_thoughts=true but estimate not increased "
                        f"({total_thoughts} <= {old_estimate})"
                    )

                session.total_thoughts_estimate = total_thoughts

                if needs_more_thoughts:
                    await safe_status_call(
                        "progress",
                        f"Adjusting complexity: {old_estimate} → {total_thoughts} thoughts"
                    )

            # Enforce memory limit
            self._enforce_memory_limit(session)

            # Check memory usage
            usage_pct = (len(session.thoughts) / session.max_history_size) * 100
            if usage_pct > 80:
                warnings.append(
                    f"Memory usage: {usage_pct:.0f}% "
                    f"({len(session.thoughts)}/{session.max_history_size})"
                )
                await safe_status_call(
                    "progress",
                    f"Memory usage: {len(session.thoughts)}/{session.max_history_size} "
                    f"thoughts ({usage_pct:.0f}%)",
                    level="warning"
                )

            # Consistent progress display: never show X/Y with X > Y
            effective_total = max(session.actual_thoughts, session.total_thoughts_estimate)

            # Get the thought that was just added
            current_thought = session.thoughts[-1]

            # Cache event_id for idempotency (if key provided)
            if idempotency_key:
                self._idempotency_cache[idempotency_key] = current_thought.event_id
                logger.debug(f"Cached idempotency_key={idempotency_key[:8]}... → event_id={current_thought.event_id[:8]}...")

            # Prepare result
            result = {
                "status": "success",
                "session_id": session.session_id,
                "event_id": current_thought.event_id,  # UUID for this entry
                "current_thought_number": server_thought_number,  # Return server number
                "client_thought_number": thought_number,  # Also return client's number for reference
                "recorded_thoughts_count": len(session.thoughts),  # Total recorded thoughts
                "total_thoughts_estimate": session.total_thoughts_estimate,
                "next_thought_needed": next_thought_needed,
                "progress": f"Thought {session.actual_thoughts}/{effective_total}",
                "branch": session.current_branch,
                "thought_history": [
                    {
                        k: v for k, v in {
                            "event_id": t.event_id,
                            "number": t.number,
                            "content": t.content[:200] + "..." if len(t.content) > 200 else t.content,
                            "branch": t.branch_id,
                            "is_revision": t.is_revision,
                            "revises_thought": t.revises_thought,  # May be None
                            "timestamp": t.timestamp.isoformat()
                        }.items() if v is not None  # Filter out None values
                    }
                    for t in session.thoughts[-self.max_summary_thoughts:]
                ]
            }
            
            # Add optional fields only if they have values (avoid None)
            if warnings:
                result["warnings"] = warnings
            
            if self.enable_branching:
                branch_tree = self._get_branch_tree(session)
                if branch_tree:
                    result["branch_summary"] = branch_tree

            # END status
            complete_msg = "✓ Complete" if not next_thought_needed else "Continue reasoning..."
            # Show actual thought count, not thought number (which can be same for revisions)
            await safe_status_call(
                "end",
                f"Thought #{session.actual_thoughts} added ({session.actual_thoughts}/{effective_total}). "
                f"{complete_msg}"
            )

            return result

        except Exception as e:
            logger.exception(f"Error in sequentialthinking: {e}")
            # Try to get status for error reporting, but don't fail if not available
            if status:
                await safe_status_call("error", f"Failed to add thought: {str(e)}")
            return {"status": "error", "error": str(e)}

    async def clear_history(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Clear thought history for a session or all sessions.

        Tool method - automatically called by generic dispatcher.
        """
        try:
            session_id = params.get("session_id")
            status = params.get("_status")

            # Helper to safely call status methods when status is available
            async def safe_status_call(method_name: str, *args, **kwargs):
                if status:
                    method = getattr(status, method_name)
                    await method(*args, **kwargs)

            if session_id:
                # Clear specific session
                if session_id not in self._sessions:
                    await safe_status_call("error", f"Session {session_id} not found")
                    return {
                        "status": "error",
                        "error": f"Session {session_id} not found"
                    }

                session = self._sessions[session_id]
                thought_count = len(session.thoughts)

                await safe_status_call(
                    "progress",
                    f"Clearing session {session_id} ({thought_count} thoughts)"
                )

                del self._sessions[session_id]

                await safe_status_call("end", "Cleared 1 session")

                return {
                    "status": "success",
                    "cleared_sessions": 1,
                    "message": f"Cleared session {session_id} ({thought_count} thoughts)"
                }
            else:
                # Clear all sessions
                session_count = len(self._sessions)

                await safe_status_call("progress", f"Clearing all sessions ({session_count} total)")

                self._sessions.clear()

                await safe_status_call("end", f"Cleared {session_count} session(s)")

                return {
                    "status": "success",
                    "cleared_sessions": session_count,
                    "message": f"Cleared all {session_count} sessions"
                }

        except Exception as e:
            logger.exception(f"Error in clear_history: {e}")
            if status:
                await safe_status_call("error", f"Failed to clear history: {str(e)}")
            return {"status": "error", "error": str(e)}

    async def get_summary(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Get summary of current thinking session.

        Tool name: {{ name }}_get_summary → e.g., 'sequential_thinking_get_summary'
        Method called after dispatcher strips prefix → 'get_summary'
        """
        try:
            session_id = params["session_id"]
            max_thoughts = params.get("max_thoughts", self.max_summary_thoughts)
            include_branches = params.get("include_branches", True)
            status = params.get("_status")

            # Helper to safely call status methods when status is available
            async def safe_status_call(method_name: str, *args, **kwargs):
                if status:
                    method = getattr(status, method_name)
                    await method(*args, **kwargs)

            if session_id not in self._sessions:
                await safe_status_call("error", f"Session {session_id} not found")
                return {
                    "status": "error",
                    "error": f"Session {session_id} not found"
                }

            session = self._sessions[session_id]

            await safe_status_call(
                "progress",
                f"Generating summary for session {session_id} "
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
                        k: v for k, v in {
                            "event_id": t.event_id,
                            "number": t.number,
                            "content": t.content,
                            "timestamp": t.timestamp.isoformat(),
                            "branch": t.branch_id,
                            "is_revision": t.is_revision,
                            "revises_thought": t.revises_thought,  # May be None
                            "revision_count": len(t.revision_history)
                        }.items() if v is not None  # Filter out None values
                    }
                    for t in recent_thoughts
                ],
                "created_at": session.created_at.isoformat(),
                "last_accessed": session.last_accessed.isoformat()
            }
            
            # Add branches only if requested and available
            if include_branches:
                branch_tree = self._get_branch_tree(session)
                if branch_tree:
                    summary["branches"] = branch_tree

            await safe_status_call(
                "end",
                f"Summary generated: {len(recent_thoughts)} thoughts, "
                f"{len(session.branches)} branches"
            )

            return summary

        except Exception as e:
            logger.exception(f"Error in get_thought_summary: {e}")
            if status:
                await safe_status_call("error", f"Failed to generate summary: {str(e)}")
            return {"status": "error", "error": str(e)}

    # =========================================================================
    # Hook Implementation: System Prompt Injection
    # =========================================================================

    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """
        Inject active sequential thinking sessions into system prompt before LLM call.

        This hook (defined in schema.yaml as inject_active_sessions) automatically
        adds recent thoughts from active sessions to the agent's context,
        providing reasoning continuity without explicit tool calls.

        Configuration is loaded from schema.yaml config section.

        Args:
            context: Hook context with messages, session_id, agent

        Returns:
            HookResult with modified=True if session info was injected
        """
        if not context.messages:
            logger.debug("SequentialThinkingHook: No messages in context, skipping")
            return HookResult(success=True, modified=False, context=context)

        if not context.session_id:
            logger.debug("SequentialThinkingHook: No session_id in context, skipping")
            return HookResult(success=True, modified=False, context=context)

        try:
            # Get hook config from schema.yaml
            max_thoughts = getattr(self.mcp_config, "max_thoughts_in_prompt", 5)
            show_branch_info = getattr(self.mcp_config, "show_branch_info", True)
            format_type = getattr(self.mcp_config, "format", "markdown")
            max_sessions_in_prompt = getattr(self.mcp_config, "max_sessions_in_prompt", 1)

            # Find active sequential thinking sessions for this agent session
            agent_session_id = context.session_id
            thinking_session_ids = self._agent_session_mapping.get(agent_session_id, [])

            # Get valid sessions sorted by last accessed (most recent first)
            active_sessions = []
            if thinking_session_ids:
                valid_sessions = [
                    self._sessions[sid] for sid in thinking_session_ids
                    if sid in self._sessions
                ]
                if valid_sessions:
                    # Sort by last_accessed descending
                    active_sessions = sorted(valid_sessions, key=lambda s: s.last_accessed, reverse=True)
                    # Limit to max_sessions_in_prompt
                    active_sessions = active_sessions[:max_sessions_in_prompt]
                    logger.debug(
                        f"Found {len(valid_sessions)} thinking session(s) for agent session {agent_session_id}, "
                        f"showing {len(active_sessions)} most recent"
                    )

            from agent_system.llm.models import ChatMessage

            # Check if already injected and REMOVE old injection to replace it
            # Search backwards to avoid index shifting issues
            for i in range(len(context.messages) - 1, -1, -1):
                msg = context.messages[i]
                msg_content = msg.content if hasattr(msg, 'content') else msg.get('content', '')
                if msg_content and ("## Sequential Thinking Tool Available" in msg_content or "## Active Sequential Thinking Session" in msg_content):
                    # Remove old injection
                    context.messages.pop(i)
                    logger.debug(f"Removed old Sequential Thinking injection at position {i}")

            if active_sessions:
                # Format active sessions (one or multiple)
                if len(active_sessions) == 1:
                    session_prompt = self._format_session_for_prompt(
                        active_sessions[0], max_thoughts, show_branch_info, format_type
                    )
                else:
                    # Multiple sessions - format with dividers
                    session_prompt = self._format_multiple_sessions_for_prompt(
                        active_sessions, max_thoughts, show_branch_info, format_type
                    )

                total_thoughts = sum(len(s.thoughts) for s in active_sessions)
                logger.info(
                    f"[SequentialThinkingHook] Injecting {len(active_sessions)} session(s) "
                    f"with {total_thoughts} total thoughts"
                )
            else:
                # No active session - inject reminder about tool
                session_prompt = self._format_thinking_reminder()
                logger.info("[SequentialThinkingHook] No active session - injecting tool reminder")

            # Insert after first system message
            insert_pos = self._find_system_message_position(context.messages)
            context.messages.insert(insert_pos, ChatMessage(
                role="system",
                content=session_prompt
            ))

            return HookResult(success=True, modified=True, context=context)

        except Exception as e:
            logger.error(f"SequentialThinkingHook failed: {e}", exc_info=True)
            # Don't fail the entire LLM call if hook fails
            return HookResult(success=True, modified=False, context=context)

    def _format_thinking_reminder(self) -> str:
        """Format sequential thinking tool reminder when no active session."""
        return """## Sequential Thinking Tool Available

Use `sequential_thinking()` for complex, multi-step reasoning. Break down problems into thoughts, revise earlier insights, and explore alternatives via branching.

**Start new session:**
```
sequential_thinking(
    thought="First, let's analyze the requirements...",
    thought_number=1,
    total_thoughts=5,
    next_thought_needed=true
)
```

**Parameter groups:**
- **Core**: thought, thought_number, total_thoughts, next_thought_needed
- **Session**: session_id (auto-generated on first call, required for continuation)
- **Revise**: is_revision=true, revises_thought=<N> (update earlier thought)
- **Branch**: branch_from_thought=<N>, branch_id='name' (explore alternatives)
"""

    def _format_session_for_prompt(
        self,
        session: SessionState,
        max_thoughts: int,
        show_branch_info: bool,
        format_type: str = "markdown"
    ) -> str:
        """Format active session for injection into prompt."""
        # Get config options for UX improvements
        show_relative_timestamps = getattr(self.mcp_config, "show_relative_timestamps", True)
        show_quick_actions = getattr(self.mcp_config, "show_quick_actions", True)

        if format_type == "markdown":
            lines = []
            lines.append("## Active Sequential Thinking Session\n")
            lines.append(f"**Session ID**: `{session.session_id}`")

            # Add relative timestamp for session age if enabled
            if show_relative_timestamps:
                session_age = self._relative_time(session.created_at)
                lines.append(f"**Started**: {session_age}")

            lines.append(f"**Progress**: {len(session.thoughts)}/{session.total_thoughts_estimate} thoughts\n")

            # Show recent thoughts
            recent_thoughts = session.thoughts[-max_thoughts:] if max_thoughts > 0 else session.thoughts

            if recent_thoughts:
                lines.append("**Recent thoughts:**")
                for thought in recent_thoughts:
                    branch_tag = f" [{thought.branch_id}]" if show_branch_info and thought.branch_id != "main" else ""
                    revision_tag = f" (revises #{thought.revises_thought})" if thought.is_revision else ""

                    # Add relative timestamp if enabled
                    time_tag = ""
                    if show_relative_timestamps:
                        time_tag = f" *({self._relative_time(thought.timestamp)})*"

                    # Truncate long thoughts
                    content = thought.content[:150] + "..." if len(thought.content) > 150 else thought.content

                    lines.append(
                        f"- **Thought #{thought.number}**{branch_tag}{revision_tag}{time_tag}: {content}"
                    )

            # Show branch info if enabled
            if show_branch_info and len(session.branches) > 1:
                lines.append(f"\n**Current branch**: `{session.current_branch}`")
                lines.append(f"**Available branches**: {', '.join(f'`{b}`' for b in session.branches.keys())}")

            # Add quick actions if enabled
            if show_quick_actions:
                lines.append("\n**Parameter Groups:**")
                lines.append("- **Core**: `thought`, `thought_number`, `total_thoughts`, `next_thought_needed`")
                lines.append(f"- **Session**: `session_id='{session.session_id}'` (required for continuation)")
                lines.append("- **Revise**: `is_revision=true`, `revises_thought=<N>` (creates new entry, keeps number)")
                lines.append("- **Branch**: Create: `branch_from_thought=<N>`, `branch_id='name'` | Switch: `branch_id='name'` only")
                lines.append("\n**Examples:**")
                lines.append(f"- Continue: `sequential_thinking(session_id='{session.session_id}', thought='...', thought_number={len(session.thoughts)+1}, ...)`")
                lines.append(f"- Revise #3: `sequential_thinking(session_id='{session.session_id}', thought='...', thought_number=3, is_revision=true, revises_thought=3, ...)`")
                lines.append(f"- Branch: `sequential_thinking(session_id='{session.session_id}', thought='...', branch_from_thought=2, branch_id='alt', ...)`")
            else:
                lines.append("\nContinue reasoning with `sequential_thinking()` or summarize findings if complete.")

            return "\n".join(lines)
        else:
            # Plain text format
            lines = [
                "Sequential Thinking Session Active",
                f"Session ID: {session.session_id}",
                f"Progress: {len(session.thoughts)}/{session.total_thoughts_estimate} thoughts",
                ""
            ]

            recent_thoughts = session.thoughts[-max_thoughts:] if max_thoughts > 0 else session.thoughts
            for thought in recent_thoughts:
                lines.append(f"Thought #{thought.number}: {thought.content[:100]}")

            return "\n".join(lines)

    def _format_multiple_sessions_for_prompt(
        self,
        sessions: list[SessionState],
        max_thoughts: int,
        show_branch_info: bool,
        format_type: str = "markdown"
    ) -> str:
        """Format multiple active sessions for injection into prompt."""
        if format_type == "markdown":
            lines = []
            lines.append(f"## Active Sequential Thinking Sessions ({len(sessions)})\n")

            for i, session in enumerate(sessions, 1):
                # Session header
                lines.append(f"### Session {i}: `{session.session_id}`")

                # Add relative timestamp if enabled
                show_relative_timestamps = getattr(self.mcp_config, "show_relative_timestamps", True)
                if show_relative_timestamps:
                    session_age = self._relative_time(session.created_at)
                    lines.append(f"**Started**: {session_age}")

                lines.append(f"**Progress**: {len(session.thoughts)}/{session.total_thoughts_estimate} thoughts")

                # Show recent thoughts (reduced for multi-session view)
                thoughts_to_show = min(max_thoughts, 3)  # Show fewer thoughts per session
                recent_thoughts = session.thoughts[-thoughts_to_show:]

                if recent_thoughts:
                    lines.append("**Recent thoughts:**")
                    for thought in recent_thoughts:
                        branch_tag = f" [{thought.branch_id}]" if show_branch_info and thought.branch_id != "main" else ""

                        # Add relative timestamp if enabled
                        time_tag = ""
                        if show_relative_timestamps:
                            time_tag = f" *({self._relative_time(thought.timestamp)})*"

                        # Truncate for compactness
                        content = thought.content[:100] + "..." if len(thought.content) > 100 else thought.content
                        lines.append(f"- **#{thought.number}**{branch_tag}{time_tag}: {content}")

                # Add divider between sessions (except after last)
                if i < len(sessions):
                    lines.append("")

            # Add quick actions for all sessions
            show_quick_actions = getattr(self.mcp_config, "show_quick_actions", True)
            if show_quick_actions:
                lines.append("\n**Quick actions:**")
                lines.append("- Continue session: `sequential_thinking(thought='...', session_id='<session_id>', ...)`")
                lines.append("- Get summary: `get_summary(session_id='<session_id>')`")

            return "\n".join(lines)
        else:
            # Plain text format for multiple sessions
            lines = [
                f"Sequential Thinking: {len(sessions)} Active Sessions",
                ""
            ]

            for i, session in enumerate(sessions, 1):
                lines.append(f"Session {i}: {session.session_id}")
                lines.append(f"Progress: {len(session.thoughts)}/{session.total_thoughts_estimate} thoughts")

                # Show fewer thoughts in plain text
                thoughts_to_show = min(max_thoughts, 2)
                recent_thoughts = session.thoughts[-thoughts_to_show:]
                for thought in recent_thoughts:
                    content = thought.content[:80] + "..." if len(thought.content) > 80 else thought.content
                    lines.append(f"  #{thought.number}: {content}")

                if i < len(sessions):
                    lines.append("")

            return "\n".join(lines)

    def _find_system_message_position(self, messages: list) -> int:
        """Find position to insert system message (after all consecutive system messages at start)."""
        # Find the end of consecutive system messages at the beginning
        position = 0
        for i, msg in enumerate(messages):
            role = msg.role if hasattr(msg, 'role') else msg.get('role')
            if role == 'system':
                position = i + 1  # Keep moving past system messages
            else:
                break  # Stop at first non-system message
        return position
