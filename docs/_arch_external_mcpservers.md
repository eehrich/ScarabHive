# Software Architecture Document: External MCP Servers

**Document Type:** Software Architecture Document (SAD)  
**Component:** External MCP Server Integration  
**Version:** 1.0  
**Last Updated:** 2025-01-15  
**Status:** Active

---

## Table of Contents

1. [Overview](#overview)
2. [Architectural Goals](#architectural-goals)
3. [MCP Protocol](#mcp-protocol)
4. [Component Architecture](#component-architecture)
5. [Connection Management](#connection-management)
6. [Tool Integration](#tool-integration)
7. [Transport Protocols](#transport-protocols)
8. [Key Design Decisions](#key-design-decisions)
9. [Data Flow](#data-flow)
10. [Security](#security)
11. [Related Documents](#related-documents)

---

## 1. Overview

### 1.1 Purpose

The External MCP Server Integration enables AgentSystem to:
- Connect to remote MCP (Model Context Protocol) servers
- Discover and use external tools
- Aggregate tools from multiple sources
- Provide unified tool interface to agents
- Support Smithery.ai and custom MCP servers

### 1.2 Scope

This document covers:
- MCP client architecture
- Connection management and health monitoring
- Tool discovery and caching
- Transport protocols (HTTP streaming)
- Security and authentication
- Configuration management

### 1.3 Audience

- System architects
- Backend developers
- DevOps engineers
- Integration developers

---

## 2. Architectural Goals

### 2.1 Design Principles

| Principle | Description | Priority |
|-----------|-------------|----------|
| **Reliability** | Graceful degradation on server failures | High |
| **Performance** | Tool list caching, parallel connections | High |
| **Flexibility** | Support multiple MCP server types | High |
| **Security** | Authentication, TLS, input validation | High |
| **Observability** | Health monitoring, connection status | Medium |
| **Simplicity** | Standard MCP protocol compliance | Medium |

### 2.2 Quality Goals

- **Availability:** Continue operation if some MCP servers fail
- **Latency:** < 200ms for cached tool lists, < 2s for remote calls
- **Scalability:** Support 10+ concurrent MCP server connections
- **Maintainability:** Clear error messages, logging, debugging

### 2.3 Non-Goals

- MCP server hosting (we are a client only)
- Custom protocol extensions beyond MCP standard
- Tool execution caching (only tool list caching)
- Bidirectional streaming (HTTP streaming sufficient)

---

## 3. MCP Protocol

### 3.1 Protocol Overview

**MCP (Model Context Protocol)** is a standardized protocol for:
- Tool discovery (`tools/list`)
- Tool execution (`tools/call`)
- Resource access (not used in AgentSystem)
- Prompts (not used in AgentSystem)

**Official Spec:** https://spec.modelcontextprotocol.io/

### 3.2 Protocol Messages

#### Initialize
```json
{
  "jsonrpc": "2.0",
  "method": "initialize",
  "params": {
    "protocolVersion": "2024-11-05",
    "capabilities": {
      "tools": {}
    },
    "clientInfo": {
      "name": "AgentSystem",
      "version": "1.0.0"
    }
  },
  "id": 1
}
```

#### List Tools
```json
{
  "jsonrpc": "2.0",
  "method": "tools/list",
  "params": {},
  "id": 2
}

Response:
{
  "jsonrpc": "2.0",
  "result": {
    "tools": [
      {
        "name": "web_search",
        "description": "Search the web",
        "inputSchema": {
          "type": "object",
          "properties": {
            "query": {"type": "string"}
          },
          "required": ["query"]
        }
      }
    ]
  },
  "id": 2
}
```

#### Call Tool
```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "web_search",
    "arguments": {
      "query": "MCP protocol"
    }
  },
  "id": 3
}

Response:
{
  "jsonrpc": "2.0",
  "result": {
    "content": [
      {
        "type": "text",
        "text": "Search results: ..."
      }
    ]
  },
  "id": 3
}
```

---

## 4. Component Architecture

### 4.1 High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      AgentSystem                                 │
│                                                                  │
│  ┌────────────────────────────────────────────────────────┐    │
│  │               MCP Integration                           │    │
│  │  ┌──────────────┐  ┌──────────────┐  ┌────────────┐  │    │
│  │  │ Tool Cache   │  │Client Manager│  │HTTP Server │  │    │
│  │  └──────────────┘  └──────────────┘  └────────────┘  │    │
│  └────────────────────────────────────────────────────────┘    │
│                          │                                       │
└──────────────────────────┼───────────────────────────────────────┘
                           │
         ┌─────────────────┼─────────────────┐
         │                 │                 │
         ▼                 ▼                 ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ MCP Server 1 │  │ MCP Server 2 │  │ MCP Server 3 │
│ (Smithery)   │  │ (Custom)     │  │ (Local)      │
└──────────────┘  └──────────────┘  └──────────────┘
```

### 4.2 Core Components

#### 4.2.1 MCPIntegration

**File:** `src/agent_system/mcp/integration.py`

**Responsibilities:**
- Initialize MCP subsystem
- Coordinate client manager and HTTP server
- Manage plugin registry
- Provide unified interface

**Key Methods:**
```python
class MCPIntegration:
    async def initialize(self, config: AgentSystemConfig):
        """Initialize MCP integration"""
    
    async def connect_external_servers(self):
        """Connect to configured external MCP servers"""
    
    async def get_all_tools(self) -> List[ToolInfo]:
        """Get aggregated tool list from all sources"""
    
    async def call_tool(
        self,
        tool_name: str,
        arguments: dict
    ) -> dict:
        """Execute tool (routes to correct server)"""
```

#### 4.2.2 MCPClientManager

**File:** `src/agent_system/mcp/client.py`

**Responsibilities:**
- Manage connections to external MCP servers
- Connection lifecycle (connect, disconnect, health check)
- Tool list caching
- Parallel connection support

**Key Methods:**
```python
class MCPClientManager:
    async def connect_server(
        self,
        name: str,
        config: RemoteMCPConfig
    ):
        """Connect to external MCP server"""
    
    async def disconnect_server(self, name: str):
        """Disconnect from server"""
    
    async def list_tools(
        self,
        server_name: str
    ) -> List[ToolInfo]:
        """Get tool list (cached)"""
    
    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict
    ) -> dict:
        """Execute tool on server"""
    
    def get_connection_status(self, name: str) -> str:
        """Get connection status (connected/disconnected/error)"""
```

#### 4.2.3 ToolCache

**File:** `src/agent_system/mcp/tool_cache.py`

**Responsibilities:**
- Cache tool lists from external servers
- TTL-based invalidation
- Memory management (max size)

**Key Methods:**
```python
class ToolCache:
    def get(self, server_name: str) -> Optional[List[ToolInfo]]:
        """Get cached tool list"""
    
    def set(
        self,
        server_name: str,
        tools: List[ToolInfo],
        ttl: float = 3600.0
    ):
        """Cache tool list with TTL"""
    
    def invalidate(self, server_name: str):
        """Invalidate cache for server"""
    
    def clear(self):
        """Clear entire cache"""
```

## 5. Connection Management

### 5.1 Connection Lifecycle

```
Configuration Load
    │
    ▼
MCPIntegration.connect_external_servers()
    │
    ├─► For each enabled remote server:
    │   │
    │   ├─► MCPClientManager.connect_server()
    │   │   │
    │   │   ├─► Create HTTP client
    │   │   ├─► Send initialize request
    │   │   ├─► Validate response
    │   │   ├─► Store connection
    │   │   │
    │   │   ▼
    │   │   Connected / Error
    │   │
    │   ▼
    │   Connection Pool Updated
    │
    ▼
All Servers Connected (best effort)
```

### 5.2 Health Monitoring

**Periodic Health Checks:**
```python
async def health_check_loop():
    """Periodic health checks for connected servers"""
    while True:
        for server_name, client in clients.items():
            try:
                # Ping with tools/list
                await client.list_tools()
                status = "connected"
            except Exception as e:
                logger.warning(f"Server {server_name} unhealthy: {e}")
                status = "disconnected"
                # Optionally: attempt reconnect
        
        await asyncio.sleep(60)  # Check every minute
```

### 5.3 Parallel Connection

**Configuration in `external_servers:` section:**
```yaml
external_servers:
  connection:
    timeout: 5.0
    parallel_connect: true  # Connect to all servers concurrently
```

**Implementation:**
```python
async def connect_external_servers(self):
    """Connect to all configured servers in parallel"""
    tasks = []
    for name, config in self.configured_external_servers.items():
        if config.enabled:
            task = self.client_manager.connect_server(name, config)
            tasks.append(task)
    
    # Wait for all connections (with timeout)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Log results
    for name, result in zip(server_names, results):
        if isinstance(result, Exception):
            logger.error(f"Failed to connect to {name}: {result}")
        else:
            logger.info(f"Connected to {name}")
```

---

## 6. Tool Integration

### 6.1 Tool Discovery

**Aggregated Tool List:**
```python
async def get_all_tools(self) -> List[ToolInfo]:
    """Get tools from all sources"""
    
    all_tools = []
    
    # 1. Get tools from internal plugins
    for plugin_name in plugin_registry.list_servers():
        plugin_tools = await plugin_registry.get_server(plugin_name).list_tools()
        all_tools.extend(plugin_tools)
    
    # 2. Get tools from external MCP servers
    for server_name in client_manager.list_servers():
        try:
            server_tools = await client_manager.list_tools(server_name)
            all_tools.extend(server_tools)
        except Exception as e:
            logger.warning(f"Failed to get tools from {server_name}: {e}")
            # Continue with other servers
    
    return all_tools
```

### 6.2 Tool Routing

**Determine Tool Source:**
```python
async def call_tool(self, tool_name: str, arguments: dict) -> dict:
    """Route tool call to correct source"""
    
    # Check internal plugins first
    for plugin_name in plugin_registry.list_servers():
        plugin_tools = await plugin_registry.get_server(plugin_name).list_tools()
        if any(t["name"] == tool_name for t in plugin_tools):
            return await plugin_registry.get_server(plugin_name).call_tool(
                tool_name, arguments
            )
    
    # Check external MCP servers
    for server_name in client_manager.list_servers():
        server_tools = await client_manager.list_tools(server_name)
        if any(t["name"] == tool_name for t in server_tools):
            return await client_manager.call_tool(
                server_name, tool_name, arguments
            )
    
    raise ToolNotFoundError(f"Tool '{tool_name}' not found")
```

### 6.3 Tool Caching

**Cache Strategy:**

| Item | Cache Location | TTL | Invalidation |
|------|----------------|-----|--------------|
| **Tool Lists** | `ToolCache` | 3600s (1 hour) | Manual, TTL |
| **Tool Results** | Not cached | N/A | N/A |

**Configuration in `external_servers:` section:**
```yaml
external_servers:
  cache:
    enabled: true
    tool_list_ttl: 3600.0  # 1 hour
    max_size: null         # Unlimited
```

---

## 7. Transport Protocols

### 7.1 HTTP Streaming (Primary)

**Why HTTP Streaming?**
- Widely supported
- Firewall-friendly
- Standard HTTP libraries
- SSE for server-to-client events

**Request/Response:**
```python
# Client → Server (JSON-RPC over HTTP POST)
POST /mcp/tools/call
Content-Type: application/json

{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {...},
  "id": 1
}

# Server → Client
HTTP/1.1 200 OK
Content-Type: application/json

{
  "jsonrpc": "2.0",
  "result": {...},
  "id": 1
}
```

### 7.2 SSE (Server-Sent Events)

**Use Case:** Long-running tools with progress updates

**Flow:**
```
Client                     MCP Server
  │                            │
  ├──GET /mcp/sse─────────────►│
  │                            │
  │◄───SSE: data: {progress}──┤
  │◄───SSE: data: {progress}──┤
  │◄───SSE: data: {result}────┤
  │                            │
  └────────────────────────────┘
```

### 7.3 Configuration

**Server Configuration in `external_servers:` section:**
```yaml
external_servers:
  remote_servers:
    my_server:
      url: https://server.example.com/mcp
      enabled: true
      description: My MCP Server
      transport: streaming  # HTTP streaming
      features:
        tools: true
      initialization_options:
        api_key: ${MCP_API_KEY}
```

---

## 8. Key Design Decisions

### ADR-001: HTTP Streaming Transport

**Context:** Need transport for external MCP servers  
**Decision:** Use HTTP streaming (JSON-RPC over HTTP)  
**Rationale:**
- Standard protocol (MCP spec compliant)
- No persistent connections needed
- Works through firewalls/proxies
- SSE for progress updates

**Status:** Accepted

---

### ADR-002: Tool List Caching

**Context:** Repeated tool discovery is expensive  
**Decision:** Cache tool lists with TTL  
**Rationale:**
- Tool lists change infrequently
- Reduces latency (< 1ms for cached)
- Reduces load on external servers
- Configurable TTL and max size

**Status:** Accepted

---

### ADR-003: Graceful Degradation

**Context:** External servers may fail  
**Decision:** Continue operation with remaining servers  
**Rationale:**
- Reliability (partial availability > no availability)
- Log errors but don't crash
- Agent can work with internal tools if external fail

**Status:** Accepted

---

### ADR-004: Parallel Connection

**Context:** Sequential connection is slow  
**Decision:** Connect to all servers concurrently  
**Rationale:**
- Faster startup (5s → 1s with 5 servers)
- Better user experience
- Configurable via `parallel_connect` flag

**Status:** Accepted

---

### ADR-005: Client-Only Mode

**Context:** MCP can be server or client  
**Decision:** AgentSystem is an MCP client only.  
**Rationale:**
- Primary use case: consume external tools
- Simpler architecture

**Status:** Accepted. Server mode existed as an option (Epic 0037) and was
removed in 2026-08 — it never had a consumer, and an unused HTTP surface that
exposes every activated plugin is a liability, not a feature. Everything that
remains under `/mcp/*` serves the internal plugin registry, not the protocol.

---

## 9. Data Flow

### 9.1 Tool Discovery Flow

```
Agent Requests Tools
    │
    ▼
MCPIntegration.get_all_tools()
    │
    ├─► Internal Plugins
    │   │
    │   ├─► Plugin 1: list_tools()
    │   ├─► Plugin 2: list_tools()
    │   │
    │   ▼
    │   Internal Tools
    │
    ├─► External MCP Servers
    │   │
    │   ├─► Check ToolCache
    │   │   │
    │   │   ├─► Cache HIT → Return cached
    │   │   ├─► Cache MISS → Query server
    │   │       │
    │   │       ├─► POST /mcp/tools/list
    │   │       ├─► Parse response
    │   │       ├─► Cache results (TTL=3600s)
    │   │       │
    │   │       ▼
    │   │   External Tools
    │   │
    │   ▼
    │   Aggregated External Tools
    │
    ▼
Merged Tool List (Internal + External)
    │
    ▼
Return to Agent
```

### 9.2 Tool Execution Flow

```
Agent Calls Tool
    │
    ▼
MCPIntegration.call_tool(tool_name, args)
    │
    ├─► Determine Tool Source
    │   │
    │   ├─► Check Internal Plugins
    │   │   │
    │   │   └─► Found? → Execute locally
    │   │
    │   ├─► Check External MCP Servers
    │   │   │
    │   │   ├─► Query cached tool lists
    │   │   ├─► Find server with tool
    │   │   │
    │   │   ▼
    │   │   Route to Server
    │   │
    │   └─► Not Found? → Error
    │
    ▼
Execute Tool
    │
    ├─► Internal Plugin
    │   │
    │   ├─► Plugin.call_tool(tool_name, args)
    │   │
    │   ▼
    │   Result
    │
    ├─► External MCP Server
    │   │
    │   ├─► POST /mcp/tools/call
    │   ├─► {
    │   │     "method": "tools/call",
    │   │     "params": {
    │   │       "name": "tool_name",
    │   │       "arguments": {...}
    │   │     }
    │   │   }
    │   │
    │   ├─► Parse response
    │   │
    │   ▼
    │   Result
    │
    ▼
Return Result to Agent
```

---

## 10. Security

### 10.1 Authentication

**Supported Methods:**

| Method | Configuration | Use Case |
|--------|---------------|----------|
| **API Key** | `initialization_options.api_key` | Smithery.ai servers |
| **Bearer Token** | `initialization_options.bearer_token` | Custom servers |
| **None** | No auth config | Local/trusted servers |

**Example:**
```yaml
remote_servers:
  smithery_server:
    url: https://server.smithery.ai/mcp?api_key=${API_KEY}
    initialization_options:
      api_key: ${API_KEY}  # From environment
```

### 10.2 TLS/HTTPS

**Requirements:**
- All external connections MUST use HTTPS in production
- Certificate validation enabled by default
- Optional: Custom CA certificates

**Configuration:**
```yaml
external_servers:
  connection:
    verify_ssl: true  # Default
    ca_bundle: /path/to/ca-bundle.crt  # Optional
```

### 10.3 Input Validation

**Tool Arguments:**
```python
async def call_tool(self, tool_name: str, arguments: dict):
    # Validate against tool schema
    tool_info = await self.get_tool_info(tool_name)
    schema = tool_info["inputSchema"]
    
    # JSON Schema validation
    validate(instance=arguments, schema=schema)
    
    # Execute if valid
    return await self._execute_tool(tool_name, arguments)
```

### 10.4 Rate Limiting

**Per-Server Limits:**
```yaml
remote_servers:
  smithery_server:
    rate_limit:
      requests_per_minute: 60
      burst: 10
```

---

## 11. Related Documents

### 11.1 Architecture Documents

- [System Architecture](agent_system_architecture.md) - Overall system
- [Plugin Architecture](plugin_architecture.md) - Internal plugins
- [App Architecture](app_architecture.md) - FastAPI application

### 11.2 Design Documents

- [MCP Configuration](mcp_configuration.md) - Configuration guide
- [Tool Execution](tool_execution.md) - Tool system
- [Caching Systems](caching_systems.md) - Cache design

### 11.3 External References

- [MCP Specification](https://spec.modelcontextprotocol.io/) - Official MCP protocol spec
- [Smithery.ai](https://smithery.ai/) - MCP server marketplace
- [MCP Servers List](https://github.com/modelcontextprotocol/servers) - Official MCP servers

---

**Document Changelog:**

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2025-01-15 | AgentSystem Team | Initial external MCP servers SAD |

---

**Approval:**

| Role | Name | Date | Signature |
|------|------|------|-----------|
| Architect | - | - | - |
| Tech Lead | - | - | - |
| Product Owner | - | - | - |
