# Cancellation System Architecture

**Status**: Active Design  
**Last Updated**: October 24, 2025  
**Component**: `src/agent_system/servers/agent/server.py`, `src/agent_system/core/cancellation.py`

---

## Overview

The agent system uses **two independent cancellation mechanisms** that work together to provide both immediate request termination and graceful tool shutdown.

## The Two Systems

### 1. Global CancellationManager (Tool-Level)

**Location**: `src/agent_system/core/cancellation.py`

**Purpose**: Graceful cancellation of tool executions with timeout escalation

**Scope**: Cross-agent - manages cancellation for all tool calls system-wide

**Mechanism**:
- Creates `CancellationToken` objects for each tool execution
- Tools check `token.is_cancelled` during long operations
- Grace period (5s default) before force-terminating
- Background monitor thread for timeout enforcement

**Usage**:
```python
cancellation_manager = get_cancellation_manager()
token = cancellation_manager.create_token(request_id)

# Tool checks token
async def long_running_tool(params, cancellation_token):
    for chunk in process_data():
        if cancellation_token.is_cancelled:
            return {"cancelled": True}
        # ... continue processing
```

**Use Cases**:
- Cancel specific tool execution (e.g., long-running web scraper)
- Timeout enforcement for unresponsive tools
- Cascading cancellation through agent-to-agent calls

### 2. Per-Agent Event System (Request-Level)

**Location**: `src/agent_system/servers/agent/server.py` 

**Purpose**: Immediate termination of agent's conversation loop

**Scope**: Agent-local - each agent tracks its own active requests via `AgentRequestManager` component

**Mechanism**:
- `AgentRequestManager` stores `asyncio.Event` in `self._active_requests[request_id]["cancel"]`
- Agent checks event at step boundaries in `_run_events()` via `_request_manager.is_cancelled()`
- No grace period - stops at next checkpoint
- Direct event signaling via `event.set()`

**Usage**:
```python
# Agent checks cancellation (via component)
if self._request_manager.is_cancelled(request_id):
    yield {"type": "cancelled", "request_id": request_id}
    return

# Cancel from outside (via component)
await self._request_manager.cancel_request(request_id, status_bus)
```

**Use Cases**:
- User cancels multi-step agent task from UI
- Immediate stop between LLM steps
- Request timeout from API layer

## How They Coordinate

When `cancel_request(request_id)` is called:

1. **Global system** cancels all matching tool tokens:
   - Main request: `"req_123"`
   - Derived tool requests: `"req_123_tool_1"`, `"req_123_tool_2"`, etc.
   - Uses prefix matching for cascading cancellation

2. **Local system** sets the agent's event flag:
   - Agent checks at step boundaries
   - Breaks out of LLM loop immediately
   - Returns cancellation event to caller

3. **Tools respond gracefully**:
   - Check `CancellationToken.is_cancelled` during execution
   - Clean up resources before returning
   - Report cancellation status

## Cancellation Points

Agent checks cancellation at multiple locations in `_run_events()`:

- **Before each LLM step**: Check event + token
- **Before tool execution**: Prevent starting cancelled tools
- **During LLM calls**: Pass `cancellation_token` parameter
- **After receiving LLM response**: Check before processing

## Prefix Matching for Cascading

When agent A calls agent B which calls tool C:

```
Request: req_123
├─ Agent A: req_123
├─ Tool call 1: req_123_001
│  └─ Agent B: req_123_001_sub
│     └─ Tool call: req_123_001_sub_001
└─ Tool call 2: req_123_002
```

Cancelling `req_123` cancels **all derived requests** automatically.

## Why Two Systems?

**Could we use only CancellationManager?**
- ❌ No - CancellationManager is for async operations (tools)
- ❌ Agent loop is synchronous between tools (no long-running operation to cancel)
- ❌ Would need to poll token constantly, wasting CPU

**Could we use only per-agent events?**
- ❌ No - Events are local to one agent
- ❌ Can't propagate to tools running in sub-agents
- ❌ Can't implement graceful timeout escalation

**Why not merge them?**
- Different lifetimes (tools vs agent loop)
- Different scopes (global vs local)
- Different mechanisms (polling vs event)
- Different use cases (tool cleanup vs request abort)

## Design Decision

✅ **Keep both systems** - they serve complementary purposes:
- **CancellationManager**: Graceful tool termination across agent boundaries
- **Per-agent events**: Immediate loop control at step boundaries

The dual design provides:
1. Fast response to cancellation requests
2. Graceful tool shutdown with cleanup
3. Cascading cancellation through nested calls
4. Clear separation of concerns

## Future Improvements

Potential unification approach (if needed):
1. Extend CancellationManager to support synchronous checkpoints
2. Make agent loop check CancellationToken at step boundaries
3. Remove per-agent event system
4. **Effort**: 12-16 hours + extensive testing
5. **Risk**: May introduce subtle timing issues
6. **Benefit**: Single cancellation mental model

**Recommendation**: Keep current design unless issues arise. Both systems are well-understood and tested.

---

**Related Files**:
- `src/agent_system/core/cancellation.py` - CancellationManager implementation
- `src/agent_system/servers/agent/server.py` - Agent cancel_request(), _is_cancelled(), _run_events()
- `tests/test_cancellation_system.py` - Unit tests
- `tests/test_webui_cancellation.py` - Integration tests
