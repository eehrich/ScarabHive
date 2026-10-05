# MCP Configuration Guide

## Overview

This guide covers the connections to **external** MCP servers: configuration, authentication and usage. They are the `mcp_client` plugin's business; the system's own plugins are tool servers and speak no protocol (see `plugin_authoring.md`).

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

**Note**: System-wide settings (ports, security, etc.) are configured in `config/config.yaml`.

## Transport Types

Transports are the official MCP SDK's; the `mcp_client` plugin selects one from
the server's `transport` field. Note the field is `transport`, not
`transport_type` — an unknown key is dropped by the config model without a
word, so a server configured with `transport_type` silently gets the default.

| `transport` | SDK client | When |
|---|---|---|
| `streaming`, `streamable_http`, `streamable-http`, `smithery` | `streamable_http_client` | The usual case for a remote server (the shipped entries are all stdio) |
| `http` | `streamable_http_client` | Historically bare JSON-RPC POSTs. No current server answers those (measured: HTTP 406), and those endpoints speak streamable HTTP today |
| `sse`, `http_sse`, `http+sse` | `sse_client` | The older HTTP+SSE transport |
| `stdio`, `local` | `stdio_client` | A locally launched server: needs `command` (plus optional `args`, `env`) instead of `url` |

```yaml
external_servers:
  remote_servers:
    basic_service:
      url: "https://api.example.com/mcp"
      transport: streaming
      enabled: true

    local_service:
      transport: stdio
      enabled: true
      command: "/usr/bin/python3"
      args: ["/opt/mcp/server.py"]
```

Connection settings come from `external_servers.connection` (timeout) and
`network.ssl_verify`. `initialization_options` is accepted but **not sent** —
the protocol has no such field, and connecting warns about it. Put credentials
in `auth:` or in the URL.

For the transport internals, the connection lifecycle and why each connection
owns a task, see `src/plugins/mcp_client/README.md`.

## CLI Management

Die CLI liest nur. Jeder Aufruf verbindet die eingeschalteten Server und trennt
danach wieder; eingeschaltet und gefiltert wird in dieser Datei.

```bash
# List all configured servers
agent-cli mcp list

# Check connection status
agent-cli mcp status weather_service

# Test server functionality
agent-cli mcp test weather_service

# Tools, blocked ones marked
agent-cli mcp tools weather_service
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
  blocked: ["delete", "admin"]   # These tools blocked
```

`blocked` ist die einzige Liste, die bei einem externen Server wirkt:
`mcp_client` verweigert den Aufruf. Ein `allowed` am Server wertet niemand aus —
welche Tools ein Agent aufrufen darf, regelt dessen `agent_config.tools.allowed`.

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
from agent_system.tools import ToolServerIntegration

# Initialize with configuration
mcp = ToolServerIntegration(config=config_dict)
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

