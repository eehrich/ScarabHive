# Software Architecture Document: AgentSystem Core

**Document Type:** Software Architecture Document (SAD)  
**Component:** AgentSystem Core Architecture  
**Version:** 1.0  
**Last Updated:** 2026-09-28  
**Status:** Active

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architectural Goals](#2-architectural-goals)
3. [System Context](#3-system-context)
4. [Component Architecture](#4-component-architecture)
5. [Key Design Decisions](#5-key-design-decisions)
6. [Data Flow](#6-data-flow)
7. [Technology Stack](#7-technology-stack)
8. [Quality Attributes](#8-quality-attributes)
9. [Deployment View](#9-deployment-view)
10. [Related Documents](#10-related-documents)

---

## 1. Overview

### 1.1 Purpose

AgentSystem is a modular, extensible AI agent framework that enables:
- Multi-agent orchestration with specialized capabilities
- Plugin-based tool ecosystem (tool servers; the MCP protocol only in the mcp_client plugin)
- Configuration-driven agent definition
- Real-time status streaming and cancellation
- Multi-user session management with authentication

### 1.2 Scope

This document describes the core architecture of AgentSystem, including:
- Core components and their interactions
- Plugin and agent systems
- Configuration management
- Communication protocols (HTTP, SSE, MCP)

### 1.3 Audience

- System architects
- Backend developers
- Plugin developers
- DevOps engineers

---

## 2. Architectural Goals

### 2.1 Primary Goals

| Goal | Description | Priority |
|------|-------------|----------|
| **Modularity** | Loosely coupled components, plugin-based extensibility | High |
| **Scalability** | Support multiple concurrent users and sessions | High |
| **Configurability** | YAML-driven configuration without code changes | High |
| **Reliability** | Graceful error handling, cancellation support | High |
| **Developer Experience** | Clear APIs, good documentation, easy plugin authoring | Medium |
| **Performance** | Caching, parallel execution, efficient resource usage | Medium |

### 2.2 Non-Goals

- Distributed agent execution (single-process architecture)
- Built-in LLM training or fine-tuning
- GUI-based configuration beyond agents (the `agent_editor` panel edits agent YAML; the rest is YAML/API)
- Real-time collaboration between multiple users on same session

---

## 3. System Context

### 3.1 Context Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                         AgentSystem                              │
│                                                                  │
│  ┌────────────────┐         ┌────────────────┐                 │
│  │   Web UI       │◄───────►│   FastAPI App  │                 │
│  │   (Browser)    │   HTTP  │   (REST API)   │                 │
│  └────────────────┘  /SSE   └────────┬───────┘                 │
│                                       │                          │
│  ┌────────────────┐                  │                          │
│  │   CLI Client   │◄─────────────────┘                          │
│  │   (Terminal)   │         │                                   │
│  └────────────────┘         │                                   │
│                              ▼                                   │
│                     ┌────────────────┐                          │
│                     │  Agent Service │                          │
│                     │   (Core Logic) │                          │
│                     └────────┬───────┘                          │
│                              │                                   │
│         ┌────────────────────┼────────────────────┐            │
│         │                    │                    │            │
│         ▼                    ▼                    ▼            │
│  ┌─────────────┐   ┌─────────────────┐   ┌─────────────┐    │
│  │   Agents    │   │   Plugins       │   │   Config    │    │
│  │  (Executor) │   │   (Tools/Hooks) │   │   (YAML)    │    │
│  └─────────────┘   └─────────────────┘   └─────────────┘    │
│         │                    │                                │
│         └────────────────────┴────────────┐                  │
│                                            ▼                   │
│                                    ┌──────────────┐           │
│                                    │  LLM Clients │           │
│                                    │ (OpenAI/etc) │           │
│                                    └──────────────┘           │
└──────────────────────────────────────────────────────────────┘
         │                    │                    │
         ▼                    ▼                    ▼
┌──────────────┐   ┌───────────────┐   ┌──────────────────┐
│  File System │   │  External MCP │   │  LLM Providers   │
│  (Sessions,  │   │    Servers    │   │  (OpenAI, etc)   │
│   Config)    │   │               │   │                  │
└──────────────┘   └───────────────┘   └──────────────────┘
```

The diagram is conceptual: the CLI runs agents in its own process (it calls the
API only for `reload`), and the "Agent Service" box is the agents' own run loop
(`Agent.run_events()`) -- `services/agent_service.py` is an unused stub (4.2.2).

### 3.2 External Systems

| System | Protocol | Purpose |
|--------|----------|---------|
| **LLM Providers** | HTTP/HTTPS | AI model inference (OpenAI, Ollama, etc.) |
| **External MCP servers** | HTTP/SSE/stdio | External tool integration (Context7, Memory, etc.) |
| **File System** | Local I/O | Configuration, sessions, cache storage |
| **Web Browsers** | HTTP/SSE | Web UI access, real-time updates |
| **CLI Clients** | In-process (HTTP only for `agent-cli reload`) | Command-line interface |

---

## 4. Component Architecture

### 4.1 High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      Presentation Layer                          │
├─────────────────────────────────────────────────────────────────┤
│  FastAPI Routes  │  WebSocket/SSE  │  Static Files (UI)         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Application Layer                           │
├─────────────────────────────────────────────────────────────────┤
│  Agent Service   │  Session Manager  │  Auth Service            │
│  Status Bus      │  Cancellation Mgr │  Config Loader           │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                       Domain Layer                               │
├─────────────────────────────────────────────────────────────────┤
│  Agent (Executor)         │  Plugin Registry                     │
│  Tool integration          │  Hook System                         │
│  LLM Clients              │  Tool Execution Manager              │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Infrastructure Layer                          │
├─────────────────────────────────────────────────────────────────┤
│  File Storage    │  Cache System   │  HTTP Clients              │
│  JSON Serializer │  Logger         │  Event Bus                 │
└─────────────────────────────────────────────────────────────────┘
```

### 4.2 Core Components

#### 4.2.1 FastAPI Application (`app.py`)

**Responsibilities:**
- HTTP server lifecycle management
- Route registration and middleware
- Lifespan events (startup/shutdown)
- CORS, authentication, rate limiting

**Key Files:**
- `src/agent_system/app.py` - Application factory (`build_app`), most routes and the SSE endpoints
- `src/agent_system/api/` - Routers for auth, admin, sessions, debug, health/version

**Dependencies:**
- FastAPI framework
- Uvicorn ASGI server
- Pydantic models

#### 4.2.2 Agent Service (`services/agent_service.py`) -- unused stub

Not wired in: `app.py` keeps `_agent_service = None`, and
`services/__init__.py` lists it as "STUB - TODO". `/run` and `/events` call
`Agent.run_events()` on the selected agent directly; agent-cli runs agents
in-process. The interface below is what the stub declares.

**Responsibilities:**
- Agent execution orchestration
- Request lifecycle management
- Status event coordination
- Error handling and recovery

**Key Interfaces:**
```python
class AgentService:
    async def execute_task(
        task: str,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
        images: Optional[list[bytes]] = None
    ) -> AsyncIterator[dict[str, Any]]
```

#### 4.2.3 Agent (`servers/agent/server.py`)

**Responsibilities:**
- Multi-step reasoning loop
- Tool discovery and execution
- LLM interaction
- Context management

**Key Interfaces:**
```python
class Agent(ToolServer):
    async def run_events(
        task: str,
        request_id: str,
        session_id: str,
        ...
    ) -> AsyncGenerator[dict, None]
```

#### 4.2.4 Initialization Service (`services/initialization_service.py`)

**Responsibilities:**
- Centralized bootstrap for all entry points (API, CLI, lightweight runner)
- Lazily provision `SessionManager` and `SessionService`
- Invoke `bootstrap_servers()` once per process and inject dependencies into every agent instance
- Coordinate with tool integration to avoid duplicate initialization via `servers_bootstrapped` flag

**Key Capabilities:**
- Works with the `ToolServerRegistry` every entry point builds (the API included); plugin instances are also kept in the `PluginToolRegistry` singleton for the web/UI side
- Injects shared services (currently `session_service`, future dependencies via `agent_injection` helpers)
- Provides specialized helpers (`initialize_for_api`, `initialize_for_cli`, `bootstrap_and_inject`)
- Ensures consistent dependency graph for sub-agent management and hooks

#### 4.2.5 Plugin Registry (`plugins/discovery.py`, `plugins/tool_adapter.py`, `runtime.py`)

**Responsibilities:**
- Plugin discovery (filesystem + config)
- Instantiation: `Runtime` (`runtime.py`) builds every declared server from the discovered factories into the `ToolServerRegistry`; `PluginToolRegistry` (`plugins/tool_adapter.py`) keeps the built plugin instances for the web/UI side
- Metadata management (`plugin.toml`, `plugins/plugin_manifest.py`)

**Key Features:**
- Auto-discovery from the `plugins.plugin_dirs` in `config/plugins.yaml` (and Python entry points)
- Config-based agent registration
- Lazy initialization support

#### 4.2.6 Tool integration (`tools/integration.py`)

**Responsibilities:**
- External MCP server connections
- Tool list aggregation
- Tool call routing
- Connection health monitoring

**Key Components:**
- External MCP connections live in the `mcp_client` plugin
- `ToolCache` - Tool list caching

#### 4.2.7 Hook System (`hooks/`)

**Responsibilities:**
- Lifecycle event interception
- Plugin hook execution
- Order management and dependencies
- Error isolation

**Hook Types** (`HookType` in `hooks/plugin_hook.py`, 10 values):
- `pre_llm_call` - Before LLM request
- `post_llm_call` - After LLM response
- `pre_llm_request` / `post_llm_response` - LLM-client level (exact API payload/response)
- `llm_progress` - During a streaming LLM call (no messages attached)
- `format_output` - Output formatting
- `session_start/end` - Session lifecycle
- `pre_tool_call` / `post_tool_call` - around every tool call of the model
  (`components/tool_execution.py`) and of a tool_script script
  (`Agent.dispatch_tool_call(hook_source=...)`): pre may change the arguments or
  block the call, post may change the result (`docs/plugin_hooks.md`)

Each hook runs under its own timeout (`asyncio.wait_for` in `hooks/registry.py`); a
timed-out hook is logged and skipped. Global `hooks.overrides` accept an exact
`plugin.hook` key or a plugin-wide `plugin` key (the exact key wins) and set
`enabled` / `timeout` / `order`; `hooks.enabled: false` disables all hooks.

#### 4.2.8 Configuration System (`config/`)

**Responsibilities:**
- Multi-file YAML loading
- Environment variable substitution
- Schema validation
- Pydantic model binding

**Configuration System:**
- `config/config.yaml` - Main config with includes mechanism
- **`llm_system:`** - LLM profiles and model configurations
- **`plugins:`** - Plugin discovery, default configs, and server configurations
- **`external_servers:`** - External MCP server connections
- **`agents*/*.yaml`, `src/plugins*/*/agents/*.yaml`** - Config-based agent definitions (as `plugins.servers` entries; there is no top-level `agents:` section)

Files listed under `includes:` contribute `llm_system`, `plugins` and `hooks` (deep-merged) and `external_servers` (the last file that has it wins). Every other top-level section (`auth`, `network`, `logging`, ...) is read from `config.yaml` only; in an included file it is silently ignored.

---

## 5. Key Design Decisions

### 5.1 Decision Records

#### ADR-001: FastAPI over Flask

**Context:** Need async HTTP server with SSE support  
**Decision:** Use FastAPI with Uvicorn  
**Rationale:**
- Native async/await support
- Built-in OpenAPI documentation
- Excellent SSE streaming performance
- Type hints and validation via Pydantic

**Status:** Accepted

---

#### ADR-002: YAML-Based Configuration

**Context:** Need flexible, user-friendly configuration  
**Decision:** Multi-file YAML configuration with Pydantic models  
**Rationale:**
- Human-readable and editable
- Support for comments and documentation
- Schema validation via Pydantic
- Environment variable substitution
- Separation of concerns (llm.yaml, agents.yaml, etc.)

**Status:** Accepted

---

#### ADR-003: Plugin Architecture

**Context:** Need extensible tool and hook system  
**Decision:** Dual plugin types (Tools via ToolServer, Hooks via PluginHook)  
**Rationale:**
- Clear separation of concerns
- Minimal inheritance (composition over inheritance)
- Supports pure tool plugins, pure hook plugins, and hybrids
- Built-in tools run in-process via `ToolServer`; the MCP protocol only for external servers (`mcp_client` plugin)

**Status:** Accepted

---

#### ADR-004: Config-Based Agents

**Context:** Many agents differ only in prompts and tool access  
**Decision:** Support YAML-defined agents without Python code  
**Rationale:**
- Lower barrier to entry for non-developers
- Faster prototyping and iteration
- Reduced code duplication
- Coexists with plugin-based agents

**Status:** Accepted

---

#### ADR-005: Session-Based Architecture

**Context:** Support multiple users with isolated contexts  
**Decision:** Session-based storage with per-user directories  
**Rationale:**
- Data isolation between users
- Persistent conversation history
- Easy backup and migration
- Simpler than database for MVP

**Status:** Accepted

---

#### ADR-006: Status Streaming via SSE

**Context:** Need real-time progress updates in UI  
**Decision:** Server-Sent Events for status streaming  
**Rationale:**
- Simpler than WebSockets for one-way communication
- Better browser compatibility
- Automatic reconnection
- HTTP/2 multiplexing support

**Status:** Accepted

---

### 5.2 Technology Choices

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| **Web Framework** | FastAPI | Async, type hints, OpenAPI |
| **ASGI Server** | Uvicorn | Performance, HTTP/2, WebSockets |
| **LLM Client** | Provider plugins (`src/plugins/llm_*`): openai, anthropic, google-genai, openrouter SDKs + httpx | Multi-provider support |
| **Validation** | Pydantic | Type safety, validation, serialization |
| **Config Format** | YAML | Human-readable, comments, nesting |
| **Session Storage** | JSON files | Simple, inspectable, version-controllable |
| **Caching** | In-memory + file | Performance, persistence |
| **Logging** | Python logging | Standard library, configurable |

---

## 6. Data Flow

### 6.1 Agent Execution Flow

```
User Request (HTTP/CLI)
         │
         ▼
   FastAPI /run, /events  (or agent-cli, in-process)
         │
     ├─► Ensure InitializationService bootstrapped registry & injections
     │      │
     │      └─► Shared SessionService available for agents/hooks
     ├─► Status Bus (emit "starting")
         │
         ▼
   Agent.run_events()
         │
         ├─► Load Session History
         ├─► Hook: pre_llm_call
         ├─► LLM Request
         ├─► Hook: post_llm_call
         │
         ├─► Parse Tool Calls
         │      │
         │      ▼
         │   Tool Execution Manager
         │      │
         │      ├─► Execute Tool (Plugin/MCP)   (no tool hooks fire)
         │      ▼
         │   Tool Results
         │
         ├─► Repeat until complete
         │
         ▼
   Hook: format_output
         │
         ▼
   Save Session
         │
         ▼
   Return Result (Stream/JSON)
```

### 6.2 Configuration Loading

```
Startup
   │
   ▼
Load config/config.yaml
   │
   ├─► Resolve includes (llm.yaml, etc.)
   ├─► Substitute environment variables
   ├─► Validate with Pydantic schemas
   │
   ▼
Initialize Components
   │
  ├─► InitializationService (SessionManager + SessionService singletons)
  ├─► LLM Clients (from llm.yaml)
  ├─► Tool integration (from mcp_servers.yaml)
  ├─► Plugin Registry (discover + config agents)
  ├─► Hook System (load hooks from plugins)
   │
   ▼
Ready for Requests
```

### 6.3 Plugin Discovery

```
System Startup
   │
   ▼
Filesystem Discovery
   │
   ├─► Scan plugin_dirs/*/ for the entrypoint (plugin.toml `entrypoint`, default plugin.py:PLUGIN_FACTORY)
   ├─► Load plugin.toml metadata
   ├─► Register factories in registry
   │
   ▼
Config-Based Agent Discovery
   │
   ├─► Load agent YAMLs from the includes (config/agents*/*.yaml, src/plugins*/*/agents/*.yaml)
   ├─► Validate agent definitions
   ├─► Create factories dynamically
   ├─► Register alongside plugins
   │
   ▼
Bootstrap Agents
   │
   ├─► Instantiate from factories
   ├─► Apply configuration overrides
   ├─► Register in tool registry
   │
   ▼
Ready
```

---

## 7. Technology Stack

### 7.1 Core Stack

```yaml
runtime:
  language: Python 3.11+
  framework: FastAPI 0.115.6
  server: Uvicorn

dependencies:
  web:
    - fastapi
    - uvicorn[standard]
    - pydantic >= 2.11
    - python-multipart
  
  llm:  # declared by the provider plugins (src/plugins/llm_*/plugin.toml)
    - openai
    - anthropic
    - google-genai
    - openrouter
  
  data:
    - pyyaml
    - jinja2
    - jsonschema
  
  utilities:
    - httpx
    - python-jose[cryptography]
    - bcrypt
```

### 7.2 Development Stack

```yaml
development:
  testing:
    - pytest
    - pytest-asyncio
    - pytest-cov
  
  code_quality:
    - ruff
    - mypy
```

---

## 8. Quality Attributes

### 8.1 Performance

| Metric | Target | Current | Notes |
|--------|--------|---------|-------|
| **Agent Response Time** | < 30s | ~10-20s | Depends on LLM latency |
| **Tool Execution** | Parallel | Parallel | asyncio.create_task() + asyncio.wait() |
| **Concurrent Users** | 50+ | Tested: 20 | Limited by LLM rate limits |
| **Session Load Time** | < 100ms | ~50ms | JSON file I/O |
| **Tool Cache Hit Rate** | > 80% | ~85% | 30s TTL |

### 8.2 Reliability

| Aspect | Implementation | Status |
|--------|---------------|--------|
| **Error Handling** | Try/catch, graceful degradation | ✅ Implemented |
| **Cancellation** | Token-based, cooperative | ✅ Implemented |
| **Retries** | Exponential backoff for LLM/MCP | ✅ Implemented |
| **Validation** | Pydantic models, schema checks | ✅ Implemented |
| **Logging** | Structured logging, levels | ✅ Implemented |

### 8.3 Security

| Feature | Status | Notes |
|---------|--------|-------|
| **Authentication** | ✅ JWT + API Keys | Optional, configurable |
| **Authorization** | ✅ User-based sessions | Per-user isolation |
| **Input Validation** | ✅ Pydantic models | All API inputs validated |
| **Rate Limiting** | ✅ Sliding 1-minute window | Per client IP (`auth.requests_per_minute`); only with `auth.enabled` and `auth.rate_limit_enabled` (off in the shipped config) |
| **CORS** | ✅ Configurable | `auth.cors_origins` (default and shipped value `*`); applied only with `auth.enabled` and `auth.cors_enabled` (on by default) |
| **Secrets Management** | ✅ Env vars / `config/secrets.env` | Provider keys referenced as `${VAR}` in the YAML; `auth.secret_key` and `auth.default_admin_password` are literals in the shipped config and must be changed |

### 8.4 Maintainability

| Aspect | Score | Notes |
|--------|-------|-------|
| **Code Coverage** | 75% | Target: 80% |
| **Documentation** | Good | SAD, API docs, user guides |
| **Modularity** | Excellent | Clear component boundaries |
| **Type Safety** | Good | Pydantic + type hints |
| **Code Quality** | Good | Ruff, Mypy checks |

---

## 9. Deployment View

### 9.1 Single-Server Deployment

```
┌─────────────────────────────────────┐
│         Server (Linux/Windows)      │
│                                     │
│  ┌──────────────────────────────┐  │
│  │   AgentSystem Process        │  │
│  │   (Python + Uvicorn)         │  │
│  │                              │  │
│  │   Port: 8000 (HTTP)          │  │
│  └──────────────────────────────┘  │
│              │                      │
│              ▼                      │
│  ┌──────────────────────────────┐  │
│  │   File System                │  │
│  │   - config/                  │  │
│  │   - data/sessions/           │  │
│  │   - data/cache/              │  │
│  │   - logs/                    │  │
│  └──────────────────────────────┘  │
└─────────────────────────────────────┘
          │
          ▼
    Internet (LLM APIs, Tool servers)
```

### 9.2 Reverse Proxy Deployment

```
Internet
    │
    ▼
┌─────────────────┐
│  Nginx/Caddy    │  HTTPS Termination
│  (Port 443)     │  Static Files
│  (Port 80)      │  Rate Limiting
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  AgentSystem    │  HTTP
│  (Port 8000)    │  Internal Only
└─────────────────┘
```

### 9.3 Environment Variables

```bash
# Provider keys - only those the configured models use (also settable in config/secrets.env)
OPENROUTER_API_KEY=sk-or-...   # the shipped default agent runs on OpenRouter
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...

# Optional
AGENT_LOG_LEVEL=info
AGENT_SESSION_STORAGE_PATH=/var/data/agent_system/sessions
AGENT_CONFIG_PATH=/etc/agent_system/config.yaml   # agent-api, agent-run, agent-cli without --config; secrets.env is read next to it
                                                  # outside <checkout>/config/: make plugin_dirs, skill_dirs and includes absolute;
                                                  # agent-api resolves other relative paths (a system_template not starting with ./, auth.database_path, logs) against its cwd
HOST=0.0.0.0                                      # overrides network.host
PORT=8000                                         # overrides network.port

# Development
AGENT_ENABLE_PROFILING=1   # debug/profiling endpoints
```

---

## 10. Related Documents

### 10.1 Architecture Documents

- [Plugin Architecture](_arch_plugin_architecture.md) - Plugin system design
- [External MCP Servers](_arch_external_mcpservers.md) - External MCP servers
- [Hook System](plugin_hooks.md) - Lifecycle hooks
- [Tool Execution](tool_execution.md) - Tool execution flow

### 10.2 Design Documents

- [Configurable Agents](configurable_agents.md) - YAML-based agents
- [Session Management](session_management.md) - Multi-user sessions
- [Status System](status_design.md) - Real-time updates
- [Caching Systems](caching_systems.md) - Performance optimization

### 10.3 User Guides

- [Plugin Authoring](plugin_authoring.md) - How to create plugins
- [Configuration Guide](../INSTALLATION.md#configuration) - Configuration reference
- API reference: the OpenAPI UI of a running server at `/docs` (`/openapi.json`); design notes in [_arch_app_architecture.md](_arch_app_architecture.md), partly outdated
- [CLI Reference](cli_reference.md) - Command-line usage

---

**Document Changelog:**

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2025-01-15 | AgentSystem Team | Initial comprehensive SAD |

---

**Approval:**

| Role | Name | Date | Signature |
|------|------|------|-----------|
| Architect | - | - | - |
| Tech Lead | - | - | - |
| Product Owner | - | - | - |
