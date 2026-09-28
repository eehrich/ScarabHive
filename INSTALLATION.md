# Installation and Configuration Guide

Complete setup guide for ScarabHive - from installation to production deployment.

## Table of Contents

- [System Requirements](#system-requirements)
- [Installation](#installation)
  - [Quick Install](#quick-install)
  - [Development Install](#development-install)
  - [Docker](#docker)
- [Configuration](#configuration)
  - [Configuration Structure](#configuration-structure)
  - [LLM Provider Setup](#llm-provider-setup)
  - [Plugin Configuration](#plugin-configuration)
  - [Agent Configuration](#agent-configuration)
- [Running the System](#running-the-system)
- [Authentication Setup](#authentication-setup)
- [Advanced Configuration](#advanced-configuration)
- [Troubleshooting](#troubleshooting)

---

## System Requirements

- **Python**: 3.11 or higher
- **Operating System**: Windows, Linux, or macOS
- **Shell**: Git Bash recommended on Windows, any bash-compatible shell on Linux/macOS
- **Memory**: Minimum 2GB RAM (4GB+ recommended for multi-agent workflows)
- **Disk Space**: several GB (the dependencies include PyTorch and embedding models) + space for session storage
- **Build tools on Linux/macOS**: the cairo headers for `pycairo` -- `sudo apt-get install libcairo2-dev pkg-config` (Debian/Ubuntu), `brew install cairo pkg-config` (macOS)

### Optional Dependencies

- **Git**: For development and version control

---

## Installation

### Quick Install

For basic usage with default configuration:

```bash
# Clone the repository
git clone https://github.com/eehrich/ScarabHive.git
cd ScarabHive

# Create virtual environment
python -m venv .venv

# Activate virtual environment
# Windows (Git Bash)
source .venv/Scripts/activate

# Linux/macOS
source .venv/bin/activate

# Install package
pip install -U pip
pip install -e .

# Verify installation
agent-cli --help
```

To run tests, read [CONTRIBUTING.md](CONTRIBUTING.md#tests) first: run the tests for what you
changed (the full suite takes 20+ minutes). pytest ends only the processes its own session
started, and orphans of test sessions that provably ended.

### Development Install

For development with additional tools (linting, type checking, testing utilities):

```bash
# After basic installation above
pip install -e '.[dev,test]'

# Run linting and type checking, as CI does
ruff check .
mypy src/agent_system src/plugins
```

Contributions: see [CONTRIBUTING.md](CONTRIBUTING.md) for the tests to run and the conventions.

### Docker

The image runs the API server (`agent-api`) with the configuration from your checkout.

```bash
# API keys go into config/secrets.env (template: config/secrets.env.example)
test -f config/secrets.env || cp config/secrets.env.example config/secrets.env

docker compose up -d --build
docker compose logs -f scarabhive
```

The web UI is then at `http://127.0.0.1:8000`, published on the loopback interface only.
`config/` is mounted read-only; sessions, `users.db`, plugin data, logs and downloaded models
live in named volumes. Build arguments (CPU/CUDA PyTorch, extra system packages, the container
user's ids) are described in [docker-compose.yml](docker-compose.yml) and the [Dockerfile](Dockerfile).

---

## Configuration

ScarabHive uses a hierarchical YAML configuration system in the `config/` directory.

### Configuration Structure

```
config/
├── config.yaml              # Main config with includes
├── llm.yaml                 # LLM provider settings
├── llm_openrouter.yaml      # OpenRouter models and profiles
├── plugins.yaml             # Plugin configuration
├── mcp_servers.yaml         # External MCP servers
├── secrets.env.example      # Template for secrets.env (API keys, loaded at startup)
└── agents/                  # Config-based agents (YAML files)
    ├── agents.yaml          # base agents (multi_turn_agent, skills_agent, chat_agent)
    ├── sysadmin_agent.yaml
    └── okf_agent.yaml
```

### LLM Provider Setup

Edit `config/llm.yaml` to configure your LLM provider(s). The config uses a `models` section with named model definitions:

#### OpenAI Example

```yaml
llm_system:
  models:
    gpt-4-turbo:
      provider: openai
      model: gpt-4-turbo-preview
      # api_key: omit to use OPENAI_API_KEY environment variable (recommended)
      # api_key: "sk-..."  # or hardcode (not recommended for production)
      context_window: 128000
      request_timeout: 180
      capabilities:
        tools: true
        function_calling: true
        streaming: true
        json_mode: true
```

Set environment variable:
```bash
export OPENAI_API_KEY="sk-..."
```

**Note**: When `api_key` is omitted, the system automatically uses `os.getenv("OPENAI_API_KEY")` as fallback.

#### Anthropic (Claude) Example

```yaml
llm_system:
  models:
    claude-sonnet:
      provider: anthropic
      model: claude-3-5-sonnet-20241022
      # api_key: omit to use ANTHROPIC_API_KEY environment variable (recommended)
      context_window: 200000
      request_timeout: 180
      capabilities:
        tools: true
        function_calling: true
        streaming: true
```

Set environment variable:
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

#### Google (Gemini) Example

```yaml
llm_system:
  models:
    gemini-pro:
      provider: gemini_sdk
      model: gemini-3-pro-preview
      # api_key: omit to use GEMINI_API_KEY or GOOGLE_API_KEY environment variable (recommended)
      context_window: 200000
      capabilities:
        tools: true
        image_input: true
        streaming: true
```

Set environment variable:
```bash
export GOOGLE_API_KEY="AIza..."
# or
export GEMINI_API_KEY="AIza..."
```

#### Using Models in Agents

Agents reference a **profile**, and a profile points at a model via `model_ref`:
```yaml
llm_system:
  profiles:
    gpt-4-turbo:
      model_ref: gpt-4-turbo   # key under llm_system.models

plugins:
  servers:
    my_agent:
      agent_config:
        llm_profile: gpt-4-turbo  # a name under llm_system.profiles
```

**API keys**: `${ENV_VAR}` placeholders are expanded in every config file, `llm.yaml` included (`api_key: ${OPENAI_API_KEY}`); an unset variable becomes empty and is named in a startup warning. Variables can also be put in `config/secrets.env` (template: `config/secrets.env.example`), which is loaded at startup without overriding the real environment. Omitting `api_key` falls back to the provider's environment variable as described above.

### Plugin Configuration

Edit `config/plugins.yaml` to enable/disable plugins and configure settings:

```yaml
plugins:
  # Plugin discovery directories
  plugin_dirs:
    - src/plugins
    - /custom/plugin/path  # Optional
  
  # Individual plugin servers
  servers:
    # Built-in plugins
    basic_operations:
      type: basic_operations
      enabled: true
      description: "Utility tools for testing"
    
    # Where settings go is per plugin: these three read them directly on the
    # entry, others (e.g. context_engineer) under a `config:` key
    terminal:
      type: terminal
      enabled: true
      security:
        blacklist:
          - "rm -rf /"
          - "mkfs"
      limits:
        max_output_size_kb: 60
        default_timeout_seconds: 300
    
    web_scraper:
      type: web_scraper
      enabled: true
      cache_ttl: 1800
      user_agent: "ScarabHive/1.0"
    
    ssh_control:
      type: ssh_control
      enabled: false  # false is the default; config/agents/sysadmin_agent.yaml ships an enabled entry
      defaults:  # applied to every entry under machines:
        connection_timeout: 10
        command_timeout: 300
```

### Agent Configuration

Create custom agents in `config/agents/*.yaml` without writing code:

**Example: the research agent, a config-only plugin** (`src/plugins/research/agents/research_agent.yaml`, picked up by the `../src/plugins*/*/agents/*.yaml` include)
```yaml
plugins:
  servers:
    research_agent:
      type: multi_turn_agent   # inherits the base agent from config/agents/agents.yaml
      enabled: true
      description: "Web research with cited sources"

      agent_config:
        llm_profile: [or-deepseek-flash, deepseek-chat]
        max_steps: 40
        system_template: "./prompts/research_agent.md"   # next to the YAML
        skills:
          always: ["web-research"]                       # from the plugin's skills/
        tools:
          allowed:                                       # "+" adds to the inherited list
            - "+tavily_search/*"
            - "+duckduckgo_search/*"
            - "+web_scraper/*"

      metadata:
        visibility: "both"  # or "ui", "tool"
```

See the existing agents in `config/agents/` for more examples.

---

## Running the System

### API Server

Start the FastAPI server for web UI and API access:

```bash
# Host/port from config/config.yaml (network.host/port, default 127.0.0.1:8000), HOST/PORT env override
agent-api

# Via uvicorn directly - single worker: run state (cancellation, mid-run messages, status streams) is per process
uvicorn agent_system.app:build_app --factory --host 127.0.0.1 --port 8000
# (bind 0.0.0.0 only after changing auth.secret_key and auth.default_admin_password)

# With specific log level
AGENT_LOG_LEVEL=debug agent-api
```

Access the web UI at `http://localhost:8000`

### CLI Mode

Use the command-line interface:

```bash
# Interactive chat
agent-cli chat

# Single query
agent-cli "What is the weather in Berlin?"

# With specific agent (run options follow the task)
agent-cli run "Research quantum computing" --agent research_agent

# Plugins, agents among them as instances
agent-cli plugins list
```

### VS Code Tasks

Use predefined VS Code tasks (`.vscode/tasks.json`):

1. Open Command Palette (Ctrl+Shift+P)
2. Select "Tasks: Run Task"
3. Choose:
   - `AgentSystem: Run API` - Start API server
   - `AgentSystem: Run API with Debug Output` - Debug mode
   - `Python: Run all tests (venv)` - The whole suite (20+ minutes; see [Testing](#testing))
   - `Python: Ruff (check & fix)` - Lint code
   - `Python: Mypy (type check)` - Type checking

---

## Authentication Setup

Enable multi-user authentication (optional; `auth.enabled` defaults to `false`, the shipped `config/config.yaml` sets it to `true`):

### 1. Enable Authentication

Edit `config/config.yaml`:

```yaml
auth:
  enabled: true
  secret_key: "${AUTH_SECRET_KEY}"  # Generate: openssl rand -hex 32
  algorithm: "HS256"
  access_token_expire_minutes: 30
  
  # Database settings
  database_path: "data/users.db"
  
  # CORS settings for web UI
  cors_enabled: true
  cors_origins:
    - "http://localhost:3000"
    - "http://localhost:8000"
  cors_credentials: true
```

### 2. Generate Secret Key

```bash
# Generate secure secret key
openssl rand -hex 32

# Set as environment variable
export AUTH_SECRET_KEY="your-generated-secret"
```

The server checks the key at startup: an empty one (for example an unset
`AUTH_SECRET_KEY`) or one shorter than 32 characters stops it; a published
key -- the shipped development key, the built-in default -- is logged as an
error. Set `auth.reject_default_secret_key: true` to refuse starting with one.

### 3. Create Admin User

On first startup with auth enabled (and no users in the database), a default admin user is created:

```
Username: admin
Password: admin123
```

**Important**: Change the default password immediately! The default credentials are set in `config/config.yaml` under `auth.default_admin_username` and `auth.default_admin_password`. Without `default_admin_password`, a random password is generated and printed in the startup log.

### 4. User Management

```bash
# List users
agent-cli users list

# Create new user (prompts for the password unless -p is given)
agent-cli users create alice alice@example.com --role user

# Generate API key for automation
agent-cli users generate-api-key alice

# Update user
agent-cli users update alice --role admin

# Delete user
agent-cli users delete alice
```

### 5. Using Authentication

**Web UI**: Opening `/` redirects to the login form at `/login` - use username/password

**API**: Include JWT token in requests:
```bash
# Login to get token
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "your-password"}'

# Use token in requests
curl http://localhost:8000/agents \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

**API Key**: For long-lived access:
```bash
curl http://localhost:8000/agents \
  -H "X-API-Key: YOUR_API_KEY"
```

---

## Advanced Configuration

### Context Management

Context management is done by the hook plugins `context_engineer` (layered compaction) and `context_summarizer` (LLM summaries), configured on their entries in `config/plugins.yaml`. There is no `agent_config.context_management` block — `agent_config` rejects unknown keys.

```yaml
plugins:
  servers:
    context_engineer:
      type: context_engineer
      enabled: true
      config:
        layer1_threshold: 140000  # start reversible compaction
        layer2_threshold: 170000  # start archiving old turns with summaries
        layer3_threshold: 200000  # start dropping old messages
        target_tokens: 70000      # target after compaction
    context_summarizer:
      type: context_summarizer
      enabled: true
      config:
        llm_profile: or-deepseek-flash
        summarization_trigger_percentage: 0.8
```

Per agent, hooks are switched on or off with `agent_config.hooks.overrides` (see `docs/plugin_hooks.md`).

### External MCP Servers

Connect to remote MCP servers.

Edit `config/mcp_servers.yaml`:

```yaml
external_servers:
  remote_servers:
    weather_service:
      url: "https://api.weather.com/mcp"
      transport: "http"  # streamable HTTP (default "streaming"); also "sse", "stdio"
      enabled: true
      auth:
        type: "api_key"
        api_key: "${WEATHER_API_KEY}"
      timeout: 30
```

### Session Storage

Session storage is handled automatically. Sessions are stored in `data/sessions/{user_id}/` by default. The storage path is managed by the system and doesn't require explicit configuration in `config.yaml`.

For a custom storage location, set the `AGENT_SESSION_STORAGE_PATH` environment variable.

### Logging Configuration

```yaml
logging:
  enabled: true
  level: INFO  # DEBUG, INFO, WARNING, ERROR, CRITICAL
  
  # File logging
  file: logs/agent.log
  file_cli: logs/cli.log
  file_api: logs/api.log
```

Note: Log format is handled by the logging system. Rotation is configured with `rotation_enabled`, `max_bytes` and `backup_count` (defaults: on, `10MB`, 5 backups).

---

## Troubleshooting

### Common Issues

#### 1. Import Errors

**Error**: `ModuleNotFoundError: No module named 'agent_system'`

**Solution**:
```bash
# Ensure venv is activated
source .venv/Scripts/activate  # Windows Git Bash
source .venv/bin/activate      # Linux/macOS

# Reinstall in editable mode
pip install -e .
```

#### 2. LLM API Errors

**Error**: `OpenAI API key not found`

**Solution**:
```bash
# Set environment variable (recommended)
export OPENAI_API_KEY="sk-..."

# Or hardcode in config/llm.yaml (not recommended)
llm_system:
  models:
    your_model:
      api_key: "sk-..."  # or ${OPENAI_API_KEY}
```

#### 3. Plugin Loading Failures

**Error**: Plugin not found or failed to load

**Solution**:
```bash
# List available plugins
agent-cli plugins list

# Details of one plugin, including ENABLED
agent-cli plugins info PLUGIN_NAME

# Verify plugin directories in config/plugins.yaml
plugins:
  plugin_dirs:
    - src/plugins
```

#### 4. Port Already in Use

**Error**: `[Errno 98] error while attempting to bind on address ('127.0.0.1', 8000): [errno 98] address already in use` (Errno 48 on macOS)

**Solution**:
```bash
# Use different port (or set network.port in config/config.yaml)
PORT=8001 agent-api

# Or kill existing process
lsof -ti:8000 | xargs kill -9  # Linux/macOS
# Windows: Use Task Manager or scripts/kill_project_python_processes.ps1
```

#### 5. Permission Errors

**Error**: Permission denied when accessing files

**Solution**:
```bash
# Ensure correct ownership
sudo chown -R $USER:$USER .

# Check directory permissions
chmod 755 data/sessions
```

### Debug Mode

Enable verbose logging:

```bash
# CLI (progress messages and tool call details)
agent-cli -v --show-tools "your query"

# API
AGENT_LOG_LEVEL=debug agent-api

# Or edit config/config.yaml
logging:
  level: DEBUG
```

### Testing

Run the tests for what you changed -- the full suite takes 20+ minutes; details, including
which processes pytest ends, in [CONTRIBUTING.md](CONTRIBUTING.md#tests):

```bash
# The tests need the [test] extra: pip install -e '.[test]'

# Specific test suite
pytest tests/agent/ -v
pytest tests/plugins/ -v

# With coverage report (pytest-cov comes with the [test] extra)
pytest tests/agent/ --cov=agent_system --cov-report=html
```

### Getting Help

1. **Check logs**: `logs/api.log` (agent-api), `logs/cli.log` (agent-cli); names set by `logging.file_api` / `file_cli` in `config/config.yaml`
2. **Review documentation**: `docs/` directory
3. **Enable debug mode**: Set `AGENT_LOG_LEVEL=debug`
4. **Run tests**: the suites for the area you changed (see [Testing](#testing))

---

## Next Steps

After successful installation:

1. **Explore Plugins**: Browse `src/plugins/` for plugin READMEs and examples
2. **Create Custom Agent**: Add your agent definition as `config/agents/your_agent.yaml`
3. **Read Architecture Docs**: Understand the system design in `docs/`
4. **Try Examples**: Explore example workflows in plugin documentation
5. **Develop Plugins**: Follow `docs/plugin_authoring.md` to create custom tools

For production deployments, see the deployment view in `docs/_arch_agent_system_architecture.md` (section 9).
