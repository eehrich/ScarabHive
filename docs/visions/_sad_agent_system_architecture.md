# Software Architecture Document: AgentSystem Core v1.1# Software Architecture Document: AgentSystem Core v1.1# Software Architecture Document: AgentSystem Core



**Document Type:** Software Architecture Document (SAD)**Document Type:** Software Architecture Document (SAD)  **Document Type:** Software Architecture Document (SAD)  

**Component:** AgentSystem Core Architecture

**Version:** 1.1 (Target State after Bug Fixes)**Component:** AgentSystem Core Architecture  **Component:** AgentSystem Core Architecture  

**Last Updated:** 2025-10-15

**Status:** ✅ Target State Definition**Version:** 1.1 (Target State after Bug Fixes)  **Version:** 1.0  



---**Last Updated:** 2025-10-15  **Last Updated:** 2025-01-15  



## 📘 Document Purpose**Status:** ✅ Target State Definition**Status:** Active



This document describes the **target architecture** of AgentSystem after v1.1 improvements have been implemented. It defines:------



- ✅ **Final class structures** with proper error handling## 📘 Document Purpose## Table of Contents

- ✅ **Complete interfaces** with type hints

- ✅ **Resource management patterns** (no leaks)This document describes the **target architecture** of AgentSystem after v1.1 improvements have been implemented. It defines:1. [Overview](#overview)

- ✅ **Exception hierarchy** and error recovery

- ✅ **Production-ready components** with logging and monitoring2. [Architectural Goals](#architectural-goals)



**This is the Soll-Zustand (target state), not a problem description.**- ✅ **Final class structures** with proper error handling3. [System Context](#system-context)



---- ✅ **Complete interfaces** with type hints4. [Component Architecture](#component-architecture)



## Table of Contents- ✅ **Resource management patterns** (no leaks)5. [Key Design Decisions](#key-design-decisions)



1. [System Overview](#1-system-overview)- ✅ **Exception hierarchy** and error recovery6. [Data Flow](#data-flow)

2. [Exception System](#2-exception-system)

3. [Service Layer](#3-service-layer)- ✅ **Production-ready components** with logging and monitoring7. [Technology Stack](#technology-stack)

4. [Agent Architecture](#4-agent-architecture)

5. [Resource Management](#5-resource-management)8. [Quality Attributes](#quality-attributes)

6. [API Contracts](#6-api-contracts)

7. [Data Flow](#7-data-flow)**This is the Soll-Zustand (target state), not a problem description.**9. [Deployment View](#deployment-view)

8. [Quality Attributes](#8-quality-attributes)

9. [Technology Stack](#9-technology-stack)10. [Related Documents](#related-documents)

10. [Related Documents](#10-related-documents)

---

---

---

## 1. System Overview

## Table of Contents

### 1.1 Architecture Principles (v1.1)

## 1. Overview

```

┌──────────────────────────────────────────────────────────────┐1. [System Overview](#1-system-overview)

│                    AgentSystem v1.1                           │

│                                                                │2. [Exception System](#2-exception-system)### 1.1 Purpose

│  Core Principles:                                              │

│  ✅ Type Safety First     - Full type hints, Mypy strict      │3. [Service Layer](#3-service-layer)

│  ✅ Error Handling        - Custom exceptions, recovery       │

│  ✅ Resource Safety       - Context managers, proper cleanup  │4. [Agent Architecture](#4-agent-architecture)AgentSystem is a modular, extensible AI agent framework that enables:

│  ✅ Async Everything      - All I/O is async                  │

│  ✅ Observable           - Structured logging, tracing        │5. [Resource Management](#5-resource-management)- Multi-agent orchestration with specialized capabilities

│  ✅ Production Ready     - No silent failures, monitoring     │

└──────────────────────────────────────────────────────────────┘6. [API Contracts](#6-api-contracts)- Plugin-based tool ecosystem (MCP protocol)

```

7. [Data Flow](#7-data-flow)- Configuration-driven agent definition

### 1.2 System Context Diagram

8. [Quality Attributes](#8-quality-attributes)- Real-time status streaming and cancellation

```

┌─────────────────────────────────────────────────────────────────────┐9. [Technology Stack](#9-technology-stack)- Multi-user session management with authentication

│                          AgentSystem v1.1                            │

│                                                                       │10. [Related Documents](#10-related-documents)

│  ┌───────────┐         ┌──────────────┐         ┌────────────┐     │

│  │  Web UI   │◄───────►│  FastAPI App │◄───────►│    CLI     │     │### 1.2 Scope

│  │ (Browser) │  HTTP   │  (app.py)    │  Local  │ (terminal) │     │

│  └───────────┘  /SSE   └──────┬───────┘         └────────────┘     │---

│                                │                                     │

│                                ▼                                     │This document describes the core architecture of AgentSystem, including:

│                   ┌────────────────────────┐                        │

│                   │   Exception Handler    │ ← Centralized          │## 1. System Overview- Core components and their interactions

│                   │  (Global Error Mgmt)   │                        │

│                   └────────────┬───────────┘                        │- Plugin and agent systems

│                                │                                     │

│                                ▼                                     │### 1.1 Architecture Principles (v1.1)- Configuration management

│                   ┌────────────────────────┐                        │

│                   │    Service Layer       │                        │- Communication protocols (HTTP, SSE, MCP)

│                   │  ┌─────────────────┐   │                        │

│                   │  │ AgentService    │   │ ← Retry logic          │```

│                   │  │ SessionService  │   │ ← File locking         │

│                   │  │ MCPService      │   │ ← Connection pool      │┌──────────────────────────────────────────────────────────────┐### 1.3 Audience

│                   │  └─────────────────┘   │                        │

│                   └────────────┬───────────┘                        ││                    AgentSystem v1.1                           │

│                                │                                     │

│         ┌──────────────────────┼──────────────────────┐            ││                                                                │- System architects

│         │                      │                      │            │

│         ▼                      ▼                      ▼            ││  Core Principles:                                              │- Backend developers

│  ┌─────────────┐      ┌──────────────┐      ┌──────────────┐     │

│  │   Agents    │      │   Plugins    │      │  MCP Servers │     ││  ✅ Type Safety First     - Full type hints, Mypy strict      │- Plugin developers

│  │ (Reasoning) │      │  (Registry)  │      │  (External)  │     │

│  └─────────────┘      └──────────────┘      └──────────────┘     ││  ✅ Error Handling        - Custom exceptions, recovery       │- DevOps engineers

│         │                      │                      │            │

│         └──────────────────────┼──────────────────────┘            ││  ✅ Resource Safety       - Context managers, proper cleanup  │

│                                ▼                                     │

│                   ┌────────────────────────┐                        ││  ✅ Async Everything      - All I/O is async                  │---

│                   │    LLM Providers       │                        │

│                   │  (OpenAI, Anthropic)   │                        ││  ✅ Observable           - Structured logging, tracing        │

│                   └────────────────────────┘                        │

│                                                                       ││  ✅ Production Ready     - No silent failures, monitoring     │## 2. Architectural Goals

└───────────────────────────────────────────────────────────────────────┘

```└──────────────────────────────────────────────────────────────┘



### 1.3 Component Structure```### 2.1 Primary Goals



```### 1.2 System Context Diagram| Goal | Description | Priority |

src/agent_system/

│|------|-------------|----------|

├── exceptions.py              ← ✅ Custom exception hierarchy

├── app.py                     ← ✅ FastAPI factory + error handlers```| **Modularity** | Loosely coupled components, plugin-based extensibility | High |

│

├── services/┌─────────────────────────────────────────────────────────────────────┐| **Scalability** | Support multiple concurrent users and sessions | High |

│   ├── agent_service.py       ← ✅ Retry logic, cancellation

│   ├── session_service.py     ← ✅ File locking, atomic writes│                          AgentSystem v1.1                            │| **Configurability** | YAML-driven configuration without code changes | High |

│   ├── mcp_service.py         ← ✅ Connection pooling

│   └── config_service.py      ← Configuration management│                                                                       │| **Reliability** | Graceful error handling, cancellation support | High |

│

├── servers/│  ┌───────────┐         ┌──────────────┐         ┌────────────┐     │| **Developer Experience** | Clear APIs, good documentation, easy plugin authoring | Medium |

│   └── agent/

│       └── server.py          ← ✅ Agent with error recovery│  │  Web UI   │◄───────►│  FastAPI App │◄───────►│    CLI     │     │| **Performance** | Caching, parallel execution, efficient resource usage | Medium |

│

├── plugins/│  │ (Browser) │  HTTP   │  (app.py)    │  Local  │ (terminal) │     │

│   └── registry.py            ← Plugin discovery & loading

││  └───────────┘  /SSE   └──────┬───────┘         └────────────┘     │### 2.2 Non-Goals

├── mcp/

│   ├── client.py              ← MCP client│                                │                                     │

│   ├── integration.py         ← MCP aggregation

│   └── transport.py           ← HTTP/SSE transport│                                ▼                                     │- Distributed agent execution (single-process architecture)

│

└── api/│                   ┌────────────────────────┐                        │- Built-in LLM training or fine-tuning

    ├── endpoints.py           ← REST endpoints

    ├── streaming.py           ← SSE streaming│                   │   Exception Handler    │ ← Centralized          │- GUI-based configuration (YAML/API only)

    └── auth.py                ← JWT authentication

```│                   │  (Global Error Mgmt)   │                        │- Real-time collaboration between multiple users on same session



---│                   └────────────┬───────────┘                        │



## 2. Exception System│                                │                                     │---



### 2.1 Exception Hierarchy│                                ▼                                     │



**File:** `src/agent_system/exceptions.py`│                   ┌────────────────────────┐                        │## 3. System Context



```python│                   │    Service Layer       │                        │

"""

Custom exception hierarchy for AgentSystem v1.1.│                   │  ┌─────────────────┐   │                        │### 3.1 Context Diagram



All exceptions inherit from AgentSystemError for easy catching.│                   │  │ AgentService    │   │ ← Retry logic          │

Each exception carries relevant context as attributes.

HTTP status codes are mapped in EXCEPTION_STATUS_MAP.│                   │  │ SessionService  │   │ ← File locking         │```

"""

│                   │  │ MCPService      │   │ ← Connection pool      │┌─────────────────────────────────────────────────────────────────┐

from typing import Optional, Any

│                   │  └─────────────────┘   │                        ││                         AgentSystem                              │

class AgentSystemError(Exception):

    """│                   └────────────┬───────────┘                        ││                                                                  │

    Base exception for all AgentSystem errors.

    │                                │                                     ││  ┌────────────────┐         ┌────────────────┐                 │

    All custom exceptions inherit from this.

    Allows catching all system errors with: except AgentSystemError│         ┌──────────────────────┼──────────────────────┐            ││  │   Web UI       │◄───────►│   FastAPI App  │                 │

    """

    pass│         │                      │                      │            ││  │   (Browser)    │   HTTP  │   (REST API)   │                 │



# ===== Configuration Errors =====│         ▼                      ▼                      ▼            ││  └────────────────┘  /SSE   └────────┬───────┘                 │



class ConfigurationError(AgentSystemError):│  ┌─────────────┐      ┌──────────────┐      ┌──────────────┐     ││                                       │                          │

    """

    Configuration is invalid or missing.│  │   Agents    │      │   Plugins    │      │  MCP Servers │     ││  ┌────────────────┐                  │                          │

    

    Raised when:│  │ (Reasoning) │      │  (Registry)  │      │  (External)  │     ││  │   CLI Client   │◄─────────────────┘                          │

    - Config file not found

    - Invalid YAML syntax│  └─────────────┘      └──────────────┘      └──────────────┘     ││  │   (Terminal)   │         │                                   │

    - Schema validation fails

    - Missing required fields│         │                      │                      │            ││  └────────────────┘         │                                   │

    

    Attributes:│         └──────────────────────┼──────────────────────┘            ││                              ▼                                   │

        config_path: Path to config file

        field: Specific field that failed│                                ▼                                     ││                     ┌────────────────┐                          │

    

    HTTP Status: 500 Internal Server Error│                   ┌────────────────────────┐                        ││                     │  Agent Service │                          │

    """

    │                   │    LLM Providers       │                        ││                     │   (Core Logic) │                          │

    def __init__(

        self,│                   │  (OpenAI, Anthropic)   │                        ││                     └────────┬───────┘                          │

        message: str,

        config_path: Optional[str] = None,│                   └────────────────────────┘                        ││                              │                                   │

        field: Optional[str] = None

    ):│                                                                       ││         ┌────────────────────┼────────────────────┐            │

        self.config_path = config_path

        self.field = field└───────────────────────────────────────────────────────────────────────┘│         │                    │                    │            │

        super().__init__(message)

```│         ▼                    ▼                    ▼            │

# ===== Session Errors =====

│  ┌─────────────┐   ┌─────────────────┐   ┌─────────────┐    │

class SessionNotFoundError(AgentSystemError):

    """### 1.3 Component Structure│  │   Agents    │   │   Plugins       │   │   Config    │    │

    Session does not exist.

    │  │  (Executor) │   │   (Tools/Hooks) │   │   (YAML)    │    │

    Raised when:

    - Session ID doesn't exist in storage```│  └─────────────┘   └─────────────────┘   └─────────────┘    │

    - Session file deleted

    src/agent_system/│         │                    │                                │

    Attributes:

        session_id: ID of missing session││         └────────────────────┴────────────┐                  │

    

    HTTP Status: 404 Not Found├── exceptions.py              ← ✅ Custom exception hierarchy│                                            ▼                   │

    """

    ├── app.py                     ← ✅ FastAPI factory + error handlers│                                    ┌──────────────┐           │

    def __init__(self, session_id: str):

        self.session_id = session_id││                                    │  LLM Clients │           │

        super().__init__(f"Session not found: {session_id}")

├── services/│                                    │ (OpenAI/etc) │           │

class SessionPermissionError(AgentSystemError):

    """│   ├── agent_service.py       ← ✅ Retry logic, cancellation│                                    └──────────────┘           │

    User lacks permission to access session.

    │   ├── session_service.py     ← ✅ File locking, atomic writes└──────────────────────────────────────────────────────────────┘

    Raised when:

    - User tries to access another user's session│   ├── mcp_service.py         ← ✅ Connection pooling         │                    │                    │

    - Session ownership check fails

    │   └── config_service.py      ← Configuration management         ▼                    ▼                    ▼

    Attributes:

        session_id: Session being accessed│┌──────────────┐   ┌───────────────┐   ┌──────────────────┐

        username: User attempting access

    ├── servers/│  File System │   │  External MCP │   │  LLM Providers   │

    HTTP Status: 403 Forbidden

    """│   └── agent/│  (Sessions,  │   │    Servers    │   │  (OpenAI, etc)   │

    

    def __init__(self, session_id: str, username: str):│       └── server.py          ← ✅ Agent with error recovery│   Config)    │   │               │   │                  │

        self.session_id = session_id

        self.username = username│└──────────────┘   └───────────────┘   └──────────────────┘

        super().__init__(

            f"User '{username}' cannot access session '{session_id}'"├── plugins/```

        )

│   └── registry.py            ← Plugin discovery & loading

# ===== Plugin Errors =====

│### 3.2 External Systems

class PluginLoadError(AgentSystemError):

    """├── mcp/

    Plugin failed to load.

    │   ├── client.py              ← MCP client| System | Protocol | Purpose |

    Raised when:

    - Plugin file not found│   ├── integration.py         ← MCP aggregation|--------|----------|---------|

    - Import error

    - Missing required methods│   └── transport.py           ← HTTP/SSE transport| **LLM Providers** | HTTP/HTTPS | AI model inference (OpenAI, Ollama, etc.) |

    - Initialization fails

    │| **External MCP Servers** | HTTP/SSE | External tool integration (Context7, Memory, etc.) |

    Attributes:

        plugin_name: Name of plugin└── api/| **File System** | Local I/O | Configuration, sessions, cache storage |

        reason: Why it failed

        ├── endpoints.py           ← REST endpoints| **Web Browsers** | HTTP/SSE | Web UI access, real-time updates |

    HTTP Status: 500 Internal Server Error

    """    ├── streaming.py           ← SSE streaming| **CLI Clients** | HTTP | Command-line interface |

    

    def __init__(self, plugin_name: str, reason: str):    └── auth.py                ← JWT authentication

        self.plugin_name = plugin_name

        self.reason = reason```---

        super().__init__(f"Failed to load plugin '{plugin_name}': {reason}")

---## 4. Component Architecture

class ToolExecutionError(AgentSystemError):

    """## 2. Exception System### 4.1 High-Level Architecture

    Tool execution failed.

    ### 2.1 Exception Hierarchy```

    Raised when:

    - Tool raises exception┌─────────────────────────────────────────────────────────────────┐

    - Tool times out

    - Invalid tool arguments**File:** `src/agent_system/exceptions.py`│                      Presentation Layer                          │

    

    Attributes:├─────────────────────────────────────────────────────────────────┤

        tool_name: Name of tool

        reason: Error description```python│  FastAPI Routes  │  WebSocket/SSE  │  Static Files (UI)         │

    

    HTTP Status: 500 Internal Server Error"""└─────────────────────────────────────────────────────────────────┘

    Note: Often non-fatal - agent continues with error message

    """Custom exception hierarchy for AgentSystem v1.1.                              │

    

    def __init__(self, tool_name: str, reason: str):                              ▼

        self.tool_name = tool_name

        self.reason = reasonAll exceptions inherit from AgentSystemError for easy catching.┌─────────────────────────────────────────────────────────────────┐

        super().__init__(f"Tool '{tool_name}' failed: {reason}")

Each exception carries relevant context as attributes.│                      Application Layer                           │

# ===== LLM Errors =====

HTTP status codes are mapped in EXCEPTION_STATUS_MAP.├─────────────────────────────────────────────────────────────────┤

class LLMConnectionError(AgentSystemError):

    """"""│  Agent Service   │  Session Manager  │  Auth Service            │

    Cannot connect to LLM service.

    │  Status Bus      │  Cancellation Mgr │  Config Loader           │

    Raised when:

    - Network errorfrom typing import Optional, Any└─────────────────────────────────────────────────────────────────┘

    - LLM service down

    - Timeout                              │

    - Invalid API key

                                  ▼

    Attributes:

        provider: LLM provider (openai, anthropic, etc.)class AgentSystemError(Exception):┌─────────────────────────────────────────────────────────────────┐

        reason: Error description

        """│                       Domain Layer                               │

    HTTP Status: 503 Service Unavailable

    Retryable: Yes (with exponential backoff)    Base exception for all AgentSystem errors.├─────────────────────────────────────────────────────────────────┤

    """

        │  Agent (Executor)         │  Plugin Registry                     │

    def __init__(self, provider: str, reason: str):

        self.provider = provider    All custom exceptions inherit from this.│  MCP Integration          │  Hook System                         │

        self.reason = reason

        super().__init__(    Allows catching all system errors with: except AgentSystemError│  LLM Clients              │  Tool Execution Manager              │

            f"LLM connection failed ({provider}): {reason}"

        )    """└─────────────────────────────────────────────────────────────────┘



class LLMRateLimitError(AgentSystemError):    pass                              │

    """

    LLM service rate limit exceeded.                              ▼

    

    Raised when:┌─────────────────────────────────────────────────────────────────┐

    - Too many requests

    - Token limit reached# ===== Configuration Errors =====│                    Infrastructure Layer                          │

    - Concurrent request limit

    ├─────────────────────────────────────────────────────────────────┤

    Attributes:

        provider: LLM providerclass ConfigurationError(AgentSystemError):│  File Storage    │  Cache System   │  HTTP Clients              │

        retry_after: Seconds to wait before retry

        """│  JSON Serializer │  Logger         │  Event Bus                 │

    HTTP Status: 429 Too Many Requests

    Retryable: Yes (after delay)    Configuration is invalid or missing.└─────────────────────────────────────────────────────────────────┘

    """

        ```

    def __init__(self, provider: str, retry_after: int = 60):

        self.provider = provider    Raised when:

        self.retry_after = retry_after

        super().__init__(    - Config file not found### 4.2 Core Components

            f"Rate limit exceeded ({provider}). "

            f"Retry after {retry_after}s"    - Invalid YAML syntax

        )

    - Schema validation fails#### 4.2.1 FastAPI Application (`app.py`)

# ===== Validation Errors =====

    - Missing required fields

class ValidationError(AgentSystemError):

    """    **Responsibilities:**

    Data validation failed.

        Attributes:- HTTP server lifecycle management

    Raised when:

    - Pydantic validation fails        config_path: Path to config file- Route registration and middleware

    - Invalid input format

    - Missing required fields        field: Specific field that failed- Lifespan events (startup/shutdown)

    - Type mismatch

        - CORS, authentication, rate limiting

    Attributes:

        field: Field that failed    HTTP Status: 500 Internal Server Error

        value: Invalid value

        """**Key Files:**

    HTTP Status: 422 Unprocessable Entity

    """    - `src/agent_system/app.py` - Application factory

    

    def __init__(    def __init__(- `src/agent_system/api/endpoints.py` - API routes

        self,

        message: str,        self,- `src/agent_system/api/streaming.py` - SSE endpoints

        field: Optional[str] = None,

        value: Optional[Any] = None        message: str,

    ):

        self.field = field        config_path: Optional[str] = None,**Dependencies:**

        self.value = value

        super().__init__(message)        field: Optional[str] = None- FastAPI framework



# ===== HTTP Status Code Mapping =====    ):- Uvicorn ASGI server



EXCEPTION_STATUS_MAP: dict[type[AgentSystemError], int] = {        self.config_path = config_path- Pydantic models

    SessionNotFoundError: 404,

    SessionPermissionError: 403,        self.field = field

    ValidationError: 422,

    LLMRateLimitError: 429,        super().__init__(message)#### 4.2.2 Agent Service (`services/agent_service.py`)

    LLMConnectionError: 503,

    ToolExecutionError: 500,**Responsibilities:**

    PluginLoadError: 500,

    ConfigurationError: 500,# ===== Session Errors =====- Agent execution orchestration

    AgentSystemError: 500,  # Fallback for any other AgentSystemError

}- Request lifecycle management

```

class SessionNotFoundError(AgentSystemError):- Status event coordination

### 2.2 Centralized Exception Handler

    """- Error handling and recovery

**File:** `src/agent_system/app.py` (excerpt)

    Session does not exist.

```python

"""    **Key Interfaces:**

FastAPI application with centralized error handling.

"""    Raised when:```python



from fastapi import FastAPI, Request    - Session ID doesn't exist in storageclass AgentService:

from fastapi.responses import JSONResponse

from contextlib import asynccontextmanager    - Session file deleted    async def run_agent_stream(

from datetime import datetime

import logging            request_id: str,



from agent_system.exceptions import (    Attributes:        agent_name: str,

    AgentSystemError,

    EXCEPTION_STATUS_MAP        session_id: ID of missing session        task: str,

)

            session_id: str

logger = logging.getLogger(__name__)

    HTTP Status: 404 Not Found    ) -> AsyncGenerator[dict, None]

@asynccontextmanager

async def lifespan(app: FastAPI):    """```

    """

    Manage application lifecycle.    

    

    Startup:    def __init__(self, session_id: str):#### 4.2.3 Agent (`servers/agent/server.py`)

    - Initialize services

    - Connect to external systems        self.session_id = session_id

    - Load configuration

            super().__init__(f"Session not found: {session_id}")**Responsibilities:**

    Shutdown:

    - Close MCP connections (always executes)- Multi-step reasoning loop

    - Flush logs

    - Clean up resources- Tool discovery and execution

    """

    # === Startup ===class SessionPermissionError(AgentSystemError):- LLM interaction

    logger.info("Starting AgentSystem v1.1...")

        """- Context management

    try:

        # Initialize services    User lacks permission to access session.

        await init_services()

            **Key Interfaces:**

        logger.info("AgentSystem ready")

            Raised when:```python

        yield  # Application runs here

        - User tries to access another user's sessionclass Agent(MCPServer):

    finally:

        # === Shutdown (always executes) ===    - Session ownership check fails    async def run_events(

        logger.info("Shutting down AgentSystem...")

                    task: str,

        # Close MCP connections

        if mcp_service:    Attributes:        request_id: str,

            try:

                await mcp_service.close_all()        session_id: Session being accessed        session_id: str,

            except Exception as e:

                logger.error("Error closing MCP connections: %s", e)        username: User attempting access        ...

        

        # Cancel background tasks        ) -> AsyncGenerator[dict, None]

        import asyncio

        tasks = [    HTTP Status: 403 Forbidden```

            t for t in asyncio.all_tasks()

            if t is not asyncio.current_task()    """

        ]

        for task in tasks:    #### 4.2.4 Plugin Registry (`plugins/registry.py`)

            task.cancel()

            def __init__(self, session_id: str, username: str):

        await asyncio.gather(*tasks, return_exceptions=True)

                self.session_id = session_id**Responsibilities:**

        logger.info("Shutdown complete")

        self.username = username- Plugin discovery (filesystem + config)

def build_app() -> FastAPI:

    """        super().__init__(- Factory registration and instantiation

    Build and configure FastAPI application.

                f"User '{username}' cannot access session '{session_id}'"- Metadata management

    Returns:

        Configured FastAPI app with:        )

        - Exception handlers

        - Middleware (CORS, auth, request ID)**Key Features:**

        - Routers

        - Lifespan management- Auto-discovery from `src/plugins/`

    """

    app = FastAPI(# ===== Plugin Errors =====- Config-based agent registration

        title="AgentSystem",

        version="1.1",- Lazy initialization support

        lifespan=lifespan

    )class PluginLoadError(AgentSystemError):

    

    # === Exception Handlers ===    """#### 4.2.5 MCP Integration (`mcp/integration.py`)

    

    @app.exception_handler(AgentSystemError)    Plugin failed to load.

    async def handle_agentsystem_error(

        request: Request,    **Responsibilities:**

        exc: AgentSystemError

    ) -> JSONResponse:    Raised when:- External MCP server connections

        """

        Handle all custom AgentSystem exceptions.    - Plugin file not found- Tool list aggregation

        

        - Maps exception to HTTP status code    - Import error- Tool call routing

        - Logs with appropriate severity

        - Returns structured JSON error    - Missing required methods- Connection health monitoring

        - Includes request context

        """    - Initialization fails

        # Get HTTP status code

        status_code = EXCEPTION_STATUS_MAP.get(type(exc), 500)    **Key Components:**

        

        # Determine log level    Attributes:- `MCPClientManager` - Client lifecycle

        if status_code >= 500:

            log_level = logging.ERROR        plugin_name: Name of plugin- `MCPHTTPServer` - Server mode

        elif status_code >= 400:

            log_level = logging.WARNING        reason: Why it failed- `ToolCache` - Tool list caching

        else:

            log_level = logging.INFO    

        

        # Extract request ID    HTTP Status: 500 Internal Server Error#### 4.2.6 Hook System (`hooks/`)

        request_id = getattr(request.state, 'request_id', None)

            """

        # Log with context

        logger.log(    **Responsibilities:**

            log_level,

            "%s: %s",    def __init__(self, plugin_name: str, reason: str):- Lifecycle event interception

            type(exc).__name__,

            str(exc),        self.plugin_name = plugin_name- Plugin hook execution

            extra={

                "exception_type": type(exc).__name__,        self.reason = reason- Order management and dependencies

                "status_code": status_code,

                "path": request.url.path,        super().__init__(f"Failed to load plugin '{plugin_name}': {reason}")- Error isolation

                "method": request.method,

                "request_id": request_id,**Hook Types:**

                "user": getattr(request.state, 'user', None)

            }class ToolExecutionError(AgentSystemError):- `pre_llm_call` - Before LLM request

        )

            """- `post_llm_call` - After LLM response

        # Build error response

        error_response = {    Tool execution failed.- `pre_tool_call` - Before tool execution

            "error": type(exc).__name__,

            "message": str(exc),    - `post_tool_call` - After tool execution

            "path": request.url.path,

            "timestamp": datetime.utcnow().isoformat() + "Z"    Raised when:- `format_output` - Output formatting

        }

            - Tool raises exception- `session_start/end` - Session lifecycle

        if request_id:

            error_response["request_id"] = request_id    - Tool times out

        

        # Add exception-specific details    - Invalid tool arguments#### 4.2.7 Configuration System (`config/`)

        details = {}

        if hasattr(exc, 'session_id'):    

            details["session_id"] = exc.session_id

        if hasattr(exc, 'plugin_name'):    Attributes:**Responsibilities:**

            details["plugin_name"] = exc.plugin_name

        if hasattr(exc, 'tool_name'):        tool_name: Name of tool- Multi-file YAML loading

            details["tool_name"] = exc.tool_name

        if hasattr(exc, 'provider'):        reason: Error description- Environment variable substitution

            details["provider"] = exc.provider

        if hasattr(exc, 'retry_after'):    - Schema validation

            details["retry_after"] = exc.retry_after

            error_response["retryable"] = True    HTTP Status: 500 Internal Server Error- Pydantic model binding

        

        if details:    Note: Often non-fatal - agent continues with error message

            error_response["details"] = details

            """**Configuration Files:**

        return JSONResponse(

            status_code=status_code,    - `config/config.yaml` - Main system config

            content=error_response

        )    def __init__(self, tool_name: str, reason: str):- `config/llm.yaml` - LLM profiles

    

    @app.exception_handler(Exception)        self.tool_name = tool_name- `config/agents.yaml` - Config-based agents

    async def handle_unexpected_error(

        request: Request,        self.reason = reason- `config/mcp_servers.yaml` - External MCP servers

        exc: Exception

    ) -> JSONResponse:        super().__init__(f"Tool '{tool_name}' failed: {reason}")- `config/plugins.yaml` - Plugin overrides

        """

        Catch-all for unexpected exceptions.---

        

        - Logs full stack trace# ===== LLM Errors =====

        - Returns generic error (no sensitive data)

        - Triggers alerts## 5. Key Design Decisions

        """

        request_id = getattr(request.state, 'request_id', None)class LLMConnectionError(AgentSystemError):

        

        # Log full exception    """### 5.1 Decision Records

        logger.exception(

            "Unexpected exception: %s",    Cannot connect to LLM service.

            str(exc),

            extra={    #### ADR-001: FastAPI over Flask

                "exception_type": type(exc).__name__,

                "path": request.url.path,    Raised when:

                "method": request.method,

                "request_id": request_id,    - Network error**Context:** Need async HTTP server with SSE support  

                "user_agent": request.headers.get("user-agent"),

                "user": getattr(request.state, 'user', None)    - LLM service down**Decision:** Use FastAPI with Uvicorn  

            }

        )    - Timeout**Rationale:**

        

        # Return generic error    - Invalid API key- Native async/await support

        return JSONResponse(

            status_code=500,    - Built-in OpenAPI documentation

            content={

                "error": "InternalServerError",    Attributes:- Excellent SSE streaming performance

                "message": "An unexpected error occurred. Please try again later.",

                "path": request.url.path,        provider: LLM provider (openai, anthropic, etc.)- Type hints and validation via Pydantic

                "timestamp": datetime.utcnow().isoformat() + "Z",

                "request_id": request_id,        reason: Error description

                "support": "Contact support if this persists."

            }    **Status:** Accepted

        )

        HTTP Status: 503 Service Unavailable

    # Register routers, middleware, etc.

    # ...    Retryable: Yes (with exponential backoff)---

    

    return app    """

```

    #### ADR-002: YAML-Based Configuration

---

    def __init__(self, provider: str, reason: str):

## 3. Service Layer

        self.provider = provider**Context:** Need flexible, user-friendly configuration  

### 3.1 AgentService

        self.reason = reason**Decision:** Multi-file YAML configuration with Pydantic models  

**File:** `src/agent_system/services/agent_service.py`

        super().__init__(**Rationale:**

```python

"""            f"LLM connection failed ({provider}): {reason}"- Human-readable and editable

Agent execution service with error recovery and retry logic.

"""        )- Support for comments and documentation



import asyncio- Schema validation via Pydantic

import logging

from typing import AsyncGenerator, Optional, Any- Environment variable substitution



from agent_system.exceptions import (class LLMRateLimitError(AgentSystemError):- Separation of concerns (llm.yaml, agents.yaml, etc.)

    SessionNotFoundError,

    SessionPermissionError,    """

    PluginLoadError,

    LLMConnectionError,    LLM service rate limit exceeded.**Status:** Accepted

    LLMRateLimitError,

)    



logger = logging.getLogger(__name__)    Raised when:---



class AgentService:    - Too many requests

    """

    Service for agent execution orchestration.    - Token limit reached#### ADR-003: Plugin Architecture

    

    Features:    - Concurrent request limit

    - Agent instantiation from plugins

    - Execution with error recovery    **Context:** Need extensible tool and hook system  

    - Retry logic for transient failures (LLM connection errors)

    - Status streaming via async generators    Attributes:**Decision:** Dual plugin types (Tools via MCPServer, Hooks via PluginHook)  

    - Request cancellation support

    - Proper session management        provider: LLM provider**Rationale:**

    

    Thread Safety: Yes (async-safe with locks)        retry_after: Seconds to wait before retry- Clear separation of concerns

    """

        - Minimal inheritance (composition over inheritance)

    def __init__(

        self,    HTTP Status: 429 Too Many Requests- Supports pure tool plugins, pure hook plugins, and hybrids

        session_service: 'SessionService',

        mcp_service: 'MCPService',    Retryable: Yes (after delay)- Standard MCP protocol for tools

        plugin_registry: 'PluginRegistry',

        config: 'Config'    """

    ):

        """    **Status:** Accepted

        Initialize agent service.

            def __init__(self, provider: str, retry_after: int = 60):

        Args:

            session_service: For session CRUD operations        self.provider = provider---

            mcp_service: For MCP tool access

            plugin_registry: For agent discovery        self.retry_after = retry_after

            config: System configuration

        """        super().__init__(#### ADR-004: Config-Based Agents

        self.session_service = session_service

        self.mcp_service = mcp_service            f"Rate limit exceeded ({provider}). "

        self.plugin_registry = plugin_registry

        self.config = config            f"Retry after {retry_after}s"**Context:** Many agents differ only in prompts and tool access  

        

        # Track active requests for cancellation        )**Decision:** Support YAML-defined agents without Python code  

        self._active_requests: dict[str, asyncio.Task] = {}

    **Rationale:**

    async def run_agent_stream(

        self,- Lower barrier to entry for non-developers

        request_id: str,

        agent_name: str,# ===== Validation Errors =====- Faster prototyping and iteration

        task: str,

        session_id: str,- Reduced code duplication

        user: str,

        output_format: str = "json",class ValidationError(AgentSystemError):- Coexists with plugin-based agents

        cancellation_token: Optional[str] = None

    ) -> AsyncGenerator[dict[str, Any], None]:    """

        """

        Execute agent and stream results.    Data validation failed.**Status:** Accepted

        

        Args:    

            request_id: Unique request identifier

            agent_name: Agent to execute    Raised when:---

            task: User's task/question

            session_id: Session context    - Pydantic validation fails

            user: Username (for authorization)

            output_format: Output format (json/html/markdown)    - Invalid input format#### ADR-005: Session-Based Architecture

            cancellation_token: Optional token for cancellation

            - Missing required fields

        Yields:

            Events in SSE format:    - Type mismatch**Context:** Support multiple users with isolated contexts  

            - {"type": "status", "status": "starting", ...}

            - {"type": "status", "status": "running", "step": 1, ...}    **Decision:** Session-based storage with per-user directories  

            - {"type": "status", "status": "tool_execution", "tool": "...", ...}

            - {"type": "status", "status": "retrying", "attempt": 1, ...}    Attributes:**Rationale:**

            - {"type": "result", "result": "...", ...}

            - {"type": "error", "error": "...", "message": "...", ...}        field: Field that failed- Data isolation between users

        

        Raises:        value: Invalid value- Persistent conversation history

            SessionNotFoundError: Session doesn't exist

            SessionPermissionError: User can't access session    - Easy backup and migration

            PluginLoadError: Agent not found

            LLMConnectionError: LLM unavailable (after retries)    HTTP Status: 422 Unprocessable Entity- Simpler than database for MVP

            LLMRateLimitError: Rate limit exceeded

        """    """

        # Load and validate session

        session = await self.session_service.load_session(session_id)    **Status:** Accepted

        

        if session.user != user:    def __init__(

            raise SessionPermissionError(session_id, user)

                self,---

        # Create agent instance

        agent = await self._create_agent(agent_name, session_id)        message: str,

        

        # Track request for cancellation        field: Optional[str] = None,#### ADR-006: Status Streaming via SSE

        current_task = asyncio.current_task()

        if current_task:        value: Optional[Any] = None

            self._active_requests[request_id] = current_task

            ):**Context:** Need real-time progress updates in UI  

        try:

            yield {        self.field = field**Decision:** Server-Sent Events for status streaming  

                "type": "status",

                "status": "starting",        self.value = value**Rationale:**

                "request_id": request_id,

                "agent": agent_name        super().__init__(message)- Simpler than WebSockets for one-way communication

            }

            - Better browser compatibility

            # Execute with retry logic

            async for event in self._execute_with_retry(- Automatic reconnection

                agent=agent,

                task=task,# ===== HTTP Status Code Mapping =====- HTTP/2 multiplexing support

                request_id=request_id,

                session_id=session_id,EXCEPTION_STATUS_MAP: dict[type[AgentSystemError], int] = {**Status:** Accepted

                output_format=output_format,

                max_retries=3    SessionNotFoundError: 404,

            ):

                yield event    SessionPermissionError: 403,---

        

        except asyncio.CancelledError:    ValidationError: 422,

            logger.info("Request cancelled: %s", request_id)

            yield {    LLMRateLimitError: 429,### 5.2 Technology Choices

                "type": "status",

                "status": "cancelled",    LLMConnectionError: 503,

                "request_id": request_id

            }    ToolExecutionError: 500,| Component | Technology | Rationale |

            raise

            PluginLoadError: 500,|-----------|-----------|-----------|

        except LLMRateLimitError as e:

            logger.warning(    ConfigurationError: 500,| **Web Framework** | FastAPI | Async, type hints, OpenAPI |

                "Rate limit exceeded: provider=%s, retry_after=%d",

                e.provider, e.retry_after    AgentSystemError: 500,  # Fallback for any other AgentSystemError| **ASGI Server** | Uvicorn | Performance, HTTP/2, WebSockets |

            )

            yield {}| **LLM Client** | LiteLLM | Multi-provider support |

                "type": "error",

                "error": "RateLimitError",```| **Validation** | Pydantic | Type safety, validation, serialization |

                "message": str(e),

                "retry_after": e.retry_after,| **Config Format** | YAML | Human-readable, comments, nesting |

                "retryable": True

            }### 2.2 Centralized Exception Handler| **Session Storage** | JSON files | Simple, inspectable, version-controllable |

            raise

        | **Caching** | In-memory + file | Performance, persistence |

        except LLMConnectionError as e:

            logger.error("LLM connection failed: %s", e)**File:** `src/agent_system/app.py` (excerpt)| **Logging** | Python logging | Standard library, configurable |

            yield {

                "type": "error",```python---

                "error": "LLMConnectionError",

                "message": "AI service temporarily unavailable. Please try again.","""

                "retryable": True

            }FastAPI application with centralized error handling.## 6. Data Flow

            raise

        """

        finally:

            # Always clean up### 6.1 Agent Execution Flow

            self._active_requests.pop(request_id, None)

            from fastapi import FastAPI, Request

            # Save session (best-effort - don't fail request)

            try:from fastapi.responses import JSONResponse```

                await self.session_service.save_session(session)

            except Exception as e:from contextlib import asynccontextmanagerUser Request (HTTP/CLI)

                logger.error(

                    "Failed to save session: %s",from datetime import datetime         │

                    e,

                    extra={import logging         ▼

                        "session_id": session_id,

                        "user": user,   FastAPI Endpoint

                        "request_id": request_id

                    }from agent_system.exceptions import (         │

                )

                # Don't raise - session save is best-effort    AgentSystemError,         ▼

    

    async def cancel_request(self, request_id: str) -> bool:    EXCEPTION_STATUS_MAP   Agent Service

        """

        Cancel an active request.)         │

        

        Args:         ├─► Status Bus (emit "starting")

            request_id: Request to cancel

        logger = logging.getLogger(__name__)         │

        Returns:

            True if cancelled, False if not found or already done         ▼

        """

        task = self._active_requests.get(request_id)   Agent.run_events()

        

        if task and not task.done():@asynccontextmanager         │

            task.cancel()

            logger.info("Cancelled request: %s", request_id)async def lifespan(app: FastAPI):         ├─► Load Session History

            return True

            """         ├─► Hook: pre_llm_call

        return False

        Manage application lifecycle.         ├─► LLM Request

    async def list_agents(self) -> list[str]:

        """             ├─► Hook: post_llm_call

        List available agents.

            Startup:         │

        Returns:

            List of agent names    - Initialize services         ├─► Parse Tool Calls

        """

        return list(self.plugin_registry.list_agents())    - Connect to external systems         │      │

    

    # ===== Private Methods =====    - Load configuration         │      ▼

    

    async def _create_agent(             │   Tool Execution Manager

        self,

        agent_name: str,    Shutdown:         │      │

        session_id: str

    ) -> 'Agent':    - Close MCP connections (always executes)         │      ├─► Hook: pre_tool_call

        """

        Create agent instance from plugin registry.    - Flush logs         │      ├─► Execute Tool (Plugin/MCP)

        

        Args:    - Clean up resources         │      ├─► Hook: post_tool_call

            agent_name: Agent to create

            session_id: Session context    """         │      ▼

        

        Returns:    # === Startup ===         │   Tool Results

            Agent instance

            logger.info("Starting AgentSystem v1.1...")         │

        Raises:

            PluginLoadError: Agent not found or failed to initialize             ├─► Repeat until complete

        """

        try:    try:         │

            factory = self.plugin_registry.get_agent_factory(agent_name)

            agent = await factory.create(session_id=session_id)        # Initialize services         ▼

            return agent

                await init_services()   Hook: format_output

        except KeyError:

            raise PluginLoadError(                 │

                agent_name,

                f"Agent '{agent_name}' not found in registry"        logger.info("AgentSystem ready")         ▼

            )

                   Save Session

        except Exception as e:

            logger.exception("Failed to create agent: %s", e)        yield  # Application runs here         │

            raise PluginLoadError(

                agent_name,                 ▼

                f"Agent initialization failed: {e}"

            )    finally:   Return Result (Stream/JSON)

    

    async def _execute_with_retry(        # === Shutdown (always executes) ===```

        self,

        agent: 'Agent',        logger.info("Shutting down AgentSystem...")

        task: str,

        request_id: str,        ### 6.2 Configuration Loading

        session_id: str,

        output_format: str,        # Close MCP connections

        max_retries: int = 3

    ) -> AsyncGenerator[dict[str, Any], None]:        if mcp_service:```

        """

        Execute agent with retry on transient errors.            try:Startup

        

        Retries on:                await mcp_service.close_all()   │

        - LLMConnectionError (network issues)

                    except Exception as e:   ▼

        Does NOT retry on:

        - ValidationError (bad input)                logger.error("Error closing MCP connections: %s", e)Load config/config.yaml

        - SessionPermissionError (auth)

        - LLMRateLimitError (let caller handle)           │

        

        Uses exponential backoff: 1s, 2s, 4s, 8s...        # Cancel background tasks   ├─► Resolve includes (llm.yaml, etc.)

        """

        retry_count = 0        import asyncio   ├─► Substitute environment variables

        backoff = 1.0  # Initial backoff in seconds

                tasks = [   ├─► Validate with Pydantic schemas

        while retry_count <= max_retries:

            try:            t for t in asyncio.all_tasks()    │

                # Execute agent

                async for event in agent.run_events(            if t is not asyncio.current_task()   ▼

                    task=task,

                    request_id=request_id,        ]Initialize Components

                    session_id=session_id,

                    output_format=output_format        for task in tasks:   │

                ):

                    yield event            task.cancel()   ├─► LLM Clients (from llm.yaml)

                

                # Success - exit retry loop           ├─► MCP Integration (from mcp_servers.yaml)

                return

                    await asyncio.gather(*tasks, return_exceptions=True)   ├─► Plugin Registry (discover + config agents)

            except LLMConnectionError as e:

                retry_count += 1           ├─► Hook System (load hooks from plugins)

                

                if retry_count > max_retries:        logger.info("Shutdown complete")   │

                    logger.error(

                        "Max retries exceeded for LLM connection: %s",   ▼

                        e,

                        extra={Ready for Requests

                            "request_id": request_id,

                            "attempts": retry_countdef build_app() -> FastAPI:```

                        }

                    )    """

                    raise

                    Build and configure FastAPI application.### 6.3 Plugin Discovery

                # Log retry

                logger.warning(    

                    "LLM connection failed (attempt %d/%d): %s",

                    retry_count, max_retries, e,    Returns:```

                    extra={"request_id": request_id}

                )        Configured FastAPI app with:System Startup

                

                # Exponential backoff        - Exception handlers   │

                await asyncio.sleep(backoff)

                backoff *= 2        - Middleware (CORS, auth, request ID)   ▼

                

                # Yield retry status        - RoutersFilesystem Discovery

                yield {

                    "type": "status",        - Lifespan management   │

                    "status": "retrying",

                    "attempt": retry_count,    """   ├─► Scan src/plugins/*/plugin.py

                    "max_attempts": max_retries,

                    "message": f"Connection failed. Retrying in {backoff}s...",    app = FastAPI(   ├─► Load plugin.yaml metadata

                    "backoff": backoff

                }        title="AgentSystem",   ├─► Register factories in registry

```

        version="1.1",   │

### 3.2 SessionService

        lifespan=lifespan   ▼

**File:** `src/agent_system/services/session_service.py`

    )Config-Based Agent Discovery

```python

"""       │

Session service with safe file operations.

"""    # === Exception Handlers ===   ├─► Load agents.yaml



import asyncio       ├─► Validate agent definitions

import json

import logging    @app.exception_handler(AgentSystemError)   ├─► Create factories dynamically

from pathlib import Path

from typing import Optional    async def handle_agentsystem_error(   ├─► Register alongside plugins



from agent_system.exceptions import (        request: Request,   │

    SessionNotFoundError,

    ValidationError        exc: AgentSystemError   ▼

)

from agent_system.models import Session    ) -> JSONResponse:Bootstrap Agents



logger = logging.getLogger(__name__)        """   │



class SessionService:        Handle all custom AgentSystem exceptions.   ├─► Instantiate from factories

    """

    Service for session CRUD with proper file handling.           ├─► Apply configuration overrides

    

    Features:        - Maps exception to HTTP status code   ├─► Register in MCP registry

    - ✅ Context managers (no resource leaks)

    - ✅ File locking (no corruption on concurrent writes)        - Logs with appropriate severity   │

    - ✅ Atomic writes (temp file + rename)

    - ✅ Async locks (no race conditions)        - Returns structured JSON error   ▼

    - ✅ Proper error handling

            - Includes request contextReady

    Thread Safety: Yes (per-session async locks)

    """        """```

    

    def __init__(self, data_dir: Path):        # Get HTTP status code

        """

        Initialize session service.        status_code = EXCEPTION_STATUS_MAP.get(type(exc), 500)---

        

        Args:        

            data_dir: Root data directory

        """        # Determine log level## 7. Technology Stack

        self.data_dir = data_dir

        self.sessions_dir = data_dir / "sessions"        if status_code >= 500:

        

        # Per-session async locks for write operations            log_level = logging.ERROR### 7.1 Core Stack

        self._write_locks: dict[str, asyncio.Lock] = {}

            elif status_code >= 400:

    async def load_session(self, session_id: str) -> Session:

        """            log_level = logging.WARNING```yaml

        Load session from disk.

                else:runtime:

        Args:

            session_id: Session identifier            log_level = logging.INFO  language: Python 3.11+

        

        Returns:          framework: FastAPI 0.109+

            Session object

                # Extract request ID  server: Uvicorn

        Raises:

            SessionNotFoundError: Session doesn't exist        request_id = getattr(request.state, 'request_id', None)

            ValidationError: Session file corrupted

        """        dependencies:

        path = self._get_session_path(session_id)

                # Log with context  web:

        if not path.exists():

            raise SessionNotFoundError(session_id)        logger.log(    - fastapi

        

        try:            log_level,    - uvicorn[standard]

            # ✅ Use context manager - file always closed

            with open(path, 'r', encoding='utf-8') as f:            "%s: %s",    - pydantic >= 2.0

                data = json.load(f)

                        type(exc).__name__,    - python-multipart

            # Validate with Pydantic

            return Session(**data)            str(exc),  

        

        except json.JSONDecodeError as e:            extra={  llm:

            logger.error(

                "Corrupted session file: %s",                "exception_type": type(exc).__name__,    - litellm

                path,

                extra={"session_id": session_id, "error": str(e)}                "status_code": status_code,    - openai

            )

            raise ValidationError(                "path": request.url.path,    - anthropic

                f"Session file corrupted: {session_id}",

                field="json"                "method": request.method,  

            )

                        "request_id": request_id,  data:

        except IOError as e:

            logger.error(                "user": getattr(request.state, 'user', None)    - pyyaml

                "Failed to read session file: %s",

                e,            }    - jinja2

                extra={"session_id": session_id}

            )        )    - jsonschema

            raise SessionNotFoundError(session_id)

              

    async def save_session(self, session: Session) -> None:

        """        # Build error response  utilities:

        Save session to disk with locking.

                error_response = {    - httpx

        Strategy:

        1. Acquire per-session async lock            "error": type(exc).__name__,    - python-jose[cryptography]

        2. Write to temp file (.tmp)

        3. Atomic rename to final file            "message": str(exc),    - passlib[bcrypt]

        4. Release lock

                    "path": request.url.path,```

        Args:

            session: Session to save            "timestamp": datetime.utcnow().isoformat() + "Z"

        

        Raises:        }### 7.2 Development Stack

            IOError: Disk write failed

        """        

        # Get or create lock for this session

        lock = self._write_locks.setdefault(        if request_id:```yaml

            session.id,

            asyncio.Lock()            error_response["request_id"] = request_iddevelopment:

        )

                  testing:

        async with lock:

            # Only one write at a time per session        # Add exception-specific details    - pytest

            path = self._get_session_path(session.id)

            path.parent.mkdir(parents=True, exist_ok=True)        details = {}    - pytest-asyncio

            

            # Write to temp file first (atomic operation)        if hasattr(exc, 'session_id'):    - pytest-cov

            temp_path = path.with_suffix('.tmp')

                        details["session_id"] = exc.session_id  

            try:

                # ✅ Use context manager        if hasattr(exc, 'plugin_name'):  code_quality:

                with open(temp_path, 'w', encoding='utf-8') as f:

                    json.dump(            details["plugin_name"] = exc.plugin_name    - ruff

                        session.dict(),

                        f,        if hasattr(exc, 'tool_name'):    - mypy

                        indent=2,

                        ensure_ascii=False            details["tool_name"] = exc.tool_name    - black

                    )

                        if hasattr(exc, 'provider'):  

                # Atomic rename (POSIX: atomic, Windows: best-effort)

                temp_path.replace(path)            details["provider"] = exc.provider  documentation:

                

                logger.debug(        if hasattr(exc, 'retry_after'):    - mkdocs

                    "Saved session: %s",

                    session.id,            details["retry_after"] = exc.retry_after    - mkdocs-material

                    extra={"user": session.user}

                )            error_response["retryable"] = True```

            

            except IOError as e:        

                logger.error(

                    "Failed to save session: %s",        if details:---

                    e,

                    extra={"session_id": session.id}            error_response["details"] = details

                )

                        ## 8. Quality Attributes

                # Clean up temp file

                if temp_path.exists():        return JSONResponse(

                    try:

                        temp_path.unlink()            status_code=status_code,### 8.1 Performance

                    except Exception:

                        pass  # Best effort            content=error_response

                

                raise        )| Metric | Target | Current | Notes |

    

    async def delete_session(self, session_id: str) -> None:    |--------|--------|---------|-------|

        """

        Delete a session.    @app.exception_handler(Exception)| **Agent Response Time** | < 30s | ~10-20s | Depends on LLM latency |

        

        Args:    async def handle_unexpected_error(| **Tool Execution** | Parallel | Parallel | asyncio.gather() |

            session_id: Session to delete

                request: Request,| **Concurrent Users** | 50+ | Tested: 20 | Limited by LLM rate limits |

        Raises:

            SessionNotFoundError: Session doesn't exist        exc: Exception| **Session Load Time** | < 100ms | ~50ms | JSON file I/O |

        """

        lock = self._write_locks.setdefault(    ) -> JSONResponse:| **Tool Cache Hit Rate** | > 80% | ~85% | 30s TTL |

            session_id,

            asyncio.Lock()        """

        )

                Catch-all for unexpected exceptions.### 8.2 Reliability

        async with lock:

            path = self._get_session_path(session_id)        

            

            if not path.exists():        - Logs full stack trace| Aspect | Implementation | Status |

                raise SessionNotFoundError(session_id)

                    - Returns generic error (no sensitive data)|--------|---------------|--------|

            try:

                path.unlink()        - Triggers alerts| **Error Handling** | Try/catch, graceful degradation | ✅ Implemented |

                logger.info("Deleted session: %s", session_id)

                        """| **Cancellation** | Token-based, cooperative | ✅ Implemented |

                # Clean up lock

                self._write_locks.pop(session_id, None)        request_id = getattr(request.state, 'request_id', None)| **Retries** | Exponential backoff for LLM/MCP | ✅ Implemented |

            

            except IOError as e:        | **Validation** | Pydantic models, schema checks | ✅ Implemented |

                logger.error(

                    "Failed to delete session: %s",        # Log full exception| **Logging** | Structured logging, levels | ✅ Implemented |

                    e,

                    extra={"session_id": session_id}        logger.exception(

                )

                raise            "Unexpected exception: %s",### 8.3 Security

    

    async def list_sessions(self, user: str) -> list[Session]:            str(exc),

        """

        List all sessions for a user.            extra={| Feature | Status | Notes |

        

        Args:                "exception_type": type(exc).__name__,|---------|--------|-------|

            user: Username

                        "path": request.url.path,| **Authentication** | ✅ JWT + API Keys | Optional, configurable |

        Returns:

            List of sessions (sorted by updated_at desc)                "method": request.method,| **Authorization** | ✅ User-based sessions | Per-user isolation |

        """

        user_dir = self.sessions_dir / user                "request_id": request_id,| **Input Validation** | ✅ Pydantic models | All API inputs validated |

        

        if not user_dir.exists():                "user_agent": request.headers.get("user-agent"),| **Rate Limiting** | ✅ Token bucket | Configurable per-user |

            return []

                        "user": getattr(request.state, 'user', None)| **CORS** | ✅ Configurable | Default: localhost only |

        sessions = []

                    }| **Secrets Management** | ✅ Env vars | No secrets in config files |

        for path in user_dir.glob("*.json"):

            try:        )

                session = await self.load_session(path.stem)

                sessions.append(session)        ### 8.4 Maintainability

            except Exception as e:

                logger.warning(        # Return generic error

                    "Skipping corrupted session: %s",

                    path,        return JSONResponse(| Aspect | Score | Notes |

                    extra={"error": str(e)}

                )            status_code=500,|--------|-------|-------|

        

        # Sort by updated_at descending            content={| **Code Coverage** | 75% | Target: 80% |

        sessions.sort(key=lambda s: s.updated_at, reverse=True)

                        "error": "InternalServerError",| **Documentation** | Good | SAD, API docs, user guides |

        return sessions

                    "message": "An unexpected error occurred. Please try again later.",| **Modularity** | Excellent | Clear component boundaries |

    def _get_session_path(self, session_id: str) -> Path:

        """                "path": request.url.path,| **Type Safety** | Good | Pydantic + type hints |

        Get file path for session.

                        "timestamp": datetime.utcnow().isoformat() + "Z",| **Code Quality** | Good | Ruff, Mypy checks |

        Session files stored as:

        data/sessions/<user>/<session_id>.json                "request_id": request_id,

        

        Args:                "support": "Contact support if this persists."---

            session_id: Session ID (format: user_timestamp)

                    }

        Returns:

            Path to session file        )## 9. Deployment View

        """

        # Extract user from session_id (format: user_timestamp)    

        user = session_id.split('_')[0]

        return self.sessions_dir / user / f"{session_id}.json"    # Register routers, middleware, etc.### 9.1 Single-Server Deployment

```

    # ...

---

    ```

## 4. Agent Architecture

    return app┌─────────────────────────────────────┐

### 4.1 Agent Class

```│         Server (Linux/Windows)      │

**File:** `src/agent_system/servers/agent/server.py`

│                                     │

```python

"""---│  ┌──────────────────────────────┐  │

Core agent implementation with reasoning loop.

"""│  │   AgentSystem Process        │  │



import asyncio## 3. Service Layer│  │   (Python + Uvicorn)         │  │

import logging

from typing import AsyncGenerator, Any, Optional│  │                              │  │



from agent_system.exceptions import (### 3.1 AgentService│  │   Port: 8000 (HTTP)          │  │

    LLMConnectionError,

    LLMRateLimitError,│  └──────────────────────────────┘  │

    ToolExecutionError

)**File:** `src/agent_system/services/agent_service.py`│              │                      │

from agent_system.mcp.server import MCPServer

│              ▼                      │

logger = logging.getLogger(__name__)

```python│  ┌──────────────────────────────┐  │

class Agent(MCPServer):

    """"""│  │   File System                │  │

    Core agent with multi-step reasoning.

    Agent execution service with error recovery and retry logic.│  │   - config/                  │  │

    Features:

    - LLM-based reasoning loop (max 10 steps)"""│  │   - data/sessions/           │  │

    - Tool discovery and execution

    - Error recovery (continues on tool failures)│  │   - .cache/                  │  │

    - Cancellation support (via asyncio.CancelledError)

    - Context management (session history)import asyncio│  │   - logs/                    │  │

    

    Inherits from MCPServer to expose tools via MCP protocol.import logging│  └──────────────────────────────┘  │

    """

    from typing import AsyncGenerator, Optional, Any└─────────────────────────────────────┘

    def __init__(

        self,          │

        name: str,

        system_prompt: str,from agent_system.exceptions import (          ▼

        llm_profile: str,

        tools: list[str],    SessionNotFoundError,    Internet (LLM APIs, MCP Servers)

        session_service: 'SessionService',

        mcp_service: 'MCPService',    SessionPermissionError,```

        config: 'Config'

    ):    PluginLoadError,

        """

        Initialize agent.    LLMConnectionError,### 9.2 Reverse Proxy Deployment

        

        Args:    LLMRateLimitError,

            name: Agent name

            system_prompt: System/role prompt)```

            llm_profile: LLM configuration to use

            tools: Available tool namesInternet

            session_service: For session access

            mcp_service: For tool executionlogger = logging.getLogger(__name__)    │

            config: System config

        """    ▼

        super().__init__(name=name)

        ┌─────────────────┐

        self.system_prompt = system_prompt

        self.llm_profile = llm_profileclass AgentService:│  Nginx/Caddy    │  HTTPS Termination

        self.tools = tools

        self.session_service = session_service    """│  (Port 443)     │  Static Files

        self.mcp_service = mcp_service

        self.config = config    Service for agent execution orchestration.│  (Port 80)      │  Rate Limiting

    

    async def run_events(    └────────┬────────┘

        self,

        task: str,    Features:         │

        request_id: str,

        session_id: str,    - Agent instantiation from plugins         ▼

        output_format: str = "json",

        max_steps: int = 10    - Execution with error recovery┌─────────────────┐

    ) -> AsyncGenerator[dict[str, Any], None]:

        """    - Retry logic for transient failures (LLM connection errors)│  AgentSystem    │  HTTP

        Execute agent reasoning loop.

            - Status streaming via async generators│  (Port 8000)    │  Internal Only

        Flow:

        1. Load session history    - Request cancellation support└─────────────────┘

        2. Call LLM with task + history

        3. Parse response for tool calls    - Proper session management```

        4. If tool calls: execute and repeat

        5. If no tool calls: return final answer    

        6. Max steps: return partial result

            Thread Safety: Yes (async-safe with locks)### 9.3 Environment Variables

        Args:

            task: User's task/question    """

            request_id: Unique request ID

            session_id: Session for context    ```bash

            output_format: Output format (json/html/markdown)

            max_steps: Max reasoning iterations    def __init__(# Required

        

        Yields:        self,OPENAI_API_KEY=sk-...

            Status events and results

                session_service: 'SessionService',ANTHROPIC_API_KEY=sk-ant-...

        Raises:

            LLMConnectionError: LLM service unavailable        mcp_service: 'MCPService',

            LLMRateLimitError: Rate limit exceeded

            asyncio.CancelledError: Request cancelled        plugin_registry: 'PluginRegistry',# Optional

        """

        step = 0        config: 'Config'AGENT_LOG_LEVEL=info

        

        try:    ):AGENT_CACHE_DIR=/var/cache/agent_system

            while step < max_steps:

                step += 1        """AGENT_SESSION_DIR=/var/data/agent_system/sessions

                

                yield {        Initialize agent service.AGENT_CONFIG_PATH=/etc/agent_system/config.yaml

                    "type": "status",

                    "status": "running",        

                    "step": step,

                    "max_steps": max_steps        Args:# Development

                }

                            session_service: For session CRUD operationsAGENT_DEBUG=1

                # Call LLM

                response = await self._call_llm(task, session_id)            mcp_service: For MCP tool accessAGENT_RELOAD=1

                

                # Check for tool calls            plugin_registry: For agent discovery```

                tool_calls = response.get("tool_calls", [])

                            config: System configuration

                if not tool_calls:

                    # Final answer        """---

                    yield {

                        "type": "result",        self.session_service = session_service

                        "result": response.get("content"),

                        "step": step        self.mcp_service = mcp_service## 10. Related Documents

                    }

                    return        self.plugin_registry = plugin_registry

                

                # Execute tools        self.config = config### 10.1 Architecture Documents

                for tool_call in tool_calls:

                    yield {        

                        "type": "status",

                        "status": "tool_execution",        # Track active requests for cancellation- [Plugin Architecture](plugin_architecture.md) - Plugin system design

                        "tool": tool_call.get("name"),

                        "step": step        self._active_requests: dict[str, asyncio.Task] = {}- [MCP Server Integration](mcp_configuration.md) - External MCP servers

                    }

                        - [Hook System](plugin_hooks.md) - Lifecycle hooks

                    try:

                        result = await self._execute_tool(tool_call)    async def run_agent_stream(- [Tool Execution](tool_execution.md) - Tool execution flow

                        # Add result to context...

                    except ToolExecutionError as e:        self,

                        logger.warning("Tool failed: %s", e)

                        # Continue with error message        request_id: str,### 10.2 Design Documents

            

            # Max steps reached        agent_name: str,

            yield {

                "type": "status",        task: str,- [Configurable Agents](configurable_agents.md) - YAML-based agents

                "status": "max_steps_reached",

                "step": step        session_id: str,- [Session Management](session_management.md) - Multi-user sessions

            }

                user: str,- [Status System](status_design.md) - Real-time updates

        except asyncio.CancelledError:

            logger.info(        output_format: str = "json",- [Caching Systems](caching_systems.md) - Performance optimization

                "Agent cancelled: request_id=%s",

                request_id        cancellation_token: Optional[str] = None

            )

            raise    ) -> AsyncGenerator[dict[str, Any], None]:### 10.3 User Guides

        

        except Exception as e:        """

            logger.exception(

                "Agent failed: %s",        Execute agent and stream results.- [Plugin Authoring](plugin_authoring.md) - How to create plugins

                e,

                extra={"request_id": request_id}        - [Configuration Guide](../config/README.md) - Configuration reference

            )

            raise        Args:- [API Documentation](../README.md#api) - REST API reference

    

    async def _call_llm(            request_id: Unique request identifier- [CLI Reference](cli_reference.md) - Command-line usage

        self,

        task: str,            agent_name: Agent to execute

        session_id: str

    ) -> dict[str, Any]:            task: User's task/question---

        """

        Call LLM with task and context.            session_id: Session context

        

        Args:            user: Username (for authorization)**Document Changelog:**

            task: User task

            session_id: Session for history            output_format: Output format (json/html/markdown)

        

        Returns:            cancellation_token: Optional token for cancellation| Version | Date | Author | Changes |

            LLM response

                |---------|------|--------|---------|

        Raises:

            LLMConnectionError: Connection failed        Yields:| 1.0 | 2025-01-15 | AgentSystem Team | Initial comprehensive SAD |

            LLMRateLimitError: Rate limit exceeded

        """            Events in SSE format:

        # Load session history

        session = await self.session_service.load_session(session_id)            - {"type": "status", "status": "starting", ...}---

        messages = session.messages

                    - {"type": "status", "status": "running", "step": 1, ...}

        # Build prompt

        messages_with_task = messages + [            - {"type": "status", "status": "tool_execution", "tool": "...", ...}**Approval:**

            {"role": "user", "content": task}

        ]            - {"type": "status", "status": "retrying", "attempt": 1, ...}

        

        try:            - {"type": "result", "result": "...", ...}| Role | Name | Date | Signature |

            # Call LLM

            response = await self.llm_client.chat(            - {"type": "error", "error": "...", "message": "...", ...}|------|------|------|-----------|

                messages=messages_with_task,

                tools=self.tools        | Architect | - | - | - |

            )

                    Raises:| Tech Lead | - | - | - |

            return response

                    SessionNotFoundError: Session doesn't exist| Product Owner | - | - | - |

        except Exception as e:

            logger.error("LLM call failed: %s", e)            SessionPermissionError: User can't access session

            raise LLMConnectionError("openai", str(e))            PluginLoadError: Agent not found

                LLMConnectionError: LLM unavailable (after retries)

    async def _execute_tool(            LLMRateLimitError: Rate limit exceeded

        self,        """

        tool_call: 'ToolCall'        # Load and validate session

    ) -> Any:        session = await self.session_service.load_session(session_id)

        """Execute tool via MCP service."""        

        try:        if session.user != user:

            return await self.mcp_service.call_tool(            raise SessionPermissionError(session_id, user)

                tool_name=tool_call.name,        

                arguments=tool_call.arguments        # Create agent instance

            )        agent = await self._create_agent(agent_name, session_id)

        except Exception as e:        

            raise ToolExecutionError(tool_call.name, str(e))        # Track request for cancellation

```        current_task = asyncio.current_task()

        if current_task:

---            self._active_requests[request_id] = current_task

        

## 5. Resource Management        try:

            yield {

### 5.1 File Handling Patterns                "type": "status",

                "status": "starting",

```python                "request_id": request_id,

# ===== ✅ CORRECT: Context Manager =====                "agent": agent_name

            }

def load_config(path: Path) -> dict:            

    """Load config with automatic cleanup."""            # Execute with retry logic

    with open(path, 'r', encoding='utf-8') as f:            async for event in self._execute_with_retry(

        return json.load(f)                agent=agent,

                task=task,

# ===== ❌ WRONG: Manual Close =====                request_id=request_id,

                session_id=session_id,

def load_config_wrong(path: Path) -> dict:                output_format=output_format,

    """BAD: File might not close on exception!"""                max_retries=3

    f = open(path, 'r')            ):

    data = json.load(f)  # Exception here = file stays open!                yield event

    f.close()        

    return data        except asyncio.CancelledError:

```            logger.info("Request cancelled: %s", request_id)

            yield {

### 5.2 Async Task Cleanup                "type": "status",

                "status": "cancelled",

```python                "request_id": request_id

# ===== ✅ CORRECT: Proper Cleanup =====            }

            raise

async def background_task():        

    """Execute background work with cleanup."""        except LLMRateLimitError as e:

    task = asyncio.create_task(do_work())            logger.warning(

                    "Rate limit exceeded: provider=%s, retry_after=%d",

    try:                e.provider, e.retry_after

        result = await task            )

        return result            yield {

    except asyncio.CancelledError:                "type": "error",

        logger.info("Task cancelled")                "error": "RateLimitError",

        raise                "message": str(e),

    finally:                "retry_after": e.retry_after,

        # Ensure task is cleaned up                "retryable": True

        if not task.done():            }

            task.cancel()            raise

        

# ===== ❌ WRONG: Task Leak =====        except LLMConnectionError as e:

            logger.error("LLM connection failed: %s", e)

async def background_task_wrong():            yield {

    """BAD: Task never awaited or cancelled!"""                "type": "error",

    task = asyncio.create_task(do_work())                "error": "LLMConnectionError",

    # Task leaks if function returns early!                "message": "AI service temporarily unavailable. Please try again.",

```                "retryable": True

            }

### 5.3 Connection Pooling            raise

        

```python        finally:

class MCPService:            # Always clean up

    """MCP service with connection pooling and cleanup."""            self._active_requests.pop(request_id, None)

                

    def __init__(self, max_connections: int = 10):            # Save session (best-effort - don't fail request)

        self._pool: dict[str, MCPClient] = {}            try:

        self._locks: dict[str, asyncio.Lock] = {}                await self.session_service.save_session(session)

                except Exception as e:

    async def get_client(self, server_name: str) -> MCPClient:                logger.error(

        """Get or create MCP client with pooling."""                    "Failed to save session: %s",

        lock = self._locks.setdefault(server_name, asyncio.Lock())                    e,

                            extra={

        async with lock:                        "session_id": session_id,

            if server_name not in self._pool:                        "user": user,

                self._pool[server_name] = await MCPClient.connect(server_name)                        "request_id": request_id

                                }

            return self._pool[server_name]                )

                    # Don't raise - session save is best-effort

    async def close_all(self) -> None:    

        """Close all connections (called at shutdown)."""    async def cancel_request(self, request_id: str) -> bool:

        for client in self._pool.values():        """

            try:        Cancel an active request.

                await client.close()        

            except Exception as e:        Args:

                logger.error("Failed to close client: %s", e)            request_id: Request to cancel

                

        self._pool.clear()        Returns:

        self._locks.clear()            True if cancelled, False if not found or already done

```        """

        task = self._active_requests.get(request_id)

---        

        if task and not task.done():

## 6. API Contracts            task.cancel()

            logger.info("Cancelled request: %s", request_id)

### 6.1 Request/Response Models            return True

        

**File:** `src/agent_system/models.py`        return False

    

```python    async def list_agents(self) -> list[str]:

"""Pydantic models for API contracts."""        """

        List available agents.

from pydantic import BaseModel, Field, field_validator        

from typing import Optional, Any, Literal        Returns:

from datetime import datetime            List of agent names

        """

class ChatRequest(BaseModel):        return list(self.plugin_registry.list_agents())

    """Chat request model."""    

        # ===== Private Methods =====

    task: str = Field(..., min_length=1, max_length=10000)    

    agent: str = Field(default="general")    async def _create_agent(

    session_id: Optional[str] = None        self,

    output_format: Literal["json", "html", "markdown"] = "json"        agent_name: str,

    stream: bool = True        session_id: str

    ) -> 'Agent':

class Message(BaseModel):        """

    """Chat message model."""        Create agent instance from plugin registry.

            

    role: Literal["user", "assistant", "system"]        Args:

    content: str            agent_name: Agent to create

    timestamp: datetime = Field(default_factory=datetime.utcnow)            session_id: Session context

    tool_calls: Optional[list[dict[str, Any]]] = None        

        Returns:

class Session(BaseModel):            Agent instance

    """Session model with validation."""        

            Raises:

    id: str = Field(..., pattern=r'^[a-zA-Z0-9_-]+$')            PluginLoadError: Agent not found or failed to initialize

    user: str        """

    agent: str        try:

    messages: list[Message] = Field(default_factory=list)            factory = self.plugin_registry.get_agent_factory(agent_name)

    created_at: datetime = Field(default_factory=datetime.utcnow)            agent = await factory.create(session_id=session_id)

    updated_at: datetime = Field(default_factory=datetime.utcnow)            return agent

    metadata: dict[str, Any] = Field(default_factory=dict)        

            except KeyError:

    @field_validator('messages')            raise PluginLoadError(

    @classmethod                agent_name,

    def validate_messages(cls, v: list[Message]) -> list[Message]:                f"Agent '{agent_name}' not found in registry"

        """Ensure messages alternate roles properly."""            )

        for i in range(len(v) - 1):        

            if v[i].role == v[i+1].role and v[i].role != "system":        except Exception as e:

                raise ValueError("Messages must alternate between user and assistant")            logger.exception("Failed to create agent: %s", e)

        return v            raise PluginLoadError(

                agent_name,

class ErrorResponse(BaseModel):                f"Agent initialization failed: {e}"

    """Error response model."""            )

        

    error: str    async def _execute_with_retry(

    message: str        self,

    path: str        agent: 'Agent',

    timestamp: str        task: str,

    request_id: Optional[str] = None        request_id: str,

    details: Optional[dict[str, Any]] = None        session_id: str,

    retryable: bool = False        output_format: str,

```        max_retries: int = 3

    ) -> AsyncGenerator[dict[str, Any], None]:

---        """

        Execute agent with retry on transient errors.

## 7. Data Flow        

        Retries on:

### 7.1 Agent Execution Flow        - LLMConnectionError (network issues)

        

```        Does NOT retry on:

User Request (HTTP/CLI)        - ValidationError (bad input)

         │        - SessionPermissionError (auth)

         ▼        - LLMRateLimitError (let caller handle)

   FastAPI Endpoint        

         │        Uses exponential backoff: 1s, 2s, 4s, 8s...

         ▼        """

   Agent Service        retry_count = 0

         │        backoff = 1.0  # Initial backoff in seconds

         ├─► Status Bus (emit "starting")        

         │        while retry_count <= max_retries:

         ▼            try:

   Agent.run_events()                # Execute agent

         │                async for event in agent.run_events(

         ├─► Load Session History                    task=task,

         ├─► Hook: pre_llm_call                    request_id=request_id,

         ├─► LLM Request                    session_id=session_id,

         ├─► Hook: post_llm_call                    output_format=output_format

         │                ):

         ├─► Parse Tool Calls                    yield event

         │      │                

         │      ▼                # Success - exit retry loop

         │   Tool Execution Manager                return

         │      │            

         │      ├─► Hook: pre_tool_call            except LLMConnectionError as e:

         │      ├─► Execute Tool (Plugin/MCP)                retry_count += 1

         │      ├─► Hook: post_tool_call                

         │      ▼                if retry_count > max_retries:

         │   Tool Results                    logger.error(

         │                        "Max retries exceeded for LLM connection: %s",

         ├─► Repeat until complete                        e,

         │                        extra={

         ▼                            "request_id": request_id,

   Hook: format_output                            "attempts": retry_count

         │                        }

         ▼                    )

   Save Session                    raise

         │                

         ▼                # Log retry

   Return Result (Stream/JSON)                logger.warning(

```                    "LLM connection failed (attempt %d/%d): %s",

                    retry_count, max_retries, e,

### 7.2 Configuration Loading                    extra={"request_id": request_id}

                )

```                

Startup                # Exponential backoff

   │                await asyncio.sleep(backoff)

   ▼                backoff *= 2

Load config/config.yaml                

   │                # Yield retry status

   ├─► Resolve includes (llm.yaml, etc.)                yield {

   ├─► Substitute environment variables                    "type": "status",

   ├─► Validate with Pydantic schemas                    "status": "retrying",

   │                    "attempt": retry_count,

   ▼                    "max_attempts": max_retries,

Initialize Components                    "message": f"Connection failed. Retrying in {backoff}s...",

   │                    "backoff": backoff

   ├─► LLM Clients (from llm.yaml)                }

   ├─► MCP Integration (from mcp_servers.yaml)```

   ├─► Plugin Registry (discover + config agents)

   ├─► Hook System (load hooks from plugins)### 3.2 SessionService

   │

   ▼**File:** `src/agent_system/services/session_service.py`

Ready for Requests

``````python

"""

### 7.3 Plugin DiscoverySession service with safe file operations.

"""

```

System Startupimport asyncio

   │import json

   ▼import logging

Filesystem Discoveryfrom pathlib import Path

   │from typing import Optional

   ├─► Scan src/plugins/*/plugin.py

   ├─► Load plugin.yaml metadatafrom agent_system.exceptions import (

   ├─► Register factories in registry    SessionNotFoundError,

   │    ValidationError

   ▼)

Config-Based Agent Discoveryfrom agent_system.models import Session

   │

   ├─► Load agents.yamllogger = logging.getLogger(__name__)

   ├─► Validate agent definitions

   ├─► Create factories dynamicallyclass SessionService:

   ├─► Register alongside plugins    """

   │    Service for session CRUD with proper file handling.

   ▼    

Bootstrap Agents    Features:

   │    - ✅ Context managers (no resource leaks)

   ├─► Instantiate from factories    - ✅ File locking (no corruption on concurrent writes)

   ├─► Apply configuration overrides    - ✅ Atomic writes (temp file + rename)

   ├─► Register in MCP registry    - ✅ Async locks (no race conditions)

   │    - ✅ Proper error handling

   ▼    

Ready    Thread Safety: Yes (per-session async locks)

```    """

    

---    def __init__(self, data_dir: Path):

        """

## 8. Quality Attributes        Initialize session service.

        

### 8.1 Performance        Args:

            data_dir: Root data directory

| Metric | Target | Current | Notes |        """

|--------|--------|---------|-------|        self.data_dir = data_dir

| **Agent Response Time** | < 30s | ~10-20s | Depends on LLM latency |        self.sessions_dir = data_dir / "sessions"

| **Tool Execution** | Parallel | Parallel | asyncio.gather() |        

| **Concurrent Users** | 50+ | Tested: 20 | Limited by LLM rate limits |        # Per-session async locks for write operations

| **Session Load Time** | < 100ms | ~50ms | JSON file I/O |        self._write_locks: dict[str, asyncio.Lock] = {}

| **Tool Cache Hit Rate** | > 80% | ~85% | 30s TTL |    

    async def load_session(self, session_id: str) -> Session:

### 8.2 Reliability        """

        Load session from disk.

| Aspect | Implementation | Status |        

|--------|---------------|--------|        Args:

| **Error Handling** | Try/catch, graceful degradation | ✅ Implemented |            session_id: Session identifier

| **Cancellation** | Token-based, cooperative | ✅ Implemented |        

| **Retries** | Exponential backoff for LLM/MCP | ✅ Implemented |        Returns:

| **Validation** | Pydantic models, schema checks | ✅ Implemented |            Session object

| **Logging** | Structured logging, levels | ✅ Implemented |        

        Raises:

### 8.3 Security            SessionNotFoundError: Session doesn't exist

            ValidationError: Session file corrupted

| Feature | Status | Notes |        """

|---------|--------|-------|        path = self._get_session_path(session_id)

| **Authentication** | ✅ JWT + API Keys | Optional, configurable |        

| **Authorization** | ✅ User-based sessions | Per-user isolation |        if not path.exists():

| **Input Validation** | ✅ Pydantic models | All API inputs validated |            raise SessionNotFoundError(session_id)

| **Rate Limiting** | ✅ Token bucket | Configurable per-user |        

| **CORS** | ✅ Configurable | Default: localhost only |        try:

| **Secrets Management** | ✅ Env vars | No secrets in config files |            # ✅ Use context manager - file always closed

            with open(path, 'r', encoding='utf-8') as f:

### 8.4 Maintainability                data = json.load(f)

            

| Aspect | Score | Notes |            # Validate with Pydantic

|--------|-------|-------|            return Session(**data)

| **Code Coverage** | 75% | Target: 80% |        

| **Documentation** | Good | SAD, API docs, user guides |        except json.JSONDecodeError as e:

| **Modularity** | Excellent | Clear component boundaries |            logger.error(

| **Type Safety** | Good | Pydantic + type hints |                "Corrupted session file: %s",

| **Code Quality** | Good | Ruff, Mypy checks |                path,

                extra={"session_id": session_id, "error": str(e)}

---            )

            raise ValidationError(

## 9. Technology Stack                f"Session file corrupted: {session_id}",

                field="json"

### 9.1 Core Stack            )

        

```yaml        except IOError as e:

runtime:            logger.error(

  language: Python 3.11+                "Failed to read session file: %s",

  framework: FastAPI 0.109+                e,

  server: Uvicorn                extra={"session_id": session_id}

            )

dependencies:            raise SessionNotFoundError(session_id)

  web:    

    - fastapi    async def save_session(self, session: Session) -> None:

    - uvicorn[standard]        """

    - pydantic >= 2.0        Save session to disk with locking.

    - python-multipart        

          Strategy:

  llm:        1. Acquire per-session async lock

    - litellm        2. Write to temp file (.tmp)

    - openai        3. Atomic rename to final file

    - anthropic        4. Release lock

          

  data:        Args:

    - pyyaml            session: Session to save

    - jinja2        

    - jsonschema        Raises:

              IOError: Disk write failed

  utilities:        """

    - httpx        # Get or create lock for this session

    - python-jose[cryptography]        lock = self._write_locks.setdefault(

    - passlib[bcrypt]            session.id,

```            asyncio.Lock()

        )

### 9.2 Development Stack        

        async with lock:

```yaml            # Only one write at a time per session

development:            path = self._get_session_path(session.id)

  testing:            path.parent.mkdir(parents=True, exist_ok=True)

    - pytest            

    - pytest-asyncio            # Write to temp file first (atomic operation)

    - pytest-cov            temp_path = path.with_suffix('.tmp')

              

  code_quality:            try:

    - ruff                # ✅ Use context manager

    - mypy                with open(temp_path, 'w', encoding='utf-8') as f:

    - black                    json.dump(

                          session.dict(),

  documentation:                        f,

    - mkdocs                        indent=2,

    - mkdocs-material                        ensure_ascii=False

```                    )

                

---                # Atomic rename (POSIX: atomic, Windows: best-effort)

                temp_path.replace(path)

## 10. Related Documents                

                logger.debug(

### 10.1 Architecture Documents                    "Saved session: %s",

                    session.id,

- [Plugin Architecture](plugin_architecture.md) - Plugin system design                    extra={"user": session.user}

- [MCP Server Integration](mcp_configuration.md) - External MCP servers                )

- [Hook System](plugin_hooks.md) - Lifecycle hooks            

- [Tool Execution](tool_execution.md) - Tool execution flow            except IOError as e:

                logger.error(

### 10.2 Design Documents                    "Failed to save session: %s",

                    e,

- [Configurable Agents](configurable_agents.md) - YAML-based agents                    extra={"session_id": session.id}

- [Session Management](session_management.md) - Multi-user sessions                )

- [Status System](status_design.md) - Real-time updates                

- [Caching Systems](caching_systems.md) - Performance optimization                # Clean up temp file

                if temp_path.exists():

### 10.3 User Guides                    try:

                        temp_path.unlink()

- [Plugin Authoring](plugin_authoring.md) - How to create plugins                    except Exception:

- [Configuration Guide](../config/README.md) - Configuration reference                        pass  # Best effort

- [API Documentation](../README.md#api) - REST API reference                

- [CLI Reference](cli_reference.md) - Command-line usage                raise

    

---    async def delete_session(self, session_id: str) -> None:

        """

**Document Changelog:**        Delete a session.

        

| Version | Date | Author | Changes |        Args:

|---------|------|--------|---------|            session_id: Session to delete

| 1.1 | 2025-10-15 | AgentSystem Team | Compact format, v1.1 target state |        

        Raises:

---            SessionNotFoundError: Session doesn't exist

        """

**Approval:**        lock = self._write_locks.setdefault(

            session_id,

| Role | Name | Date | Signature |            asyncio.Lock()

|------|------|------|-----------|        )

| Architect | - | - | - |        

| Tech Lead | - | - | - |        async with lock:

| Product Owner | - | - | - |            path = self._get_session_path(session_id)

            
            if not path.exists():
                raise SessionNotFoundError(session_id)
            
            try:
                path.unlink()
                logger.info("Deleted session: %s", session_id)
                
                # Clean up lock
                self._write_locks.pop(session_id, None)
            
            except IOError as e:
                logger.error(
                    "Failed to delete session: %s",
                    e,
                    extra={"session_id": session_id}
                )
                raise
    
    async def list_sessions(self, user: str) -> list[Session]:
        """
        List all sessions for a user.
        
        Args:
            user: Username
        
        Returns:
            List of sessions (sorted by updated_at desc)
        """
        user_dir = self.sessions_dir / user
        
        if not user_dir.exists():
            return []
        
        sessions = []
        
        for path in user_dir.glob("*.json"):
            try:
                session = await self.load_session(path.stem)
                sessions.append(session)
            except Exception as e:
                logger.warning(
                    "Skipping corrupted session: %s",
                    path,
                    extra={"error": str(e)}
                )
        
        # Sort by updated_at descending
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        
        return sessions
    
    def _get_session_path(self, session_id: str) -> Path:
        """
        Get file path for session.
        
        Session files stored as:
        data/sessions/<user>/<session_id>.json
        
        Args:
            session_id: Session ID (format: user_timestamp)
        
        Returns:
            Path to session file
        """
        # Extract user from session_id (format: user_timestamp)
        user = session_id.split('_')[0]
        return self.sessions_dir / user / f"{session_id}.json"
```

---

## 4. Agent Architecture

### 4.1 Agent Class

**File:** `src/agent_system/servers/agent/server.py`

```python
"""
Core agent implementation with reasoning loop.
"""

import asyncio
import logging
from typing import AsyncGenerator, Any, Optional

from agent_system.exceptions import (
    LLMConnectionError,
    LLMRateLimitError,
    ToolExecutionError
)
from agent_system.mcp.server import MCPServer

logger = logging.getLogger(__name__)

class Agent(MCPServer):
    """
    Core agent with multi-step reasoning.
    
    Features:
    - LLM-based reasoning loop (max 10 steps)
    - Tool discovery and execution
    - Error recovery (continues on tool failures)
    - Cancellation support (via asyncio.CancelledError)
    - Context management (session history)
    
    Inherits from MCPServer to expose tools via MCP protocol.
    """
    
    def __init__(
        self,
        name: str,
        system_prompt: str,
        llm_profile: str,
        tools: list[str],
        session_service: 'SessionService',
        mcp_service: 'MCPService',
        config: 'Config'
    ):
        """
        Initialize agent.
        
        Args:
            name: Agent name
            system_prompt: System/role prompt
            llm_profile: LLM configuration to use
            tools: Available tool names
            session_service: For session access
            mcp_service: For tool execution
            config: System config
        """
        super().__init__(name=name)
        
        self.system_prompt = system_prompt
        self.llm_profile = llm_profile
        self.tools = tools
        self.session_service = session_service
        self.mcp_service = mcp_service
        self.config = config
    
    async def run_events(
        self,
        task: str,
        request_id: str,
        session_id: str,
        output_format: str = "json",
        max_steps: int = 10
    ) -> AsyncGenerator[dict[str, Any], None]:
        """
        Execute agent reasoning loop.
        
        Flow:
        1. Load session history
        2. Call LLM with task + history
        3. Parse response for tool calls
        4. If tool calls: execute and repeat
        5. If no tool calls: return final answer
        6. Max steps: return partial result
        
        Args:
            task: User's task/question
            request_id: Unique request ID
            session_id: Session for context
            output_format: Output format (json/html/markdown)
            max_steps: Max reasoning iterations
        
        Yields:
            Status events and results
        
        Raises:
            LLMConnectionError: LLM service unavailable
            LLMRateLimitError: Rate limit exceeded
            asyncio.CancelledError: Request cancelled
        """
        step = 0
        
        try:
            while step < max_steps:
                step += 1
                
                yield {
                    "type": "status",
                    "status": "running",
                    "step": step,
                    "max_steps": max_steps,
                    "message": f"Reasoning step {step}/{max_steps}..."
                }
                
                # Call LLM (may raise LLMConnectionError or LLMRateLimitError)
                response = await self._call_llm(task, session_id)
                
                # Parse tool calls
                tool_calls = self._parse_tool_calls(response)
                
                if not tool_calls:
                    # Final answer - done!
                    result = self._extract_answer(response, output_format)
                    
                    yield {
                        "type": "result",
                        "result": result,
                        "request_id": request_id,
                        "steps": step
                    }
                    return
                
                # Execute tools
                for tool_call in tool_calls:
                    yield {
                        "type": "status",
                        "status": "tool_execution",
                        "tool": tool_call.name,
                        "step": step,
                        "message": f"Executing tool: {tool_call.name}"
                    }
                    
                    try:
                        tool_result = await self._execute_tool(tool_call)
                    
                    except ToolExecutionError as e:
                        # Tool failed - continue with error message
                        logger.warning(
                            "Tool execution failed: %s",
                            e,
                            extra={
                                "tool": tool_call.name,
                                "request_id": request_id
                            }
                        )
                        tool_result = f"Error: {e.reason}"
                    
                    # Add to context
                    await self._add_to_context(
                        session_id,
                        tool_call,
                        tool_result
                    )
            
            # Max steps reached
            yield {
                "type": "result",
                "result": "Max reasoning steps reached without final answer.",
                "request_id": request_id,
                "steps": step,
                "warning": "max_steps_exceeded"
            }
        
        except asyncio.CancelledError:
            logger.info(
                "Agent execution cancelled",
                extra={"request_id": request_id, "step": step}
            )
            raise
        
        except Exception as e:
            logger.exception(
                "Unexpected error in agent: %s",
                e,
                extra={"request_id": request_id, "step": step}
            )
            raise
    
    async def _call_llm(
        self,
        task: str,
        session_id: str
    ) -> dict[str, Any]:
        """
        Call LLM with task and context.
        
        Args:
            task: User task
            session_id: Session for history
        
        Returns:
            LLM response
        
        Raises:
            LLMConnectionError: Connection failed
            LLMRateLimitError: Rate limit exceeded
        """
        # Load session history
        session = await self.session_service.load_session(session_id)
        messages = session.messages
        
        # Build prompt
        messages_with_task = messages + [
            {"role": "user", "content": task}
        ]
        
        try:
            # Call LLM
            response = await self._llm_client.complete(
                model=self.llm_profile,
                messages=messages_with_task,
                tools=self._get_tool_schemas()
            )
            
            return response
        
        except ConnectionError as e:
            raise LLMConnectionError(
                provider=self.llm_profile.split('/')[0],
                reason=str(e)
            )
        
        except RateLimitException as e:
            raise LLMRateLimitError(
                provider=self.llm_profile.split('/')[0],
                retry_after=60
            )
    
    async def _execute_tool(
        self,
        tool_call: 'ToolCall'
    ) -> Any:
        """
        Execute tool via MCP service.
        
        Args:
            tool_call: Tool to execute
        
        Returns:
            Tool result
        
        Raises:
            ToolExecutionError: Tool failed
        """
        try:
            result = await self.mcp_service.execute_tool(
                server_name=tool_call.server,
                tool_name=tool_call.name,
                arguments=tool_call.arguments
            )
            return result
        
        except Exception as e:
            raise ToolExecutionError(
                tool_name=tool_call.name,
                reason=str(e)
            )
```

---

## 5. Resource Management

### 5.1 File Handling Patterns

```python
# ===== ✅ CORRECT: Context Manager =====

def load_config(path: Path) -> dict:
    """Load config with automatic cleanup."""
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

# ===== ❌ WRONG: Manual Close =====

def load_config_wrong(path: Path) -> dict:
    """BAD: File might not close on exception!"""
    f = open(path, 'r')
    data = json.load(f)  # Exception here = file stays open!
    f.close()
    return data
```

### 5.2 Async Task Cleanup

```python
# ===== ✅ CORRECT: Proper Cleanup =====

async def background_task():
    """Execute background work with cleanup."""
    task = asyncio.create_task(do_work())
    
    try:
        result = await task
        return result
    except asyncio.CancelledError:
        logger.info("Task cancelled")
        raise
    finally:
        # Ensure task is cleaned up
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

# ===== ❌ WRONG: Task Leak =====

async def background_task_wrong():
    """BAD: Task never awaited or cancelled!"""
    task = asyncio.create_task(do_work())
    # Task leaks if function returns early!
```

### 5.3 Connection Pooling

```python
class MCPService:
    """MCP service with connection pooling and cleanup."""
    
    def __init__(self):
        self._connections: dict[str, MCPClient] = {}
    
    async def connect_server(self, server_name: str) -> None:
        """Connect to MCP server (idempotent)."""
        if server_name in self._connections:
            return  # Already connected
        
        try:
            client = MCPClient(server_name)
            await client.connect()
            self._connections[server_name] = client
        
        except Exception as e:
            logger.error("Failed to connect to %s: %s", server_name, e)
            raise LLMConnectionError(server_name, str(e))
    
    async def close_all(self) -> None:
        """Close all connections (called on shutdown)."""
        for name, client in self._connections.items():
            try:
                await client.close()
            except Exception as e:
                logger.warning(
                    "Error closing connection to %s: %s",
                    name, e
                )
        
        self._connections.clear()
```

---

## 6. API Contracts

### 6.1 Request/Response Models

**File:** `src/agent_system/models.py`

```python
"""Pydantic models for API contracts."""

from pydantic import BaseModel, Field, field_validator
from typing import Optional, Any
from datetime import datetime

class ChatRequest(BaseModel):
    """Chat request model."""
    
    agent: str = Field(
        ...,
        description="Agent name to execute",
        min_length=1,
        max_length=100
    )
    
    message: str = Field(
        ...,
        description="User's task or question",
        min_length=1,
        max_length=10000
    )
    
    output_format: str = Field(
        default="json",
        description="Output format: json, html, or markdown"
    )
    
    cancellation_token: Optional[str] = Field(
        default=None,
        description="Optional token for request cancellation"
    )
    
    @field_validator('output_format')
    def validate_output_format(cls, v):
        allowed = ['json', 'html', 'markdown']
        if v not in allowed:
            raise ValueError(f"output_format must be one of: {allowed}")
        return v

class ChatResponse(BaseModel):
    """Chat response model."""
    
    result: str = Field(..., description="Agent's response")
    session_id: str = Field(..., description="Session identifier")
    request_id: str = Field(..., description="Request identifier")
    steps: int = Field(default=1, description="Reasoning steps taken")
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class ErrorResponse(BaseModel):
    """Error response model."""
    
    error: str = Field(..., description="Exception class name")
    message: str = Field(..., description="Human-readable message")
    path: str = Field(..., description="Request path")
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    request_id: Optional[str] = None
    retryable: bool = Field(default=False)
    details: Optional[dict[str, Any]] = None

class Session(BaseModel):
    """Session model."""
    
    id: str = Field(..., description="Session ID (user_timestamp)")
    user: str = Field(..., description="Username")
    messages: list[dict[str, str]] = Field(
        default_factory=list,
        description="Message history"
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata"
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
```

---

## 7. Data Flow

### 7.1 Request Processing Flow

```
HTTP Request
      │
      ▼
FastAPI Middleware
  ├─ Request ID generation
  ├─ CORS handling
  └─ JWT authentication
      │
      ▼
Endpoint Handler
  ├─ Parse request (Pydantic)
  ├─ Extract user
  └─ Call service
      │
      ▼
AgentService.run_agent_stream()
      │
      ├─► SessionService.load_session()
      │     ├─ SessionNotFoundError? → 404
      │     └─ SessionPermissionError? → 403
      │
      ├─► Create Agent
      │     └─ PluginLoadError? → 500
      │
      ├─► Execute Agent (with retry)
      │     ├─ LLMConnectionError? → Retry 3x → 503
      │     ├─ LLMRateLimitError? → 429
      │     └─ ToolExecutionError? → Continue (non-fatal)
      │
      └─► SessionService.save_session()
            └─ Error? → Log only (best-effort)
      │
      ▼
Exception Handler
  ├─ Map to HTTP status
  ├─ Log with context
  └─ Return JSON error
      │
      ▼
Response (SSE or JSON)
```

### 7.2 Session Save Flow (Atomic)

```
save_session(session)
      │
      ▼
Acquire async lock (per-session)
      │
      ▼
Create parent directories
      │
      ▼
Write to temp file (.tmp)
      │
      ├─ Success → Atomic rename → Release lock → ✅ Done
      │
      └─ Failure → Delete temp → Release lock → ❌ Raise IOError
```

---

## 8. Quality Attributes

### 8.1 Reliability

| Metric | Target | Implementation |
|--------|--------|----------------|
| **Uptime** | 99.9% | Error recovery, graceful degradation |
| **Error Rate** | < 1% | Retry logic, proper exceptions |
| **Data Loss** | 0% | File locking, atomic writes |
| **Resource Leaks** | 0 | Context managers, proper cleanup |

### 8.2 Performance

| Metric | Target | Current |
|--------|--------|---------|
| **API Latency (p50)** | < 50ms | ~30ms |
| **API Latency (p99)** | < 200ms | ~150ms |
| **Throughput** | 50+ req/s | 60+ req/s |
| **Memory** | < 512MB | ~300MB per worker |

### 8.3 Security

| Aspect | Implementation |
|--------|----------------|
| **Authentication** | JWT tokens (30min expiry) |
| **Authorization** | Session ownership validation |
| **Input Validation** | Pydantic schemas |
| **Error Messages** | No sensitive data leakage |
| **CORS** | Configurable allowed origins |

---

## 9. Technology Stack

### 9.1 Core Stack

```yaml
runtime:
  language: Python 3.11+
  framework: FastAPI 0.109+
  server: Uvicorn 0.27+ (with uvloop)

dependencies:
  web:
    - fastapi >= 0.109
    - uvicorn[standard] >= 0.27
    - pydantic >= 2.0
    - python-multipart
    
  auth:
    - python-jose[cryptography]
    - passlib[bcrypt]
  
  llm:
    - litellm >= 1.0
    - openai >= 1.0
    - anthropic >= 0.18
  
  data:
    - pyyaml >= 6.0
    - jinja2 >= 3.1
    - jsonschema >= 4.0
  
  utilities:
    - aiofiles  # Async file I/O
    - filelock  # File locking
    - structlog  # Structured logging (optional)

dev_dependencies:
  testing:
    - pytest >= 7.4
    - pytest-asyncio >= 0.21
    - pytest-cov >= 4.1
    - httpx  # Async HTTP client
  
  quality:
    - ruff >= 0.1
    - mypy >= 1.7
    - types-pyyaml
```

### 9.2 Type Checking (Mypy)

```toml
[tool.mypy]
python_version = "3.11"
strict = true
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true
disallow_any_generics = true
check_untyped_defs = true
no_implicit_reexport = true
warn_redundant_casts = true
warn_unused_ignores = true
warn_no_return = true
```

---

## 10. Related Documents

### Architecture Documents (SAD)
- `_sad_app_architecture.md` - FastAPI application layer
- `_sad_cli_architecture.md` - CLI tools
- `_sad_plugin_architecture.md` - Plugin system
- `_sad_external_mcpservers.md` - External MCP integration

### Requirements Documents (SRS)
- `_srs_mcpserver_and_agents.md` - MCP protocol requirements
- `_srs_pluginsystem.md` - Plugin system requirements
- `_srs_external_mcpservers.md` - External MCP requirements

### Implementation Guides
- `_ARCHITECTURE_EVOLUTION_v1.1.md` - v1.1 improvements details
- `_COMPLEXITY_REFACTORING_v1.1.md` - Refactoring plan
- `docs/plugin_authoring.md` - Plugin development guide
- `docs/error_handling.md` - Error handling patterns

---

**Document Version History:**
- v1.1 (2025-10-15): Target state after bug fixes and improvements
- v1.0 (2025-01-15): Initial architecture

**End of Document**
