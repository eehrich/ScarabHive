# Plugin Authoring Guide

This document explains how to create plugins (MCP servers) for AgentSystem. It walks you through the entire process: from the initial idea to implementation and deployment.

## Table of Contents

- [What is a Plugin?](#what-is-a-plugin)
- [Quick Start: Your First Plugin](#quick-start-your-first-plugin)
- [Plugin Structure and Layout](#plugin-structure-and-layout)
- [Defining Schema (`schema.yaml`)](#defining-schema-schemayaml)
- [Defining Metadata (`plugin.yaml`)](#defining-metadata-pluginyaml)
- [Implementing the Server](#implementing-the-server)
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

class HelloWorldServer(SchemaBasedMCPServer):
    def __init__(self, name, config, ssl_verify=True):
        super().__init__(name, config, ssl_verify)
        
        # Read configuration with defaults
        self.greeting_prefix = self.config.get("greeting_prefix", "Hello")
        
        # Log effective configuration
        self.logger.info(f"HelloWorld configured: prefix='{self.greeting_prefix}'")
    
    async def call(self, tool: str, params: dict):
        if tool == "say_hello":
            name = params.get("name", "World")
            return {
                "status": "success", 
                "message": f"{self.greeting_prefix}, {name}!"
            }
        return {"status": "error", "error": f"Unknown tool: {tool}"}

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

### Method 1: Schema-Based Server (Recommended)

Use `SchemaBasedMCPServer` for automatic schema loading and consistent behavior:

```python
from agent_system.mcp.schema_based import SchemaBasedMCPServer

class WebScrapingServer(SchemaBasedMCPServer):
    def __init__(self, name, config, ssl_verify=True):
        super().__init__(name, config, ssl_verify)
        
        # Read configuration with validation
        self.timeout = float(self.config.get("timeout", 30))
        self.user_agent = self.config.get("user_agent", "AgentSystem/1.0")
        self.max_retries = int(self.config.get("max_retries", 3))
        
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        
        # Log effective configuration
        self.logger.info(f"WebScraping configured: timeout={self.timeout}, "
                        f"user_agent={self.user_agent}, max_retries={self.max_retries}")

    async def call(self, tool: str, params: dict) -> dict:
        """Route tool calls to appropriate handlers."""
        if tool == "fetch_url":
            return await self._fetch_url(params)
        elif tool == "parse_html":
            return await self._parse_html(params)
        else:
            return {"status": "error", "error": f"Unknown tool: {tool}"}

    async def _fetch_url(self, params: dict) -> dict:
        """Fetch content from a URL."""
        url = params["url"]
        
        # Extract runtime parameters
        status = params.get("_status")
        token = params.get("_cancellation_token")
        request_id = params.get("request_id")
        
        if status:
            await status.progress(f"Fetching {url}")
        
        # Check cancellation before starting
        if token and token.is_cancelled:
            return {"status": "cancelled", "request_id": request_id}
        
        try:
            # Implementation with proper error handling...
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(self.timeout)) as session:
                async with session.get(url, headers={"User-Agent": self.user_agent}) as response:
                    content = await response.text()
                    
            return {
                "status": "success", 
                "content": content,
                "url": url,
                "status_code": response.status
            }
        except Exception as e:
            if status:
                await status.error(f"Failed to fetch {url}: {e}")
            return {"status": "error", "error": str(e), "request_id": request_id}

PLUGIN_FACTORY = WebScrapingServer
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

class DashboardWebEndpoints(PluginWebInterface):
    """Web endpoints for dashboard plugin"""
    
    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        
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

class DashboardPlugin:
    """Web-only plugin (no MCP server)"""
    
    def __init__(self, name: str, config: dict, ssl_verify: bool = True):
        self.web_endpoints = DashboardWebEndpoints(name, config)
    
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
- Registers UI panels in main interface
- No MCP server or tools required
- Uses `web_ui` schema for interface configuration
- Can still have CLI support

### Method 3: Hybrid Plugin (MCP + Web Endpoints)

For plugins that provide both MCP tools and web interfaces:

```python
from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.plugins.web_adapter import PluginWebInterface
from fastapi import APIRouter
from pathlib import Path

class MyMCPServer(SchemaBasedMCPServer):
    """MCP server component"""
    async def call(self, tool: str, params: dict):
        if tool == "my_tool":
            return {"status": "success", "result": "MCP result"}
        return {"status": "error", "error": f"Unknown tool: {tool}"}

class MyWebEndpoints(PluginWebInterface):
    """Web endpoints component"""
    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config

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
    """Hybrid plugin combining MCP and web capabilities"""
    def __init__(self, name: str, config: dict, ssl_verify: bool = True):
        self.mcp_server = MyMCPServer(name, config, ssl_verify)
        self.web_endpoints = MyWebEndpoints(name, config)
    
    # MCP interface delegation
    async def call(self, tool: str, params: dict):
        return await self.mcp_server.call(tool, params)
    
    def get_tools(self):
        return self.mcp_server.get_tools()
    
    # Web interface delegation  
    def get_web_router(self):
        return self.web_endpoints.get_web_router()
    
    def get_panels(self):
        return self.web_endpoints.get_panels()

PLUGIN_FACTORY = MyHybridPlugin
```

### Plugin Types Summary

| Plugin Type | MCP Server | Web Endpoints | CLI | Use Cases |
|-------------|------------|---------------|-----|-----------|
| **MCP-only** | ✅ Required | ❌ Optional | ✅ Recommended | Agent tools, API integrations |
| **Web-only** | ❌ None | ✅ Required | ✅ Recommended | Dashboards, monitoring, admin tools |
| **Hybrid** | ✅ Required | ✅ Required | ✅ Required | Full-featured plugins (like log_viewer) |
| **CLI-only** | ❌ None | ❌ None | ✅ Required | Standalone utilities, converters |

### Server Interface Requirements (MCP Plugins Only)

For MCP-enabled plugins, your server class must implement:

1. **`__init__(self, name, config, ssl_verify=True)`** - Constructor
2. **`async def call(self, tool: str, params: dict)`** - Tool execution
3. **`async def list_tools(self)`** - Return available tools (or inherit from SchemaBasedMCPServer)

### Runtime Parameters

The agent passes these special parameters in `params`:

- **`_status`**: StatusScope for progress updates
- **`_cancellation_token`**: CancellationToken for cooperative cancellation  
- **`request_id`/`requestId`**: String for correlation and logging

Always extract these early in your tool handlers:

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
from agent_system.core.cancellation import get_cancellation_manager

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
    
    def get_panels(self):
        """Register UI panels in the main interface"""
        return [{
            "id": f"{self.name}_panel",
            "title": "My Plugin Dashboard",
            "url": f"/plugins/{self.name}/dashboard",
            "icon": "settings",
            "position": "right",
            "width": "400px"
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
from agent_system.core.cancellation import get_cancellation_manager
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
class WebScraperServer(SchemaBasedMCPServer):
    def __init__(self, name, config, ssl_verify=True):
        super().__init__(name, config, ssl_verify)
        
        # Read configuration with defaults
        self.timeout = float(self.config.get("timeout", 30))
        self.user_agent = self.config.get("user_agent", "AgentSystem/1.0")
        self.max_retries = int(self.config.get("max_retries", 3))
        
        # Validate configuration
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        
        # Log effective configuration
        self.logger.info(
            f"WebScraper configured: timeout={self.timeout}, "
            f"user_agent={self.user_agent}, max_retries={self.max_retries}"
        )
```

### Environment Variables

For sensitive configuration, use environment variables:

```python
import os

class APIClientServer(SchemaBasedMCPServer):
    def __init__(self, name, config, ssl_verify=True):
        super().__init__(name, config, ssl_verify)
        
        # Sensitive config from environment
        self.api_key = os.getenv("API_CLIENT_KEY")
        if not self.api_key:
            raise ValueError("API_CLIENT_KEY environment variable required")
        
        # Non-sensitive config from mcp.yaml
        self.base_url = self.config.get("base_url", "https://api.example.com")
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
from agent_system.core.cancellation import CancellationToken

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
- Use `python -m agent_system.cli plugins` to list discovered plugins

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
python -m agent_system.cli plugins list --format json
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
- Use `python -m agent_system.cli plugins --help` for CLI options

---

**Ready to build your plugin?** Start with the [Quick Start](#quick-start-your-first-plugin) section and refer back to specific sections as needed.
