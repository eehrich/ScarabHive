# Installation and Configuration Guide

Complete setup guide for ScarabHive - from installation to production deployment.

## Table of Contents

- [System Requirements](#system-requirements)
- [Installation](#installation)
  - [Quick Install](#quick-install)
  - [Development Install](#development-install)
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
- **Disk Space**: ~500MB for base installation + space for session storage

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
agent-cli --version
pytest -q  # Run tests
```

### Development Install

For development with additional tools (linting, type checking, testing utilities):

```bash
# After basic installation above
pip install -e '.[dev]'

# Run linting and type checking
ruff check src --fix
mypy src --show-traceback
```

---

## Configuration

ScarabHive uses a hierarchical YAML configuration system in the `config/` directory.

### Configuration Structure

```
config/
├── config.yaml              # Main config with includes
├── llm.yaml                 # LLM provider settings
├── plugins.yaml             # Plugin configuration
├── mcp_servers.yaml         # External MCP servers
├── mcp_server_mode.yaml     # MCP server mode settings
└── agents/                  # Config-based agents (YAML files)
    ├── meta_agent.yaml
    ├── coding_agent.yaml
    └── web_research_agent.yaml
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

Reference model names in agent configuration:
```yaml
plugins:
  servers:
    my_agent:
      agent_config:
        llm_profile: gpt-4-turbo  # or claude-sonnet, gemini-pro, etc.
```

**Important**: The `${ENV_VAR}` syntax does NOT work in `llm.yaml`. Instead:
- **Recommended**: Omit `api_key` field → automatic `os.getenv()` fallback
- **Alternative**: Hardcode the API key directly (not recommended for production)

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
    
    terminal:
      type: terminal
      enabled: true
      config:
        timeout: 30
        max_output_length: 10000
        security:
          enabled: true
          blacklist:
            - "rm -rf"
            - "format"
    
    web_scraper:
      type: web_scraper
      enabled: true
      config:
        max_content_length: 100000
        timeout: 30
        user_agent: "ScarabHive/1.0"
    
    ssh_control:
      type: ssh_control
      enabled: false  # Disabled by default for security
      config:
        timeout: 30
        max_hosts: 10
```

### Agent Configuration

Create custom agents in `config/agents/*.yaml` without writing code:

**Example: `config/agents/research_agent.yaml`**
```yaml
plugins:
  servers:
    research_agent:
      type: basic_agent
      enabled: true
      description: "Web research specialist"
      
      agent_config:
        llm_profile: claude-sonnet
        max_steps: 25
        system_prompt: |
          You are a web research specialist. Use search and scraping tools
          to find comprehensive information on any topic.
        
        tools:
          allowed:
            - "web_scraper/*"
            - "duckduckgo_search/*"
            - "sequential_thinking/*"
          blocked:
            - "terminal/*"  # No terminal access
      
      metadata:
        visibility: "both"  # or "ui", "tool"
```

**Example: `config/agents/web_research_agent.yaml`**
```yaml
plugins:
  servers:
    web_research_agent:
      type: basic_agent
      enabled: true
      description: "Web research specialist with search and scraping capabilities"
      
      agent_config:
        llm_profile: claude-sonnet
        max_steps: 25
        
        tools:
          allowed:
            - "web_scraper/*"
            - "duckduckgo_search/*"
            - "sequential_thinking/*"
          blocked:
            - "terminal/*"  # No terminal access
      
      metadata:
        visibility: "both"  # or "ui", "tool"
```

See the existing agents in `config/agents/` for more examples.

---

## Running the System

### API Server

Start the FastAPI server for web UI and API access:

```bash
# Development mode (auto-reload)
agent-api

# Production mode (with Uvicorn workers)
uvicorn agent_system.app:build_app --factory --host 0.0.0.0 --port 8000 --workers 4

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

# With specific agent
agent-cli --agent research_agent "Research quantum computing"

# List available agents
agent-cli agents list

# View plugin status
agent-cli plugins status
```

### VS Code Tasks

Use predefined VS Code tasks (`.vscode/tasks.json`):

1. Open Command Palette (Ctrl+Shift+P)
2. Select "Tasks: Run Task"
3. Choose:
   - `ScarabHive: Run API` - Start API server
   - `ScarabHive: Run API with Debug Output` - Debug mode
   - `Python: Run all tests (venv)` - Run test suite
   - `Python: Ruff (check & fix)` - Lint code
   - `Python: Mypy (type check)` - Type checking

---

## Authentication Setup

Enable multi-user authentication (optional, disabled by default):

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

### 3. Create Admin User

On first startup with auth enabled, a default admin user is created:

```
Username: admin
Password: admin
```

**Important**: Change the default password immediately! The default credentials are set in `config/config.yaml` under `auth.default_admin_username` and `auth.default_admin_password`.

### 4. User Management

```bash
# List users
agent-cli users list

# Create new user
agent-cli users create --username alice --email alice@example.com --role USER

# Generate API key for automation
agent-cli users generate-api-key --username alice

# Update user
agent-cli users update --username alice --role ADMIN

# Delete user
agent-cli users delete --username alice
```

### 5. Using Authentication

**Web UI**: Login form appears at `/` - use username/password

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

Context management is configured per agent in their YAML files:

```yaml
plugins:
  servers:
    my_agent:
      agent_config:
        context_management:
          max_tokens: 100000
          warning_threshold: 0.7
          strategy: SMART_COMPRESSION  # or TRUNCATE_OLDEST, SUMMARIZE_OLDEST, SLIDING_WINDOW
          summarization:
            max_summary_length: 500
            preserve_system_prompt: true
```

See agent examples in `config/agents/` for reference.

### MCP Server Mode

Expose ScarabHive as an MCP server for external clients.

Edit `config/mcp_server_mode.yaml`:

```yaml
server_mode:
  enabled: true
  endpoint: "/mcp"
  
  # Expose specific plugins or all
  expose_plugins:
    - "*"  # All plugins
    # OR specific: ["datetime", "web_scraper"]
  
  authentication:
    required: true
    methods:
      - jwt
      - api_key
  
  rate_limit:
    enabled: true
    requests_per_minute: 60
    requests_per_hour: 1000
```

### External MCP Servers

Connect to remote MCP servers.

Edit `config/mcp_servers.yaml`:

```yaml
external_servers:
  remote_servers:
    weather_service:
      url: "https://api.weather.com/mcp"
      transport_type: "http"
      enabled: true
      auth:
        type: "api_key"
        api_key: "${WEATHER_API_KEY}"
      timeout: 30
```

### Session Storage

Session storage is handled automatically. Sessions are stored in `data/sessions/{user_id}/` by default. The storage path is managed by the system and doesn't require explicit configuration in `config.yaml`.

For custom storage locations, you can set environment variables or modify the session service initialization in the code.

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

Note: Log rotation and format are handled by the logging system. The config only specifies enable/level/file paths.

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
      api_key: "sk-..."  # Direct value, not ${ENV_VAR} syntax
```

#### 3. Plugin Loading Failures

**Error**: Plugin not found or failed to load

**Solution**:
```bash
# List available plugins
agent-cli plugins list

# Check plugin status
agent-cli plugins status

# Verify plugin directories in config/plugins.yaml
plugins:
  plugin_dirs:
    - src/plugins
```

#### 4. Port Already in Use

**Error**: `Address already in use: 0.0.0.0:8000`

**Solution**:
```bash
# Use different port
agent-api --port 8001

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
# CLI
agent-cli --debug "your query"

# API
AGENT_LOG_LEVEL=debug agent-api

# Or edit config/config.yaml
logging:
  level: DEBUG
```

### Testing

Run tests to verify installation:

```bash
# All tests
pytest -q

# Specific test suite
pytest tests/agent/ -v
pytest tests/plugins/ -v

# With coverage report
pytest --cov=agent_system --cov-report=html
```

### Getting Help

1. **Check logs**: `logs/agent.log`, `logs/api.log`, `logs/cli.log`
2. **Review documentation**: `docs/` directory
3. **Search backlog**: `backlog.md` for known issues
4. **Enable debug mode**: Set `AGENT_LOG_LEVEL=debug`
5. **Run tests**: `pytest -q` to verify system integrity

---

## Next Steps

After successful installation:

1. **Explore Plugins**: Browse `src/plugins/` for plugin READMEs and examples
2. **Create Custom Agent**: Add your agent definition as `config/agents/your_agent.yaml`
3. **Read Architecture Docs**: Understand the system design in `docs/`
4. **Try Examples**: Explore example workflows in plugin documentation
5. **Develop Plugins**: Follow `docs/plugin_authoring.md` to create custom tools

For production deployments, see deployment guides in `docs/` directory.
