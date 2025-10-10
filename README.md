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
  - [Script Interpreter](src/plugins/script_interpreter/README.md) — Sandboxed Python code execution
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
* **Multi-user authentication and authorization** with JWT tokens and API keys
  - Role-based access control (ADMIN, USER, GUEST)
  - Web-based user management interface
  - Flexible dropdown menu system for UI organization
  - CLI commands for user administration
* **Multimodal vision support** with image uploads via WebUI and API (see [Vision Support](docs/vision_support.md))
* Backlog & status management
* Context window management (summarization / truncation strategies)
* Streaming events API (SSE)
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
python -m agent_system.agent.interface_api
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
- `POST /sessions` — create a new conversation session
- `POST /sessions/{session_id}/append` — append message to a session
- `POST /sessions/{session_id}/force_optimize` — force context optimization for a session
- `POST /sessions/force_optimize` — force context optimization for all sessions
- `GET /status/stream` — SSE stream of status events
- `GET /status/meta` — status stream metadata and statistics
- `POST /status/publish-test` — publish a test status event
- `GET /` — web UI home page
- `GET /status` — web UI status page
- **Authentication Endpoints** (when auth enabled):
  - `POST /auth/login` — login and receive JWT token
  - `POST /auth/logout` — logout (client-side token disposal)
  - `GET /auth/me` — get current user information
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
Primary manifest: `config/agent.yaml`. The manifest may include other files (recommended for MCP-specific settings) via `includes:`. The CLI writes only to included managed files (for example `mcp.yaml`) when managing MCP settings.

Example minimal snippet:

```yaml
llm:
  provider: openai
  model: gpt-5-mini
  openai_api_key: ${OPENAI_API_KEY}
  context_window: 32768

includes:
  - mcp.yaml

network:
  ssl_verify: false
  host: 127.0.0.1
  port: 8000

logging:
  enabled: true
  level: DEBUG
  file: logs/agent.log
  file_cli: logs/cli.log
  file_api: logs/api.log
  as_json: false

prompts:
  system_template: config/prompts/system_prompt.yaml

max_steps: 50

# Context window management (optional)
context_management:
  context_window: 128000
  summarization_threshold: 102400
  strategy: "SUMMARIZE_OLDEST"
  max_summary_words: 500
  tool_result_preview_chars: 200
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

The `includes:` field allows you to split your configuration across multiple YAML files. This is useful for separating sensitive or frequently changing settings from the main manifest.

- The primary `config/agent.yaml` should contain core settings and list included files.
- CLI commands that modify configuration (e.g., enabling/disabling plugins) write changes only to the included files, preserving the master manifest.
- Example: `includes: - mcp.yaml` means MCP-related settings are in `config/mcp.yaml`.

### Plugin Directory Overrides

Plugin discovery can be customized via environment variables:

- `AGENT_PLUGIN_DIR`: Single directory to search for plugins (overrides config).
- `AGENT_PLUGIN_DIRS`: Comma-separated list of directories (overrides config).
- If not set, falls back to `mcp.plugin_dirs` in config, then repository `plugins/` directory.

### Selecting the Entry Agent (Dynamic)

You can choose which agent instance acts as the primary entry point for `/run` and `/events` by setting `entry_agent` in the top-level config (e.g. `config/agent.yaml`):

```yaml
entry_agent: basic_agent
```

Behavior:
* The named agent server must either be defined under `servers:` (plugin / custom) or will be instantiated as a core `Agent`.
* An alias `agent` is automatically registered for backward compatibility.
* Legacy `MainAgent` has been deprecated and replaced by this mechanism.
* Legacy `cli_agent` alias has been removed. Scripts that previously targeted `cli_agent` should now target the configured `entry_agent` (e.g. `basic_agent`). Remove any profile overrides keyed by `cli_agent` / `cli_agent_summarizer` from `agent_llm_profiles`.

### Per-Agent Tool Allow / Deny Lists

Each agent has zero tool access unless explicitly granted through `tools.allowed` patterns. (Secure by default — no silent broad access.)

Example:

```yaml
servers:
  basic_agent:
    type: basic_agent
    agent_config:
      tools:
        allowed:
          - "web_scraper/*"      # all tools from web_scraper plugin/server
          - "datetime.*"         # any datetime.* tool
        blocked:
          - "datetime.legacy_*"  # remove deprecated subset
```

Pattern rules:
* `plugin` or `plugin/*` — all tools from that plugin/server
* `plugin.function` — a single tool function
* `external_server/*` — all tools from an external MCP server
* `*` — allow everything (only for experimentation; tighten later)
* `tools.blocked` is applied after allow filtering to subtract matches

Diagnostics:
* `GET /agents` — list registered agents
* `GET /agents/{name}/allowed-tools` — effective allowed list (may be empty if deny-all)
* `GET /agents/{name}/allowed-tools/debug` — includes which patterns matched or were skipped

Migration tips:
1. Start with `tools.allowed: ["*"]` while auditing actual tool usage.
2. Narrow to specific plugins / functions.
3. Add `tools.blocked` for carve-outs (experimental / unsafe tools).
4. Use the `/debug` endpoint to validate pattern intent.

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

MCP settings are configured in `config/mcp.yaml`. See `docs/mcp_configuration.md` for detailed configuration options including:

- External server definitions
- Authentication methods (API key, Bearer token, Basic auth)
- Transport settings and timeouts
- Feature filtering and security options
- SSL verification and retry policies

Example configuration:

```yaml
mcp:
  enabled: true
  expose_local_server: true
  local_server_port: 8000

  servers:
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

**Note:** Full session isolation (per-user sessions) is planned for a future update. Current implementation provides authentication, user management, and access controls, but sessions are not yet isolated by user.

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

## Documentation

Comprehensive guides are available in the `docs/` directory:

- **[Vision Support](docs/vision_support.md)** — Complete guide to multimodal image input via WebUI and API
- **[MCP Configuration](docs/mcp_configuration.md)** — External MCP server setup and configuration
- **[HTTP Streaming Transport](docs/http_streaming_transport.md)** — SSE-based MCP communication details
- **[Plugin Authoring](docs/plugin_authoring.md)** — Create custom plugins and tools
- **[Plugin Caching](docs/plugin_caching.md)** — Optimize plugin loading with smart caching
- **[Plugin Web API Design](docs/plugin_web_api_design.md)** — Design principles for plugin APIs
- **[Context Management](docs/context_management.md)** — Token budget and context window strategies
- **[Status Design](docs/status_design.md)** — Real-time status streaming architecture
- **[Backlog Tool](docs/backlog_tool.md)** — Backlog management CLI reference

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
