# MCP Configuration Guide

## Overview

The Model Context Protocol (MCP) implementation in AgentSystem provides seamless integration with external MCP servers and exposes local plugins as MCP endpoints. This document covers configuration, authentication, and usage.

## Configuration Schema

### Basic Configuration

Create or update your `config/mcp.yaml` file:

```yaml
mcp:
  enabled: true

  # Local server settings
  expose_local_server: true
  local_server_port: 8000
  local_server_host: "localhost"

  # Global settings
  default_timeout: 30.0
  max_concurrent_requests: 10
  enable_health_checks: true
  health_check_interval: 300.0  # 5 minutes

  # Security
  require_auth: false
  allowed_origins: ["http://localhost:3000"]
  rate_limit_requests: 1000
  rate_limit_window: 3600  # 1 hour

  # External MCP servers
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
        prefix: "weather_"
        allowed: ["get_forecast", "get_current"]
        blocked: ["admin_tools"]
      tags: ["weather", "data"]
      priority: 50
```

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

Advanced HTTP transport with Server-Sent Events (SSE) for real-time status streaming:

```yaml
external_servers:
  streaming_service:
    url: "https://streaming.example.com/mcp"
    transport_type: "smithery"  # Legacy alias, use "http" for new configurations
    enabled: true
    timeout: 30.0
    stream_status_events: true  # Enable real-time status streaming
    session_config:
      max_session_lifetime: 3600  # 1 hour
      keepalive_interval: 30      # 30 seconds
```

**Features of HTTP Streaming Transport:**
- **Real-time status updates**: Receive status events via SSE streams
- **Base64 config encoding**: Secure configuration transmission
- **Session management**: Persistent sessions with unique identifiers
- **Connection recovery**: Automatic reconnection on network failures
- **Resource efficiency**: Long-lived connections reduce overhead

**Migration Note**: The `smithery` transport type is deprecated. Use `http` for new configurations. The system automatically uses streaming features when available.

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
python -m agent_system.cli plugins

# Run MCP-enabled API server
python -m agent_system.agent.interface_api
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

The web scraper plugin supports proxy rotation to avoid IP-based blocking. Configure proxies in your `config/mcp.yaml`:

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