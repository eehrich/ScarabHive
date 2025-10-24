"""
TODO Management Plugin - MCP Server Implementation

Provides persistent task tracking and lifecycle management for agent workflows.
Complements sequential_thinking plugin: tracks WHAT to do (vs HOW to think).

Key features:
- 6 core tools: create/update/list/get/delete/summary
- Task status lifecycle with validation
- Dependency tracking (depends_on, blocks, circular detection)
- JSON file persistence (data/todos/{session_id}.json)
- Integration with sequential_thinking (thinking_session_id linkage)
- Progress metrics and filtering
"""

import json
import logging
from datetime import datetime, UTC
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set
from uuid import uuid4

from pydantic import BaseModel, Field

from agent_system.mcp.schema_based import SchemaBasedMCPServer

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


class TaskCollection(BaseModel):
    """Session-scoped task collection"""

    session_id: str
    tasks: Dict[str, Task] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: Dict[str, str] = Field(default_factory=dict)


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

class TodoManagementServer(SchemaBasedMCPServer):
    """
    TODO Management MCP Server
    
    Provides task lifecycle tracking with dependency management and persistence.
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
        super().__init__(name, system_config, mcp_config)
        
        # Configuration (using getattr like sequential_thinking)
        self._storage_path = Path(
            getattr(mcp_config, "storage_path", "data/todos")
        )
        self._max_tasks = int(getattr(mcp_config, "max_tasks_per_session", 1000))
        self._enable_deps = bool(getattr(mcp_config, "enable_dependencies", True))
        self._auto_save = bool(getattr(mcp_config, "auto_save", True))

        # In-memory cache: session_id → TaskCollection
        self._sessions: Dict[str, TaskCollection] = {}

        # Task ID counter per session
        self._task_counters: Dict[str, int] = {}

        # Create storage directory
        self._storage_path.mkdir(parents=True, exist_ok=True)

        logger.info(
            f"TodoManagementServer initialized (storage={self._storage_path}, "
            f"max_tasks={self._max_tasks})"
        )

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
        # Check cache first
        if session_id in self._sessions:
            return self._sessions[session_id]

        # Load from disk
        file_path = self._get_storage_path(session_id)

        if file_path.exists():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                collection = TaskCollection(**data)
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
        if session_id not in self._sessions:
            logger.warning(f"Cannot save non-existent session {session_id}")
            return

        collection = self._sessions[session_id]
        collection.updated_at = datetime.now(UTC)

        file_path = self._get_storage_path(session_id)

        try:
            # Ensure directory exists
            file_path.parent.mkdir(parents=True, exist_ok=True)

            # Write atomically (write to temp, then rename)
            temp_path = file_path.with_suffix(".tmp")

            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(
                    collection.model_dump(mode='json'),
                    f,
                    indent=2,
                    default=str,  # Handle datetime serialization
                )

            temp_path.replace(file_path)

            logger.debug(
                f"Saved session {session_id}: {len(collection.tasks)} tasks"
            )

        except Exception as e:
            raise StorageError(
                f"Failed to save session {session_id}: {e}"
            ) from e

    def _get_storage_path(self, session_id: str) -> Path:
        """Get file path for session storage"""
        return self._storage_path / f"{session_id}.json"

    def _generate_task_id(self, session_id: str) -> str:
        """
        Generate unique task ID for session.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Task ID (e.g., "task_042")
        """
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

    def _update_blocked_status(
        self, task_id: str, collection: TaskCollection
    ) -> None:
        """
        Update tasks that depend on this task.
        
        When a task is completed, unblock dependent tasks.
        When a task is incomplete, mark dependent tasks as blocked.
        
        Args:
            task_id: Task that changed
            collection: Task collection context
        """
        if not self._enable_deps:
            return

        changed_task = collection.tasks.get(task_id)
        if not changed_task:
            return

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

    async def todo(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Multi-mode task management tool (CREATE/UPDATE/LIST/GET).
        
        Modes auto-detected:
        - CREATE: Provide title only → creates new task
        - UPDATE: Provide task_id + fields → updates existing task
        - LIST: Provide filters (filter_status/filter_priority/tags) → query tasks
        - GET/SUMMARY: Provide task_id="SUMMARY" → progress stats, else task details
        
        Args:
            params: Tool parameters dict containing:
                - title: Task title (required for CREATE)
                - task_id: For UPDATE/GET, or "SUMMARY" for stats
                - description, status, progress, priority, tags, depends_on, note: Task fields
                - thinking_session_id, thought_number: Integration fields
                - filter_status, filter_priority, only_unblocked, limit: LIST query filters
                - context: System parameters (includes _status for progress reporting)
            
        Returns:
            Dict with operation type and result (task/tasks/summary)
        """
        # Extract parameters
        title = params.get("title")
        task_id = params.get("task_id")
        description = params.get("description")
        status = params.get("status")  # Renamed from status_param
        progress = params.get("progress")
        priority = params.get("priority", "medium")
        tags = params.get("tags")
        depends_on = params.get("depends_on")
        note = params.get("note")
        filter_status = params.get("filter_status")
        filter_priority = params.get("filter_priority")
        only_unblocked = params.get("only_unblocked", False)
        limit = params.get("limit")
        delete = params.get("delete", False)  # NEW: delete mode flag
        cascade = params.get("cascade", False)  # NEW: cascade delete flag
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
        # Mode detection
        # ============================================================
        
        # Mode 0: DEFAULT (no params) → GET SUMMARY
        if not any([task_id, title, status, progress, note, filter_status, filter_priority, tags, only_unblocked]):
            return await self.get_progress_summary(
                context=context,
            )
        
        # Mode 1: GET SUMMARY (explicit)
        if task_id == "SUMMARY":
            return await self.get_progress_summary(
                context=context,
            )
        
        # Mode 2: DELETE task
        if delete and task_id:
            return await self._delete_todo_impl(
                task_id=task_id,
                cascade=cascade,
                context=context,
            )
        
        # Mode 3: GET task details
        if task_id and not any([
            title, status, progress, note, filter_status, filter_priority
        ]):
            return await self.get_todo(
                task_id=task_id,
                context=context,
            )
        
        # Mode 4: LIST tasks
        if any([filter_status, filter_priority, only_unblocked]) or (
            tags and not task_id and not title
        ):
            return await self.list_todos(
                filter_status=filter_status,
                filter_priority=filter_priority,
                filter_tags=tags,
                only_unblocked=only_unblocked,
                limit=limit,
                context=context,
            )
        
        # Mode 4: CREATE
        if title and not task_id:
            return await self.create_todo(
                title=title,
                description=description,
                priority=priority,
                tags=tags,
                depends_on=depends_on,
                context=context,
            )
        
        # Mode 5: UPDATE
        if task_id:
            return await self.update_todo(
                task_id=task_id,
                new_status=status,
                progress=progress,
                priority=priority if priority != "medium" else None,
                add_note=note,
                add_tags=tags,
                context=context,
            )
        
        # Fallback: invalid arguments
        raise ValidationError(
            "Invalid arguments. Modes:\n"
            "- CREATE: provide title\n"
            "- UPDATE: provide task_id + fields\n"
            "- DELETE: provide task_id + delete=True\n"
            "- LIST: provide filter_status/filter_priority/tags\n"
            "- GET: provide task_id or task_id='SUMMARY'"
        )

    async def create_todo(
        self,
        title: str,
        description: Optional[str] = None,
        priority: str = "medium",
        tags: Optional[List[str]] = None,
        depends_on: Optional[List[str]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Create a new task.
        
        Args:
            title: Task title (required, max 200 chars)
            description: Detailed description (optional, max 2000 chars)
            priority: Priority level (low/medium/high/critical)
            tags: List of tags for categorization
            depends_on: List of task IDs this task depends on
            context: MCP tool call context
            
        Returns:
            Created task details with task_id
            
        Raises:
            ValidationError: If validation fails
            DependencyError: If dependencies invalid
            StorageError: If save fails
        """
        session_id = self._get_session_id(context)
        
        # Get status for progress reporting
        status = context.get("_status") if context else None

        try:
            # Load session
            collection = self._load_session(session_id)

            # Check task limit
            if len(collection.tasks) >= self._max_tasks:
                raise ValidationError(
                    f"Session has reached max tasks limit ({self._max_tasks})"
                )

            # Generate task ID
            task_id = self._generate_task_id(session_id)

            # Parse priority
            try:
                priority_enum = TaskPriority(priority.lower())
            except ValueError:
                raise ValidationError(
                    f"Invalid priority '{priority}'. "
                    f"Must be: low, medium, high, critical"
                )

            # Create task
            task = Task(
                task_id=task_id,
                title=title,
                description=description,
                priority=priority_enum,
                tags=tags or [],
                depends_on=depends_on or [],
                session_id=session_id,
                agent_name=context.get("agent_name") if context else None,
            )

            # Validate dependencies
            self._validate_dependencies(task, collection)

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
                self._save_session(session_id)

            # Short status message
            if status:
                await status.end(f"Created {task_id}")

            return {
                "task_id": task_id,
                "status": "created",
                "is_blocked": is_blocked,
                "task": task.model_dump(mode='json'),
            }

        except (ValidationError, DependencyError) as e:
            if status:
                await status.error(f"Failed to create task: {e}")
            raise

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
            collection = self._load_session(session_id)

            # Find task
            if task_id not in collection.tasks:
                raise ValidationError(f"Task '{task_id}' not found")

            task = collection.tasks[task_id]
            changes = {}

            # Update status
            if new_status:
                try:
                    new_status_enum = TaskStatus(new_status)
                except ValueError:
                    raise ValidationError(
                        f"Invalid status '{new_status}'. "
                        f"Must be: not-started, in-progress, completed, blocked, cancelled"
                    )

                old_status = task.status
                task.status = new_status_enum
                self._update_timestamps(task, new_status_enum)

                changes["status"] = f"{old_status.value} → {new_status_enum.value}"

                # Update dependent tasks
                self._update_blocked_status(task_id, collection)

            # Update progress
            if progress is not None:
                if not 0 <= progress <= 100:
                    raise ValidationError("Progress must be between 0 and 100")

                old_progress = task.progress
                task.progress = progress
                task.updated_at = datetime.now(UTC)

                changes["progress"] = f"{old_progress} → {progress}"

            # Update priority
            if priority:
                try:
                    priority_enum = TaskPriority(priority.lower())
                except ValueError:
                    raise ValidationError(
                        f"Invalid priority '{priority}'. "
                        f"Must be: low, medium, high, critical"
                    )

                old_priority = task.priority
                task.priority = priority_enum
                task.updated_at = datetime.now(UTC)

                changes["priority"] = f"{old_priority.value} → {priority_enum.value}"

            # Add note
            if add_note:
                timestamp = datetime.now(UTC).isoformat()
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

            # Save
            if self._auto_save:
                self._save_session(session_id)

            # Short status message
            if status:
                await status.end(f"Updated {task_id}")

            return {
                "task_id": task_id,
                "status": "updated",
                "changes": changes,
                "task": task.model_dump(mode='json'),
            }

        except ValidationError as e:
            if status:
                await status.error(f"Failed to update task: {e}")
            raise

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
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Query tasks with filters.
        
        Args:
            filter_status: Filter by status (can be list)
            filter_priority: Filter by priority (can be list)
            filter_tags: Filter by tags (OR logic)
            only_unblocked: Exclude tasks with incomplete dependencies
            sort_by: Sort field (priority/created_at/updated_at/progress)
            limit: Max results to return
            context: MCP tool call context
            
        Returns:
            Filtered task list with metadata
        """
        session_id = self._get_session_id(context)
        
        # Get status for progress reporting
        status = context.get("_status") if context else None

        try:
            # Load session
            collection = self._load_session(session_id)

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

            # Apply limit
            filtered_count = len(tasks)
            if limit and limit > 0:
                tasks = tasks[:limit]

            # Build response
            task_summaries = [
                {
                    "task_id": t.task_id,
                    "title": t.title,
                    "status": t.status.value,
                    "priority": t.priority.value,
                    "progress": t.progress,
                    "is_blocked": self._is_blocked(t, collection),
                    "created_at": t.created_at.isoformat(),
                    "updated_at": t.updated_at.isoformat(),
                    "tags": t.tags,
                }
                for t in tasks
            ]

            # Short status message
            if status:
                await status.end(f"Listed {len(task_summaries)} task(s)")

            return {
                "total_count": len(collection.tasks),
                "filtered_count": filtered_count,
                "returned_count": len(task_summaries),
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
            collection = self._load_session(session_id)

            # Find task
            if task_id not in collection.tasks:
                raise ValidationError(f"Task '{task_id}' not found")

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

        except ValidationError as e:
            if status:
                await status.error(f"Task not found: {e}")
            raise

        except Exception as e:
            logger.error(f"Error getting task {task_id}: {e}", exc_info=True)
            if status:
                await status.error(f"Internal error: {e}")
            raise TodoError(f"Failed to get task: {e}") from e

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
            collection = self._load_session(session_id)

            # Find task
            if task_id not in collection.tasks:
                raise ValidationError(f"Task '{task_id}' not found")

            task = collection.tasks[task_id]

            # Check for dependents
            if task.blocks and not cascade:
                raise DependencyError(
                    f"Cannot delete task {task_id}: {len(task.blocks)} tasks depend on it. "
                    f"Use cascade=True to delete all: {', '.join(task.blocks)}"
                )

            # Cascade delete
            cascade_deleted = []
            if cascade and task.blocks:
                for block_id in list(task.blocks):
                    if block_id in collection.tasks:
                        # Recursive cascade (call implementation directly)
                        sub_result = await self._delete_todo_impl(
                            task_id=block_id,
                            cascade=True,
                            context=context
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
                self._save_session(session_id)

            # Short status message
            if status:
                await status.end(f"Deleted {task_id}")

            return {
                "task_id": task_id,
                "status": "deleted",
                "cascade_deleted": cascade_deleted,
            }

        except (ValidationError, DependencyError) as e:
            if status:
                await status.error(f"Failed to delete task: {e}")
            raise

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
            collection = self._load_session(session_id)

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
