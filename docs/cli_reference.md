# Agent CLI Reference

Complete command-line interface reference for AgentSystem.

## Installation

After installing the package (`pip install -e .`), the `agent-cli` command is available globally:

```bash
agent-cli --help
```

## Global Options

Available for all commands:

```bash
agent-cli [OPTIONS] COMMAND [ARGS]...

Options:
  --config PATH              Path to config file (default: config/config.yaml)
  -v, --verbose             Enable verbose logging
  --color {auto,always,never}  Color output mode (default: auto)
  --no-color                Disable colored output
  --show-mcp                Show MCP communication details
  --no-status               Disable status event output
  --raw                     Output raw JSON (machine-readable)
  -h, --help                Show help message
```

## Commands Overview

| Command | Description |
|---------|-------------|
| `run` | Execute an agent task (default command) |
| `chat` | Interactive chat with an agent (stays in the session) |
| `plugins` | Manage plugin servers |
| `mcp` | Manage external MCP servers |
| `users` | User management (requires auth) |
| `config-agents` | Manage configuration-based agents |

---

## `agent-cli run` - Execute Agent Tasks

Run an agent with a given prompt.

### Usage

```bash
agent-cli run [OPTIONS] [AGENT] PROMPT

# Default agent (from config)
agent-cli run "What is the weather in Berlin?"

# Specify agent by name
agent-cli run sysadmin_agent "Check system status"

# Use config-based agent
agent-cli run financial_analyst "Analyze AAPL stock"

# Override LLM profile
agent-cli run --llm turbo "Fast question about Python"

# Multimodal with image
agent-cli run --image screenshot.png "What's in this image?"
```

### Options

```bash
--agent TEXT               Agent name to use (plugin or config-based)
--llm TEXT                LLM profile to use (overrides agent's default)
--image PATH              Path to image file for vision models
--max-steps INTEGER       Maximum reasoning steps (overrides agent config)
--no-status               Disable status event streaming
--raw                     Output raw JSON instead of human-readable
```

### Examples

```bash
# Quick query with default agent
agent-cli run "What time is it in Tokyo?"

# Use specialized config agent
agent-cli run code_reviewer "Review this PR: https://github.com/..."

# Override settings for specific task
agent-cli run --agent web_research_agent --llm think --max-steps 30 \
  "Research the latest AI developments in 2025"

# Vision task with image
agent-cli run --image diagram.png "Explain this architecture diagram"

# Machine-readable output for scripting
agent-cli run --raw "List top 3 tech stocks" | jq '.result'
```

---

## `agent-cli chat` - Interactive Chat

REPL mode: stay in the session and keep talking to the agent, like `ollama run`.
The session is saved after every turn and can be resumed later (`--session`).

```bash
# Chat with the default agent
agent-cli chat

# Chat with a specific agent and LLM profile
agent-cli chat --agent amiga_coder --llm deepseek-chat

# Send a first message immediately
agent-cli chat "Wie ist der Stand?" --agent sysadmin_agent

# Resume an earlier session (/sessions and /session print this line for you)
agent-cli chat --session a1b2c3d4 --agent amiga_coder

# List sessions without entering the chat
agent-cli chat --list-sessions
```

Accepts the same `--agent`, `--llm`, `--llm-params`, `--session`,
`--session-user`, `--list-sessions` and `--vars` options as `run`, plus the
global `--color` and `--no-status`.

**In-chat commands:**

| Command | Effect |
|---------|--------|
| `/exit`, `/quit`, `/q`, `/bye` | End the chat (Ctrl-D / Ctrl-Z+Enter work too) |
| `/new` | Start a fresh session (the current one stays saved) |
| `/session` | Show the current session and the command that resumes it |
| `/sessions` | List the 10 most recent sessions |
| `/resume <id>` | Continue an earlier session without leaving the chat |
| `/history [n]` | Show the last `n` exchanges (default 6) with their tool traffic |
| `/last` | Tool calls and results of the last turn, in full |
| `/help`, `/h`, `/?` | List the commands |
| Ctrl-C | Cancel the **running turn**; twice at the prompt exits |

**Multi-line input.** A plain Enter sends the message, so pasting a block
needs one of:

```
"""
move.w  d0,d1
rts
"""
```

or a trailing backslash to continue on the next line. A message that has to
*start* with a command word is escaped with a doubled slash (`//new ...`);
anything else beginning with `/` that is not a known command — a path like
`/etc/nginx/nginx.conf`, for instance — is sent as an ordinary message.

**Display:** tool activity is rendered like the WebUI front panel -- one line
per operation that updates in place and collapses into its `✓`/`✗` end state,
instead of a chronological log. Thinking tokens appear as a live counter
(`✻ Thinking… (~120 tokens · 4s)`), intermediate agent narration between tool
calls is shown dimmed, and the final answer is rendered as markdown. Each turn
ends with a dim usage footer (`↑1.2k ↓830 · $0.0213 · 3m41s`) and the session
total is printed on exit. On a non-ANSI terminal (or when piped) the display
falls back to plain chronological lines.

---

## `agent-cli config-agents` - Configuration-Based Agents

Manage agents defined in `config/agents.yaml`.

### Subcommands

#### `list` - List All Config Agents

```bash
agent-cli config-agents list [--format {table,json}]

# Pretty table (default)
agent-cli config-agents list

# JSON output
agent-cli config-agents list --format json
```

**Example Output:**

```
Config-Based Agents:
╭────────────────────┬──────────┬────────┬──────────┬────────────────────────╮
│ NAME               │ LLM      │ STEPS  │ STATUS   │ DESCRIPTION            │
├────────────────────┼──────────┼────────┼──────────┼────────────────────────┤
│ financial_analyst  │ turbo    │ 20     │ Enabled  │ Financial analyst...   │
│ code_reviewer      │ deepseek │ 15     │ Enabled  │ Code review expert...  │
│ research_assistant │ think    │ 25     │ Disabled │ Research specialist... │
╰────────────────────┴──────────┴────────┴──────────┴────────────────────────╯
```

#### `show` - Display Agent Details

```bash
agent-cli config-agents show AGENT_NAME [--format {table,json}]

# View specific agent configuration
agent-cli config-agents show financial_analyst
```

**Example Output:**

```
Agent: financial_analyst
Status: Enabled
Description: Professional financial analyst for market analysis

Configuration:
  LLM Profile:    turbo
  Max Steps:      20
  System Prompt:  config/prompts/financial_analyst_prompt.md

Tools:
  Allowed:
    - yahoo_finance/*
    - web_scraper/*
    - duckduckgo_search/*
  Blocked:
    - ssh_control/*
    - script_interpreter/*

Context Management:
  Enabled:   true
  Strategy:  SUMMARIZE_OLDEST
  Preserve:  8 recent messages

Metadata:
  Author:   YourName
  Version:  1.0.0
  Tags:     finance, analysis
```

#### `validate` - Validate Agent Configuration

```bash
agent-cli config-agents validate [AGENT_NAME]

# Validate all agents
agent-cli config-agents validate

# Validate specific agent
agent-cli config-agents validate financial_analyst
```

**Example Output:**

```
Validating config agents...
✓ financial_analyst - OK
✓ code_reviewer - OK
✗ research_assistant - ERROR: Missing required field 'llm_profile'

Summary: 2 valid, 1 invalid
```

#### `enabled` - List Only Enabled Agents

```bash
agent-cli config-agents enabled

# JSON format
agent-cli config-agents enabled --format json
```

---

## `agent-cli plugins` - Plugin Management

Manage local plugin servers. The command displays plugin types and their configured instances.

### Subcommands

#### `list` - List Available Plugins

```bash
agent-cli plugins list [--format {table,json}]

# Table view (default) - shows plugin types and instances
agent-cli plugins list

# JSON for scripting
agent-cli plugins list --format json | jq '.[] | select(.enabled)'
```

**Example Output:**

```
| NAME                       | ENABLED | DESCRIPTION                                                              | VERSION |
|----------------------------|---------|--------------------------------------------------------------------------|---------|
| basic_agent                | YES     | Basic agent plugin providing agent execution capabilities as MCP tools   | 1.0.0   |
| ├─ basic_agent             | YES     |                                                                          |         |
| ├─ meta_agent              | YES     | Meta Agent for orchestrating other agents and managing complex tasks     |         |
| ├─ financial_analyst_agent | YES     | Professional financial analyst for stock market analysis, fundamental... |         |
| ├─ sysadmin_agent          | YES     | System Administrator who has ssh access to different servers             |         |
| web_research_agent         | YES     | Specialized web research agent combining DuckDuckGo search with web s... | 0.1.0   |
| ├─ web_research_agent      | YES     |                                                                          |         |
| ├─ meta_web_research_agent | YES     | Meta Web Research Agent for advanced web scraping and research tasks     |         |
| llm_router                 | YES     | Route requests to different LLM providers for specialized tasks or al... | 1.0.0   |
| web_scraper                | YES     | Fetch and extract readable text and structured data (tables/forms/li... | 0.1.0   |
```

**Features:**
- **Plugin types** shown as main rows with version numbers
- **Multiple instances** grouped under their type with tree characters (`├─`)
- **Individual enabled status** for each instance
- **Descriptions truncated** to 80 characters for readability
- **Full descriptions** available in JSON output

**JSON Output Structure:**
```json
[
  {
    "name": "basic_agent",
    "description": "Basic agent plugin providing agent execution capabilities...",
    "version": "1.0.0",
    "enabled": true,
    "instances": [
      {
        "instance_name": "basic_agent",
        "enabled": true,
        "description": ""
      },
      {
        "instance_name": "meta_agent",
        "enabled": true,
        "description": "Meta Agent for orchestrating other agents..."
      }
    ]
  }
]
```

#### `info` - Show Plugin Details

```bash
agent-cli plugins info PLUGIN_NAME [--raw]

# Human-readable info
agent-cli plugins info llm_router

# Raw metadata
agent-cli plugins info llm_router --raw
```

#### `search` - Search Plugins

```bash
agent-cli plugins search TERM

# Search by name or description
agent-cli plugins search "web"
```

#### `status` - Show Plugin Status

```bash
agent-cli plugins status

# JSON output with enabled/disabled status for all plugins
```

**Note:** To enable/disable plugins, modify the `plugins:` configuration section in any included config file. Set `enabled: true/false` under `plugins.servers.<plugin_name>`. The system automatically merges all plugin configurations from included YAML files.

---

## `agent-cli mcp` - External MCP Server Management

Manage connections to external MCP servers.

### Subcommands

#### `list` - List Configured MCP Servers

```bash
agent-cli mcp list [--format {table,json}]

# Show all configured servers
agent-cli mcp list
```

**Example Output:**

```
╭──────────────┬─────────────────────────┬──────────┬────────────╮
│ NAME         │ URL                     │ STATUS   │ TOOLS      │
├──────────────┼─────────────────────────┼──────────┼────────────┤
│ remote_ai    │ http://ai-server:3000   │ Connected│ 12 tools   │
│ data_service │ http://data.api.com     │ Offline  │ -          │
╰──────────────┴─────────────────────────┴──────────┴────────────╯
```

#### `connect` - Connect to Server

```bash
agent-cli mcp connect SERVER_NAME

# Establish connection
agent-cli mcp connect remote_ai
```

#### `disconnect` - Disconnect from Server

```bash
agent-cli mcp disconnect SERVER_NAME

# Close connection
agent-cli mcp disconnect remote_ai
```

#### `status` - Check Server Status

```bash
agent-cli mcp status [SERVER_NAME]

# All servers
agent-cli mcp status

# Specific server with details
agent-cli mcp status remote_ai
```

#### `test` - Test Server Connection

```bash
agent-cli mcp test SERVER_NAME

# Verify connectivity and list available tools
agent-cli mcp test remote_ai
```

**Example Output:**

```
Testing MCP server: remote_ai
✓ Connection successful
✓ Server version: 1.2.0
✓ Available tools: 12
  - analyze_sentiment
  - summarize_text
  - translate
  ...
```

---

## `agent-cli users` - User Management

Manage user accounts and authentication (requires `auth.enabled: true`).

### Subcommands

#### `list` - List All Users

```bash
agent-cli users list

# Requires admin privileges
```

**Example Output:**

```
╭────┬──────────┬───────────────────────┬────────┬────────╮
│ ID │ USERNAME │ EMAIL                 │ ROLE   │ ACTIVE │
├────┼──────────┼───────────────────────┼────────┼────────┤
│ 1  │ admin    │ admin@example.com     │ ADMIN  │ Yes    │
│ 2  │ john     │ john@example.com      │ USER   │ Yes    │
│ 3  │ guest    │ guest@example.com     │ GUEST  │ No     │
╰────┴──────────┴───────────────────────┴────────┴────────╯
```

#### `create` - Create New User

```bash
agent-cli users create USERNAME EMAIL [--role {ADMIN,USER,GUEST}]

# Interactive password prompt
agent-cli users create alice alice@example.com --role USER

# Programmatic (not recommended for security)
agent-cli users create alice alice@example.com --password secret123
```

#### `update` - Update User Details

```bash
agent-cli users update USER_ID [OPTIONS]

# Update email
agent-cli users update 2 --email newemail@example.com

# Change role
agent-cli users update 2 --role ADMIN

# Update multiple fields
agent-cli users update 2 --email new@example.com --role ADMIN
```

#### `delete` - Delete User

```bash
agent-cli users delete USER_ID [--yes]

# Prompts for confirmation
agent-cli users delete 3

# Skip confirmation
agent-cli users delete 3 --yes
```

#### `activate` / `deactivate` - Toggle User Status

```bash
# Activate inactive user
agent-cli users activate USER_ID

# Deactivate user (prevents login)
agent-cli users deactivate USER_ID
```

#### `generate-api-key` - Create API Key

```bash
agent-cli users generate-api-key USER_ID

# Generate long-lived API key for a user
agent-cli users generate-api-key 2
```

**Example Output:**

```
API Key generated for user 'john':
  Key: ak_1234567890abcdef1234567890abcdef
  
⚠ WARNING: Save this key securely. It cannot be retrieved again.
```

---

## Environment Variables

AgentSystem respects the following environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `AGENT_CONFIG_PATH` | Path to config file | `config/config.yaml` |
| `AGENT_LOG_LEVEL` | Logging level | `INFO` |
| `OPENAI_API_KEY` | OpenAI API key | - |
| `ANTHROPIC_API_KEY` | Anthropic API key | - |
| `DEEPSEEK_API_KEY` | DeepSeek API key | - |
| `NO_COLOR` | Disable color output | - |

---

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error (configuration, runtime) |
| 2 | Command-line argument error |
| 3 | Authentication/authorization error |
| 130 | Interrupted by user (Ctrl+C) |

---

## Configuration Files

CLI behavior can be customized through configuration files:

### Main Configuration

**File:** `config/config.yaml`

```yaml
# Entry agent (used when no --agent specified)
entry_agent: "default_agent"

# Default LLM profile
default_llm_profile: "normal"

# Logging
logging:
  level: INFO
  file_cli: logs/cli.log

# Network
network:
  host: 127.0.0.1
  port: 8000
```

### Config-Based Agents

**File:** `config/agents.yaml`

```yaml
agents:
  my_agent:
    enabled: true
    description: "My custom agent"
    base_type: "agent"
    agent_config:
      llm_profile: "turbo"
      max_steps: 20
      system_template: "config/prompts/my_agent.md"
      tools:
        allowed: ["*"]
```

See [Configuration-Based Agents Guide](config_based_agents.md) for complete reference.

### Plugin Configuration

**File:** `config/plugins.yaml`

```yaml
plugins:
  plugin_dirs:
    - "src/plugins"
  
  servers:
    llm_router:
      type: llm_router
      enabled: true
    
    web_scraper:
      type: web_scraper
      enabled: true
```

---

## Tips and Best Practices

### 1. Use Config Agents for Specialization

Instead of creating multiple prompt variations, use config-based agents:

```bash
# Bad: manual prompting
agent-cli run "Act as a financial analyst and analyze..."

# Good: dedicated config agent
agent-cli run financial_analyst "Analyze AAPL stock"
```

### 2. Override Settings Per Task

Use command-line options to adjust behavior without changing config:

```bash
# Quick task with faster model
agent-cli run --llm turbo "Quick summary of..."

# Complex task with more steps
agent-cli run --max-steps 50 "Comprehensive research on..."
```

### 3. Script with JSON Output

Use `--raw` for machine-readable output:

```bash
# Process results with jq
agent-cli run --raw "Top 5 tech stocks" | \
  jq -r '.result.summary' | \
  mail -s "Daily Report" user@example.com
```

### 4. Check Agent Capabilities

Before running a task, verify what tools an agent has access to:

```bash
# View agent configuration
agent-cli config-agents show financial_analyst

# List available plugins
agent-cli plugins list
```

### 5. Test MCP Connectivity

Before relying on external MCP servers, test them:

```bash
# Verify server is reachable
agent-cli mcp test remote_ai

# Check current status
agent-cli mcp status
```

---

## Troubleshooting

### Common Issues

#### 1. "Agent not found"

```bash
agent-cli run unknown_agent "task"
# Error: Agent 'unknown_agent' not found
```

**Solution:** List available agents:

```bash
# Check plugins
agent-cli plugins list

# Check config agents
agent-cli config-agents list
```

#### 2. "Configuration file not found"

```bash
agent-cli run "task"
# Error: Could not load config from config/config.yaml
```

**Solution:** Specify config path or create default:

```bash
# Use custom config
agent-cli --config /path/to/config.yaml run "task"

# Or create default config
cp config/config.yaml.example config/config.yaml
```

#### 3. "Permission denied" for user management

```bash
agent-cli users list
# Error: Insufficient permissions
```

**Solution:** Ensure you're logged in as admin or auth is disabled.

#### 4. Color output issues in scripts

If you're piping output and see ANSI codes:

```bash
# Disable colors explicitly
agent-cli --no-color run "task" > output.txt

# Or use environment variable
NO_COLOR=1 agent-cli run "task"
```

---

## Further Reading

- [Configuration-Based Agents](config_based_agents.md) - Deep dive into agent definitions
- [MCP Configuration](mcp_configuration.md) - External server setup
- [Plugin Authoring](plugin_authoring.md) - Create custom plugins
- [Authentication Guide](multi_user_authentication.md) - Security and user management
- [Context Management](context_management.md) - Token budget strategies

---

## Quick Reference Card

```bash
# Core Operations
agent-cli run "prompt"                          # Execute with default agent
agent-cli run agent_name "prompt"               # Execute with specific agent
agent-cli run --llm profile "prompt"            # Override LLM

# Config Agents
agent-cli config-agents list                    # List all config agents
agent-cli config-agents show NAME               # View details
agent-cli config-agents validate                # Check configuration

# Plugins
agent-cli plugins list                          # List plugins
agent-cli plugins info NAME                     # Plugin details

# MCP Servers
agent-cli mcp list                              # List MCP servers
agent-cli mcp test NAME                         # Test connection
agent-cli mcp status                            # Check all statuses

# Users (requires auth)
agent-cli users list                            # List users
agent-cli users create USER EMAIL               # Create user
agent-cli users generate-api-key ID             # Generate API key

# Debugging
agent-cli --verbose run "prompt"                # Detailed logs
agent-cli --show-mcp run "prompt"               # Show MCP communication
agent-cli --raw run "prompt"                    # JSON output
```
