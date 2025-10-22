# Status Events in SSE Stream - Architecture Design

## Overview
Status events are now delivered through the main `/events` SSE endpoint instead of a separate `/status/stream` endpoint. This simplifies the architecture and improves security.

## Architecture

### Before (Dual Stream)
```
User Request → /events endpoint
              ↓
        [SSE Stream] ────→ thinking, tool_calls, final, end
              ↓
        User Auth ✓

User Request → /status/stream endpoint
              ↓
        [Status Stream] ──→ status updates from tools
              ↓
        User Auth ✓ (but complex timing issues)
```

**Problems:**
- Two separate SSE connections per request
- Status stream can't get request_id reliably in frontend
- Race conditions between connection establishment
- More memory overhead
- More attack surface

### After (Single Stream)
```
User Request → /events endpoint
              ↓
        User Auth ✓
              ↓
        [SSE Stream] ────→ thinking, tool_calls, final, end, STATUS
              ↑
              │
        StatusBus ────────→ Subscribe by request_id
              ↑
              │
        Tools/MCP Servers publish status
```

**Benefits:**
- ✅ Single SSE connection per request
- ✅ User authentication inherent from /events connection
- ✅ No timing issues - status flows through authenticated channel
- ✅ Simpler frontend code
- ✅ Less memory overhead
- ✅ Smaller attack surface

## Event Format

### Status Event Structure
```json
{
  "type": "status",
  "server": "web_scraper",
  "request_id": "abc123",
  "message": "Scraping webpage...",
  "phase": "PROGRESS",
  "level": "info",
  "sequence": 42,
  "meta": {
    "url": "https://example.com"
  },
  "tree": {
    "parent_id": "abc123_001",
    "depth_level": 2,
    "child_count": 0,
    "is_leaf": true
  }
}
```

### All Event Types in /events
- `start` - Request begins, provides request_id and session_id
- `thinking` - LLM reasoning, tool planning
- `tool_calls` - Tool execution starting
- `status` - **NEW** Status updates from tools/operations (replaces /status/stream)
- `final` - Final response ready
- `end` - Request complete
- `error` - Error occurred
- `cancelled` - Request was cancelled

## Implementation Flow

### 1. Agent Server (Backend)
```python
async def event_stream():
    # Subscribe to status bus for this request
    status_queue = await status_bus.subscribe(request_id=request_id)
    
    # Background task to forward status events
    async def forward_status():
        while not done:
            status_event = await status_queue.get()
            yield {
                "type": "status",
                "server": status_event.server,
                "message": status_event.message,
                # ... all status fields
            }
    
    # Main event loop merges agent events + status events
    async for event in merge_streams(agent_events, status_events):
        yield event
```

### 2. Frontend (chat_module.js)
```javascript
es.onmessage = ev => {
  const data = JSON.parse(ev.data);
  
  switch (data.type) {
    case 'status':
      addStatusEvent(blk.status, data);
      break;
    // ... other event types
  }
};
```

## Security Properties

### User Isolation
1. User authenticates when opening `/events` connection
2. `request_id` is generated server-side and sent in 'start' event
3. Status bus subscribes with `request_id` filter
4. Only status events matching that `request_id` are forwarded
5. Different users = different `/events` connections = different request_ids = isolated status streams

### Attack Prevention
- ❌ **Cannot subscribe to other users' status**: No direct `/status/stream` endpoint
- ❌ **Cannot guess request_ids**: Server-generated, forwarded through authenticated channel
- ❌ **Cannot intercept status events**: All flow through single authenticated SSE connection
- ✅ **Automatic cleanup**: When SSE connection closes, status subscription is closed

## Migration Path

### Removed
- `/status/stream` endpoint - **DELETED**
- Frontend status stream connection code - **REMOVED**
- `currentStatusEventSource` global variable - **REMOVED**

### Modified
- Agent server `run_events()` - Subscribes to status_bus and forwards events
- Frontend `handleSSEEvent()` - Handles new 'status' event type
- `/events` endpoint docs - Documents 'status' event type

## Testing Strategy

1. **Unit Tests**: Verify status events are forwarded through agent server
2. **Integration Tests**: Verify user A doesn't see user B's status
3. **Manual Tests**: Run tool-heavy requests (web search, scraping) and verify status appears

## Performance Impact

**Memory:**
- Before: 2 SSE connections × N requests = 2N connections
- After: 1 SSE connection × N requests = N connections
- **Improvement: 50% reduction**

**Latency:**
- Before: Status events → StatusBus → /status/stream → Frontend
- After: Status events → StatusBus → /events → Frontend
- **Same path length, no additional latency**

**Throughput:**
- Multiplexing status events into main stream adds minimal overhead
- EventSource protocol handles multiple event types efficiently

## Conclusion

Single SSE stream architecture is:
- **Simpler**: Less code, fewer moving parts
- **More Secure**: Inherent user isolation, smaller attack surface
- **More Efficient**: Fewer connections, less memory
- **More Maintainable**: One event pipeline to reason about
