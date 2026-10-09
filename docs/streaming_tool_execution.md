# Streaming Tool Execution Architecture

## Overview

The Agent System implements real-time streaming of status events during parallel tool execution. This allows sub-agent status messages to be displayed in real-time while tools are still running, providing better user experience and transparency.

## Architecture

### Components

1. **ToolExecutionManager** (`src/agent_system/servers/agent/components/tool_execution.py`)
   - Manages parallel tool execution
   - Streams status events in real-time via `execute_tools_streaming()`
     (the sole production interface; the old `execute_tools()` wrapper was removed)
   - Runs each single call through `ToolInvoker` (`components/tool_invocation.py`)

2. **StatusEventForwarder** (`src/agent_system/servers/agent/components/status_forwarding.py`)
   - Collects status events from `status_bus` via a direct handler (no background task)
   - Provides `get_pending_events()` for polling
   - Automatically resets state between requests

3. **Agent Server** (`src/agent_system/servers/agent/server.py`)
   - Integrates components
   - Uses `execute_tools_streaming()` for real-time status delivery
   - Yields status events to SSE stream

### Flow Diagram

```
User Request → Agent.run_events()
  ↓
LLM Streaming (yields chunks)
  ↓
ToolExecutionManager.execute_tools_streaming()
  ├─ Check the calls (_check_calls)
  ├─ pre_tool_call hooks, in call order (_ask_pre_hooks_in_call_order);
  │  status events keep flowing while a hook waits
  ├─ Start parallel tasks (asyncio.create_task) (_start_calls)
  ├─ Poll loop (50ms interval) (_stream_until_done):
  │  ├─ await asyncio.wait(tasks, timeout=0.05)
  │  ├─ Yield pending status events ← StatusEventForwarder
  │  └─ Process completed tasks
  ├─ post_tool_call hooks and answers, in call order (_answer_in_call_order)
  └─ Yield final results
  ↓
Continue conversation loop
```

## Key Features

### 1. Parallel Execution
Tools run concurrently using `asyncio.create_task()` and `asyncio.wait()`:

```python
tasks = [create_task(execute_tool(tc)) for tc in tool_calls]
pending = set(tasks)

while pending:
    done, pending = await asyncio.wait(pending, timeout=0.05, return_when=asyncio.FIRST_COMPLETED)
    # Process completed tasks
```

### 2. Real-Time Streaming
Status events are yielded during execution, not after:

```python
async for item in execute_tools_streaming(...):
    if item["type"] == "status":
        yield item["event"]  # Real-time status from sub-agents
    elif item["type"] == "complete":
        messages = item["messages"]
```

### 3. Zero CPU Overhead
Status collection uses a direct `status_bus` handler that appends matching events to a list (no queue, no background task):

```python
# DirectStatusHandler.process(): append events for this request (or its sub-requests)
self.events_list.append(status_sse_event)
```

### 4. Request ID Hierarchy
Sub-agents get suffixed request IDs for tracking:

```
Main request: "abc123"
  ├─ Tool 1: "abc123_001"
  ├─ Tool 2: "abc123_002"  (sub-agent call)
  │   ├─ Sub-tool 1: "abc123_002_001"
  │   └─ Sub-tool 2: "abc123_002_002"
  └─ Tool 3: "abc123_003"
```

## API

### execute_tools_streaming()

Async generator that yields events during tool execution:

```python
async def execute_tools_streaming(
    tool_calls: List[Dict],
    tool_name_mapping: Dict[str, str],
    available_tools: List[str],
    step: int,
    request_id: str | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    status_forwarder: Optional[StatusEventForwarder] = None,
    ...
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Yields:
        {"type": "status", "event": {...}}          - Real-time status
        {"type": "tool_events", "events": [...]}    - Tool execution events
        {"type": "complete", "messages": [...], "results": [...]} - Final results
    """
```

### Collected results (tests)

The former `execute_tools()` legacy wrapper was removed (test-only production
code). Tests that want the
final tuple use the shared collector helper
`tests/tool_execution_test_helpers.py::execute_tools_collect(manager, ...)`.

## Usage Example

### Production (Streaming)

```python
async for item in tool_execution_manager.execute_tools_streaming(...):
    if item["type"] == "status":
        # Yield status events immediately to SSE stream
        yield item["event"]
    elif item["type"] == "tool_events":
        for event in item["events"]:
            yield event
    elif item["type"] == "complete":
        # Process final results
        tool_messages = item["messages"]
        tool_results = item["results"]
```

### Testing (Collected)

```python
# Shared test helper (tests/tool_execution_test_helpers.py)
from tool_execution_test_helpers import execute_tools_collect
messages, events, results = await execute_tools_collect(tool_execution_manager, ...)
```

## Performance

### Polling Interval
- **50ms** (0.05s) - Balances responsiveness vs overhead
- Status events appear within ~100ms of creation
- No noticeable CPU usage when idle

### Comparison

| Approach | Latency | CPU (Idle) | CPU (Active) |
|----------|---------|------------|--------------|
| Old (buffered) | Seconds | 0% | 0% |
| **New (streaming)** | **~100ms** | **0%** | **<1%** |

## State Management

### Request Lifecycle

```python
# 1. Request starts
await status_forwarder.start_forwarding(request_id)
  ↓ Resets: status_events_to_forward, _open_deltas
  ↓ Registers: DirectStatusHandler on status_bus

# 2. During execution
async for item in execute_tools_streaming(...):
  ↓ Polls status_forwarder.get_pending_events() every 50ms
  ↓ Yields events in real-time

# 3. Request ends
await status_forwarder.stop_forwarding()
  ↓ Removes the status_bus handler
```

### Multi-Request Support

Each request gets:
- Fresh StatusEventForwarder (created per request in `run_events()`)
- Unique request_id hierarchy (base + suffixes)
- Independent status_bus handler

## Testing

### Unit Tests

```python
async def test_streaming_status_events():
    """Test that status events are streamed during tool execution"""
    forwarder = StatusEventForwarder()
    manager = ToolExecutionManager(registry, agent)
    
    # Start forwarding
    await forwarder.start_forwarding("test123")
    
    # Collect streamed events
    status_events = []
    async for item in manager.execute_tools_streaming(..., status_forwarder=forwarder):
        if item["type"] == "status":
            status_events.append(item["event"])
    
    # Verify events arrived during execution (not after)
    assert len(status_events) > 0
```

### Integration Tests

See `tests/tool/test_tool_execution_streaming.py` for full examples.

## Troubleshooting

### Status Events Not Appearing

**Symptom**: No status events during tool execution

**Causes**:
1. StatusEventForwarder not passed to `execute_tools_streaming(status_forwarder=...)`
2. Handler not registered (`start_forwarding()` not called)
3. Events filtered out (wrong request_id)

**Fix**: Ensure a per-request forwarder is created and passed (as in `Agent.run_events()`):

```python
status_forwarder = StatusEventForwarder()
await status_forwarder.start_forwarding(request_id)
async for item in self._tool_execution_manager.execute_tools_streaming(
    ..., status_forwarder=status_forwarder
):
    ...
```

### Events Appear Only at End

**Symptom**: All status events arrive after tool completion

**Causes**:
1. Collecting all items (e.g. `execute_tools_collect()`) instead of yielding from `execute_tools_streaming()`
2. Not yielding status events in the generator loop

**Fix**: Use streaming version:

```python
# Wrong (buffered)
messages, events, results = await execute_tools_collect(manager, ...)

# Right (streaming)
async for item in execute_tools_streaming(...):
    if item["type"] == "status":
        yield item["event"]
```

### High CPU Usage

**Symptom**: CPU usage while idle

**Causes**:
1. Polling timeout too aggressive (<10ms)
2. Busy-wait loop in status collection

**Fix**: Verify the poll timeout in `execute_tools_streaming()`:

```python
# Efficient (50ms)
done, pending = await asyncio.wait(pending, timeout=0.05, return_when=asyncio.FIRST_COMPLETED)

# Inefficient (spinning)
await asyncio.wait(pending, timeout=0.001)  # Too fast!
```

## Future Improvements

### Potential Enhancements

1. **Adaptive Polling**: Adjust interval based on event frequency
2. **Event Prioritization**: High-priority status events first
3. **Backpressure Handling**: Slow clients don't block fast producers
4. **Metrics**: Track streaming latency and throughput

### Backward Compatibility

The `execute_tools()` wrapper was removed; tests use `execute_tools_collect()` from `tests/tool_execution_test_helpers.py`. New code should use `execute_tools_streaming()` for optimal performance.

## References

- [Tool Execution](./tool_execution.md)
- [MCP Client Plugin](../src/plugins/mcp_client/README.md)
