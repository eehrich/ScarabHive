# Sequential Thinking Plugin

The Sequential Thinking plugin provides step-by-step reasoning tools for LLMs to break down complex problems dynamically. It supports branching to explore alternative approaches, revising previous thoughts, and adaptive complexity estimation.

## Overview

This plugin enables structured, iterative thinking for complex problem-solving tasks. It's inspired by Anthropic's sequential thinking approach but implemented in Python with deep integration into the AgentSystem framework.

**Key Features:**
- **Dynamic Problem Breakdown**: Start with an estimate, adjust as you learn
- **Branching**: Explore alternative approaches without losing main reasoning chain
- **Revision**: Refine previous thoughts with new insights
- **Session Management**: Automatic session creation, TTL-based cleanup
- **Memory Management**: Configurable limits with automatic oldest-thought removal
- **Status Updates**: Real-time progress feedback via status context

## Use Cases

### When to Use Sequential Thinking

✅ **Perfect for:**
- Complex multi-step planning (architecture design, project planning)
- System analysis requiring structured breakdown
- Problem-solving with uncertainty (explore alternatives via branching)
- Iterative refinement (revise thoughts as understanding deepens)
- Educational explanations (build up concepts step-by-step)

❌ **Not ideal for:**
- Simple one-shot questions
- Tasks requiring single-step answers
- Real-time conversation (adds latency)

### Example Scenarios

**Software Architecture Design:**
```
Thought 1: Analyze requirements (auth, multi-tenancy, scalability)
Thought 2: Consider JWT authentication approach
  → Branch: Explore OAuth2 alternative
  → Branch: Explore session-based auth
Thought 3: Revise thought 2 after comparing approaches
Thought 4: Design final architecture with chosen approach
```

**Complex Debugging:**
```
Thought 1: Identify symptoms and error messages
Thought 2: Hypothesis 1 - Database connection issue
  → Branch: Hypothesis 2 - Memory leak
Thought 3: Test hypothesis, gather evidence
Thought 4: Narrow down to root cause
Thought 5: Propose solution with rationale
```

## Configuration

Enable the plugin in `config/plugins.yaml`:

```yaml
plugins:
  servers:
    sequential_thinking:
      type: sequential_thinking
      enabled: true
      max_history_size: 100         # Max thoughts per session
      session_ttl_seconds: 3600      # Session expiration (1 hour)
      enable_branching: true         # Allow exploring alternatives
      enable_revisions: true         # Allow refining previous thoughts
      max_summary_thoughts: 10       # Default thoughts in summaries
```

### Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `max_history_size` | integer | 100 | Maximum thoughts per session before oldest are removed |
| `session_ttl_seconds` | integer | 3600 | Session expiration time in seconds |
| `enable_branching` | boolean | true | Allow branching to explore alternatives |
| `enable_revisions` | boolean | true | Allow revising previous thoughts |
| `max_summary_thoughts` | integer | 10 | Default max thoughts in summaries |

## Tools

### `sequentialthinking`

Main reasoning tool for step-by-step problem-solving.

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `thought` | string | ✅ Yes | Current thinking step content (1-10000 chars) |
| `next_thought_needed` | boolean | ✅ Yes | Whether another thought step is needed |
| `thought_number` | integer | ✅ Yes | Current thought number (1-indexed) |
| `total_thoughts` | integer | ✅ Yes | Estimated total thoughts (adjustable) |
| `session_id` | string | ❌ No | Session ID (auto-generated if omitted) |
| `is_revision` | boolean | ❌ No | Whether this revises previous thinking |
| `revises_thought` | integer | ❌ No | Which thought number to revise |
| `branch_from_thought` | integer | ❌ No | Thought number to branch from |
| `branch_id` | string | ❌ No | Branch identifier (e.g., "oauth_alternative") |
| `needs_more_thoughts` | boolean | ❌ No | Flag to extend total_thoughts |

**Returns:**
```json
{
  "status": "success",
  "session_id": "a3f9c2d1-...",
  "current_thought_number": 3,
  "total_thoughts_estimate": 7,
  "next_thought_needed": true,
  "progress": "Thought 3/7",
  "branch": "main",
  "thought_history": [
    {
      "number": 1,
      "content": "First thought...",
      "branch": "main",
      "is_revision": false
    }
  ],
  "branch_summary": {
    "main": {
      "parent": null,
      "branched_from": 0,
      "thoughts_count": 2,
      "active": true
    },
    "alternative": {
      "parent": "main",
      "branched_from": 2,
      "thoughts_count": 1,
      "active": false
    }
  },
  "error": null,
  "warning": null
}
```

**Status Messages:**
- `START`: "Adding thought 3/7 to session a3f9c2d1"
- `PROGRESS`: "Creating branch 'oauth_alternative' from thought 2"
- `PROGRESS`: "Revising thought 2 with new insights"
- `PROGRESS`: "Memory usage: 85/100 thoughts (85%)" ⚠️
- `END`: "Thought 3 added. Progress: 3/7 thoughts. Continue reasoning..."

### `clear_history`

Clear thought history for a session or all sessions.

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `session_id` | string | ❌ No | Session to clear (if omitted, clears all) |

**Returns:**
```json
{
  "status": "success",
  "cleared_sessions": 1,
  "message": "Cleared session abc123 (7 thoughts)"
}
```

### `get_thought_summary`

Get condensed summary of thought chain with metadata.

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `session_id` | string | ✅ Yes | Session ID to summarize |
| `max_thoughts` | integer | ❌ No | Max thoughts to include (default: 10) |
| `include_branches` | boolean | ❌ No | Include branch tree (default: true) |

**Returns:**
```json
{
  "status": "success",
  "session_id": "abc123",
  "total_thoughts": 15,
  "current_branch": "oauth_alternative",
  "thoughts": [
    {
      "number": 1,
      "content": "Full thought content...",
      "timestamp": "2025-10-24T15:00:00.123456",
      "branch": "main",
      "is_revision": false,
      "revises_thought": null,
      "revision_count": 0
    }
  ],
  "branches": { /* branch tree */ },
  "created_at": "2025-10-24T14:30:00.000000",
  "last_accessed": "2025-10-24T15:00:00.000000"
}
```

## Usage Examples

### Basic Linear Reasoning

```python
# Thought 1: Initial analysis
result1 = await agent.call("sequentialthinking", {
    "thought": "First, identify the authentication requirements: JWT tokens, refresh tokens, MFA support",
    "thought_number": 1,
    "total_thoughts": 5,
    "next_thought_needed": True
})

session_id = result1["session_id"]

# Thought 2: Design approach
result2 = await agent.call("sequentialthinking", {
    "session_id": session_id,
    "thought": "Design token service with RS256 signing, 15-minute expiry, Redis for refresh tokens",
    "thought_number": 2,
    "total_thoughts": 5,
    "next_thought_needed": True
})

# ... continue until thought 5
```

### Adaptive Complexity

```python
# Start with initial estimate
result = await agent.call("sequentialthinking", {
    "thought": "Initial estimate: 3 steps should be enough",
    "thought_number": 1,
    "total_thoughts": 3,
    "next_thought_needed": True
})

# Realize more steps needed
result2 = await agent.call("sequentialthinking", {
    "session_id": result["session_id"],
    "thought": "Actually, this is more complex - need to consider edge cases",
    "thought_number": 2,
    "total_thoughts": 8,  # Increased from 3
    "needs_more_thoughts": True,
    "next_thought_needed": True
})
# Status: "Adjusting complexity: 3 → 8 thoughts"
```

### Branching to Explore Alternatives

```python
# Main approach
result1 = await agent.call("sequentialthinking", {
    "thought": "Main approach: Monolithic architecture",
    "thought_number": 1,
    "total_thoughts": 5,
    "next_thought_needed": True
})

result2 = await agent.call("sequentialthinking", {
    "session_id": result1["session_id"],
    "thought": "Implement auth, business logic, and DB in single service",
    "thought_number": 2,
    "total_thoughts": 5,
    "next_thought_needed": True
})

# Create branch to explore alternative
result3 = await agent.call("sequentialthinking", {
    "session_id": result1["session_id"],
    "thought": "Alternative: What about microservices architecture?",
    "thought_number": 3,
    "total_thoughts": 5,
    "branch_from_thought": 1,
    "branch_id": "microservices_alternative",
    "next_thought_needed": True
})
# Status: "Creating branch 'microservices_alternative' from thought 1"

# Now on microservices branch
result4 = await agent.call("sequentialthinking", {
    "session_id": result1["session_id"],
    "thought": "Microservices: Separate auth service, business service, event bus",
    "thought_number": 4,
    "total_thoughts": 5,
    "next_thought_needed": True
})
```

### Revising Previous Thoughts

```python
result1 = await agent.call("sequentialthinking", {
    "thought": "Use PostgreSQL for user data",
    "thought_number": 1,
    "total_thoughts": 3,
    "next_thought_needed": True
})

result2 = await agent.call("sequentialthinking", {
    "session_id": result1["session_id"],
    "thought": "Design schema with users, roles, permissions tables",
    "thought_number": 2,
    "total_thoughts": 3,
    "next_thought_needed": True
})

# Realize earlier thought needs refinement
result3 = await agent.call("sequentialthinking", {
    "session_id": result1["session_id"],
    "thought": "Correction: MongoDB is better for this use case - flexible schema for user attributes",
    "thought_number": 1,  # Same number as original
    "total_thoughts": 3,
    "is_revision": True,
    "revises_thought": 1,
    "next_thought_needed": False
})
# Status: "Revising thought 1 with new insights"

# Get summary to see revision history
summary = await agent.call("get_thought_summary", {
    "session_id": result1["session_id"]
})
# summary["thoughts"][0]["revision_count"] == 1
# summary["thoughts"][0]["content"] == "Correction: MongoDB..."
```

### Getting Summary

```python
# After complex reasoning session with multiple branches
summary = await agent.call("get_thought_summary", {
    "session_id": session_id,
    "max_thoughts": 5,  # Only last 5 thoughts
    "include_branches": True
})

print(f"Total thoughts: {summary['total_thoughts']}")
print(f"Current branch: {summary['current_branch']}")
print(f"Branches: {list(summary['branches'].keys())}")
for thought in summary['thoughts']:
    print(f"  [{thought['branch']}] Thought {thought['number']}: {thought['content'][:50]}...")
```

## CLI Usage

The plugin can be used directly from command line:

```bash
# Add a thought
python -m plugins.sequential_thinking --operation think \
  --thought "First, analyze the requirements" \
  --thought-number 1 \
  --total-thoughts 5 \
  --next-thought-needed

# Continue with session ID
python -m plugins.sequential_thinking --operation think \
  --session-id "abc123..." \
  --thought "Next, design the solution" \
  --thought-number 2 \
  --total-thoughts 5 \
  --next-thought-needed

# Create branch
python -m plugins.sequential_thinking --operation think \
  --session-id "abc123..." \
  --thought "Alternative approach" \
  --thought-number 3 \
  --total-thoughts 5 \
  --branch-from-thought 1 \
  --branch-id "alternative" \
  --next-thought-needed

# Get summary
python -m plugins.sequential_thinking --operation summary \
  --session-id "abc123..." \
  --max-thoughts 10

# Clear session
python -m plugins.sequential_thinking --operation clear \
  --session-id "abc123..."

# Run as MCP server
python -m plugins.sequential_thinking --server --port 9011
```

## Integration with Agents

Add to agent tool allowlist in `config/agents.yaml`:

```yaml
agents:
  meta_agent:
    tools:
      allowed:
        - "sequential_thinking/*"  # All tools
        # or specific tools:
        - "sequential_thinking/sequentialthinking"
        - "sequential_thinking/get_thought_summary"
  
  basic_agent:
    tools:
      allowed:
        - "sequential_thinking/sequentialthinking"  # Only main tool
```

**Agent Usage Example:**
```
Agent: I need to design a complex authentication system.

[Uses sequentialthinking tool]
Thought 1/5: First, identify requirements...
Thought 2/5: Consider JWT approach...
  → Branch: Alternative OAuth2 approach
Thought 3/5 (revision of 2): Actually, OAuth2 is better because...
...
Thought 5/5: Final architecture decision with rationale.

Agent: Based on my step-by-step analysis, here's the recommended architecture...
```

## Memory Management

### Automatic Cleanup

- **Session TTL**: Sessions expire after `session_ttl_seconds` (default 3600s = 1 hour)
- **Memory Limits**: When thoughts exceed `max_history_size`, oldest thoughts are removed
- **Warning Threshold**: Status warning at 80% capacity

### Manual Cleanup

```python
# Clear specific session
await agent.call("clear_history", {"session_id": "abc123"})

# Clear all sessions
await agent.call("clear_history", {})
```

## Best Practices

### 1. Start with Reasonable Estimates
```python
# Good: Conservative estimate that can grow
total_thoughts = 5

# Avoid: Overestimating from the start
total_thoughts = 50
```

### 2. Use Branching Sparingly
```python
# Good: Branch only for significant alternatives
if considering_fundamentally_different_approach:
    branch_id = "alternative_approach"

# Avoid: Branching for minor variations
```

### 3. Revise When Understanding Changes
```python
# Good: Revise when new insights invalidate previous reasoning
if new_information_contradicts_thought_2:
    is_revision = True
    revises_thought = 2

# Avoid: Revising for minor wording changes
```

### 4. Provide Meaningful Session IDs
```python
# If you need to continue later, capture session_id
session_id = result["session_id"]

# Don't rely on auto-generated IDs for long-term storage
```

### 5. Monitor Memory Usage
```python
# Check warnings in result
if result.get("warning"):
    print(f"Warning: {result['warning']}")
    # Consider getting summary and starting new session
```

## Testing

Run the comprehensive test suite:

```bash
# All tests (30+ covering all features)
pytest tests/test_plugin_sequential_thinking.py -v

# Specific test categories
pytest tests/test_plugin_sequential_thinking.py -k "branching" -v
pytest tests/test_plugin_sequential_thinking.py -k "revision" -v
pytest tests/test_plugin_sequential_thinking.py -k "session" -v
```

Test coverage includes:
- ✅ Basic thought sequences (linear reasoning)
- ✅ Adaptive complexity (adjusting total_thoughts)
- ✅ Branching (creating, switching, multiple branches)
- ✅ Revisions (single, chained, history tracking)
- ✅ Session management (TTL, cleanup, last_accessed)
- ✅ Memory limits (enforcement, warnings)
- ✅ Edge cases (empty content, invalid params, duplicates)
- ✅ Status messages (START, PROGRESS, END, ERROR)

## Troubleshooting

### Session Not Found

**Error:** `"Session abc123 not found"`

**Causes:**
- Session expired (TTL exceeded)
- Wrong session_id
- Session was cleared

**Solution:**
```python
# Start new session (omit session_id)
result = await agent.call("sequentialthinking", {
    "thought": "...",
    "thought_number": 1,
    "total_thoughts": 5,
    "next_thought_needed": True
})
new_session_id = result["session_id"]
```

### Branching Disabled

**Error:** `"Branching is disabled"`

**Solution:** Enable in config:
```yaml
sequential_thinking:
  enable_branching: true
```

### Memory Limit Warnings

**Warning:** `"Session approaching memory limit (85% used)"`

**Solutions:**
```python
# Option 1: Get summary and start fresh
summary = await agent.call("get_thought_summary", {"session_id": session_id})
await agent.call("clear_history", {"session_id": session_id})

# Option 2: Increase limit in config
# config/plugins.yaml:
max_history_size: 200
```

### Branch Already Exists

**Error:** `"Branch 'alternative' already exists"`

**Cause:** Trying to create branch with duplicate ID

**Solution:**
```python
# Use unique branch IDs
branch_id = f"alternative_{approach_name}"

# Or switch to existing branch (omit branch_from_thought)
result = await agent.call("sequentialthinking", {
    "session_id": session_id,
    "thought": "Continue on existing branch",
    "branch_id": "alternative",  # Switches to existing
    # No branch_from_thought
})
```

## Performance Considerations

- **In-Memory Storage**: Sessions stored in memory, not persisted across restarts
- **TTL Cleanup**: Runs on every `sequentialthinking` call (negligible overhead)
- **Memory Usage**: ~1KB per thought (depends on content length)
- **Scaling**: Single-process only (not distributed)

**For Production:**
- Set reasonable `max_history_size` (default 100 is good for most cases)
- Use shorter `session_ttl_seconds` if sessions rarely continue (e.g., 1800s)
- Monitor memory usage if handling many concurrent sessions

## Future Enhancements

See design document for planned integrations:
- **TODO Plugin Integration**: Combine task tracking (WHAT) with reasoning (HOW)
- **Agent Session Continuity**: Persistent sessions across sub-agent calls
- **Message Archive**: Long-term storage with retrieval tool

## Contributing

This plugin follows repository conventions:

1. **Tests First**: Add tests before implementing features
2. **Schema Updates**: Update `schema.yaml` when changing tool signatures
3. **Documentation**: Update this README for new features
4. **Type Safety**: Use type hints, pass mypy checks
5. **Code Quality**: Run ruff and fix lint errors

```bash
# Run tests
pytest tests/test_plugin_sequential_thinking.py -v

# Type checking
mypy src/plugins/sequential_thinking

# Linting
ruff check src/plugins/sequential_thinking --fix
```

## License

Part of AgentSystem project. See main repository LICENSE.

---

**Questions or Issues?** Check the [design document](../../../docs/sequential_thinking_design.md) for detailed architecture and data structures.
