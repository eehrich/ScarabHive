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
- **Support nesting** up to 5 levels deep
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
- **Nesting Depth**: Maximum 5 levels to prevent infinite recursion

### Instance IDs

Format: `sub_{agent_type}_{counter}`

Examples:
- `sub_web_research_001`
- `sub_financial_analyst_002`
- `sub_code_reviewer_003`

Instance IDs are globally unique and auto-incremented.

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

The Sub-Agent Manager provides 5 MCP tools:

### 1. `manage_sub_agent` (create)

**Create a new sub-agent instance**

```json
{
  "tool": "manage_sub_agent",
  "arguments": {
    "operation": "create",
    "agent_type": "web_research_agent",
    "task": "Research the latest developments in quantum computing"
  }
}
```

**Parameters:**
- `operation`: Must be `"create"`
- `agent_type`: Name of the agent to spawn (must exist in registry)
- `task`: Initial message/task for the sub-agent
- `instance_label` (optional): Custom label for the instance

**Returns:**
```json
{
  "instance_id": "sub_web_research_001",
  "status": "completed",
  "result": "Research findings: ..."
}
```

### 2. `manage_sub_agent` (continue)

**Continue conversation with an existing sub-agent**

```json
{
  "tool": "manage_sub_agent",
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
  "tool": "manage_sub_agent",
  "arguments": {
    "operation": "list",
    "include_completed": false
  }
}
```

**Parameters:**
- `operation`: Must be `"list"`
- `include_completed` (optional): Include archived sub-agents (default: false)

**Returns:**
```json
{
  "sub_agents": [
    {
      "instance_id": "sub_web_research_001",
      "agent_type": "web_research_agent",
      "status": "active",
      "last_used": "2025-11-02T10:35:00Z",
      "task_summary": "Research latest AI developments"
    }
  ]
}
```

### 4. `manage_sub_agent` (info)

**Get detailed information about a sub-agent**

```json
{
  "tool": "manage_sub_agent",
  "arguments": {
    "operation": "info",
    "instance_id": "sub_web_research_001"
  }
}
```

**Returns:**
```json
{
  "instance_id": "sub_web_research_001",
  "agent_type": "web_research_agent",
  "status": "active",
  "created_at": "2025-11-02T10:30:00Z",
  "last_used": "2025-11-02T10:35:00Z",
  "message_count": 5,
  "task_summary": "Research latest AI developments",
  "recent_messages": [...]
}
```

### 5. `manage_sub_agent` (delete)

**Archive a sub-agent (soft delete)**

```json
{
  "tool": "manage_sub_agent",
  "arguments": {
    "operation": "delete",
    "instance_id": "sub_web_research_001"
  }
}
```

**Note:** This marks the sub-agent as archived but preserves the session file for audit trail.

## Usage Examples

### Example 1: Multi-Stage Research Workflow

```python
# Stage 1: Create sub-agent for initial research
{
  "tool": "manage_sub_agent",
  "arguments": {
    "operation": "create",
    "agent_type": "web_research_agent",
    "task": "Research the top 5 quantum computing companies"
  }
}
# Returns: {"instance_id": "sub_web_research_001", "result": "..."}

# Stage 2: Follow up with specific question
{
  "tool": "manage_sub_agent",
  "arguments": {
    "operation": "continue",
    "instance_id": "sub_web_research_001",
    "message": "Compare their qubit counts and error rates"
  }
}

# Stage 3: Create analysis sub-agent
{
  "tool": "manage_sub_agent",
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
  "tool": "manage_sub_agent",
  "arguments": {
    "operation": "create",
    "agent_type": "project_manager_agent",
    "task": "Coordinate website redesign project"
  }
}

# Level 2: Project manager creates design agent
# (project_manager_agent internally calls manage_sub_agent)
{
  "tool": "manage_sub_agent",
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
  "tool": "manage_sub_agent",
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
  "tool": "manage_sub_agent",
  "arguments": {
    "operation": "list",
    "include_completed": false
  }
}
```

### 5. Monitor Nesting Depth

Maximum nesting depth is **5 levels**. Design workflows to stay within limits:

```
Level 1: meta_agent
Level 2: project_manager_agent
Level 3: design_agent
Level 4: asset_creator_agent
Level 5: image_optimizer_agent
Level 6: ❌ EXCEEDS LIMIT
```

## Configuration

### Plugin Configuration (`config/plugins.yaml`)

```yaml
sub_agent_manager:
  type: sub_agent_manager
  enabled: true
  max_nesting_depth: 5

  hook_config:
    enabled: true
    inject_sub_agent_context:
      enabled: true
      max_sub_agents_shown: 5
      format: "markdown"
```

### Hook Configuration

The `inject_sub_agent_context` hook automatically injects active sub-agent information into the system prompt:

**Options:**
- `enabled`: Enable/disable context injection (default: true)
- `max_sub_agents_shown`: Limit sub-agents in prompt (default: 5)
- `format`: "markdown" or "text" (default: "markdown")

**Injected Context Example:**

```markdown
## Active Sub-Agents

You have access to the following persistent sub-agent instances:

### web_research_agent
- **Instance ID**: `sub_web_research_001`
- **Status**: active
- **Last Used**: 2 minutes ago
- **Messages**: 5
- **Task**: Research latest AI developments

**To continue this sub-agent:**
```json
{
  "tool": "manage_sub_agent",
  "arguments": {
    "operation": "continue",
    "instance_id": "sub_web_research_001",
    "message": "Your follow-up question here"
  }
}
```
```

## Troubleshooting

### Error: "Maximum nesting depth exceeded"

**Cause:** Trying to create a sub-agent beyond level 5

**Solution:** Redesign workflow to reduce nesting or use parallel sub-agents instead of nested ones

```python
# Instead of deep nesting:
meta → project_manager → designer → developer → tester → deployer (TOO DEEP)

# Use parallel structure:
meta → project_manager
     ├── designer
     ├── developer
     ├── tester
     └── deployer
```

### Error: "Agent type 'xyz' not found in registry"

**Cause:** Specified agent doesn't exist or isn't enabled

**Solution:** Check `config/plugins.yaml` and ensure agent is enabled with `visibility: "tool"` or `"both"`

```yaml
web_research_agent:
  type: web_research_agent
  enabled: true
  metadata:
    visibility: "both"  # ← Must be "tool" or "both"
```

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
2. Reduce `max_sub_agents_shown` in hook config
3. Consider splitting into multiple parent sessions

## Performance Benchmarks

Based on `tests/performance/test_sub_agent_performance.py`:

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
    max_nesting_depth=5
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
- [MCP Configuration](../docs/mcp_configuration.md)
