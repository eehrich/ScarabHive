# SSE Streaming Transport Refactor Design

## Problem Analysis

Current HTTPStreamingTransport makes individual HTTP POST requests for each MCP message.
Smithery servers expect persistent SSE (Server-Sent Events) connections that stay open.

**Current Issues:**
- Each request creates a new HTTP connection
- Connections are closed after each response
- Smithery servers return "Transport is closed" for subsequent requests
- No session state persistence between requests

## Proposed Architecture

### 1. SSEConnectionManager Class

Manages persistent SSE connections for each MCP server endpoint.

**Responsibilities:**
- Maintain persistent aiohttp session per server
- Handle SSE stream parsing and message routing
- Connection health monitoring and reconnection
- Request-response correlation via message IDs

**Key Methods:**
```python
class SSEConnectionManager:
    async def connect(self, url: str) -> str:  # Returns connection_id
    async def disconnect(self, connection_id: str) -> None:
    async def send_request(self, connection_id: str, message: MCPMessage) -> MCPMessage:
    async def get_connection_status(self, connection_id: str) -> ConnectionStatus:
```

### 2. Connection Lifecycle

**States:**
- DISCONNECTED: No active connection
- CONNECTING: Establishing SSE stream
- CONNECTED: SSE stream active, receiving events
- RECONNECTING: Connection lost, attempting reconnect
- ERROR: Connection failed, needs manual intervention

**Events:**
- Connection established → Send initialize
- SSE event received → Parse and route to waiting request
- Connection lost → Attempt reconnect with backoff
- Timeout → Mark as error

### 3. Message Flow

**Request Path:**
1. Client calls `send_request(message)`
2. Message queued with correlation ID
3. HTTP POST sent over persistent connection
4. Response awaited via Future/Promise

**Response Path:**
1. SSE stream receives `data: {...}` event
2. JSON parsed and matched to correlation ID
3. Future resolved with response
4. Client receives response

### 4. Error Handling

**Connection Errors:**
- Network timeouts → Reconnect with exponential backoff
- SSL errors → Log and mark connection as failed
- Server errors → Retry with different endpoint if available

**Message Errors:**
- Invalid JSON → Log and continue
- Missing correlation ID → Log warning
- Timeout waiting for response → Return timeout error

### 5. Integration Points

**HTTPStreamingTransport Changes:**
- Replace individual requests with SSEConnectionManager calls
- Maintain backward compatibility for non-SSE servers
- Add connection pooling for multiple servers

**MCPClientFactory Changes:**
- Detect server capabilities during initialize
- Use SSE transport for servers that support it
- Fallback to HTTP for legacy servers

## Implementation Plan

1. **Phase 1**: Create SSEConnectionManager skeleton
2. **Phase 2**: Implement basic SSE connection handling
3. **Phase 3**: Add message correlation and routing
4. **Phase 4**: Integrate with HTTPStreamingTransport
5. **Phase 5**: Add reconnection logic and error handling
6. **Phase 6**: Test with Smithery servers

## Testing Strategy

**Unit Tests:**
- SSEConnectionManager connection lifecycle
- Message correlation and routing
- Error handling scenarios

**Integration Tests:**
- Full MCP client initialization with SSE
- Tool listing and calling with Smithery servers
- Connection recovery after network issues

**Compatibility Tests:**
- Ensure existing HTTP transport still works
- Test with multiple server types simultaneously