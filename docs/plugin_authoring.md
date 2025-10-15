# Plugin Authoring Guide

This document explains how to create plugins (MCP servers) for AgentSystem. It walks you through the entire process: from the initial idea to implementation and deployment.

## Table of Contents

- [What is a Plugin?](#what-is-a-plugin)
  - [Alternative: Configuration-Based Agents](#alternative-configuration-based-agents)
- [Quick Start: Your First Plugin](#quick-start-your-first-plugin)
- [Plugin Structure and Layout](#plugin-structure-and-layout)
- [Defining Schema (`schema.yaml`)](#defining-schema-schemayaml)
- [Defining Metadata (`plugin.yaml`)](#defining-metadata-pluginyaml)
- [Implementing the Server](#implementing-the-server)
  - [Agent-Based Plugins](#agent-based-plugins)
  - [Plugin Types Summary](#plugin-types-summary)
- [Tools and Parameters](#tools-and-parameters)
- [Advanced Features](#advanced-features)
  - [Status and Progress Reporting](#status-and-progress-reporting)
  - [Cooperative Cancellation](#cooperative-cancellation)
  - [Web Endpoints and UI Integration](#web-endpoints-and-ui-integration)
  - [CLI Support (Required)](#cli-support-required)
  - [Background Tasks](#background-tasks)
- [Configuration and Deployment](#configuration-and-deployment)
- [Testing and Quality Assurance](#testing-and-quality-assurance)
- [Packaging and Distribution](#packaging-and-distribution)
- [Best Practices](#best-practices)
- [Troubleshooting](#troubleshooting)

## What is a Plugin?

A plugin in AgentSystem is an MCP (Model Context Protocol) server that provides new tools to the agent. Plugins extend the agent's capabilities with specific functions like web scraping, database access, or API integration.

### How Plugins Work

1. **Discovery**: AgentSystem automatically finds plugins in `src/plugins/` or as installed Python packages
2. **Schema**: Each plugin describes its tools in `schema.yaml` (what parameters, what they do)
3. **Execution**: The agent calls tools via `call(tool_name, parameters)`
4. **Response**: The plugin returns structured results

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
- **Tool subset configurations** (restrict agent to specific MCP servers/tools)
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
```yaml
# In config/mcp.yaml under mcp_system.config_agents:
financial_analyst:
  base_type: generic
  description: "Financial analysis and market research agent"
  system_template: prompts/financial_analyst.md
  llm_profile: deepseek-chat
  tools:
    allowed:
      - "mcp_*"  # All MCP tools
      - "browser_*"  # Browser tools
    blocked:
      - "filesystem_*"  # Block file operations
```

See [Configuration-Based Agents Guide](config_based_agents.md) for complete documentation.

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
      name: say_hello
      description: "Says hello to a person"
      parameters:
        type: object
        properties:
          name:
            type: string
            description: "Name of the person to greet"
        required: ["name"]
```

**Step 2: Create `plugin.yaml`**
```yaml
name: hello_world
version: 1.0.0
description: "Simple hello world plugin"
entrypoint: server:HelloWorldServer
```

**Step 3: Create `server.py`**
```python
from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.config import AgentSystemConfig, MCPConfig

class HelloWorldServer(SchemaBasedMCPServer):
    """Simple hello world plugin demonstrating modern API."""
    
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        """
        Modern constructor signature.
        
        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            mcp_config: Plugin-specific configuration from config.mcp_system.servers[name]
        """
        super().__init__(name, system_config, mcp_config)
        
        # Extract plugin-specific configuration from mcp_config
        self.greeting_prefix = mcp_config.get("greeting_prefix", "Hello")
        
        # Log effective configuration
        self.logger.info(f"HelloWorld configured: prefix='{self.greeting_prefix}'")
    
    async def say_hello(self, params: dict) -> dict:
        """
        Tool method - automatically called by generic dispatcher.
        Method name MUST match tool name in schema.yaml exactly.
        """
        name = params.get("name", "World")
        return {
            "status": "success", 
            "message": f"{self.greeting_prefix}, {name}!"
        }

PLUGIN_FACTORY = HelloWorldServer
```

That's it! Your plugin is ready to use.

## Plugin Structure and Layout

### Recommended Directory Structure

```
src/plugins/<plugin_name>/
  ├── plugin.yaml       # Plugin metadata
  ├── schema.yaml       # Tool definitions
  ├── server.py         # Main server implementation
  └── README.md         # Documentation and examples
```

### Test Structure

Tests go in the project root under `tests/` and follow the naming convention:
```
tests/
  ├── test_plugin_<plugin_name>_basic.py
  ├── test_plugin_<plugin_name>_integration.py
  └── test_plugin_<plugin_name>_cancellation.py
```

**Example test file:** `tests/test_plugin_hello_world_basic.py`

### File Responsibilities

- **`schema.yaml`**: Defines tools, parameters, and validation rules
- **`plugin.yaml`**: Metadata for discovery (name, version, entry point)
- **`server.py`**: Core logic, tool routing, and MCP protocol implementation
- **`README.md`**: Usage examples, configuration options, troubleshooting

## Defining Metadata (`plugin.yaml`)

The `plugin.yaml` file contains essential metadata for plugin discovery and management.

### Basic Structure

```yaml
name: my_plugin
version: 1.0.0
description: "Brief description of what the plugin does"
author: "Your Name"
entrypoint: server:PLUGIN_FACTORY
```

### Field Descriptions

- **`name`**: Unique plugin identifier (used in configuration)
- **`version`**: Semantic version (x.y.z)
- **`description`**: Short, clear description for users
- **`author`**: Plugin author/maintainer
- **`entrypoint`**: Points to the server class (`module:symbol`)

### Plugin Types and Categories

```yaml
# MCP-only plugin
name: web_scraper
version: 1.0.0
description: "Web scraping tools for content extraction"
author: "Your Name"
entrypoint: server:WebScraperServer
type: mcp_only
category: tools

# Web-only plugin  
name: monitoring_dashboard
version: 1.2.0
description: "System monitoring dashboard with real-time metrics"
author: "Team Name"
entrypoint: plugin:DashboardPlugin
type: web_only
category: monitoring

# Hybrid plugin (MCP + Web)
name: log_viewer
version: 1.0.0
description: "Real-time log streaming and viewing with web interface"
author: "AgentSystem Team"
entrypoint: plugin:LogViewerPlugin
type: hybrid
category: monitoring

# CLI-only plugin
name: data_converter
version: 1.0.0
description: "Data format conversion utilities"
author: "Utilities Team"
entrypoint: cli:main
type: cli_only
category: utilities
```

### Advanced Options

```yaml
name: advanced_plugin
version: 2.1.0
description: "Advanced plugin with dependencies and configuration"
author: "Team Name"
entrypoint: server:AdvancedServer
type: hybrid
category: tools

# Dependencies (for external packages)
dependencies:
  - requests>=2.25.0
  - beautifulsoup4>=4.9.0

# Plugin classification
tags:
  - web
  - scraping
  - api
```

### Metadata Field Reference

**Core Fields:**
- **`name`**: Unique plugin identifier (used in configuration and URLs)
- **`version`**: Semantic version (x.y.z) for compatibility tracking  
- **`description`**: Short, clear description for users and UIs
- **`author`**: Plugin author/maintainer for support
- **`entrypoint`**: Module and symbol path (`module:symbol`)

**Plugin Classification:**
- **`type`**: Plugin type (`mcp_only`, `web_only`, `hybrid`, `cli_only`)
- **`category`**: Functional category (`tools`, `monitoring`, `data`, `ui`, `utilities`)
- **`tags`**: Searchable keywords for discovery

**Dependencies:**
- **`dependencies`**: Python package requirements (for external plugins)

## Defining Schema (`schema.yaml`)

The `schema.yaml` file defines your plugin's tools using OpenAI function format. This is how the agent knows what tools are available and how to call them.

> **Important**: `schema.yaml` contains only tool definitions and web UI configuration. Plugin metadata (type, category, version, etc.) belongs in `plugin.yaml`, not here.

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
class MyServer(SchemaBasedMCPServer):
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
- Document custom template variables in your plugin's README
- **Prefer `get_template_vars()` override** over `_load_schema()` override for custom variables

**Why use `get_template_vars()` instead of overriding `_load_schema()`?**
- **Cleaner code**: Just return a dictionary instead of duplicating schema loading logic
- **Less error-prone**: Base class handles caching, error handling, and directory resolution
- **Better maintainability**: Your code focuses only on the template variables, not infrastructure
- **Future-proof**: Benefits from base class improvements automatically

### Web UI Configuration (For Hybrid/Web Plugins)
```

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
  enabled: true
  button_text: "Web Scraper"
  button_icon: "🌐"
  panel_title: "Web Scraping Dashboard"
  panel_endpoint: "/plugins/web_scraper/dashboard"
  panel_type: "fetch"
  description: "Web scraping tools with real-time monitoring"
  
  panels:
    - id: "scraper_panel"
      title: "Scraping Status"
      icon: "activity"
      position: "right"
      width: "400px"
      height: "300px"
      url: "/plugins/{name}/status"
  
  endpoints:
    - path: "/api/scrape"
      method: "POST"
      description: "Start scraping job"
    - path: "/api/jobs"
      method: "GET"
      description: "List active scraping jobs"
    - path: "/dashboard"
      method: "GET"
      description: "Main scraping dashboard"
```

### Web UI Configuration (For Hybrid/Web Plugins)

For plugins that provide web interfaces, add a `web_ui` section to your schema:

> **Note**: The `web_ui` section belongs in `schema.yaml` for UI configuration. Plugin metadata (type, category, etc.) belongs in `plugin.yaml`.

```yaml
# After your tools definitions
tools:
  - type: function
    # ... your tool definitions ...

# Web UI configuration
web_ui:
  # Button registration in main interface
  enabled: true
  button_text: "My Plugin"
  button_icon: "🔧"  # Optional emoji or icon
  panel_title: "My Plugin Dashboard"
  panel_endpoint: "/plugins/my_plugin/panel"
  panel_type: "fetch"  # "fetch" or "iframe"
  description: "Plugin description for UI"
  
  # Panel configuration
  panels:
    - id: "my_plugin_panel"
      title: "Main Panel"
      icon: "dashboard"
      position: "center"  # "top", "bottom", "left", "right", "center"
      width: "800px"
      height: "600px"
      url: "/plugins/{name}/panel.html"
  
  # Document your endpoints for API discovery
  endpoints:
    - path: "/api/data"
      method: "GET"
      description: "Retrieve plugin data"
    - path: "/api/action"
      method: "POST"
      description: "Perform plugin action"
    - path: "/dashboard"
      method: "GET"
      description: "Main dashboard interface"
    - path: "/static/{file_path}"
      method: "GET"
      description: "Static assets (CSS, JS, images)"
```

### Web UI Fields Reference

**Button Registration:**
- `enabled`: Enable/disable web UI integration
- `button_text`: Text for plugin button in main interface
- `button_icon`: Emoji or icon for the button
- `panel_title`: Title for the plugin panel
- `panel_endpoint`: URL endpoint for the main panel
- `panel_type`: How to load content (`fetch` or `iframe`)

**Panel Configuration:**
- `id`: Unique panel identifier
- `title`: Panel display title
- `icon`: Icon name or emoji for the panel
- `position`: Where to position the panel
- `width`/`height`: Panel dimensions
- `url`: Panel URL (supports `{name}` placeholder)

**Endpoint Documentation:**
- Documents all web endpoints your plugin provides
- Used for API discovery and debugging
- Include path, method, and clear description

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
1. **Modern Constructor**: `(name, system_config, mcp_config)` signature
2. **No Manual Routing**: Remove `call()` override - use generic dispatcher
3. **Tool Methods**: Implement methods matching tool names exactly
4. **Type Hints**: Use modern Python type hints (`| None` instead of `Optional[]`)
5. **Configuration**: Extract from `mcp_config` (plugin-specific) and `system_config` (system-wide)

### Method 1: Schema-Based Server (Recommended)

Use `SchemaBasedMCPServer` for automatic schema loading and generic dispatching:

```python
from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.config import AgentSystemConfig, MCPConfig

class WebScrapingServer(SchemaBasedMCPServer):
    """Modern schema-based plugin with automatic tool routing."""
    
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        """
        Modern constructor signature.
        
        Args:
            name: Plugin instance name (used for tool prefixing in schema templates)
            system_config: System-wide configuration (ports, paths, etc.)
            mcp_config: Plugin-specific configuration from config.mcp_system.servers[name]
        """
        super().__init__(name, system_config, mcp_config)
        
        # Extract plugin-specific configuration from mcp_config
        self.timeout = float(mcp_config.get("timeout", 30))
        self.user_agent = mcp_config.get("user_agent", "AgentSystem/1.0")
        self.max_retries = int(mcp_config.get("max_retries", 3))
        
        # Validate configuration
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        
        # System-wide config examples (optional)
        self.base_url = system_config.api_base_url if hasattr(system_config, 'api_base_url') else None
        
        # Log effective configuration
        self.logger.info(
            f"WebScraping configured: timeout={self.timeout}, "
            f"user_agent={self.user_agent}, max_retries={self.max_retries}"
        )

    # Tool methods - automatically called by generic dispatcher
    # Method names MUST match tool names in schema.yaml exactly!
    
    async def fetch_url(self, params: dict) -> dict:
        """
        Fetch content from a URL.
        
        This method is automatically called when the 'fetch_url' tool is invoked.
        No manual routing needed - MCPServer.call() dispatches automatically.
        """
        url = params["url"]
        
        # Extract runtime parameters (automatically injected by framework)
        status = params.get("_status")
        token = params.get("_cancellation_token")
        request_id = params.get("request_id")
        
        if status:
            await status.progress(f"Fetching {url}")
        
        # Check cancellation before starting
        if token and token.is_cancelled:
            return {"status": "cancelled", "request_id": request_id}
        
        try:
            import aiohttp
            timeout_cfg = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
                headers = {"User-Agent": self.user_agent}
                async with session.get(url, headers=headers) as response:
                    content = await response.text()
                    
            if status:
                await status.complete(f"Fetched {len(content)} bytes from {url}")
                    
            return {
                "status": "success", 
                "content": content,
                "url": url,
                "status_code": response.status
            }
        except Exception as e:
            if status:
                await status.error(f"Failed to fetch {url}: {e}")
            self.logger.error(f"fetch_url failed: {e}", exc_info=True)
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
            self.logger.error(f"parse_html failed: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

PLUGIN_FACTORY = WebScrapingServer
```

**How the Generic Dispatcher Works:**

1. Agent calls tool: `await server.call("fetch_url", {"url": "https://example.com"})`
2. MCPServer.call() receives request
3. Generic dispatcher looks for method named `fetch_url`
4. Automatically invokes `self.fetch_url(params)`
5. Returns result to caller

**No manual `call()` override needed!** The base class handles all routing automatically.

**Method Naming Rule:**
- Tool name in `schema.yaml`: `fetch_url`
- Method name in server: `async def fetch_url(self, params: dict)`
- They MUST match exactly for automatic routing to work

**Schema Template Constraint:**
If using `{{ name }}` templates in schema (e.g., `{{ name }}_calculator`), your method must match the resolved name:
```python
# schema.yaml: {{ name }}_calculator
# Plugin instantiated as: ExampleServer("example", ...)
# Method must be named: async def example_calculator(self, params: dict)
```

### Method 2: Web-Only Plugin

For plugins that only provide web endpoints (no MCP tools):

```python
# src/plugins/my_dashboard/endpoints.py
"""Web-only plugin - provides dashboard endpoints"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from agent_system.plugins.web_adapter import PluginWebInterface
from agent_system.config import AgentSystemConfig, MCPConfig

class DashboardWebEndpoints(PluginWebInterface):
    """Web endpoints for dashboard plugin"""
    
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        """Modern constructor signature for web-only plugins."""
        self.name = name
        self.system_config = system_config
        self.mcp_config = mcp_config
        
        # Setup templates
        template_dir = Path(__file__).parent / "templates"
        self.templates = Jinja2Templates(directory=str(template_dir))
    
    def get_web_router(self) -> APIRouter:
        """Return FastAPI router with dashboard endpoints"""
        router = APIRouter(prefix=f"/plugins/{self.name}")
        
        @router.get("/", response_class=HTMLResponse)
        async def dashboard_home(request: Request):
            """Main dashboard page"""
            return self.templates.TemplateResponse(
                request,
                "dashboard.html",
                {"plugin_name": self.name}
            )
        
        @router.get("/api/metrics")
        async def get_metrics():
            """API endpoint for metrics data"""
            return {
                "cpu_usage": 45.2,
                "memory_usage": 67.8,
                "disk_usage": 23.1,
                "active_processes": 156
            }
        
        @router.get("/api/status")
        async def get_status():
            """System status endpoint"""
            return {"status": "healthy", "uptime": "2d 14h 23m"}
        
        @router.get("/static/{file_path:path}")
        async def serve_static(file_path: str):
            """Serve static assets (CSS, JS, images)"""
            from fastapi.responses import FileResponse
            from fastapi import HTTPException
            
            static_dir = Path(__file__).parent / "static"
            file_full_path = static_dir / file_path
            
            if not file_full_path.exists():
                raise HTTPException(status_code=404)
            
            return FileResponse(file_full_path)
        
        return router
    
    def get_panels(self):
        """Register dashboard panel in main UI"""
        return [{
            "id": f"{self.name}_panel",
            "title": "System Dashboard",
            "url": f"/plugins/{self.name}/",
            "icon": "dashboard",
            "position": "center",
            "width": "800px",
            "height": "600px"
        }]

# src/plugins/my_dashboard/plugin.py
from .endpoints import DashboardWebEndpoints
from agent_system.config import AgentSystemConfig, MCPConfig

class DashboardPlugin:
    """Web-only plugin (no MCP server)"""
    
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        """Modern constructor - matches MCP server signature."""
        self.web_endpoints = DashboardWebEndpoints(name, system_config, mcp_config)
    
    # Web interface delegation
    def get_web_router(self):
        return self.web_endpoints.get_web_router()
    
    def get_panels(self):
        return self.web_endpoints.get_panels()

PLUGIN_FACTORY = DashboardPlugin
```

**Plugin Configuration:**
```yaml
# plugin.yaml
name: my_dashboard
version: 1.0.0
description: "System dashboard web interface"
type: web_only
entrypoint: plugin:PLUGIN_FACTORY
```

**Web-Only Schema (no tools):**
```yaml
# No tools section needed for web-only plugins

# Web UI configuration
web_ui:
  enabled: true
  button_text: "System Dashboard"
  button_icon: "📊"
  panel_title: "System Monitoring Dashboard"
  panel_endpoint: "/plugins/my_dashboard/"
  panel_type: "iframe"
  description: "Real-time system monitoring and metrics"
  
  panels:
    - id: "main_dashboard"
      title: "System Overview"
      icon: "monitor"
      position: "center"
      width: "100%"
      height: "600px"
      url: "/plugins/{name}/"
  
  endpoints:
    - path: "/api/metrics"
      method: "GET"
      description: "System metrics data"
    - path: "/api/status"
      method: "GET"
      description: "System status information"
    - path: "/"
      method: "GET"
      description: "Main dashboard page"
```

**Accessible at:** `http://localhost:8000/plugins/my_dashboard/`

**Web-Only Plugin Features:**
- Provides web endpoints at `/plugins/<name>/`
- Can serve static assets, templates, APIs
- Registers UI panels in the main interface
- No MCP server or tools required
- Uses `web_ui` schema for interface configuration
- Can still have CLI support

### Method 3: Hybrid Plugin (MCP + Web Endpoints)

For plugins that provide both MCP tools and web interfaces:

```python
from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.plugins.web_adapter import PluginWebInterface
from agent_system.config import AgentSystemConfig, MCPConfig
from fastapi import APIRouter
from pathlib import Path

class MyMCPServer(SchemaBasedMCPServer):
    """MCP server component with modern signature."""
    
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        super().__init__(name, system_config, mcp_config)
    
    # Tool methods - automatically routed by generic dispatcher
    async def my_tool(self, params: dict) -> dict:
        """Tool method matching 'my_tool' in schema.yaml."""
        return {"status": "success", "result": "MCP result"}

class MyWebEndpoints(PluginWebInterface):
    """Web endpoints component with modern signature."""
    
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        self.name = name
        self.system_config = system_config
        self.mcp_config = mcp_config

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

    def get_panels(self):
        """Register UI panels"""
        return [{
            "id": f"{self.name}_panel",
            "title": "My Plugin Dashboard",
            "url": f"/plugins/{self.name}/dashboard",
            "icon": "dashboard"
        }]

class MyHybridPlugin:
    """Hybrid plugin combining MCP and web capabilities."""
    
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        """Modern constructor signature for hybrid plugins."""
        self.mcp_server = MyMCPServer(name, system_config, mcp_config)
        self.web_endpoints = MyWebEndpoints(name, system_config, mcp_config)
    
    # MCP interface delegation
    async def call(self, tool: str, params: dict):
        """Delegate to MCP server - generic dispatcher handles routing."""
        return await self.mcp_server.call(tool, params)
    
    def get_tools(self):
        """Delegate to MCP server for tool discovery."""
        return self.mcp_server.get_tools()
    
    # Web interface delegation  
    def get_web_router(self):
        """Delegate to web endpoints for router."""
        return self.web_endpoints.get_web_router()
    
    def get_panels(self):
        """Delegate to web endpoints for UI panels."""
        return self.web_endpoints.get_panels()

PLUGIN_FACTORY = MyHybridPlugin
```

### Modern Plugin Pattern Summary

**Key Changes from Legacy Pattern:**

1. **Constructor Signature**
   - ✅ Modern: `(name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig)`
   - ❌ Legacy: `(name: str, config: dict, ssl_verify: bool = True)`

2. **Tool Routing**
   - ✅ Modern: Implement methods matching tool names, let generic dispatcher route
   - ❌ Legacy: Override `call()` with manual if/elif routing logic

3. **Type Hints**
   - ✅ Modern: Use `| None` for optional types
   - ❌ Legacy: Use `Optional[]` from typing module

4. **Configuration Access**
   - ✅ Modern: Extract from `mcp_config` (plugin-specific) and `system_config` (system-wide)
   - ❌ Legacy: Access `self.config` dictionary

5. **Method Naming**
   - ✅ Modern: Method names MUST match tool names in `schema.yaml` exactly
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
class OldPlugin(SchemaBasedMCPServer):
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
class ModernPlugin(SchemaBasedMCPServer):
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        super().__init__(name, system_config, mcp_config)
        self.timeout = mcp_config.get("timeout", 30)
    
    # No call() override needed - generic dispatcher handles routing!
    
    async def fetch_url(self, params: dict) -> dict:
        """Method name matches tool name - automatically routed."""
        # Implementation...
        pass
    
    async def parse_html(self, params: dict) -> dict:
        """Another tool - also automatically routed."""
        # Implementation...
        pass
```

**Critical Rules:**
1. Method names MUST match tool names in `schema.yaml` exactly
2. If using `{{ name }}` in schema templates, plugin instance name must match method prefixes
3. No manual `call()` override - let the generic dispatcher work
4. Extract config from `mcp_config`, not `self.config`
5. Use modern type hints (`| None` not `Optional[]`)

## Agent-Based Plugins

For plugins that need full agent execution capabilities (conversation, LLM integration, multi-turn interactions), inherit from the `Agent` class instead of implementing MCP server interfaces manually.

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
├── plugin.yaml          # Metadata
├── schema.yaml          # Tool definitions  
├── server.py           # Agent class implementation
└── plugin.py           # Factory function
```

**1. Plugin Metadata (`plugin.yaml`):**
```yaml
name: my_agent
version: 1.0.0
description: "Agent-based plugin for complex tasks"
type: mcp_only
entrypoint: plugin:PLUGIN_FACTORY
dependencies:
  - agent_system>=0.1.0
```

**2. Tool Schema (`schema.yaml`):**
```yaml
{{ name }}_execute_task:
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
        default: ""
    required:
      - task

{{ name }}_list_tools:
  description: "List available tools in this agent"
  parameters:
    type: object
    properties: {}
    required: []
```

**3. Agent Implementation (`server.py`):**
```python
from agent_system.agent.agent import Agent
from agent_system.config import AgentSystemConfig, MCPConfig
from agent_system.mcp.schema_based import SchemaBasedMCPServer


class MyAgent(Agent):
    """Agent-based plugin with full conversation capabilities."""
    
    def __init__(
        self, 
        name: str, 
        system_config: AgentSystemConfig, 
        mcp_config: MCPConfig,
        registry=None,
        llm=None,
        llm_factory=None
    ):
        """
        Modern constructor for agent-based plugins.
        
        Note: Agent plugins have additional parameters (registry, llm, llm_factory)
        beyond the standard plugin signature.
        """
        super().__init__(
            name=name, 
            system_config=system_config,
            mcp_config=mcp_config,
            registry=registry,
            llm=llm,
            llm_factory=llm_factory
        )
        self.schema_server = SchemaBasedMCPServer(name, system_config, mcp_config)
        
    async def get_tools(self):
        """Return available tools from schema."""
        return await self.schema_server.list_tools()
    
    # Tool methods - automatically routed by generic dispatcher
    
    async def execute_task(self, params: dict) -> dict:
        """
        Execute complex task using agent capabilities.
        Method name matches tool name in schema.yaml.
        """
        task = params["task"]
        
        # Extract runtime parameters
        status = params.get("_status")
        token = params.get("_cancellation_token")
        request_id = params.get("request_id") or params.get("requestId")
        context = params.get("context", "")
        
        if status:
            await status.update(f"Planning task: {task}")
        
        # Use agent's conversation capabilities
        prompt = f"Execute this task: {task}"
        if context:
            prompt += f"\n\nContext: {context}"
        
        # Process through agent conversation
        message = {"content": prompt, "role": "user"}
        response = await self.run_conversation([message])
        
        if status:
            await status.update("Task completed")
        
        return {
            "success": True,
            "result": response[-1]["content"] if response else "No response",
            "task": task
        }
    
    async def _list_available_tools(self):
        """List all available tools."""
        tools = await self.get_tools()
        return {
            "tools": [tool["function"]["name"] for tool in tools]
        }
```

**4. Factory Function (`plugin.py`):**
```python
from typing import Optional
from agent_system.agent.config import AgentConfig
from agent_system.mcp.registry import MCPRegistry
from .server import MyAgent


def PLUGIN_FACTORY(
    name: str,
    config: dict,
    registry: MCPRegistry,
    parent_llm: Optional[dict] = None,
    **kwargs
) -> MyAgent:
    """Create and configure the agent plugin."""
    
    # Use parent_llm config if available
    if parent_llm:
        config = config.copy()
        config["llm"] = parent_llm
    
    # Create agent config
    agent_config = AgentConfig(**config)
    
    # Create and register agent
    agent = MyAgent(name=name, config=agent_config.model_dump())
    
    # Bootstrap with registry (gives access to other agents/tools)
    agent.bootstrap_servers(registry)
    
    return agent
```

### Agent vs Schema-Based Plugins

| Aspect | Agent Plugin | Schema-Based Plugin |
|--------|-------------|-------------------|
| **Base Class** | `Agent` | `SchemaBasedMCPServer` |
| **Complexity** | High - full agent capabilities | Low - simple tool execution |
| **LLM Access** | ✅ Built-in conversation | ❌ Manual integration needed |
| **Multi-turn** | ✅ Conversation history | ❌ Stateless calls |
| **Tool Access** | ✅ Can use other agent tools | ❌ Limited to own tools |
| **Use Cases** | Complex reasoning, planning | Simple utilities, API calls |

### Agent Plugin Best Practices

1. **Status Updates**: Always use `status.update()` for long-running tasks
2. **Cancellation**: Check `token.is_cancelled()` in loops
3. **Error Handling**: Wrap agent calls in try/catch blocks
4. **Resource Management**: Properly clean up agent resources
5. **Tool Naming**: Use descriptive tool names with plugin prefix

### Configuration Requirements

tbd.

### Plugin Types Summary

| Plugin Type | MCP Server | Web Endpoints | CLI | Use Cases |
|-------------|------------|---------------|-----|-----------|
| **Agent** | ✅ Required (Agent class) | ❌ Optional | ✅ Recommended | Complex reasoning, multi-turn tasks |
| **MCP-only** | ✅ Required | ❌ Optional | ✅ Recommended | Agent tools, API integrations |
| **Web-only** | ❌ None | ✅ Required | ✅ Recommended | Dashboards, monitoring, admin tools |
| **Hybrid** | ✅ Required | ✅ Required | ✅ Required | Full-featured plugins (like log_viewer) |
| **CLI-only** | ❌ None | ❌ None | ✅ Required | Standalone utilities, converters |

### Server Interface Requirements (MCP Plugins Only)

For MCP-enabled plugins, your server class must implement:

1. **Constructor**: `__init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig)`
2. **Tool Methods**: Implement methods matching tool names from `schema.yaml` (e.g., `async def fetch_url(self, params: dict)`)
3. **Tool Discovery**: Inherit from `SchemaBasedMCPServer` for automatic `list_tools()` or implement manually

**No `call()` override needed** - the generic dispatcher in `MCPServer` automatically routes tool calls to matching methods.

### Runtime Parameters

The agent passes these special parameters in `params`:

- **`_status`**: StatusScope for progress updates
- **`_cancellation_token`**: CancellationToken for cooperative cancellation  
- **`request_id`/`requestId`**: String for correlation and logging

Always extract these early in your tool methods:

```python
async def _my_tool(self, params: dict):
    # Extract runtime parameters
    status = params.get("_status")
    token = params.get("_cancellation_token")
    request_id = params.get("request_id") or params.get("requestId")
    
    # Extract tool parameters
    user_input = params["input"]  # from schema.yaml
    
    # Your implementation...
```

## Tools and Parameters

### Parameter Validation

Always validate inputs before processing:

```python
async def _my_tool(self, params: dict):
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
        "timestamp": datetime.utcnow().isoformat()
    }
}

# Error response
return {
    "status": "error", 
    "error": "Detailed error message",
    "error_code": "INVALID_INPUT",  # Optional
    "request_id": request_id
}

# Cancelled response
return {
    "status": "cancelled",
    "request_id": request_id,
    "forced": token.is_forced if token else False
}
```

## Advanced Features

### Status and Progress Reporting

Use the `_status` parameter to provide real-time feedback:

```python
async def _long_running_tool(self, params: dict):
    status = params.get("_status")
    
    if status:
        await status.progress("Starting analysis...")
    
    # Do some work
    for i, item in enumerate(items):
        if status and i % 10 == 0:
            await status.progress(f"Processing item {i+1}/{len(items)}")
        
        # Process item...
    
    if status:
        await status.info("Analysis complete")
    
    return {"status": "success", "results": results}
```

**Status Methods:**
- `await status.progress("message")` - Progress updates
- `await status.info("message")` - Informational messages  
- `await status.error("message")` - Error notifications
- `await status.warning("message")` - Warning messages

**Best Practices:**
- Keep messages concise and user-friendly
- Don't spam with too many updates (batch them)
- Always return a final result; status is supplementary

### Cooperative Cancellation

**REQUIRED**: All plugins must support cancellation for long-running operations.

#### Why Cancellation Matters
- Users expect "Stop" button to work reliably
- Prevents resource leaks and stuck operations
- Maintains system responsiveness

#### CancellationToken API

```python
token = params.get("_cancellation_token")

# Check if cancellation was requested
if token and token.is_cancelled:
    return {"status": "cancelled"}

# Check if forced termination is happening
if token and token.is_forced:
    # Stop immediately, no cleanup
    return {"status": "cancelled", "forced": True}
```

#### Basic Cancellation Pattern

```python
async def _long_operation(self, params: dict):
    token = params.get("_cancellation_token")
    request_id = params.get("request_id")
    
    # Early exit if already cancelled
    if token and token.is_cancelled:
        return {"status": "cancelled", "request_id": request_id}
    
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
                return {"status": "cancelled", "request_id": request_id}
            
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
from agent_system.utils.cancellation import get_cancellation_manager

async def _tool_with_background_tasks(self, params: dict):
    token = params.get("_cancellation_token")
    request_id = params.get("request_id")
    
    # Create background task
    task = asyncio.create_task(self._background_worker())
    
    # Register with cancellation manager
    manager = get_cancellation_manager()
    tool_request_id = f"{request_id}_{self.task_counter:03d}"
    manager.register_task(tool_request_id, task)
    
    try:
        # Wait for task or cancellation
        while not task.done():
            if token and token.is_cancelled:
                task.cancel()
                return {"status": "cancelled"}
            await asyncio.sleep(0.1)
        
        result = await task
        return {"status": "success", "result": result}
    
    except asyncio.CancelledError:
        return {"status": "cancelled"}
```

#### Cancellation Best Practices

1. **Check early and often** - Before starting work and in loops
2. **Keep cleanup short** - Cleanup callbacks should be < 1 second
3. **Use chunks** - Break long operations into small pieces
4. **Register background tasks** - So they can be force-cancelled
5. **Test cancellation** - Include cancellation tests in your test suite

### Web Endpoints and UI Integration

Plugins can provide custom web interfaces and API endpoints accessible at `/plugins/<plugin_name>/`. This pattern allows plugins like `log_viewer` to serve web dashboards, APIs, and static assets:

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
    
    def get_panels(self) -> List[Dict[str, Any]]:
        """Register UI panels in the main interface"""
        return [{
            "id": f"{self.name}_panel",
            "title": "My Plugin Dashboard",
            "url": f"/plugins/{self.name}/dashboard",
            "icon": "dashboard"
        }]
```

#### Static Assets and Templates

```python
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

class MyWebEndpoints(PluginWebInterface):
    def __init__(self, name: str, config: dict):
        self.name = name
        
        # Setup templates and static files
        self.templates_dir = Path(__file__).parent / "templates"
        self.static_dir = Path(__file__).parent / "static"
        self.templates = Jinja2Templates(directory=str(self.templates_dir))
    
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

### CLI Support (Required)

Every plugin should provide CLI support for development, testing, and standalone use:

#### CLI Structure

Create a `cli.py` file in your plugin directory:

```python
# src/plugins/my_plugin/cli.py
import asyncio
import json
import logging
from argparse import ArgumentParser, Namespace
from .server import MyPluginServer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def run_tool(args: Namespace):
    """Execute plugin tool from command line"""
    server = MyPluginServer("cli", config={"debug": args.debug})
    
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
mcp-my-plugin = "plugins.my_plugin.cli:main"
```

This creates an executable that users can run:
```bash
# After installation, users can run:
my-plugin-cli tool my_tool --input "test data"

# Or with the mcp prefix:
mcp-my-plugin tool my_tool --input "test data"
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
from agent_system.utils.cancellation import get_cancellation_manager
import asyncio

async def _tool_with_subtasks(self, params: dict):
    token = params.get("_cancellation_token")
    request_id = params.get("request_id")
    
    # Create subtasks
    manager = get_cancellation_manager()
    tasks = []
    
    for i in range(3):
        task = asyncio.create_task(self._subtask(i))
        task_id = f"{request_id}_{i:03d}"
        manager.register_task(task_id, task)
        tasks.append(task)
    
    try:
        # Wait for completion or cancellation
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {"status": "success", "results": results}
    
    except asyncio.CancelledError:
        return {"status": "cancelled"}
```

## Configuration and Deployment

### Plugin Configuration

Operators configure plugins in `config/mcp.yaml`:

```yaml
mcp:
  enabled_servers:
    - web_scraper
    - database_client

servers:
  web_scraper:
    type: web_scraper
    timeout: 30
    user_agent: "MyAgent/1.0"
    max_retries: 3
  
  database_client:
    type: database_client
    connection_string: "postgresql://..."
    pool_size: 10
```

### Reading Configuration in Your Plugin

```python
from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.config import AgentSystemConfig, MCPConfig

class WebScraperServer(SchemaBasedMCPServer):
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        super().__init__(name, system_config, mcp_config)
        
        # Extract plugin-specific configuration from mcp_config
        self.timeout = float(mcp_config.get("timeout", 30))
        self.user_agent = mcp_config.get("user_agent", "AgentSystem/1.0")
        self.max_retries = int(mcp_config.get("max_retries", 3))
        
        # Validate configuration
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        
        # Access system-wide configuration (optional)
        # system_config.api_base_url, system_config.log_level, etc.
        
        # Log effective configuration
        self.logger.info(
            f"WebScraping configured: timeout={self.timeout}, "
            f"user_agent={self.user_agent}, max_retries={self.max_retries}"
        )
```

### Environment Variables

For sensitive configuration, use environment variables:

```python
import os
from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.config import AgentSystemConfig, MCPConfig

class APIClientServer(SchemaBasedMCPServer):
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        super().__init__(name, system_config, mcp_config)
        
        # Sensitive config from environment
        self.api_key = os.getenv("API_CLIENT_KEY")
        if not self.api_key:
            raise ValueError("API_CLIENT_KEY environment variable required")
        
        # Non-sensitive config from mcp_config
        self.base_url = mcp_config.get("base_url", "https://api.example.com")
```

## Testing and Quality Assurance

### Test Structure

Tests go in the project root `tests/` directory with naming convention:
```
tests/
├── test_plugin_<plugin_name>_basic.py       # Basic functionality
├── test_plugin_<plugin_name>_integration.py # Integration tests  
├── test_plugin_<plugin_name>_cancellation.py # Cancellation behavior
└── test_plugin_<plugin_name>_config.py      # Configuration handling
```

### Basic Plugin Test

```python
# tests/test_plugin_web_scraper_basic.py
import pytest
from src.plugins.web_scraper.server import WebScraperServer

@pytest.mark.asyncio
async def test_fetch_url_success():
    server = WebScraperServer("web_scraper", {"timeout": 10})
    
    result = await server.call("fetch_url", {
        "url": "https://httpbin.org/json"
    })
    
    assert result["status"] == "success"
    assert "content" in result

@pytest.mark.asyncio 
async def test_invalid_tool():
    server = WebScraperServer("web_scraper", {})
    
    result = await server.call("invalid_tool", {})
    
    assert result["status"] == "error"
    assert "Unknown tool" in result["error"]
```

### Cancellation Testing

```python
# tests/test_plugin_web_scraper_cancellation.py
import asyncio
import pytest
from unittest.mock import Mock
from src.plugins.web_scraper.server import WebScraperServer
from agent_system.utils.cancellation import CancellationToken

@pytest.mark.asyncio
async def test_cancellation_during_operation():
    server = WebScraperServer("web_scraper", {})
    
    # Create a cancellation token
    token = CancellationToken()
    
    # Start long operation
    task = asyncio.create_task(
        server.call("long_operation", {
            "_cancellation_token": token,
            "request_id": "test_123"
        })
    )
    
    # Cancel after short delay
    await asyncio.sleep(0.1)
    await token.cancel()
    
    # Should return cancelled status
    result = await task
    assert result["status"] == "cancelled"
    assert result["request_id"] == "test_123"

@pytest.mark.asyncio
async def test_early_cancellation_check():
    server = WebScraperServer("web_scraper", {})
    
    # Pre-cancelled token
    token = CancellationToken()
    await token.cancel()
    
    # Should return immediately
    result = await server.call("any_tool", {
        "_cancellation_token": token,
        "request_id": "test_456"
    })
    
    assert result["status"] == "cancelled"
    assert result["request_id"] == "test_456"
```

### Status Testing

```python
# Mock status for testing
class MockStatus:
    def __init__(self):
        self.messages = []
    
    async def progress(self, msg):
        self.messages.append(("progress", msg))
    
    async def error(self, msg):
        self.messages.append(("error", msg))

@pytest.mark.asyncio
async def test_status_reporting():
    server = WebScraperServer("web_scraper", {})
    status = MockStatus()
    
    await server.call("fetch_url", {
        "url": "https://example.com",
        "_status": status
    })
    
    # Check that progress was reported
    assert len(status.messages) > 0
    assert any("Fetching" in msg for _, msg in status.messages)
```

### Integration Testing

```python
# tests/test_plugin_web_scraper_integration.py
import pytest
from pathlib import Path
from agent_system.plugins.discovery import discover_all_plugins

def test_plugin_discovery():
    """Test that the plugin is discovered correctly."""
    plugins = discover_all_plugins()
    
    plugin_names = [p[0] for p in plugins]
    assert "web_scraper" in plugin_names

def test_plugin_schema_valid():
    """Test that schema.yaml is valid."""
    from agent_system.plugins.schema_loader import load_schema_from_dir
    
    plugin_dir = Path("src/plugins/web_scraper")
    schema = load_schema_from_dir(plugin_dir)
    
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
# Run all plugin tests
pytest tests/test_plugin_* -v

# Run specific plugin tests
pytest tests/test_plugin_web_scraper_* -v

# Run with coverage
pytest tests/test_plugin_web_scraper_* --cov=src.plugins.web_scraper
```

## Packaging and Distribution

### For Internal Plugins

Internal plugins live in `src/plugins/` and are discovered automatically. No additional packaging needed.

### For External Distribution  

Package as a Python package with entry points:

**MCP Plugin pyproject.toml:**
```toml
[project]
name = "my-agent-plugin"
version = "1.0.0"
description = "My awesome MCP plugin for AgentSystem"
dependencies = [
    "agent-system>=1.0.0",
    "fastapi>=0.100.0",  # If using web endpoints
    "aiohttp>=3.8.0"     # For HTTP requests
]

[project.entry-points]
# MCP server registration
agent_system.mcp_plugins = [
    "my_plugin = my_agent_plugin.server:PLUGIN_FACTORY"
]

[project.scripts]
# CLI executable
mcp-my-plugin = "my_agent_plugin.cli:main"
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
# Just the CLI, no MCP server
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
    └── plugin.yaml
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
✅ **Documentation** - README with examples and troubleshooting  

### Implementation Checklist

**Required for MCP plugins:**
- [ ] `schema.yaml` with proper tool definitions
- [ ] `plugin.yaml` with metadata  
- [ ] Server class extending `SchemaBasedMCPServer`
- [ ] Support for `_status` parameter
- [ ] Support for `_cancellation_token` parameter
- [ ] Input validation and structured error responses
- [ ] CLI support with `cli.py` and pyproject.toml entry point
- [ ] Tests in `tests/test_plugin_<name>_*.py`

**Required for hybrid plugins:**
- [ ] All MCP plugin requirements (above)
- [ ] Web endpoints class extending `PluginWebInterface`
- [ ] `web_ui` section in `schema.yaml` with panel/endpoint configuration
- [ ] `get_web_router()` and `get_panels()` implementation
- [ ] Static assets handling (CSS, JS, images)
- [ ] `plugin.yaml` with `type: hybrid` and `category` metadata

**Required for web-only plugins:**
- [ ] Web endpoints class extending `PluginWebInterface`
- [ ] `web_ui` section in `schema.yaml` (no tools section needed)
- [ ] `get_web_router()` returning FastAPI router with `/plugins/<name>/` prefix
- [ ] `plugin.yaml` with `type: web_only` and `category` metadata
- [ ] Static assets handling (CSS, JS, images)
- [ ] UI panels registration via `get_panels()`
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
- [ ] README with usage examples
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

## Troubleshooting

### Common Issues

**Plugin not discovered:**
- Check `plugin.yaml` exists and has correct `entrypoint`
- Verify `PLUGIN_FACTORY` is defined in your server module
- Use `python -m agent_system.agent_cli plugins` to list discovered plugins

**Tools not working:**
- Validate `schema.yaml` syntax (use online YAML validator)
- Check tool names match between schema and `call()` method
- Verify `list_tools()` returns the correct schema

**Cancellation not working:**
- Ensure you're checking `token.is_cancelled` in loops
- Register cleanup callbacks with `token.add_cleanup_callback()`
- Background tasks must be registered with CancellationManager

**Status updates not appearing:**
- Check that you're using `await status.progress()`
- Verify `_status` parameter is being passed to your tool
- Don't send too many rapid updates (batch them)

**Configuration issues:**
- Log effective configuration in `__init__()`
- Check `config/mcp.yaml` has your plugin enabled
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
server = MyServer("test", {"debug": True})
result = await server.call("my_tool", {"param": "value"})
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

schema = load_schema_from_dir(Path("src/plugins/my_plugin"))
print(schema)
```

### Getting Help

- Check existing plugins in `src/plugins/` for examples
- Read the MCP specification for protocol details  
- Check logs in `logs/` directory for error details
- Use `python -m agent_system.agent_cli plugins --help` for CLI options

---

## Hooks-Only Plugins

In addition to MCP tool plugins, AgentSystem supports **hooks-only plugins** that intercept agent lifecycle points without providing tools. This is ideal for cross-cutting concerns like logging, validation, context management, and monitoring.

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

Hooks can intercept these lifecycle points:

1. **SESSION_START** - Agent session begins
2. **SESSION_END** - Agent session ends
3. **PRE_LLM_CALL** - Before sending messages to LLM
4. **POST_LLM_CALL** - After receiving LLM response
5. **PRE_TOOL_CALL** - Before executing a tool
6. **POST_TOOL_CALL** - After tool execution
7. **FORMAT_OUTPUT** - Before returning output to user

### Schema-Based Hooks Pattern

The recommended pattern uses `SchemaBasedPluginHook` with declarative YAML configuration.

**Directory Structure:**
```
src/plugins/my_hook_plugin/
├── plugin.py        # Factory function
├── hooks.py         # Hook implementation
├── schema.yaml      # Hook definitions + config
└── README.md        # Documentation
```

**Example: schema.yaml**
```yaml
# Hook definitions
hooks:
  - name: my_handler        # Must match method name exactly
    type: pre_llm_call
    enabled: true
    timeout: 30.0
    description: "What this hook does"
    order:
      after: ["begin"]
      before: ["end"]

# Configuration schema
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
    
    def __init__(self, plugin_dir: Path | str):
        super().__init__(plugin_dir)
        
        # Load config from schema
        config = self.get_config()
        self.max_items = config.get('max_items', {}).get('default', 100)
        self.enabled = config.get('enable_feature', {}).get('default', True)
    
    # Handler name MUST match hook name in schema.yaml
    async def my_handler(self, context: HookContext) -> HookResult:
        """Handle pre-LLM call hook.
        
        Args:
            context: Hook execution context with messages, agent, metadata
            
        Returns:
            HookResult with success status and optionally modified context
        """
        try:
            # Access context data
            messages = context.messages or []
            session_id = context.session_id
            
            # Perform hook logic
            if self.enabled and len(messages) > self.max_items:
                # Modify context (example)
                modified_messages = messages[-self.max_items:]
                
                # Create modified context
                modified_context = HookContext(
                    hook_type=context.hook_type,
                    request_id=context.request_id,
                    session_id=context.session_id,
                    agent=context.agent,
                    messages=modified_messages,
                    llm_response=context.llm_response,
                    metadata=context.metadata
                )
                
                return HookResult(
                    success=True,
                    modified=True,  # We modified the context
                    context=modified_context,
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

def PLUGIN_FACTORY() -> MyHookPlugin:
    """Factory function for plugin discovery."""
    plugin_dir = Path(__file__).parent
    return MyHookPlugin(plugin_dir)
```

### Hook Ordering

Hooks can specify execution order using named dependencies:

```yaml
hooks:
  - name: optimize_context
    type: pre_llm_call
    order:
      after: ["begin"]                    # Run after these hooks
      before: ["summarize", "validate"]   # Run before these hooks
```

**Special Order Names:**
- `begin`: Virtual hook at start (always first)
- `end`: Virtual hook at end (always last)

**Example Chain:**
```yaml
# Execution order: begin -> optimize -> summarize -> validate -> end
hooks:
  - name: optimize_context
    order:
      after: ["begin"]
      before: ["summarize_context"]
  
  - name: summarize_context
    order:
      after: ["optimize_context"]
      before: ["validate_messages"]
  
  - name: validate_messages
    order:
      after: ["summarize_context"]
      before: ["end"]
```

### Global Configuration

Override hook behavior in `config/plugins.yaml`:

```yaml
hooks:
  enabled: true
  default_timeout: 30.0
  overrides:
    my_hook_plugin.my_handler:
      enabled: false           # Disable this specific hook
      timeout: 60.0           # Override timeout
      order:
        after: ["other_hook"] # Override order
      config:
        max_items: 50         # Override config values
```

### HookContext Reference

```python
@dataclass
class HookContext:
    hook_type: HookType              # Type of hook
    request_id: str                  # Unique request ID
    session_id: Optional[str]        # Session identifier
    agent: Optional[Any]             # Agent instance
    agent_name: Optional[str]        # Agent name
    messages: Optional[List[Dict]]   # Conversation messages
    llm_response: Optional[Any]      # LLM response (post-LLM only)
    tool_call: Optional[Dict]        # Tool info (tool hooks only)
    tool_result: Optional[Any]       # Tool result (post-tool only)
    output: Optional[str]            # Output (format hook only)
    metadata: Optional[Dict]         # Additional metadata
    step: Optional[int]              # Execution step
    llm: Optional[Any]               # LLM instance
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
    
    for msg in messages:
        if 'role' not in msg or 'content' not in msg:
            return HookResult(
                success=False,
                modified=False,
                context=context,
                error="Invalid message format: missing role or content"
            )
    
    return HookResult(success=True, modified=False, context=context)
```

**Transformation Hook:**
```python
async def add_timestamp(self, context: HookContext) -> HookResult:
    """Add timestamps to messages."""
    messages = context.messages or []
    modified_messages = []
    
    for msg in messages:
        if 'timestamp' not in msg:
            msg['timestamp'] = datetime.now().isoformat()
        modified_messages.append(msg)
    
    modified_context = HookContext(
        hook_type=context.hook_type,
        request_id=context.request_id,
        session_id=context.session_id,
        agent=context.agent,
        messages=modified_messages,
        llm_response=context.llm_response,
        metadata=context.metadata
    )
    
    return HookResult(
        success=True,
        modified=True,
        context=modified_context,
        metadata={'timestamps_added': len(messages)}
    )
```

### Testing Hooks

```python
import pytest
from agent_system.hooks import HookContext, HookType
from plugins.my_hook_plugin.plugin import PLUGIN_FACTORY

@pytest.fixture
def plugin():
    return PLUGIN_FACTORY()

@pytest.mark.asyncio
async def test_my_handler(plugin):
    """Test hook handler."""
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id='test-123',
        session_id='session-1',
        messages=[
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Hi there!'}
        ]
    )
    
    result = await plugin.my_handler(context)
    
    assert result.success is True
    assert result.modified is False  # Or True if modified
    assert result.error is None
```

### Complete Hook Plugin Examples

See these example implementations:

- **[context_optimizer](../src/plugins/context_optimizer/)** - Basic context optimization (truncation, deduplication)
- **[context_summarizer](../src/plugins/context_summarizer/)** - Intelligent LLM-based summarization
- **[message_validator](../src/plugins/message_validator/)** - Message format validation
- **[request_logger](../src/plugins/request_logger/)** - Request/response logging with timing

### Hybrid Plugins (Tools + Hooks)

You can combine tools and hooks in a single plugin:

```python
from agent_system.mcp import MCPServer
from agent_system.hooks import PluginHook, HookContext, HookResult

class MyHybridPlugin(MCPServer, PluginHook):
    """Plugin with both tools and hooks."""
    
    def __init__(self, name: str, config: dict = None):
        MCPServer.__init__(self, name, config)
        PluginHook.__init__(self, name, config)
    
    # MCP Tools
    async def call(self, name: str, arguments: dict) -> dict:
        if name == "my_tool":
            return await self._my_tool(**arguments)
        raise ValueError(f"Unknown tool: {name}")
    
    async def _my_tool(self, param: str) -> dict:
        """Example tool."""
        return {"result": f"Processed: {param}"}
    
    # Hook Handlers
    async def on_session_start(self, context: HookContext) -> HookResult:
        """Initialize at session start."""
        self.session_data = {}
        return HookResult(success=True, modified=False, context=context)
```

For complete hooks documentation, see [Plugin Hook System](./plugin_hooks.md).

---

**Ready to build your plugin?** Start with the [Quick Start](#quick-start-your-first-plugin) section and refer back to specific sections as needed.

