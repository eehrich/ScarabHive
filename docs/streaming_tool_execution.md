# Streaming Tool Execution Architecture

## Overview

The Agent System implements real-time streaming of status events during parallel tool execution. This allows sub-agent status messages to be displayed in real-time while tools are still running, providing better user experience and transparency.

## Architecture

### Components

1. **ToolExecutionManager** (`src/agent_system/servers/agent/components/tool_execution.py`)
   - Manages parallel tool execution
   - Streams status events in real-time via `execute_tools_streaming()`
   - Provides backward-compatible wrapper `execute_tools()`

2. **StatusEventForwarder** (`src/agent_system/servers/agent/components/status_forwarding.py`)
   - Collects status events from `status_bus` in background task
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
  ├─ Start parallel tasks (asyncio.create_task)
  ├─ Poll loop (50ms interval):
  │  ├─ await asyncio.wait(tasks, timeout=0.05)
  │  ├─ Yield pending status events ← StatusEventForwarder
  │  └─ Process completed tasks
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
Background status collection uses blocking `queue.get()`:

```python
# Blocks efficiently until event arrives
status_event = await self.status_queue.get()  # No timeout!
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
    request_id: str | None = None
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Yields:
        {"type": "status", "event": {...}}          - Real-time status
        {"type": "tool_events", "events": [...]}    - Tool execution events
        {"type": "complete", "messages": [...], "results": [...]} - Final results
    """
```

### execute_tools() (Legacy Wrapper)

Convenience method that collects all streaming results:

```python
async def execute_tools(...) -> tuple[List[ChatMessage], List[Dict], List[Dict]]:
    """Backward-compatible wrapper around execute_tools_streaming()"""
```

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
# Simple non-streaming interface for tests
messages, events, results = await tool_execution_manager.execute_tools(...)
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
  ↓ Resets: forwarding_done, forwarding_ready, first_get_started
  ↓ Starts: Background collection task

# 2. During execution
async for item in execute_tools_streaming(...):
  ↓ Polls status_forwarder.get_pending_events() every 50ms
  ↓ Yields events in real-time

# 3. Request ends
await status_forwarder.stop_forwarding()
  ↓ Cancels background task
  ↓ Cleans up queue subscription
```

### Multi-Request Support

Each request gets:
- Fresh StatusEventForwarder state (via `.clear()` on Events)
- Unique request_id hierarchy (base + suffixes)
- Independent background collection task

## Testing

### Unit Tests

```python
async def test_streaming_status_events():
    """Test that status events are streamed during tool execution"""
    forwarder = StatusEventForwarder()
    manager = ToolExecutionManager(registry, agent, status_forwarder=forwarder)
    
    # Start forwarding
    await forwarder.start_forwarding("test123")
    
    # Collect streamed events
    status_events = []
    async for item in manager.execute_tools_streaming(...):
        if item["type"] == "status":
            status_events.append(item["event"])
    
    # Verify events arrived during execution (not after)
    assert len(status_events) > 0
```

### Integration Tests

See `tests/test_agent_streaming_status.py` for full examples.

## Troubleshooting

### Status Events Not Appearing

**Symptom**: No status events during tool execution

**Causes**:
1. StatusEventForwarder not injected into ToolExecutionManager
2. Background task not started (`start_forwarding()` not called)
3. Events filtered out (wrong request_id)

**Fix**: Ensure proper initialization in `Agent.__init__()`:

```python
self._status_event_forwarder = StatusEventForwarder()
self._tool_execution_manager = ToolExecutionManager(
    registry, 
    self, 
    status_forwarder=self._status_event_forwarder
)
```

### Events Appear Only at End

**Symptom**: All status events arrive after tool completion

**Causes**:
1. Using `execute_tools()` instead of `execute_tools_streaming()`
2. Not yielding status events in the generator loop

**Fix**: Use streaming version:

```python
# Wrong (buffered)
messages, events, results = await execute_tools(...)

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

**Fix**: Verify blocking queue.get():

```python
# Efficient (blocking)
status_event = await self.status_queue.get()  # No timeout!

# Inefficient (spinning)
await asyncio.wait_for(queue.get(), timeout=0.001)  # Too fast!
```

## Future Improvements

### Potential Enhancements

1. **Adaptive Polling**: Adjust interval based on event frequency
2. **Event Prioritization**: High-priority status events first
3. **Backpressure Handling**: Slow clients don't block fast producers
4. **Metrics**: Track streaming latency and throughput

### Backward Compatibility

The `execute_tools()` wrapper ensures existing tests and code continue working without changes. New code should use `execute_tools_streaming()` for optimal performance.

## References

- [Status Design](./status_design.md)
- [Tool Execution](./tool_execution.md)
- [SSE Streaming](./mcp_streamable_http_transport.md)
