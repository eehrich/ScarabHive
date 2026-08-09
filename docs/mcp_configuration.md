# MCP Configuration Guide

## Overview

The Model Context Protocol (MCP) implementation in AgentSystem provides seamless integration with external MCP servers and exposes local plugins as MCP endpoints. This document covers configuration, authentication, and usage.

## Configuration Schema

### Basic Configuration

MCP servers are configured in `config/mcp_servers.yaml`:

```yaml
# External MCP server connections
external_servers:
  weather_service:
    url: "https://api.weather.com/mcp"
    enabled: true
    description: "Weather data and forecasting"
    auth:
      type: "api_key"
      api_key: "${WEATHER_API_KEY}"
      api_key_header: "X-API-Key"
    ssl_verify: true
    timeout: 30.0
    max_retries: 3
    retry_delay: 1.0
    features:
      tools: true
      resources: true
      prompts: true
    tools:
      allowed: ["get_forecast", "get_current"]
      blocked: ["admin_tools"]
    tags: ["weather", "data"]
    priority: 50

# Connection and cache settings
connection:
  timeout: 5.0
  parallel_connect: true

cache:
  enabled: true
  tool_list_ttl: 30.0
  max_size: 1000
```

**Note**: System-wide MCP settings (ports, security, etc.) are configured in `config/config.yaml` under the `mcp` section.

## Transport Types

AgentSystem supports multiple transport protocols for MCP communication:

### HTTP Transport

Standard HTTP transport for basic MCP communication:

```yaml
external_servers:
  basic_service:
    url: "https://api.example.com/mcp"
    transport_type: "http"
    enabled: true
    timeout: 30.0
```

### HTTP Streaming Transport

The `HTTPStreamingTransport` class provides an advanced HTTP-based transport layer for the Model Context Protocol (MCP) with Server-Sent Events (SSE) support. This transport enables real-time status streaming, efficient session management, and robust error handling.

#### Key Features

**Real-time Status Streaming**
- Status events are pushed to clients via Server-Sent Events (SSE)
- JSON-RPC 2.0 notifications with method `notifications/status`
- Supports filtering by server and request_id
- Automatic event forwarding from the status bus

**Base64 Configuration Encoding**
- Configuration data is automatically encoded in base64 for secure transmission
- Prevents configuration exposure in URLs or logs
- Supports complex nested configuration objects

**Session Management**
- Unique session IDs for each connection
- Configurable session lifetime and keepalive intervals
- Automatic session cleanup and resource management
- Session-based event filtering and routing

**Connection Lifecycle**
- Automatic connection establishment and teardown
- Graceful error handling and recovery
- Support for connection pooling and reuse
- Health checks and connection validation

#### Configuration

```yaml
external_servers:
  streaming_service:
    url: "https://streaming.example.com/mcp"
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

**Migration Note**: The `smithery` transport type is deprecated. Update configurations to use `transport_type: "http"`. The system will automatically detect and use streaming capabilities when available.

#### Usage Examples

**Basic Connection**
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

**With Status Event Handling**
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

#### CLI Commands

**Server Management**
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

**Feature Management**
```bash
# List available features
agent-cli mcp feature list streaming_server

# Enable/disable specific features
agent-cli mcp feature streaming_server tools on
agent-cli mcp feature streaming_server status_stream off
```

#### Event Format

**Status Notification Format**
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

**Error Event Format**
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

#### Error Handling

**Connection Errors**
The transport implements robust error handling:
- **Automatic retries**: Configurable retry count and delay
- **Exponential backoff**: Prevents overwhelming failed servers
- **Circuit breaker**: Temporarily disables failed connections
- **Graceful degradation**: Falls back to basic HTTP when streaming fails

**Session Management Errors**
- **Session expiry**: Automatic session renewal before expiration
- **Invalid sessions**: Detection and cleanup of corrupted sessions
- **Resource limits**: Protection against memory leaks and resource exhaustion

#### Performance Considerations

**Memory Usage**
- Event queues are bounded to prevent memory growth
- Automatic cleanup of expired sessions and connections
- Efficient JSON parsing and serialization
- Stream buffering to optimize network usage

**Network Efficiency**
- Keep-alive connections reduce connection overhead
- Base64 encoding minimizes configuration exposure
- Event batching reduces network chattiness
- Compression support for large payloads

**Scalability**
- Support for multiple concurrent connections
- Configurable connection pooling
- Resource isolation between sessions
- Monitoring and metrics integration

#### Troubleshooting

**Common Issues**
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

**Debug Configuration**
Enable detailed logging for troubleshooting:

```yaml
logging:
  level: DEBUG
  modules:
    agent_system.mcp.streaming_transport: DEBUG
    agent_system.mcp.client: DEBUG
```

**CLI Diagnostics**
```bash
# Test connectivity with verbose output
agent-cli mcp test streaming_server --verbose

# Check detailed status information
agent-cli mcp status streaming_server --format json

# Monitor real-time events (if supported)
agent-cli mcp monitor streaming_server
```

#### Best Practices

**Configuration**
- Use environment variables for sensitive data
- Set appropriate timeouts based on expected response times
- Configure retries and circuit breakers for reliability
- Enable SSL verification in production environments

**Error Handling**
- Implement proper error handling for connection failures
- Use status events to monitor long-running operations
- Set up alerting for connection and authentication errors
- Log transport events for debugging and monitoring

**Performance**
- Tune session lifetime based on usage patterns
- Monitor memory usage and connection counts
- Use connection pooling for high-throughput scenarios
- Consider load balancing for multiple server instances

## CLI Management

Use the built-in CLI commands to manage MCP servers:

```bash
# List all configured servers
agent-cli mcp list

# Connect to a server
agent-cli mcp connect weather_service

# Check connection status
agent-cli mcp status weather_service

# Test server functionality
agent-cli mcp test weather_service

# Disconnect from a server
agent-cli mcp disconnect weather_service
```

## Authentication Types

### API Key Authentication

```yaml
auth:
  type: "api_key"
  api_key: "${YOUR_API_KEY}"
  api_key_header: "X-API-Key"  # Optional, defaults to "Authorization"
```

### Bearer Token Authentication

```yaml
auth:
  type: "bearer"
  bearer_token: "${YOUR_BEARER_TOKEN}"
```

### Basic Authentication

```yaml
auth:
  type: "basic"
  username: "${YOUR_USERNAME}"
  password: "${YOUR_PASSWORD}"
```

### No Authentication

```yaml
auth:
  type: "none"
```

## Environment Variables

Use environment variables for sensitive data:

- `${WEATHER_API_KEY}` - Weather service API key
- `${DATABASE_TOKEN}` - Database access token
- `${SERVICE_USERNAME}` - Service username
- `${SERVICE_PASSWORD}` - Service password

Example `.env` file:

```bash
WEATHER_API_KEY=your_weather_api_key_here
DATABASE_TOKEN=your_database_token_here
SERVICE_USERNAME=your_username
SERVICE_PASSWORD=your_password
```

## Feature Configuration

### Tools Filtering

Control which tools are available from external servers:

```yaml
tools:
  prefix: "external_"          # Add prefix to all tool names
  allowed: ["search", "analyze"] # Only these tools allowed
  blocked: ["delete", "admin"]   # These tools blocked
```

### Resources Filtering

Similar filtering for resources:

```yaml
resources:
  prefix: "res_"
  allowed: ["documents", "images"]
  blocked: ["sensitive_data"]
```

### Prompts Filtering

Control prompt availability:

```yaml
prompts:
  prefix: "prompt_"
  allowed: ["generate", "summarize"]
  blocked: ["system_prompts"]
```

## Complete Example

```yaml
mcp:
  enabled: true
  expose_local_server: true
  local_server_port: 8000

  external_servers:
    # Weather service with API key auth
    weather_api:
      url: "https://api.weather.com/mcp"
      enabled: true
      description: "Weather forecasting and current conditions"
      auth:
        type: "api_key"
        api_key: "${WEATHER_API_KEY}"
        api_key_header: "X-Weather-Key"
      features:
        tools: true
        resources: false
        prompts: true
      tools:
        prefix: "weather_"
        allowed: ["forecast", "current", "alerts"]
      priority: 10

    # Database service with bearer token
    database_service:
      url: "https://db.company.com/mcp"
      enabled: true
      description: "Company database access"
      auth:
        type: "bearer"
        bearer_token: "${DB_ACCESS_TOKEN}"
      ssl_verify: true
      timeout: 60.0
      features:
        tools: true
        resources: true
        prompts: false
      tools:
        prefix: "db_"
        blocked: ["delete", "drop", "truncate"]
      priority: 20

    # Analytics service with basic auth
    analytics:
      url: "https://analytics.company.com/mcp"
      enabled: true
      auth:
        type: "basic"
        username: "${ANALYTICS_USER}"
        password: "${ANALYTICS_PASS}"
      features:
        tools: true
        resources: true
        prompts: true
      tools:
        prefix: "analytics_"
      priority: 30
```

## Entry Agent Selection (Dynamic)

The system no longer relies on a hard-coded `MainAgent`. Instead you can choose which
registered agent server acts as the primary entry point for `/run` and `/events` calls.

Add to your top-level configuration (e.g. `agent.yaml` or merged config):

```yaml
entry_agent: basic_agent
```

Behavior:
* If `entry_agent` matches a plugin-provided agent (e.g. `basic_agent`), that instance is used.
* If it does not exist, a core `Agent` is created under that name.
* For backward compatibility an alias `agent` is also registered pointing to the chosen entry agent.
* Legacy module `main_agent.py` has been deprecated and replaced by this dynamic selection.

## Per-Agent Tool Allow / Deny Lists

Each agent can define which tool servers it may use via an allow list (and optional block list):

```yaml
servers:
  basic_agent:
    type: basic_agent
    agent_config:
      allowed_tools:
        - "web_scraper/*"
        - "web_research_agent/*"
      blocked_tools:
        - "web_scraper.experimental_*"
```

Policy:
* Default is DENY-ALL if `allowed_tools` is absent or empty.
* `allowed_tools` patterns support:
  * `plugin` or `plugin/*` – all tools from a plugin
  * `plugin.function` – single function
  * `external_server/*` or `external_server.tool`
  * `*` – allow everything (use cautiously)
* `blocked_tools` (if present) is applied after allow filtering and subtracts matches.
* Unmatched patterns are logged at DEBUG level to help diagnose typos.

Diagnostics Endpoints:
* `GET /agents` – list agent servers.
* `GET /agents/{name}/allowed-tools` – effective filtered list (already filtered, default deny may yield empty list).
* `GET /agents/{name}/allowed-tools/debug` – includes which patterns matched each tool.

Examples:

| Configuration | Effective result |
|---------------|------------------|
| (no allowed_tools) | No tools available to that agent |
| allowed_tools: ["*"] | All discovered tool servers available |
| allowed_tools: ["web_scraper/*", "datetime.*"], blocked_tools: ["datetime.legacy_*"] | Only web_scraper tools and datetime.* minus legacy_* |

## Migration Notes

| Legacy | New Approach |
|--------|--------------|
| `MainAgent` class | Removed; use `entry_agent` selection |
| Implicit all tools available | Default deny-all until explicitly allowed |
| Ad-hoc tool filtering in code | Centralized in `Agent.list_usable_tools()` |

To migrate existing deployments:
1. Add an explicit `entry_agent` if you relied on a custom main agent.
2. Add `allowed_tools` lists for each agent that should have tool access.
3. (Optional) Use `*` temporarily while phasing in tighter allow lists.
4. Verify via `/agents/{entry_agent}/allowed-tools/debug`.


## Usage Examples

### Python Code Integration

```python
from agent_system.mcp import MCPIntegration

# Initialize with configuration
mcp = MCPIntegration(config=config_dict)
await mcp.initialize()

# List all available tools
tools = await mcp.list_all_tools()
print(f"Available tools: {tools}")

# Get FastAPI app with MCP endpoints
app = mcp.get_app()

# Shutdown when done
await mcp.shutdown()
```

### CLI Usage

```bash
# List available plugins
python -m agent_system.agent_cli plugins

# Run MCP-enabled API server
python -m agent_system.app
```

## Security Best Practices

1. **Environment Variables**: Always use environment variables for secrets
2. **SSL Verification**: Keep `ssl_verify: true` for production
3. **Rate Limiting**: Configure appropriate rate limits
4. **Tool Filtering**: Use allowlists for critical services
5. **Network Security**: Restrict `allowed_origins` in production
6. **Regular Rotation**: Rotate API keys and tokens regularly

## Troubleshooting

### Connection Issues

1. Check server URL and network connectivity
2. Verify authentication credentials
3. Check SSL certificate validity
4. Review server logs for errors

### Performance Issues

1. Adjust timeout values
2. Reduce `max_concurrent_requests`
3. Enable health checks for early detection
4. Monitor server response times

### Authentication Problems

1. Verify environment variables are set
2. Check API key/token validity
3. Confirm correct authentication type
4. Review server authentication requirements

## Advanced Configuration

### Health Checks

```yaml
mcp:
  enable_health_checks: true
  health_check_interval: 300.0  # Check every 5 minutes
```

### Rate Limiting

```yaml
mcp:
  rate_limit_requests: 1000   # Max requests per window
  rate_limit_window: 3600     # Window size in seconds
```

### Web Scraper Proxy Configuration

The web scraper plugin supports proxy rotation to avoid IP-based blocking. Configure proxies in your `config/plugins.yaml`:

```yaml
servers:
  web_scraper:
    type: web_scraper
    proxies:
      - "http://proxy1.example.com:8080"
      - "https://proxy2.example.com:8080"
      - "socks5://proxy3.example.com:1080"
```

#### Proxy Options

- **Free Proxies**: Not recommended due to unreliability, slow speeds, and frequent blocking
- **Paid Proxy Services**: Recommended for production use
  - Bright Data (formerly Luminati)
  - Oxylabs
  - Smart Proxy
  - ProxyMesh
- **Residential Proxies**: Best for avoiding detection, but most expensive
- **Datacenter Proxies**: Faster but more easily detected

#### Proxy Rotation

The web scraper automatically rotates through configured proxies based on the target domain, improving success rates against anti-bot measures.

### CORS Configuration

```yaml
mcp:
  allowed_origins:
    - "http://localhost:3000"
    - "https://your-frontend.com"
```

This configuration provides a robust, secure, and flexible MCP integration for your AgentSystem deployment.

