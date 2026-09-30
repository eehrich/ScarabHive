"""Sequential Thinking Tool Server implementation.

This module provides step-by-step reasoning tools for LLMs to break down
complex problems dynamically with support for branching and revision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.hooks.plugin_hook import HookContext, HookResult
from agent_system.utils.id import short_id

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)


def _as_whole(value: Any, name: str, default: int | None = None) -> int | None:
    """A whole number from an int, a whole float or its text; else ValueError."""
    if value is None:
        return default
    if not isinstance(value, bool):
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = None
        if number is not None and number.is_integer():
            return int(number)
    raise ValueError(f"{name} must be a whole number, got {value!r}")


def _as_flag(value: Any, default: bool, name: str) -> bool:
    """A bool from a bool, 0/1 or the text true/false; else ValueError."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ValueError(f"{name} must be true or false, got {value!r}")


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
    # Highest number a NEW thought got. A revision keeps the number it
    # revises, so it must not advance this -- actual_thoughts counts it.
    last_number: int = 0
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


class SequentialThinkingServer(SchemaBasedToolServer):
    """Sequential Thinking tool server for step-by-step reasoning.

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

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig) -> None:
        """
        Initialize Sequential Thinking server.

        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            server_config: Plugin-specific configuration
        """
        super().__init__(name, system_config, server_config)

        # Configuration
        self.max_history_size = int(getattr(server_config, 'max_history_size', 100))
        self.session_ttl_seconds = int(getattr(server_config, 'session_ttl_seconds', 3600))
        self.enable_branching = bool(getattr(server_config, 'enable_branching', True))
        self.enable_revisions = bool(getattr(server_config, 'enable_revisions', True))
        self.max_summary_thoughts = int(getattr(server_config, 'max_summary_thoughts', 10))

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
            "enable_revisions": self.enable_revisions,
            "max_summary_thoughts": self.max_summary_thoughts,
        }

    def _find_session(self, session_id: str | None, agent_session_id: str | None) -> SessionState | None:
        """The session _get_or_create_session would return, if it exists -- without
        creating it, mapping it or refreshing its last_accessed."""
        if session_id:
            return self._sessions.get(session_id)
        valid = [self._sessions[sid] for sid in self._agent_session_mapping.get(agent_session_id, [])
                 if sid in self._sessions] if agent_session_id else []
        return max(valid, key=lambda s: s.last_accessed) if valid else None

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
            logger.info(f"Cleaned up expired session: {sid}")
        if expired:
            self._forget_removed_state()

    def _forget_removed_state(self) -> None:
        """Drop the mapping entries and idempotency keys of what is gone.

        Both only ever grew: clear_history left the mapping of a cleared
        session behind, and no path removed an idempotency key at all, so a
        long-running process kept one entry per key it had ever seen.
        """
        for agent_sid, thinking_sids in list(self._agent_session_mapping.items()):
            thinking_sids[:] = [sid for sid in thinking_sids if sid in self._sessions]
            if not thinking_sids:
                del self._agent_session_mapping[agent_sid]
        live = {t.event_id for s in self._sessions.values() for t in s.thoughts}
        self._idempotency_cache = {
            key: event_id for key, event_id in self._idempotency_cache.items()
            if event_id in live
        }

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
            dropped_thoughts = session.thoughts[:excess]
            dropped = {t.event_id for t in dropped_thoughts}
            session.thoughts = session.thoughts[excess:]
            # The branches hold the same thoughts; untrimmed they kept every
            # thought ever written, so the limit bounded nothing.
            for branch in session.branches.values():
                branch.thoughts = [t for t in branch.thoughts if t.event_id not in dropped]
            # Only the keys of the dropped thoughts, found through the thought
            # itself: this runs on every call of a full session, so no sweep
            # over the whole cache or all sessions here.
            for t in dropped_thoughts:
                key = t.metadata.get("idempotency_key")
                if key is not None and self._idempotency_cache.get(key) == t.event_id:
                    del self._idempotency_cache[key]
            logger.warning(
                f"Memory limit reached for session {session.session_id}, "
                f"removed {excess} oldest thoughts"
            )

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
            # Numbers and flags are parsed, not cast: int() cut 2.5 to 2 and
            # took True as 1, and the string "false" was a true flag.
            next_thought_needed = _as_flag(
                params.get("next_thought_needed", params.get("nextThoughtNeeded")), True,
                "next_thought_needed")
            thought_number = _as_whole(
                params.get("thought_number", params.get("thoughtNumber")), "thought_number", 1)
            total_thoughts = _as_whole(
                params.get("total_thoughts", params.get("totalThoughts")), "total_thoughts", 5)
            session_id = params.get("session_id", params.get("sessionId"))
            is_revision = _as_flag(
                params.get("is_revision", params.get("isRevision")), False, "is_revision")
            revises_thought = _as_whole(
                params.get("revises_thought", params.get("revisesThought")), "revises_thought")
            branch_from_thought = _as_whole(
                params.get("branch_from_thought", params.get("branchFromThought")),
                "branch_from_thought")
            branch_id = params.get("branch_id", params.get("branchId"))
            needs_more_thoughts = _as_flag(
                params.get("needs_more_thoughts", params.get("needsMoreThoughts")), False,
                "needs_more_thoughts")
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
                # The end fired here before, i.e. BEFORE the cached thought was
                # found. On a miss the code falls through and writes a NEW
                # thought whose real end is then dropped (StatusScope.ended).
                # Find the cached thought and return its response
                for s in self._sessions.values():
                    for t in s.thoughts:
                        if t.event_id == cached_event_id:
                            await safe_status_call(
                                "end",
                                f"Idempotent hit: thought #{t.number} in session "
                                f"{s.session_id} ({len(s.thoughts)} thoughts, "
                                f"key={idempotency_key[:8]}...)")
                            return {
                                "status": "success",
                                "session_id": s.session_id,
                                "event_id": t.event_id,
                                "current_thought_number": t.number,
                                "client_thought_number": thought_number,
                                "recorded_thoughts_count": len(s.thoughts),
                                "total_thoughts_estimate": s.total_thoughts_estimate,
                                "next_thought_needed": next_thought_needed,
                                "progress": f"Thought {s.last_number}/{max(s.last_number, s.total_thoughts_estimate)}",
                                "branch": s.current_branch,
                                "thought_history": [],  # Minimal response for cached
                                "warnings": ["Idempotent request - returned cached result"]
                            }

            # Validate parameters BEFORE the session exists: a refused call
            # used to leave an empty session behind, which the hook then
            # showed as the conversation's active one.
            if thought_number < 1:
                await safe_status_call("error", "thought_number must be >= 1")
                return {"status": "error", "error": "thought_number must be >= 1"}
            if total_thoughts < 1:
                await safe_status_call("error", "total_thoughts must be >= 1")
                return {"status": "error", "error": "total_thoughts must be >= 1"}
            if not thought_content.strip():
                await safe_status_call("error", "thought content cannot be empty")
                return {"status": "error", "error": "thought content cannot be empty"}

            # The remaining checks run against the session as it is, WITHOUT
            # creating it or touching it: a refused revision or branch switch
            # must not leave a session behind or keep one alive.
            existing = self._find_session(session_id, agent_session_id)
            known_thoughts = existing.thoughts if existing else []
            known_branches = existing.branches if existing else {"main": None}

            # Validate revision parameters
            if is_revision:
                if revises_thought is None:
                    await safe_status_call("error", "revises_thought is required when is_revision=true")
                    return {"status": "error", "error": "revises_thought is required when is_revision=true"}
                if revises_thought < 1:
                    await safe_status_call("error", "revises_thought must be >= 1")
                    return {"status": "error", "error": "revises_thought must be >= 1"}
                # Check if thought to revise exists
                thought_exists = any(t.number == revises_thought for t in known_thoughts)
                if not thought_exists:
                    logger.info(f"Thought #{revises_thought} not found in session {session_id}")
                    await safe_status_call("error", f"Thought #{revises_thought} not found in session")
                    return {"status": "error", "error": f"Thought #{revises_thought} not found in session"}

            # Check feature flags
            if is_revision and not self.enable_revisions:
                await safe_status_call("error", "Revisions are disabled")
                return {"status": "error", "error": "Revisions are disabled"}
            if branch_id and not self.enable_branching:
                await safe_status_call("error", "Branching is disabled")
                return {"status": "error", "error": "Branching is disabled"}

            creates_branch = (branch_from_thought is not None and branch_id
                              and branch_id not in known_branches)
            if creates_branch and not any(t.number == branch_from_thought for t in known_thoughts):
                await safe_status_call("error", f"Thought #{branch_from_thought} not found")
                return {"status": "error", "error": f"Thought #{branch_from_thought} not found"}
            if branch_id and branch_from_thought is None and branch_id not in known_branches:
                await safe_status_call("error", f"Branch '{branch_id}' not found")
                return {"status": "error", "error": f"Branch '{branch_id}' not found"}

            # All checks passed: only now is the session created or touched.
            session = self._get_or_create_session(session_id, agent_session_id)

            # Server-assigned thought number: a revision keeps the number it
            # revises; a new thought gets the next one. Counting from
            # actual_thoughts (which includes revisions) left a gap after
            # every revision.
            if is_revision and revises_thought is not None:
                server_thought_number = revises_thought
            else:
                server_thought_number = session.last_number + 1

            # Handle branching
            if branch_from_thought is not None and branch_id:
                # Creating new branch - must not exist
                if branch_id in session.branches:
                    # Silently switch to existing branch instead of erroring
                    logger.debug(
                        "Branch '%s' already exists in session %s — switching to it",
                        branch_id, session_id,
                    )
                    session.current_branch = branch_id
                else:
                    await safe_status_call(
                        "progress",
                        f"Creating branch '{branch_id}' from thought {branch_from_thought}"
                    )
                    self._create_branch(session, branch_id, branch_from_thought)
            elif branch_id:
                # Switching to an existing branch (checked above)
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
            if not is_revision:
                session.last_number = server_thought_number

            # Collect warnings for validation issues
            warnings = []

            # Update estimate if needed with auto-clamp and validation
            if needs_more_thoughts or total_thoughts != session.total_thoughts_estimate:
                old_estimate = session.total_thoughts_estimate

                # Auto-clamp: the estimate must not be below the thoughts so far
                # (revisions do not count: they repeat a step, not add one)
                if total_thoughts < session.last_number:
                    warnings.append(
                        f"total_thoughts ({total_thoughts}) < recorded thoughts "
                        f"({session.last_number}), adjusted to {session.last_number}"
                    )
                    total_thoughts = session.last_number
                    # Use the already-parsed local (old_estimate), NOT
                    # params['total_thoughts']: callers may send camelCase
                    # 'totalThoughts', so the subscript raised KeyError here -
                    # AFTER the thought was already persisted and the counter
                    # advanced, leaving the caller told 'error' while the state
                    # had actually changed.
                    logger.info(
                        f"Auto-clamped total_thoughts from {old_estimate} "
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
                # No level= here: StatusScope.progress() takes (message, meta)
                # only, and the extra kwarg raised TypeError -- so from thought
                # 81 on, every call to this tool reported a failure although
                # the thought had already been persisted.
                await safe_status_call(
                    "progress",
                    f"Memory usage: {len(session.thoughts)}/{session.max_history_size} "
                    f"thoughts ({usage_pct:.0f}%)"
                )

            # Consistent progress display: never show X/Y with X > Y
            effective_total = max(session.last_number, session.total_thoughts_estimate)

            # Get the thought that was just added
            current_thought = session.thoughts[-1]

            # Cache event_id for idempotency (if key provided)
            if idempotency_key:
                self._idempotency_cache[idempotency_key] = current_thought.event_id
                current_thought.metadata["idempotency_key"] = idempotency_key
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
                "progress": f"Thought {session.last_number}/{effective_total}",
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
            # The number of this entry, and the thoughts so far (revisions excluded)
            await safe_status_call(
                "end",
                f"Thought #{server_thought_number} added ({session.last_number}/{effective_total}). "
                f"{complete_msg}"
            )

            return result

        except Exception as e:
            logger.info(f"Error in sequentialthinking: {e}")
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
                self._forget_removed_state()

                await safe_status_call(
                    "end", f"Cleared session {session_id} ({thought_count} thoughts)")

                return {
                    "status": "success",
                    "cleared_sessions": 1,
                    "message": f"Cleared session {session_id} ({thought_count} thoughts)"
                }
            else:
                # "All" is all of the calling conversation's sessions. The
                # store is shared by every agent and user of the process, and
                # the tool used to wipe the sessions of all of them.
                agent_session_id = params.get("_session_id")
                if agent_session_id:
                    doomed = [sid for sid in self._agent_session_mapping.get(agent_session_id, [])
                              if sid in self._sessions]
                else:
                    # No conversation (a direct call, the CLI): only sessions
                    # that belong to none -- never another conversation's.
                    owned = {sid for sids in self._agent_session_mapping.values() for sid in sids}
                    doomed = [sid for sid in self._sessions if sid not in owned]
                session_count = len(doomed)

                await safe_status_call("progress", f"Clearing {session_count} session(s)")

                for sid in doomed:
                    del self._sessions[sid]
                self._forget_removed_state()

                await safe_status_call("end", f"Cleared {session_count} session(s)")

                return {
                    "status": "success",
                    "cleared_sessions": session_count,
                    "message": f"Cleared all {session_count} sessions"
                }

        except Exception as e:
            logger.info(f"Error in clear_history: {e}")
            if status:
                await safe_status_call("error", f"Failed to clear history: {str(e)}")
            return {"status": "error", "error": str(e)}

    async def get_summary(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Get summary of current thinking session.

        Tool name: {{ name }}_get_summary → e.g., 'sequential_thinking_get_summary'
        Method called after dispatcher strips prefix → 'get_summary'
        """
        status = params.get("_status")

        # Helper to safely call status methods when status is available
        async def safe_status_call(method_name: str, *args, **kwargs):
            if status:
                method = getattr(status, method_name)
                await method(*args, **kwargs)

        try:
            # Read AFTER status, and explicitly: params["session_id"] raised
            # KeyError while `status` was still unbound, so the except block
            # below reported an UnboundLocalError instead of the real reason.
            session_id = params.get("session_id")
            if not session_id:
                await safe_status_call("error", "session_id is required")
                return {"status": "error", "error": "session_id is required"}
            max_thoughts = _as_whole(params.get("max_thoughts"), "max_thoughts",
                                     self.max_summary_thoughts)
            include_branches = _as_flag(params.get("include_branches"), True, "include_branches")

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
            logger.info(f"Error in get_thought_summary: {e}")
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
            # The server entry's keys, with the calling agent's own
            # hooks.overrides entry on top -- the registry hands that over as
            # context.hook_config, and without this it was read by nobody.
            agent_settings = context.hook_config or {}

            def setting(key: str, default: Any) -> Any:
                return agent_settings.get(key, getattr(self.server_config, key, default))

            max_thoughts = setting("max_thoughts_in_prompt", 5)
            show_branch_info = setting("show_branch_info", True)
            format_type = setting("format", "markdown")
            max_sessions_in_prompt = setting("max_sessions_in_prompt", 1)
            show_quick_actions = setting("show_quick_actions", True)

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

            from agent_system.llm.message_roles import DEVELOPER
            from agent_system.llm.models import ChatMessage

            if active_sessions:
                # Format active sessions (one or multiple)
                if len(active_sessions) == 1:
                    session_prompt = self._format_session_for_prompt(
                        active_sessions[0], max_thoughts, show_branch_info, format_type,
                        show_quick_actions
                    )
                else:
                    # Multiple sessions - format with dividers
                    session_prompt = self._format_multiple_sessions_for_prompt(
                        active_sessions, max_thoughts, show_branch_info, format_type,
                        show_quick_actions
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

            # Append-only: the block is a turn in the history, not a text
            # at the head rebuilt on every call. At the head it changed the
            # prompt prefix every step, so the whole history was paid for
            # again; appended at the end, everything before it stays
            # byte-identical. The previous block stays where it is, and one
            # that compaction took away simply comes back.
            previous = next(
                (msg for msg in reversed(context.messages)
                 if getattr(msg, 'injected_by', None) == self.name), None)
            if previous is not None and previous.content == session_prompt:
                return HookResult(success=True, modified=False, context=context)

            context.messages.append(ChatMessage(
                role=DEVELOPER,
                content=session_prompt,
                injected_by=self.name,
            ))

            return HookResult(success=True, modified=True, context=context)

        except Exception as e:
            logger.error(f"SequentialThinkingHook failed: {e}", exc_info=True)
            # Don't fail the entire LLM call if hook fails
            return HookResult(success=True, modified=False, context=context)

    def _format_thinking_reminder(self) -> str:
        """Format sequential thinking tool reminder when no active session.

        The tool is named after the plugin instance, so the texts use
        self.name rather than a fixed "sequential_thinking".
        """
        return f"""## Sequential Thinking Tool Available

Use `{self.name}()` for complex, multi-step reasoning. Break down problems into thoughts, revise earlier insights, and explore alternatives via branching.

**Start new session:**
```
{self.name}(
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
        format_type: str = "markdown",
        show_quick_actions: bool = True
    ) -> str:
        """Format active session for injection into prompt.

        Nothing here may change unless the session does. The message sits right
        behind the system prompt and is rebuilt before every step; it used to
        carry "Started: 19s ago" and "(8s ago)" per thought, so every call of a
        run changed the front of the prompt and re-billed the whole
        conversation behind it, on steps that never touched this tool.
        """
        if format_type == "markdown":
            lines = []
            lines.append("## Active Sequential Thinking Session\n")
            lines.append(f"**Session ID**: `{session.session_id}`")
            lines.append(f"**Progress**: {session.last_number}/{session.total_thoughts_estimate} thoughts\n")

            # Show recent thoughts
            recent_thoughts = session.thoughts[-max_thoughts:] if max_thoughts > 0 else session.thoughts

            if recent_thoughts:
                lines.append("**Recent thoughts:**")
                for thought in recent_thoughts:
                    branch_tag = f" [{thought.branch_id}]" if show_branch_info and thought.branch_id != "main" else ""
                    revision_tag = f" (revises #{thought.revises_thought})" if thought.is_revision else ""

                    # Truncate long thoughts
                    content = thought.content[:150] + "..." if len(thought.content) > 150 else thought.content

                    lines.append(
                        f"- **Thought #{thought.number}**{branch_tag}{revision_tag}: {content}"
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
                lines.append(f"- Continue: `{self.name}(session_id='{session.session_id}', thought='...', thought_number={session.last_number + 1}, ...)`")
                lines.append(f"- Revise #3: `{self.name}(session_id='{session.session_id}', thought='...', thought_number=3, is_revision=true, revises_thought=3, ...)`")
                lines.append(f"- Branch: `{self.name}(session_id='{session.session_id}', thought='...', branch_from_thought=2, branch_id='alt', ...)`")
            else:
                lines.append(f"\nContinue reasoning with `{self.name}()` or summarize findings if complete.")

            return "\n".join(lines)
        else:
            # Plain text format
            lines = [
                "Sequential Thinking Session Active",
                f"Session ID: {session.session_id}",
                f"Progress: {session.last_number}/{session.total_thoughts_estimate} thoughts",
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
        format_type: str = "markdown",
        show_quick_actions: bool = True
    ) -> str:
        """Format multiple active sessions for injection into prompt."""
        if format_type == "markdown":
            lines = []
            lines.append(f"## Active Sequential Thinking Sessions ({len(sessions)})\n")

            for i, session in enumerate(sessions, 1):
                # Session header
                lines.append(f"### Session {i}: `{session.session_id}`")
                lines.append(f"**Progress**: {session.last_number}/{session.total_thoughts_estimate} thoughts")

                # Show recent thoughts (reduced for multi-session view)
                thoughts_to_show = min(max_thoughts, 3) if max_thoughts > 0 else 3  # [-0:] would be all
                recent_thoughts = session.thoughts[-thoughts_to_show:]

                if recent_thoughts:
                    lines.append("**Recent thoughts:**")
                    for thought in recent_thoughts:
                        branch_tag = f" [{thought.branch_id}]" if show_branch_info and thought.branch_id != "main" else ""

                        # Truncate for compactness
                        content = thought.content[:100] + "..." if len(thought.content) > 100 else thought.content
                        lines.append(f"- **#{thought.number}**{branch_tag}: {content}")

                # Add divider between sessions (except after last)
                if i < len(sessions):
                    lines.append("")

            # Add quick actions for all sessions
            if show_quick_actions:
                lines.append("\n**Quick actions:**")
                lines.append(f"- Continue session: `{self.name}(thought='...', session_id='<session_id>', ...)`")
                lines.append(f"- Get summary: `{self.name}_get_summary(session_id='<session_id>')`")

            return "\n".join(lines)
        else:
            # Plain text format for multiple sessions
            lines = [
                f"Sequential Thinking: {len(sessions)} Active Sessions",
                ""
            ]

            for i, session in enumerate(sessions, 1):
                lines.append(f"Session {i}: {session.session_id}")
                lines.append(f"Progress: {session.last_number}/{session.total_thoughts_estimate} thoughts")

                # Show fewer thoughts in plain text
                thoughts_to_show = min(max_thoughts, 2) if max_thoughts > 0 else 2
                recent_thoughts = session.thoughts[-thoughts_to_show:]
                for thought in recent_thoughts:
                    content = thought.content[:80] + "..." if len(thought.content) > 80 else thought.content
                    lines.append(f"  #{thought.number}: {content}")

                if i < len(sessions):
                    lines.append("")

            return "\n".join(lines)
