# TODO Management Plugin

Persistent task lifecycle tracking and workflow orchestration for agent systems.

## Overview

The TODO Management plugin provides comprehensive task tracking capabilities with dependency management, progress metrics, and seamless integration with the sequential_thinking plugin.

**Key Features:**
- ✅ Complete task lifecycle (not-started → in-progress → completed)
- 🔗 Dependency tracking with circular detection
- 📊 Progress metrics and completion statistics
- 💾 JSON file persistence (session-scoped)
- 🧠 Sequential thinking integration (link tasks to thought processes)
- 🎯 Priority levels and tag-based organization
- 🔍 Advanced filtering (status, priority, tags, unblocked)

## Quick Start

### Installation

The plugin is automatically available when AgentSystem starts. Enable it in `config/plugins.yaml`:

```yaml
todo:
  type: tool_server
  server_config:
    storage_path: data/todos
    max_tasks_per_session: 1000
    enable_dependencies: true
    auto_save: true
```

### Basic Usage

```python
# Create a task
await agent.todo(
    operation="create",
    title="Implement user authentication",
    description="Add JWT-based auth with refresh tokens",
    priority="high",
    tags=["backend", "security"]
)

# Update task status
await agent.todo(
    operation="update",
    task_id="task_001",
    status="in-progress",
    progress=50,
    note="Completed JWT signing logic"
)

# List tasks
result = await agent.todo(
    operation="list",
    filter_status=["in-progress"],
    filter_priority=["high", "critical"]
)

# Get progress summary
summary = await agent.todo(operation="summary")
```

## Tool Reference

### `todo` (Multi-Mode Universal Tool)

The single entry point for all task operations. Operation must be explicitly specified.

**CREATE Mode** (operation: `create`, requires `title`):
```python
await agent.todo(
    operation="create",
    title="Task title",
    description="Optional description",
    priority="medium",  # low/medium/high/critical
    tags=["tag1", "tag2"],
    depends_on=["task_001"],  # Blocking dependencies
    thinking_session_id="abc123",  # Link to sequential_thinking
    thought_number=5,
    allow_duplicates=False,  # Prevent duplicates (default)
    idempotency_key="unique-operation-id"  # Prevent retries creating duplicates
)
```

**Duplicate Detection (CREATE mode only):**
- **Default behavior** (`allow_duplicates=False`): Checks for similar titles before creating
  * 95%+ similarity → Returns existing task (`status='exists'`, `reason='duplicate_title'`)
  * 80-95% similarity → Warns but creates new task
  * <80% similarity → Creates new task without warning
  * Completed tasks are excluded from duplicate check
- **Override:** Set `allow_duplicates=True` to always create new task
- **Idempotency:** Use `idempotency_key` to prevent duplicates on retries (returns existing if key matches)

**Examples:**
```python
# Duplicate prevention (default)
result = await agent.todo(operation="create", title="Fix login bug")
# Returns existing if similar task exists

# Allow duplicates
result = await agent.todo(
    operation="create",
    title="Fix login bug",
    allow_duplicates=True  # Always creates new
)

# Idempotency (safe retries)
result = await agent.todo(
    operation="create",
    title="Deploy v2.1.0",
    idempotency_key="deploy-v2.1.0-20251025"  # Same key returns existing
)
```

**UPDATE Mode** (operation: `update`, requires `task_id`):
```python
await agent.todo(
    operation="update",
    task_id="task_001",
    status="in-progress",  # not-started/in-progress/completed/blocked/cancelled
    progress=75,
    note="Progress update with timestamp"
)
```

**LIST Mode** (operation: `list`, all filters optional):
```python
await agent.todo(
    operation="list",
    filter_status=["in-progress", "not-started"],
    filter_priority=["high"],
    filter_tags=["backend", "security"],
    only_unblocked=True,  # Exclude blocked tasks
    limit=10
)
```

**GET Mode** (operation: `get`, requires `task_id`):
```python
await agent.todo(
    operation="get",
    task_id="task_001"  # Full task details
)
```

**SUMMARY Mode** (operation: `summary`, no parameters needed):
```python
await agent.todo(operation="summary")  # Progress statistics
```

### DELETE Mode (operation: `delete`, requires `task_id`)

Delete tasks with optional cascade.

```python
# Delete single task
await agent.todo(
    operation="delete",
    task_id="task_001"
)

# Delete with cascade (also delete dependent tasks)
await agent.todo(
    operation="delete",
    task_id="task_001",
    cascade=True
)
```

## Workflow Patterns

### 1. Multi-Step Implementation Workflow

```python
# Planning phase
await agent.todo(operation="create", title="Design JWT schema", priority="high", tags=["planning"])
await agent.todo(operation="create", title="Implement JWT signing", depends_on=["task_001"], tags=["implementation"])
await agent.todo(operation="create", title="Add refresh token logic", depends_on=["task_002"], tags=["implementation"])
await agent.todo(operation="create", title="Write authentication tests", depends_on=["task_003"], tags=["testing"])

# Execution
await agent.todo(operation="update", task_id="task_001", status="completed")
await agent.todo(operation="update", task_id="task_002", status="in-progress", progress=60)

# Progress check
summary = await agent.todo(operation="summary")
print(f"Completion: {summary['completion_rate']}")
print(f"Next: {summary['next_unblocked'][0]['title']}")
```

### 2. Integration with Sequential Thinking

```python
# During thinking process
thinking_session = "session_abc123"

# Generate actionable tasks from thoughts
await agent.todo(
    operation="create",
    title="Refactor authentication module",
    thinking_session_id=thinking_session,
    thought_number=3,  # Generated from thought #3
    priority="high"
)

# Later: Query tasks from thinking session
tasks = await agent.todo(operation="list", filter_tags=["thinking_session:abc123"])
```

### 3. Sub-Agent Task Coordination

```python
# Main agent creates tasks
await agent.todo(operation="create", title="Backend API", tags=["backend-team"])
await agent.todo(operation="create", title="Frontend UI", tags=["frontend-team"])
await agent.todo(operation="create", title="Integration tests", depends_on=["task_001", "task_002"], tags=["qa-team"])

# Sub-agents filter by tag
backend_tasks = await agent.todo(operation="list", filter_tags=["backend-team"], only_unblocked=True)
frontend_tasks = await agent.todo(operation="list", filter_tags=["frontend-team"], only_unblocked=True)
```

## Best Practices

### Avoiding Duplicate Tasks

**Problem:** LLMs may accidentally create duplicate tasks when checking status or listing tasks.

**Solutions:**

1. **Use explicit operations** (prevents accidental creation):
   ```python
   # ✅ GOOD: Explicit list operation
   tasks = await agent.todo(operation="list", filter_status=["in-progress"])
   
   # ❌ BAD (old implicit mode): Could accidentally create if title provided
   # tasks = await agent.todo(title="...", filter_status=["in-progress"])
   ```

2. **Rely on automatic duplicate detection** (default behavior):
   ```python
   # First call creates task
   result1 = await agent.todo(operation="create", title="Fix bug #123")
   # task_001 created
   
   # Second call with same/similar title returns existing
   result2 = await agent.todo(operation="create", title="Fix bug #123")
   # Returns task_001 (status='exists', reason='duplicate_title')
   ```

3. **Use idempotency keys for critical operations**:
   ```python
   # Safe to retry - same key returns existing task
   await agent.todo(
       operation="create",
       title="Deploy production v1.2.3",
       idempotency_key="deploy-prod-v1.2.3-20251025",
       priority="critical"
   )
   ```

4. **Check for existing tasks before creating**:
   ```python
   # List similar tasks first
   existing = await agent.todo(
       operation="list",
       filter_tags=["authentication"],
       filter_status=["not-started", "in-progress"]
   )
   
   # Only create if none found
   if not existing["tasks"]:
       await agent.todo(
           operation="create",
           title="Implement OAuth2",
           tags=["authentication"]
       )
   ```

### When to Override Duplicate Detection

Use `allow_duplicates=True` when:
- Creating intentionally similar tasks (e.g., "Code review PR #123", "Code review PR #124")
- Tasks have same title but different context (different sessions, agents, or time periods)
- Testing or debugging scenarios

```python
# Allow multiple similar tasks
for pr_num in [123, 124, 125]:
    await agent.todo(
        operation="create",
        title=f"Code review PR #{pr_num}",
        allow_duplicates=True,  # Creates separate task for each PR
        tags=[f"pr-{pr_num}"]
    )
```

## Task Lifecycle

```
NOT_STARTED ─┬──> IN_PROGRESS ──> COMPLETED (terminal)
             │         │
             │         ├──> BLOCKED (dependency incomplete)
             │         └──> CANCELLED
             │
             └──> CANCELLED ──> NOT_STARTED (reopen)
```

**Valid Transitions:**
- `not-started` → `in-progress`, `cancelled`
- `in-progress` → `completed`, `blocked`, `cancelled`, `not-started` (rollback)
- `blocked` → `in-progress`, `cancelled`
- `completed` → (terminal - no transitions)
- `cancelled` → `not-started` (reopen)

**Auto-Updates:**
- `started_at` set when first moved to `in-progress`
- `completed_at` set when moved to `completed`
- `progress` auto-set to 100 when completed
- `status` auto-set to `blocked` if dependencies incomplete

## Dependency Management

**Features:**
- Circular dependency detection (DFS traversal)
- Auto-blocking when dependencies incomplete
- Cascade delete support
- Dependency chain visualization

**Example:**
```python
# Create dependency chain
task_a = await agent.todo(title="Design schema")
task_b = await agent.todo(title="Implement model", depends_on=[task_a["task_id"]])
task_c = await agent.todo(title="Write tests", depends_on=[task_b["task_id"]])

# Query unblocked tasks (only task_a initially)
unblocked = await agent.todo(only_unblocked=True)

# Complete task_a → task_b becomes unblocked
await agent.todo(task_id=task_a["task_id"], status="completed")
```

## CLI Usage

```bash
# Create task
python -m plugins.todo create \
  --title "Implement feature X" \
  --priority high \
  --tags backend security

# List tasks
python -m plugins.todo list \
  --status in-progress \
  --priority high

# Update task
python -m plugins.todo update task_001 \
  --status completed \
  --progress 100

# Get summary
python -m plugins.todo summary

# Delete task
python -m plugins.todo delete task_001 --cascade
```

## Configuration

**Server Config (`config/plugins.yaml`):**

```yaml
todo:
  type: tool_server
  server_config:
    storage_path: data/todos           # JSON file directory
    max_tasks_per_session: 1000        # Task limit per session
    enable_dependencies: true          # Enable dependency tracking
    auto_save: true                    # Auto-save on modifications
    max_cache_size: 50                 # Max sessions in memory cache (rest on disk)
```

**Agent Allowlists:**

```yaml
meta_agent:
  tools:
    - todo/*

basic_agent:
  tools:
    - todo/todo  # Single tool with all operations
```

## Storage Format

Tasks are persisted as JSON files in `data/todos/{session_id}.json`:

```json
{
  "session_id": "test_session_001",
  "tasks": {
    "task_001": {
      "task_id": "task_001",
      "title": "Implement authentication",
      "status": "in-progress",
      "progress": 60,
      "priority": "high",
      "tags": ["backend", "security"],
      "depends_on": [],
      "blocks": ["task_002"],
      "created_at": "2025-10-24T15:30:00Z",
      "updated_at": "2025-10-24T16:45:00Z",
      "started_at": "2025-10-24T15:35:00Z",
      "notes": [
        "2025-10-24T16:00:00Z: Completed JWT signing",
        "2025-10-24T16:45:00Z: Working on refresh tokens"
      ]
    }
  }
}
```

## Best Practices

1. **Use Dependencies for Sequencing**
   - Define task dependencies to ensure correct execution order
   - Check `only_unblocked` to get next actionable tasks

2. **Link to Thinking Sessions**
   - Associate tasks with sequential_thinking sessions
   - Track WHY tasks were created (thought provenance)

3. **Regular Progress Updates**
   - Update `progress` and add `note` fields frequently
   - Helps with debugging and progress tracking

4. **Tag Organization**
   - Use consistent tag hierarchies (`team:backend`, `type:bug`)
   - Enable efficient filtering across agents

5. **Session Isolation**
   - Each agent session has separate task storage
   - Prevents cross-contamination in multi-agent scenarios

## Integration Examples

### With Sequential Thinking

```python
# Thought generates task
async def process_thought(thought_result):
    if thought_result.get("needs_implementation"):
        await agent.todo(
            title=f"Implement: {thought_result['title']}",
            description=thought_result['details'],
            thinking_session_id=thought_result['session_id'],
            thought_number=thought_result['thought_number'],
            priority="high"
        )

# Query tasks from thinking session
thinking_tasks = await agent.todo(thinking_session_id="session_abc123")
```

### With Multi-Agent Systems

```python
# Orchestrator creates high-level tasks
orchestrator_session = "orchestrator_001"
await agent.todo(title="Backend implementation", tags=["backend-team"], session=orchestrator_session)
await agent.todo(title="Frontend implementation", tags=["frontend-team"], session=orchestrator_session)

# Sub-agent picks up tasks
backend_agent_session = "backend_001"
orchestrator_tasks = load_session(orchestrator_session)
for task in orchestrator_tasks["tasks"].values():
    if "backend-team" in task["tags"]:
        # Clone to sub-agent session
        await agent.todo(title=task["title"], session=backend_agent_session)
```

## Performance

- **Session Load**: O(1) with in-memory caching
- **Task Creation**: O(n) for dependency validation (n = existing tasks)
- **Circular Detection**: O(V + E) where V = tasks, E = dependencies (DFS)
- **Filtering**: O(n) linear scan (acceptable for n < 1000)

**Optimization Tips:**
- Use `limit` parameter for large result sets
- Filter early with `only_unblocked` for actionable tasks
- Archive completed sessions periodically

## Automatic Task Injection (Hook)

The TODO plugin keeps the active tasks in front of the model: before every LLM call the hook appends the list as a `developer` turn at the END of the history, and only when it says something new. It used to insert the list behind the system prompt on every call, where a text that is rebuilt every step invalidates the cached prefix behind it.

### How It Works

**Pre-LLM Hook:**
- Executes before each LLM call
- Queries active tasks from current session
- Formats as markdown and appends it as a `developer` turn; the previous block stays where it is and is superseded by the newer one
- Agent sees tasks automatically in context

**Configuration:**

For the whole instance, in `config/plugins.yaml`:

```yaml
todo:
  type: todo
  enabled: true

  hook_config:
    max_tasks: 20
    filter_status: ["not-started", "in-progress", "blocked"]
    include_completed: false
    format: "markdown"
```

For one agent, in its `config/agents/*.yaml` — these win over the instance:

```yaml
  agent_config:
    hooks:
      enabled: true
      overrides:
        todo.inject_todo_tasks:
          enabled: true
          max_tasks: 5
          format: "text"
```

**Default Behavior:**
- **Max tasks**: 20 (most recent active tasks)
- **Filter**: Not-started, in-progress, blocked
- **Excludes**: Completed, cancelled
- **Format**: Markdown with emojis

**Injected Format:**

```markdown
## Active TODO Tasks

- ☐ **task_001**: Implement authentication [🟠 HIGH, 0%]
  - Depends on: task_000
- ⏳ **task_002**: Write tests [🟡 MED, 50%]
- 🚫 **task_003**: Deploy to prod [🔴 CRIT, 0%]
  - Blocks: task_004

Use `todo()` tool to update task status as you complete work.
```

**Benefits:**
- No need to call `todo()` to see tasks
- Agent maintains task awareness across turns
- Automatic context without token overhead
- Session-isolated (only current session's tasks)

**Disabling:**

```yaml
# Per-agent override
meta_agent:
  agent_config:
    hooks:
      overrides:
        todo.inject_todo_tasks:
          enabled: false
```

## The panel

**Todos** in the launcher under **Agents & tools**; a session's info button offers it too, opened on that session.

- The task list of the session open in the chat, or of the one the link names (`?session_id=`); with no session open
  it says so. Auto refresh runs every 5 s.
- Figures: all tasks, and how many are not started, in progress, blocked, cancelled and completed (with the share
  completed). Filters by status and priority.
- Each task shows its status, id, title, priority, age, tags, description, progress, what it depends on and what it
  blocks; a task not started that waits on another is marked `waiting`. **Start** a task not started that waits on
  no other, **Complete** one in progress, blocked or waiting, **Delete** one no other task depends on (asks first).
  What the server refuses -- a task gone meanwhile, a dependent added, a task an agent finished -- is shown as an
  error, and the list is loaded anew.

Under `/plugins/todo/`: `GET tasks?session_id=`, `POST tasks/{task_id}/start?session_id=` (only a task not started),
`POST tasks/{task_id}/complete?session_id=` (only one not completed or cancelled), `DELETE tasks/{task_id}?session_id=`,
`GET /` (the panel). A refusal answers 404 (task not found) or 409 (a task others depend on, or one no longer in a
status the action takes).

## Troubleshooting

**Task stuck in BLOCKED:**
```python
# Check dependencies
task = await agent.todo(task_id="task_042")
print("Depends on:", task["dependency_info"]["depends_on"])

# Verify parent completion
for dep_id in task["task"]["depends_on"]:
    parent = await agent.todo(task_id=dep_id)
    print(f"{dep_id}: {parent['task']['status']}")
```

**Circular dependency errors:**
- Review dependency chain in error message
- Break cycles by removing redundant dependencies
- Use `get_todo` to visualize full dependency tree

**Session not persisting:**
- Check `auto_save` configuration
- Verify `storage_path` is writable
- Check logs for `StorageError`

## API Reference

See [todo_design.md](../../../docs/todo_design.md) for complete API specification and design details.

## License

Part of AgentSystem project.
