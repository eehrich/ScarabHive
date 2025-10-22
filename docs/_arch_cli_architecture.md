# Software Architecture Document: CLI Architecture

**Document Type:** Software Architecture Document (SAD)  
**Component:** Command-Line Interface (CLI)  
**Version:** 1.0  
**Last Updated:** 2025-01-15  
**Status:** Active

---

## Table of Contents

1. [Overview](#overview)
2. [Architectural Goals](#architectural-goals)
3. [Component Architecture](#component-architecture)
4. [Command Structure](#command-structure)
5. [CLI Tools](#cli-tools)
6. [Output Formatting](#output-formatting)
7. [Key Design Decisions](#key-design-decisions)
8. [Data Flow](#data-flow)
9. [Related Documents](#related-documents)

---

## 1. Overview

### 1.1 Purpose

The CLI provides command-line access to AgentSystem functionality:
- Quick agent execution (`agent-run`)
- Comprehensive system management (`agent-cli`)
- Plugin/server inspection and management
- Session management
- Configuration validation

### 1.2 Scope

This document covers:
- CLI entry points (`agent_cli.py`, `agent_run.py`)
- Command structure and argument parsing
- Output formatting (JSON, table, color)
- CLI utilities (`cli_utils/`)

### 1.3 CLI Tools

| Tool | Purpose | Use Case |
|------|---------|----------|
| **agent-run** | Quick agent execution | One-off questions, scripting |
| **agent-cli** | Full system management | Server mgmt, plugin inspection, admin tasks |

---

## 2. Architectural Goals

### 2.1 Design Principles

| Principle | Description | Priority |
|-----------|-------------|----------|
| **Simplicity** | Easy to use, minimal required arguments | High |
| **Scriptability** | JSON output for automation | High |
| **Discoverability** | Help text, command listing, examples | High |
| **Consistency** | Common patterns across commands | Medium |
| **Performance** | Fast startup, minimal overhead | Medium |

### 2.2 Quality Goals

- **Startup Time:** < 500ms (cold start)
- **UX:** Intuitive, minimal keystrokes
- **Compatibility:** Works on Windows, Linux, macOS
- **Error Handling:** Clear error messages

---

## 3. Component Architecture

### 3.1 CLI Structure

```
┌─────────────────────────────────────────────────────────────────┐
│                         CLI Layer                                │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌────────────────────┐         ┌────────────────────┐         │
│  │    agent-run       │         │    agent-cli       │         │
│  │  (agent_run.py)    │         │  (agent_cli.py)    │         │
│  │                    │         │                    │         │
│  │  - Quick exec      │         │  - Full commands   │         │
│  │  - Simple args     │         │  - Subcommands     │         │
│  │  - Streaming       │         │  - Admin ops       │         │
│  └────────────────────┘         └────────────────────┘         │
│           │                              │                       │
│           └──────────────┬───────────────┘                       │
│                          ▼                                       │
│  ┌───────────────────────────────────────────────────┐          │
│  │           CLI Utilities (cli_utils/)              │          │
│  ├───────────────────────────────────────────────────┤          │
│  │  - common.py (colors, formatting, hooks)          │          │
│  │  - agent_runner.py (shared agent creation)        │          │
│  │  - commands/ (hooks, status, sessions)            │          │
│  └───────────────────────────────────────────────────┘          │
│                          │                                       │
└──────────────────────────┼───────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Service Layer                                 │
│  ConfigService │ MCPService │ AgentService │ ToolService        │
└─────────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Domain Layer                                  │
│  Agent │ MCPRegistry │ Plugin System │ LLM Clients              │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 Core Components

#### 3.2.1 agent-run (`agent_run.py`)

**File:** `src/agent_system/agent_run.py`

**Purpose:** Lightweight CLI for quick agent execution

**Responsibilities:**
- Minimal argument parsing
- Agent initialization
- Request execution
- Output formatting

**Usage:**
```bash
# Basic usage
agent-run "What is the weather in Berlin?"

# Specify agent
agent-run "Translate to German: Hello" --agent translator

# Specify LLM profile
agent-run "Complex task" --llm-profile gpt4

# JSON output
agent-run "Question?" --json

# Show status events
agent-run "Task" --show-status

# No colors
agent-run "Task" --no-color
```

**Key Features:**
- Single-command execution
- Session persistence (auto session-id)
- Status streaming to stderr
- Hook-based output formatting

**Code Structure:**
```python
async def main_async(
    request: str,
    agent_name: str | None = None,
    llm_profile: str | None = None,
    show_status: bool = True,
    ...
) -> None:
    """Main async execution logic"""
    
    # 1. Load configuration
    config = load_settings()
    
    # 2. Initialize system
    registry = await initialize_system(config)
    
    # 3. Create agent
    agent = await create_agent(config, registry, agent_name)
    
    # 4. Execute request (with status subscriber)
    result = await run_agent_request(
        agent, request, session_id, ...
    )
    
    # 5. Format output (hooks applied)
    output = await format_output_with_hooks(result, ...)
    
    # 6. Print to stdout
    print_agent_response(output, ...)

def main() -> None:
    """Entry point (parses args, runs async)"""
    parser = argparse.ArgumentParser(...)
    args = parser.parse_args()
    
    asyncio.run(main_async(args.request, ...))
```

#### 3.2.2 agent-cli (`agent_cli.py`)

**File:** `src/agent_system/agent_cli.py`

**Purpose:** Full-featured CLI for system management

**Responsibilities:**
- Multi-command interface
- Plugin/server management
- Session operations
- Configuration inspection
- Admin tasks

**Command Categories:**

| Category | Commands | Description |
|----------|----------|-------------|
| **Agent** | `chat`, `run` | Execute agent tasks |
| **MCP** | `mcp list-servers`, `mcp connect`, `mcp disconnect` | External MCP server management |
| **Tools** | `tools list`, `tools info` | Tool discovery and inspection |
| **Sessions** | `sessions list`, `sessions show`, `sessions delete` | Session management |
| **Plugins** | `plugins list`, `plugins info` | Plugin inspection |
| **Config** | `config-agents list`, `config-agents validate` | Config-based agent management |
| **Hooks** | `hooks list`, `hooks test` | Hook system inspection |
| **Status** | `status`, `status metrics` | System status |

**Usage Examples:**
```bash
# List all MCP servers
agent-cli mcp list-servers

# Connect to external MCP server
agent-cli mcp connect context7

# List available tools
agent-cli tools list --agent default

# Show session history
agent-cli sessions show sess_abc123

# List config-based agents
agent-cli config-agents list

# Validate config agents
agent-cli config-agents validate

# List hooks
agent-cli hooks list

# Test hook execution
agent-cli hooks test format_output --data '{"result": "test"}'
```

**Code Structure:**
```python
def main() -> None:
    """Entry point with subcommand parsing"""
    
    # Preliminary parser (for --color, --json)
    prelim = argparse.ArgumentParser(add_help=False)
    prelim.add_argument("--no-color", ...)
    prelim.add_argument("--out-format", ...)
    prelim_args, _ = prelim.parse_known_args()
    
    # Set global color mode
    set_color_mode(not prelim_args.no_color)
    
    # Main parser with subcommands
    parser = argparse.ArgumentParser(...)
    subparsers = parser.add_subparsers(dest="command")
    
    # Add subcommands
    _add_chat_command(subparsers)
    _add_mcp_commands(subparsers)
    _add_tools_commands(subparsers)
    _add_sessions_commands(subparsers)
    _add_plugins_commands(subparsers)
    _add_config_agents_commands(subparsers)
    _add_hooks_commands(subparsers)
    _add_status_commands(subparsers)
    
    # Parse and execute
    args = parser.parse_args()
    
    # Dispatch to command handler
    asyncio.run(execute_command(args))

async def execute_command(args):
    """Execute the selected command"""
    # Load config and initialize services
    config = load_settings()
    mcp_service = MCPService(config)
    tool_service = ToolService(config, mcp_service)
    
    # Dispatch based on args.command
    if args.command == "mcp":
        await _mcp_list_servers(mcp_service, args)
    elif args.command == "tools":
        await _tools_list(tool_service, args)
    # ... more commands
```

#### 3.2.3 CLI Utilities (`cli_utils/`)

**Directory:** `src/agent_system/cli_utils/`

**Structure:**
```
cli_utils/
├── common.py           # Colors, formatting, hooks, output
├── agent_runner.py     # Shared agent creation logic
└── commands/
    ├── hooks.py        # Hook command handlers
    ├── status.py       # Status command handlers
    └── sessions.py     # Session command handlers
```

**Key Utilities:**

##### common.py

```python
# Color Support
def supports_color() -> bool:
    """Detect terminal color support"""

def colorize(text: str, code: str) -> str:
    """Apply ANSI color codes"""

# Output Formatting
async def format_output_with_hooks(
    result: str,
    registry: MCPRegistry,
    hooks_config: dict
) -> str:
    """Apply format_output hooks"""

def print_agent_response(
    output: str,
    json_mode: bool,
    result_dict: dict
) -> None:
    """Print formatted output"""

# Status Subscriber
async def status_subscriber(
    request_id: str,
    verbose: bool = False
) -> None:
    """Subscribe to status events and print to stderr"""
```

##### agent_runner.py

```python
async def create_and_register_agent(
    config: AgentSystemConfig,
    registry: MCPRegistry,
    agent_name: str
) -> Agent:
    """Shared logic to create and register an agent"""
    
    # Find agent config
    agent_config = config.agents.get(agent_name)
    
    # Create agent instance
    agent = Agent(
        name=agent_name,
        llm_profile=agent_config.llm_profile,
        system_template=agent_config.system_template,
        ...
    )
    
    # Register in MCP registry
    registry.register(agent_name, agent)
    
    return agent
```

---

## 4. Command Structure

### 4.1 Command Hierarchy

```
agent-cli
├── chat                    # Execute agent task (interactive)
├── run                     # Execute agent task (one-shot)
├── mcp
│   ├── list-servers        # List external MCP servers
│   ├── connect <name>      # Connect to MCP server
│   └── disconnect <name>   # Disconnect from MCP server
├── tools
│   ├── list                # List available tools
│   └── info <tool-name>    # Tool details
├── sessions
│   ├── list                # List all sessions
│   ├── show <id>           # Show session messages
│   └── delete <id>         # Delete session
├── plugins
│   ├── list                # List plugins
│   └── info <plugin-name>  # Plugin details
├── config-agents
│   ├── list                # List config-based agents
│   ├── show <name>         # Agent definition
│   └── validate            # Validate all agents
├── hooks
│   ├── list                # List available hooks
│   └── test <hook> <data>  # Test hook execution
└── status
    └── metrics             # System metrics
```

### 4.2 Global Options

Available for all commands:

| Option | Description | Default |
|--------|-------------|---------|
| `--no-color` | Disable colored output | Color enabled |
| `--out-format <fmt>` | Output format (json, table, plain) | `table` |
| `--verbose` | Enable verbose logging | `false` |
| `--config <path>` | Configuration file path | `config/config.yaml` |

### 4.3 Common Patterns

**List Commands:**
```bash
# Table format (default)
agent-cli tools list
agent-cli plugins list

# JSON format
agent-cli tools list --out-format json
agent-cli sessions list --json
```

**Detail Commands:**
```bash
# Show details about specific item
agent-cli tools info web_search
agent-cli plugins info basic_operations
agent-cli sessions show sess_abc123
```

**Action Commands:**
```bash
# Perform action on resource
agent-cli mcp connect context7
agent-cli sessions delete sess_abc123
```

---

## 5. CLI Tools

### 5.1 agent-run

**Entry Point:** `agent-run` (console script)

**Arguments:**
```
positional arguments:
  request               The task/question for the agent

optional arguments:
  --agent AGENT         Agent name (default: from config)
  --llm-profile PROFILE LLM profile to use
  --session-id ID       Session ID (default: auto-generated)
  --json                Output as JSON
  --no-color            Disable colors
  --show-status         Show status events (default: true)
  --no-status           Hide status events
  --verbose             Verbose logging
```

**Output:**
```bash
# Default (formatted, with colors)
$ agent-run "What is 2+2?"
Starting agent 'default' with gpt-3.5-turbo...
▶ Step 1/10: Processing request
✓ Complete

The answer is 4.

# JSON mode
$ agent-run "What is 2+2?" --json
{
  "result": "The answer is 4.",
  "usage_stats": { "total_tokens": 15 },
  "session_id": "sess_xyz"
}

# No status events
$ agent-run "What is 2+2?" --no-status
The answer is 4.
```

**Exit Codes:**
- `0` - Success
- `1` - Error (agent failure, config error, etc.)
- `130` - Interrupted (Ctrl+C)

### 5.2 agent-cli

**Entry Point:** `agent-cli` (console script)

**Subcommands:**

#### 5.2.1 MCP Commands

```bash
# List external MCP servers
$ agent-cli mcp list-servers
NAME        ADDRESS                  STATUS       DESCRIPTION
context7    http://localhost:8001    Connected    Context7 docs
memory      http://localhost:8002    Disconnected Memory system

# Connect to server
$ agent-cli mcp connect context7
{"status": "connected", "server": "context7"}

# Disconnect from server
$ agent-cli mcp disconnect context7
{"status": "disconnected", "server": "context7"}
```

#### 5.2.2 Tools Commands

```bash
# List all tools
$ agent-cli tools list
NAME              SOURCE          DESCRIPTION
web_search        basic_ops       Search the web
calculator        basic_ops       Perform calculations
get_weather       weather_plugin  Get weather data

# List tools for specific agent
$ agent-cli tools list --agent researcher
NAME              SOURCE          DESCRIPTION
web_search        basic_ops       Search the web
read_file         basic_ops       Read file contents

# Tool details
$ agent-cli tools info web_search --json
{
  "name": "web_search",
  "description": "Search the web",
  "source": "basic_operations",
  "parameters": {
    "query": {"type": "string", "required": true}
  }
}
```

#### 5.2.3 Session Commands

```bash
# List all sessions (for current user)
$ agent-cli sessions list
SESSION_ID         CREATED              MESSAGES  LAST_ACCESSED
sess_abc123        2025-01-15 10:00     5         2025-01-15 10:05
sess_def456        2025-01-14 15:30     12        2025-01-14 16:00

# Show session messages
$ agent-cli sessions show sess_abc123
[2025-01-15 10:00:00] user: Hello
[2025-01-15 10:00:02] assistant: Hi! How can I help?
[2025-01-15 10:00:10] user: What is 2+2?
[2025-01-15 10:00:12] assistant: The answer is 4.

# Delete session
$ agent-cli sessions delete sess_abc123
{"status": "deleted", "session_id": "sess_abc123"}
```

#### 5.2.4 Config Agents Commands

```bash
# List config-based agents
$ agent-cli config-agents list
NAME          ENABLED  LLM_PROFILE  TOOLS             DESCRIPTION
researcher    true     gpt4         web_search, ...   Research assistant
translator    true     gpt3.5       none              Translation agent
summarizer    false    claude       none              Summarization agent

# Show agent details
$ agent-cli config-agents show researcher
name: researcher
enabled: true
llm_profile: gpt4
max_steps: 10
system_template: prompts/researcher.md
tools:
  include: [web_search, calculator]
hooks:
  format_output: [markdown_formatter]
metadata:
  visibility: ui

# Validate all config agents
$ agent-cli config-agents validate
Validating config-based agents...
✓ researcher - valid
✓ translator - valid
✗ summarizer - error: LLM profile 'claude' not found

Total: 3, Passed: 2, Failed: 1
```

#### 5.2.5 Hooks Commands

```bash
# List available hooks
$ agent-cli hooks list
HOOK_TYPE         PLUGIN            ORDER
pre_llm_call      token_counter     10
post_llm_call     token_counter     10
format_output     markdown_fmt      20
format_output     html_fmt          30

# Test hook execution
$ agent-cli hooks test format_output \
  --data '{"result": "**bold**"}'
{
  "result": "<strong>bold</strong>",
  "hook": "markdown_fmt"
}
```

---

## 6. Output Formatting

### 6.1 Output Formats

| Format | Description | Use Case |
|--------|-------------|----------|
| **table** | Formatted table (tabulate) | Human-readable lists |
| **json** | JSON output | Scripting, automation |
| **plain** | Plain text (no formatting) | Logs, simple output |

### 6.2 Color Support

**Detection:**
- Check `TERM` environment variable
- Check `NO_COLOR` environment variable
- Check `--no-color` flag

**Color Codes:**
```python
COLORS = {
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "gray": "90",
}

def colorize(text: str, color: str) -> str:
    """Apply ANSI color"""
    if not supports_color():
        return text
    code = COLORS.get(color, "0")
    return f"\033[{code}m{text}\033[0m"
```

**Usage:**
```python
# Status indicators
print(colorize("✓ Success", "green"))
print(colorize("✗ Failed", "red"))
print(colorize("▶ Running", "blue"))

# Server status
if status == "connected":
    print(colorize("Connected", "green"))
elif status == "disconnected":
    print(colorize("Disconnected", "red"))
```

### 6.3 Hook-Based Formatting

**Flow:**
```
Agent Result (text)
    │
    ▼
format_output hooks (in order)
    │
    ├─► Hook 1: markdown_formatter
    ├─► Hook 2: html_formatter
    ├─► Hook 3: custom_formatter
    │
    ▼
Final Output
    │
    ▼
Print to stdout
```

**Example:**
```python
async def format_output_with_hooks(
    result: str,
    registry: MCPRegistry,
    hooks_config: dict
) -> str:
    """Apply format_output hooks"""
    
    # Get format_output hooks
    hooks = hooks_config.get("format_output", [])
    
    output = result
    for hook_name in hooks:
        # Execute hook
        plugin = registry.get(hook_name)
        if hasattr(plugin, "format_output"):
            output = await plugin.format_output(output)
    
    return output
```

---

## 7. Key Design Decisions

### ADR-001: Two CLI Tools

**Context:** Need both quick execution and comprehensive management  
**Decision:** Provide `agent-run` (simple) and `agent-cli` (full-featured)  
**Rationale:**
- agent-run: Minimal keystrokes for common use case
- agent-cli: Full control for admin/power users
- Avoid bloat in simple tool

**Status:** Accepted

---

### ADR-002: Argparse over Click

**Context:** Need CLI argument parsing  
**Decision:** Use argparse (stdlib)  
**Rationale:**
- No external dependency
- Good enough for our needs
- Familiar to Python developers

**Status:** Accepted

---

### ADR-003: JSON Output Mode

**Context:** Need scriptable CLI  
**Decision:** Add `--out-format json` flag to all commands  
**Rationale:**
- Enables scripting and automation
- Machine-readable output
- Consistent across commands

**Status:** Accepted

---

### ADR-004: Status on stderr

**Context:** Status events vs. final result  
**Decision:** Status events → stderr, final result → stdout  
**Rationale:**
- Allows piping output without noise
- Status is informational, not result
- Common Unix pattern

**Status:** Accepted

---

## 8. Data Flow

### 8.1 agent-run Flow

```
User Command
    │
    ▼
Parse Arguments (argparse)
    │
    ├─► request (positional)
    ├─► agent_name (--agent)
    ├─► llm_profile (--llm-profile)
    ├─► options (--json, --no-color, etc.)
    │
    ▼
main_async()
    │
    ├─► Load configuration (ConfigService)
    ├─► Initialize system (bootstrap plugins)
    ├─► Create agent (Agent factory)
    │
    ▼
run_agent_request()
    │
    ├─► Subscribe to status events (stderr)
    ├─► Execute agent (Agent.run_events)
    ├─► Collect final result
    │
    ▼
format_output_with_hooks()
    │
    ├─► Apply format_output hooks
    │
    ▼
print_agent_response()
    │
    ├─► Format as JSON or plain text
    ├─► Print to stdout
    │
    ▼
Exit (code 0 or 1)
```

### 8.2 agent-cli Flow

```
User Command
    │
    ▼
Preliminary Parser (--no-color, --out-format)
    │
    ├─► Set global color mode
    │
    ▼
Main Parser (subcommands)
    │
    ├─► Parse subcommand (mcp, tools, sessions, etc.)
    ├─► Parse subcommand args
    │
    ▼
execute_command()
    │
    ├─► Load configuration
    ├─► Initialize services (MCPService, ToolService)
    │
    ▼
Command Handler
    │
    ├─► mcp list-servers → _mcp_list_servers()
    ├─► tools list → _tools_list()
    ├─► sessions show → _sessions_show()
    │
    ▼
Service Call
    │
    ├─► MCPService.list_servers()
    ├─► ToolService.list_tools()
    │
    ▼
Format Output
    │
    ├─► JSON or table format
    ├─► Apply colors if enabled
    │
    ▼
Print to stdout
    │
    ▼
Exit
```

---

## 9. Related Documents

### 9.1 Architecture Documents

- [System Architecture](agent_system_architecture.md) - Overall system
- [App Architecture](app_architecture.md) - FastAPI application
- [Plugin Architecture](plugin_architecture.md) - Plugin system

### 9.2 User Guides

- [CLI Reference](cli_reference.md) - Detailed command reference
- [Configuration Guide](../config/README.md) - Config files
- [Plugin Authoring](plugin_authoring.md) - Creating plugins

### 9.3 Design Documents

- [Session Management](session_management.md) - Sessions
- [Status System](status_design.md) - Real-time status
- [Hook System](plugin_hooks.md) - Lifecycle hooks

---

**Document Changelog:**

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2025-01-15 | AgentSystem Team | Initial CLI architecture SAD |

---

**Approval:**

| Role | Name | Date | Signature |
|------|------|------|-----------|
| Architect | - | - | - |
| Tech Lead | - | - | - |
| Product Owner | - | - | - |
