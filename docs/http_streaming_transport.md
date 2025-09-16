"""
HTTP Streaming Transport for MCP

This document covers the HTTPStreamingTransport implementation in AgentSystem's MCP
framework, providing detailed information about features, configuration, and usage.
"""

# HTTP Streaming Transport for MCP

## Overview

The `HTTPStreamingTransport` class provides an advanced HTTP-based transport layer for the Model Context Protocol (MCP) with Server-Sent Events (SSE) support. This transport enables real-time status streaming, efficient session management, and robust error handling.

## Key Features

### Real-time Status Streaming
- Status events are pushed to clients via Server-Sent Events (SSE)
- JSON-RPC 2.0 notifications with method `notifications/status`
- Supports filtering by server and request_id
- Automatic event forwarding from the status bus

### Base64 Configuration Encoding
- Configuration data is automatically encoded in base64 for secure transmission
- Prevents configuration exposure in URLs or logs
- Supports complex nested configuration objects

### Session Management
- Unique session IDs for each connection
- Configurable session lifetime and keepalive intervals
- Automatic session cleanup and resource management
- Session-based event filtering and routing

### Connection Lifecycle
- Automatic connection establishment and teardown
- Graceful error handling and recovery
- Support for connection pooling and reuse
- Health checks and connection validation

## Configuration

### Basic Configuration

```yaml
mcp:
  servers:
    streaming_server:
      url: "https://api.example.com/mcp"
      transport_type: "http"  # Use "http" for new configurations
      enabled: true
      description: "Streaming MCP server"
      
      # Transport-specific settings
      timeout: 30.0
      max_retries: 3
      retry_delay: 1.0
      ssl_verify: true
      
      # Streaming configuration
      stream_status_events: true
      session_config:
        max_session_lifetime: 3600  # 1 hour
        keepalive_interval: 30      # 30 seconds
        buffer_size: 8192
        max_event_queue_size: 1000
```

### Legacy Configuration (Deprecated)

```yaml
mcp:
  servers:
    legacy_server:
      transport_type: "smithery"  # Deprecated - use "http" instead
      # ... other settings
```

**Migration Note**: The `smithery` transport type is deprecated. Update configurations to use `transport_type: "http"`. The system will automatically detect and use streaming capabilities when available.

## Usage Examples

### Basic Connection

```python
from agent_system.mcp.streaming_transport import HTTPStreamingTransport
from agent_system.mcp.client import StandardMCPClient

# Create transport
transport = HTTPStreamingTransport(
    url="https://api.example.com/mcp",
    config={"api_key": "your-api-key"}
)

# Create client
client = StandardMCPClient(transport, name="MyClient")

# Connect and use
await client.connect()
tools = await client.list_tools()
```

### With Status Event Handling

```python
import asyncio
from agent_system.mcp.streaming_transport import HTTPStreamingTransport

async def handle_status_events(transport):
    """Example status event handler"""
    async for event in transport.status_stream():
        print(f"Status: {event['message']} (Phase: {event['phase']})")

# Create transport with status streaming
transport = HTTPStreamingTransport(
    url="https://streaming.example.com/mcp",
    config={"stream_events": True}
)

# Start status event handling
asyncio.create_task(handle_status_events(transport))
```

## CLI Commands

### Server Management

```bash
# List all configured MCP servers
agent-cli mcp list

# Show detailed server information
agent-cli mcp list --format json

# Connect to a streaming server
agent-cli mcp connect streaming_server

# Check connection status and streaming info
agent-cli mcp status streaming_server

# Test server connectivity and features
agent-cli mcp test streaming_server
```

### Feature Management

```bash
# List available features
agent-cli mcp feature list streaming_server

# Enable/disable specific features
agent-cli mcp feature streaming_server tools on
agent-cli mcp feature streaming_server status_stream off
```

## Event Format

### Status Notification Format

Status events are transmitted as JSON-RPC 2.0 notifications:

```json
{
  "jsonrpc": "2.0", 
  "method": "notifications/status",
  "params": {
    "server": "streaming_server",
    "request_id": "req_123", 
    "message": "Processing request",
    "timestamp": "2025-01-15T10:30:45Z",
    "phase": "progress",
    "level": "info",
    "meta": {
      "step": 3,
      "total": 10,
      "percentage": 30
    }
  }
}
```

### Error Event Format

Error events include detailed error information:

```json
{
  "jsonrpc": "2.0",
  "method": "notifications/status", 
  "params": {
    "server": "streaming_server",
    "request_id": "req_123",
    "message": "Connection failed",
    "timestamp": "2025-01-15T10:30:45Z",
    "phase": "error",
    "level": "error",
    "meta": {
      "error_code": "CONNECTION_FAILED",
      "retry_count": 2,
      "next_retry": "2025-01-15T10:31:00Z"
    }
  }
}
```

## Error Handling

### Connection Errors

The transport implements robust error handling:

- **Automatic retries**: Configurable retry count and delay
- **Exponential backoff**: Prevents overwhelming failed servers
- **Circuit breaker**: Temporarily disables failed connections
- **Graceful degradation**: Falls back to basic HTTP when streaming fails

### Session Management Errors

- **Session expiry**: Automatic session renewal before expiration
- **Invalid sessions**: Detection and cleanup of corrupted sessions
- **Resource limits**: Protection against memory leaks and resource exhaustion

## Performance Considerations

### Memory Usage

- Event queues are bounded to prevent memory growth
- Automatic cleanup of expired sessions and connections
- Efficient JSON parsing and serialization
- Stream buffering to optimize network usage

### Network Efficiency

- Keep-alive connections reduce connection overhead
- Base64 encoding minimizes configuration exposure
- Event batching reduces network chattiness
- Compression support for large payloads

### Scalability

- Support for multiple concurrent connections
- Configurable connection pooling
- Resource isolation between sessions
- Monitoring and metrics integration

## Troubleshooting

### Common Issues

1. **Connection timeouts**:
   - Check network connectivity
   - Verify server URL and port
   - Increase timeout values in configuration

2. **Status events not received**:
   - Ensure `stream_status_events: true` is set
   - Check server supports SSE streaming
   - Verify firewall/proxy settings

3. **Session expiry errors**:
   - Increase `max_session_lifetime` setting
   - Check server session management
   - Monitor connection stability

### Debug Configuration

Enable detailed logging for troubleshooting:

```yaml
logging:
  level: DEBUG
  modules:
    agent_system.mcp.streaming_transport: DEBUG
    agent_system.mcp.client: DEBUG
```

### CLI Diagnostics

```bash
# Test connectivity with verbose output
agent-cli mcp test streaming_server --verbose

# Check detailed status information
agent-cli mcp status streaming_server --format json

# Monitor real-time events (if supported)
agent-cli mcp monitor streaming_server
```

## Migration Guide

### From SmitheryHTTPTransport

1. **Update configuration**:
   ```yaml
   # Before (deprecated)
   transport_type: "smithery"
   
   # After (recommended)
   transport_type: "http"
   ```

2. **Update code imports**:
   ```python
   # Before
   from agent_system.mcp.smithery_transport import SmitheryHTTPTransport
   
   # After
   from agent_system.mcp.streaming_transport import HTTPStreamingTransport
   ```

3. **Configuration compatibility**:
   - All existing configuration options are supported
   - New streaming options are available but optional
   - Legacy configurations continue to work during transition

### Breaking Changes

- The `smithery` transport type is deprecated but still functional
- No immediate breaking changes, but plan migration for future versions
- New features only available with `http` transport type

## Best Practices

### Configuration

- Use environment variables for sensitive data
- Set appropriate timeouts based on expected response times
- Configure retries and circuit breakers for reliability
- Enable SSL verification in production environments

### Error Handling

- Implement proper error handling for connection failures
- Use status events to monitor long-running operations
- Set up alerting for connection and authentication errors
- Log transport events for debugging and monitoring

### Performance

- Tune session lifetime based on usage patterns
- Monitor memory usage and connection counts
- Use connection pooling for high-throughput scenarios
- Consider load balancing for multiple server instances

## Related Documentation

- [MCP Configuration Guide](mcp_configuration.md)
- [MCP Status Event Streaming](mcp_status_streaming.md)
- [Plugin Authoring Guide](plugin_authoring.md)