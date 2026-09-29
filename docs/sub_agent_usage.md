# Sub-Agent Management Guide

The Sub-Agent Manager plugin enables meta-agents to spawn, manage, and coordinate persistent sub-agent instances that maintain conversation context across multiple requests.

## Table of Contents

- [Overview](#overview)
- [Core Concepts](#core-concepts)
- [Tool Reference](#tool-reference)
- [Usage Examples](#usage-examples)
- [Best Practices](#best-practices)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)

## Overview

Sub-agents are specialized agent instances that:
- **Maintain persistent state** across multiple interactions
- **Preserve full conversation history** for continuity
- **Support nesting** (by default up to 5 levels below the calling session)
- **Provide automatic context injection** via hooks
- **Track relationships** between parent and child agents

### When to Use Sub-Agents

Use sub-agents for:
- **Multi-stage workflows** (research → analysis → report)
- **Specialized tasks** requiring domain expertise
- **Iterative refinement** with follow-up questions
- **Parallel processing** with independent sub-tasks
- **Long-running operations** that need to resume

## Core Concepts

### Session Hierarchy

```
Root Session (meta_agent)
├── sub_web_research_001 (web_research_agent)
│   └── sub_web_scraper_001 (web_scraper)
├── sub_financial_analyst_002 (financial_analyst_agent)
└── sub_code_reviewer_003 (code_review_agent)
```

- **Root/Parent Session**: The coordinator agent (e.g., `meta_agent`)
- **Sub-Sessions**: Child agent instances with unique instance IDs
- **Nesting Depth**: `max_nesting_depth` (default 5) is the number of levels a SAM instance grants below its caller. Sub-sessions inherit the remaining budget; a SAM further down can only lower it, never raise it

### Instance IDs

Format: `sub_{agent_type}_{counter}`, or `sub_{instance_label}_{counter}` when a label is given (label sanitized to `[a-zA-Z0-9_-]`). The counter is 4-digit, shared across all SAM instances in the process, and each generated ID is checked against existing sessions.

Examples:
- `sub_web_research_agent_4521`
- `sub_financial_analyst_agent_4522`

Always use the returned `instance_id` for follow-ups, not the label.

### Session Metadata

Each parent session tracks sub-agents with metadata:

```json
{
  "metadata": {
    "sub_agents": {
      "sub_web_research_001": {
        "instance_id": "sub_web_research_001",
        "agent_type": "web_research_agent",
        "created_at": "2025-11-02T10:30:00Z",
        "last_used": "2025-11-02T10:35:00Z",
        "status": "active",
        "task_summary": "Research latest AI developments",
        "depth": 2
      }
    }
  }
}
```

## Tool Reference

Each Sub-Agent Manager (SAM) instance provides **one** tool, `{{ name }}_manage_sub_agent`, where `name` is the instance name of the SAM server entry (e.g. `sub_agent_manager_manage_sub_agent`). The `operation` parameter selects one of nine operations:

| Operation | Purpose |
|-----------|---------|
| `create` | Spawn and run a sub-agent (`blocking=true` by default; `false` = async) |
| `continue` | Send a new message to an existing instance (`blocking` as above) |
| `poll` | Check the status of an async run (non-blocking) |
| `wait` | Wait for one instance to finish (blocking) |
| `wait_all` | Wait for several instances (`instance_ids`) |
| `cancel` | Stop a running instance |
| `list` | List sub-agents of the current session |
| `info` | Read an instance's transcript (paged via `offset`/`limit`/`max_chars`) |
| `delete` | Archive one instance (`instance_id`) or several (`instance_ids`) |

The same `instance_id` cannot run concurrently. Pattern for parallel work: several `create` calls with `blocking: false`, then `wait_all`.

### 1. `manage_sub_agent` (create)

**Create a new sub-agent instance**

```json
{
  "tool": "sub_agent_manager_manage_sub_agent",
  "arguments": {
    "operation": "create",
    "agent_type": "web_research_agent",
    "task": "Research the latest developments in quantum computing"
  }
}
```

**Parameters:**
- `operation`: Must be `"create"`
- `agent_type`: Instance name of the agent to spawn (must be allowed by this SAM instance)
- `task`: Initial message/task for the sub-agent
- `instance_label` (optional): Custom label for the instance
- `blocking` (optional, default `true`): `false` starts the run in the background (`"status": "running"`)
- `use_advanced_model` (optional, default `false`): use the agent's advanced LLM chain (ignored if the SAM has `allow_advanced_model: false`)

**Returns:**
```json
{
  "instance_id": "sub_web_research_agent_4521",
  "status": "completed",
  "outcome": "...",
  "result": "Research findings: ...",
  "message_count": 2,
  "agent_type": "web_research_agent"
}
```

### 2. `manage_sub_agent` (continue)

**Continue conversation with an existing sub-agent**

```json
{
  "tool": "sub_agent_manager_manage_sub_agent",
  "arguments": {
    "operation": "continue",
    "instance_id": "sub_web_research_001",
    "message": "Can you provide more details about IBM's quantum roadmap?"
  }
}
```

**Parameters:**
- `operation`: Must be `"continue"`
- `instance_id`: ID of existing sub-agent
- `message`: Follow-up question or instruction

**Returns:**
```json
{
  "instance_id": "sub_web_research_001",
  "status": "completed",
  "result": "IBM's quantum roadmap details: ..."
}
```

### 3. `manage_sub_agent` (list)

**List all sub-agents for current session**

```json
{
  "tool": "sub_agent_manager_manage_sub_agent",
  "arguments": {
    "operation": "list",
    "include_completed": false
  }
}
```

**Parameters:**
- `operation`: Must be `"list"`
- `include_completed` (optional): Include archived sub-agents (default: false; every other one is always listed -- running, idle, interrupted, failed, cancelled)

**Returns:**
```json
{
  "instances": [
    {
      "instance_id": "sub_web_research_001",
      "agent_type": "web_research_agent",
      "status": "idle",
      "created_at": "2025-11-02T10:30:00Z",
      "last_used": "2025-11-02T10:35:00Z",
      "task_summary": "Research latest AI developments",
      "current_activity": null,
      "activity_updated_at": null,
      "message_count": 5
    }
  ],
  "count": 1
}
```

### 4. `manage_sub_agent` (info)

**Read a sub-agent's transcript**

```json
{
  "tool": "sub_agent_manager_manage_sub_agent",
  "arguments": {
    "operation": "info",
    "instance_id": "sub_web_research_001"
  }
}
```

**Parameters (optional):**
- `limit`: Messages per call (default 20, capped at 200)
- `offset`: 0-based start index; omit for the tail (most recent `limit` messages)
- `max_chars`: Truncation per message/tool-call argument (default 4000; 0 or negative = none)

**Returns:**
```json
{
  "instance_id": "sub_web_research_001",
  "agent_type": "web_research_agent",
  "status": "idle",
  "created_at": "2025-11-02T10:30:00Z",
  "last_used": "2025-11-02T10:35:00Z",
  "message_count": 5,
  "task_summary": "Research latest AI developments",
  "messages": [...],
  "window": {"mode": "tail", "start_index": 0, "returned": 5, "total": 5,
             "has_more_before": false, "has_more_after": false}
}
```

### 5. `manage_sub_agent` (delete)

**Archive a sub-agent (soft delete)**

```json
{
  "tool": "sub_agent_manager_manage_sub_agent",
  "arguments": {
    "operation": "delete",
    "instance_id": "sub_web_research_001"
  }
}
```

**Note:** This marks the sub-agent as archived but preserves the session file for audit trail. Pass `instance_ids` instead of `instance_id` to archive several at once.

`poll`, `wait` and `cancel` take an `instance_id`; `wait_all` takes `instance_ids` (all must exist).

## Usage Examples

### Example 1: Multi-Stage Research Workflow

```python
# Stage 1: Create sub-agent for initial research
{
  "tool": "sub_agent_manager_manage_sub_agent",
  "arguments": {
    "operation": "create",
    "agent_type": "web_research_agent",
    "task": "Research the top 5 quantum computing companies"
  }
}
# Returns: {"instance_id": "sub_web_research_001", "result": "..."}

# Stage 2: Follow up with specific question
{
  "tool": "sub_agent_manager_manage_sub_agent",
  "arguments": {
    "operation": "continue",
    "instance_id": "sub_web_research_001",
    "message": "Compare their qubit counts and error rates"
  }
}

# Stage 3: Create analysis sub-agent
{
  "tool": "sub_agent_manager_manage_sub_agent",
  "arguments": {
    "operation": "create",
    "agent_type": "financial_analyst_agent",
    "task": "Analyze investment potential of these quantum companies"
  }
}
```

### Example 2: Parallel Processing

```python
# Create multiple sub-agents in parallel
tasks = [
  {"agent_type": "web_research_agent", "task": "Research Company A"},
  {"agent_type": "web_research_agent", "task": "Research Company B"},
  {"agent_type": "web_research_agent", "task": "Research Company C"}
]

# Each creates a unique sub-agent instance
# sub_web_research_001, sub_web_research_002, sub_web_research_003
```

### Example 3: Nested Sub-Agents

```python
# Level 1: Meta-agent creates project manager
{
  "tool": "sub_agent_manager_manage_sub_agent",
  "arguments": {
    "operation": "create",
    "agent_type": "project_manager_agent",
    "task": "Coordinate website redesign project"
  }
}

# Level 2: Project manager creates design agent
# (project_manager_agent internally calls manage_sub_agent)
{
  "tool": "sub_agent_manager_manage_sub_agent",
  "arguments": {
    "operation": "create",
    "agent_type": "design_agent",
    "task": "Create UI mockups for homepage"
  }
}
```

## Best Practices

### 1. Use Meaningful Task Descriptions

✅ Good:
```json
{"task": "Research quantum computing companies focusing on error correction techniques"}
```

❌ Bad:
```json
{"task": "Research stuff"}
```

### 2. Continue Conversations Instead of Recreating

✅ Good:
```python
# Create once
create_sub_agent(agent_type="researcher", task="Initial research")
# Continue multiple times
continue_sub_agent(instance_id="sub_researcher_001", message="Follow-up question 1")
continue_sub_agent(instance_id="sub_researcher_001", message="Follow-up question 2")
```

❌ Bad:
```python
# Creating new instances for each question (loses context)
create_sub_agent(agent_type="researcher", task="Question 1")
create_sub_agent(agent_type="researcher", task="Question 2")
```

### 3. Clean Up Completed Sub-Agents

```python
# Archive sub-agents when done
{
  "tool": "sub_agent_manager_manage_sub_agent",
  "arguments": {
    "operation": "delete",
    "instance_id": "sub_web_research_001"
  }
}
```

### 4. Use List to Track Active Sub-Agents

```python
# Check what sub-agents are active
{
  "tool": "sub_agent_manager_manage_sub_agent",
  "arguments": {
    "operation": "list",
    "include_completed": false
  }
}
```

### 5. Monitor Nesting Depth

With the default `max_nesting_depth: 5`, five levels of sub-agents fit below the root session. Design workflows to stay within limits:

```
Depth 1: meta_agent (root)
Depth 2: project_manager_agent
Depth 3: design_agent
Depth 4: asset_creator_agent
Depth 5: image_optimizer_agent
Depth 6: format_converter_agent
Depth 7: ❌ EXCEEDS LIMIT
```

## Configuration

### Plugin Configuration

A SAM is a server entry in the `plugins:` section. All knobs are **top-level keys of that entry** — there is no `config:` block:

```yaml
plugins:
  servers:
    sub_agent_manager:
      type: sub_agent_manager
      enabled: true
      allowed_agents:              # instance names; default ['*'] when unset; fnmatch globs allowed
        - research_agent
      blocked_agents: []           # exact names only
      allow_advanced_model: true   # false = caller's use_advanced_model is ignored
      advanced_create_only_agents: []  # use_advanced_model honoured on create only, never on continue
      max_nesting_depth: 5         # levels granted below the caller
      max_sub_agents_per_session: 10
      max_sub_agents_per_type: 3
      auto_archive_on_limit: false # true = archive the oldest instead of failing
      default_wait_timeout: 3600   # seconds
      phase_filtering:
        enabled: false
        phase_variable: workflow_phase
        phase_agents:              # phase value -> allowed agents; "_default" as fallback
          research: [research_agent]
```

`allowed_agents`, `blocked_agents`, `allow_advanced_model`, the limits, phase filtering and the `inject_sub_agent_context` options are hot-reloaded by `agent-cli reload` (POST `/admin/reload-config`). A **new agent definition** needs a restart, because the agent must be registered.

### Enabling a New Sub-Agent

A sub-agent is spawnable only when all of these hold:

1. Its server entry has `enabled: true`.
2. Its instance name is in the calling SAM instance's `allowed_agents` (or matched by `*`/a glob) and not in `blocked_agents`.
3. The calling agent's `agent_config.tools.allowed` contains `<sam instance>/*`.
4. Its `metadata.visibility` is not `private` (the default) — `ui`, `tool` or `both`. Private agents are left out of the "Available" list in the tool description.

The "Available" list applies the same `allowed_agents`/`blocked_agents` check as a spawn, globs included.

### Hook Configuration

The `inject_sub_agent_context` hook appends this SAM instance's sub-agents and their status as a `developer` turn at the end of the history before each LLM call. The hook is registered with `enabled: false`; an agent turns it on with `hooks.overrides: {<sam instance>.inject_sub_agent_context: {enabled: true}}`. The `enabled` option below is the injector's own switch, not the registration. The options are read from `hook_config.inject_sub_agent_context` on the SAM server entry.

The block is marked with `injected_by: sub_agent_manager:<instance>` and written only when it says something new -- a sub-agent added, removed or changed status; rows are ordered open ones first (running, idle, interrupted), each group newest created first, and carry the task, but no usage counters or times. The status is what the sub-agent is doing: `running` while a run is under way (in this process, or in any other that holds the lock beside its session), `idle` once it is over, or `interrupted`, `failed`, `cancelled` for a last run that did not finish. Stored, running and idle are both `active`; the block never says that word. An earlier block keeps its place and is superseded by the newer one: deleting it would rewrite the prefix the provider has already cached. When the last sub-agent is archived, that is news too and is said once.

**Options:**
- `enabled`: Enable/disable context injection (default: true)
- `max_sub_agents_shown`: Limit sub-agents shown, open ones first, then newest created first (default: 10)
- `show_completed`: Include archived sub-agents (default: false); failed and cancelled ones are always shown
- `format`: "markdown" or "text" (default: "markdown")

**Injected Context Example:**

```markdown
## Sub-Agents

**Available agents:** web_research_agent, financial_analyst_agent

| Type | Instance ID | Status | Task |
|------|-------------|--------|------|
| web_research_agent | `sub_web_research_0002` | running | Compare the three vendors' pricing |
| web_research_agent | `sub_web_research_0001` | idle | Collect the vendors' published SLAs |

running: working now. idle: its last run is done; poll or info for the answer, continue for more. interrupted, failed, cancelled: its last run did not finish.

Continue: `sub_agent_manager_manage_sub_agent(operation='continue', instance_id='...', message='...')`
```

## Troubleshooting

### Error: "Maximum nesting depth exceeded"

**Cause:** The calling session has no nesting budget left (`max_nesting_depth` of this SAM, or a smaller budget inherited from an ancestor)

**Solution:** Redesign workflow to reduce nesting or use parallel sub-agents instead of nested ones

```python
# Instead of deep nesting:
meta → project_manager → designer → developer → tester → deployer → monitor (TOO DEEP with max_nesting_depth: 5)

# Use parallel structure:
meta → project_manager
     ├── designer
     ├── developer
     ├── tester
     └── deployer
```

### Error: "Agent type 'xyz' not found in registry"

**Cause:** The sub-agent's server was not built: the entry is missing or `enabled: false`, or its `type` cannot be resolved. Visibility does not cause this error.

**Solution:** Check the server entry of the sub-agent (a new agent definition needs a restart):

```yaml
plugins:
  servers:
    web_research_agent:
      type: multi_turn_agent
      enabled: true
      metadata:
        visibility: "tool"  # not "private" (default), otherwise hidden from the Available list
```

### Error type `agent_blocked` / `phase_blocked`

- `agent_blocked` ("Agent 'x' not allowed by this sub-agent manager"): the name is not matched by this SAM instance's `allowed_agents`, or is in `blocked_agents`. Add it there — `agent-cli reload` is enough.
- `phase_blocked` ("Agent 'x' not allowed in current phase"): phase filtering is on and `phase_agents` does not list the agent for the current phase. A denial by `allowed_agents`/`blocked_agents` is always `agent_blocked`, phase or not.

See [Enabling a New Sub-Agent](#enabling-a-new-sub-agent) for the full checklist.

### Sub-Agent Not Preserving Context

**Cause:** Creating new instances instead of continuing existing ones

**Solution:** Use `operation: "continue"` with the same `instance_id`

```python
# First interaction
result1 = create_sub_agent(agent_type="researcher", task="Initial task")
instance_id = result1["instance_id"]

# Follow-up (preserves context)
result2 = continue_sub_agent(instance_id=instance_id, message="Follow-up question")
```

### Performance Issues with Many Sub-Agents

**Cause:** Too many active sub-agents in a single session

**Solution:**
1. Archive completed sub-agents: `operation: "delete"`
2. Reduce `max_sub_agents_shown` in the hook options
3. Consider splitting into multiple parent sessions

Limits: `max_sub_agents_per_session` (default 10) and `max_sub_agents_per_type` (default 3) count active sub-agents; with `auto_archive_on_limit: true` the oldest one is archived instead of failing.

## Performance Benchmarks

Based on `src/plugins/sub_agent_manager/tests/test_plugin_sub_agent_manager_performance.py`:

| Operation | Target | Actual |
|-----------|--------|--------|
| Sub-agent creation | <100ms | ~50-80ms |
| Concurrent creation (15 agents) | - | ~200ms total (~13ms/agent) |
| List 100 sub-agents | <50ms | ~20-30ms |
| Nested creation (depth 3) | <150ms/level | ~80-120ms/level |
| Session file overhead | <500 bytes/agent | ~300-400 bytes/agent |

## API Reference

For developers integrating sub-agent management:

```python
from plugins.sub_agent_manager.manager import SubAgentManager

# Initialize
manager = SubAgentManager(
    session_service=session_service,
    registry=registry,
    max_nesting_depth=5,
    max_sub_agents_per_type=3,
    max_sub_agents_per_session=10,
    auto_archive_on_limit=False
)

# Create sub-session
sub_id = await manager.create_sub_session(
    parent_session_id="parent_001",
    agent_type="web_research_agent",
    initial_message="Research task",
    params={"_user_id": "user123", "_agent": parent_agent}
)

# List sub-sessions
sub_agents = await manager.list_sub_sessions(
    parent_session_id="parent_001",
    include_completed=False
)

# Update metadata
await manager.update_sub_session_metadata(
    parent_session_id="parent_001",
    sub_session_id=sub_id,
    status="completed",
    last_used=datetime.now(UTC).isoformat()
)
```

## See Also

- [Agent Architecture](../docs/_arch_agent_architecture.md)
- [Session Management](../docs/session_management.md)
- [Plugin Authoring Guide](../docs/plugin_authoring.md)
- [Tool server configuration](../docs/server_configuration.md)
