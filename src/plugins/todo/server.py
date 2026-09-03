"""
TODO Management Plugin - MCP Server Implementation

Provides persistent task tracking and lifecycle management for agent workflows.
Complements sequential_thinking plugin: tracks WHAT to do (vs HOW to think).

Key features:
- Ultra-minimal design: 1 tool (todo) with multi-mode detection
- Unrestricted status transitions (no state machine)
- DELETE mode integrated (via delete=true parameter)
- Task dependency tracking (depends_on, blocks, circular detection)
- JSON file persistence (data/todos/{session_id}.json)
- System prompt injection hook (auto-inject tasks before LLM calls)
- Progress metrics and filtering
"""

import asyncio
import json
import logging
import re
import threading
from datetime import datetime, UTC
from difflib import SequenceMatcher
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4

from pydantic import BaseModel, Field, field_serializer

from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.hooks.plugin_hook import PluginHook, HookContext, HookResult

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


# =============================================================================
# Data Models
# =============================================================================

class TaskStatus(str, Enum):
    """Task lifecycle states"""
    NOT_STARTED = "not-started"
    IN_PROGRESS = "in-progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class TaskPriority(str, Enum):
    """Task priority levels"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# Status transitions are unrestricted - any status can transition to any other status


class Task(BaseModel):
    """Core task data structure"""

    # Identity
    task_id: str = Field(..., description="Unique task identifier")
    title: str = Field(..., min_length=1, max_length=200, description="Task title")

    # Status & Progress
    status: TaskStatus = Field(default=TaskStatus.NOT_STARTED)
    progress: int = Field(default=0, ge=0, le=100, description="Completion %")

    # Priority
    priority: TaskPriority = Field(default=TaskPriority.MEDIUM)

    # Description
    description: Optional[str] = Field(default=None, max_length=2000)
    tags: List[str] = Field(default_factory=list)

    # Dependencies
    depends_on: List[str] = Field(default_factory=list, description="Blocking task IDs")
    blocks: List[str] = Field(default_factory=list, description="Tasks blocked by this")

    # Integration
    session_id: Optional[str] = Field(default=None)
    agent_name: Optional[str] = Field(default=None)

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: Optional[datetime] = Field(default=None)
    completed_at: Optional[datetime] = Field(default=None)

    # Notes
    notes: List[str] = Field(default_factory=list)

    # Metadata (system-managed, non-user-facing data)
    metadata: Dict[str, str] = Field(default_factory=dict)

    @field_serializer('created_at', 'updated_at', 'started_at', 'completed_at')
    def serialize_datetime(self, dt: Optional[datetime], _info) -> Optional[str]:
        """Serialize datetime to ISO format with 'Z' suffix for UTC."""
        if dt is None:
            return None
        # Replace '+00:00' with 'Z' for cleaner UTC timestamps
        return dt.isoformat().replace('+00:00', 'Z')


class TaskCollection(BaseModel):
    """Session-scoped task collection"""

    session_id: str
    tasks: Dict[str, Task] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: Dict[str, str] = Field(default_factory=dict)

    @field_serializer('created_at', 'updated_at')
    def serialize_datetime(self, dt: datetime, _info) -> str:
        """Serialize datetime to ISO format with 'Z' suffix for UTC."""
        return dt.isoformat().replace('+00:00', 'Z')


# =============================================================================
# Exceptions
# =============================================================================

class TodoError(Exception):
    """Base exception for TODO plugin errors"""
    pass


class ValidationError(TodoError):
    """Task data validation failed"""
    pass


class DependencyError(TodoError):
    """Dependency constraint violated"""
    pass


class StorageError(TodoError):
    """Persistence layer error"""
    pass


# =============================================================================
# TODO Management Server
# =============================================================================

class TodoServer(SchemaBasedMCPServer, PluginHook):
    """
    TODO Management MCP Server with Hook Integration

    Provides task lifecycle tracking with dependency management and persistence.
    Implements PluginHook to inject tasks into system prompts (hooks defined in schema.yaml).
    Hook configuration is loaded from schema.yaml config section.
    """

    def __init__(self, name: str, system_config: "AgentSystemConfig", mcp_config: "MCPConfig"):
        """
        Initialize TODO Management server.

        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            mcp_config: Plugin-specific configuration with:
                - storage_path: Path to JSON storage directory
                - max_tasks_per_session: Limit on tasks per session
                - enable_dependencies: Whether to enforce dependencies
                - auto_save: Auto-save on modifications
        """
        # Initialize MCP server (loads schema.yaml for tools)
        SchemaBasedMCPServer.__init__(self, name, system_config, mcp_config)

        # Initialize PluginHook with hook config from schema.yaml
        # Extract hook config defaults from loaded schema
        hook_config = self._extract_hook_config_from_schema()
        PluginHook.__init__(self, name, config=hook_config)

        # Configuration (using getattr like sequential_thinking)
        self._storage_path = Path(
            getattr(mcp_config, "storage_path", "data/todos")
        )
        self._max_tasks = int(getattr(mcp_config, "max_tasks_per_session", 1000))
        self._enable_deps = bool(getattr(mcp_config, "enable_dependencies", True))
        self._auto_save = bool(getattr(mcp_config, "auto_save", True))

        # In-memory cache: session_id → TaskCollection
        # Limited to prevent memory leaks - sessions are persisted to disk
        self._sessions: Dict[str, TaskCollection] = {}
        self._max_cache_size = int(getattr(mcp_config, 'max_cache_size', 50))

        # Task ID counter per session
        self._task_counters: Dict[str, int] = {}

        # Per-session asyncio locks (event-loop only) to serialize load/save of
        # the SAME session. NOTE: these are never evicted (see
        # _evict_cache_if_needed) so that mutual exclusion can never be broken.
        self._session_locks: Dict[str, asyncio.Lock] = {}

        # Guards all structural access to _sessions / _task_counters. These dicts
        # are mutated from worker threads (load/save/evict run via
        # asyncio.to_thread) AND read on the event loop, so a plain threading
        # lock is required — an asyncio.Lock would not serialize the threads.
        # RLock because _load_session holds it and calls _evict_cache_if_needed.
        # Only ever held around fast dict ops, never around disk I/O.
        self._cache_lock = threading.RLock()

        # Create storage directory
        self._storage_path.mkdir(parents=True, exist_ok=True)

        logger.info(
            f"TodoServer initialized (storage={self._storage_path}, "
            f"max_tasks={self._max_tasks})"
        )

    def _extract_hook_config_from_schema(self) -> Dict[str, Any]:
        """
        Extract hook configuration defaults from schema.yaml.

        SchemaBasedMCPServer already loaded schema.yaml via SchemaBaseMixin.
        This method extracts the config section and converts it to runtime values.

        Returns:
            Dict with hook config values (defaults from schema.yaml)
        """
        schema_data = self.get_schema_data()
        schema_config = schema_data.get("config", {})
        hook_config = {}

        for key, value in schema_config.items():
            if isinstance(value, dict) and 'default' in value:
                # Schema format: {key: {type: ..., default: value}}
                hook_config[key] = value['default']
            else:
                # Already a simple value
                hook_config[key] = value

        return hook_config

    # =========================================================================
    # Session Management
    # =========================================================================

    def _get_session_id(self, context: Optional[Dict[str, Any]] = None) -> str:
        """
        Extract or generate session ID from context.

        Args:
            context: MCP tool call context with session metadata

        Returns:
            Session ID string
        """
        if context:
            # Priority 1: Agent-provided session ID (internal, from agent's session tracker)
            if "_session_id" in context:
                return context["_session_id"]

            # Priority 2: Explicit session_id (from CLI or direct calls)
            if "session_id" in context:
                return context["session_id"]

        # Fallback: generate session ID
        return f"session_{uuid4().hex[:12]}"

    def _evict_cache_if_needed(self) -> None:
        """Evict oldest sessions from cache if over limit.

        Sessions are persisted to disk, so eviction only removes from memory.
        They will be reloaded on next access.

        Concurrency: acquires self._cache_lock (RLock) so the snapshot+delete
        cannot race other threads mutating the dicts; re-entrant, so it is also
        safe when called from _load_session which already holds the lock.
        Sessions whose per-session lock is currently held are SKIPPED — they have
        an in-flight load/save and evicting them would drop state another
        coroutine is using. The per-session asyncio.Lock objects are
        intentionally NOT evicted: popping a lock that another coroutine holds
        (or is about to acquire) would let a fresh Lock be created and break
        mutual exclusion.
        """
        with self._cache_lock:
            if len(self._sessions) < self._max_cache_size:
                return

            # Oldest first; skip sessions with an in-flight (locked) lock.
            sessions_by_time = sorted(
                self._sessions.items(),
                key=lambda x: x[1].updated_at or datetime.min.replace(tzinfo=UTC)
            )

            target = len(self._sessions) - (self._max_cache_size // 2)
            evicted = 0
            for session_id, _ in sessions_by_time:
                if evicted >= target:
                    break
                lock = self._session_locks.get(session_id)
                if lock is not None and lock.locked():
                    continue  # in-flight load/save — do not evict
                del self._sessions[session_id]
                self._task_counters.pop(session_id, None)
                # NOTE: do not pop self._session_locks[session_id] (see docstring)
                evicted += 1
                logger.debug(f"Evicted session {session_id} from cache (LRU)")

        if evicted:
            logger.info(f"TodoServer: Evicted {evicted} sessions from cache")

    def _load_session(self, session_id: str) -> TaskCollection:
        """
        Load task collection from storage (lazy loading).

        Args:
            session_id: Session identifier

        Returns:
            TaskCollection (from cache or disk)

        Raises:
            StorageError: If file parsing fails
        """
        # Check cache + evict under the cache lock (fast dict ops only). Same-
        # session concurrency is already serialized by the per-session asyncio
        # lock held in _load_session_async, so the cache miss below cannot be
        # raced by another load of the SAME session; the lock only guards
        # cross-session mutation and eviction.
        with self._cache_lock:
            if session_id in self._sessions:
                return self._sessions[session_id]
            # Evict old entries before adding new one
            self._evict_cache_if_needed()

        # Load from disk (I/O OUTSIDE the cache lock so the event loop is never
        # blocked on file reads held by a worker thread).
        file_path = self._get_storage_path(session_id)

        if file_path.exists():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                collection = TaskCollection(**data)
                with self._cache_lock:
                    self._sessions[session_id] = collection

                logger.debug(
                    f"Loaded session {session_id}: {len(collection.tasks)} tasks"
                )
                return collection

            except Exception as e:
                raise StorageError(
                    f"Failed to load session {session_id}: {e}"
                ) from e

        # Create new session
        collection = TaskCollection(session_id=session_id)
        with self._cache_lock:
            self._sessions[session_id] = collection
            self._task_counters[session_id] = 0

        logger.debug(f"Created new session {session_id}")
        return collection

    def _save_session(self, session_id: str) -> None:
        """
        Persist task collection to JSON file.

        Args:
            session_id: Session identifier

        Raises:
            StorageError: If file write fails
        """
        # Resolve the collection atomically (membership check + fetch) so a
        # concurrent eviction cannot delete it between the two statements.
        with self._cache_lock:
            collection = self._sessions.get(session_id)
        if collection is None:
            logger.warning(f"Cannot save non-existent session {session_id}")
            return

        collection.updated_at = datetime.now(UTC)

        file_path = self._get_storage_path(session_id)

        try:
            # Ensure directory exists
            file_path.parent.mkdir(parents=True, exist_ok=True)

            # Write atomically (write to temp, then rename)
            # Use unique temp file to avoid race conditions between parallel saves
            import time
            temp_path = file_path.with_suffix(f".tmp.{int(time.time() * 1000000)}")

            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(
                    collection.model_dump(mode='json'),
                    f,
                    indent=2,
                    default=str,  # Handle datetime serialization
                )

            # On Windows, replace can fail if file is still open - retry a few times
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    # Verify temp file still exists before replacing
                    if not temp_path.exists():
                        raise FileNotFoundError(f"Temp file disappeared: {temp_path}")
                    temp_path.replace(file_path)
                    break
                except (PermissionError, FileNotFoundError):
                    if attempt < max_retries - 1:
                        time.sleep(0.01)  # 10ms delay
                    else:
                        raise

            logger.debug(
                f"Saved session {session_id}: {len(collection.tasks)} tasks"
            )

        except Exception as e:
            # Clean up temp file if it still exists
            try:
                if 'temp_path' in locals() and temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass
            raise StorageError(
                f"Failed to save session {session_id}: {e}"
            ) from e

    async def _load_session_async(self, session_id: str) -> TaskCollection:
        """Async wrapper for _load_session - runs in thread pool with locking."""
        # Get or create lock for this session
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        
        async with self._session_locks[session_id]:
            return await asyncio.to_thread(self._load_session, session_id)

    async def _save_session_async(self, session_id: str) -> None:
        """Async wrapper for _save_session - runs in thread pool with locking."""
        # Get or create lock for this session
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        
        async with self._session_locks[session_id]:
            await asyncio.to_thread(self._save_session, session_id)

    def _get_storage_path(self, session_id: str) -> Path:
        """Get file path for session storage.

        SECURITY: session_id can reach this from the web router unvalidated
        (web_endpoints.py accepts session_id as a query param with no auth/IDOR
        check). Without sanitization a value like '../../tmp/evil' or an
        absolute path escapes the storage dir -> arbitrary JSON read/write.
        Allow only a safe charset and assert containment.
        """
        if not session_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id):
            raise StorageError(f"Invalid session_id: {session_id!r}")
        path = (self._storage_path / f"{session_id}.json").resolve()
        if not path.is_relative_to(self._storage_path.resolve()):
            raise StorageError(f"session_id escapes storage directory: {session_id!r}")
        return path

    def _generate_task_id(self, session_id: str) -> str:
        """
        Generate unique task ID for session.

        Args:
            session_id: Session identifier

        Returns:
            Task ID (e.g., "task_042")
        """
        # Counter init + increment under the cache lock so the read-modify-write
        # cannot race a concurrent eviction/load mutating the same dicts.
        with self._cache_lock:
            # Initialize counter from existing tasks if not set
            if session_id not in self._task_counters and session_id in self._sessions:
                collection = self._sessions[session_id]
                if collection.tasks:
                    # Find highest task number
                    max_num = 0
                    for task_id in collection.tasks.keys():
                        if task_id.startswith("task_"):
                            try:
                                num = int(task_id.split("_")[1])
                                max_num = max(max_num, num)
                            except (IndexError, ValueError):
                                pass
                    self._task_counters[session_id] = max_num

            counter = self._task_counters.get(session_id, 0)
            counter += 1
            self._task_counters[session_id] = counter

        return f"task_{counter:03d}"

    # =========================================================================
    # Dependency Management
    # =========================================================================

    def _validate_dependencies(
        self, task: Task, collection: TaskCollection
    ) -> None:
        """
        Validate task dependencies exist and no circular refs.

        Args:
            task: Task to validate
            collection: Task collection context

        Raises:
            DependencyError: If validation fails
        """
        if not self._enable_deps:
            return

        # Check all dependencies exist
        for dep_id in task.depends_on:
            if dep_id not in collection.tasks:
                raise DependencyError(
                    f"Dependency '{dep_id}' does not exist"
                )

        # Check for circular dependencies
        self._detect_circular_deps(task, collection)

    def _detect_circular_deps(
        self, task: Task, collection: TaskCollection
    ) -> None:
        """
        Detect circular dependency chains.

        Args:
            task: Task to check
            collection: Task collection context

        Raises:
            DependencyError: If circular dependency detected
        """
        visited: Set[str] = set()
        path: List[str] = []

        def visit(task_id: str) -> None:
            if task_id in path:
                # Circular dependency found
                cycle = " → ".join(path[path.index(task_id):] + [task_id])
                raise DependencyError(
                    f"Circular dependency detected: {cycle}"
                )

            if task_id in visited:
                return

            visited.add(task_id)
            path.append(task_id)

            if task_id in collection.tasks:
                for dep in collection.tasks[task_id].depends_on:
                    visit(dep)

            path.pop()

        # Check from this task
        for dep_id in task.depends_on:
            visit(dep_id)

    def _is_blocked(self, task: Task, collection: TaskCollection) -> bool:
        """
        Check if task is blocked by incomplete dependencies.

        Args:
            task: Task to check
            collection: Task collection context

        Returns:
            True if blocked (has incomplete dependencies)
        """
        if not self._enable_deps or not task.depends_on:
            return False

        for dep_id in task.depends_on:
            if dep_id not in collection.tasks:
                return True  # Missing dependency = blocked

            dep_task = collection.tasks[dep_id]
            if dep_task.status != TaskStatus.COMPLETED:
                return True

        return False

    def _find_similar_tasks(
        self,
        title: str,
        collection: TaskCollection,
        threshold: float = 0.80,
        exclude_completed: bool = True,
    ) -> List[Tuple[str, float, Task]]:
        """
        Find tasks with similar titles using fuzzy string matching.

        Args:
            title: Title to search for
            collection: Task collection to search in
            threshold: Minimum similarity ratio (0.0-1.0, default: 0.80)
            exclude_completed: Skip completed tasks (default: True)

        Returns:
            List of (task_id, similarity_ratio, task) tuples, sorted by similarity (highest first)
        """
        title_normalized = title.lower().strip()
        similar: List[Tuple[str, float, Task]] = []

        for task_id, task in collection.tasks.items():
            # Skip completed tasks if requested
            if exclude_completed and task.status == TaskStatus.COMPLETED:
                continue

            # Calculate similarity ratio
            task_title_normalized = task.title.lower().strip()
            ratio = SequenceMatcher(None, title_normalized, task_title_normalized).ratio()

            # Add to results if above threshold
            if ratio >= threshold:
                similar.append((task_id, ratio, task))

        # Sort by similarity (highest first)
        similar.sort(key=lambda x: x[1], reverse=True)

        return similar

    def _update_blocked_status(
        self, task_id: str, collection: TaskCollection
    ) -> None:
        """
        Update blocked status for task and its dependents.

        When a task is completed, unblock dependent tasks.
        When a task becomes incomplete, mark dependent tasks as blocked.
        Also checks if the task itself should be blocked.

        Args:
            task_id: Task that changed
            collection: Task collection context
        """
        if not self._enable_deps:
            return

        changed_task = collection.tasks.get(task_id)
        if not changed_task:
            return

        # Check if THIS task should be blocked (new dependencies added)
        is_this_blocked = self._is_blocked(changed_task, collection)
        if is_this_blocked and changed_task.status == TaskStatus.IN_PROGRESS:
            changed_task.status = TaskStatus.BLOCKED
            changed_task.updated_at = datetime.now(UTC)
            logger.debug(
                f"Auto-blocked {changed_task.task_id} "
                f"(has incomplete dependencies)"
            )
        elif not is_this_blocked and changed_task.status == TaskStatus.BLOCKED:
            # Unblock if all dependencies are met
            changed_task.status = TaskStatus.NOT_STARTED
            changed_task.updated_at = datetime.now(UTC)
            logger.debug(
                f"Auto-unblocked {changed_task.task_id} "
                f"(all dependencies met)"
            )

        # Find tasks that depend on this task
        for other_task in collection.tasks.values():
            if task_id in other_task.depends_on:
                is_blocked = self._is_blocked(other_task, collection)

                # Auto-update blocked status
                if is_blocked and other_task.status == TaskStatus.IN_PROGRESS:
                    other_task.status = TaskStatus.BLOCKED
                    other_task.updated_at = datetime.now(UTC)
                    logger.debug(
                        f"Auto-blocked {other_task.task_id} "
                        f"(dependency {task_id} incomplete)"
                    )
                elif not is_blocked and other_task.status == TaskStatus.BLOCKED:
                    # Unblock if all dependencies are met
                    other_task.status = TaskStatus.NOT_STARTED
                    other_task.updated_at = datetime.now(UTC)
                    logger.debug(
                        f"Auto-unblocked {other_task.task_id} "
                        f"(dependency {task_id} completed)"
                    )

    # =========================================================================
    # Status Transition Validation
    # =========================================================================

    def _update_timestamps(self, task: Task, new_status: TaskStatus) -> None:
        """
        Update task timestamps based on status change.

        Args:
            task: Task to update
            new_status: New status
        """
        now = datetime.now(UTC)
        task.updated_at = now

        # Set started_at on first IN_PROGRESS
        if new_status == TaskStatus.IN_PROGRESS and not task.started_at:
            task.started_at = now

        # Set completed_at on COMPLETED
        if new_status == TaskStatus.COMPLETED:
            task.completed_at = now
            task.progress = 100

        # Clear completed_at if moving away from COMPLETED
        if task.status == TaskStatus.COMPLETED and new_status != TaskStatus.COMPLETED:
            task.completed_at = None

    # =========================================================================
    # Tool Implementations (Multi-Mode)
    # =========================================================================

    async def execute(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Main tool entry point - called when tool name matches server name ({{name}}).

        Multi-mode task management tool with explicit operation parameter.

        Operations:
        - create: Create new task (requires: title)
        - update: Update existing task (requires: task_id + fields)
        - delete: Delete task (requires: task_id, optional: cascade)
        - list: Query tasks with filters (optional: filter_status, filter_priority, filter_tags, only_unblocked, limit)
        - get: Get single task details (requires: task_id)
        - summary: Get progress statistics (no parameters required)

        Args:
            params: Tool parameters dict containing:
                - operation: Required operation type (create/update/delete/list/get/summary)
                - title: Task title (required for CREATE)
                - task_id: Task ID (required for UPDATE/GET/DELETE)
                - description, status, progress, priority, tags, depends_on, note: Task fields
                - thinking_session_id, thought_number: Integration fields
                - filter_status, filter_priority, filter_tags, only_unblocked, limit: LIST query filters
                - cascade: Delete dependent tasks (for DELETE operation)
                - context: System parameters (includes _status for progress reporting)

        Returns:
            Dict with operation type and result (task/tasks/summary)
        """
        # Extract operation (required)
        operation = params.get("operation")
        if not operation:
            raise ValidationError(
                "Missing required parameter 'operation'. "
                "Must be one of: create, update, delete, list, get, summary"
            )

        # Extract common parameters
        task_id = params.get("task_id")
        title = params.get("title")
        description = params.get("description")
        status = params.get("status")
        progress = params.get("progress")
        priority = params.get("priority", "medium")
        tags = params.get("tags")
        depends_on = params.get("depends_on")
        add_depends_on = params.get("add_depends_on")
        remove_depends_on = params.get("remove_depends_on")
        note = params.get("note")
        filter_status = params.get("filter_status")
        filter_priority = params.get("filter_priority")
        filter_tags = params.get("filter_tags")
        only_unblocked = params.get("only_unblocked", False)
        limit = params.get("limit")
        cascade = params.get("cascade", False)
        allow_duplicates = params.get("allow_duplicates", False)
        idempotency_key = params.get("idempotency_key")
        context = params.get("context") or {}

        # Extract session_id from params and inject into context
        # Priority 1: _session_id (internal, from agent)
        if "_session_id" in params:
            context["_session_id"] = params["_session_id"]
        # Priority 2: session_id (from tool parameter or CLI)
        elif "session_id" in params:
            context["session_id"] = params["session_id"]

        # Inject agent_name from params into context
        if "_agent_name" in params:
            context["agent_name"] = params["_agent_name"]

        # Inject _status from params into context for helper methods
        if "_status" in params:
            context["_status"] = params["_status"]

        # ============================================================
        # Operation dispatch
        # ============================================================

        if operation == "create":
            if not title:
                msg = "CREATE operation requires 'title' parameter"
                logger.info("Manage rejected (missing title): operation=create")
                return {
                    "status": "validation_failed",
                    "message": msg,
                }
            return await self.create_todo(
                title=title,
                description=description,
                priority=priority,
                tags=tags,
                depends_on=depends_on,
                allow_duplicates=allow_duplicates,
                idempotency_key=idempotency_key,
                context=context,
            )

        elif operation == "update":
            if not task_id:
                msg = "UPDATE operation requires 'task_id' parameter"
                logger.info("Manage rejected (missing task_id): operation=update")
                return {
                    "status": "validation_failed",
                    "message": msg,
                }

            # Handle depends_on parameter: if passed directly, treat as "set" operation
            # (replace all dependencies), otherwise use add/remove for incremental changes
            set_depends_on = None
            final_add_depends_on = add_depends_on
            final_remove_depends_on = remove_depends_on

            if depends_on is not None:
                # User passed depends_on directly → replace all dependencies
                set_depends_on = depends_on if isinstance(depends_on, list) else [depends_on]

            return await self.update_todo(
                task_id=task_id,
                new_status=status,
                progress=progress,
                priority=priority if priority != "medium" else None,
                add_note=note,
                add_tags=tags,
                set_depends_on=set_depends_on,
                add_depends_on=final_add_depends_on,
                remove_depends_on=final_remove_depends_on,
                context=context,
            )

        elif operation == "delete":
            if not task_id:
                msg = "DELETE operation requires 'task_id' parameter"
                logger.info("Manage rejected (missing task_id): operation=delete")
                return {
                    "status": "validation_failed",
                    "message": msg,
                }
            return await self._delete_todo_impl(
                task_id=task_id,
                cascade=cascade,
                context=context,
            )

        elif operation == "list":
            return await self.list_todos(
                filter_status=filter_status,
                filter_priority=filter_priority,
                filter_tags=filter_tags,
                only_unblocked=only_unblocked,
                limit=limit,
                context=context,
            )

        elif operation == "get":
            if not task_id:
                msg = "GET operation requires 'task_id' parameter"
                logger.info("Manage rejected (missing task_id): operation=get")
                return {
                    "status": "validation_failed",
                    "message": msg,
                }
            return await self.get_todo(
                task_id=task_id,
                context=context,
            )

        elif operation == "summary":
            return await self.get_progress_summary(
                context=context,
            )

        else:
            msg = (
                f"Invalid operation '{operation}'. "
                f"Must be one of: create, update, delete, list, get, summary"
            )
            logger.info(f"Manage rejected (invalid operation): {operation}")
            return {
                "status": "validation_failed",
                "message": msg,
            }

    async def create_todo(
        self,
        title: str,
        description: Optional[str] = None,
        priority: str = "medium",
        tags: Optional[List[str]] = None,
        depends_on: Optional[List[str]] = None,
        allow_duplicates: bool = False,
        idempotency_key: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Create a new task with optional duplicate detection.

        Args:
            title: Task title (required, max 200 chars)
            description: Detailed description (optional, max 2000 chars)
            priority: Priority level (low/medium/high/critical)
            tags: List of tags for categorization
            depends_on: List of task IDs this task depends on
            allow_duplicates: Allow creating duplicate tasks (default: False)
            idempotency_key: Optional key to prevent duplicate creation on retries
            context: MCP tool call context

        Returns:
            Created task details with task_id OR existing task if duplicate found

        Raises:
            ValidationError: If validation fails or duplicate found (when allow_duplicates=False)
            DependencyError: If dependencies invalid
            StorageError: If save fails
        """
        session_id = self._get_session_id(context)

        # Get status for progress reporting
        status = context.get("_status") if context else None

        try:
            # Load session (async to avoid blocking event loop)
            collection = await self._load_session_async(session_id)

            # Check for idempotency key
            if idempotency_key:
                # Search for existing task with this idempotency key in metadata
                for task_id, task in collection.tasks.items():
                    if task.metadata.get("idempotency_key") == idempotency_key:
                        if status:
                            await status.end(f"Returned existing {task_id} (idempotency key)")
                        return {
                            "task_id": task_id,
                            "status": "exists",
                            "reason": "idempotency_key",
                            "is_blocked": self._is_blocked(task, collection),
                            "task": task.model_dump(mode='json'),
                        }

            # Check for duplicate titles (if not allowed)
            if not allow_duplicates:
                similar = self._find_similar_tasks(
                    title=title,
                    collection=collection,
                    threshold=0.80,  # 80% similarity
                    exclude_completed=True,
                )

                if similar:
                    # Found similar task(s)
                    best_match_id, similarity, best_match = similar[0]

                    # Exact match (100%) or very high similarity (>= 95%)
                    if similarity >= 0.95:
                        if status:
                            await status.end(
                                f"Returned existing {best_match_id} "
                                f"(title {similarity*100:.0f}% similar)"
                            )
                        return {
                            "task_id": best_match_id,
                            "status": "exists",
                            "reason": "duplicate_title",
                            "similarity": similarity,
                            "is_blocked": self._is_blocked(best_match, collection),
                            "task": best_match.model_dump(mode='json'),
                        }

                    # High similarity (80-95%) - inform but allow creation
                    elif similarity >= 0.80:
                        logger.warning(
                            f"Creating task similar to existing {best_match_id}: "
                            f"'{title}' vs '{best_match.title}' ({similarity*100:.0f}% similar)"
                        )
                        if status:
                            await status.progress(
                                f"⚠️  Similar task exists: {best_match_id} "
                                f"({similarity*100:.0f}% match)"
                            )

            # Check task limit
            if len(collection.tasks) >= self._max_tasks:
                msg = f"Session has reached max tasks limit ({self._max_tasks})"
                logger.info(f"Create rejected (max tasks limit): {session_id}")
                if status:
                    await status.error(msg)
                return {
                    "status": "limit_reached",
                    "message": msg,
                }

            # Generate task ID
            task_id = self._generate_task_id(session_id)

            # Parse priority
            try:
                priority_enum = TaskPriority(priority.lower())
            except ValueError:
                msg = (
                    f"Invalid priority '{priority}'. "
                    f"Must be: low, medium, high, critical"
                )
                logger.info(f"Create rejected (invalid priority): {priority}")
                if status:
                    await status.error(msg)
                return {
                    "status": "validation_failed",
                    "message": msg,
                }

            # Prepare tags and metadata
            task_tags = tags or []
            task_metadata: Dict[str, str] = {}

            # Store idempotency key in metadata (not as tag)
            if idempotency_key:
                task_metadata["idempotency_key"] = idempotency_key

            # Create task
            task = Task(
                task_id=task_id,
                title=title,
                description=description,
                priority=priority_enum,
                tags=task_tags,
                depends_on=depends_on or [],
                session_id=session_id,
                agent_name=context.get("agent_name") if context else None,
                metadata=task_metadata,
            )

            # Validate dependencies
            try:
                self._validate_dependencies(task, collection)
            except DependencyError as e:
                msg = str(e)
                logger.info(f"Create rejected (dependency error): {msg}")
                if status:
                    await status.error(msg)
                return {
                    "status": "validation_failed",
                    "message": msg,
                }

            # Update reverse dependency (blocks) on parent tasks
            for dep_id in task.depends_on:
                dep_task = collection.tasks[dep_id]
                if task_id not in dep_task.blocks:
                    dep_task.blocks.append(task_id)

            # Check if blocked
            is_blocked = self._is_blocked(task, collection)
            if is_blocked:
                task.status = TaskStatus.BLOCKED

            # Add to collection
            collection.tasks[task_id] = task

            # Save
            if self._auto_save:
                await self._save_session_async(session_id)

            # Short status message
            if status:
                await status.end(
                    f"Created {task_id}" + (" (blocked)" if is_blocked else "")
                    + f": {title[:60]}")

            return {
                "task_id": task_id,
                "status": "created",
                "is_blocked": is_blocked,
                "task": task.model_dump(mode='json'),
            }

        except Exception as e:
            logger.error(f"Error creating task: {e}", exc_info=True)
            if status:
                await status.error(f"Internal error: {e}")
            raise TodoError(f"Failed to create task: {e}") from e

    async def update_todo(
        self,
        task_id: str,
        new_status: Optional[str] = None,
        progress: Optional[int] = None,
        priority: Optional[str] = None,
        add_note: Optional[str] = None,
        add_tags: Optional[List[str]] = None,
        remove_tags: Optional[List[str]] = None,
        set_depends_on: Optional[List[str]] = None,
        add_depends_on: Optional[List[str]] = None,
        remove_depends_on: Optional[List[str]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Update task fields.

        Args:
            task_id: Task identifier
            new_status: New status (validates transitions)
            progress: Completion percentage (0-100)
            priority: New priority level
            add_note: Append note to task history
            add_tags: Tags to add
            remove_tags: Tags to remove
            set_depends_on: Replace all dependencies (None = no change)
            add_depends_on: Dependencies to add (incremental)
            remove_depends_on: Dependencies to remove (incremental)
            context: MCP tool call context

        Returns:
            Updated task with change summary

        Raises:
            ValidationError: If task not found or validation fails
            StorageError: If save fails
        """
        session_id = self._get_session_id(context)

        # Get status for progress reporting
        status = context.get("_status") if context else None

        try:
            # Load session
            collection = await self._load_session_async(session_id)

            # Find task
            if task_id not in collection.tasks:
                msg = f"Task '{task_id}' not found"
                logger.info(f"Update rejected (task not found): {task_id}")
                if status:
                    await status.error(msg)
                return {
                    "task_id": task_id,
                    "status": "not_found",
                    "message": msg,
                }

            task = collection.tasks[task_id]
            changes = {}

            # Save old values BEFORE any updates (for accurate change tracking)
            old_progress = task.progress if progress is not None else None

            # Update status
            if new_status:
                try:
                    new_status_enum = TaskStatus(new_status)
                except ValueError:
                    raise ValidationError(
                        f"Invalid status '{new_status}'. "
                        f"Must be: not-started, in-progress, completed, blocked, cancelled"
                    )

                # Check if trying to manually set 'blocked' status
                if new_status_enum == TaskStatus.BLOCKED:
                    # Check if task actually has unmet dependencies
                    is_actually_blocked = self._is_blocked(task, collection)
                    if not is_actually_blocked:
                        msg = (
                            "Cannot manually set status to 'blocked'. "
                            "This status is automatically managed based on dependencies. "
                            "Task has no unmet dependencies."
                        )
                        logger.info(f"Update rejected (blocked is auto-managed): {task_id}")
                        if status:
                            await status.error(msg)
                        return {
                            "task_id": task_id,
                            "status": "validation_failed",
                            "message": msg,
                        }
                    # If task IS blocked, allow the status (it's redundant but harmless)

                old_status = task.status
                task.status = new_status_enum
                self._update_timestamps(task, new_status_enum)

                changes["status"] = f"{old_status.value} → {new_status_enum.value}"

                # Update dependent tasks
                self._update_blocked_status(task_id, collection)

            # Update progress
            if progress is not None:
                if not 0 <= progress <= 100:
                    msg = "Progress must be between 0 and 100"
                    logger.info(f"Update rejected (invalid progress): {progress}")
                    if status:
                        await status.error(msg)
                    return {
                        "task_id": task_id,
                        "status": "validation_failed",
                        "message": msg,
                    }

                task.progress = progress
                task.updated_at = datetime.now(UTC)

                changes["progress"] = f"{old_progress} → {progress}"

            # Update priority
            if priority:
                try:
                    priority_enum = TaskPriority(priority.lower())
                except ValueError:
                    msg = (
                        f"Invalid priority '{priority}'. "
                        f"Must be: low, medium, high, critical"
                    )
                    logger.info(f"Update rejected (invalid priority): {priority}")
                    if status:
                        await status.error(msg)
                    return {
                        "task_id": task_id,
                        "status": "validation_failed",
                        "message": msg,
                    }

                old_priority = task.priority
                task.priority = priority_enum
                task.updated_at = datetime.now(UTC)

                changes["priority"] = f"{old_priority.value} → {priority_enum.value}"

            # Add note
            if add_note:
                timestamp = datetime.now(UTC).isoformat().replace('+00:00', 'Z')
                note = f"[{timestamp}] {add_note}"
                task.notes.append(note)
                task.updated_at = datetime.now(UTC)

                changes["notes"] = f"Added note: {add_note[:50]}..."

            # Manage tags
            if add_tags:
                for tag in add_tags:
                    if tag not in task.tags:
                        task.tags.append(tag)
                task.updated_at = datetime.now(UTC)
                changes["tags_added"] = ", ".join(add_tags)  # type: ignore[assignment]

            if remove_tags:
                for tag in remove_tags:
                    if tag in task.tags:
                        task.tags.remove(tag)
                task.updated_at = datetime.now(UTC)
                changes["tags_removed"] = ", ".join(remove_tags)  # type: ignore[assignment]

            # Manage dependencies - SET (replace all)
            if set_depends_on is not None:
                # Validate all new dependencies exist
                for dep_id in set_depends_on:
                    if dep_id not in collection.tasks:
                        msg = f"Dependency '{dep_id}' does not exist"
                        logger.info(f"Update rejected (dependency not found): {dep_id}")
                        if status:
                            await status.error(msg)
                        return {
                            "task_id": task_id,
                            "status": "validation_failed",
                            "message": msg,
                        }

                # Remove old dependencies (update reverse links)
                old_deps = task.depends_on.copy()
                for dep_id in old_deps:
                    if dep_id in collection.tasks and task_id in collection.tasks[dep_id].blocks:
                        collection.tasks[dep_id].blocks.remove(task_id)

                # Set new dependencies
                task.depends_on = list(set_depends_on)  # Remove duplicates

                # Add reverse dependencies (blocks)
                for dep_id in task.depends_on:
                    if task_id not in collection.tasks[dep_id].blocks:
                        collection.tasks[dep_id].blocks.append(task_id)

                # Check for circular dependencies
                try:
                    self._detect_circular_deps(task, collection)
                except DependencyError as e:
                    msg = str(e)
                    logger.info(f"Update rejected (circular dependency): {msg}")
                    if status:
                        await status.error(msg)
                    # Rollback to old dependencies
                    for dep_id in task.depends_on:
                        if dep_id in collection.tasks and task_id in collection.tasks[dep_id].blocks:
                            collection.tasks[dep_id].blocks.remove(task_id)
                    task.depends_on = old_deps
                    for dep_id in old_deps:
                        if dep_id in collection.tasks and task_id not in collection.tasks[dep_id].blocks:
                            collection.tasks[dep_id].blocks.append(task_id)
                    return {
                        "task_id": task_id,
                        "status": "validation_failed",
                        "message": msg,
                    }

                task.updated_at = datetime.now(UTC)
                changes["depends_on"] = {
                    "from": old_deps,
                    "to": task.depends_on,
                    "added": [d for d in task.depends_on if d not in old_deps],
                    "removed": [d for d in old_deps if d not in task.depends_on]
                }

                # Update blocked status
                self._update_blocked_status(task_id, collection)

            # Manage dependencies - ADD (incremental) - only if set_depends_on was not used
            elif add_depends_on:
                added = []
                for dep_id in add_depends_on:
                    if dep_id not in collection.tasks:
                        msg = f"Dependency '{dep_id}' does not exist"
                        logger.info(f"Update rejected (dependency not found): {dep_id}")
                        if status:
                            await status.error(msg)
                        return {
                            "task_id": task_id,
                            "status": "validation_failed",
                            "message": msg,
                        }
                    if dep_id not in task.depends_on:
                        task.depends_on.append(dep_id)
                        # Update reverse dependency
                        collection.tasks[dep_id].blocks.append(task_id)
                        added.append(dep_id)

                if added:
                    # Check for circular dependencies
                    try:
                        self._detect_circular_deps(task, collection)
                    except DependencyError as e:
                        msg = str(e)
                        logger.info(f"Update rejected (circular dependency): {msg}")
                        if status:
                            await status.error(msg)
                        # Rollback changes
                        for dep_id in added:
                            task.depends_on.remove(dep_id)
                            collection.tasks[dep_id].blocks.remove(task_id)
                        return {
                            "task_id": task_id,
                            "status": "validation_failed",
                            "message": msg,
                        }

                    task.updated_at = datetime.now(UTC)
                    changes["dependencies_added"] = ", ".join(added)  # type: ignore[assignment]

                    # Update blocked status
                    self._update_blocked_status(task_id, collection)

            # Manage dependencies - REMOVE (incremental) - only if set_depends_on was not used
            elif remove_depends_on:
                removed = []
                for dep_id in remove_depends_on:
                    if dep_id in task.depends_on:
                        task.depends_on.remove(dep_id)
                        # Update reverse dependency
                        if dep_id in collection.tasks and task_id in collection.tasks[dep_id].blocks:
                            collection.tasks[dep_id].blocks.remove(task_id)
                        removed.append(dep_id)

                if removed:
                    task.updated_at = datetime.now(UTC)
                    changes["dependencies_removed"] = ", ".join(removed)  # type: ignore[assignment]

                    # Update blocked status
                    self._update_blocked_status(task_id, collection)

            # Save
            if self._auto_save:
                await self._save_session_async(session_id)

            # Calculate is_blocked for response
            is_blocked = self._is_blocked(task, collection)

            # Build dependency info for response
            blocked_by = []
            for dep_id in task.depends_on:
                if dep_id in collection.tasks:
                    dep_task = collection.tasks[dep_id]
                    if dep_task.status != TaskStatus.COMPLETED:
                        blocked_by.append({
                            "task_id": dep_id,
                            "title": dep_task.title,
                            "status": dep_task.status.value,
                        })

            blocking = []
            for block_id in task.blocks:
                if block_id in collection.tasks:
                    block_task = collection.tasks[block_id]
                    blocking.append({
                        "task_id": block_id,
                        "title": block_task.title,
                        "status": block_task.status.value,
                    })

            # Short status message
            if status:
                what = ", ".join(changes) if changes else "no changes"
                await status.end(f"Updated {task_id}: {what[:60]}")

            return {
                "task_id": task_id,
                "status": "updated",
                "changes": changes,
                "is_blocked": is_blocked,
                "dependency_info": {
                    "blocked_by": blocked_by,
                    "blocking": blocking,
                    "all_dependencies_met": len(blocked_by) == 0,
                },
                "task": task.model_dump(mode='json'),
            }

        except Exception as e:
            logger.error(f"Error updating task {task_id}: {e}", exc_info=True)
            if status:
                await status.error(f"Internal error: {e}")
            raise TodoError(f"Failed to update task: {e}") from e

    async def list_todos(
        self,
        filter_status: Optional[List[str]] = None,
        filter_priority: Optional[List[str]] = None,
        filter_tags: Optional[List[str]] = None,
        only_unblocked: bool = False,
        sort_by: str = "created_at",
        limit: Optional[int] = None,
        offset: int = 0,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Query tasks with filters and pagination.

        Args:
            filter_status: Filter by status (can be list)
            filter_priority: Filter by priority (can be list)
            filter_tags: Filter by tags (OR logic)
            only_unblocked: Exclude tasks with incomplete dependencies
            sort_by: Sort field (priority/created_at/updated_at/progress)
            limit: Max results to return
            offset: Number of results to skip (for pagination)
            context: MCP tool call context

        Returns:
            Filtered task list with metadata
        """
        session_id = self._get_session_id(context)

        # Get status for progress reporting
        status = context.get("_status") if context else None

        try:
            # Load session
            collection = await self._load_session_async(session_id)

            # Start with all tasks
            tasks = list(collection.tasks.values())

            # Apply filters
            if filter_status:
                status_set = {TaskStatus(s) for s in filter_status}
                tasks = [t for t in tasks if t.status in status_set]

            if filter_priority:
                priority_set = {TaskPriority(p.lower()) for p in filter_priority}
                tasks = [t for t in tasks if t.priority in priority_set]

            if filter_tags:
                tasks = [
                    t for t in tasks if any(tag in t.tags for tag in filter_tags)
                ]

            if only_unblocked:
                tasks = [t for t in tasks if not self._is_blocked(t, collection)]

            # Sort
            sort_key = {
                "priority": lambda t: (
                    ["critical", "high", "medium", "low"].index(t.priority.value),
                    t.created_at,
                ),
                "created_at": lambda t: t.created_at,
                "updated_at": lambda t: t.updated_at,
                "progress": lambda t: (-t.progress, t.created_at),
            }.get(sort_by, lambda t: t.created_at)

            tasks = sorted(tasks, key=sort_key)

            # Apply pagination (offset + limit)
            filtered_count = len(tasks)
            tasks_page = tasks[offset:]
            if limit and limit > 0:
                tasks_page = tasks_page[:limit]

            # Calculate pagination metadata
            has_more = (offset + len(tasks_page)) < filtered_count
            next_offset = offset + len(tasks_page) if has_more else None

            # Build response
            task_summaries = [
                {
                    "task_id": t.task_id,
                    "title": t.title,
                    "description": t.description,
                    "status": t.status.value,
                    "priority": t.priority.value,
                    "progress": t.progress,
                    "is_blocked": self._is_blocked(t, collection),
                    "created_at": t.created_at.isoformat().replace('+00:00', 'Z'),
                    "updated_at": t.updated_at.isoformat().replace('+00:00', 'Z'),
                    "started_at": t.started_at.isoformat().replace('+00:00', 'Z') if t.started_at else None,
                    "completed_at": t.completed_at.isoformat().replace('+00:00', 'Z') if t.completed_at else None,
                    "depends_on": t.depends_on,
                    "blocks": t.blocks,
                    "tags": t.tags,
                }
                for t in tasks_page
            ]

            # Short status message
            if status:
                await status.end(f"Listed {len(task_summaries)} task(s)")

            return {
                "total_count": len(collection.tasks),
                "filtered_count": filtered_count,
                "returned_count": len(task_summaries),
                "offset": offset,
                "limit": limit,
                "has_more": has_more,
                "next_offset": next_offset,
                "tasks": task_summaries,
                "filters_applied": {
                    k: v
                    for k, v in {
                        "status": filter_status,
                        "priority": filter_priority,
                        "tags": filter_tags,
                        "only_unblocked": only_unblocked,
                    }.items()
                    if v is not None and v is not False
                },
            }

        except Exception as e:
            logger.error(f"Error listing tasks: {e}", exc_info=True)
            if status:
                await status.error(f"Internal error: {e}")
            raise TodoError(f"Failed to list tasks: {e}") from e

    async def get_todo(
        self,
        task_id: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Get complete task details.

        Args:
            task_id: Task identifier
            context: MCP tool call context

        Returns:
            Full task object with dependency info

        Raises:
            ValidationError: If task not found
        """
        session_id = self._get_session_id(context)

        # Get status for progress reporting
        status = context.get("_status") if context else None

        try:
            # Load session
            collection = await self._load_session_async(session_id)

            # Find task
            if task_id not in collection.tasks:
                msg = f"Task '{task_id}' not found"
                logger.info(f"Get rejected (task not found): {task_id}")
                if status:
                    await status.error(msg)
                return {
                    "task_id": task_id,
                    "status": "not_found",
                    "message": msg,
                }

            task = collection.tasks[task_id]

            # Build dependency info
            blocked_by = []
            for dep_id in task.depends_on:
                if dep_id in collection.tasks:
                    dep_task = collection.tasks[dep_id]
                    if dep_task.status != TaskStatus.COMPLETED:
                        blocked_by.append({
                            "task_id": dep_id,
                            "title": dep_task.title,
                            "status": dep_task.status.value,
                        })

            blocking = []
            for block_id in task.blocks:
                if block_id in collection.tasks:
                    block_task = collection.tasks[block_id]
                    blocking.append({
                        "task_id": block_id,
                        "title": block_task.title,
                        "status": block_task.status.value,
                    })

            # Short status message
            if status:
                await status.end(f"Got {task_id}")

            return {
                "task_id": task_id,
                "task": task.model_dump(mode='json'),
                "dependency_info": {
                    "blocked_by": blocked_by,
                    "blocking": blocking,
                    "all_dependencies_met": len(blocked_by) == 0,
                },
            }

        except Exception as e:
            logger.error(f"Error getting task {task_id}: {e}", exc_info=True)
            if status:
                await status.error(f"Internal error: {e}")
            raise TodoError(f"Failed to get task: {e}") from e

    # =========================================================================
    # Delete Todo
    # =========================================================================

    async def delete_todo(
        self,
        task_id: str,
        cascade: bool = False,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Delete a task (public API for web endpoints).

        Args:
            task_id: Task identifier
            cascade: If true, also delete dependent tasks
            context: MCP tool call context

        Returns:
            Deletion summary with cascade list
        """
        return await self._delete_todo_impl(
            task_id=task_id,
            cascade=cascade,
            context=context,
        )

    async def _delete_todo_impl(
        self,
        task_id: str,
        cascade: bool = False,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Internal implementation for delete_todo (called by todo() wrapper).


        Args:
            task_id: Task identifier
            cascade: If true, also delete dependent tasks
            context: MCP tool call context

        Returns:
            Deletion summary with cascade list

        Raises:
            ValidationError: If task not found
            DependencyError: If task has dependents and cascade=False
            StorageError: If save fails
        """
        session_id = self._get_session_id(context)

        # Get status for progress reporting
        status = context.get("_status") if context else None

        try:
            # Load session
            collection = await self._load_session_async(session_id)

            # Find task
            if task_id not in collection.tasks:
                msg = f"Task '{task_id}' not found"
                logger.info(f"Delete rejected (task not found): {task_id}")
                if status:
                    await status.error(msg)
                return {
                    "task_id": task_id,
                    "status": "not_found",
                    "message": msg,
                }

            task = collection.tasks[task_id]

            # Check for dependents - inform agent instead of raising error
            if task.blocks and not cascade:
                msg = (
                    f"Cannot delete task {task_id}: {len(task.blocks)} task(s) depend on it. "
                    f"Use cascade=True to delete all: {', '.join(task.blocks)}"
                )
                logger.info(f"Delete rejected (has dependencies): {task_id}")
                if status:
                    await status.error(msg)
                # Return informative response instead of raising exception
                return {
                    "task_id": task_id,
                    "status": "rejected",
                    "reason": "has_dependents",
                    "message": msg,
                    "dependent_tasks": task.blocks,
                }

            # Cascade delete
            cascade_deleted = []
            if cascade and task.blocks:
                for block_id in list(task.blocks):
                    if block_id in collection.tasks:
                        # Recursive cascade (call implementation directly).
                        # WITHOUT _status: all frames share one StatusScope,
                        # so the deepest child used to call status.end() and
                        # set ended=True -- the end line of the task that was
                        # actually requested was a no-op after that.
                        sub_context = {k: v for k, v in (context or {}).items()
                                       if k != "_status"}
                        sub_result = await self._delete_todo_impl(
                            task_id=block_id,
                            cascade=True,
                            context=sub_context
                        )
                        cascade_deleted.append(block_id)
                        cascade_deleted.extend(sub_result.get("cascade_deleted", []))

            # Remove from parent dependencies
            for dep_id in task.depends_on:
                if dep_id in collection.tasks:
                    dep_task = collection.tasks[dep_id]
                    if task_id in dep_task.blocks:
                        dep_task.blocks.remove(task_id)

            # Delete task
            del collection.tasks[task_id]

            # Save
            if self._auto_save:
                await self._save_session_async(session_id)

            # Short status message
            if status:
                await status.end(
                    f"Deleted {task_id}"
                    + (f" + {len(cascade_deleted)} dependent task(s)"
                       if cascade_deleted else ""))

            return {
                "task_id": task_id,
                "status": "deleted",
                "cascade_deleted": cascade_deleted,
            }

        except Exception as e:
            logger.error(f"Error deleting task {task_id}: {e}", exc_info=True)
            if status:
                await status.error(f"Internal error: {e}")
            raise TodoError(f"Failed to delete task: {e}") from e

    async def get_progress_summary(
        self,
        group_by: str = "status",
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Get progress statistics and metrics.

        Args:
            group_by: Grouping field (status/priority)
            context: MCP tool call context

        Returns:
            Progress summary with completion metrics
        """
        session_id = self._get_session_id(context)

        # Get status for progress reporting
        status = context.get("_status") if context else None

        try:
            # Load session
            collection = await self._load_session_async(session_id)

            if not collection.tasks:
                if status:
                    await status.end("No tasks")
                return {
                    "total_tasks": 0,
                    "by_status": {},
                    "by_priority": {},
                    "overall_progress": 0.0,
                    "completion_rate": "0/0 (0.0%)",
                }

            # Count by status
            by_status = {}
            for task_status in TaskStatus:
                count = sum(
                    1 for t in collection.tasks.values() if t.status == task_status
                )
                by_status[task_status.value] = count

            # Count by priority
            by_priority = {}
            for task_priority in TaskPriority:
                count = sum(
                    1 for t in collection.tasks.values() if t.priority == task_priority
                )
                by_priority[task_priority.value] = count

            # Calculate overall progress (average of all task progress)
            total_progress = sum(t.progress for t in collection.tasks.values())
            overall_progress = total_progress / len(collection.tasks)

            # Completion metrics
            completed_count = by_status.get("completed", 0)
            total_count = len(collection.tasks)
            completion_rate = (
                f"{completed_count}/{total_count} "
                f"({100 * completed_count / total_count:.1f}%)"
            )

            # Find next unblocked tasks
            unblocked_tasks = [
                t
                for t in collection.tasks.values()
                if t.status == TaskStatus.NOT_STARTED
                and not self._is_blocked(t, collection)
            ]
            unblocked_tasks.sort(
                key=lambda t: (
                    ["critical", "high", "medium", "low"].index(t.priority.value),
                    t.created_at,
                )
            )

            next_unblocked = [
                {
                    "task_id": t.task_id,
                    "title": t.title,
                    "priority": t.priority.value,
                }
                for t in unblocked_tasks[:5]  # Top 5
            ]

            # Short status message
            if status:
                await status.end(f"Summary: {completed_count}/{total_count} done")

            return {
                "total_tasks": total_count,
                "by_status": by_status,
                "by_priority": by_priority,
                "overall_progress": round(overall_progress, 1),
                "completion_rate": completion_rate,
                "blocked_count": by_status.get("blocked", 0),
                "next_unblocked": next_unblocked,
            }

        except Exception as e:
            logger.error(f"Error calculating progress: {e}", exc_info=True)
            if status:
                await status.error(f"Internal error: {e}")
            raise TodoError(f"Failed to calculate progress: {e}") from e

    # =========================================================================
    # Hook Implementation (PluginHook interface)
    # =========================================================================

    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """
        Inject TODO tasks into system prompt before LLM call.

        This hook (defined in schema.yaml as inject_todo_tasks) automatically
        adds active tasks from the current session to the agent's context,
        providing task awareness without explicit tool calls.

        Configuration is loaded from schema.yaml config section.

        Args:
            context: Hook context with messages, session_id, agent

        Returns:
            HookResult with modified=True if tasks were injected
        """
        if not context.messages:
            logger.debug("TodoHook: No messages in context, skipping")
            return HookResult(success=True, modified=False, context=context)

        if not context.session_id:
            logger.debug("TodoHook: No session_id in context, skipping")
            return HookResult(success=True, modified=False, context=context)

        try:
            # Get hook config from PluginHook (loaded from schema.yaml via _extract_hook_config_from_schema)
            max_tasks = self.config.get("max_tasks", 20)
            filter_status = self.config.get("filter_status", [
                "not-started", "in-progress", "blocked"
            ])
            include_completed = self.config.get("include_completed", False)
            format_type = self.config.get("format", "markdown")

            # Query tasks from current session
            result = await self.list_todos(
                filter_status=filter_status if not include_completed else None,
                limit=max_tasks,
                context={"session_id": context.session_id}
            )

            # Always inject TODO tool reminder, with or without tasks
            from agent_system.llm.models import ChatMessage

            tasks_list = result.get("tasks", []) if result else []

            # Remove old injection (identified by injected_by attribute)
            for i in range(len(context.messages) - 1, -1, -1):
                if getattr(context.messages[i], 'injected_by', None) == "todo":
                    context.messages.pop(i)

            if tasks_list and len(tasks_list) > 0:
                # Format existing tasks with reminder
                task_prompt = self._format_tasks_for_prompt(tasks_list, format_type)
            else:
                # No tasks yet - inject reminder about todo tool
                task_prompt = self._format_todo_reminder()

            # Insert after first system message
            insert_pos = self._find_system_message_position(context.messages)
            context.messages.insert(insert_pos, ChatMessage(
                role="system",
                content=task_prompt,
                injected_by="todo",
            ))

            return HookResult(success=True, modified=True, context=context)

        except Exception as e:
            logger.error(f"TodoHook failed: {e}", exc_info=True)
            # Don't fail the entire LLM call if hook fails
            return HookResult(success=True, modified=False, context=context)

    def _format_todo_reminder(self) -> str:
        """Format TODO tool reminder when no tasks exist yet."""
        return """## TODO Tool Available

Use `todo()` to break down and track your work. Create tasks to organize complex problems into manageable steps.

Example: `todo(operation="create", title="Analyze data and create report", priority="high")`
"""

    def _format_tasks_for_prompt(self, tasks: list, format_type: str = "markdown") -> str:
        """Format task list for injection into prompt."""
        if format_type == "markdown":
            # Start with reminder
            lines = [self._format_todo_reminder().rstrip()]
            lines.append("\n**Current active tasks:**\n")

            for task in tasks:
                status_icon = self._get_status_icon(task["status"])
                priority_label = self._get_priority_label(task["priority"])

                lines.append(
                    f"- {status_icon} **{task['task_id']}**: {task['title']} "
                    f"[{priority_label}, {task['progress']}%]"
                )

                if task.get("depends_on"):
                    lines.append(f"  - Depends on: {', '.join(task['depends_on'])}")

                if task.get("blocks"):
                    lines.append(f"  - Blocks: {', '.join(task['blocks'])}")

            return "\n".join(lines)

        else:  # text format
            lines = ["=== TODO Tool Available ===\n"]

            for task in tasks:
                lines.append(
                    f"{task['task_id']}: {task['title']} "
                    f"[{task['status']}, {task['priority']}, {task['progress']}%]"
                )

            return "\n".join(lines)

    def _get_status_icon(self, status: str) -> str:
        """Map status to emoji/icon."""
        icons = {
            "not-started": "☐",
            "in-progress": "⏳",
            "completed": "✅",
            "blocked": "🚫",
            "cancelled": "❌"
        }
        return icons.get(status, "•")

    def _get_priority_label(self, priority: str) -> str:
        """Map priority to short label."""
        labels = {
            "critical": "🔴 CRIT",
            "high": "🟠 HIGH",
            "medium": "🟡 MED",
            "low": "🟢 LOW"
        }
        return labels.get(priority, priority.upper())

    def _find_system_message_position(self, messages: list) -> int:
        """Find position to insert task list (after all consecutive system messages at start)."""
        # Find the end of consecutive system messages at the beginning
        position = 0
        for i, msg in enumerate(messages):
            if msg.role == "system":
                position = i + 1  # Keep moving past system messages
            else:
                break  # Stop at first non-system message
        return position
