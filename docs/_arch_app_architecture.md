# Software Architecture Document: FastAPI Application Layer

**Document Type:** Software Architecture Document (SAD)  
**Component:** FastAPI Application & API Layer  
**Version:** 1.0  
**Last Updated:** 2025-01-15  
**Status:** Active

---

## Table of Contents

1. [Overview](#overview)
2. [Architectural Goals](#architectural-goals)
3. [Component Architecture](#component-architecture)
4. [API Design](#api-design)
5. [Service Layer](#service-layer)
6. [Authentication & Authorization](#authentication--authorization)
7. [Real-Time Communication](#real-time-communication)
8. [Key Design Decisions](#key-design-decisions)
9. [Data Flow](#data-flow)
10. [Error Handling](#error-handling)
11. [Related Documents](#related-documents)

---

## 1. Overview

### 1.1 Purpose

The FastAPI Application Layer provides:
- RESTful API endpoints for agent interaction
- Server-Sent Events (SSE) for real-time streaming
- Authentication and authorization
- Session management
- Service coordination layer
- Static file serving and web UI

### 1.2 Scope

This document covers:
- FastAPI application structure (`app.py`)
- API endpoint design (`api/endpoints.py`)
- Service layer architecture (`services/`)
- Authentication system (`api/auth.py`)
- Streaming endpoints (`api/streaming.py`)

### 1.3 Key Features

| Feature | Description |
|---------|-------------|
| **RESTful API** | JSON-based REST API for all operations |
| **SSE Streaming** | Real-time status and result streaming |
| **Multi-Format Output** | JSON, HTML, Markdown output formats |
| **Authentication** | JWT-based auth with API key support |
| **Session Management** | Per-user isolated sessions |
| **Cancellation** | Request cancellation via tokens |
| **Plugin UI** | Dynamic plugin UI integration |

---

## 2. Architectural Goals

### 2.1 Design Principles

| Principle | Description | Priority |
|-----------|-------------|----------|
| **Separation of Concerns** | Clear layers: API → Service → Domain | High |
| **Async-First** | All I/O operations async (LLM, MCP, file) | High |
| **Stateless API** | No session state in HTTP layer | High |
| **Type Safety** | Pydantic models for all API I/O | High |
| **Testability** | Dependency injection, mocking support | Medium |
| **Performance** | Streaming responses, parallel execution | Medium |

### 2.2 Quality Goals

- **Latency:** < 100ms for non-streaming endpoints
- **Throughput:** 50+ concurrent users
- **Availability:** 99.9% uptime (excluding LLM downtime)
- **Security:** JWT tokens, input validation, CORS protection

---

## 3. Component Architecture

### 3.1 Application Structure

```
┌─────────────────────────────────────────────────────────────────┐
│                      FastAPI Application                         │
│                         (app.py)                                 │
└─────────────────────────────────────────────────────────────────┘
         │
         ├─► Lifespan (startup/shutdown)
         ├─► Middleware (CORS, rate limiting, auth)
         ├─► Route Registration
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                       API Layer                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │  endpoints.py│  │ streaming.py │  │   auth.py    │          │
│  │  (REST API)  │  │     (SSE)    │  │   (JWT)      │          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
└─────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Service Layer                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │AgentService  │  │SessionManager│  │ToolService   │          │
│  ├──────────────┤  ├──────────────┤  ├──────────────┤          │
│  │ConfigService │  │ MCPService   │  │SessionService│          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
└─────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Domain Layer                                │
│  Agent │ MCPRegistry │ Plugin System │ LLM Clients              │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 Core Components

#### 3.2.1 FastAPI Application (`app.py`)

**File:** `src/agent_system/app.py`

**Responsibilities:**
- Application factory pattern (`build_app()`)
- Dependency injection (services, config)
- Route registration
- Middleware configuration
- Lifecycle management (startup/shutdown)

**Key Code:**
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    # Startup
    logger.info("Application starting up")
    yield
    # Shutdown
    logger.info("Application shutting down")

def build_app(config_path: Optional[str] = None) -> FastAPI:
    """Factory function to build FastAPI app"""
    # Load config
    config_service = ConfigService()
    config = config_service.load_config(config_path)
    
    # Initialize services
    mcp_service = MCPService(config)
    agent_service = AgentService(mcp_service)
    session_manager = SessionManager(config)
    
    # Create FastAPI app
    app = FastAPI(lifespan=lifespan)
    
    # Register routes
    app.include_router(api_router)
    
    # Store services in app.state
    app.state.agent_service = agent_service
    app.state.session_manager = session_manager
    
    return app
```

**Global State:**
- `_config_service` - Configuration management
- `_mcp_service` - MCP integration
- `_agent_service` - Agent orchestration
- `_session_manager` - Session lifecycle
- `_app_registry` - MCP server registry

#### 3.2.2 Initialization Service (`services/initialization_service.py`)

**Responsibilities:**
- Provide centralized bootstrap for FastAPI entry point
- Lazily create and cache `SessionManager`/`SessionService`
- Invoke `bootstrap_servers()` once and inject shared dependencies into every agent via `agent_injection`
- Coordinate with `MCPIntegration` through the `servers_bootstrapped` flag so CLI and API do not double-bootstrap

**How the API Uses It:**
- `build_app()` instantiates `InitializationService` immediately after loading config
- Startup hook (`_init_mcp_for_app`) delegates to `initialize_for_api(skip_bootstrap=True)` because `initialize_mcp()` already handled registry bootstrap
- `app.state.session_manager` is populated from the service for dependency injection into routes
- Ensures sub-agent manager, hooks, and web endpoints all observe the same `SessionService`

#### 3.2.3 API Endpoints (`api/endpoints.py`)

**File:** `src/agent_system/api/endpoints.py`

**Responsibilities:**
- RESTful API routes
- Request validation (Pydantic)
- Response formatting
- Error handling
- Authentication enforcement

**Endpoint Categories:**

| Category | Endpoints | Purpose |
|----------|-----------|---------|
| **Agent** | `/chat`, `/chat-stream` | Execute agent tasks |
| **Session** | `/sessions`, `/sessions/{id}` | Manage sessions |
| **Config** | `/config-agents`, `/config-agents/{name}` | Config-based agents |
| **Tools** | `/tools`, `/tools/execute` | Tool discovery & execution |
| **Status** | `/status`, `/status/metrics` | System status |
| **Auth** | `/login`, `/logout`, `/me` | Authentication |
| **Admin** | `/admin/users`, `/admin/sessions` | Admin operations |

**Example Endpoint:**
```python
@router.post("/chat")
async def chat_endpoint(
    request: ChatRequest,
    session_id: str = Header(...),
    user: User = Depends(get_current_user)
) -> ChatResponse:
    """Execute an agent task and return result"""
    # Validate session ownership
    session_manager.validate_session(session_id, user.username)
    
    # Execute agent
    result = await agent_service.run_agent(
        agent_name=request.agent,
        task=request.message,
        session_id=session_id
    )
    
    return ChatResponse(
        result=result,
        session_id=session_id
    )
```

#### 3.2.4 Streaming API (`api/streaming.py`)

**File:** `src/agent_system/api/streaming.py`

**Responsibilities:**
- Server-Sent Events (SSE) endpoints
- Real-time status streaming
- Agent result streaming
- Connection lifecycle

**Key Endpoints:**

```python
@router.get("/chat-stream")
async def chat_stream_endpoint(
    agent: str,
    message: str,
    session_id: str,
    user: User = Depends(get_current_user)
) -> StreamingResponse:
    """Stream agent execution in real-time"""
    
    async def event_generator():
        async for event in agent_service.run_agent_stream(
            agent_name=agent,
            task=message,
            session_id=session_id
        ):
            # Format as SSE
            data = json.dumps(event)
            yield f"data: {data}\n\n"
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream"
    )
```

**SSE Event Format:**
```json
{
  "type": "status",
  "request_id": "req_abc123",
  "status": "running",
  "step": 2,
  "max_steps": 10,
  "message": "Executing tool: web_search"
}
```

#### 3.2.5 Authentication (`api/auth.py`)

**File:** `src/agent_system/api/auth.py`

**Responsibilities:**
- JWT token generation and validation
- API key authentication
- User management
- Password hashing (bcrypt)

**Authentication Flow:**
```
User Login (POST /login)
    │
    ├─► Validate credentials (username/password)
    ├─► Generate JWT token
    ├─► Return token + user info
    │
User Request (with Authorization: Bearer <token>)
    │
    ├─► Extract JWT from header
    ├─► Validate signature & expiry
    ├─► Extract user info
    ├─► Inject into request context (Depends)
    │
Protected Endpoint
    │
    ├─► Access user from dependency
    ├─► Perform operation
    ├─► Return result
```

**Key Functions:**
```python
def create_access_token(data: dict) -> str:
    """Generate JWT token"""
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=30)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(
    token: str = Depends(oauth2_scheme)
) -> User:
    """Dependency for protected endpoints"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if username is None:
            raise HTTPException(401, "Invalid token")
        return User(username=username)
    except JWTError:
        raise HTTPException(401, "Invalid token")
```

---

## 4. API Design

### 4.1 Request/Response Models

**Pydantic Models:**

```python
# Chat Request
class ChatRequest(BaseModel):
    agent: str  # Agent name
    message: str  # User task/question
    output_format: str = "json"  # json|html|markdown
    cancellation_token: Optional[str] = None

# Chat Response
class ChatResponse(BaseModel):
    result: str  # Agent output
    session_id: str
    usage_stats: Dict[str, Any]
    request_id: str

# Session Info
class SessionInfo(BaseModel):
    session_id: str
    user: str
    created_at: datetime
    last_accessed: datetime
    message_count: int

# Config Agent Info
class ConfigAgentInfo(BaseModel):
    name: str
    enabled: bool
    description: Optional[str]
    llm_profile: str
    max_steps: int
    tools: Dict[str, List[str]]
    metadata: Dict[str, Any]
```

### 4.2 REST API Reference

#### 4.2.1 Agent Endpoints

```http
POST /chat
Content-Type: application/json
Authorization: Bearer <token>
X-Session-ID: <session_id>

{
  "agent": "default",
  "message": "What is the weather?",
  "output_format": "json"
}

Response 200:
{
  "result": "The current weather is...",
  "session_id": "sess_xyz",
  "request_id": "req_abc",
  "usage_stats": { "total_tokens": 1234 }
}
```

```http
GET /chat-stream
  ?agent=default
  &message=hello
  &session_id=sess_xyz
Accept: text/event-stream
Authorization: Bearer <token>

Response 200 (streaming):
data: {"type": "status", "status": "starting"}

data: {"type": "status", "status": "running", "step": 1}

data: {"type": "result", "content": "Hello!"}

data: {"type": "complete"}
```

#### 4.2.2 Session Endpoints

```http
GET /sessions
Authorization: Bearer <token>

Response 200:
{
  "sessions": [
    {
      "session_id": "sess_abc",
      "user": "alice",
      "created_at": "2025-01-15T10:00:00Z",
      "message_count": 5
    }
  ]
}
```

```http
GET /sessions/{session_id}/messages
Authorization: Bearer <token>

Response 200:
{
  "messages": [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hi!"}
  ],
  "message_count": 2
}
```

#### 4.2.3 Config Agent Endpoints

```http
GET /config-agents
Authorization: Bearer <token>

Response 200:
{
  "total": 5,
  "enabled": 3,
  "disabled": 2,
  "agents": [
    {
      "name": "researcher",
      "enabled": true,
      "description": "Research assistant",
      "llm_profile": "gpt4",
      "tools": {"include": ["web_search", "calculator"]}
    }
  ]
}
```

```http
GET /config-agents/{agent_name}
Authorization: Bearer <token>

Response 200:
{
  "name": "researcher",
  "enabled": true,
  "description": "Research assistant",
  "llm_profile": "gpt4",
  "max_steps": 10,
  "system_template": "prompts/researcher.md",
  "tools": {"include": ["web_search"]},
  "hooks": {"format_output": ["markdown_formatter"]},
  "metadata": {"visibility": "ui"}
}
```

### 4.3 Error Responses

**Standard Error Format:**
```json
{
  "detail": "Error message",
  "error_code": "AGENT_NOT_FOUND",
  "request_id": "req_abc123",
  "timestamp": "2025-01-15T10:00:00Z"
}
```

**HTTP Status Codes:**

| Code | Meaning | Example |
|------|---------|---------|
| 200 | Success | Normal response |
| 400 | Bad Request | Invalid parameters |
| 401 | Unauthorized | Missing/invalid token |
| 403 | Forbidden | Access denied |
| 404 | Not Found | Agent/session not found |
| 422 | Validation Error | Pydantic validation failed |
| 429 | Rate Limit | Too many requests |
| 500 | Server Error | Internal error |
| 503 | Service Unavailable | LLM/MCP unavailable |

---

## 5. Service Layer

### 5.1 Service Architecture

Services encapsulate business logic and coordinate domain components.

#### 5.1.1 InitializationService

**File:** `src/agent_system/services/initialization_service.py`

**Responsibilities:**
- Single source of truth for bootstrap across API, CLI, and `agent_run`
- Lazily instantiate `SessionManager` and `SessionService`
- Bridge between `initialize_mcp()` and dependency injection utility functions
- Track initialization state (`servers_bootstrapped`, `initialized`) to avoid redundant work during hot reloads

**Key Methods:**
```python
class InitializationService:
    def bootstrap_and_inject(
        self,
        registry: Optional[MCPRegistry] = None,
        inject_sessions: bool = True
    ) -> MCPRegistry:
        """Create registry, bootstrap plugins, inject session service."""

    def initialize_for_api(
        self,
        plugin_registry=None,
        skip_bootstrap: bool = False
    ) -> SessionService:
        """Inject dependencies into the global plugin registry used by FastAPI."""

    def initialize_for_cli(self) -> tuple[MCPRegistry, SessionService]:
        """Convenience helper for CLI tools (used by `agent_cli` and `agent_run`)."""
```

#### 5.1.2 ConfigService

**File:** `src/agent_system/services/config_service.py`

**Responsibilities:**
- Load and parse YAML configuration
- Environment variable substitution
- Schema validation
- Pydantic model binding

**Key Methods:**
```python
class ConfigService:
    def load_config(self, config_path: str) -> AgentConfig:
        """Load and validate configuration"""
    
    def get_llm_profile(self, name: str) -> LLMProfile:
        """Get LLM profile by name"""
    
    def get_agent_config(self, name: str) -> ConfigAgentDefinition:
        """Get config-based agent definition"""
```

#### 5.1.3 AgentService

**File:** `src/agent_system/services/agent_service.py`

**Responsibilities:**
- Agent execution orchestration
- Request lifecycle management
- Status event coordination
- Error handling and recovery

**Key Methods:**
```python
class AgentService:
    async def run_agent_stream(
        self,
        agent_name: str,
        task: str,
        session_id: str,
        request_id: str,
        cancellation_token: Optional[str] = None
    ) -> AsyncGenerator[dict, None]:
        """Stream agent execution events"""
```

#### 5.1.4 SessionManager

**File:** `src/agent_system/services/session_manager.py`

**Responsibilities:**
- Session CRUD operations
- Session file I/O (JSON)
- User isolation (per-user directories)
- Session cleanup

**Key Methods:**
```python
class SessionManager:
    def create_session(self, user: str) -> str:
        """Create new session for user"""
    
    def get_session(self, session_id: str, user: str) -> Session:
        """Load session (validates ownership)"""
    
    def save_session(self, session: Session):
        """Persist session to disk"""
    
    def list_sessions(self, user: str) -> List[SessionInfo]:
        """List all sessions for user"""
```

#### 5.1.5 MCPService

**File:** `src/agent_system/services/mcp_service.py`

**Responsibilities:**
- MCP client/server lifecycle
- External MCP server connections
- Tool list aggregation
- Health monitoring

**Key Methods:**
```python
class MCPService:
    async def initialize(self):
        """Connect to external MCP servers"""
    
    async def shutdown(self):
        """Disconnect from MCP servers"""
    
    def get_all_tools(self) -> List[ToolInfo]:
        """Get aggregated tool list"""
```

#### 5.1.6 ToolService

**File:** `src/agent_system/services/tool_service.py`

**Responsibilities:**
- Tool discovery and filtering
- Tool execution
- Hook invocation
- Parallel execution

**Key Methods:**
```python
class ToolService:
    def discover_tools(
        self,
        agent_name: str,
        include: List[str],
        exclude: List[str]
    ) -> List[ToolInfo]:
        """Discover and filter tools"""
    
    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict,
        request_id: str
    ) -> dict:
        """Execute single tool"""
```

### 5.2 Dependency Injection

Services are injected via FastAPI's dependency system:

```python
# app.py - Store in app.state
app.state.agent_service = AgentService(...)
app.state.session_manager = SessionManager(...)

# endpoints.py - Access via dependency
def get_agent_service(request: Request) -> AgentService:
    return request.app.state.agent_service

@router.post("/chat")
async def chat(
    agent_service: AgentService = Depends(get_agent_service)
):
    result = await agent_service.run_agent(...)
```

---

## 6. Authentication & Authorization

### 6.1 Authentication Modes

| Mode | Mechanism | Use Case |
|------|-----------|----------|
| **JWT** | Bearer token in header | Web UI, CLI |
| **API Key** | X-API-Key header | Service-to-service |
| **None** | No auth (configurable) | Development, internal networks |

### 6.2 JWT Configuration

```yaml
# config/config.yaml
authentication:
  enabled: true
  jwt:
    secret_key: ${JWT_SECRET}  # From env var
    algorithm: HS256
    access_token_expire_minutes: 30
  api_keys:
    - name: service_account
      key: ${API_KEY_SERVICE}
```

### 6.3 Authorization

**Session Ownership:**
- Sessions are per-user (stored in `data/sessions/{username}/`)
- API validates session ownership on every request
- Users cannot access other users' sessions

**Admin Endpoints:**
```python
async def require_admin(
    user: User = Depends(get_current_user)
) -> User:
    """Dependency for admin-only endpoints"""
    if not user.is_admin:
        raise HTTPException(403, "Admin access required")
    return user

@router.get("/admin/sessions")
async def list_all_sessions(
    admin: User = Depends(require_admin)
):
    """Admin-only: list all sessions"""
```

---

## 7. Real-Time Communication

### 7.1 Server-Sent Events (SSE)

**Why SSE?**
- Simpler than WebSockets for one-way communication
- Automatic reconnection
- HTTP/2 multiplexing
- Better browser compatibility

**SSE Format:**
```
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive

data: {"type": "status", "status": "starting"}

data: {"type": "llm_response", "content": "Hello"}

data: {"type": "complete"}
```

### 7.2 Status Bus

**Architecture:**
```python
from agent_system.mcp.status import status_bus, publish_status, StatusPhase, StatusScope

# Method 1: Using publish_status helper (recommended)
await publish_status(
    server="my_tool",
    message="Processing...",
    request_id="req_abc",
    phase=StatusPhase.PROGRESS,
    level="info"
)

# Method 2: Using StatusScope for automatic START/END (best practice)
async with StatusScope(status_bus, "my_tool", request_id="req_abc",
                       start_msg="Starting...", end_msg="Done"):
    # Work happens here - automatic START/END messages
    pass

# Method 3: Direct publish (low-level, rarely needed)
from agent_system.mcp.status import StatusEvent
await status_bus.publish(StatusEvent(
    server="my_tool",
    request_id="req_abc",
    message="Processing...",
    phase=StatusPhase.PROGRESS
))

# Subscribe to events
queue = await status_bus.subscribe(request_id="req_abc")
async for event in queue:
    yield event
```

**Event Types:**
- `status` - Agent status updates
- `llm_request` - LLM API calls
- `llm_response` - LLM responses
- `tool_execution` - Tool calls
- `error` - Errors
- `complete` - Agent finished

### 7.3 Cancellation

**Token-Based Cancellation:**
```python
# Client sends cancellation token
POST /chat
{
  "message": "Long task",
  "cancellation_token": "cancel_xyz"
}

# Client cancels request
POST /cancel/{cancellation_token}

# Agent checks cancellation periodically
if cancellation_manager.is_cancelled(cancellation_token):
    raise CancellationError()
```

---

## 8. Key Design Decisions

### ADR-001: FastAPI over Flask

**Context:** Need async HTTP framework with SSE support  
**Decision:** Use FastAPI  
**Rationale:**
- Native async/await
- Built-in OpenAPI docs
- Pydantic validation
- Excellent performance
- Type safety

**Status:** Accepted

---

### ADR-002: Service Layer Pattern

**Context:** Decouple API from domain logic  
**Decision:** Introduce service layer between API and domain  
**Rationale:**
- Clear separation of concerns
- Easier testing (mock services)
- Business logic not in routes
- Reusable across API/CLI

**Status:** Accepted

---

### ADR-003: SSE for Streaming

**Context:** Need real-time updates in web UI  
**Decision:** Use Server-Sent Events (SSE)  
**Rationale:**
- Simpler than WebSockets
- One-way communication sufficient
- Auto-reconnect
- HTTP/2 support

**Status:** Accepted

---

### ADR-004: JWT Authentication

**Context:** Need stateless authentication  
**Decision:** JWT tokens in Authorization header  
**Rationale:**
- Stateless (no session storage)
- Standard protocol
- Works with API and web UI
- Easy to validate

**Status:** Accepted

---

## 9. Data Flow

### 9.1 Synchronous Request Flow

```
Client (POST /chat)
    │
    ▼
FastAPI Router (endpoints.py)
    │
    ├─► Validate JWT token (get_current_user)
    ├─► Validate request body (Pydantic)
    │
    ▼
InitializationService (bootstrapped during startup)
    │
    ├─► Offers shared SessionService & injected registry state
    │
    ▼
AgentService.run_agent()
    │
    ├─► Load session
    ├─► Execute agent
    ├─► Save session
    │
    ▼
Agent.run_events()
    │
    ├─► LLM request
    ├─► Tool execution
    ├─► Multi-step loop
    │
    ▼
AgentService
    │
    ├─► Format output
    ├─► Build response
    │
    ▼
FastAPI Router
    │
    ├─► JSON serialization
    │
    ▼
Client (JSON response)
```

### 9.2 Streaming Request Flow

```
Client (GET /chat-stream)
    │
    ▼
FastAPI Router (streaming.py)
    │
    ├─► Validate JWT
    ├─► Create SSE connection
    │
    ▼
AgentService.run_agent_stream()
    │
    ├─► Async generator
    ├─► Subscribe to status_bus
    │
    ▼
Agent.run_events()
    │
    ├─► Emit status events
    ├─► Emit LLM events
    ├─► Emit tool events
    │
    ▼
Status Bus
    │
    ├─► Broadcast to subscribers
    │
    ▼
AgentService
    │
    ├─► Yield events
    │
    ▼
FastAPI Router
    │
    ├─► Format as SSE
    ├─► Stream to client
    │
    ▼
Client (SSE events)
```

---

## 10. Error Handling

### 10.1 Error Categories

| Category | HTTP Code | Handling |
|----------|-----------|----------|
| **Validation** | 422 | Pydantic validation errors |
| **Authentication** | 401 | Invalid/missing token |
| **Authorization** | 403 | Insufficient permissions |
| **Not Found** | 404 | Agent/session not found |
| **Rate Limit** | 429 | Too many requests |
| **LLM Error** | 503 | LLM API unavailable |
| **Internal** | 500 | Unexpected errors |

### 10.2 Error Handling Strategy

```python
# Global exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error",
            "error_code": "INTERNAL_ERROR",
            "request_id": request.state.request_id
        }
    )

# Specific exception handlers
@app.exception_handler(AgentNotFoundError)
async def agent_not_found_handler(request, exc):
    return JSONResponse(
        status_code=404,
        content={
            "detail": f"Agent '{exc.agent_name}' not found",
            "error_code": "AGENT_NOT_FOUND"
        }
    )
```

### 10.3 Graceful Degradation

- LLM unavailable → Return cached response or error
- MCP server down → Continue without external tools
- Session load error → Create new session
- Tool execution error → Continue agent loop, report error

---

## 11. Related Documents

### 11.1 Architecture Documents

- [System Architecture](agent_system_architecture.md) - Overall system design
- [Plugin Architecture](plugin_architecture.md) - Plugin system
- [CLI Architecture](cli_architecture.md) - Command-line interface

### 11.2 Design Documents

- [Authentication](multi_user_authentication.md) - Auth system
- [Session Management](session_management.md) - Session handling
- [Status System](status_design.md) - Real-time status
- [Tool Execution](tool_execution.md) - Tool system

### 11.3 API Reference

- [API Documentation](../README.md#api) - Endpoint reference
- [OpenAPI Spec](http://localhost:8000/docs) - Interactive API docs

---

**Document Changelog:**

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2025-01-15 | AgentSystem Team | Initial FastAPI app architecture SAD |

---

**Approval:**

| Role | Name | Date | Signature |
|------|------|------|-----------|
| Architect | - | - | - |
| Tech Lead | - | - | - |
| Product Owner | - | - | - |
