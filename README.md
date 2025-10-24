# AgentSystem — Flexible MCP-based Agent Framework (Python)

A lightweight, pluggable agent framework that composes LLMs and tool servers using the Model Context Protocol (MCP).

This README is a concise developer and user guide matching this repository layout.

## Quick links
- Code: `src/agent_system`
- Config: all yaml files in `config/`
- Docs: `docs/`
- Logfiles: `logs/` - AgentSystem logfiles. cli and api
- Plugins: `src/plugins/` (each plugin has its own README.md)
  - [Basic Operations](src/plugins/basic_operations/README.md) — Utility tools for testing and orchestration
  - [DateTime](src/plugins/datetime/README.md) — Date and time operations with timezone support
  - [DuckDuckGo Search](src/plugins/duckduckgo_search/README.md) — Privacy-focused web search
  - [Example Plugin](src/plugins/example/README.md) — Reference implementation for plugin development
  - [HTTP Server](src/plugins/http_server/README.md) — HTTP adapter for MCP servers
  - [IBKR](src/plugins/ibkr/README.md) — Interactive Brokers trading platform integration
  - [LLM Router](src/plugins/llm_router/README.md) — Multi-provider LLM routing with profiles
  - [Log Viewer](src/plugins/log_viewer/README.md) — Log file management with web interface
  - [Markdown Formatter](src/plugins/markdown_formatter/README.md) — Multi-format output rendering (HTML, ANSI, text)
  - [Script Interpreter](src/plugins/script_interpreter/README.md) — Sandboxed Python code execution
  - [Sequential Thinking](src/plugins/sequential_thinking/README.md) — Step-by-step reasoning with branching and revision
  - [SSH Control](src/plugins/ssh_control/README.md) — Multi-machine SSH management with web UI
  - [Twitter Search](src/plugins/twitter_search/README.md) — Twitter/X public content search
  - [User Management](src/plugins/user_management/) — Web-based user administration and role management
  - [Weather](src/plugins/weather/README.md) — Weather information with multiple data sources
  - [Web Research Agent](src/plugins/web_research_agent/README.md) — Advanced web research and fact-checking
  - [Web Scraper](src/plugins/web_scraper/README.md) — Web content extraction and scraping
  - [Yahoo Finance](src/plugins/yahoo_finance/README.md) — Stock market data with fallback scraping
- Tests: `tests/` (pytest)
- Prompts for assistant sessions: `.prompts/` and `.github/`
- Helper Scripts: `scripts/`
- Ticketsystem/Backlog: `backlog.md`
- Python-ProjectSetup: `pyenvironment.toml`

## Features
* Modular agent core with MCP integration (consume & expose tool servers)
* Pluggable plugin system (local + external MCP servers)
* **Multi-Format Output Rendering**: Adaptive formatting for different interfaces
  - HTML with Prism.js syntax highlighting for web frontend
  - ANSI colored terminal output for CLI
  - Plain Markdown storage format
  - Same content optimized for each interface
  - See [Multi-Format Output Guide](docs/multi_format_output.md) for details
* **Configuration-Based Agents (Epic 0043)**: Create custom agents without writing code
  - Define agents purely in YAML configuration
  - Configure prompts, tools, LLM profiles, and context management
  - CLI commands for listing, validation, and inspection
  - API endpoints for programmatic access
  - See [Configuration-Based Agents Guide](docs/config_based_agents.md) for details
* **MCP Server Mode (Epic 0037)**: Expose AgentSystem as a remote MCP server
  - Activated plugins become MCP tools accessible to remote MCP clients
  - JSON-RPC 2.0 protocol with HTTP Streamable Transport
  - Session management, authentication, and rate limiting
  - Tool discovery and execution via MCP protocol endpoints
* **Multi-user authentication and authorization** with JWT tokens and API keys
  - Role-based access control (ADMIN, USER, GUEST)
  - Web-based user management interface
  - Flexible dropdown menu system for UI organization
  - CLI commands for user administration
* **Multimodal vision support** with image uploads via WebUI and API (see [Vision Support](docs/vision_support.md))
* Backlog & status management
* Context window management (summarization / truncation strategies)
* **Real-time Streaming Events** (SSE): Unified `/events` endpoint streams all events including:
  - Sub-agent status events during parallel tool execution
  - LLM thinking process and responses
  - Tool execution results
  - Zero CPU overhead when idle (blocking queue pattern)
  - See [Streaming Tool Execution](docs/streaming_tool_execution.md) for architecture details
* Per-agent tool allow / deny lists (secure default: deny-all until explicitly allowed)
* Configurable entry agent (no more hard-coded MainAgent; uses `entry_agent` setting)
* Diagnostics endpoints for tool filtering (`/agents/{name}/allowed-tools[ /debug]`)
* Structured configuration with include support and environment overrides
* Test-first design with extensive pytest suite
* Simplified CLI entrypoint (legacy `cli_agent` alias removed; use the configured `entry_agent` name)

## Requirements
- Python 3.11+
- Git (for development)
- Optional: API key(s) for LLM providers (configured via env vars or `config/agent.yaml`)

## Recommended shell on Windows
On Windows we recommend Git Bash (or another bash-compatible shell) for interactive debugging and venv activation. PowerShell examples remain supported but the repository's examples below use bash.

## Quickstart (Git Bash — recommended on Windows)
1. Create and activate a virtualenv (Git Bash):

```bash
python -m venv .venv
source .venv/Scripts/activate
```

(PowerShell alternative shown for reference in Windows environments.)

```powershell
python -m venv .venv
. .venv/Scripts/Activate.ps1    # may require changing ExecutionPolicy
```

(CMD alternative)

```cmd
python -m venv .venv
.venv\Scripts\activate.bat
```

2. Install the project (editable):

```bash
pip install -U pip
pip install -e .
```

3. Run tests:

```bash
python -m pytest -q
```

## How to use
- CLI: `agent-cli "Your question or task"` or `agent-cli --help`
- API: start the HTTP API in a separate, activated terminal and call the endpoints below.

Start the API (recommended in a second terminal):

```bash
with venv activated in the terminal
python -m agent_system.app
# or the convenience wrapper (if installed in PATH)
agent-api
```

API endpoints (FastAPI):
- `GET /health` — health check with uptime and version info
- `GET /config` — returns loaded configuration
- `GET /agents` — list all registered agent servers
- `GET /agents/{agent_name}/allowed-tools` — get effective allowed tools for an agent
- `GET /agents/{agent_name}/allowed-tools/debug` — detailed pattern match diagnostics for allowed tools
- `GET /agents/{agent_name}/system-prompt` — get the system prompt for an agent
- `POST /run?task=...` — run a task and return final result
- `GET /events?task=...` — SSE stream of MCP events
- `POST /cancel/{request_id}` — cancel a running request
- `POST /events/{request_id}/append` — append message to an existing conversation
- **Session Management Endpoints**:
  - `POST /api/sessions` — create a new conversation session
  - `GET /api/sessions` — list all sessions for current user (or anonymous)
  - `GET /api/sessions/{id}` — get session details with full message history
  - `PATCH /api/sessions/{id}` — update session metadata (rename, tags)
  - `DELETE /api/sessions/{id}` — delete a session (with optional backup)
  - `POST /api/sessions/{id}/restore` — load session into active conversation
- `GET /events` — Unified SSE stream for all events (start, thinking, status, tool_events, final, end)
- `GET /status/meta` — status stream metadata and statistics
- `POST /status/publish-test` — publish a test status event
- `GET /` — web UI home page
- `GET /status` — web UI status page (deprecated - now integrated in main UI)
- **MCP Server Mode Endpoints** (when `server_mode.enabled: true` in `config/mcp_server_mode.yaml`):
  - `POST /mcp` — Main MCP JSON-RPC 2.0 endpoint for remote MCP clients
  - `GET /mcp/sse` — SSE stream for server-initiated messages (future)
  - `GET /mcp/server-info` — MCP server information and statistics
- **Authentication Endpoints** (when auth enabled):
  - `POST /auth/login` — login and receive JWT token
  - `POST /auth/logout` — logout (client-side token disposal)
  - `GET /auth/me` — get current user information
  - `PATCH /auth/me` — update current user profile (email, name, password)
  - `POST /auth/api-key` — generate API key for current user
  - `DELETE /auth/api-key` — revoke API key
- **Admin Endpoints** (admin-only, when auth enabled):
  - `GET /admin/users` — list all users
  - `POST /admin/users` — create new user
  - `GET /admin/users/{id}` — get user by ID
  - `PATCH /admin/users/{id}` — update user
  - `DELETE /admin/users/{id}` — delete user
  - `POST /admin/users/{id}/activate` — activate user
  - `POST /admin/users/{id}/deactivate` — deactivate user
- **Menu API** (for dynamic UI menus):
  - `GET /api/menu-definitions` — get available menu definitions
  - `GET /api/menu-items` — get menu items (filtered by user role)

## How to debug
1. Use two terminals: one for the API, one for running tests / CLI commands. Do not run a long-lived server and tests in the same terminal.
2. Activate the venv in both terminals (see Quickstart).
3. Start the API in terminal A and leave it running.
4. Reproduce issues or run tests in terminal B.

Using VS Code launch configurations (recommended)
- Ensure VS Code uses the project venv: open the Command Palette -> `Python: Select Interpreter` and pick `.venv/Scripts/python.exe`.
- Open the Run and Debug view (Ctrl+Shift+D) and choose one of the launch configurations:
  - `Launch Agent API (module)` — starts the API module under the debugger.
  - `Agent CLI (module) — with args` — prompts for CLI args (for example: `run "What is the time in Nitra/Slovakia?"`).
  - `Debug pytest (module) — run single test or folder` — prompts for pytest target (for example `tests/test_cli.py::test_case` or `tests/`).
- When prompted for inputs, enter the desired args or pytest target and start debugging. Breakpoints will bind to your source code.

Tips:
- To run a single test with an interactive debugger (pytest + pdb fallback):

```bash
python -m pytest tests/test_example.py::test_case -q -s --maxfail=1 --pdb
```

- Increase logging for troubleshooting: edit `config/agent.yaml` and set `logging.level: DEBUG`. Logs are written to `logs/` (for example `logs/agent.log`, `logs/cli.log`, `logs/api.log`).
- If Windows PowerShell blocks activation, prefer Git Bash or CMD to avoid ExecutionPolicy issues.

## Configuration
Primary manifest: `config/config.yaml`. The manifest includes specialized config files via `includes:` for better organization:

**Config Structure (Epic 0044):**
- `config/config.yaml` - Main manifest with includes, network, auth, logging settings
- `config/llm.yaml` - LLM provider settings and profiles
- `config/agents.yaml` - Configuration-based agents (Epic 0043) - define custom agents without code
- `config/plugins.yaml` - Local MCP plugin servers configuration
- `config/mcp_servers.yaml` - External MCP servers (remote tool providers)
- `config/mcp_server_mode.yaml` - MCP Server Mode settings (expose AgentSystem as MCP server)

**Key Configuration Sections:**

### Main Configuration (`config/config.yaml`)

```yaml
# Main manifest with includes
includes:
  - llm.yaml           # LLM providers and profiles
  - agents.yaml        # Config-based agents (no code required!)
  - plugins.yaml       # Local plugin servers
  - mcp_servers.yaml   # External MCP servers
  - mcp_server_mode.yaml  # Server mode settings

# Network settings
network:
  ssl_verify: false
  host: 127.0.0.1
  port: 8000

# Default agent for CLI and API
default_agent: basic_agent

# Multi-user authentication (optional)
auth:
  enabled: false  # Set to true to enable authentication
  secret_key: "your-secret-key-min-32-chars"
  access_token_expire_minutes: 30

# Logging configuration
logging:
  enabled: true
  level: INFO  # DEBUG, INFO, WARNING, ERROR
  file: logs/agent.log
  file_cli: logs/cli.log
  file_api: logs/api.log
```

### Configuration-Based Agents (`config/agents.yaml`)

Define custom agents without writing code - see [Configuration-Based Agents Guide](docs/config_based_agents.md):

```yaml
agents:
  financial_analyst:
    enabled: true
    description: "Financial data analyst with market research tools"
    base_type: basic_agent
    agent_config:
      llm_profile: turbo
      max_steps: 20
      system_prompt: "You are a financial analyst..."
      tools:
        allowed:
          - "yahoo_finance/*"
          - "web_scraper/*"
```

### Plugin Configuration (`config/plugins.yaml`)

```yaml
plugins:
  # Plugin discovery
  plugin_dirs:
    - src/plugins
  
  # Individual plugin servers
  servers:
    datetime:
      type: datetime
      enabled: true
      description: "Date and time operations"
    
    web_scraper:
      type: web_scraper
      enabled: true
      config:
        max_content_length: 100000
```

### External MCP Servers (`config/mcp_servers.yaml`)

```yaml
external_servers:
  remote_servers:
    weather_api:
      url: "https://api.example.com/mcp"
      transport_type: "http"
      enabled: true
      auth:
        type: "api_key"
        api_key: "${WEATHER_API_KEY}"
```

### MCP Server Mode (`config/mcp_server_mode.yaml`)

Expose AgentSystem as an MCP server:

```yaml
server_mode:
  enabled: true
  endpoint: "/mcp"
  expose_plugins:
    - "*"  # All plugins, or list specific ones
  authentication:
    required: true
    methods: [jwt, api_key]
  rate_limit:
    enabled: true
    requests_per_minute: 60
```

### Context Window Management

AgentSystem includes an intelligent context window management system that automatically handles token limits, provides warnings, and implements smart summarization strategies. This prevents context overflow and maintains conversation continuity.

Key features:
- **Multi-level warnings** at 70%, 85%, and 95% of context window
- **Four management strategies**: TRUNCATE_OLDEST, SUMMARIZE_OLDEST, SLIDING_WINDOW, SMART_COMPRESSION
- **Intelligent summarization** using LLM to preserve important context
- **Configurable parameters** for summarization length and tool result previews
- **Real-time status events** for monitoring and feedback

For detailed configuration options, strategy explanations, and tuning guidance, see `docs/context_management.md`.

### Include Pattern and Managed Files

The `includes:` field allows you to split your configuration across multiple YAML files for better organization and maintainability.

**Config File Organization:**
- **`config.yaml`** - Main manifest, includes other configs, network/auth/logging settings
- **`llm.yaml`** - LLM provider API keys, model configurations, and named profiles
- **`agents.yaml`** - Configuration-based agent definitions (no code required!)
- **`plugins.yaml`** - Local MCP plugin servers (plugin_dirs, server configs)
- **`mcp_servers.yaml`** - External MCP servers (remote tool providers)
- **`mcp_server_mode.yaml`** - MCP server mode settings (expose AgentSystem as MCP server)

**How Includes Work:**
- Files listed in `includes:` are loaded and merged into the main configuration
- Settings in included files can reference environment variables: `${ENV_VAR}`
- CLI commands that modify configuration write changes to the appropriate included file
- The main `config.yaml` remains clean and focused on core settings

**Example Structure:**
```yaml
# config/config.yaml
includes:
  - llm.yaml
  - agents.yaml
  - plugins.yaml
  - mcp_servers.yaml
  - mcp_server_mode.yaml

# Settings here apply to core system behavior
network:
  host: 127.0.0.1
  port: 8000
```

### Plugin Directory Overrides

Plugin discovery can be customized via configuration or environment variables:

**Configuration (Recommended):**
```yaml
# config/plugins.yaml
plugins:
  plugin_dirs:
    - src/plugins
    - /path/to/custom/plugins
```

**Environment Variables:**
- `AGENT_PLUGIN_DIR`: Single directory to search for plugins (overrides config)
- `AGENT_PLUGIN_DIRS`: Comma-separated list of directories (overrides config)
- If not set, uses `plugins.plugin_dirs` from config, defaulting to `src/plugins/`

**Discovery Order:**
1. `AGENT_PLUGIN_DIRS` environment variable (if set)
2. `AGENT_PLUGIN_DIR` environment variable (if set)
3. `plugins.plugin_dirs` in config
4. Default: `src/plugins/`

### Selecting the Entry Agent (Dynamic)

You can choose which agent acts as the default entry point for CLI and API by setting `default_agent` in the main config:

```yaml
# config/config.yaml
default_agent: basic_agent
```

**Behavior:**
* The named agent must be defined in `config/agents.yaml` or available as a plugin
* Used as default for `agent-cli run "task"` when no `--agent` specified
* Used as default for API `/run` endpoint when no agent parameter provided
* Replaces legacy `MainAgent` and `cli_agent` concepts

### Per-Agent Tool Allow / Deny Lists

Each agent has **zero tool access by default** unless explicitly granted through `tools.allowed` patterns. This secure-by-default approach prevents unauthorized tool access.

**Configuration Example:**

```yaml
# config/agents.yaml
agents:
  research_agent:
    enabled: true
    agent_config:
      tools:
        allowed:
          - "web_scraper/*"      # All tools from web_scraper plugin
          - "duckduckgo_search/*" # All search tools
          - "datetime.get_*"     # Only datetime.get_* tools
        blocked:
          - "web_scraper.admin_*" # Block admin tools

# Or in config/plugins.yaml for plugin-based agents
plugins:
  servers:
    basic_agent:
      type: basic_agent
      agent_config:
        tools:
          allowed:
            - "datetime/*"
            - "basic_operations/*"
```

**Pattern Matching Rules:**
* `plugin_name` or `plugin_name/*` — All tools from that plugin/server
* `plugin_name.function_name` — A single specific tool function
* `plugin_name.prefix_*` — All tools matching the prefix pattern
* `external_server/*` — All tools from an external MCP server
* `*` — Allow everything (⚠️ only for testing; tighten in production)
* `tools.blocked` is applied **after** `tools.allowed` to subtract specific matches

**Diagnostics Endpoints:**
* `GET /agents` — List all registered agents
* `GET /agents/{name}/allowed-tools` — Show effective allowed tools (may be empty if deny-all)
* `GET /agents/{name}/allowed-tools/debug` — Detailed pattern matching diagnostics

**Migration Strategy:**
1. Start with `tools.allowed: ["*"]` while auditing actual tool usage
2. Review logs to see which tools are actually called
3. Narrow to specific plugins/functions: `["web_scraper/*", "datetime/*"]`
4. Add `tools.blocked` for carve-outs (experimental/unsafe tools)
5. Use `/allowed-tools/debug` endpoint to validate pattern behavior

## Plugins
Plugins live under `plugins/<name>/` and should expose a package-style layout with `plugin.py` and optional `plugin.yaml` for metadata. The loader also supports legacy single-file plugins.

Important: plugins are loaded in-memory under the `plugins.<name>` namespace to avoid collisions with stdlib modules (for example `datetime`).

See `docs/plugin_authoring.md` and `plugins/example` for examples.

Plugin-Management with:

```bash
agent-cli plugins list|info|enable|disable|search|status — manage plugins
```

## MCP (Model Context Protocol) Integration

AgentSystem provides comprehensive MCP support for both consuming external MCP servers and exposing local functionality as MCP endpoints.

### MCP CLI Commands

Manage external MCP server connections:

```bash
# List all configured MCP servers
agent-cli mcp list [--format json|table]

# Connect to a specific server
agent-cli mcp connect <server_name>

# Disconnect from a server
agent-cli mcp disconnect <server_name>

# Check server status and connection details
agent-cli mcp status [server_name]

# Test server connectivity and functionality
agent-cli mcp test <server_name>

# Manage server features
agent-cli mcp feature list <server_name>
agent-cli mcp feature <server_name> <feature> on|off
```

### HTTP Streaming Transport

The `HTTPStreamingTransport` (formerly SmitheryHTTPTransport) provides efficient MCP communication over HTTP with Server-Sent Events (SSE):

- **Real-time status streaming**: Status events are pushed to clients via SSE
- **Base64 config encoding**: Secure configuration transmission
- **Session management**: Persistent sessions with unique IDs
- **Error handling**: Robust error recovery and connection management

Transport types supported:
- `http`: Standard HTTP transport for basic MCP communication
- `smithery`: Legacy alias for HTTP streaming transport (deprecated)

### Configuration

MCP settings are configured in separate YAML files within the `config/` directory. See `docs/mcp_configuration.md` for detailed configuration options including:

**Configuration Files:**
- `config/mcp_servers.yaml` - External MCP server definitions
- `config/mcp_server_mode.yaml` - MCP Server Mode settings (expose AgentSystem as MCP server)
- `config/plugins.yaml` - Local plugin servers that can be exposed via MCP

**Configuration Options:**
- External server definitions (URL, transport, authentication)
- Authentication methods (API key, Bearer token, Basic auth)
- Transport settings and timeouts
- Feature filtering and security options
- SSL verification and retry policies

Example external server configuration:

```yaml
# config/mcp_servers.yaml
external_servers:
  remote_servers:
    weather_service:
      url: "https://api.weather.com/mcp"
      transport_type: "http"
      enabled: true
      description: "Weather data service"
      auth:
        type: "api_key"
        api_key: "${WEATHER_API_KEY}"
```

## Multi-User Authentication (Optional)

AgentSystem supports multi-user authentication and authorization, allowing multiple users to access the API with role-based access control. This feature is **disabled by default** and must be explicitly enabled in configuration.

### Quick Start

1. **Enable authentication** in `config/config.yaml`:

```yaml
auth:
  enabled: true
  secret_key: "your-secret-key-here-CHANGE-IN-PRODUCTION-min-32-chars"
  # Generate with: openssl rand -hex 32
```

2. **Start the API** — the system automatically creates a default admin user on first startup:

```bash
agent-cli run-api
```

3. **Login and get a token**:

```bash
curl -X POST http://127.0.0.1:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "CHANGE_THIS_PASSWORD"}'
```

4. **Use the token** in subsequent requests:

```bash
curl http://127.0.0.1:8000/auth/me \
  -H "Authorization: Bearer <your_token_here>"
```

### Features

- **User Management**: Create, update, delete users via CLI or API
- **Role-Based Access**: ADMIN, USER, and GUEST roles with different permissions
- **Multiple Auth Methods**:
  - JWT tokens (30-minute expiration, configurable)
  - API keys for long-lived access
- **Security Features**:
  - bcrypt password hashing
  - Rate limiting (60 req/min per IP)
  - Security headers (HSTS, CSP, X-Frame-Options, etc.)
  - CORS configuration
- **CLI Commands**:
  ```bash
  agent-cli users list            # List all users
  agent-cli users create          # Create new user (prompts for password)
  agent-cli users delete          # Delete a user
  agent-cli users update          # Update user details
  agent-cli users generate-api-key  # Generate API key for a user
  ```

### API Endpoints

- `POST /auth/register` — Register a new user (admin-only when enabled)
- `POST /auth/login` — Login and receive JWT token
- `POST /auth/logout` — Logout (client-side token disposal)
- `GET /auth/me` — Get current user information
- `PATCH /auth/me` — Update current user profile (email, name, password)
- `POST /auth/api-key` — Generate API key for current user
- `DELETE /auth/api-key` — Revoke API key
- `GET /admin/users` — List all users (admin-only)
- `GET /admin/users/{id}` — Get user by ID (admin-only)
- `POST /admin/users` — Create user (admin-only)
- `PATCH /admin/users/{id}` — Update user (admin-only)
- `DELETE /admin/users/{id}` — Delete user (admin-only)
- `POST /admin/users/{id}/activate` — Activate user (admin-only)
- `POST /admin/users/{id}/deactivate` — Deactivate user (admin-only)

### Documentation

For complete setup instructions, configuration options, API reference, security best practices, and migration guide, see:

**[Multi-User Authentication](docs/multi_user_authentication.md)** — Complete authentication and authorization guide

### Backward Compatibility

When `auth.enabled: false` (default):
- All endpoints work without authentication
- No user isolation or access controls
- Single-user mode (existing behavior)

When `auth.enabled: true`:
- Authentication required for most endpoints
- Admin-only endpoints restricted to ADMIN role
- Rate limiting and security headers automatically activated
- Per-user session isolation (each user has their own conversations)
- Anonymous users can also create and manage sessions (stored under "anonymous" user_id)

## Session Management

AgentSystem provides persistent, multi-user session management:

- **Session Storage**: Sessions stored as JSON files in `data/sessions/{user_id}/{session_id}.json`
- **User Isolation**: Sessions are scoped per user (authenticated users by username, anonymous as "anonymous")
- **Auto-save**: Every message automatically saves to session file
- **Web UI**: Sidebar with session list, create/rename/delete operations, session restore on page reload
- **CLI Support**: `agent-cli` and `agent-run` support `--session`, `--list-sessions`, `--session-user` options

See `docs/session_management.md` for detailed documentation.

## MCP Server Mode

AgentSystem can operate as an MCP server, exposing activated plugins as remote tools to MCP clients.

### Enabling MCP Server Mode

Edit `config/mcp_server_mode.yaml`:

```yaml
server_mode:
  enabled: true
  endpoint: "/mcp"
  
  # Plugin exposure
  expose_plugins:
    - "*"  # Expose all enabled plugins, or list specific ones: ["datetime", "web_scraper"]
  
  # Authentication
  authentication:
    required: true
    methods:
      - jwt
      - api_key
  
  # Rate limiting
  rate_limit:
    enabled: true
    requests_per_minute: 60
    requests_per_hour: 1000
    burst_size: 10
  
  # Session management
  session_ttl: 3600  # Session timeout in seconds (1 hour)
```

### Using MCP Server Mode

Once enabled, AgentSystem exposes the following MCP endpoints:

- **`POST /mcp`** — Main JSON-RPC 2.0 endpoint for MCP clients
- **`GET /mcp/sse`** — SSE stream for server-initiated messages (future)
- **`GET /mcp/server-info`** — Server metadata and statistics

### MCP Client Example

Connect to AgentSystem from an MCP client using HTTP Streamable Transport:

```python
from mcp.client import Client
from agent_system.mcp.streaming_transport import HTTPStreamingTransport

# Create transport
transport = HTTPStreamingTransport(
    endpoint="http://127.0.0.1:8000/mcp",
    headers={"Authorization": "Bearer YOUR_JWT_TOKEN"}
)

# Initialize client
async with Client(server_name="AgentSystem") as client:
    # Connect
    await client.connect(transport)
    
    # Initialize session
    await client.initialize(client_info={"name": "MyClient", "version": "1.0"})
    
    # List available tools
    tools = await client.list_tools()
    for tool in tools.tools:
        print(f"Tool: {tool.name} - {tool.description}")
    
    # Call a tool
    result = await client.call_tool("tool_name", {"param": "value"})
    print(result)
```

### Authentication

MCP server mode supports two authentication methods:

1. **JWT Token**: Pass in `Authorization: Bearer <token>` header
2. **API Key**: Pass in `X-API-Key: <api_key>` header

Obtain tokens via the authentication endpoints (see Authentication section).

### Session Management

Each MCP client connection gets a unique session ID via the `Mcp-Session-Id` header. Sessions:

- Track rate limits per client
- Expire after `session_ttl_seconds` (default: 3600s)
- Are automatically cleaned up when expired

### Rate Limiting

MCP server mode implements token bucket rate limiting per session:

- **requests_per_minute**: Maximum requests per minute (default: 60)
- **requests_per_hour**: Maximum requests per hour (default: 1000)
- **burst_size**: Maximum burst requests (default: 10)

When rate limits are exceeded, the server returns a JSON-RPC error:

```json
{
  "jsonrpc": "2.0",
  "error": {
    "code": -32000,
    "message": "Rate limit exceeded"
  },
  "id": null
}
```

For more details, see [MCP Configuration Documentation](docs/mcp_configuration.md).

## Development (with AI)
- Follow instructions and guidlines. For repository rules: venv activation, testing, and commit guidance.
- Run tests with `pytest -q` or `python -m pytest -q` and do not leave failing tests.
- To install dev dependencies (with venv activated):

```bash
pip install -e '.[dev]'
```

- When changing behavior, update `README.md` and tests accordingly.
- Maintain `backlog.md`

## VS Code tasks (Git Bash)

If you use Git Bash as the VS Code integrated terminal, the included `.vscode/tasks.json` is configured to run the project's checks using the venv Python and bash as the task shell. Example: open the Command Palette -> `Tasks: Run Task` -> choose `Python: Run all tests (venv)`.

If your VS Code uses Git Bash, the task runner will execute commands like:

```bash
# runs pytest via the repository venv
.venv/Scripts/python.exe -m pytest -q
```

If you prefer a different terminal, adjust the `options.shell.executable` in `.vscode/tasks.json`.

## Comprehensive guides are available in the `docs/` directory:

- **[CLI Command Reference](docs/cli_reference.md)** — Complete guide to all agent-cli commands and options
- **[Configuration-Based Agents](docs/config_based_agents.md)** — Create agents without writing code
- **[Vision Support](docs/vision_support.md)** — Complete guide to multimodal image input via WebUI and API
- **[MCP Configuration](docs/mcp_configuration.md)** — External MCP server setup and configuration
- **[HTTP Streaming Transport](docs/http_streaming_transport.md)** — SSE-based MCP communication details
- **[Plugin Authoring](docs/plugin_authoring.md)** — Create custom plugins and tools
- **[Plugin Caching](docs/plugin_caching.md)** — Optimize plugin loading with smart caching
- **[Plugin Web API Design](docs/plugin_web_api_design.md)** — Design principles for plugin APIs
- **[Context Management](docs/context_management.md)** — Token budget and context window strategies
- **[Status Design](docs/status_design.md)** — Real-time status streaming architecture
- **[Backlog Tool](docs/backlog_tool.md)** — Backlog management CLI reference
- **[Multi-User Authentication](docs/multi_user_authentication.md)** — Security and user management

## Maintenance notes

- Keep `backlog.md` updated for project-relevant changes (new tasks, epics, decisions). The repository includes a conservative `backlog` CLI that supports dry-run and write modes; prefer dry-run first and use `--write` to persist.
- Keep `backlog.md` updated for project-relevant changes (new tasks, epics, decisions). The repository includes a conservative `backlog` CLI that supports dry-run and write modes; prefer dry-run first and use `--write` to persist.

Dry-run vs write semantics (backlog CLI):

- Dry-run (default): most `backlog` subcommands (for example `add-task`, `add-epic`, `move-task`, `update-status`, `fix-format`) show what would change without modifying files. This mode is safe and suitable for CI / automated checks.
- Write (`--write`): when provided, the CLI will perform an atomic write to the target backlog file. Before the write, a timestamped backup is created in a `.backups` directory adjacent to the backlog file. When using `--write`, some arguments that are optional for dry-run (for example `--epic` on `add-task`) become required; missing required information for a write will produce a non-zero exit code and an explanatory error message.
- Interactive (`--interactive`): available for `show` and `edit` commands. For `show`, prompts to select items from a list if no IDs provided. For `edit`, prompts for fields and values if no `--set` options given.

Always run subcommands in dry-run first to inspect changes, then re-run with `--write` to persist when you're confident.
- Record short "lessons learned" entries in `.prompts/lessons_learned.md` when a non-trivial architectural decision or incident occurs.

## Contributing
- Use `backlog.md` to track tasks and update it after finishing or documenting progress.
- Add tests for new or changed behavior (happy path + at least one edge case).
- Keep changes minimal and focused; prefer small, well-tested commits.

## Contact / Notes
- This repository includes assistant prompt templates in `.prompts/` (developer rules and project objectives). Load `developer_rules.md` and `project_objectives.md` first in any assistant session, then `master_system_prompt.md`.

## License
