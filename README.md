# AgentSystem — Flexible MCP-based Agent Framework (Python)

A lightweight, pluggable agent framework that composes LLMs and tool servers using the Model Context Protocol (MCP).

This README is a concise developer and user guide matching this repository layout.

## Quick links
- Code: `src/agent_system`
- Config: all yaml files in `config/`
- Docs: `docs/`
- Logfiles: `logs/` - AgentSystem logfiles. cli and api
- Plugins: `plugins/`
- Tests: `tests/` (pytest)
- Prompts for assistant sessions: `.prompts/` and `.github/`
- Helper Scripts: `scripts/`
- Ticketsystem/Backlog: `backlog.md`
- Python-ProjectSetup: `pyenvironment.toml`

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
- `GET /health` — health check
- `GET /config` — returns loaded configuration
- `POST /run?task=...` — run a task and return final result
- `GET /events?task=...` — SSE stream of MCP events

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
MIT

---
