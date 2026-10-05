# Plugin Authoring Guide

The verified quick reference is the Claude skill `.claude/skills/plugin-authoring/` (`SKILL.md` + `references/`).

This document explains how to create plugins (tool servers) for AgentSystem. It walks you through the entire process: from the initial idea to implementation and deployment.

## Table of Contents

- [What is a Plugin?](#what-is-a-plugin)
  - [Alternative: Configuration-Based Agents](#alternative-configuration-based-agents)
- [Quick Start: Your First Plugin](#quick-start-your-first-plugin)
- [Plugin Structure and Layout](#plugin-structure-and-layout)
- [Defining Schema (`schema.yaml`)](#defining-schema-schemayaml)
  - [Slash Commands (`commands:`)](#slash-commands-commands)
- [Defining Metadata (`plugin.toml`)](#defining-metadata-plugintoml)
- [Implementing the Server](#implementing-the-server)
  - [Agent-Based Plugins](#agent-based-plugins)
  - [Plugin Types Summary](#plugin-types-summary)
- [Tools and Parameters](#tools-and-parameters)
- [Model Experience (in the plugin's guide)](#model-experience-in-the-plugins-guide)
- [Advanced Features](#advanced-features)
  - [Status and Progress Reporting](#status-and-progress-reporting)
  - [Cooperative Cancellation](#cooperative-cancellation)
  - [Web Endpoints and UI Integration](#web-endpoints-and-ui-integration)
  - [CLI Support (Optional)](#cli-support-optional)
  - [Background Tasks](#background-tasks)
  - [Starting and Stopping (`start_plugin` / `stop_plugin`)](#starting-and-stopping-start_plugin--stop_plugin)
- [Configuration and Deployment](#configuration-and-deployment)
- [Testing and Quality Assurance](#testing-and-quality-assurance)
- [Packaging and Distribution](#packaging-and-distribution)
- [Best Practices](#best-practices)
- [Troubleshooting](#troubleshooting)

## What is a Plugin?

A plugin in AgentSystem is a **tool server**: a Python object with a `call(tool, params)` method that provides new tools to the agent. Plugins extend the agent's capabilities with specific functions like web scraping, database access, or API integration. It speaks no protocol -- talking to external MCP servers is the job of one particular plugin, `mcp_client`.

### How Plugins Work

1. **Discovery**: AgentSystem finds plugins in the `plugins.plugin_dirs` of `config/plugins.yaml` (`src/plugins*`: `src/plugins` and every further `src/plugins_<name>/`) and in installed packages via the `agent_system.tool_plugins` entry point group. LLM providers sit in the same directory but are found separately, by the LLM registry, which reads the manifests rather than the folder name.
2. **Schema**: Each plugin describes its tools in `schema.yaml` (what parameters, what they do)
3. **Activation**: A server entry under `plugins: servers:` with `type: <plugin folder name>` and `enabled: true` (the default is `false`) builds an instance; an agent sees its tools only if its `agent_config.tools.allowed` admits them
4. **Execution**: The agent calls tools via `call(tool_name, parameters)`
5. **Response**: The plugin returns structured results

### Plugin Goals
- **Small, focused tools** instead of monolithic functions
- **Reliable execution** with error handling and cancellation
- **Good UX** through status updates and progress reporting
- **Simple configuration** and clear documentation

### Alternative: Configuration-Based Agents

**Before writing a plugin, consider if you need one at all.**

AgentSystem now supports **configuration-based agents** that can be defined purely in YAML without writing any Python code. These are ideal for:

- **Specialized assistants** with unique prompts (e.g., financial analyst, code reviewer, research assistant)
- **Prompt variations** for different use cases (formal vs casual tone, domain-specific language)
- **Tool subset configurations** (restrict agent to specific tool servers/tools)
- **Quick prototyping** of agent behaviors before building custom plugins

**Use configuration-based agents when:**
- You need an agent with a specialized prompt or persona
- You want to restrict/allow specific tools without code
- You want to test different LLM profiles/strategies
- You don't need custom tool implementations

**Use plugin-based agents when:**
- You need custom tool implementations with complex logic
- You require state management or background tasks
- You need web endpoints or CLI commands
- You want to package reusable tools for distribution

**Example Configuration-Based Agent:**

An agent is a server entry whose `type` is `basic_agent` (or another agent it
inherits from). The file lives under `config/agents*/*.yaml` or
`src/plugins*/<plugin>/agents/*.yaml` — both globs are included by
`config/config.yaml`. A top-level `agents:` key is silently ignored.

```yaml
# e.g. config/agents/financial_analyst.yaml
plugins:
  servers:
    financial_analyst:
      type: basic_agent
      enabled: true                     # default: false
      description: "Financial analysis and market research agent"
      # server level, NOT under agent_config (agent_config rejects unknown keys)
      self_tool_descriptions:
        financial_analyst_execute_task: "Analyze stocks and market data"
      metadata:
        visibility: both                # ui | tool | both | private (default)
      agent_config:
        llm_profile: [turbo, normal]    # chain: [primary, fallback, ...]
        # Optional: override LLM parameters on top of the referenced model
        # config (applies to every member of the chain, fallbacks included)
        # — see docs/basic_agent_llm_profiles.md
        llm_params:
          thinking_level: low
          max_tokens: 8000
        max_steps: 20
        system_template: "./prompts/financial_analyst.md"   # ./ = relative to this YAML
        tools:
          allowed:                      # empty = no tools at all
            - "yahoo_finance/*"
            - "web_scraper/*"
            - "duckduckgo_search/*"
          blocked:
            - "ssh_control/*"
```

Allowlist patterns are `instance/*`, `instance`, `instance/<full tool name>`
(the rendered name including the instance prefix, e.g.
`web_scraper/web_scraper_fetch`) and fnmatch globs.

See [Configurable Agents Guide](configurable_agents.md) for complete documentation.

---

## Quick Start: Your First Plugin

Let's create a simple "hello world" plugin to understand the basics:

```bash
# Create plugin directory
mkdir -p src/plugins/hello_world
cd src/plugins/hello_world
```

**Step 1: Create `schema.yaml`**
```yaml
tools:
  - type: function
    function:
      name: "{{ name }}_say_hello"   # rendered with the instance name
      description: "Says hello to a person"
      parameters:
        type: object
        properties:
          name:
            type: string
            description: "Name of the person to greet"
        required: ["name"]
```

**Step 2: Create `plugin.toml`**
```toml
[plugin]
name = "hello_world"
version = "1.0.0"
description = "Simple hello world plugin"
entrypoint = "server:HelloWorldServer"
type = ["tool-server"]
requires = { agent_system = ">=0.6.0" }   # required by the manifest schema
```

**Step 3: Create `server.py`**
```python
import logging

from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)


class HelloWorldServer(SchemaBasedToolServer):
    """Simple hello world plugin demonstrating modern API."""

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig):
        """
        Modern constructor signature.

        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            server_config: Merged server entry from plugins: servers: (a pydantic
                model with extra="allow", not a dict)
        """
        super().__init__(name, system_config, server_config)

        # Flat keys of the server entry arrive as attributes
        self.greeting_prefix = getattr(server_config, "greeting_prefix", "Hello")

        # Log effective configuration
        logger.info(f"HelloWorld configured: prefix='{self.greeting_prefix}'")

    async def say_hello(self, params: dict) -> dict:
        """
        Tool method - called for the tool "{name}_say_hello" (the mixin
        strips the "{name}_" prefix) or for an unprefixed "say_hello".
        """
        name = params.get("name", "World")
        return {
            "status": "success",
            "message": f"{self.greeting_prefix}, {name}!"
        }
```

The `entrypoint` names the module and the factory: `server:HelloWorldServer`
loads `server.py` from the plugin folder and calls `HelloWorldServer(name,
system_config, server_config)`. Without an `entrypoint`, discovery loads
`plugin.py` and looks for `PLUGIN_FACTORY`.

**Step 4: Enable an instance** (any file included by `config/config.yaml`, e.g. `config/plugins.yaml`):
```yaml
plugins:
  servers:
    hello_world:              # instance name
      type: hello_world       # plugin folder name
      enabled: true
      greeting_prefix: "Hi"
```

Then allow `hello_world/*` in the `agent_config.tools.allowed` of the agent that
should use it.

## Plugin Structure and Layout

### Recommended Directory Structure

**Option 1: Simple (all-in-one)**
```
src/plugins/<plugin_name>/
  ├── plugin.toml       # Plugin metadata + Python requirements (entrypoint = "server:MyServer")
  ├── schema.yaml       # Tool definitions
  ├── server.py         # Main server implementation
  ├── tests/            # Colocated tests (test_*.py)
  ├── README.md         # Short overview
  └── <plugin_name>.guide  # The manual (Help panel)
```

**Option 2: Separated**
```
src/plugins/<plugin_name>/
  ├── plugin.toml       # Plugin metadata + Python requirements (entrypoint omitted or "plugin:PLUGIN_FACTORY")
  ├── schema.yaml       # Tool definitions
  ├── plugin.py         # PLUGIN_FACTORY export
  ├── server.py         # tool server (SchemaBasedToolServer)
  ├── tests/            # Colocated tests (test_*.py)
  ├── README.md         # Short overview
  └── <plugin_name>.guide  # The manual (Help panel)
```

```python
# plugin.py
from .server import MyPluginServer
PLUGIN_FACTORY = MyPluginServer
```

**How discovery loads a plugin** (`agent_system/plugins/discovery.py`):
- It reads `entrypoint` from `plugin.toml` (default `plugin:PLUGIN_FACTORY`) and
  loads `<module>.py` **directly in the plugin folder**. Dotted modules
  (`pkg.mod:X`) are not loaded.
- A module-level `register()` function wins; it returns `(name, factory)`.
- Otherwise the named factory is used, falling back to `PLUGIN_FACTORY`.
- A missing module file or a missing factory skips the folder with a **DEBUG**
  log only; an import error logs a WARNING.
- The plugin type is the **folder name** (unless the module sets `PLUGIN_NAME`),
  not `name` from `plugin.toml`.
- The runtime calls the factory as `factory(name, system_config, server_config)`,
  plus `registry=` only if the factory carries `_accepts_registry = True`.

### Test Structure

Tests are **colocated with the plugin**, in a `tests/` subfolder, and follow the
naming convention:
```
src/plugins/<plugin_name>/tests/
  ├── test_plugin_<plugin_name>_basic.py
  ├── test_plugin_<plugin_name>_integration.py
  └── test_plugin_<plugin_name>_cancellation.py
```

**Example test file:** `src/plugins/hello_world/tests/test_plugin_hello_world_basic.py`

`pytest.ini` collects these via the `src/plugins*/*/tests` testpath (every
plugin root). The shared fixtures (`mock_system_config`,
`reset_global_state`, the fake-LLM patch, …) live in the **root-level
`conftest.py`**, so colocated tests inherit them exactly like tests under `tests/`.

Framework-level tests that span multiple plugins (discovery, hook wiring,
cross-plugin integration) stay under `tests/`. Keep test basenames globally
unique (the naming convention already ensures this) — pytest's default
prepend import mode requires it.

To find a file relative to the plugin, anchor on the plugin dir, not the repo
root: `Path(__file__).resolve().parents[1]` is the plugin directory (the test
lives in `<plugin>/tests/`).

### File Responsibilities

- **`schema.yaml`**: Defines tools, parameters, and validation rules
- **`plugin.toml`**: Metadata for discovery (name, version, entry point) **and**
  the plugin's own Python requirements
- **`plugin.py`** (if separated structure): exports `PLUGIN_FACTORY` (the default entrypoint)
- **`server.py`**: Server implementation; tool routing comes from the base class
- **`tests/`**: Colocated plugin tests
- **`README.md`**: a short overview; usage, configuration and troubleshooting go in `<plugin_name>.guide`

## Defining Metadata (`plugin.toml`)

The `plugin.toml` file contains essential metadata for plugin discovery and
management, plus the plugin's Python package requirements. All metadata lives
under a single `[plugin]` table. `plugin.toml` is the only manifest format that
is read.

### Basic Structure

```toml
[plugin]
name = "my_plugin"
version = "1.0.0"
description = "Brief description of what the plugin does"
author = "Your Name"
entrypoint = "server:PLUGIN_FACTORY"
type = ["tool-server"]
requires = { agent_system = ">=0.6.0" }
```

`name`, `version`, `description` and `requires` are required by
`schemas/plugin-config.schema.json`.

### Field Descriptions

- **`name`**: Plugin name for listings and the validator. The type used in
  configuration (`type: <x>`) is the plugin **folder name**.
- **`version`**: Semantic version (x.y.z)
- **`description`**: Short, clear description for users
- **`author`**: Plugin author/maintainer
- **`entrypoint`**: Module and factory name in format `module:FACTORY_NAME`
  - Format: `module:FactoryFunction` (e.g., `plugin:PLUGIN_FACTORY`, `server:MyServer`)
  - The discovery system loads **only** the specified module file, which must sit directly in the plugin folder
  - Default if omitted: `plugin:PLUGIN_FACTORY` (loads `plugin.py`)
  - If the named factory is missing, `PLUGIN_FACTORY` in the same module is tried
  - Examples:
    - `plugin:PLUGIN_FACTORY` → loads `plugin.py`, uses `PLUGIN_FACTORY` function
    - `server:CustomServer` → loads `server.py`, uses `CustomServer` class
    - `my_module:create_plugin` → loads `my_module.py`, uses `create_plugin()` function

### Plugin Types and Categories

The `type` list describes a plugin's capabilities; combine values for hybrids.
The runtime does **not** read it to decide what a plugin can do: tools come from
`tools:` in `schema.yaml`, hooks from `hooks:` in `schema.yaml`, web routes from a
`get_web_router()` method. `type` is checked by `src/scripts/validate_plugin.py` —
and by the LLM registry, which skips every plugin whose `type` does not
contain `llm-provider` — that list is what tells its providers apart from
the tool servers beside them. Set it correctly anyway.

```toml
# Tool-only plugin (provides tool server/tools)
[plugin]
name = "web_scraper"
version = "1.0.0"
description = "Web scraping tools for content extraction"
author = "Your Name"
entrypoint = "server:WebScraperServer"
type = ["tool-server"]
category = "tools"
```

```toml
# Hybrid plugin (tools + Web + Hooks) — combine types in the list
[plugin]
name = "todo"
version = "1.0.0"
description = "Todo management with tools, web UI, and hooks"
author = "AgentSystem"
entrypoint = "plugin:PLUGIN_FACTORY"
type = ["tool-server", "web", "hooks"]
category = "productivity"
```

Other single-capability examples: `type = ["web"]` (web UI/endpoints only),
`type = ["hooks"]` (lifecycle event handlers only).

**Type Field Options:**
- `tool-server`: Plugin provides tools/server
- `web`: Plugin provides web UI/endpoints
- `hooks`: Plugin provides lifecycle event hooks
- `library`: Config only — agents, skills, prompts, no code. Such a plugin has
  **no `entrypoint` and no `plugin.py`** (`coder`, `amiga`,
  `research`)
- `llm-provider`: An LLM/TTS/batch/decisions backend under `src/plugins/`.
  Found by `agent_system.llm.registry` through its `provides` /
  `provides_batch` / `provides_tts` / `provides_decisions` manifest keys and
  its `provider.py`, which exports the matching dict (`PROVIDERS`,
  `DECISION_PROVIDERS`, …) — also **without an `entrypoint`**. A plugin may
  serve only the non-chat seams: `llm_decisions` declares no `provides` at all
- `custom`: allowed by the schema, used by no plugin

Combine multiple types by listing them (e.g., `["tool-server", "web"]` for hybrid plugins).

`src/scripts/validate_plugin.py <dir>` checks a manifest against
`schemas/plugin-config.schema.json`, which is strict
(`additionalProperties: false`) — **a new manifest key has to be entered there**,
or the validator rejects the plugin.

### Advanced Options

```toml
[plugin]
name = "advanced_plugin"
version = "2.1.0"
description = "Advanced plugin with dependencies and configuration"
author = "Team Name"
entrypoint = "server:AdvancedServer"
type = ["tool-server", "web"]
category = "tools"

# Searchable keywords
tags = ["web", "scraping", "api"]

# Framework version constraint (not a pip dependency; agent_system is required)
requires = { python = ">=3.11", agent_system = ">=0.6.0" }

# Agent plugins only: a promise that the constructor touches nothing but
# config and an LLM client -- no file, no thread, no socket, no network.
# Runtime.start() builds every declared server. What the runtime checks for a
# lazy type: the instance must be an Agent (TypeError otherwise, the server is
# dropped); its LLM config and system_template are checked at start and logged
# as ERROR, but the server is still built. The contract test
# tests/bootstrap/test_runtime_lazy_contract.py builds every lazy type.
# Nobody checks the no-I/O promise. Leave it off unless the __init__ has been
# read with this in mind.
lazy = true

# Python package requirements OWNED by this plugin (pip specs only — not
# other plugins). Aggregated into the install; see "Dependencies" below.
dependencies = ["requests>=2.25.0", "beautifulsoup4>=4.9.0"]
```

### Metadata Field Reference

**Core Fields:**
- **`name`**: Plugin name for listings and validation (configuration uses the folder name as the type)
- **`version`**: Semantic version (x.y.z) for compatibility tracking
- **`description`**: Short, clear description for users and UIs
- **`author`**: Plugin author/maintainer for support
- **`entrypoint`**: Module and factory name in format `module:FACTORY_NAME` (see Field Descriptions above for details)
  - **Critical**: Only the specified module is loaded by the discovery system
  - Default: `plugin:PLUGIN_FACTORY` if field is omitted

**Plugin Classification:**
- **`type`**: List of plugin capabilities (`tool-server`, `web`, `hooks`,
  `library`, `llm-provider`, `custom`) — see above; `library` and
  `llm-provider` need no `entrypoint`. Read by the validator and the LLM
  registry, not by the runtime
- **`category`**: Functional category (`tools`, `monitoring`, `data`, `ui`, `utilities`)
- **`tags`**: Searchable keywords for discovery

**Framework & dependencies:**
- **`requires`**: Framework/runtime version constraints (e.g.
  `{ python = ">=3.11", agent_system = ">=0.6.0" }`); `agent_system` is
  required. NOT pip packages.
- **`dependencies`**: the plugin's own pip requirements, as a list of PEP 508
  specs (`["ruamel.yaml>=0.18"]`). List only real PyPI packages — never other
  plugin names (inter-plugin needs are resolved by discovery, not pip).

### How plugin dependencies reach the install

Plugin-owned requirements do **not** live in the root `pyproject.toml`. Instead:

1. Each plugin declares its pip deps in `plugin.toml` (`[plugin] dependencies`).
2. `scripts/aggregate_plugin_deps.py` merges `requirements/core.txt` (framework
   deps shared by many plugins) with every plugin's `dependencies` into
   `requirements/all.txt`. Scanned root: `src/plugins` (LLM provider plugins
   included). Where further plugin roots `src/plugins_<name>/` exist, what
   they need beyond that goes to
   `requirements/private.txt`; it is not part of the open-source release.
3. The root `pyproject.toml` reads both files via `[tool.setuptools.dynamic]`
   (setuptools skips one that does not exist), so `pip install .` installs
   the full set of the checkout.

After adding or changing a plugin's `dependencies`, re-run:
```
python scripts/aggregate_plugin_deps.py
```
A drift-guard test (`tests/pluginsystem/test_plugin_deps_aggregation.py`) fails
if `requirements/all.txt` is stale. Deps used by many plugins or by the
framework stay in `requirements/core.txt`; a dep used by exactly one plugin
belongs in that plugin's `plugin.toml`.

## Defining Schema (`schema.yaml`)

The `schema.yaml` file defines your plugin's tools using OpenAI function format. This is how the agent knows what tools are available and how to call them.

> **Important**: `schema.yaml` contains only tool definitions and web UI configuration. Plugin metadata (type, category, version, etc.) belongs in `plugin.toml`, not here.

### Basic Tool Definition

```yaml
tools:
  - type: function
    function:
      name: search_web
      description: "Search the web for information"
      parameters:
        type: object
        properties:
          query:
            type: string
            description: "Search query"
          max_results:
            type: integer
            description: "Maximum number of results"
            minimum: 1
            maximum: 100
            default: 10
        required: ["query"]
        additionalProperties: false
```

The framework does not validate tool arguments against this schema — `required`,
`enum`, `minimum` and `additionalProperties` are hints to the model. Validate in
the handler (see [Parameter Validation](#parameter-validation)).

### Schema Template Variables

Schema files support Jinja2 template variables that are resolved when the schema is loaded. This allows you to create dynamic tool names and configuration-based constraints.

**Available Template Variables:**
- `{{ name }}`: Plugin instance name (useful for namespacing tools)
- Custom variables provided by your server (via `_load_schema` override)

**Example with Plugin Name:**
```yaml
tools:
  - type: function
    function:
      name: "{{ name }}_calculator"  # Becomes "example_calculator" for plugin instance "example"
      description: Perform basic arithmetic operations
      parameters:
        type: object
        properties:
          operation:
            type: string
            enum: [add, subtract, multiply, divide]
        required: ["operation"]
        additionalProperties: false
```

**Example with Custom Configuration Variables:**
```yaml
tools:
  - type: function
    function:
      name: "wait"
      description: Wait for specified seconds
      parameters:
        type: object
        properties:
          seconds:
            type: number
            minimum: 0.1
            maximum: {{ max_wait_seconds }}  # Resolved from server configuration
            description: "Number of seconds to wait (0.1 to {{ max_wait_seconds }} seconds)"
        required: ["seconds"]
        additionalProperties: false
```

**Custom Template Variables in Server:**
```python
class MyServer(SchemaBasedToolServer):
    def get_template_vars(self) -> dict[str, Any]:
        """Override to provide custom template variables."""
        return {
            "name": self.name,
            "max_wait_seconds": self.max_wait_seconds,
            "available_models": self.get_available_models()
        }
```



**Template Best Practices:**
- Keep numeric template variables unquoted so they render with correct types
- Use `{{ name }}` for tool name prefixing to avoid conflicts between plugin instances
- Validate template variables in your server initialization
- Document custom template variables in your plugin's guide
- **Prefer `get_template_vars()` override** over `_load_schema()` override for custom variables

**Why use `get_template_vars()` instead of overriding `_load_schema()`?**
- **Cleaner code**: Just return a dictionary instead of duplicating schema loading logic
- **Less error-prone**: Base class handles caching, error handling, and directory resolution
- **Better maintainability**: Your code focuses only on the template variables, not infrastructure
- **Future-proof**: Benefits from base class improvements automatically

### Parameter Types and Validation

```yaml
properties:
  # String with constraints
  url:
    type: string
    format: uri
    description: "Valid URL"

  # Enum values
  format:
    type: string
    enum: ["json", "xml", "csv"]
    description: "Output format"

  # Number with range
  timeout:
    type: number
    minimum: 1
    maximum: 300
    default: 30
    description: "Timeout in seconds"

  # Array of strings
  tags:
    type: array
    items:
      type: string
    description: "List of tags"

  # Complex object
  options:
    type: object
    properties:
      recursive:
        type: boolean
        default: false
      depth:
        type: integer
        minimum: 1
    additionalProperties: false
```

### Multi-Tool Plugin with Web Interface

```yaml
tools:
  - type: function
    function:
      name: fetch_url
      description: "Fetch content from a URL"
      parameters:
        type: object
        properties:
          url:
            type: string
            format: uri
        required: ["url"]

  - type: function
    function:
      name: parse_html
      description: "Parse HTML content"
      parameters:
        type: object
        properties:
          html:
            type: string
        required: ["html"]

  - type: function
    function:
      name: extract_links
      description: "Extract links from HTML"
      parameters:
        type: object
        properties:
          html:
            type: string
        required: ["html"]

# Web UI configuration for hybrid plugins
web_ui:
  panel:
    endpoint: "/plugins/{{ name }}/dashboard"
    title: "Web Scraper"
    description: "Web scraping tools with real-time monitoring"
    icon: globe
    category: agents
    keywords: [scraping, jobs]

  endpoints:
    - path: "/api/scrape"
      method: "POST"
      handler: "start_scrape"
      response_type: "json"
      description: "Start scraping job"
    - path: "/api/jobs"
      method: "GET"
      handler: "list_jobs"
      response_type: "json"
      description: "List active scraping jobs"
    - path: "/dashboard"
      method: "GET"
      handler: "dashboard"
      response_type: "html"
      description: "Main scraping dashboard"
```

### Web UI Configuration (For Hybrid/Web Plugins)

For plugins that provide web interfaces, add a `web_ui` section to your schema:

> **Note**: The `web_ui` section belongs in `schema.yaml` for UI configuration. Plugin metadata (type, category, etc.) belongs in `plugin.toml`.

```yaml
# After your tools definitions
tools:
  - type: function
    # ... your tool definitions ...

# Web UI configuration
web_ui:
  # The panel's entry in the panel catalogue (launcher, command palette, chat links)
  panel:
    endpoint: "/plugins/{{ name }}/"          # required: URL of the panel page
    title: "My Plugin"                        # required
    description: "Plugin description for UI"  # shown and searched in the launcher
    icon: wrench                              # required: a symbol id in static/kit/icons.svg
    category: agents                          # required: session, context, agents, debug, system, admin
    keywords: [dashboard, data]               # optional search words
    window: {width: 800, height: 600}         # optional: size of the detached window
    contexts:                                 # optional entry points from the chat: session, request
      session: "/plugins/{{ name }}/?session_id={session_id}"   # must start with the endpoint

  # The routes generated from the schema
  endpoints:
    - path: "/"
      method: "GET"
      handler: "render_panel"
      response_type: "html"
      description: "Main dashboard interface"
    - path: "/api/data"
      method: "GET"
      handler: "get_data"
      response_type: "json"
      description: "Retrieve plugin data"
    - path: "/api/action"
      method: "POST"
      handler: "perform_action"
      response_type: "json"
      description: "Perform plugin action"
    - path: "/static/{file_path:path}"
      method: "GET"
      handler: "serve_static"
      response_type: "response"
      description: "Static assets (CSS, JS, images)"
```

### Web UI Fields Reference

`web_ui` has exactly two keys, `panel` and `endpoints`. `src/scripts/validate_plugin.py`
refuses any other key in `web_ui` or in `panel`. At runtime a `panel` block that does
not parse is left out of the catalogue with an error log.

**`panel`** (optional) — the plugin's entry in the panel catalogue, parsed by
`plugin_panel()` in `src/agent_system/ui/catalog.py`:
- `endpoint` (required): URL of the panel page; `{{ name }}` is the plugin instance
- `title` (required): name in the launcher, on the tab and in the window bar
- `icon` (required): a symbol id from `static/kit/icons.svg` (all of them render at `/ui/kit`)
- `category` (required): one of `session`, `context`, `agents`, `debug`, `system`, `admin`
- `description`: one sentence, shown and searched in the launcher
- `keywords`: search words that are not in the title
- `window`: `{width, height}` of the detached window
- `contexts`: entry points from the chat, `session` and `request`. The URL must start
  with `endpoint`; the shell fills in `{session_id}` / `{request_id}`. Declare a context
  only if the panel reads that parameter

Who sees the panel is not declared here: the catalogue lists it for the roles both
layers of route security in `config/security.yaml` (included by `config/config.yaml`) let open its `endpoint` -- the
app-wide `auth.endpoint_security` rules and `auth.plugin_security`. An admin-only
route is an admin-only panel.

**`endpoints`** — the routes `SchemaRouterGenerator` builds from the schema (`path`,
`method`, `handler`, `response_type`, `description`); see
[Schema-Based Web Routing](./schema_based_web_routing.md).

### Slash Commands (`commands:`)

Tools are what the *model* calls. A `commands:` entry is what a *person* can
type at the chat prompt — `/compact` runs context_engineer's compaction without
spending an LLM turn.

```yaml
commands:
  - name: compact
    description: "shrink this conversation now"   # the only thing /help shows
    tool: "{{ name }}_compact"                    # required: a tool of THIS plugin
    argument: query                               # optional: rest of the line -> this parameter
    argument_hint: "<words>"                      # optional: shown in /help
```

Rules worth knowing before you add one:

- **A command runs one of your own tools.** Execution goes through
  `Agent.dispatch_tool_call`, so it inherits the agent's allowlist and the
  runtime params (`_session_id`, `_agent`). An agent that may not call the tool
  never even sees the command in `/help`.
- **Names are shared with built-ins and skills.** Built-ins win, then plugin
  commands, then skills. A bare name only resolves while it is unique; two
  plugins claiming `compact` are both reachable as `/<plugin>:compact`.
- **No arguments unless you declare `argument:`.** Typed arguments to a command
  that takes none are refused, not dropped.
- `pytest tests/pluginsystem/test_plugin_command_declarations.py` catches a
  typo in `tool:` or `argument:` — without it, the command is filtered out by
  authorization and simply never appears.

The full rationale (why these are actions, not prompt expansion like Claude
Code's plugin commands) is in `docs/plugin_commands_design.md`.

### Schema Best Practices

- **Small tools**: Prefer many focused tools over one complex tool
- **Clear names**: Use descriptive, action-oriented names
- **Good descriptions**: Explain what the tool does and when to use it
- **Strict validation**: Use `additionalProperties: false` and constraints
- **Sensible defaults**: Provide defaults for optional parameters
- **Web UI integration**: Add `web_ui` section for plugins with web interfaces

## Implementing the Server

The server is the core of your plugin. It handles tool routing, validation, and execution.

### Modern Plugin Pattern (Recommended)

**Key Principles:**
1. **Modern Constructor**: `(name, system_config, server_config)` signature
2. **No Manual Routing**: Remove `call()` override - `SchemaBasedToolMixin` handles it
3. **Tool Methods**: Tool `{name}_x` → method `x`; tool exactly `{name}` → `execute`; any other tool name → the method of the same name
4. **Type Hints**: Use modern Python type hints (`| None` instead of `Optional[]`)
5. **Configuration**: Extract from `server_config` (plugin-specific) and `system_config` (system-wide)
6. **Datenpfade**: Das Datenverzeichnis ist konfigurierbar (`AGENT_DATA_DIR`, sonst `paths.data_dir` in `config/config.yaml`). Einen Default nie als `"data/..."` oder `ROOT / "data" / ...` schreiben, sondern `data_path("mein_plugin", "x.db")` aus `agent_system.paths` — erst zur Laufzeit aufrufen, nicht auf Modulebene. Einen Wert aus Umgebung, Kommandozeile oder einer Datenbankzeile mit `resolve_data_path(wert)` auflösen. Werte aus `plugins.yaml` und die Defaults aus `schema.yaml` verschiebt der Loader selbst. Der Wächter `tests/config/test_no_hardcoded_data_dir.py` schlägt bei einem neuen Literal an.

### Method 1: Schema-Based Server (Recommended)

Use `SchemaBasedToolServer` for automatic schema loading and generic dispatching.

**A plugin that is a tool server AND a hook inherits
`SchemaBasedHookToolServer`** (`agent_system/tools/hook_tool_server.py`) and
calls `super().__init__(name, system_config, server_config)` once. Do not call
the two base initialisers by hand: their signatures disagree, so
`ToolServer.__init__` does not call up the chain, and the six plugins that
wired it themselves ended up with four spellings and three behaviours -- two of
them never ran `PluginHook.__init__` at all, so they had no `self.config` and
`get_order_spec()` raised. The base builds the hook config from the schema's
`config:` block with the plugins.yaml `hook_config` block on top, and builds it
lazily: a schema whose template reads the subclass's own attributes cannot be
rendered while the base initialiser is still running.

**Note:** `SchemaBasedToolServer` inherits from `SchemaBasedToolMixin` (`agent_system/tools/schema_mixin.py`) which provides:
- Automatic `schema.yaml` loading and caching
- Generic `call()` dispatcher (routes tool calls to methods automatically)
- Template variable support (`{{name}}` in schema.yaml)
- Development utilities (`get_schema_data()`, `clear_schema_cache()`)

```python
import logging

from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.config.models import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)   # ToolServer has no self.logger


class WebScrapingServer(SchemaBasedToolServer):
    """Modern schema-based plugin with automatic tool routing."""

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig):
        """
        Modern constructor signature.

        Args:
            name: Plugin instance name (used for tool prefixing in schema templates)
            system_config: System-wide configuration (ports, paths, etc.)
            server_config: Plugin-specific configuration from config/plugins.yaml
        """
        super().__init__(name, system_config, server_config)

        # Nested `config:` block of the server entry (server_config is a pydantic
        # model with extra="allow" -- no .get(), and the block may be absent)
        config = getattr(server_config, "config", None) or {}
        self.timeout = float(config.get("timeout", 30))
        self.user_agent = config.get("user_agent", "AgentSystem/1.0")
        self.max_retries = int(config.get("max_retries", 3))

        # Validate configuration -- the framework does not validate plugin config
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")

        # Log effective configuration
        logger.info(
            f"WebScraping configured: timeout={self.timeout}, "
            f"user_agent={self.user_agent}, max_retries={self.max_retries}"
        )

    # Tool methods - routed by SchemaBasedToolMixin.call():
    # tool "{name}_fetch_url" -> method fetch_url (prefix stripped)

    async def fetch_url(self, params: dict) -> dict:
        """
        Fetch content from a URL.

        Called for the tool "{name}_fetch_url" (or an unprefixed "fetch_url").
        No manual routing needed.
        """
        url = params.get("url")
        if not url:
            return {"status": "error", "error": "url parameter is required"}

        # Runtime parameters injected by the framework (may be absent in tests)
        status = params.get("_status")
        token = params.get("_cancellation_token")
        request_id = params.get("request_id")

        if status:
            await status.progress(f"Fetching {url}")

        # Check cancellation before starting
        if token and token.is_cancelled:
            return {"error": "Tool 'fetch_url' was cancelled.", "cancelled": True,
                    "forced": token.is_forced}

        try:
            import aiohttp
            timeout_cfg = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
                headers = {"User-Agent": self.user_agent}
                async with session.get(url, headers=headers) as response:
                    content = await response.text()

            if status:
                await status.end(f"Fetched {len(content)} bytes from {url}")

            return {
                "status": "success",
                "content": content,
                "url": url,
                "status_code": response.status
            }
        except Exception as e:
            if status:
                await status.error(f"Failed to fetch {url}: {e}")
            logger.error(f"fetch_url failed: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "request_id": request_id
            }

    async def parse_html(self, params: dict) -> dict:
        """
        Parse HTML content.

        Another tool method - also automatically routed by generic dispatcher.
        """
        html = params["html"]

        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, 'html.parser')

            return {
                "status": "success",
                "title": soup.title.string if soup.title else None,
                "text": soup.get_text(strip=True)
            }
        except Exception as e:
            logger.error(f"parse_html failed: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

PLUGIN_FACTORY = WebScrapingServer
```

**How the Generic Dispatcher Works:**

1. Agent calls tool: `await server.call_with_status("web_scraper_fetch_url", {"url": "https://example.com"})` — this opens the status scope and injects `_status`
2. `SchemaBasedToolMixin.call()` receives the request
3. `_get_method_name()` maps the tool name to a method name
4. Automatically invokes `self.fetch_url(params)` (sync or async)
5. Returns result to caller

**No manual `call()` override needed!** The `SchemaBasedToolMixin` base class handles all routing automatically.

**Method Naming Rule** (`SchemaBasedToolMixin._get_method_name`, for every schema-based server and agent):
- Tool name `{{ name }}_fetch_url` (rendered `web_scraper_fetch_url`) → method `fetch_url`
- Tool name exactly `{{ name }}` → method `execute` (for an `Agent` subclass: `Agent.call`, which runs a task)
- Any other tool name → the method of the same name
- Always prefix tool names with `{{ name }}_`: two instances with equal tool names collide silently

**Architecture Note:**
Both `SchemaBasedToolServer` and `SchemaBasedAgent` inherit from `SchemaBasedToolMixin`, which provides:
- `get_tools()` - loads from schema.yaml with caching
- `call()` - generic dispatcher with customizable routing
- `_get_method_name()` - override to customize tool → method mapping
- `get_schema_data()` - access full schema (not just tools)
- `clear_schema_cache()` - development/testing utility

### Method 2: Web-Only Plugin (Schema-Based Routing)

For plugins that only provide web endpoints (no tools), you can use **schema-based routing** for cleaner, more maintainable code:

```python
# src/plugins/my_dashboard/web_endpoints.py
"""Web-only plugin - provides dashboard endpoints using schema-based routing"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, FileResponse
from pathlib import Path
from agent_system.plugins.schema_router import create_schema_router
from agent_system.ui.resources import ui_templates

class DashboardWebEndpoints:
    """Web endpoints for dashboard plugin"""

    def __init__(self, name: str, schema: dict):
        self.name = name
        self.schema = schema
        # the plugin's own templates first, then the UI kit (kit/panel_base.html)
        self.templates = ui_templates(Path(__file__).parent / "templates")

    def get_web_router(self) -> APIRouter:
        """Return FastAPI router generated from web_ui.endpoints"""
        return create_schema_router(
            plugin_name=self.name,
            schema=self.schema,
            handler_class=self,
        )

    # Handler methods (called by schema router)

    async def dashboard_home(self, request: Request) -> HTMLResponse:
        """Main dashboard page"""
        return self.templates.TemplateResponse(
            request,
            "dashboard.html",
            {"plugin_name": self.name}
        )

    async def get_metrics(self, request: Request) -> dict:
        """API endpoint for metrics data"""
        return {
            "cpu_usage": 45.2,
            "memory_usage": 67.8,
            "disk_usage": 23.1,
            "active_processes": 156
        }

    async def get_status(self, request: Request) -> dict:
        """System status endpoint"""
        return {"status": "healthy", "uptime": "2d 14h 23m"}

    async def serve_static(self, request: Request, file_path: str) -> FileResponse:
        """Serve static assets (CSS, JS, images)"""
        from fastapi import HTTPException

        static_dir = Path(__file__).parent / "static"
        file_full_path = static_dir / file_path

        if not file_full_path.exists():
            raise HTTPException(status_code=404)

        return FileResponse(file_full_path)

# src/plugins/my_dashboard/plugin.py
from .web_endpoints import DashboardWebEndpoints
from agent_system.config import AgentSystemConfig, ToolServerConfig
from agent_system.plugins.web_base import SchemaBasedPluginWebInterface

class DashboardPlugin(SchemaBasedPluginWebInterface):
    """Web-only plugin (no tool server). The base class loads schema.yaml and
    provides get_schema_data(), from which the registry reads web_ui."""

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig):
        super().__init__(name, system_config, server_config)
        self.web_endpoints = DashboardWebEndpoints(name, self.get_schema_data())

    def get_web_router(self):
        return self.web_endpoints.get_web_router()

PLUGIN_FACTORY = DashboardPlugin
```

**Plugin Configuration:**
```toml
# plugin.toml
[plugin]
name = "my_dashboard"
version = "1.0.0"
description = "System dashboard web interface"
type = ["web"]
entrypoint = "plugin:PLUGIN_FACTORY"
```

**Schema with Endpoints (schema.yaml):**
```yaml
# Schema-based routing configuration
name: "{{ name }}"
version: "1.0.0"
description: "System dashboard web interface"

# Web UI configuration
web_ui:
  panel:
    endpoint: "/plugins/{{ name }}/"
    title: "System Dashboard"
    description: "Real-time system monitoring and metrics"
    icon: gauge
    category: system
    keywords: [metrics, monitoring]

  # Define all endpoints with handlers
  endpoints:
    - path: "/"
      method: "GET"
      handler: "dashboard_home"
      response_type: "html"
      description: "Main dashboard page"

    - path: "/api/metrics"
      method: "GET"
      handler: "get_metrics"
      response_type: "json"
      description: "System metrics data"

    - path: "/api/status"
      method: "GET"
      handler: "get_status"
      response_type: "json"
      description: "System status information"

    - path: "/static/{file_path:path}"
      method: "GET"
      handler: "serve_static"
      response_type: "response"
      description: "Serve static assets"
```

> **Note:** For more details on schema-based routing, see [Schema-Based Web Routing](./schema_based_web_routing.md).

#### Legacy Manual Routing (Not Recommended)

If you need to manually define routes without the schema system, you can still use traditional FastAPI decorators:

```python
def get_web_router(self) -> APIRouter:
    """Traditional manual routing (legacy approach)"""
    router = APIRouter(prefix=f"/plugins/{self.name}")

    @router.get("/", response_class=HTMLResponse)
    async def dashboard_home(request: Request):
        return self.templates.TemplateResponse(...)

    return router
```

However, **schema-based routing is preferred** because:
- Single source of truth (schema.yaml)
- Automatic validation at startup
- Better documentation
- Cleaner code separation
- Easier to maintain and test

**Accessible at:** `http://localhost:8000/plugins/my_dashboard/`

**Web-Only Plugin Features:**
- Provides web endpoints at `/plugins/<name>/`
- Can serve static assets, templates, APIs
- Declares its panel as `web_ui.panel`; the launcher and the command palette list it
- No tool server or tools required
- Uses `web_ui` schema for interface configuration
- Can still have CLI support

### Method 3: Hybrid Plugin (tools + Web Endpoints)

For plugins that provide both tools and web interfaces:

```python
from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.plugins.web_adapter import PluginWebInterface
from agent_system.config import AgentSystemConfig, ToolServerConfig
from fastapi import APIRouter
from pathlib import Path

class MyToolServer(SchemaBasedToolServer):
    """tool server component with modern signature."""

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig):
        super().__init__(name, system_config, server_config)

    # Tool methods - automatically routed by generic dispatcher
    async def my_tool(self, params: dict) -> dict:
        """Tool method matching 'my_tool' in schema.yaml."""
        return {"status": "success", "result": "tool result"}

class MyWebEndpoints(PluginWebInterface):
    """Web endpoints component with modern signature."""

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig):
        self.name = name
        self.system_config = system_config
        self.server_config = server_config

    def get_web_router(self) -> APIRouter:
        """Return FastAPI router with custom endpoints"""
        router = APIRouter(prefix=f"/plugins/{self.name}")

        @router.get("/status")
        async def get_status():
            return {"status": "active", "plugin": self.name}

        @router.get("/dashboard")
        async def dashboard():
            # Serve custom web UI
            return {"message": "Custom dashboard here"}

        return router

class MyHybridPlugin:
    """Hybrid plugin combining tools and web capabilities."""

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig):
        """Modern constructor signature for hybrid plugins."""
        self.tool_server = MyToolServer(name, system_config, server_config)
        self.web_endpoints = MyWebEndpoints(name, system_config, server_config)

    # tool interface delegation
    async def call(self, tool: str, params: dict):
        """Delegate to tool server - generic dispatcher handles routing."""
        return await self.tool_server.call(tool, params)

    def get_tools(self):
        """Delegate to tool server for tool discovery."""
        return self.tool_server.get_tools()

    # Web interface delegation
    def get_web_router(self):
        """Delegate to web endpoints for router."""
        return self.web_endpoints.get_web_router()

    def get_schema_data(self):
        """Delegate to tool server: the panel catalogue reads web_ui.panel from this schema."""
        return self.tool_server.get_schema_data()

PLUGIN_FACTORY = MyHybridPlugin
```

### Modern Plugin Pattern Summary

**Key Changes from Legacy Pattern:**

1. **Constructor Signature**
   - ✅ Modern: `(name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig)`
   - ❌ Legacy: `(name: str, config: dict, ssl_verify: bool = True)`

2. **Tool Routing**
   - ✅ Modern: Implement methods matching tool names, let generic dispatcher route
   - ❌ Legacy: Override `call()` with manual if/elif routing logic

3. **Type Hints**
   - ✅ Modern: Use `| None` for optional types
   - ❌ Legacy: Use `Optional[]` from typing module

4. **Configuration Access**
   - ✅ Modern: Extract from `server_config` (plugin-specific) and `system_config` (system-wide)
   - ❌ Legacy: Access `self.config` dictionary

5. **Method Naming**
   - ✅ Modern: Tool `{{ name }}_x` in `schema.yaml` → method `x` (routed by `SchemaBasedToolMixin`)
   - ❌ Legacy: Private methods with manual routing (e.g., `async def _fetch_url(self, params)`)

**Benefits:**
- **Less Boilerplate**: No need for 20+ lines of if/elif routing code
- **Type Safety**: Modern type hints with better IDE support
- **Clear Configuration**: Separation between system and plugin config
- **Automatic Routing**: Generic dispatcher eliminates routing bugs
- **Easier Testing**: Test methods directly, no routing layer to mock

**Example Comparison:**

```python
# ❌ Legacy Pattern (Don't use)
class OldPlugin(SchemaBasedToolServer):
    def __init__(self, name, config, ssl_verify=True):
        super().__init__(name, config, ssl_verify)
        self.timeout = self.config.get("timeout", 30)

    async def call(self, tool: str, params: dict):
        if tool == "fetch_url":
            return await self._fetch_url(params)
        elif tool == "parse_html":
            return await self._parse_html(params)
        else:
            return {"status": "error", "error": f"Unknown tool: {tool}"}

    async def _fetch_url(self, params: dict):
        # Implementation...
        pass

# ✅ Modern Pattern (Use this)
class ModernPlugin(SchemaBasedToolServer):
    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig):
        super().__init__(name, system_config, server_config)
        self.timeout = getattr(server_config, "timeout", 30)

    # No call() override needed - generic dispatcher handles routing!

    async def fetch_url(self, params: dict) -> dict:
        """Tool "{name}_fetch_url" - automatically routed."""
        # Implementation...
        pass

    async def parse_html(self, params: dict) -> dict:
        """Another tool - also automatically routed."""
        # Implementation...
        pass
```

**Critical Rules:**
1. Tool `{{ name }}_x` routes to method `x` — for `SchemaBasedToolServer` and `SchemaBasedAgent` alike
2. No manual `call()` override - `SchemaBasedToolMixin` handles routing automatically
3. Read config with `getattr(server_config, "key", default)` (flat keys) or `getattr(server_config, "config", None) or {}` (nested block) — `server_config` is not a dict
4. Use modern type hints (`| None` not `Optional[]`)

**SchemaBasedToolMixin Architecture:**
Both `SchemaBasedToolServer` and `SchemaBasedAgent` inherit from `SchemaBasedToolMixin` for shared functionality:
- Schema loading and caching
- Generic call dispatcher
- Template variable support
- Development utilities

This eliminates code duplication and ensures consistent behavior across simple tools and intelligent agents.

## Agent-Based Plugins

For plugins that need full agent execution capabilities (conversation, LLM integration, multi-turn interactions), inherit from the `Agent` or `SchemaBasedAgent` class instead of implementing tool server interfaces manually.

> **📖 See Also:** [Agent Architecture Guide](_arch_agent_architecture.md) for detailed explanation of `Agent` vs `SchemaBasedAgent` base classes and when to use each.

### When to Use Agent Plugins

Agent-based plugins are ideal for:
- Multi-step reasoning tasks
- Complex workflows requiring conversation history
- Tasks needing LLM integration (planning, summarization, code generation)
- Interactive capabilities with back-and-forth communication
- Execution of other agent tools within the plugin

### Creating an Agent Plugin

**File Structure:**
```
src/plugins/my_agent/
├── plugin.toml          # Metadata
├── schema.yaml          # Tool definitions
├── server.py           # Agent class implementation
└── plugin.py           # Factory function
```

**1. Plugin Metadata (`plugin.toml`):**
```toml
[plugin]
name = "my_agent"
version = "1.0.0"
description = "Agent-based plugin for complex tasks"
type = ["tool-server"]
entrypoint = "plugin:PLUGIN_FACTORY"
requires = { agent_system = ">=0.6.0" }
dependencies = []   # pip specs only, never agent_system or other plugins
```

**2. Tool Schema (`schema.yaml`):**
```yaml
tools:
  - type: function
    function:
      name: "{{ name }}_execute_task"
      description: "Execute a complex task using agent capabilities"
      parameters:
        type: object
        properties:
          task:
            type: string
            description: "Task description for the agent to execute"
          context:
            type: string
            description: "Optional context for the task"
        required: ["task"]
        additionalProperties: false

  - type: function
    function:
      # not "{{ name }}_list_tools": it would route to ToolServer.list_tools()
      name: "{{ name }}_list_available_tools"
      description: "List the tools this agent may use"
      parameters:
        type: object
        properties: {}
        additionalProperties: false
```

**3. Agent Implementation (`server.py`):**

> **💡 Choosing the Right Base Class:**
> - Use `SchemaBasedAgent` (recommended) for agents with declarative `schema.yaml` tool definitions
> - Use `Agent` only if tools require runtime generation or complex logic
> - See [Agent Architecture Guide](_arch_agent_architecture.md#when-to-use-each-base-class) for decision guide

```python
# SchemaBasedAgent (recommended for most cases)
from agent_system.servers.agent.schema_based import SchemaBasedAgent
from agent_system.servers.agent.result_utils import collect_final_result


class MyAgent(SchemaBasedAgent):
    """Agent with schema.yaml tool definitions (automatically loaded)."""

    def __init__(self, name, system_config, server_config, registry=None, **kwargs):
        # Agent.__init__(name, system_config, server_config, registry=None,
        #                llm=None, llm_factory=None, session_service=None)
        super().__init__(name, system_config, server_config, registry, **kwargs)

    # Tool "{name}_execute_task" -> method execute_task (prefix stripped)
    async def execute_task(self, params: dict) -> dict:
        """Execute complex task using agent capabilities."""
        task = params.get("task")
        if not task:
            return {"status": "error", "error": "Missing required parameter 'task'"}
        context = params.get("context", "")
        prompt = f"{task}\n\nContext: {context}" if context else task

        # Never on the caller's session: on one of this agent's own below it
        # (tool_session). Refused -- nothing ran -- when it is another user's
        # ("foreign_session"), when this agent runs above the call already
        # ("recursive_call"), or when it cannot be stored with the caller's
        # sub-agent budget ("tool_session_unavailable"). A session that cannot be
        # read at all raises, as any other failure of the call.
        session_id, refusal = await self.tool_session(params)
        if refusal:
            return {"status": "error", **refusal}  # "error" and "error_type"

        # Runs the agent loop (LLM + allowed tools) and returns the final result
        result = await collect_final_result(
            self, prompt,
            request_id=params.get("request_id") or params.get("_request_id"),
            session_id=session_id,
        )
        if result.get("refused"):  # refused at its start: another call runs on the session
            return {"status": "error", "error": (result.get("errors") or ["refused"])[-1],
                    "error_type": result["refused"]}
        return {"status": "success", "result": result}

    # Tool "{name}_list_available_tools" -> method list_available_tools
    async def list_available_tools(self, params: dict) -> list[dict]:
        """List the tools this agent may use."""
        return await self._list_usable_tools_with_details(params)
```

A plain `Agent` subclass (no `schema.yaml`) is exposed as one tool named after
the instance; its `call()` takes `task` / `query` / `prompt` and runs the agent.
Use it only when the tools have to be generated at runtime — then override
`list_tools()` (or `get_schema()`) and `call()` yourself — a `get_tools()`
override is only read when `list_tools()` returns nothing.

**4. Factory Function (`plugin.py`):**

Normally one line — the helper produces the factory bootstrap expects:

```python
from agent_system.plugins.factory_utils import make_agent_plugin_factory
from .server import MyAgent

PLUGIN_FACTORY = make_agent_plugin_factory(MyAgent)
```

Write it by hand only when the factory has to decide something (e.g. return a
plain tool server for one configuration and an agent for another). Then keep
the signature bootstrap calls, and take the SHARED registry:

```python
from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.tools.base import ToolServerRegistry
from .server import MyAgent


def PLUGIN_FACTORY(name: str, system_config: AgentSystemConfig,
                   server_config: ToolServerConfig, registry: ToolServerRegistry | None = None) -> MyAgent:
    """Create the agent plugin instance."""
    return MyAgent(name, system_config, server_config,
                   registry if registry is not None else ToolServerRegistry())


# Tells bootstrap to pass registry= at all. Without it the agent gets a private,
# empty registry, and the ToolExecutionManager built in __init__ keeps that one
# -- assigning inst.registry afterwards is too late.
PLUGIN_FACTORY._accepts_registry = True
```

`server_config` is the MERGED server config (plugins.default_config plus the
`type:` chain), not the raw entry — the same one every other agent gets.

### Agent vs Schema-Based Plugins

| Aspect | Agent Plugin | Schema-Based Plugin |
|--------|-------------|-------------------|
| **Base Class** | `Agent` or `SchemaBasedAgent` | `SchemaBasedToolServer` |
| **Complexity** | High - full agent capabilities | Low - simple tool execution |
| **LLM Access** | ✅ Built-in conversation | ❌ Manual integration needed |
| **Multi-turn** | ✅ Conversation history | ❌ Stateless calls |
| **Tool Access** | ✅ Can use other agent tools | ❌ Limited to own tools |
| **Use Cases** | Complex reasoning, planning | Simple utilities, API calls |

> **📚 For More Details:** See [Agent Architecture Guide](_arch_agent_architecture.md) for comprehensive comparison of `Agent`, `SchemaBasedAgent`, and `SchemaBasedToolServer`.

### Agent Plugin Best Practices

1. **Status Updates**: Use `status.progress()` for long-running tasks and close with one `status.end()` / `status.error()`
2. **Cancellation**: Check `token.is_cancelled` (a property) in loops
3. **Error Handling**: Wrap agent calls in try/except blocks
4. **Resource Management**: Properly clean up agent resources
5. **Tool Naming**: Use descriptive tool names with the `{{ name }}_` prefix
6. **Factory**: Always `make_agent_plugin_factory(Cls)` — it sets `_accepts_registry`, so the agent gets the shared registry

### Configuration Requirements

An agent instance is a server entry (see the example under
[Alternative: Configuration-Based Agents](#alternative-configuration-based-agents)):

- **`enabled: true`** — the default is `false`, and it is checked on the entry itself, not inherited.
- **`metadata.visibility`** — `private` (default: neither tool nor UI), `tool`
  or `both` (callable as a tool by other agents), `ui` or `both` (listed in the UI).
  Display only: it hides an agent, it does not stop anyone who knows its name.
  Called as a tool, an agent runs on a session of its own per caller session
  (`Agent.tool_session`: `<caller session>--<agent>-<digest>`), not on the caller's:
  it remembers its earlier calls in that caller session, is saved under the call's
  user below the caller's session (hidden from the session list, like a sub-agent's),
  and a throwaway caller's is never saved and leaves memory with the caller's session
  (if the agent starts sub-agents, the sub-agent manager files it as the listed
  "Coordinator Session" it makes for any parent it does not find). A call to an
  agent that runs above it already -- itself, directly or through other agents called
  as tools -- is refused with `error_type: "recursive_call"`; across a SAM or
  stategraph hop the sub-agent nesting budget bounds it instead (a stategraph agent
  activity only where a SAM above set one). A call whose session cannot be stored
  with the caller's sub-agent budget is refused (`"tool_session_unavailable"`). A
  session the person deleted (`DELETE /sessions/<id>`) is forgotten: the next call
  starts it afresh. An `execute_task` of your own gets that session, or the
  refusal, from `await self.tool_session(params)` (see the example above); a run
  refused at its start -- a second call while the first still runs on the session
  -- comes back from `collect_final_result` with `refused` set to its error_type,
  and is answered as an error with it, as `Agent.call` does.
- **`metadata.min_role`** — `guest`, `user` or `admin`: the lowest account role that
  may *run* the agent, on every path (HTTP, SAM, agent as a tool, stategraph, wakes,
  each of its `<name>_*` tools); absent = no gate. Give `admin` to every agent that
  carries a shell, a coding CLI, SSH, a tool that runs arbitrary code
  (`blender_execute`, `godot_script`), or file access to the checkout or above, to
  `config/`, to `data/` itself (the user store and every user's sessions live there;
  a folder of its own below it, such as `data/workspace`, is fine) or write access to
  `src/` (the code that runs). Details:
  [agent_visibility.md](agent_visibility.md#wer-darf-einen-agent-ausführen-metadatamin_role).
  - **No identity:** a run without a registered request owner or session user is
    judged as `anonymous` -- refused unless anonymous access is enabled with a
    sufficient role; the SAM and the agent's own tools refuse it outright. Unknown
    or inactive accounts are refused. `cli_user` counts as the local operator only
    inside `agent-cli`/`agent-run`, never in the API.
  - **Inherited through `type:`:** `metadata` is deep-merged along the type chain, so an
    agent based on a gated one is gated too. `min_role: null` does **not** lift an
    inherited value -- set a lower role explicitly (`guest`/`user`).
  - Without `auth.enabled` there are no roles; the server logs the gates it cannot
    enforce at start.
- **`agent_config.tools.allowed`** — the tools this agent may call; empty means none.
- **`self_tool_descriptions`** — server level, not inside `agent_config`
  (`agent_config` rejects unknown keys at load).

**Sub-agents** (spawned through a `sub_agent_manager` instance, the SAM) need in addition:

1. If the **calling** SAM instance sets `allowed_agents` (default `['*']` = all),
   the instance name listed there (exact name or fnmatch glob).
2. `visibility` other than `private`, or the agent is missing from that list.
3. The caller allows the SAM instance: `tools.allowed: ["<sam instance>/*"]`.
4. The calling run's user passes the sub-agent's `metadata.min_role`, if it has one;
   otherwise `create`/`continue` answer `error_type: "agent_role_gate"` before any
   sub-session exists.
5. SAM settings (`allowed_agents`, `blocked_agents`, `allow_advanced_model`, …)
   are **top-level keys** of the SAM server entry, not under `config:`.

**Servers a plugin offers without a config entry.** A plugin factory may carry
`offered_servers(system_config) -> {name: entry}`, where `entry` is the mapping a
`plugins.servers` entry would hold. Every process that builds a `Runtime` asks it once, after
the configured servers and before anything is built, and declares each one with
`Runtime.declare(name, entry, offered_by=<plugin type>)`: it enters `config.plugins.servers`
(so lookups by name find it) and is built at start like a configured server; its
`ServerDecl.offered_by` names the plugin. A name the config holds is refused and listed in
`Runtime.problems`; an offer that raises costs its servers, not the start; a config reload keeps
the offered entries (`Runtime.carry_offered`). Example: `stategraph_machine` offers every machine
whose file has an `agent:` block.

### Plugin Types Summary

| Plugin Type | Tool server | Web Endpoints | CLI | Use Cases |
|-------------|------------|---------------|-----|-----------|
| **Agent** | ✅ Required (Agent class) | Optional | Optional | Complex reasoning, multi-turn tasks |
| **Tool-only** | ✅ Required | Optional | Optional | Agent tools, API integrations |
| **Web-only** | ❌ None | ✅ Required | Optional | Dashboards, monitoring, admin tools |
| **Hybrid** | ✅ Required | ✅ Required | Optional | Full-featured plugins (like log_viewer) |
| **Hooks-only** | ❌ None | Optional | Optional | Context management, logging, validation |
| **Library (config-only)** | ❌ None | ❌ None | ❌ None | Agent YAMLs, prompts, skills — no entrypoint |

### Server Interface Requirements (tool plugins only)

For plugins that provide tools, your server class must implement:

1. **Constructor**: `__init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig)`
2. **Tool Methods**: Tool `{{ name }}_fetch_url` → `async def fetch_url(self, params: dict)`
3. **Tool Discovery**: Inherit from `SchemaBasedToolServer` for automatic `list_tools()` or implement manually

**No `call()` override needed** - `SchemaBasedToolMixin.call()` routes tool calls to the matching methods.

### Runtime Parameters

The framework injects these parameters into `params`:

- **`_status`**: StatusScope for progress updates (injected by `call_with_status`)
- **`_request_id`**, **`_session_id`**, **`_user_id`**, **`_agent_name`**: caller context
- **`_agent`**: the calling Agent instance
- **`_cancellation_token`**: CancellationToken for cooperative cancellation — only when the call carries a request id
- **`request_id`/`requestId`**: per-tool request id for correlation and logging (set by the framework; a model-supplied value is dropped)

Model-supplied parameters starting with `_` or named `request_id`/`requestId`
are dropped, so never declare a tool parameter with those names.
In tests, the CLI and `dispatch_tool_call` some of these are absent — always use
`params.get(...)`.

```python
async def my_tool(self, params: dict):
    # Extract runtime parameters
    status = params.get("_status")
    token = params.get("_cancellation_token")
    request_id = params.get("request_id") or params.get("requestId")

    # Extract tool parameters
    user_input = params.get("input")  # from schema.yaml

    # Your implementation...
```

## Tools and Parameters

### Parameter Validation

The framework does no JSON-schema validation of tool arguments — it only rejects
malformed JSON. `required`, `enum` and ranges in `schema.yaml` are hints to the
model, so validate inputs before processing:

```python
async def my_tool(self, params: dict):
    # Validate required parameters
    if "query" not in params:
        return {"status": "error", "error": "Missing required parameter: query"}

    query = params["query"]
    if not isinstance(query, str) or not query.strip():
        return {"status": "error", "error": "Query must be a non-empty string"}

    # Optional parameters with defaults
    limit = params.get("limit", 10)
    if not isinstance(limit, int) or limit < 1:
        return {"status": "error", "error": "Limit must be a positive integer"}
```

### Response Format

Always return structured responses:

```python
# Success response
return {
    "status": "success",
    "data": {...},
    "metadata": {
        "request_id": request_id,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
}

# Error response
return {
    "status": "error",
    "error": "Detailed error message",
    "error_type": "INVALID_INPUT",  # Optional; forwarded as status meta
    "request_id": request_id
}

# Cancelled response (the shape the framework itself produces)
return {
    "error": "Tool 'my_tool' was cancelled.",
    "cancelled": True,
    "forced": token.is_forced if token else False
}
```

The result reaches the model as JSON. If a handler returns an error without
reporting it on `_status`, `call_with_status` closes the status line as an error
itself — it recognizes `"status": "error"`, an `error` key without `status`, and
`"success": False` together with `error`. `{"status": "failed"}` or
`{"success": False}` without `error` show up as "completed".

## Model Experience (in the plugin's guide)

A plugin's real interface is not its Python signature — it is **what the model
sees**, and **what that costs**. Both have repeatedly been reconstructed by
hand during reviews because nobody wrote them down. Three short sections in
your plugin's guide (`<folder>.guide`; the `README.md` until it has one) remove
that guesswork -- the README itself stays a short overview. They are required for new plugins;
retrofit an existing plugin only when you are already editing it. No test
enforces this — reviews do.

**Scope:** plugins whose tools a model calls. A package that exposes no tools
to a model — `type = ["llm-provider"]` (the LLM clients under
`src/plugins/`) or `type = ["library"]` — has no model-facing surface to
describe, and these three sections do not apply to it. Its guide still owes
the ordinary things: what it provides, how to configure it, and the gotchas.

### 1. What the model sees

The literal text that reaches the model — tool descriptions are obvious, but
the ones that get forgotten matter more: **error strings**, injected context,
and any notice that replaces a result. Quote them verbatim, don't paraphrase;
the exact wording is the contract, and a reviewer must be able to compare it
against the code.

```markdown
### What the model sees

Normal result: `{"status": "ok", "matches": [...]}`.

On a path outside `allowed_directories`, verbatim:

    Path is outside the allowed directories. Allowed: <list>.

Nothing else from this plugin enters the context.
```

### 2. Token and cache effect

State whether the plugin changes anything **before** the end of the request —
system prompt, tool list, injected context, or an existing message. That is
the question that decides whether it breaks the provider's prompt cache, and a
cache break costs far more than the bytes it saves.

The three honest answers:

| Answer | Meaning |
|---|---|
| **Append-only** | New content lands after the reusable prefix. No invalidation. |
| **Prefix-changing** | Alters system prompt, tool schemas, or an earlier message — invalidates everything after it. Say WHEN it happens and how often. |
| **None** | The plugin contributes nothing to the model context at all. |

```markdown
### Token and cache effect

Append-only. Results are added at the end of the conversation; the plugin
never rewrites an existing message. Per call ~200 tokens, dominated by the
match list.
```

### 3. Known gaps

What the plugin deliberately does NOT do, and why. This is the same rule the
codebase follows elsewhere: naming a gap is a decision; leaving it unnamed is
an accident waiting to be rediscovered. If a limit is deliberate, its reason
belongs here — otherwise the next person "fixes" it and reintroduces the
problem it was avoiding.

```markdown
### Known gaps

- Symlinks are resolved before the containment check, so a link INTO an
  allowed directory is followed. Deliberate: the alternative rejects ordinary
  working setups.
- No quota — a caller can fill the allowed directory. The boundary is who
  gets the tool, not how much they write.
```

## Advanced Features

### Status and Progress Reporting

Use the `_status` parameter to provide real-time feedback:

```python
async def long_running_tool(self, params: dict):
    status = params.get("_status")
    items = params.get("items") or []
    results = []

    if status:
        await status.progress("Starting analysis...")

    # Do some work
    for i, item in enumerate(items):
        if status and i % 10 == 0:
            await status.progress(f"Processing item {i+1}/{len(items)}")

        # Process item...

    if status:
        await status.end(f"Analyzed {len(items)} items, {len(results)} findings")

    return {"status": "success", "results": results}
```

**Status Methods** (`StatusScope` in `agent_system/tools/status.py` has exactly these three):
- `await status.progress(message, meta=None)` - Progress updates
- `await status.end(message, meta=None)` - Close the scope with a result line
- `await status.error(message, meta=None)` - Close the scope as failed

**Best Practices:**
- Exactly one closing `end` or `error` per call
- The end line names the result (counts, ids, titles), at most 140 characters —
  the WebUI shows only that line. `tests/plugins/test_status_end_lines.py`
  checks this, including an AST scan of every `.end("…")`/`.error("…")` in `src/plugins`
- Don't spam with too many updates (batch them)
- Always return a final result; status is supplementary

### Cooperative Cancellation

Nothing enforces cancellation support. It is needed for tools with long loops,
network calls or subprocesses; a short tool can ignore the token.

#### Why Cancellation Matters
- Users expect "Stop" button to work reliably
- Prevents resource leaks and stuck operations
- Maintains system responsiveness

#### CancellationToken API

`agent_system.core.cancellation.CancellationToken` — `is_cancelled` and
`is_forced` are properties.

```python
token = params.get("_cancellation_token")   # None when the call has no request id

# Check if cancellation was requested
if token and token.is_cancelled:
    return {"error": "Tool 'my_tool' was cancelled.", "cancelled": True,
            "forced": token.is_forced}
```

Return the cancelled dict; don't raise `CancellationError` from a tool — like any
exception it reaches the model as a plain `{"error": str(e)}`. After
`tool_cleanup_timeout` the cancellation manager cancels the task hard.

#### Basic Cancellation Pattern

```python
async def long_operation(self, params: dict):
    token = params.get("_cancellation_token")
    cancelled = {"error": "Tool 'long_operation' was cancelled.", "cancelled": True, "forced": False}

    # Early exit if already cancelled
    if token and token.is_cancelled:
        return cancelled

    # Register cleanup
    async def cleanup():
        # Close files, connections, etc.
        await self._close_resources()

    if token:
        token.add_cleanup_callback(cleanup)

    try:
        # Do work in chunks, check cancellation frequently
        for i in range(100):
            # Check cancellation before each chunk
            if token and token.is_cancelled:
                await token.cleanup()  # Run cleanup callbacks
                return cancelled

            # Do a small amount of work
            await self._process_chunk(i)
            await asyncio.sleep(0.1)  # Yield control

        return {"status": "success", "processed": 100}

    finally:
        # Remove cleanup callback
        if token:
            try:
                token.remove_cleanup_callback(cleanup)
            except Exception:
                pass
```

#### Advanced: Background Tasks

Register long-running background tasks for force-cancellation:

```python
from agent_system.core.cancellation import get_cancellation_manager

async def tool_with_background_tasks(self, params: dict):
    token = params.get("_cancellation_token")
    request_id = params.get("request_id")
    cancelled = {"error": "Tool 'tool_with_background_tasks' was cancelled.",
                 "cancelled": True, "forced": False}

    # Create background task
    task = asyncio.create_task(self._background_worker())

    # Register with cancellation manager (id prefixed with the tool's request id)
    if request_id:
        get_cancellation_manager().register_task(f"{request_id}_bg", task)

    # Wait for task or cancellation
    while not task.done():
        if token and token.is_cancelled:
            task.cancel()
            return cancelled
        await asyncio.sleep(0.1)

    result = await task
    return {"status": "success", "result": result}
```

#### Cancellation Best Practices

1. **Check early and often** - Before starting work and in loops
2. **Keep cleanup short** - Cleanup callbacks should be < 1 second
3. **Use chunks** - Break long operations into small pieces
4. **Register background tasks** - So they can be force-cancelled
5. **Test cancellation** - Include cancellation tests in your test suite

### Web Endpoints and UI Integration

Plugins can provide custom web interfaces and API endpoints accessible at `/plugins/<plugin_name>/`. This pattern allows plugins like `log_viewer` to serve web dashboards, APIs, and static assets:

#### Panels in the WebUI

The WebUI finds panels through one catalogue: the shell loads `GET /api/ui/catalog`,
which lists the core panels plus the `web_ui.panel` block of every registered web
plugin, filtered by the viewer's role. Users open a panel from the launcher (grid
button in the header), the command palette (Ctrl+K) or a context link in the chat.
It opens as a tab docked beside the chat and can be detached into a floating window;
every panel runs in an iframe.

```yaml
web_ui:
  panel:
    endpoint: "/plugins/{{ name }}/"
    title: "My Plugin"
    icon: wrench
    category: agents
```

A plugin's panel appears when:

1. its `schema.yaml` has a valid `web_ui.panel` block (fields: [Web UI Fields Reference](#web-ui-fields-reference)),
2. the plugin is registered as a web plugin, i.e. it provides `get_web_router()`,
3. the registered plugin object returns that schema from `get_schema_data()`.
   `SchemaBasedPluginWebInterface` and `SchemaBasedToolServer` provide it; a wrapper
   delegates to the component that owns the schema. Without it the catalogue sees no
   `web_ui` block, and the panel is missing without an error.

Check it with `python src/scripts/validate_plugin.py src/plugins/my_plugin` (the panel
block goes through the catalogue's own parser) and with `GET /api/ui/catalog` on the
running API. A panel block that does not parse is left out of the catalogue with an
error log.

#### Basic Web Endpoints

```python
from agent_system.plugins.web_adapter import PluginWebInterface
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, HTMLResponse

class MyWebEndpoints(PluginWebInterface):
    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config

    def get_web_router(self) -> APIRouter:
        """Define custom API endpoints"""
        router = APIRouter(prefix=f"/plugins/{self.name}")

        @router.get("/api/data")
        async def get_data():
            return {"data": "example", "plugin": self.name}

        @router.post("/api/action")
        async def perform_action(request: Request):
            data = await request.json()
            # Process action...
            return {"result": "success"}

        @router.get("/dashboard", response_class=HTMLResponse)
        async def dashboard():
            return "<h1>Custom Dashboard</h1><p>Plugin interface here</p>"

        return router
```

#### Static Assets and Templates

```python
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from agent_system.plugins.web_adapter import PluginWebInterface
from agent_system.ui.resources import ui_templates

class MyWebEndpoints(PluginWebInterface):
    def __init__(self, name: str, config: dict):
        self.name = name

        # Setup templates and static files
        self.templates_dir = Path(__file__).parent / "templates"
        self.static_dir = Path(__file__).parent / "static"
        # Searches the plugin's templates first, then the shared kit templates
        self.templates = ui_templates(self.templates_dir)

    def get_web_router(self) -> APIRouter:
        router = APIRouter(prefix=f"/plugins/{self.name}")

        # Serve static files (CSS, JS, images)
        @router.get("/static/{file_path:path}")
        async def serve_static(file_path: str):
            from fastapi import HTTPException
            from fastapi.responses import FileResponse

            file_full_path = self.static_dir / file_path
            if not file_full_path.exists():
                raise HTTPException(status_code=404)
            return FileResponse(file_full_path)

        # Template-based pages
        @router.get("/panel", response_class=HTMLResponse)
        async def panel(request: Request):
            return self.templates.TemplateResponse(
                request,
                "panel.html",
                {"plugin_name": self.name, "config": self.config}
            )

        return router

    def get_static_assets(self) -> Optional[Path]:
        """Return path to static assets directory"""
        return self.static_dir if self.static_dir.exists() else None
```

Panel templates build on the UI kit: they extend `kit/panel_base.html`
(`{% extends "kit/panel_base.html" %}`, blocks `title`, `toolbar`, `content`, `scripts`),
and panel scripts import from `/static/kit/panel-kit.js`. `ui_templates()` is what lets
a plugin template reach the kit templates. The component catalogue renders at `/ui/kit`;
the full guide is `.claude/skills/panel-authoring/SKILL.md`.

### CLI Support (Optional)

A CLI is optional — some plugins have one (`cli.py` or `__main__.py`, registered
under `[project.scripts]` in `pyproject.toml`). It helps
for development, testing, and standalone use:

#### CLI Structure

Create a `cli.py` file in your plugin directory:

```python
# src/plugins/my_plugin/cli.py
import asyncio
import json
import logging
from argparse import ArgumentParser, Namespace

from agent_system.config.settings import load_settings, get_tool_server_config
from .server import MyPluginServer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def run_tool(args: Namespace):
    """Execute plugin tool from command line"""
    system_config = load_settings()
    server_config = get_tool_server_config("my_plugin", system_config)
    server = MyPluginServer("my_plugin", system_config, server_config)

    params = {
        "input": args.input,
        # Add other parameters...
    }

    try:
        result = await server.call(args.tool, params)
        return result
    except Exception as e:
        logger.error(f"Tool execution error: {e}")
        return {"error": str(e)}

def create_parser() -> ArgumentParser:
    """Create command line parser"""
    parser = ArgumentParser(description="My Plugin CLI")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Tool command
    tool_parser = subparsers.add_parser("tool", help="Execute plugin tool")
    tool_parser.add_argument("tool", help="Tool name to execute")
    tool_parser.add_argument("--input", required=True, help="Input data")

    return parser

def main():
    """Main CLI entry point"""
    parser = create_parser()
    args = parser.parse_args()

    if args.command == "tool":
        result = asyncio.run(run_tool(args))
        print(json.dumps(result, indent=2))
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
```

#### pyproject.toml Entry Point

Register your CLI in the project's `pyproject.toml`:

```toml
[project.scripts]
# Add your plugin CLI executable
my-plugin-cli = "plugins.my_plugin.cli:main"

# Or in the existing plugin CLIs section:
tool-my-plugin = "plugins.my_plugin.cli:main"
```

This creates an executable that users can run:
```bash
# After installation, users can run:
my-plugin-cli tool my_tool --input "test data"

# Or with the mcp prefix:
tool-my-plugin tool my_tool --input "test data"
```

#### CLI Best Practices

- **Async support**: Use `asyncio.run()` for async operations
- **JSON output**: Return results as JSON for scripting
- **Error handling**: Catch exceptions and return error objects
- **Debug mode**: Support `--debug` flag for verbose logging
- **Help text**: Provide clear descriptions and examples
- **Configuration**: Allow CLI to override plugin config options

### Background Tasks

For spawning background tasks that should be cancelled:

```python
from agent_system.core.cancellation import get_cancellation_manager
import asyncio

async def tool_with_subtasks(self, params: dict):
    request_id = params.get("request_id")

    # Create subtasks
    manager = get_cancellation_manager()
    tasks = []

    for i in range(3):
        task = asyncio.create_task(self._subtask(i))
        if request_id:
            manager.register_task(f"{request_id}_sub{i:03d}", task)
        tasks.append(task)

    # A forced cancellation cancels this coroutine; let CancelledError propagate
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return {"status": "success", "results": results}
```

### Starting and Stopping (`start_plugin` / `stop_plugin`)

A plugin that opens anything -- a socket, a thread, a background task, a
vector store, a child process -- closes it in **`stop_plugin`**, and in no
other method. These two names are the whole lifecycle contract:

```python
class MyServer(SchemaBasedToolServer):
    async def start_plugin(self) -> None:
        """After registration, once. Optional."""
        self._worker = asyncio.create_task(self._pump())

    async def stop_plugin(self) -> None:
        """At app shutdown and on unregister. Optional -- but it is the ONLY
        teardown the framework calls."""
        self._worker.cancel()
        await self._store.close()
```

`plugins/capabilities.stop_plugin` looks up exactly one attribute and has **no
fallback**:

```python
hook = getattr(plugin, "stop_plugin", None)
if hook is None:
    return
```

so a teardown called `shutdown`, `close` or `cleanup` is never called at all,
and nothing says so -- no log, no error, the resources simply stay. Three
plugins had exactly that on 2026-09-21: `terminal` (`cleanup`) left every
background process with its capture task attached, `ssh_control` (`close`)
left remote commands and their SSH channels open, and `file_ops` (`shutdown`)
never released the semantic indexer or its vector store. At process exit the
OS takes all of it back; on a plugin reload inside a living process nobody
does.

Two more rules that follow from how the hook is reached
(`getattr(adapter, "plugin_server", adapter)` in `plugins/tool_adapter.py`):

* It must sit on the object **`PLUGIN_FACTORY` returns**. A hook on an inner
  helper -- a connection manager, a tool server the plugin wraps -- is never
  found. Forward from the outer object if the work lives inside.
* Exceptions are swallowed into a warning, so a hook that raises looks like a
  hook that worked. Test through `capabilities.stop_plugin` and assert that
  nothing was logged, not just that the first line ran.

* Release only what the plugin itself opened. The tool integration is the
  **process's** -- the entry point (app lifespan, `agent-cli`, `agent-run`,
  chat) ends it with `shutdown_tools()`. The `Agent` base class has no
  teardown at all, on purpose: an agent that stopped the integration would
  stop every plugin in the process, and one that cleared its session tracker
  would take the session locks from runs `shutdown_tools()` does not wait for.
  An agent-based plugin that opens something of its own adds `stop_plugin`
  for exactly that.

`tests/pluginsystem/test_pluginsystem_teardown_hook.py` reads every plugin's
factory class and fails if it has a teardown under any other name.

## Configuration and Deployment

### Plugin Configuration

Plugins are configured using the **`plugins:`** configuration key. The system automatically merges plugin configurations from multiple YAML files through `config/config.yaml`'s include mechanism.

**Configuration Structure:**

All plugin configurations must use the `plugins:` top-level key with this structure:

```yaml
plugins:
  # Plugin discovery directories
  plugin_dirs:
    - src/plugins*          # src/plugins and every src/plugins_<name>/, sorted

  # Default configuration inherited by all plugins
  default_config:
    enabled: false
    # ... default settings ...

  # Individual plugin configurations
  servers:
    my_plugin:            # instance name
      type: my_plugin     # plugin folder name
      enabled: true       # checked on this entry; default false
      config:
        # Plugin-specific settings (read via getattr(server_config, "config", None) or {})
        timeout: 30
        max_retries: 3
```

The framework does not validate plugin settings; the `config:` block in a
plugin's `schema.yaml` is read only for hook plugins (as defaults). For tool
servers, defaults belong in code.

**Where to Add Plugin Configurations:**

1. **Standard plugins**: Add to any file included by `config/config.yaml` (commonly in files under `config/` that are included)
2. **Specialized namespaces**: Create separate config files (e.g., `config/agents_research/plugin_configs.yaml`) that get auto-loaded via the wildcard include `agents*/*.yaml`

**Example - Standard Plugin Configuration:**

```yaml
# Any included config file (e.g., a file under config/ that's included in config.yaml)
plugins:
  servers:
    web_scraper:
      type: web_scraper
      enabled: true
      config:
        timeout: 30
        user_agent: "MyAgent/1.0"
        max_retries: 3
```

**Example - Specialized Namespace Configuration:**

```yaml
# config/agents_research/plugin_configs.yaml (auto-loaded via the agents*/*.yaml include)
# CRITICAL: Must have 'plugins:' wrapper to match the deep_merge structure!
plugins:
  servers:
    research_scraper:     # a second instance of web_scraper
      type: web_scraper
      enabled: true
      config:
        timeout: 60
```

**Important Rules:**

1. **Always use `plugins:` wrapper**: Without it, configs won't be merged correctly
2. **Configs are deep-merged**: Multiple files can contribute to the `plugins:` section
3. **Server configs must be under `plugins.servers`**: The `servers` key is required
4. **Don't reference specific files in code**: Use the `server_config` passed to the constructor, or `get_tool_server_config(name, config)` — the raw `config.plugins.servers[name]` shows pydantic defaults, not the inherited values


### Reading Configuration in Your Plugin

`server_config` is the merged server entry — a pydantic model with `extra="allow"`,
not a dict. Flat keys are attributes; a nested `config:` block is a dict attribute.

```python
import logging

from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)


class WebScraperServer(SchemaBasedToolServer):
    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig):
        super().__init__(name, system_config, server_config)

        # Nested `config:` block of the server entry
        config = getattr(server_config, "config", None) or {}
        self.timeout = float(config.get("timeout", 30))
        self.user_agent = config.get("user_agent", "AgentSystem/1.0")
        # Flat key directly on the server entry
        self.max_retries = int(getattr(server_config, "max_retries", 3))

        # Validate configuration
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")

        # Log effective configuration
        logger.info(
            f"WebScraping configured: timeout={self.timeout}, "
            f"user_agent={self.user_agent}, max_retries={self.max_retries}"
        )
```

### Environment Variables

For sensitive configuration, use environment variables:

```python
import os
from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.config import AgentSystemConfig, ToolServerConfig

class APIClientServer(SchemaBasedToolServer):
    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig):
        super().__init__(name, system_config, server_config)

        # Sensitive config from environment
        self.api_key = os.getenv("API_CLIENT_KEY")
        if not self.api_key:
            raise ValueError("API_CLIENT_KEY environment variable required")

        # Non-sensitive config from server_config
        self.base_url = getattr(server_config, "base_url", "https://api.example.com")
```

## Testing and Quality Assurance

### Test Structure

Tests live next to the plugin (see [Test Structure](#test-structure) above):
```
src/plugins/<plugin_name>/tests/
├── test_plugin_<plugin_name>_basic.py        # Basic functionality
├── test_plugin_<plugin_name>_integration.py  # Integration tests
├── test_plugin_<plugin_name>_cancellation.py # Cancellation behavior
└── test_plugin_<plugin_name>_config.py       # Configuration handling
```

`tests/plugins/` is for cross-plugin tests only. The root `conftest.py` replaces
`build_client` with a fake, so no real LLM calls happen; point caches and storage
at `tmp_path` (otherwise they write to the real `data/`).

### Basic Plugin Test

```python
# src/plugins/my_scraper/tests/conftest.py — shared by all test files below
import pytest

from agent_system.config.models import ToolServerConfig
from plugins.my_scraper.server import WebScrapingServer


@pytest.fixture
def server(mock_system_config):          # mock_system_config comes from the root conftest.py
    return WebScrapingServer(
        "my_scraper", mock_system_config,
        ToolServerConfig(type="my_scraper", enabled=True, config={"timeout": 10}),
    )
```

```python
# src/plugins/my_scraper/tests/test_plugin_my_scraper_basic.py
import pytest


async def test_missing_url_is_an_error(server):
    result = await server.call("my_scraper_fetch_url", {})

    assert result["status"] == "error"
    assert "url" in result["error"]


async def test_unknown_tool_raises(server):
    with pytest.raises(ValueError, match="not found"):
        await server.call("my_scraper_invalid_tool", {})
```

(`asyncio_mode = auto` in `pytest.ini` makes the `asyncio` marker unnecessary.)

### Cancellation Testing

```python
# src/plugins/my_scraper/tests/test_plugin_my_scraper_cancellation.py
from agent_system.core.cancellation import CancellationToken


async def test_pre_cancelled_token_returns_cancelled_shape(server):
    token = CancellationToken("test_456")
    token.cancel()                        # synchronous

    result = await server.call("my_scraper_fetch_url", {
        "url": "https://example.com",
        "_cancellation_token": token,
    })

    assert result["cancelled"] is True
```

### Status Testing

Drive the tool through `call_with_status`, the path the agent uses — a plain
`call()` gets no `_status`:

```python
async def test_status_reporting(server, monkeypatch):
    events = []

    class Bus:
        async def publish(self, event):
            events.append(event)

    monkeypatch.setattr("agent_system.tools.status.get_status_bus", lambda: Bus())

    await server.call_with_status("my_scraper_fetch_url", {"request_id": "r1"})

    # the missing url closes the scope as an error
    assert any(e.phase.value == "error" for e in events)
```

### Integration Testing

```python
# src/plugins/my_scraper/tests/test_plugin_my_scraper_integration.py
from pathlib import Path
from agent_system.plugins.discovery import discover_all_plugins

def test_plugin_discovery():
    """Test that the plugin is discovered correctly."""
    plugins = discover_all_plugins(dirs=[Path("src/plugins")])

    # dict: plugin type (folder name) -> factory
    assert "my_scraper" in plugins

def test_plugin_schema_valid():
    """Test that schema.yaml is valid."""
    from agent_system.plugins.schema_loader import load_schema_from_dir

    plugin_dir = Path(__file__).resolve().parents[1]
    schema = load_schema_from_dir(plugin_dir, {"name": "my_scraper"})

    assert "tools" in schema
    assert len(schema["tools"]) > 0

    # Check first tool has required fields
    tool = schema["tools"][0]
    assert "type" in tool
    assert "function" in tool
    assert "name" in tool["function"]
    assert "description" in tool["function"]
```

### Running Tests

```bash
# Run one plugin's tests (test selectively; the full suite takes 20+ minutes)
pytest src/plugins/my_scraper/tests -v

# Run one test file
pytest src/plugins/my_scraper/tests/test_plugin_my_scraper_basic.py -v
```

The root `conftest.py` terminates only processes it can prove a test run
started: a child a session starts with the inherited environment carries
`AGENT_SYSTEM_TEST_SESSION=<pid>:<start time>:<pid namespace>` of that pytest.
At the end of a session it stops what carries its own marker; at the start,
what carries the marker of a session that provably no longer runs (the
leftovers of a crashed run). Only processes of the same user without a setuid
identity qualify, and each is checked again right before the signal.
agent-api, workers, audio or another agent's scripts carry no marker and are
never touched. A child started with an environment of its own -- an
allowlist such as coding_cli's `child_env` or an MCP stdio server's default
environment -- carries none either: a test that starts one passes the marker
on, or what it leaves behind keeps running.

A test that starts a pytest of its own must set `AGENT_SYSTEM_TEST_NO_REAP=1`
for that run: the inner session's sweeps would otherwise send real signals,
outside whatever guards the outer run. The conftest cannot tell such a run
apart itself -- an xdist worker, too, carries the marker of a live owner and
must reap. `run_nested_pytest` in `tests/other/test_conftest_process_cleanup.py`
does it right: switch set, every `-p` plugin of the outer run passed on, and a
check that the inner session left nothing running (with its cleanup off,
nothing would end it).

Still don't run pytest on a server host: a test that starts the app as a
subprocess loads the host's real config and `config/secrets.env` -- the
conftest's fake LLM client does not reach it. `tests/app/test_app_shutdown.py`
boots the full API on a free port in a temporary directory, blanks the API keys
the config expands and gives coding_cli its own `CODING_CLI_DATA_ROOT`, but it
still starts every plugin the host has enabled. (`tests/cli/test_cli_tool_invocation.py`
runs agent-cli in-process on a scripted model and a temporary config, and fails
on any network connection.)

## Packaging and Distribution

### For Internal Plugins

Internal plugins live in one of the `plugin_dirs` (`src/plugins/` or a further root `src/plugins_<name>/`) and are discovered automatically. No additional packaging needed.

### For External Distribution

Package as a Python package with entry points:

**Tool plugin pyproject.toml:**
```toml
[project]
name = "my-agent-plugin"
version = "1.0.0"
description = "My awesome plugin for AgentSystem"
dependencies = [
    "agent-system>=1.0.0",
    "fastapi>=0.100.0",  # If using web endpoints
    "aiohttp>=3.8.0"     # For HTTP requests
]

[project.entry-points]
# tool server registration
agent_system.tool_plugins = [
    "my_plugin = my_agent_plugin.server:PLUGIN_FACTORY"
]

[project.scripts]
# CLI executable
tool-my-plugin = "my_agent_plugin.cli:main"
```

**Web-Only Plugin pyproject.toml:**
```toml
[project]
name = "my-dashboard-plugin"
version = "1.0.0"
description = "Web dashboard for AgentSystem"
dependencies = [
    "agent-system>=1.0.0",
    "fastapi>=0.100.0",
    "jinja2>=3.0.0"
]

[project.scripts]
# CLI for configuration and management
my-dashboard-cli = "my_dashboard_plugin.cli:main"
```

**CLI-Only Plugin pyproject.toml:**
```toml
[project]
name = "my-utility-tool"
version = "1.0.0"
description = "Standalone utility for AgentSystem ecosystem"
dependencies = [
    # Only what you need, no agent-system dependency required
    "click>=8.0.0",
    "requests>=2.25.0"
]

[project.scripts]
# Just the CLI, no tool server
my-utility = "my_utility_tool.cli:main"
data-converter = "my_utility_tool.converters:converter_main"
log-analyzer = "my_utility_tool.analyzers:analyzer_main"
```

**Directory structure:**
```
my-agent-plugin/
├── pyproject.toml
├── README.md
└── my_agent_plugin/
    ├── __init__.py
    ├── server.py
    ├── schema.yaml
    └── plugin.toml
```

**Installation:**
```bash
pip install my-agent-plugin
```

### Entry Point Discovery

AgentSystem discovers plugins through entry points:

```python
# In your package
PLUGIN_FACTORY = MyPluginServer

# Entry point registration makes it discoverable
# User installs package, plugin becomes available automatically
```

## Best Practices

### Design Principles

✅ **Small, focused tools** - One tool, one responsibility
✅ **Clear naming** - Use action verbs: `fetch_url`, `parse_html`, `extract_data`
✅ **Good error messages** - Help users understand what went wrong
✅ **Consistent responses** - Always include `status` field
✅ **Documentation** - a guide with examples and troubleshooting, a short README

### Implementation Checklist

**Required for plugins:**
- [ ] `schema.yaml` with proper tool definitions
- [ ] `plugin.toml` with metadata
- [ ] Server class extending `SchemaBasedToolServer`
- [ ] Support for `_status` parameter (one closing `end`/`error` naming the result)
- [ ] Support for `_cancellation_token` parameter (long-running tools)
- [ ] Input validation and structured error responses
- [ ] Server entry with `enabled: true` and the tools allowed for the agents that need them
- [ ] Guide with the "Model Experience" sections; README a short overview
- [ ] Tests in `src/plugins/<name>/tests/test_plugin_<name>_*.py`
- [ ] Optional: CLI (`cli.py` / `__main__.py` and a pyproject.toml script)

**Required for hybrid plugins:**
- [ ] All plugin requirements (above)
- [ ] Web endpoints class extending `PluginWebInterface`
- [ ] `web_ui` section in `schema.yaml`: `panel` (catalogue entry) and `endpoints`
- [ ] `get_web_router()` implementation; the plugin object delegates `get_schema_data()`
- [ ] Static assets handling (CSS, JS, images)
- [ ] `plugin.toml` with `type = ["tool-server", "web"]` and `category` metadata

**Required for web-only plugins:**
- [ ] Web endpoints class extending `PluginWebInterface`
- [ ] `web_ui` section in `schema.yaml` (no tools section needed)
- [ ] `get_web_router()` returning FastAPI router with `/plugins/<name>/` prefix
- [ ] `plugin.toml` with `type = ["web"]` and `category` metadata
- [ ] Static assets handling (CSS, JS, images)
- [ ] Panel declared as `web_ui.panel`; `python src/scripts/validate_plugin.py <plugin dir>` passes and `GET /api/ui/catalog` lists it
- [ ] Security considerations for web access

**Required for CLI-only plugins:**
- [ ] CLI implementation with proper argument parsing
- [ ] Entry point registration in `pyproject.toml`
- [ ] Error handling and JSON output
- [ ] Help text and documentation
- [ ] Tests for CLI functionality

**For long-running tools:**
- [ ] Check cancellation before starting work
- [ ] Poll cancellation token in loops
- [ ] Register cleanup callbacks
- [ ] Register background tasks with cancellation manager
- [ ] Keep cleanup callbacks under 1 second

**Quality assurance:**
- [ ] All tools have clear descriptions
- [ ] Parameter validation with helpful error messages
- [ ] Configuration logging at startup
- [ ] Guide with usage examples
- [ ] Tests covering normal operation, errors, and cancellation

### Performance Tips

- **Yield control**: Use `await asyncio.sleep(0)` in tight loops
- **Batch operations**: Don't send status updates for every item
- **Connection pooling**: Reuse HTTP connections, database connections
- **Caching**: Cache expensive computations when appropriate
- **Timeouts**: Always set reasonable timeouts for external calls

### Security Considerations

- **Input validation**: Never trust user input
- **Sanitize outputs**: Escape HTML, validate URLs
- **Rate limiting**: Implement rate limiting for external APIs
- **Secrets**: Use environment variables, never hardcode credentials
- **Sandboxing**: Consider process isolation for untrusted plugins
- **Schlüssel im Pfad**: Trägt eine Route einen Schlüssel in ihrem Pfad (wer die URL hat, darf sie
  benutzen — die Callback-URLs von `stategraph`), steht er direkt hinter `callback/`:
  `/plugins/<instanz>/callback/<schlüssel>`, oder im Parameter `token` derselben Route:
  `/plugins/<instanz>/callback?token=<schlüssel>` — die Query-Form braucht, wer von einem anderen
  Rechner erreichbar sein soll (`network.remote_paths` lässt nur exakte Pfade durch). Nur diese
  beiden Stellen maskieren die Logs (App-Log, Access-Log, `security.log`, Security-Audit-Panel,
  Profiling; die letzten drei führen die Query gar nicht), auch prozentkodiert und im Traceback
  (`agent_system/utils/logging.py`, `loggable_path`). Ein Schlüssel an anderer Stelle steht im
  Klartext darin.

## Troubleshooting

### Common Issues

**Plugin not discovered:**
- The entrypoint module or its factory is missing — discovery skips the folder with a **DEBUG** log only
  - The module named in `entrypoint` (default `plugin.py`) must sit directly in the plugin folder
  - It must define the named factory or `PLUGIN_FACTORY`
- An import error in the entrypoint module logs a WARNING; every server entry of that type then logs "Unknown server type"
- Verify the plugin directory is listed in `config/plugins.yaml` under `plugin_dirs`
- The server entry's `type` must be the plugin **folder name**
- Use `python -m agent_system.agent_cli plugins` to list discovered plugins

**Debug plugin discovery:**
```python
from pathlib import Path
from agent_system.plugins import discover_plugins

# Test if your plugin is discoverable
plugins = discover_plugins(Path('src/plugins'))
print(plugins.keys())  # Is your plugin listed? (keys are folder names)
```

**Tools not working / not visible:**
- Validate `schema.yaml` (it is rendered as Jinja first; a broken schema fails only on the first `get_tools()`)
- Check the tool → method mapping: `{{ name }}_x` → method `x`
- The server entry has `enabled: true` (default `false`)
- The agent's `tools.allowed` admits the tool: `instance/*`, `instance`, or `instance/<full tool name incl. prefix>` — an empty list allows nothing

**Cancellation not working:**
- Ensure you're checking `token.is_cancelled` in loops
- Register cleanup callbacks with `token.add_cleanup_callback()`
- Background tasks must be registered with CancellationManager

**Status updates not appearing:**
- Check that you're using `await status.progress()`
- Verify `_status` parameter is being passed to your tool (only `call_with_status` injects it)
- Don't send too many rapid updates (batch them)

**Configuration issues:**
- Log effective configuration in `__init__()`
- Check the `plugins: servers:` entry (e.g. in `config/plugins.yaml`) has `enabled: true`; `config/mcp_servers.yaml` is for external MCP servers
- Inspect the merged config with `get_tool_server_config(name, config)`, not the raw `config.plugins.servers[name]`
- Validate configuration values and provide good defaults

### Debugging Tips

**Enable debug logging:**
```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

**Test plugins in isolation:**
```python
# Quick test without full agent
from agent_system.config import load_settings, get_tool_server_config

config = load_settings()
server = MyServer("my_plugin", config, get_tool_server_config("my_plugin", config))
result = await server.call("my_plugin_my_tool", {"param": "value"})
print(result)
```

**Check plugin discovery:**
```bash
python -m agent_system.agent_cli plugins list --format json
```

**Validate schema:**
```python
from agent_system.plugins.schema_loader import load_schema_from_dir
from pathlib import Path

schema = load_schema_from_dir(Path("src/plugins/my_plugin"), {"name": "my_plugin"})
print(schema)
```

### Getting Help

- Check existing plugins in `src/plugins/` for examples
- Read the MCP specification if you work on the `mcp_client` plugin
- Check logs in `logs/` directory for error details
- Use `python -m agent_system.agent_cli plugins --help` for CLI options

---

## Hooks-Only Plugins

In addition to tool plugins, AgentSystem supports **hooks-only plugins** that intercept agent lifecycle points without providing tools. This is ideal for cross-cutting concerns like logging, validation, context management, and monitoring.

### When to Use Hooks vs Tools

**Use Hooks When:**
- You need to modify agent behavior globally
- You want to intercept lifecycle events (session start/end, LLM calls, tool calls)
- You're implementing cross-cutting concerns (logging, metrics, validation)
- You want to transform inputs/outputs automatically
- You don't need the agent to explicitly call your functionality

**Use Tools When:**
- The agent should decide when to use your functionality
- You're providing specific capabilities (web scraping, database access)
- The functionality should appear in tool listings
- Users need to configure when/how it's used

**Use Both (Hybrid) When:**
- You provide tools AND want to modify behavior (e.g., caching plugin with cache invalidation tool)
- You need lifecycle hooks to support your tools (e.g., cleanup at session end)

### Hook Types

`HookType` (`agent_system/hooks/plugin_hook.py`) has nine values:

1. **SESSION_START** - New session only, before history and user input
2. **PRE_LLM_CALL** - Every step before the LLM call; a changed `messages` list is sent as-is
3. **LLM_PROGRESS** - While streaming, every few KB of thinking; no messages, no effect
4. **POST_LLM_CALL** - After the assistant message is appended
5. **SESSION_END** - After saving; no effect
6. **PRE_LLM_REQUEST** / 7. **POST_LLM_RESPONSE** - At LLM client level, read-only
8. **PRE_TOOL_CALL** - Before each tool call of the model (and of a tool_script script); may change the arguments or block the call
9. **POST_TOOL_CALL** - After the call ran, before its result joins the history; may change the result

Each hook gets a deep copy of the context. Changes count only with
`modified=True`; `success=False` discards context and metadata.

### Schema-Based Hooks Pattern

The recommended pattern uses `SchemaBasedPluginHook` with declarative YAML configuration.

**Directory Structure:**
```
src/plugins/my_hook_plugin/
├── plugin.toml      # type = ["hooks"], entrypoint = "plugin:PLUGIN_FACTORY"
├── plugin.py        # Factory function
├── hooks.py         # Hook implementation
├── schema.yaml      # Hook definitions + config
└── README.md        # Documentation
```

The hooks are registered only if `schema.yaml` has a `hooks:` key **and** the
server entry is `enabled: true`.

**Example: schema.yaml**
```yaml
# Hook definitions
hooks:
  - name: my_handler        # Must match method name exactly
    type: pre_llm_call
    enabled: true
    timeout: 30.0
    category: prompt_injection   # order may reference categories
    description: "What this hook does"
    order:
      after: ["begin"]
      before: ["end"]

# Configuration defaults (read for hook plugins only)
config:
  max_items:
    type: integer
    default: 100
    description: "Maximum items to process"

  enable_feature:
    type: boolean
    default: true
    description: "Enable special feature"
```

**Example: hooks.py**
```python
from pathlib import Path
from agent_system.hooks import SchemaBasedPluginHook, HookContext, HookResult

class MyHookPlugin(SchemaBasedPluginHook):
    """Example hooks-only plugin."""

    def __init__(self, plugin_dir: Path | str, server_config=None):
        super().__init__(plugin_dir)

        # get_config() holds the schema.yaml defaults (plain values);
        # merging the server entry's config: block is the plugin's job
        config = dict(self.get_config())
        if server_config is not None and getattr(server_config, "config", None):
            config.update(server_config.config)
        self.max_items = config.get('max_items', 100)
        self.enabled = config.get('enable_feature', True)

    # Handler name MUST match hook name in schema.yaml
    async def my_handler(self, context: HookContext) -> HookResult:
        """Handle pre-LLM call hook.

        Args:
            context: Hook execution context (a deep copy for this hook);
                messages are ChatMessage objects

        Returns:
            HookResult with success status and optionally modified context
        """
        try:
            # Access context data
            messages = context.messages or []

            # Perform hook logic
            if self.enabled and len(messages) > self.max_items:
                # Modify the copy in place (example; note that dropping earlier
                # messages breaks the provider's prompt cache)
                modified_messages = messages[-self.max_items:]
                context.messages = modified_messages

                return HookResult(
                    success=True,
                    modified=True,  # We modified the context
                    context=context,
                    metadata={'items_removed': len(messages) - len(modified_messages)}
                )

            # No modifications needed
            return HookResult(
                success=True,
                modified=False,
                context=context
            )

        except Exception as e:
            # Always return HookResult, never raise
            return HookResult(
                success=False,
                modified=False,
                context=context,
                error=str(e)
            )
```

**Example: plugin.py**
```python
from pathlib import Path
from .hooks import MyHookPlugin

def PLUGIN_FACTORY(name=None, system_config=None, server_config=None) -> MyHookPlugin:
    """Called by the runtime as (name, system_config, server_config)."""
    plugin_dir = Path(__file__).parent
    return MyHookPlugin(plugin_dir, server_config)
```

### Hook Ordering

A hook's full name is `<server instance name>.<hook name>` (e.g.
`context_engineer.engineer_context`). `order` resolves **full names, categories
and `begin`/`end`** only — a short hook name is silently ignored.

```yaml
hooks:
  - name: optimize_context
    type: pre_llm_call
    order:
      after: ["begin"]                           # virtual node: start
      before: ["my_summarizer.summarize_context", "validation"]   # full name, category
```

**Special Order Names:**
- `begin`: Virtual hook at start
- `end`: Virtual hook at end

Ties sort alphabetically. A cycle is logged as an error and the registration
order is used; no exception is raised.

### Global Configuration

Override hook behavior in the top-level `hooks:` section of `config/plugins.yaml`
(or any included file):

```yaml
hooks:
  enabled: true
  overrides:
    my_hook_plugin.my_handler:   # <server instance>.<hook>, or <server instance> for all its hooks
      enabled: false             # Disable this specific hook
      timeout: 60.0              # Override timeout
      order:
        after: ["other_plugin.other_hook"]
```

A global override knows only `enabled`, `timeout` and `order`; only these field
names are validated. A key that matches no registered hook has no effect, and
startup logs a warning for it. `hooks.default_timeout` applies to hooks whose
schema sets no `timeout`.

Per agent, `agent_config.hooks.enabled: false` switches off all hooks of that
agent, `agent_config.hooks.overrides["<full name>"].enabled` switches one, and
every other key there arrives as `context.hook_config`. Agent overrides are not
validated at all — copy the full name from the registration log.

### HookContext Reference

Main fields (full list in `agent_system/hooks/plugin_hook.py`):

```python
@dataclass
class HookContext:
    hook_type: HookType
    request_id: str
    session_id: str
    agent: Optional[Agent] = None
    agent_name: str = ""
    messages: Optional[List[ChatMessage]] = None   # ChatMessage objects, not dicts
    tools_schema: Optional[List[Dict]] = None      # per-request tool schema
    llm_response: Optional[Dict] = None
    tool_call: Optional[Dict] = None
    tool_result: Optional[Dict] = None
    metadata: Dict[str, Any] = {}
    hook_config: Dict[str, Any] = {}               # per-agent keys from hooks.overrides
    step: int = 0
    llm: Optional[Any] = None
    cancellation_token: Optional[Any] = None
    # plus llm_* fields for pre_llm_request / post_llm_response and
    # reasoning_* fields for llm_progress
```

### Hook Best Practices

1. **Keep Hooks Fast**: Target <100ms execution time
2. **Return HookResult**: Even on error, never raise exceptions
3. **Set modified=True**: When you change the context
4. **Use Async/Await**: For I/O operations
5. **Log Appropriately**: Use logger for debugging, not print()
6. **Handle Errors Gracefully**: Return success=False with error message
7. **Test Edge Cases**: Empty inputs, large inputs, concurrent execution

### Hook Examples

**Logging Hook:**
```python
async def log_request(self, context: HookContext) -> HookResult:
    """Log LLM requests."""
    logger.info(
        f"LLM call: session={context.session_id}, "
        f"messages={len(context.messages or [])}"
    )
    return HookResult(success=True, modified=False, context=context)
```

**Validation Hook:**
```python
async def validate_messages(self, context: HookContext) -> HookResult:
    """Validate message format."""
    messages = context.messages or []

    for msg in messages:                      # ChatMessage objects
        if not msg.role or (msg.content is None and not msg.tool_calls):
            return HookResult(
                success=False,
                modified=False,
                context=context,
                error="Invalid message format: missing role or content"
            )

    return HookResult(success=True, modified=False, context=context)
```

**Transformation Hook** (appends the state as a turn and writes again only when
it changed, so the request prefix stays stable for the prompt cache):
```python
from agent_system.llm.message_roles import DEVELOPER
from agent_system.llm.models import ChatMessage

MARK = "my_hook_plugin"

async def inject_note(self, context: HookContext) -> HookResult:
    """Append the state as a turn, and only when it says something new."""
    if context.messages is None:
        return HookResult(success=True, modified=False, context=context)

    text = self._render()
    previous = next((m for m in reversed(context.messages)
                     if getattr(m, "injected_by", None) == MARK), None)
    if previous is not None and previous.content == text:
        return HookResult(success=True, modified=False, context=context)

    context.messages.append(ChatMessage(role=DEVELOPER, content=text, injected_by=MARK))
    return HookResult(success=True, modified=True, context=context)
```

**Why not behind the system prompt, and why not `system`.** A block at the head
is rebuilt on every call, so the cached prefix behind it is invalid every time;
and Anthropic and Gemini have no system role inside a history, so they hoist
such a message into the prompt itself, where it reads as if it had held since
the first turn. Appended as a `developer` turn it keeps its place and leaves
everything before it byte-identical. The old block is never deleted -- deleting
it is the same rewrite -- it is superseded by the newer one, which is simply
the last of them in the history. A block that compaction took away is simply
appended again; that is the same branch as the first one.

The price is paid in the history: a state that changes on every step leaves one
block per step, all of them in the present tense. Two things follow. Render
only what actually changes -- a counter, a timestamp or an unstable sort order
in the text makes every call a change, and that is the difference between one
block and thirty. And when the state can become *empty*, say that in a block of
its own instead of writing nothing: an older block that says "three sub-agents
are running" is otherwise the last word on the subject.

Every `role: user` message a hook inserts carries `injected_by`; `injected_by is None`
means "written by a person".

### Testing Hooks

```python
import pytest
from agent_system.hooks import HookContext, HookType
from agent_system.llm.models import ChatMessage
from plugins.my_hook_plugin.plugin import PLUGIN_FACTORY

@pytest.fixture
def plugin():
    return PLUGIN_FACTORY("my_hook_plugin", None, None)

async def test_my_handler(plugin):
    """Test hook handler."""
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-123',
        session_id='session-1',
        messages=[
            ChatMessage(role='user', content='Hello'),
            ChatMessage(role='assistant', content='Hi there!'),
        ]
    )

    result = await plugin.my_handler(context)

    assert result.success is True
    assert result.modified is False  # Or True if modified
    assert result.error is None
```

### Complete Hook Plugin Examples

See these example implementations:

- **[context_engineer](../src/plugins/context_engineer/)** - Layered context compaction that preserves the prompt cache
- **[context_summarizer](../src/plugins/context_summarizer/)** - Intelligent LLM-based summarization
- **[message_validator](../src/plugins/message_validator/)** - Message format validation
- **[request_logger](../src/plugins/request_logger/)** - Request/response logging with timing

### Hybrid Plugins (Tools + Hooks)

You can combine tools and hooks in a single plugin. The server's `schema.yaml`
carries both `tools:` and `hooks:`; the hook handlers are found either on a
`hooks_plugin` attribute (a `SchemaBasedPluginHook` instance) or on the server
itself as `on_<hook type>` methods (duck typing, see `src/plugins/okf/server.py`):

```python
from pathlib import Path

from agent_system.tools.schema_based import SchemaBasedToolServer
from .hooks import MyHookPlugin


class MyHybridPlugin(SchemaBasedToolServer):
    """Plugin with both tools and hooks."""

    def __init__(self, name, system_config, server_config):
        super().__init__(name, system_config, server_config)
        self.hooks_plugin = MyHookPlugin(Path(__file__).parent, server_config)

    # Tool "{name}_my_tool"
    async def my_tool(self, params: dict) -> dict:
        return {"status": "success", "result": f"Processed: {params.get('param')}"}
```

With two hooks of the same type on the duck-typed `on_*` path, the `on_*`
method runs once per registered hook name.

For complete hooks documentation, see [Plugin Hook System](./plugin_hooks.md).

---

**Ready to build your plugin?** Start with the [Quick Start](#quick-start-your-first-plugin) section and refer back to specific sections as needed.

