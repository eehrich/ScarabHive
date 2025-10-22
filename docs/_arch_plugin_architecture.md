# Software Architecture Document: Plugin Architecture

**Document Type:** Software Architecture Document (SAD)  
**Component:** Plugin System & Extensibility  
**Version:** 1.1  
**Last Updated:** 2025-01-15  
**Status:** Active

---

## Table of Contents

1. [Overview](#overview)
2. [Architectural Goals](#architectural-goals)
3. [Plugin Types](#plugin-types)
4. [Component Architecture](#component-architecture)
5. [Plugin Discovery](#plugin-discovery)
6. [Hook System](#hook-system)
7. [Configuration-Based Agents](#configuration-based-agents)
8. [Key Design Decisions](#key-design-decisions)
9. [Data Flow](#data-flow)
10. [Best Practices](#best-practices)
11. [Related Documents](#related-documents)

---

## 1. Overview

### 1.1 Purpose

The Plugin System provides extensibility through:
- **Tool plugins** - Callable functions for agents (MCP protocol)
- **Hook plugins** - Lifecycle interception and modification
- **Hybrid plugins** - Combined tools and hooks
- **Config-based agents** - YAML-defined agents without code

### 1.2 Scope

This document covers:
- Plugin types and characteristics
- Discovery and registration mechanisms
- Hook system architecture
- Configuration-based agents
- Best practices for plugin development

### 1.3 Audience

- Plugin developers
- System architects
- Core maintainers
- Technical leads

---

## 2. Architectural Goals

### 2.1 Design Principles

| Principle | Description | Priority |
|-----------|-------------|----------|
| **Modularity** | Self-contained plugins, minimal core dependencies | High |
| **Discoverability** | Auto-discovery from filesystem + config | High |
| **Configurability** | Enable/disable/configure without code changes | High |
| **Type Safety** | Pydantic models for validation | High |
| **Minimal Coupling** | Well-defined interfaces, composition over inheritance | High |
| **Developer Experience** | Easy to author, test, debug plugins | Medium |

### 2.2 Quality Goals

- **Performance:** Plugin overhead < 10ms per operation
- **Reliability:** Plugin errors don't crash system (isolation)
- **Testability:** Plugins testable in isolation
- **Documentation:** Clear examples and authoring guides

### 2.3 Non-Goals

- Plugin versioning and dependency management
- Runtime plugin loading/unloading (startup only)
- Cross-plugin explicit dependencies (implicit tool deps allowed)
- Plugin marketplace or distribution system

---

## 3. Plugin Types

The plugin system supports **three distinct plugin types**:

### 3.1 Tool-Only Plugins (MCPServer)

**Purpose:** Provide callable tools for agents

**Base Class:** `MCPServer`

**Capabilities:**
- Expose tools via MCP protocol
- Tool discovery (`list_tools`)
- Tool execution (`call_tool`)

**Example:**
```python
from agent_system.mcp.base import MCPServer

class WebSearchPlugin(MCPServer):
    """Provides web search tools."""
    
    async def list_tools(self):
        return [
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
    
    async def call_tool(self, tool_name: str, arguments: dict):
        if tool_name == "web_search":
            query = arguments["query"]
            results = await self._perform_search(query)
            return {"results": results}
```

**Use Cases:**
- Web search, API calls
- Data fetching, file operations
- Calculations, conversions
- External service integration

---

### 3.2 Hook-Only Plugins (PluginHook)

**Purpose:** Intercept and modify agent lifecycle events

**Base Class:** `PluginHook`

**Capabilities:**
- Lifecycle event interception
- Message modification
- Context optimization
- Logging and monitoring

**Example:**
```python
from agent_system.hooks import PluginHook, HookContext, HookResult

class RequestLoggerPlugin(PluginHook):
    """Logs agent requests - NO tools provided."""
    
    def __init__(self, name: str, config: dict = None):
        super().__init__(name, config or {})
        self.request_count = 0
    
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        self.request_count += 1
        logger.info(f"Request #{self.request_count}")
        return HookResult(
            success=True,
            modified=False,
            context=context
        )
    
    async def on_format_output(self, context: HookContext) -> HookResult:
        # Format output as markdown
        context.result = f"**Response:**\n\n{context.result}"
        return HookResult(
            success=True,
            modified=True,
            context=context
        )
```

**Use Cases:**
- Logging, monitoring
- Context optimization
- Message validation
- Output formatting
- Token tracking

---

### 3.3 Hybrid Plugins (MCPServer + PluginHook)

**Purpose:** Provide both tools AND lifecycle hooks

**Base Classes:** `MCPServer, PluginHook` (multiple inheritance)

**Capabilities:**
- All tool plugin features
- All hook plugin features
- Coordinated tool + hook logic

**Example:**
```python
from agent_system.mcp.base import MCPServer
from agent_system.hooks import PluginHook, HookContext, HookResult

class EnhancedSearchPlugin(MCPServer, PluginHook):
    """Provides search tools + optimizes search queries."""
    
    # Tool functionality
    async def list_tools(self):
        return [{
            "name": "smart_search",
            "description": "AI-enhanced search"
        }]
    
    async def call_tool(self, tool_name: str, arguments: dict):
        query = arguments["query"]
        results = await self._search(query)
        return {"results": results}
    
    # Hook functionality
    async def on_pre_tool_call(self, context: HookContext) -> HookResult:
        # Optimize search queries before execution
        if context.tool_call.get("name") == "smart_search":
            original = context.tool_call["arguments"]["query"]
            optimized = self._optimize_query(original)
            context.tool_call["arguments"]["query"] = optimized
            return HookResult(
                success=True,
                modified=True,
                context=context
            )
        return HookResult(success=True, modified=False, context=context)
```

**Use Cases:**
- Tools that need lifecycle awareness
- Query/response optimization for specific tools
- Tool usage analytics

---

### 3.4 Plugin Type Selection Guide

| Scenario | Plugin Type | Base Class(es) |
|----------|-------------|----------------|
| Provide callable tools | Tool-Only | `MCPServer` |
| Log/monitor agent | Hook-Only | `PluginHook` |
| Optimize context | Hook-Only | `PluginHook` |
| Validate messages | Hook-Only | `PluginHook` |
| Format output | Hook-Only | `PluginHook` |
| Tools + monitor usage | Hybrid | `MCPServer, PluginHook` |
| Tools + optimize queries | Hybrid | `MCPServer, PluginHook` |

**Rule:** Choose the simplest base class that meets your needs. Don't inherit from `MCPServer` if you don't provide tools.

---

## 4. Component Architecture

### 4.1 Plugin System Components

```
┌─────────────────────────────────────────────────────────────────┐
│                      Plugin System                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌───────────────────┐         ┌───────────────────┐           │
│  │  Plugin Registry  │◄────────│ Plugin Discovery  │           │
│  │   (factories)     │         │   (filesystem)    │           │
│  └─────────┬─────────┘         └───────────────────┘           │
│            │                                                     │
│            │ instantiate                                         │
│            ▼                                                     │
│  ┌───────────────────────────────────────────────┐             │
│  │           Plugin Instances                     │             │
│  ├───────────────────────────────────────────────┤             │
│  │  Tool Plugins   │  Hook Plugins │ Hybrid      │             │
│  │  (MCPServer)    │  (PluginHook) │ (Both)      │             │
│  └───────────────────────────────────────────────┘             │
│            │                    │                                │
└────────────┼────────────────────┼────────────────────────────────┘
             │                    │
             ▼                    ▼
┌────────────────────┐   ┌────────────────────┐
│   MCP Registry     │   │   Hook Manager     │
│  (tool discovery)  │   │ (lifecycle events) │
└────────────────────┘   └────────────────────┘
```

### 4.2 Core Components

#### 4.2.1 Plugin Registry

**File:** `src/agent_system/plugins/registry.py`

**Responsibilities:**
- Plugin factory registration
- Plugin instantiation
- Metadata management
- Factory pattern

**Key Methods:**
```python
class PluginRegistry:
    def register_factory(
        self,
        name: str,
        factory: Callable,
        metadata: PluginMetadata
    ):
        """Register plugin factory"""
    
    def create_instance(
        self,
        name: str,
        config: dict = None
    ) -> Union[MCPServer, PluginHook]:
        """Create plugin instance from factory"""
    
    def list_plugins(self) -> List[PluginMetadata]:
        """List all registered plugins"""
```

#### 4.2.2 Plugin Discovery

**File:** `src/agent_system/plugins/__init__.py`

**Responsibilities:**
- Filesystem scanning (`src/plugins/*/plugin.py`)
- Metadata loading (`plugin.yaml`)
- Factory registration
- Config-based agent discovery

**Discovery Process:**
```python
def discover_all_plugins(config: AgentSystemConfig) -> PluginRegistry:
    """Discover and register all plugins"""
    
    registry = PluginRegistry()
    
    # 1. Discover filesystem plugins
    plugin_dirs = Path("src/plugins").iterdir()
    for plugin_dir in plugin_dirs:
        metadata = load_plugin_metadata(plugin_dir / "plugin.yaml")
        module = import_plugin_module(plugin_dir / "plugin.py")
        factory = extract_plugin_factory(module)
        registry.register_factory(
            name=metadata.name,
            factory=factory,
            metadata=metadata
        )
    
    # 2. Discover config-based agents
    for agent_name, agent_def in config.agents.items():
        factory = create_config_agent_factory(agent_def)
        registry.register_factory(
            name=agent_name,
            factory=factory,
            metadata=PluginMetadata(
                name=agent_name,
                type="config_agent",
                ...
            )
        )
    
    return registry
```

#### 4.2.3 Hook Manager

**File:** `src/agent_system/hooks/manager.py`

**Responsibilities:**
- Hook discovery from plugins
- Hook execution order management
- Hook error isolation
- Hook configuration

**Hook Lifecycle:**
```python
class HookManager:
    async def execute_hooks(
        self,
        hook_type: str,
        context: HookContext
    ) -> HookContext:
        """Execute all hooks of given type"""
        
        # Get hooks for this type
        hooks = self._get_hooks_for_type(hook_type)
        
        # Sort by order (after/before directives)
        sorted_hooks = self._sort_hooks(hooks)
        
        # Execute in order
        for hook in sorted_hooks:
            try:
                result = await hook.execute(context)
                if result.modified:
                    context = result.context
            except Exception as e:
                logger.error(f"Hook {hook.name} failed: {e}")
                # Continue with next hook (isolation)
        
        return context
```

---

## 5. Plugin Discovery

### 5.1 Filesystem-Based Discovery

**Structure:**
```
src/plugins/
├── basic_operations/
│   ├── plugin.py          # Plugin implementation
│   ├── plugin.yaml        # Metadata
│   ├── __init__.py
│   └── tests/
├── web_search/
│   ├── plugin.py
│   ├── plugin.yaml
│   └── __init__.py
└── context_optimizer/
    ├── plugin.py
    ├── plugin.yaml
    └── __init__.py
```

**plugin.yaml Example:**
```yaml
name: basic_operations
version: 1.0.0
type: mcp_only  # mcp_only | hooks_only | hybrid_with_hooks
description: Basic file and system operations

# Tool metadata (for mcp_only or hybrid)
tools:
  - name: read_file
    description: Read file contents
  - name: write_file
    description: Write to file

# Hook configuration (for hooks_only or hybrid)
hooks:
  - name: log_requests
    type: pre_llm_call
    enabled: true
    order:
      after: ["begin"]
      before: ["context_optimizer"]

# Plugin configuration
config:
  max_file_size: 10485760  # 10MB
  allowed_extensions: [".txt", ".md", ".json"]
```

### 5.2 Configuration-Based Discovery

**File:** `config/agents.yaml`

```yaml
agents:
  researcher:
    enabled: true
    base_type: agent
    llm_profile: gpt4
    system_template: prompts/researcher.md
    max_steps: 10
    tools:
      include:
        - web_search
        - calculator
    hooks:
      format_output:
        - markdown_formatter
    metadata:
      visibility: ui
      description: Research assistant
```

**How It Works:**
1. ConfigService loads `agents.yaml`
2. For each agent definition, a factory is created
3. Factory dynamically creates `Agent` instances with config
4. Registered in PluginRegistry alongside filesystem plugins

**Benefits:**
- No Python code needed for simple agents
- Easy to prototype and iterate
- Configuration-driven
- Coexists with plugin-based agents

---

## 6. Hook System

### 6.1 Hook Types

| Hook Type | When | Purpose |
|-----------|------|---------|
| `pre_llm_call` | Before LLM request | Modify messages, add context |
| `post_llm_call` | After LLM response | Extract metadata, log |
| `pre_tool_call` | Before tool execution | Validate args, optimize queries |
| `post_tool_call` | After tool execution | Transform results, cache |
| `format_output` | Before returning to user | Format result (MD, HTML, etc.) |
| `session_start` | Session begins | Initialize session state |
| `session_end` | Session ends | Cleanup, save state |

### 6.2 Hook Context

**Data Structure:**
```python
@dataclass
class HookContext:
    """Context passed to hooks"""
    
    # Common
    session_id: str
    request_id: str
    agent_name: str
    
    # LLM hooks
    messages: Optional[List[dict]] = None
    llm_response: Optional[str] = None
    
    # Tool hooks
    tool_call: Optional[dict] = None  # {"name": "...", "arguments": {...}}
    tool_result: Optional[dict] = None
    
    # Output hooks
    result: Optional[str] = None
    
    # Metadata
    metadata: dict = field(default_factory=dict)
```

### 6.3 Hook Result

```python
@dataclass
class HookResult:
    """Result from hook execution"""
    success: bool
    modified: bool  # Whether context was modified
    context: HookContext
    error: Optional[str] = None
```

### 6.4 Hook Ordering

**Directives:**
- `after: ["hook1", "hook2"]` - Execute after these hooks
- `before: ["hook3"]` - Execute before these hooks

**Example:**
```yaml
hooks:
  - name: token_counter
    type: pre_llm_call
    order:
      after: ["begin"]  # Special marker for start
      before: ["context_optimizer"]
  
  - name: context_optimizer
    type: pre_llm_call
    order:
      after: ["token_counter"]
      before: ["end"]  # Special marker for end
```

**Resolution:**
Topological sort based on dependencies.

---

## 7. Configuration-Based Agents

### 7.1 Architecture

Config-based agents are **dynamically created** from YAML definitions:

```yaml
# config/agents.yaml
agents:
  translator:
    enabled: true
    base_type: agent
    llm_profile: gpt3.5
    system_template: |
      You are a professional translator.
      Translate user requests accurately.
    max_steps: 5
    tools:
      exclude: ["*"]  # No tools needed
    metadata:
      visibility: ui
      description: Translation agent
```

**Factory Creation:**
```python
def create_config_agent_factory(agent_def: ConfigAgentDefinition):
    """Create factory for config-based agent"""
    
    def factory(name: str, config: dict = None):
        return Agent(
            name=name,
            llm_profile=agent_def.llm_profile,
            system_template=agent_def.system_template,
            max_steps=agent_def.max_steps,
            tool_filter=agent_def.tools,
            hooks_config=agent_def.hooks,
            metadata=agent_def.metadata
        )
    
    return factory
```

### 7.2 Benefits

| Benefit | Description |
|---------|-------------|
| **Low Barrier** | Non-developers can create agents |
| **Fast Iteration** | Edit YAML, restart, test |
| **No Code** | Prompts, tool filters, hooks in YAML |
| **Version Control** | YAML in git, easy diffs |
| **Validation** | Schema validation, error checking |

### 7.3 Limitations

| Limitation | Workaround |
|------------|------------|
| No custom logic | Use plugin-based agents |
| No complex tool filters | Use simple include/exclude |
| No custom hooks | Register hook plugins separately |

---

## 8. Key Design Decisions

### ADR-001: Composition Over Inheritance

**Context:** Need flexible plugin types  
**Decision:** Independent `MCPServer` and `PluginHook` base classes  
**Rationale:**
- Hook-only plugins don't need MCP methods
- Tool-only plugins don't need hook methods
- Hybrid plugins use multiple inheritance
- Clear separation of concerns

**Status:** Accepted

---

### ADR-002: Factory Pattern for Plugins

**Context:** Need plugin instantiation with config  
**Decision:** Register factories, not instances  
**Rationale:**
- Lazy instantiation
- Config injection at creation time
- Multiple instances of same plugin possible
- Easier testing (mock factories)

**Status:** Accepted

---

### ADR-003: Auto-Discovery from Filesystem

**Context:** Need plugin registration without manual steps  
**Decision:** Scan `src/plugins/` at startup  
**Rationale:**
- Developer-friendly (drop in folder)
- No registration boilerplate
- plugin.yaml for metadata
- Consistent with config-based agents

**Status:** Accepted

---

### ADR-004: Hook Isolation

**Context:** Hook errors shouldn't crash agent  
**Decision:** Try/catch around each hook, continue on error  
**Rationale:**
- Reliability (one bad hook doesn't break system)
- Plugin independence
- Error logging for debugging

**Status:** Accepted

---

### ADR-005: Config-Based Agents

**Context:** Many agents differ only in prompts/tools  
**Decision:** Support YAML-defined agents  
**Rationale:**
- Lower barrier for non-developers
- Faster prototyping
- Coexists with plugin-based agents
- No code duplication

**Status:** Accepted

---

## 9. Data Flow

### 9.1 Plugin Discovery Flow

```
System Startup
    │
    ▼
discover_all_plugins(config)
    │
    ├─► Scan src/plugins/ directories
    │   │
    │   ├─► Load plugin.yaml (metadata)
    │   ├─► Import plugin.py (implementation)
    │   ├─► Extract factory function
    │   ├─► Register in PluginRegistry
    │   │
    ├─► Load config/agents.yaml
    │   │
    │   ├─► For each agent definition
    │   ├─► Create dynamic factory
    │   ├─► Register in PluginRegistry
    │   │
    ▼
PluginRegistry (all plugins + config agents)
    │
    ▼
bootstrap_servers(config, registry)
    │
    ├─► Instantiate plugins from factories
    ├─► Register in MCPRegistry (for tools)
    ├─► Register hooks in HookManager
    │
    ▼
System Ready
```

### 9.2 Hook Execution Flow

```
Agent Execution
    │
    ▼
Pre-LLM Hook Point
    │
    ├─► HookManager.execute_hooks("pre_llm_call", context)
    │   │
    │   ├─► Get all pre_llm_call hooks
    │   ├─► Sort by order (topological sort)
    │   ├─► For each hook:
    │   │   ├─► await hook.on_pre_llm_call(context)
    │   │   ├─► If modified, update context
    │   │   ├─► If error, log and continue
    │   │
    │   ▼
    │   Updated Context
    │
    ▼
LLM Call (with modified context)
    │
    ▼
Post-LLM Hook Point
    │
    ├─► HookManager.execute_hooks("post_llm_call", context)
    │   │
    │   ├─► Execute hooks in order
    │   ├─► Update context if modified
    │   │
    │   ▼
    │   Updated Context
    │
    ▼
Continue Agent Loop
```

---

## 10. Best Practices

### 10.1 Plugin Development

**DO:**
- ✅ Use minimal base class (don't inherit `MCPServer` if you don't provide tools)
- ✅ Validate inputs with Pydantic models
- ✅ Handle errors gracefully (return error results, don't raise)
- ✅ Document tools in `plugin.yaml` metadata
- ✅ Write unit tests for plugin logic
- ✅ Use type hints everywhere

**DON'T:**
- ❌ Depend on other plugins explicitly
- ❌ Access global state directly
- ❌ Block async operations (use async/await)
- ❌ Raise exceptions from hooks (return error results)
- ❌ Modify core system state

### 10.2 Hook Development

**DO:**
- ✅ Return `HookResult` with `success=False` on errors
- ✅ Set `modified=True` only if you changed context
- ✅ Use `context.metadata` for passing data between hooks
- ✅ Make hooks idempotent (safe to run multiple times)
- ✅ Document expected context fields

**DON'T:**
- ❌ Assume other hooks ran before you (unless using `order`)
- ❌ Modify context without setting `modified=True`
- ❌ Raise exceptions (system catches but wastes resources)
- ❌ Have side effects beyond context modification

### 10.3 Configuration

**DO:**
- ✅ Use config-based agents for simple cases
- ✅ Provide good defaults in plugin config
- ✅ Validate config with Pydantic models
- ✅ Document config options in plugin.yaml

**DON'T:**
- ❌ Hardcode values (use config)
- ❌ Require complex config for simple plugins
- ❌ Change config structure without migration plan

---

## 11. Related Documents

### 11.1 Architecture Documents

- [System Architecture](agent_system_architecture.md) - Overall system
- [App Architecture](app_architecture.md) - FastAPI application
- [CLI Architecture](cli_architecture.md) - Command-line interface

### 11.2 Development Guides

- [Plugin Authoring](plugin_authoring.md) - How to create plugins
- [Plugin Hooks](plugin_hooks.md) - Hook system details
- [Configurable Agents](configurable_agents.md) - YAML-based agents

### 11.3 Design Documents

- [Hook System Design](plugin_hooks.md) - Lifecycle hooks
- [Tool Execution](tool_execution.md) - Tool discovery and execution
- [Configuration](../config/README.md) - Configuration system

---

**Document Changelog:**

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2025-01-14 | AgentSystem Team | Initial plugin architecture |
| 1.1 | 2025-01-15 | AgentSystem Team | Unified SAD template, expanded sections |

---

**Approval:**

| Role | Name | Date | Signature |
|------|------|------|-----------|
| Architect | - | - | - |
| Tech Lead | - | - | - |
| Product Owner | - | - | - |
