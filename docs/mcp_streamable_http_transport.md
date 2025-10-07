# MCP Streamable HTTP Transport Implementation

## Overview

This document describes the implementation of the MCP (Model Context Protocol) Streamable HTTP transport in the AgentSystem project. The transport enables communication between MCP clients and servers using HTTP POST and GET requests with optional Server-Sent Events (SSE) streaming.

## What is MCP Streamable HTTP?

The MCP Streamable HTTP transport is the standard protocol for MCP communication over HTTP, as defined in the [MCP specification](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports#streamable-http).

### Key Features

- **Request-Response Model**: Every client message is sent as a separate HTTP POST request
- **Dual Response Formats**:
  - `Content-Type: application/json` for immediate single responses
  - `Content-Type: text/event-stream` for SSE streams with multiple messages
- **Session Management**: Optional session IDs via `Mcp-Session-Id` header
- **Server-Initiated Messages**: Optional standalone SSE streams via HTTP GET
- **Fresh Sessions**: Each request uses a new HTTP session to prevent connection pool issues

## Implementation Architecture

### Core Components

#### 1. HTTPStreamingTransport Class
**File**: `src/agent_system/mcp/streaming_transport.py`

The main transport implementation that handles:
- HTTP connection management
- Message serialization/deserialization
- SSE stream processing
- Session lifecycle management

#### 2. MCP Client Integration
**File**: `src/agent_system/mcp/client.py`

Integrates the transport with the MCP client:
- Transport initialization and configuration
- Request routing through the transport
- Error handling and retry logic

#### 3. MCP Integration Layer
**File**: `src/agent_system/mcp/integration.py`

Provides high-level MCP server integration:
- Tool caching and performance optimization
- Server discovery and management
- Transport abstraction for different MCP server types

#### 4. Configuration Management
**File**: `src/agent_system/config/models.py`

Configuration models for MCP settings:
- Server definitions and credentials
- Transport parameters (timeouts, SSL settings)
- Tool filtering and permissions

### Transport Flow

#### Client Request Flow
```
1. Client → MCP Client → Transport.send_request()
2. Transport creates fresh aiohttp session
3. POST request to MCP endpoint with JSON-RPC payload
4. Server responds with JSON or SSE stream
5. Transport parses response and returns MCPMessage
```

#### Server Notification Flow
```
1. Client → MCP Client → Transport.send_message()
2. Transport creates fresh aiohttp session
3. POST request with notification payload
4. Server returns 202 Accepted
```

#### Standalone SSE Stream Flow
```
1. Client → Transport.start_standalone_sse()
2. Transport creates dedicated aiohttp session
3. GET request to MCP endpoint with Accept: text/event-stream
4. Server opens SSE stream for server-initiated messages
5. Transport processes incoming messages via callback
```

## Key Implementation Details

### Session Management

The transport implements proper MCP session management:

```python
# Session ID extraction from initialize response
if 'mcp-session-id' in response.headers:
    self.session_id = response.headers['mcp-session-id']

# Session ID inclusion in subsequent requests
if include_session and self.session_id:
    headers['Mcp-Session-Id'] = self.session_id
```

### Fresh Session Strategy

To prevent aiohttp connection pool issues with SSE streams, each request creates a fresh session:

```python
# Create fresh session for each request
connector = aiohttp.TCPConnector(ssl=self.ssl_verify, limit=10, limit_per_host=5)
timeout = aiohttp.ClientTimeout(total=self.timeout)

async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
    # Use session for single request
    async with session.post(self.url, json=payload, headers=headers) as response:
        # Process response
```

### SSE Stream Processing

The transport properly handles SSE streams by consuming them completely:

```python
async def _read_sse_response(self, response, request_id):
    response_message = None

    async for line in response.content:
        decoded = line.decode('utf-8').strip()
        if decoded.startswith('data:'):
            event_data = json.loads(decoded[5:].strip())
            if event_data.get('id') == request_id:
                response_message = self._parse_json_response(event_data)
                # Continue consuming stream to avoid connection issues
```

## Configuration

### MCP Configuration File
**File**: `config/mcp.yaml`

```yaml
mcp_system:
  plugin_dirs:
    - src/plugins

  external_servers:
    smithery_context7:
      type: external
      enabled: true
      url: https://server.smithery.ai/@upstash/context7-mcp/mcp?api_key=...
      transport:
        type: streamable_http
        timeout: 30.0
        ssl_verify: true
```

### Transport Parameters

- `url`: MCP endpoint URL (preferred) or `base_url` (legacy)
- `timeout`: Request timeout in seconds (default: 30.0)
- `ssl_verify`: SSL certificate verification (default: true)
- `use_sse`: Legacy parameter, auto-detected (default: true)

## Compliance Status

### ✅ Fully Compliant (Required)

- HTTP POST requests for all messages
- Accept header: `application/json, text/event-stream`
- 202 Accepted handling for notifications
- JSON and SSE response processing
- Session management with `Mcp-Session-Id`
- Standalone SSE stream support
- Proper SSE stream consumption

### ❌ Not Implemented (Optional)

- Message batching (arrays of messages)
- SSE resumability (event IDs, Last-Event-ID)
- Server-initiated messages in request SSE streams

## Files and Responsibilities

### Core Transport Files

| File | Responsibility |
|------|----------------|
| `src/agent_system/mcp/streaming_transport.py` | Main transport implementation |
| `src/agent_system/mcp/core.py` | Base transport interfaces and types |
| `src/agent_system/mcp/client.py` | MCP client with transport integration |
| `src/agent_system/mcp/integration.py` | High-level MCP server integration |

### Configuration Files

| File | Responsibility |
|------|----------------|
| `config/mcp.yaml` | MCP server and transport configuration |
| `src/agent_system/config/models.py` | Configuration data models |

### Test Files

| File | Responsibility |
|------|----------------|
| `tests/test_mcp_client.py` | MCP client unit tests |
| `tests/test_mcp_client_factory_streaming.py` | Transport factory tests |
| `tests/test_mcp_http_streaming_transport_refactor.py` | Transport implementation tests |
| `tests/test_mcp_sse_connection_manager.py` | SSE connection management tests |

### Documentation Files

| File | Responsibility |
|------|----------------|
| `docs/mcp_configuration.md` | MCP configuration guide |
| `docs/sse_streaming_refactor_design.md` | Design document for refactoring |

## Usage Examples

### Basic Agent Usage

```bash
# Simple query (uses internal agent)
agent-cli "what is 2+2"

# External MCP server query
agent-cli "search for python tutorials"
```

### Programmatic Usage

```python
from agent_system.mcp.streaming_transport import HTTPStreamingTransport
from agent_system.mcp.client import MCPClient

# Create transport
transport = HTTPStreamingTransport(
    url="https://server.smithery.ai/@upstash/context7-mcp/mcp",
    timeout=30.0,
    ssl_verify=True
)

# Create client
client = MCPClient(transport)

# Connect and initialize
await transport.connect()
await client.initialize()

# Make requests
result = await client.call_tool("search", {"query": "python"})
```

## Testing

### Unit Tests

Run transport-specific tests:
```bash
pytest tests/test_mcp_http_streaming_transport_refactor.py -v
```

### Integration Tests

Test with real MCP servers:
```bash
pytest tests/test_mcp_client_factory_streaming.py -v
```

### End-to-End Testing

Use the CLI to test real-world scenarios:
```bash
agent-cli "get weather for New York"
agent-cli "search for python documentation"
```

## Performance Considerations

### Connection Management
- Fresh sessions prevent connection pool exhaustion
- Configurable connection limits (`limit=10, limit_per_host=5`)
- Proper timeout handling prevents hanging requests

### Resource Cleanup
- Async context managers ensure proper session cleanup
- SSE streams fully consumed to prevent connection issues
- Standalone SSE tasks properly cancelled on disconnect

### Caching
- Tool caching implemented in `integration.py` for performance
- Cache keyed by server name and configuration hash
- Automatic cache invalidation on configuration changes

## Troubleshooting

### Common Issues

1. **Timeout Errors**: Check network connectivity and increase timeout
2. **SSL Errors**: Verify `ssl_verify` setting or certificate validity
3. **405 Method Not Allowed**: Server doesn't support the requested HTTP method
4. **404 Session Expired**: Re-initialize session (handled automatically)

### Debug Logging

Enable debug logging to troubleshoot issues:
```python
import logging
logging.getLogger('agent_system.mcp.streaming_transport').setLevel(logging.DEBUG)
```

### Connection Monitoring

Monitor active connections and sessions:
- Check `_standalone_sse_task` status for SSE streams
- Verify `session_id` is set after initialization
- Monitor HTTP status codes in logs

## Future Enhancements

### Optional Features to Consider

1. **Message Batching**: Support arrays of JSON-RPC messages
2. **SSE Resumability**: Implement event IDs and Last-Event-ID header
3. **Advanced Session Management**: Session persistence and recovery
4. **Connection Pooling**: Shared connection pools for performance (if SSE issues resolved)

## Conclusion

The MCP Streamable HTTP transport implementation provides full compliance with the MCP specification while maintaining robust error handling and resource management. The fresh session approach ensures reliable operation with external MCP servers, and the modular architecture supports easy extension and maintenance.</content>
<parameter name="filePath">e:\Projects\AgentSystem\docs\mcp_streamable_http_transport.md